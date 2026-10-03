from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from workers_projects_runtime.grok_runtime import GrokBuildRuntime, HostGrokBuildRuntime
from workers_projects_runtime.coordinator import CoordinatorScopeError
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo, ProviderAuthenticationMissingError
from workers_projects_runtime.profile_runtime import BaseCliWorkerRuntime, HostNativeCliMixin


def worker(tmp_path):
    return {'worker_id':'worker-grok', 'owner_id':'local', 'profile':'grok-build',
            'model':'grok-selected', 'workspace_dir':str(tmp_path / 'workspace'),
            'bootstrap_bundle_json':'{}', 'execution_mode':'host'}


def info(tmp_path, session=None):
    return RuntimeInfo('grok-build','grok-selected','',None,None,session,str(tmp_path/'state'),str(tmp_path/'workspace'),None)


def test_adapters_reuse_existing_exact_generation_lifecycle():
    assert HostGrokBuildRuntime.run_task is HostNativeCliMixin.run_task
    assert HostGrokBuildRuntime.interrupt_worker is HostNativeCliMixin.interrupt_worker
    assert HostGrokBuildRuntime.terminate_worker is HostNativeCliMixin.terminate_worker
    assert GrokBuildRuntime.run_task is BaseCliWorkerRuntime.run_task
    assert GrokBuildRuntime.interrupt_worker is BaseCliWorkerRuntime.interrupt_worker


@pytest.mark.parametrize('host', [True, False])
@pytest.mark.parametrize('lane,run_mode,run_id,attempt_id,restricted', [
    ('conversation', 'conversation', 'run-owned', 'attempt-owned', True),
    ('mission', 'mission', 'run-owned', 'attempt-owned', False),
    ('mission', 'conversation', 'run-owned', 'attempt-owned', False),
    ('conversation', 'mission', 'run-owned', 'attempt-owned', False),
    (None, 'conversation', 'run-owned', 'attempt-owned', False),
    ('conversation', 'conversation', '', 'attempt-owned', False),
    ('conversation', 'conversation', 'run-owned', '', False),
])
def test_only_admitted_conversation_environment_hides_local_spawn(
    tmp_path, monkeypatch, host, lane, run_mode, run_id, attempt_id, restricted
):
    from types import SimpleNamespace
    import workers_projects_runtime.grok_runtime as module
    from workers_projects_runtime.grok_projection import mcp_servers_for_bundle
    runtime = HostGrokBuildRuntime(str(tmp_path / 'state'))
    runtime.sandbox = SimpleNamespace(home_mount='/workspace/home', box=None)
    monkeypatch.setattr(runtime, '_host_env', lambda _: {})
    monkeypatch.setattr(runtime, '_container_env_for_worker', lambda _: {})
    # Bound account projection precedes the lifetime clamp.
    monkeypatch.setattr(module, 'apply_bound_provider_account_environment',
                        lambda _, env, **kw: env.update(XAI_API_KEY='synthetic-key', GROK_SUBAGENTS='1', GROK_WORKFLOWS='1'))
    servers = [{'name': 'worker-tools', 'type': 'http', 'url': 'https://example.test/mcp'}]
    bundle = {'run_mode': run_mode, 'provider_model': 'grok-selected',
              'env': {'WPR_GROK_REASONING_EFFORT': 'high'},
              'developer_instructions': 'Exact synthetic authority.', 'grok_mcp_servers': servers}
    record = {**worker(tmp_path), 'trusted_run_lane': lane,
              '_active_run_id': run_id, '_run_attempt_id': attempt_id,
              # Caller-looking fields cannot replace the admitted runtime fields.
              'run_id': 'untrusted-run', 'attempt_id': 'untrusted-attempt',
              '_active_attempt_id': 'untrusted-attempt',
              'bootstrap_bundle_json': json.dumps(bundle)}
    env = runtime._native_environment(record, host=host)
    assert env['GROK_SUBAGENTS'] == ('0' if restricted else '1')
    assert env['GROK_WORKFLOWS'] == ('0' if restricted else '1')
    assert env['XAI_API_KEY'] == 'synthetic-key'
    assert mcp_servers_for_bundle(bundle, env) == servers
    command = runtime._grok_command(record, info(tmp_path, 'saved-session'), host=host)
    assert command[command.index('--model') + 1] == 'grok-selected'
    assert command[command.index('--effort') + 1] == 'high'
    assert command[command.index('--session-id') + 1] == 'saved-session'
    target = runtime._home_dir(record['worker_id']) / '.xperfect-grok'
    assert json.loads((target / 'mcp-servers.json').read_text()) == servers
    assert (target / 'developer-instructions.txt').read_text() == bundle['developer_instructions']
    assert json.loads(record['bootstrap_bundle_json']) == bundle


@pytest.mark.parametrize('boundary', ['local', 'enterprise', 'multi_user', 'installed', 'named_account', 'explicit_key'])
def test_host_current_login_respects_existing_account_boundaries(tmp_path, monkeypatch, boundary):
    import workers_projects_runtime.grok_runtime as module
    source = tmp_path / 'native-grok'
    source.mkdir(mode=0o755)
    auth = source / 'auth.json'
    auth.write_text('{"synthetic":"native-sign-in"}')
    auth.chmod(0o600)
    monkeypatch.setenv('GROK_HOME', str(source))
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE', '1' if boundary == 'enterprise' else '0')
    monkeypatch.setenv('WPR_ENTERPRISE_MODE', '0')
    monkeypatch.setattr(module, 'multi_user_security_enabled', lambda: boundary == 'multi_user')
    monkeypatch.setattr(module, 'native_installed_owner_id', lambda: 'owner' if boundary == 'installed' else None)
    monkeypatch.setattr(HostNativeCliMixin, 'ensure_worker_ready', lambda self, record: info(tmp_path))
    record = worker(tmp_path)
    if boundary == 'named_account':
        record['bootstrap_bundle_json'] = json.dumps({'provider_account': {'policy': 'personal_required', 'account_id': 'other-account'}})
    elif boundary == 'explicit_key':
        record['bootstrap_bundle_json'] = json.dumps({'env': {'XAI_API_KEY': 'synthetic-key'}})
    runtime = HostGrokBuildRuntime(str(tmp_path / 'state'))
    runtime.ensure_worker_ready(record)
    target = runtime._grok_home(record) / 'auth.json'
    assert not target.exists()
    assert runtime._current_host_auth_path(record) == (auth if boundary == 'local' else None)


