"""Single-use initialized Codex stdio over the existing native pool and relay."""
from __future__ import annotations

import contextlib
import copy
import fnmatch
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import uuid

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workers_projects_runtime.bootstrap import bootstrap_bundle_for, CLEAN_ROOM_RUNTIME_ENV_KEYS
from workers_projects_runtime.direct_native_warm import _EventWriter, validate_admitted_lease
from workers_projects_runtime.grok_native_warm import GrokNativeWarmMixin
from workers_projects_runtime.mission_provider_accounts import native_current_account_allowed
from workers_projects_runtime.native_warm_pool import (
    NativeWarmConfig, WarmScope, _native_state_lock, native_environment_digest,
    read_native_warm_record, start_native_warm_server,
)
from workers_projects_runtime import native_input

SECRET_ENVIRONMENT = "XPERFECT_CODEX_WARM_SECRET"
CONFIG_LOCK_SECONDS = 2.0
FRESH_ENVIRONMENT = frozenset({*CLEAN_ROOM_RUNTIME_ENV_KEYS, "GLASSHIVE_RUN_ID"})
# Other CLI settings remain on the unchanged cold path. In particular, a
# same-server CLI MCP override must never be shadowed by the fresh projection.
THREAD_CONFIG_KEYS = frozenset({"model", "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "sandbox_mode", "sandbox_workspace_write", "approval_policy",
    "developer_instructions", "web_search", "project_doc_max_bytes", "features"})


class ConfigurationChanged(RuntimeError):
    """A changed admission cannot fall back against another run's authority."""


def static_environment(environment):
    return {key: value for key, value in environment.items() if key not in FRESH_ENVIRONMENT}


def require_projectable_config(config):
    policy = config.get("shell_environment_policy") or {}
    if (config.get("hooks") or policy.get("ignore_default_excludes") is True
            or CLEAN_ROOM_RUNTIME_ENV_KEYS.intersection(policy.get("set") or {})):
        raise ValueError("Native startup environment requires ordinary startup")


def command_config(command):
    """The existing translated app-server flags, applied at native thread start."""
    if command[1:3] != ["app-server", "--stdio"]:
        raise ValueError("Codex command has no supported stdio projection")
    result = {}

    def merge(target, incoming):
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    tokens = command[3:]
    if len(tokens) % 2:
        raise ValueError("Codex startup-only flags require ordinary startup")
    for flag, value in zip(tokens[::2], tokens[1::2]):
        if flag in {"-c", "--config"}:
            merge(result, tomllib.loads(value))
        elif flag in {"--enable", "--disable"}:
            result.setdefault("features", {})[value] = flag == "--enable"
        else:
            raise ValueError("Codex startup-only flags require ordinary startup")
    if result.pop("cli_auth_credentials_store", "file") != "file" or set(result) - THREAD_CONFIG_KEYS:
        raise ValueError("Codex startup configuration requires ordinary startup")
    return result


