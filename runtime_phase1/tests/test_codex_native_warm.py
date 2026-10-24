"""Codex preparation retains admitted authority and existing native ownership."""
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from workers_projects_runtime import codex_native_warm as warm, native_input
from workers_projects_runtime.native_warm_pool import _native_state_lock
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime, HostNativeCliMixin

TOKEN = 'GLASSHIVE_CAPABILITY_BROKER_TOKEN'
SOURCE = '''developer_instructions = "Exact current authority"
[mcp_servers."broker.v1"]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
enabled_tools = ["read", "write"]
default_tools_approval_mode = "approve"
[mcp_servers."broker.v1".http_headers]
Static = "retained"
[mcp_servers.native]
command = "native-mcp"
args = ["serve"]
env_vars = ["STATIC_NATIVE_ENV"]
'''


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def config_file(tmp_path, source=SOURCE):
    path = tmp_path / 'config.toml'
    path.write_text(source)
    path.chmod(0o600)
    return path


def test_projection_retains_native_tools_and_resolves_only_fresh_authority():
    source = SOURCE + '''
[mcp_servers.child]
command = "child-mcp"
env_vars = ["GLASSHIVE_CONTEXT_TOKEN", "STATIC_NATIVE_ENV"]
[mcp_servers.headers]
url = "http://127.0.0.1:8766/mcp"
[mcp_servers.headers.env_http_headers]
Current = "GLASSHIVE_PEER_TOKEN"
'''
    env = {TOKEN: 'fresh-a', 'GLASSHIVE_CONTEXT_TOKEN': 'fresh-b',
           'GLASSHIVE_PEER_TOKEN': 'fresh-c', 'STATIC_NATIVE_ENV': 'static-value', 'GLASSHIVE_RUN_ID': 'run-a'}
    projected, rpc = warm.project_config(source, env, {'model_reasoning_effort': 'high'})
    assert tomllib.loads(projected)['mcp_servers']['native'] == tomllib.loads(source)['mcp_servers']['native']
    assert all(value not in projected for value in ('fresh-a', 'fresh-b', 'fresh-c'))
    server = rpc['mcp_servers."broker.v1"']
    assert server['http_headers'] == {'Static': 'retained', 'Authorization': 'Bearer fresh-a'}
    assert server['enabled_tools'] == ['read', 'write']
    assert server['default_tools_approval_mode'] == 'approve'
    assert rpc['mcp_servers."child"']['env'] == {'GLASSHIVE_CONTEXT_TOKEN': 'fresh-b', 'STATIC_NATIVE_ENV': 'static-value'}
    assert rpc['mcp_servers."headers"']['http_headers'] == {'Current': 'fresh-c'}
    assert rpc['shell_environment_policy.set.GLASSHIVE_RUN_ID'] == 'run-a'
    assert warm.static_environment(env) == {'STATIC_NATIVE_ENV': 'static-value'}


@pytest.mark.parametrize('setting', [
    'mcp_servers."broker.v1".url="http://127.0.0.1:9999/mcp"',
    'mcp_servers."broker.v1".enabled_tools=["other"]',
    'shell_environment_policy.ignore_default_excludes=true',
    'hooks={}', 'model_provider="other"', 'cli_auth_credentials_store="keyring"'])
def test_cli_override_never_silently_loses_cold_authority(setting):
    with pytest.raises(ValueError, match='ordinary startup'):
        warm.command_config(['codex', 'app-server', '--stdio', '-c', setting])


def test_supported_thread_flags_keep_exact_settings():
    assert warm.command_config(['codex', 'app-server', '--stdio', '-c', 'model="selected-model"',
        '-c', 'sandbox_workspace_write.network_access=false', '-c', 'sandbox_workspace_write.writable_roots=["/workspace"]',
        '--enable', 'code_mode_host', '-c', 'cli_auth_credentials_store="file"']) == {
        'model': 'selected-model', 'sandbox_workspace_write': {'network_access': False, 'writable_roots': ['/workspace']},
        'features': {'code_mode_host': True}}


