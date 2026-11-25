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
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import teams_admin
from . import expert_pictures
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
# While a change spreads, Microsoft's report flips between yes, no and no answer (for more than an hour in the first
# real test, even after fourteen yes in a row). It is a hint, so nobody waits for it to settle: after a clear "no",
# the first clear yes for both switches creates the app, and the card says Microsoft may still be applying it.
_NOT_REPORTED = ("Microsoft did not say this time whether Teams allows the app here. This page checks again by itself "
                 "and goes on once Microsoft clearly says yes.")


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
    off = ("both the person's switch and the organisation's are" if tenant is False and user is False
           else "the person's own switch is" if user is False else "the organisation's switches are")
    return (f"Teams does not yet let the account you signed in with add custom apps, so {name} cannot be installed. "
            "An administrator turns them on in the Teams admin center: \"Upload custom apps\" in the person's app setup "
            "policy (Teams apps → Setup policies; a new policy can give it to this one person only), and for the whole "
            "organisation, under Teams apps → Manage apps → Actions → Org-wide app settings → Custom apps, \"Let users "
            "install and use available apps by default\" and \"Let users interact with custom apps in preview\". "
            f"Microsoft reports that {off} off. This page checks again by itself every two minutes.")


_DETAIL_FLAGS = (("developer", "--developer"), ("website", "--website"), ("privacy", "--privacy-url"),
                 ("terms", "--terms-url"))


def _detail_problem(values: dict[str, str]) -> str:
    """Microsoft's rules for the app's public details: a developer name of 1 to 32 characters and HTTPS links with a
    domain (Teams requires all four; the pinned tool checks the links the same way)."""
    if not 1 <= len(values["developer"]) <= 32:
        return "The developer name needs 1 to 32 characters."
    for key, label in (("website", "website"), ("privacy", "privacy policy"), ("terms", "terms of use")):
        parsed = urlparse(values[key])
        if parsed.scheme != "https" or "." not in parsed.netloc or len(values[key]) > 2000:
            return f"The {label} link must start with https:// and include a domain."
    return ""


def _account_changed(before: str, now: str) -> str:
    if not now:
        return f"The Teams tool is no longer signed in as {before}, so nothing was made. Press Connect again to sign in."
    return (f"The Teams tool is now signed in as {now}, not {before}, so nothing was made. Press Connect again: it "
            "names the account signed in now.")


def _custom_apps_allowed(status: dict) -> bool:
    """Microsoft's explicit yes for both switches. After a "no", anything less (a missing or partial report, as from a
    signed-out tool or a portal that did not answer) is not a yes."""
    sideloading = _find(status, "sideloading")
    return isinstance(sideloading, dict) and sideloading.get("tenant") is True and sideloading.get("user") is True


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
                    "install and use available apps by default\" and \"Let users interact with custom apps in preview\".")
    steps = "\n".join(f"{number}. {ask}" for number, ask in enumerate(asks, 1))
    account = str(status.get("username") or "").strip()
    return (f"Hello, could you let me add a Teams app I set up for myself, {name}? In the Teams admin center:\n{steps}\n"
            + (f"My account: {account}.\n" if account else "")
            + "If the admin center says it is still setting up, that can take up to 30 minutes, and it can take an hour "
            "or more before Teams lets me add the app. Thank you!")


