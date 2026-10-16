"""Import-only Direct adapter for the existing single-use native process pool.

No client, account, tool connection, history or model work exists before lease.
The authenticated admitted attempt supplies its complete fresh request once.
"""
from __future__ import annotations

import contextlib
import codecs
import hashlib
import hmac
import io
import json
import os
import select
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

# Import paths and OS locale only: no ambient account, model or broker settings.
IDLE_ENVIRONMENT_KEYS = frozenset({
    'HOME', 'PATH', 'SHELL', 'TERM', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE',
    'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'DYLD_LIBRARY_PATH', '__CF_USER_TEXT_ENCODING',
})
SECRET_ENVIRONMENT = 'XPERFECT_DIRECT_WARM_SECRET'


def idle_environment():
    return {key: value for key, value in os.environ.items() if key in IDLE_ENVIRONMENT_KEYS}


def environment_digest(environment):
    if set(environment) - IDLE_ENVIRONMENT_KEYS:
        raise ValueError('Idle Direct environment contains execution authority')
    return hashlib.sha256(json.dumps(environment, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def preload_sdk(profile):
    """Imports only; official clients and tools are created by execute after lease."""
    import importlib
    modules = ['pydantic_ai', 'pydantic_ai.messages', 'pydantic_ai.mcp', 'fastmcp']
    modules += (['pydantic_ai.models.anthropic', 'pydantic_ai.providers.anthropic']
                if profile == 'direct-anthropic' else
                ['pydantic_ai.models.openai', 'pydantic_ai.providers.openai'])
    for module in modules:
        importlib.import_module(module)


def validate_admitted_lease(path, *, worker_id, run_id, attempt_id, profile, model=None, server_record=None):
    from workers_projects_runtime.native_warm_pool import read_native_warm_lease, read_native_warm_record, process_start_identity, listener_owned_by
    validation_started = time.monotonic()
    lease = read_native_warm_lease(Path(path), worker_id=worker_id, run_id=run_id,
        attempt_id=attempt_id, profile=profile, model=model)
    record = read_native_warm_record(Path(lease.get('state_record') or ''))
    scope, identity = lease['scope'], lease['server']
    endpoint = urlsplit(lease.get('url') or '')
    if (lease.get('stop_confirmed') or endpoint.scheme != 'ws' or endpoint.hostname != '127.0.0.1'
            or not endpoint.port or record.get('phase') != 'leased'
            or record.get('lease_id') != lease['lease_id'] or record.get('scope') != scope
            or record.get('run_id') != run_id or record.get('attempt_id') != attempt_id
            or record.get('attempt_descriptor') != str(path) or record.get('child') != identity
            or record.get('secret') != lease.get('secret')
            or record.get('port') != endpoint.port):
        raise ValueError('Direct warm process does not match the admitted attempt')
    if server_record is None:
        if (process_start_identity(identity['pid']) != identity['process_start_identity']
                or not listener_owned_by(identity['pid'], endpoint.port)):
            raise ValueError('Direct warm listener identity changed')
    elif (server_record.get('phase') != 'booting'
            or not server_record.get('owner') or server_record.get('child') != identity
            or identity['pid'] != os.getpid() or identity['process_group'] != os.getpgrp()
            or server_record.get('owner') != record.get('owner')
            or server_record.get('scope') != scope or server_record.get('port') != endpoint.port
            or server_record.get('secret') != lease.get('secret')):
        # The already executing server cannot be PID-reused. Its own guarded
        # startup record anchors the fresh lease; the client verifies the OS
        # process/listener before authenticating to that exact server.
        raise ValueError('Direct server startup identity changed')
    lease['_validation_ms'] = (time.monotonic() - validation_started) * 1000
    return lease


def admitted_workspace(environment):
    """The host supervisor already declares the exact cold process workspace."""
    value = environment.get('GLASSHIVE_WORKSPACE_DIR')
    if not isinstance(value, str) or not value or not Path(value).is_absolute() or not Path(value).is_dir():
        raise ValueError('Direct admitted workspace is unavailable')
    return value


def start_direct_server(*, scope, static_environment, cwd, state_directory, stop_runtime, timeout=12):
    from workers_projects_runtime.native_warm_pool import start_native_warm_server
    from workers_projects_runtime.profile_registry import require_worker_profile
    from websockets.sync.client import connect
    if (require_worker_profile(scope.profile).native_transport != 'api'
            or scope.execution_mode != 'host'
            or environment_digest(static_environment) != scope.environment_digest):
        raise ValueError('Invalid import-only Direct warm scope')

    def command(port, record_path):
        return [sys.executable, str(Path(__file__).resolve()), '--serve', str(record_path)]

    def probe(server, deadline):
        with connect(server.url, additional_headers={'Authorization': 'Bearer ' + server.secret},
                     open_timeout=min(1, max(.05, deadline - time.monotonic())), close_timeout=0,
                     proxy=None) as connection:
            connection.send(json.dumps({'type': 'ready'}))
            result = json.loads(connection.recv(timeout=max(.05, deadline - time.monotonic())))
            if result != {'type': 'ready', 'profile': scope.profile, 'inference': False}:
                raise ValueError('Direct SDK readiness did not match the selected profile')

    return start_native_warm_server(command=command, probe=probe, cwd=cwd,
        static_environment=static_environment, secret_environment=SECRET_ENVIRONMENT,
        state_directory=state_directory, scope=scope, stop_runtime=stop_runtime, timeout=timeout)


class _EventWriter:
    def __init__(self, connection, stream='event'):
        self.connection, self.pending, self.stream = connection, '', stream
        self._pipe = None
        self._drain = None
        self._closing = threading.Event()
    def write(self, text):
        if self.stream == 'stderr':
            self.connection.send(json.dumps({'type': 'stderr', 'text': text}))
            return len(text)
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            if line:
                self.connection.send(json.dumps({'type': self.stream, 'line': line}))
        return len(text)
    def flush(self):
        pass
    def fileno(self):
        # Official stdio MCP transports pass stderr directly to subprocesses.
        # Their writes must reach the same live relay as Python diagnostics.
        if self.stream != 'stderr':
            raise io.UnsupportedOperation('Structured stdout has no raw descriptor')
        if self._pipe is None:
            self._pipe = os.pipe()
            self._drain = threading.Thread(target=self._drain_stderr, daemon=True)
            self._drain.start()
        return self._pipe[1]
    def _drain_stderr(self):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while True:
                readable, _, _ = select.select([self._pipe[0]], [], [], .05)
                if not readable:
                    if self._closing.is_set():
                        break
                    continue
                data = os.read(self._pipe[0], 65536)
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    self.write(text)
            tail = decoder.decode(b'', final=True)
            if tail:
                self.write(tail)
        finally:
            os.close(self._pipe[0])
    def close(self):
        if self._pipe is not None and not self._closing.is_set():
            os.close(self._pipe[1])
            self._closing.set()
            self._drain.join(timeout=.5)


def serve(record_path):
    from workers_projects_runtime.native_warm_pool import read_native_warm_record
    from websockets.sync.server import serve as websocket_serve
    record_path = Path(record_path)
    record = read_native_warm_record(record_path)
    scope, secret = record['scope'], os.environ.pop(SECRET_ENVIRONMENT)
    child = record.get('child') or {}
    if (record.get('phase') != 'booting' or child.get('pid') != os.getpid()
            or child.get('process_group') != os.getpgrp()
            or not str(child.get('process_start_identity') or '').startswith('ps-lstart:')
            or not record.get('owner') or not hmac.compare_digest(record.get('secret') or '', secret)):
        raise ValueError('Direct SDK process has no exact guarded startup identity')
    if set(os.environ) - IDLE_ENVIRONMENT_KEYS or environment_digest(dict(os.environ)) != scope['environment_digest']:
        raise ValueError('Direct idle interpreter inherited execution authority')
    preload_sdk(scope['profile'])
    # The retained executor is imported without running main or constructing a client.
    from workers_projects_runtime import direct_runner
    claimed = threading.Lock()
    finished = threading.Event()

    def handler(connection):
        if not hmac.compare_digest(connection.request.headers.get('Authorization', ''), 'Bearer ' + secret):
            connection.close(1008, 'Unauthorized')
            return
        message = json.loads(connection.recv(timeout=5))
        if message == {'type': 'ready'}:
            if claimed.locked():
                connection.close(1008, 'Already admitted')
                return
            connection.send(json.dumps({'type': 'ready', 'profile': scope['profile'], 'inference': False}))
            return
        if message.get('type') != 'execute' or not claimed.acquire(blocking=False):
            connection.close(1008, 'Already admitted or invalid request')
            return
        try:
            lease = validate_admitted_lease(message['lease'], worker_id=scope['worker_id'],
                run_id=message['run_id'], attempt_id=message['attempt_id'],
                profile=scope['profile'], model=scope['model'], server_record=record)
            request = message['request']
            if (lease['scope'] != scope or lease['server']['pid'] != os.getpid()
                    or request['run_id'] != message['run_id'] or request['model'] != scope['model']
                    or request['history_scope']['worker'] != scope['worker_id']
                    or request['history_scope']['tenant'] != scope['tenant_id']
                    or request['history_scope']['owner'] != scope['owner_id']
                    or 'direct-' + request['provider'] != scope['profile']
                    or not isinstance(message['environment'], dict)
                    or any(not isinstance(k, str) or not isinstance(v, str) for k, v in message['environment'].items())):
                raise ValueError('Direct request changed its admitted execution scope')
            # Relative stdio MCP commands inherit the same workspace as cold
            # execution. Resolve it only after admission, before exposing authority.
            workspace = admitted_workspace(message['environment'])
            os.chdir(workspace)
            # Fresh account/grants/context enter once, only after the exact lease check.
            os.environ.clear()
            os.environ.update(message['environment'])
            direct_runner.STARTED_AT = time.monotonic()
            code = 0
            with contextlib.closing(_EventWriter(connection, 'stderr')) as stderr_writer, \
                    contextlib.redirect_stdout(_EventWriter(connection)), contextlib.redirect_stderr(stderr_writer):
                old_stdin = sys.stdin
                try:
                    sys.stdin = io.StringIO(json.dumps(request))
                    direct_runner.emit({'type': 'direct.stage', 'stage': 'warm_process_attached',
                                        'lease_validation_ms': lease['_validation_ms']})
                    direct_runner.main()
                except SystemExit as exc:
                    code = int(exc.code or 0)
                finally:
                    sys.stdin = old_stdin
            connection.send(json.dumps({'type': 'done', 'code': code}))
        finally:
            # Single-use interpreter; secrets/history never return to idle capacity.
            finished.set()

    with websocket_serve(handler, '127.0.0.1', record['port'], max_size=None) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        finished.wait()
        server.shutdown()


def attach(path, worker_id, run_id, attempt_id, profile):
    from websockets.sync.client import connect
    request = json.loads(sys.stdin.read())
    lease = validate_admitted_lease(path, worker_id=worker_id, run_id=run_id,
        attempt_id=attempt_id, profile=profile, model=request['model'])
    print(json.dumps({'type': 'direct.stage', 'stage': 'warm_client_lease_verified',
                      'observed_at_ms': time.time() * 1000, 'duration_ms': lease['_validation_ms']}), flush=True)
    with connect(lease['url'], additional_headers={'Authorization': 'Bearer ' + lease['secret']},
                 open_timeout=2, close_timeout=1, max_size=None, proxy=None) as connection:
        connection.send(json.dumps({'type': 'execute', 'lease': path, 'run_id': run_id,
            'attempt_id': attempt_id, 'request': request, 'environment': dict(os.environ)}))
        for raw in connection:
            message = json.loads(raw)
            if message.get('type') == 'event':
                print(message['line'], flush=True)
            elif message.get('type') == 'stderr':
                sys.stderr.write(message['text'])
                sys.stderr.flush()
            elif message.get('type') == 'done':
                return int(message['code'])
            else:
                raise ValueError('Invalid Direct warm response')
    raise RuntimeError('Direct warm attempt ended without a terminal receipt')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--serve':
        serve(sys.argv[2])
    elif len(sys.argv) == 7 and sys.argv[1] == '--attach':
        raise SystemExit(attach(*sys.argv[2:]))
    else:
        raise SystemExit('Direct warm execution requires an exact private native lease')