def test_current_login_refresh_survives_reused_and_second_worker_start(tmp_path, monkeypatch):
    import workers_projects_runtime.grok_runtime as module
    source = tmp_path / 'native-grok'
    source.mkdir(mode=0o755)
    auth = source / 'auth.json'
    auth.write_text('{"synthetic":"expired"}')
    auth.chmod(0o600)
    monkeypatch.setenv('GROK_HOME', str(source))
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE', '0')
    monkeypatch.setenv('WPR_ENTERPRISE_MODE', '0')
    monkeypatch.setattr(module, 'multi_user_security_enabled', lambda: False)
    monkeypatch.setattr(module, 'native_installed_owner_id', lambda: None)
    monkeypatch.setattr(HostNativeCliMixin, 'ensure_worker_ready', lambda self, record: info(tmp_path))
    runtime = HostGrokBuildRuntime(str(tmp_path / 'state'))
    monkeypatch.setattr(runtime, '_host_env', lambda _: {'GROK_AUTH_PATH': '/untrusted/auth.json'})
    first = worker(tmp_path)
    runtime.ensure_worker_ready(first)
    first_env = runtime._native_environment(first, host=True)
    assert first_env['GROK_AUTH_PATH'] == str(auth)
    # Native refresh atomically replaces the shared path; a second startup must
    # use that refreshed credential rather than restoring a copied old token.
    refreshed = source / 'refreshed.json'
    refreshed.write_text('{"synthetic":"refreshed"}')
    refreshed.chmod(0o600)
    os.replace(refreshed, auth)
    for record in (first, {**first, 'worker_id': 'second-grok-worker'}):
        stale = runtime._grok_home(record) / 'auth.json'
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"synthetic":"legacy-copy"}')
        runtime.ensure_worker_ready(record)
        env = runtime._native_environment(record, host=True)
        assert env['GROK_AUTH_PATH'] == str(auth)
        assert Path(env['GROK_AUTH_PATH']).read_text() == '{"synthetic":"refreshed"}'
        assert env['GROK_HOME'] == str(stale.parent)
        assert stale.read_text() == '{"synthetic":"legacy-copy"}'
        assert stale.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('unsafe', ['writable_home', 'open_auth', 'linked_home', 'linked_auth'])
def test_current_login_rejects_unsafe_shared_auth_path(tmp_path, monkeypatch, unsafe):
    import workers_projects_runtime.grok_runtime as module
    source = tmp_path / 'native-grok'
    source.mkdir(mode=0o700)
    auth = source / 'auth.json'
    auth.write_text('{}')
    auth.chmod(0o600)
    if unsafe == 'writable_home':
        source.chmod(0o777)
    elif unsafe == 'open_auth':
        auth.chmod(0o644)
    elif unsafe == 'linked_home':
        linked = tmp_path / 'linked-grok'
        linked.symlink_to(source, target_is_directory=True)
        source = linked
    else:
        moved = source / 'other-auth.json'
        auth.rename(moved)
        auth.symlink_to(moved)
    monkeypatch.setenv('GROK_HOME', str(source))
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE', '0')
    monkeypatch.setenv('WPR_ENTERPRISE_MODE', '0')
    monkeypatch.setattr(module, 'multi_user_security_enabled', lambda: False)
    monkeypatch.setattr(module, 'native_installed_owner_id', lambda: None)
    with pytest.raises(RuntimeErrorBase, match='authentication is unsafe'):
        HostGrokBuildRuntime(str(tmp_path / 'state'))._current_host_auth_path(worker(tmp_path))


def test_command_preserves_exact_model_and_resume_without_instruction_in_argv(tmp_path):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    command = runtime._grok_command(worker(tmp_path), info(tmp_path,'native-id'), host=True)
    assert command[command.index('--model')+1] == 'grok-selected'
    assert command[command.index('--session-id')+1] == 'native-id'
    assert Path(command[1]).is_file()
    assert command[1].startswith(str(runtime._home_dir('worker-grok')))
    assert str(tmp_path / 'workspace') not in command


@pytest.mark.parametrize('host', [True, False])
def test_restricted_command_uses_fresh_empty_workspace_no_resume_or_mcp(tmp_path, monkeypatch, host):
    from types import SimpleNamespace
    import workers_projects_runtime.grok_projection as projection
    runtime = HostGrokBuildRuntime(str(tmp_path / 'state'))
    runtime.sandbox = SimpleNamespace(home_mount='/workspace/home', box=None)
    bundle = {'run_mode': 'conversation', 'access_mode': 'full',
              'provider_capabilities': {'native_tools': False},
              'developer_instructions': 'Synthetic authority.',
              'env': {'WPR_GROK_REASONING_EFFORT': 'high', 'GLASSHIVE_PROVIDER_SESSION_MODE': 'stateless'},
              'grok_mcp_servers': [{'name': 'synthetic-tools', 'url': 'https://example.test/mcp'}]}
    record = {**worker(tmp_path), '_active_run_id': 'run-restricted', '_run_attempt_id': 'attempt-restricted',
              'trusted_run_lane': 'conversation', 'bootstrap_bundle_json': json.dumps(bundle)}
    original_mcp_projection = projection.mcp_servers_for_bundle
    monkeypatch.setattr(projection, 'mcp_servers_for_bundle', lambda *args: pytest.fail('Restricted MCP projection'))
    runtime._write_session_key(record['worker_id'], 'ordinary-saved-session')
    paths = []
    for _ in range(2):
        command = runtime._grok_command(record, info(tmp_path, 'ordinary-saved-session'), host=host)
        assert '--session-id' not in command and '--allow-mcp-tool' not in command
        assert '--native-tools-disabled' in command
        assert command[command.index('--model') + 1] == 'grok-selected'
        assert command[command.index('--effort') + 1] == 'high'
        assert command[command.index('--yolo-mode') + 1] == 'false'
        path = command[command.index('--restricted-workspace') + 1]
        root = runtime._home_dir(record['worker_id']) / '.xperfect-grok'
        actual = Path(path) if host else root / Path(path).name
        assert actual.is_dir() and not list(actual.iterdir())
        assert actual.stat().st_mode & 0o777 == 0o700
        assert json.loads((root / 'mcp-servers.json').read_text()) == []
        paths.append(path)
    assert paths[0] != paths[1]
    assert runtime._host_runtime_info(record).session_key is None
    runtime._remember_native_session_key(record, 'restricted-session')
    assert runtime._read_session_key(record['worker_id']) == 'ordinary-saved-session'
    monkeypatch.setattr(projection, 'mcp_servers_for_bundle', original_mcp_projection)
    authorized = {**record, 'bootstrap_bundle_json': json.dumps({
        **bundle, 'provider_capabilities': {'native_tools': True}, 'env': {}})}
    command = runtime._grok_command(authorized, info(tmp_path, 'ordinary-saved-session'), host=host)
    assert '--native-tools-disabled' not in command and '--restricted-workspace' not in command
    assert command[command.index('--session-id') + 1] == 'ordinary-saved-session'
    assert runtime._host_runtime_info(authorized).session_key == 'ordinary-saved-session'


