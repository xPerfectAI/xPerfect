"""Connect a Teams channel with one button, and keep its channel package running.

The owner never runs a terminal command. This helper wraps Microsoft's Teams developer CLI
(pinned) for sign-in and app creation, keeps the app secret in a private file, and starts
the ``integrations/teams`` package for each connected channel. The owner types Microsoft's
sign-in code on Microsoft's own page; nothing here enters it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .experts import ExpertError

TEAMS_CLI_PACKAGE = "@microsoft/teams.cli@3.1.0"
_CODE = re.compile(r"(https://\S+)\s+and enter the code\s+([A-Z0-9]{6,12})")


def _checkout() -> Path:
    return Path(__file__).resolve().parents[3]


def _state_dir() -> Path:
    explicit = os.environ.get("XPERFECT_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("WPR_DB_PATH", "")).resolve().parent.parent


def _private(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    keep = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG")
    env = {key: os.environ[key] for key in keep if os.environ.get(key)}
    env.update(extra or {})
    return env


class TeamsSetup:
    """One sign-in and app creation at a time per channel, and one package process per channel."""

    def __init__(self, channels: Any) -> None:
        self.channels = channels
        self._sessions: dict[str, dict] = {}
        self._packages: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ tools
    def _cli(self) -> list[str]:
        configured = os.environ.get("XPERFECT_TEAMS_CLI", "").strip()
        if configured and Path(configured).exists():
            return [configured]
        found = shutil.which("teams")
        if found:
            return [found]
        local = _state_dir() / "tools" / "teams-cli" / "node_modules" / ".bin" / "teams"
        if local.exists():
            return [str(local)]
        npm = shutil.which("npm")
        if not npm:
            raise ExpertError("Teams setup needs Node.js on this computer. Install Node.js, then press Connect again.",
                              code="channel_setup_needs_node")
        target = _private(_state_dir() / "tools") / "teams-cli"
        subprocess.run([npm, "install", "--no-fund", "--no-audit", "--prefix", str(target), TEAMS_CLI_PACKAGE],
                       check=True, capture_output=True, timeout=600, env=_clean_env())
        return [str(local)]

    def _run(self, *args: str, timeout: int = 300) -> dict:
        completed = subprocess.run([*self._cli(), *args, "--json", "--disable-auto-update"], capture_output=True,
                                   text=True, timeout=timeout, env=_clean_env({"TEAMS_NO_INTERACTIVE": "1"}))
        text = completed.stdout.strip()
        start = text.find("{")
        try:
            value = json.loads(text[start:]) if start >= 0 else {}
        except ValueError:
            value = {}
        if completed.returncode != 0:
            detail = (value.get("error") or value.get("message") or completed.stderr.strip() or text)[:400]
            raise ExpertError(f"Microsoft Teams setup stopped: {detail}", code="channel_setup_failed")
        return value

    def credentials_path(self, binding_id: str) -> Path:
        return _private(_state_dir() / "data" / "channels") / f"{binding_id}.env"

    # ------------------------------------------------------------------ connect
    def status(self, tenant: str, owner: str, binding_id: str) -> dict:
        binding = self.channels._binding(binding_id, tenant, owner)
        with self._lock:
            session = dict(self._sessions.get(binding_id) or {})
        if not session:
            view = self.channels.view(binding)
            return {"step": "connected" if view["app"].get("app_id") else "start", "channel": view}
        if session.get("step") == "sign_in" and session["process"].poll() is not None:
            ok = session["process"].returncode == 0
            with self._lock:
                self._sessions[binding_id] = session = {**session, "step": "creating" if ok else "error",
                                                        **({} if ok else {"error": "Microsoft sign-in did not finish. Press Connect to try again."})}
            if ok:
                threading.Thread(target=self._create, args=(tenant, owner, binding_id), daemon=True).start()
        # A code is shown only while it can still be used.
        keys = ("step", "url", "code", "error") if session.get("step") == "sign_in" else ("step", "error")
        public = {key: session[key] for key in keys if key in session}
        return {**public, "channel": self.channels.view(self.channels._binding(binding_id))}

    def connect(self, tenant: str, owner: str, binding_id: str) -> dict:
        binding = self.channels._binding(binding_id, tenant, owner)
        if binding["kind"] != "teams":
            raise ExpertError("This channel is not available yet", code="channel_kind_unavailable")
        with self._lock:
            current = self._sessions.get(binding_id)
            if current and current.get("step") in {"sign_in", "creating"}:
                return self.status(tenant, owner, binding_id)
        signed_in = False
        try:
            signed_in = bool(self._run("status", timeout=60).get("loggedIn"))
        except ExpertError:
            signed_in = False
        if signed_in:
            with self._lock:
                self._sessions[binding_id] = {"step": "creating"}
            threading.Thread(target=self._create, args=(tenant, owner, binding_id), daemon=True).start()
            return self.status(tenant, owner, binding_id)
        process = subprocess.Popen([*self._cli(), "login", "--device-code", "--disable-auto-update"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   env=_clean_env({"TEAMS_NO_INTERACTIVE": "1"}))
        found = None
        deadline = time.monotonic() + 60
        lines: list[str] = []
        while time.monotonic() < deadline and process.poll() is None:
            line = process.stdout.readline() if process.stdout else ""
            if not line:
                time.sleep(0.1)
                continue
            lines.append(line)
            found = _CODE.search(line)
            if found:
                break
        if not found:
            process.kill()
            raise ExpertError("Microsoft sign-in did not start. Try Connect again.", code="channel_sign_in_unavailable")
        threading.Thread(target=lambda: process.stdout and process.stdout.read(), daemon=True).start()
        with self._lock:
            self._sessions[binding_id] = {"step": "sign_in", "url": found.group(1).rstrip("."), "code": found.group(2),
                                          "process": process}
        return self.status(tenant, owner, binding_id)

    def _create(self, tenant: str, owner: str, binding_id: str) -> None:
        try:
            binding = self.channels._binding(binding_id, tenant, owner)
            expert = self.channels.experts.latest(tenant, owner, binding["lineage_id"])
            status = self._run("status", timeout=60)
            path = self.credentials_path(binding_id)
            created = self._run("app", "create", "--name", expert["name"][:30], "--messaging-mode", "socket",
                                "--teams-managed", "--env", str(path), timeout=600)
            if path.exists():
                path.chmod(0o600)
            settings = {"app_id": created.get("botId", ""), "teams_app_id": created.get("teamsAppId", ""),
                        "app_name": created.get("appName", expert["name"]),
                        "tenant_id": str(status.get("tenantId") or ""),
                        "mode": "socket" if created.get("socketMode") else "http",
                        "install_link": created.get("installLink", "")}
            self.channels.record_setup(tenant, owner, binding_id, settings)
            # The signed-in owner is the first person who may message the Expert.
            people = self.channels.view(self.channels._binding(binding_id))["people"]
            me = {"id": str(status.get("userObjectId") or ""), "name": str(status.get("username") or "You"),
                  "tenant": str(status.get("tenantId") or "")}
            if me["id"] and all(person["id"] != me["id"] for person in people):
                people.append(me)
            self.channels.update(tenant, owner, binding_id, people=people,
                                 state="connected" if settings["mode"] == "socket" else None)
            note = {} if settings["mode"] == "socket" else {
                "error": "Teams did not turn on socket mode for this app. " + str(created.get("socketModeError") or "")}
            with self._lock:
                self._sessions[binding_id] = {"step": "connected" if not note else "error", **note}
            self.ensure_package(binding_id)
        except Exception as exc:
            with self._lock:
                self._sessions[binding_id] = {"step": "error", "error": str(exc)[:400]}

    # ------------------------------------------------------------------ package processes
    def _package_python(self) -> Path:
        venv = _state_dir() / "venvs" / "teams"
        python = venv / "bin" / "python"
        if not python.exists():
            uv = shutil.which("uv")
            if not uv:
                raise ExpertError("The Teams channel needs uv to install its package", code="channel_package_unavailable")
            subprocess.run([uv, "sync", "--quiet", "--frozen", "--project", str(_checkout() / "integrations" / "teams")],
                           check=True, capture_output=True, timeout=900,
                           env=_clean_env({"UV_PROJECT_ENVIRONMENT": str(venv)}))
        return python

    def _pid_path(self, binding_id: str) -> Path:
        return self.credentials_path(binding_id).with_suffix(".pid")

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        try:
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                     timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "xperfect_teams" in command

    def ensure_package(self, binding_id: str) -> str:
        """Exactly one package process per connected channel, also across runtime restarts."""
        binding = self.channels._binding(binding_id)
        credentials = self.credentials_path(binding_id)
        pid_path = self._pid_path(binding_id)
        pid = int(pid_path.read_text().strip() or 0) if pid_path.exists() else 0
        running = bool(pid) and self._alive(pid)
        if binding["state"] != "connected" or not credentials.exists():
            if running:
                os.kill(pid, 15)
            pid_path.unlink(missing_ok=True)
            return "off"
        if running:
            return "running"
        runtime = os.environ.get("GLASSHIVE_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
        logs = _private(_state_dir() / "logs")
        with open(logs / f"teams-{binding_id[-8:]}.log", "ab") as handle:
            process = subprocess.Popen(
                [str(self._package_python()), "-m", "xperfect_teams", "--runtime", runtime, "--binding", binding_id,
                 "--state-dir", str(_state_dir()), "--credentials", str(credentials)],
                stdout=handle, stderr=subprocess.STDOUT, env=_clean_env(), cwd=str(_state_dir()),
                start_new_session=True)
        self._packages[binding_id] = process
        pid_path.write_text(str(process.pid))
        pid_path.chmod(0o600)
        return "started"

    def reconcile_once(self) -> list[dict]:
        outcomes = []
        with self.channels.store._connect() as conn:
            ids = [row[0] for row in conn.execute("SELECT binding_id FROM channel_bindings WHERE kind='teams'")]
        for binding_id in ids:
            try:
                state = self.ensure_package(binding_id)
                if state == "started":
                    outcomes.append({"binding_id": binding_id, "package": state})
            except Exception as exc:
                outcomes.append({"binding_id": binding_id, "error": type(exc).__name__})
        return outcomes

    def stop(self, binding_id: str) -> None:
        pid_path = self._pid_path(binding_id)
        pid = int(pid_path.read_text().strip() or 0) if pid_path.exists() else 0
        if pid and self._alive(pid):
            os.kill(pid, 15)
        pid_path.unlink(missing_ok=True)
