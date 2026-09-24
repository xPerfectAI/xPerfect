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
from datetime import datetime, timezone
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .openclaw_runtime import RuntimeErrorBase


ADAPTER_ID = "openai_responses_v1"
_ISSUER_AUDIENCE = "glasshive-inference-grant-issuer"
_GRANT_AUDIENCE = "glasshive-inference-proxy"
_ISSUER_TTL_SECONDS = 60
_GRANT_TTL_SECONDS = 10 * 60
_SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_LOCAL_IDENTITY_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
_GRANT_ID_PATTERN = re.compile(r"^ghcb_infer_[a-f0-9]{64}$")
_ALLOWED_BINDING_PROOFS = {"operator_verified", "shared_oidc_subject"}


class InferenceBrokerError(RuntimeErrorBase):
    """A public-safe, typed failure from the inference broker boundary."""

    def __init__(self, message: str, *, code: str = "inference_broker_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InferenceBrokerOwnerBinding:
    glasshive_tenant_id: str
    glasshive_owner_id: str
    librechat_user_id: str
    proof: str


@dataclass(frozen=True)
class InferenceBrokerConfig:
    issuer_url: str
    proxy_base_url: str
    secret: str
    broker_tenant_id: str
    owner_bindings: tuple[InferenceBrokerOwnerBinding, ...]
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class InferenceBrokerGrant:
    token: str
    grant_id: str
    tenant_id: str
    user_id: str
    worker_id: str
    run_id: str
    route: str
    models: tuple[str, ...]
    base_url: str
    expires_at: int

    def projection(self) -> dict[str, object]:
        """Return an in-memory-only Codex adapter projection.

        Callers must never place this object into a bootstrap bundle, workspace record, template,
        run evidence payload, or control-plane store.
        """

        return {
            "adapter": ADAPTER_ID,
            "grant_token": self.token,
            "grant_id": self.grant_id,
            "base_url": self.base_url,
            "worker_id": self.worker_id,
            "run_id": self.run_id,
            "models": list(self.models),
            "expires_at": self.expires_at,
        }


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


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _derived_secret(secret: str, purpose: str) -> bytes:
    return hmac.new(
        secret.strip().encode("utf-8"),
        f"viventium-glasshive-inference:{purpose}:v1".encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _sign_claims(claims: dict[str, object], secret: str, purpose: str) -> str:
    return _urlsafe_b64encode(
        hmac.new(
            _derived_secret(secret, purpose),
            _stable_json(claims).encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )


def _normalized_scope(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SCOPE_PATTERN.fullmatch(normalized):
        raise InferenceBrokerError(
            f"GlassHive inference {label} is not a valid broker scope",
            code="invalid_scope",
        )
    return normalized


def _normalized_local_identity(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _LOCAL_IDENTITY_PATTERN.fullmatch(normalized):
        raise InferenceBrokerError(
            f"GlassHive canonical {label} identity is invalid",
            code="owner_binding_invalid",
        )
    return normalized


def _normalized_models(models: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(model).strip() for model in models if str(model).strip()}))
    if (
        not normalized
        or len(normalized) != len(models)
        or len(normalized) > 32
        or any(not _MODEL_PATTERN.fullmatch(model) for model in normalized)
    ):
        raise InferenceBrokerError(
            "GlassHive inference models are not valid for the reviewed broker adapter",
            code="invalid_models",
        )
    return normalized


def _normalized_https_url(value: object, label: str) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise InferenceBrokerError(
            f"GlassHive inference {label} must be a configured HTTPS URL",
            code="broker_unavailable",
        )
    return raw.rstrip("/")


def _owner_bindings_from_json(raw: str) -> tuple[InferenceBrokerOwnerBinding, ...]:
    if not raw.strip():
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InferenceBrokerError(
            "GlassHive inference owner bindings are invalid",
            code="owner_binding_invalid",
        ) from exc
    if not isinstance(payload, list):
        raise InferenceBrokerError(
            "GlassHive inference owner bindings must be a JSON list",
            code="owner_binding_invalid",
        )
    bindings: list[InferenceBrokerOwnerBinding] = []
    seen: set[tuple[str, str]] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise InferenceBrokerError(
                "GlassHive inference owner binding entries must be structured objects",
                code="owner_binding_invalid",
            )
        tenant_id = _normalized_local_identity(
            item.get("glasshive_tenant_id"), "tenant"
        )
        owner_id = _normalized_local_identity(item.get("glasshive_owner_id"), "owner")
        librechat_user_id = _normalized_scope(item.get("librechat_user_id"), "user")
        proof = str(item.get("proof") or "").strip().lower()
        if proof not in _ALLOWED_BINDING_PROOFS:
            raise InferenceBrokerError(
                "GlassHive inference owner bindings require an explicit reviewed proof",
                code="owner_binding_invalid",
            )
        if proof == "shared_oidc_subject" and owner_id != librechat_user_id:
            raise InferenceBrokerError(
                "A shared OIDC subject binding must use the same canonical principal",
                code="owner_binding_invalid",
            )
        key = (tenant_id, owner_id)
        if key in seen:
            raise InferenceBrokerError(
                "GlassHive inference owner bindings contain a duplicate canonical owner",
                code="owner_binding_invalid",
            )
        seen.add(key)
        bindings.append(
            InferenceBrokerOwnerBinding(
                glasshive_tenant_id=tenant_id,
                glasshive_owner_id=owner_id,
                librechat_user_id=librechat_user_id,
                proof=proof,
            )
        )
    return tuple(bindings)


def inference_broker_config_from_environment() -> InferenceBrokerConfig | None:
    issuer_url = str(os.environ.get("GLASSHIVE_INFERENCE_BROKER_URL") or "").strip()
    secret = str(os.environ.get("GLASSHIVE_INFERENCE_BROKER_SECRET") or "").strip()
    broker_tenant_id = str(
        os.environ.get("GLASSHIVE_INFERENCE_BROKER_TENANT_ID") or ""
    ).strip()
    bindings_raw = str(
        os.environ.get("GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON") or ""
    )
    if not any((issuer_url, secret, broker_tenant_id, bindings_raw.strip())):
        return None
    if not all((issuer_url, secret, broker_tenant_id, bindings_raw.strip())):
        raise InferenceBrokerError(
            "GlassHive inference broker configuration is incomplete",
            code="broker_unavailable",
        )
    if len(secret) < 32:
        raise InferenceBrokerError(
            "GlassHive inference broker signing secret is too short",
            code="broker_unavailable",
        )
    proxy_url = str(
        os.environ.get("GLASSHIVE_INFERENCE_BROKER_PROXY_BASE_URL") or issuer_url
    ).strip()
    try:
        timeout = float(
            str(os.environ.get("GLASSHIVE_INFERENCE_BROKER_TIMEOUT_SECONDS") or "10")
        )
    except ValueError as exc:
        raise InferenceBrokerError(
            "GlassHive inference broker timeout is invalid",
            code="broker_unavailable",
        ) from exc
    return InferenceBrokerConfig(
        issuer_url=_normalized_https_url(issuer_url, "issuer URL"),
        proxy_base_url=_normalized_https_url(proxy_url, "proxy URL"),
        secret=secret,
        broker_tenant_id=_normalized_scope(broker_tenant_id, "tenant"),
        owner_bindings=_owner_bindings_from_json(bindings_raw),
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
            response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
    except HTTPError as exc:
        status = int(exc.code)
        if 300 <= status < 400:
            exc.close()
            raise InferenceBrokerError(
                "GlassHive inference broker refused an unsafe redirect",
                code="broker_redirect_rejected",
            ) from exc
        raw = exc.read(64 * 1024)
        response_headers = {str(key).lower(): str(value) for key, value in exc.headers.items()}
    except (OSError, URLError) as exc:
        raise InferenceBrokerError(
            "GlassHive inference broker is unavailable",
            code="broker_unavailable",
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InferenceBrokerError(
            "GlassHive inference broker returned an invalid response",
            code="broker_invalid_response",
        ) from exc
    if not isinstance(parsed, dict):
        raise InferenceBrokerError(
            "GlassHive inference broker returned an invalid response",
            code="broker_invalid_response",
        )
    return status, response_headers, parsed


class GlassHiveInferenceBroker:
    """Issue, track, and revoke run-bound LibreChat inference grants in memory only."""

    def __init__(
        self,
        config: InferenceBrokerConfig | None,
        *,
        request: BrokerRequest | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self._request = request or _default_request
        self._now = now or time.time
        self._active: dict[tuple[str, str], InferenceBrokerGrant] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> "GlassHiveInferenceBroker":
        return cls(inference_broker_config_from_environment())

    @property
    def configured(self) -> bool:
        return self.config is not None

    def principal_for_owner(self, *, tenant_id: str, owner_id: str) -> str:
        config = self.config
        if config is None:
            raise InferenceBrokerError(
                "GlassHive inference broker is not configured",
                code="broker_unavailable",
            )
        normalized_tenant = _normalized_local_identity(tenant_id, "tenant")
        normalized_owner = _normalized_local_identity(owner_id, "owner")
        matches = [
            binding
            for binding in config.owner_bindings
            if hmac.compare_digest(binding.glasshive_tenant_id, normalized_tenant)
            and hmac.compare_digest(binding.glasshive_owner_id, normalized_owner)
        ]
        if len(matches) != 1:
            raise InferenceBrokerError(
                "This GlassHive owner has no explicitly verified LibreChat principal binding",
                code="owner_binding_required",
            )
        return matches[0].librechat_user_id

    def _issuer_assertion(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
        run_id: str,
        route: str,
        models: tuple[str, ...],
        action: str,
    ) -> str:
        config = self.config
        if config is None:
            raise InferenceBrokerError(
                "GlassHive inference broker is not configured",
                code="broker_unavailable",
            )
        issued_at = int(self._now())
        unsigned: dict[str, object] = {
            "aud": _ISSUER_AUDIENCE,
            "tenant_id": config.broker_tenant_id,
            "user_id": self.principal_for_owner(tenant_id=tenant_id, owner_id=owner_id),
            "worker_id": _normalized_scope(worker_id, "worker"),
            "run_id": _normalized_scope(run_id, "run"),
            "provider": "openai",
            "route": route,
            "adapter": ADAPTER_ID,
            "models": list(models),
            "action": action,
            "iat": issued_at,
            "exp": issued_at + _ISSUER_TTL_SECONDS,
            "nonce": secrets.token_hex(16),
        }
        claims = {**unsigned, "sig": _sign_claims(unsigned, config.secret, "issuer")}
        return _urlsafe_b64encode(
            json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
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
            code = str(error.get("code") or "inference_broker_rejected").strip()
            messages = {
                "credential_action_required": "The selected OpenAI connection must be connected again",
                "enterprise_route_unavailable": "The selected enterprise OpenAI route is unavailable",
                "personal_credentials_required": "This user requires a personal OpenAI connection",
                "user_unavailable": "The verified LibreChat user is unavailable",
            }
            raise InferenceBrokerError(
                messages.get(code, "GlassHive inference broker rejected this run"),
                code=code,
            )
        return payload

    def _verified_grant(
        self,
        payload: dict[str, object],
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
        run_id: str,
        route: str,
        models: tuple[str, ...],
    ) -> InferenceBrokerGrant:
        config = self.config
        assert config is not None
        token = str(payload.get("grantToken") or "")
        if not token or len(token) > 8192:
            raise InferenceBrokerError(
                "GlassHive inference broker returned an invalid grant",
                code="broker_invalid_response",
            )
        try:
            claims = json.loads(_urlsafe_b64decode(token).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InferenceBrokerError(
                "GlassHive inference broker returned an invalid grant",
                code="broker_invalid_response",
            ) from exc
        if not isinstance(claims, dict):
            raise InferenceBrokerError(
                "GlassHive inference broker returned an invalid grant",
                code="broker_invalid_response",
            )
        signature = str(claims.pop("sig", ""))
        expected_signature = _sign_claims(claims, config.secret, "grant")
        if not signature or not hmac.compare_digest(signature, expected_signature):
            raise InferenceBrokerError(
                "GlassHive inference broker grant signature is invalid",
                code="broker_invalid_response",
            )
        now = int(self._now())
        expected_user = self.principal_for_owner(tenant_id=tenant_id, owner_id=owner_id)
        claim_models = _normalized_models(tuple(claims.get("models") or ()))
        grant_id = str(claims.get("grant_id") or "")
        expires_at = int(claims.get("exp") or 0)
        issued_at = int(claims.get("iat") or 0)
        valid = (
            claims.get("aud") == _GRANT_AUDIENCE
            and claims.get("tenant_id") == config.broker_tenant_id
            and claims.get("user_id") == expected_user
            and claims.get("worker_id") == worker_id
            and claims.get("run_id") == run_id
            and claims.get("provider") == "openai"
            and claims.get("route") == route
            and claims.get("adapter") == ADAPTER_ID
            and claim_models == models
            and _GRANT_ID_PATTERN.fullmatch(grant_id)
            and issued_at <= now + 30
            and expires_at > now
            and expires_at > issued_at
            and expires_at - issued_at <= _GRANT_TTL_SECONDS
            and str(payload.get("grantId") or "") == grant_id
            and str(payload.get("provider") or "") == "openai"
            and str(payload.get("route") or "") == route
        )
        adapter = payload.get("adapter") if isinstance(payload.get("adapter"), dict) else {}
        expected_base_url = f"{config.proxy_base_url}/openai/v1"
        valid = valid and (
            str(adapter.get("id") or "") == ADAPTER_ID
            and str(adapter.get("auth") or "") == "bearer_grant"
            and adapter.get("paths") == ["/responses"]
            and adapter.get("supportsStreaming") is True
            and str(adapter.get("baseUrl") or "").rstrip("/") == expected_base_url
        )
        if not valid:
            raise InferenceBrokerError(
                "GlassHive inference broker grant scope does not match this run",
                code="broker_invalid_response",
            )
        expires_text = str(payload.get("expiresAt") or "").replace("Z", "+00:00")
        try:
            response_expiry = int(datetime.fromisoformat(expires_text).astimezone(timezone.utc).timestamp())
        except (ValueError, TypeError) as exc:
            raise InferenceBrokerError(
                "GlassHive inference broker returned an invalid grant expiry",
                code="broker_invalid_response",
            ) from exc
        if abs(response_expiry - expires_at) > 1:
            raise InferenceBrokerError(
                "GlassHive inference broker returned an inconsistent grant expiry",
                code="broker_invalid_response",
            )
        return InferenceBrokerGrant(
            token=token,
            grant_id=grant_id,
            tenant_id=config.broker_tenant_id,
            user_id=expected_user,
            worker_id=worker_id,
            run_id=run_id,
            route=route,
            models=models,
            base_url=expected_base_url,
            expires_at=expires_at,
        )

    def issue(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
        run_id: str,
        auth_method: str,
        models: list[str] | tuple[str, ...],
    ) -> InferenceBrokerGrant:
        route = {
            "api_key": "personal_api_key",
            "enterprise_route": "enterprise_route",
        }.get(str(auth_method or "").strip().lower())
        if route is None:
            raise InferenceBrokerError(
                "Only OpenAI API keys and enterprise routes use the inference broker",
                code="unsupported_route",
            )
        normalized_models = _normalized_models(models)
        assertion = self._issuer_assertion(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
            run_id=run_id,
            route=route,
            models=normalized_models,
            action="issue",
        )
        payload = self._post("/grants", assertion=assertion)
        grant = self._verified_grant(
            payload,
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
            run_id=run_id,
            route=route,
            models=normalized_models,
        )
        with self._lock:
            key = (worker_id, run_id)
            if key in self._active:
                raise InferenceBrokerError(
                    "This worker run already has an active inference grant",
                    code="grant_conflict",
                )
            self._active[key] = grant
        return grant

    def _revoke_grant(
        self,
        grant: InferenceBrokerGrant,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> None:
        assertion = self._issuer_assertion(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=grant.worker_id,
            run_id=grant.run_id,
            route=grant.route,
            models=grant.models,
            action="revoke",
        )
        payload = self._post(
            "/grants/revoke",
            assertion=assertion,
            body={"grantToken": grant.token},
        )
        if payload.get("revoked") is not True or str(payload.get("grantId") or "") != grant.grant_id:
            raise InferenceBrokerError(
                "GlassHive inference broker did not confirm grant revocation",
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
        with self._lock:
            keys = [
                key
                for key in self._active
                if key[0] == worker_id and (run_id is None or key[1] == run_id)
            ]
            grants = [self._active.pop(key) for key in keys]
        failures: list[InferenceBrokerError] = []
        for grant in grants:
            try:
                self._revoke_grant(grant, tenant_id=tenant_id, owner_id=owner_id)
            except InferenceBrokerError as exc:
                failures.append(exc)
        if failures:
            raise InferenceBrokerError(
                "GlassHive could not confirm inference grant revocation; the worker was stopped and the grant will expire",
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
        auth_method: str,
        models: list[str] | tuple[str, ...],
    ) -> Iterator[dict[str, object]]:
        grant = self.issue(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
            run_id=run_id,
            auth_method=auth_method,
            models=models,
        )
        body_error: BaseException | None = None
        try:
            yield grant.projection()
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
            except InferenceBrokerError:
                if body_error is None:
                    raise


def validated_codex_broker_projection(worker: dict) -> dict[str, object] | None:
    raw = worker.get("_glasshive_inference_broker")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RuntimeErrorBase("GlassHive inference broker projection is invalid")
    adapter = str(raw.get("adapter") or "")
    grant_token = str(raw.get("grant_token") or "")
    base_url = str(raw.get("base_url") or "").rstrip("/")
    worker_id = str(raw.get("worker_id") or "")
    run_id = str(raw.get("run_id") or "")
    active_run_id = str(worker.get("_active_run_id") or "")
    models = tuple(str(item) for item in raw.get("models") or ())
    model = str(worker.get("model") or "").strip()
    expires_at = int(raw.get("expires_at") or 0)
    if (
        adapter != ADAPTER_ID
        or not grant_token
        or len(grant_token) > 8192
        or worker_id != str(worker.get("worker_id") or "")
        or run_id != active_run_id
        or not _SCOPE_PATTERN.fullmatch(worker_id)
        or not _SCOPE_PATTERN.fullmatch(run_id)
        or not models
        or (model and model not in models)
        or expires_at <= int(time.time())
    ):
        raise RuntimeErrorBase("GlassHive inference broker projection does not match this worker run")
    _normalized_https_url(base_url, "adapter URL")
    return {
        "adapter": ADAPTER_ID,
        "grant_token": grant_token,
        "base_url": base_url,
        "worker_id": worker_id,
        "run_id": run_id,
        "models": list(models),
        "expires_at": expires_at,
    }
