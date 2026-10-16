"""One existing pool, import-only readiness and exact single-use Direct leases."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from workers_projects_runtime.direct_runtime import DIRECT_PROVIDERS, DirectModelRuntime
from workers_projects_runtime.direct_native_warm import idle_environment, environment_digest, validate_admitted_lease
from workers_projects_runtime.native_warm_pool import NativeWarmConfig, read_native_warm_lease
from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime


def worker(tmp_path, profile='direct-xai', **changes):
    return {'worker_id': 'wrk_synthetic_warm', 'tenant_id': 'local', 'owner_id': 'synthetic-owner',
            'name': 'Synthetic warm', 'profile': profile, 'execution_mode': 'host',
            'model': DIRECT_PROVIDERS[profile]['model'], 'workspace_root': str(tmp_path / 'workspace'),
            'bootstrap_bundle_json': json.dumps({'run_mode': 'conversation'}), **changes}


def ready(runtime, w):
    # Target TTL must exceed the supported 12s startup budget and test wait.
    # Preserve a factory failure diagnostic: the pool deliberately fails closed.
    failures = []
    original_factory = runtime._start_native_warm_server
    def observed_factory(*args, **kwargs):
        try:
            return original_factory(*args, **kwargs)
        except Exception as exc:
            failures.append((type(exc).__name__, str(exc)))
            raise
    runtime._start_native_warm_server = observed_factory
    status = runtime.configure_native_warm(w, {'native_warm_servers': 1, 'native_warm_idle_seconds': 60})
    assert status['supported'] is True
    until = time.monotonic() + 15
    while time.monotonic() < until:
        status = runtime.native_warm_status(w, {'native_warm_servers': 1})
        if status['idle'] == 1:
            return runtime._warm_contexts[runtime._warm_owner(w)][0]
        if status['state'] == 'unavailable':
            break
        time.sleep(.01)
    pytest.fail('SDK interpreter did not become ready: ' + repr({'status': status, 'factory_failures': failures}))


def test_idle_environment_never_carries_account_or_attempt_authority(monkeypatch):
    monkeypatch.setenv('XAI_API_KEY', 'synthetic-secret')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-secret')
    monkeypatch.setenv('GLASSHIVE_CAPABILITY_BROKER_TOKEN', 'synthetic-secret')
    monkeypatch.setenv('GLASSHIVE_RUN_ID', 'synthetic-run')
    env = idle_environment()
    assert not any(k in env for k in ('XAI_API_KEY', 'OPENAI_API_KEY', 'GLASSHIVE_CAPABILITY_BROKER_TOKEN', 'GLASSHIVE_RUN_ID'))
    assert environment_digest(env)
    with pytest.raises(ValueError, match='authority'):
        environment_digest({**env, 'XAI_API_KEY': 'synthetic-secret'})


@pytest.mark.parametrize('profile', list(DIRECT_PROVIDERS))
def test_real_sdk_readiness_exact_lease_stop_and_secret_free_receipt(tmp_path, profile):
    runtime = DirectModelRuntime(profile, base_dir=str(tmp_path / 'state'))
    w = worker(tmp_path, profile)
    try:
        scope = ready(runtime, w)
        assert scope.profile == profile and scope.account_id == 'import-only-unbound'
        lease = runtime._warm_pool.claim(scope, run_id='synthetic-run', attempt_id='synthetic-attempt')
        assert lease is not None and lease.server.alive()
        descriptor = runtime._warm_descriptor_path(w['worker_id'], 'synthetic-run', 'synthetic-attempt')
        lease.write_descriptor(descriptor)
        loaded = validate_admitted_lease(descriptor, worker_id=w['worker_id'], run_id='synthetic-run',
            attempt_id='synthetic-attempt', profile=profile, model=w['model'])
        assert loaded['lease_id'] == lease.lease_id
        runtime._warm_leases[(w['worker_id'], 'synthetic-run', 'synthetic-attempt')] = lease
        runtime._disable_native_warm(w)
        assert runtime._stop_native_warm_descriptor(descriptor, worker_id=w['worker_id'],
            run_id='synthetic-run', attempt_id='synthetic-attempt') is True
        stopped = read_native_warm_lease(descriptor, worker_id=w['worker_id'], run_id='synthetic-run',
            attempt_id='synthetic-attempt', profile=profile)
        assert stopped['stop_confirmed'] is True and 'secret' not in stopped and 'url' not in stopped
        assert not lease.server.alive()
    finally:
        runtime.close_native_warm()


def test_real_warm_executor_receives_fresh_request_once_without_provider_call(tmp_path):
    runtime = DirectModelRuntime('direct-xai', base_dir=str(tmp_path / 'state'))
    w = worker(tmp_path)
    try:
        scope = ready(runtime, w)
        lease = runtime._warm_pool.claim(scope, run_id='synthetic-run', attempt_id='synthetic-attempt')
        descriptor = runtime._warm_descriptor_path(w['worker_id'], 'synthetic-run', 'synthetic-attempt')
        lease.write_descriptor(descriptor)
        request = {'provider': 'xai', 'model': w['model'], 'run_id': 'synthetic-run',
                   'history_scope': {'worker': w['worker_id'], 'tenant': 'local', 'owner': 'synthetic-owner'}}
        command = [sys.executable, str(Path(__file__).parents[1] / 'src/workers_projects_runtime/direct_native_warm.py'),
                   '--attach', str(descriptor), w['worker_id'], 'synthetic-run', 'synthetic-attempt', 'direct-xai']
        # No credentials: the real retained executor must fail at configuration,
        # before history, tools, client construction or provider HTTP.
        result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True,
                                env={**idle_environment(), 'GLASSHIVE_WORKSPACE_DIR': str(tmp_path)}, timeout=10)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        assert result.returncode == 1, result.stderr
        assert any(e.get('stage') == 'warm_process_attached' for e in events)
        assert events[-1]['type'] == 'direct.error' and events[-1]['stage'] == 'configuration'
        assert not any(e.get('stage') == 'provider_request_sent' for e in events)
        until = time.monotonic() + 3
        while lease.server.alive() and time.monotonic() < until:
            time.sleep(.01)
        assert not lease.server.alive()
        runtime._warm_pool.finish(lease)
    finally:
        runtime.close_native_warm()


@pytest.mark.parametrize('field,value', [('worker_id', 'another-worker'), ('run_id', 'another-run'),
    ('attempt_id', 'another-attempt'), ('profile', 'direct-openai'), ('model', 'another-model')])
def test_direct_lease_rejects_wrong_admitted_identity_before_attachment(tmp_path, monkeypatch, field, value):
    from workers_projects_runtime.native_warm_pool import _write_private
    descriptor = tmp_path / 'lease.json'
    tmp_path.chmod(0o700)
    _write_private(descriptor, {'version': 1, 'lease_id': 'synthetic-lease', 'run_id': 'run',
        'attempt_id': 'attempt', 'scope': {'worker_id': 'worker', 'profile': 'direct-xai',
            'execution_mode': 'host', 'model': 'grok-4.7'},
        'server': {'pid': 123, 'process_group': 123, 'process_start_identity': 'ps-lstart:synthetic'}})
    expected = dict(worker_id='worker', run_id='run', attempt_id='attempt', profile='direct-xai', model='grok-4.7')
    expected[field] = value
    with pytest.raises(ValueError, match='admitted attempt'):
        read_native_warm_lease(descriptor, **expected)


def test_no_ready_lease_keeps_exact_cold_command_and_scope_changes_invalidate(tmp_path, monkeypatch):
    runtime = DirectModelRuntime('direct-xai', base_dir=str(tmp_path / 'state'))
    w = worker(tmp_path)
    command = ['exact-cold-command']
    assert runtime._prepare_native_warm_command(w, command, run_root=tmp_path) is command
    first = runtime._warm_context(w)[0]
    assert first != runtime._warm_context({**w, 'worker_id': 'another-worker'})[0]
    assert first != runtime._warm_context({**w, 'owner_id': 'another-owner'})[0]
    assert first != runtime._warm_context({**w, 'model': 'another-model'})[0]
    runtime.close_native_warm()


def test_profiled_adapters_share_one_existing_aggregate_capacity_owner(tmp_path):
    runtime = ProfiledWorkerRuntime(str(tmp_path / 'state'))
    try:
        pools = [adapter._native_warm_pool_factory() for adapter in runtime._native_warm_adapters()]
        assert all(pool is pools[0] for pool in pools)
        assert pools[0].global_limit == 16 and pools[0].owner_limit == 8
        assert len(runtime._native_warm_adapters()) == 4
    finally:
        runtime.close_native_warm()


def test_relay_streams_each_event_and_partial_stderr_before_completion():
    from workers_projects_runtime.direct_native_warm import _EventWriter
    connection = Mock()
    writer = _EventWriter(connection)
    writer.write('{"type":"synthetic"}')
    connection.send.assert_not_called()
    writer.write('\n')
    assert json.loads(connection.send.call_args.args[0]) == {'type': 'event', 'line': '{"type":"synthetic"}'}
    err = _EventWriter(connection, 'stderr')
    err.write('partial diagnostic')
    assert json.loads(connection.send.call_args.args[0]) == {'type': 'stderr', 'text': 'partial diagnostic'}


def test_installed_identity_change_invalidates_import_only_scope(tmp_path, monkeypatch):
    import workers_projects_runtime.direct_warm_runtime as adapter
    marker = tmp_path / 'synthetic-wheel-record'
    marker.write_text('first-record')
    monkeypatch.setattr(adapter, '_installed_identity_files', lambda: (marker,))
    runtime = DirectModelRuntime('direct-xai', base_dir=str(tmp_path / 'state'))
    try:
        first = runtime._warm_context(worker(tmp_path))[0]
        marker.write_text('changed-installed-record')
        second = runtime._warm_context(worker(tmp_path))[0]
        assert first.configuration_revision != second.configuration_revision
    finally:
        runtime.close_native_warm()


@pytest.mark.parametrize('mutation', [None, 'secret', 'port', 'scope', 'owner', 'child', 'lease', 'stopped', 'idle'])
def test_server_self_anchor_preserves_fresh_receipt_checks_without_duplicate_os_probe(tmp_path, monkeypatch, mutation):
    import copy
    import workers_projects_runtime.native_warm_pool as owner
    pid = os.getpid()
    monkeypatch.setattr(os, 'getpgrp', lambda: pid)
    monkeypatch.setattr(owner, 'process_start_identity', lambda _pid: pytest.fail('Server self check must not spawn ps'))
    monkeypatch.setattr(owner, 'listener_owned_by', lambda *_args: pytest.fail('Client already verified the listener'))
    identity = {'pid': pid, 'process_group': pid, 'process_start_identity': 'ps-lstart:synthetic-generation'}
    scope = {'worker_id': 'worker', 'profile': 'direct-xai', 'execution_mode': 'host', 'model': 'grok-4.7'}
    record_path, descriptor = tmp_path / 'record.json', tmp_path / 'lease.json'
    anchor = {'kind': 'xperfect.native.warm.v1', 'phase': 'booting', 'scope': scope,
        'owner': {'pid': 100, 'process_start_identity': 'ps-lstart:synthetic-owner'},
        'child': identity, 'port': 12345, 'secret': 'synthetic-secret'}
    record = copy.deepcopy({**anchor, 'phase': 'leased', 'lease_id': 'lease', 'run_id': 'run',
        'attempt_id': 'attempt', 'attempt_descriptor': str(descriptor)})
    lease = copy.deepcopy({'version': 1, 'lease_id': 'lease', 'run_id': 'run', 'attempt_id': 'attempt',
        'scope': scope, 'server': identity, 'url': 'ws://127.0.0.1:12345/ws',
        'secret': 'synthetic-secret', 'state_record': str(record_path)})
    if mutation == 'secret':
        record['secret'] = lease['secret'] = 'changed-secret'
    elif mutation == 'port':
        record['port'] = 12346; lease['url'] = 'ws://127.0.0.1:12346/ws'
    elif mutation == 'scope':
        record['scope']['owner_id'] = 'changed-owner'; lease['scope']['owner_id'] = 'changed-owner'
    elif mutation == 'owner':
        record['owner']['pid'] = 200
    elif mutation == 'child':
        record['child']['process_start_identity'] = lease['server']['process_start_identity'] = 'ps-lstart:changed'
    elif mutation == 'lease':
        record['lease_id'] = 'changed-lease'
    elif mutation == 'stopped':
        lease['stop_confirmed'] = True
    elif mutation == 'idle':
        record['phase'] = 'idle'
    owner._write_private(record_path, record); owner._write_private(descriptor, lease)
    if mutation is None:
        assert validate_admitted_lease(descriptor, worker_id='worker', run_id='run', attempt_id='attempt',
            profile='direct-xai', model='grok-4.7', server_record=anchor)['lease_id'] == 'lease'
    else:
        with pytest.raises(ValueError):
            validate_admitted_lease(descriptor, worker_id='worker', run_id='run', attempt_id='attempt',
                profile='direct-xai', model='grok-4.7', server_record=anchor)


def test_configured_but_not_ready_never_waits_or_replays_cold_command(tmp_path, monkeypatch):
    from types import SimpleNamespace
    runtime = DirectModelRuntime('direct-xai', base_dir=str(tmp_path / 'state'))
    w = worker(tmp_path, _active_run_id='run', _run_attempt_id='attempt',
               bootstrap_bundle_json=json.dumps({'runtime_performance': {'native_warm_servers': 1}}))
    scope = runtime._warm_context(w)[0]
    monkeypatch.setattr(runtime, '_safe_warm_prepare', lambda *_args: scope)
    runtime._warm_pool = SimpleNamespace(claim=Mock(return_value=None))
    monkeypatch.setattr(runtime, '_write_action_audit', Mock())
    command = ['exact-cold-command']
    assert runtime._prepare_native_warm_command(w, command, run_root=tmp_path) is command
    runtime._warm_pool.claim.assert_called_once_with(scope, run_id='run', attempt_id='attempt')


def test_shared_launch_refactor_keeps_exact_native_grok_initialize_protocol(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from websockets.sync import client
    import workers_projects_runtime.native_warm_pool as owner
    environment = {'GROK_SUBAGENTS': '0', 'GROK_WORKFLOWS': '0'}
    scope = owner.WarmScope('tenant', 'owner', 'worker', 'grok-build', 'grok-4.7', 'account', 'revision',
        owner.native_environment_digest(environment, lane='conversation'))
    socket = Mock()
    socket.recv.return_value = json.dumps({'id': 1, 'result': {'protocolVersion': 1, '_meta': {'grokShell': True}}})
    connection = Mock(); connection.__enter__ = Mock(return_value=socket); connection.__exit__ = Mock(return_value=False)
    connect = Mock(return_value=connection); monkeypatch.setattr(client, 'connect', connect)
    def launch(**kwargs):
        assert kwargs['secret_environment'] == 'GROK_AGENT_SECRET'
        assert kwargs['command'](12345, tmp_path / 'record') == ['synthetic-grok', 'agent', '--no-leader', '--model', 'grok-4.7', 'serve', '--bind', '127.0.0.1:12345']
        kwargs['probe'](SimpleNamespace(url='ws://127.0.0.1:12345/ws', secret='synthetic-secret'), time.monotonic() + 3)
        return 'same-native-server'
    monkeypatch.setattr(owner, 'start_native_warm_server', launch)
    assert owner.start_grok_server(binary='synthetic-grok', model='grok-4.7', cwd=tmp_path,
        static_environment=environment, state_directory=tmp_path, scope=scope, stop_runtime=None) == 'same-native-server'
    assert connect.call_args.kwargs['additional_headers'] == {'Authorization': 'Bearer synthetic-secret'}
    assert json.loads(socket.send.call_args.args[0])['method'] == 'initialize'


@pytest.mark.parametrize('value', [None, '', 'relative-folder', '/missing-synthetic-direct-workspace'])
def test_invalid_admitted_workspace_fails_before_authority_or_sdk(value):
    from workers_projects_runtime.direct_native_warm import admitted_workspace
    env = {} if value is None else {'GLASSHIVE_WORKSPACE_DIR': value}
    with pytest.raises(ValueError, match='workspace is unavailable'):
        admitted_workspace(env)


def test_real_warm_relative_stdio_mcp_uses_exact_admitted_cold_workspace(tmp_path):
    runtime = DirectModelRuntime('direct-xai', base_dir=str(tmp_path / 'state'))
    w = worker(tmp_path)
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    try:
        scope = ready(runtime, w)
        lease = runtime._warm_pool.claim(scope, run_id='synthetic-cwd-run', attempt_id='synthetic-cwd-attempt')
        descriptor = runtime._warm_descriptor_path(w['worker_id'], 'synthetic-cwd-run', 'synthetic-cwd-attempt')
        lease.write_descriptor(descriptor)
        request = {'provider': 'xai', 'model': w['model'], 'run_id': 'synthetic-cwd-run',
            'history_scope': {'worker': w['worker_id'], 'tenant': 'local', 'owner': 'synthetic-owner'},
            'history_path': str(tmp_path / 'unused-history.json'), 'history_enabled': False,
            'effort': 'low', 'session_id': 'direct:synthetic-cwd', 'instructions': 'Synthetic authority',
            'instruction': 'Synthetic local initialization', 'mcp_servers': [{
                'name': 'synthetic-relative-stdio', 'command': sys.executable,
                'args': ['-c', "from pathlib import Path; Path('relative-stdio-proof').write_text('initialized'); import os; os.write(2, b'partial stdio diagnostic'); raise SystemExit(0)"], 'env': []}]}
        command = [sys.executable, str(Path(__file__).parents[1] / 'src/workers_projects_runtime/direct_native_warm.py'),
            '--attach', str(descriptor), w['worker_id'], 'synthetic-cwd-run', 'synthetic-cwd-attempt', 'direct-xai']
        result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True,
            env={**idle_environment(), 'XAI_API_KEY': 'synthetic-not-sent', 'GLASSHIVE_WORKSPACE_DIR': str(workspace)},
            cwd=workspace, timeout=20)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        assert (workspace / 'relative-stdio-proof').exists(), [(e.get('type'), e.get('stage'), e.get('error_code'), e.get('error_type')) for e in events]
        assert (workspace / 'relative-stdio-proof').read_text() == 'initialized'
        assert not (runtime.runtime_root / 'relative-stdio-proof').exists()
        events = [json.loads(line) for line in result.stdout.splitlines()]
        assert result.returncode == 1
        assert events[-1]['stage'] == 'tool_connection'
        assert 'partial stdio diagnostic' in result.stderr
        assert not any(e.get('stage') == 'provider_request_sent' for e in events)
        runtime._warm_pool.finish(lease)
    finally:
        runtime.close_native_warm()


def test_subprocess_stderr_descriptor_streams_without_newline_and_closes():
    import threading
    from workers_projects_runtime.direct_native_warm import _EventWriter
    arrived = threading.Event()
    frames = []
    connection = Mock()
    def observe(value):
        frames.append(json.loads(value))
        if ''.join(frame.get('text', '') for frame in frames) == 'partial \u00e9':
            arrived.set()
    connection.send.side_effect = observe
    writer = _EventWriter(connection, 'stderr')
    fd = writer.fileno()
    read_fd = writer._pipe[0]
    try:
        os.write(fd, b'partial \xc3')
        os.write(fd, b'\xa9')
        assert arrived.wait(1), 'Subprocess diagnostics must stream before close, without a newline'
    finally:
        writer.close()
    assert not writer._drain.is_alive()
    for descriptor in (fd, read_fd):
        with pytest.raises(OSError):
            os.fstat(descriptor)
    writer.close()  # Cleanup remains idempotent on an error path.


@pytest.mark.parametrize('profile', list(DIRECT_PROVIDERS))
@pytest.mark.parametrize('binding', ['configured', 'admitted'])
def test_direct_cold_workspace_and_fresh_warm_carrier_share_bound_owner(tmp_path, monkeypatch, profile, binding):
    runtime = DirectModelRuntime(profile, base_dir=str(tmp_path / 'state'))
    bound = tmp_path / 'bound-project'; bound.mkdir()
    w = worker(tmp_path, profile, workspace_dir=str(bound), _active_run_id='synthetic-bound-run')
    if binding == 'admitted':
        # Exact existing durable-session binding takes priority over configured path.
        w['workspace_dir'] = str(tmp_path / 'unused-configured-project')
        monkeypatch.setattr(runtime, '_read_active_session', lambda _worker: {
            'run_id': 'synthetic-bound-run', 'workspace_dir': str(bound),
            'stdout_path': str(runtime._run_root(w['worker_id'], 'synthetic-bound-run') / 'stdout.log')})
    monkeypatch.setenv(DIRECT_PROVIDERS[profile]['key'], 'synthetic-not-sent')
    try:
        info = runtime.ensure_worker_ready(w)
        current = {**w, 'workspace_dir': info.workspace_dir}  # Both real cold launch owners do this.
        _, environment = runtime._build_command(current, 'Synthetic bound workspace', info)
        cold_cwd = Path(str(info.workspace_dir or runtime._host_workspace_dir(current)))
        assert cold_cwd == bound
        assert environment['GLASSHIVE_WORKSPACE_DIR'] == str(cold_cwd)
    finally:
        runtime.close_native_warm()
