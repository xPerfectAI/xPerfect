from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlencode

try:
    import grp
except ImportError:  # pragma: no cover - POSIX-only shared hosted mode
    grp = None


DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_LINK_REF_TTL_SECONDS = 0
LINK_REF_ROUTE = "/v1/link-refs"
_SAFE_LINK_REF_RE = re.compile(r"^[A-Za-z0-9_-]{12,96}$")
_SENSITIVE_QUERY_RE = re.compile(r"(?i)((?:^|[?&])(?:gh_token|gh_sig|gh_exp|gh_kind)=)([^&#\s\"']+)")
_SIGNED_LINK_PATH_RE = re.compile(r"(?i)(/v1/signed-links/)([^/?#\s\"']+)")
_LINK_REF_PATH_RE = re.compile(r"(?i)(/(?:v1/link-refs|r|w)/)(ghr_[A-Za-z0-9_-]{12,96})")
_WORKER_COOKIE_TOKEN_RE = re.compile(r"(?i)(\bglasshive_gh_token_[A-Za-z0-9._-]+=)([^;\s\"']+)")
_LOG_FILTER_INSTALLED = False
_LOG_RECORD_FACTORY_INSTALLED = False
_STANDARD_LOG_RECORD_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}


def signed_link_secret() -> str:
    return (
        os.environ.get("GLASSHIVE_SIGNED_LINK_SECRET", "").strip()
        or os.environ.get("WPR_API_TOKEN", "").strip()
    )


def _worker_is_terminated_in_runtime_db(worker_id: str) -> bool:
    clean_worker_id = str(worker_id or "").strip()
    raw_db_path = str(os.environ.get("WPR_DB_PATH") or "").strip()
    if not clean_worker_id or not raw_db_path:
        return False
    db_path = Path(raw_db_path).expanduser()
    if not db_path.is_file():
        return False
    try:
        with closing(sqlite3.connect(db_path, timeout=1)) as conn:
            row = conn.execute(
                "SELECT state FROM workers WHERE worker_id = ?",
                (clean_worker_id,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and str(row[0] or "") == "terminated")

def worker_signed_links_blocked(worker_id: str) -> bool:
    return _worker_is_terminated_in_runtime_db(
        worker_id
    ) or is_worker_signed_link_revoked(worker_id)

def signed_link_ttl_seconds() -> int:
    raw = os.environ.get("GLASSHIVE_SIGNED_LINK_TTL_S", "").strip()
    if raw.lower() in {"0", "none", "never", "disabled", "off", "false", "no"}:
        return 0
    try:
        value = int(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        value = DEFAULT_TTL_SECONDS
    return max(60, min(value, 24 * 3600))


def link_ref_ttl_seconds() -> int:
    raw = (
        os.environ.get("GLASSHIVE_LINK_REF_TTL_SECONDS", "").strip()
        or os.environ.get("WPR_LINK_REF_TTL_SECONDS", "").strip()
    )
    if not raw:
        return DEFAULT_LINK_REF_TTL_SECONDS
    if raw.lower() in {"0", "none", "never", "disabled", "off", "false", "no"}:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LINK_REF_TTL_SECONDS
    return max(0, value)


def signed_link_ttl_for_kind(kind: str, ttl_seconds: int | None = None) -> int:
    explicit = ttl_seconds is not None
    ttl = int(ttl_seconds) if explicit else signed_link_ttl_seconds()
    if explicit and ttl <= 0:
        return ttl
    if not explicit and ttl <= 0:
        return 0
    if kind == "worker_view":
        raw = os.environ.get("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "").strip()
        try:
            watch_ttl = int(raw) if raw else 0
        except ValueError:
            watch_ttl = 0
        if watch_ttl > 0:
            ttl = min(ttl, watch_ttl)
    return min(ttl, 24 * 3600) if explicit else max(60, min(ttl, 24 * 3600))


def _message(
    *,
    kind: str,
    worker_id: str,
    tenant_id: str,
    owner_id: str,
    path: str,
    expires_at: int,
) -> str:
    return "\0".join(
        [
            "v1",
            str(kind or ""),
            str(worker_id or ""),
            str(tenant_id or ""),
            str(owner_id or ""),
            str(path or ""),
            str(expires_at),
        ]
    )


def _signature(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), sha256).hexdigest()


def sign_link_params(
    *,
    kind: str,
    worker_id: str,
    tenant_id: str,
    owner_id: str,
    path: str = "",
    ttl_seconds: int | None = None,
    expires_at: int | None = None,
) -> dict[str, str]:
    secret = signed_link_secret()
    if not secret or worker_signed_links_blocked(worker_id):
        return {}
    if expires_at is not None:
        resolved_expires_at = int(expires_at)
    else:
        ttl = signed_link_ttl_for_kind(kind, ttl_seconds)
        resolved_expires_at = 0 if ttl == 0 else int(time.time()) + ttl
    message = _message(
        kind=kind,
        worker_id=worker_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        path=path,
        expires_at=resolved_expires_at,
    )
    signature = _signature(secret, message)
    return {
        "gh_kind": kind,
        "gh_exp": str(resolved_expires_at),
        "gh_sig": signature,
    }


def append_signed_query(url: str, params: dict[str, str]) -> str:
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


def redact_sensitive_url_text(value: object) -> str:
    text = str(value or "")
    text = _SENSITIVE_QUERY_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _SIGNED_LINK_PATH_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _LINK_REF_PATH_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    return _WORKER_COOKIE_TOKEN_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)


def _redact_log_value(value: object) -> object:
    """Redact sensitive URL material without coercing harmless structured args."""
    rendered = str(value or "")
    redacted = redact_sensitive_url_text(rendered)
    return redacted if redacted != rendered else value


class SensitiveUrlLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_log_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_log_value(item) for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_log_value(value) for key, value in record.args.items()}
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_LOG_RECORD_FIELDS:
                continue
            record.__dict__[key] = _redact_log_value(value)
        return True


