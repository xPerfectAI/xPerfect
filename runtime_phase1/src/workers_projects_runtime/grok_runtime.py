"""Native Grok adapter on the retained worker runtime/supervisor plane."""
from __future__ import annotations

import json
import hashlib
import fcntl
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime

from .bootstrap import bootstrap_env_for, bootstrap_bundle_for, native_profile_tool_access_instructions, native_instruction_before_tail, worker_prompt_layer_producer
from .agent_builder_control import conversation_output_schema, conversation_output_contract_instructions, conversation_controls_from_schema
from .mission_provider_accounts import apply_bound_provider_account_environment, native_current_account_allowed
from .auth import _env_bool, multi_user_security_enabled, native_installed_owner_id
from .grok_acp import validated_session_model
from .grok_native_warm import GrokNativeWarmMixin
from .openclaw_runtime import ProviderAuthenticationMissingError, RuntimeConfigurationError, RuntimeDependencyMissingError, RuntimeErrorBase
from .profile_runtime import BaseCliWorkerRuntime, HostNativeCliMixin, _atomic_write_private_text, _native_tools_disabled, tomllib


def _grok_conversation_skill_config(text):
    """Insert one native policy value without rewriting unrelated TOML bytes."""
    skill = 'long-running-background-tasks'
    original = tomllib.loads(text)
    skills = original.get('skills', {})
    if not isinstance(skills, dict):
        raise ValueError('Grok skills configuration must be a table')
    disabled = skills.get('disabled', [])
    if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
        raise ValueError('Grok disabled skills must be a list of names')
    if skill in disabled:
        return text
    expected = {**original, 'skills': {**skills, 'disabled': [*disabled, skill]}}
    value = json.dumps(skill)
    # TOML permits multiline arrays, quoted/dotted tables and inline tables.
    # Accept an insertion only when its parsed document changes this one list.
    # This preserves comments, whitespace and all other caller-owned bytes.
    candidates = []
    if 'skills' not in original:
        candidates.append((len(text), '\n[skills]\ndisabled = [' + value + ']\n'))
    elif 'disabled' not in skills:
        candidates.append((0, 'skills.disabled = [' + value + ']\n'))
        candidates.append((len(text), '\ndisabled = [' + value + ']\n'))
        candidates.extend((i + 1, 'disabled = [' + value + ']\n')
                          for i, char in enumerate(text) if char == '\n')
        candidates.extend((i, prefix + 'disabled = [' + value + ']')
                          for i, char in enumerate(text) if char == '}' for prefix in ('', ', '))
    else:
        candidates.extend((i, addition) for i, char in enumerate(text) if char == ']'
                          for addition in (value, ', ' + value, value + ','))
    for offset, addition in candidates:
        candidate = text[:offset] + addition + text[offset:]
        try:
            if tomllib.loads(candidate) == expected:
                return candidate
        except tomllib.TOMLDecodeError:
            continue
    raise ValueError('Grok skill policy could not preserve the existing TOML document')


def _grok_conversation_output_schema(schema):
    """Couple terminal controls using Grok's supported JSON Schema union."""
    if not schema or 'tool_call' not in schema['properties']['type']['enum']:
        return schema
    branches = []
    for result_type, names in (
        ('assistant_response', [None]),
        ('tool_call', [name for name in schema['properties']['tool_name']['enum'] if name is not None]),
    ):
        branches.append({
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                **schema['properties'],
                'type': {'type': 'string', 'enum': [result_type]},
                'tool_name': {'type': 'null' if result_type == 'assistant_response' else 'string', 'enum': names},
            },
            'required': list(schema['required']),
        })
    return {**schema, 'anyOf': branches}


def _grok_minimal_wire_output_contract(worker, bundle, output_schema):
    return bool(output_schema and any(conversation_controls_from_schema(output_schema))
                and bundle.get('run_mode') == 'conversation'
                and worker.get('trusted_run_lane') == 'conversation'
                and not _native_tools_disabled(bundle))


