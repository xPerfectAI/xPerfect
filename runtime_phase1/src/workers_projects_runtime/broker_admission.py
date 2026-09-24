from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

ADMISSION_HEADER = "X-Viventium-GlassHive-Admission"
_BODY_KEYS = {
    "authorizationRef",
    "containerGenerationId",
    "originRef",
    "runId",
    "workRef",
    "workerId",
}
_HOST_BODY_KEYS = (_BODY_KEYS - {"containerGenerationId"}) | {"hostStartupLeaseId"}
_SCHEDULED_PREPARE_BODY_KEYS = {
    "containerGenerationId",
    "originRef",
    "ownerId",
    "runId",
    "workRef",
    "workerId",
}
_SUCCESS_KEYS = _BODY_KEYS | {
    "status",
    "scopeFingerprint",
    "brokerUrl",
    "grantToken",
    "grant",
    "maxExpiresAt",
}
_SCHEDULED_PREPARE_SUCCESS_KEYS = _SCHEDULED_PREPARE_BODY_KEYS | {
    "authorizationRef",
    "brokerUrl",
    "maxExpiresAt",
    "scopeFingerprint",
    "status",
}
_GRANT_KEYS = {
    "grantId",
    "expiresAt",
    "allowedServers",
    "allowedHostTools",
    "scopes",
}
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,191}$")
_CONTAINER_GENERATION_ID = re.compile(r"^[a-f0-9]{64}$")


class BrokerAdmissionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        needs_input: bool = False,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "broker_admission_failed")
        self.needs_input = bool(needs_input)
        self.retryable = bool(retryable)
        self.status_code = status_code


@dataclass(frozen=True)
class BrokerAdmissionGrant:
    grant_token: str
    broker_url: str
    scope_fingerprint: str
    grant_id: str
    expires_at: int
    allowed_servers: tuple[str, ...]
    allowed_host_tools: tuple[str, ...]
    scopes: dict[str, object]
    max_expires_at: str
    container_generation_id: str = ""
    host_startup_lease_id: str = ""

    def broker_projection(self) -> dict[str, object]:
        return {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": self.broker_url,
            "grant_id": self.grant_id,
            "grant_expires_at": self.expires_at,
            "allowed_servers": list(self.allowed_servers),
            "allowed_host_tools": list(self.allowed_host_tools),
            "scopes": dict(self.scopes),
            "scope_fingerprint": self.scope_fingerprint,
            "max_expires_at": self.max_expires_at,
        }


@dataclass(frozen=True)
class ScheduledProviderAuthorization:
    authorization_ref: str
    origin_ref: str
    work_ref: str
    worker_id: str
    run_id: str
    container_generation_id: str
    scope_fingerprint: str
    broker_url: str
    max_expires_at: str


def _binding_keys(body: dict[str, str], *, revoke: bool = False) -> set[str]:
    suffix = {"grantId"} if revoke else set()
    for keys in (_BODY_KEYS, _HOST_BODY_KEYS):
        expected = keys | suffix
        if isinstance(body, dict) and set(body) == expected:
            return expected
    raise BrokerAdmissionError(
        "broker_admission_request_invalid",
        "The broker admission request binding is invalid.",
    )