def project_config(source, environment, overrides):
    """Keep the disk projection secret-free; resolve fresh MCP values only in RPC."""
    from workers_projects_runtime.direct_runtime import direct_mcp_servers
    from workers_projects_runtime.openclaw_runtime import RuntimeConfigurationError
    from workers_projects_runtime.profile_runtime import _render_toml_document

    disk = tomllib.loads(source)
    policy = disk.get("shell_environment_policy") or {}
    # These consumers can inherit process authority outside MCP. Do not pretend
    # that an already started process can replace their environment.
    require_projectable_config(disk)
    projected = copy.deepcopy(disk)
    rpc_config = copy.deepcopy(overrides)
    for name, server in projected.get("mcp_servers", {}).items():
        if not isinstance(server, dict) or server.get("enabled") is False:
            continue
        bearer = server.get("bearer_token_env_var")
        headers = server.get("env_http_headers") or {}
        variables = server.get("env_vars") or []
        fresh_headers = {key for key, value in headers.items() if value in FRESH_ENVIRONMENT}
        fresh_variables = [key for key in variables if isinstance(key, str) and key in FRESH_ENVIRONMENT]
        if bearer not in FRESH_ENVIRONMENT and not fresh_headers and not fresh_variables:
            continue
        if any(not isinstance(key, str) for key in variables):
            raise ValueError("Native MCP environment mapping requires ordinary startup")
        try:
            resolved = direct_mcp_servers({"codex_config_append": _render_toml_document(
                {"mcp_servers": {name: server}})}, environment)[0]
        except RuntimeConfigurationError as exc:
            raise ValueError("Native MCP environment binding requires ordinary startup") from exc
        if bearer in FRESH_ENVIRONMENT:
            server.pop("bearer_token_env_var")
        if fresh_headers:
            server["env_http_headers"] = {key: value for key, value in headers.items() if key not in fresh_headers}
        if fresh_variables:
            server["env_vars"] = [key for key in variables if key not in fresh_variables]
        item = copy.deepcopy(server)
        if server.get("url"):
            item["http_headers"] = {value["name"]: value["value"] for value in resolved["headers"]}
        else:
            item["env"] = {value["name"]: value["value"] for value in resolved["env"]}
        # Quoted server names retain native names containing dots or punctuation.
        rpc_config["mcp_servers." + json.dumps(name)] = item
    # Cold shell policy normally inherits this non-secret run identity. Respect
    # its filters and explicit values; fresh token variables remain excluded.
    run_key = "GLASSHIVE_RUN_ID"
    inherits_run = (policy.get("inherit", "all") == "all"
        and not any(fnmatch.fnmatchcase(run_key, item) for item in policy.get("exclude") or [])
        and (not policy.get("include_only") or any(fnmatch.fnmatchcase(run_key, item)
            for item in policy["include_only"])))
    if run_key in environment and inherits_run and run_key not in (policy.get("set") or {}):
        rpc_config["shell_environment_policy.set." + run_key] = environment[run_key]
    return _render_toml_document(projected).rstrip() + "\n", rpc_config


def start_projected_thread(call, method, options, *, config_path, expected_digest, environment, overrides):
    from workers_projects_runtime.profile_runtime import _atomic_write_private_text
    with _native_state_lock(config_path.parent, timeout=CONFIG_LOCK_SECONDS):
        original = config_path.read_text()
        if hashlib.sha256(original.encode()).hexdigest() != expected_digest:
            raise ConfigurationChanged("Admitted native configuration changed before attachment")
        projected, config = project_config(original, environment, overrides)
        _atomic_write_private_text(config_path, projected)
        try:
            return call(method, {**options, "config": {**(options.get("config") or {}), **config}})
        finally:
            if config_path.read_text() != projected:
                raise ConfigurationChanged("Native configuration changed during attachment; newer bytes preserved")
            _atomic_write_private_text(config_path, original)


