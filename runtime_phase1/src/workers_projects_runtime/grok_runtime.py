"""Native Grok adapter on the retained worker runtime/supervisor plane."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import sys

from .bootstrap import bootstrap_env_for, bootstrap_bundle_for
from .mission_provider_accounts import apply_bound_provider_account_environment
from .openclaw_runtime import ProviderAuthenticationMissingError, RuntimeConfigurationError, RuntimeDependencyMissingError, RuntimeErrorBase
from .profile_runtime import BaseCliWorkerRuntime, HostNativeCliMixin, _atomic_write_private_text


class GrokBuildRuntime(BaseCliWorkerRuntime):
    runtime_name = 'grok-build'
    worker_root_name = 'grok_build_runtime'
    binary_env_var = 'WPR_GROK_BIN'
    binary_name = 'grok'

    def resolve_model(self, profile: str) -> str:
        if profile != 'grok-build':
            raise RuntimeErrorBase('Unsupported profile for Grok Build')
        model = os.environ.get('WPR_MODEL_GROK_BUILD', '').strip()
        if not model:
            raise RuntimeConfigurationError('Configure an exact Grok model with WPR_MODEL_GROK_BUILD')
        return model

    def _agent_type(self):
        return 'grok'

    def _default_session_key(self, worker):
        return self._read_session_key(worker['worker_id']) or worker.get('session_key')

    def _command_stdin_text(self, worker, instruction, info):
        return self._instruction_with_completion_contract(instruction)

    def _runner_files(self, worker):
        target = self._home_dir(worker['worker_id']) / '.xperfect-grok'
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.chmod(0o700)
        for name in ('grok_acp.py', 'grok_acp_runner.py', 'grok_control.py'):
            _atomic_write_private_text(target / name, Path(__file__).with_name(name).read_text())
        return target

    def _grok_home(self, worker):
        return self._home_dir(worker['worker_id']) / '.grok'

    def _native_environment(self, worker, *, host):
        env = self._host_env(worker) if host else self._container_env_for_worker(worker)
        if not host:
            env.update(bootstrap_env_for(worker))
        # Native state must never follow a caller-supplied GROK_HOME into another worker.
        home = self._grok_home(worker)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        env.pop('GROK_AUTH_PATH', None)
        env['GROK_HOME'] = str(home) if host else f'{self.sandbox.home_mount}/.grok'
        env['GROK_DISABLE_AUTOUPDATER'] = '1'
        env['GROK_TELEMETRY_ENABLED'] = 'false'
        env['GROK_TELEMETRY_TRACE_UPLOAD'] = 'false'
        env['GROK_EXTERNAL_OTEL'] = '0'
        env['GROK_CURSOR_MCPS_ENABLED'] = 'false'
        env['GROK_CLAUDE_MCPS_ENABLED'] = 'false'
        for key in tuple(env):
            if key.startswith('OTEL_') or key in ('GROK_TELEMETRY_GCS_BUCKET', 'GROK_AGENT_METADATA'):
                env.pop(key)
        apply_bound_provider_account_environment(worker, env, runtime_name=self.runtime_name)
        auth_path = Path(env.get('GROK_AUTH_PATH') or home / 'auth.json')
        projected_key = False
        if not host and env.get('GROK_AUTH_PATH'):
            if worker.get('_glasshive_provider_account_projected'):
                if env['GROK_AUTH_PATH'] != f'{self.sandbox.home_mount}/.grok/auth.json':
                    raise RuntimeErrorBase('Grok projected credential placement changed')
                auth_path = home / 'auth.json'
                secret_files = worker.get('_glasshive_provider_account_secret_files') or {}
                if secret_files:
                    expected = f'{self.sandbox.home_mount}/.xperfect/credentials/grok-api-key.json'
                    if secret_files != {'XAI_API_KEY': expected}:
                        raise RuntimeErrorBase('Grok projected key placement changed')
                    projected_key = (self._home_dir(worker['worker_id']) / '.xperfect/credentials/grok-api-key.json').is_file()
            else:
                mount = str(worker.get('_glasshive_provider_account_mount_host') or '')
                if not mount:
                    raise RuntimeErrorBase('Grok account credential mount is missing')
                auth_path = Path(mount) / 'grok' / 'auth.json'
        if not env.get('XAI_API_KEY') and not projected_key and not auth_path.is_file():
            raise ProviderAuthenticationMissingError(
                'Grok authentication is missing. Connect the selected account in this worker’s private Grok home or provide an authorized XAI_API_KEY.',
                binary=self.binary, runtime_name=self.runtime_name, profile='grok-build',
                execution_mode='host' if host else 'docker', dependency_label='Grok authentication')
        if env.get('XAI_API_KEY') and auth_path.exists():
            try:
                stored = json.loads(auth_path.read_text())
            except (OSError, ValueError) as exc:
                raise RuntimeErrorBase('Grok private authentication state is unreadable') from exc
            if not isinstance(stored, dict) or any(
                not isinstance(entry, dict) or entry.get('auth_mode') != 'api_key'
                or entry.get('key') != env['XAI_API_KEY'] for entry in stored.values()
            ):
                raise RuntimeErrorBase('Grok has conflicting authentication sources; reconnect the selected account')
        return env

    def _grok_command(self, worker, info, *, host):
        target = self._runner_files(worker)
        runner = str(target / 'grok_acp_runner.py') if host else f'{self.sandbox.home_mount}/.xperfect-grok/grok_acp_runner.py'
        binary = self.binary if host else os.environ.get('WPR_GROK_CONTAINER_BIN', 'grok')
        command = [sys.executable if host else 'python3', runner, '--binary', binary,
                   '--model', str(info.model)]
        if not host and getattr(self.sandbox, 'box', None) is not None:
            command.append('--managed-home')
        run_id = str(worker.get('_active_run_id') or '')
        attempt_id = str(worker.get('_run_attempt_id') or '')
        if run_id and attempt_id:
            control = self._control_dir(worker, run_id, attempt_id)
            if not host and getattr(self.sandbox, 'box', None) is not None:
                control.mkdir(parents=True, exist_ok=True, mode=0o700)
            control_path = str(control) if host else f'{self.sandbox.home_mount}/.xperfect-grok/controls/{control.name}'
            command += ['--control-dir', control_path, '--run-id', run_id, '--attempt-id', attempt_id]
        reviewed = os.environ.get('WPR_GROK_REVIEWED_BINARY_SHA256' if host else 'WPR_GROK_CONTAINER_REVIEWED_BINARY_SHA256', '')
        if reviewed:
            command += ['--reviewed-binary-sha256', reviewed]
        if info.session_key:
            command += ['--session-id', info.session_key]
        effort = self._bootstrap_env_value(worker, 'WPR_GROK_REASONING_EFFORT') or os.environ.get('WPR_GROK_REASONING_EFFORT', '')
        if effort:
            command += ['--effort', effort]
        bundle = bootstrap_bundle_for(worker)
        from .grok_projection import mcp_servers_for_bundle
        try:
            servers = mcp_servers_for_bundle(bundle, self._native_environment(worker, host=host) if bundle.get('claude_project_mcp') else {})
        except ValueError as exc:
            raise RuntimeErrorBase(str(exc)) from exc
        if servers is not None:
            if not isinstance(servers, list) or any(not isinstance(server, dict) for server in servers):
                raise RuntimeErrorBase('grok_mcp_servers must contain native ACP MCP server objects')
            path = target / 'mcp-servers.json'
            _atomic_write_private_text(path, json.dumps(servers))
            command += ['--mcp-file', str(path) if host else f'{self.sandbox.home_mount}/.xperfect-grok/mcp-servers.json']
            projection = worker.get('_coordinator_native_projection')
            if (isinstance(projection, dict)
                    and projection.get('worker_id') == worker.get('worker_id')
                    and projection.get('run_id') == run_id
                    and any(server.get('name') == 'xperfect-coordinator'
                            and server.get('url') == projection.get('url')
                            and {'name': 'Authorization', 'value': 'Bearer ' + str(projection.get('token') or '')}
                            in server.get('headers', []) for server in servers)):
                from .coordinator_mcp import coordinator_tool_manifest
                for name in coordinator_tool_manifest()['tools']:
                    command += ['--allow-mcp-tool', 'xperfect-coordinator__' + name]
        return command

    def _control_dir(self, worker, run_id, attempt_id):
        identity = hashlib.sha256(json.dumps([run_id, attempt_id]).encode()).hexdigest()
        return self._home_dir(worker['worker_id']) / '.xperfect-grok' / 'controls' / identity

    def native_control_state(self, worker, *, run_id, attempt_id):
        from .grok_control import read_message
        import time
        active = self._read_active_session(worker['worker_id']) or {}
        if active.get('run_id') != run_id or active.get('attempt_id') != attempt_id:
            raise RuntimeErrorBase('Grok control does not match the active run attempt')
        root = self._control_dir(worker, run_id, attempt_id)
        pending=[]
        for path in list(root.glob('*.pending'))[:32]:
            try:
                value=read_message(path)
            except (OSError, ValueError):
                continue
            if value.get('expires_at',0)>time.time():
                pending.append(value)
        receipts=[]
        for path in list(root.glob('*.response'))[:32]:
            try:
                receipts.append({'message_id':path.stem, **read_message(path)})
            except (OSError, ValueError):
                continue
        return {'run_id':run_id,'attempt_id':attempt_id,'pending_requests':pending,'receipts':receipts}

    def native_control(self, worker, *, run_id, attempt_id, action, payload=None):
        from .grok_control import submit_control
        active = self._read_active_session(worker['worker_id']) or {}
        if not run_id or not attempt_id or active.get('run_id') != run_id or active.get('attempt_id') != attempt_id:
            raise RuntimeErrorBase('Grok control does not match the active run attempt')
        session_id = self._read_session_key(worker['worker_id'])
        if not session_id:
            raise RuntimeErrorBase('Grok native session is not yet ready for controls')
        body = dict(payload or {})
        body.update(run_id=run_id, attempt_id=attempt_id, session_id=session_id, action=action)
        return submit_control(self._control_dir(worker, run_id, attempt_id), body,
            managed_acl=getattr(self.sandbox, 'box', None) is not None)

    def _build_command(self, worker, instruction, info):
        return self._grok_command(worker, info, host=False), self._native_environment(worker, host=False)

    @staticmethod
    def _events(stdout):
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event

    def _parse_output(self, worker, stdout, stderr, info):
        sessions = [event for event in self._events(stdout) if event.get('type') == 'grok.session.started']
        results = [event for event in self._events(stdout) if event.get('type') == 'grok.result']
        if len(sessions) != 1 or len(results) != 1:
            raise RuntimeErrorBase('Grok did not return one native session and terminal result')
        session, result = sessions[0], results[0]
        if (session.get('model') != info.model or not isinstance(session.get('session_id'), str)
                or not session['session_id'] or result.get('session_id') != session['session_id']
                or result.get('stop_reason') != 'end_turn' or not isinstance(result.get('output'), str)):
            raise RuntimeErrorBase('Grok terminal result does not match its configured native session/model')
        if info.session_key and session['session_id'] != info.session_key:
            raise RuntimeErrorBase('Grok resumed a different native session')
        return session['session_id'], result['output']

    def _stdout_has_complete_response(self, stdout_path):
        try:
            events = list(self._events(stdout_path.read_text()))
        except OSError:
            return False
        results = [event for event in events if event.get('type') == 'grok.result']
        return len(results) == 1 and results[0].get('stop_reason') == 'end_turn'

    def preflight_worker_profile(self, profile, execution_mode='docker'):
        if profile != 'grok-build':
            raise RuntimeErrorBase('Unsupported Grok Build profile')
        # Container images must explicitly include the reviewed native binary.
        if execution_mode == 'host' and shutil.which(self.binary) is None:
            raise RuntimeDependencyMissingError('Grok Build executable is missing', binary=self.binary,
                runtime_name=self.runtime_name, profile=profile, execution_mode=execution_mode)


class HostGrokBuildRuntime(HostNativeCliMixin, GrokBuildRuntime):
    worker_root_name = 'host_grok_build_runtime'

    def _agent_type(self):
        return 'grok'

    def preflight_worker_profile(self, profile, execution_mode='host'):
        return GrokBuildRuntime.preflight_worker_profile(self, profile, execution_mode)

    def _build_command(self, worker, instruction, info):
        return self._grok_command(worker, info, host=True), self._native_environment(worker, host=True)

    def _command_stdin_text(self, worker, instruction, info):
        return GrokBuildRuntime._command_stdin_text(self, worker, instruction, info)
