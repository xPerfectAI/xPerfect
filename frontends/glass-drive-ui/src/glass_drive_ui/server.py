from __future__ import annotations

import os
import re
import asyncio
import base64
import hmac
import json
import logging
import secrets
import shlex
import sqlite3
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urlparse

import httpx
import websockets
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from .prompt_template import (
    build_operator_brief,
    build_project_title,
    initial_watch_surface_for_launch,
    normalize_launch_surface,
)
from .runtime_client import RuntimeClient
from .file_routes import install_file_routes
from .terminal_routes import install_terminal_routes
from .peer_routes import install_peer_ui_routes
from .member_routes import install_member_ui_routes
from .workspace_routes import install_workspace_ui_routes
from .worker_configuration_routes import install_worker_configuration_ui_routes
from .allowed_ai_policy_routes import install_allowed_ai_policy_ui_routes
from .internal_assertions import InternalAssertionSigner
from .auth_gateway import AuthGatewayError, HumanAuthGateway
from .signed_links import (
    create_signed_link_ref,
    install_sensitive_url_log_filter,
    resolve_signed_link_ref,
    signed_link_ref_url,
    sign_link_token,
    verify_signed_link_ref_token,
    verify_signed_link_token,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAFE_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Runtime opaque refs accept 12-96 characters total; account view refs use the
# structural ``ghr_`` namespace within that bounded alphabet.
SAFE_WORK_VIEW_REF_RE = re.compile(r"^ghr_[A-Za-z0-9_-]{8,92}$")
SAFE_UPLOAD_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
PROVIDER_ACCOUNT_POLICIES = {"legacy", "personal_preferred", "personal_required"}
AUTH_SESSION_COOKIE = "glasshive_session"
AUTH_CSRF_COOKIE = "glasshive_csrf"
AUTH_OIDC_STATE_COOKIE = "glasshive_oidc_state"
AUTH_LOGIN_CSRF_COOKIE = "glasshive_login_csrf"
LOCAL_LOGIN_MAX_BODY_BYTES = 4096
OIDC_START_WINDOW_SECONDS = 5 * 60
OIDC_START_MAX_ATTEMPTS = 30
OIDC_START_MAX_SOURCES = 4096
NOVNC_VIEW_URL_CACHE_TTL_SECONDS = 15.0
NOVNC_ASSET_CACHE_TTL_SECONDS = 10 * 60.0
NOVNC_ASSET_CACHE_MAX_BYTES = 2 * 1024 * 1024
RUNTIME_ENV_KEYS = {
    "GLASSHIVE_RELEASE_ID",
    "GLASSHIVE_PARENT_REVISION",
    "GLASSHIVE_COMPONENT_REVISION",
    "GLASSHIVE_ENTERPRISE_MODE",
    "GLASSHIVE_PUBLIC_LINKS_ONLY",
    "WPR_ENTERPRISE_MODE",
    "GLASSHIVE_AUTH_MODE",
    "GLASSHIVE_HUMAN_AUTH_MODE",
    "GLASSHIVE_ALLOW_EMAIL_LOGIN",
    "GLASSHIVE_ALLOW_EMAIL_REGISTRATION",
    "GLASSHIVE_PROVIDER_EMAIL_LOGIN",
    "GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT",
    "GLASSHIVE_LOCAL_PASSWORD_LOGIN",
    "GLASSHIVE_OIDC_LOGIN_VISIBLE",
    "GLASSHIVE_LOCAL_AUTH_ALLOWED_EMAIL_DOMAINS",
    "GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY",
    "GLASSHIVE_ALLOWED_EMAIL_DOMAINS",
    "GLASSHIVE_ALLOWED_ORIGINS",
    "GLASSHIVE_AUTH_STATE_PATH",
    "GLASSHIVE_AUTH_SESSION_TTL_SECONDS",
    "GLASSHIVE_OIDC_ISSUER",
    "GLASSHIVE_OIDC_CLIENT_ID",
    "GLASSHIVE_OIDC_CLIENT_SECRET",
    "GLASSHIVE_OIDC_REDIRECT_URI",
    "GLASSHIVE_OIDC_POST_LOGOUT_REDIRECT_URI",
    "GLASSHIVE_OIDC_SCOPES",
    "GLASSHIVE_OIDC_ROLE_CLAIM",
    "GLASSHIVE_OIDC_ROLE_MAP_JSON",
    "GLASSHIVE_INTERNAL_ASSERTION_ISSUER",
    "GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE",
    "GLASSHIVE_INTERNAL_ASSERTION_KEY_ID",
    "GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE",
    "GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL",
    "GLASSHIVE_MCP_OAUTH_ISSUER",
    "GLASSHIVE_MCP_PUBLIC_URL",
    "GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES",
    "GLASSHIVE_MCP_OAUTH_TOKEN_TENANT_ID",
    "GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES",
    "GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES",
    "GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS",
    "GLASSHIVE_MCP_CLAUDE_CLIENT_ID",
    "GLASSHIVE_MCP_CLAUDE_CALLBACK_PORT",
    "GLASSHIVE_MCP_CODEX_CLIENT_ID",
    "GLASSHIVE_MCP_CODEX_CALLBACK_PORT",
    "GLASSHIVE_MCP_CODEX_RESOURCE",
    "GLASSHIVE_MCP_OAUTH_SUBJECT_CLAIM",
    "GLASSHIVE_MCP_DOCUMENTATION_URL",
    "GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS",
    "GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH",
    "GLASSHIVE_PROVIDER_SECRET_STORE_ENABLED",
    "GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION",
    "GLASSHIVE_INFERENCE_BROKER_URL",
    "GLASSHIVE_INFERENCE_BROKER_PROXY_BASE_URL",
    "GLASSHIVE_INFERENCE_BROKER_SECRET",
    "GLASSHIVE_INFERENCE_BROKER_TENANT_ID",
    "GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON",
    "GLASSHIVE_INFERENCE_BROKER_TIMEOUT_SECONDS",
    "GLASSHIVE_CONNECTED_ACCOUNTS_URL",
    "GLASSHIVE_ENTERPRISE_TENANT_ID",
    "WPR_ENTERPRISE_TENANT_ID",
    "GLASSHIVE_OPERATOR_BASE_URL",
    "GLASSHIVE_RUNTIME_BASE_URL",
    "GLASSHIVE_SIGNED_LINK_SECRET",
    "GLASSHIVE_LINK_REF_STATE_PATH",
    "GLASSHIVE_LINK_REF_SHARED_GROUP",
    "GLASSHIVE_LINK_REF_TTL_SECONDS",
    "WPR_LINK_REF_TTL_SECONDS",
    "GLASSHIVE_WATCH_SESSION_STATE_PATH",
    "GLASSHIVE_MAX_WATCH_SESSION_DURATION_S",
    "GLASSHIVE_TRUST_INBOUND_IDENTITY",
    "GLASSHIVE_OWNER_IDENTITY_CLAIMS",
    "GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON",
    "GLASSHIVE_OWNER_IDENTITY_ALIASES_FILE",
    "WPR_API_TOKEN",
}


class _BoundedAttemptLimiter:
    """Small in-process throttle whose source map cannot grow without bound."""

    def __init__(self, *, window_seconds: float, max_attempts: int, max_sources: int):
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.max_sources = max(1, int(max_sources))
        self.attempts_by_source: dict[str, list[float]] = {}

    def _prune(self, now: float) -> None:
        for source, attempts in list(self.attempts_by_source.items()):
            active = [value for value in attempts if now - value < self.window_seconds]
            if active:
                self.attempts_by_source[source] = active
            else:
                self.attempts_by_source.pop(source, None)

    def admit(self, source: str, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else float(now)
        self._prune(timestamp)
        normalized_source = str(source or "unknown")[:128]
        attempts = self.attempts_by_source.get(normalized_source, [])
        if len(attempts) >= self.max_attempts:
            raise HTTPException(status_code=429, detail="Too many sign-in attempts; try again shortly")
        if normalized_source not in self.attempts_by_source and len(self.attempts_by_source) >= self.max_sources:
            oldest_source = min(
                self.attempts_by_source,
                key=lambda item: self.attempts_by_source[item][-1],
            )
            self.attempts_by_source.pop(oldest_source, None)
        self.attempts_by_source.setdefault(normalized_source, []).append(timestamp)
_NOVNC_VIEW_URL_CACHE: dict[str, tuple[float, str]] = {}
_NOVNC_ASSET_CACHE: dict[str, tuple[float, int, bytes, str]] = {}
_NOVNC_HTTP_CLIENT: httpx.Client | None = None
logger = logging.getLogger(__name__)


def _watch_session_cap_seconds() -> int:
    raw = os.environ.get("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "").strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    return max(0, min(value, 24 * 3600))


def _watch_session_state_path() -> Path:
    raw = str(os.environ.get("GLASSHIVE_WATCH_SESSION_STATE_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_root = (
        Path(os.environ["XDG_STATE_HOME"]).expanduser()
        if os.environ.get("XDG_STATE_HOME")
        else Path.home() / ".local" / "state"
    )
    return state_root / "glasshive" / "watch_sessions.sqlite3"


def _harden_watch_session_state(db_path: Path) -> None:
    if os.name == "nt":
        return
    db_path.parent.chmod(0o700)
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists() and not candidate.is_symlink():
            candidate.chmod(0o600)


def _watch_session_conn(*, timeout_seconds: float = 30.0) -> sqlite3.Connection:
    db_path = _watch_session_state_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=max(0.0, float(timeout_seconds)))
    try:
        _harden_watch_session_state(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watch_sessions (
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (tenant_id, owner_id, worker_id)
            )
            """
        )
        _harden_watch_session_state(db_path)
        return conn
    except Exception:
        conn.close()
        raise


def _watch_session_expires_at(
    worker_id: str,
    identity: dict[str, str] | None,
    *,
    sqlite_timeout_seconds: float = 30.0,
) -> int | None:
    cap_seconds = _watch_session_cap_seconds()
    if cap_seconds <= 0 or not identity:
        return None
    tenant_id = str(identity.get("tenant_id") or "").strip()
    owner_id = str(identity.get("user_id") or "").strip()
    if not tenant_id or not owner_id:
        return None
    now = int(time.time())
    with _watch_session_conn(timeout_seconds=sqlite_timeout_seconds) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM watch_sessions WHERE expires_at < ?", (now - 24 * 3600,))
        row = conn.execute(
            """
            SELECT expires_at FROM watch_sessions
            WHERE tenant_id = ? AND owner_id = ? AND worker_id = ?
            """,
            (tenant_id, owner_id, worker_id),
        ).fetchone()
        if row is not None and int(row[0]) > now:
            expires_at = int(row[0])
            conn.execute(
                """
                UPDATE watch_sessions
                SET updated_at = ?
                WHERE tenant_id = ? AND owner_id = ? AND worker_id = ?
                """,
                (now, tenant_id, owner_id, worker_id),
            )
            return expires_at
        expires_at = now + cap_seconds
        conn.execute(
            """
            INSERT INTO watch_sessions (tenant_id, owner_id, worker_id, started_at, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, owner_id, worker_id) DO UPDATE SET
                started_at = excluded.started_at,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (tenant_id, owner_id, worker_id, now, expires_at, now),
        )
        return expires_at


def _existing_watch_session_expires_at(worker_id: str, identity: dict[str, str] | None) -> int | None:
    if _watch_session_cap_seconds() <= 0 or not identity:
        return None
    tenant_id = str(identity.get("tenant_id") or "").strip()
    owner_id = str(identity.get("user_id") or "").strip()
    if not tenant_id or not owner_id:
        return None
    with _watch_session_conn() as conn:
        row = conn.execute(
            """
            SELECT expires_at FROM watch_sessions
            WHERE tenant_id = ? AND owner_id = ? AND worker_id = ?
            """,
            (tenant_id, owner_id, worker_id),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _ensure_signed_worker_watch_session(worker_id: str, payload: dict[str, object]) -> None:
    if str(payload.get("kind") or "") != "worker_view":
        return
    identity = {
        "tenant_id": str(payload.get("tenant_id") or "").strip(),
        "user_id": str(payload.get("owner_id") or "").strip(),
    }
    existing = _existing_watch_session_expires_at(worker_id, identity)
    now = int(time.time())
    if existing is not None and existing > now:
        return
    _watch_session_expires_at(worker_id, identity)


def _load_viventium_runtime_env() -> None:
    explicit = os.environ.get("VIVENTIUM_ENV_FILE", "").strip()
    if not explicit:
        return
    try:
        lines = Path(explicit).expanduser().read_text().splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        try:
            part = shlex.split(stripped, comments=True, posix=True)[0]
        except (ValueError, IndexError):
            continue
        key, _, value = part.partition("=")
        if key in RUNTIME_ENV_KEYS and not os.environ.get(key):
            os.environ[key] = value


def _novnc_http_client() -> httpx.Client:
    global _NOVNC_HTTP_CLIENT
    if _NOVNC_HTTP_CLIENT is None:
        _NOVNC_HTTP_CLIENT = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _NOVNC_HTTP_CLIENT


def _fetch_novnc_asset(target: str) -> httpx.Response:
    global _NOVNC_HTTP_CLIENT
    try:
        return _novnc_http_client().get(target)
    except httpx.HTTPError:
        if _NOVNC_HTTP_CLIENT is not None:
            close = getattr(_NOVNC_HTTP_CLIENT, "close", None)
            if callable(close):
                close()
            _NOVNC_HTTP_CLIENT = None
        return _novnc_http_client().get(target)


class UploadedFileRequest(BaseModel):
    name: str = Field(min_length=1)
    mime_type: str | None = None
    size: int | None = Field(default=None, ge=0)
    content_base64: str = Field(min_length=1)


class LaunchRequest(BaseModel):
    description: str = Field(min_length=1)
    success_criteria: str = ""
    context: str | None = None
    workspace_option: str | None = None
    workspace_type: str | None = None
    workspace_mode: Literal["isolated", "shared"] = "isolated"
    workspace_file_placement: Literal["common", "member_private"] = "common"
    worker_option: str | None = None
    launch_surface: str | None = None
    schedule_text: str | None = None
    effort: str | None = None
    provider_account_policy: Literal["legacy", "personal_preferred", "personal_required"] | None = None
    provider_account_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    files: list[UploadedFileRequest] = Field(default_factory=list)
    file_upload_ids: list[str] = Field(default_factory=list)
    file_upload_revisions: list[str] = Field(default_factory=list)


class PreferencesRequest(BaseModel):
    default_worker_profile: str | None = None
    codex_reasoning_effort: str | None = None
    claude_effort: str | None = None
    openclaw_effort: str | None = None
    grok_model: str | None = None


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)


class NativeControlRequest(BaseModel):
    class Config:
        extra = "forbid"

    run_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    action: Literal["interject", "cancel", "permission"]
    text: str | None = Field(default=None, max_length=65536)
    request_id: str | None = Field(default=None, max_length=128)
    option_id: str | None = Field(default=None, max_length=128)
    response: dict[str, Any] | None = None


class ActionRequest(BaseModel):
    url: str | None = None


class MetadataRequest(BaseModel):
    favorite: bool | None = None
    name: str | None = None
    tags: list[str] | None = None
    workspace_kind: Literal["named", "ephemeral", "legacy"] | None = None


class DuplicateWorkspaceRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=160)


class SaveWorkspaceTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    lineage_id: str | None = Field(default=None, min_length=1, max_length=80)


class InstantiateWorkspaceTemplateRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=160)


class ProviderAccountRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=160)
    auth_method: str = Field(min_length=1, max_length=40)
    make_default: bool = False


class ProviderApiKeyRequest(BaseModel):
    value: SecretStr = Field(min_length=1, max_length=8192)


class ProviderSetupInputRequest(BaseModel):
    value: str


class PendingChangeRequest(BaseModel):
    change_type: str = Field(min_length=1, max_length=120)
    target_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class PendingChangeConfirmRequest(BaseModel):
    confirmation_token: str = Field(min_length=16, max_length=512)


class AdminPrincipalUpdateRequest(BaseModel):
    disabled: bool


class RecurringScheduleRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=10000)
    recurrence_type: Literal["once", "daily", "interval", "cron", "rfc5545"]
    interval_seconds: int | None = None
    local_time: str = ""
    timezone_name: str = "UTC"
    dst_policy: Literal["next_valid_earliest", "next_valid_latest"] = "next_valid_earliest"
    first_run_at: str | None = None
    cron_expression: str = ""
    rrule: str = ""
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool = True
    overlap_policy: Literal["skip", "queue"] = "skip"
    misfire_grace_seconds: int = Field(default=300, ge=0, le=604800)
    catch_up_policy: Literal["skip", "bounded", "coalesce"] = "skip"
    max_catch_up_occurrences: int = Field(default=1, ge=1, le=10)
    jitter_seconds: int = Field(default=0, ge=0, le=900)
    schedule_text: str = ""


class RecurringScheduleUpdateRequest(BaseModel):
    instruction: str | None = Field(default=None, min_length=1, max_length=10000)
    recurrence_type: Literal["once", "daily", "interval", "cron", "rfc5545"] | None = None
    interval_seconds: int | None = None
    local_time: str | None = None
    timezone_name: str | None = None
    dst_policy: Literal["next_valid_earliest", "next_valid_latest"] | None = None
    cron_expression: str | None = None
    rrule: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool | None = None
    overlap_policy: Literal["skip", "queue"] | None = None
    misfire_grace_seconds: int | None = Field(default=None, ge=0, le=604800)
    catch_up_policy: Literal["skip", "bounded", "coalesce"] | None = None
    max_catch_up_occurrences: int | None = Field(default=None, ge=1, le=10)
    jitter_seconds: int | None = Field(default=None, ge=0, le=900)
    schedule_text: str | None = None


class RecurringScheduleRunNowRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)


def _recurring_path_id(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not SAFE_WORKER_ID_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail=f"Invalid {label}")
    return normalized


SIGNED_QUERY_KEYS = {"gh_token", "gh_sig", "gh_exp", "gh_kind"}
LOGIN_RETURN_SENSITIVE_QUERY_KEYS = SIGNED_QUERY_KEYS | {
    "code",
    "state",
    "error_description",
}
OIDC_UI_ERROR_CODES = {
    "access_denied",
    "account_not_authorized",
    "account_not_registered",
    "callback_invalid",
    "cancelled",
    "identity_invalid",
    "provider_configuration",
    "provider_unavailable",
    "sign_in_failed",
    "state_expired",
    "state_invalid",
    "token_invalid",
}


def _canonical_codex_server_url(value: str) -> str:
    """Mirror Rust `Url::parse(...).as_str()` for supported HTTP(S) MCP URLs."""
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid MCP server URL")
    if any(ord(character) > 127 for character in str(value or "")) or re.search(
        r"%(?![0-9A-Fa-f]{2})",
        str(value or ""),
    ):
        raise ValueError("invalid MCP server URL encoding")
    hostname = str(parsed.hostname).encode("idna").decode("ascii").lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{netloc}{path}{query}"


def _codex_oauth_callback_uri(mcp_url: str, callback_port: int) -> str:
    canonical = _canonical_codex_server_url(mcp_url)
    callback_hash = base64.urlsafe_b64encode(
        sha256(canonical.encode("utf-8")).digest()[:9]
    ).decode("ascii").rstrip("=")
    return f"http://127.0.0.1:{callback_port}/callback/{callback_hash}"


def _mcp_client_server_name(mcp_url: str) -> str:
    """Return a stable shell-safe name without leaking a deployment label."""
    canonical = _canonical_codex_server_url(mcp_url)
    return f"glasshive-{sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def _strip_signed_query_params(url: str) -> str:
    parsed = urlparse(str(url or ""))
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in SIGNED_QUERY_KEYS]
    )
    return parsed._replace(query=query).geturl()