class CodexNativeWarmMixin(GrokNativeWarmMixin):
    def _init_native_warm(self):
        super()._init_native_warm()
        self._codex_startup_configurations = {}

    def _native_warm_supported(self, worker):
        return worker.get("profile") == "codex-cli" and worker.get("execution_mode") == "host"

    def _warm_context(self, worker):
        if (not self._native_warm_supported(worker) or not native_current_account_allowed(worker)
                or not self._native_midstream_schema_supported()):
            raise ValueError("Codex preparation requires its supported native current-account stdio path")
        home = self._host_codex_home(worker)
        if not (home / "config.toml").is_file():
            self.prepare_worker_workspace(worker)
        self._copy_host_codex_auth(home)
        if not (home / "auth.json").is_file():
            raise ValueError("Native current account is unavailable")
        config = tomllib.loads((home / "config.toml").read_text())
        if (config.get("model_provider", "openai") != "openai"
                or config.get("cli_auth_credentials_store", "file") != "file"):
            raise ValueError("Native account startup settings require ordinary startup")
        environment = self._host_env(worker)
        lane = "conversation" if bootstrap_bundle_for(worker).get("run_mode") == "conversation" else "mission"
        if lane == "conversation":
            environment["GLASSHIVE_RUN_MODE"] = lane
        require_projectable_config(config)
        # Thread-bound fields are reread after lease. Other native settings can
        # belong to process initialization. Compare their complete values only in
        # memory; persist a random revision, never a digest of credential values.
        startup = {key: value for key, value in config.items()
                   if key not in THREAD_CONFIG_KEYS and key != "mcp_servers"}
        with self._warm_lock:
            previous = self._codex_startup_configurations.get(self._warm_owner(worker))
            if previous is None or previous[0] != startup:
                previous = (startup, uuid.uuid4().hex)
                self._codex_startup_configurations[self._warm_owner(worker)] = previous
            startup_revision = previous[1]
        # User/project layers stay native-owned. Never rewrite them to make a
        # warm attachment possible; their per-attempt env lookups require cold.
        project_configs = []
        workspace = self._host_workspace_dir(worker).resolve()
        for directory in (workspace, *workspace.parents):
            path = directory / ".codex" / "config.toml"
            if path == home / "config.toml" or not path.is_file():
                continue
            layer = tomllib.loads(path.read_text())
            require_projectable_config(layer)
            for server in (layer.get("mcp_servers") or {}).values():
                references = [server.get("bearer_token_env_var"),
                    *(server.get("env_http_headers") or {}).values(), *(server.get("env_vars") or [])]
                if any(isinstance(key, str) and key in FRESH_ENVIRONMENT for key in references):
                    raise ValueError("Project MCP environment binding requires ordinary startup")
            project_configs.append(path)
        environment = static_environment(environment)
        binary = Path(shutil.which(self.binary, path=environment.get("PATH")) or self.binary).resolve()
        paths = [binary, home / "auth.json", self._source_host_codex_home() / "config.toml", *project_configs,
                 *[Path(__file__).with_name(name + ".py") for name in
                   ("codex_native_warm", "native_input", "native_warm_pool", "profile_runtime")]]
        revision = [startup_revision]
        for path in paths:
            info = path.stat() if path.exists() else None
            revision.append([str(path), [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns] if info else None])
        scope = WarmScope(*self._warm_owner(worker), "codex-cli",
            str(worker.get("model") or self.resolve_model("codex-cli")), "native-current",
            hashlib.sha256(json.dumps(revision, separators=(",", ":")).encode()).hexdigest(),
            native_environment_digest(environment, lane=lane, profile="codex-cli"), lane=lane)
        return scope, environment, str(binary)

    def _start_native_warm_server(self, worker, scope, environment, binary):
        from websockets.sync.client import connect

        def probe(server, deadline):
            with connect(server.url, additional_headers={"Authorization": "Bearer " + server.secret},
                         open_timeout=1, close_timeout=0, proxy=None) as connection:
                connection.send(json.dumps({"type": "ready"}))
                reply = json.loads(connection.recv(timeout=max(.05, deadline - time.monotonic())))
                if reply != {"type": "ready", "profile": "codex-cli", "inference": False}:
                    raise ValueError("Codex initialization did not complete")

        return start_native_warm_server(
            command=lambda _port, path: [sys.executable, str(Path(__file__).resolve()), "--serve", str(path), binary],
            probe=probe, cwd=self._host_workspace_dir(worker), static_environment=environment,
            secret_environment=SECRET_ENVIRONMENT, state_directory=self._warm_directory(),
            scope=scope, stop_runtime=self)

    def _prepare_native_warm_command(self, worker, command, *, run_root):
        # Native mission control is independent of the optional warm pool.
        cold = command
        if len(command) > 1 and command[1] == "exec":
            command = self._prepare_native_midstream_command(worker, command, run_root=run_root)
        if not NativeWarmConfig.from_performance(bootstrap_bundle_for(worker).get("runtime_performance")).workers:
            return command
        context_path = run_root / "native-input-context.json"
        cold_control = cold
        try:
            context = native_input.read_object(context_path)
            if (context.get("workerId") != worker.get("worker_id")
                    or context.get("runId") != worker.get("_active_run_id")
                    or context.get("attemptId") != worker.get("_run_attempt_id")):
                return cold
            # A validated native control relay remains the faithful cold path
            # when startup-only policy is ineligible for a warm process.
            cold_control = command
            overrides = command_config(context["launch"]["command"])
            project_config((self._host_codex_home(worker) / "config.toml").read_text(),
                           self._host_env(worker, worker.get("_active_run_id")), overrides)
        except (OSError, ValueError, KeyError, native_input.NativeInputError):
            return cold_control
        prepared = super()._prepare_native_warm_command(worker, command, run_root=run_root)
        if prepared == command:
            return cold_control
        config_path = self._host_codex_home(worker) / "config.toml"
        authority_path = run_root / "native-authority-context.json"
        authority = (native_input.read_object(authority_path)
                     if len(command) == 3 and command[2] == str(authority_path) else None)
        try:
            with _native_state_lock(config_path.parent, timeout=CONFIG_LOCK_SECONDS):
                context["warmConfigDigest"] = hashlib.sha256(config_path.read_bytes()).hexdigest()
                context["warmColdCommand"] = authority["command"] if authority else cold
                native_input.publish(context_path, context)
        except (OSError, ValueError, native_input.NativeInputError):
            self._finish_native_warm(worker)
            return cold_control
        attach_command = [sys.executable, str(Path(__file__).resolve()), "--attach", prepared[-1], str(context_path)]
        if authority:
            authority["command"] = attach_command
            native_input.publish(authority_path, authority)
            return command  # Keep the exact existing resume-authority preflight.
        return attach_command


