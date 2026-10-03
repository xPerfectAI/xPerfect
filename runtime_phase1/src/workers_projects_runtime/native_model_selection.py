"""Exact native model selection shared by Connections and worker admission."""
from __future__ import annotations

import os
import shutil
import subprocess
import hashlib
import json
import time
import logging
from contextvars import ContextVar
from functools import lru_cache
from concurrent.futures import Future
from pathlib import Path
from threading import RLock

from .failure_classification import FailureClassification
from .openclaw_runtime import RuntimeConfigurationError


class ModelConfigurationRequired(RuntimeConfigurationError):
    code = "model_configuration_required"


def gateway_codex_model_required() -> ModelConfigurationRequired:
    """A gateway route authorizes one exact model, so native Codex's own default cannot run there."""
    error = ModelConfigurationRequired(
        "This AI connection runs through xPerfect's model gateway, which needs an exact Codex model. "
        "Set --model codex-cli=<id> when starting xPerfect."
    )
    error.failure_classification = FailureClassification(
        failure_class=ModelConfigurationRequired.code,
        retryable=False,
        user_message=str(error),
        recommended_recovery=(
            "Start or upgrade xPerfect with --model codex-cli=<id>, using an exact model this "
            "connection lists, then continue the worker."
        ),
        diagnostic_summary="Gateway-routed Codex run has no configured exact model.",
        structured=True,
    )
    return error


def valid_model_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 180 and all(ord(char) >= 33 for char in value)


class _NativeCatalogUnavailable(Exception):
    pass


_catalog_lock = RLock()
_catalog_inflight: dict[tuple, Future] = {}
_catalog_observation: ContextVar[dict | None] = ContextVar("native_catalog_observation", default=None)
_logger = logging.getLogger(__name__)


def native_timing_enabled() -> bool:
    return os.environ.get("VIVENTIUM_VOICE_LOG_LATENCY", "").strip().lower() in {"1", "true", "yes", "on"}


def native_timing_hash(kind: str, value: object) -> str:
    return hashlib.sha256(repr(("native-p0-v1", kind, value)).encode()).hexdigest()


def native_timing_event(stage: str, **fields: object) -> None:
    if not native_timing_enabled():
        return
    try:
        _logger.info("[NativeP0] %s", json.dumps({
            "event": "native_p0", "stage": stage,
            "observedAtMs": time.time_ns() / 1_000_000,
            "monotonicMs": time.monotonic_ns() / 1_000_000, **fields,
        }, separators=(",", ":")))
    except Exception:
        pass


