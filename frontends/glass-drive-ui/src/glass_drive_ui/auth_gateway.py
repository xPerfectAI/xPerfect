from __future__ import annotations

import hashlib
import hmac
import base64
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id


HUMAN_AUTH_MODES = {"disabled", "trusted_proxy", "oidc", "local_password"}
HUMAN_ROLES = {"member", "viewer", "tenant_admin", "service"}
LOCAL_PASSWORD_MEMORY_KIB = 19 * 1024
LOCAL_PASSWORD_ITERATIONS = 2
LOCAL_PASSWORD_LANES = 1
LOCAL_PASSWORD_LENGTH = 32
LOCAL_PASSWORD_MIN_CHARACTERS = 24
LOCAL_PASSWORD_MIN_UNIQUE_CHARACTERS = 12
LOCAL_PASSWORD_MAX_CHARACTERS = 128
LOCAL_ACCOUNT_MAX_FAILURES = 5
LOCAL_SOURCE_MAX_FAILURES = 20
LOCAL_FAILURE_WINDOW_SECONDS = 15 * 60
LOCAL_LOCK_BASE_SECONDS = 30
LOCAL_LOCK_MAX_SECONDS = 15 * 60
LOCAL_SOURCE_LOCK_SECONDS = 5 * 60
LOCAL_THROTTLE_RETENTION_SECONDS = 24 * 60 * 60
LOCAL_THROTTLE_MAX_SOURCES = 10_000
LOCAL_KDF_MAX_CONCURRENCY = 4
_PASSWORD_KDF_SLOTS = threading.BoundedSemaphore(LOCAL_KDF_MAX_CONCURRENCY)


class AuthGatewayError(RuntimeError):
    def __init__(self, message: str, *, code: str = "sign_in_failed") -> None:
        super().__init__(message)
        self.code = code


class _PasswordKdfBusy(RuntimeError):
    pass


def _env_bool(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "enabled"}


def _canonical_env_bool(name: str, legacy_name: str, default: bool = False) -> bool:
    if str(os.environ.get(name) or "").strip():
        return _env_bool(name, default)
    return _env_bool(legacy_name, default)


def _configured_domains(name: str) -> tuple[str, ...]:
    domains: list[str] = []
    for item in str(os.environ.get(name) or "").replace(",", " ").split():
        try:
            normalized = item.strip().rstrip(".").encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise RuntimeError(f"{name} contains an invalid domain") from exc
        if normalized and normalized not in domains:
            domains.append(normalized)
    return tuple(domains)