def _canonical_body(
    body: dict[str, str], *, expected_keys: set[str] | frozenset[str] | None = None
) -> bytes:
    if expected_keys is None:
        expected_keys = _binding_keys(body)
    if not isinstance(body, dict) or set(body) != expected_keys:
        raise BrokerAdmissionError(
            "broker_admission_request_invalid",
            "The broker admission request binding is invalid.",
        )
    normalized: dict[str, str] = {}
    for key in sorted(expected_keys):
        value = body.get(key)
        if not isinstance(value, str) or not _OPAQUE_REF.fullmatch(value):
            raise BrokerAdmissionError(
                "broker_admission_request_invalid",
                "The broker admission request binding is invalid.",
            )
        normalized[key] = value
    if not _CONTAINER_GENERATION_ID.fullmatch(
        normalized.get("containerGenerationId", normalized.get("hostStartupLeaseId", ""))
    ):
        raise BrokerAdmissionError(
            "broker_admission_request_invalid",
            "The broker admission request binding is invalid.",
        )
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def mint_admission_header(
    body: dict[str, str],
    *,
    secret: str,
    timestamp: int | None = None,
    nonce: str | None = None,
    expected_keys: set[str] | frozenset[str] | None = None,
) -> str:
    """Sign Core's exact, replay-protected deferred-admission envelope."""

    if not secret:
        raise BrokerAdmissionError(
            "broker_admission_unconfigured",
            "Broker admission is not configured.",
        )
    canonical = _canonical_body(body, expected_keys=expected_keys).decode("utf-8")
    issued_at = int(time.time()) if timestamp is None else int(timestamp)
    fresh_nonce = nonce or f"nonce_{secrets.token_urlsafe(18)}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,192}", fresh_nonce):
        raise BrokerAdmissionError(
            "broker_admission_request_invalid",
            "The broker admission nonce is invalid.",
        )
    signing_input = f"v1\n{issued_at}\n{fresh_nonce}\n{canonical}".encode()
    digest = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"v1:{issued_at}:{fresh_nonce}:{_b64url(digest)}"


def _admission_url(value: str) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise BrokerAdmissionError(
            "broker_admission_unconfigured", "Broker admission is not configured."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(ord(char) < 32 or char.isspace() for char in url)
    ):
        raise BrokerAdmissionError(
            "broker_admission_unconfigured", "Broker admission is not configured."
        )
    return url


