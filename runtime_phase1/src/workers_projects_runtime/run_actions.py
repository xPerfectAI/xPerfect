from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

RunActionName = Literal["retry", "cancel"]
ACTION_CAPABILITY_HEADER = "X-Viventium-Action-Capability"
ACTION_ENDPOINT = "/v1/run-actions"
ACTION_CAPABILITY_VERSION = 1
ACTION_CAPABILITY_TYPE = "glasshive.run_action.v1"
ACTION_RETRY_CAPABILITY_TTL_SECONDS = 900
ACTION_CANCEL_CAPABILITY_TTL_SECONDS = 24 * 60 * 60
ACTION_CAPABILITY_MAX_TTL_SECONDS = ACTION_CANCEL_CAPABILITY_TTL_SECONDS

_SCOPED_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,191}$")


class RunActionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not value or len(value) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401)
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401) from exc
    if _b64url_encode(decoded) != value:
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401)
    return decoded


def _strict_scoped_id(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not _SCOPED_ID_PATTERN.fullmatch(text):
        raise RunActionError(
            "capability_invalid",
            f"The action capability has an invalid {field}.",
            status_code=401,
        )
    return text


def _strict_principal(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise RunActionError(
            "capability_invalid",
            f"The action capability has an invalid {field}.",
            status_code=401,
        )
    return text


def _strict_epoch(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunActionError(
            "capability_invalid",
            f"The action capability has an invalid {field}.",
            status_code=401,
        )
    return value


def _canonical_claims(claims: dict[str, object]) -> bytes:
    return json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _action_secret(secret: str, *, worker_id: str, run_id: str) -> bytes:
    context = f"{ACTION_CAPABILITY_TYPE}:{worker_id}:{run_id}".encode()
    return hmac.new(secret.encode("utf-8"), context, hashlib.sha256).digest()


def _operation_for(action: RunActionName) -> str:
    return "workspace_continue" if action == "retry" else "cancel"


def _strict_claims(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "type",
        "capabilityId",
        "action",
        "operation",
        "projectId",
        "workerId",
        "runId",
        "tenantId",
        "ownerId",
        "issuedAtEpoch",
        "expiresAtEpoch",
    }:
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401)
    if raw.get("version") != ACTION_CAPABILITY_VERSION or raw.get("type") != ACTION_CAPABILITY_TYPE:
        raise RunActionError("capability_invalid", "The action capability version is invalid.", status_code=401)
    action = str(raw.get("action") or "")
    if action not in {"retry", "cancel"} or raw.get("operation") != _operation_for(action):
        raise RunActionError("capability_invalid", "The action capability operation is invalid.", status_code=401)
    claims = {
        **raw,
        "capabilityId": _strict_scoped_id(raw.get("capabilityId"), field="capabilityId"),
        "projectId": _strict_scoped_id(raw.get("projectId"), field="projectId"),
        "workerId": _strict_scoped_id(raw.get("workerId"), field="workerId"),
        "runId": _strict_scoped_id(raw.get("runId"), field="runId"),
        "tenantId": _strict_principal(raw.get("tenantId"), field="tenantId"),
        "ownerId": _strict_principal(raw.get("ownerId"), field="ownerId"),
        "issuedAtEpoch": _strict_epoch(raw.get("issuedAtEpoch"), field="issuedAtEpoch"),
        "expiresAtEpoch": _strict_epoch(raw.get("expiresAtEpoch"), field="expiresAtEpoch"),
    }
    issued_at = int(claims["issuedAtEpoch"])
    expires_at = int(claims["expiresAtEpoch"])
    if expires_at <= issued_at or expires_at - issued_at > ACTION_CAPABILITY_MAX_TTL_SECONDS:
        raise RunActionError("capability_invalid", "The action capability lifetime is invalid.", status_code=401)
    return claims


def unverified_run_action_claims(capability: str) -> dict[str, object]:
    """Decode only enough scope to resolve the signing key; callers must not trust the result."""
    if len(str(capability or "")) > 16384:
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401)
    parts = str(capability or "").split(".")
    if len(parts) != 2:
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401)
    try:
        raw = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401) from exc
    return _strict_claims(raw)


def verify_run_action_capability(
    capability: str,
    *,
    secret: str,
    now_epoch: int | None = None,
) -> dict[str, object]:
    claims = unverified_run_action_claims(capability)
    if not secret:
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401)
    payload_segment, signature_segment = str(capability).split(".")
    expected = hmac.new(
        _action_secret(secret, worker_id=str(claims["workerId"]), run_id=str(claims["runId"])),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    supplied = _b64url_decode(signature_segment)
    if not hmac.compare_digest(expected, supplied):
        raise RunActionError("capability_invalid", "The action capability is invalid.", status_code=401)
    current = int(time.time()) if now_epoch is None else int(now_epoch)
    if current >= int(claims["expiresAtEpoch"]):
        raise RunActionError("capability_expired", "The action capability has expired.", status_code=401)
    if int(claims["issuedAtEpoch"]) > current + 30:
        raise RunActionError("capability_invalid", "The action capability is not yet valid.", status_code=401)
    return claims


def mint_run_action_capability(
    secret: str,
    *,
    worker: dict,
    run: dict,
    action: RunActionName,
    now_epoch: int | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, object]:
    if not secret:
        raise RunActionError("capability_unavailable", "Action capability signing is unavailable.", status_code=503)
    if action not in {"retry", "cancel"}:
        raise RunActionError("capability_invalid", "The action capability operation is invalid.", status_code=400)
    issued_at = int(time.time()) if now_epoch is None else int(now_epoch)
    default_ttl = (
        ACTION_RETRY_CAPABILITY_TTL_SECONDS
        if action == "retry"
        else ACTION_CANCEL_CAPABILITY_TTL_SECONDS
    )
    ttl = max(30, min(int(default_ttl if ttl_seconds is None else ttl_seconds), ACTION_CAPABILITY_MAX_TTL_SECONDS))
    expires_at = issued_at + ttl
    claims: dict[str, object] = {
        "version": ACTION_CAPABILITY_VERSION,
        "type": ACTION_CAPABILITY_TYPE,
        "capabilityId": f"gac_{uuid.uuid4().hex}",
        "action": action,
        "operation": _operation_for(action),
        "projectId": _strict_scoped_id(worker.get("project_id"), field="projectId"),
        "workerId": _strict_scoped_id(worker.get("worker_id"), field="workerId"),
        "runId": _strict_scoped_id(run.get("run_id"), field="runId"),
        "tenantId": _strict_principal(worker.get("tenant_id") or "local", field="tenantId"),
        "ownerId": _strict_principal(worker.get("owner_id"), field="ownerId"),
        "issuedAtEpoch": issued_at,
        "expiresAtEpoch": expires_at,
    }
    payload_segment = _b64url_encode(_canonical_claims(claims))
    signature = hmac.new(
        _action_secret(secret, worker_id=str(claims["workerId"]), run_id=str(claims["runId"])),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    capability = f"{payload_segment}.{_b64url_encode(signature)}"
    expires_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    return {
        "version": ACTION_CAPABILITY_VERSION,
        "capabilityId": claims["capabilityId"],
        "action": action,
        "operation": claims["operation"],
        "endpoint": ACTION_ENDPOINT,
        "projectId": claims["projectId"],
        "workerId": claims["workerId"],
        "runId": claims["runId"],
        "expiresAt": expires_iso,
        "capability": capability,
    }