@pytest.mark.parametrize('policy', ['inherit="none"', 'exclude=["GLASSHIVE_*"]', 'include_only=["PATH"]', 'set={GLASSHIVE_RUN_ID="explicit"}'])
def test_run_identity_respects_cold_shell_policy(policy):
    _, rpc = warm.project_config(SOURCE + '\n[shell_environment_policy]\n' + policy,
                                {TOKEN: 'fresh', 'GLASSHIVE_RUN_ID': 'run'}, {})
    assert 'shell_environment_policy.set.GLASSHIVE_RUN_ID' not in rpc


@pytest.mark.parametrize('failure', ['missing', 'remote-env'])
def test_unsupported_fresh_binding_stays_cold(failure):
    source = SOURCE if failure == 'missing' else '''[mcp_servers.child]
command="native"
env_vars=["GLASSHIVE_CONTEXT_TOKEN", {name="REMOTE", source="remote"}]
'''
    with pytest.raises(ValueError, match='ordinary startup'):
        warm.project_config(source, {}, {})


def test_guard_restores_exact_bytes_and_releases_before_turn(tmp_path):
    path = config_file(tmp_path)
    def call(method, options):
        assert method == 'thread/resume' and options['threadId'] == 'retained-thread'
        assert TOKEN not in path.read_text() and 'fresh' not in path.read_text()
        assert options['config']['mcp_servers."broker.v1"']['http_headers']['Authorization'] == 'Bearer fresh'
        return {'thread': {'id': 'retained-thread'}}
    assert warm.start_projected_thread(call, 'thread/resume', {'threadId': 'retained-thread'},
        config_path=path, expected_digest=digest(SOURCE), environment={TOKEN: 'fresh'}, overrides={}) == {'thread': {'id': 'retained-thread'}}
    assert path.read_text() == SOURCE
    with _native_state_lock(tmp_path, timeout=.05):
        pass


@pytest.mark.parametrize('failure', ['stale-before', 'changed-during', 'lost-ack'])
def test_configuration_races_preserve_newer_bytes_and_never_send_stale_authority(tmp_path, failure):
    path = config_file(tmp_path)
    calls = []
    if failure == 'stale-before':
        path.write_text('developer_instructions="newer"\n')
    def call(*args):
        calls.append(args)
        if failure == 'changed-during':
            path.write_text('developer_instructions="newer"\n')
        else:
            raise native_input.NativeInputError('lost-ack')
        return {}
    with pytest.raises((warm.ConfigurationChanged, native_input.NativeInputError)):
        warm.start_projected_thread(call, 'thread/start', {}, config_path=path,
            expected_digest=digest(SOURCE), environment={TOKEN: 'fresh'}, overrides={})
    assert len(calls) == (0 if failure == 'stale-before' else 1)
    assert path.read_text() == (SOURCE if failure == 'lost-ack' else 'developer_instructions="newer"\n')


