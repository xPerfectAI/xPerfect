from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .openclaw_runtime import RuntimeErrorBase


_ISSUER_AUDIENCE = "glasshive-capability-grant-issuer"
_ISSUER_TTL_SECONDS = 60
_SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_LOCAL_IDENTITY_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
_ALLOWED_BINDING_PROOFS = {"operator_verified", "shared_oidc_subject"}
_SHARED_OIDC_PRINCIPAL_PATTERN = re.compile(r"^usr_[a-f0-9]{32}$")
_READINESS_STATES = {
    "ready",
    "degraded",
    "action_required",
    "no_connections",
    "broker_unavailable",
}


class CapabilityBrokerError(RuntimeErrorBase):
    """A public-safe failure at the direct connected-capability boundary."""

    def __init__(self, message: str, *, code: str = "capability_broker_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CapabilityOwnerBinding:
    glasshive_tenant_id: str
    glasshive_owner_id: str
    librechat_user_id: str
    proof: str


@dataclass(frozen=True)
class CapabilityBrokerConfig:
    issuer_url: str
    secret: str
    broker_tenant_id: str
    owner_bindings: tuple[CapabilityOwnerBinding, ...]
    identity_binding: str = "operator_verified"
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class CapabilityGrantRef:
    grant_id: str
    tenant_id: str
    user_id: str
    worker_id: str
    run_id: str
    execution_mode: str
    expires_at: int
    renewable_until: int


BrokerRequest = Callable[
    [str, str, dict[str, str], bytes, float],
    tuple[int, dict[str, str], dict[str, object]],
]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalized_scope(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SCOPE_PATTERN.fullmatch(normalized):
        raise CapabilityBrokerError(
            f"GlassHive capability {label} is not a valid broker scope",
            code="invalid_scope",
        )
    return normalized


def _normalized_local_identity(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _LOCAL_IDENTITY_PATTERN.fullmatch(normalized):
        raise CapabilityBrokerError(
            f"GlassHive canonical {label} identity is invalid",
            code="owner_binding_invalid",
        )
    return normalized


def _normalized_https_url(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise CapabilityBrokerError(
            "GlassHive capability issuer must be a configured HTTPS URL",
            code="broker_unavailable",
        )
    return raw


def _owner_bindings(raw: str) -> tuple[CapabilityOwnerBinding, ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CapabilityBrokerError(
            "GlassHive capability owner bindings are invalid",
            code="owner_binding_invalid",
        ) from exc
    if not isinstance(payload, list):
        raise CapabilityBrokerError(
            "GlassHive capability owner bindings must be a JSON list",
            code="owner_binding_invalid",
        )
    bindings: list[CapabilityOwnerBinding] = []
    seen: set[tuple[str, str]] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise CapabilityBrokerError(
                "GlassHive capability owner bindings must be structured objects",
                code="owner_binding_invalid",
            )
        tenant_id = _normalized_local_identity(item.get("glasshive_tenant_id"), "tenant")
        owner_id = _normalized_local_identity(item.get("glasshive_owner_id"), "owner")
        librechat_user_id = _normalized_scope(item.get("librechat_user_id"), "user")
        proof = str(item.get("proof") or "").strip().lower()
        if proof not in _ALLOWED_BINDING_PROOFS:
            raise CapabilityBrokerError(
                "GlassHive capability owner bindings require an explicit reviewed proof",
                code="owner_binding_invalid",
            )
        if proof == "shared_oidc_subject" and owner_id != librechat_user_id:
            raise CapabilityBrokerError(
                "A shared OIDC subject binding must use the same canonical principal",
                code="owner_binding_invalid",
            )
        key = (tenant_id, owner_id)
        if key in seen:
            raise CapabilityBrokerError(
                "GlassHive capability owner bindings contain a duplicate canonical owner",
                code="owner_binding_invalid",
            )
        seen.add(key)
        bindings.append(
            CapabilityOwnerBinding(
                glasshive_tenant_id=tenant_id,
                glasshive_owner_id=owner_id,
                librechat_user_id=librechat_user_id,
                proof=proof,
            )
        )
    return tuple(bindings)


def capability_broker_config_from_environment() -> CapabilityBrokerConfig | None:
    issuer_url = str(os.environ.get("GLASSHIVE_CAPABILITY_BROKER_ISSUER_URL") or "").strip()
    direct_secret = str(
        os.environ.get("GLASSHIVE_CAPABILITY_BROKER_ISSUER_SECRET") or ""
    ).strip()
    direct_tenant_id = str(
        os.environ.get("GLASSHIVE_CAPABILITY_BROKER_TENANT_ID") or ""
    ).strip()
    direct_bindings_raw = str(
        os.environ.get("GLASSHIVE_CAPABILITY_BROKER_OWNER_BINDINGS_JSON") or ""
    )
    identity_binding = str(
        os.environ.get("GLASSHIVE_CAPABILITY_BROKER_IDENTITY_BINDING")
        or "operator_verified"
    ).strip().lower()
    if not any(
        (
            issuer_url,
            direct_secret,
            direct_tenant_id,
            direct_bindings_raw.strip(),
            (
                identity_binding
                if "GLASSHIVE_CAPABILITY_BROKER_IDENTITY_BINDING" in os.environ
                else ""
            ),
        )
    ):
        return None
    secret = str(
        direct_secret
        or os.environ.get("GLASSHIVE_INFERENCE_BROKER_SECRET")
        or ""
    ).strip()
    broker_tenant_id = str(
        direct_tenant_id
        or os.environ.get("GLASSHIVE_INFERENCE_BROKER_TENANT_ID")
        or ""
    ).strip()
    bindings_raw = str(
        direct_bindings_raw
        or os.environ.get("GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON")
        or ""
    )
    if identity_binding not in _ALLOWED_BINDING_PROOFS:
        raise CapabilityBrokerError(
            "GlassHive capability identity binding proof is invalid",
            code="owner_binding_invalid",
        )
    if not all((issuer_url, secret, broker_tenant_id)) or (
        identity_binding == "operator_verified" and not bindings_raw.strip()
    ):
        raise CapabilityBrokerError(
            "GlassHive capability broker configuration is incomplete",
            code="broker_unavailable",
        )
    if len(secret) < 32:
        raise CapabilityBrokerError(
            "GlassHive capability issuer secret is too short",
            code="broker_unavailable",
        )
    try:
        timeout = float(
            str(os.environ.get("GLASSHIVE_CAPABILITY_BROKER_TIMEOUT_SECONDS") or "10")
        )
    except ValueError as exc:
        raise CapabilityBrokerError(
            "GlassHive capability broker timeout is invalid",
            code="broker_unavailable",
        ) from exc
    return CapabilityBrokerConfig(
        issuer_url=_normalized_https_url(issuer_url),
        secret=secret,
        broker_tenant_id=_normalized_scope(broker_tenant_id, "tenant"),
        owner_bindings=_owner_bindings(bindings_raw or "[]"),
        identity_binding=identity_binding,
        timeout_seconds=max(1.0, min(timeout, 60.0)),
    )


def _default_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, dict[str, str], dict[str, object]]:
    request = Request(url, method=method, headers=headers, data=body)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(256 * 1024)
            status = int(response.status)
            response_headers = {
                str(key).lower(): str(value) for key, value in response.headers.items()
            }
    except HTTPError as exc:
        status = int(exc.code)
        if 300 <= status < 400:
            exc.close()
            raise CapabilityBrokerError(
                "GlassHive capability broker refused an unsafe redirect",
                code="broker_redirect_rejected",
            ) from exc
        raw = exc.read(64 * 1024)
        response_headers = {
            str(key).lower(): str(value) for key, value in exc.headers.items()
        }
    except (OSError, URLError) as exc:
        raise CapabilityBrokerError(
            "GlassHive connected capability broker is unavailable",
            code="broker_unavailable",
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapabilityBrokerError(
            "GlassHive connected capability broker returned an invalid response",
            code="broker_invalid_response",
        ) from exc
    if not isinstance(payload, dict):
        raise CapabilityBrokerError(
            "GlassHive connected capability broker returned an invalid response",
            code="broker_invalid_response",
        )
    return status, response_headers, payload


def _deep_merge(first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
    merged = dict(first)
    for key, value in second.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def worker_with_ephemeral_capability_bundle(
    worker: dict,
    bundle: dict[str, object],
) -> dict:
    """Overlay a grant in memory; never mutate the stored worker or workspace record."""

    raw = str(worker.get("bootstrap_bundle_json") or "").strip()
    try:
        persistent = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        persistent = {}
    if not isinstance(persistent, dict):
        persistent = {}
    projected = dict(worker)
    projected["bootstrap_bundle_json"] = json.dumps(
        _deep_merge(persistent, bundle),
        sort_keys=True,
        separators=(",", ":"),
    )
    return projected


class GlassHiveCapabilityBroker:
    """Mint and revoke direct connected-capability grants in process memory only."""

    def __init__(
        self,
        config: CapabilityBrokerConfig | None,
        *,
        request: BrokerRequest | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self._request = request or _default_request
        self._now = now or time.time
        self._active: dict[tuple[str, str], CapabilityGrantRef] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> "GlassHiveCapabilityBroker":
        return cls(capability_broker_config_from_environment())

    @property
    def configured(self) -> bool:
        return self.config is not None

    def binding_for_owner(self, *, tenant_id: str, owner_id: str) -> tuple[str, str]:
        config = self.config
        if config is None:
            raise CapabilityBrokerError(
                "GlassHive connected capability broker is not configured",
                code="broker_unavailable",
            )
        clean_tenant = _normalized_local_identity(tenant_id, "tenant")
        clean_owner = _normalized_local_identity(owner_id, "owner")
        matches = [
            binding
            for binding in config.owner_bindings
            if hmac.compare_digest(binding.glasshive_tenant_id, clean_tenant)
            and hmac.compare_digest(binding.glasshive_owner_id, clean_owner)
        ]
        if len(matches) == 1:
            return matches[0].librechat_user_id, matches[0].proof
        if len(matches) > 1:
            raise CapabilityBrokerError(
                "This GlassHive owner has multiple connected-account bindings",
                code="owner_binding_required",
            )
        if config.identity_binding == "shared_oidc_subject":
            if not hmac.compare_digest(clean_tenant, config.broker_tenant_id):
                raise CapabilityBrokerError(
                    "This GlassHive tenant is outside the shared OIDC binding",
                    code="owner_binding_required",
                )
            if not _SHARED_OIDC_PRINCIPAL_PATTERN.fullmatch(clean_owner):
                raise CapabilityBrokerError(
                    "This GlassHive owner is not a canonical shared OIDC principal",
                    code="owner_binding_required",
                )
            return clean_owner, "shared_oidc_subject"
        raise CapabilityBrokerError(
            "This GlassHive owner has no explicitly verified LibreChat principal binding",
            code="owner_binding_required",
        )

    def principal_for_owner(self, *, tenant_id: str, owner_id: str) -> str:
        return self.binding_for_owner(tenant_id=tenant_id, owner_id=owner_id)[0]

    def _assertion(
        self,
        *,
        action: str,
        tenant_id: str,
        owner_id: str,
        execution_mode: str,
        worker_id: str = "",
        run_id: str = "",
    ) -> str:
        config = self.config
        assert config is not None
        issued_at = int(self._now())
        principal, binding_proof = self.binding_for_owner(
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        claims: dict[str, object] = {
            "aud": _ISSUER_AUDIENCE,
            "tenant_id": config.broker_tenant_id,
            "user_id": principal,
            "binding_proof": binding_proof,
            "action": action,
            "execution_mode": execution_mode,
            "iat": issued_at,
            "exp": issued_at + _ISSUER_TTL_SECONDS,
            "nonce": secrets.token_hex(16),
        }
        if action != "status":
            claims["worker_id"] = _normalized_scope(worker_id, "worker")
            claims["run_id"] = _normalized_scope(run_id, "run")
        derived = hmac.new(
            config.secret.encode("utf-8"),
            b"viventium-glasshive-capability:issuer:v1",
            hashlib.sha256,
        ).digest()
        signature = hmac.new(
            derived,
            _stable_json(claims).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return _urlsafe_b64encode(
            json.dumps(
                {**claims, "sig": _urlsafe_b64encode(signature)},
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )

    def _post(
        self,
        path: str,
        *,
        assertion: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        config = self.config
        assert config is not None
        status, _headers, payload = self._request(
            "POST",
            f"{config.issuer_url}{path}",
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {assertion}",
                "Cache-Control": "no-store",
                "Content-Type": "application/json",
            },
            _stable_json(body or {}).encode("utf-8"),
            config.timeout_seconds,
        )
        if status < 200 or status >= 300:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = str(error.get("code") or "broker_unavailable").strip()
            messages = {
                "owner_binding_required": "This GlassHive user is not mapped to connected accounts",
                "connected_account_action_required": "A connected account must be reconnected",
                "user_unavailable": "The verified connected-account user is unavailable",
                "registry_unavailable": "Connected-account readiness is temporarily unavailable",
                "grant_unavailable": "Connected-account authorization is temporarily unavailable",
            }
            raise CapabilityBrokerError(
                messages.get(code, "GlassHive connected capability broker is unavailable"),
                code=code,
            )
        return payload

    @staticmethod
    def _redacted_status(payload: dict[str, object]) -> dict[str, object]:
        status = str(payload.get("status") or "broker_unavailable").strip()
        if status not in _READINESS_STATES:
            raise CapabilityBrokerError(
                "GlassHive connected capability broker returned invalid readiness",
                code="broker_invalid_response",
            )
        connections = payload.get("connections")
        if not isinstance(connections, list) or len(connections) > 100:
            raise CapabilityBrokerError(
                "GlassHive connected capability broker returned invalid readiness",
                code="broker_invalid_response",
            )
        safe_connections: list[dict[str, str]] = []
        for item in connections:
            if not isinstance(item, dict):
                raise CapabilityBrokerError(
                    "GlassHive connected capability broker returned invalid readiness",
                    code="broker_invalid_response",
                )
            item_status = str(item.get("status") or "").strip()
            if item_status not in {"ready", "action_required"}:
                raise CapabilityBrokerError(
                    "GlassHive connected capability broker returned invalid readiness",
                    code="broker_invalid_response",
                )
            safe_connections.append(
                {
                    "connection_id": str(item.get("connection_id") or "")[:240],
                    "label": str(item.get("label") or "Connected service")[:200],
                    "kind": str(item.get("kind") or "connected_service")[:200],
                    "adapter": "librechat_capability_broker",
                    "status": item_status,
                }
            )
        return {
            "status": status,
            "reason": str(payload.get("reason") or "")[:120],
            "connections": safe_connections,
        }

    def status(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        execution_mode: str = "docker",
    ) -> dict[str, object]:
        if self.config is None:
            return {"status": "broker_unavailable", "reason": "not_configured", "connections": []}
        mode = str(execution_mode or "docker").strip().lower()
        if mode not in {"host", "docker"}:
            raise CapabilityBrokerError("Invalid connected capability execution mode", code="invalid_scope")
        assertion = self._assertion(
            action="status",
            tenant_id=tenant_id,
            owner_id=owner_id,
            execution_mode=mode,
        )
        return self._redacted_status(self._post("/status", assertion=assertion))

    def issue(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
        run_id: str,
        execution_mode: str,
    ) -> tuple[dict[str, object], CapabilityGrantRef | None, dict[str, object]]:
        config = self.config
        if config is None:
            return {}, None, {
                "status": "broker_unavailable",
                "reason": "not_configured",
                "connections": [],
            }
        mode = str(execution_mode or "docker").strip().lower()
        if mode not in {"host", "docker"}:
            raise CapabilityBrokerError("Invalid connected capability execution mode", code="invalid_scope")
        assertion = self._assertion(
            action="grant",
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
            run_id=run_id,
            execution_mode=mode,
        )
        payload = self._post("/grant", assertion=assertion)
        bundle = payload.get("bootstrapBundle")
        grant_ref = payload.get("grantRef")
        capability_status = payload.get("capabilityStatus")
        if not isinstance(bundle, dict) or not isinstance(capability_status, dict):
            raise CapabilityBrokerError(
                "GlassHive connected capability broker returned an invalid grant",
                code="broker_invalid_response",
            )
        if len(_stable_json(bundle).encode("utf-8")) > 256 * 1024:
            raise CapabilityBrokerError(
                "GlassHive connected capability grant is too large",
                code="broker_invalid_response",
            )
        redacted_status = self._redacted_status(
            {
                "status": capability_status.get("status"),
                "reason": capability_status.get("reason"),
                "connections": capability_status.get("connections") or [],
            }
        )
        if grant_ref is None:
            return bundle, None, redacted_status
        if not isinstance(grant_ref, dict):
            raise CapabilityBrokerError(
                "GlassHive connected capability broker returned an invalid grant",
                code="broker_invalid_response",
            )
        principal = self.principal_for_owner(tenant_id=tenant_id, owner_id=owner_id)
        ref = CapabilityGrantRef(
            grant_id=_normalized_scope(grant_ref.get("grant_id"), "grant"),
            tenant_id=_normalized_scope(grant_ref.get("tenant_id"), "tenant"),
            user_id=_normalized_scope(grant_ref.get("user_id"), "user"),
            worker_id=_normalized_scope(grant_ref.get("worker_id"), "worker"),
            run_id=_normalized_scope(grant_ref.get("run_id"), "run"),
            execution_mode=str(grant_ref.get("execution_mode") or ""),
            expires_at=int(grant_ref.get("exp") or 0),
            renewable_until=int(grant_ref.get("renewable_until") or 0),
        )
        if (
            ref.tenant_id != config.broker_tenant_id
            or ref.user_id != principal
            or ref.worker_id != worker_id
            or ref.run_id != run_id
            or ref.execution_mode != mode
            or ref.expires_at <= int(self._now())
            or ref.renewable_until < ref.expires_at
        ):
            raise CapabilityBrokerError(
                "GlassHive connected capability grant scope does not match this run",
                code="broker_invalid_response",
            )
        with self._lock:
            key = (worker_id, run_id)
            if key in self._active:
                raise CapabilityBrokerError(
                    "This worker run already has an active connected capability grant",
                    code="grant_conflict",
                )
            self._active[key] = ref
        return bundle, ref, redacted_status

    def _revoke_ref(
        self,
        ref: CapabilityGrantRef,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> None:
        assertion = self._assertion(
            action="revoke",
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=ref.worker_id,
            run_id=ref.run_id,
            execution_mode=ref.execution_mode,
        )
        payload = self._post(
            "/revoke",
            assertion=assertion,
            body={
                "grant_id": ref.grant_id,
                "renewable_until": ref.renewable_until,
            },
        )
        if payload.get("revoked") is not True or str(payload.get("grant_id") or "") != ref.grant_id:
            raise CapabilityBrokerError(
                "GlassHive capability broker did not confirm grant revocation",
                code="revoke_failed",
            )

    def revoke_active(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
        run_id: str | None = None,
    ) -> None:
        if self.config is None:
            return
        with self._lock:
            keys = [
                key
                for key in self._active
                if key[0] == worker_id and (run_id is None or key[1] == run_id)
            ]
            refs = [self._active.pop(key) for key in keys]
        failures: list[CapabilityBrokerError] = []
        for ref in refs:
            try:
                self._revoke_ref(ref, tenant_id=tenant_id, owner_id=owner_id)
            except CapabilityBrokerError as exc:
                failures.append(exc)
        if failures:
            raise CapabilityBrokerError(
                "GlassHive could not confirm capability grant revocation; the worker was stopped and the grant will expire",
                code="revoke_failed",
            ) from failures[0]

    @contextmanager
    def bind_run(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
        run_id: str,
        execution_mode: str,
    ) -> Iterator[tuple[dict[str, object], dict[str, object]]]:
        bundle, _ref, readiness = self.issue(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
            run_id=run_id,
            execution_mode=execution_mode,
        )
        body_error: BaseException | None = None
        try:
            yield bundle, readiness
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            try:
                self.revoke_active(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    run_id=run_id,
                )
            except CapabilityBrokerError:
                if body_error is None:
                    raise
