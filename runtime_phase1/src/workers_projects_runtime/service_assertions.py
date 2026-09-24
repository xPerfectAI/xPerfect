from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Any


SERVICE_ASSERTION_HEADER = "X-Viventium-Service-Assertion"
SERVICE_ASSERTION_AUDIENCE = "glasshive-account-api"
SERVICE_ASSERTION_VERSION = 1
SERVICE_ASSERTION_MAX_TTL_SECONDS = 60
SERVICE_ASSERTION_FUTURE_SKEW_SECONDS = 5

_CLAIM_KEYS = {"v", "aud", "tenant_id", "owner_id", "iat", "exp", "nonce"}
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,191}$")


class ServiceAssertionError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _invalid(message: str = "The Viventium service assertion is invalid.") -> ServiceAssertionError:
    return ServiceAssertionError("service_assertion_invalid", message)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not value or len(value) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise _invalid()
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise _invalid() from exc
    if _b64url_encode(decoded) != value:
        raise _invalid()
    return decoded


def _canonical_claims(claims: dict[str, Any]) -> bytes:
    return json.dumps(
        claims,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _principal(value: object, *, name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise _invalid(f"The Viventium service assertion has an invalid {name}.")
    return text


def _epoch(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"The Viventium service assertion has an invalid {name}.")
    return value


def verify_service_assertion(
    assertion: str,
    *,
    secret: str,
    now_epoch: int | None = None,
) -> dict[str, object]:
    """Verify one canonical, short-lived Viventium account-service assertion."""

    token = str(assertion or "").strip()
    if not secret or not token or len(token) > 16384:
        raise _invalid()
    parts = token.split(".")
    if len(parts) != 2:
        raise _invalid()
    payload_segment, signature_segment = parts
    payload_bytes = _b64url_decode(payload_segment)
    supplied_signature = _b64url_decode(signature_segment)
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise _invalid()
    try:
        raw = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid() from exc
    if not isinstance(raw, dict) or set(raw) not in (_CLAIM_KEYS, _CLAIM_KEYS | {"native_input_digest"}):
        raise _invalid()
    if payload_bytes != _canonical_claims(raw):
        raise _invalid("The Viventium service assertion payload is not canonical JSON.")
    if raw.get("v") != SERVICE_ASSERTION_VERSION or raw.get("aud") != SERVICE_ASSERTION_AUDIENCE:
        raise _invalid()

    native_input_digest = raw.get("native_input_digest")
    if native_input_digest is not None and not re.fullmatch(r"[a-f0-9]{64}", str(native_input_digest)):
        raise _invalid("Native input digest is invalid")
    tenant_id = _principal(raw.get("tenant_id"), name="tenant_id")
    owner_id = _principal(raw.get("owner_id"), name="owner_id")
    nonce = str(raw.get("nonce") or "").strip()
    if not _NONCE_PATTERN.fullmatch(nonce):
        raise _invalid("The Viventium service assertion has an invalid nonce.")
    issued_at = _epoch(raw.get("iat"), name="iat")
    expires_at = _epoch(raw.get("exp"), name="exp")
    if expires_at <= issued_at or expires_at - issued_at > SERVICE_ASSERTION_MAX_TTL_SECONDS:
        raise _invalid("The Viventium service assertion lifetime is invalid.")
    current = int(time.time()) if now_epoch is None else int(now_epoch)
    if issued_at > current + SERVICE_ASSERTION_FUTURE_SKEW_SECONDS:
        raise _invalid("The Viventium service assertion is not yet valid.")
    if current >= expires_at:
        raise ServiceAssertionError(
            "service_assertion_expired",
            "The Viventium service assertion has expired.",
        )
    return {
        "v": SERVICE_ASSERTION_VERSION,
        "aud": SERVICE_ASSERTION_AUDIENCE,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "iat": issued_at,
        "exp": expires_at,
        "nonce": nonce,
        **({"native_input_digest": native_input_digest} if native_input_digest is not None else {}),
    }


def mint_service_assertion(
    secret: str,
    *,
    tenant_id: str,
    owner_id: str,
    now_epoch: int | None = None,
    ttl_seconds: int = SERVICE_ASSERTION_MAX_TTL_SECONDS,
    nonce: str | None = None,
    native_input_digest: str | None = None,
) -> str:
    if not secret:
        raise ServiceAssertionError(
            "service_assertion_unavailable",
            "The Viventium service assertion signer is not configured.",
            status_code=503,
        )
    issued_at = int(time.time()) if now_epoch is None else int(now_epoch)
    ttl = int(ttl_seconds)
    if ttl <= 0 or ttl > SERVICE_ASSERTION_MAX_TTL_SECONDS:
        raise ValueError("Service assertion TTL must be between 1 and 60 seconds")
    claims: dict[str, object] = {
        "v": SERVICE_ASSERTION_VERSION,
        "aud": SERVICE_ASSERTION_AUDIENCE,
        "tenant_id": _principal(tenant_id, name="tenant_id"),
        "owner_id": _principal(owner_id, name="owner_id"),
        "iat": issued_at,
        "exp": issued_at + ttl,
        "nonce": nonce or f"nonce_{uuid.uuid4().hex}",
    }
    if native_input_digest is not None:
        if not re.fullmatch(r"[a-f0-9]{64}", native_input_digest):
            raise ValueError("Native input digest is invalid")
        claims["native_input_digest"] = native_input_digest
    if not _NONCE_PATTERN.fullmatch(str(claims["nonce"])):
        raise ValueError("Service assertion nonce is invalid")
    payload_segment = _b64url_encode(_canonical_claims(claims))
    signature = hmac.new(
        secret.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_segment}.{_b64url_encode(signature)}"
