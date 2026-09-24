"""Causal checks for the standalone lifecycle boundary; no provider calls."""
import contextlib
import importlib.util
import io
import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("host", Path(__file__).resolve().parents[1] / "scripts/host.py")
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


class HostActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"

    def test_private_state_and_credentials_survive_reconfiguration(self):
        config = host.configuration(self.state)
        secret = (self.state / "secrets.json").read_bytes()
        again = host.configuration(self.state, {"api": 22001, "ui": 22002, "mcp": 22003})
        self.assertEqual(config, again)
        self.assertEqual(secret, (self.state / "secrets.json").read_bytes())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "secrets.json").stat().st_mode & 0o777, 0o600)
        values = json.loads(secret)
        self.assertEqual(set(values), {"api_token", "mcp_token", "link_secret",
                                       "local_auth_throttle_key", "local_auth_namespace"})
        self.assertEqual(len(set(values.values())), len(values))

    def test_local_launcher_does_not_import_parent_runtime_identity(self):
        config = host.configuration(self.state)
        with patch.dict(os.environ, {"VIVENTIUM_ENV_FILE": "/private/unrelated.env", "WPR_DB_PATH": "/unrelated.db", "GLASSHIVE_SECURITY_MODE": "multi_user"}):
            env = host.environment(self.state, config, "expected-instance")
        self.assertEqual(env["VIVENTIUM_ENV_FILE"], "")
        self.assertEqual(env["GLASSHIVE_SECURITY_MODE"], "local")
        self.assertEqual(env["WPR_DB_PATH"], str(self.state / "data/runtime.db"))
        self.assertEqual(env["WPR_BOOTSTRAP_SOURCE_ROOTS"], str(self.state / "data/managed-files"))
        self.assertEqual(env["WPR_DEFAULT_EXECUTION_MODE"], "host")

    def test_local_peer_endpoint_tracks_its_api_port(self):
        config = host.configuration(self.state, {"api": 22001, "ui": 22002, "mcp": 22003})
        config["env"]["GLASSHIVE_PEER_RUNTIME_BASE_URL"] = "https://other.example"
        with patch.dict(os.environ, {"GLASSHIVE_PEER_RUNTIME_BASE_URL": "http://127.0.0.1:9999"}):
            env = host.environment(self.state, config, "expected-instance")
        self.assertEqual(env["GLASSHIVE_PEER_RUNTIME_BASE_URL"], "http://127.0.0.1:22001")
        self.assertEqual(env["GLASSHIVE_RUNTIME_BASE_URL"], "http://127.0.0.1:22001")

    def test_conversation_work_stays_in_private_state_not_the_checkout(self):
        config = host.configuration(self.state)
        with patch.dict(os.environ, {"GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE": str(host.ROOT)}):
            env = host.environment(self.state, config, "expected-instance")
        workspace = Path(env["GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE"])
        self.assertEqual(workspace, self.state / "workspaces" / "conversation")
        self.assertTrue(workspace.is_dir())
        self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(host.ROOT, workspace.parents)
        chosen = Path(self.temp.name) / "chosen-life"
        config["env"]["GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE"] = str(chosen)
        self.assertEqual(host.environment(self.state, config, "expected-instance")["GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE"], str(chosen))

    def test_local_launcher_offers_native_connections_without_hidden_flags(self):
        config = host.configuration(self.state)
        env = host.environment(self.state, config, "expected-instance")
        self.assertEqual(env["GLASSHIVE_ENABLE_NATIVE_API_KEYS"], "1")
        self.assertEqual(env["GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS"], "1")
        config["env"].update(
            GLASSHIVE_ENABLE_NATIVE_API_KEYS="0",
            GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS="0",
        )
        disabled = host.environment(self.state, config, "expected-instance")
        self.assertEqual(disabled["GLASSHIVE_ENABLE_NATIVE_API_KEYS"], "0")
        self.assertEqual(disabled["GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS"], "0")

    def test_rejects_duplicate_ports_before_launch(self):
        with self.assertRaisesRegex(RuntimeError, "ports must differ"):
            host.configuration(self.state, {"api": 19000, "ui": 19000, "mcp": 19001})

    def test_rejects_foreign_state_symlink(self):
        target = Path(self.temp.name) / "other"
        target.mkdir()
        self.state.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            host.configuration(self.state)

    def test_does_not_accept_healthy_different_instance(self):
        config = host.configuration(self.state)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"status":"ok","release":{"release_id":"other"}}'
        with patch.object(host.urllib.request, "urlopen", return_value=Response()):
            result = host.probe(config, "expected", {"WPR_API_TOKEN": "synthetic", "GLASSHIVE_MCP_API_KEY": "synthetic-mcp"})
        self.assertFalse(any(result.values()))

    def test_an_occupied_port_is_named_and_its_owner_keeps_running(self):
        ports = {}
        for name in host.DEFAULT_PORTS:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                ports[name] = sock.getsockname()[1]
        host.configuration(self.state, ports)
        host.private_dir(self.state / "logs")
        old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        with socket.socket() as owner:
            owner.bind(("127.0.0.1", ports["mcp"]))
            owner.listen(1)
            try:
                with patch.object(host.subprocess, "Popen", side_effect=AssertionError("nothing may start")):
                    host.serve(self.state)
            finally:
                for sig, handler in old_handlers.items(): signal.signal(sig, handler)
            report = json.loads((self.state / "startup.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertIn(f"Port {ports['mcp']} for the MCP server is already in use", report["error"])
            self.assertIn("nothing was stopped", report["error"])
            owner.getsockname()  # the owner's socket is still open and listening

    def test_a_port_failure_other_than_in_use_is_reported_as_itself(self):
        ports = {}
        for name in host.DEFAULT_PORTS:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                ports[name] = sock.getsockname()[1]
        host.configuration(self.state, ports)
        host.private_dir(self.state / "logs")
        old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        refused = PermissionError(errno.EACCES, "Permission denied")
        try:
            with patch.object(host.socket.socket, "bind", side_effect=refused), \
                    patch.object(host.subprocess, "Popen", side_effect=AssertionError("nothing may start")):
                host.serve(self.state)
        finally:
            for sig, handler in old_handlers.items(): signal.signal(sig, handler)
        error = json.loads((self.state / "startup.json").read_text())["error"]
        self.assertIn("could not be opened on 127.0.0.1: Permission denied", error)
        self.assertNotIn("already in use", error)

    def test_failed_launch_stops_siblings_before_reporting_failure(self):
        ports = {}
        for name in host.DEFAULT_PORTS:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                ports[name] = sock.getsockname()[1]
        host.configuration(self.state, ports)
        host.private_dir(self.state / "logs")
        children = []
        original_popen = subprocess.Popen
        def capture(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child
        commands = {"api": [sys.executable, "-c", "raise SystemExit(7)"], "ui": [sys.executable, "-c", "import time; time.sleep(30)"], "mcp": [sys.executable, "-c", "import time; time.sleep(30)"]}
        old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            with patch.object(host, "commands", return_value=commands), patch.object(host, "probe", return_value={"api": False}), patch.object(host.subprocess, "Popen", side_effect=capture):
                host.serve(self.state)
        finally:
            for sig, handler in old_handlers.items(): signal.signal(sig, handler)
        self.assertEqual(json.loads((self.state / "startup.json").read_text())["status"], "failed")
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertFalse(Path(host.control_path(self.state)).exists())



class HostModelTests(unittest.TestCase):
    """Exact model choices are typed, persisted and reach only the runtime API."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"

    def test_saved_model_survives_restart_and_reaches_only_the_runtime_api(self):
        config = host.set_models(self.state, host.configuration(self.state), {"grok-build": "grok-exact-1"})
        again = host.configuration(self.state)
        self.assertEqual(again["models"], {"grok-build": "grok-exact-1"})
        self.assertEqual(again["env"], {})
        with patch.dict(os.environ, {"WPR_MODEL_GROK_BUILD": "ambient-other-model"}):
            env = host.environment(self.state, again, "instance")
        self.assertEqual(env["WPR_MODEL_GROK_BUILD"], "grok-exact-1")
        self.assertEqual(host.role_environment(self.state, again, env, "api")["WPR_MODEL_GROK_BUILD"], "grok-exact-1")
        self.assertNotIn("WPR_MODEL_GROK_BUILD", host.role_environment(self.state, again, env, "mcp"))
        self.assertEqual(config, again)

    def test_no_model_is_invented_and_ambient_models_are_ignored(self):
        config = host.configuration(self.state)
        with patch.dict(os.environ, {"WPR_MODEL_GROK_BUILD": "ambient-other-model"}):
            env = host.environment(self.state, config, "instance")
        self.assertNotIn("WPR_MODEL_GROK_BUILD", env)
        with patch.object(host.shutil, "which", lambda name: "/usr/bin/" + name):
            report = host.model_report(config)
        self.assertTrue(report["grok-build"].startswith("missing"))
        self.assertIn("--model grok-build=", report["grok-build"])
        self.assertEqual(report["codex-cli"], "not set: Codex uses the model in its own Codex configuration")
        self.assertIn('"opus" model alias', report["claude-code"])

    def test_the_saved_codex_model_reaches_the_host_codex_runtime_and_the_shell_cannot_override_it(self):
        from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
        config = host.configuration(self.state)
        with patch.dict(os.environ, {"CODEX_MODEL": "shell-model"}):
            unset = host.environment(self.state, config, "instance")
        self.assertNotIn("CODEX_MODEL", unset)
        config = host.set_models(self.state, config, {"codex-cli": "gpt-exact-1"})
        with patch.dict(os.environ, {"CODEX_MODEL": "shell-model"}):
            env = host.environment(self.state, config, "instance")
        api = host.role_environment(self.state, config, env, "api")
        with patch.dict(os.environ, api, clear=True):
            self.assertEqual(HostCodexCliRuntime.resolve_model(object.__new__(HostCodexCliRuntime), "codex-cli"),
                             "gpt-exact-1")
        self.assertNotIn("WPR_MODEL_HOST_CODEX_CLI", host.role_environment(self.state, config, env, "mcp"))

    def test_a_saved_model_stays_visible_before_its_cli_is_installed(self):
        config = host.set_models(self.state, host.configuration(self.state), {"grok-build": "grok-exact-1"})
        with patch.object(host.shutil, "which", lambda name: None):
            report = host.model_report(config)
        self.assertEqual(report, {"grok-build": "grok-exact-1 (the grok CLI is not installed or not on PATH)"})

    def test_invalid_or_conflicting_model_configuration_is_refused(self):
        host.configuration(self.state)
        path = self.state / "config.json"
        value = json.loads(path.read_text())
        for models, message in (({"grok": "x"}, "Unknown worker profile"), ({"grok-build": "has space"}, "exact model ID")):
            path.write_text(json.dumps({**value, "models": models}))
            with self.assertRaisesRegex(RuntimeError, message):
                host.configuration(self.state)
        path.write_text(json.dumps({**value, "models": {"grok-build": "a"}, "env": {"WPR_MODEL_GROK_BUILD": "b"}}))
        with self.assertRaisesRegex(RuntimeError, "two different grok-build models"):
            host.configuration(self.state)
        for name, profile in (("CODEX_MODEL", "codex-cli"), ("WPR_MODEL_HOST_CODEX_CLI", "codex-cli"),
                              ("WPR_CLAUDE_CODE_PROVIDER_MODEL", "claude-code")):
            path.write_text(json.dumps({**value, "models": {profile: "a"}, "env": {name: "b"}}))
            with self.assertRaisesRegex(RuntimeError, f"two different {profile} models"):
                host.configuration(self.state)


class LocalUnlockTests(unittest.TestCase):
    """O06: one unlock password, created once; the UI signs, the runtime verifies."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.config = host.configuration(self.state)
        key, jwks = host.assertion_paths(self.state)
        host.private_dir(key.parent)
        key.write_text("synthetic private key")
        jwks.write_text(json.dumps({"keys": [{"kty": "RSA", "n": "x", "e": "AQAB", "kid": "xperfect-local-test"}]}))

    def test_ui_signs_runtime_verifies_and_mcp_holds_neither(self):
        base = host.environment(self.state, self.config, "instance")
        ui = host.role_environment(self.state, self.config, base, "ui")
        api = host.role_environment(self.state, self.config, base, "api")
        mcp = host.role_environment(self.state, self.config, base, "mcp")
        key, jwks = host.assertion_paths(self.state)
        self.assertEqual(ui["GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE"], str(key))
        self.assertEqual(ui["GLASSHIVE_INTERNAL_ASSERTION_KEY_ID"], "xperfect-local-test")
        self.assertGreaterEqual(len(ui["GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY"]), 32)
        self.assertNotIn("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE", api)
        self.assertEqual(api["GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE"], str(jwks))
        for role_env in (ui, api):
            self.assertEqual(role_env["GLASSHIVE_HUMAN_AUTH_MODE"], "local_password")
            self.assertEqual(role_env["GLASSHIVE_LOCAL_HUMAN_ASSERTION"], "1")
            self.assertEqual(role_env["GLASSHIVE_SECURITY_MODE"], "local")
            self.assertTrue(role_env["WPR_API_TOKEN"])
        self.assertEqual(ui["GLASSHIVE_INTERNAL_ASSERTION_ISSUER"], api["GLASSHIVE_INTERNAL_ASSERTION_ISSUER"])
        self.assertEqual(ui["GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE"], api["GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE"])
        self.assertFalse({k for k in mcp if k in host.ASSERTION_KEYS})
        self.assertEqual(mcp["GLASSHIVE_HUMAN_AUTH_MODE"], "")

    def test_private_config_cannot_redirect_signing_or_trust(self):
        # config.json env reaches environment() unfiltered; the role split must still win.
        config = {**self.config, "env": {
            "GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE": "/elsewhere/key.pem",
            "GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL": "https://elsewhere.invalid/jwks",
            "GLASSHIVE_LOCAL_HUMAN_ASSERTION": "1"}}
        base = host.environment(self.state, config, "instance")
        self.assertEqual(base["GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL"], "https://elsewhere.invalid/jwks")
        api = host.role_environment(self.state, config, base, "api")
        mcp = host.role_environment(self.state, config, base, "mcp")
        self.assertNotIn("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE", api)
        self.assertNotIn("GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL", api)
        self.assertFalse([k for k in mcp if k.startswith("GLASSHIVE_INTERNAL_ASSERTION_")])
        self.assertNotIn("GLASSHIVE_LOCAL_HUMAN_ASSERTION", mcp)

    def _run(self, owner, calls):
        def run(command, **kwargs):
            calls.append((command, kwargs))
            stdout = json.dumps(owner) if "-c" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        return run

    FIRST_START = {"valid": False, "principals": 0, "reason": "Local owner authentication is not safely provisioned"}

    def test_an_unlocked_owner_is_reused_never_reprovisioned(self):
        calls = []
        with patch.object(host.subprocess, "run", self._run({"valid": True, "principals": 1}, calls)):
            host.ensure_local_owner(self.state, self.config)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("provision-local-owner", calls[0][0])

    def test_first_start_without_a_terminal_names_the_one_step(self):
        calls = []
        with patch.object(host.subprocess, "run", self._run(self.FIRST_START, calls)), \
                patch.object(host.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "run ./xperfect start in a terminal"):
                host.ensure_local_owner(self.state, self.config)
        self.assertFalse(any("provision-local-owner" in call[0] for call in calls))

    def test_the_password_travels_only_on_standard_input(self):
        calls = []
        secret_value = "synthetic-unlock-password-with-entropy-42"
        with patch.object(host.subprocess, "run", self._run(self.FIRST_START, calls)), \
                patch.object(host.sys, "stdin", io.StringIO(secret_value + "\n")):
            host.ensure_local_owner(self.state, self.config, password_stdin=True)
        command, kwargs = calls[-1]
        self.assertIn("provision-local-owner", command)
        self.assertEqual(json.loads(kwargs["input"]), {"password": secret_value})
        self.assertNotIn(secret_value, " ".join(command))
        self.assertNotIn(secret_value, json.dumps(kwargs["env"]))

    def test_an_unusable_existing_owner_is_left_unchanged(self):
        calls = []
        owner = {"valid": False, "principals": 1, "reason": "Local owner authentication is not safely provisioned"}
        with patch.object(host.subprocess, "run", self._run(owner, calls)):
            with self.assertRaisesRegex(RuntimeError, "left unchanged"):
                host.ensure_local_owner(self.state, self.config, password_stdin=True)
        self.assertFalse(any("provision-local-owner" in call[0] for call in calls))

    def test_a_made_password_is_only_shown_on_a_terminal(self):
        calls = []
        with patch.object(host.subprocess, "run", self._run(self.FIRST_START, calls)), \
                patch.object(host.sys.stdin, "isatty", return_value=True), \
                patch.object(host.sys.stdout, "isatty", return_value=False), \
                patch("getpass.getpass", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "not a terminal"):
                host.ensure_local_owner(self.state, self.config)
        self.assertFalse(any("provision-local-owner" in call[0] for call in calls))

    def test_a_made_password_meets_the_sign_in_policy(self):
        value = host._generated_unlock_password()
        self.assertGreaterEqual(len(value), 24)
        self.assertGreaterEqual(len(set(value.casefold())), 12)

if __name__ == "__main__":
    unittest.main()
