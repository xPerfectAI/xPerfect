from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Iterable

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


_RSA_PRIVATE_JWK_FIELDS = {"d", "p", "q", "dp", "dq", "qi", "oth"}
_MIN_ROTATION_OVERLAP_SECONDS = 180
_MAX_ROTATION_OVERLAP_SECONDS = 900


def _previous_public_keys_from_env(
    *,
    current_key_id: str,
    now: int,
) -> tuple[tuple[dict[str, object], ...], int]:
    jwks_file = str(
        os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_PREVIOUS_JWKS_FILE") or ""
    ).strip()
    raw_expiry = str(
        os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_PREVIOUS_KEYS_EXPIRE_AT") or ""
    ).strip()
    if bool(jwks_file) != bool(raw_expiry):
        raise RuntimeError(
            "Internal assertion rotation requires both the previous public JWKS file and expiry"
        )
    if not jwks_file:
        return (), 0
    try:
        expires_at = int(raw_expiry)
    except ValueError as exc:
        raise RuntimeError(
            "GLASSHIVE_INTERNAL_ASSERTION_PREVIOUS_KEYS_EXPIRE_AT must be a Unix timestamp"
        ) from exc
    if expires_at <= now:
        return (), expires_at
    overlap_seconds = expires_at - now
    if overlap_seconds < _MIN_ROTATION_OVERLAP_SECONDS:
        raise RuntimeError(
            "Internal assertion previous-key overlap must be at least 180 seconds"
        )
    if overlap_seconds > _MAX_ROTATION_OVERLAP_SECONDS:
        raise RuntimeError(
            "Internal assertion previous-key overlap must not exceed 900 seconds"
        )
    path = Path(jwks_file)
    try:
        if path.stat().st_size > 64 * 1024:
            raise RuntimeError("Internal assertion previous public JWKS is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError("Internal assertion previous public JWKS could not be read") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Internal assertion previous public JWKS is invalid") from exc
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, list) or not keys or len(keys) > 4:
        raise RuntimeError("Internal assertion previous public JWKS must contain 1 to 4 keys")
    normalized: list[dict[str, object]] = []
    seen = {current_key_id}
    for item in keys:
        if not isinstance(item, dict):
            raise RuntimeError("Internal assertion previous JWKS keys must be objects")
        key = dict(item)
        key_id = str(key.get("kid") or "").strip()
        if not key_id or key_id in seen:
            raise RuntimeError("Internal assertion rotation key ids must be present and unique")
        if str(key.get("kty") or "") != "RSA" or _RSA_PRIVATE_JWK_FIELDS & set(key):
            raise RuntimeError("Internal assertion previous JWKS must contain public RSA keys only")
        if str(key.get("alg") or "") not in {"", "RS256"}:
            raise RuntimeError("Internal assertion previous key algorithm must be RS256")
        if str(key.get("use") or "") not in {"", "sig"}:
            raise RuntimeError("Internal assertion previous key use must be sig")
        try:
            public_key = RSAAlgorithm.from_jwk(key)
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError("Internal assertion previous public key is invalid") from exc
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise RuntimeError("Internal assertion previous JWKS must resolve to RSA public keys")
        key.update({"kid": key_id, "use": "sig", "alg": "RS256"})
        seen.add(key_id)
        normalized.append(key)
    return tuple(normalized), expires_at


class InternalAssertionSigner:
    """Mints narrow, short-lived assertions for the private runtime hop."""

    def __init__(
        self,
        *,
        private_key: rsa.RSAPrivateKey,
        key_id: str,
        issuer: str,
        audience: str,
        ttl_seconds: int,
        previous_public_keys: tuple[dict[str, object], ...] = (),
        previous_keys_expire_at: int = 0,
    ) -> None:
        self.private_key = private_key
        self.key_id = key_id
        self.issuer = issuer
        self.audience = audience
        self.ttl_seconds = ttl_seconds
        self.previous_public_keys = previous_public_keys
        self.previous_keys_expire_at = previous_keys_expire_at

    @classmethod
    def from_env(cls) -> "InternalAssertionSigner":
        key_file = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE") or "").strip()
        issuer = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_ISSUER") or "").strip()
        audience = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE") or "").strip()
        key_id = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_KEY_ID") or "").strip()
        if not key_file:
            raise RuntimeError("Signed internal assertion mode requires a dedicated private signing key file")
        if not issuer:
            raise RuntimeError("Signed internal assertion mode requires GLASSHIVE_INTERNAL_ASSERTION_ISSUER")
        if not audience:
            raise RuntimeError("Signed internal assertion mode requires GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE")
        if not key_id:
            raise RuntimeError("Signed internal assertion mode requires GLASSHIVE_INTERNAL_ASSERTION_KEY_ID")
        key_path = Path(key_file)
        try:
            stat_result = key_path.stat()
            key_bytes = key_path.read_bytes()
        except OSError as exc:
            raise RuntimeError("Internal assertion private signing key could not be read") from exc
        if os.name != "nt" and stat_result.st_mode & 0o077:
            raise RuntimeError("Internal assertion private signing key must be owner-only (mode 0600)")
        try:
            private_key = serialization.load_pem_private_key(key_bytes, password=None)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Internal assertion private signing key is invalid") from exc
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise RuntimeError("Internal assertion private signing key must be an RSA key")
        raw_ttl = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_TTL_SECONDS") or "60").strip()
        try:
            ttl_seconds = int(raw_ttl)
        except ValueError as exc:
            raise RuntimeError("GLASSHIVE_INTERNAL_ASSERTION_TTL_SECONDS must be an integer") from exc
        if ttl_seconds < 15 or ttl_seconds > 120:
            raise RuntimeError("GLASSHIVE_INTERNAL_ASSERTION_TTL_SECONDS must be between 15 and 120")
        previous_public_keys, previous_keys_expire_at = _previous_public_keys_from_env(
            current_key_id=key_id,
            now=int(time.time()),
        )
        return cls(
            private_key=private_key,
            key_id=key_id,
            issuer=issuer,
            audience=audience,
            ttl_seconds=ttl_seconds,
            previous_public_keys=previous_public_keys,
            previous_keys_expire_at=previous_keys_expire_at,
        )

    def jwks(self) -> dict[str, object]:
        public_jwk = json.loads(RSAAlgorithm.to_jwk(self.private_key.public_key()))
        public_jwk.update({"kid": self.key_id, "use": "sig", "alg": "RS256"})
        keys = [public_jwk]
        if self.previous_public_keys and int(time.time()) < self.previous_keys_expire_at:
            keys.extend(dict(key) for key in self.previous_public_keys)
        return {"keys": keys}

    def sign(
        self,
        *,
        subject: str,
        tenant_id: str,
        email: str,
        role: str,
        scopes: Iterable[str],
    ) -> str:
        now = int(time.time())
        normalized_scopes = " ".join(dict.fromkeys(scope.strip() for scope in scopes if scope.strip()))
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": subject,
                "tenant_id": tenant_id,
                "email": email,
                "role": role,
                "scope": normalized_scopes,
                "iat": now,
                "nbf": now - 1,
                "exp": now + self.ttl_seconds,
                "jti": uuid.uuid4().hex,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": self.key_id, "typ": "JWT"},
        )