def install_sensitive_url_log_filter() -> None:
    global _LOG_FILTER_INSTALLED, _LOG_RECORD_FACTORY_INSTALLED
    log_filter = SensitiveUrlLogFilter()
    target_loggers = [
        logging.getLogger(),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("workers_projects_runtime"),
    ]
    for logger in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(logger, logging.Logger) and logger.name.startswith("workers_projects_runtime."):
            target_loggers.append(logger)
    seen_handlers: set[int] = set()
    for logger in target_loggers:
        if not any(isinstance(existing, SensitiveUrlLogFilter) for existing in logger.filters):
            logger.addFilter(log_filter)
        for handler in logger.handlers:
            handler_id = id(handler)
            if handler_id in seen_handlers:
                continue
            seen_handlers.add(handler_id)
            if not any(isinstance(existing, SensitiveUrlLogFilter) for existing in handler.filters):
                handler.addFilter(log_filter)
    if not _LOG_RECORD_FACTORY_INSTALLED:
        original_factory = logging.getLogRecordFactory()

        def redacting_record_factory(*args, **kwargs):
            record = original_factory(*args, **kwargs)
            log_filter.filter(record)
            return record

        logging.setLogRecordFactory(redacting_record_factory)
        _LOG_RECORD_FACTORY_INSTALLED = True
    _LOG_FILTER_INSTALLED = True