@pytest.mark.parametrize('lane,host', [('mission', True), ('mission', False), ('conversation', True)])
def test_mission_permission_deadline_uses_only_exact_admitted_runtime_binding(tmp_path, lane, host):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    if not host:
        from types import SimpleNamespace
        runtime.sandbox = SimpleNamespace(home_mount='/workspace/home', box=None)
    record = {**worker(tmp_path), 'trusted_run_lane': lane,
              '_active_run_id': 'run-1', '_run_attempt_id': 'attempt-1',
              '_run_startup_token_digest': 'a' * 64, '_native_input_deadline_at': 4102444800,
              '_run_local_capability_binding': {
                  'workerId': 'worker-grok', 'runId': 'run-1',
                  ('hostStartupLeaseId' if host else 'containerGenerationId'): 'a' * 64}}
    command = runtime._grok_command(record, info(tmp_path), host=host)
    if lane == 'mission':
        assert command[command.index('--permission-deadline-at') + 1] == '4102444800'
    else:
        assert '--permission-deadline-at' not in command
    plain = {**record}
    plain.pop('_native_input_deadline_at')
    assert '--permission-deadline-at' not in runtime._grok_command(plain, info(tmp_path), host=host)


@pytest.mark.parametrize('invalid', ['run', 'worker', 'generation', 'missing_binding', 'nan', 'boolean', 'string'])
def test_mission_permission_deadline_rejects_unbound_or_invalid_authority(tmp_path, invalid):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    record = {**worker(tmp_path), 'trusted_run_lane': 'mission',
              '_active_run_id': 'run-1', '_run_attempt_id': 'attempt-1',
              '_run_startup_token_digest': 'a' * 64, '_native_input_deadline_at': 4102444800,
              '_run_local_capability_binding': {'workerId': 'worker-grok', 'runId': 'run-1',
                                                'hostStartupLeaseId': 'a' * 64}}
    if invalid in ('run', 'worker', 'generation'):
        field = {'run': 'runId', 'worker': 'workerId', 'generation': 'hostStartupLeaseId'}[invalid]
        record['_run_local_capability_binding'][field] = 'other'
    elif invalid == 'missing_binding':
        record.pop('_run_local_capability_binding')
    else:
        record['_native_input_deadline_at'] = {'nan': float('nan'), 'boolean': True, 'string': '4102444800'}[invalid]
    with pytest.raises(RuntimeErrorBase, match='mission input authority'):
        runtime._grok_command(record, info(tmp_path), host=True)


def test_worker_bundle_cannot_supply_a_mission_permission_deadline(tmp_path):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    record = {**worker(tmp_path), 'trusted_run_lane': 'mission',
              'bootstrap_bundle_json': json.dumps({'_native_input_deadline_at': 4102444800})}
    assert '--permission-deadline-at' not in runtime._grok_command(record, info(tmp_path), host=True)


def test_conversation_prompt_does_not_acquire_worker_report_instructions(tmp_path):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    record = worker(tmp_path)
    record['bootstrap_bundle_json'] = json.dumps({'run_mode': 'conversation'})
    instruction = 'Respond to the synthetic user task.'
    assert runtime._command_stdin_text(record, instruction, info(tmp_path)) == instruction
    record['bootstrap_bundle_json'] = '{}'
    assert 'FINAL REPORT' in runtime._command_stdin_text(record, instruction, info(tmp_path))