@pytest.mark.parametrize('mission', [False, True])
def test_materializer_serializes_with_projection_without_blocking_stop(tmp_path, monkeypatch, mission):
    runtime = HostCodexCliRuntime(str(tmp_path / 'runtime'))
    worker = {'worker_id': 'synthetic-worker', 'profile': 'codex-cli', 'owner_id': 'synthetic-owner'}
    home = tmp_path / 'home'; home.mkdir()
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    path = config_file(home)
    monkeypatch.setattr('workers_projects_runtime.profile_runtime.require_native_installed_owner', lambda *_: None)
    monkeypatch.setattr(runtime, '_host_codex_home', lambda _: home)
    monkeypatch.setattr(runtime, '_host_codex_worker_config', lambda *a, **k: 'developer_instructions="next"')
    monkeypatch.setattr(runtime, '_copy_host_codex_auth', lambda *_: None)
    monkeypatch.setattr(runtime, '_project_host_codex_capability_roots', lambda *_: None)
    entered, release, wrote = threading.Event(), threading.Event(), threading.Event()
    def call(*args):
        entered.set()
        assert release.wait(2)
        return {}
    def writer():
        if mission:
            runtime._write_host_project_mcp_files(worker, workspace, {'developer_instructions': 'next'})
        else:
            runtime._write_conversation_runtime_files(worker, {'developer_instructions': 'next'})
        wrote.set()
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(warm.start_projected_thread, call, 'thread/start', {}, config_path=path,
            expected_digest=digest(SOURCE), environment={TOKEN: 'fresh'}, overrides={})
        assert entered.wait(2)
        with runtime._try_active_session_file_lock(worker['worker_id']) as acquired:
            assert acquired
        writing = pool.submit(writer)
        assert not wrote.wait(.05)
        release.set()
        future.result(); writing.result()
    assert path.read_text() == 'developer_instructions="next"\n'
    if mission:
        assert (workspace / '.codex/config.toml').read_bytes() == path.read_bytes()


def test_config_lock_timeout_is_bounded_and_worker_homes_are_independent(tmp_path):
    with _native_state_lock(tmp_path):
        with pytest.raises(TimeoutError):
            with _native_state_lock(tmp_path, timeout=.02):
                pytest.fail('Overlapping lock acquired')
        with _native_state_lock(tmp_path / 'other', timeout=.02):
            pass


def execute_context(tmp_path):
    path = config_file(tmp_path)
    launch = {'command': ['codex', 'app-server', '--stdio'], 'thread': {'model': 'exact'},
              'turn': {}, 'images': [], 'resumed': False}
    context = tmp_path / 'native-input-context.json'
    native_input.publish(context, {'workerId': 'worker', 'runId': 'run', 'attemptId': 'attempt',
        'launch': launch, 'warmConfigDigest': digest(SOURCE)})
    message = {'context': str(context), 'lease': 'lease', 'instruction': 'Exact input',
               'environment': {'CODEX_HOME': str(tmp_path), TOKEN: 'fresh'}}
    return path, context, message