def _configured_redirect_hosts(request: Request) -> set[str]:
    hosts = {str(request.url.netloc or "").lower(), str(request.base_url.netloc or "").lower()}
    for name in ("GLASSHIVE_OPERATOR_BASE_URL", "GLASSHIVE_RUNTIME_BASE_URL"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            parsed = urlparse(value)
            if parsed.netloc:
                hosts.add(parsed.netloc.lower())
    for name in ("GLASSHIVE_ALLOWED_REDIRECT_HOSTS", "WPR_ALLOWED_REDIRECT_HOSTS"):
        raw = str(os.environ.get(name) or "").strip()
        for item in raw.split(","):
            value = item.strip()
            if not value:
                continue
            parsed = urlparse(value)
            hosts.add((parsed.netloc or value).strip().rstrip("/").lower())
    return {host for host in hosts if host}


def _validate_short_ref_redirect_target(target_url: str, request: Request) -> str:
    target = str(target_url or "").strip()
    if "\\" in target or target.startswith("//"):
        raise HTTPException(status_code=400, detail="GlassHive workspace link target path is not allowed")
    parsed = urlparse(target)
    if not parsed.scheme and not parsed.netloc:
        return target
    if parsed.scheme.lower() not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="GlassHive workspace link target scheme is not allowed")
    if str(parsed.netloc or "").lower() not in _configured_redirect_hosts(request):
        raise HTTPException(status_code=403, detail="GlassHive workspace link target is not allowed")
    return target


def _worker_view_token(
    worker_id: str,
    identity: dict[str, str] | None,
    *,
    storage_timeout_seconds: float = 30.0,
) -> str:
    if not identity:
        return ""
    ttl_seconds = None
    expires_at = _watch_session_expires_at(
        worker_id,
        identity,
        sqlite_timeout_seconds=storage_timeout_seconds,
    )
    if expires_at is not None:
        ttl_seconds = max(1, expires_at - int(time.time()))
    return sign_link_token(
        kind="worker_view",
        worker_id=worker_id,
        tenant_id=str(identity.get("tenant_id") or ""),
        owner_id=str(identity.get("user_id") or ""),
        ttl_seconds=ttl_seconds,
    )


def _append_signed_worker_token(
    url: str,
    worker_id: str,
    identity: dict[str, str] | None,
    *,
    storage_timeout_seconds: float = 30.0,
) -> str:
    target_url = _strip_signed_query_params(url)
    # Local operator navigation already has local-owner authority. Minting a
    # view credential here would downgrade that same browser to read-only.
    if identity and str(identity.get("auth_source") or "") == "local":
        return target_url
    token = _worker_view_token(
        worker_id,
        identity,
        storage_timeout_seconds=storage_timeout_seconds,
    )
    if not token:
        return target_url
    ref_id = create_signed_link_ref(
        token=token,
        target_url=target_url,
        sqlite_timeout_seconds=storage_timeout_seconds,
    )
    if not ref_id:
        return target_url
    return signed_link_ref_url("", ref_id)