def _safe_string(value: object, *, maximum: int = 512) -> str:
    text = value if isinstance(value, str) else ""
    if (
        not text
        or len(text) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        return ""
    return text


def _safe_string_list(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > 128:
        return None
    result: list[str] = []
    for item in value:
        text = _safe_string(item, maximum=192)
        if not text:
            return None
        result.append(text)
    return tuple(result)


def _safe_scopes(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or len(value) > 128:
        return None
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    if len(encoded) > 32_768:
        return None
    return dict(value)


def _parse_max_expiry(value: object) -> tuple[str, float] | None:
    text = _safe_string(value, maximum=64)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return text, parsed.astimezone(timezone.utc).timestamp()


def _invalid_response() -> BrokerAdmissionError:
    return BrokerAdmissionError(
        "broker_admission_response_invalid",
        "The broker admission response is invalid.",
        retryable=True,
    )


def _invalid_scheduled_prepare_response() -> BrokerAdmissionError:
    return BrokerAdmissionError(
        "scheduled_provider_authorization_response_invalid",
        "The scheduled provider authorization response is invalid.",
        retryable=True,
    )


def prepare_scheduled_provider_authorization(
    admission_url: str,
    *,
    secret: str,
    body: dict[str, str],
    timeout_seconds: float = 5.0,
) -> ScheduledProviderAuthorization:
    """Prepare one provider-only authorization for an exact scheduled sandbox."""

    endpoint = _admission_url(admission_url)
    parsed = urlparse(endpoint)
    suffix = "/capabilities/admit"
    if (
        not parsed.path.endswith(suffix)
        or not isinstance(body, dict)
        or body.get("originRef") != body.get("workRef")
    ):
        raise BrokerAdmissionError(
            "scheduled_provider_authorization_request_invalid",
            "The scheduled provider authorization binding is invalid.",
        )
    try:
        encoded = _canonical_body(
            body, expected_keys=_SCHEDULED_PREPARE_BODY_KEYS
        )
        header = mint_admission_header(
            body,
            secret=secret,
            expected_keys=_SCHEDULED_PREPARE_BODY_KEYS,
        )
    except BrokerAdmissionError as exc:
        raise BrokerAdmissionError(
            "scheduled_provider_authorization_request_invalid",
            "The scheduled provider authorization binding is invalid.",
        ) from exc
    prepare_path = f"{parsed.path[:-len(suffix)]}/capabilities/prepare-scheduled"
    prepare_url = parsed._replace(path=prepare_path).geturl()
    try:
        response = httpx.post(
            prepare_url,
            content=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                ADMISSION_HEADER: header,
            },
            timeout=max(0.1, min(float(timeout_seconds), 30.0)),
        )
    except httpx.HTTPError as exc:
        raise BrokerAdmissionError(
            "scheduled_provider_authorization_unavailable",
            "Scheduled provider authorization is temporarily unavailable.",
            retryable=True,
        ) from exc
    if len(response.content) > 65_536:
        raise _invalid_scheduled_prepare_response()
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise _invalid_scheduled_prepare_response() from exc
    if not isinstance(payload, dict):
        raise _invalid_scheduled_prepare_response()
    if response.status_code < 200 or response.status_code >= 300:
        raw_error = payload.get("error")
        if (
            set(payload) != {"error"}
            or not isinstance(raw_error, dict)
            or set(raw_error) != {"code", "message", "needsInput"}
            or not isinstance(raw_error.get("needsInput"), bool)
        ):
            raise BrokerAdmissionError(
                "scheduled_provider_authorization_rejected",
                "Scheduled provider authorization was rejected.",
                retryable=(
                    response.status_code >= 500
                    or response.status_code in {408, 425, 429}
                ),
                status_code=response.status_code,
            )
        code = _safe_string(raw_error.get("code"), maximum=128)
        message = _safe_string(raw_error.get("message"), maximum=1000)
        needs_input = raw_error.get("needsInput") is True
        raise BrokerAdmissionError(
            code or "scheduled_provider_authorization_rejected",
            message or "Scheduled provider authorization was rejected.",
            needs_input=needs_input,
            retryable=(
                False
                if needs_input
                else response.status_code >= 500
                or response.status_code in {408, 425, 429}
            ),
            status_code=response.status_code,
        )
    cache_control = str(response.headers.get("cache-control") or "").lower()
    if (
        "no-store" not in cache_control
        or set(payload) != _SCHEDULED_PREPARE_SUCCESS_KEYS
        or payload.get("status") != "prepared"
    ):
        raise _invalid_scheduled_prepare_response()
    for key in _SCHEDULED_PREPARE_BODY_KEYS:
        if payload.get(key) != body[key]:
            raise _invalid_scheduled_prepare_response()
    authorization_ref = _safe_string(
        payload.get("authorizationRef"), maximum=192
    )
    scope_fingerprint = _safe_string(
        payload.get("scopeFingerprint"), maximum=256
    )
    broker_url = _safe_string(payload.get("brokerUrl"), maximum=2048)
    max_expiry = _parse_max_expiry(payload.get("maxExpiresAt"))
    if (
        not authorization_ref
        or not _OPAQUE_REF.fullmatch(authorization_ref)
        or not scope_fingerprint
        or max_expiry is None
        or max_expiry[1] <= time.time()
    ):
        raise _invalid_scheduled_prepare_response()
    try:
        broker_url = _admission_url(broker_url)
    except BrokerAdmissionError as exc:
        raise _invalid_scheduled_prepare_response() from exc
    return ScheduledProviderAuthorization(
        authorization_ref=authorization_ref,
        origin_ref=body["originRef"],
        work_ref=body["workRef"],
        worker_id=body["workerId"],
        run_id=body["runId"],
        container_generation_id=body["containerGenerationId"],
        scope_fingerprint=scope_fingerprint,
        broker_url=broker_url,
        max_expires_at=max_expiry[0],
    )


def admit_capability_grant(
    url: str,
    *,
    secret: str,
    body: dict[str, str],
    expected_scope_fingerprint: str,
    expected_max_expires_at: str | None = None,
    timeout_seconds: float = 5.0,
) -> BrokerAdmissionGrant:
    endpoint = _admission_url(url)
    encoded = _canonical_body(body)
    header = mint_admission_header(body, secret=secret)
    try:
        response = httpx.post(
            endpoint,
            content=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                ADMISSION_HEADER: header,
            },
            timeout=max(0.1, min(float(timeout_seconds), 30.0)),
        )
    except httpx.HTTPError as exc:
        raise BrokerAdmissionError(
            "broker_admission_unavailable",
            "Broker admission is temporarily unavailable.",
            retryable=True,
        ) from exc
    if len(response.content) > 65_536:
        raise _invalid_response()
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise _invalid_response() from exc
    if not isinstance(payload, dict):
        raise _invalid_response()

    if response.status_code < 200 or response.status_code >= 300:
        raw_error = payload.get("error")
        if (
            set(payload) != {"error"}
            or not isinstance(raw_error, dict)
            or set(raw_error) != {"code", "message", "needsInput"}
            or not isinstance(raw_error.get("needsInput"), bool)
        ):
            raise BrokerAdmissionError(
                "broker_admission_rejected",
                "Broker admission was rejected.",
                retryable=response.status_code >= 500 or response.status_code in {408, 425, 429},
                status_code=response.status_code,
            )
        code = _safe_string(raw_error.get("code"), maximum=128) or "broker_admission_rejected"
        message = _safe_string(raw_error.get("message"), maximum=1000) or "Broker admission was rejected."
        needs_input = raw_error.get("needsInput") is True
        raise BrokerAdmissionError(
            code,
            message,
            needs_input=needs_input,
            retryable=(
                False
                if needs_input
                else response.status_code >= 500 or response.status_code in {408, 425, 429}
            ),
            status_code=response.status_code,
        )

    cache_control = str(response.headers.get("cache-control") or "").lower()
    if (
        "no-store" not in cache_control
        or set(payload) != (set(body) | (_SUCCESS_KEYS - _BODY_KEYS))
        or payload.get("status") != "authorized"
    ):
        raise _invalid_response()
    for key in body:
        if payload.get(key) != body[key]:
            raise _invalid_response()
    scope_fingerprint = _safe_string(payload.get("scopeFingerprint"), maximum=256)
    if not scope_fingerprint or scope_fingerprint != expected_scope_fingerprint:
        raise _invalid_response()
    grant_token = _safe_string(payload.get("grantToken"), maximum=8192)
    broker_url = _safe_string(payload.get("brokerUrl"), maximum=2048)
    if not grant_token:
        raise _invalid_response()
    try:
        broker_url = _admission_url(broker_url)
    except BrokerAdmissionError as exc:
        raise _invalid_response() from exc
    grant = payload.get("grant")
    max_expiry = _parse_max_expiry(payload.get("maxExpiresAt"))
    if not isinstance(grant, dict) or set(grant) != _GRANT_KEYS or max_expiry is None:
        raise _invalid_response()
    if expected_max_expires_at is not None and max_expiry[0] != expected_max_expires_at:
        raise _invalid_response()
    grant_id = _safe_string(grant.get("grantId"), maximum=256)
    expires_at = grant.get("expiresAt")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise _invalid_response()
    if expires_at <= int(time.time()) or expires_at > max_expiry[1]:
        raise _invalid_response()
    allowed_servers = _safe_string_list(grant.get("allowedServers"))
    allowed_host_tools = _safe_string_list(grant.get("allowedHostTools"))
    scopes = _safe_scopes(grant.get("scopes"))
    if not grant_id or allowed_servers is None or allowed_host_tools is None or scopes is None:
        raise _invalid_response()
    return BrokerAdmissionGrant(
        grant_token=grant_token,
        broker_url=broker_url,
        scope_fingerprint=scope_fingerprint,
        grant_id=grant_id,
        expires_at=expires_at,
        allowed_servers=allowed_servers,
        allowed_host_tools=allowed_host_tools,
        scopes=scopes,
        max_expires_at=max_expiry[0],
        container_generation_id=body.get("containerGenerationId", ""),
        host_startup_lease_id=body.get("hostStartupLeaseId", ""),
    )


def preflight_provider_authorization(
    admission_url: str,
    *,
    grant_token: str,
    provider: str,
    expected_worker_id: str,
    expected_run_id: str,
    timeout_seconds: float = 3.0,
) -> None:
    endpoint = _admission_url(admission_url)
    parsed = urlparse(endpoint)
    suffix = "/capabilities/admit"
    normalized_provider = str(provider or "").strip().lower()
    token = str(grant_token or "").strip()
    worker_id = str(expected_worker_id or "").strip()
    run_id = str(expected_run_id or "").strip()
    if (
        not parsed.path.endswith(suffix)
        or normalized_provider not in {"openai", "anthropic"}
        or not token
        or len(token) > 8192
        or not _OPAQUE_REF.fullmatch(worker_id)
        or not _OPAQUE_REF.fullmatch(run_id)
    ):
        raise BrokerAdmissionError(
            "provider_auth_preflight_unconfigured",
            "Provider authorization preflight is not configured.",
        )
    preflight_path = (
        f"{parsed.path[:-len(suffix)]}/providers/"
        f"{normalized_provider}/auth/preflight"
    )
    preflight_url = parsed._replace(path=preflight_path).geturl()
    content = b'{"version":1}'
    try:
        response = httpx.post(
            preflight_url,
            content=content,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=max(0.1, min(float(timeout_seconds), 30.0)),
        )
    except httpx.HTTPError as exc:
        raise BrokerAdmissionError(
            "provider_auth_preflight_unavailable",
            "Provider authorization preflight is temporarily unavailable.",
            retryable=True,
        ) from exc
    if len(response.content) > 65_536:
        raise BrokerAdmissionError(
            "provider_auth_preflight_response_invalid",
            "Provider authorization preflight returned an invalid response.",
            retryable=True,
        )
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise BrokerAdmissionError(
            "provider_auth_preflight_response_invalid",
            "Provider authorization preflight returned an invalid response.",
            retryable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise BrokerAdmissionError(
            "provider_auth_preflight_response_invalid",
            "Provider authorization preflight returned an invalid response.",
            retryable=True,
        )
    if response.status_code < 200 or response.status_code >= 300:
        error = payload.get("error")
        if (
            set(payload) != {"error"}
            or not isinstance(error, dict)
            or set(error) != {"code", "message", "needsInput"}
            or not isinstance(error.get("needsInput"), bool)
        ):
            raise BrokerAdmissionError(
                "provider_auth_preflight_rejected",
                "Provider authorization preflight was rejected.",
                retryable=(
                    response.status_code >= 500
                    or response.status_code in {408, 425, 429}
                ),
                status_code=response.status_code,
            )
        code = _safe_string(error.get("code"), maximum=128)
        message = _safe_string(error.get("message"), maximum=1000)
        needs_input = error.get("needsInput") is True
        raise BrokerAdmissionError(
            code or "provider_auth_preflight_rejected",
            message or "Provider authorization preflight was rejected.",
            needs_input=needs_input,
            retryable=(
                False
                if needs_input
                else response.status_code >= 500
                or response.status_code in {408, 425, 429}
            ),
            status_code=response.status_code,
        )
    cache_control = str(response.headers.get("cache-control") or "").lower()
    expected = {
        "status": "authorized",
        "provider": normalized_provider,
        "workerId": worker_id,
        "runId": run_id,
    }
    if "no-store" not in cache_control or payload != expected:
        raise BrokerAdmissionError(
            "provider_auth_preflight_response_invalid",
            "Provider authorization preflight returned an invalid response.",
            retryable=True,
        )


def revoke_capability_grant(
    admission_url: str,
    *,
    secret: str,
    body: dict[str, str],
    timeout_seconds: float = 3.0,
) -> None:
    endpoint = _admission_url(admission_url)
    parsed = urlparse(endpoint)
    if not parsed.path.endswith("/admit"):
        raise BrokerAdmissionError(
            "broker_revocation_unconfigured",
            "Broker revocation is not configured.",
        )
    revoke_url = parsed._replace(path=f"{parsed.path[:-6]}/revoke").geturl()
    expected_keys = _binding_keys(body, revoke=True)
    encoded = _canonical_body(body, expected_keys=expected_keys)
    header = mint_admission_header(
        body, secret=secret, expected_keys=expected_keys
    )
    try:
        response = httpx.post(
            revoke_url,
            content=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                ADMISSION_HEADER: header,
            },
            timeout=max(0.1, min(float(timeout_seconds), 30.0)),
        )
    except httpx.HTTPError as exc:
        raise BrokerAdmissionError(
            "broker_revocation_unavailable",
            "Broker revocation is temporarily unavailable.",
            retryable=True,
        ) from exc
    if response.status_code != 204:
        raise BrokerAdmissionError(
            "broker_revocation_rejected",
            "Broker revocation was rejected.",
            retryable=response.status_code >= 500 or response.status_code in {408, 425, 429},
            status_code=response.status_code,
        )