class TeamsSetup:
    """One sign-in and app creation at a time per channel, and one package process per channel."""

    def __init__(self, channels: Any) -> None:
        self.channels = channels
        self._sessions: dict[str, dict] = {}
        self._packages: dict[str, subprocess.Popen] = {}
        self._started: dict[str, str] = {}  # when each package started here began
        self._restarts: dict[str, tuple[int, float]] = {}  # exits before the socket held, and when to try again
        self._refused: set[str] = set()  # channels for which Microsoft clearly said no: only a clear yes goes on
        self._signed_in_cache: tuple[float, dict] = (0.0, {})  # who the tool is signed in as, and when it was read
        self._clock = time.monotonic
        self._microsoft = teams_admin.Microsoft  # the administrator's route: Microsoft's sign-in and Graph
        self._lock = threading.Lock()
        self._package_lock = threading.Lock()  # starting or stopping a package process: one at a time

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

    def _cli_present(self) -> bool:
        """Whether the pinned tool is already on this computer (a read must never install it)."""
        configured = os.environ.get("XPERFECT_TEAMS_CLI", "").strip()
        local = _state_dir() / "tools" / "teams-cli" / "node_modules" / ".bin" / "teams"
        return bool((configured and Path(configured).exists()) or shutil.which("teams") or local.exists())

    def _signed_in_as(self) -> dict:
        """Who the pinned tool is signed in as, for the Connect button: read at most every 30 s, and only when the
        tool is already here; {} when it is not signed in or cannot say."""
        at, who = self._signed_in_cache
        if at and self._clock() - at < 30.0:
            return who
        who = {}
        if self._cli_present():
            try:
                status = self._run("status", timeout=60)
                if status.get("loggedIn"):
                    who = {"account": str(status.get("username") or ""), "tenant_id": str(status.get("tenantId") or "")}
            except (ExpertError, OSError, subprocess.SubprocessError):
                who = {}
        self._signed_in_cache = (self._clock(), who)
        return who

    # ------------------------------------------------------------------ connect
    def status(self, tenant: str, owner: str, binding_id: str) -> dict:
        binding = self.channels._binding(binding_id, tenant, owner)
        with self._lock:
            session = dict(self._sessions.get(binding_id) or {})
        if not session:
            view = self.channels.view(binding)
            if view["app"].get("app_id"):
                return {"step": "connected", "channel": view}
            # Who Connect would make the app as, so its button can say so before anything is made.
            return {"step": "start", "signed_in_as": self._signed_in_as(), "channel": view}
        if session.get("step") == "sign_in" and session["process"].poll() is not None:
            # The sign-in has ended. Reading never moves anything on (the thread watching the sign-in does, once);
            # it only stops showing a code that can no longer be used.
            ok = session["process"].returncode == 0
            session = {**session, "step": "creating" if ok else "error", **({} if ok else {"error": _SIGN_IN_UNFINISHED})}
        if session.get("step") == "admin_sign_in" and session.get("expires_at", 0.0) <= self._clock():
            session = {**session, "step": "blocked", "error": teams_admin.plain(teams_admin.AdminRouteError("expired"), "")}
        # A code is shown only while it can still be used. The administrator's press is offered while Teams refuses
        # the app, once the owner has chosen the Microsoft sign-in it uses.
        keys = {"sign_in": ("step", "url", "code", "error"), "blocked": ("step", "error", "note"),
                "creating": ("step", "account", "tenant_id"), "admin_sign_in": ("step", "url", "code", "account", "tenant_id"),
                "adding": ("step", "account", "progress"),
                "admin_waiting": ("step", "error", "note")}.get(session.get("step"), ("step", "error"))
        public = {key: session[key] for key in keys if key in session}
        if public.get("step") in {"blocked", "admin_waiting"}:
            public["admin"] = bool(teams_admin.client_id())
        return {**public, "channel": self.channels.view(self.channels._binding(binding_id))}

    def connect(self, tenant: str, owner: str, binding_id: str, *, as_account: str = "",
                other_account: bool = False, as_admin: bool = False) -> dict:
        binding = self.channels._binding(binding_id, tenant, owner)
        if binding["kind"] != "teams":
            raise ExpertError("This channel is not available yet", code="channel_kind_unavailable")
        with self._lock:
            pending = self._pending(binding_id)
            current = dict(self._sessions.get(binding_id) or {})
        if pending:  # a second press shows the sign-in already under way (status takes the lock itself)
            return self.status(tenant, owner, binding_id)
        if as_admin:
            return self._start_admin(tenant, owner, binding_id)
        status: dict = {}
        if other_account:  # the tool forgets the account it holds; Microsoft's code sign-in below takes another
            self._sign_out()
        else:
            try:
                status = self._run("status", timeout=60)
            except ExpertError:
                status = {}
        signed_in, account = bool(status.get("loggedIn")), str(status.get("username") or "")
        who = {"account": account, "tenant_id": str(status.get("tenantId") or "")} if signed_in else {}
        self._signed_in_cache = (self._clock(), who)
        # Already signed in: the Connect button names the account and organisation, and pressing it is the yes (a
        # computer can hold another organisation's login). A press without that name, or for an account that is no
        # longer the one signed in, makes nothing and shows the account now signed in. A press after Microsoft
        # refused custom apps checks again for the account already shown.
        if signed_in and current.get("step") != "blocked" and as_account != account:
            with self._lock:
                if not self._pending(binding_id):
                    self._sessions.pop(binding_id, None)  # an old error or code no longer applies
            return {"step": "start", "signed_in_as": who,
                    "channel": self.channels.view(self.channels._binding(binding_id))}
        if signed_in:
            # The app is made only as the account the press expects: the one the button named, or the one a
            # blocked channel was refused for (the tool's login is shared by the whole computer).
            expected = current.get("account") if current.get("step") == "blocked" else as_account
            with self._lock:  # claimed here, so a check that began meanwhile and this press never both create
                start = not self._pending(binding_id)
                if start:
                    self._sessions[binding_id] = {"step": "creating"}
            if start:
                self._in_background(self._create, tenant, owner, binding_id, expected)
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

    def set_details(self, tenant: str, owner: str, binding_id: str, details: dict) -> dict:
        """The owner's own public details for the Teams app (Teams shows them on the app's page) instead of the
        tool's placeholders: checked first, then applied with the tool's own `app update`, then kept."""
        binding = self.channels._binding(binding_id, tenant, owner)
        settings = json.loads(binding["settings_json"] or "{}")
        app_id = str(settings.get("teams_app_id") or "")
        if not app_id:
            raise ExpertError("Connect the channel first: its Teams app does not exist yet.", code="channel_not_connected")
        values = {key: str(details.get(key) or "").strip() for key, _ in _DETAIL_FLAGS}
        problem = _detail_problem(values)
        if problem:
            raise ExpertError(problem, code="channel_details_invalid")
        flags = [part for key, flag in _DETAIL_FLAGS for part in (flag, values[key])]
        self._run("app", "update", app_id, *flags, timeout=120)
        return self.channels.record_setup(tenant, owner, binding_id, {
            **settings, "developer": values["developer"], "website_url": values["website"],
            "privacy_url": values["privacy"], "terms_url": values["terms"]})

    def _sign_out(self) -> None:
        """The pinned tool forgets the Microsoft account it is signed in with (its `logout` takes no --json)."""
        try:
            subprocess.run([*self._cli(), "logout", "--disable-auto-update"], capture_output=True, text=True,
                           timeout=60, env=_clean_env({"TEAMS_NO_INTERACTIVE": "1"}))
        except (OSError, subprocess.TimeoutExpired):
            pass  # the code sign-in that follows shows Microsoft's page either way

    def _pending(self, binding_id: str) -> bool:
        """Under the lock: a sign-in, an app creation or a check of Microsoft's report is under way."""
        current = self._sessions.get(binding_id) or {}
        if current.get("step") == "admin_sign_in" and current.get("expires_at", 0.0) <= self._clock():
            return False  # its code can no longer be used: a new press may start again
        return current.get("step") in {"sign_in", "creating", "admin_sign_in", "adding"} or bool(current.get("checking"))

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
                    due.append((session["tenant"], session["owner"], binding_id, session.get("account")))
        for tenant, owner, binding_id, account in due:
            self._in_background(self._create, tenant, owner, binding_id, account)

    def _finish_sign_in(self, tenant: str, owner: str, binding_id: str, process: Any) -> None:
        """The one place a sign-in moves on: once its process ends, the step changes in one lock section and
        the app is created once, or the error is shown. A newer Connect that replaced this sign-in wins."""
        if process.stdout:
            process.stdout.read()
        process.wait()
        ok = process.returncode == 0
        self._signed_in_cache = (0.0, {})  # a new sign-in: the next read asks the tool again
        with self._lock:
            session = self._sessions.get(binding_id) or {}
            if session.get("process") is not process or session.get("step") != "sign_in":
                return
            self._sessions[binding_id] = {"step": "creating"} if ok else {"step": "error", "error": _SIGN_IN_UNFINISHED}
        if ok:
            self._create(tenant, owner, binding_id)

    def _create(self, tenant: str, owner: str, binding_id: str, expected_account: str | None = None) -> None:
        """Make the channel's Teams app once Microsoft allows it. expected_account: the account the person chose
        (a named press, or the one a blocked channel was refused for); None after a fresh code sign-in."""
        try:
            binding = self.channels._binding(binding_id, tenant, owner)
            if json.loads(binding["settings_json"] or "{}").get("teams_app_id"):
                with self._lock:  # the app exists already (the administrator's route made it): never a second one
                    self._sessions[binding_id] = {"step": "connected"}
                return
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
            account = str(status.get("username") or "") if status.get("loggedIn") else ""
            if expected_account is not None and account != expected_account:
                # The tool's login changed since the person chose (another Expert signed in elsewhere, or a sign-out):
                # nothing is made in an organisation nobody chose; Connect names the account signed in now.
                with self._lock:
                    self._refused.discard(binding_id)
                    self._sessions[binding_id] = {"step": "error", "error": _account_changed(expected_account, account)}
                return
            blocked = _custom_apps_blocked(status, expert["name"])
            with self._lock:
                now = self._clock()
                if blocked:
                    # Nothing is created that Teams would not let this account install; the page says why, plainly,
                    # with a note for the administrator, and maintenance reads Microsoft's report again by itself.
                    self._refused.add(binding_id)
                    self._sessions[binding_id] = {"step": "blocked", "error": blocked,
                                                  "note": _note_for_admin(status, expert["name"]), "account": account,
                                                  "tenant": tenant, "owner": owner, "check_at": now + _RECHECK_S}
                    return
                if binding_id in self._refused:  # Microsoft said no before: only a clear yes for both switches
                    if not _custom_apps_allowed(status):  # a missing or half answer is not a yes: nothing created
                        previous = self._sessions.get(binding_id) or {}
                        self._sessions[binding_id] = {"step": "blocked", "error": _NOT_REPORTED,
                                                      "note": previous.get("note", ""), "account": account, "tenant": tenant,
                                                      "owner": owner, "check_at": now + _RECHECK_S}
                        return
                    self._refused.discard(binding_id)
                # Which account and organisation the app is made in, as Microsoft reports them, shown while it is
                # made and on the card afterwards, so a wrong one is noticed at once.
                self._sessions[binding_id] = {"step": "creating", "account": account,
                                              "tenant_id": str(status.get("tenantId") or "")}
            settings, created = self._make_app(tenant, owner, binding_id, expert, status)
            note = {} if settings["mode"] == "socket" else {
                "error": "Teams did not turn on socket mode for this app. " + str(created.get("socketModeError") or "")}
            with self._lock:
                self._sessions[binding_id] = {"step": "connected" if not note else "error", **note}
            self.ensure_package(binding_id)
        except Exception as exc:
            with self._lock:
                self._sessions[binding_id] = {"step": "error", "error": str(exc)[:400]}

    def _make_app(self, tenant: str, owner: str, binding_id: str, expert: dict, status: dict,
                  extra: dict | None = None) -> tuple[dict, dict]:
        """Make the channel's Teams app with the tool, narrow it to personal chats, record it and list the signed-in
        owner. extra: settings recorded with it in the same write (the administrator's route marks it "not yet in the
        catalogue", so a later press resumes there and never makes a second app)."""
        account = str(status.get("username") or "") if status.get("loggedIn") else ""
        path = self.credentials_path(binding_id)
        created = self._run("app", "create", "--name", expert["name"][:30], "--messaging-mode", "socket",
                            "--teams-managed", "--env", str(path), timeout=600)
        if path.exists():
            path.chmod(0o600)
        # The tool's manifest offers the bot in personal chats, teams and group chats; the Expert answers only in
        # personal chats, so Teams is told the same. The app exists now: a failure here is said on the card, never
        # retried by making another app.
        scopes = "personal,team,groupChat"
        if created.get("teamsAppId"):
            try:
                self._run("app", "update", str(created["teamsAppId"]), "--scopes", "personal", timeout=120)
                scopes = "personal"
            except ExpertError:
                pass
        settings = {"app_id": created.get("botId", ""), "teams_app_id": created.get("teamsAppId", ""),
                    "app_name": created.get("appName", expert["name"]), "account": account,
                    "connected_by": str(status.get("userObjectId") or ""),
                    "tenant_id": str(status.get("tenantId") or ""),
                    "mode": "socket" if created.get("socketMode") else "http",
                    "install_link": created.get("installLink", ""), "scopes": scopes, **(extra or {})}
        self.channels.record_setup(tenant, owner, binding_id, settings)
        # The signed-in owner is the first person who may message the Expert.
        people = self.channels.view(self.channels._binding(binding_id))["people"]
        me = {"id": str(status.get("userObjectId") or ""), "name": str(status.get("username") or "You"),
              "tenant": str(status.get("tenantId") or "")}
        if me["id"] and all(person["id"] != me["id"] for person in people):
            people.append(me)
        self.channels.update(tenant, owner, binding_id, people=people,
                             state="connected" if settings["mode"] == "socket" else None)
        # The Expert's own picture becomes the app's colour icon; a failure is said on the card, never fatal.
        try:
            self.apply_picture(tenant, owner, binding_id)
        except Exception:
            pass
        return settings, created

    def apply_picture(self, tenant: str, owner: str, binding_id: str) -> dict:
        """The Expert's picture as its Teams app's colour icon, with the tool's own `app update`. Once per picture;
        a failure is kept for the card and never retried by itself."""
        binding = self.channels._binding(binding_id, tenant, owner)
        settings = json.loads(binding["settings_json"] or "{}")
        app_id = str(settings.get("teams_app_id") or "")
        data = expert_pictures.load(binding["lineage_id"])
        if not app_id or data is None:
            return {"applied": False, "reason": "no_app" if not app_id else "no_picture"}
        digest = expert_pictures.digest(data)
        if settings.get("picture_digest") == digest:
            return {"applied": True, "unchanged": True}
        path = _private(_state_dir() / "tmp") / f"picture-{binding_id}.png"
        path.write_bytes(data)
        path.chmod(0o600)
        try:
            self._run("app", "update", app_id, "--color-icon", str(path), timeout=120)
            settings.update({"picture_digest": digest, "picture_error": ""})
            result = {"applied": True}
        except ExpertError as exc:
            settings.update({"picture_digest": "", "picture_error": str(exc)[:300]})
            result = {"applied": False, "error": str(exc)[:300]}
        finally:
            path.unlink(missing_ok=True)
        self.channels.record_setup(tenant, owner, binding_id, settings)
        return result

    # ------------------------------------------------------------------ the administrator's route
    def _start_admin(self, tenant: str, owner: str, binding_id: str) -> dict:
        """One press for a Teams administrator: Microsoft's code sign-in and consent page (the yes), then by itself
        the app is made if it is not, added to the organisation's catalogue and installed for the person."""
        if not teams_admin.client_id():
            raise ExpertError("This xPerfect has no Microsoft sign-in for administrators yet, so this route is not "
                              "offered. Send the note to your administrator.", code="channel_admin_unavailable")
        try:
            status = self._run("status", timeout=60)
        except ExpertError:
            status = {}
        if not status.get("loggedIn"):
            raise ExpertError("The Teams tool is not signed in. Press Connect first.", code="channel_admin_needs_connect")
        account, tenant_id = str(status.get("username") or ""), str(status.get("tenantId") or "")
        # The tool's sign-in is shared by the whole computer: the press goes on only as the account the channel was
        # refused for, or the one its app was made with, in that app's organisation. Nothing reaches Microsoft else.
        with self._lock:
            current = dict(self._sessions.get(binding_id) or {})
        settings = json.loads(self.channels._binding(binding_id)["settings_json"] or "{}")
        made_with = str(settings.get("account") or "") if settings.get("teams_app_id") else ""
        expected = made_with or (str(current.get("account") or "") if current.get("step") == "blocked" else "")
        expected_tenant = str(settings.get("tenant_id") or "") if made_with else ""
        if (expected and account != expected) or (expected_tenant and tenant_id != expected_tenant):
            line = (f"The Teams tool is now signed in as {account or 'nobody'}, not {expected}, which made this channel's "
                    f"app, so nothing was made. Press the button again once the tool is signed in as {expected}."
                    if made_with else _account_changed(expected, account))
            with self._lock:
                self._sessions[binding_id] = {"step": "error", "error": line}
            return self.status(tenant, owner, binding_id)
        microsoft = self._microsoft(tenant_id)
        try:
            sign_in = microsoft.start_sign_in()
        except teams_admin.AdminRouteError as exc:
            raise ExpertError(teams_admin.plain(exc, ""), code="channel_admin_sign_in_unavailable") from None
        run = uuid.uuid4().hex
        with self._lock:
            if self._pending(binding_id):  # two presses at once: the first one's code is shown
                return self.status(tenant, owner, binding_id)
            previous = dict(self._sessions.get(binding_id) or {})
            self._sessions[binding_id] = {"step": "admin_sign_in", "url": sign_in.url, "code": sign_in.code,
                                          "account": account, "tenant_id": tenant_id,
                                          "expires_at": sign_in.expires_at, "run": run,
                                          "note": previous.get("note", "")}
        self._in_background(self._admin_route, tenant, owner, binding_id, run, account, microsoft, sign_in)
        return self.status(tenant, owner, binding_id)

    def _current(self, binding_id: str, run: str) -> bool:
        with self._lock:
            return (self._sessions.get(binding_id) or {}).get("run") == run

    def _admin_step(self, binding_id: str, run: str, session: dict) -> None:
        """A step of this press, written only while this press is still the channel's current one."""
        with self._lock:
            current = self._sessions.get(binding_id) or {}
            if current.get("run") == run:
                self._sessions[binding_id] = {**session, "run": run, "note": current.get("note", "")}

    def _admin_route(self, tenant: str, owner: str, binding_id: str, run: str, account: str,
                     microsoft: teams_admin.Microsoft, sign_in: teams_admin.SignIn) -> None:
        name = ""
        token = None
        try:
            binding = self.channels._binding(binding_id, tenant, owner)
            expert = self.channels.experts.latest(tenant, owner, binding["lineage_id"])
            name = expert["name"]
            token = microsoft.wait_for_token(sign_in, lambda: self._current(binding_id, run))
            if teams_admin.PUBLISH_PERMISSION not in token.granted:
                raise teams_admin.AdminRouteError("not_granted", "scope")  # checked before anything is made
            self._admin_step(binding_id, run, {"step": "adding", "account": account, "progress": "Checking the account…"})
            person = microsoft.me(token)
            status = self._run("status", timeout=60)
            now = str(status.get("username") or "") if status.get("loggedIn") else ""
            if now != account:  # the tool's login changed meanwhile: nothing is made in an organisation nobody chose
                with self._lock:
                    if (self._sessions.get(binding_id) or {}).get("run") == run:
                        self._sessions[binding_id] = {"step": "error", "error": _account_changed(account, now)}
                return
            settings = json.loads(self.channels._binding(binding_id)["settings_json"] or "{}")
            if not settings.get("teams_app_id"):
                self._admin_step(binding_id, run, {"step": "adding", "account": account,
                                                   "progress": f"Making {name}'s Teams app…"})
                # Made by this press while Teams refuses the app: marked waiting for the catalogue, so the card hides
                # the blocked developer link and offers the press again. An app made the usual way keeps its card as
                # it is until it is in the catalogue.
                settings, _created = self._make_app(tenant, owner, binding_id, expert, status,
                                                    {"catalogue": "pending"})
            manifest_id = str(settings["teams_app_id"])
            self._admin_step(binding_id, run, {"step": "adding", "account": account,
                                               "progress": f"Adding {name} to your organisation's Teams apps…"})
            catalogue_id = str(settings.get("catalogue_id") or "") or microsoft.find(token, manifest_id)
            if not catalogue_id:
                catalogue_id = microsoft.publish(token, self._download_package(manifest_id), manifest_id)
            link = teams_admin.catalogue_link(catalogue_id, str(settings.get("tenant_id") or microsoft.tenant_id))
            settings = {**settings, "catalogue": "listed", "catalogue_id": catalogue_id, "install_link": link}
            self.channels.record_setup(tenant, owner, binding_id, settings)
            # Installing for oneself only: the account that signed in here must be the one the app was made with
            # (the administrator can be another account; then the person adds it from the catalogue link).
            if person["id"] and person["id"] == str(settings.get("connected_by") or ""):
                self._admin_step(binding_id, run, {"step": "adding", "account": account,
                                                   "progress": f"Installing {name} in your Teams…"})
                if microsoft.install_for(token, person["id"], catalogue_id, lambda: self._current(binding_id, run)):
                    settings = {**settings, "catalogue": "installed"}
                    self.channels.record_setup(tenant, owner, binding_id, settings)
            with self._lock:
                if (self._sessions.get(binding_id) or {}).get("run") == run:
                    self._sessions[binding_id] = {"step": "connected"}
            self.ensure_package(binding_id)
        except teams_admin.AdminRouteError as exc:
            made = bool(json.loads(self.channels._binding(binding_id)["settings_json"] or "{}").get("teams_app_id"))
            with self._lock:
                current = self._sessions.get(binding_id) or {}
                if current.get("run") == run:
                    line = teams_admin.plain(exc, name or "The Expert")
                    # Nothing made yet: the channel stays blocked and Microsoft's report is read again by itself.
                    # Made but not in the catalogue: it waits there, and a press resumes at the catalogue.
                    self._sessions[binding_id] = (
                        {"step": "admin_waiting", "error": line, "note": current.get("note", "")} if made else
                        {"step": "blocked", "error": line, "note": current.get("note", ""), "account": account,
                         "tenant": tenant, "owner": owner, "check_at": self._clock() + _RECHECK_S})
        except ExpertError as exc:  # the Teams tool's own plain line, as Connect shows it
            with self._lock:
                if (self._sessions.get(binding_id) or {}).get("run") == run:
                    self._sessions[binding_id] = {"step": "error", "error": str(exc)[:400]}
        except Exception as exc:  # never Microsoft's raw words or a token on the card: the error's kind only
            with self._lock:
                if (self._sessions.get(binding_id) or {}).get("run") == run:
                    self._sessions[binding_id] = {"step": "error", "error": "This setup stopped "
                                                  f"({type(exc).__name__}). Press the button again to go on."}
        finally:
            token = None  # noqa: F841 (the token is dropped with this press)

    def _download_package(self, manifest_id: str) -> bytes:
        """The app's package from the tool (its download command takes no --json), read, then removed."""
        folder = _private(_state_dir() / "tmp")
        path = folder / f"teams-package-{uuid.uuid4().hex}.zip"
        try:
            completed = subprocess.run([*self._cli(), "app", "package", "download", manifest_id, "--output", str(path),
                                        "--disable-auto-update"], capture_output=True, text=True, timeout=180,
                                       env=_clean_env({"TEAMS_NO_INTERACTIVE": "1"}))
            if completed.returncode != 0 or not path.exists():
                raise ExpertError("Microsoft's Teams tool could not download the app's package.",
                                  code="channel_package_download_failed")
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

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
        with self._package_lock:  # the scheduler and a Connect never both start one: two sockets for one bot
            return self._ensure_package(binding_id, by_owner=by_owner)

    def _ensure_package(self, binding_id: str, *, by_owner: bool) -> str:
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
        with self._package_lock:
            pid_path = self._pid_path(binding_id)
            pid = int(pid_path.read_text().strip() or 0) if pid_path.exists() else 0
            if pid and self._alive(pid):
                os.kill(pid, 15)
            pid_path.unlink(missing_ok=True)
            self._forget(binding_id)