class _Diagnostics:
    def __init__(self):
        self.connection = None
        self.pending = []
        self.lock = threading.Lock()

    def send(self, message):
        with self.lock:
            if self.connection is not None:
                self.connection.send(message)
            elif sum(map(len, self.pending)) < 65536:
                self.pending.append(message)

    def attach(self, connection):
        with self.lock:
            self.connection = connection
            for message in self.pending:
                connection.send(message)
            self.pending.clear()


def execute(connection, message, record, static, rpc, diagnostics):
    """Fallback is allowed only while no native thread RPC has been dispatched."""
    dispatched = False
    scope = record["scope"]
    try:
        context_path = Path(message["context"])
        context = native_input.read_object(context_path)
        validate_admitted_lease(message["lease"], worker_id=scope["worker_id"],
            run_id=context["runId"], attempt_id=context["attemptId"], profile="codex-cli",
            model=scope["model"], server_record=record)
        if (context["workerId"] != scope["worker_id"]
                or not isinstance(message["environment"], dict)
                or static_environment(message["environment"]) != static):
            raise ValueError("Native static environment changed before attachment")
        launch = context["launch"]
        overrides = command_config(launch["command"])
        config_path = Path(static["CODEX_HOME"]) / "config.toml"
        project_config(config_path.read_text(), message["environment"], overrides)

        def start(call, method, options):
            def dispatch(method, options):
                nonlocal dispatched
                dispatched = True  # Even a lost ACK must never cause cold replay.
                return call(method, options)
            return start_projected_thread(dispatch, method, options, config_path=config_path,
                expected_digest=context["warmConfigDigest"], environment=message["environment"],
                overrides=overrides)

        diagnostics.attach(connection)
        with contextlib.redirect_stdout(_EventWriter(connection)):
            code = native_input.codex_relay(context_path, launch["command"], initialized_rpc=rpc,
                instruction=message["instruction"], start_thread=start)
        connection.send(json.dumps({"type": "done", "code": code} if dispatched else {"type": "unavailable"}))
    except ConfigurationChanged:
        rpc.close()
        connection.send(json.dumps({"type": "done", "code": 1}))
    except (ValueError, OSError, KeyError, native_input.NativeInputError):
        rpc.close()
        connection.send(json.dumps({"type": "done", "code": 1} if dispatched else {"type": "unavailable"}))


