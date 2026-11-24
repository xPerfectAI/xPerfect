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
from datetime import UTC, datetime
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


_SIGN_IN_UNFINISHED = "Microsoft sign-in did not finish. Press Connect to try again."
_RESTART_FIRST_WAIT_S = 5.0
_RESTART_MAX_WAIT_S = 300.0
_RECHECK_S = 120.0  # while Teams does not allow the app, Microsoft's report is read again this often
# While a change spreads, Microsoft's servers answer differently (no, no, yes, no within eight seconds in the first
# real test). After a "no", Connect goes on only after this many "yes" in a row spanning at least a minute.
_STEADY_READS = 3
_STEADY_SPAN_S = 60.0
_STEADY_RECHECK_S = 30.0
_STILL_APPLYING = ("Microsoft is still applying the change: it has said yes, but not every time yet. This page checks "
                   "again by itself and goes on when the answer is steady.")


def _find(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find(item, key)
            if found is not None:
                return found
    return None


def _custom_apps_blocked(status: dict, name: str) -> str:
    """Microsoft's own report (the Teams CLI's status) of whether the signed-in account may add a custom app: one
    plain line naming both switches (Microsoft's names) and which is off, or "" when allowed or not reported."""
    sideloading = _find(status, "sideloading")
    if not isinstance(sideloading, dict):
        return ""
    tenant, user = sideloading.get("tenant"), sideloading.get("user")
    if tenant is not False and user is not False:
        return ""
    off = "both are" if tenant is False and user is False else ("the first is" if user is False else "the second is")
    return (f"Teams does not yet let the account you signed in with add custom apps, so {name} cannot be installed. "
            "An administrator turns on two switches in the Teams admin center: \"Upload custom apps\" in an app setup "
            "policy (Teams apps → Setup policies; a new policy can give it to this one person only) and \"Let users "
            "interact with custom apps in preview\" (Teams apps → Manage apps → Actions → Org-wide app settings). "
            f"Microsoft reports that {off} off. This page checks again by itself every two minutes.")


def _note_for_admin(status: dict, name: str) -> str:
    """A short note the person can send to their administrator: only the switches Microsoft reports off."""
    sideloading = _find(status, "sideloading")
    sideloading = sideloading if isinstance(sideloading, dict) else {}
    asks = []
    if sideloading.get("user") is False:
        asks.append("Teams apps → Setup policies: turn on \"Upload custom apps\" in an app setup policy assigned to "
                    "my account. A new policy assigned to me alone changes nothing for anyone else.")
    if sideloading.get("tenant") is False:
        asks.append("Teams apps → Manage apps → Actions → Org-wide app settings → Custom apps: turn on \"Let users "
                    "interact with custom apps in preview\".")
    steps = "\n".join(f"{number}. {ask}" for number, ask in enumerate(asks, 1))
    account = str(status.get("username") or "").strip()
    return (f"Hello, could you let me add a Teams app I set up for myself, {name}? In the Teams admin center:\n{steps}\n"
            + (f"My account: {account}.\n" if account else "")
            + "If the admin center says it is still setting up, that can take up to 30 minutes, and Microsoft can take a "
            "while to apply the change. Thank you!")


class TeamsSetup:
    """One sign-in and app creation at a time per channel, and one package process per channel."""

    def __init__(self, channels: Any) -> None:
        self.channels = channels
        self._sessions: dict[str, dict] = {}
        self._packages: dict[str, subprocess.Popen] = {}
        self._started: dict[str, str] = {}  # when each package started here began
        self._restarts: dict[str, tuple[int, float]] = {}  # exits before the socket held, and when to try again
        self._steady: dict[str, tuple[int, float]] = {}  # after a "no": the "yes" in a row, and when the first came
        self._clock = time.monotonic
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
            # The sign-in has ended. Reading never moves anything on (the thread watching the sign-in does, once);
            # it only stops showing a code that can no longer be used.
            ok = session["process"].returncode == 0
            session = {**session, "step": "creating" if ok else "error", **({} if ok else {"error": _SIGN_IN_UNFINISHED})}
        # A code is shown only while it can still be used.
        keys = {"sign_in": ("step", "url", "code", "error"), "blocked": ("step", "error", "note"),
                "creating": ("step", "account", "tenant_id")}.get(session.get("step"), ("step", "error"))
        public = {key: session[key] for key in keys if key in session}
        return {**public, "channel": self.channels.view(self.channels._binding(binding_id))}

    def connect(self, tenant: str, owner: str, binding_id: str) -> dict:
        binding = self.channels._binding(binding_id, tenant, owner)
        if binding["kind"] != "teams":
            raise ExpertError("This channel is not available yet", code="channel_kind_unavailable")
        with self._lock:
            pending = self._pending(binding_id)
        if pending:  # a second press shows the sign-in already under way (status takes the lock itself)
            return self.status(tenant, owner, binding_id)
        signed_in = False
        try:
            signed_in = bool(self._run("status", timeout=60).get("loggedIn"))
        except ExpertError:
            signed_in = False
        if signed_in:
            with self._lock:  # claimed here, so a check that began meanwhile and this press never both create
                start = not self._pending(binding_id)
                if start:
                    self._sessions[binding_id] = {"step": "creating"}
            if start:
                self._in_background(self._create, tenant, owner, binding_id)
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
        with self._lock:
            self._sessions[binding_id] = {"step": "sign_in", "url": found.group(1).rstrip("."), "code": found.group(2),
                                          "process": process}
        threading.Thread(target=self._finish_sign_in, args=(tenant, owner, binding_id, process), daemon=True).start()
        return self.status(tenant, owner, binding_id)

    def _pending(self, binding_id: str) -> bool:
        """Under the lock: a sign-in, an app creation or a check of Microsoft's report is under way."""
        current = self._sessions.get(binding_id) or {}
        return current.get("step") in {"sign_in", "creating"} or bool(current.get("checking"))

    @staticmethod
    def _in_background(target: Any, *args: Any) -> None:
        threading.Thread(target=target, args=args, daemon=True).start()

    def _recheck_due(self) -> None:
        """While Teams does not allow the app, read Microsoft's report again every two minutes; Connect goes on by
        itself once it does. One check at a time per channel, claimed under the lock."""
        due = []
        with self._lock:
            for binding_id, session in self._sessions.items():
                if session.get("step") == "blocked" and not session.get("checking") \
                        and session.get("check_at", 0.0) <= self._clock():
                    self._sessions[binding_id] = {**session, "checking": True}
                    due.append((session["tenant"], session["owner"], binding_id))
        for tenant, owner, binding_id in due:
            self._in_background(self._create, tenant, owner, binding_id)

    def _finish_sign_in(self, tenant: str, owner: str, binding_id: str, process: Any) -> None:
        """The one place a sign-in moves on: once its process ends, the step changes in one lock section and
        the app is created once, or the error is shown. A newer Connect that replaced this sign-in wins."""
        if process.stdout:
            process.stdout.read()
        process.wait()
        ok = process.returncode == 0
        with self._lock:
            session = self._sessions.get(binding_id) or {}
            if session.get("process") is not process or session.get("step") != "sign_in":
                return
            self._sessions[binding_id] = {"step": "creating"} if ok else {"step": "error", "error": _SIGN_IN_UNFINISHED}
        if ok:
            self._create(tenant, owner, binding_id)

    def _create(self, tenant: str, owner: str, binding_id: str) -> None:
        try:
            binding = self.channels._binding(binding_id, tenant, owner)
            expert = self.channels.experts.latest(tenant, owner, binding["lineage_id"])
            try:
                status = self._run("status", timeout=60)
            except Exception:
                with self._lock:
                    session = self._sessions.get(binding_id) or {}
                    if session.get("checking"):  # a check that could not read the report: nothing exists yet, so
                        self._sessions[binding_id] = {**session, "checking": False,  # it is simply tried again later
                                                      "check_at": self._clock() + _RECHECK_S}
                        return
                raise
            blocked = _custom_apps_blocked(status, expert["name"])
            with self._lock:
                now = self._clock()
                if blocked:
                    # Nothing is created that Teams would not let this account install; the page says why, plainly,
                    # with a note for the administrator, and maintenance reads Microsoft's report again by itself.
                    self._steady[binding_id] = (0, 0.0)
                    self._sessions[binding_id] = {"step": "blocked", "error": blocked,
                                                  "note": _note_for_admin(status, expert["name"]),
                                                  "tenant": tenant, "owner": owner, "check_at": now + _RECHECK_S}
                    return
                if binding_id in self._steady:  # Microsoft said no before: wait until its "yes" is steady
                    count, first = self._steady[binding_id]
                    count, first = count + 1, first if count else now
                    if count < _STEADY_READS or now - first < _STEADY_SPAN_S:
                        self._steady[binding_id] = (count, first)
                        self._sessions[binding_id] = {"step": "blocked", "error": _STILL_APPLYING, "note": "",
                                                      "tenant": tenant, "owner": owner,
                                                      "check_at": now + _STEADY_RECHECK_S}
                        return
                    del self._steady[binding_id]
                # Which account and organisation the app is made in, as Microsoft reports them, shown while it is
                # made and on the card afterwards, so a wrong one is noticed at once.
                account = str(status.get("username") or "")
                self._sessions[binding_id] = {"step": "creating", "account": account,
                                              "tenant_id": str(status.get("tenantId") or "")}
            path = self.credentials_path(binding_id)
            created = self._run("app", "create", "--name", expert["name"][:30], "--messaging-mode", "socket",
                                "--teams-managed", "--env", str(path), timeout=600)
            if path.exists():
                path.chmod(0o600)
            settings = {"app_id": created.get("botId", ""), "teams_app_id": created.get("teamsAppId", ""),
                        "app_name": created.get("appName", expert["name"]), "account": account,
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

    def ensure_package(self, binding_id: str, *, by_owner: bool = True) -> str:
        """Exactly one package process per connected channel, also across runtime restarts. The owner's own
        action starts it at once; maintenance (by_owner=False) waits while it keeps exiting before it held."""
        binding = self.channels._binding(binding_id)
        credentials = self.credentials_path(binding_id)
        pid_path = self._pid_path(binding_id)
        pid = int(pid_path.read_text().strip() or 0) if pid_path.exists() else 0
        running = bool(pid) and self._alive(pid)
        if binding["state"] != "connected" or not credentials.exists():
            if running:
                os.kill(pid, 15)
            pid_path.unlink(missing_ok=True)
            self._forget(binding_id)
            return "off"
        if running:
            return "running"
        if by_owner:
            self._forget(binding_id)
        elif self._backing_off(binding_id, binding):
            return "waiting"
        runtime = os.environ.get("GLASSHIVE_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
        logs = _private(_state_dir() / "logs")
        with open(logs / f"teams-{binding_id[-8:]}.log", "ab") as handle:
            process = subprocess.Popen(
                [str(self._package_python()), "-m", "xperfect_teams", "--runtime", runtime, "--binding", binding_id,
                 "--state-dir", str(_state_dir()), "--credentials", str(credentials)],
                stdout=handle, stderr=subprocess.STDOUT, env=_clean_env(), cwd=str(_state_dir()),
                start_new_session=True)
        self._packages[binding_id] = process
        self._started[binding_id] = datetime.now(UTC).isoformat()
        pid_path.write_text(str(process.pid))
        pid_path.chmod(0o600)
        return "started"

    def _forget(self, binding_id: str) -> None:
        self._packages.pop(binding_id, None)
        self._started.pop(binding_id, None)
        self._restarts.pop(binding_id, None)

    def _backing_off(self, binding_id: str, binding: dict) -> bool:
        """Whether maintenance waits before starting this package again: 5 s after the first exit before
        Microsoft's socket held, doubling to at most 5 minutes; a package that held starts again at once."""
        process = self._packages.pop(binding_id, None)
        if process is not None:
            if process.poll() is None:  # still alive though its pid says otherwise: never start a second one
                self._packages[binding_id] = process
                return True
            started = self._started.pop(binding_id, "")
            seen = binding.get("package_seen_at") or ""
            held = (binding.get("package_socket") == "ready" and bool(seen) and bool(started)
                    and datetime.fromisoformat(seen) >= datetime.fromisoformat(started))
            failures = 0 if held else self._restarts.get(binding_id, (0, 0.0))[0] + 1
            wait = min(_RESTART_FIRST_WAIT_S * 2 ** (failures - 1), _RESTART_MAX_WAIT_S) if failures else 0.0
            self._restarts[binding_id] = (failures, self._clock() + wait)
        return self._clock() < self._restarts.get(binding_id, (0, 0.0))[1]

    def reconcile_once(self) -> list[dict]:
        outcomes = []
        self._recheck_due()
        with self.channels.store._connect() as conn:
            ids = [row[0] for row in conn.execute("SELECT binding_id FROM channel_bindings WHERE kind='teams'")]
        for binding_id in ids:
            try:
                state = self.ensure_package(binding_id, by_owner=False)
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
        self._forget(binding_id)
