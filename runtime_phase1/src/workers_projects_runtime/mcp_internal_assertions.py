from __future__ import annotations

import os
import time
import uuid
from functools import lru_cache
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class McpInternalAssertionError(RuntimeError):
    pass


@lru_cache(maxsize=4)
def _private_key(path_value: str, modified_ns: int) -> rsa.RSAPrivateKey:
    _ = modified_ns
    path = Path(path_value)
    try:
        stat_result = path.stat()
        key_bytes = path.read_bytes()
    except OSError as exc:
        raise McpInternalAssertionError("MCP internal assertion signing key could not be read") from exc
    if os.name != "nt" and stat_result.st_mode & 0o077:
        raise McpInternalAssertionError("MCP internal assertion signing key must be owner-only")
    try:
        key = serialization.load_pem_private_key(key_bytes, password=None)
    except (TypeError, ValueError) as exc:
        raise McpInternalAssertionError("MCP internal assertion signing key is invalid") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise McpInternalAssertionError("MCP internal assertion signing key must be RSA")
    return key


def signed_runtime_assertion(
    *,
    subject: str,
    tenant_id: str,
    email: str,
    role: str,
    write: bool,
) -> str:
    key_path = Path(str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE") or "").strip())
    issuer = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_ISSUER") or "").strip()
    audience = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE") or "").strip()
    key_id = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_KEY_ID") or "").strip()
    if not all((str(key_path), issuer, audience, key_id, subject, tenant_id)) or str(key_path) == ".":
        raise McpInternalAssertionError("MCP signed runtime hop is not fully configured")
    try:
        stat_result = key_path.stat()
    except OSError as exc:
        raise McpInternalAssertionError("MCP internal assertion signing key could not be read") from exc
    key = _private_key(str(key_path), stat_result.st_mtime_ns)
    normalized_role = str(role or "member").strip().lower()
    if normalized_role not in {"member", "viewer", "tenant_admin", "service"}:
        normalized_role = "member"
    scopes = ["runtime:access", "workspaces:read"]
    if write and normalized_role != "viewer":
        scopes.append("workspaces:write")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "tenant_id": tenant_id,
            "email": str(email or "").strip(),
            "role": normalized_role,
            "scope": " ".join(scopes),
            "iat": now,
            "nbf": now - 1,
            "exp": now + 60,
            "jti": uuid.uuid4().hex,
        },
        key,
        algorithm="RS256",
        headers={"kid": key_id, "typ": "JWT"},
    )