def test_only_run_bound_coordinator_projection_can_preapprove_its_own_tools(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    monkeypatch.setattr(runtime, '_native_environment', lambda _worker, host: {
        'GLASSHIVE_PEER_TOKEN': 'synthetic-token'
    })
    record = worker(tmp_path)
    record['_active_run_id'] = 'run-1'
    record['_coordinator_native_projection'] = {
        'worker_id': record['worker_id'], 'run_id': 'run-1',
        'token': 'synthetic-token', 'url': 'https://example.test/coordinator',
    }
    command = runtime._grok_command(record, info(tmp_path), host=True)
    assert 'xperfect-coordinator__coordinator_accept_goals' in command
    assert 'synthetic-token' not in command
    stale = {**record, '_active_run_id': 'run-2'}
    with pytest.raises(CoordinatorScopeError, match='Coordinator native projection mismatch'):
        runtime._grok_command(stale, info(tmp_path), host=True)
    plain = worker(tmp_path)
    assert '--allow-mcp-tool' not in runtime._grok_command(plain, info(tmp_path), host=True)


def test_box_socket_bridge_projection_preapproves_only_its_own_coordinator_tools(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    monkeypatch.setattr(runtime, '_native_environment', lambda _worker, host: {
        'GLASSHIVE_PEER_TOKEN': 'synthetic-token',
    })
    url = 'http+unix://%2Fworkspace%2Fdata%2F.xperfect-runtime.sock/v1/native/coordinator/'
    record = worker(tmp_path)
    record['_active_run_id'] = 'run-1'
    record['_coordinator_native_projection'] = {
        'worker_id': record['worker_id'], 'run_id': 'run-1', 'token': 'synthetic-token',
        'url': url, 'transport': 'stdio',
    }
    command = runtime._grok_command(record, info(tmp_path), host=True)
    assert 'xperfect-coordinator__coordinator_accept_goals' in command
    assert 'synthetic-token' not in command
    # A bridge carrying any other bearer is not this run's projection.
    monkeypatch.setattr(runtime, '_native_environment', lambda _worker, host: {
        'GLASSHIVE_PEER_TOKEN': 'different-token',
    })
    assert '--allow-mcp-tool' not in runtime._grok_command(record, info(tmp_path), host=True)


def test_no_model_default_or_unknown_profile_fallback(tmp_path, monkeypatch):
    runtime = GrokBuildRuntime(str(tmp_path))
    monkeypatch.delenv('WPR_MODEL_GROK_BUILD', raising=False)
    with pytest.raises(RuntimeErrorBase, match='exact Grok model'):
        runtime.resolve_model('grok-build')
    with pytest.raises(RuntimeErrorBase, match='Unsupported profile'):
        runtime.resolve_model('unknown')


def test_native_auth_missing_is_distinct_and_private_home_cannot_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'unconnected-grok'))
    runtime = HostGrokBuildRuntime(str(tmp_path))
    record = worker(tmp_path)
    monkeypatch.setattr(runtime, '_host_env', lambda _: {'GROK_HOME':'/other-worker', 'OTEL_EXPORTER_OTLP_ENDPOINT':'https://example.invalid'})
    with pytest.raises(ProviderAuthenticationMissingError):
        runtime._native_environment(record, host=True)
    native_home = runtime._grok_home(record)
    (native_home/'auth.json').write_text('{}')
    env = runtime._native_environment(record, host=True)
    assert env['GROK_HOME'] == str(native_home)
    assert env['GROK_SESSION_SUMMARY_MODEL'] == 'grok-4.7'
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT' not in env
    assert native_home.stat().st_mode & 0o777 == 0o700


def test_native_title_helper_preserves_explicit_model_choice(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path))
    record = worker(tmp_path)
    native_home = runtime._grok_home(record)
    native_home.mkdir(parents=True)
    (native_home / 'auth.json').write_text('{}')
    monkeypatch.setattr(runtime, '_host_env', lambda _: {
        'GROK_SESSION_SUMMARY_MODEL': 'explicit-helper-model',
    })
    env = runtime._native_environment(record, host=True)
    assert env['GROK_SESSION_SUMMARY_MODEL'] == 'explicit-helper-model'
    assert record['model'] == 'grok-selected'


def test_parser_rejects_mismatched_terminal_session(tmp_path):
    runtime = GrokBuildRuntime(str(tmp_path))
    events = [{'type':'grok.session.started','session_id':'native-id','model':'grok-selected'},
              {'type':'grok.result','session_id':'wrong','stop_reason':'end_turn','output':'not accepted'}]
    with pytest.raises(RuntimeErrorBase, match='native session/model'):
        runtime._parse_output(worker(tmp_path), '\n'.join(map(json.dumps,events)), '', info(tmp_path))


@pytest.mark.parametrize('runtime_type', [GrokBuildRuntime, HostGrokBuildRuntime])
@pytest.mark.parametrize('native_tools', [False, True, None])
def test_parser_matches_resume_identity_only_for_unrestricted_turns(tmp_path, runtime_type, native_tools):
    runtime = runtime_type(str(tmp_path))
    bundle = {'run_mode': 'conversation'}
    if native_tools is not None:
        bundle['provider_capabilities'] = {'native_tools': native_tools}
    record = {**worker(tmp_path), 'bootstrap_bundle_json': json.dumps(bundle)}
    retained = info(tmp_path, 'previous-native-session')
    for session_id in ('fresh-first-session', 'fresh-second-session'):
        events = [{'type': 'grok.session.started', 'session_id': session_id, 'model': retained.model},
                  {'type': 'grok.result', 'session_id': session_id, 'stop_reason': 'end_turn',
                   'output': 'Useful answer.'}]
        stdout = '\n'.join(map(json.dumps, events))
        if native_tools is False:
            assert runtime._parse_output(record, stdout, '', retained) == (session_id, 'Useful answer.')
            events[-1]['session_id'] = 'different-terminal-session'
            with pytest.raises(RuntimeErrorBase, match='native session/model'):
                runtime._parse_output(record, '\n'.join(map(json.dumps, events)), '', retained)
        else:
            with pytest.raises(RuntimeErrorBase, match='resumed a different native session'):
                runtime._parse_output(record, stdout, '', retained)
            assert runtime._parse_output(record, stdout, '', info(tmp_path, session_id)) == (session_id, 'Useful answer.')


def test_parser_settles_the_report_like_the_other_harnesses(tmp_path):
    runtime = GrokBuildRuntime(str(tmp_path))
    started = {'type':'grok.session.started','session_id':'native-id','model':'grok-selected'}
    for output, stored in (('Working.\n\nFINAL REPORT:\nGrok report.', 'Grok report.'),
                           ('Plain answer.', 'Plain answer.'),
                           ('Working.\n\nFINAL REPORT:\n', '')):
        events = [started, {'type':'grok.result','session_id':'native-id','stop_reason':'end_turn','output':output}]
        assert runtime._parse_output(worker(tmp_path), '\n'.join(map(json.dumps,events)), '', info(tmp_path, 'native-id'))[1] == stored


def test_run_evidence_reads_the_report_from_the_typed_grok_terminal_event(tmp_path):
    from workers_projects_runtime.run_evidence import build_run_evidence

    runtime = GrokBuildRuntime(str(tmp_path))
    events = [{'type':'grok.session.started','session_id':'native-id','model':'grok-selected'},
              {'type':'grok.result','session_id':'native-id','stop_reason':'end_turn','output':'Working.\n\nFINAL REPORT:\nDone.'}]
    stdout = '\n'.join(map(json.dumps, events))
    _, output = runtime._parse_output(worker(tmp_path), stdout, '', info(tmp_path, 'native-id'))
    evidence = build_run_evidence(
        worker={'worker_id': 'wrk_grok_evidence', 'profile': 'grok-build', 'execution_mode': 'docker'},
        run_id='run_grok_evidence', runtime_name='grok-build', model='grok-selected', command=['grok'], env={},
        workspace_dir=tmp_path, stdout_text=stdout, stderr_text='', output_text=output, error_text='', exit_code=0,
        timeout_seconds=None, stop_reason='process_exit', constraint_ledger=None)

    assert output == 'Done.'
    assert evidence['final_output']['has_final_report'] is True