def _default_state_path() -> Path:
    configured = str(os.environ.get("GLASSHIVE_AUTH_STATE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GlassHive" / "auth.sqlite3"
    state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
    return (Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state") / "glasshive" / "auth.sqlite3"


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _principal_id(issuer: str, subject: str) -> str:
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode("utf-8")).hexdigest()
    return f"usr_{digest[:32]}"


def _normalize_subject(value: object) -> str:
    if not isinstance(value, str):
        raise AuthGatewayError("Enter the provider's exact stable subject")
    normalized = value.strip()
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuthGatewayError("Enter the provider's exact stable subject") from exc
    if (
        not normalized
        or len(normalized) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise AuthGatewayError("Enter the provider's exact stable subject")
    return normalized


def _normalize_display_name(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise AuthGatewayError("Enter a valid display name")
    normalized = unicodedata.normalize("NFC", value).strip()
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuthGatewayError("Enter a valid display name") from exc
    if (
        len(normalized) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise AuthGatewayError("Enter a valid display name")
    return normalized


def _normalize_email(value: object) -> str:
    if not isinstance(value, str):
        raise AuthGatewayError("Enter a valid email address")
    text = value.strip()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuthGatewayError("Enter a valid email address") from exc
    if len(text) > 320 or text.count("@") != 1 or any(character.isspace() for character in text):
        raise AuthGatewayError("Enter a valid email address")
    local, domain = text.rsplit("@", 1)
    if not local or not domain or len(local) > 64:
        raise AuthGatewayError("Enter a valid email address")
    try:
        ascii_domain = domain.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise AuthGatewayError("Enter a valid email address") from exc
    if not ascii_domain or "." not in ascii_domain:
        raise AuthGatewayError("Enter a valid email address")
    return f"{local.casefold()}@{ascii_domain}"


def _normalized_password(value: object, *, provisioning: bool) -> str:
    if not isinstance(value, str):
        raise AuthGatewayError("Enter a valid password")
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized_bytes = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuthGatewayError("Enter a valid password") from exc
    if (
        not normalized
        or len(normalized) > LOCAL_PASSWORD_MAX_CHARACTERS
        or len(normalized_bytes) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise AuthGatewayError("Enter a valid password")
    if provisioning and (
        len(normalized) < LOCAL_PASSWORD_MIN_CHARACTERS
        or len(set(normalized.casefold())) < LOCAL_PASSWORD_MIN_UNIQUE_CHARACTERS
    ):
        raise AuthGatewayError(
            f"Password must be at least {LOCAL_PASSWORD_MIN_CHARACTERS} characters with at least "
            f"{LOCAL_PASSWORD_MIN_UNIQUE_CHARACTERS} distinct characters"
        )
    return normalized


def _password_phc(password: str) -> str:
    if not _PASSWORD_KDF_SLOTS.acquire(timeout=5.0):
        raise AuthGatewayError("Password service is busy; retry shortly")
    try:
        return Argon2id(
            salt=os.urandom(16),
            length=LOCAL_PASSWORD_LENGTH,
            iterations=LOCAL_PASSWORD_ITERATIONS,
            lanes=LOCAL_PASSWORD_LANES,
            memory_cost=LOCAL_PASSWORD_MEMORY_KIB,
        ).derive_phc_encoded(password.encode("utf-8"))
    finally:
        _PASSWORD_KDF_SLOTS.release()


def _password_matches(password: str, password_phc: str) -> bool:
    if not _PASSWORD_KDF_SLOTS.acquire(blocking=False):
        raise _PasswordKdfBusy("password verifier capacity is busy")
    try:
        Argon2id.verify_phc_encoded(password.encode("utf-8"), password_phc)
    except (InvalidKey, ValueError):
        return False
    finally:
        _PASSWORD_KDF_SLOTS.release()
    return True


def _local_password_phc_valid(value: str) -> bool:
    # Local packages only accept the bounded KDF profile they provision. Reject
    # corrupt/hostile PHC parameters before a login can allocate their resources.
    prefix = (f"$argon2id$v=19$m={LOCAL_PASSWORD_MEMORY_KIB},"
              f"t={LOCAL_PASSWORD_ITERATIONS},p={LOCAL_PASSWORD_LANES}$")
    if not value.startswith(prefix):
        return False
    try:
        salt, digest = value[len(prefix):].split("$")
        decoded = [base64.b64decode(part + "=" * (-len(part) % 4), validate=True)
                   for part in (salt, digest)]
        return len(decoded[0]) == 16 and len(decoded[1]) == LOCAL_PASSWORD_LENGTH
    except (ValueError, TypeError):
        return False


def _password_phc_needs_rehash(password_phc: str) -> bool:
    return not (
        password_phc.startswith("$argon2id$v=19$")
        and f"m={LOCAL_PASSWORD_MEMORY_KIB},t={LOCAL_PASSWORD_ITERATIONS},p={LOCAL_PASSWORD_LANES}$"
        in password_phc
    )


_DUMMY_PASSWORD_PHC = _password_phc("synthetic unavailable credential")


def _secure_oidc_url(value: str, *, allow_loopback_http: bool) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        _ = parsed.port
    except ValueError:
        return False
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return False
    if parsed.scheme == "https":
        if allow_loopback_http:
            return True
        hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                # IPv4 shorthand such as 127.1 or 2130706433 is still an address.
                address = ipaddress.ip_address(socket.inet_aton(hostname))
            except OSError:
                return True
        return address.is_global
    return bool(
        allow_loopback_http
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )


class HumanAuthGateway:
    def __init__(
        self,
        *,
        mode: str,
        state_path: Path,
        allowed_email_domains: tuple[str, ...],
        oidc_issuer: str,
        oidc_client_id: str,
        oidc_redirect_uri: str,
        oidc_client_secret: str,
        session_ttl_seconds: int,
    ) -> None:
        self.mode = mode
        self.state_path = state_path
        self.allowed_email_domains = allowed_email_domains
        self.oidc_issuer = oidc_issuer
        self.oidc_client_id = oidc_client_id
        self.oidc_redirect_uri = oidc_redirect_uri
        self.oidc_client_secret = oidc_client_secret
        self.oidc_principal_claim = str(
            os.environ.get("GLASSHIVE_OIDC_PRINCIPAL_CLAIM") or "sub"
        ).strip()
        self.oidc_email_claim = str(os.environ.get("GLASSHIVE_OIDC_EMAIL_CLAIM") or "email").strip()
        self.oidc_email_claim_trusted = _env_bool("GLASSHIVE_OIDC_EMAIL_CLAIM_TRUSTED")
        self.allow_registration = _canonical_env_bool(
            "GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT",
            "GLASSHIVE_ALLOW_EMAIL_REGISTRATION",
        )
        self.provider_email_login = _canonical_env_bool(
            "GLASSHIVE_PROVIDER_EMAIL_LOGIN",
            "GLASSHIVE_ALLOW_EMAIL_LOGIN",
        )
        self.local_password_login = self.mode == "local_password" or _env_bool("GLASSHIVE_LOCAL_PASSWORD_LOGIN")
        self.local_owner_id = str(os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID") or "").strip()
        self.local_namespace = str(os.environ.get("GLASSHIVE_LOCAL_AUTH_NAMESPACE") or "").strip()
        self.password_namespace = self.oidc_issuer
        if self.mode == "local_password":
            if (str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "local") != "local"
                    or not self.local_owner_id or not self.local_namespace
                    or len(self.local_owner_id) > 512 or len(self.local_namespace) > 128
                    or any(ord(char) < 32 for char in self.local_owner_id + self.local_namespace)
                    or _env_bool("GLASSHIVE_TRUST_INBOUND_IDENTITY")):
                raise RuntimeError("Local password auth requires one fixed local owner and deployment namespace")
            self.password_namespace = "local-password:" + self.local_namespace
            self.allow_registration = False
            self.provider_email_login = False
        self.oidc_login_visible = _env_bool("GLASSHIVE_OIDC_LOGIN_VISIBLE", True)
        if self.mode == "oidc" and not self.oidc_login_visible and not self.local_password_login:
            raise RuntimeError("GlassHive requires at least one browser login method")
        self.local_allowed_email_domains = _configured_domains(
            "GLASSHIVE_LOCAL_AUTH_ALLOWED_EMAIL_DOMAINS"
        )
        throttle_key = str(
            os.environ.get("GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY") or ""
        ).encode("utf-8")
        if self.local_password_login and len(throttle_key) < 32:
            raise RuntimeError(
                "Local password login requires GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY with at least 32 bytes"
            )
        self.local_auth_throttle_key = throttle_key
        self.oidc_post_logout_redirect_uri = str(
            os.environ.get("GLASSHIVE_OIDC_POST_LOGOUT_REDIRECT_URI") or ""
        ).strip()
        self.session_ttl_seconds = session_ttl_seconds
        if self.session_enabled:
            self._initialize()

    @classmethod
    def from_env(cls) -> "HumanAuthGateway":
        mode = str(os.environ.get("GLASSHIVE_HUMAN_AUTH_MODE") or "disabled").strip().lower()
        if mode in {"email", "hybrid"}:
            raise RuntimeError(
                "GlassHive email/password auth must be provided by Viventium/LibreChat or another external identity provider"
            )
        if mode not in HUMAN_AUTH_MODES:
            raise RuntimeError("GLASSHIVE_HUMAN_AUTH_MODE must be disabled, trusted_proxy, oidc, or local_password")
        domains = list(_configured_domains("GLASSHIVE_ALLOWED_EMAIL_DOMAINS"))
        oidc_issuer = str(os.environ.get("GLASSHIVE_OIDC_ISSUER") or "").strip().rstrip("/")
        oidc_client_id = str(os.environ.get("GLASSHIVE_OIDC_CLIENT_ID") or "").strip()
        oidc_redirect_uri = str(os.environ.get("GLASSHIVE_OIDC_REDIRECT_URI") or "").strip()
        oidc_client_secret = str(os.environ.get("GLASSHIVE_OIDC_CLIENT_SECRET") or "").strip()
        if mode == "oidc" and not all((oidc_issuer, oidc_client_id, oidc_redirect_uri)):
            raise RuntimeError("OIDC human auth requires issuer, client ID, and redirect URI")
        if mode == "oidc":
            multi_user = str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user"
            if not _secure_oidc_url(oidc_issuer, allow_loopback_http=not multi_user):
                raise RuntimeError("OIDC issuer must use HTTPS")
            if not _secure_oidc_url(oidc_redirect_uri, allow_loopback_http=not multi_user):
                raise RuntimeError("OIDC redirect URI must use HTTPS")
            post_logout_redirect_uri = str(
                os.environ.get("GLASSHIVE_OIDC_POST_LOGOUT_REDIRECT_URI") or ""
            ).strip()
            if post_logout_redirect_uri and not _secure_oidc_url(
                post_logout_redirect_uri,
                allow_loopback_http=not multi_user,
            ):
                raise RuntimeError("OIDC post-logout redirect URI must use HTTPS")
            principal_claim = str(os.environ.get("GLASSHIVE_OIDC_PRINCIPAL_CLAIM") or "").strip()
            if multi_user and not principal_claim:
                raise RuntimeError(
                    "Multi-user OIDC requires GLASSHIVE_OIDC_PRINCIPAL_CLAIM so browser and MCP ownership stay identical"
                )
            legacy_mcp_claim = str(os.environ.get("GLASSHIVE_MCP_OAUTH_SUBJECT_CLAIM") or "").strip()
            if principal_claim and legacy_mcp_claim and principal_claim != legacy_mcp_claim:
                raise RuntimeError("Browser and MCP principal claim configuration must match")
        try:
            session_ttl_seconds = int(str(os.environ.get("GLASSHIVE_AUTH_SESSION_TTL_SECONDS") or "43200"))
        except ValueError as exc:
            raise RuntimeError("GlassHive auth limits must be integers") from exc
        if session_ttl_seconds < 300 or session_ttl_seconds > 30 * 24 * 60 * 60:
            raise RuntimeError("GLASSHIVE_AUTH_SESSION_TTL_SECONDS is outside the supported range")
        return cls(
            mode=mode,
            state_path=_default_state_path(),
            allowed_email_domains=tuple(domains),
            oidc_issuer=oidc_issuer,
            oidc_client_id=oidc_client_id,
            oidc_redirect_uri=oidc_redirect_uri,
            oidc_client_secret=oidc_client_secret,
            session_ttl_seconds=session_ttl_seconds,
        )

    @property
    def session_enabled(self) -> bool:
        return self.mode in {"oidc", "local_password"}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if os.name != "nt":
            for path in (
                self.state_path,
                Path(f"{self.state_path}-wal"),
                Path(f"{self.state_path}-shm"),
            ):
                try:
                    path.chmod(0o600)
                except FileNotFoundError:
                    # SQLite may unlink transient WAL/SHM files between lookup and chmod.
                    continue
        return connection

    def _initialize(self) -> None:
        if self.mode == "local_password" and (self.state_path.exists() or self.state_path.is_symlink()):
            info = self.state_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise RuntimeError("Local authentication state must be an owner-only regular file")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.state_path.parent.chmod(0o700)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_principals (
                    user_id TEXT PRIMARY KEY,
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'member',
                    disabled_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(issuer, subject)
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    session_hash TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL REFERENCES auth_principals(user_id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_principal ON auth_sessions(principal_id, expires_at);
                CREATE TABLE IF NOT EXISTS auth_oidc_flows (
                    state_hash TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    verifier TEXT NOT NULL,
                    return_to TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_local_credentials (
                    principal_id TEXT PRIMARY KEY REFERENCES auth_principals(user_id) ON DELETE CASCADE,
                    login_email TEXT NOT NULL UNIQUE,
                    password_phc TEXT NOT NULL,
                    password_version INTEGER NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL,
                    last_failed_at REAL,
                    disabled_at REAL,
                    password_changed_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_local_source_attempts (
                    source_hash TEXT PRIMARY KEY,
                    failed_attempts INTEGER NOT NULL,
                    window_started REAL NOT NULL,
                    locked_until REAL,
                    last_failed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_local_sessions (
                    session_hash TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL REFERENCES auth_principals(user_id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_auth_local_sessions_principal
                    ON auth_local_sessions(principal_id, expires_at);
                """
            )
            if not self.local_password_login:
                now = time.time()
                conn.execute(
                    """
                    UPDATE auth_local_sessions SET revoked_at = ?
                    WHERE revoked_at IS NULL
                    """,
                    (now,),
                )
        if os.name != "nt" and self.state_path.exists():
            self.state_path.chmod(0o600)
        if os.name != "nt":
            self.state_path.chmod(0o600)

    def _domain_allowed(self, email: str) -> bool:
        domain = email.rsplit("@", 1)[1]
        return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in self.allowed_email_domains)

    def email_allowed(self, email: str) -> bool:
        if not self.allowed_email_domains:
            return True
        try:
            normalized = _normalize_email(email)
        except AuthGatewayError:
            return False
        return self._domain_allowed(normalized)

    def _principal_payload(self, row: sqlite3.Row) -> dict[str, str]:
        return {
            "tenant_id": str(os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID") or "local").strip(),
            "user_id": str(row["user_id"]),
            "email": str(row["email"] or ""),
            "display_name": str(row["display_name"] or ""),
            "role": str(row["role"] or "member"),
        }

    def upsert_oidc_principal(
        self,
        *,
        issuer: str,
        subject: str,
        email: str,
        display_name: str,
        role: str,
    ) -> dict[str, str]:
        issuer = str(issuer or "").strip().rstrip("/")
        subject = _normalize_subject(subject)
        if not issuer:
            raise AuthGatewayError("OIDC identity is missing issuer or subject")
        normalized_email = _normalize_email(email) if email else ""
        normalized_display_name = _normalize_display_name(display_name)
        normalized_role = role if role in HUMAN_ROLES else "member"
        user_id = _principal_id(issuer, subject)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_principals
                    (user_id, issuer, subject, email, display_name, role, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer, subject) DO UPDATE SET
                    email = excluded.email,
                    display_name = excluded.display_name,
                    role = excluded.role,
                    updated_at = excluded.updated_at
                """,
                (user_id, issuer, subject, normalized_email, normalized_display_name, normalized_role, now, now),
            )
            row = conn.execute(
                "SELECT * FROM auth_principals WHERE issuer = ? AND subject = ?",
                (issuer, subject),
            ).fetchone()
        assert row is not None
        return self._principal_payload(row)

    def preapprove_oidc_principal(
        self,
        *,
        subject: str,
        email: str = "",
        display_name: str = "",
        role: str = "member",
    ) -> dict[str, str]:
        """Idempotently admit one exact external identity without opening enrollment.

        The immutable provider subject is the only ownership key. Email remains
        display metadata and is deliberately never used to find or merge users.
        """
        normalized_subject = _normalize_subject(subject)
        normalized_role = str(role or "").strip()
        if normalized_role not in HUMAN_ROLES:
            raise AuthGatewayError("Choose an approved GlassHive role")
        if self.mode != "oidc" or not self.oidc_issuer:
            raise AuthGatewayError("OIDC issuer is unavailable", code="provider_configuration")
        existing = self.find_oidc_principal(issuer=self.oidc_issuer, subject=normalized_subject)
        if existing is not None and self._principal_disabled(
            issuer=self.oidc_issuer,
            subject=normalized_subject,
        ):
            raise AuthGatewayError(
                "Account is disabled; use the approved re-enable workflow",
                code="account_disabled",
            )
        principal = self.upsert_oidc_principal(
            issuer=self.oidc_issuer,
            subject=normalized_subject,
            email=email,
            display_name=display_name,
            role=normalized_role,
        )
        if self._principal_disabled(issuer=self.oidc_issuer, subject=normalized_subject):
            raise AuthGatewayError(
                "Account is disabled; use the approved re-enable workflow",
                code="account_disabled",
            )
        return principal

    def _principal_disabled(self, *, issuer: str, subject: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT disabled_at FROM auth_principals WHERE issuer = ? AND subject = ?",
                (str(issuer).strip().rstrip("/"), str(subject).strip()),
            ).fetchone()
        return row is not None and row["disabled_at"] is not None

    def find_oidc_principal(self, *, issuer: str, subject: str) -> dict[str, str] | None:
        normalized_issuer = str(issuer or "").strip().rstrip("/")
        try:
            normalized_subject = _normalize_subject(subject)
        except AuthGatewayError:
            return None
        if not normalized_issuer:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM auth_principals WHERE issuer = ? AND subject = ?",
                (normalized_issuer, normalized_subject),
            ).fetchone()
        return self._principal_payload(row) if row is not None else None

    def reconcile_oidc_principal(
        self,
        *,
        issuer: str,
        subject: str,
        email: str,
        display_name: str,
        role: str,
    ) -> dict[str, str]:
        existing = self.find_oidc_principal(issuer=issuer, subject=subject)
        if existing is None and not self.allow_registration:
            raise AuthGatewayError(
                "This account has not been approved for GlassHive",
                code="account_not_registered",
            )
        return self.upsert_oidc_principal(
            issuer=issuer,
            subject=subject,
            email=email,
            display_name=display_name,
            role=role,
        )

    @staticmethod
    def _oidc_role_map_configured() -> bool:
        try:
            role_map = json.loads(str(os.environ.get("GLASSHIVE_OIDC_ROLE_MAP_JSON") or "{}"))
        except json.JSONDecodeError as exc:
            raise AuthGatewayError("OIDC role map configuration is invalid") from exc
        return bool(role_map) if isinstance(role_map, dict) else True

    def _create_session_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        principal_id: str,
        auth_method: str,
    ) -> dict[str, Any]:
        if auth_method not in {"oidc", "local_password"}:
            raise AuthGatewayError("Unsupported authentication method")
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + self.session_ttl_seconds
        row = conn.execute(
            "SELECT user_id FROM auth_principals WHERE user_id = ? AND disabled_at IS NULL",
            (principal_id,),
        ).fetchone()
        if row is None:
            raise AuthGatewayError("Account is unavailable")
        session_table = "auth_local_sessions" if auth_method == "local_password" else "auth_sessions"
        conn.execute(
            f"""
            INSERT INTO {session_table}
                (session_hash, principal_id, csrf_hash, expires_at, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _hash_secret(token),
                principal_id,
                _hash_secret(csrf_token),
                expires_at,
                now,
                now,
            ),
        )
        return {
            "token": token,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
            "auth_method": auth_method,
        }

    def create_session(
        self,
        principal_id: str,
        *,
        auth_method: str = "oidc",
    ) -> dict[str, Any]:
        with self._connect() as conn:
            return self._create_session_in_connection(
                conn,
                principal_id=principal_id,
                auth_method=auth_method,
            )

    def resolve_session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, s.csrf_hash, s.expires_at, 'oidc' AS auth_method
                FROM auth_sessions s
                JOIN auth_principals p ON p.user_id = s.principal_id
                WHERE s.session_hash = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND p.disabled_at IS NULL AND ? != 'local_password'
                """,
                (_hash_secret(token), now, self.mode),
            ).fetchone()
            session_table = "auth_sessions"
            if row is None and self.local_password_login:
                row = conn.execute(
                    """
                    SELECT p.*, s.csrf_hash, s.expires_at, 'local_password' AS auth_method
                    FROM auth_local_sessions s
                    JOIN auth_principals p ON p.user_id = s.principal_id
                    WHERE s.session_hash = ?
                      AND s.revoked_at IS NULL
                      AND s.expires_at > ?
                      AND p.disabled_at IS NULL
                      AND p.issuer = ?
                    """,
                    (_hash_secret(token), now, self.password_namespace),
                ).fetchone()
                session_table = "auth_local_sessions"
            if row is None:
                return None
            conn.execute(
                f"UPDATE {session_table} SET last_seen_at = ? WHERE session_hash = ?",
                (now, _hash_secret(token)),
            )
        payload: dict[str, Any] = self._principal_payload(row)
        payload["expires_at"] = float(row["expires_at"])
        payload["_csrf_hash"] = str(row["csrf_hash"])
        payload["auth_method"] = str(row["auth_method"] or "oidc")
        payload["auth_source"] = "session"
        return payload

    def session_csrf_valid(self, session: dict[str, Any], token: str) -> bool:
        expected = str(session.get("_csrf_hash") or "")
        return bool(expected and token and hmac.compare_digest(expected, _hash_secret(token)))

    def revoke_session(self, token: str) -> None:
        if not token:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                (time.time(), _hash_secret(token)),
            )
            conn.execute(
                "UPDATE auth_local_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                (time.time(), _hash_secret(token)),
            )

    def revoke_local_sessions(self, *, principal_id: str = "") -> int:
        now = time.time()
        query = "UPDATE auth_local_sessions SET revoked_at = ? WHERE revoked_at IS NULL"
        parameters: tuple[object, ...] = (now,)
        if principal_id:
            query += " AND principal_id = ?"
            parameters = (now, str(principal_id).strip())
        with self._connect() as conn:
            cursor = conn.execute(query, parameters)
        return max(0, int(cursor.rowcount))

    LOCAL_LOGIN = "local-owner@xperfect.invalid"

    def provision_local_owner(self, *, password: str) -> dict[str, str]:
        """Private admin entry: preserve the configured owner, never merge identities."""
        if self.mode != "local_password":
            raise AuthGatewayError("Local owner provisioning requires local_password mode")
        _normalized_password(password, provisioning=True)
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM auth_principals").fetchall()
            if rows and (len(rows) != 1 or rows[0]["user_id"] != self.local_owner_id
                    or rows[0]["issuer"] != self.password_namespace or rows[0]["subject"] != "owner"
                    or rows[0]["disabled_at"] is not None or rows[0]["role"] != "tenant_admin"):
                raise AuthGatewayError("Existing identity requires an explicit owner migration")
            conn.execute("""INSERT OR IGNORE INTO auth_principals
              (user_id,issuer,subject,email,display_name,role,created_at,updated_at)
              VALUES(?,?,?,'','Local owner','tenant_admin',?,?)""",
              (self.local_owner_id, self.password_namespace, "owner", now, now))
        return self.provision_local_password(subject="owner", login_email=self.LOCAL_LOGIN, password=password)

    def validate_local_owner(self) -> None:
        if self.mode != "local_password":
            return
        with self._connect() as conn:
            rows = conn.execute("""SELECT p.*,c.password_phc,c.login_email,c.disabled_at AS credential_disabled
              FROM auth_principals p LEFT JOIN auth_local_credentials c ON c.principal_id=p.user_id""").fetchall()
        if (len(rows) != 1 or rows[0]["user_id"] != self.local_owner_id
                or rows[0]["issuer"] != self.password_namespace or rows[0]["subject"] != "owner"
                or rows[0]["disabled_at"] is not None or rows[0]["credential_disabled"] is not None
                or rows[0]["role"] != "tenant_admin"
                or rows[0]["login_email"] != self.LOCAL_LOGIN or not rows[0]["password_phc"]
                or not _local_password_phc_valid(str(rows[0]["password_phc"]))):
            raise RuntimeError("Local owner authentication is not safely provisioned")

    def _local_principal_row(
        self,
        conn: sqlite3.Connection,
        *,
        subject: str,
    ) -> sqlite3.Row:
        normalized_subject = _normalize_subject(subject)
        row = conn.execute(
            """
            SELECT * FROM auth_principals
            WHERE issuer = ? AND subject = ? AND disabled_at IS NULL
            """,
            (self.password_namespace, normalized_subject),
        ).fetchone()
        if row is None:
            raise AuthGatewayError(
                "The exact provider subject must be preapproved before adding a local password"
            )
        return row

    def provision_local_password(
        self,
        *,
        subject: str,
        login_email: str,
        password: str,
    ) -> dict[str, str]:
        if self.mode not in {"oidc", "local_password"} or not self.password_namespace:
            raise AuthGatewayError("Local password login requires its configured identity namespace")
        normalized_subject = _normalize_subject(subject)
        normalized_email = _normalize_email(login_email)
        if self.mode == "local_password" and (normalized_subject != "owner" or normalized_email != self.LOCAL_LOGIN):
            raise AuthGatewayError("Local package credentials must retain the fixed owner")
        if self.local_allowed_email_domains:
            domain = normalized_email.rsplit("@", 1)[1]
            if not any(
                domain == allowed or domain.endswith(f".{allowed}")
                for allowed in self.local_allowed_email_domains
            ):
                raise AuthGatewayError("Local login email domain is not approved")
        normalized_password = _normalized_password(password, provisioning=True)
        password_phc = _password_phc(normalized_password)
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                principal = self._local_principal_row(conn, subject=normalized_subject)
                conn.execute(
                    """
                    INSERT INTO auth_local_credentials (
                        principal_id, login_email, password_phc, password_version,
                        failed_attempts, locked_until, last_failed_at, disabled_at,
                        password_changed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 0, NULL, NULL, NULL, ?, ?, ?)
                    ON CONFLICT(principal_id) DO UPDATE SET
                        login_email = excluded.login_email,
                        password_phc = excluded.password_phc,
                        password_version = auth_local_credentials.password_version + 1,
                        failed_attempts = 0,
                        locked_until = NULL,
                        last_failed_at = NULL,
                        password_changed_at = excluded.password_changed_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(principal["user_id"]),
                        normalized_email,
                        password_phc,
                        now,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE auth_local_sessions SET revoked_at = ?
                    WHERE principal_id = ? AND revoked_at IS NULL
                    """,
                    (now, str(principal["user_id"])),
                )
                payload = self._principal_payload(principal)
        except sqlite3.IntegrityError as exc:
            raise AuthGatewayError("That local login email is already assigned") from exc
        return payload

    @staticmethod
    def _local_failure() -> AuthGatewayError:
        return AuthGatewayError(
            "Email or password is incorrect",
            code="sign_in_failed",
        )

    def _source_key(self, source: object) -> str:
        normalized_source = str(source or "unknown").strip()[:256] or "unknown"
        return hmac.new(
            self.local_auth_throttle_key,
            normalized_source.encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _throttle_locked(row: sqlite3.Row | None, now: float) -> bool:
        return bool(
            row is not None
            and row["locked_until"] is not None
            and float(row["locked_until"]) > now
        )

    def _record_source_failure(
        self,
        conn: sqlite3.Connection,
        *,
        source_hash: str,
        now: float,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM auth_local_source_attempts WHERE source_hash = ?",
            (source_hash,),
        ).fetchone()
        if row is None or now - float(row["window_started"]) > LOCAL_FAILURE_WINDOW_SECONDS:
            failures = 1
            window_started = now
        else:
            failures = int(row["failed_attempts"]) + 1
            window_started = float(row["window_started"])
        locked_until = (
            now + LOCAL_SOURCE_LOCK_SECONDS
            if failures >= LOCAL_SOURCE_MAX_FAILURES
            else None
        )
        conn.execute(
            """
            INSERT INTO auth_local_source_attempts
                (source_hash, failed_attempts, window_started, locked_until, last_failed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_hash) DO UPDATE SET
                failed_attempts = excluded.failed_attempts,
                window_started = excluded.window_started,
                locked_until = excluded.locked_until,
                last_failed_at = excluded.last_failed_at
            """,
            (source_hash, failures, window_started, locked_until, now),
        )
        conn.execute(
            "DELETE FROM auth_local_source_attempts WHERE last_failed_at < ?",
            (now - LOCAL_THROTTLE_RETENTION_SECONDS,),
        )
        count = int(
            conn.execute("SELECT count(*) FROM auth_local_source_attempts").fetchone()[0]
        )
        excess = max(0, count - LOCAL_THROTTLE_MAX_SOURCES)
        if excess:
            conn.execute(
                """
                DELETE FROM auth_local_source_attempts WHERE source_hash IN (
                    SELECT source_hash FROM auth_local_source_attempts
                    ORDER BY last_failed_at, source_hash LIMIT ?
                )
                """,
                (excess,),
            )

    def _record_account_failure(
        self,
        conn: sqlite3.Connection,
        *,
        principal_id: str,
        now: float,
    ) -> None:
        row = conn.execute(
            "SELECT failed_attempts, last_failed_at FROM auth_local_credentials WHERE principal_id = ?",
            (principal_id,),
        ).fetchone()
        if row is None:
            return
        last_failed_at = float(row["last_failed_at"] or 0)
        failures = (
            1
            if not last_failed_at or now - last_failed_at > LOCAL_FAILURE_WINDOW_SECONDS
            else int(row["failed_attempts"]) + 1
        )
        locked_until: float | None = None
        if failures >= LOCAL_ACCOUNT_MAX_FAILURES:
            multiplier = 2 ** min(8, failures - LOCAL_ACCOUNT_MAX_FAILURES)
            locked_until = now + min(
                LOCAL_LOCK_MAX_SECONDS,
                LOCAL_LOCK_BASE_SECONDS * multiplier,
            )
        conn.execute(
            """
            UPDATE auth_local_credentials
            SET failed_attempts = ?, locked_until = ?, last_failed_at = ?, updated_at = ?
            WHERE principal_id = ?
            """,
            (failures, locked_until, now, now, principal_id),
        )

    def authenticate_local_password(
        self,
        *,
        login_email: str,
        password: str,
        source: str,
    ) -> dict[str, Any]:
        if not self.local_password_login:
            raise AuthGatewayError("Local password login is disabled")
        try:
            normalized_email = _normalize_email(login_email)
        except AuthGatewayError:
            normalized_email = "invalid@example.invalid"
        local_domain_allowed = not self.local_allowed_email_domains or any(
            normalized_email.rsplit("@", 1)[1] == allowed
            or normalized_email.rsplit("@", 1)[1].endswith(f".{allowed}")
            for allowed in self.local_allowed_email_domains
        )
        try:
            normalized_password = _normalized_password(password, provisioning=False)
            valid_password_shape = True
        except AuthGatewayError:
            normalized_password = "synthetic unavailable credential"
            valid_password_shape = False
        source_hash = self._source_key(source)
        now = time.time()
        with self._connect() as conn:
            credential = conn.execute(
                """
                SELECT c.*, p.disabled_at AS principal_disabled_at
                FROM auth_local_credentials c
                JOIN auth_principals p ON p.user_id = c.principal_id
                WHERE c.login_email = ? AND p.issuer = ?
                """,
                (normalized_email, self.password_namespace),
            ).fetchone()
            source_attempt = conn.execute(
                "SELECT * FROM auth_local_source_attempts WHERE source_hash = ?",
                (source_hash,),
            ).fetchone()
        if self._throttle_locked(source_attempt, now):
            raise self._local_failure()
        verified_phc = str(credential["password_phc"]) if credential is not None else _DUMMY_PASSWORD_PHC
        try:
            password_verified = _password_matches(normalized_password, verified_phc)
        except _PasswordKdfBusy as exc:
            raise AuthGatewayError(
                "Sign-in is temporarily busy; retry shortly",
                code="sign_in_busy",
            ) from exc

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """
                SELECT c.*, p.disabled_at AS principal_disabled_at
                FROM auth_local_credentials c
                JOIN auth_principals p ON p.user_id = c.principal_id
                WHERE c.login_email = ? AND p.issuer = ?
                """,
                (normalized_email, self.password_namespace),
            ).fetchone()
            source_attempt = conn.execute(
                "SELECT * FROM auth_local_source_attempts WHERE source_hash = ?",
                (source_hash,),
            ).fetchone()
            allowed = bool(
                local_domain_allowed
                and valid_password_shape
                and password_verified
                and current is not None
                and hmac.compare_digest(str(current["password_phc"]), verified_phc)
                and current["disabled_at"] is None
                and current["principal_disabled_at"] is None
                and not self._throttle_locked(current, now)
                and not self._throttle_locked(source_attempt, now)
            )
            if not allowed:
                current_matches_verified = bool(
                    current is not None
                    and credential is not None
                    and hmac.compare_digest(str(current["password_phc"]), verified_phc)
                    and int(current["password_version"]) == int(credential["password_version"])
                )
                if current_matches_verified:
                    self._record_account_failure(
                        conn,
                        principal_id=str(current["principal_id"]),
                        now=now,
                    )
                self._record_source_failure(conn, source_hash=source_hash, now=now)
                conn.commit()
                raise self._local_failure()
            principal_id = str(current["principal_id"])
            if _password_phc_needs_rehash(verified_phc):
                conn.execute(
                    """
                    UPDATE auth_local_credentials
                    SET password_phc = ?, password_version = password_version + 1,
                        password_changed_at = ?, updated_at = ?
                    WHERE principal_id = ? AND password_phc = ?
                    """,
                    (
                        _password_phc(normalized_password),
                        now,
                        now,
                        principal_id,
                        verified_phc,
                    ),
                )
            conn.execute(
                """
                UPDATE auth_local_credentials
                SET failed_attempts = 0, locked_until = NULL, last_failed_at = NULL, updated_at = ?
                WHERE principal_id = ?
                """,
                (now, principal_id),
            )
            return self._create_session_in_connection(
                conn,
                principal_id=principal_id,
                auth_method="local_password",
            )

    def unlock_local_password(self, *, subject: str) -> dict[str, str]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            principal = self._local_principal_row(conn, subject=subject)
            cursor = conn.execute(
                """
                UPDATE auth_local_credentials
                SET failed_attempts = 0, locked_until = NULL, last_failed_at = NULL, updated_at = ?
                WHERE principal_id = ?
                """,
                (time.time(), str(principal["user_id"])),
            )
            if cursor.rowcount != 1:
                raise AuthGatewayError("Local password credential was not found")
            return self._principal_payload(principal)

    def set_local_password_disabled(
        self,
        *,
        subject: str,
        disabled: bool,
    ) -> dict[str, str]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            principal = self._local_principal_row(conn, subject=subject)
            cursor = conn.execute(
                """
                UPDATE auth_local_credentials
                SET disabled_at = ?, updated_at = ? WHERE principal_id = ?
                """,
                (now if disabled else None, now, str(principal["user_id"])),
            )
            if cursor.rowcount != 1:
                raise AuthGatewayError("Local password credential was not found")
            if disabled:
                conn.execute(
                    """
                    UPDATE auth_local_sessions SET revoked_at = ?
                    WHERE principal_id = ? AND revoked_at IS NULL
                    """,
                    (now, str(principal["user_id"])),
                )
            return self._principal_payload(principal)

    def list_principals(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, email, display_name, role, disabled_at, created_at, updated_at
                FROM auth_principals
                ORDER BY created_at, user_id
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [
            {
                "user_id": str(row["user_id"]),
                "email": str(row["email"] or ""),
                "display_name": str(row["display_name"] or ""),
                "role": str(row["role"] or "member"),
                "disabled": row["disabled_at"] is not None,
                "disabled_at": float(row["disabled_at"]) if row["disabled_at"] is not None else None,
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in rows
        ]

    def set_principal_disabled(self, *, principal_id: str, disabled: bool) -> dict[str, Any]:
        user_id = str(principal_id or "").strip()
        if not user_id:
            raise AuthGatewayError("Account id is required")
        now = time.time()
        disabled_at = now if disabled else None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE auth_principals SET disabled_at = ?, updated_at = ? WHERE user_id = ?",
                (disabled_at, now, user_id),
            )
            if cursor.rowcount != 1:
                raise AuthGatewayError("Account was not found")
            if disabled:
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",
                    (now, user_id),
                )
                conn.execute(
                    "UPDATE auth_local_sessions SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",
                    (now, user_id),
                )
            row = conn.execute(
                """
                SELECT user_id, email, display_name, role, disabled_at, created_at, updated_at
                FROM auth_principals WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        assert row is not None
        return {
            "user_id": str(row["user_id"]),
            "email": str(row["email"] or ""),
            "display_name": str(row["display_name"] or ""),
            "role": str(row["role"] or "member"),
            "disabled": row["disabled_at"] is not None,
            "disabled_at": float(row["disabled_at"]) if row["disabled_at"] is not None else None,
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def _oidc_configuration(self) -> dict[str, str]:
        if self.mode not in {"oidc", "hybrid"}:
            raise AuthGatewayError("OIDC login is disabled")
        try:
            response = httpx.get(
                f"{self.oidc_issuer}/.well-known/openid-configuration",
                timeout=10.0,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AuthGatewayError(
                "OIDC provider metadata is unavailable",
                code="provider_unavailable",
            ) from exc
        if not isinstance(payload, dict) or str(payload.get("issuer") or "").rstrip("/") != self.oidc_issuer:
            raise AuthGatewayError(
                "OIDC provider issuer does not match configuration",
                code="provider_configuration",
            )
        result: dict[str, str] = {"issuer": self.oidc_issuer}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = str(payload.get(name) or "").strip()
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise AuthGatewayError(
                    f"OIDC provider metadata has an invalid {name}",
                    code="provider_configuration",
                )
            result[name] = value
        end_session_endpoint = str(payload.get("end_session_endpoint") or "").strip()
        if end_session_endpoint:
            parsed = urlparse(end_session_endpoint)
            if parsed.scheme == "https" and parsed.netloc and not parsed.fragment:
                result["end_session_endpoint"] = end_session_endpoint
        return result

    def provider_logout_url(self) -> str:
        if not self.oidc_post_logout_redirect_uri:
            return ""
        configuration = self._oidc_configuration()
        endpoint = str(configuration.get("end_session_endpoint") or "").strip()
        if not endpoint:
            return ""
        separator = "&" if urlparse(endpoint).query else "?"
        return f"{endpoint}{separator}{urlencode({'post_logout_redirect_uri': self.oidc_post_logout_redirect_uri, 'client_id': self.oidc_client_id})}"

    def begin_oidc(self, *, return_to: str = "/") -> dict[str, str]:
        configuration = self._oidc_configuration()
        safe_return_to = str(return_to or "/").strip()
        if (
            not safe_return_to.startswith("/")
            or safe_return_to.startswith("//")
            or "\\" in safe_return_to
        ):
            safe_return_to = "/"
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM auth_oidc_flows WHERE expires_at <= ?",
                (time.time(),),
            )
            conn.execute(
                "INSERT INTO auth_oidc_flows (state_hash, nonce, verifier, return_to, expires_at) VALUES (?, ?, ?, ?, ?)",
                (_hash_secret(state), nonce, verifier, safe_return_to, time.time() + 10 * 60),
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.oidc_client_id,
                "redirect_uri": self.oidc_redirect_uri,
                "scope": str(os.environ.get("GLASSHIVE_OIDC_SCOPES") or "openid profile email").strip(),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return {
            "authorization_url": f"{configuration['authorization_endpoint']}?{query}",
            "state": state,
            "nonce": nonce,
        }

    def _oidc_role(self, claims: dict[str, Any]) -> str:
        claim_name = str(os.environ.get("GLASSHIVE_OIDC_ROLE_CLAIM") or "roles").strip()
        raw_values = claims.get(claim_name)
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        try:
            role_map = json.loads(str(os.environ.get("GLASSHIVE_OIDC_ROLE_MAP_JSON") or "{}"))
        except json.JSONDecodeError as exc:
            raise AuthGatewayError("OIDC role map configuration is invalid") from exc
        if not isinstance(role_map, dict):
            raise AuthGatewayError("OIDC role map configuration is invalid")
        mapped: set[str] = set()
        for value in values:
            role = str(role_map.get(str(value)) or "").strip()
            if role in HUMAN_ROLES:
                mapped.add(role)
        for preferred in ("tenant_admin", "service", "viewer", "member"):
            if preferred in mapped:
                return preferred
        if role_map:
            raise AuthGatewayError(
                "This account does not have an approved GlassHive role",
                code="account_not_authorized",
            )
        return "member"

    def complete_oidc(self, *, state: str, code: str) -> dict[str, Any]:
        if not state or not code:
            raise AuthGatewayError(
                "OIDC callback is missing state or code",
                code="callback_invalid",
            )
        state_hash = _hash_secret(state)
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            flow = conn.execute(
                "SELECT * FROM auth_oidc_flows WHERE state_hash = ? AND expires_at > ?",
                (state_hash, now),
            ).fetchone()
            if flow is None:
                raise AuthGatewayError(
                    "OIDC login state expired or already used",
                    code="state_expired",
                )
            conn.execute("DELETE FROM auth_oidc_flows WHERE state_hash = ?", (state_hash,))
        configuration = self._oidc_configuration()
        token_data = {
            "grant_type": "authorization_code",
            "client_id": self.oidc_client_id,
            "code": code,
            "redirect_uri": self.oidc_redirect_uri,
            "code_verifier": str(flow["verifier"]),
        }
        request_kwargs: dict[str, Any] = {
            "data": token_data,
            "timeout": 15.0,
            "follow_redirects": False,
        }
        if self.oidc_client_secret:
            request_kwargs["auth"] = (self.oidc_client_id, self.oidc_client_secret)
        try:
            response = httpx.post(configuration["token_endpoint"], **request_kwargs)
            response.raise_for_status()
            token_payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AuthGatewayError(
                "OIDC token exchange failed",
                code="provider_unavailable",
            ) from exc
        id_token = str(token_payload.get("id_token") or "") if isinstance(token_payload, dict) else ""
        if not id_token:
            raise AuthGatewayError(
                "OIDC provider did not return an ID token",
                code="token_invalid",
            )
        try:
            jwks_response = httpx.get(configuration["jwks_uri"], timeout=10.0, follow_redirects=False)
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
            header = jwt.get_unverified_header(id_token)
            key_id = str(header.get("kid") or "")
            key_data = next(
                item
                for item in jwks.get("keys", [])
                if isinstance(item, dict) and str(item.get("kid") or "") == key_id
            )
            claims = jwt.decode(
                id_token,
                jwt.PyJWK.from_dict(key_data).key,
                algorithms=["RS256"],
                audience=self.oidc_client_id,
                issuer=self.oidc_issuer,
                leeway=30,
                options={"require": ["iss", "aud", "sub", "nonce", "iat", "exp"]},
            )
        except StopIteration as exc:
            raise AuthGatewayError(
                "OIDC signing key is not trusted",
                code="token_invalid",
            ) from exc
        except (httpx.HTTPError, ValueError, TypeError, jwt.PyJWTError) as exc:
            raise AuthGatewayError(
                "OIDC ID token validation failed",
                code="token_invalid",
            ) from exc
        if not hmac.compare_digest(str(claims.get("nonce") or ""), str(flow["nonce"])):
            raise AuthGatewayError("OIDC nonce validation failed", code="token_invalid")
        raw_audience = claims.get("aud")
        if isinstance(raw_audience, list) and len(raw_audience) > 1:
            if not hmac.compare_digest(str(claims.get("azp") or ""), self.oidc_client_id):
                raise AuthGatewayError(
                    "OIDC authorized party does not match the GlassHive client",
                    code="token_invalid",
                )
        subject = str(claims.get(self.oidc_principal_claim) or "").strip()
        if not subject:
            raise AuthGatewayError(
                "OIDC identity is missing the configured stable principal claim",
                code="identity_invalid",
            )
        email = str(claims.get(self.oidc_email_claim) or "").strip()
        email_is_verified = claims.get("email_verified") is True or self.oidc_email_claim_trusted
        if email and not email_is_verified:
            email = ""
        # Email-like claims are mutable display metadata. Admission and ownership are
        # anchored exclusively to the validated issuer plus configured stable claim;
        # tenant/domain restrictions belong in IdP policy or immutable role/group claims.
        role = self._oidc_role(claims)
        if not self._oidc_role_map_configured():
            # Without an identity-provider role map, the admission record
            # (preapproval or the admin page) owns the role; sign-in only proves
            # identity. A configured role map makes the provider's claims authoritative.
            existing = self.find_oidc_principal(
                issuer=str(claims.get("iss") or ""), subject=subject
            )
            if existing is not None:
                role = existing["role"]
        principal = self.reconcile_oidc_principal(
            issuer=str(claims.get("iss") or ""),
            subject=subject,
            email=email,
            display_name=str(claims.get("name") or ""),
            role=role,
        )
        return {"principal": principal, "return_to": str(flow["return_to"])}
