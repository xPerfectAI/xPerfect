from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from .auth import trusted_service_url_allowed


class McpOAuthConfigurationError(RuntimeError):
    pass


HUMAN_ROLES = {"member", "viewer", "tenant_admin", "service"}
_ROLE_RANK = {"viewer": 0, "member": 1, "tenant_admin": 2}


def _least_privileged_role(durable_role: str, token_role: str) -> str:
    """Use both authorities without allowing either one to silently elevate the request."""

    if durable_role == "service" or token_role == "service":
        return "service" if durable_role == token_role == "service" else "viewer"
    durable_rank = _ROLE_RANK.get(durable_role, 0)
    token_rank = _ROLE_RANK.get(token_role, 0)
    effective_rank = min(durable_rank, token_rank)
    return next(role for role, rank in _ROLE_RANK.items() if rank == effective_rank)


def principal_id(issuer: str, subject: str) -> str:
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode("utf-8")).hexdigest()
    return f"usr_{digest[:32]}"


def _oauth_url_allowed(value: str) -> bool:
    return trusted_service_url_allowed(value)


def _boolean_env(name: str, *, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None or not str(raw_value).strip():
        return default
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise McpOAuthConfigurationError(f"{name} must be a boolean")


def _canonical_boolean_env(
    name: str,
    legacy_name: str,
    *,
    default: bool,
) -> bool:
    if str(os.environ.get(name) or "").strip():
        return _boolean_env(name, default=default)
    return _boolean_env(legacy_name, default=default)


def _auth_state_connection(auth_state_path: str) -> sqlite3.Connection:
    raw_path = str(auth_state_path or "").strip()
    if not raw_path:
        raise McpOAuthConfigurationError("Multi-user MCP OAuth requires GLASSHIVE_AUTH_STATE_PATH")
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise McpOAuthConfigurationError("GlassHive authentication state is unavailable") from exc
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("SELECT user_id, disabled_at FROM auth_principals LIMIT 1").fetchone()
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise McpOAuthConfigurationError("GlassHive authentication state is unreadable") from exc


def _enroll_principal_and_check_enabled(
    auth_state_path: str,
    *,
    issuer: str,
    subject: str,
    principal: str,
    email: str,
    role: str,
    allow_registration: bool,
) -> str | None:
    """Enroll an MCP-first OIDC user inside the signer-capable gateway boundary.

    This module is executed by the public MCP gateway service, not by worker/runtime
    processes. The database must already have been initialized by Glass Drive; this
    function never creates or migrates the authentication schema.
    """

    raw_path = str(auth_state_path or "").strip()
    if not raw_path:
        raise McpOAuthConfigurationError("Multi-user MCP OAuth requires GLASSHIVE_AUTH_STATE_PATH")
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise McpOAuthConfigurationError("GlassHive authentication state is unavailable") from exc
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{resolved}?mode=rw", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(auth_principals)").fetchall()
        }
        required_columns = {
            "user_id",
            "issuer",
            "subject",
            "email",
            "display_name",
            "role",
            "disabled_at",
            "created_at",
            "updated_at",
        }
        if not required_columns.issubset(columns):
            raise McpOAuthConfigurationError("GlassHive authentication state is unreadable")
        now = time.time()
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT user_id, disabled_at FROM auth_principals
            WHERE issuer = ? AND subject = ?
            """,
            (issuer, subject),
        ).fetchone()
        if existing is None and not allow_registration:
            connection.rollback()
            return None
        connection.execute(
            """
            INSERT INTO auth_principals
                (user_id, issuer, subject, email, display_name, role, disabled_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, '', ?, NULL, ?, ?)
            ON CONFLICT(issuer, subject) DO UPDATE SET
                email = excluded.email,
                updated_at = excluded.updated_at
            """,
            (principal, issuer, subject, email, role, now, now),
        )
        row = connection.execute(
            """
            SELECT user_id, role, disabled_at FROM auth_principals
            WHERE issuer = ? AND subject = ?
            """,
            (issuer, subject),
        ).fetchone()
        connection.commit()
    except sqlite3.Error as exc:
        raise McpOAuthConfigurationError("GlassHive authentication state is unreadable") from exc
    finally:
        if connection is not None:
            connection.close()
    if row is None or not hmac.compare_digest(str(row["user_id"]), principal):
        raise McpOAuthConfigurationError("GlassHive authentication principal mapping is inconsistent")
    durable_role = str(row["role"] or "").strip()
    if row["disabled_at"] is not None or durable_role not in HUMAN_ROLES:
        return None
    return durable_role


class OidcJwtTokenVerifier:
    """Verifies OIDC/OAuth JWT access tokens without forwarding them downstream."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | tuple[str, ...],
        resource: str = "",
        token_scopes: tuple[str, ...],
        authorization_scopes: tuple[str, ...] = (),
        deployment_tenant_id: str = "",
        token_tenant_id: str = "",
        subject_claim: str = "sub",
        allowed_email_domains: tuple[str, ...] = (),
        email_claim: str = "email",
        email_claim_trusted: bool = False,
        role_claim: str = "roles",
        role_map: dict[str, str] | None = None,
        principal_id_format: str = "hashed_issuer_subject",
        allowed_client_ids: tuple[str, ...] = (),
        client_id_claims: tuple[str, ...] = ("azp", "appid", "client_id"),
        auth_state_path: str = "",
        require_auth_state: bool = False,
        allow_registration: bool = True,
        discovery_ttl_seconds: int = 300,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        raw_audiences = (audience,) if isinstance(audience, str) else audience
        self.audiences = tuple(
            dict.fromkeys(value.strip() for value in raw_audiences if value.strip())
        )
        if not self.audiences:
            raise McpOAuthConfigurationError("MCP OAuth requires at least one token audience")
        self.resource = str(resource or self.audiences[0]).strip()
        self.token_scopes = tuple(
            dict.fromkeys(value.strip() for value in token_scopes if value.strip())
        )
        if not self.token_scopes:
            raise McpOAuthConfigurationError(
                "MCP OAuth requires at least one access-token scope"
            )
        self.authorization_scopes = tuple(
            dict.fromkeys(
                value.strip() for value in authorization_scopes if value.strip()
            )
        )
        exact_authorization_scopes = tuple(
            scope for scope in self.authorization_scopes if scope in self.token_scopes
        )
        canonical_alias_candidates = tuple(
            scope
            for scope in self.authorization_scopes
            if scope not in exact_authorization_scopes
            and scope.startswith(f"{self.resource.rstrip('/')}/")
        )
        exact_aliases = {
            f"{self.resource.rstrip('/')}/{token_scope}": token_scope
            for token_scope in self.token_scopes
        }
        self.authorization_scope_aliases = {
            scope: exact_aliases[scope]
            for scope in canonical_alias_candidates
            if scope in exact_aliases
        }
        if canonical_alias_candidates and len(
            self.authorization_scope_aliases
        ) != len(
            canonical_alias_candidates
        ):
            raise McpOAuthConfigurationError(
                "Every MCP authorization scope must map to a validated access-token scope"
            )
        self.deployment_tenant_id = str(deployment_tenant_id or "").strip()
        self.token_tenant_id = str(token_tenant_id or "").strip()
        self.subject_claim = subject_claim
        self.allowed_email_domains = allowed_email_domains
        self.email_claim = email_claim
        self.email_claim_trusted = email_claim_trusted
        self.role_claim = role_claim
        self.role_map = dict(role_map or {})
        self.principal_id_format = principal_id_format
        self.allowed_client_ids = tuple(dict.fromkeys(value for value in allowed_client_ids if value))
        self.client_id_claims = tuple(dict.fromkeys(value for value in client_id_claims if value))
        self.auth_state_path = str(auth_state_path or "").strip()
        self.require_auth_state = bool(require_auth_state)
        self.allow_registration = bool(allow_registration)
        if self.require_auth_state:
            if not self.auth_state_path:
                raise McpOAuthConfigurationError(
                    "Multi-user MCP OAuth requires GLASSHIVE_AUTH_STATE_PATH"
                )
            if not self.allowed_client_ids:
                raise McpOAuthConfigurationError(
                    "Multi-user MCP OAuth requires at least one allowed client id"
                )
            _auth_state_connection(self.auth_state_path).close()
        self.discovery_ttl_seconds = max(30, min(discovery_ttl_seconds, 3600))
        self._jwks: dict[str, Any] = {}
        self._jwks_uri = ""
        self._loaded_at = 0.0
        self._lock = threading.RLock()

    def _role(self, claims: dict[str, Any]) -> str | None:
        raw_values = claims.get(self.role_claim)
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        mapped = {
            str(self.role_map.get(str(value)) or "").strip()
            for value in values
        }
        for preferred in ("tenant_admin", "service", "viewer", "member"):
            if preferred in mapped and preferred in HUMAN_ROLES:
                return preferred
        # A configured map is an admission boundary, not only a privilege reducer.
        # Missing or unmapped claims must fail closed on both browser and MCP paths.
        return None if self.role_map else "member"

    def _refresh_keys(self, *, force: bool = False) -> None:
        with self._lock:
            if not force and self._jwks and time.time() - self._loaded_at < self.discovery_ttl_seconds:
                return
            discovery_url = f"{self.issuer}/.well-known/openid-configuration"
            discovery_response = httpx.get(discovery_url, timeout=10, follow_redirects=False)
            discovery_response.raise_for_status()
            discovery = discovery_response.json()
            if str(discovery.get("issuer") or "").rstrip("/") != self.issuer:
                raise McpOAuthConfigurationError("OIDC discovery issuer does not match GlassHive MCP configuration")
            jwks_uri = str(discovery.get("jwks_uri") or "").strip()
            if not _oauth_url_allowed(jwks_uri):
                raise McpOAuthConfigurationError("OIDC JWKS URI must use HTTPS")
            jwks_response = httpx.get(jwks_uri, timeout=10, follow_redirects=False)
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
            if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
                raise McpOAuthConfigurationError("OIDC JWKS response is invalid")
            self._jwks_uri = jwks_uri
            self._jwks = jwks
            self._loaded_at = time.time()

    def _key(self, kid: str) -> Any:
        self._refresh_keys()
        for candidate in self._jwks.get("keys", []):
            if isinstance(candidate, dict) and str(candidate.get("kid") or "") == kid:
                return jwt.PyJWK.from_dict(candidate).key
        self._refresh_keys(force=True)
        for candidate in self._jwks.get("keys", []):
            if isinstance(candidate, dict) and str(candidate.get("kid") or "") == kid:
                return jwt.PyJWK.from_dict(candidate).key
        raise jwt.InvalidTokenError("OIDC signing key is not available")

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            if str(header.get("alg") or "") != "RS256" or not str(header.get("kid") or ""):
                return None
            signing_key = await asyncio.to_thread(self._key, str(header["kid"]))
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=list(self.audiences),
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, McpOAuthConfigurationError):
            return None
        upstream_subject = str(claims.get(self.subject_claim) or "").strip()
        if not upstream_subject:
            return None
        client_values: list[str] = []
        for claim_name in self.client_id_claims:
            raw_value = claims.get(claim_name)
            if raw_value in (None, ""):
                continue
            if not isinstance(raw_value, str) or not raw_value.strip():
                return None
            client_values.append(raw_value.strip())
        if len(set(client_values)) != 1:
            return None
        client_id = client_values[0]
        if self.allowed_client_ids and not any(
            hmac.compare_digest(client_id, allowed) for allowed in self.allowed_client_ids
        ):
            return None
        tenant_values: list[str] = []
        for claim_name in ("tenant_id", "tid"):
            raw_tenant = claims.get(claim_name)
            if raw_tenant in (None, ""):
                continue
            if not isinstance(raw_tenant, str) or not raw_tenant.strip():
                return None
            tenant_values.append(raw_tenant.strip())
        if len(set(tenant_values)) > 1:
            return None
        token_tenant = tenant_values[0] if tenant_values else ""
        if self.token_tenant_id and token_tenant != self.token_tenant_id:
            return None
        raw_scopes = claims.get("scope") or claims.get("scp") or ""
        if isinstance(raw_scopes, list):
            scopes = [str(scope) for scope in raw_scopes if str(scope).strip()]
        else:
            scopes = [scope for scope in str(raw_scopes).split() if scope]
        if not set(self.token_scopes).issubset(scopes):
            return None
        email = str(claims.get(self.email_claim) or "").strip()
        if email and claims.get("email_verified") is not True and not self.email_claim_trusted:
            email = ""
        if self.allowed_email_domains:
            if email.count("@") != 1:
                return None
            try:
                email_domain = email.rsplit("@", 1)[1].rstrip(".").encode("idna").decode("ascii").casefold()
            except UnicodeError:
                return None
            if not any(
                email_domain == allowed or email_domain.endswith(f".{allowed}")
                for allowed in self.allowed_email_domains
            ):
                return None
        canonical_principal = (
            upstream_subject
            if self.principal_id_format == "raw_claim"
            else principal_id(self.issuer, upstream_subject)
        )
        role = self._role(claims)
        if role is None:
            return None
        token_role = role
        if self.require_auth_state:
            try:
                durable_role = await asyncio.to_thread(
                    _enroll_principal_and_check_enabled,
                    self.auth_state_path,
                    issuer=self.issuer,
                    subject=upstream_subject,
                    principal=canonical_principal,
                    email=email,
                    role=role,
                    allow_registration=self.allow_registration,
                )
            except McpOAuthConfigurationError:
                return None
            if durable_role is None:
                return None
            role = _least_privileged_role(durable_role, token_role)
        identity_claims = {
            "iss": self.issuer,
            "tenant_id": self.deployment_tenant_id,
            "upstream_tenant_id": token_tenant,
            "email": email,
            "role": role,
            "upstream_subject": upstream_subject,
        }
        projected_scopes = list(scopes)
        projected_scopes.extend(
            authorization_scope
            for authorization_scope, token_scope in self.authorization_scope_aliases.items()
            if token_scope in scopes
        )
        projected_scopes = list(dict.fromkeys(projected_scopes))
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=projected_scopes,
            expires_at=int(claims["exp"]),
            resource=self.resource,
            subject=canonical_principal,
            claims=identity_claims,
        )