def test_runner_subprocess_preserves_native_protocol_and_resumes(tmp_path):
    # This peer is a protocol fixture, not evidence of provider or account parity.
    fake = tmp_path/'grok-fixture'
    fake.write_text('''#!'''+sys.executable+'''
import json,sys
assert sys.argv[1:]==['agent','--no-leader','--model','grok-selected','stdio']
for line in sys.stdin:
 r=json.loads(line); method=r['method']
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'agentVersion':'fixture','defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate': result={}
 elif method=='session/load':
  assert r['params']['sessionId']=='native-id'
  result={'sessionId':'native-id','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  assert r['params']['prompt']==[{'type':'text','text':'exact task'}]
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-id','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'answer'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 else: raise Exception(method)
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    fake.chmod(0o700)
    runtime = HostGrokBuildRuntime(str(tmp_path/'state'))
    runtime.binary = str(fake)
    command = runtime._grok_command(worker(tmp_path), info(tmp_path,'native-id'), host=True)
    result = subprocess.run(command, input='exact task', capture_output=True, text=True,
                            env={**os.environ,'GROK_HOME':str(tmp_path/'private-grok')}, timeout=5)
    assert result.returncode == 0, result.stderr
    assert runtime._parse_output(worker(tmp_path),result.stdout,result.stderr,info(tmp_path,'native-id')) == ('native-id','answer')


def test_host_adapter_runs_inside_real_supervisor_and_persists_exact_session(tmp_path, monkeypatch):
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'missing-host-auth'))
    fake = tmp_path/'native-fixture'
    fake.write_text('''#!'''+sys.executable+'''
import json,sys
for line in sys.stdin:
 r=json.loads(line);method=r['method']
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate': result={}
 elif method in ('session/new','session/load'): result={'sessionId':'native-supervised','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-supervised','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Completed the requested synthetic task.'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 else: raise Exception(method)
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    fake.chmod(0o700)
    runtime=HostGrokBuildRuntime(str(tmp_path/'data'))
    runtime.binary=str(fake)
    record={**worker(tmp_path),'_run_attempt_id':'attempt-1','workspace_root':str(tmp_path/'workspace-root')}
    home=runtime._grok_home(record)
    home.mkdir(parents=True)
    (home/'auth.json').write_text('{}')
    output=runtime.run_task(record,'Complete the synthetic task.',timeout_sec=10,run_id='run-real-supervisor')
    assert output == 'Completed the requested synthetic task.'
    assert runtime._read_session_key(record['worker_id']) == 'native-supervised'
    assert not runtime._active_pid(record['worker_id'])
    run_root=runtime._run_root(record['worker_id'],'run-real-supervisor')
    assert (run_root/'exit_code').read_text().strip()=='0'
    assert 'grok.session.started' in (run_root/'stdout.log').read_text()


def test_container_command_does_not_reuse_host_binary_path(tmp_path, monkeypatch):
    runtime=GrokBuildRuntime(str(tmp_path/'data'))
    runtime.binary='/host-only/grok'
    monkeypatch.delenv('WPR_GROK_CONTAINER_BIN',raising=False)
    command=runtime._grok_command(worker(tmp_path),info(tmp_path),host=False)
    assert command[command.index('--binary')+1]=='grok'
    assert '/host-only/grok' not in command


def test_reviewed_artifact_mismatch_fails_before_native_launch(tmp_path, monkeypatch):
    runtime=HostGrokBuildRuntime(str(tmp_path/'data'))
    runtime.binary=sys.executable
    monkeypatch.setenv('WPR_GROK_REVIEWED_BINARY_SHA256','0'*64)
    command=runtime._grok_command(worker(tmp_path),info(tmp_path),host=True)
    result=subprocess.run(command,input='task',text=True,capture_output=True,
                          env={**os.environ,'GROK_HOME':str(tmp_path/'private-grok')},timeout=5)
    assert result.returncode==2
    assert 'does not match' in result.stderr
    assert not result.stdout


def test_docker_launch_preserves_private_grok_selectors_and_discovery_policy():
    from workers_projects_runtime.docker_sandbox import _safe_docker_exec_env
    expected = {
        "GROK_HOME": "/workspace/member/home/.grok",
        "GROK_SESSION_SUMMARY_MODEL": "grok-4.7",
        "GROK_AUTH_PATH": "/workspace/member/home/.grok/auth.json",
        "GROK_DISABLE_AUTOUPDATER": "1",
        "GROK_TELEMETRY_ENABLED": "false",
        "GROK_TELEMETRY_TRACE_UPLOAD": "false",
        "GROK_EXTERNAL_OTEL": "0",
        "GROK_CURSOR_MCPS_ENABLED": "false",
        "GROK_CLAUDE_MCPS_ENABLED": "false",
    }
    assert _safe_docker_exec_env({**expected, "UNRELATED_SECRET": "synthetic", "LD_PRELOAD": "untrusted"}) == expected


def test_runner_preserves_managed_home_acl(tmp_path, monkeypatch):
    from workers_projects_runtime import grok_acp_runner as runner
    home = tmp_path / "grok-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GROK_HOME", str(home))
    original = Path.chmod
    def deny_home_chmod(path, *args, **kwargs):
        if path == home:
            raise PermissionError("service-owned ACL")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "chmod", deny_home_chmod)
    monkeypatch.setattr(runner, "run", lambda args: 0)
    assert runner.main(["--binary", "grok", "--model", "synthetic", "--managed-home"]) == 0
    with pytest.raises(PermissionError):
        runner.main(["--binary", "grok", "--model", "synthetic"])