def link_ref_state_path() -> Path:
    raw = str(os.environ.get("GLASSHIVE_LINK_REF_STATE_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    watch_state = str(os.environ.get("GLASSHIVE_WATCH_SESSION_STATE_PATH") or "").strip()
    if watch_state:
        return Path(watch_state).expanduser().parent / "link_refs.sqlite3"
    state_root = (
        Path(os.environ["XDG_STATE_HOME"]).expanduser()
        if os.environ.get("XDG_STATE_HOME")
        else Path.home() / ".local" / "state"
    )
    return state_root / "glasshive" / "link_refs.sqlite3"


def _harden_sqlite_state_path(db_path: Path) -> None:
    if os.name == "nt":
        return
    shared_gid = _shared_link_ref_group_gid()
    if shared_gid is not None:
        _validate_shared_link_ref_state(db_path, shared_gid)
        return
    db_path.parent.chmod(0o700)
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists() and not candidate.is_symlink():
            candidate.chmod(0o600)


def _shared_link_ref_group_gid() -> int | None:
    group_name = str(os.environ.get("GLASSHIVE_LINK_REF_SHARED_GROUP") or "").strip()
    if not group_name:
        return None
    if grp is None:
        raise PermissionError("shared link reference groups require a POSIX host")
    try:
        group_gid = int(grp.getgrnam(group_name).gr_gid)
    except KeyError as exc:
        raise PermissionError("configured shared link reference group does not exist") from exc
    process_groups = {int(os.getegid()), *(int(value) for value in os.getgroups())}
    if os.geteuid() != 0 and group_gid not in process_groups:
        raise PermissionError("process is not a member of the shared link reference group")
    return group_gid


def _validate_shared_link_ref_state(db_path: Path, shared_gid: int) -> None:
    parent = db_path.parent
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise PermissionError("shared link reference directory is unavailable") from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent.is_symlink()
        or parent.resolve(strict=True) != parent.absolute()
        or int(parent_stat.st_uid) not in {0, os.geteuid()}
        or int(parent_stat.st_gid) != shared_gid
        or stat.S_IMODE(parent_stat.st_mode) != (0o2770 if sys.platform.startswith("linux") else 0o770)
    ):
        raise PermissionError("shared link reference directory has unsafe ownership or permissions")
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            if candidate == db_path:
                raise PermissionError("shared link reference database must be pre-created")
            continue
        except OSError as exc:
            raise PermissionError("shared link reference database is unavailable") from exc
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or candidate.is_symlink()
            or int(candidate_stat.st_gid) != shared_gid
            or stat.S_IMODE(candidate_stat.st_mode) != 0o660
        ):
            raise PermissionError("shared link reference database has unsafe ownership or permissions")