@worker_prompt_layer_producer("developer_instructions")
def grok_native_authority_instructions(worker, bundle, servers, output_schema):
    """Compose the registered native facts and declared protocol before the pinned tail."""
    instructions = native_profile_tool_access_instructions(
        str(worker.get('profile') or 'grok-build'), bundle, servers,
    )
    if _grok_minimal_wire_output_contract(worker, bundle, output_schema):
        try:
            return native_instruction_before_tail(
                bundle, instructions, conversation_output_contract_instructions(output_schema),
                require_exact_tail=True,
            )
        except ValueError as exc:
            raise RuntimeErrorBase(str(exc)) from exc
    return instructions


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
        if bootstrap_bundle_for(worker).get('run_mode') == 'conversation':
            return instruction
        return self._instruction_with_completion_contract(instruction)

    def _conversation_output_schema(self, worker):
        bundle = bootstrap_bundle_for(worker)
        if bundle.get('run_mode') != 'conversation':
            return None
        return _grok_conversation_output_schema(conversation_output_schema(
            bundle.get('agent_builder_control'), bundle.get('messaging_delivery_control'),
            surface=str(bundle.get('surface') or ''),
            same_turn_continuation=(worker.get('trusted_run_lane') == 'conversation'
                                    and not _native_tools_disabled(bundle))))

    def _runner_files(self, worker):
        target = self._home_dir(worker['worker_id']) / '.xperfect-grok'
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.chmod(0o700)
        for name in ('grok_acp.py', 'grok_acp_runner.py', 'grok_control.py', 'agent_builder_control.py',
                     'grok_websocket.py', 'native_warm_pool.py'):
            _atomic_write_private_text(target / name, Path(__file__).with_name(name).read_text())
        return target

    def _grok_home(self, worker):
        return self._home_dir(worker['worker_id']) / '.grok'

    def _prepare_conversation_skills(self, worker, home):
        # This is the persisted worker lane, not a run-local bootstrap option.
        # Mission homes and the shared native account home are never changed.
        if worker.get('trusted_run_lane') != 'conversation':
            return
        path = home / 'config.toml'
        try:
            if home.is_symlink() or path.is_symlink():
                raise ValueError('Native worker config must not be a symlink')
            # Reuse Grok's own config-init flock so its settings writer cannot
            # race the one-time policy insertion. No per-turn config toggle.
            descriptor = os.open(home / '.config-init.lock',
                                 os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as lock:
                deadline = time.monotonic() + 1
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeErrorBase('Grok native configuration is busy; retry startup')
                        time.sleep(.02)
                if path.is_symlink():
                    raise ValueError('Native worker config changed to a symlink')
                before = path.read_bytes().decode('utf-8') if path.exists() else ''
                after = _grok_conversation_skill_config(before)
                if after != before:
                    _atomic_write_private_text(path, after)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeErrorBase('Grok worker skill configuration could not be preserved safely') from exc

    def _current_host_auth_path(self, worker: dict) -> Path | None:
        # Keep the existing local current-account boundary. Named accounts and
        # hosted runtimes retain their validated credential projection.
        if (multi_user_security_enabled() or native_installed_owner_id() is not None
                or _env_bool('GLASSHIVE_ENTERPRISE_MODE')
                or _env_bool('WPR_ENTERPRISE_MODE')
                or not native_current_account_allowed(worker)
                or bootstrap_env_for(worker).get('XAI_API_KEY')):
            return None
        home = Path(os.environ.get('GROK_HOME') or Path.home() / '.grok')
        source = home / 'auth.json'
        if not source.is_file():
            return None
        try:
            directory = home.lstat()
            credential = source.lstat()
            if (not home.is_absolute() or home.resolve(strict=True) != home
                    or not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                    or stat.S_IMODE(directory.st_mode) & 0o022
                    or source.resolve(strict=True) != source
                    or not stat.S_ISREG(credential.st_mode) or credential.st_uid != os.getuid()
                    or stat.S_IMODE(credential.st_mode) != 0o600):
                raise ValueError('unsafe current-account auth path')
        except (OSError, ValueError) as exc:
            raise RuntimeErrorBase('Grok current-account authentication is unsafe; reconnect the selected account.') from exc
        return source

    def _native_environment(self, worker, *, host):
        env = self._host_env(worker) if host else self._container_env_for_worker(worker)
        if not host:
            env.update(bootstrap_env_for(worker))
        # Native state must never follow a caller-supplied GROK_HOME into another worker.
        home = self._grok_home(worker)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        self._prepare_conversation_skills(worker, home)
        env.pop('GROK_AUTH_PATH', None)
        env['GROK_HOME'] = str(home) if host else f'{self.sandbox.home_mount}/.grok'
        # Native titles have a separate sampler. Keep its default current while
        # preserving an explicit native helper-model choice.
        env.setdefault('GROK_SESSION_SUMMARY_MODEL', 'grok-4.7')
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
        if (_native_tools_disabled(bootstrap_bundle_for(worker)) or
                (str(worker.get('_active_run_id') or '')
                and str(worker.get('_run_attempt_id') or '')
                and str(worker.get('trusted_run_lane') or '').strip().lower() == 'conversation'
                and bootstrap_bundle_for(worker).get('run_mode') == 'conversation')):
            # This process ends with the conversation turn. Durable background
            # delegation remains available through the existing MCP broker.
            env['GROK_SUBAGENTS'] = '0'
            env['GROK_WORKFLOWS'] = '0'
        if host and not env.get('GROK_AUTH_PATH') and not env.get('XAI_API_KEY'):
            source = self._current_host_auth_path(worker)
            if source is not None:
                # Native Grok derives token reads, atomic refresh writes and its
                # refresh lock from this path; sessions stay in private GROK_HOME.
                env['GROK_AUTH_PATH'] = str(source)
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
        admitted_mission = False
        deadline = worker.get('_native_input_deadline_at')
        if str(worker.get('trusted_run_lane') or '') == 'mission' and deadline is not None:
            binding = worker.get('_run_local_capability_binding')
            generation_key = 'hostStartupLeaseId' if host else 'containerGenerationId'
            generation = binding.get(generation_key) if isinstance(binding, dict) else None
            if (not run_id or not attempt_id or not isinstance(binding, dict)
                    or binding.get('workerId') != worker.get('worker_id') or binding.get('runId') != run_id
                    or not isinstance(generation, str) or len(generation) != 64
                    or any(char not in '0123456789abcdef' for char in generation)
                    or (host and generation != worker.get('_run_startup_token_digest'))
                    or isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                    or not math.isfinite(deadline) or deadline <= 0):
                raise RuntimeErrorBase('Grok mission input authority does not match the admitted run')
            admitted_mission = True
            command += ['--permission-deadline-at', str(deadline)]
        reviewed = os.environ.get('WPR_GROK_REVIEWED_BINARY_SHA256' if host else 'WPR_GROK_CONTAINER_REVIEWED_BINARY_SHA256', '')
        if reviewed:
            command += ['--reviewed-binary-sha256', reviewed]
        bundle = bootstrap_bundle_for(worker)
        restricted = _native_tools_disabled(bundle)
        if restricted:
            workspace = Path(tempfile.mkdtemp(prefix='restricted-workspace-', dir=target))
            workspace.chmod(0o700)
            workspace_path = str(workspace) if host else f'{self.sandbox.home_mount}/.xperfect-grok/{workspace.name}'
            command += ['--native-tools-disabled', '--restricted-workspace', workspace_path]
        elif info.session_key:
            command += ['--session-id', info.session_key]
        effort = self._bootstrap_env_value(worker, 'WPR_GROK_REASONING_EFFORT') or os.environ.get('WPR_GROK_REASONING_EFFORT', '')
        if effort:
            command += ['--effort', effort]
        if restricted:
            command += ['--yolo-mode', 'false']
        elif admitted_mission:
            authority = bundle.get('viventium_launch_authority') or {}
            command += ['--yolo-mode', 'true' if (
                isinstance(authority, dict)
                and authority.get('version') == 1
                and not isinstance(authority.get('version'), bool)
                and float(deadline) > time.time()
                and authority.get('kind') == 'conversation_orchestrator'
                and authority.get('execution_mode') == ('host' if host else 'docker')
                and authority.get('worker_access') == 'full'
            ) else 'false']
        elif (run_id and attempt_id
                and str(worker.get('trusted_run_lane') or '').strip().lower() == 'conversation'
                and bundle.get('run_mode') == 'conversation'):
            command += ['--yolo-mode', 'true' if bundle.get('access_mode') == 'full' else 'false']
        from .grok_projection import mcp_servers_for_bundle, required_mcp_servers_for_bundle
        try:
            servers = [] if restricted else mcp_servers_for_bundle(bundle, self._native_environment(worker, host=host) if bundle.get('claude_project_mcp') else {})
            required_servers = required_mcp_servers_for_bundle(bundle, servers)
        except ValueError as exc:
            raise RuntimeErrorBase(str(exc)) from exc
        if servers is not None and (not isinstance(servers, list) or any(not isinstance(server, dict) for server in servers)):
            raise RuntimeErrorBase('grok_mcp_servers must contain native ACP MCP server objects')
        output_schema = self._conversation_output_schema(worker)
        rules = grok_native_authority_instructions(worker, bundle, servers, output_schema)
        if rules:
            path = target / 'developer-instructions.txt'
            _atomic_write_private_text(path, rules)
            # ACP rules are creation-only. Replace pinned conversation authority on resume.
            flag = '--system-prompt-file' if bundle.get('run_mode') == 'conversation' else '--rules-file'
            command += [flag, str(path) if host else f'{self.sandbox.home_mount}/.xperfect-grok/developer-instructions.txt']
        if output_schema:
            schema_path = target / 'conversation-output-schema.json'
            _atomic_write_private_text(schema_path, json.dumps(output_schema, separators=(',', ':')))
            command += ['--output-schema-file', str(schema_path) if host else f'{self.sandbox.home_mount}/.xperfect-grok/conversation-output-schema.json']
            if _grok_minimal_wire_output_contract(worker, bundle, output_schema):
                command += ['--output-schema-minimal-wire']
        if servers is not None:
            path = target / 'mcp-servers.json'
            _atomic_write_private_text(path, json.dumps(servers))
            command += ['--mcp-file', str(path) if host else f'{self.sandbox.home_mount}/.xperfect-grok/mcp-servers.json']
            for name in required_servers:
                command += ['--required-mcp-server', name]
            # Admission authorizes this transport only. Core still checks each
            # capability's scope, effect and confirmation using the signed grant.
            broker = bundle.get('glasshive_capability_broker')
            token = bundle.get('env', {}).get('GLASSHIVE_CAPABILITY_BROKER_TOKEN') if isinstance(bundle.get('env'), dict) else None
            broker_servers = [server for server in servers if isinstance(broker, dict) and server.get('name') == broker.get('name')]
            if (admitted_mission and not restricted and isinstance(broker, dict)
                    and broker.get('grant_id') == binding.get('grantId') and binding.get('grantId')
                    and broker.get('grant_expires_at') == deadline and deadline > time.time()
                    and isinstance(token, str) and token
                    and len(broker_servers) == 1 and broker_servers[0].get('url') == broker.get('url')
                    and [header for header in broker_servers[0].get('headers', [])
                         if isinstance(header, dict) and header.get('name', '').lower() == 'authorization']
                    == [{'name': 'Authorization', 'value': 'Bearer ' + token}]):
                command += ['--allow-mcp-server', broker['name']]
            projection = worker.get('_coordinator_native_projection')
            from .native_transport import is_projected_stdio
            token = str(projection.get('token') or '') if isinstance(projection, dict) else ''
            if (not restricted and isinstance(projection, dict)
                    and projection.get('worker_id') == worker.get('worker_id')
                    and projection.get('run_id') == run_id
                    and token
                    and any(server.get('name') == 'xperfect-coordinator'
                            and ((server.get('url') == projection.get('url')
                                  and {'name': 'Authorization', 'value': 'Bearer ' + token}
                                  in server.get('headers', []))
                                 or (is_projected_stdio(server, projection.get('url'))
                                     and {'name': 'GLASSHIVE_PEER_TOKEN', 'value': token}
                                     in server.get('env', [])))
                            for server in servers)):
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
        return {'run_id':run_id,'attempt_id':attempt_id,
            'session_id': self._read_session_key(worker['worker_id']),
            'pending_requests':pending,'receipts':receipts}

    def pending_native_input(self, worker, *, run_id):
        from .provider_native_input import project_native_request
        active = self._read_active_session(worker['worker_id']) or {}
        attempt_id = str(active.get('attempt_id') or '')
        if (active.get('run_id') != run_id or not attempt_id
                or (worker.get('_run_attempt_id') and worker['_run_attempt_id'] != attempt_id)):
            return None
        native = self.native_control_state(worker, run_id=run_id, attempt_id=attempt_id)
        session_id = native.get('session_id')
        if not session_id or active.get('native_session_id') != session_id:
            return None
        for item in native['pending_requests']:
            projected = project_native_request(item, run_id=run_id,
                attempt_id=attempt_id, session_id=session_id)
            if projected is None:
                continue
            if projected['kind'] == 'question':
                return {**{key: value for key, value in projected.items() if key != 'prompt'},
                    'version': 1, 'runtimeName': 'Grok', 'sessionId': session_id,
                    'state': 'pending', 'mode': 'form', 'message': projected['prompt']}
            choices = projected['choices']
            return {**{key: value for key, value in projected.items() if key not in {'prompt', 'choices', 'operationTitle'}},
                'version': 1, 'kind': 'permission', 'runtimeName': 'Grok',
                'sessionId': session_id, 'state': 'pending', 'mode': 'form',
                'message': projected['prompt'],
                'requestedSchema': {'type': 'object', 'properties': {
                    'optionId': {'type': 'string', 'title': 'Response',
                        'description': projected['operationTitle'],
                        'enum': [choice['value'] for choice in choices],
                        'enumKinds': [choice.get('kind') for choice in choices],
                        'enumNames': [choice['label'] for choice in choices]}},
                    'required': ['optionId']}}
        return None

    def _validate_native_human_permission(self, worker, run_id, attempt_id, pending, option_id, human):
        """Bind the signed original human receipt to the exact native offer."""
        from .grok_control import control_digest
        native = self.native_control_state(worker, run_id=run_id, attempt_id=attempt_id)
        current = [item for item in native.get('pending_requests', [])
                   if item.get('request_id') == pending['requestId']]
        if (len(current) != 1 or pending.get('kind') != 'permission'
                or native.get('session_id') != pending.get('sessionId')
                or control_digest(current[0]) != pending['requestFingerprint']):
            raise RuntimeErrorBase('native_input_stale')
        item = current[0]
        params = item.get('request')
        if not isinstance(params, dict) or params.get('sessionId') != pending['sessionId']:
            raise RuntimeErrorBase('native_input_stale')
        offered = [option for option in params.get('options', [])
                   if isinstance(option, dict) and option.get('optionId') == option_id]
        if (len(offered) != 1 or offered[0].get('kind') not in ('allow_once', 'reject_once')):
            raise RuntimeErrorBase('native_input_invalid')
        transcript = self._attempt_transcript(worker['worker_id'], run_id, attempt_id)
        if transcript is None:
            raise RuntimeErrorBase('native_input_stale')
        matches = []
        try:
            with transcript[0].open() as lines:
                for line in lines:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(event, dict) and event.get('type') == 'grok.permission.requested'
                            and event.get('request_id') == pending['requestId']):
                        matches.append(event)
        except OSError as exc:
            raise RuntimeErrorBase('native_input_stale') from exc
        if (self._attempt_transcript(worker['worker_id'], run_id, attempt_id) != transcript
                or len(matches) != 1 or matches[0].get('session_id') != pending['sessionId']
                or matches[0].get('method') != 'session/request_permission'
                or matches[0].get('request') != params):
            raise RuntimeErrorBase('native_input_stale')
        try:
            observed = datetime.fromisoformat(matches[0]['observed_at'].replace('Z', '+00:00'))
            if observed.tzinfo is None or observed.timestamp() * 1000 >= human['receivedAt']:
                raise ValueError('Human receipt must follow the exact native offer')
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise RuntimeErrorBase('native_input_stale') from exc

    def respond_native_input(self, worker, *, run_id, request_id, request_fingerprint,
                             action, content=None, allow_new=True, native_human_input=None):
        from .grok_control import control_receipt, permission_message_id, read_message, submit_control
        from .provider_native_input import native_question_response
        if native_human_input is not None:
            from .models import NativeHumanInputRequest
            try:
                human = NativeHumanInputRequest.model_validate(native_human_input).model_dump(by_alias=True)
            except ValueError as exc:
                raise RuntimeErrorBase('native_input_invalid') from exc
            if (type(human['version']) is not int or action != 'accept'
                    or not isinstance(content, dict) or set(content) != {'optionId'}):
                raise RuntimeErrorBase('native_input_invalid')
        if (action not in {'accept', 'decline', 'cancel'}
                or (action == 'accept' and (not isinstance(content, dict)
                    or not (set(content) == {'optionId'} and isinstance(content['optionId'], str)
                            or 'answers' in content)))
                or (action != 'accept' and content not in (None, {}))):
            raise RuntimeErrorBase('native_input_invalid')
        attempt_id = str(worker.get('_run_attempt_id') or '')
        active = self._read_active_session(worker['worker_id']) or {}
        if not attempt_id and active.get('run_id') == run_id:
            attempt_id = str(active.get('attempt_id') or '')
        if not attempt_id:
            raise RuntimeErrorBase('native_input_stale')
        root = self._control_dir(worker, run_id, attempt_id)
        message_id = permission_message_id(run_id, attempt_id, request_id, request_fingerprint)
        try:
            retained = None
            response_path = root / (message_id + '.response')
            queued = root / (message_id + '.request')
            if response_path.is_file():
                retained = read_message(response_path).get('_request')
            elif queued.is_file():
                retained = read_message(queued)
            pending = None
            question = action == 'accept' and 'answers' in content
            if action != 'accept':
                if isinstance(retained, dict):
                    question = 'response' in retained
                elif allow_new:
                    pending = self.pending_native_input(worker, run_id=run_id)
                    question = bool(pending and pending.get('kind') == 'question')
            expected = {'run_id': run_id, 'attempt_id': attempt_id, 'action': 'permission',
                'request_id': request_id, 'request_fingerprint': request_fingerprint}
            if native_human_input is not None:
                expected['native_human_input'] = human
            if question:
                expected['response'] = (native_question_response(content) if action == 'accept'
                                        else {'outcome': 'cancelled'})
            else:
                expected['option_id'] = content['optionId'] if action == 'accept' else None
            receipt = control_receipt(root, message_id, expected)
            if receipt is not None:
                if receipt.get('status') != 'permission_submitted':
                    raise RuntimeErrorBase('native_input_stale')
                return {'status': 'already_accepted'}
            if queued.is_file():
                body = read_message(queued)
                if any(body.get(key) != value for key, value in expected.items()):
                    raise RuntimeErrorBase('native_input_conflict')
                # Reconcile the same submitted message even after the question vanishes.
                body.pop('message_id', None)
                result = submit_control(root, body, message_id=message_id, replay_only=True,
                    managed_acl=getattr(self.sandbox, 'box', None) is not None)
            else:
                if not allow_new:
                    raise RuntimeErrorBase('native_input_confirmation_unavailable')
                pending = pending or self.pending_native_input(worker, run_id=run_id)
                if (not pending or pending['requestId'] != request_id
                        or pending['requestFingerprint'] != request_fingerprint
                        or question != (pending.get('kind') == 'question')):
                    raise RuntimeErrorBase('native_input_stale')
                if action == 'accept':
                    if question:
                        native_question_response(content, pending['requestedSchema'])
                    elif content['optionId'] not in pending['requestedSchema']['properties']['optionId']['enum']:
                        raise RuntimeErrorBase('native_input_invalid')
                if native_human_input is not None:
                    self._validate_native_human_permission(worker, run_id, attempt_id, pending, content['optionId'], human)
                body = {**expected, 'session_id': pending['sessionId']}
                result = submit_control(root, body, message_id=message_id,
                    managed_acl=getattr(self.sandbox, 'box', None) is not None)
        except (ValueError, OSError) as exc:
            if not allow_new and not root.is_dir():
                raise RuntimeErrorBase('native_input_confirmation_unavailable') from exc
            raise RuntimeErrorBase(str(exc)) from exc
        if result.get('status') == 'permission_submitted':
            return {'status': 'accepted'}
        if result.get('status') == 'pending':
            return {'status': 'pending'}
        raise RuntimeErrorBase('native_input_stale')

    def supports_native_control(self, worker, action, *, run_id='', attempt_id=''):
        if action != 'interject':
            return action in {'cancel', 'permission'}
        active = self._read_active_session(worker['worker_id']) or {}
        capabilities = active.get('native_control_capabilities')
        return bool(run_id and attempt_id and active.get('run_id') == run_id
                    and active.get('attempt_id') == attempt_id
                    and active.get('native_session_id')
                    and isinstance(capabilities, dict) and capabilities.get('interject') is True)

    def native_control(self, worker, *, run_id, attempt_id, action, payload=None):
        from .grok_control import submit_control
        active = self._read_active_session(worker['worker_id']) or {}
        if not run_id or not attempt_id or active.get('run_id') != run_id or active.get('attempt_id') != attempt_id:
            raise RuntimeErrorBase('Grok control does not match the active run attempt')
        # Current-turn context also applies to stateless/restricted sessions. Their
        # exact active receipt is not a persisted continuity or permission key.
        session_id = (active.get('native_session_id') if action == 'interject'
                      else self._read_session_key(worker['worker_id']))
        if not session_id:
            raise RuntimeErrorBase('Grok native session is not yet ready for controls')
        body = dict(payload or {})
        message_id = body.pop('message_id', None) if action == 'interject' else None
        if message_id is not None:
            fingerprint = body.get('request_fingerprint')
            if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                    or any(c not in '0123456789abcdef' for c in fingerprint)):
                raise ValueError('Retained Grok interjection requires an exact request fingerprint')
        body.update(run_id=run_id, attempt_id=attempt_id, session_id=session_id, action=action)
        # Automatic retained context only writes the guarded mailbox. Native ACK
        # arrives independently; it must not hold admission/Stop locks while waiting.
        submit_options = {'timeout': 0} if message_id is not None else {}
        return submit_control(self._control_dir(worker, run_id, attempt_id), body,
            managed_acl=getattr(self.sandbox, 'box', None) is not None, message_id=message_id,
            **submit_options)

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
        if (validated_session_model(session, info.model) is None or not isinstance(session.get('session_id'), str)
                or not session['session_id'] or result.get('session_id') != session['session_id']
                or result.get('stop_reason') != 'end_turn' or not isinstance(result.get('output'), str)):
            raise RuntimeErrorBase('Grok terminal result does not match its configured native session/model')
        # Restricted commands always open a fresh session; retained worker identity is not a resume request.
        if (not _native_tools_disabled(bootstrap_bundle_for(worker))
                and info.session_key and session['session_id'] != info.session_key):
            raise RuntimeErrorBase('Grok resumed a different native session')
        output = self._select_native_agent_output(worker, info, [result['output']])
        capture = getattr(self, '_capture_completed_native_edits', None)
        if callable(capture):
            capture(worker, info, stdout, session['session_id'], result['output'])
        return session['session_id'], output

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