PERMISSION_FIXTURE = '''#!PYTHON
import json,sys
for line in sys.stdin:
 r=json.loads(line); method=r.get('method')
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'agentVersion':'fixture','defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate': result={}
 elif method=='session/new': result={'sessionId':'native-id','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','id':'perm-1','method':'session/request_permission','params':{'sessionId':'native-id','toolCall':{'title':'Run a command'},'options':[{'optionId':'allow','kind':'allow_once','name':'Allow'},{'optionId':'reject','kind':'reject_once','name':'No'}]}}),flush=True)
  reply={}
  while reply.get('id')!='perm-1': reply=json.loads(sys.stdin.readline())
  selected=reply['result']['outcome'].get('optionId')=='allow'
  if selected: print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-id','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'done'}}}}),flush=True)
  result={'stopReason':'end_turn' if selected else 'cancelled'}
 else: continue
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
'''


@pytest.mark.parametrize('expired', [False, True])
def test_runner_mission_deadline_covers_native_permission_and_acp_backstop(tmp_path, monkeypatch, capfd, expired):
    import io, threading, time
    from workers_projects_runtime import grok_acp_runner as runner
    from workers_projects_runtime.grok_control import submit_control
    fake = tmp_path / 'grok-fixture'
    fake.write_text(PERMISSION_FIXTURE.replace('PYTHON', sys.executable)); fake.chmod(0o700)
    root = tmp_path / 'control'
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'home'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('task'))
    deadline_at = time.time() + (-1 if expired else 120)
    clients, mailboxes = [], []
    original_client, original_mailbox = runner.AcpClient, runner.ControlMailbox
    def client(*args, **kwargs):
        value = original_client(*args, **kwargs); clients.append(value); return value
    def mailbox(*args, **kwargs):
        value = original_mailbox(*args, **kwargs); mailboxes.append(value); return value
    monkeypatch.setattr(runner, 'AcpClient', client)
    monkeypatch.setattr(runner, 'ControlMailbox', mailbox)
    failures = []
    def owner():
        try:
            stop_at = time.monotonic() + 5
            while time.monotonic() < stop_at:
                paths = list(root.glob('*.pending'))
                if paths:
                    question = json.loads(paths[0].read_text())
                    assert question['expires_at'] == deadline_at
                    reply = submit_control(root, {'run_id': 'run-1', 'attempt_id': 'attempt-1',
                        'session_id': 'native-id', 'action': 'permission',
                        'request_id': question['request_id'], 'option_id': 'allow'})
                    assert reply['status'] == 'permission_submitted'
                    return
                time.sleep(.02)
            raise AssertionError('native question was not published')
        except BaseException as exc:
            failures.append(exc)
    thread = threading.Thread(target=owner) if not expired else None
    if thread:
        thread.start()
    returned = runner.main(['--binary', str(fake), '--model', 'grok-selected',
        '--control-dir', str(root), '--run-id', 'run-1', '--attempt-id', 'attempt-1',
        '--permission-deadline-at', str(deadline_at)])
    if thread:
        thread.join(5); assert not thread.is_alive()
    assert not failures
    events = [json.loads(line) for line in capfd.readouterr().out.splitlines() if line.startswith('{')]
    assert clients[0].permission_timeout == mailboxes[0].permission_timeout + 5
    started = next(event for event in events if event['type'] == 'grok.session.started')
    assert started['input_authority_remaining_seconds'] == round(mailboxes[0].permission_timeout, 3)
    if expired:
        assert returned == 2 and events[-1]['failure_class'] == 'native_input_expired'
        assert not any(event['type'] == 'grok.permission.requested' for event in events)
    else:
        assert returned == 0 and events[-1]['output'] == 'done'
        assert 100 < mailboxes[0].permission_timeout <= 120


@pytest.mark.parametrize('answer,code,failure_class,message', [
    (None, 2, 'native_input_expired',
     'Grok stopped because its request for your response expired after 0.4 seconds without an answer.'),
    ({}, 2, 'native_input_cancelled', 'Grok stopped because you cancelled its request.'),
    ({'action':'cancel'}, 2, 'native_turn_cancelled', 'Grok stopped because its turn was cancelled.'),
    ({'option_id':'reject'}, 2, 'native_input_declined', 'Grok stopped after you declined its request.'),
    ({'option_id':'allow'}, 0, None, None),
])
def test_runner_reports_why_a_native_request_ended_the_turn(tmp_path, monkeypatch, capfd, answer, code, failure_class, message):
    import functools, io, threading, time
    from workers_projects_runtime import grok_acp_runner as runner
    from workers_projects_runtime.grok_control import submit_control
    fake = tmp_path/'grok-fixture'
    fake.write_text(PERMISSION_FIXTURE.replace('PYTHON', sys.executable))
    fake.chmod(0o700)
    control = tmp_path/'control'
    monkeypatch.setenv('GROK_HOME', str(tmp_path/'home'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('task'))
    monkeypatch.setattr(runner, 'ControlMailbox', functools.partial(runner.ControlMailbox, permission_timeout=.4))
    def owner():
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            pending = list(control.glob('*.pending')) if control.is_dir() else []
            if pending:
                request = json.loads(pending[0].read_text())
                body = {'run_id':'run-1','attempt_id':'attempt-1','session_id':'native-id'}
                body.update({'action':'permission','request_id':request['request_id']})
                body.update(answer)
                submit_control(control, body)
                return
            time.sleep(.02)
    thread = threading.Thread(target=owner) if answer is not None else None
    if thread: thread.start()
    returned = runner.main(['--binary', str(fake), '--model', 'grok-selected', '--control-dir', str(control),
                            '--run-id', 'run-1', '--attempt-id', 'attempt-1'])
    if thread: thread.join(5)
    captured = capfd.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines() if line.startswith('{')]
    assert returned == code, captured.err
    if message is None:
        assert events[-1]['type'] == 'grok.result' and events[-1]['output'] == 'done'
        return
    error = events[-1]
    assert (error['type'], error['failure_class'], error['message']) == ('grok.error', failure_class, message)
    assert captured.err.strip().splitlines()[-1] == message