def serve(record_path, binary):
    from websockets.sync.server import serve as websocket_serve
    record = read_native_warm_record(Path(record_path))
    scope, secret = record["scope"], os.environ.pop(SECRET_ENVIRONMENT)
    child = record.get("child") or {}
    if (record.get("phase") != "booting" or child.get("pid") != os.getpid()
            or child.get("process_group") != os.getpgrp()
            or not hmac.compare_digest(record.get("secret") or "", secret)
            or static_environment(dict(os.environ)) != dict(os.environ)
            or scope.get("profile") != "codex-cli"
            or native_environment_digest(dict(os.environ), lane=scope["lane"], profile="codex-cli")
                != scope["environment_digest"]):
        raise ValueError("Codex holder has no exact idle startup identity")
    static = dict(os.environ)
    claimed, finished = threading.Lock(), threading.Event()
    diagnostics = _Diagnostics()
    with contextlib.closing(_EventWriter(diagnostics, "stderr")) as stderr:
        rpc = native_input.CodexRpc([binary, "app-server", "--stdio"], stderr=stderr.fileno())
        try:
            rpc.call("initialize", {"clientInfo": {"name": "xperfect-native", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
            rpc.send({"method": "initialized", "params": {}})

            def native_exited():
                rpc.reader.join()
                if not claimed.locked():
                    finished.set()
            threading.Thread(target=native_exited, daemon=True).start()

            def handler(connection):
                if not hmac.compare_digest(connection.request.headers.get("Authorization", ""), "Bearer " + secret):
                    connection.close(1008, "Unauthorized")
                    return
                message = json.loads(connection.recv(timeout=5))
                if message == {"type": "ready"} and not claimed.locked() and not rpc.closed:
                    connection.send(json.dumps({"type": "ready", "profile": "codex-cli", "inference": False}))
                    return
                if message.get("type") != "execute" or not claimed.acquire(blocking=False):
                    connection.close(1008, "Already admitted or invalid request")
                    return
                try:
                    execute(connection, message, record, static, rpc, diagnostics)
                finally:
                    finished.set()

            with websocket_serve(handler, "127.0.0.1", record["port"], max_size=None) as server:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                finished.wait()
                server.shutdown()
        finally:
            rpc.close()


def attach(lease_path, context_path):
    from websockets.sync.client import connect
    context = native_input.read_object(Path(context_path))
    lease = validate_admitted_lease(lease_path, worker_id=context["workerId"], run_id=context["runId"],
        attempt_id=context["attemptId"], profile="codex-cli", model=context["launch"]["thread"].get("model"))
    instruction = sys.stdin.buffer.read().decode()
    def cold():
        # The admitted command is retained before wrapping, including mission
        # exec and the existing conversation relay. Never reconstruct its flags.
        config_path = Path(os.environ["CODEX_HOME"]) / "config.toml"
        with _native_state_lock(config_path.parent, timeout=CONFIG_LOCK_SECONDS):
            if hashlib.sha256(config_path.read_bytes()).hexdigest() != context["warmConfigDigest"]:
                raise ConfigurationChanged("Admitted native configuration changed before fallback")
        return subprocess.run(context["warmColdCommand"], input=instruction, text=True).returncode
    try:
        connection = connect(lease["url"], additional_headers={"Authorization": "Bearer " + lease["secret"]},
                             open_timeout=2, close_timeout=1, max_size=None, proxy=None)
    except (OSError, TimeoutError):
        return cold()
    with connection:
        connection.send(json.dumps({"type": "execute", "lease": lease_path, "context": context_path,
            "instruction": instruction, "environment": dict(os.environ)}))
        for raw in connection:
            message = json.loads(raw)
            if message.get("type") == "event":
                print(message["line"], flush=True)
            elif message.get("type") == "stderr":
                sys.stderr.write(message["text"])
                sys.stderr.flush()
            elif message.get("type") == "done":
                return int(message["code"])
            elif message.get("type") == "unavailable":
                return cold()
            else:
                raise ValueError("Invalid Codex warm response")
    raise RuntimeError("Native attachment ended without a terminal receipt")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--serve":
        serve(*sys.argv[2:])
    elif len(sys.argv) == 4 and sys.argv[1] == "--attach":
        raise SystemExit(attach(*sys.argv[2:]))
    else:
        raise SystemExit("Codex warm execution requires an exact private native lease")