@contextmanager
def _link_ref_conn() -> Iterator[sqlite3.Connection]:
    db_path = link_ref_state_path()
    if _shared_link_ref_group_gid() is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        _harden_sqlite_state_path(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _harden_sqlite_state_path(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signed_link_refs (
                ref_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                token TEXT NOT NULL,
                target_url TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '',
                scope_key TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signed_link_worker_revocations (
                worker_id TEXT PRIMARY KEY,
                revoked_at INTEGER NOT NULL
            )
            """
        )
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(signed_link_refs)").fetchall()}
        if "payload_json" not in columns:
            conn.execute("ALTER TABLE signed_link_refs ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''")
        if "scope_key" not in columns:
            conn.execute("ALTER TABLE signed_link_refs ADD COLUMN scope_key TEXT NOT NULL DEFAULT ''")
        _migrate_legacy_link_ref_rows(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signed_link_refs_expires_at ON signed_link_refs(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signed_link_refs_scope_key ON signed_link_refs(scope_key)")
        _harden_sqlite_state_path(db_path)
        with conn:
            yield conn
    finally:
        conn.close()


def _payload_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _scope_key(payload: dict[str, object], target_url: str = "") -> str:
    scoped = {
        "kind": str(payload.get("kind") or ""),
        "worker_id": str(payload.get("worker_id") or ""),
        "tenant_id": str(payload.get("tenant_id") or ""),
        "owner_id": str(payload.get("owner_id") or ""),
        "path": str(payload.get("path") or ""),
        "target_url": str(target_url or ""),
    }
    return json.dumps(scoped, sort_keys=True, separators=(",", ":"))


def _migrate_legacy_link_ref_rows(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT ref_id, token, target_url, payload_json, scope_key FROM signed_link_refs WHERE payload_json = '' OR scope_key = ''"
    ).fetchall()
    if not rows:
        return
    default_never_expires = link_ref_ttl_seconds() <= 0
    for row in rows:
        payload = _decode_signed_link_token(str(row["token"] or ""), allow_expired=True)
        if not payload:
            continue
        expires_at = 0 if default_never_expires else None
        payload_json = str(row["payload_json"] or "") or _payload_json(payload)
        scope_key = str(row["scope_key"] or "") or _scope_key(payload, str(row["target_url"] or ""))
        if expires_at is None:
            conn.execute(
                "UPDATE signed_link_refs SET payload_json = ?, scope_key = ? WHERE ref_id = ?",
                (payload_json, scope_key, row["ref_id"]),
            )
        else:
            conn.execute(
                "UPDATE signed_link_refs SET payload_json = ?, scope_key = ?, expires_at = ? WHERE ref_id = ?",
                (payload_json, scope_key, expires_at, row["ref_id"]),
            )


def create_signed_link_ref(*, token: str, target_url: str = "") -> str:
    payload = verify_signed_link_token(token)
    if not payload:
        return ""
    now = int(time.time())
    ref_ttl = link_ref_ttl_seconds()
    expires_at = now + ref_ttl if ref_ttl > 0 else 0
    payload_json = _payload_json(payload)
    scope_key = _scope_key(payload, target_url)
    with _link_ref_conn() as conn:
        conn.execute("DELETE FROM signed_link_refs WHERE expires_at > 0 AND expires_at < ?", (now,))
        existing = conn.execute(
            """
            SELECT ref_id, token FROM signed_link_refs
            WHERE scope_key = ?
              AND (expires_at = 0 OR expires_at >= ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (scope_key, now),
        ).fetchone()
        if existing is not None:
            if str(payload.get("kind") or "") != "worker_view":
                return str(existing["ref_id"] or "")
            existing_payload = _decode_signed_link_token(str(existing["token"] or ""), allow_expired=True)
            try:
                existing_exp = int((existing_payload or {}).get("exp") or 0)
            except (TypeError, ValueError):
                existing_exp = 0
            if existing_exp >= now:
                return str(existing["ref_id"] or "")
        ref_id = f"ghr_{uuid.uuid4().hex[:24]}"
        conn.execute(
            """
            INSERT INTO signed_link_refs (ref_id, kind, token, target_url, expires_at, created_at, payload_json, scope_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref_id,
                str(payload.get("kind") or ""),
                str(token or ""),
                str(target_url or ""),
                expires_at,
                now,
                payload_json,
                scope_key,
            ),
        )
    return ref_id


def is_worker_signed_link_revoked(worker_id: str) -> bool:
    clean_worker_id = str(worker_id or "").strip()
    if not clean_worker_id:
        return False
    db_path = link_ref_state_path()
    if not db_path.is_file():
        return False
    try:
        with closing(sqlite3.connect(db_path, timeout=1)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'signed_link_worker_revocations'"
            ).fetchone()
            if table is None:
                return False
            return (
                conn.execute(
                    "SELECT 1 FROM signed_link_worker_revocations "
                    "WHERE worker_id = ?",
                    (clean_worker_id,),
                ).fetchone()
                is not None
            )
    except sqlite3.Error:
        return False

def revoke_signed_link_refs_for_worker(worker_id: str) -> int:
    clean_worker_id = str(worker_id or "").strip()
    if not clean_worker_id:
        return 0
    worker_value = json.dumps(clean_worker_id, separators=(",", ":"))
    pattern = f'%"worker_id":{worker_value}%'
    with _link_ref_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO signed_link_worker_revocations (
                worker_id, revoked_at
            ) VALUES (?, ?)
            """,
            (clean_worker_id, int(time.time())),
        )
        cursor = conn.execute("DELETE FROM signed_link_refs WHERE scope_key LIKE ?", (pattern,))
        return int(cursor.rowcount or 0)


def resolve_signed_link_ref(ref_id: str) -> dict[str, object] | None:
    clean_ref = str(ref_id or "").strip()
    if not _SAFE_LINK_REF_RE.fullmatch(clean_ref):
        return None
    now = int(time.time())
    with _link_ref_conn() as conn:
        conn.execute("DELETE FROM signed_link_refs WHERE expires_at > 0 AND expires_at < ?", (now,))
        row = conn.execute(
            "SELECT * FROM signed_link_refs WHERE ref_id = ?",
            (clean_ref,),
        ).fetchone()
    if row is None:
        return None
    token = str(row["token"] or "")
    verified_payload = _decode_signed_link_token(token, allow_expired=True)
    if not verified_payload:
        return None
    if worker_signed_links_blocked(
        str(verified_payload.get("worker_id") or "")
    ):
        return None
    return {
        "ref_id": clean_ref,
        "kind": str(verified_payload.get("kind") or ""),
        "token": token,
        "target_url": str(row["target_url"] or ""),
        "expires_at": int(row["expires_at"]),
        "created_at": int(row["created_at"]),
        "payload": verified_payload,
    }


def signed_link_ref_url(base_url: str, ref_id: str, *, route: str = LINK_REF_ROUTE) -> str:
    clean_ref = str(ref_id or "").strip()
    if not clean_ref:
        return ""
    return f"{str(base_url or '').rstrip('/')}{route}/{quote(clean_ref, safe='')}"


def verify_signed_link(
    *,
    kind: str,
    worker_id: str,
    tenant_id: str,
    owner_id: str,
    path: str = "",
    expires_at: str,
    signature: str,
) -> bool:
    secret = signed_link_secret()
    if (
        not secret
        or not signature
        or worker_signed_links_blocked(worker_id)
    ):
        return False
    try:
        exp_int = int(str(expires_at or ""))
    except ValueError:
        return False
    if exp_int > 0 and exp_int < int(time.time()):
        return False
    message = _message(
        kind=kind,
        worker_id=worker_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        path=path,
        expires_at=exp_int,
    )
    expected = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature or ""))


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padded = f"{value}{'=' * (-len(value) % 4)}"
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign_link_token(
    *,
    kind: str,
    worker_id: str,
    tenant_id: str,
    owner_id: str,
    path: str = "",
    ttl_seconds: int | None = None,
) -> str:
    secret = signed_link_secret()
    if not secret or worker_signed_links_blocked(worker_id):
        return ""
    ttl = signed_link_ttl_for_kind(kind, ttl_seconds)
    payload = {
        "v": 1,
        "kind": str(kind or ""),
        "worker_id": str(worker_id or ""),
        "tenant_id": str(tenant_id or ""),
        "owner_id": str(owner_id or ""),
        "path": str(path or ""),
        "iat": int(time.time()),
        "exp": 0 if ttl == 0 else int(time.time()) + ttl,
    }
    encoded = _base64url_encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_signature(secret, encoded)}"


def _decode_signed_link_token(token: str, *, allow_expired: bool = False) -> dict[str, object] | None:
    secret = signed_link_secret()
    if not secret:
        return None
    parts = str(token or "").split(".", 1)
    if len(parts) != 2:
        return None
    encoded, signature = parts
    if not encoded or not hmac.compare_digest(_signature(secret, encoded), signature):
        return None
    try:
        payload = json.loads(_base64url_decode(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    try:
        expires_at = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if expires_at > 0 and expires_at < int(time.time()) and not allow_expired:
        return None
    return payload


def verify_signed_link_token(token: str) -> dict[str, object] | None:
    payload = _decode_signed_link_token(token, allow_expired=False)
    if payload and worker_signed_links_blocked(
        str(payload.get("worker_id") or "")
    ):
        return None
    return payload