@pytest.mark.parametrize('failure_class,message', [
    ('native_input_expired', 'Grok stopped because its request for your response expired without an answer.'),
    ('native_input_declined', 'Grok stopped after you declined its request.'),
    ('native_input_cancelled', 'Grok stopped because you cancelled its request.'),
    ('native_turn_cancelled', 'Grok stopped because its turn was cancelled.'),
])
def test_typed_native_stop_is_the_run_failure_truth(failure_class, message):
    from workers_projects_runtime.failure_classification import classify_cli_failure
    stdout = '\n'.join(json.dumps(event) for event in (
        {'type':'grok.session.started','session_id':'native-id','model':'grok-selected'},
        {'type':'grok.error','session_id':'native-id','code':None,'failure_class':failure_class,'message':'runner text'},
    ))
    result = classify_cli_failure(stdout=stdout, stderr='runner text', runtime_name='grok-build', exit_code=2)
    assert (result.failure_class, result.user_message, result.retryable, result.structured) == (failure_class, message, False, True)
    assert 'new instruction' in result.recommended_recovery
    # Only the Grok runner's typed event counts: another runtime or an unknown class stays unclassified.
    assert classify_cli_failure(stdout=stdout, stderr='x', runtime_name='codex-cli', exit_code=2).failure_class != failure_class
    other = stdout.replace(failure_class, 'native_something_else')
    assert classify_cli_failure(stdout=other, stderr='x', runtime_name='grok-build', exit_code=2).failure_class == 'unknown'


def test_runner_records_effective_standard_before_one_prompt(tmp_path, monkeypatch, capfd):
    import io
    from workers_projects_runtime import grok_acp_runner as runner
    binary = tmp_path / 'grok'
    binary.write_text("""#!/usr/bin/env python3
import sys,json
assert sys.argv[1:]==['agent','--no-leader','--model','grok-4.7-build-fast','stdio']
for line in sys.stdin:
    request=json.loads(line); method=request['method']
    if method=='initialize':
        result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},
          'authMethods':[{'id':'cached_token'}], '_meta':{'grokShell':True,'defaultAuthMethodId':'cached_token'}}
    elif method=='authenticate': result={}
    elif method=='session/new':
        result={'sessionId':'account-session','models':{'currentModelId':'grok-4.7'},
          'configOptions':[{'id':'model','currentValue':'grok-4.7','options':[{'value':'grok-4.7'}]},
                           {'id':'reasoning_effort','currentValue':'medium','options':[{'value':'high'}]}]}
    elif method=='session/set_config_option':
        assert request['params']['configId']=='reasoning_effort' and request['params']['value']=='high'
        result={'configOptions':[{'id':'reasoning_effort','currentValue':'high'}]}
    elif method=='session/prompt':
        assert request['params']['prompt']==[{'type':'text','text':'Synthetic goal.'}]
        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'account-session',
             'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Useful answer.'}}}}),flush=True)
        result={'stopReason':'end_turn'}
    else: raise AssertionError(method)
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
""")
    binary.chmod(0o700)
    monkeypatch.setenv('GROK_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('XAI_API_KEY', raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('Synthetic goal.'))
    assert runner.main(['--binary', str(binary), '--model', 'grok-4.7-build-fast', '--effort', 'high']) == 0
    captured = capfd.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    session_index = next(i for i,e in enumerate(records) if e['type']=='grok.session.started')
    prompt_index = next(i for i,e in enumerate(records) if e.get('phase')=='prompt_started')
    assert session_index < prompt_index
    assert records[session_index]['model']=='grok-4.7'
    assert records[session_index]['requested_model']=='grok-4.7-build-fast'
    assert len([e for e in records if e['type']=='grok.result']) == 1
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    exact_info = info(tmp_path)
    exact_info.model = 'grok-4.7-build-fast'
    assert runtime._parse_output(worker(tmp_path), captured.out, captured.err, exact_info) == ('account-session', 'Useful answer.')


@pytest.mark.parametrize('requested,effective,expected', [
    ('grok-4.7-build-fast', 'grok-4.7', True),
    ('grok-4.7-build-fast', 'grok-4.7-build-fast', True),
    ('other-model', 'grok-4.7', False),
    ('grok-4.7-build-fast', 'other-model', False),
    ('grok-4.7', 'grok-4.7', False),
    (None, 'grok-4.7', False),
])
def test_parser_accepts_only_exact_authored_optional_model_pair(tmp_path, requested, effective, expected):
    runtime=HostGrokBuildRuntime(str(tmp_path))
    selected_info=info(tmp_path, 'native-id')
    selected_info.model='grok-4.7-build-fast'
    session={'type':'grok.session.started','session_id':'native-id','model':effective}
    if requested is not None: session['requested_model']=requested
    stdout='\n'.join(map(json.dumps,[session,{'type':'grok.result','session_id':'native-id',
                                             'stop_reason':'end_turn','output':'Useful answer.'}]))
    if expected:
        assert runtime._parse_output(worker(tmp_path),stdout,'',selected_info)==('native-id','Useful answer.')
    else:
        with pytest.raises(RuntimeErrorBase, match='configured native session/model'):
            runtime._parse_output(worker(tmp_path),stdout,'',selected_info)


def test_native_grok_authority_is_private_exact_and_rejects_changed_bytes(tmp_path, monkeypatch):
    import hashlib
    runtime = HostGrokBuildRuntime(str(tmp_path / 'data'))
    record = worker(tmp_path)
    authority = 'Synthetic system authority.\n<viventium_feeling_state>synthetic</viventium_feeling_state>'
    record['trusted_run_lane'] = 'conversation'
    record['bootstrap_bundle_json'] = json.dumps({'developer_instructions': authority, 'run_mode': 'conversation'})
    command = runtime._grok_command(record, info(tmp_path), host=True)
    path = Path(command[command.index('--system-prompt-file') + 1])
    assert path.read_text() == authority
    assert path.stat().st_mode & 0o777 == 0o600
    assert authority not in command
    receipt = runtime._native_provider_authority_receipt(record, command=command, run_id='run-authority', model='grok-selected')
    assert receipt['placement'] == 'grok_system_prompt_override'
    assert receipt['authority_sha256'] == hashlib.sha256(authority.encode()).hexdigest()
    assert receipt['feeling_capsule_count'] == 1
    path.write_text('Changed authority')
    with pytest.raises(RuntimeErrorBase, match='differs from the request-pinned'):
        runtime._native_provider_authority_receipt(record, command=command, run_id='run-authority', model='grok-selected')


def test_native_grok_materializes_and_checks_the_declared_conversation_output_schema(tmp_path):
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    runtime = HostGrokBuildRuntime(str(tmp_path / 'data'))
    record = worker(tmp_path)
    record['trusted_run_lane'] = 'conversation'
    control = {'version': 1, 'tools': [{'name': 'lc_transfer_to_specialist', 'description': 'Consult specialist.'}]}
    record['bootstrap_bundle_json'] = json.dumps({'developer_instructions': 'Synthetic authority.',
        'run_mode': 'conversation', 'agent_builder_control': control})
    command = runtime._grok_command(record, info(tmp_path), host=True)
    path = Path(command[command.index('--output-schema-file') + 1])
    assert json.loads(path.read_text()) == conversation_output_schema(control, None)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.read_text() not in command
    assert runtime._native_provider_authority_receipt(record, command=command,
        run_id='run-schema', model='grok-selected')['materialized'] is True
    path.write_text('{}')
    with pytest.raises(RuntimeErrorBase, match='output schema differs from the request-pinned'):
        runtime._native_provider_authority_receipt(record, command=command,
            run_id='run-schema', model='grok-selected')


@pytest.mark.parametrize('lane,run_mode,access,run_id,attempt_id,expected', [
    ('conversation', 'conversation', 'full', 'run-owned', 'attempt-owned', 'true'),
    ('conversation', 'conversation', 'workspace', 'run-owned', 'attempt-owned', 'false'),
    ('conversation', 'conversation', None, 'run-owned', 'attempt-owned', 'false'),
    ('conversation', 'conversation', 'unknown', 'run-owned', 'attempt-owned', 'false'),
    ('conversation', 'conversation', 'FULL', 'run-owned', 'attempt-owned', 'false'),
    ('mission', 'conversation', 'full', 'run-owned', 'attempt-owned', None),
    (None, 'conversation', 'full', 'run-owned', 'attempt-owned', None),
    ('conversation', 'mission', 'full', 'run-owned', 'attempt-owned', None),
    ('conversation', 'conversation', 'full', '', 'attempt-owned', None),
    ('conversation', 'conversation', 'full', 'run-owned', '', None),
])
def test_only_admitted_conversation_attempt_maps_native_permission_mode(
    tmp_path, lane, run_mode, access, run_id, attempt_id, expected
):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'state'))
    record = {**worker(tmp_path), 'trusted_run_lane': lane,
              '_active_run_id': run_id, '_run_attempt_id': attempt_id,
              'bootstrap_bundle_json': json.dumps({'run_mode': run_mode, 'access_mode': access})}
    command = runtime._grok_command(record, info(tmp_path), host=True)
    if expected is None:
        assert '--yolo-mode' not in command
    else:
        assert command[command.index('--yolo-mode') + 1] == expected
    assert '--allow-mcp-tool' not in command
    assert command[command.index('--model') + 1] == 'grok-selected'