@pytest.mark.parametrize('failure', ['pre-dispatch', 'lost-ack', 'final-publish'])
def test_fallback_is_impossible_after_native_dispatch_or_completion(tmp_path, monkeypatch, failure):
    path, context, message = execute_context(tmp_path)
    monkeypatch.setattr(warm, 'validate_admitted_lease', lambda *a, **k: {})
    events = queue.Queue()
    events.put({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed'}}})
    calls = []
    def call(method, params):
        calls.append(method)
        if failure == 'lost-ack':
            raise native_input.NativeInputError('uncertain accepted RPC')
        return {'thread': {'id': 'thread'}, 'model': 'exact'} if method == 'thread/start' else {'turn': {'id': 'turn'}}
    rpc = SimpleNamespace(call=call, close=Mock(), events=events)
    if failure == 'pre-dispatch':
        path.write_text('developer_instructions="newer"\n')
    if failure == 'final-publish':
        publish = native_input.publish
        def failing_publish(target, value):
            if value.get('nativeSessionActive') is False:
                raise OSError('synthetic terminal persistence failure')
            publish(target, value)
        monkeypatch.setattr(native_input, 'publish', failing_publish)
    messages = []
    connection = SimpleNamespace(send=lambda value: messages.append(json.loads(value)))
    warm.execute(connection, message, {'scope': {'worker_id': 'worker', 'model': 'exact'}},
        {'CODEX_HOME': str(tmp_path)}, rpc, warm._Diagnostics())
    terminal = [item for item in messages if item['type'] in {'done', 'unavailable'}]
    assert terminal == [{'type': 'done', 'code': 1}]
    assert calls == ([] if failure == 'pre-dispatch' else ['thread/start'] if failure == 'lost-ack' else ['thread/start', 'turn/start'])


@pytest.fixture
def runtime_fixture(tmp_path, monkeypatch):
    # Load the real SDK before replacing Popen: its platform helpers use
    # Popen as a generic type during their first import.
    import mcp.client.stdio  # noqa: F401
    from test_codex_native_midstream import fake_native
    runtime = HostCodexCliRuntime(str(tmp_path / 'runtime'))
    cli = fake_native(tmp_path)
    cli.write_text('#!' + sys.executable + '\n' + cli.read_text().replace("json.dumps(v)+'\\n'", "json.dumps({**v, '_native_pid': os.getpid()})+'\\n'"))
    cli.chmod(0o700)
    runtime.binary = str(cli)
    original_popen = subprocess.Popen
    diagnostics = tmp_path / 'holder-stderr.txt'
    def observed_popen(command, **kwargs):
        if '--guarded-exec' in command:
            with diagnostics.open('a') as stream:
                return original_popen(command, **{**kwargs, 'stderr': stream})
        return original_popen(command, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', observed_popen)
    runtime._test_holder_diagnostics = diagnostics
    worker = {'worker_id': 'worker', 'owner_id': 'owner', 'tenant_id': 'tenant', 'profile': 'codex-cli',
        'execution_mode': 'host', 'model': 'synthetic-model', '_active_run_id': 'run', '_run_attempt_id': 'attempt',
        'bootstrap_bundle_json': json.dumps({'run_mode': 'conversation', 'runtime_performance': {'native_warm_servers': 1}})}
    home = tmp_path / 'home'; home.mkdir()
    config_file(home)
    (home / 'auth.json').write_text('{}')
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    env = {'HOME': str(home), 'CODEX_HOME': str(home), 'PATH': os.environ.get('PATH', ''), 'LANG': 'en_US.UTF-8',
        'PYTHONPATH': str(Path(warm.__file__).parent.parent), 'GLASSHIVE_WORKSPACE_DIR': str(workspace),
        'NATIVE_RECEIPT': str(tmp_path / 'requests.jsonl'), 'NATIVE_SERVER_REQUESTS': '1', TOKEN: 'fresh'}
    monkeypatch.setattr(warm, 'native_current_account_allowed', lambda _: True)
    monkeypatch.setattr(runtime, '_native_midstream_schema_supported', lambda: True)
    monkeypatch.setattr(runtime, '_host_codex_home', lambda _: home)
    monkeypatch.setattr(runtime, '_source_host_codex_home', lambda: tmp_path / 'source-home')
    monkeypatch.setattr(runtime, '_host_workspace_dir', lambda _: workspace)
    monkeypatch.setattr(runtime, '_copy_host_codex_auth', lambda *_: None)
    monkeypatch.setattr(runtime, '_host_env', lambda _, run_id=None: {**env, **({'GLASSHIVE_RUN_ID': run_id} if run_id else {})})
    yield runtime, worker, home, env
    runtime.close_native_warm()


def ready(runtime, worker):
    status = runtime.configure_native_warm(worker, {'native_warm_servers': 1, 'native_warm_idle_seconds': 60})
    assert status['supported'] is True, status
    try:
        wait_for(lambda: runtime.native_warm_status(worker, {'native_warm_servers': 1})['idle'] == 1)
    except AssertionError:
        pytest.fail(runtime._test_holder_diagnostics.read_text())
    return runtime._warm_contexts[runtime._warm_owner(worker)][0]


def test_real_stdio_idle_then_exact_fresh_lease_uses_one_initialize_and_existing_relay(runtime_fixture, tmp_path):
    from test_codex_native_midstream import launch
    runtime, worker, home, env = runtime_fixture
    ready(runtime, worker)
    receipt = Path(env['NATIVE_RECEIPT'])
    assert [json.loads(line)['method'] for line in receipt.read_text().splitlines()] == ['initialize', 'initialized']
    launch_value, _ = launch(tmp_path)
    launch_value['command'][0] = runtime.binary
    context = tmp_path / 'native-input-context.json'
    native_input.publish(context, {'workerId': 'worker', 'runId': 'run', 'attemptId': 'attempt', 'launch': launch_value})
    cold = ['existing-relay', str(context)]
    command = runtime._prepare_native_warm_command(worker, cold, run_root=tmp_path)
    assert '--attach' in command
    execution = subprocess.run(command, input='Exact admitted input', text=True, capture_output=True,
        env={**env, 'GLASSHIVE_RUN_MODE': 'conversation', 'GLASSHIVE_RUN_ID': 'run'}, timeout=8)
    assert execution.returncode == 0, execution.stdout + execution.stderr
    calls = [json.loads(line) for line in receipt.read_text().splitlines()]
    active_pid = next(item['_native_pid'] for item in calls if item.get('method') == 'thread/start')
    calls = [item for item in calls if item['_native_pid'] == active_pid]
    assert [item.get('method') for item in calls[:4]] == ['initialize', 'initialized', 'thread/start', 'turn/start']
    assert sum(item.get('method') == 'initialize' for item in calls) == 1
    assert not any(item.get('method') in {'mcpServerStatus/list', 'config/mcpServer/reload'} for item in calls)
    config = calls[2]['params']['config']
    assert config['mcp_servers."broker.v1"']['http_headers']['Authorization'] == 'Bearer fresh'
    assert calls[3]['params']['input'][0]['text'] == 'Exact admitted input'
    assert calls[3]['params']['input'][1:] == launch_value['images']
    assert 'Typed native requests resolved' in execution.stdout
    assert (home / 'config.toml').read_text() == SOURCE
    assert 'Bearer fresh' not in context.read_text()
    descriptor = Path(worker['_external_native_lease_path'])
    runtime._finish_native_warm(worker)
    assert json.loads(descriptor.read_text())['stop_confirmed'] is True


@pytest.mark.parametrize('operation', ['disable', 'pause_worker', 'terminate_worker', 'crash'])
def test_codex_idle_lifecycle_retains_existing_stop_owner(runtime_fixture, monkeypatch, operation):
    runtime, worker, _, _ = runtime_fixture
    scope = ready(runtime, worker)
    lease = runtime._warm_pool.claim(scope, run_id='run', attempt_id='attempt')
    assert lease is not None
    server = lease.server
    if operation == 'crash':
        descendants = subprocess.run(['pgrep', '-P', str(server.process.pid)], text=True, capture_output=True, check=True)
        os.kill(int(descendants.stdout.splitlines()[0]), 15)
        wait_for(lambda: not server.alive())
    elif operation == 'disable':
        runtime.configure_native_warm(worker, {'native_warm_servers': 0})
    else:
        base = Mock(return_value='existing control')
        monkeypatch.setattr(HostNativeCliMixin, operation, base)
        assert getattr(runtime, operation)(worker) == 'existing control'
        base.assert_called_once_with(worker)
    runtime._warm_pool.finish(lease)
    assert not server.alive()


@pytest.mark.parametrize('change', ['model', 'account', 'binary', 'source-config', 'worker-startup-config', 'static-env'])
def test_idle_identity_changes_invalidate_reuse(runtime_fixture, change):
    runtime, worker, home, env = runtime_fixture
    scope, static, _ = runtime._warm_context(worker)
    if change == 'model':
        worker['model'] = 'different-selected-model'
    elif change == 'account':
        (home / 'auth.json').write_text('{"changed":true}')
    elif change == 'binary':
        with Path(runtime.binary).open('a') as stream: stream.write('\n# changed\n')
    elif change == 'worker-startup-config':
        (home / 'config.toml').write_text(SOURCE + '\n[analytics]\nenabled=false\n')
    elif change == 'source-config':
        path = runtime._source_host_codex_home() / 'config.toml'
        path.parent.mkdir(); path.write_text('personality="none"')
    else:
        env['PATH'] += ':/synthetic'
    changed, changed_static, _ = runtime._warm_context(worker)
    assert scope != changed or static != changed_static


def test_zero_and_unsupported_cli_return_exact_original_command_without_claim(runtime_fixture, tmp_path):
    runtime, worker, _, _ = runtime_fixture
    command = ['original', 'exec', '-']
    worker['bootstrap_bundle_json'] = '{}'
    assert runtime._prepare_native_warm_command(worker, command, run_root=tmp_path) is command
    worker['bootstrap_bundle_json'] = json.dumps({'runtime_performance': {'native_warm_servers': 1}})
    native_input.publish(tmp_path / 'native-input-context.json', {'workerId': 'worker', 'runId': 'run', 'attemptId': 'attempt',
        'launch': {'command': ['codex', 'app-server', '--stdio', '-c', 'mcp_servers.broker.url="other"']}})
    command = ['original-relay']
    assert runtime._prepare_native_warm_command(worker, command, run_root=tmp_path) is command
    assert not runtime._warm_leases


def test_unowned_project_fresh_environment_and_missing_auth_remain_cold(runtime_fixture):
    runtime, worker, home, _ = runtime_fixture
    (home / 'auth.json').unlink()
    with pytest.raises(ValueError, match='account'):
        runtime._warm_context(worker)
    (home / 'auth.json').write_text('{}')
    path = runtime._host_workspace_dir(worker) / '.codex/config.toml'
    path.parent.mkdir(); path.write_text(SOURCE)
    with pytest.raises(ValueError, match='Project MCP'):
        runtime._warm_context(worker)
    assert path.read_text() == SOURCE


def test_two_concurrent_leases_keep_attempt_grants_and_native_readers_separate(runtime_fixture, tmp_path):
    from test_codex_native_midstream import launch
    runtime, worker, home, env = runtime_fixture
    worker['bootstrap_bundle_json'] = json.dumps({'run_mode': 'conversation', 'runtime_performance': {'native_warm_servers': 2}})
    runtime.configure_native_warm(worker, {'native_warm_servers': 2, 'native_warm_idle_seconds': 60})
    wait_for(lambda: runtime.native_warm_status(worker, {'native_warm_servers': 2})['idle'] == 2)
    prepared = []
    for suffix in ['a', 'b']:
        root = tmp_path / suffix; root.mkdir()
        launch_value, _ = launch(root)
        launch_value['command'][0] = runtime.binary
        current = {**worker, '_active_run_id': 'run-' + suffix, '_run_attempt_id': 'attempt-' + suffix}
        context = root / 'native-input-context.json'
        native_input.publish(context, {'workerId': 'worker', 'runId': current['_active_run_id'],
            'attemptId': current['_run_attempt_id'], 'launch': launch_value})
        command = runtime._prepare_native_warm_command(current, ['existing-relay', str(context)], run_root=root)
        assert '--attach' in command
        prepared.append((suffix, current, command))
    def run(item):
        suffix, current, command = item
        result = subprocess.run(command, input='Input ' + suffix, text=True, capture_output=True, timeout=8,
            env={**env, TOKEN: 'grant-' + suffix, 'GLASSHIVE_RUN_MODE': 'conversation', 'GLASSHIVE_RUN_ID': 'run-' + suffix})
        assert result.returncode == 0, result.stdout + result.stderr
        runtime._finish_native_warm(current)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(run, prepared))
    calls = [json.loads(line) for line in Path(env['NATIVE_RECEIPT']).read_text().splitlines()]
    starts = [item for item in calls if item.get('method') == 'thread/start']
    assert len(starts) == 2 and len({item['_native_pid'] for item in starts}) == 2
    assert {item['params']['config']['mcp_servers."broker.v1"']['http_headers']['Authorization'] for item in starts} == {'Bearer grant-a', 'Bearer grant-b'}
    for start in starts:
        own = [item for item in calls if item['_native_pid'] == start['_native_pid']]
        suffix = start['params']['config']['shell_environment_policy.set.GLASSHIVE_RUN_ID'][-1]
        assert next(item['params']['input'][0]['text'] for item in own if item.get('method') == 'turn/start') == 'Input ' + suffix
        assert sum(item.get('method') == 'initialize' for item in own) == 1
    assert (home / 'config.toml').read_text() == SOURCE


def test_existing_resume_authority_wrapper_and_cold_inner_command_are_preserved(runtime_fixture, tmp_path):
    from test_codex_native_midstream import launch
    runtime, worker, _, _ = runtime_fixture
    ready(runtime, worker)
    launch_value, _ = launch(tmp_path, resume=True)
    launch_value['command'][0] = runtime.binary
    context = tmp_path / 'native-input-context.json'
    native_input.publish(context, {'workerId': 'worker', 'runId': 'run', 'attemptId': 'attempt', 'launch': launch_value})
    authority_path = tmp_path / 'native-authority-context.json'
    inner = ['existing-relay', str(context)]
    authority = {'session': 'session-synthetic', 'authority': 'Exact authority', 'command': inner}
    native_input.publish(authority_path, authority)
    outer = [sys.executable, 'existing-authority-preflight.py', str(authority_path)]
    assert runtime._prepare_native_warm_command(worker, outer, run_root=tmp_path) is outer
    updated = native_input.read_object(authority_path)
    assert updated['authority'] == authority['authority'] and updated['session'] == authority['session']
    assert '--attach' in updated['command']
    saved = native_input.read_object(context)
    assert saved['warmColdCommand'] == inner
    assert saved['launch']['thread']['threadId'] == 'session-synthetic'
    runtime._finish_native_warm(worker)


@pytest.mark.parametrize('phase', ['connect-failed', 'unavailable', 'send-uncertain', 'config-changed'])
def test_attach_fallback_retains_exact_cold_command_only_before_dispatch(tmp_path, monkeypatch, phase):
    path, context_path, _ = execute_context(tmp_path)
    context = native_input.read_object(context_path)
    command = ['selected-native', 'exec', '--unchanged-config', '-']
    native_input.publish(context_path, {**context, 'warmColdCommand': command})
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    monkeypatch.setattr(warm, 'validate_admitted_lease', lambda *a, **k: {'url': 'ws://127.0.0.1:12345', 'secret': 'synthetic'})
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'Exact original input\n')))
    cold = Mock(return_value=SimpleNamespace(returncode=7))
    monkeypatch.setattr(subprocess, 'run', cold)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def send(self, value):
            if phase == 'send-uncertain':
                raise OSError('uncertain delivery')
        def __iter__(self): return iter([json.dumps({'type': 'unavailable'})])
    def connect(*args, **kwargs):
        if phase == 'connect-failed':
            raise OSError('not connected')
        return Connection()
    monkeypatch.setattr('websockets.sync.client.connect', connect)
    if phase == 'config-changed':
        path.write_text('developer_instructions="newer"')
    if phase in {'send-uncertain', 'config-changed'}:
        with pytest.raises((OSError, warm.ConfigurationChanged)):
            warm.attach('lease', str(context_path))
        cold.assert_not_called()
    else:
        assert warm.attach('lease', str(context_path)) == 7
        cold.assert_called_once_with(command, input='Exact original input\n', text=True)


def test_fresh_thread_config_does_not_invalidate_prepared_process(runtime_fixture):
    runtime, worker, home, _ = runtime_fixture
    before, _, _ = runtime._warm_context(worker)
    (home / 'config.toml').write_text(SOURCE.replace('Exact current authority', 'New admitted authority').replace(':8765/', ':9876/'))
    after, _, _ = runtime._warm_context(worker)
    assert before == after
