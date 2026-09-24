from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SchedulingOwnerError(RuntimeError):
    """Public-safe failure from the authoritative recurrence owner boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "scheduling_owner_unavailable",
        status_code: int = 503,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class SchedulingOwnerIdentity:
    tenant_id: str
    owner_id: str
    agent_id: str = "scheduling-cortex"


OwnerRequest = Callable[[str, dict[str, str], bytes, float], tuple[int, dict[str, object]]]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())
SCHEDULING_CORTEX_ASSERTION_HEADER = "X-Viventium-Scheduler-Assertion"
SCHEDULING_CORTEX_ASSERTION_ISSUER = "viventium:scheduling-cortex"
SCHEDULING_CORTEX_ASSERTION_AUDIENCE = "glasshive:workspace-run"
SCHEDULING_CORTEX_ASSERTION_SCOPE = "workspace:run"
SCHEDULING_CORTEX_ASSERTION_MAX_TTL_SECONDS = 120


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padded = value + ("=" * (-len(value) % 4))
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _workspace_assertion_subject(request_payload: dict[str, Any]) -> dict[str, str]:
    instruction = str(request_payload.get("instruction") or "")
    bootstrap_bundle = request_payload.get("bootstrap_bundle")
    canonical_bundle = json.dumps(
        bootstrap_bundle if isinstance(bootstrap_bundle, dict) else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return {
        "occurrence_id": str(request_payload.get("occurrence_id") or ""),
        "task_id": str(request_payload.get("task_id") or ""),
        "tenant_id": str(request_payload.get("tenant_id") or ""),
        "owner_id": str(request_payload.get("owner_id") or ""),
        "project_id": str(request_payload.get("project_id") or ""),
        "worker_id": str(request_payload.get("worker_id") or ""),
        "execution_mode": str(request_payload.get("execution_mode") or ""),
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "bootstrap_bundle_sha256": hashlib.sha256(canonical_bundle.encode("utf-8")).hexdigest(),
    }


def mint_scheduling_cortex_workspace_assertion(
    *,
    secret: str,
    request_payload: dict[str, Any],
    issued_at: int | None = None,
    ttl_seconds: int = 90,
) -> str:
    """Mint a fresh, short-lived assertion bound to one delegated occurrence request."""

    normalized_secret = str(secret or "").strip()
    if not normalized_secret:
        raise SchedulingOwnerError("Viventium scheduler assertion secret is unavailable")
    issued = int(time.time()) if issued_at is None else int(issued_at)
    ttl = max(30, min(int(ttl_seconds), SCHEDULING_CORTEX_ASSERTION_MAX_TTL_SECONDS))
    claims: dict[str, Any] = {
        "v": 1,
        "iss": SCHEDULING_CORTEX_ASSERTION_ISSUER,
        "aud": SCHEDULING_CORTEX_ASSERTION_AUDIENCE,
        "scope": SCHEDULING_CORTEX_ASSERTION_SCOPE,
        "iat": issued,
        "exp": issued + ttl,
        "jti": secrets.token_urlsafe(18),
        **_workspace_assertion_subject(request_payload),
    }
    encoded_claims = _base64url_encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    signature = hmac.new(
        normalized_secret.encode("utf-8"),
        encoded_claims.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_claims}.{_base64url_encode(signature)}"


def verify_scheduling_cortex_workspace_assertion(
    assertion: str,
    *,
    secret: str,
    request_payload: dict[str, Any],
    now: int | None = None,
) -> bool:
    """Verify signature, lifetime, audience, and the exact delegated request binding."""

    normalized_secret = str(secret or "").strip()
    token = str(assertion or "").strip()
    if not normalized_secret or not token or len(token) > 8192 or token.count(".") != 1:
        return False
    encoded_claims, encoded_signature = token.split(".", 1)
    try:
        provided_signature = _base64url_decode(encoded_signature)
        expected_signature = hmac.new(
            normalized_secret.encode("utf-8"),
            encoded_claims.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(provided_signature, expected_signature):
            return False
        claims = json.loads(_base64url_decode(encoded_claims).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(claims, dict):
        return False
    current = int(time.time()) if now is None else int(now)
    try:
        issued = int(claims.get("iat"))
        expires = int(claims.get("exp"))
    except (TypeError, ValueError):
        return False
    if (
        claims.get("v") != 1
        or claims.get("iss") != SCHEDULING_CORTEX_ASSERTION_ISSUER
        or claims.get("aud") != SCHEDULING_CORTEX_ASSERTION_AUDIENCE
        or claims.get("scope") != SCHEDULING_CORTEX_ASSERTION_SCOPE
        or not str(claims.get("jti") or "").strip()
        or issued > current + 30
        or current >= expires
        or expires <= issued
        or expires - issued > SCHEDULING_CORTEX_ASSERTION_MAX_TTL_SECONDS
    ):
        return False
    expected_subject = _workspace_assertion_subject(request_payload)
    return all(str(claims.get(name) or "") == value for name, value in expected_subject.items())


def _owner_endpoint(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise SchedulingOwnerError("Viventium Scheduling Cortex URL is invalid") from exc
    loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not loopback_http)
    ):
        raise SchedulingOwnerError(
            "Viventium Scheduling Cortex must use configured HTTPS or exact loopback HTTP"
        )
    path = parsed.path.rstrip("/")
    if path.endswith("/mcp"):
        path = path[:-4]
    path = f"{path}/internal/glasshive/recurring-schedules"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _default_request(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, dict[str, object]]:
    request = Request(url, method="POST", headers=headers, data=body)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise SchedulingOwnerError("Viventium Scheduling Cortex response is too large")
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return int(response.status), payload
    except HTTPError as exc:
        raw = exc.read(65_537)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        return int(exc.code), payload
    except (OSError, TimeoutError, URLError) as exc:
        raise SchedulingOwnerError("Viventium Scheduling Cortex is unavailable") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchedulingOwnerError("Viventium Scheduling Cortex returned an invalid response") from exc


class ViventiumSchedulingOwnerClient:
    def __init__(
        self,
        *,
        owner_url: str | None = None,
        scheduler_secret: str | None = None,
        timeout_seconds: float | None = None,
        request: OwnerRequest = _default_request,
    ) -> None:
        self._owner_url = owner_url
        self._scheduler_secret = scheduler_secret
        self._timeout_seconds = timeout_seconds
        self._request = request

    def _configuration(self) -> tuple[str, str, float]:
        owner_url = self._owner_url or os.environ.get("GLASSHIVE_SCHEDULING_OWNER_URL", "")
        secret = self._scheduler_secret or os.environ.get("VIVENTIUM_SCHEDULER_SECRET", "")
        if not str(owner_url or "").strip() or not str(secret or "").strip():
            raise SchedulingOwnerError(
                "Viventium Scheduling Cortex ownership is not fully configured"
            )
        raw_timeout = self._timeout_seconds
        if raw_timeout is None:
            try:
                raw_timeout = float(
                    str(os.environ.get("GLASSHIVE_SCHEDULING_OWNER_TIMEOUT_SECONDS") or "10")
                )
            except ValueError as exc:
                raise SchedulingOwnerError("Viventium Scheduling Cortex timeout is invalid") from exc
        return _owner_endpoint(str(owner_url)), str(secret).strip(), max(1.0, min(raw_timeout, 60.0))

    def call(
        self,
        action: str,
        payload: dict[str, object],
        *,
        identity: SchedulingOwnerIdentity,
    ) -> object:
        url, secret, timeout = self._configuration()
        body = json.dumps(
            {
                "action": str(action or "").strip(),
                "tenant_id": identity.tenant_id,
                "owner_id": identity.owner_id,
                "agent_id": identity.agent_id,
                "payload": payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > 1_048_576:
            raise SchedulingOwnerError(
                "Viventium Scheduling Cortex request is too large",
                code="invalid_schedule",
                status_code=400,
            )
        status, response = self._request(
            url,
            {
                "Content-Type": "application/json",
                "X-Viventium-Scheduler-Secret": secret,
                "X-Viventium-Tenant-Id": identity.tenant_id,
                "X-Viventium-User-Id": identity.owner_id,
                "X-Viventium-Agent-Id": identity.agent_id,
            },
            body,
            timeout,
        )
        if 200 <= status < 300 and isinstance(response, dict) and "result" in response:
            return response["result"]
        code = str(response.get("code") or "scheduling_owner_failed")
        message = str(response.get("error") or "Viventium Scheduling Cortex request failed")
        safe_status = status if status in {400, 404, 409, 422, 503} else 503
        raise SchedulingOwnerError(message, code=code, status_code=safe_status)
