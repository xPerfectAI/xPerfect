"""An administrator's one press for Teams: Microsoft's own sign-in and consent, then Microsoft Graph adds the Expert's
app to the organisation's catalogue and installs it for the person.

Microsoft's consent page is the yes. The token and the sign-in's device code live only in the memory of the one setup
that asked for them: nothing here writes them anywhere, and errors are told in plain words, never Microsoft's raw reply.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

LOGIN_URL = "https://login.microsoftonline.com"
GRAPH_URL = "https://graph.microsoft.com/v1.0"
# Graph "Publish teamsApp" (admin consent), "Install app for user" for an app other than the caller (admin consent;
# the Self variant installs only the calling app itself), and the signed-in person's own id.
SCOPES = ("https://graph.microsoft.com/AppCatalog.ReadWrite.All",
          "https://graph.microsoft.com/TeamsAppInstallation.ReadWriteForUser",
          "https://graph.microsoft.com/User.Read")
PUBLISH_PERMISSION = "AppCatalog.ReadWrite.All"
INSTALL_PERMISSION = "TeamsAppInstallation.ReadWriteForUser"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def client_id() -> str:
    """The Microsoft sign-in client the owner chose for this route; none means the route is not offered."""
    return os.environ.get("XPERFECT_TEAMS_ADMIN_CLIENT_ID", "").strip()


def _endpoint(name: str, default: str) -> str:
    """Microsoft's address, or a stand-in on this computer for tests: an override that leaves this computer is
    ignored, so a setting can never send a person's sign-in elsewhere."""
    override = os.environ.get(name, "").strip().rstrip("/")
    if override:
        parsed = urllib.parse.urlparse(override)
        if parsed.scheme in {"http", "https"} and (parsed.hostname or "") in _LOOPBACK:
            return override
    return default


def catalogue_link(catalogue_id: str, tenant_id: str) -> str:
    """Teams' own link to an app in the organisation's catalogue (its catalogue id, not the manifest id)."""
    link = "https://teams.microsoft.com/l/app/" + urllib.parse.quote(catalogue_id, safe="")
    return link + ("?tenantId=" + urllib.parse.quote(tenant_id, safe="") if tenant_id else "")