def oauth_from_env() -> tuple[OidcJwtTokenVerifier, AuthSettings] | None:
    issuer = str(os.environ.get("GLASSHIVE_MCP_OAUTH_ISSUER") or "").strip().rstrip("/")
    resource_url = str(os.environ.get("GLASSHIVE_MCP_PUBLIC_URL") or "").strip().rstrip("/")
    if not issuer and not resource_url:
        return None
    if not issuer or not resource_url:
        raise McpOAuthConfigurationError(
            "MCP OAuth requires both GLASSHIVE_MCP_OAUTH_ISSUER and GLASSHIVE_MCP_PUBLIC_URL"
        )
    for label, value in (("issuer", issuer), ("resource", resource_url)):
        if not _oauth_url_allowed(value):
            raise McpOAuthConfigurationError(f"MCP OAuth {label} URL must use HTTPS")
    authorization_scopes = tuple(
        dict.fromkeys(
            scope
            for scope in str(os.environ.get("GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES") or "glasshive:access").split()
            if scope
        )
    )
    if not authorization_scopes:
        raise McpOAuthConfigurationError(
            "MCP OAuth requires at least one authorization scope"
        )
    configured_token_scopes = tuple(
        dict.fromkeys(
            scope
            for scope in str(
                os.environ.get("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES") or ""
            ).replace(",", " ").split()
            if scope
        )
    )
    configured_token_audiences = tuple(
        dict.fromkeys(
            value
            for value in str(
                os.environ.get("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES") or ""
            ).replace(",", " ").split()
            if value
        )
    )
    deployment_tenant_id = str(
        os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID")
        or os.environ.get("WPR_ENTERPRISE_TENANT_ID")
        or ""
    ).strip()
    token_tenant_id = str(
        os.environ.get("GLASSHIVE_MCP_OAUTH_TOKEN_TENANT_ID") or ""
    ).strip()
    principal_claim = str(os.environ.get("GLASSHIVE_OIDC_PRINCIPAL_CLAIM") or "").strip()
    legacy_subject_claim = str(os.environ.get("GLASSHIVE_MCP_OAUTH_SUBJECT_CLAIM") or "").strip()
    if principal_claim and legacy_subject_claim and principal_claim != legacy_subject_claim:
        raise McpOAuthConfigurationError("Browser and MCP principal claim configuration must match")
    canonical_subject_claim = principal_claim or legacy_subject_claim
    # Only the canonical mode enables the OAuth 2.1 multi-user admission contract. Legacy
    # enterprise flags retain their separately authenticated compatibility boundary while
    # deployments migrate; they must not be mistaken for a configured OIDC control plane.
    effective_multi_user = (
        str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower()
        == "multi_user"
    )
    if effective_multi_user and not configured_token_scopes:
        raise McpOAuthConfigurationError(
            "Multi-user MCP OAuth requires GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES"
        )
    token_scopes = configured_token_scopes or authorization_scopes
    if effective_multi_user and not configured_token_audiences:
        raise McpOAuthConfigurationError(
            "Multi-user MCP OAuth requires GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES"
        )
    token_audiences = configured_token_audiences or (resource_url,)
    if effective_multi_user and not canonical_subject_claim:
        raise McpOAuthConfigurationError(
            "Multi-user MCP OAuth requires GLASSHIVE_OIDC_PRINCIPAL_CLAIM for canonical UI/MCP ownership"
        )
    allowed_domains: list[str] = []
    for item in str(os.environ.get("GLASSHIVE_ALLOWED_EMAIL_DOMAINS") or "").replace(",", " ").split():
        try:
            normalized = item.strip().rstrip(".").encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise McpOAuthConfigurationError("MCP allowed email domains are invalid") from exc
        if normalized and normalized not in allowed_domains:
            allowed_domains.append(normalized)
    principal_id_format = str(
        os.environ.get("GLASSHIVE_PRINCIPAL_ID_FORMAT") or "hashed_issuer_subject"
    ).strip()
    if principal_id_format not in {"hashed_issuer_subject", "raw_claim"}:
        raise McpOAuthConfigurationError("MCP principal ID format is invalid")
    role_claim = str(os.environ.get("GLASSHIVE_OIDC_ROLE_CLAIM") or "roles").strip()
    try:
        role_map = json.loads(str(os.environ.get("GLASSHIVE_OIDC_ROLE_MAP_JSON") or "{}"))
    except json.JSONDecodeError as exc:
        raise McpOAuthConfigurationError("OIDC role map configuration is invalid") from exc
    if not isinstance(role_map, dict):
        raise McpOAuthConfigurationError("OIDC role map configuration is invalid")
    browser_issuer = str(os.environ.get("GLASSHIVE_OIDC_ISSUER") or "").strip().rstrip("/")
    raw_allowed_client_ids = str(
        os.environ.get("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS") or ""
    ).replace(",", " ")
    allowed_client_ids = tuple(dict.fromkeys(value for value in raw_allowed_client_ids.split() if value))
    client_id_claims = tuple(
        dict.fromkeys(
            value
            for value in str(
                os.environ.get("GLASSHIVE_MCP_OAUTH_CLIENT_ID_CLAIMS")
                or "azp appid client_id"
            ).replace(",", " ").split()
            if value
        )
    )
    if effective_multi_user and principal_id_format != "hashed_issuer_subject":
        raise McpOAuthConfigurationError(
            "Multi-user MCP OAuth requires hashed issuer/subject principal IDs"
        )
    if principal_id_format == "hashed_issuer_subject":
        if browser_issuer and browser_issuer != issuer:
            raise McpOAuthConfigurationError(
                "GlassHive browser and MCP OAuth issuers must match for canonical ownership"
            )
        if effective_multi_user and not browser_issuer:
            raise McpOAuthConfigurationError(
                "Multi-user hashed ownership requires GLASSHIVE_OIDC_ISSUER"
            )
    if effective_multi_user and not allowed_client_ids:
        raise McpOAuthConfigurationError(
            "Multi-user MCP OAuth requires GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS"
        )
    verifier = OidcJwtTokenVerifier(
        issuer=issuer,
        audience=token_audiences,
        resource=resource_url,
        token_scopes=token_scopes,
        authorization_scopes=authorization_scopes,
        deployment_tenant_id=deployment_tenant_id,
        token_tenant_id=token_tenant_id,
        subject_claim=canonical_subject_claim or "sub",
        allowed_email_domains=tuple(allowed_domains),
        email_claim=str(os.environ.get("GLASSHIVE_OIDC_EMAIL_CLAIM") or "email").strip(),
        email_claim_trusted=str(os.environ.get("GLASSHIVE_OIDC_EMAIL_CLAIM_TRUSTED") or "").strip().lower()
        in {"1", "true", "yes", "on", "enabled"},
        role_claim=role_claim,
        role_map={str(key): str(value) for key, value in role_map.items()},
        principal_id_format=principal_id_format,
        allowed_client_ids=allowed_client_ids,
        client_id_claims=client_id_claims,
        auth_state_path=str(os.environ.get("GLASSHIVE_AUTH_STATE_PATH") or "").strip(),
        require_auth_state=effective_multi_user,
        allow_registration=_canonical_boolean_env(
            "GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT",
            "GLASSHIVE_ALLOW_EMAIL_REGISTRATION",
            default=False,
        ),
    )
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(resource_url),
        required_scopes=list(authorization_scopes),
        service_documentation_url=(
            AnyHttpUrl(str(os.environ["GLASSHIVE_MCP_DOCUMENTATION_URL"]).strip())
            if str(os.environ.get("GLASSHIVE_MCP_DOCUMENTATION_URL") or "").strip()
            else None
        ),
    )
    return verifier, settings
