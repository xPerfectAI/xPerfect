"""Local, single-user service lifecycle. Uses the existing API, UI and MCP apps."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = {"runtime": ROOT / "runtime_phase1", "ui": ROOT / "frontends/glass-drive-ui"}
DEFAULT_PORTS = {"api": 8766, "ui": 8780, "mcp": 8767}
PORT_LABELS = {"api": "API", "ui": "web UI", "mcp": "MCP server"}


def _launcher():
    """The packaged launcher owns the one exact-model contract (standard library only)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("xperfect_launch", ROOT / "deployment/linux/launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Host-native harnesses read these before the profile setting (see the runtime's
# host Codex and Claude Code model resolution); a choice must reach them too.
HOST_MODEL_ENVIRONMENTS = {"codex-cli": ("WPR_MODEL_HOST_CODEX_CLI", "CODEX_MODEL"),
                           "claude-code": ("WPR_CLAUDE_CODE_PROVIDER_MODEL",)}


def models_of(config: dict) -> dict:
    """The typed exact-model choices; environment variable names are never hand-written."""
    launcher = _launcher()
    models = launcher.validate_models(config.get("models") or {})
    for profile, model in models.items():
        for name in (launcher.MODEL_ENVIRONMENTS[profile], *HOST_MODEL_ENVIRONMENTS.get(profile, ())):
            written = config["env"].get(name)
            if written is not None and written != model:
                raise RuntimeError(f"config.json sets two different {profile} models; keep the models entry only")
    return models


def set_models(state: Path, config: dict, models: dict) -> dict:
    """Persist explicit --model choices; other settings are left exactly as they are."""
    if not models:
        return config
    updated = {**config, "models": {**(config.get("models") or {}), **models}}
    models_of(updated)
    write_json(state / "config.json", updated)
    return updated


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError("State must be a directory owned by the current user, not a symlink")
    path.chmod(0o700)


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def configuration(state: Path, ports: dict | None = None) -> dict:
    private_dir(state)
    path = state / "config.json"
    if not path.exists():
        write_json(path, {"ports": ports or DEFAULT_PORTS, "owner_id": "local-owner", "env": {}})
    value = json.loads(path.read_text())
    selected = value["ports"]
    if set(selected) != set(DEFAULT_PORTS) or any(type(p) is not int or not 1024 <= p <= 65535 for p in selected.values()):
        raise RuntimeError("config.json requires distinct api/ui/mcp ports between 1024 and 65535")
    if len(set(selected.values())) != 3:
        raise RuntimeError("API, UI and MCP ports must differ")
    if not isinstance(value.get("env"), dict) or not all(isinstance(v, str) for v in value["env"].values()):
        raise RuntimeError("config.json env must map names to strings")
    try:
        models_of(value)
    except ValueError as exc:
        raise RuntimeError(f"config.json models: {exc}") from None
    if not (state / "secrets.json").exists():
        write_json(state / "secrets.json", {"api_token": secrets.token_urlsafe(32), "mcp_token": secrets.token_urlsafe(32), "link_secret": secrets.token_urlsafe(32)})
    secret = json.loads((state / "secrets.json").read_text())
    added = {
        "mcp_token": secrets.token_urlsafe(32),
        "local_auth_throttle_key": secrets.token_urlsafe(48),
        "local_auth_namespace": "xperfect-local-" + secrets.token_hex(8),
    }
    if any(name not in secret for name in added):
        secret = {**added, **secret}
        write_json(state / "secrets.json", secret)
    return value


def control_path(state: Path) -> str:
    # Unix socket paths are bounded to 104 bytes on macOS. A private runtime
    # directory avoids coupling that limit to a user's checkout/state path.
    folder = Path("/tmp") / f"xperfect-{os.getuid()}"
    private_dir(folder)
    return str(folder / (hashlib.sha256(str(state).encode()).hexdigest()[:24] + ".sock"))


def control(state: Path, command: str) -> dict | None:
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(control_path(state))
            client.sendall(command.encode())
            data = client.recv(65536)
            return json.loads(data)
    except (OSError, ValueError):
        return None


def environment(state: Path, config: dict, instance: str) -> dict:
    # A model named only in the shell that happened to start xPerfect is not a
    # choice; config.json names it explicitly.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("WPR_", "GLASSHIVE_", "VIVENTIUM_", "LIBRECHAT_", "UV_")) and k != "CODEX_MODEL"}
    env.update(config["env"])
    models = models_of(config)
    env.update(_launcher().model_environment(models))
    if "codex-cli" in models:
        env["WPR_MODEL_HOST_CODEX_CLI"] = models["codex-cli"]
    # A single trusted local OS user can connect native keys and the current
    # Codex sign-in without hidden setup flags. Private config can opt out.
    env.setdefault("GLASSHIVE_ENABLE_NATIVE_API_KEYS", "1")
    env.setdefault("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "1")
    ports = config["ports"]
    secret = json.loads((state / "secrets.json").read_text())
    managed = state / "data" / "managed-files"
    private_dir(managed)
    # Conversation work defaults to the process directory, which is this
    # checkout. Keep it in private state unless private config chooses a place.
    if not env.get("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE"):
        conversation = state / "workspaces" / "conversation"
        private_dir(state / "workspaces")
        private_dir(conversation)
        env["GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE"] = str(conversation)
    env.update({
        "VIVENTIUM_ENV_FILE": "", "GLASSHIVE_SECURITY_MODE": "local", "GLASSHIVE_AUTH_MODE": "local",
        "GLASSHIVE_HUMAN_AUTH_MODE": "", "GLASSHIVE_PUBLIC_LINKS_ONLY": "false",
        "WPR_DB_PATH": str(state / "data" / "runtime.db"),
        "WPR_RUNTIME_BACKEND": "openclaw", "WPR_DEFAULT_EXECUTION_MODE": "host",
        "GLASSHIVE_DEFAULT_EXECUTION_MODE": "host", "GLASSHIVE_DEFAULT_LAUNCH_SURFACE": "terminal",
        "WPR_DEFAULT_OWNER_ID": config["owner_id"], "GLASSHIVE_DEFAULT_OWNER_ID": config["owner_id"],
        "WPR_HOST_WORKSPACE_ROOT": str(state / "workspaces"),
        "WPR_BOOTSTRAP_SOURCE_ROOTS": str(managed),
        "GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT": str(state / "data" / "provider_accounts"),
        "GLASSHIVE_AUTH_STATE_PATH": str(state / "data" / "auth.sqlite3"),
        "GLASSHIVE_LINK_REF_STATE_PATH": str(state / "data" / "link_refs.sqlite3"),
        "GLASSHIVE_WATCH_SESSION_STATE_PATH": str(state / "data" / "watch_sessions.sqlite3"),
        "GLASSHIVE_RUNTIME_BASE_URL": f"http://127.0.0.1:{ports['api']}",
        "GLASSHIVE_PEER_RUNTIME_BASE_URL": f"http://127.0.0.1:{ports['api']}",
        "WPR_OPERATOR_BASE_URL": f"http://127.0.0.1:{ports['ui']}",
        "GLASSHIVE_PUBLIC_BASE_URL": f"http://127.0.0.1:{ports['ui']}",
        "WPR_API_TOKEN": secret["api_token"], "GLASSHIVE_MCP_API_KEY": secret["mcp_token"],
        "GLASSHIVE_SIGNED_LINK_SECRET": secret["link_secret"],
        "GLASSHIVE_RELEASE_ID": instance,
    })
    return env


# The UI signs a short-lived assertion for a person unlocked in the browser; the
# runtime only verifies it. Only the UI ever holds the private signing key.
LOCAL_ASSERTION_AUDIENCE = "xperfect-local-runtime"
LOCAL_SESSION_TTL_SECONDS = str(30 * 24 * 60 * 60)
ASSERTION_KEYS = (
    "GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE", "GLASSHIVE_INTERNAL_ASSERTION_KEY_ID",
    "GLASSHIVE_INTERNAL_ASSERTION_TTL_SECONDS", "GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE",
    "GLASSHIVE_INTERNAL_ASSERTION_JWKS_JSON", "GLASSHIVE_INTERNAL_ASSERTION_ISSUER",
    "GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE", "GLASSHIVE_LOCAL_HUMAN_ASSERTION",
    "GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY",
)


def assertion_paths(state: Path) -> tuple[Path, Path]:
    folder = state / "data" / "local-assertion"
    return folder / "ui-signing-key.pem", folder / "runtime-jwks.json"


def ensure_assertion_keys(state: Path) -> str:
    """Create the UI signing key and the runtime's public key set once; reuse them after."""
    key, jwks = assertion_paths(state)
    if not (key.exists() and jwks.exists()):
        private_dir(key.parent)
        script = (
            "import json,os,sys,secrets\n"
            "from cryptography.hazmat.primitives import serialization\n"
            "from cryptography.hazmat.primitives.asymmetric import rsa\n"
            "from jwt.algorithms import RSAAlgorithm\n"
            "key=rsa.generate_private_key(public_exponent=65537,key_size=2048)\n"
            "kid='xperfect-local-'+secrets.token_hex(8)\n"
            "pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())\n"
            "public=json.loads(RSAAlgorithm.to_jwk(key.public_key()));public.update(kid=kid,alg='RS256',use='sig')\n"
            "for path,data in ((sys.argv[1],pem),(sys.argv[2],json.dumps({'keys':[public]}).encode())):\n"
            "    fd=os.open(path+'.tmp',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.write(fd,data);os.close(fd);os.replace(path+'.tmp',path)\n"
        )
        subprocess.run([str(state / "venvs/ui/bin/python"), "-I", "-c", script, str(key), str(jwks)], check=True)
    keys = json.loads(jwks.read_text()).get("keys") or []
    if len(keys) != 1 or not str(keys[0].get("kid") or "").startswith("xperfect-local-") or "d" in keys[0]:
        raise RuntimeError(f"Local sign-in keys are damaged; move {key.parent} aside and start again")
    return str(keys[0]["kid"])


def role_environment(state: Path, config: dict, env: dict, role: str) -> dict:
    """UI signs, runtime verifies, MCP neither. Only the runtime API reads model choices."""
    model_names = (set(_launcher().MODEL_ENVIRONMENTS.values()) | {"WPR_MODEL_HOST_CODEX_CLI"}
                   if role != "api" else set())
    env = {k: v for k, v in env.items()
           if k not in ASSERTION_KEYS and not k.startswith("GLASSHIVE_INTERNAL_ASSERTION_")
           and k not in model_names}
    if role == "mcp":
        return env
    secret = json.loads((state / "secrets.json").read_text())
    key, jwks = assertion_paths(state)
    env.update({
        "GLASSHIVE_HUMAN_AUTH_MODE": "local_password", "GLASSHIVE_LOCAL_HUMAN_ASSERTION": "1",
        "GLASSHIVE_LOCAL_AUTH_NAMESPACE": secret["local_auth_namespace"],
        "GLASSHIVE_AUTH_SESSION_TTL_SECONDS": LOCAL_SESSION_TTL_SECONDS,
        "GLASSHIVE_INTERNAL_ASSERTION_ISSUER": f"http://127.0.0.1:{config['ports']['ui']}/local-assertion",
        "GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE": LOCAL_ASSERTION_AUDIENCE,
    })
    if role == "ui":
        env.update({
            "GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE": str(key),
            "GLASSHIVE_INTERNAL_ASSERTION_KEY_ID": json.loads(jwks.read_text())["keys"][0]["kid"],
            "GLASSHIVE_INTERNAL_ASSERTION_TTL_SECONDS": "30",
            "GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY": secret["local_auth_throttle_key"],
        })
    else:
        env["GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE"] = str(jwks)
    return env


def _generated_unlock_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        value = "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(5))
        if len(set(value)) >= 13:
            return value


OWNER_CHECK = """
import json
from glass_drive_ui.auth_gateway import HumanAuthGateway
gateway = HumanAuthGateway.from_env()
with gateway._connect() as conn:
    principals = conn.execute("SELECT COUNT(*) FROM auth_principals").fetchone()[0]
try:
    gateway.validate_local_owner()
    print(json.dumps({"valid": True, "principals": principals}))
except RuntimeError as exc:
    print(json.dumps({"valid": False, "principals": principals, "reason": str(exc)}))
"""


def ensure_local_owner(state: Path, config: dict, *, password_stdin: bool = False) -> None:
    """Keep an unlocked owner as is; create the unlock password only on first start."""
    ui_python = str(state / "venvs/ui/bin/python")
    env = role_environment(state, config, environment(state, config, "setup"), "ui")
    check = subprocess.run([ui_python, "-c", OWNER_CHECK], cwd=ROOT, env=env, capture_output=True, text=True)
    try:
        owner = json.loads(check.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise RuntimeError("Could not check the unlock setup; see private logs") from None
    if owner.get("valid"):
        return
    if owner.get("principals"):
        # An owner exists but is not usable (for example a disabled password).
        # Never replace it here: that would reset the password and sign out every browser.
        raise RuntimeError("The unlock setup exists but is not usable and was left unchanged: "
                           f"{owner.get('reason') or 'unknown'}. See docs/host-setup.md, 'Unlock password'")
    generated = False
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    elif sys.stdin.isatty():
        import getpass
        print("xPerfect keeps your workspaces behind an unlock password on this computer.")
        password = getpass.getpass("Choose one (24+ characters), or press Enter to have one made for you: ")
        if password:
            if getpass.getpass("Type it again: ") != password:
                raise RuntimeError("The two passwords differ; run start again")
        elif sys.stdout.isatty():
            password, generated = _generated_unlock_password(), True
        else:
            raise RuntimeError("Output is not a terminal, so a made-up password could not be shown safely; type one instead")
    else:
        raise RuntimeError("First start needs an unlock password: run ./xperfect start in a terminal "
                           "(or pass --unlock-password-stdin)")
    result = subprocess.run(
        [ui_python, "-m", "glass_drive_ui.auth_admin", "provision-local-owner", "--stdin-json"],
        cwd=ROOT, env=env, input=json.dumps({"password": password}), capture_output=True, text=True)
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["Unlock password was not accepted"])[-1]
        raise RuntimeError(message.replace(password, "***"))
    if generated:
        print(f"Your unlock password: {password}")
        print("Save it in your password manager now. It is not stored anywhere readable.")
    print("Unlock xPerfect once in your browser; that browser stays unlocked for 30 days.")


def install(state: Path) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run again")
    for name, project in PROJECTS.items():
        env = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
        env["UV_PROJECT_ENVIRONMENT"] = str(state / "venvs" / name)
        subprocess.run([uv, "sync", "--frozen", "--no-dev", "--project", str(project)], env=env, check=True)


def commands(state: Path, config: dict) -> dict[str, list[str]]:
    runtime = str(state / "venvs/runtime/bin/python")
    ui = str(state / "venvs/ui/bin/python")
    ports = config["ports"]
    return {
        "api": [runtime, "-m", "uvicorn", "workers_projects_runtime.api:create_app", "--factory", "--host", "127.0.0.1", "--port", str(ports["api"]), "--no-access-log"],
        "ui": [ui, "-m", "uvicorn", "glass_drive_ui.server:create_app", "--factory", "--host", "127.0.0.1", "--port", str(ports["ui"]), "--no-access-log"],
        "mcp": [runtime, "-m", "workers_projects_runtime.mcp_server", "--transport", "streamable-http", "--base-url", f"http://127.0.0.1:{ports['api']}", "--host", "127.0.0.1", "--port", str(ports["mcp"])],
    }


def probe(config: dict, instance: str, env: dict) -> dict:
    result = {}
    for name, port in config["ports"].items():
        try:
            token = env["GLASSHIVE_MCP_API_KEY"] if name == "mcp" else env["WPR_API_TOKEN"]
            request = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, timeout=2) as response:
                body = json.load(response)
            result[name] = body.get("status") == "ok" and body.get("release", {}).get("release_id") == instance
        except (OSError, ValueError):
            result[name] = False
    return result


def shutdown(children: dict) -> None:
    for child in children.values():
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 15
    for child in children.values():
        try:
            child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def serve(state: Path) -> None:
    os.umask(0o077)
    children = {}
    running = True
    def stop(_signum, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    lock = (state / "service.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    sock_path = control_path(state)
    config = configuration(state)
    instance = secrets.token_hex(16)
    error = None
    try:
        for name, port in config["ports"].items():
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    check.bind(("127.0.0.1", port))
                except OSError as exc:
                    if exc.errno == errno.EADDRINUSE:
                        raise RuntimeError(
                            f"Port {port} for the {PORT_LABELS[name]} is already in use on 127.0.0.1; nothing was stopped. "
                            "Stop the program using it, or change the ports in config.json while xPerfect is stopped") from None
                    # Any other failure (for example a permission refusal) is reported as itself.
                    raise RuntimeError(f"Port {port} for the {PORT_LABELS[name]} could not be opened on 127.0.0.1: "
                                       f"{exc.strerror or exc}; nothing was stopped") from None
        env = environment(state, config, instance)
        ensure_assertion_keys(state)
        for name, command in commands(state, config).items():
            with (state / "logs" / f"{name}.log").open("a") as log:
                children[name] = subprocess.Popen(command, cwd=ROOT, env=role_environment(state, config, env, name),
                                                  stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 60
        ready = {}
        while running and time.monotonic() < deadline:
            if any(child.poll() is not None for child in children.values()):
                raise RuntimeError("A service exited. See the private API/UI/MCP logs")
            ready = probe(config, instance, env)
            if all(ready.values()):
                break
            time.sleep(0.2)
        if not all(ready.values()):
            raise RuntimeError(f"Readiness failed: {ready}. See private logs")
        with socket.socket(socket.AF_UNIX) as server:
            Path(sock_path).unlink(missing_ok=True)
            server.bind(sock_path)
            server.listen(4)
            server.settimeout(0.5)
            write_json(state / "startup.json", {"status": "ready", "instance": instance})
            while running:
                if any(child.poll() is not None for child in children.values()):
                    raise RuntimeError("A service exited; remaining services were stopped. Run doctor, then start")
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(3)
                    request = connection.recv(64).decode()
                    payload = {"status": "running", "instance": instance, "ports": config["ports"], "checkout": str(ROOT)}
                    if request == "stop":
                        running = False
                        payload["status"] = "stopping"
                    elif request == "doctor":
                        payload["health"] = probe(config, instance, env)
                    connection.sendall(json.dumps(payload).encode())
    except Exception as exc:
        error = str(exc)
    finally:
        shutdown(children)
        Path(sock_path).unlink(missing_ok=True)
        if error:
            write_json(state / "startup.json", {"status": "failed", "error": error})
        lock.close()


def stop_services(state: Path) -> None:
    result = control(state, "stop")
    if result is None:
        with (state / "service.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Startup or shutdown is still in progress. Retry shortly; no other process was stopped")
        print("xPerfect is stopped.")
        return
    deadline = time.monotonic() + 25
    while Path(control_path(state)).exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if Path(control_path(state)).exists():
        raise RuntimeError("Shutdown is still in progress; inspect private logs before retrying")
    print("xPerfect stopped. Files, accounts and project state are preserved.")


def start_services(state: Path, config: dict, *, password_stdin: bool = False) -> None:
    existing = control(state, "doctor")
    if existing:
        if existing.get("checkout") != str(ROOT):
            raise RuntimeError("This state is running from another checkout. Stop that instance before upgrading")
        if not all(existing.get("health", {}).values()):
            raise RuntimeError("A running service is unhealthy. Run doctor, then restart")
        print(f"xPerfect is running: http://127.0.0.1:{config['ports']['ui']}")
        return
    install(state)
    ensure_assertion_keys(state)
    ensure_local_owner(state, config, password_stdin=password_stdin)
    private_dir(state / "logs")
    (state / "startup.json").unlink(missing_ok=True)
    with (state / "logs/supervisor.log").open("a") as log:
        child = subprocess.Popen([sys.executable, str(ROOT / "xperfect"), "_serve", "--state-dir", str(state)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        if control(state, "status"):
            print(f"xPerfect ready: http://127.0.0.1:{config['ports']['ui']}")
            print(f"API: http://127.0.0.1:{config['ports']['api']}  MCP: http://127.0.0.1:{config['ports']['mcp']}/mcp")
            print("Use ./xperfect doctor for native CLI and account setup status before running work.")
            return
        report = state / "startup.json"
        if report.exists():
            value = json.loads(report.read_text())
            if value.get("status") == "failed":
                raise RuntimeError(value["error"])
        if child.poll() is not None:
            raise RuntimeError(f"Startup exited; inspect {state / 'logs/supervisor.log'}")
        time.sleep(0.2)
    child.terminate()
    raise RuntimeError("Startup timed out; inspect private logs")


def model_report(config: dict) -> dict:
    """The model each installed harness gets for new work, stated without inventing one."""
    models, env = models_of(config), config["env"]
    report = {}
    for cli, profile in (("codex", "codex-cli"), ("claude", "claude-code"), ("grok", "grok-build")):
        if not shutil.which(cli):
            if profile in models:  # a saved choice stays visible until its CLI is installed
                report[profile] = f"{models[profile]} (the {cli} CLI is not installed or not on PATH)"
            continue
        written = next((env[name] for name in HOST_MODEL_ENVIRONMENTS.get(profile, ()) if env.get(name)), "")
        if profile in models:
            report[profile] = models[profile]
        elif written:
            report[profile] = written + " (from config.json env)"
        elif profile == "grok-build":
            report[profile] = "missing: Grok needs an exact model. Run ./xperfect restart --model grok-build=<model>"
        elif profile == "codex-cli":
            report[profile] = "not set: Codex uses the model in its own Codex configuration"
        else:
            report[profile] = 'not set: xPerfect asks Claude Code for its "opus" model alias'
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="xPerfect local host setup and service control")
    parser.add_argument("command", choices=["start", "stop", "restart", "doctor", "mcp", "_serve"], nargs="?", default="start")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("XPERFECT_STATE_DIR", str(Path.home() / ".local/state/xperfect"))))
    for name, port in DEFAULT_PORTS.items():
        parser.add_argument(f"--{name}-port", type=int, default=port, help="Used when first creating config.json")
    parser.add_argument("--unlock-password-stdin", action="store_true",
                        help="First start only: read the new unlock password from standard input")
    parser.add_argument("--model", action="append", metavar="PROFILE=MODEL",
                        help="start/restart: save the exact model for a harness, e.g. grok-build=<model>")
    args = parser.parse_args()
    os.umask(0o077)
    state = args.state_dir.expanduser().absolute()
    try:
        config = configuration(state, {name: getattr(args, f"{name}_port") for name in DEFAULT_PORTS})
        if args.model and args.command not in {"start", "restart"}:
            raise RuntimeError("--model applies to start or restart")
        models = _launcher().parse_model_arguments(args.model) if args.model else {}
        command_lock = None
        if args.command in {"start", "stop", "restart"}:
            command_lock = (state / "command.lock").open("a")
            try:
                fcntl.flock(command_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another start/stop/restart command is running; retry when it finishes")
        if args.command == "mcp":
            if not control(state, "status"):
                raise RuntimeError("Run ./xperfect start before connecting an MCP client")
            command = commands(state, config)["mcp"]
            command[command.index("streamable-http")] = "stdio"
            os.execve(command[0], command, role_environment(state, config, environment(state, config, "stdio"), "mcp"))
        elif args.command == "_serve":
            serve(state)
        elif args.command == "stop":
            stop_services(state)
        elif args.command == "doctor":
            result = control(state, "doctor") or {"status": "stopped", "ports": config["ports"]}
            result["state"] = str(state)
            result["uv"] = bool(shutil.which("uv"))
            result["native_cli"] = {name: bool(shutil.which(name)) for name in ("codex", "claude", "grok", "openclaw")}
            result["provider_auth"] = "Native login: codex login / claude auth login. CLI presence does not prove authentication or a completed run"
            result["models"] = model_report(config)
            if result["status"] == "running":
                try:
                    env = environment(state, config, result["instance"])
                    request = urllib.request.Request(f"http://127.0.0.1:{config['ports']['api']}/health", headers={"Authorization": f"Bearer {env['WPR_API_TOKEN']}"})
                    with urllib.request.urlopen(request, timeout=2) as response:
                        result["ui_account_setup"] = json.load(response).get("provider_setup_support", {})
                except (OSError, ValueError):
                    result["ui_account_setup"] = "unavailable"
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "running" and all(result.get("health", {}).values()) else 1
        else:
            changed = set_models(state, config, models) != config
            config = configuration(state)
            if args.command == "start" and changed and control(state, "status"):
                print("Saved the model choice. Run ./xperfect restart to apply it.")
                return 0
            if args.command == "restart":
                stop_services(state)
            start_services(state, config, password_stdin=args.unlock_password_stdin)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"xPerfect: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