class AdminRouteError(Exception):
    """kind: declined, expired, not_granted, not_admin, unavailable, failed."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail[:200]


@dataclass
class SignIn:
    """What the person sees (the code and Microsoft's page) and, kept apart, what only this setup may redeem."""
    url: str
    code: str
    expires_at: float
    interval: float
    device_code: str = field(repr=False)


@dataclass
class Token:
    access: str = field(repr=False)
    granted: frozenset[str]
    expires_at: float


Http = Callable[..., tuple[int, bytes]]


def _http(method: str, url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
          timeout: float = 60.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (Microsoft or loopback only)
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdminRouteError("unavailable", type(exc).__name__) from None


def _json(body: bytes) -> dict:
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class Microsoft:
    """Microsoft's sign-in and the four Graph calls the route needs, for one organisation."""

    def __init__(self, tenant_id: str, *, http: Http = _http, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.tenant_id = tenant_id
        self.login = _endpoint("XPERFECT_MICROSOFT_LOGIN_URL", LOGIN_URL) + "/" + urllib.parse.quote(tenant_id, safe="")
        self.graph = _endpoint("XPERFECT_MICROSOFT_GRAPH_URL", GRAPH_URL)
        self._http, self._sleep, self._clock = http, sleep, clock

    # ------------------------------------------------------------------ sign-in
    def start_sign_in(self) -> SignIn:
        status, body = self._http("POST", self.login + "/oauth2/v2.0/devicecode",
                                  data=urllib.parse.urlencode({"client_id": client_id(), "scope": " ".join(SCOPES)}).encode(),
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        value = _json(body)
        if status != 200 or not value.get("device_code") or not value.get("user_code"):
            raise AdminRouteError("unavailable", str(value.get("error") or status))
        return SignIn(url=str(value.get("verification_uri") or "https://microsoft.com/devicelogin"),
                      code=str(value["user_code"]), expires_at=self._clock() + float(value.get("expires_in") or 900),
                      interval=max(1.0, float(value.get("interval") or 5)), device_code=str(value["device_code"]))

    def wait_for_token(self, sign_in: SignIn, still_wanted: Callable[[], bool]) -> Token:
        """Microsoft's answer to the code: the token once the person signed in and consented, or why not."""
        interval = sign_in.interval
        form = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                       "client_id": client_id(), "device_code": sign_in.device_code}).encode()
        while self._clock() < sign_in.expires_at:
            if not still_wanted():
                raise AdminRouteError("declined", "replaced")
            self._sleep(interval)
            status, body = self._http("POST", self.login + "/oauth2/v2.0/token", data=form,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            value = _json(body)
            if status == 200 and value.get("access_token"):
                granted = frozenset(part.rsplit("/", 1)[-1] for part in str(value.get("scope") or "").split())
                return Token(access=str(value["access_token"]), granted=granted,
                             expires_at=self._clock() + float(value.get("expires_in") or 3600))
            error = str(value.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error in {"authorization_declined", "access_denied"}:
                raise AdminRouteError("declined", error)
            if error in {"expired_token", "code_expired"}:
                raise AdminRouteError("expired", error)
            codes = {int(code) for code in value.get("error_codes") or [] if str(code).isdigit()}
            if codes & {65001, 65004, 90094, 90008}:  # consent missing, declined or held for an administrator
                raise AdminRouteError("not_granted", error)
            raise AdminRouteError("failed", error or str(status))
        raise AdminRouteError("expired", "deadline")

    # ------------------------------------------------------------------ Graph
    def _graph(self, token: Token, method: str, path: str, *, data: bytes | None = None,
               content_type: str = "application/json") -> tuple[int, dict]:
        headers = {"Authorization": "Bearer " + token.access}
        if data is not None:
            headers["Content-Type"] = content_type
        status, body = self._http(method, self.graph + path, data=data, headers=headers)
        return status, _json(body)

    def me(self, token: Token) -> dict:
        status, value = self._graph(token, "GET", "/me?$select=id,userPrincipalName")
        if status != 200 or not value.get("id"):
            raise AdminRouteError("failed", f"me {status}")
        return {"id": str(value["id"]), "account": str(value.get("userPrincipalName") or "")}

    def find(self, token: Token, manifest_id: str) -> str:
        """The organisation's catalogue entry for this manifest id, if an administrator already added it."""
        query = urllib.parse.quote(f"externalId eq '{manifest_id}'", safe="")
        status, value = self._graph(token, "GET", f"/appCatalogs/teamsApps?$filter={query}")
        if status != 200:
            raise AdminRouteError("not_admin" if status in {401, 403} else "failed", f"find {status}")
        for app in value.get("value") or []:
            if isinstance(app, dict) and app.get("id"):
                return str(app["id"])
        return ""

    def publish(self, token: Token, package: bytes, manifest_id: str) -> str:
        """Graph "Publish teamsApp" as an administrator: in the catalogue at once, no review."""
        status, value = self._graph(token, "POST", "/appCatalogs/teamsApps", data=package, content_type="application/zip")
        if status in {200, 201} and value.get("id"):
            return str(value["id"])
        if status == 409:  # added meanwhile, by this press or another administrator
            found = self.find(token, manifest_id)
            if found:
                return found
        if status in {401, 403}:
            raise AdminRouteError("not_admin", f"publish {status}")
        raise AdminRouteError("failed", f"publish {status}")

    def install(self, token: Token, user_id: str, catalogue_id: str) -> str:
        """Graph "Install app for user": installed, already, or refused (Microsoft may still be spreading the app)."""
        body = json.dumps({"teamsApp@odata.bind": f"{GRAPH_URL}/appCatalogs/teamsApps/{catalogue_id}"}).encode()
        status, _value = self._graph(token, "POST", f"/users/{urllib.parse.quote(user_id, safe='')}/teamwork/installedApps",
                                     data=body)
        if status in {200, 201, 204}:
            return "installed"
        if status == 409:
            return "already"
        return "refused"

    def install_for(self, token: Token, user_id: str, catalogue_id: str, still_wanted: Callable[[], bool], *,
                    every: float = 20.0, within: float = 600.0) -> bool:
        """Install, trying again while Microsoft spreads a new catalogue entry: within the token's life and at most
        `within` seconds; False means the person adds it from the link instead."""
        deadline = min(self._clock() + within, token.expires_at - 60)
        while True:
            if self.install(token, user_id, catalogue_id) in {"installed", "already"}:
                return True
            if self._clock() + every > deadline or not still_wanted():
                return False
            self._sleep(every)


def plain(error: AdminRouteError, name: str) -> str:
    """What the card says, in plain words; Microsoft's raw reply is never shown."""
    return {
        "declined": "Microsoft's sign-in was declined or replaced, so nothing more was done.",
        "expired": "Microsoft's code expired before the sign-in finished, so nothing more was done. Press the button "
                   "again for a new code.",
        "not_granted": "Microsoft did not give this sign-in the right to add apps to your organisation's Teams apps. "
                       "That needs a Teams administrator. Nothing more was done; you can send the note to your "
                       "administrator.",
        "not_admin": f"Microsoft refused to add {name} to your organisation's Teams apps for this account: that needs a "
                     f"Teams administrator. {name}'s Teams app is made and waits for it. An administrator can press "
                     f"the button again, or add it in the admin center.",
        "unavailable": "Microsoft could not be reached. Nothing more was changed; press the button again to go on.",
    }.get(error.kind, "Microsoft stopped this setup. Nothing more was changed; press the button again to go on.")