def flatten_workspaces(
    client: RuntimeClient,
    identity: dict[str, str] | None = None,
    *,
    availability: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build the bounded primary catalog from the runtime's owner-scoped cursor API."""

    items: list[dict[str, Any]] = []
    seen_worker_ids: set[str] = set()
    try:
        safe_catalog = client.list_workspace_catalog(kind="named", limit=25)
        if availability is not None:
            availability["workspace_catalog"] = "ready"
    except Exception:
        safe_catalog = {"items": []}
        if availability is not None:
            availability["workspace_catalog"] = "unavailable"
    active_states = {"created", "starting", "queued", "running", "resuming"}
    resumable_states = {"ready", "paused", "idle", "idle_terminated", "stopped"}
    for worker in safe_catalog.get("items", []):
        if not isinstance(worker, dict):
            continue
        worker_state = str(worker.get("close_state") or worker.get("state") or "").strip().lower()
        if worker_state in {"terminating", "termination_failed", "terminated"}:
            continue
        project_id = str(worker.get("project_id") or "")
        worker_id = str(worker.get("worker_id") or "")
        if not worker_id or worker_id in seen_worker_ids:
            continue
        seen_worker_ids.add(worker_id)
        project_title = str(worker.get("project_title") or worker.get("name") or project_id)
        worker_name = str(worker.get("name") or worker_id)
        watch_url = f"/watch/{worker_id}?project_id={project_id}&surface=desktop"
        project_url = f"/ui/projects/{project_id}?worker_id={worker_id}"
        desktop_url = f"/desktop/{worker_id}"
        desktop_preview_url = f"/desktop/{worker_id}?preview=1"
        api_url = f"/api/worker/{worker_id}"
        signed_watch_url = _append_signed_worker_token(watch_url, worker_id, identity)
        items.append(
            {
                **worker,
                "project_id": project_id,
                "project_title": project_title,
                "worker_id": worker_id,
                "name": worker_name,
                "workspace_label": project_title or worker_name,
                "is_active": worker_state in active_states,
                "is_resumable": worker_state in resumable_states,
                "state_label": "retained" if worker_state == "ready" else (worker.get("state") or ""),
                "watch_url": signed_watch_url,
                # Primary user navigation stays on the modern GlassHive surface.
                # Keep project_url below as an additive compatibility contract for
                # direct operators and older API consumers.
                "workspace_url": signed_watch_url,
                "project_url": _append_signed_worker_token(project_url, worker_id, identity),
                "desktop_url": _append_signed_worker_token(desktop_url, worker_id, identity),
                "desktop_preview_url": _append_signed_worker_token(
                    desktop_preview_url,
                    worker_id,
                    identity,
                ),
                "api_url": _append_signed_worker_token(api_url, worker_id, identity),
                # Browser controls run inside the authenticated GlassHive shell. Keep the
                # navigation URLs opaque while child control paths remain same-origin.
                "control_url": api_url,
            }
        )
    return items


def _project_title_for_worker(client: RuntimeClient, project_id: str) -> str:
    try:
        project = client.get_project(project_id)
        return str(project.get("title") or project_id)
    except Exception:
        return project_id


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return _env_flag(name, False)


def _public_links_only_enabled() -> bool:
    return _truthy_env("GLASSHIVE_PUBLIC_LINKS_ONLY")


def _public_links_only_enabled() -> bool:
    return _truthy_env("GLASSHIVE_PUBLIC_LINKS_ONLY")


def _multi_user_security_enabled() -> bool:
    return str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user"


def _personal_account_isolation_ready() -> bool:
    return (
        not _multi_user_security_enabled()
        or str(os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION") or "").strip().lower()
        == "per_worker_container"
    )


def _validate_enterprise_startup() -> None:
    security_mode = str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower()
    if security_mode not in {"", "local", "legacy_compatibility", "multi_user"}:
        raise RuntimeError("GLASSHIVE_SECURITY_MODE must be local, legacy_compatibility, or multi_user")
    enterprise = _truthy_env("GLASSHIVE_ENTERPRISE_MODE") or _truthy_env("WPR_ENTERPRISE_MODE")
    human_auth_mode = str(os.environ.get("GLASSHIVE_HUMAN_AUTH_MODE") or "").strip().lower()
    if human_auth_mode == "oidc" and _truthy_env("GLASSHIVE_TRUST_INBOUND_IDENTITY"):
        raise RuntimeError("OIDC human auth cannot trust inbound identity headers")
    if _public_links_only_enabled() and not str(
        os.environ.get("GLASSHIVE_SIGNED_LINK_SECRET") or ""
    ).strip():
        raise RuntimeError(
            "GlassHive public link mode requires GLASSHIVE_SIGNED_LINK_SECRET"
        )
    if not enterprise:
        return
    api_token = str(os.environ.get("WPR_API_TOKEN") or "").strip()
    signed_link_secret = str(os.environ.get("GLASSHIVE_SIGNED_LINK_SECRET") or "").strip()
    if not api_token:
        raise RuntimeError("GlassHive enterprise UI requires WPR_API_TOKEN for runtime service auth")
    if not signed_link_secret:
        raise RuntimeError("GlassHive enterprise UI requires GLASSHIVE_SIGNED_LINK_SECRET")
    if signed_link_secret == api_token:
        raise RuntimeError("GlassHive enterprise UI requires GLASSHIVE_SIGNED_LINK_SECRET to differ from WPR_API_TOKEN")
    if _multi_user_security_enabled() and not str(
        os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID") or os.environ.get("WPR_ENTERPRISE_TENANT_ID") or ""
    ).strip():
        raise RuntimeError("GLASSHIVE_SECURITY_MODE=multi_user requires a deployment tenant id")
    if (
        _multi_user_security_enabled()
        and human_auth_mode == "trusted_proxy"
        and not _truthy_env("GLASSHIVE_TRUSTED_PROXY_BOUNDARY_PROVEN")
    ):
        raise RuntimeError(
            "Multi-user trusted-proxy auth requires a proven private ingress or mTLS boundary"
        )
    try:
        _validate_owner_identity_config()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


OWNER_IDENTITY_CLAIM_NAMES = {"user_id", "email"}
DEFAULT_OWNER_IDENTITY_CLAIMS = ("user_id",)


def _sanitize_identity_value(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("{{") and text.endswith("}}"):
        return ""
    if text.startswith("${") and text.endswith("}"):
        return ""
    return text[:512]


def _identity_values_match(expected: object, actual: object) -> bool:
    expected_text = _sanitize_identity_value(expected)
    actual_text = _sanitize_identity_value(actual)
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


def _owner_identity_claims() -> tuple[str, ...]:
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
        owner_id = _sanitize_identity_value(owner)
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
            clean = _sanitize_identity_value(value)
            if clean and clean not in clean_values:
                clean_values.append(clean)
        if clean_values:
            aliases[owner_id] = tuple(clean_values)
    return aliases


def _validate_owner_identity_config() -> None:
    _parse_owner_identity_claims(os.environ.get("GLASSHIVE_OWNER_IDENTITY_CLAIMS", ""), strict=True)
    _parse_owner_identity_aliases(strict=True)


def _owner_matches_identity(owner_id: object, identity: dict[str, str]) -> bool:
    owner = _sanitize_identity_value(owner_id)
    if not owner:
        return False
    claim_values = {
        "user_id": identity.get("user_id", ""),
        "email": identity.get("email", ""),
    }
    candidates = [claim_values.get(claim, "") for claim in _owner_identity_claims()]
    if any(_identity_values_match(owner, candidate) for candidate in candidates):
        return True
    for canonical_owner, aliases in _parse_owner_identity_aliases().items():
        if not _identity_values_match(owner, canonical_owner):
            continue
        if any(_identity_values_match(alias, candidate) for alias in aliases for candidate in candidates):
            return True
    return False


def _default_launch_surface() -> str:
    return normalize_launch_surface(os.environ.get("GLASSHIVE_DEFAULT_LAUNCH_SURFACE", "desktop"))


def _launch_surface_options() -> list[dict[str, str]]:
    return [
        {
            "value": "desktop",
            "label": "Live desktop",
            "description": "Open the workstation desktop first. This is the recommended default.",
        },
        {
            "value": "terminal",
            "label": "Exact live session",
            "description": "Open the raw live terminal session first instead of the desktop.",
        },
        {
            "value": "auto",
            "label": "Auto",
            "description": "Let GlassHive choose the initial surface from the task type.",
        },
    ]


def _workspace_type_options() -> list[dict[str, object]]:
    host_available = _env_flag("GLASSHIVE_HOST_WORKERS_ENABLED", True)
    options: list[dict[str, object]] = [
        {
            "value": "sandboxed",
            "label": "Sandboxed Workspace",
            "description": "Runs on managed GlassHive workspace compute with project files and browser state preserved for resume.",
            "disabled": False,
        }
    ]
    if host_available:
        options.append(
            {
                "value": "host",
                "label": "Your Computer",
                "description": "Runs on this computer with host-native tools. Not available in Azure enterprise mode.",
                "disabled": False,
            }
        )
    return options


def _default_workspace_type() -> str:
    default_mode = str(
        os.environ.get("GLASSHIVE_DEFAULT_EXECUTION_MODE")
        or os.environ.get("WPR_DEFAULT_EXECUTION_MODE")
        or "docker"
    ).strip().lower()
    if default_mode == "host" and _env_flag("GLASSHIVE_HOST_WORKERS_ENABLED", True):
        return "host"
    return "sandboxed"


def _new_workspace_options() -> list[dict[str, str]]:
    options = [
        {"value": "new:codex-cli", "label": "Codex worker", "profile": "codex-cli"},
        {"value": "new:claude-code", "label": "Claude Code worker", "profile": "claude-code"},
        {"value": "new:grok-build", "label": "Grok Build worker", "profile": "grok-build"},
        {"value": "new:openclaw-general", "label": "OpenClaw worker", "profile": "openclaw-general"},
    ]
    raw = (
        os.environ.get("GLASSHIVE_ALLOWED_WORKER_PROFILES", "").strip()
        or os.environ.get("WPR_ALLOWED_WORKER_PROFILES", "").strip()
    )
    if not raw:
        return options
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    filtered = [item for item in options if item["profile"] in allowed]
    if filtered:
        return filtered
    raise RuntimeError("GLASSHIVE_ALLOWED_WORKER_PROFILES must include at least one supported worker profile")


def _default_worker_profile() -> str:
    configured = str(os.environ.get("GLASSHIVE_DEFAULT_WORKER_PROFILE") or "").strip()
    profile = configured or "codex-cli"
    options = _new_workspace_options()
    available = {item["profile"] for item in options}
    if profile in available:
        return profile
    if configured:
        raise RuntimeError(
            "GLASSHIVE_DEFAULT_WORKER_PROFILE must be included in GLASSHIVE_ALLOWED_WORKER_PROFILES"
        )
    return str(options[0]["profile"]) if options else "codex-cli"


def _profile_allowed(profile: str) -> bool:
    if not profile:
        return False
    return profile in {item["profile"] for item in _new_workspace_options()}


def _default_workspace_option(preferences: dict[str, Any] | None = None) -> str:
    preferred = str((preferences or {}).get("default_worker_profile") or "").strip()
    profile = preferred if _profile_allowed(preferred) else _default_worker_profile()
    return f"new:{profile}"


def _effort_for_profile(profile: str, explicit_effort: str | None, preferences: dict[str, Any] | None) -> str:
    explicit = str(explicit_effort or "").strip().lower()
    if explicit:
        return explicit
    prefs = preferences or {}
    if profile == "codex-cli":
        return str(prefs.get("codex_reasoning_effort") or "").strip().lower()
    if profile == "claude-code":
        return str(prefs.get("claude_effort") or "").strip().lower()
    if profile == "openclaw-general":
        return str(prefs.get("openclaw_effort") or "").strip().lower()
    return ""


def _bootstrap_bundle_with_effort(bundle: dict[str, Any] | None, profile: str, effort: str) -> dict[str, Any] | None:
    clean_effort = str(effort or "").strip().lower()
    if not clean_effort:
        return bundle
    next_bundle: dict[str, Any] = dict(bundle or {})
    if profile == "codex-cli":
        if clean_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise HTTPException(status_code=400, detail="Codex effort must be none, minimal, low, medium, high, or xhigh")
        env = dict(next_bundle.get("env") or {})
        env["WPR_CODEX_CLI_REASONING_EFFORT"] = clean_effort
        next_bundle["env"] = env
        return next_bundle
    if profile == "claude-code":
        if clean_effort not in {"default", "max", "xhigh"}:
            raise HTTPException(status_code=400, detail="Claude effort must be default, max, or xhigh")
        if clean_effort == "default":
            return next_bundle
        env = dict(next_bundle.get("env") or {})
        env["WPR_CLAUDE_CODE_EFFORT"] = clean_effort
        next_bundle["env"] = env
        return next_bundle
    if profile == "grok-build":
        if any(ord(character) < 32 for character in clean_effort) or len(clean_effort) > 64:
            raise HTTPException(status_code=400, detail="Invalid native Grok effort ID")
        env = dict(next_bundle.get("env") or {})
        env["WPR_GROK_REASONING_EFFORT"] = clean_effort
        next_bundle["env"] = env
        return next_bundle
    elif profile == "openclaw-general":
        if clean_effort not in {"default", "high", "max"}:
            raise HTTPException(status_code=400, detail="OpenClaw effort must be default, high, or max")
    else:
        return next_bundle
    if clean_effort == "default":
        return next_bundle
    current = str(next_bundle.get("system_instructions") or "").strip()
    addition = f"Worker effort preference for this run: {clean_effort}."
    next_bundle["system_instructions"] = f"{current}\n\n{addition}".strip()
    return next_bundle


def _provider_account_selection_for_launch(
    accounts: list[dict[str, Any]],
    *,
    profile: str,
    profile_providers: dict[str, list[str]],
    requested_policy: str | None,
    requested_account_id: str | None,
) -> dict[str, str] | None:
    """Resolve one owner-scoped account without changing legacy callers that omit the fields."""

    account_id = str(requested_account_id or "").strip()
    raw_policy = str(requested_policy or "").strip().lower()
    if not raw_policy and not account_id:
        return None
    policy = raw_policy or "personal_required"
    if policy not in PROVIDER_ACCOUNT_POLICIES:
        raise HTTPException(status_code=400, detail="Unsupported worker credential policy")
    if policy == "legacy":
        if account_id:
            raise HTTPException(
                status_code=400,
                detail="Deployment account policy cannot include a personal account",
            )
        return {"policy": "legacy"}

    supported_providers = set(profile_providers.get(profile) or [])
    if not supported_providers:
        raise HTTPException(
            status_code=409,
            detail="Personal worker accounts are not available for this worker type",
        )
    normalized_accounts = [dict(account) for account in accounts if isinstance(account, dict)]
    selected: dict[str, Any] | None = None
    if account_id:
        selected = next(
            (
                account
                for account in normalized_accounts
                if str(account.get("account_id") or "").strip() == account_id
            ),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail="The selected personal account is not available for this user",
            )
        if str(selected.get("provider") or "").strip().lower() not in supported_providers:
            raise HTTPException(
                status_code=409,
                detail="The selected personal account does not match this worker type",
            )
        if str(selected.get("status") or "").strip().lower() != "ready":
            raise HTTPException(
                status_code=409,
                detail="The selected personal account is not ready; reconnect it before launching",
            )
    else:
        selected = next(
            (
                account
                for account in normalized_accounts
                if str(account.get("provider") or "").strip().lower() in supported_providers
                and str(account.get("status") or "").strip().lower() == "ready"
                and bool(account.get("is_default"))
            ),
            None,
        )
        if selected is None and policy == "personal_required":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "provider_account_required",
                    "message": "Connect an AI account to start.",
                },
            )

    selection = {"policy": policy}
    if selected is not None:
        selection["account_id"] = str(selected.get("account_id") or "").strip()
    return selection


def _bootstrap_bundle_with_provider_account(
    bundle: dict[str, Any] | None,
    selection: dict[str, str] | None,
) -> dict[str, Any] | None:
    if selection is None:
        return bundle
    next_bundle = dict(bundle or {})
    next_bundle["provider_account"] = dict(selection)
    return next_bundle


def _execution_mode_from_workspace_type(workspace_type: str | None) -> str:
    requested = str(workspace_type or _default_workspace_type()).strip().lower()
    if requested == "host":
        if _env_flag("GLASSHIVE_HOST_WORKERS_ENABLED", True):
            return "host"
        raise HTTPException(status_code=400, detail="Your Computer workspaces are not available in this GlassHive mode")
    return "docker"


def _format_launch_error(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    return "The project launch failed before the first run could start."


def create_app(runtime_client: RuntimeClient | None = None) -> FastAPI:
    _load_viventium_runtime_env()
    install_sensitive_url_log_filter()
    _validate_enterprise_startup()
    client = runtime_client or RuntimeClient()
    human_auth = HumanAuthGateway.from_env()
    if human_auth.mode == "local_password":
        human_auth.validate_local_owner()
    enterprise = _multi_user_security_enabled() or _truthy_env("GLASSHIVE_ENTERPRISE_MODE") or _truthy_env("WPR_ENTERPRISE_MODE")
    configured_auth_mode = str(os.environ.get("GLASSHIVE_AUTH_MODE") or "").strip().lower()
    auth_mode = configured_auth_mode or ("signed_internal_assertion" if _multi_user_security_enabled() else "local")
    if _multi_user_security_enabled() and auth_mode != "signed_internal_assertion":
        raise RuntimeError("GLASSHIVE_SECURITY_MODE=multi_user requires signed_internal_assertion auth")
    if _multi_user_security_enabled() and human_auth.mode != "oidc":
        raise RuntimeError(
            "GLASSHIVE_SECURITY_MODE=multi_user requires built-in OIDC human auth; "
            "plaintext trusted-proxy identity is legacy/local compatibility only"
        )
    local_human_assertions = _truthy_env("GLASSHIVE_LOCAL_HUMAN_ASSERTION")
    if local_human_assertions and (
        enterprise or auth_mode != "local" or human_auth.mode != "local_password"
        or not str(os.environ.get("WPR_API_TOKEN") or "").strip()
    ):
        raise RuntimeError("Local human assertions require first-party local password sessions and service auth")
    internal_assertion_signer = (
        InternalAssertionSigner.from_env()
        if (enterprise and auth_mode == "signed_internal_assertion") or local_human_assertions
        else None
    )
    public_links_only = _public_links_only_enabled()
    app = FastAPI(
        title="GlassHive",
        version="0.1.0",
        docs_url=None if enterprise or public_links_only else "/docs",
        redoc_url=None if enterprise or public_links_only else "/redoc",
        openapi_url=None if enterprise or public_links_only else "/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    oidc_start_limiter = _BoundedAttemptLimiter(
        window_seconds=OIDC_START_WINDOW_SECONDS,
        max_attempts=OIDC_START_MAX_ATTEMPTS,
        max_sources=OIDC_START_MAX_SOURCES,
    )

    def _admit_oidc_start(request: Request) -> None:
        source = str(request.client.host if request.client else "unknown")[:128]
        oidc_start_limiter.admit(source)

    def _session_for_request(request: Request) -> dict[str, Any] | None:
        if not human_auth.session_enabled:
            return None
        return human_auth.resolve_session(str(request.cookies.get(AUTH_SESSION_COOKIE) or ""))

    def _session_identity_for_request(request: Request | WebSocket) -> dict[str, str] | None:
        session = _session_for_request(request)
        if session is None:
            return None
        return {
            "tenant_id": str(session.get("tenant_id") or "").strip(),
            "user_id": str(session.get("user_id") or "").strip(),
            "email": str(session.get("email") or "").strip(),
            "display_name": str(session.get("display_name") or "").strip(),
            "role": str(session.get("role") or "member").strip(),
            "auth_method": str(session.get("auth_method") or "oidc").strip(),
            "auth_source": "session",
        }

    def _safe_login_return_to(request: Request) -> str:
        path = str(request.url.path or "/")
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            return "/"
        query = urlencode(
            [
                (key, value)
                for key, value in request.query_params.multi_items()
                if key not in LOGIN_RETURN_SENSITIVE_QUERY_KEYS
            ],
            doseq=True,
        )
        target = f"{path}?{query}" if query else path
        return target if len(target) <= 2048 else "/"

    def _login_redirect_if_needed(
        request: Request,
        *,
        worker_id: str | None = None,
    ) -> RedirectResponse | None:
        if not human_auth.session_enabled or _session_for_request(request) is not None:
            return None
        signed_token = _signed_token_from_request(request, worker_id)
        if signed_token:
            payload = verify_signed_link_token(signed_token)
            token_worker_id = str((payload or {}).get("worker_id") or "").strip()
            token_tenant_id = str((payload or {}).get("tenant_id") or "").strip()
            deployment_tenant_id = _enterprise_tenant_id()
            if (
                payload
                and str(payload.get("kind") or "") in _allowed_signed_link_kinds(request)
                and (not worker_id or token_worker_id == worker_id)
                and (
                    not _enterprise_mode_enabled()
                    or not deployment_tenant_id
                    or token_tenant_id == deployment_tenant_id
                )
            ):
                return None
            signed_attempt_in_url = bool(
                str(request.query_params.get("gh_token") or "").strip()
                or str(request.url.path or "").startswith("/v1/signed-links/")
            )
            if signed_attempt_in_url:
                # Keep an invalid bearer-link attempt on its bounded error path. Redirecting it
                # would either copy a secret into the login URL or silently discard the user's
                # requested capability. Invalid cookies carry no URL secret and may recover via login.
                return None
        target = _safe_login_return_to(request)
        return RedirectResponse(
            f"/login?{urlencode({'return_to': target})}",
            status_code=303,
        )

    def _oidc_error_redirect(request: Request, code: str) -> RedirectResponse:
        bounded_code = code if code in OIDC_UI_ERROR_CODES else "sign_in_failed"
        response = RedirectResponse(
            f"/login?{urlencode({'auth_error': bounded_code})}",
            status_code=303,
        )
        response.delete_cookie(AUTH_OIDC_STATE_COOKIE, path="/auth/oidc")
        return response

    def _set_auth_cookies(response: Response, request: Request, session: dict[str, Any]) -> None:
        max_age = max(1, int(float(session["expires_at"]) - time.time()))
        response.set_cookie(
            AUTH_SESSION_COOKIE,
            str(session["token"]),
            max_age=max_age,
            httponly=True,
            secure=_request_uses_https(request),
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            AUTH_CSRF_COOKIE,
            str(session["csrf_token"]),
            max_age=max_age,
            httponly=False,
            secure=_request_uses_https(request),
            samesite="strict",
            path="/",
        )

    def _request_origin_allowed(request: Request) -> bool:
        supplied = str(request.headers.get("origin") or "").strip()
        if not supplied:
            return True
        allowed = {
            str(value).strip().rstrip("/")
            for value in str(os.environ.get("GLASSHIVE_ALLOWED_ORIGINS") or "").split(",")
            if str(value).strip()
        }
        allowed.add(str(request.base_url).rstrip("/"))
        operator_url = str(os.environ.get("GLASSHIVE_OPERATOR_BASE_URL") or "").strip()
        if operator_url:
            parsed = urlparse(operator_url)
            if parsed.scheme and parsed.netloc:
                allowed.add(f"{parsed.scheme}://{parsed.netloc}")
        return supplied.rstrip("/") in allowed

    @app.middleware("http")
    async def browser_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; frame-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'self'; form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if _request_uses_https(request):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.middleware("http")
    async def session_csrf_guard(request: Request, call_next):
        if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return await call_next(request)
        if _enterprise_mode_enabled() and not _request_origin_allowed(request):
            return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed"})
        if _signed_link_file_export_request(request):
            if not request.headers.get("origin") or not _request_origin_allowed(request):
                return JSONResponse(status_code=403, content={"detail": "File export requires an allowed Origin"})
            return await call_next(request)
        if not human_auth.session_enabled:
            return await call_next(request)
        if request.url.path == "/auth/oidc/callback":
            return await call_next(request)
        if request.url.path in {
            "/auth/email/login",
            "/auth/email/register",
            "/auth/email/reset",
        }:
            # This unauthenticated endpoint owns a separate strict-Origin and
            # double-submit login-CSRF contract below. Registration and reset
            # remain deliberately unimplemented and therefore reach a real 404.
            return await call_next(request)
        if not _request_origin_allowed(request):
            return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed"})
        # A worker-view link is already an expiring, worker-bound authorization
        # token. Preserve its deliberately narrow message/steer compatibility
        # path without turning signed links into a general CSRF bypass.
        if _valid_signed_link_communication_request(request):
            return await call_next(request)
        supplied = str(request.headers.get("x-glasshive-csrf") or "").strip()
        if (
            not supplied
            and request.method.upper() == "POST"
            and (request.url.path == "/api/file-uploads/export" or _workspace_file_export_worker_id(request))
            and request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded")
        ):
            try:
                fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
                token_field = "draft_export_csrf" if request.url.path == "/api/file-uploads/export" else "workspace_export_csrf"
                tokens = fields.get(token_field, [])
                if len(tokens) == 1:
                    supplied = tokens[0]
            except UnicodeDecodeError:
                pass
        cookie_value = str(request.cookies.get(AUTH_CSRF_COOKIE) or "").strip()
        session = _session_for_request(request)
        valid = bool(supplied and cookie_value and hmac.compare_digest(supplied, cookie_value))
        if valid and session is not None:
            valid = human_auth.session_csrf_valid(session, supplied)
        if not valid:
            return JSONResponse(status_code=403, content={"detail": "Invalid or missing CSRF token"})
        return await call_next(request)

    @app.get("/auth/config")
    def auth_config() -> dict[str, object]:
        provider_email_login = human_auth.mode == "oidc" and human_auth.provider_email_login
        local_password_login = human_auth.mode in {"oidc", "local_password"} and bool(
            getattr(human_auth, "local_password_login", False)
        )
        oidc_login_visible = human_auth.mode == "oidc" and bool(
            getattr(human_auth, "oidc_login_visible", True)
        )
        login_methods = [
            method
            for method, enabled in (
                ("oidc", oidc_login_visible),
                ("local_password", local_password_login),
            )
            if enabled
        ]
        return {
            "mode": human_auth.mode,
            # Keep the two legacy keys for older login assets, but do not claim
            # GlassHive itself creates identity-provider accounts.
            "email_login": provider_email_login or local_password_login,
            "email_registration": False,
            "provider_email_login": provider_email_login,
            "local_password_login": local_password_login,
            "local_password_signup": False,
            "principal_enrollment": human_auth.mode == "oidc" and human_auth.allow_registration,
            "identity_owner": "local_owner" if human_auth.mode == "local_password" else "external_provider",
            "oidc": human_auth.mode == "oidc",
            "oidc_login_visible": oidc_login_visible,
            "login_methods": login_methods,
        }

    @app.get("/auth/session")
    def auth_session(request: Request) -> JSONResponse:
        session = _session_for_request(request)
        csrf_cookie = str(request.cookies.get(AUTH_CSRF_COOKIE) or "").strip()
        if session is not None and human_auth.session_csrf_valid(session, csrf_cookie):
            payload = {key: value for key, value in session.items() if not key.startswith("_")}
            payload.update({"authenticated": True, "csrf_token": csrf_cookie})
            return JSONResponse(payload)
        return JSONResponse({"authenticated": False, "csrf_token": ""})

    @app.post("/auth/logout")
    def auth_logout(
        request: Request,
        payload: dict[str, str] | None = Body(default=None),
    ) -> JSONResponse:
        requested_scope = str((payload or {}).get("scope") or "local").strip().lower()
        human_auth.revoke_session(str(request.cookies.get(AUTH_SESSION_COOKIE) or ""))
        redirect_url = "/login?logged_out=local"
        completed_scope = "local"
        if requested_scope == "provider":
            try:
                provider_url = human_auth.provider_logout_url()
            except AuthGatewayError:
                provider_url = ""
            if provider_url:
                redirect_url = provider_url
                completed_scope = "provider"
            else:
                redirect_url = "/login?logged_out=local&provider_logout=unavailable"
        response = JSONResponse(
            {
                "authenticated": False,
                "logout_scope": completed_scope,
                "redirect_url": redirect_url,
            }
        )
        response.delete_cookie(AUTH_SESSION_COOKIE, path="/")
        response.delete_cookie(AUTH_CSRF_COOKIE, path="/")
        response.delete_cookie(AUTH_CSRF_COOKIE, path="/auth")
        return response

    @app.get("/auth/oidc/start")
    def auth_oidc_start(request: Request) -> RedirectResponse:
        _admit_oidc_start(request)
        try:
            flow = human_auth.begin_oidc(return_to=str(request.query_params.get("return_to") or "/"))
        except AuthGatewayError as exc:
            return _oidc_error_redirect(request, exc.code)
        response = RedirectResponse(str(flow["authorization_url"]), status_code=303)
        response.set_cookie(
            AUTH_OIDC_STATE_COOKIE,
            str(flow["state"]),
            max_age=10 * 60,
            httponly=True,
            secure=_request_uses_https(request),
            samesite="lax",
            path="/auth/oidc",
        )
        return response

    @app.get("/auth/oidc/callback")
    def auth_oidc_callback(request: Request) -> Response:
        error = str(request.query_params.get("error") or "").strip()
        if error:
            code = "access_denied" if error == "access_denied" else "cancelled"
            return _oidc_error_redirect(request, code)
        state = str(request.query_params.get("state") or "").strip()
        cookie_state = str(request.cookies.get(AUTH_OIDC_STATE_COOKIE) or "").strip()
        if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
            return _oidc_error_redirect(request, "state_invalid")
        try:
            completed = human_auth.complete_oidc(
                state=state,
                code=str(request.query_params.get("code") or ""),
            )
            principal = completed["principal"]
            session = human_auth.create_session(str(principal["user_id"]))
        except AuthGatewayError as exc:
            return _oidc_error_redirect(request, exc.code)
        response = RedirectResponse(str(completed["return_to"]), status_code=303)
        response.delete_cookie(AUTH_OIDC_STATE_COOKIE, path="/auth/oidc")
        _set_auth_cookies(response, request, session)
        return response

    @app.get("/auth/email/login")
    def auth_email_login_get() -> None:
        raise HTTPException(status_code=404, detail="Not found")

    @app.post("/auth/email/login")
    async def auth_email_login(request: Request) -> JSONResponse:
        if human_auth.mode not in {"oidc", "local_password"} or not bool(
            getattr(human_auth, "local_password_login", False)
        ):
            raise HTTPException(status_code=404, detail="Not found")
        origin = str(request.headers.get("origin") or "").strip()
        if not origin or not _request_origin_allowed(request):
            return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed"})
        supplied_csrf = str(request.headers.get("x-glasshive-csrf") or "").strip()
        cookie_csrf = str(request.cookies.get(AUTH_LOGIN_CSRF_COOKIE) or "").strip()
        if not supplied_csrf or not cookie_csrf or not hmac.compare_digest(supplied_csrf, cookie_csrf):
            return JSONResponse(status_code=403, content={"detail": "Invalid login request"})
        content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse(status_code=415, content={"detail": "JSON is required"})
        try:
            content_length = int(str(request.headers.get("content-length") or "0"))
        except ValueError:
            content_length = LOCAL_LOGIN_MAX_BODY_BYTES + 1
        if content_length > LOCAL_LOGIN_MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"detail": "Sign-in request is too large"})
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > LOCAL_LOGIN_MAX_BODY_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Sign-in request is too large"})
        try:
            payload = json.loads(bytes(body))
        except (TypeError, ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"detail": "Invalid sign-in request"})
        email = HumanAuthGateway.LOCAL_LOGIN if human_auth.mode == "local_password" else payload.get("email")
        password = payload.get("password")
        return_to_value = payload.get("return_to", "/")
        try:
            email_bytes = email.encode("utf-8") if isinstance(email, str) else b""
            password_bytes = password.encode("utf-8") if isinstance(password, str) else b""
            return_to_bytes = (
                return_to_value.encode("utf-8")
                if isinstance(return_to_value, str)
                else b""
            )
        except UnicodeEncodeError:
            return JSONResponse(status_code=400, content={"detail": "Invalid sign-in request"})
        if (
            not isinstance(email, str)
            or not isinstance(password, str)
            or not isinstance(return_to_value, str)
            or len(email) > 320
            or len(email_bytes) > 1280
            or len(password) > 128
            or len(password_bytes) > 1024
            or len(return_to_value) > 2048
            or len(return_to_bytes) > 8192
        ):
            return JSONResponse(status_code=400, content={"detail": "Invalid sign-in request"})
        safe_return_to = return_to_value
        if (
            not safe_return_to.startswith("/")
            or safe_return_to.startswith("//")
            or "\\" in safe_return_to
        ):
            safe_return_to = "/"
        try:
            session = await run_in_threadpool(
                human_auth.authenticate_local_password,
                login_email=email,
                password=password,
                source=str(request.client.host if request.client else "unknown")[:256],
            )
        except AuthGatewayError as exc:
            if exc.code == "sign_in_busy":
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Sign-in is temporarily busy; retry shortly"},
                )
            return JSONResponse(
                status_code=401,
                content={"detail": "Email or password is incorrect"},
            )
        response = JSONResponse(
            {"authenticated": True, "redirect_url": safe_return_to}
        )
        _set_auth_cookies(response, request, session)
        response.delete_cookie(AUTH_LOGIN_CSRF_COOKIE, path="/")
        return response

    @app.get("/login")
    def login_page(request: Request) -> FileResponse:
        if not human_auth.session_enabled:
            raise HTTPException(status_code=404, detail="Not found")
        response = FileResponse(STATIC_DIR / "login.html")
        response.headers["Cache-Control"] = "no-store, no-cache, private, max-age=0"
        response.headers["Pragma"] = "no-cache"
        if bool(getattr(human_auth, "local_password_login", False)):
            response.set_cookie(
                AUTH_LOGIN_CSRF_COOKIE,
                secrets.token_urlsafe(32),
                max_age=10 * 60,
                httponly=False,
                secure=_request_uses_https(request),
                samesite="strict",
                path="/",
            )
        return response

    @app.get("/.well-known/jwks.json")
    def internal_assertion_jwks() -> dict[str, object]:
        if internal_assertion_signer is None:
            raise HTTPException(status_code=404, detail="Not found")
        return internal_assertion_signer.jwks()

    def _incoming_identity_header(request: Request, name: str) -> str:
        aliases = {
            "X-Viventium-Tenant-Id": ("X-GlassHive-Tenant-Id", "X-LibreChat-Tenant-Id"),
            "X-Viventium-User-Id": ("X-GlassHive-User-Id", "X-LibreChat-User-Id"),
            "X-Viventium-User-Email": ("X-GlassHive-User-Email", "X-LibreChat-User-Email"),
            "X-Viventium-User-Role": ("X-GlassHive-User-Role", "X-LibreChat-User-Role"),
        }
        for candidate in (name, *aliases.get(name, ())):
            value = str(request.headers.get(candidate) or "").strip()
            if value:
                return value
        return ""

    def _enterprise_mode_enabled() -> bool:
        return _multi_user_security_enabled() or _truthy_env("GLASSHIVE_ENTERPRISE_MODE") or _truthy_env("WPR_ENTERPRISE_MODE")

    def _enterprise_tenant_id() -> str:
        return str(
            os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID")
            or os.environ.get("WPR_ENTERPRISE_TENANT_ID")
            or ""
        ).strip()

    def _allow_default_owner() -> bool:
        if not _enterprise_mode_enabled():
            return True
        if _multi_user_security_enabled():
            return False
        return _truthy_env("GLASSHIVE_ALLOW_LOCAL_DEMO_OWNER")

    def _worker_cookie_name(worker_id: str) -> str:
        if not SAFE_WORKER_ID_RE.match(str(worker_id or "")):
            raise HTTPException(status_code=400, detail="Invalid worker id")
        digest = sha256(str(worker_id).encode("utf-8")).hexdigest()[:24]
        return f"glasshive_gh_token_{digest}"

    def _signed_token_from_request(request: Request | WebSocket, worker_id: str | None = None) -> str:
        path = str(request.url.path or "")
        file_cookie_only = bool(getattr(request.state, "file_cookie_only", False))
        if not file_cookie_only and _public_links_only_enabled() and path.startswith("/v1/link-refs/"):
            ref_id = unquote(path.removeprefix("/v1/link-refs/")).strip().split("/", 1)[0]
            record = resolve_signed_link_ref(ref_id)
            return str((record or {}).get("token") or "").strip()
        token = "" if file_cookie_only else str(request.query_params.get("gh_token") or "").strip()
        if token:
            return token
        if not file_cookie_only and path.startswith("/v1/signed-links/"):
            token = unquote(path.removeprefix("/v1/signed-links/")).strip()
            if token:
                return token
        if not file_cookie_only and _public_links_only_enabled() and path.startswith("/v1/link-refs/"):
            ref_id = unquote(path.removeprefix("/v1/link-refs/")).strip().split("/", 1)[0]
            record = resolve_signed_link_ref(ref_id)
            token = str((record or {}).get("token") or "").strip()
            if token:
                return token
        cookie_worker_id = str(worker_id or request.path_params.get("worker_id") or "").strip()
        if cookie_worker_id:
            token = str(request.cookies.get(_worker_cookie_name(cookie_worker_id)) or "").strip()
            if token:
                return token
        if file_cookie_only:
            return ""
        referer = str(request.headers.get("referer") or "").strip()
        if not referer:
            return ""
        parsed = urlparse(referer)
        return str(parse_qs(parsed.query).get("gh_token", [""])[0]).strip()

    def _request_uses_https(request: Request) -> bool:
        forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
        return (
            _multi_user_security_enabled()
            or request.url.scheme == "https"
            or forwarded_proto == "https"
            or _truthy_env("GLASSHIVE_COOKIE_SECURE")
        )

    def _set_signed_worker_cookie(response: Response, request: Request, worker_id: str) -> None:
        token = _signed_token_from_request(request, worker_id)
        payload = verify_signed_link_token(token) if token else None
        if (
            payload
            and str(payload.get("kind") or "") == "worker_view"
            and str(payload.get("worker_id") or "").strip() == str(worker_id or "").strip()
        ):
            _set_worker_cookie_from_payload(response, request, worker_id, payload)

    def _set_worker_cookie_from_payload(
        response: Response,
        request: Request,
        worker_id: str,
        payload: dict[str, object],
    ) -> None:
        token, refreshed_payload = _fresh_worker_view_token_from_payload(worker_id, payload)
        try:
            cookie_max_age = max(
                1,
                min(30 * 60, int(refreshed_payload.get("exp") or 0) - int(time.time())),
            )
        except (TypeError, ValueError):
            cookie_max_age = 30 * 60
        response.set_cookie(
            _worker_cookie_name(worker_id),
            token,
            max_age=cookie_max_age,
            httponly=True,
            samesite="lax",
            secure=_request_uses_https(request),
        )

    def _file_response_with_signed_cookie(request: Request, worker_id: str, path: Path) -> FileResponse:
        response = FileResponse(path)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "same-origin"
        _set_signed_worker_cookie(response, request, worker_id)
        return response

    def _json_response_with_signed_cookie(request: Request, worker_id: str, payload: dict[str, Any]) -> JSONResponse:
        response = JSONResponse(payload)
        _set_signed_worker_cookie(response, request, worker_id)
        return response

    def _allowed_signed_link_kinds(request: Request | WebSocket) -> set[str]:
        path = str(request.url.path or "")
        if path.startswith("/v1/signed-links/") or (
            _public_links_only_enabled() and path.startswith("/v1/link-refs/")
        ):
            return {"artifact_download", "artifact_open"}
        return {"worker_view"}

    def _signed_link_payload(request: Request | WebSocket, worker_id: str | None = None) -> dict[str, object] | None:
        token = _signed_token_from_request(request, worker_id)
        if not token:
            return None
        path = str(request.url.path or "")
        link_ref_request = (
            _public_links_only_enabled()
            and path.startswith("/v1/link-refs/")
        )
        payload = (
            verify_signed_link_ref_token(token)
            if link_ref_request
            else verify_signed_link_token(token)
        )
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired GlassHive workspace link")
        if str(payload.get("kind") or "") not in _allowed_signed_link_kinds(request):
            raise HTTPException(status_code=403, detail="This GlassHive link cannot open a workspace")
        token_worker_id = str(payload.get("worker_id") or "").strip()
        if worker_id and token_worker_id != worker_id:
            raise HTTPException(status_code=403, detail="This GlassHive link is for a different workspace")
        token_tenant_id = str(payload.get("tenant_id") or "").strip()
        deployment_tenant_id = _enterprise_tenant_id()
        if _enterprise_mode_enabled() and deployment_tenant_id and token_tenant_id != deployment_tenant_id:
            raise HTTPException(status_code=401, detail="GlassHive workspace link is for a different tenant")
        if str(payload.get("kind") or "") == "worker_view" and token_worker_id:
            _ensure_signed_worker_watch_session(token_worker_id, payload)
        return payload

    def _uses_worker_view_credential(
        request: Request | WebSocket,
        worker_id: str | None = None,
    ) -> bool:
        """Return true when this request's authority is a shareable view token."""

        tokens = [_signed_token_from_request(request, worker_id)]
        # HTTP middleware runs before route path parameters are populated, so
        # inspect the worker-scoped cookie namespace directly as well. One
        # read-only view cookie can never be treated as ambient member auth for
        # an account-wide or differently shaped mutation route.
        tokens.extend(
            str(value or "").strip()
            for name, value in request.cookies.items()
            if str(name).startswith("glasshive_gh_token_")
        )
        for token in tokens:
            if not token:
                continue
            payload = verify_signed_link_token(token)
            if isinstance(payload, dict) and str(payload.get("kind") or "") == "worker_view":
                return True
        return False

    @app.middleware("http")
    async def local_operator_session_required(request: Request, call_next):
        if human_auth.mode != "local_password":
            return await call_next(request)
        path = request.url.path
        public = {"/health", "/login", "/auth/config", "/auth/session", "/auth/email/login", "/favicon.ico"}
        if path in public or path.startswith(("/static/", "/r/", "/w/")):
            return await call_next(request)
        try:
            _request_identity(request)
        except HTTPException as exc:
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/login?" + urlencode({"return_to": _safe_login_return_to(request)}), status_code=303)
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    @app.middleware("http")
    async def worker_view_is_read_only(request: Request, call_next):
        # A viewRef is intentionally reusable for bounded observation. It is
        # never an account/member/action capability—even after the watch page
        # moves it to an HttpOnly cookie or a browser sends it via Referer.
        # When the signed-in owner session is the request's authority (the
        # same precedence as _request_identity), a leftover view cookie from
        # watching a launch must not refuse that owner's own actions.
        owner_session_authority = (
            not _public_links_only_enabled() and _session_for_request(request) is not None
        )
        if (
            request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
            and not owner_session_authority
            and _uses_worker_view_credential(request)
            and not _valid_signed_link_communication_request(request)
            and not _workspace_file_export_worker_id(request)
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "This GlassHive workspace link is read-only. Use the authenticated "
                        "Parallel Work action controls to change or stop work."
                    )
                },
            )
        return await call_next(request)

    def _signed_link_identity(request: Request | WebSocket, worker_id: str | None = None) -> dict[str, str] | None:
        payload = _signed_link_payload(request, worker_id)
        if not payload:
            return None
        token_tenant_id = str(payload.get("tenant_id") or "").strip()
        return {
            "tenant_id": token_tenant_id,
            "user_id": str(payload.get("owner_id") or "").strip(),
            "email": "",
            "role": "viewer",
            "auth_source": "signed_link",
        }

    def _watch_session_timeout_seconds(request: Request | WebSocket, worker_id: str) -> float:
        raw = os.environ.get("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "").strip()
        try:
            configured = int(raw) if raw else 0
        except ValueError:
            configured = 0
        payload = _signed_link_payload(request, worker_id)
        now = int(time.time())
        signed_remaining = 0
        if payload:
            try:
                signed_remaining = int(payload.get("exp") or 0) - now
            except (TypeError, ValueError):
                signed_remaining = 0
        persisted_remaining = 0
        if payload and str(payload.get("kind") or "") == "worker_view":
            persisted = _existing_watch_session_expires_at(
                worker_id,
                {
                    "tenant_id": str(payload.get("tenant_id") or "").strip(),
                    "user_id": str(payload.get("owner_id") or "").strip(),
                },
            )
            if persisted is not None:
                persisted_remaining = persisted - now
        values = [value for value in (configured, signed_remaining, persisted_remaining) if value > 0]
        return float(max(1, min(values))) if values else 0.0

    def _request_identity(request: Request | WebSocket, worker_id: str | None = None) -> dict[str, str]:
        if _public_links_only_enabled():
            signed_identity = _signed_link_identity(request, worker_id)
            if signed_identity is not None:
                return signed_identity
            raise HTTPException(
                status_code=401,
                detail="This public GlassHive surface requires a signed workspace or artifact link",
            )

        session_identity = _session_identity_for_request(request)
        if session_identity is not None:
            return session_identity

        if _public_links_only_enabled():
            raise HTTPException(
                status_code=401,
                detail="This public GlassHive surface requires a signed workspace or artifact link",
            )

        enterprise = _enterprise_mode_enabled()
        trust_inbound_identity = _truthy_env("GLASSHIVE_TRUST_INBOUND_IDENTITY")
        tenant_id = _enterprise_tenant_id()
        user_id = str(os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID") or "demo-owner").strip()
        email = ""
        role = ""

        if trust_inbound_identity:
            asserted_tenant = _incoming_identity_header(request, "X-Viventium-Tenant-Id")
            asserted_user = _incoming_identity_header(request, "X-Viventium-User-Id")
            asserted_email = _incoming_identity_header(request, "X-Viventium-User-Email")
            asserted_role = _incoming_identity_header(request, "X-Viventium-User-Role")
            if any((asserted_tenant, asserted_user, asserted_email, asserted_role)):
                if enterprise and asserted_tenant and tenant_id and asserted_tenant != tenant_id:
                    raise HTTPException(
                        status_code=401,
                        detail="GlassHive tenant assertion does not match this deployment",
                    )
                tenant_id = asserted_tenant or tenant_id
                user_id = asserted_user or (user_id if _allow_default_owner() else "")
                email = asserted_email
                role = asserted_role
                if human_auth.allowed_email_domains and not human_auth.email_allowed(email):
                    raise HTTPException(
                        status_code=403,
                        detail="This account is outside the approved email domains",
                    )
                if enterprise and not user_id:
                    raise HTTPException(
                        status_code=401,
                        detail="GlassHive enterprise UI requires an authenticated user assertion from the trusted proxy",
                    )
                return {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "email": email,
                    "role": role,
                    "auth_source": "trusted_proxy",
                }

        signed_identity = _signed_link_identity(request, worker_id)
        if signed_identity is not None:
            return signed_identity

        if human_auth.session_enabled and not trust_inbound_identity:
            raise HTTPException(status_code=401, detail="Sign in to continue")
        if trust_inbound_identity:
            user_id = user_id if _allow_default_owner() else ""
        elif not _allow_default_owner():
            user_id = ""

        if enterprise and not user_id:
            raise HTTPException(
                status_code=401,
                detail="GlassHive enterprise UI requires an authenticated user assertion from the trusted proxy",
            )

        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "email": email,
            "role": role,
            "auth_source": "local",
        }

    def _normalized_identity_role(identity: dict[str, str]) -> str:
        incoming_role = str(identity.get("role") or "").strip().lower()
        aliases = {
            "admin": "tenant_admin",
            "owner": "tenant_admin",
            "operator": "member",
            "local_operator": "member",
        }
        normalized = aliases.get(incoming_role, incoming_role)
        if normalized in {"member", "viewer", "tenant_admin", "service"}:
            return normalized
        if not normalized and str(identity.get("auth_source") or "") != "signed_link":
            return "member"
        return "viewer"

    def _restricted_identity(identity: dict[str, str]) -> bool:
        return (
            str(identity.get("auth_source") or "") == "signed_link"
            or _normalized_identity_role(identity) == "viewer"
        )

    def _require_tenant_admin(request: Request) -> dict[str, str]:
        identity = _request_identity(request)
        if _normalized_identity_role(identity) != "tenant_admin":
            raise HTTPException(status_code=403, detail="Tenant administrator role required")
        if not human_auth.session_enabled:
            raise HTTPException(status_code=404, detail="User administration is not available")
        return identity

    def _request_method(request: Request | WebSocket) -> str:
        return str(getattr(request, "method", "GET") or "GET").upper()

    def _signed_link_communication_allowed(
        request: Request | WebSocket,
        worker_id: str | None,
        identity: dict[str, str],
    ) -> bool:
        if str(identity.get("auth_source") or "") != "signed_link":
            return False
        if _request_method(request) != "POST" or not worker_id or not SAFE_WORKER_ID_RE.fullmatch(worker_id):
            return False
        path = str(request.url.path or "")
        return path in {
            f"/api/workspace/{worker_id}/message",
            f"/api/worker/{worker_id}/message",
            f"/api/workspace/{worker_id}/steer",
            f"/api/worker/{worker_id}/steer",
        }

    def _valid_signed_link_communication_request(request: Request) -> bool:
        path_match = re.fullmatch(
            r"/api/(?:workspace|worker)/(?P<worker_id>[A-Za-z0-9._-]{1,128})/(?P<action>message|steer)",
            str(request.url.path or ""),
        )
        if request.method.upper() != "POST" or path_match is None:
            return False
        worker_id = path_match.group("worker_id")
        try:
            identity = _request_identity(request, worker_id)
        except HTTPException:
            return False
        return _signed_link_communication_allowed(request, worker_id, identity)

    def _workspace_file_export_worker_id(request: Request) -> str | None:
        if _request_method(request) != "POST":
            return None
        match = re.fullmatch(r"/api/workspace/([A-Za-z0-9._-]{1,128})/files/export", request.url.path)
        return match.group(1) if match else None

    def _signed_link_file_export_request(request: Request) -> bool:
        # This exact POST only reads bytes, as its GET counterpart does. A view
        # cookie needs its own worker binding and Origin; URL tokens never qualify.
        worker_id = _workspace_file_export_worker_id(request)
        if not worker_id:
            return False
        request.state.file_cookie_only = True
        try:
            identity = _request_identity(request, worker_id)
        except HTTPException:
            return False
        return identity.get("auth_source") == "signed_link"

    def _require_interactive_access(request: Request | WebSocket, worker_id: str) -> None:
        identity = _request_identity(request, worker_id)
        if _restricted_identity(identity):
            raise HTTPException(
                status_code=403,
                detail="Interactive desktop access requires a workspace member",
            )

    def _trusted_proxy_identity_for_short_ref(request: Request) -> dict[str, str] | None:
        if not _enterprise_mode_enabled():
            return None
        tenant_id = _enterprise_tenant_id()
        user_id = str(os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID") or "demo-owner").strip()
        asserted_email = ""
        if _truthy_env("GLASSHIVE_TRUST_INBOUND_IDENTITY"):
            asserted_tenant = _incoming_identity_header(request, "X-Viventium-Tenant-Id")
            asserted_user = _incoming_identity_header(request, "X-Viventium-User-Id")
            asserted_email = _incoming_identity_header(request, "X-Viventium-User-Email")
            asserted_role = _incoming_identity_header(request, "X-Viventium-User-Role")
            if not any((asserted_tenant, asserted_user, asserted_email, asserted_role)):
                if not _allow_default_owner():
                    raise HTTPException(
                        status_code=401,
                        detail="GlassHive enterprise UI requires an authenticated user assertion from the trusted proxy",
                    )
            if asserted_tenant and tenant_id and asserted_tenant != tenant_id:
                raise HTTPException(status_code=401, detail="GlassHive tenant assertion does not match this deployment")
            tenant_id = asserted_tenant or tenant_id
            user_id = asserted_user or (user_id if _allow_default_owner() else "")
        elif not _allow_default_owner():
            raise HTTPException(
                status_code=401,
                detail="GlassHive enterprise UI requires an authenticated user assertion from the trusted proxy",
            )
        if not user_id:
            raise HTTPException(
                status_code=401,
                detail="GlassHive enterprise UI requires an authenticated user assertion from the trusted proxy",
            )
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "email": asserted_email,
        }

    def _authenticated_identity_for_short_ref(request: Request) -> dict[str, str] | None:
        session_identity = _session_identity_for_request(request)
        if session_identity is not None:
            return session_identity
        return _trusted_proxy_identity_for_short_ref(request)

    def _require_short_ref_owner(payload: dict[str, object], request: Request) -> None:
        identity = _authenticated_identity_for_short_ref(request)
        if identity is None:
            return
        tenant_id = str(payload.get("tenant_id") or "").strip()
        owner_id = str(payload.get("owner_id") or "").strip()
        if tenant_id != str(identity.get("tenant_id") or "").strip() or not _owner_matches_identity(owner_id, identity):
            raise HTTPException(status_code=404, detail="GlassHive workspace link not found for this user")

    def _fresh_worker_view_token_from_payload(worker_id: str, payload: dict[str, object]) -> tuple[str, dict[str, object]]:
        token = _worker_view_token(
            worker_id,
            {
                "tenant_id": str(payload.get("tenant_id") or "").strip(),
                "user_id": str(payload.get("owner_id") or "").strip(),
            },
        )
        refreshed_payload = verify_signed_link_token(token) if token else None
        if not token or not isinstance(refreshed_payload, dict):
            raise HTTPException(status_code=500, detail="GlassHive workspace session could not be refreshed")
        return token, refreshed_payload

    def _runtime_headers_for_request(
        request: Request | WebSocket,
        worker_id: str | None = None,
        *,
        role_override: str | None = None,
        human_confirmation: bool = False,
        file_write_scope: bool = False,
    ) -> dict[str, str]:
        api_token = str(os.environ.get("WPR_API_TOKEN") or "").strip()
        identity = _request_identity(request, worker_id) if _public_links_only_enabled() else None
        if not api_token:
            if _enterprise_mode_enabled():
                raise HTTPException(status_code=503, detail="GlassHive enterprise UI is missing service authentication")
            if human_auth.session_enabled:
                _request_identity(request, worker_id)
            return {}
        identity = identity or _request_identity(request, worker_id)
        if human_confirmation and local_human_assertions:
            if not isinstance(request, Request):
                raise HTTPException(status_code=403, detail="A signed-in owner session is required")
            session = _session_for_request(request)
            supplied = str(request.headers.get("x-glasshive-csrf") or "").strip()
            cookie = str(request.cookies.get(AUTH_CSRF_COOKIE) or "").strip()
            if (
                session is None or identity.get("auth_source") != "session"
                or not str(request.headers.get("origin") or "").strip()
                or not _request_origin_allowed(request)
                or not supplied or not cookie or not hmac.compare_digest(supplied, cookie)
                or not human_auth.session_csrf_valid(session, supplied)
                or str(session.get("user_id") or "") != identity.get("user_id")
                or str(session.get("tenant_id") or "") != identity.get("tenant_id")
            ):
                raise HTTPException(status_code=403, detail="A signed-in owner session is required")
        assertion_role = _normalized_identity_role(identity)
        read_only = (
            _request_method(request) in {"GET", "HEAD", "OPTIONS"}
            or (worker_id is not None and _workspace_file_export_worker_id(request) == worker_id)
        ) and not file_write_scope
        restricted = _restricted_identity(identity)
        narrow_communication = _signed_link_communication_allowed(request, worker_id, identity)
        if restricted and not read_only and not narrow_communication:
            raise HTTPException(
                status_code=403,
                detail="This restricted workspace identity cannot perform that action",
            )
        headers = {"X-WPR-Token": api_token}
        if internal_assertion_signer is not None and (
            not local_human_assertions or identity.get("auth_source") == "session"
        ):
            scopes = ["runtime:access", "workspaces:read"]
            if narrow_communication:
                scopes.append("workspaces:communicate")
            elif not restricted and not read_only:
                scopes.append("workspaces:write")
            if role_override and not restricted:
                scopes.append("runtime:internal_details")
            if human_confirmation and not restricted:
                scopes.append("human:confirm")
            headers["X-GlassHive-User-Assertion"] = internal_assertion_signer.sign(
                subject=identity["user_id"],
                tenant_id=identity["tenant_id"],
                email=identity["email"],
                role=assertion_role,
                scopes=scopes,
            )
            return headers
        if identity["tenant_id"]:
            headers["X-Viventium-Tenant-Id"] = identity["tenant_id"]
        if identity["user_id"]:
            headers["X-Viventium-User-Id"] = identity["user_id"]
        if identity["email"]:
            headers["X-Viventium-User-Email"] = identity["email"]
        role = (
            assertion_role
            if restricted
            else (role_override or str(identity.get("role") or "").strip())
        )
        if role:
            headers["X-Viventium-User-Role"] = role
        return headers

    def _client_for_request(
        request: Request,
        worker_id: str | None = None,
        *,
        internal_details: bool = False,
        human_confirmation: bool = False,
        file_write_scope: bool = False,
    ) -> RuntimeClient:
        # The browser should never receive raw noVNC/runtime internals, but the
        # UI backend needs them to proxy the scoped desktop surface.
        role_override = "operator" if internal_details else None
        if internal_assertion_signer is not None and hasattr(client, "with_headers_factory"):
            return client.with_headers_factory(
                lambda: _runtime_headers_for_request(
                    request,
                    worker_id,
                    role_override=role_override,
                    human_confirmation=human_confirmation,
                    file_write_scope=file_write_scope,
                )
            )
        headers = _runtime_headers_for_request(
            request,
            worker_id,
            role_override=role_override,
            human_confirmation=human_confirmation,
            file_write_scope=file_write_scope,
        )
        if not headers or not hasattr(client, "with_headers"):
            return client
        return client.with_headers(headers)

    def _runtime_headers_for_short_ref_payload(payload: dict[str, object]) -> dict[str, str]:
        api_token = str(os.environ.get("WPR_API_TOKEN") or "").strip()
        if not api_token:
            if _enterprise_mode_enabled():
                raise HTTPException(
                    status_code=503,
                    detail="GlassHive enterprise UI is missing service authentication",
                )
            return {}
        headers = {"X-WPR-Token": api_token}
        tenant_id = str(payload.get("tenant_id") or "").strip()
        owner_id = str(payload.get("owner_id") or "").strip()
        if _enterprise_mode_enabled() and (not tenant_id or not owner_id):
            raise HTTPException(status_code=401, detail="GlassHive workspace identity is invalid")
        if tenant_id:
            headers["X-Viventium-Tenant-Id"] = tenant_id
        if owner_id:
            headers["X-Viventium-User-Id"] = owner_id
        headers["X-Viventium-User-Role"] = "viewer"
        return headers

    def _client_for_short_ref_payload(payload: dict[str, object]) -> RuntimeClient:
        api_token = str(os.environ.get("WPR_API_TOKEN") or "").strip()
        if not api_token or not hasattr(client, "with_headers"):
            return client
        tenant_id = str(payload.get("tenant_id") or "").strip()
        owner_id = str(payload.get("owner_id") or "").strip()

        def scoped_headers() -> dict[str, str]:
            headers = {"X-WPR-Token": api_token}
            if internal_assertion_signer is not None:
                headers["X-GlassHive-User-Assertion"] = internal_assertion_signer.sign(
                    subject=owner_id,
                    tenant_id=tenant_id,
                    email="",
                    role="viewer",
                    scopes=("runtime:access", "workspaces:read"),
                )
                return headers
            if tenant_id:
                headers["X-Viventium-Tenant-Id"] = tenant_id
            if owner_id:
                headers["X-Viventium-User-Id"] = owner_id
            headers["X-Viventium-User-Role"] = "viewer"
            return headers

        if internal_assertion_signer is not None and hasattr(client, "with_headers_factory"):
            return client.with_headers_factory(scoped_headers)
        return client.with_headers(scoped_headers())
    def _record_workspace_link_open(payload: dict[str, object], worker_id: str) -> None:
        """Require the runtime's scoped, current worker authorization."""

        scoped_client = _client_for_short_ref_payload(payload)
        try:
            scoped_client.record_worker_view_open(worker_id)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 503
            if status_code not in {401, 403, 404, 410}:
                status_code = 503
            raise HTTPException(
                status_code=status_code,
                detail="GlassHive workspace link is no longer available",
            ) from exc
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503,
                detail="GlassHive runtime could not authorize this workspace link",
            ) from exc
        except Exception as exc:
            # Any unexpected client/transport failure is denial, never
            # permission to fall back to the duplicate ambient verifier.
            raise HTTPException(
                status_code=503,
                detail="GlassHive runtime could not authorize this workspace link",
            ) from exc
        # Opening a reusable view capability must never mutate mission state.
        # Resume belongs to the owner-scoped, idempotent account action plane.

    def _require_ui_auth(request: Request, worker_id: str | None = None) -> None:
        _runtime_headers_for_request(request, worker_id)
        if worker_id:
            payload = _signed_link_payload(request, worker_id)
            if isinstance(payload, dict) and str(payload.get("kind") or "") == "worker_view":
                _record_workspace_link_open(payload, worker_id)

    def _owner_id_for_request(request: Request) -> str:
        identity = _request_identity(request)
        return identity["user_id"] or os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")

    def _browser_live_payload(payload: dict[str, Any]) -> dict[str, Any]:
        safe = dict(payload)
        worker = dict(safe.get("worker") or {})
        for key in ("gateway_url", "gateway_token", "session_key", "workspace_dir", "state_dir", "home_dir", "container_name"):
            worker.pop(key, None)
        safe["worker"] = worker
        runtime = dict(safe.get("runtime_details") or {})
        view_available = bool(runtime.get("view_available")) if "view_available" in runtime else bool(runtime.get("view_url"))
        safe["runtime_details"] = {
            key: runtime.get(key)
            for key in ("mode", "runtime", "sandbox_state", "view_health")
            if runtime.get(key) not in (None, "", [])
        }
        safe["runtime_details"]["view_available"] = view_available
        return safe

    def _worker_live_or_http(
        active_client: RuntimeClient,
        worker_id: str,
        *,
        compact: bool = False,
    ) -> dict[str, Any]:
        try:
            return active_client.worker_live(worker_id, compact=compact)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(status_code=status_code, detail="GlassHive worker is not available") from exc

    def _validated_novnc_asset_path(asset_path: str) -> str:
        normalized = str(asset_path or "").strip().lstrip("/")
        if (
            not normalized
            or normalized.startswith(".")
            or "\\" in normalized
            or ".." in Path(normalized).parts
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", normalized)
        ):
            raise HTTPException(status_code=400, detail="Invalid noVNC asset path")
        return normalized

    def _validated_novnc_ws_path(path: str) -> str:
        normalized = str(path or "websockify").strip().lstrip("/") or "websockify"
        if "\\" in normalized or ".." in Path(normalized).parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", normalized):
            raise HTTPException(status_code=400, detail="Invalid noVNC websocket path")
        return normalized

    def _runtime_view_url(active_client: RuntimeClient, worker_id: str, *, cache_key: str | None = None) -> str:
        now = time.monotonic()
        payload = _worker_live_or_http(active_client, worker_id)
        worker = payload.get("worker") or {}
        if str(worker.get("close_state") or worker.get("state") or "").strip().lower() in {
            "terminating",
            "termination_failed",
            "terminated",
        }:
            if cache_key:
                _NOVNC_VIEW_URL_CACHE.pop(cache_key, None)
            raise HTTPException(status_code=409, detail="Workspace is closed; create a new workspace for new work")
        if cache_key:
            cached = _NOVNC_VIEW_URL_CACHE.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]
        runtime = payload.get("runtime_details") or {}
        view_url = str(runtime.get("view_url") or "").strip()
        if not view_url:
            raise HTTPException(status_code=404, detail="No live desktop is available for this worker")
        if cache_key:
            _NOVNC_VIEW_URL_CACHE[cache_key] = (now + NOVNC_VIEW_URL_CACHE_TTL_SECONDS, view_url)
        return view_url

    def _novnc_view_cache_key(request: Request, worker_id: str) -> str:
        identity = _request_identity(request, worker_id)
        tenant_id = identity.get("tenant_id", "")
        user_id = identity.get("user_id", "")
        return f"{tenant_id}:{user_id}:{worker_id}"

    def _cached_novnc_asset(target: str) -> tuple[int, bytes, str] | None:
        cached = _NOVNC_ASSET_CACHE.get(target)
        if not cached:
            return None
        expires_at, status_code, content, content_type = cached
        if expires_at <= time.monotonic():
            _NOVNC_ASSET_CACHE.pop(target, None)
            return None
        return status_code, content, content_type

    def _store_novnc_asset(target: str, response: httpx.Response) -> None:
        content = response.content
        if response.status_code != 200 or len(content) > NOVNC_ASSET_CACHE_MAX_BYTES:
            return
        content_type = response.headers.get("content-type", "")
        _NOVNC_ASSET_CACHE[target] = (
            time.monotonic() + NOVNC_ASSET_CACHE_TTL_SECONDS,
            response.status_code,
            content,
            content_type,
        )

    def _novnc_asset_response(status_code: int, content: bytes, content_type: str) -> Response:
        response = Response(content=content, status_code=status_code, media_type=content_type or None)
        response.headers["Cache-Control"] = "private, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _runtime_status_detail(
        exc: httpx.HTTPStatusError,
        fallback: str,
    ) -> str | dict[str, Any]:
        response = exc.response
        if response is None:
            return fallback
        try:
            body = response.json()
        except ValueError:
            return fallback
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, dict):
            safe_detail = {
                key: value.strip()[:1000]
                for key in ("code", "message", "recovery")
                if isinstance((value := detail.get(key)), str) and value.strip()
            }
            if isinstance(detail.get("same_key_retry_allowed"), bool):
                safe_detail["same_key_retry_allowed"] = detail["same_key_retry_allowed"]
            if safe_detail.get("message"):
                return safe_detail
        return fallback

    def _runtime_http_exception(
        exc: httpx.HTTPStatusError,
        fallback: str,
    ) -> HTTPException:
        status_code = exc.response.status_code if exc.response is not None else 502
        return HTTPException(
            status_code=status_code,
            detail=_runtime_status_detail(exc, fallback),
        )

    def _runtime_proxy_base_url() -> str:
        return str(getattr(client, "base_url", "") or os.environ.get("GLASSHIVE_RUNTIME_BASE_URL", "http://127.0.0.1:8766")).rstrip("/")

    def _upstream_safe_query(raw_query: str) -> str:
        signed_query_keys = {"gh_token", "gh_sig", "gh_exp", "gh_kind"}
        pairs = [
            (key, value)
            for key, value in parse_qsl(str(raw_query or ""), keep_blank_values=True)
            if key not in signed_query_keys
        ]
        return urlencode(pairs, doseq=True)

    def _worker_id_from_runtime_proxy_path(path: str, request: Request) -> str | None:
        parts = [part for part in str(path or "").split("/") if part]
        if parts and parts[0] == "workers" and len(parts) >= 2:
            return parts[1]
        if len(parts) >= 2 and parts[0] == "link-refs":
            record = resolve_signed_link_ref(parts[1])
            payload = record.get("payload") if record else None
            if isinstance(payload, dict):
                worker_id = str(payload.get("worker_id") or "").strip()
                if worker_id:
                    return worker_id
        query_worker_id = str(request.query_params.get("worker_id") or "").strip()
        return query_worker_id or None

    def _runtime_proxy_allowed(prefix: str, path: str, method: str) -> bool:
        normalized_method = str(method or "").upper()
        normalized_path = str(path or "").strip("/")
        if prefix == "v1" and normalized_path.startswith("coordinator/"):
            coordinator_path = re.fullmatch(
                r"coordinator/conversations(?:/coordinator-[a-f0-9]{32}(?:/turns(?:/[A-Za-z0-9._:-]{1,160}/(?:stop|retry-result))?|/goals(?:/[^/]{1,160}/(?:control|result))?|/dispatch)?)?",
                normalized_path,
            )
            return bool(coordinator_path and normalized_method in {"GET", "POST"})
        if normalized_method not in {"GET", "HEAD"}:
            return False
        if prefix == "w":
            return bool(re.fullmatch(r"ghr_[A-Za-z0-9_-]{12,96}", normalized_path))
        if prefix == "ui":
            return bool(
                re.fullmatch(r"(?:projects|workers)/[A-Za-z0-9._-]{1,128}", normalized_path)
            )
        if prefix != "v1":
            return False
        return bool(
            re.fullmatch(r"signed-links/[^/]{1,16384}", normalized_path)
            or re.fullmatch(r"link-refs/[A-Za-z0-9._-]{1,256}", normalized_path)
            or re.fullmatch(
                r"workers/[A-Za-z0-9._-]{1,128}/(?:live|artifacts/(?:download|open))",
                normalized_path,
            )
        )

    async def _runtime_proxy(prefix: str, path: str, request: Request) -> Response:
        if not _runtime_proxy_allowed(prefix, path, request.method):
            raise HTTPException(status_code=404, detail="Not found")
        if _public_links_only_enabled() and prefix == "v1" and str(path).startswith("signed-links/"):
            raise HTTPException(status_code=404, detail="GlassHive public links use opaque references")
        if prefix == "v1" and str(path).startswith("coordinator/"):
            if _restricted_identity(_request_identity(request)):
                raise HTTPException(status_code=403, detail="An authenticated conversation owner is required")
        worker_id = _worker_id_from_runtime_proxy_path(path, request)
        auth_headers = _runtime_headers_for_request(request, worker_id)
        upstream_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {"accept", "content-type", "range"}
        }
        upstream_headers.update(auth_headers)
        target = f"{_runtime_proxy_base_url()}/{prefix}/{path}"
        upstream_query = _upstream_safe_query(str(request.url.query or ""))
        if upstream_query:
            target = f"{target}?{upstream_query}"
        parts=path.strip("/").split("/")
        file_response=(prefix=="v1" and (parts[0] in {"signed-links","link-refs"} or (len(parts)>2 and parts[2]=="artifacts")))
        if file_response:
            upstream=httpx.AsyncClient(timeout=60.0,follow_redirects=False)
            context=upstream.stream(request.method,target,headers=upstream_headers)
            try:
                received=await context.__aenter__()
            except httpx.HTTPError as exc:
                await upstream.aclose()
                raise HTTPException(status_code=502,detail="File download is temporarily unavailable") from exc
            async def close_stream():
                try:
                    await context.__aexit__(None,None,None)
                finally:
                    await upstream.aclose()
            response_headers={key:value for key,value in received.headers.items() if key.lower() not in {"connection","transfer-encoding","content-encoding","content-length"}}
            response=StreamingResponse(received.aiter_bytes(),status_code=received.status_code,headers=response_headers,background=BackgroundTask(close_stream))
            if worker_id:
                _set_signed_worker_cookie(response,request,worker_id)
            return response
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as upstream:
                upstream_response = await upstream.request(
                    request.method,
                    target,
                    headers=upstream_headers,
                    content=await request.body(),
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive runtime proxy failed") from exc
        response_headers = {
            key: value
            for key, value in upstream_response.headers.items()
            if key.lower() not in {"content-length", "connection", "transfer-encoding"}
        }
        response = Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=upstream_response.headers.get("content-type"),
        )
        if worker_id:
            _set_signed_worker_cookie(response, request, worker_id)
        return response

    async def _runtime_work_view_proxy(ref_id: str, request: Request) -> Response:
        """Proxy one opaque, read-only work view without upgrading its authority."""

        if not SAFE_WORK_VIEW_REF_RE.fullmatch(str(ref_id or "")):
            raise HTTPException(status_code=400, detail="Invalid GlassHive workspace view reference")
        record = resolve_signed_link_ref(ref_id)
        payload = record.get("payload") if record else None
        if not isinstance(payload, dict) or str(payload.get("kind") or "") != "worker_view":
            raise HTTPException(status_code=401, detail="Invalid or expired GlassHive workspace link")
        _require_short_ref_owner(payload, request)
        worker_id = str(payload.get("worker_id") or "").strip()
        if not SAFE_WORKER_ID_RE.fullmatch(worker_id):
            raise HTTPException(status_code=401, detail="Invalid GlassHive workspace link")
        target = f"{_runtime_proxy_base_url()}/w/{ref_id}"
        upstream_headers = _runtime_headers_for_short_ref_payload(payload)
        accept = str(request.headers.get("accept") or "").strip()
        if accept:
            upstream_headers["accept"] = accept
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as upstream:
                upstream_response = await upstream.request(
                    request.method,
                    target,
                    headers=upstream_headers,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive runtime view proxy failed") from exc
        # A runtime redirect would expose the opaque bearer in both Location
        # and browser history. The operator owns the public URL, so fail closed.
        if 300 <= upstream_response.status_code < 400:
            raise HTTPException(status_code=502, detail="GlassHive runtime view returned an unexpected redirect")
        response_headers = {}
        safe_response_headers = {
            "cache-control": ("Cache-Control", "no-store"),
            "pragma": ("Pragma", "no-cache"),
            "content-security-policy": (
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            ),
            "referrer-policy": ("Referrer-Policy", "no-referrer"),
            "x-content-type-options": ("X-Content-Type-Options", "nosniff"),
        }
        for upstream_name, (response_name, fallback) in safe_response_headers.items():
            response_headers[response_name] = str(
                upstream_response.headers.get(upstream_name) or fallback
            )
        content_type = str(upstream_response.headers.get("content-type") or "").strip()
        if content_type:
            response_headers["Content-Type"] = content_type
        response = Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=response_headers,
        )
        if upstream_response.status_code < 400:
            _set_worker_cookie_from_payload(response, request, worker_id, payload)
        return response

    @app.get("/health")
    def health() -> dict[str, Any]:
        release = {
            "release_id": str(os.environ.get("GLASSHIVE_RELEASE_ID") or "").strip(),
            "parent_revision": str(
                os.environ.get("GLASSHIVE_PARENT_REVISION") or ""
            ).strip(),
            "glasshive_revision": str(
                os.environ.get("GLASSHIVE_COMPONENT_REVISION") or ""
            ).strip(),
        }
        return {"status": "ok", "release": release, "runtime": client.health()}

    @app.api_route("/w/{ref_id}", methods=["GET", "HEAD"])
    async def runtime_work_view(ref_id: str, request: Request) -> Response:
        return await _runtime_work_view_proxy(ref_id, request)

    @app.get("/r/{ref_id}")
    def open_short_link(ref_id: str, request: Request) -> Response:
        login_redirect = _login_redirect_if_needed(request)
        if login_redirect is not None:
            return login_redirect
        record = resolve_signed_link_ref(ref_id)
        if not record:
            raise HTTPException(status_code=401, detail="Invalid or expired GlassHive workspace link")
        payload = record.get("payload")
        if not isinstance(payload, dict) or str(payload.get("kind") or "") != "worker_view":
            raise HTTPException(status_code=403, detail="This GlassHive link cannot open a workspace")
        _require_short_ref_owner(payload, request)
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            raise HTTPException(status_code=401, detail="Invalid GlassHive workspace link")
        token_tenant_id = str(payload.get("tenant_id") or "").strip()
        deployment_tenant_id = _enterprise_tenant_id()
        if _enterprise_mode_enabled() and deployment_tenant_id and token_tenant_id != deployment_tenant_id:
            raise HTTPException(status_code=401, detail="GlassHive workspace link is for a different tenant")
        target_url = _validate_short_ref_redirect_target(
            _strip_signed_query_params(str(record.get("target_url") or "").strip()),
            request,
        )
        if not target_url:
            raise HTTPException(status_code=400, detail="GlassHive workspace link has no target")
        _ensure_signed_worker_watch_session(worker_id, payload)
        _record_workspace_link_open(payload, worker_id)
        response = RedirectResponse(target_url, status_code=307)
        session_token, session_payload = _fresh_worker_view_token_from_payload(worker_id, payload)
        try:
            cookie_max_age = max(1, min(30 * 60, int(session_payload.get("exp") or 0) - int(time.time())))
        except (TypeError, ValueError):
            cookie_max_age = 30 * 60
        response.set_cookie(
            _worker_cookie_name(worker_id),
            session_token,
            max_age=cookie_max_age,
            httponly=True,
            samesite="lax",
            secure=_request_uses_https(request),
        )
        return response

    @app.get("/favicon.ico")
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg")

    @app.get("/api/bootstrap")
    def bootstrap(request: Request) -> dict[str, Any]:
        active_client = _client_for_request(request)
        identity = _request_identity(request)
        owner_id = identity["user_id"] or os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")
        bootstrap_sections: dict[str, str] = {}
        try:
            preferences = active_client.get_preferences()
            bootstrap_sections["preferences"] = "ready"
        except Exception:
            preferences = {}
            bootstrap_sections["preferences"] = "unavailable"
        existing_workspaces = flatten_workspaces(
            active_client,
            identity=identity,
            availability=bootstrap_sections,
        )
        try:
            activity = active_client.list_activity(limit=50)
            bootstrap_sections["activity"] = "ready"
        except Exception:
            activity = []
            bootstrap_sections["activity"] = "unavailable"
        try:
            provider_accounts = active_client.list_provider_accounts()
            capability_loader = getattr(active_client, "provider_account_capabilities", None)
            profile_account_providers = capability_loader() if callable(capability_loader) else {}
            bootstrap_sections["provider_accounts"] = "ready"
        except Exception:
            provider_accounts = []
            profile_account_providers = {}
            bootstrap_sections["provider_accounts"] = "unavailable"
        try:
            workspace_templates = active_client.list_workspace_templates()
            bootstrap_sections["workspace_templates"] = "ready"
        except Exception:
            workspace_templates = []
            bootstrap_sections["workspace_templates"] = "unavailable"
        try:
            recurring_schedules = active_client.recurring_schedules(include_inactive=False)
            recurring_schedules_status = "ready"
            bootstrap_sections["recurring_schedules"] = "ready"
        except Exception:
            recurring_schedules = []
            recurring_schedules_status = "unavailable"
            bootstrap_sections["recurring_schedules"] = "unavailable"
        return {
            "owner_id": owner_id,
            "draft_owner_scope": sha256(json.dumps(
                [str(identity.get("tenant_id") or ""), owner_id], separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
            "identity": {
                "email": identity.get("email", ""),
                "display_name": identity.get("display_name", ""),
                "role": identity.get("role", ""),
                "auth_method": identity.get("auth_method", ""),
                "provider_switch_visible": bool(
                    identity.get("auth_method") == "oidc"
                    and getattr(human_auth, "oidc_login_visible", True)
                ),
            },
            "csrf_token": (
                str(request.cookies.get(AUTH_CSRF_COOKIE) or "")
                if _session_for_request(request) is not None
                else ""
            ),
            "user_preferences": preferences,
            "default_workspace_option": _default_workspace_option(preferences),
            "deployment_default_workspace_option": f"new:{_default_worker_profile()}",
            "default_launch_surface": _default_launch_surface(),
            "launch_surface_options": _launch_surface_options(),
            "default_workspace_type": _default_workspace_type(),
            "workspace_type_options": _workspace_type_options(),
            "new_workspace_options": _new_workspace_options(),
            "provider_accounts": provider_accounts,
            "profile_account_providers": profile_account_providers,
            "workspace_templates": workspace_templates,
            "recurring_schedules": recurring_schedules,
            "recurring_schedules_status": recurring_schedules_status,
            "existing_workspaces": existing_workspaces,
            "activity": activity,
            "bootstrap_sections": bootstrap_sections,
        }

    @app.get("/api/admin/users")
    def list_admin_users(request: Request, limit: int = 100) -> dict[str, object]:
        _require_tenant_admin(request)
        return {"items": human_auth.list_principals(limit=limit)}

    @app.patch("/api/admin/users/{principal_id}")
    def update_admin_user(
        request: Request,
        principal_id: str,
        payload: AdminPrincipalUpdateRequest,
    ) -> dict[str, Any]:
        identity = _require_tenant_admin(request)
        if payload.disabled and hmac.compare_digest(
            str(identity.get("user_id") or ""), str(principal_id or "")
        ):
            raise HTTPException(
                status_code=409,
                detail="Use another tenant administrator to disable this account",
            )
        active_client = _client_for_request(request)
        try:
            authority = active_client.set_schedule_principal_authority(
                principal_id,
                enabled=not payload.disabled,
            )
        except httpx.HTTPStatusError as exc:
            detail = _runtime_status_detail(
                exc,
                "GlassHive could not update this account's scheduling authority",
            )
            raise HTTPException(status_code=502, detail=detail) from exc
        except Exception as exc:
            logger.warning(
                "schedule principal authority update failed principal_hash=%s disabled=%s error=%s",
                sha256(str(principal_id or "").encode("utf-8")).hexdigest()[:12],
                payload.disabled,
                exc,
            )
            raise HTTPException(
                status_code=503,
                detail="GlassHive scheduling authority is unavailable; the account was not changed",
            ) from exc
        try:
            principal = human_auth.set_principal_disabled(
                principal_id=principal_id,
                disabled=payload.disabled,
            )
            return {**principal, "schedule_authority": authority}
        except AuthGatewayError as exc:
            if "not found" in str(exc).lower():
                raise HTTPException(status_code=404, detail="Account was not found") from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/control-plane")
    def control_plane_bootstrap(request: Request) -> dict[str, object]:
        active_client = _client_for_request(request)
        runtime_health = active_client.health() or {}
        native_key_support = dict(runtime_health.get("native_api_key_support") or {})
        runtime_provider_setup_support = dict(runtime_health.get("provider_setup_support") or {})
        recurrence_owner = str(
            os.environ.get("GLASSHIVE_RECURRING_SCHEDULE_OWNER") or "glasshive_native"
        ).strip().lower()
        recurrence_owner = {
            "native": "glasshive_native",
            "scheduling_cortex": "viventium_cortex",
        }.get(recurrence_owner, recurrence_owner)
        isolation_ready = _personal_account_isolation_ready()
        codex_subscription_support = (
            "isolated_substrate_required"
            if not isolation_ready
            else "supported"
            if _truthy_env("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS")
            else "proof_required"
        )
        if codex_subscription_support == "supported":
            codex_subscription_support = str(
                runtime_provider_setup_support.get("codex") or "setup_cli_required"
            )
        if not isolation_ready:
            claude_subscription_support = "isolated_substrate_required"
        elif sys.platform == "darwin":
            claude_subscription_support = "unsupported_macos_host"
        elif _truthy_env("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH"):
            claude_subscription_support = "supported"
        else:
            claude_subscription_support = "provider_permission_required"
        if claude_subscription_support == "supported":
            claude_subscription_support = str(
                runtime_provider_setup_support.get("claude") or "setup_cli_required"
            )
        inference_broker_support = (
            "supported"
            if all(
                str(os.environ.get(name) or "").strip()
                for name in (
                    "GLASSHIVE_INFERENCE_BROKER_URL",
                    "GLASSHIVE_INFERENCE_BROKER_SECRET",
                    "GLASSHIVE_INFERENCE_BROKER_TENANT_ID",
                    "GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON",
                )
            )
            else "managed_connection_required"
        )
        return {
            "me": active_client.current_user(),
            "provider_accounts": active_client.list_provider_accounts(),
            "connections": active_client.list_connections(),
            "library": active_client.list_library(),
            "provider_options": [
                {
                    "provider": "codex",
                    "label": "Codex",
                    "native_api_key_support": native_key_support.get("codex", "unavailable"),
                    "methods": (
                        (["subscription"] if codex_subscription_support == "supported" else [])
                        + (["api_key"] if native_key_support.get("codex") == "supported" or inference_broker_support == "supported" else [])
                        + (["enterprise_route"] if inference_broker_support == "supported" else [])
                    ),
                    "subscription_support": codex_subscription_support,
                    "inference_broker_support": inference_broker_support,
                },
                {
                    "provider": "claude",
                    "label": "Claude Code",
                    "methods": (["subscription"] if claude_subscription_support == "supported" else []) + (["api_key"] if native_key_support.get("claude") == "supported" else []),
                    "native_api_key_support": native_key_support.get("claude", "unavailable"),
                    "subscription_support": claude_subscription_support,
                    "inference_broker_support": "unsupported",
                    "api_key_support": native_key_support.get("claude", "unavailable"),
                    "api_key_support_note": "Native API keys are stored in the private account home. Model access is checked when work runs.",
                },
                {"provider": "grok", "label": "Grok",
                 "methods": (["subscription"] if runtime_provider_setup_support.get("grok") == "supported" else []) + (["api_key"] if native_key_support.get("grok") == "supported" else []),
                 "subscription_support": runtime_provider_setup_support.get("grok", "setup_cli_required"),
                 "native_api_key_support": native_key_support.get("grok", "unavailable")},
            ],
            "current_native_claude": {"available": (
                isinstance(runtime_health.get("current_native_claude"), dict)
                and runtime_health["current_native_claude"].get("available") is True
                and runtime_health["current_native_claude"].get("provider") == "claude"
                and runtime_health["current_native_claude"].get("execution_mode") == "host"
                and str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").lower() != "multi_user"
            )},
            "native_provider_note": "",
            "microsoft_connection_note": (
                "Connected services stay in your managed connected-accounts profile. GlassHive checks "
                "their readiness for your user and gives each worker run only a short-lived broker grant."
            ),
            "manage_connections_url": str(
                os.environ.get("GLASSHIVE_CONNECTED_ACCOUNTS_URL") or ""
            ).strip(),
            "recurrence_owner": recurrence_owner,
            "recurrence_owner_url": str(
                os.environ.get("GLASSHIVE_SCHEDULING_OWNER_URL") or ""
            ).strip(),
        }

    @app.get("/api/connect-ai")
    def connect_ai(request: Request) -> dict[str, object]:
        _request_identity(request)
        mcp_url = str(os.environ.get("GLASSHIVE_MCP_PUBLIC_URL") or "").strip()
        if not mcp_url:
            base = str(os.environ.get("GLASSHIVE_OPERATOR_BASE_URL") or request.base_url).rstrip("/")
            mcp_url = f"{base}/mcp"
        parsed = urlparse(mcp_url)
        try:
            server_name = _mcp_client_server_name(mcp_url)
        except (UnicodeError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail="GlassHive MCP requires a valid public URL",
            ) from exc
        multi_user = _multi_user_security_enabled()
        if multi_user and (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise HTTPException(
                status_code=503,
                detail="GlassHive MCP requires a configured HTTPS public URL",
            )
        oauth_issuer = str(os.environ.get("GLASSHIVE_MCP_OAUTH_ISSUER") or "").strip()
        if multi_user and not oauth_issuer:
            raise HTTPException(
                status_code=503,
                detail="GlassHive MCP client connection requires a configured OAuth issuer",
            )
        claude_client_id = str(os.environ.get("GLASSHIVE_MCP_CLAUDE_CLIENT_ID") or "").strip()
        claude_callback_port = str(
            os.environ.get("GLASSHIVE_MCP_CLAUDE_CALLBACK_PORT") or ""
        ).strip()
        codex_client_id = str(os.environ.get("GLASSHIVE_MCP_CODEX_CLIENT_ID") or "").strip()
        codex_callback_port = str(
            os.environ.get("GLASSHIVE_MCP_CODEX_CALLBACK_PORT") or ""
        ).strip()
        codex_resource = str(os.environ.get("GLASSHIVE_MCP_CODEX_RESOURCE") or "").strip()
        token_audiences = str(
            os.environ.get("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES") or ""
        ).strip()
        token_scopes = str(
            os.environ.get("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES") or ""
        ).strip()
        required_scopes = str(
            os.environ.get("GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES") or ""
        ).strip()
        required_scope_values = [
            value for value in re.split(r"[,\s]+", required_scopes) if value
        ]
        raw_allowed_client_ids = str(
            os.environ.get("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS") or ""
        ).strip()
        allowed_client_ids = {
            value for value in re.split(r"[,\s]+", raw_allowed_client_ids) if value
        }
        registration_policy_ready = not multi_user or bool(
            token_audiences
            and token_scopes
            and required_scope_values
            and allowed_client_ids
        )

        def client_is_allowed(client_id: str) -> bool:
            return bool(
                client_id
                and registration_policy_ready
                and (not multi_user or client_id in allowed_client_ids)
            )

        clients: dict[str, dict[str, object]] = {}
        claude_callback_port_number = (
            int(claude_callback_port) if claude_callback_port.isdigit() else 0
        )
        if (
            client_is_allowed(claude_client_id)
            and 1024 <= claude_callback_port_number <= 65535
        ):
            clients["claude"] = {
                "add_command": (
                    "claude mcp add --transport http --scope user "
                    f"--client-id {shlex.quote(claude_client_id)} "
                    f"--callback-port {shlex.quote(claude_callback_port)} "
                    f"{server_name} {shlex.quote(mcp_url)}"
                ),
                "callback_port": claude_callback_port_number,
                "callback_uri": (
                    f"http://localhost:{claude_callback_port_number}/callback"
                ),
                "login_note": "Run /mcp in Claude Code and complete sign-in.",
            }
        codex_callback_port_number = (
            int(codex_callback_port) if codex_callback_port.isdigit() else 0
        )
        try:
            parsed_codex_resource = urlparse(codex_resource)
            codex_resource_matches = bool(
                codex_resource
                and not parsed_codex_resource.query
                and not parsed_codex_resource.fragment
                and _canonical_codex_server_url(codex_resource)
                == _canonical_codex_server_url(mcp_url)
            )
            codex_callback_uri = _codex_oauth_callback_uri(
                mcp_url,
                codex_callback_port_number,
            )
        except (UnicodeError, ValueError):
            codex_resource_matches = False
            codex_callback_uri = ""
        if (
            client_is_allowed(codex_client_id)
            and 1024 <= codex_callback_port_number <= 65535
            and codex_resource_matches
        ):
            codex_scope_values = list(required_scope_values)
            if "offline_access" not in codex_scope_values:
                codex_scope_values.append("offline_access")
            codex_config_toml = "\n".join(
                (
                    f"[mcp_servers.{server_name}]",
                    f"url = {json.dumps(mcp_url)}",
                    "scopes = ["
                    + ", ".join(json.dumps(scope) for scope in codex_scope_values)
                    + "]",
                    "",
                    f"[mcp_servers.{server_name}.oauth]",
                    f"client_id = {json.dumps(codex_client_id)}",
                )
            )
            codex_callback_url_override = shlex.quote(
                'mcp_oauth_callback_url='
                f'"http://127.0.0.1:{codex_callback_port_number}/callback"'
            )
            clients["codex"] = {
                "add_command": (
                    "codex mcp add "
                    f"-c mcp_oauth_callback_port={codex_callback_port_number} "
                    f"-c {codex_callback_url_override} "
                    f"{server_name} --url {shlex.quote(mcp_url)} "
                    f"--oauth-client-id {shlex.quote(codex_client_id)}"
                ),
                "login_command": (
                    "codex mcp login "
                    f"-c mcp_oauth_callback_port={codex_callback_port_number} "
                    f"-c {codex_callback_url_override} "
                    f"{server_name}"
                ),
                "config_toml": codex_config_toml,
                "callback_port": codex_callback_port_number,
                "callback_uri": codex_callback_uri,
            }
        documentation_url = str(
            os.environ.get("GLASSHIVE_MCP_DOCUMENTATION_URL") or ""
        ).strip()
        guided_steps = [
            "Connect the AI app you are using to xPerfect. Follow only the matching section; do not configure another client."
        ]
        codex_client = clients.get("codex")
        if isinstance(codex_client, dict):
            codex_setup_prompt = "\n".join(
                (
                    "You are Codex. Connect this Codex client to xPerfect.",
                    "If xPerfect tools already work, do not change setup; make only the requested tool call.",
                    "Otherwise install the official xPerfect skill once:",
                    "codex plugin marketplace add xPerfectAI/xPerfect",
                    "codex plugin add glasshive@project-glasshive",
                    "Then add or update this server in your native Codex MCP config, preserving unrelated settings. Persist these scopes so Reconnect uses the right OAuth resource and can renew the login:",
                    str(codex_client["config_toml"]),
                    "Restart the Codex/ChatGPT desktop app once so it reloads the MCP config.",
                    "Then use native sign-in:",
                    str(codex_client["login_command"]),
                    "Complete the native browser sign-in. Do not build OAuth URLs, inspect tokens, or configure another client.",
                    "When connected, call workspace_list once to verify. Never enumerate or summarize the tool catalog.",
                )
            )
            codex_client["setup_prompt"] = codex_setup_prompt
            guided_steps.extend(
                ("", "If you are Codex, follow only the Codex section.", codex_setup_prompt)
            )
        claude_client = clients.get("claude")
        if isinstance(claude_client, dict):
            claude_setup_prompt = "\n".join(
                (
                    "You are Claude Code. Connect this Claude Code client to xPerfect.",
                    "Install the official xPerfect skill once if it is not already installed:",
                    "claude plugin marketplace add xPerfectAI/xPerfect",
                    "claude plugin install glasshive@glasshive --scope user --yes",
                    f"Check `claude mcp get {server_name}`. If it already exists, do not add a duplicate. Otherwise run:",
                    str(claude_client["add_command"]),
                    str(claude_client.get("login_note") or "Open /mcp and finish sign-in."),
                    "Use only Claude Code's native browser sign-in. Do not build OAuth URLs, inspect tokens, or configure another client.",
                    "When connected, call workspace_list once to verify. Never enumerate or summarize the tool catalog.",
                )
            )
            claude_client["setup_prompt"] = claude_setup_prompt
            guided_steps.extend(
                (
                    "",
                    "If you are Claude Code, follow only the Claude Code section.",
                    claude_setup_prompt,
                )
            )
        return {
            "mcp_url": mcp_url,
            "server_name": server_name,
            "supported_clients": sorted(clients),
            "guided_prompt": "\n".join(guided_steps),
            "clients": clients,
            "configuration_status": "ready" if clients else "action_required",
            "configuration_note": (
                "Copy a command below, then complete your organization's sign-in."
                if clients
                else "No complete pre-registered and allowlisted AI client is available for this deployment. Ask an administrator to verify its token audiences, delegated scopes, client id, and fixed callback registration."
            ),
            "documentation_url": documentation_url,
            "source": {
                "license": "Apache-2.0",
                "label": "Open source",
                "repository_url": "https://github.com/xPerfectAI/xPerfect",
            },
        }

    @app.get("/api/conversation-projects")
    def conversation_projects(request: Request) -> dict[str, Any]:
        projects = _client_for_request(request).list_projects()
        return {"items": [
            {"project_id": str(item.get("project_id") or ""),
             "title": str(item.get("title") or "")}
            for item in projects if isinstance(item, dict) and item.get("project_id")
            and item.get("origin_surface") != "coordinator"
        ]}

    @app.get("/api/workspaces")
    def workspace_catalog(
        request: Request,
        kind: str = "named",
        search: str = "",
        tags: str = "",
        favorite: bool | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        return _client_for_request(request).list_workspace_catalog(
            kind=kind,
            search=search,
            tags=tags,
            favorite=favorite,
            cursor=cursor,
            limit=limit,
        )

    @app.get("/api/workspaces/{worker_id}")
    def get_saved_workspace(request: Request, worker_id: str) -> dict[str, Any]:
        try:
            worker = _client_for_request(request, worker_id).get_worker(worker_id)
            return {
                "worker_id": str(worker.get("worker_id") or worker_id),
                "duplication_report": (
                    worker.get("duplication_report")
                    if isinstance(worker.get("duplication_report"), dict)
                    else {}
                ),
            }
        except httpx.HTTPStatusError as exc:
            status_code = 404 if exc.response is not None and exc.response.status_code == 404 else 502
            raise HTTPException(
                status_code=status_code,
                detail=("Workspace not found" if status_code == 404 else "GlassHive could not load this workspace"),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive could not load this workspace") from exc

    @app.post("/api/workspaces/{worker_id}/duplicate", status_code=201)
    def duplicate_saved_workspace(
        request: Request,
        worker_id: str,
        payload: DuplicateWorkspaceRequest,
    ) -> dict[str, Any]:
        active_client = _client_for_request(request, worker_id)
        try:
            result = active_client.duplicate_workspace(
                worker_id,
                idempotency_key=payload.idempotency_key,
                name=str(payload.name or "").strip(),
            )
            return {
                "project_id": (result.get("project") or {}).get("project_id"),
                "worker": result.get("workspace") or {},
            }
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not duplicate this workspace."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive workspace duplication failed") from exc

    @app.get("/api/workspace-templates")
    def list_workspace_templates(request: Request) -> dict[str, Any]:
        return {"items": _client_for_request(request).list_workspace_templates()}

    @app.post("/api/workspaces/{worker_id}/templates", status_code=201)
    def save_workspace_template(
        request: Request,
        worker_id: str,
        payload: SaveWorkspaceTemplateRequest,
    ) -> dict[str, Any]:
        worker_id = _recurring_path_id(worker_id, "workspace id")
        active_client = _client_for_request(request, worker_id)
        try:
            return active_client.save_workspace_template(
                worker_id,
                payload.model_dump(exclude_none=True),
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not save this workspace template."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Workspace template save failed") from exc

    @app.post("/api/workspace-templates/{template_id}/instantiate", status_code=201)
    def instantiate_workspace_template(
        request: Request,
        template_id: str,
        payload: InstantiateWorkspaceTemplateRequest,
    ) -> dict[str, Any]:
        template_id = _recurring_path_id(template_id, "workspace template id")
        active_client = _client_for_request(request)
        try:
            return active_client.instantiate_workspace_template(
                template_id,
                payload.model_dump(exclude_none=True),
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not start this workspace template."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Workspace template launch failed") from exc

    @app.get("/api/recurring-schedules")
    def recurring_schedules(
        request: Request,
        include_inactive: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        active_client = _client_for_request(request)
        try:
            return {"items": active_client.recurring_schedules(include_inactive=include_inactive)}
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive schedules are not available yet."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive schedules could not be loaded") from exc

    @app.post("/api/workspace/{worker_id}/recurring-schedules", status_code=201)
    def create_recurring_schedule(
        request: Request,
        worker_id: str,
        payload: RecurringScheduleRequest,
    ) -> dict[str, Any]:
        worker_id = _recurring_path_id(worker_id, "worker id")
        payload_dict = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        active_client = _client_for_request(request, worker_id)
        try:
            return active_client.create_recurring_schedule(worker_id, payload_dict)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not save this recurring schedule."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive recurring schedule creation failed") from exc

    @app.get("/api/recurring-schedules/{definition_id}/occurrences")
    def recurring_schedule_occurrences(
        request: Request,
        definition_id: str,
        limit: int = 50,
    ) -> dict[str, list[dict[str, Any]]]:
        definition_id = _recurring_path_id(definition_id, "recurring schedule id")
        active_client = _client_for_request(request)
        try:
            return {
                "items": active_client.recurring_schedule_occurrences(
                    definition_id,
                    limit=limit,
                )
            }
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "Recurring schedule history is not available."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Recurring schedule history could not be loaded") from exc

    @app.patch("/api/recurring-schedules/{definition_id}")
    def update_recurring_schedule(
        request: Request,
        definition_id: str,
        payload: RecurringScheduleUpdateRequest,
    ) -> dict[str, Any]:
        definition_id = _recurring_path_id(definition_id, "recurring schedule id")
        payload_dict = payload.model_dump(exclude_none=True)
        active_client = _client_for_request(request)
        try:
            return active_client.update_recurring_schedule(definition_id, payload_dict)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not update this recurring schedule."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Recurring schedule update failed") from exc

    @app.delete("/api/recurring-schedules/{definition_id}")
    def retire_recurring_schedule(request: Request, definition_id: str) -> dict[str, Any]:
        definition_id = _recurring_path_id(definition_id, "recurring schedule id")
        active_client = _client_for_request(request)
        try:
            return active_client.retire_recurring_schedule(definition_id)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not retire this recurring schedule."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Recurring schedule retirement failed") from exc

    @app.post("/api/recurring-schedules/{definition_id}/run-now")
    def run_recurring_schedule_now(
        request: Request,
        definition_id: str,
        payload: RecurringScheduleRunNowRequest,
    ) -> dict[str, Any]:
        definition_id = _recurring_path_id(definition_id, "recurring schedule id")
        active_client = _client_for_request(request)
        try:
            return active_client.run_recurring_schedule_now(definition_id, payload.idempotency_key)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not run this schedule now."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Recurring schedule run-now failed") from exc

    @app.post("/api/recurring-schedules/{definition_id}/deactivate")
    def deactivate_recurring_schedule(request: Request, definition_id: str) -> dict[str, Any]:
        definition_id = _recurring_path_id(definition_id, "recurring schedule id")
        active_client = _client_for_request(request)
        try:
            return active_client.deactivate_recurring_schedule(definition_id)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not stop this recurring schedule."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive recurring schedule update failed") from exc

    def _current_native_client(request: Request):
        active_client = _client_for_request(request)
        capability = (active_client.health() or {}).get("current_native_claude")
        if (str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").lower() == "multi_user"
                or not isinstance(capability, dict) or capability.get("available") is not True
                or capability.get("provider") != "claude" or capability.get("execution_mode") != "host"):
            raise HTTPException(status_code=409, detail="Existing native sign-in is unavailable on this deployment")
        return active_client

    @app.get("/api/provider-accounts/current-native/claude")
    def current_native_claude_status(request: Request) -> dict[str, Any]:
        try:
            return _current_native_client(request).current_native_claude_status()
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(exc, "xPerfect could not check the existing Claude sign-in") from exc

    @app.post("/api/provider-accounts/current-native/claude")
    def connect_current_native_claude(request: Request) -> dict[str, Any]:
        try:
            return _current_native_client(request).connect_current_native_claude()
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(exc, "xPerfect could not connect the existing Claude sign-in") from exc

    @app.post("/api/provider-accounts")
    def create_provider_account(request: Request, payload: ProviderAccountRequest) -> dict[str, Any]:
        provider = payload.provider.strip().lower()
        auth_method = payload.auth_method.strip().lower()
        if provider not in {"codex", "claude", "grok"}:
            raise HTTPException(status_code=400, detail="Choose Codex, Claude Code or Grok")
        if auth_method not in {"subscription", "api_key", "enterprise_route"}:
            raise HTTPException(status_code=400, detail="Unsupported account connection method")
        native_key = auth_method == "api_key" and ((_client_for_request(request).health() or {}).get("native_api_key_support") or {}).get(provider) == "supported"
        if auth_method != "subscription" and provider != "codex" and not native_key:
            raise HTTPException(status_code=409, detail="This provider key route is unavailable on this host")
        if auth_method == "subscription" and not _personal_account_isolation_ready():
            raise HTTPException(
                status_code=409,
                detail="Personal subscriptions require a dedicated per-worker isolation substrate in multi-user deployments",
            )
        if provider == "claude" and auth_method == "subscription" and sys.platform == "darwin":
            platform_support = "unsupported_macos_host"
        elif provider == "claude" and auth_method == "subscription" and not _truthy_env(
            "GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH"
        ):
            platform_support = "provider_permission_required"
        elif provider == "codex" and auth_method == "subscription" and not _truthy_env(
            "GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS"
        ):
            platform_support = "proof_required"
        elif auth_method in {"api_key", "enterprise_route"}:
            platform_support = "supported"
        else:
            platform_support = "supported"
        locator = (
            "native-home://auto"
            if auth_method == "subscription" or native_key
            else "broker://librechat-openai"
        )
        try:
            return _client_for_request(request).create_provider_account(
                {
                    "provider": provider,
                    "label": payload.label,
                    "auth_method": auth_method,
                    "platform_support": platform_support,
                    "secret_locator": locator,
                    "make_default": payload.make_default,
                }
            )
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc, "GlassHive could not create this account connection"
            ) from exc

    @app.exception_handler(RequestValidationError)
    async def private_credential_validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.endswith("/credentials"):
            return JSONResponse(status_code=422, content={"detail": "Enter a valid API key"})
        return await request_validation_exception_handler(request, exc)

    @app.post("/api/provider-accounts/{account_id}/credentials")
    def connect_provider_api_key(request: Request, account_id: str, payload: ProviderApiKeyRequest) -> dict[str, Any]:
        try:
            return _client_for_request(request).connect_provider_api_key(account_id, payload.value.get_secret_value())
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(exc, "xPerfect could not check this API key") from exc

    @app.post("/api/provider-accounts/{account_id}/setup")
    def start_provider_account_setup(request: Request, account_id: str) -> dict[str, Any]:
        try:
            return _client_for_request(request).start_provider_account_setup(account_id)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc, "GlassHive could not start this account connection"
            ) from exc

    @app.get("/api/provider-accounts/{account_id}/setup")
    def provider_account_setup_status(request: Request, account_id: str) -> dict[str, Any]:
        try:
            return _client_for_request(request).provider_account_setup_status(account_id)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc, "GlassHive could not check this account connection"
            ) from exc

    @app.post("/api/provider-accounts/{account_id}/setup/input")
    def submit_provider_account_setup_input(
        request: Request, account_id: str, payload: ProviderSetupInputRequest
    ) -> dict[str, Any]:
        try:
            return _client_for_request(request).submit_provider_account_setup_input(
                account_id, payload.value
            )
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc, "GlassHive could not finish this account connection"
            ) from exc

    @app.post("/api/provider-accounts/{account_id}/setup/cancel")
    def cancel_provider_account_setup(request: Request, account_id: str) -> dict[str, Any]:
        try:
            return _client_for_request(request).cancel_provider_account_setup(account_id)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc, "GlassHive could not cancel this account connection"
            ) from exc

    @app.post("/api/provider-accounts/{account_id}/verify")
    def verify_provider_account(request: Request, account_id: str) -> dict[str, Any]:
        try:
            return _client_for_request(request).verify_provider_account(account_id)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc, "GlassHive could not verify this account connection"
            ) from exc

    @app.post("/api/provider-accounts/{account_id}/disconnect")
    def disconnect_provider_account(request: Request, account_id: str) -> dict[str, Any]:
        try:
            return _client_for_request(request).disconnect_provider_account(account_id)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc, "GlassHive could not remove this account"
            ) from exc

    @app.delete("/api/provider-accounts/{account_id}")
    def forget_provider_account(request: Request, account_id: str) -> dict[str, Any]:
        try:
            return _client_for_request(request).forget_provider_account(account_id)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc, "GlassHive could not remove this account"
            ) from exc

    @app.get("/api/workspaces/{worker_id}/capability-grants")
    def list_workspace_capability_grants(request: Request, worker_id: str) -> dict[str, Any]:
        return {"items": _client_for_request(request).list_workspace_grants(worker_id)}

    @app.delete("/api/workspaces/{worker_id}/capability-grants/{grant_id}")
    def revoke_workspace_capability_grant(
        request: Request,
        worker_id: str,
        grant_id: str,
    ) -> dict[str, Any]:
        return _client_for_request(request).revoke_workspace_grant(worker_id, grant_id)

    @app.post("/api/pending-changes")
    def create_pending_change(request: Request, payload: PendingChangeRequest) -> dict[str, Any]:
        try:
            return _client_for_request(request).create_pending_change(
                {
                    "change_type": payload.change_type,
                    "target_id": payload.target_id,
                    "payload": payload.payload,
                }
            )
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc,
                "GlassHive could not prepare this workspace change.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail="GlassHive workspace changes are unavailable",
            ) from exc

    @app.get("/api/pending-changes/{change_id}")
    def get_pending_change(request: Request, change_id: str) -> dict[str, Any]:
        active_client = _client_for_request(request)
        try:
            pending = dict(active_client.get_pending_change(change_id))
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not find this pending change."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive pending changes are unavailable") from exc
        payload = dict(pending.get("payload") or {})
        try:
            catalog = active_client.list_workspace_catalog(kind="named,ephemeral,legacy", limit=100)
            workspace = next(
                (
                    item
                    for item in catalog.get("items", [])
                    if str(item.get("worker_id") or "") == str(pending.get("target_id") or "")
                ),
                {},
            )
            pending["target_label"] = str(workspace.get("name") or workspace.get("title") or "Workspace")
        except Exception:
            pending["target_label"] = "Workspace"
        capability_label = "Approved capability"
        effective_scopes = [str(value) for value in (payload.get("scopes") or []) if str(value).strip()]
        if str(pending.get("change_type") or "") == "workspace_provider_account":
            account_snapshot = dict(payload.get("account_snapshot") or {})
            if str(payload.get("policy") or "") == "legacy":
                capability_label = "Deployment-managed worker account"
            else:
                capability_label = str(
                    account_snapshot.get("label")
                    or account_snapshot.get("provider")
                    or "Selected personal worker account"
                )
            effective_scopes = []
        elif str(pending.get("change_type") or "") == "workspace_duplication_reapproval_waiver":
            capability_label = str(payload.get("label") or "Copied capability")
            effective_scopes = []
        elif payload.get("library_id"):
            snapshot = dict(payload.get("library_snapshot") or {})
            capability_label = str(snapshot.get("display_label") or snapshot.get("stable_id") or capability_label)
            if capability_label == "Approved capability":
                try:
                    library = next(
                        (
                            item
                            for item in active_client.list_library()
                            if str(item.get("library_id") or "") == str(payload.get("library_id") or "")
                        ),
                        {},
                    )
                    manifest = dict(library.get("manifest") or {})
                    capability_label = str(
                        manifest.get("label")
                        or manifest.get("name")
                        or library.get("stable_id")
                        or capability_label
                    )
                except Exception:
                    pass
            if not effective_scopes:
                effective_scopes = [
                    str(value)
                    for value in (snapshot.get("allowed_scopes") or [])
                    if str(value).strip()
                ]
        elif payload.get("connection_id"):
            connection = next(
                (
                    item
                    for item in active_client.list_connections()
                    if str(item.get("connection_id") or "") == str(payload.get("connection_id") or "")
                ),
                {},
            )
            capability_label = str(connection.get("label") or connection.get("kind") or capability_label)
        elif payload.get("account_id"):
            account = next(
                (
                    item
                    for item in active_client.list_provider_accounts()
                    if str(item.get("account_id") or "") == str(payload.get("account_id") or "")
                ),
                {},
            )
            capability_label = str(account.get("label") or account.get("provider") or capability_label)
        pending["capability_label"] = capability_label
        pending["effective_scopes"] = effective_scopes
        return pending

    @app.post("/api/pending-changes/{change_id}/confirm")
    def confirm_pending_change(
        request: Request,
        change_id: str,
        payload: PendingChangeConfirmRequest,
    ) -> dict[str, Any]:
        try:
            return _client_for_request(
                request,
                human_confirmation=True,
            ).confirm_pending_change(change_id, payload.confirmation_token)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc,
                "GlassHive could not confirm this workspace change.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail="GlassHive workspace changes are unavailable",
            ) from exc

    @app.patch("/api/preferences")
    def update_preferences(request: Request, payload: PreferencesRequest) -> dict[str, Any]:
        payload_dict = (
            payload.model_dump(exclude_none=True)
            if hasattr(payload, "model_dump")
            else payload.dict(exclude_none=True)
        )
        return _client_for_request(request).update_preferences(payload_dict)

    @app.get("/api/native-models/grok-build")
    def grok_native_models(request: Request) -> dict[str, Any]:
        try:
            return _client_for_request(request).grok_native_models()
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(exc, "xPerfect could not load Grok models") from exc

    @app.post("/api/launch")
    def launch(request: Request, payload: LaunchRequest) -> dict[str, Any]:
        if not payload.description.strip():
            raise HTTPException(status_code=422, detail="Describe what you want done")
        active_client = _client_for_request(request)
        identity = _request_identity(request)
        owner_id = identity["user_id"] or os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")
        brief = build_operator_brief(payload.description, payload.success_criteria, payload.context)
        try:
            preferences = active_client.get_preferences()
        except Exception:
            preferences = {}
        workspace_option = payload.workspace_option or payload.worker_option or _default_workspace_option(preferences)
        schedule_text = str(payload.schedule_text or "").strip()
        if payload.files:
            raise HTTPException(
                status_code=422,
                detail={"code":"legacy_upload_transport",
                        "message":"Upload files through /api/file-uploads, then launch with file_upload_ids. No workspace was created or started.",
                        "retryable":False},
            )
        if payload.file_upload_revisions and len(payload.file_upload_revisions) != len(payload.file_upload_ids):
            raise HTTPException(
                status_code=422,
                detail="Each selected file needs its matching revision",
            )
        bootstrap_bundle = None
        execution_mode = _execution_mode_from_workspace_type(payload.workspace_type)
        project_id: str
        worker_id: str | None = None
        profile: str
        created_new_worker = False
        duplication_reapproval_count = 0
        duplication_reapproval_items: list[dict[str, Any]] = []
        new_workspace = not workspace_option.startswith(("open:", "existing:", "duplicate:"))
        shared_workspace = payload.workspace_mode == "shared"
        if shared_workspace and not new_workspace:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "shared_new_workspace_required",
                    "message": "Shared workspaces are available when creating a new workspace.",
                },
            )
        if shared_workspace and execution_mode != "docker":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "shared_host_execution_unavailable",
                    "message": "Shared workspaces require managed container placement.",
                },
            )
        requested_policy = str(payload.provider_account_policy or "").strip().lower()
        requested_account_id = str(payload.provider_account_id or "").strip()
        requested_account_selection = bool(requested_policy or requested_account_id)
        if requested_account_selection and not new_workspace:
            raise HTTPException(
                status_code=409,
                detail="Credential policy is selected when creating a new workspace; existing workspaces keep their saved policy",
            )
        provider_account_selection: dict[str, str] | None = None
        if new_workspace and requested_account_selection:
            if requested_policy and requested_policy not in PROVIDER_ACCOUNT_POLICIES:
                raise HTTPException(status_code=400, detail="Unsupported worker credential policy")
            if requested_policy == "legacy":
                if requested_account_id:
                    raise HTTPException(
                        status_code=400,
                        detail="Deployment account policy cannot include a personal account",
                    )
                provider_account_selection = {"policy": "legacy"}
            else:
                requested_profile = (
                    workspace_option.split(":", 1)[1]
                    if ":" in workspace_option
                    else _default_worker_profile()
                )
                try:
                    provider_accounts = active_client.list_provider_accounts()
                    profile_account_providers = active_client.provider_account_capabilities()
                except Exception as exc:
                    raise HTTPException(
                        status_code=502,
                        detail="Personal worker accounts could not be loaded; retry before launching",
                    ) from exc
                provider_account_selection = _provider_account_selection_for_launch(
                    provider_accounts,
                    profile=requested_profile,
                    profile_providers=profile_account_providers,
                    requested_policy=payload.provider_account_policy,
                    requested_account_id=payload.provider_account_id,
                )

        if workspace_option.startswith("duplicate:") and not payload.idempotency_key:
            raise HTTPException(
                status_code=422,
                detail="A reusable idempotency key is required to copy a workspace",
            )

        try:
            if workspace_option.startswith("open:") or workspace_option.startswith("existing:"):
                worker_id = workspace_option.split(":", 1)[1]
                worker = active_client.get_worker(worker_id)
                project_id = str(worker["project_id"])
                profile = str(worker.get("profile") or "codex-cli")
            elif workspace_option.startswith("duplicate:"):
                source_worker_id = workspace_option.split(":", 1)[1]
                source_worker = active_client.get_worker(source_worker_id)
                profile = str(source_worker.get("profile") or "codex-cli")
                duplicated = active_client.duplicate_workspace(
                    source_worker_id,
                    idempotency_key=payload.idempotency_key,
                )
                project = duplicated.get("project") or {}
                worker = duplicated.get("workspace") or {}
                project_id = str(project.get("project_id") or worker.get("project_id") or "")
                worker_id = str(worker.get("worker_id") or "")
                if not project_id or not worker_id:
                    raise RuntimeError("GlassHive returned an incomplete workspace copy")
                duplication_report = worker.get("duplication_report") or {}
                duplication_reapproval_count = int(
                    duplication_report.get(
                        "capabilities_requiring_reapproval",
                        0,
                    )
                    or 0
                )
                raw_reapproval_items = duplication_report.get("reapproval_items") or []
                if isinstance(raw_reapproval_items, list):
                    duplication_reapproval_items = [
                        dict(item)
                        for item in raw_reapproval_items
                        if isinstance(item, dict)
                    ]
                created_new_worker = True
            else:
                profile = workspace_option.split(":", 1)[1] if ":" in workspace_option else _default_worker_profile()
                effort = _effort_for_profile(profile, payload.effort, preferences)
                selected_policy = str(
                    (provider_account_selection or {}).get("policy") or "legacy"
                ).strip().lower()
                selected_account_id = str(
                    (provider_account_selection or {}).get("account_id") or ""
                ).strip()
                uses_deployment_provider = selected_policy == "legacy" or (
                    selected_policy == "personal_preferred" and not selected_account_id
                )
                if uses_deployment_provider:
                    readiness = active_client.provider_readiness(profile)
                    if str(readiness.get("readiness") or "") != "deployment_managed":
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Work AI is not set up. Ask an administrator to finish provider "
                                "setup or connect a personal account in Connections."
                            ),
                        )
                project = active_client.create_project(owner_id, build_project_title(payload.description), payload.description.strip(), profile)
                project_id = str(project["project_id"])
                if shared_workspace:
                    shared = active_client.create_execution_workspace(
                        project_id,
                        execution_mode=execution_mode,
                        file_placement=payload.workspace_file_placement,
                    )
                    readiness = shared.get("runtime_readiness") or {}
                    if readiness.get("available") is not True:
                        code = str(readiness.get("code") or "shared_runtime_unavailable")
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": code,
                                "message": f"Shared workspace is unavailable ({code}).",
                            },
                        )
                    workspace_id = str(shared.get("workspace_id") or "").strip()
                    if not workspace_id:
                        raise RuntimeError("GlassHive returned an incomplete shared workspace")
                    member_payload: dict[str, Any] = {
                        "name": build_project_title(payload.description),
                        "role": payload.success_criteria.strip()[:160] or "main",
                        "profile": profile,
                    }
                    if effort:
                        member_payload["effort"] = effort
                    if provider_account_selection:
                        member_payload["provider_account_policy"] = selected_policy
                        if selected_account_id:
                            member_payload["provider_account_id"] = selected_account_id
                    worker = active_client.add_execution_workspace_member(
                        workspace_id,
                        member_payload,
                    )
                else:
                    bootstrap_bundle = _bootstrap_bundle_with_effort(
                        bootstrap_bundle,
                        profile,
                        effort,
                    )
                    bootstrap_bundle = _bootstrap_bundle_with_provider_account(
                        bootstrap_bundle,
                        provider_account_selection,
                    )
                    worker = active_client.create_worker(
                        project_id,
                        owner_id,
                        profile,
                        name=build_project_title(payload.description),
                        role=payload.success_criteria.strip()[:160] or "main",
                        bootstrap_bundle=bootstrap_bundle,
                        execution_mode=execution_mode,
                        # Cold image preparation belongs to the durable worker queue, not the
                        # browser request budget. The immediately assigned run transitions this
                        # prepared workspace to starting and the watch surface reports progress.
                        start_synchronously=False,
                    )
                worker_id = str(worker["worker_id"])
                created_new_worker = True
            if payload.file_upload_ids and (not schedule_text or duplication_reapproval_count):
                binding_payload = {
                    "upload_ids": payload.file_upload_ids,
                    "idempotency_key": payload.idempotency_key or secrets.token_hex(16),
                }
                if payload.file_upload_revisions:
                    binding_payload["revisions"] = payload.file_upload_revisions
                active_client.file_request(
                    "POST", f"/v1/workers/{worker_id}/files",
                    payload=binding_payload,
                )
            scheduled = None
            run = None
            if duplication_reapproval_count:
                # Capability grants belong to one immutable workspace. The canonical
                # duplicate stays paused until the user reviews equivalent grants.
                pass
            elif schedule_text:
                schedule_payload = {}
                if payload.file_upload_ids:
                    schedule_payload["file_upload_ids"] = payload.file_upload_ids
                    if payload.file_upload_revisions:
                        schedule_payload["file_upload_revisions"] = payload.file_upload_revisions
                scheduled = active_client.schedule_run(
                    str(worker_id), brief, schedule_text=schedule_text, **schedule_payload
                )
            else:
                run = active_client.assign_run(str(worker_id), brief)
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            reason = _runtime_status_detail(exc, _format_launch_error(exc))
            if created_new_worker and worker_id:
                try:
                    active_client.launch_failed(str(worker_id), str(reason))
                except Exception:
                    pass
            raise _runtime_http_exception(exc, _format_launch_error(exc)) from exc
        except Exception as exc:
            reason = _format_launch_error(exc)
            if created_new_worker and worker_id:
                try:
                    active_client.launch_failed(str(worker_id), reason)
                except Exception:
                    pass
            raise HTTPException(status_code=502, detail=reason) from exc

        surface = initial_watch_surface_for_launch(
            profile,
            payload.description,
            launch_surface=payload.launch_surface or _default_launch_surface(),
        )
        watch_url = f"/watch/{worker_id}?project_id={project_id}&surface={surface}"
        try:
            launch_watch_url = _append_signed_worker_token(
                watch_url,
                str(worker_id),
                identity,
                storage_timeout_seconds=0.1,
            )
        except (OSError, sqlite3.Error):
            # Project, worker, and run are already durable. An authenticated browser can use the
            # ordinary owner-scoped route, so do not turn auxiliary short-link state failure into
            # a retry that duplicates work. Keep storage paths and exception text out of logs.
            logger.warning(
                "Signed launch link storage unavailable after durable launch for worker %s",
                worker_id,
            )
            launch_watch_url = watch_url
        return {
            "project_id": project_id,
            "worker_id": str(worker_id),
            "run_id": (run or {}).get("run_id"),
            "schedule_id": (scheduled or {}).get("schedule_id"),
            "scheduled_for": (scheduled or {}).get("run_at"),
            "status": (
                "action_required"
                if duplication_reapproval_count
                else "scheduled"
                if scheduled
                else "dispatched"
            ),
            "capabilities_requiring_reapproval": duplication_reapproval_count,
            "reapproval_items": duplication_reapproval_items,
            "watch_url": launch_watch_url,
        }

    install_terminal_routes(app, static_dir=STATIC_DIR, client_for_request=_client_for_request,
        identity_for_request=_request_identity, restricted_identity=_restricted_identity,
        runtime_headers=_runtime_headers_for_request, runtime_base_url=_runtime_proxy_base_url,
        human_auth=human_auth, session_for_request=_session_for_request,
        uses_worker_view=_uses_worker_view_credential)
    install_peer_ui_routes(app, _client_for_request)
    install_member_ui_routes(app, _client_for_request)
    install_workspace_ui_routes(app, _client_for_request)
    install_worker_configuration_ui_routes(app, _client_for_request)
    install_allowed_ai_policy_ui_routes(app, _client_for_request)

    install_file_routes(app, _client_for_request, _request_identity, _restricted_identity, _runtime_http_exception)

    @app.get("/api/workspace/{worker_id}/live")
    @app.get("/api/worker/{worker_id}/live")
    def worker_live(request: Request, worker_id: str, compact: bool = False) -> JSONResponse:
        active_client = _client_for_request(request, worker_id, internal_details=True)
        payload = _worker_live_or_http(active_client, worker_id, compact=compact)
        worker = payload.get("worker") or {}
        project_id = str(worker.get("project_id") or "")
        payload["project_title"] = _project_title_for_worker(active_client, project_id) if project_id else ""
        return _json_response_with_signed_cookie(request, worker_id, _browser_live_payload(payload))

    @app.get("/api/workspace/{worker_id}/native-control")
    @app.get("/api/worker/{worker_id}/native-control")
    def native_control_state(request: Request, worker_id: str) -> dict[str, Any]:
        _require_interactive_access(request, worker_id)
        try:
            return _client_for_request(request, worker_id, internal_details=True).native_control_state(worker_id)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc,
                "Native controls are not available for this workspace.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Native controls are unavailable") from exc

    @app.post("/api/workspace/{worker_id}/native-control")
    @app.post("/api/worker/{worker_id}/native-control")
    def native_control(
        request: Request,
        worker_id: str,
        payload: NativeControlRequest,
    ) -> dict[str, Any]:
        _require_interactive_access(request, worker_id)
        payload_dict = (
            payload.model_dump(exclude={"run_id", "attempt_id", "action"}, exclude_none=True)
            if hasattr(payload, "model_dump")
            else payload.dict(exclude={"run_id", "attempt_id", "action"}, exclude_none=True)
        )
        try:
            return _client_for_request(request, worker_id, internal_details=True).native_control(
                worker_id,
                {
                    "run_id": payload.run_id,
                    "attempt_id": payload.attempt_id,
                    "action": payload.action,
                    **payload_dict,
                },
            )
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc,
                "Native control could not be applied to this workspace.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Native control failed") from exc

    @app.get("/api/workspace/{worker_id}/desktop-credentials")
    def desktop_credentials(request: Request, worker_id: str) -> JSONResponse:
        """Return only the owner-scoped noVNC password needed by the in-app client.

        Raw runtime URLs and Selenium router credentials remain server-side.  The response is
        deliberately non-cacheable and the browser keeps the password in memory only.
        """

        _require_interactive_access(request, worker_id)
        active_client = _client_for_request(request, worker_id, internal_details=True)
        view_url = _runtime_view_url(
            active_client,
            worker_id,
            cache_key=_novnc_view_cache_key(request, worker_id),
        )
        parsed = urlparse(view_url)
        password = str((parse_qs(parsed.query).get("password") or [""])[0])
        if len(password) > 128 or any(ord(character) < 32 for character in password):
            raise HTTPException(status_code=502, detail="Live desktop credentials are invalid")
        if (
            str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user"
            and not password
        ):
            raise HTTPException(status_code=503, detail="Live desktop authentication is unavailable")
        response = _json_response_with_signed_cookie(
            request,
            worker_id,
            {"password": password},
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/novnc/{worker_id}/{asset_path:path}")
    def novnc_asset(request: Request, worker_id: str, asset_path: str) -> Response:
        _require_interactive_access(request, worker_id)
        active_client = _client_for_request(request, worker_id, internal_details=True)
        safe_asset_path = _validated_novnc_asset_path(asset_path)
        view_url = _runtime_view_url(
            active_client,
            worker_id,
            cache_key=_novnc_view_cache_key(request, worker_id),
        )
        parsed = urlparse(view_url)
        if not parsed.scheme or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Invalid live desktop URL")
        target = f"{parsed.scheme}://{parsed.netloc}/{safe_asset_path}"
        cached = _cached_novnc_asset(target)
        if cached:
            return _novnc_asset_response(*cached)
        try:
            upstream_response = _fetch_novnc_asset(target)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Live desktop asset proxy failed") from exc
        _store_novnc_asset(target, upstream_response)
        return _novnc_asset_response(
            upstream_response.status_code,
            upstream_response.content,
            upstream_response.headers.get("content-type", ""),
        )

    @app.websocket("/novnc/{worker_id}/websockify")
    async def novnc_websocket(websocket: WebSocket, worker_id: str) -> None:
        try:
            if _uses_worker_view_credential(websocket, worker_id):
                # noVNC is bidirectional (keyboard/mouse/clipboard/protocol
                # messages), so it cannot truthfully be exposed as read-only.
                await websocket.close(code=1008, reason="GlassHive view link is read-only")
                return
            active_client = _client_for_request(websocket, worker_id, internal_details=True)
            view_url = _runtime_view_url(active_client, worker_id)
            parsed = urlparse(view_url)
            if not parsed.scheme or not parsed.netloc:
                raise HTTPException(status_code=400, detail="Invalid live desktop URL")
            query = parse_qs(parsed.query)
            ws_path = _validated_novnc_ws_path((query.get("path") or ["websockify"])[0])
            ws_scheme = "wss" if parsed.scheme == "https" else "ws"
            upstream_url = f"{ws_scheme}://{parsed.netloc}/{ws_path}"
            session_timeout = _watch_session_timeout_seconds(websocket, worker_id)
        except HTTPException:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        try:
            async with websockets.connect(upstream_url, max_size=None) as upstream:
                async def browser_to_sandbox() -> None:
                    while True:
                        message = await websocket.receive()
                        message_type = message.get("type")
                        if message_type == "websocket.disconnect":
                            await upstream.close()
                            return
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                async def sandbox_to_browser() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(str(message))

                async def enforce_session_timeout() -> None:
                    await asyncio.sleep(session_timeout)
                    await upstream.close()
                    await websocket.close(code=1008, reason="GlassHive watch session expired")

                async def enforce_workspace_open() -> None:
                    while True:
                        await asyncio.sleep(0.25)
                        try:
                            live = await asyncio.to_thread(active_client.worker_live, worker_id)
                            live_worker = live.get("worker") if isinstance(live, dict) else None
                            state = str(
                                (live_worker or {}).get("close_state")
                                or (live_worker or {}).get("state")
                                or ""
                            ).strip().lower()
                        except Exception:
                            state = "terminated"
                        if state in {"terminating", "termination_failed", "terminated"}:
                            await upstream.close()
                            await websocket.close(code=1008, reason="Workspace closed")
                            return

                tasks = {
                    asyncio.create_task(browser_to_sandbox()),
                    asyncio.create_task(sandbox_to_browser()),
                    asyncio.create_task(enforce_workspace_open()),
                }
                if session_timeout > 0:
                    tasks.add(asyncio.create_task(enforce_session_timeout()))
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
        except WebSocketDisconnect:
            return
        except Exception:
            try:
                await websocket.close(code=1011)
            except Exception:
                return

    @app.post("/api/workspace/{worker_id}/message")
    @app.post("/api/worker/{worker_id}/message")
    def worker_message(request: Request, worker_id: str, payload: MessageRequest) -> dict[str, Any]:
        try:
            return _client_for_request(request, worker_id).message(worker_id, payload.message)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc,
                "GlassHive could not queue that workspace message.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive workspace message failed") from exc

    @app.post("/api/workspace/{worker_id}/steer")
    @app.post("/api/worker/{worker_id}/steer")
    def worker_steer(request: Request, worker_id: str, payload: MessageRequest) -> dict[str, Any]:
        try:
            return _client_for_request(request, worker_id).steer(worker_id, payload.message)
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc,
                "GlassHive could not steer that workspace.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive workspace steer failed") from exc

    @app.api_route("/api/workspace/{worker_id}/metadata", methods=["POST", "PATCH"])
    @app.api_route("/api/worker/{worker_id}/metadata", methods=["POST", "PATCH"])
    def worker_metadata(request: Request, worker_id: str, payload: MetadataRequest) -> dict[str, Any]:
        payload_dict = (
            payload.model_dump(exclude_none=True)
            if hasattr(payload, "model_dump")
            else payload.dict(exclude_none=True)
        )
        return _client_for_request(request, worker_id).update_worker_metadata(
            worker_id,
            payload_dict,
        )

    @app.post("/api/workspace/{worker_id}/action/{action}")
    @app.post("/api/worker/{worker_id}/action/{action}")
    def worker_action(
        request: Request,
        worker_id: str,
        action: str,
        payload: ActionRequest | None = Body(default=None),
    ) -> dict[str, Any]:
        active_client = _client_for_request(request, worker_id)
        try:
            if action in {"pause", "resume", "interrupt", "terminate"}:
                return active_client.lifecycle(worker_id, action)
            if action in {"terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"}:
                return active_client.desktop_action(worker_id, action, url=(payload.url if payload else None))
        except httpx.HTTPStatusError as exc:
            raise _runtime_http_exception(
                exc,
                "GlassHive could not apply that workspace action yet.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive workspace action failed") from exc
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    @app.api_route("/ui/{runtime_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def runtime_ui_proxy(runtime_path: str, request: Request) -> Response:
        legacy_worker = re.fullmatch(r"workers/([A-Za-z0-9._-]{1,128})", runtime_path.strip("/"))
        if request.method.upper() in {"GET", "HEAD"} and legacy_worker:
            worker_id = legacy_worker.group(1)
            redirect = _login_redirect_if_needed(request, worker_id=worker_id)
            if redirect is not None:
                return redirect
            _require_ui_auth(request, worker_id)
            response = RedirectResponse(url=f"/watch/{quote(worker_id, safe='')}", status_code=302)
            response.headers["Cache-Control"] = "no-store"
            _set_signed_worker_cookie(response, request, worker_id)
            return response
        if request.method.upper() in {"GET", "HEAD"}:
            redirect = _login_redirect_if_needed(
                request,
                worker_id=str(request.query_params.get("worker_id") or "").strip() or None,
            )
            if redirect is not None:
                return redirect
        return await _runtime_proxy("ui", runtime_path, request)

    @app.api_route("/v1/{runtime_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def runtime_v1_proxy(runtime_path: str, request: Request) -> Response:
        normalized_runtime_path = str(runtime_path or "").strip("/")
        if request.method.upper() in {"GET", "HEAD"} and re.fullmatch(
            r"link-refs/[A-Za-z0-9._-]{1,256}",
            normalized_runtime_path,
        ):
            redirect = _login_redirect_if_needed(
                request,
                worker_id=_worker_id_from_runtime_proxy_path(normalized_runtime_path, request),
            )
            if redirect is not None:
                return redirect
        return await _runtime_proxy("v1", runtime_path, request)

    @app.get("/")
    def home(request: Request) -> Response:
        redirect = _login_redirect_if_needed(request)
        if redirect is not None:
            return redirect
        _require_ui_auth(request)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/conversation")
    def conversation_page(request: Request) -> Response:
        redirect = _login_redirect_if_needed(request)
        if redirect is not None:
            return redirect
        _require_ui_auth(request)
        page = (STATIC_DIR / "conversation.html").read_text(encoding="utf-8")
        for asset in ("conversation.css", "conversation.js"):
            fingerprint = sha256((STATIC_DIR / asset).read_bytes()).hexdigest()[:16]
            page = page.replace(f'/static/{asset}"', f'/static/{asset}?v={fingerprint}"')
        return Response(page, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/confirm-change")
    def confirm_change_page() -> FileResponse:
        # The opaque confirmation token stays in the URL fragment and never reaches
        # access logs. The page itself performs session/OIDC recovery before reading
        # or applying authenticated, owner-scoped change metadata.
        return FileResponse(STATIC_DIR / "confirm.html")

    @app.get("/watch/{worker_id}")
    def watch(request: Request, worker_id: str) -> Response:
        redirect = _login_redirect_if_needed(request, worker_id=worker_id)
        if redirect is not None:
            return redirect
        _require_ui_auth(request, worker_id)
        return _file_response_with_signed_cookie(request, worker_id, STATIC_DIR / "watch.html")

    @app.get("/desktop/{worker_id}")
    def desktop(request: Request, worker_id: str) -> Response:
        redirect = _login_redirect_if_needed(request, worker_id=worker_id)
        if redirect is not None:
            return redirect
        _require_interactive_access(request, worker_id)
        return _file_response_with_signed_cookie(request, worker_id, STATIC_DIR / "desktop.html")

    return app


app = create_app()