@pytest.mark.parametrize('session_id', [None, 'native-id'])
@pytest.mark.parametrize('access', ['full', 'workspace'])
def test_materialized_runner_maps_only_native_session_permission_mode(
    tmp_path, monkeypatch, session_id, access
):
    monkeypatch.setenv('WPR_GROK_REASONING_EFFORT', '')
    fake = tmp_path / 'native-permission-fixture'
    expected_meta = {'systemPromptOverride': 'Exact synthetic developer authority.',
                     'yoloMode': access == 'full'}
    if session_id:
        expected_meta['noReplay'] = True
    if access != 'full':
        expected_meta['autoMode'] = False
    fake.write_text('#!' + sys.executable + '\n' + '''
import json,os,sys
assert sys.argv[1:]==['agent','--no-leader','--model','grok-selected','stdio']
assert os.environ['GROK_SUBAGENTS']=='0'
assert os.environ['GROK_WORKFLOWS']=='0'
expected_meta=EXPECTED_META
for line in sys.stdin:
 r=json.loads(line); method=r['method']
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},'authMethods':[{'id':'cached_token'}],'_meta':{'grokShell':True,'defaultAuthMethodId':'cached_token'}}
 elif method=='authenticate': result={}
 elif method in ('session/new','session/load'):
  assert r['params']['_meta']==expected_meta
  if method=='session/load': assert r['params']['sessionId']=='native-id'
  result={'sessionId':'native-id','models':{'currentModelId':'grok-selected'}}
 elif method=='session/prompt':
  assert r['params']['prompt']==[{'type':'text','text':'exact task'}]
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-id','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'answer'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 else: raise Exception(method)
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
'''.replace('EXPECTED_META', repr(expected_meta)))
    fake.chmod(0o700)
    runtime = HostGrokBuildRuntime(str(tmp_path / 'state'))
    runtime.binary = str(fake)
    record = {**worker(tmp_path), 'trusted_run_lane': 'conversation',
              '_active_run_id': 'run-owned', '_run_attempt_id': 'attempt-owned',
              'bootstrap_bundle_json': json.dumps({'run_mode': 'conversation', 'access_mode': access,
                  'developer_instructions': 'Exact synthetic developer authority.'})}
    command = runtime._grok_command(record, info(tmp_path, session_id), host=True)
    monkeypatch.setattr(runtime, '_host_env', lambda _: {'GROK_SUBAGENTS': '1', 'GROK_WORKFLOWS': '1'})
    monkeypatch.setattr(runtime, '_current_host_auth_path', lambda _: None)
    native_home = runtime._grok_home(record)
    native_home.mkdir(parents=True, exist_ok=True)
    (native_home / 'auth.json').write_text('{}')
    env = runtime._native_environment(record, host=True)
    result = subprocess.run(command, input='exact task', capture_output=True, text=True,
                            env={**os.environ, **env}, timeout=5)
    assert result.returncode == 0, result.stderr
    assert runtime._parse_output(record, result.stdout, result.stderr, info(tmp_path, session_id)) == ('native-id', 'answer')
