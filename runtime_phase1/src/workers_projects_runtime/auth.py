from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


DEFAULT_TENANT_ID = "local"
DEFAULT_AUTH_MODE = "local"
ENTERPRISE_AUTH_MODES = {
    "first_party_assertion",
    "external_oidc",
    "oauth_oidc",
    "oauth_entra",
    "oauth_direct_registration",
    "signed_internal_assertion",
}
IDENTITY_HEADER_ALIASES = {
    "x-viventium-tenant-id": ("x-glasshive-tenant-id", "x-librechat-tenant-id"),
    "x-viventium-user-id": ("x-glasshive-user-id", "x-librechat-user-id"),
    "x-viventium-user-email": ("x-glasshive-user-email", "x-librechat-user-email"),
    "x-viventium-user-role": ("x-glasshive-user-role", "x-librechat-user-role"),
}
OWNER_IDENTITY_CLAIM_NAMES = {"user_id", "email"}
DEFAULT_OWNER_IDENTITY_CLAIMS = ("user_id",)
INTERNAL_ASSERTION_HEADER = "x-glasshive-user-assertion"
INTERNAL_ASSERTION_REQUIRED_SCOPE = "runtime:access"
INTERNAL_ASSERTION_ROLES = {"member", "viewer", "tenant_admin", "service"}


class NativeOwnerUnavailableError(RuntimeError):
    """The installed identity owner needs recovery before provider admission."""


def native_installed_owner_id() -> str | None:
    """Read the installed instance's existing protected first-admin authority."""
    selected = os.environ.get("VIVENTIUM_NATIVE_FIRST_ADMIN_STATE")
    if selected is None:
        return None
    path = Path(selected)
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or path.is_symlink()
            or path.resolve(strict=True) != path
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or multi_user_security_enabled()
        ):
            raise ValueError("unsafe native owner authority")
        with path.open(encoding="utf-8") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ValueError("native owner authority changed")
            value = json.load(handle)
        owner = value.get("admin_user_id")
        if (
            value.get("schema_version") != 1 or value.get("status") != "closed"
            or "token" in value or not isinstance(owner, str)
            or re.fullmatch(r"[a-fA-F0-9]{24}", owner) is None
        ):
            raise ValueError("native owner setup is incomplete")
        return owner
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise NativeOwnerUnavailableError("Installed native owner authority is unavailable; complete owner recovery.") from exc


def require_native_installed_owner(owner_id: object) -> None:
    installed_owner = native_installed_owner_id()
    if installed_owner is not None and owner_id != installed_owner:
        raise GlassHiveAuthError("Only the authenticated installed owner can use the native provider login.")


def _env_bool(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "enabled"}


def multi_user_security_enabled() -> bool:
    """Return whether cross-user isolation must be enforced.

    ``GLASSHIVE_SECURITY_MODE=multi_user`` is the canonical switch.  The
    legacy enterprise flags remain accepted so older deployments do not lose
    their existing protections while migrating.
    """

    security_mode = str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower()
    # An explicit security mode is authoritative.  In particular, a legacy
    # WPR enterprise flag must not silently turn a deliberately local or
    # legacy-compatibility GlassHive process into a different security mode.
    # The flags remain a migration fallback only when the canonical switch is
    # absent.
    if security_mode:
        return security_mode == "multi_user"
    return _env_bool("GLASSHIVE_ENTERPRISE_MODE") or _env_bool("WPR_ENTERPRISE_MODE")


def trusted_service_url_allowed(value: str) -> bool:
    """Allow HTTPS, plus exact loopback HTTP only outside multi-user mode."""

    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        return False
    if parsed.scheme == "https":
        if not multi_user_security_enabled():
            return True
        hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        return address is None or address.is_global
    return (
        not multi_user_security_enabled()
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )


def sanitize_identity_value(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("{{") and text.endswith("}}"):
        return ""
    if text.startswith("${") and text.endswith("}"):
        return ""
    return text[:512]


def header_identity_value(headers: dict[str, str], primary: str) -> str:
    candidates = (primary, *IDENTITY_HEADER_ALIASES.get(primary, ()))
    for name in candidates:
        value = sanitize_identity_value(headers.get(name))
        if value:
            return value
    return ""


def normalize_identity_segment(value: object, fallback: str) -> str:
    text = sanitize_identity_value(value).lower()
    if not text:
        text = fallback
    text = re.sub(r"[^a-z0-9_.@-]+", "-", text).strip(".-")
    return text[:160] or fallback


def _identity_values_match(expected: object, actual: object) -> bool:
    expected_text = sanitize_identity_value(expected)
    actual_text = sanitize_identity_value(actual)
    if not expected_text or not actual_text:
        return False
    if expected_text == actual_text:
        return True
    if "@" in expected_text and "@" in actual_text:
        return expected_text.casefold() == actual_text.casefold()
    return False


def _parse_owner_identity_claims(raw: str, *, strict: bool = False) -> tuple[str, ...]:
    value = str(raw or "").strip()
    if not value:
        return DEFAULT_OWNER_IDENTITY_CLAIMS
    claims: list[str] = []
    invalid: list[str] = []
    for item in re.split(r"[, ]+", value):
        claim = item.strip().lower()
        if not claim:
            continue
        if claim not in OWNER_IDENTITY_CLAIM_NAMES:
            invalid.append(claim)
            continue
        if claim not in claims:
            claims.append(claim)
    if invalid and strict:
        raise ValueError(
            "GLASSHIVE_OWNER_IDENTITY_CLAIMS only supports: "
            + ", ".join(sorted(OWNER_IDENTITY_CLAIM_NAMES))
        )
    return tuple(claims) or DEFAULT_OWNER_IDENTITY_CLAIMS


def owner_identity_claims() -> tuple[str, ...]:
    return _parse_owner_identity_claims(os.environ.get("GLASSHIVE_OWNER_IDENTITY_CLAIMS", ""))


def _owner_identity_aliases_payload(*, strict: bool = False) -> str:
    raw = os.environ.get("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", "").strip()
    if raw:
        return raw
    path = os.environ.get("GLASSHIVE_OWNER_IDENTITY_ALIASES_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        if strict:
            raise ValueError("GLASSHIVE_OWNER_IDENTITY_ALIASES_FILE could not be read") from exc
        return ""


def _parse_owner_identity_aliases(*, strict: bool = False) -> dict[str, tuple[str, ...]]:
    raw = _owner_identity_aliases_payload(strict=strict)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        if strict:
            raise ValueError("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON must be valid JSON") from exc
        return {}
    if not isinstance(parsed, dict):
        if strict:
            raise ValueError("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON must be a JSON object")
        return {}
    aliases: dict[str, tuple[str, ...]] = {}
    for owner, values in parsed.items():
        owner_id = sanitize_identity_value(owner)
        if not owner_id:
            continue
        if isinstance(values, str):
            raw_values = [values]
        elif isinstance(values, list):
            raw_values = values
        else:
            if strict:
                raise ValueError("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON values must be strings or lists")
            continue
        clean_values: list[str] = []
        for value in raw_values:
            clean = sanitize_identity_value(value)
            if clean and clean not in clean_values:
                clean_values.append(clean)
        if clean_values:
            aliases[owner_id] = tuple(clean_values)
    return aliases


def validate_owner_identity_config() -> None:
    _parse_owner_identity_claims(os.environ.get("GLASSHIVE_OWNER_IDENTITY_CLAIMS", ""), strict=True)
    _parse_owner_identity_aliases(strict=True)


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str = DEFAULT_TENANT_ID
    user_id: str = ""
    email: str = ""
    role: str = ""
    scopes: tuple[str, ...] = ()
    auth_mode: str = DEFAULT_AUTH_MODE
    enterprise: bool = False

    @property
    def owner_id(self) -> str:
        return self.user_id

    @property
    def is_user_scoped(self) -> bool:
        return bool(self.user_id and self.auth_mode not in {"", DEFAULT_AUTH_MODE})


def owner_matches_auth_context(owner_id: object, ctx: AuthContext) -> bool:
    owner = sanitize_identity_value(owner_id)
    if not owner:
        return False
    claim_values = {
        "user_id": ctx.user_id,
        "email": ctx.email,
    }
    candidates = [claim_values.get(claim, "") for claim in owner_identity_claims()]
    if any(_identity_values_match(owner, candidate) for candidate in candidates):
        return True
    for canonical_owner, aliases in _parse_owner_identity_aliases().items():
        if not _identity_values_match(owner, canonical_owner):
            continue
        if any(_identity_values_match(alias, candidate) for alias in aliases for candidate in candidates):
            return True
    return False


def canonicalize_auth_context(ctx: AuthContext) -> AuthContext:
    """Resolve an explicitly configured login alias to the durable stored owner id."""

    claim_values = {
        "user_id": ctx.user_id,
        "email": ctx.email,
    }
    candidates = [claim_values.get(claim, "") for claim in owner_identity_claims()]
    for canonical_owner, aliases in _parse_owner_identity_aliases().items():
        if any(
            _identity_values_match(alias, candidate)
            for alias in aliases
            for candidate in candidates
        ):
            return AuthContext(
                tenant_id=ctx.tenant_id,
                user_id=canonical_owner,
                email=ctx.email,
                role=ctx.role,
                scopes=ctx.scopes,
                auth_mode=ctx.auth_mode,
                enterprise=ctx.enterprise,
            )
    return ctx


class GlassHiveAuthError(RuntimeError):
    pass


_RSA_PRIVATE_JWK_FIELDS = {"d", "p", "q", "dp", "dq", "qi", "oth"}


def _rsa_public_verification_key(jwk: dict[str, object]):
    if str(jwk.get("kty") or "").strip() != "RSA":
        raise GlassHiveAuthError("Signed internal assertion key must be RSA")
    if _RSA_PRIVATE_JWK_FIELDS & set(jwk):
        raise GlassHiveAuthError("Signed internal assertion JWKS must contain public keys only")
    if str(jwk.get("alg") or "").strip() not in {"", "RS256"}:
        raise GlassHiveAuthError("Signed internal assertion key algorithm must be RS256")
    if str(jwk.get("use") or "").strip() not in {"", "sig"}:
        raise GlassHiveAuthError("Signed internal assertion key use must be sig")
    key_ops = jwk.get("key_ops")
    if key_ops is not None:
        if not isinstance(key_ops, list):
            raise GlassHiveAuthError("Signed internal assertion key_ops must be a list")
        operations = {str(operation or "").strip() for operation in key_ops}
        if "verify" not in operations or "sign" in operations:
            raise GlassHiveAuthError("Signed internal assertion key may authorize verification only")
    if not str(jwk.get("n") or "").strip() or not str(jwk.get("e") or "").strip():
        raise GlassHiveAuthError("Signed internal assertion RSA key is missing public parameters")
    try:
        key = jwt.PyJWK.from_dict(jwk, algorithm="RS256").key
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise GlassHiveAuthError("Signed internal assertion key is invalid") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise GlassHiveAuthError("Signed internal assertion JWKS must resolve to RSA public keys")
    return key


InternalAssertionReplayConsumer = Callable[..., bool]


class InternalAssertionVerifier:
    """Validate short-lived gateway assertions without trusting browser headers."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        tenant_id: str,
        jwks: dict[str, object] | None,
        jwks_url: str,
        replay_consumer: InternalAssertionReplayConsumer | None = None,
    ):
        self.issuer = issuer
        self.audience = audience
        self.tenant_id = tenant_id
        self.jwks = jwks or {"keys": []}
        self.jwks_url = jwks_url
        self.replay_consumer = replay_consumer
        self._remote_client = jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300) if jwks_url else None

    @classmethod
    def from_env(
        cls,
        *,
        tenant_id: str,
        replay_consumer: InternalAssertionReplayConsumer | None = None,
    ) -> "InternalAssertionVerifier":
        issuer = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_ISSUER") or "").strip()
        audience = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE") or "").strip()
        raw_jwks = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_JWKS_JSON") or "").strip()
        jwks_file = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE") or "").strip()
        jwks_url = str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL") or "").strip()
        if not issuer:
            raise RuntimeError("Signed internal assertion mode requires GLASSHIVE_INTERNAL_ASSERTION_ISSUER")
        if not audience:
            raise RuntimeError("Signed internal assertion mode requires GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE")
        if raw_jwks and jwks_file:
            raise RuntimeError("Configure one local internal assertion JWKS source, not both JSON and file")
        if jwks_url and not trusted_service_url_allowed(jwks_url):
            raise RuntimeError("Internal assertion JWKS URL must use trusted HTTPS")
        if jwks_file:
            try:
                raw_jwks = Path(jwks_file).read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError("Internal assertion JWKS file could not be read") from exc
        jwks: dict[str, object] | None = None
        if raw_jwks:
            try:
                loaded = json.loads(raw_jwks)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Internal assertion JWKS JSON is invalid") from exc
            if not isinstance(loaded, dict):
                raise RuntimeError("Internal assertion JWKS must be a JSON object")
            jwks = loaded
            keys = jwks.get("keys")
            if not isinstance(keys, list) or not keys:
                raise RuntimeError("Signed internal assertion mode requires a non-empty JWKS or JWKS URL")
            seen_key_ids: set[str] = set()
            for item in keys:
                if not isinstance(item, dict):
                    raise RuntimeError("Internal assertion JWKS keys must be JSON objects")
                key_id = sanitize_identity_value(item.get("kid"))
                if not key_id or key_id in seen_key_ids:
                    raise RuntimeError("Internal assertion JWKS key ids must be present and unique")
                seen_key_ids.add(key_id)
                try:
                    _rsa_public_verification_key(item)
                except GlassHiveAuthError as exc:
                    raise RuntimeError(str(exc)) from exc
        if not jwks_url and not (jwks and isinstance(jwks.get("keys"), list) and jwks["keys"]):
            raise RuntimeError("Signed internal assertion mode requires a non-empty JWKS or JWKS URL")
        return cls(
            issuer=issuer,
            audience=audience,
            tenant_id=tenant_id,
            jwks=jwks,
            jwks_url=jwks_url,
            replay_consumer=replay_consumer,
        )

    def _signing_key(self, token: str):
        if self._remote_client is not None:
            try:
                key = self._remote_client.get_signing_key_from_jwt(token).key
            except Exception as exc:
                raise GlassHiveAuthError("Signed internal assertion remote key lookup failed") from exc
            if not isinstance(key, rsa.RSAPublicKey):
                raise GlassHiveAuthError("Signed internal assertion remote JWKS returned a non-public RSA key")
            return key
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise GlassHiveAuthError("Signed internal assertion is malformed") from exc
        kid = sanitize_identity_value(header.get("kid"))
        if not kid:
            raise GlassHiveAuthError("Signed internal assertion is missing a key id")
        for item in self.jwks.get("keys", []):
            if isinstance(item, dict) and sanitize_identity_value(item.get("kid")) == kid:
                return _rsa_public_verification_key(item)
        raise GlassHiveAuthError("Signed internal assertion key id is not trusted")

    def verify(self, token: str) -> AuthContext:
        if not token:
            raise GlassHiveAuthError("Missing signed internal assertion")
        try:
            claims = jwt.decode(
                token,
                self._signing_key(token),
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=5,
                options={
                    "require": ["iss", "aud", "sub", "tenant_id", "role", "scope", "iat", "nbf", "exp", "jti"],
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise GlassHiveAuthError("Signed internal assertion expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise GlassHiveAuthError("Signed internal assertion audience is invalid") from exc
        except jwt.InvalidIssuerError as exc:
            raise GlassHiveAuthError("Signed internal assertion issuer is invalid") from exc
        except jwt.PyJWTError as exc:
            raise GlassHiveAuthError("Signed internal assertion is invalid") from exc
        except GlassHiveAuthError:
            raise
        except Exception as exc:
            raise GlassHiveAuthError("Signed internal assertion verification failed safely") from exc

        subject = sanitize_identity_value(claims.get("sub"))
        tenant_id = sanitize_identity_value(claims.get("tenant_id"))
        role = sanitize_identity_value(claims.get("role"))
        scope_text = sanitize_identity_value(claims.get("scope"))
        scopes = tuple(dict.fromkeys(part for part in scope_text.split() if part))
        jti = str(claims.get("jti") or "").strip()
        try:
            issued_at = int(claims.get("iat"))
            expires_at = int(claims.get("exp"))
        except (TypeError, ValueError) as exc:
            raise GlassHiveAuthError("Signed internal assertion timestamps are invalid") from exc
        if tenant_id != self.tenant_id:
            raise GlassHiveAuthError("Signed internal assertion tenant does not match this deployment")
        if not subject:
            raise GlassHiveAuthError("Signed internal assertion subject is missing")
        if role not in INTERNAL_ASSERTION_ROLES:
            raise GlassHiveAuthError("Signed internal assertion role is invalid")
        if INTERNAL_ASSERTION_REQUIRED_SCOPE not in scopes:
            raise GlassHiveAuthError("Signed internal assertion is missing runtime access scope")
        if expires_at - issued_at > 120:
            raise GlassHiveAuthError("Signed internal assertion lifetime is too long")
        if not jti or len(jti) > 512:
            raise GlassHiveAuthError("Signed internal assertion id is invalid")
        if self.replay_consumer is not None:
            accepted = self.replay_consumer(
                tenant_id=tenant_id,
                issuer=self.issuer,
                jti=jti,
                expires_at=expires_at + 5,
                now=int(time.time()),
            )
            if not accepted:
                raise GlassHiveAuthError("Signed internal assertion was already used")
        return AuthContext(
            tenant_id=tenant_id,
            user_id=subject,
            email=sanitize_identity_value(claims.get("email")),
            role=role,
            scopes=scopes,
            auth_mode="signed_internal_assertion",
            enterprise=True,
        )


class EnterpriseAuthSettings:
    def __init__(self) -> None:
        self.security_mode = str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower()
        if self.security_mode not in {"", "local", "legacy_compatibility", "multi_user"}:
            raise RuntimeError("GLASSHIVE_SECURITY_MODE must be local, legacy_compatibility, or multi_user")
        self.enterprise = (
            self.security_mode == "multi_user"
            or _env_bool("GLASSHIVE_ENTERPRISE_MODE")
            or _env_bool("WPR_ENTERPRISE_MODE")
        )
        configured_auth_mode = str(os.environ.get("GLASSHIVE_AUTH_MODE") or "").strip().lower()
        self.auth_mode = configured_auth_mode or (
            "signed_internal_assertion" if self.security_mode == "multi_user" else DEFAULT_AUTH_MODE
        )
        self.tenant_id = sanitize_identity_value(
            os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID")
            or os.environ.get("WPR_ENTERPRISE_TENANT_ID")
            or DEFAULT_TENANT_ID
        )
        self.user_header = os.environ.get("GLASSHIVE_AUTH_USER_HEADER", "x-viventium-user-id").strip().lower()
        self.email_header = os.environ.get("GLASSHIVE_AUTH_EMAIL_HEADER", "x-viventium-user-email").strip().lower()
        self.role_header = os.environ.get("GLASSHIVE_AUTH_ROLE_HEADER", "x-viventium-user-role").strip().lower()
        self.tenant_header = os.environ.get("GLASSHIVE_AUTH_TENANT_HEADER", "x-viventium-tenant-id").strip().lower()
        self.external_validation_required = _env_bool(
            "GLASSHIVE_AUTH_EXTERNAL_VALIDATION_REQUIRED",
            default=True,
        )
        self.local_human_assertions = _env_bool("GLASSHIVE_LOCAL_HUMAN_ASSERTION")
        self.internal_assertion_verifier: InternalAssertionVerifier | None = None

    def validate_startup(
        self,
        *,
        api_token: str,
        assertion_replay_consumer: InternalAssertionReplayConsumer | None = None,
    ) -> None:
        if self.local_human_assertions:
            if (self.enterprise or self.security_mode not in {"", "local"}
                    or self.auth_mode != "local" or not api_token
                    or str(os.environ.get("GLASSHIVE_HUMAN_AUTH_MODE") or "") != "local_password"
                    or str(os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE") or "").strip()):
                raise RuntimeError("Local human assertions require local password auth, service token, and public-only runtime verification")
            self.internal_assertion_verifier = InternalAssertionVerifier.from_env(
                tenant_id=DEFAULT_TENANT_ID,
                replay_consumer=assertion_replay_consumer,
            )
        if not self.enterprise:
            return
        if self.security_mode == "multi_user" and self.auth_mode != "signed_internal_assertion":
            raise RuntimeError("GLASSHIVE_SECURITY_MODE=multi_user requires GLASSHIVE_AUTH_MODE=signed_internal_assertion")
        if not api_token:
            raise RuntimeError("GLASSHIVE_ENTERPRISE_MODE requires WPR_API_TOKEN for fail-closed service auth")
        signed_link_secret = os.environ.get("GLASSHIVE_SIGNED_LINK_SECRET", "").strip()
        if not signed_link_secret:
            raise RuntimeError(
                "GLASSHIVE_ENTERPRISE_MODE requires GLASSHIVE_SIGNED_LINK_SECRET for scoped takeover and artifact links"
            )
        if signed_link_secret == api_token:
            raise RuntimeError("GLASSHIVE_SIGNED_LINK_SECRET must be distinct from WPR_API_TOKEN")
        if not self.tenant_id or self.tenant_id == DEFAULT_TENANT_ID:
            raise RuntimeError("GLASSHIVE_ENTERPRISE_MODE requires GLASSHIVE_ENTERPRISE_TENANT_ID")
        if self.auth_mode not in ENTERPRISE_AUTH_MODES:
            raise RuntimeError(
                "GLASSHIVE_ENTERPRISE_MODE requires GLASSHIVE_AUTH_MODE to be one of "
                + ", ".join(sorted(ENTERPRISE_AUTH_MODES))
            )
        try:
            validate_owner_identity_config()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if self.auth_mode == "signed_internal_assertion":
            private_key_file = str(
                os.environ.get("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE") or ""
            ).strip()
            if private_key_file:
                key_path = Path(private_key_file)
                if key_path.exists() and os.access(key_path, os.R_OK):
                    raise RuntimeError(
                        "The private internal-assertion signing key must not be readable by the runtime process"
                    )
            self.internal_assertion_verifier = InternalAssertionVerifier.from_env(
                tenant_id=self.tenant_id,
                replay_consumer=assertion_replay_consumer,
            )
            return
        if self.auth_mode != "first_party_assertion" and self.external_validation_required:
            raise RuntimeError(
                "GLASSHIVE_AUTH_MODE values other than first_party_assertion require an external "
                "token validator before the runtime can trust OAuth/OIDC identity assertions"
            )

    def context_from_headers(self, headers: dict[str, str]) -> AuthContext:
        if not self.enterprise:
            return AuthContext()

        if self.auth_mode == "signed_internal_assertion":
            verifier = self.internal_assertion_verifier
            if verifier is None:
                raise GlassHiveAuthError("Signed internal assertion verifier is unavailable")
            assertion = str(headers.get(INTERNAL_ASSERTION_HEADER) or "").strip()
            if len(assertion) > 16_384:
                raise GlassHiveAuthError("Signed internal assertion is too large")
            return canonicalize_auth_context(verifier.verify(assertion))

        asserted_tenant_id = header_identity_value(headers, self.tenant_header)
        tenant_id = self.tenant_id
        if asserted_tenant_id and asserted_tenant_id != tenant_id:
            raise GlassHiveAuthError("Tenant assertion does not match this GlassHive deployment")
        user_id = header_identity_value(headers, self.user_header)
        email = header_identity_value(headers, self.email_header)
        role = header_identity_value(headers, self.role_header)
        if not tenant_id:
            raise GlassHiveAuthError("Missing enterprise tenant assertion")
        if not user_id:
            raise GlassHiveAuthError("Missing authenticated user assertion")
        return canonicalize_auth_context(
            AuthContext(
                tenant_id=tenant_id,
                user_id=user_id,
                email=email,
                role=role,
                auth_mode=self.auth_mode,
                enterprise=True,
            )
        )

    def local_human_context_from_headers(self, headers: dict[str, str]) -> AuthContext | None:
        """Verify an optional owner-only browser assertion on the local service hop."""
        assertion = str(headers.get(INTERNAL_ASSERTION_HEADER) or "").strip()
        if not assertion:
            return None
        verifier = self.internal_assertion_verifier if self.local_human_assertions else None
        if verifier is None:
            raise GlassHiveAuthError("Local human assertion verifier is unavailable")
        if len(assertion) > 16_384:
            raise GlassHiveAuthError("Signed internal assertion is too large")
        verified = verifier.verify(assertion)
        expected_owner = str(
            os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID")
            or os.environ.get("WPR_DEFAULT_OWNER_ID") or ""
        ).strip()
        # The owner's workspace links arrive as the owner with the viewer role; the
        # service middleware keeps viewer requests read-only apart from link messages.
        if (not expected_owner or verified.user_id != expected_owner
                or verified.role not in {"member", "tenant_admin", "viewer"}):
            raise GlassHiveAuthError("Local human assertion owner is unavailable")
        return AuthContext(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=verified.user_id,
            email=verified.email,
            role=verified.role,
            scopes=verified.scopes,
            auth_mode="signed_internal_assertion",
            enterprise=False,
        )


def scoped_alias(ctx: AuthContext, alias: str) -> str:
    clean_alias = normalize_identity_segment(alias, "workspace")
    if not ctx.enterprise:
        return clean_alias
    tenant = normalize_identity_segment(ctx.tenant_id, "tenant")
    user = normalize_identity_segment(ctx.user_id, "user")
    return f"{tenant}--{user}--{clean_alias}"[:240]