class HostGrokBuildRuntime(GrokNativeWarmMixin, HostNativeCliMixin, GrokBuildRuntime):
    worker_root_name = 'host_grok_build_runtime'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_native_warm()

    def _conversation_output_schema(self, worker):
        schema = super()._conversation_output_schema(worker)
        if not schema:
            return None
        bundle = self._bootstrap_bundle_for_worker(worker)
        return _grok_conversation_output_schema(conversation_output_schema(
            bundle.get('agent_builder_control'), bundle.get('messaging_delivery_control'),
            surface=str(bundle.get('surface') or ''),
            same_turn_continuation=(worker.get('trusted_run_lane') == 'conversation'
                                    and not _native_tools_disabled(bundle))))

    def _capture_completed_native_edits(self, worker, info, stdout, session_id, output):
        run_id, attempt_id = worker.get('_active_run_id'), worker.get('_run_attempt_id')
        if not run_id or not attempt_id:
            return
        transcript = self._attempt_transcript(worker['worker_id'], run_id, attempt_id)
        if transcript is None:
            return
        try:
            if transcript[0].read_text() != stdout:
                return
        except OSError:
            return
        calls, completed = {}, {}
        for event in self._events(stdout):
            if event.get('type') != 'grok.session.update' or event.get('session_id') != session_id:
                continue
            update = event.get('update')
            if (not isinstance(update, dict) or update.get('sessionUpdate')
                    not in ('tool_call', 'tool_call_update')):
                continue
            call_id = update.get('toolCallId')
            if not isinstance(call_id, str) or not call_id:
                continue
            metadata = update.get('_meta')
            tool = metadata.get('x.ai/tool') if isinstance(metadata, dict) else None
            if isinstance(tool, dict) and tool.get('version') == 1:
                if tool.get('kind') in ('write', 'edit') and tool.get('read_only') is False:
                    calls[call_id] = tool
                else:
                    calls.pop(call_id, None)
            if update.get('status') != 'completed' or call_id not in calls:
                if update.get('status') in ('failed', 'cancelled'):
                    completed.pop(call_id, None)
                continue
            diffs = update.get('content')
            if not isinstance(diffs, list):
                continue
            edits = []
            for diff in diffs:
                if not isinstance(diff, dict) or diff.get('type') != 'diff':
                    continue
                path, old, new = diff.get('path'), diff.get('oldText'), diff.get('newText')
                if (not isinstance(path, str) or not path or (old is not None and not isinstance(old, str))
                        or not isinstance(new, str)):
                    continue
                declared = calls[call_id].get('input')
                if isinstance(declared, dict) and declared.get('path') not in (None, path):
                    continue
                edits.append((path, hashlib.sha256(new.encode('utf-8')).hexdigest()))
            completed[call_id] = edits
        if self._attempt_transcript(worker['worker_id'], run_id, attempt_id) != transcript:
            return
        latest_edits = {path: digest for values in completed.values() for path, digest in values}
        edits = tuple(latest_edits.items())
        if edits:
            scope = self._read_native_image_scope(worker['worker_id'], run_id)
            self._capture_native_output_files(worker, str(info.workspace_dir or ''), scope,
                                              run_id, output, native_edits=edits)

    def pending_native_input(self, worker, *, run_id):
        return GrokBuildRuntime.pending_native_input(self, worker, run_id=run_id)

    def respond_native_input(self, worker, **kwargs):
        return GrokBuildRuntime.respond_native_input(self, worker, **kwargs)

    def _agent_type(self):
        return 'grok'

    def preflight_worker_profile(self, profile, execution_mode='host'):
        return GrokBuildRuntime.preflight_worker_profile(self, profile, execution_mode)

    def _build_command(self, worker, instruction, info):
        return self._grok_command(worker, info, host=True), self._native_environment(worker, host=True)

    def _command_stdin_text(self, worker, instruction, info):
        return GrokBuildRuntime._command_stdin_text(self, worker, instruction, info)