def _file_identity(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return (str(path), None)


def _native_catalog_scope(binary: str) -> tuple:
    home = Path(os.environ.get('GROK_HOME') or Path.home() / '.grok').expanduser()
    roots = {home, *(parent / '.grok' for parent in (Path.cwd(), *Path.cwd().parents))}
    files = {root / name for root in roots for name in (
        'auth.json', 'settings.json', 'config.toml', 'managed_config.toml',
        'requirements.toml', 'managed-settings.json',
    )}
    for name in ('GROK_AUTH_PATH', 'GROK_CONFIG_PATH'):
        if os.environ.get(name):
            files.add(Path(os.environ[name]).expanduser())
    # Only a digest stays in the cache key; environment values and credential contents are not logged.
    environment = hashlib.sha256(json.dumps(sorted(os.environ.items())).encode()).digest()
    return (_file_identity(Path(binary)), str(Path.cwd()), environment,
            tuple(_file_identity(path) for path in sorted(files)))


def native_grok_models() -> list[str]:
    """Read the installed native CLI's catalog; never infer an ID from its default."""
    configured = os.environ.get("WPR_GROK_BIN") or "grok"
    binary = configured if os.path.isabs(configured) else shutil.which(configured)
    if not binary:
        return []
    scope = _native_catalog_scope(binary)
    observation = {"misses": 0} if native_timing_enabled() else None
    token = _catalog_observation.set(observation) if observation is not None else None
    started = time.monotonic_ns() if observation is not None else 0
    decision, status, age = "join", "completed", None
    key = (binary, scope)
    with _catalog_lock:
        flight = _catalog_inflight.get(key)
        leader = flight is None
        if leader:
            flight = Future()
            _catalog_inflight[key] = flight
    try:
        if leader:
            try:
                decision = "miss_scope_or_cold"
                observed, models = _native_grok_catalog(binary, scope)
                age = time.monotonic() - observed
                decision = "miss_scope_or_cold" if observation and observation["misses"] else "hit"
                if age >= 30.0:
                    decision = "miss_ttl"
                    _native_grok_catalog.cache_clear()
                    observed, models = _native_grok_catalog(binary, scope)
                flight.set_result((observed, models))
            except BaseException as exc:
                flight.set_exception(exc)
                raise
            finally:
                with _catalog_lock:
                    _catalog_inflight.pop(key, None)
        else:
            _, models = flight.result()
        return list(models)
    except _NativeCatalogUnavailable:
        status = "unavailable"
        return []
    except BaseException:
        status = "failed"
        raise
    finally:
        if observation is not None:
            _catalog_observation.reset(token)
            native_timing_event("catalog_lookup", decision=decision, status=status,
                                durationMs=(time.monotonic_ns() - started) / 1_000_000,
                                cacheAgeMs=age * 1000 if age is not None else None,
                                subprocessCount=observation["misses"],
                                scopeHash=native_timing_hash("catalog_scope", scope),
                                componentHashes={name: native_timing_hash(name, value) for name, value in zip(
                                    ("binary", "cwd", "environment", "managed_files"), scope)})


@lru_cache(maxsize=16)
def _native_grok_catalog(binary: str, scope: tuple) -> tuple[float, tuple[str, ...]]:
    # Match existing native help caching: failures raise and are not cached.
    observation = _catalog_observation.get()
    started = time.monotonic_ns() if observation is not None else 0
    if observation is not None:
        observation["misses"] += 1
    try:
        result = subprocess.run(
            [binary, "models"], capture_output=True, text=True, timeout=12, check=False,
            env={**os.environ, "GROK_DISABLE_AUTOUPDATER": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        if observation is not None:
            native_timing_event("catalog_subprocess", status="unavailable",
                                durationMs=(time.monotonic_ns() - started) / 1_000_000,
                                scopeHash=native_timing_hash("catalog_scope", scope))
        raise _NativeCatalogUnavailable from None
    if observation is not None:
        native_timing_event("catalog_subprocess", status="completed" if result.returncode == 0 else "unavailable",
                            durationMs=(time.monotonic_ns() - started) / 1_000_000,
                            scopeHash=native_timing_hash("catalog_scope", scope))
    if result.returncode != 0:
        raise _NativeCatalogUnavailable
    listed = False
    models: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped == "Available models:":
            listed = True
            continue
        if not listed:
            continue
        if not stripped:
            if models:
                break
            continue
        if not stripped.startswith(("* ", "- ")):
            break
        model = stripped[2:].split(" (", 1)[0]
        if valid_model_id(model) and model not in models:
            models.append(model)
    if not models:
        raise _NativeCatalogUnavailable
    return time.monotonic(), tuple(models)


def selected_grok_model(store, tenant_id: str = "local", owner_id: str = "") -> tuple[str, str]:
    """Deployment selection wins; a saved owner choice is used only when absent."""
    deployment = os.environ.get("WPR_MODEL_GROK_BUILD", "")
    if deployment:
        if not valid_model_id(deployment):
            raise ModelConfigurationRequired("The deployment Grok model is invalid. Set an exact model with --model grok-build=<id>.")
        return deployment, "deployment"
    if owner_id and store is not None:
        saved = (store.get_user_preferences(tenant_id, owner_id) or {}).get("grok_model", "")
        if saved:
            if not valid_model_id(saved):
                raise ModelConfigurationRequired("Your saved Grok model is invalid. Choose an exact model in Connections.")
            return saved, "owner"
    raise ModelConfigurationRequired("Choose an exact Grok model in Connections, or set --model grok-build=<id> when starting xPerfect.")
