from __future__ import annotations

from .native_context_projection import apply_claude_context, prepare_private_command
from .worker_configuration import contextual_instruction, enforce_context_tools

from .secret_redaction import CREDENTIAL_REDACTIONS, RedactionRule
from . import native_input
from .account_native_launch import GuardedAccountLauncher

import json
import base64
import fcntl
import hashlib
import hmac
import logging
import math
import mimetypes
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    import tomli as tomllib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable

from .agent_builder_control import conversation_output_schema
from .auth import multi_user_security_enabled, native_installed_owner_id, require_native_installed_owner
from .bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_NATIVE_CAPABILITY_INVENTORY,
    GLASSHIVE_PROVIDER_SESSION_MODE_ENV,
    GLASSHIVE_PROVIDER_SESSION_EPOCH_ENV,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    GLASSHIVE_WORKER_COMPLETION_CONTRACT,
    _worker_prompt,
    bootstrap_env_for,
    canonicalize_viventium_feeling_projection,
    claude_project_mcp_payload_for_bundle,
    glasshive_project_agents_md,
    glasshive_project_claude_md,
    glasshive_project_codex_md,
    merge_glasshive_worker_instructions,
    worker_prompt_layer_producer,
    refresh_project_runtime_files_for_worker,
    refresh_runtime_env_for_worker,
    resolve_authorized_bootstrap_source_path,
    resolve_bootstrap_source_path,
)
from .profile_registry import require_worker_profile, UnsupportedWorkerProfileError
from .docker_sandbox import DockerSandboxManager
from .deliverables import (
    capture_native_media, _native_media_snapshot, NATIVE_IMAGE_SUFFIXES,
    NATIVE_MEDIA_MAX_ITEMS, NATIVE_MEDIA_MAX_BYTES, NATIVE_MEDIA_MAX_TOTAL_BYTES,
)
from .failure_classification import (
    FailureClassification,
    classify_cli_failure,
    compact_provider_failure_diagnostic,
    classify_runtime_error,
)
from .capability_broker import (
    GlassHiveCapabilityBroker,
    worker_with_ephemeral_capability_bundle,
)
from .inference_broker import (
    GlassHiveInferenceBroker,
    InferenceBrokerError,
    validated_codex_broker_projection,
)
from .mission_provider_accounts import (
    MissionProviderAccountBinder,
    MissionProviderAccountSelection,
    apply_bound_provider_account_environment,
    deployment_provider_readiness,
    mission_provider_account_selection,
    native_current_account_allowed,
)
from .openclaw_runtime import (
    HostCapacityError,
    ProviderAuthenticationMissingError,
    ProviderRateLimitError,
    RuntimeErrorBase,
    RuntimeDependencyMissingError,
    RuntimeInfo,
    WorkerDetachedError,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
    _PROVIDER_ENV_KEYS,
    notify_runtime_started,
    notify_runtime_info,
    runtime_start_boundary,
)
from .openclaw_release import reviewed_openclaw_env
from .provider_accounts import ProviderAccountHomeManager
from .runtime_requirements import CLAUDE_CODE_EFFORT_LEVELS, host_runtime_requirement_issue
from .run_evidence import (
    FINAL_REPORT_PATTERN,
    build_constraint_ledger,
    build_run_evidence,
    write_constraint_ledger,
    write_run_evidence,
)
from .terminal_takeover import TerminalTarget

import fcntl

import hmac

import math

import queue

import sqlite3

import tempfile

from contextlib import contextmanager

from urllib.parse import unquote, urlsplit

from .agent_builder_control import graph_transfer_output_schema

from .bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_NATIVE_CAPABILITY_INVENTORY,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    GLASSHIVE_WORKER_COMPLETION_CONTRACT,
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    _split_viventium_feeling_capsules,
    _write_clean_room_file,
    bootstrap_bundle_for,
    bootstrap_env_for,
    canonicalize_viventium_feeling_projection,
    claude_project_mcp_payload_for_bundle,
    glasshive_project_agents_md,
    glasshive_project_claude_md,
    glasshive_project_codex_md,
    merge_glasshive_worker_instructions,
    worker_prompt_layer_producer,
    refresh_project_runtime_files_for_worker,
    refresh_runtime_env_for_worker,
    resolve_authorized_bootstrap_source_path,
    resolve_bootstrap_source_path,
)

from .durable_capture import DurableSecretScrubber, scrub_durable_text_artifacts

from .native_team import project_native_events

from .openclaw_runtime import (
    HostCapacityError,
    ProviderAuthenticationMissingError,
    ProviderRateLimitError,
    RuntimeErrorBase,
    RuntimeDependencyMissingError,
    RuntimeInfo,
    RunStartupRejectedError,
    WorkerDetachedError,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
    _PROVIDER_ENV_KEYS,
)

from .run_evidence import (
    FINAL_REPORT_PATTERN,
    build_constraint_ledger,
    build_run_evidence,
    write_constraint_ledger,
    write_run_evidence,
)


logger = logging.getLogger(__name__)

_ACTIVE_SESSION_EXPECTATION_UNSET = object()
_HOST_SLOT_EXPECTATION_UNSET = object()

def _declared_long_mission(worker: dict) -> bool:
    try:
        bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(bundle, dict)
        and bundle.get("viventium_run_liveness")
        == {"version": 1, "long_mission": True}
    )

def _drain_scrubbed_provider_stream(
    stream: object,
    destination: object,
    scrubber: DurableSecretScrubber,
    errors: list[BaseException],
) -> None:
    """Copy one provider pipe into a durable transcript after structural scrubbing."""

    try:
        for line in stream:  # type: ignore[union-attr]
            destination.write(scrubber.scrub_text(str(line)))  # type: ignore[union-attr]
            destination.flush()  # type: ignore[union-attr]
    except BaseException as exc:  # pragma: no cover - defensive pipe/filesystem boundary
        errors.append(exc)
    finally:
        try:
            stream.close()  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

def _scrub_provider_owned_artifacts(
    *,
    state_dir: Path,
    home_dir: Path,
    run_root: Path,
    workspace: Path,
    scrubber: DurableSecretScrubber,
) -> None:
    # Deliberately exclude the worker workspace: it can contain user-authored
    # files and deliverables whose contents GlassHive must never rewrite.
    roots = [state_dir, home_dir, run_root]
    harness_workspace = workspace / "glasshive-run"
    if harness_workspace.exists():
        roots.append(harness_workspace)
    scrub_durable_text_artifacts(roots, scrubber=scrubber)

def _provider_process_exit_error(
    *,
    runtime_name: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    message: str,
    classification: FailureClassification | None = None,
) -> RuntimeErrorBase:
    classification = classification or classify_cli_failure(
        stdout=stdout,
        stderr=stderr,
        runtime_name=runtime_name,
        exit_code=exit_code,
    )
    attestation = {
        "version": 1,
        "producer": f"glasshive.profile_runtime.{runtime_name}",
        "evidence_kind": str(classification.provider_event_source or ""),
        "schema": "provider_native_terminal_v1",
    }
    if (
        classification.failure_class == "provider_rate_limited"
        and classification.retryable
        and classification.retry_after_s is not None
    ):
        return ProviderRateLimitError(
            message,
            retry_after_s=classification.retry_after_s,
            provider_event_source=classification.provider_event_source,
            provider_failure_attestation=attestation,
        )
    error = RuntimeErrorBase(message)
    # Preserve the structured provider classification across the process-exit boundary. Downstream
    # recovery must not infer authentication state from localized CLI prose.
    if classification.structured:
        error.failure_classification = classification
        error.failure_class = classification.failure_class
        error.failure_retryable = classification.retryable
        error.provider_event_source = classification.provider_event_source
        if classification.provider_event_source:
            error.provider_failure_attestation = attestation
    return error

def _codex_preauthoring_failed_thread_id(stdout: str) -> str:
    """Return one failed Codex thread only when no model/tool item was emitted."""

    if len(stdout.encode("utf-8", errors="ignore")) > 256 * 1024:
        return ""
    events: list[dict[str, object]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(events) >= 32:
            return ""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return ""
        if not isinstance(event, dict):
            return ""
        events.append(event)
    event_types = [str(event.get("type") or "") for event in events]
    if (
        len(events) < 3
        or event_types[0] != "thread.started"
        or event_types[1] != "turn.started"
        or event_types[-1] != "turn.failed"
        or any(event_type != "error" for event_type in event_types[2:-1])
        or event_types.count("thread.started") != 1
        or event_types.count("turn.started") != 1
        or event_types.count("turn.failed") != 1
    ):
        return ""
    thread_id = str(events[0].get("thread_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        return ""
    return thread_id


def _codex_failed_thread(stdout: str) -> tuple[str, bool]:
    """Bind one failed CLI turn to its native thread, including authored turns.

    Authored turns may be classified from the app-server's typed failure, but
    must never be retried or switched to a fallback automatically.
    """
    preauthoring = _codex_preauthoring_failed_thread_id(stdout)
    if preauthoring:
        return preauthoring, False
    if len(stdout.encode("utf-8", errors="ignore")) > 4 * 1024 * 1024:
        return "", False
    events: list[dict[str, object]] = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        if len(events) >= 4096:
            return "", False
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return "", False
        if not isinstance(event, dict):
            return "", False
        events.append(event)
    types = [str(event.get("type") or "") for event in events]
    if (len(events) < 3 or types[:2] != ["thread.started", "turn.started"]
            or types[-1] != "turn.failed"
            or any(types.count(kind) != 1 for kind in ("thread.started", "turn.started", "turn.failed"))):
        return "", False
    thread_id = str(events[0].get("thread_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        return "", False
    authored = any(kind != "error" for kind in types[2:-1])
    return (thread_id, True) if authored else ("", False)

def _codex_typed_provider_failure(
    *,
    thread_id: str,
    thread_result: dict[str, object],
    rate_limits_result: dict[str, object],
    expected_model_provider: str = "openai",
    now: datetime | None = None,
    authored: bool = False,
) -> tuple[FailureClassification, dict[str, object]] | None:
    """Classify only the official app-server result bound to one failed thread."""

    thread = thread_result.get("thread")
    if not isinstance(thread, dict):
        return None
    if (
        str(thread.get("id") or "") != thread_id
        or str(thread.get("modelProvider") or "")
        != str(expected_model_provider or "").strip()
    ):
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns or not isinstance(turns[-1], dict):
        return None
    final_turn = turns[-1]
    error = final_turn.get("error")
    items = final_turn.get("items")
    if (
        str(final_turn.get("status") or "") != "failed"
        or not isinstance(error, dict)
        or error.get("codexErrorInfo") != "usageLimitExceeded"
        or not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        or (authored != any(item.get("type") != "userMessage" for item in items))
    ):
        return None

    snapshots: list[dict[str, object]] = []
    by_limit_id = rate_limits_result.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict) and isinstance(by_limit_id.get("codex"), dict):
        snapshots.append(by_limit_id["codex"])
    legacy_snapshot = rate_limits_result.get("rateLimits")
    if isinstance(legacy_snapshot, dict):
        snapshots.append(legacy_snapshot)
    reached_types = {
        str(snapshot.get("rateLimitReachedType") or "")
        for snapshot in snapshots
    }
    reached_types.discard("")
    quota_types = {
        "workspace_owner_credits_depleted",
        "workspace_member_credits_depleted",
        "workspace_owner_usage_limit_reached",
        "workspace_member_usage_limit_reached",
    }
    if reached_types and reached_types <= quota_types:
        failure_class = "provider_quota_exhausted"
        user_message = (
            "The selected model provider quota was exhausted before the worker could finish."
        )
        recovery = (
            "Keep the same configured route queued until its exact provider reset, unless an "
            "explicitly configured fallback is available."
        )
    elif reached_types == {"rate_limit_reached"}:
        failure_class = "provider_rate_limited"
        user_message = "The selected model provider rate-limited the worker."
        recovery = "Retry the same configured route after its exact provider reset."
    elif not reached_types:
        failure_class = "provider_quota_exhausted"
        user_message = (
            "The selected model provider quota was exhausted before the worker could finish."
        )
        recovery = (
            "Continue the same untouched mission with its explicitly configured fallback."
        )
    else:
        return None

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    reset_epochs: list[int] = []
    for snapshot in snapshots:
        for window_name in ("primary", "secondary"):
            window = snapshot.get(window_name)
            if not isinstance(window, dict):
                continue
            used_percent = window.get("usedPercent")
            reset_epoch = window.get("resetsAt")
            if (
                isinstance(used_percent, int)
                and not isinstance(used_percent, bool)
                and used_percent >= 100
                and isinstance(reset_epoch, int)
                and not isinstance(reset_epoch, bool)
                and current.timestamp() < reset_epoch <= current.timestamp() + 400 * 86_400
            ):
                reset_epochs.append(reset_epoch)
    retry_at = (
        datetime.fromtimestamp(max(reset_epochs), tz=timezone.utc).isoformat()
        if reset_epochs
        else ""
    )
    retry_after_s = (
        max(0.0, max(reset_epochs) - current.timestamp())
        if reset_epochs
        else None
    )
    final_turn_id = str(final_turn.get("id") or "").strip()
    evidence_material = json.dumps(
        {
            "thread_id": thread_id,
            "turn_id": final_turn_id,
            "failure_class": failure_class,
            "retry_at": retry_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if authored:
        recovery = (
            "Wait for the selected provider's reset or explicitly choose another account. "
            "Review the existing workspace files before continuing; prior actions will not rerun automatically."
        )
    classification = FailureClassification(
        failure_class=failure_class,
        retryable=not authored,
        user_message=user_message,
        recommended_recovery=recovery,
        diagnostic_summary=(
            "Codex app-server returned a typed usage-limit result for the exact failed thread."
        ),
        structured=True,
        retry_after_s=retry_after_s,
        provider_event_source="codex_app_server",
    )
    return classification, {
        "version": 1,
        "failure_class": failure_class,
        "failure_structured": True,
        "retry_at": retry_at,
        "retry_after_s": retry_after_s,
        "evidence_kind": "codex_app_server",
        "evidence_id": hashlib.sha256(evidence_material.encode("utf-8")).hexdigest(),
    }

_CODEX_PROVIDER_CONTROL_MAX_OUTPUT_BYTES = 4 * 1024 * 1024

_CODEX_PROVIDER_CONTROL_CLIENT = r'''
import json
import queue
import subprocess
import sys
import threading
import time

MAX_LINE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_MESSAGES = 64
MAX_REQUEST_BYTES = 16 * 1024
DEADLINE = time.monotonic() + 8.0

binary, thread_id = sys.argv[1:3]
process = None
reader = None
messages = queue.Queue()


def read_messages():
    total_bytes = 0
    message_count = 0
    try:
        while True:
            line = process.stdout.readline(MAX_LINE_BYTES + 1)
            if not line:
                messages.put(None)
                return
            total_bytes += len(line)
            message_count += 1
            if (
                len(line) > MAX_LINE_BYTES
                or total_bytes > MAX_OUTPUT_BYTES
                or message_count > MAX_MESSAGES
            ):
                messages.put(False)
                return
            messages.put(line)
    except Exception:
        messages.put(False)


def send(payload):
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("app-server request exceeds the bounded protocol size")
    process.stdin.write(encoded)
    process.stdin.flush()


def await_result(request_id, allow_unavailable=False):
    while True:
        remaining = DEADLINE - time.monotonic()
        if remaining <= 0:
            return None
        try:
            message = messages.get(timeout=remaining)
        except queue.Empty:
            return None
        if not isinstance(message, bytes):
            return None
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("id") or "") != request_id:
            continue
        if "result" not in payload:
            if allow_unavailable and isinstance(payload.get("error"), dict):
                return {}
            return None
        result = payload.get("result")
        if isinstance(result, dict):
            return result
        if allow_unavailable and (
            result is None or isinstance(payload.get("error"), dict)
        ):
            return {}
        return None


try:
    process = subprocess.Popen(
        [binary, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    reader = threading.Thread(target=read_messages, daemon=True)
    reader.start()
    send(
        {
            "id": "glasshive-provider-health-init",
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "glasshive-provider-health",
                    "title": "GlassHive Provider Health",
                    "version": "1",
                },
                "capabilities": {},
            },
        }
    )
    if await_result("glasshive-provider-health-init") is None:
        raise RuntimeError("app-server initialization failed")
    send({"method": "initialized", "params": {}})
    send(
        {
            "id": "glasshive-provider-thread",
            "method": "thread/read",
            "params": {"threadId": thread_id, "includeTurns": True},
        }
    )
    thread_result = await_result("glasshive-provider-thread")
    if thread_result is None:
        raise RuntimeError("typed thread state is unavailable")
    send(
        {
            "id": "glasshive-provider-limits",
            "method": "account/rateLimits/read",
            "params": None,
        }
    )
    rate_limits_result = await_result(
        "glasshive-provider-limits", allow_unavailable=True
    )
    if rate_limits_result is None:
        raise RuntimeError("typed rate-limit state is unavailable")
    encoded = json.dumps(
        {
            "thread_result": thread_result,
            "rate_limits_result": rate_limits_result,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("typed provider control exceeds the bounded output size")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
except Exception:
    sys.exit(1)
finally:
    if process is not None:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
    if reader is not None:
        reader.join(timeout=0.25)
'''

_CODEX_MCP_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_HOST_CODEX_NATIVE_MCP_ALLOWLIST = ("computer-use", "node_repl", "cua_repl")
_CODEX_STALE_THREAD_ERROR = "thread/resume failed: no rollout found for thread id"
_CLAUDE_STALE_SESSION_ERROR = "No conversation found with session ID:"
_PERSONAL_ACCOUNT_RECONNECT_MARKER = "_glasshive_personal_account_reconnect_required"
_CODEX_NATIVE_WEB_LOCKDOWN_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "in_app_browser",
    "plugins",
    "remote_plugin",
)
_CODEX_RESTRICTED_NATIVE_FEATURES = (
    *_CODEX_NATIVE_WEB_LOCKDOWN_FEATURES,
    "shell_tool",
    "multi_agent",
    "image_generation",
    "view_image",
    "hooks",
)


def _native_tools_disabled(bundle: dict[str, object]) -> bool:
    capabilities = bundle.get("provider_capabilities")
    return isinstance(capabilities, dict) and capabilities.get("native_tools") is False


_FALSEY_ENV_VALUES = {"0", "false", "no", "off", "none", "disabled"}
_CODEX_BROKER_CONFLICTING_ENV = {
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
}
_RUN_SCOPED_CREDENTIAL_ENV_KEYS = tuple(
    sorted(
        set(_PROVIDER_ENV_KEYS)
        | _CODEX_BROKER_CONFLICTING_ENV
        | {
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
        }
    )
)
_MAX_TELEMETRY_LINE_BYTES = 1024 * 1024
_ACTIVE_RUN_STATUS_LOCK = Lock()
_ACTIVE_RUN_TERMINAL_STATES = frozenset(
    {"completed", "failed", "timeout", "paused", "interrupted", "terminated"}
)
_TELEMETRY_INTEGER_FIELDS = frozenset(
    {
        "duration_ms",
        "duration_api_ms",
        "ttft_ms",
        "ttft_stream_ms",
        "time_to_request_ms",
        "num_turns",
        "api_retry_count",
        "last_api_retry_event_sequence",
        "api_retry_delay_ms",
        "tool_call_count",
        "event_count",
        "assistant_event_count",
        "malformed_line_count",
        "oversized_line_count",
        "sample_sequence",
        "parsed_bytes",
        "log_bytes",
        "stream_input_tokens",
        "stream_output_tokens",
        "stream_cache_read_input_tokens",
        "stream_cache_creation_input_tokens",
    }
)
_TELEMETRY_TOKEN_FIELDS = frozenset(
    {
        "claude_code_version",
        "model",
        "service_tier",
        "speed",
        "result_state",
        "stop_reason",
        "telemetry_scope",
        "run_id",
    }
)
_TELEMETRY_TIMESTAMP_FIELDS = frozenset(
    {
        "first_timestamp",
        "last_timestamp",
        "sampled_at",
        "first_observed_at",
        "last_progress_at",
    }
)
_SAFE_TELEMETRY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/+-]{0,63}$")
_TELEMETRY_TOKEN_PATTERNS = {
    "claude_code_version": re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?$"),
    "model": re.compile(r"^(?:claude|anthropic)[A-Za-z0-9_.:/+-]{0,120}$"),
    "service_tier": re.compile(r"^(?:standard|priority|batch|flex|default)$"),
    "speed": re.compile(r"^(?:standard|fast|normal|default)$"),
    "result_state": re.compile(
        r"^(?:success|error|failed|cancelled|interrupted|timeout|error_[a-z0-9_]{1,48})$"
    ),
    "stop_reason": re.compile(
        r"^(?:end_turn|max_tokens|tool_use|stop_sequence|refusal|error|timeout|"
        r"cancelled|interrupted|terminated|paused|completed)$"
    ),
    "telemetry_scope": re.compile(
        r"^(?:full_active_run_incremental|full_active_run|console_tail|"
        r"active_run_unavailable)$"
    ),
    "run_id": re.compile(r"^(?:run[_-]?|r)[A-Za-z0-9_-]{0,120}$"),
}


def _private_cli_failure_classification(
    classification: FailureClassification,
    *,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
) -> FailureClassification:
    """Preserve typed failure metadata without carrying the worker transcript."""

    return replace(
        classification,
        diagnostic_summary=compact_provider_failure_diagnostic(
            stdout=stdout, stderr=stderr,
            classification=classification, exit_code=exit_code,
        ),
    )


def _safe_run_telemetry(value: object, *, run_id: str | None = None) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, object] = {}
    for key in _TELEMETRY_INTEGER_FIELDS:
        if key not in value or isinstance(value[key], bool):
            continue
        try:
            safe[key] = max(0, int(value[key]))
        except (TypeError, ValueError, OverflowError):
            continue
    for key in _TELEMETRY_TOKEN_FIELDS:
        text = str(value.get(key) or "").strip()
        if text and _TELEMETRY_TOKEN_PATTERNS[key].fullmatch(text):
            safe[key] = text
    for key in _TELEMETRY_TIMESTAMP_FIELDS:
        text = str(value.get(key) or "").strip()
        if not text:
            continue
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        safe[key] = text[:128]
    if isinstance(value.get("is_error"), bool):
        safe["is_error"] = value["is_error"]
    if isinstance(value.get("partial_line_present"), bool):
        safe["partial_line_present"] = value["partial_line_present"]
    for key in ("seconds_since_progress", "total_cost_usd"):
        if key not in value or isinstance(value[key], bool):
            continue
        try:
            number = float(value[key])
        except (TypeError, ValueError, OverflowError):
            continue
        if number == number and abs(number) != float("inf") and number >= 0:
            safe[key] = number
    statuses = value.get("api_retry_statuses")
    if isinstance(statuses, (list, tuple)):
        safe_statuses = sorted(
            {
                str(item).strip()
                for item in statuses[:20]
                if re.fullmatch(r"^[A-Za-z0-9_.:/+-]{1,32}$", str(item).strip())
            }
        )
        safe["api_retry_statuses"] = safe_statuses
    counts = value.get("tool_call_counts")
    if isinstance(counts, dict):
        safe_counts: dict[str, int] = {}
        for raw_name, raw_count in list(counts.items())[:100]:
            name = str(raw_name or "").strip()
            if not _SAFE_TELEMETRY_NAME.fullmatch(name) or isinstance(raw_count, bool):
                continue
            try:
                safe_counts[name] = max(0, int(raw_count))
            except (TypeError, ValueError, OverflowError):
                continue
        if safe_counts:
            safe["tool_call_counts"] = dict(sorted(safe_counts.items()))
    expected_run_id = str(run_id or "").strip()
    nested_run_id = str(safe.get("run_id") or "").strip()
    if expected_run_id:
        if nested_run_id and nested_run_id != expected_run_id:
            return {}
        safe["run_id"] = expected_run_id
    if safe:
        safe["schema"] = "glasshive.claude-run-telemetry.v1"
    return safe


def _write_private_json(path: Path, value: object) -> bool:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(value, sort_keys=True))
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _claude_effort_help_supports(help_text: str, effort: str) -> bool:
    marker = help_text.find("--effort")
    if marker < 0:
        return False
    if effort != "xhigh":
        return True
    return re.search(r"\bxhigh\b", help_text[marker : marker + 320], flags=re.IGNORECASE) is not None


_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"

_CLAUDE_ACCESS_TOKEN_EXPIRY_BUFFER_MS = 60_000

def _usable_claude_oauth_token(value: object) -> str:
    token = str(value or "").strip()
    if not token or token == "user_provided" or "${" in token:
        return ""
    return token

def _usable_explicit_claude_oauth_token() -> str:
    return _usable_claude_oauth_token(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

def _read_claude_keychain_oauth(*, subprocess_runner=None) -> dict[str, object]:
    if sys.platform != "darwin" or not shutil.which("security"):
        return {}
    runner = subprocess_runner or subprocess.run
    try:
        completed = runner(
            ["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        credential = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    oauth = credential.get("claudeAiOauth") if isinstance(credential, dict) else {}
    return oauth if isinstance(oauth, dict) else {}

def _claude_keychain_access_token_is_fresh(
    oauth: dict[str, object], *, now_ms: int | None = None
) -> bool:
    access_token = str(oauth.get("accessToken") or "").strip()
    if not access_token:
        return False
    try:
        expires_at = float(oauth.get("expiresAt"))
    except (TypeError, ValueError):
        return False
    if not expires_at or not math.isfinite(expires_at):
        return False
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return expires_at > current_ms + _CLAUDE_ACCESS_TOKEN_EXPIRY_BUFFER_MS

def _claude_cli_managed_auth_available(
    binary: str,
    *,
    child_env: dict[str, str] | None,
    subprocess_runner=None,
) -> bool:
    if child_env is None:
        return False
    resolved_binary = str(binary or "").strip()
    if not resolved_binary:
        return False
    if not Path(resolved_binary).is_absolute():
        resolved_binary = str(shutil.which(resolved_binary) or "")
    if not resolved_binary:
        return False
    runner = subprocess_runner or subprocess.run
    try:
        completed = runner(
            [resolved_binary, "auth", "status"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            env=dict(child_env),
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("loggedIn") is True

def _native_cli_status_env() -> dict[str, str]:
    """A status probe needs the CLI owner selectors, never service credentials."""
    names = {"HOME", "CODEX_HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}
    return {name: value for name, value in os.environ.items() if name in names}


def _native_cli_managed_auth_env(env: dict[str, str]) -> dict[str, str]:
    """Select the installed CLI's auth owner without reading its credentials."""
    result = dict(env)
    home = Path(str(os.environ.get("HOME") or ""))
    if not home.is_absolute() or home.is_symlink() or not home.is_dir():
        raise RuntimeErrorBase("Installed native login home is unavailable")
    result["HOME"] = str(home)
    # Native credential storage is independent of the worker session directory.
    # Empty selects the default owner keychain/file store without copying credentials.
    result["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = ""
    for name in (
        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES", "ANTHROPIC_API_KEY",
    ):
        result.pop(name, None)
    return result


def _claude_owner_managed_auth_env() -> dict[str, str]:
    """Return the trusted parent environment used only to verify owner-managed auth."""
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.pop("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", None)
    # This owner-only status probe uses the default owner configuration.
    # Worker launches select credential and session storage independently below.
    env.pop("CLAUDE_CONFIG_DIR", None)
    return env

def _claude_host_auth_available(
    binary: str,
    *,
    child_env: dict[str, str] | None = None,
    subprocess_runner=None,
) -> bool:
    if native_installed_owner_id() is not None:
        probe_env = _native_cli_managed_auth_env(
            child_env or _native_cli_status_env()
        )
        probe_env.pop("CLAUDE_CONFIG_DIR", None)
        return _claude_cli_managed_auth_available(
            binary, child_env=probe_env,
            subprocess_runner=subprocess_runner,
        )
    if child_env and _usable_claude_oauth_token(child_env.get("CLAUDE_CODE_OAUTH_TOKEN")):
        return True
    if _usable_explicit_claude_oauth_token():
        return True
    keychain_oauth = _read_claude_keychain_oauth(
        subprocess_runner=subprocess_runner
    )
    if _claude_keychain_access_token_is_fresh(keychain_oauth):
        return True
    return _claude_cli_managed_auth_available(
        binary,
        child_env=child_env,
        subprocess_runner=subprocess_runner,
    )

def _atomic_write_private_text(path: Path, text: str) -> None:
    """Publish private runtime state without exposing partial cross-process reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        handle = os.fdopen(descriptor, "w")
        descriptor = -1  # fdopen owns the descriptor from this point onward.
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise

def _codex_mcp_section_server_name(section_name: str) -> str | None:
    section_name = section_name.strip()
    if not section_name.startswith("mcp_servers."):
        return None
    server = section_name[len("mcp_servers.") :].split(".", 1)[0].strip()
    return server.strip("\"'") or None


def _codex_mcp_server_names(config_text: str) -> set[str]:
    names: set[str] = set()
    for line in config_text.splitlines():
        match = _CODEX_MCP_SECTION_RE.match(line)
        if not match:
            continue
        server = _codex_mcp_section_server_name(match.group(1))
        if server:
            names.add(server)
    return names


def _select_codex_mcp_server_blocks(config_text: str, names: set[str]) -> str:
    if not config_text.strip() or not names:
        return ""
    output: list[str] = []
    keeping = False
    for line in config_text.splitlines():
        section = _CODEX_MCP_SECTION_RE.match(line)
        if section:
            server = _codex_mcp_section_server_name(section.group(1))
            keeping = server in names if server else False
        if keeping:
            output.append(line)
    return "\n".join(output).strip()


def _strip_codex_mcp_server_blocks(config_text: str, names: set[str]) -> str:
    if not config_text.strip() or not names:
        return config_text.rstrip()
    output: list[str] = []
    skipping = False
    for line in config_text.splitlines():
        section = _CODEX_MCP_SECTION_RE.match(line)
        if section:
            server = _codex_mcp_section_server_name(section.group(1))
            skipping = server in names if server else False
        if not skipping:
            output.append(line)
    return "\n".join(output).rstrip()


def _sanitize_malformed_codex_source_config(
    config_text: str,
    preserve_names: set[str],
    append_names: set[str],
) -> str:
    output: list[str] = []
    keeping = True
    for line in config_text.splitlines():
        section = _CODEX_MCP_SECTION_RE.match(line)
        if section:
            section_name = section.group(1).strip()
            if section_name == "mcp_servers" or section_name.startswith("mcp_servers."):
                server = _codex_mcp_section_server_name(section_name)
                keeping = bool(server and server in preserve_names and server not in append_names)
            else:
                keeping = True
        if keeping:
            output.append(line)
    return "\n".join(output).rstrip()


def _toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _path_entries(value: str) -> list[Path]:
    entries: list[Path] = []
    for raw in str(value or "").split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        entries.append(Path(raw).expanduser())
    return entries


def _append_unique_paths(existing: str, additions: list[Path]) -> str:
    parts = [part for part in str(existing or "").split(os.pathsep) if part]
    seen = set(parts)
    for path in additions:
        value = str(path)
        if value and value not in seen:
            parts.append(value)
            seen.add(value)
    return os.pathsep.join(parts)


def _existing_dirs_from_env(*names: str) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for name in names:
        for path in _path_entries(os.environ.get(name, "")):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            key = str(resolved)
            if key in seen or not path.is_dir():
                continue
            seen.add(key)
            found.append(path)
    return found


def _workspace_dependency_auto_discovery_enabled() -> bool:
    raw = (
        os.environ.get("GLASSHIVE_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "").strip()
        or os.environ.get("WPR_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "").strip()
    )
    return raw.lower() not in _FALSEY_ENV_VALUES


def _codex_workspace_dependency_roots() -> list[Path]:
    roots = _existing_dirs_from_env("GLASSHIVE_CODEX_WORKSPACE_DEPS_ROOT", "WPR_CODEX_WORKSPACE_DEPS_ROOT")
    if _workspace_dependency_auto_discovery_enabled():
        default_root = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies"
        if default_root.is_dir():
            roots.append(default_root)
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _workspace_dependency_paths() -> dict[str, list[Path]]:
    node_modules = _existing_dirs_from_env("GLASSHIVE_WORKSPACE_NODE_MODULES", "WPR_WORKSPACE_NODE_MODULES")
    bin_dirs = _existing_dirs_from_env("GLASSHIVE_WORKSPACE_BIN_DIRS", "WPR_WORKSPACE_BIN_DIRS")
    node_bins = _existing_dirs_from_env("GLASSHIVE_WORKSPACE_NODE_BIN", "WPR_WORKSPACE_NODE_BIN")
    python_bins = _existing_dirs_from_env("GLASSHIVE_WORKSPACE_PYTHON_BIN", "WPR_WORKSPACE_PYTHON_BIN")
    for root in _codex_workspace_dependency_roots():
        candidate_node_modules = root / "node" / "node_modules"
        if candidate_node_modules.is_dir():
            node_modules.append(candidate_node_modules)
        candidate_node_bin = root / "node" / "bin"
        if candidate_node_bin.is_dir():
            node_bins.append(candidate_node_bin)
        candidate_bin = root / "bin"
        if candidate_bin.is_dir():
            bin_dirs.append(candidate_bin)
        candidate_python_bin = root / "python" / "bin"
        if candidate_python_bin.is_dir():
            python_bins.append(candidate_python_bin)

    output: dict[str, list[Path]] = {}
    for key, values in {
        "node_modules": node_modules,
        "node_bins": node_bins,
        "python_bins": python_bins,
        "bin_dirs": bin_dirs,
    }.items():
        deduped: list[Path] = []
        seen: set[str] = set()
        for value in values:
            try:
                resolved = value.resolve()
            except OSError:
                resolved = value
            path_key = str(resolved)
            if path_key in seen or not value.is_dir():
                continue
            seen.add(path_key)
            deduped.append(value)
        output[key] = deduped
    return output


def _project_workspace_dependency_env(env: dict[str, str]) -> None:
    paths = _workspace_dependency_paths()
    executable_dirs = [
        *paths.get("node_bins", []),
        *paths.get("python_bins", []),
        *paths.get("bin_dirs", []),
    ]
    if executable_dirs:
        env["PATH"] = _append_unique_paths(env.get("PATH", ""), executable_dirs)
    node_modules = paths.get("node_modules", [])
    if node_modules:
        env["NODE_PATH"] = _append_unique_paths(env.get("NODE_PATH") or os.environ.get("NODE_PATH", ""), node_modules)
        env["GLASSHIVE_WORKSPACE_NODE_MODULES"] = os.pathsep.join(str(path) for path in node_modules)
    if paths.get("node_bins"):
        env["GLASSHIVE_WORKSPACE_NODE_BIN"] = os.pathsep.join(str(path) for path in paths["node_bins"])
    if paths.get("python_bins"):
        env["GLASSHIVE_WORKSPACE_PYTHON_BIN"] = os.pathsep.join(str(path) for path in paths["python_bins"])
    if paths.get("bin_dirs"):
        env["GLASSHIVE_WORKSPACE_BIN_DIRS"] = os.pathsep.join(str(path) for path in paths["bin_dirs"])


def _toml_value(value: object, *, manifest_dir: Path | None = None, key: str = "") -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        rendered = str(manifest_dir) if key == "cwd" and value == "." and manifest_dir else value
        return _toml_string(rendered)
    if isinstance(value, list):
        rendered_items: list[str] = []
        for item in value:
            item_rendered = _toml_value(item)
            if item_rendered is None:
                return None
            rendered_items.append(item_rendered)
        return "[" + ", ".join(rendered_items) + "]"
    return None


def _toml_table_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _toml_string(name)


_PLUGIN_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*"
)


def _host_plugin_denylist() -> tuple[str, ...]:
    raw = (
        os.environ.get("GLASSHIVE_HOST_PLUGIN_DENYLIST", "").strip()
        or os.environ.get("WPR_HOST_PLUGIN_DENYLIST", "").strip()
    )
    values: list[str] = []
    for item in raw.split(","):
        plugin_id = item.strip()
        if not plugin_id:
            continue
        if not _PLUGIN_ID_RE.fullmatch(plugin_id):
            raise RuntimeErrorBase(
                "Host plugin denylist entries must use the canonical name@marketplace plugin ID"
            )
        if plugin_id not in values:
            values.append(plugin_id)
    return tuple(values)


def _host_codex_personality() -> str:
    personality = os.environ.get("WPR_CODEX_CLI_PERSONALITY", "inherit").strip().lower()
    if personality not in {"inherit", "none", "friendly", "pragmatic"}:
        raise RuntimeErrorBase(
            "Host Codex personality must be inherit, none, friendly, or pragmatic"
        )
    return personality


def _host_codex_conversation_project_instructions() -> str:
    mode = os.environ.get(
        "WPR_CODEX_CLI_CONVERSATION_PROJECT_INSTRUCTIONS",
        "inherit",
    ).strip().lower()
    if mode not in {"inherit", "exclude"}:
        raise RuntimeErrorBase(
            "Host Codex conversation project instructions must be inherit or exclude"
        )
    return mode


def _host_native_web_access() -> str:
    mode = (
        os.environ.get("WPR_HOST_NATIVE_WEB_ACCESS", "").strip()
        or os.environ.get("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "inherit").strip()
    ).lower()
    if mode not in {"inherit", "disabled"}:
        raise RuntimeErrorBase(
            "Host native web access must be inherit or disabled"
        )
    return mode


def _host_codex_personality_policy_state() -> str:
    configured = _host_codex_personality()
    if configured != "inherit":
        return configured
    source_config = Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser() / "config.toml"
    try:
        parsed = tomllib.loads(source_config.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return "inherit"
    inherited = str(parsed.get("personality") or "").strip().lower()
    if inherited in {"none", "friendly", "pragmatic"}:
        return f"inherit:{inherited}"
    return "inherit"


def _render_codex_mcp_server_from_json(name: str, config: object, manifest_dir: Path) -> str:
    if not isinstance(config, dict):
        return ""
    # Reuse the native TOML serializer so nested tool settings survive projection too.
    projected = dict(config)
    if projected.get("cwd") == ".":
        projected["cwd"] = str(manifest_dir)
    return "\n\n".join(_render_toml_table(["mcp_servers", name], projected)).strip()


def _render_toml_document(data: dict[str, object]) -> str:
    root_lines: list[str] = []
    table_blocks: list[str] = []
    for key, value in data.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        if isinstance(value, dict):
            table_blocks.extend(_render_toml_table([key_text], value))
            continue
        rendered = _toml_value(value)
        if rendered is not None:
            root_lines.append(f"{_toml_table_name(key_text)} = {rendered}")
    blocks: list[str] = []
    if root_lines:
        blocks.append("\n".join(root_lines))
    blocks.extend(table_blocks)
    return "\n\n".join(block for block in blocks if block.strip()).strip()


_PLUGIN_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*"
)








def _host_claude_conversation_auto_memory() -> bool | None:
    configured = os.environ.get("WPR_CLAUDE_CODE_CONVERSATION_AUTO_MEMORY", "").strip().lower()
    if not configured:
        return None
    if configured not in {"true", "false"}:
        raise RuntimeErrorBase("Host Claude conversation auto-memory must be true or false")
    return configured == "true"





def _apply_codex_plugin_denylist(config_text: str, plugin_ids: tuple[str, ...]) -> str:
    if not plugin_ids:
        return config_text.strip()
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase(
            "Cannot apply the host plugin denylist because the Codex worker config is invalid"
        ) from exc
    if not isinstance(parsed, dict):
        parsed = {}
    plugins = parsed.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        parsed["plugins"] = plugins
    for plugin_id in plugin_ids:
        existing = plugins.get(plugin_id)
        entry = dict(existing) if isinstance(existing, dict) else {}
        entry["enabled"] = False
        plugins[plugin_id] = entry
    return _render_toml_document(parsed)


def _apply_codex_personality(config_text: str, personality: str) -> str:
    if personality == "inherit":
        return config_text.strip()
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase(
            "Cannot apply the host Codex personality because the worker config is invalid"
        ) from exc
    if not isinstance(parsed, dict):
        parsed = {}
    parsed["personality"] = personality
    return _render_toml_document(parsed)


@worker_prompt_layer_producer("developer_instructions")
def _apply_codex_developer_instructions(
    config_text: str,
    developer_instructions: str | None,
) -> str:
    if developer_instructions is None:
        return config_text.strip()
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase(
            "Cannot apply host Codex developer instructions because the worker config is invalid"
        ) from exc
    if not isinstance(parsed, dict):
        parsed = {}
    if developer_instructions:
        parsed["developer_instructions"] = developer_instructions
    else:
        parsed.pop("developer_instructions", None)
    return _render_toml_document(parsed)


def _assert_codex_worker_policy(
    config_text: str,
    *,
    plugin_ids: tuple[str, ...],
    personality: str,
    developer_instructions: str | None = None,
) -> None:
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase("Host Codex worker policy config is invalid") from exc
    plugins = parsed.get("plugins") if isinstance(parsed, dict) else None
    for plugin_id in plugin_ids:
        entry = plugins.get(plugin_id) if isinstance(plugins, dict) else None
        if not isinstance(entry, dict) or entry.get("enabled") is not False:
            raise RuntimeErrorBase(
                "Host Codex plugin denylist policy was not materialized; refusing to launch"
            )
    if personality != "inherit" and (
        not isinstance(parsed, dict) or parsed.get("personality") != personality
    ):
        raise RuntimeErrorBase(
            "Host Codex personality policy was not materialized; refusing to launch"
        )
    if developer_instructions is not None and str(
        parsed.get("developer_instructions") or ""
    ) != developer_instructions:
        raise RuntimeErrorBase(
            "Host Codex developer instruction authority was not materialized; refusing to launch"
        )


def _render_toml_table(path: list[str], table: dict[str, object]) -> list[str]:
    scalar_lines: list[str] = []
    nested_blocks: list[str] = []
    for key, value in table.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        if isinstance(value, dict):
            nested_blocks.extend(_render_toml_table([*path, key_text], value))
            continue
        rendered = _toml_value(value)
        if rendered is not None:
            scalar_lines.append(f"{_toml_table_name(key_text)} = {rendered}")
    blocks: list[str] = []
    if scalar_lines:
        table_name = ".".join(_toml_table_name(part) for part in path)
        blocks.append("\n".join([f"[{table_name}]", *scalar_lines]))
    blocks.extend(nested_blocks)
    return blocks


def _sanitize_codex_source_config(config_text: str, preserve_names: set[str], append_names: set[str]) -> str:
    if not config_text.strip():
        return ""
    try:
        parsed = tomllib.loads(config_text)
    except Exception:
        return _sanitize_malformed_codex_source_config(config_text, preserve_names, append_names)
    if not isinstance(parsed, dict):
        return ""
    sanitized: dict[str, object] = {
        str(key): value
        for key, value in parsed.items()
        if str(key) != "mcp_servers"
    }
    mcp_servers = parsed.get("mcp_servers")
    if isinstance(mcp_servers, dict):
        kept_servers = {
            str(name): value
            for name, value in mcp_servers.items()
            if str(name) in preserve_names and str(name) not in append_names
        }
        if kept_servers:
            sanitized["mcp_servers"] = kept_servers
    return _render_toml_document(sanitized)










# Keep prompt templates near the top. Host-native workers read these through real files in their
# workspace (`harness-prompt.md`, `AGENTS.md`, `CLAUDE.md`, `CODEX.md`) and through the command-line
# instruction wrapper. The constants live here so future edits do not require spelunking through the
# host runtime implementation.
_COMPLETION_CONTRACT = GLASSHIVE_WORKER_COMPLETION_CONTRACT
HOST_NATIVE_HARNESS_PROMPT = _worker_prompt("worker.host_native_harness", f"""# GlassHive Host-Native Harness

You are running directly on the user's main computer, not inside a sandbox.
You may use the local browser, filesystem, shell, and installed OS tools.
Default execution is no-approval/full-access for this worker class.

{GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS}

Operational requirements:
- Treat the workspace directory as the primary project root.
- Keep `work-log.md` current with concise progress, blockers, and completion notes.
- Write task files inside the workspace by default; use another location when the user's request or applicable prior authorization calls for it. The workspace is a working location, not an additional permission boundary.
- Apply the safety boundary below to the action's effects and existing authorization. A path outside the workspace does not by itself make an action destructive.
- Do not print credentials, tokens, cookies, personal data, or private local paths unless absolutely required for the local operator.
- Respect OS permissions and quarantine. If a helper is blocked, report the required supported approval or setup; do not invoke it through another interpreter or remove security metadata to evade that denial.
- For screen evidence on macOS, the workspace helper `glasshive-host-tools/capture-front-window.sh` is available when authorized and supported.
- For web research or document-generation tasks, prefer `python3 glasshive-host-tools/content-hygiene.py readable <html-file>` before putting page text into structured files, and run `python3 glasshive-host-tools/content-hygiene.py check <csv-or-json-file>...` before final delivery when the output contains sourced research fields.
- If you create research plans, specs, subagent prompts, or delegation notes, carry the user's source/date/auth/scope constraints forward exactly instead of widening, weakening, or rewriting them.
- Keep source publication/evidence dates distinct from retrieval/access timestamps; an access date must not widen or replace a user-limited source window.
- When `glasshive-run/constraint-ledger.json` exists, use its original admitted request, continuation context, and typed authority as input. Interpret the user’s goals and constraints yourself; the runtime does not extract semantic requirements from prose.
- `glasshive-run/` is internal harness evidence, not a user-facing artifact directory.
- For host browser or desktop tasks, first use the user's existing local app/session when the task asks for the main computer, Chrome, browser profile, local files, or installed OS tools. Do not claim host control is unavailable until you have checked the available local shell/desktop/browser automation paths.

{GLASSHIVE_NATIVE_CAPABILITY_INVENTORY}

{GLASSHIVE_WORKER_COMPLETION_CONTRACT}

{GLASSHIVE_SAFETY_CHECKPOINT_RULE}

Required context files in this workspace:
- project-definition.md
- work-log.md
- harness-prompt.md
- AGENTS.md (canonical project instructions for Codex-style workers)
- agents.md (compatibility mirror)
- CLAUDE.md / claude.md (Claude Code compatibility; should import or mirror AGENTS.md when possible)
- CODEX.md / codex.md (legacy compatibility mirror only)
""", variables={
    "critical_operating_instructions": GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    "native_capability_inventory": GLASSHIVE_NATIVE_CAPABILITY_INVENTORY,
    "completion_contract": GLASSHIVE_WORKER_COMPLETION_CONTRACT,
    "safety_checkpoint": GLASSHIVE_SAFETY_CHECKPOINT_RULE,
})
HOST_DEFAULT_AGENTS_MD = (
    "Follow these AGENTS.md project instructions and keep `work-log.md` updated.\n"
    "When the task involves the host browser, desktop, files, shell, or installed apps, operate on the real local machine session unless the project definition explicitly says sandbox.\n"
    "If `glasshive-run/constraint-ledger.json` exists, use it as the canonical run constraint reminder and keep `glasshive-run/` out of user-facing deliverables.\n"
    "For sourced work, keep source publication/evidence dates distinct from retrieval/access timestamps; an access date must not widen or replace a user-limited source window.\n"
    f"{GLASSHIVE_NATIVE_CAPABILITY_INVENTORY}\n"
    "Before `FINAL REPORT:`, inspect the concrete output, files/artifacts, tool results, or visible state you produced; compare it with the user's request and success criteria; then continue, fix, or report the exact blocker.\n"
    "End with `FINAL REPORT:` containing the user-facing result in the user's requested form; mention artifacts only when you intentionally created user-facing files and blockers only when they remain.\n"
)
HOST_DEFAULT_CLAUDE_MD = (
    "Claude host worker context. Treat AGENTS.md as the canonical project instruction source and use bypass permission mode only for this GlassHive workspace.\n"
    "For host browser/desktop tasks, check local automation paths before reporting unavailable. Before `FINAL REPORT:`, inspect the result against the user's request and success criteria. End with `FINAL REPORT:`."
)
HOST_DEFAULT_CODEX_MD = (
    "Codex host worker context. AGENTS.md is the canonical project instruction source; this file is a compatibility mirror.\n"
    "For host browser/desktop tasks, check local automation paths before reporting unavailable. Before `FINAL REPORT:`, inspect the result against the user's request and success criteria. End with `FINAL REPORT:`."
)
HOST_CONTENT_HYGIENE_TOOL = r'''#!/usr/bin/env python3
"""Small, dependency-free helper for research artifact hygiene.

Usage:
  python3 glasshive-host-tools/content-hygiene.py readable page.html [output.txt]
  python3 glasshive-host-tools/content-hygiene.py check file.csv [file.json ...]

The helper is generic on purpose: it strips common page chrome/script noise from HTML and flags
structured cells that still look like navigation, cookie banners, CSS, JavaScript, or raw page dumps.
"""
from __future__ import annotations

import csv
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


BAD_TEXT_RE = re.compile(
    r"skip\s+(?:to\s+)?(?:main\s+)?(?:content|navigation)"
    r"|cookie\s+(?:settings|preferences|policy)"
    r"|privacy\s+policy"
    r"|terms\s+(?:of\s+(?:use|service)|and\s+conditions)"
    r"|all\s+rights\s+reserved"
    r"|(?:read|view)\s+(?:more|all)"
    r"|please\s+enable\s+(?:java\s*)?script|enable\s+js"
    r"|subscribe\s+to\s+continue|sign\s+in\s+to\s+continue|paywall"
    r"|are\s+you\s+(?:a\s+)?robot|captcha|bot\s+wall|403\s+forbidden|access\s+denied"
    r"|lp\s+login|dataroom"
    r"|sourceurl=|__nuxt__|@layer|tailwindcss"
    r"|\b(?:window|document)\.[A-Za-z_$]"
    r"|\bfunction\s+(?:[A-Za-z_$][\w$]*)?\s*\([^)]*\)\s*\{"
    r"|\bvar\s+[A-Za-z_$][\w$]*\s*="
    r"|&(?:nbsp|amp|lt|gt|quot|apos);|&#\d+;|&#x[0-9a-f]+;",
    re.IGNORECASE,
)
CHROME_LINE_RE = re.compile(r"^(?:menu|close|follow us:?|linkedin|facebook|instagram|x)$", re.IGNORECASE)


class ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "template"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "template"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", html.unescape(data)).strip()
        if len(text) >= 2:
            self._chunks.append(text)

    def text(self) -> str:
        lines: list[str] = []
        previous = ""
        for chunk in self._chunks:
            if chunk == previous:
                continue
            previous = chunk
            if CHROME_LINE_RE.fullmatch(chunk.strip()):
                continue
            if BAD_TEXT_RE.search(chunk) and len(chunk) < 90:
                continue
            lines.append(chunk)
        return "\n".join(lines).strip() + ("\n" if lines else "")


def readable(path: Path) -> str:
    parser = ReadableHTMLParser()
    parser.feed(path.read_text(encoding="utf-8", errors="ignore"))
    return parser.text()


def iter_structured_values(path: Path) -> Iterable[tuple[str, int, str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row_index, row in enumerate(csv.DictReader(handle), start=2):
                for field, value in row.items():
                    yield (path.name, row_index, str(field), str(value or ""))
        return
    if suffix in {".json", ".jsonl"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        records = [json.loads(line) for line in text.splitlines() if line.strip()] if suffix == ".jsonl" else [json.loads(text)]
        for row_index, record in enumerate(records, start=1):
            yield from _walk_json(path.name, row_index, "", record)


def _walk_json(name: str, row_index: int, prefix: str, value: object) -> Iterable[tuple[str, int, str, str]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_json(name, row_index, next_prefix, nested)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_json(name, row_index, f"{prefix}[{index}]", nested)
    else:
        yield (name, row_index, prefix, str(value or ""))


def check(paths: list[Path]) -> int:
    failures: list[dict[str, object]] = []
    for path in paths:
        for name, row_index, field, value in iter_structured_values(path):
            text = " ".join(value.split())
            if not text:
                continue
            match = BAD_TEXT_RE.search(text)
            if match:
                failures.append(
                    {
                        "file": name,
                        "row": row_index,
                        "field": field,
                        "match": match.group(0),
                        "sample": text[:180],
                    }
                )
            elif len(text) > 900 and not re.search(r"(source|evidence|notes|summary|description|rationale)", field, re.I):
                failures.append(
                    {
                        "file": name,
                        "row": row_index,
                        "field": field,
                        "match": "overlong structured cell",
                        "sample": text[:180],
                    }
                )
    print(json.dumps({"ok": not failures, "failures": failures[:50], "failure_count": len(failures)}, indent=2))
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in {"readable", "check"}:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    command = argv[1]
    paths = [Path(arg) for arg in argv[2:]]
    if command == "readable":
        text = readable(paths[0])
        if len(paths) > 1:
            paths[1].write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    return check(paths)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
'''


@worker_prompt_layer_producer("run_instruction")
def _instruction_with_completion_contract(instruction: str) -> str:
    body = str(instruction or "").strip()
    return f"{body}\n\n{_COMPLETION_CONTRACT}" if body else _COMPLETION_CONTRACT


def _instruction_file_pointer_message(path: str) -> str:
    return "\n".join(
        [
            "Read the full GlassHive task instruction from this local file and follow it exactly:",
            path,
            "",
            "The file contains the user's task and the GlassHive completion contract.",
            "If the file is unavailable, report a clear runtime setup failure instead of guessing.",
        ]
    )


class ProfiledWorkerRuntime:
    def __init__(
        self,
        base_dir: str | None = None,
        provider_account_db_path: str | None = None,
        *, create_directories: bool = True,
        provider_account_owner_root_resolver: Callable[[str, str], Path] | None = None,
        provider_account_native_launcher: GuardedAccountLauncher | None = None,
    ) -> None:
        self.openclaw = OpenClawWorkstationRuntime(base_dir=base_dir, create_directories=create_directories)
        self.codex = CodexCliRuntime(base_dir=base_dir, create_directories=create_directories)
        self.claude = ClaudeCodeRuntime(base_dir=base_dir, create_directories=create_directories)
        self.host_openclaw = HostOpenClawRuntime(base_dir=base_dir, create_directories=create_directories)
        self.host_codex = HostCodexCliRuntime(base_dir=base_dir, create_directories=create_directories)
        self.host_claude = HostClaudeCodeRuntime(base_dir=base_dir, create_directories=create_directories)
        from .grok_runtime import GrokBuildRuntime, HostGrokBuildRuntime
        self.grok = GrokBuildRuntime(base_dir=base_dir, create_directories=create_directories)
        self.host_grok = HostGrokBuildRuntime(base_dir=base_dir, create_directories=create_directories)
        self._provider_log_cache: dict[tuple[str, str, str], dict[str, object]] = {}
        self._provider_log_cache_lock = Lock()
        provider_home_root = Path(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT")
            or (self.codex.base_dir / "provider_accounts")
        ).expanduser()
        self.provider_account_binder = MissionProviderAccountBinder(
            db_path=provider_account_db_path,
            home_root=provider_home_root,
            owner_root_resolver=provider_account_owner_root_resolver,
            native_launcher=provider_account_native_launcher,
        )
        self.inference_broker = GlassHiveInferenceBroker.from_environment()
        self.capability_broker = GlassHiveCapabilityBroker.from_environment()
        self._isolated_readiness_cache: tuple[float, dict[str, object]] | None = None
        self._isolated_readiness_lock = Lock()

    def _runtime_for_profile(self, profile: str, execution_mode: str = "docker") -> WorkerRuntime:
        descriptor = require_worker_profile(profile)
        runtime = getattr(self, descriptor.runtime_for(execution_mode), None)
        if runtime is None:
            raise UnsupportedWorkerProfileError("Worker profile adapter is not installed")
        return runtime

    def native_control(self, worker: dict, **kwargs):
        runtime = self._runtime_for_worker(worker)
        handler = getattr(runtime, "native_control", None)
        if not callable(handler):
            raise RuntimeErrorBase("This runtime does not support native controls")
        return handler(worker, **kwargs)

    def native_control_state(self, worker: dict, **kwargs):
        runtime = self._runtime_for_worker(worker)
        handler = getattr(runtime, "native_control_state", None)
        if not callable(handler):
            raise RuntimeErrorBase("This runtime does not expose native control requests")
        return handler(worker, **kwargs)

    def shared_workspace_readiness(self, workspace: dict) -> dict:
        shared = getattr(self, "_shared_workspace_runtimes", None)
        if shared is None:
            return {"available": False, "code": "shared_runtime_unavailable"}
        return shared.readiness(workspace, runtime=self)

    def shared_workspace_profile_support(self, profile: str) -> bool:
        """Return adapter registration for the supported shared native profiles.

        OpenClaw has a registered adapter, but no shared-workspace adapter.  Runtime
        readiness remains a separate workspace-level check.
        """
        if str(profile or "").strip() not in {"codex-cli", "claude-code", "grok-build"}:
            return False
        if getattr(self, "_shared_workspace_runtimes", None) is None:
            return False
        try:
            return self._runtime_for_profile(str(profile).strip(), "docker") is not None
        except Exception:
            return False

    def native_workspace_placement(self, worker: dict) -> dict | None:
        runtime = self._runtime_for_worker(worker)
        sandbox = getattr(runtime, "sandbox", None)
        box = getattr(sandbox, "box", None)
        if box is None:
            return None
        workspace = self._shared_workspace_runtimes.store.get_execution_workspace(
            worker["workspace_id"], worker["tenant_id"], worker["owner_id"])
        paths = box.paths()
        return {"mode": workspace["mode"], "file_placement": box.file_placement,
                "workspace_id": box.binding.workspace_id, "member_id": box.binding.worker_id,
                "home_dir": str(paths["home_dir"]), "native_home": sandbox.home_mount,
                "workspace_dir": str(paths["workspace_dir"]), "native_workspace": sandbox.workspace_mount,
                "private_project_dir": str(box.member_root / "worktree"),
                "native_private_project": f"/workspace/data/members/{box.binding.uid}/worktree"}

    def configure_owner_storage(self, storage) -> None:
        binder = self.provider_account_binder
        if getattr(binder, "_homes", None) is not None:
            raise RuntimeErrorBase("Configure owner storage before provider account homes are initialized")
        binder.owner_root_resolver = storage.owner_root
        self._owner_storage = storage
        storage.account_coverage = lambda tenant, owner, snapshot: storage.provider_account_coverage(
            binder, tenant, owner, snapshot)

    def configure_execution_workspaces(self, store) -> None:
        from .workspace_runtime import SharedWorkspaceRuntimes
        from .execution_profile import packaged_linux
        if packaged_linux() and self.provider_account_binder.native_launcher is None:
            from .packaged_linux import account_launcher_from_environment
            self.provider_account_binder.native_launcher = account_launcher_from_environment()
        self._shared_workspace_runtimes = SharedWorkspaceRuntimes(store, self.provider_account_binder, getattr(self, "_owner_storage", None))
        self._shared_workspace_runtimes.recover_pending(self)

    def provider_projection_recovery_members(self) -> list[str]:
        """Members whose account projection startup recovery could not finish."""
        runtimes = getattr(self, "_shared_workspace_runtimes", None)
        return runtimes.recovery_members() if runtimes is not None else []

    def settle_member_provider_projections(self, worker: dict) -> float | None:
        """Finish a member's projections left by an ended run; returns when to retry."""
        runtimes = getattr(self, "_shared_workspace_runtimes", None)
        if runtimes is None:
            return None
        return runtimes.settle_member_projections(worker, self)

    def recover_quarantined_provider_projections(self, *, account_id: str) -> list[str]:
        """Settle one account's quarantined projections on an owner's verify/reconnect."""
        runtimes = getattr(self, "_shared_workspace_runtimes", None)
        if runtimes is None:
            return []
        return runtimes.recover_quarantined(self, account_id=account_id)

    def configure_allowed_ai_policy(self, policy_service) -> None:
        """Connect the owner policy to the final native binder boundary."""

        checker = getattr(policy_service, "native_start_fence", None)
        self.provider_account_binder.configure_allowed_ai_start(
            (lambda worker, receipt: checker(worker, receipt=receipt))
            if callable(checker)
            else None
        )

    def _runtime_for_worker(self, worker: dict) -> WorkerRuntime:
        runtime = self._runtime_for_profile(
            str(worker.get("profile") or ""),
            str(worker.get("execution_mode") or "docker"),
        )
        shared = getattr(self, "_shared_workspace_runtimes", None)
        return shared.for_worker(worker, runtime) if shared is not None else runtime

    def resolve_model(self, profile: str, execution_mode: str = "docker") -> str:
        return self._runtime_for_profile(profile, execution_mode).resolve_model(profile)

    def preflight_worker_profile(self, profile: str, execution_mode: str = "docker") -> None:
        runtime = self._runtime_for_profile(profile, execution_mode)
        if hasattr(runtime, "preflight_worker_profile"):
            runtime.preflight_worker_profile(profile, execution_mode)

    def reconcile_provider_account_binding(self, account_home: Path) -> None:
        """Seal account state using the active execution substrate."""
        from .execution_profile import packaged_linux
        if packaged_linux():
            # Packaged workers receive credentials through the fenced native
            # projection. Their account home is inside a named Docker volume,
            # not a host bind source for the legacy workstation sandbox.
            homes = self.provider_account_binder.homes
            homes.assert_native_quiescent(account_home)
            if account_home.exists() or account_home.is_symlink():
                homes.tighten_permissions(account_home=account_home)
            return
        self.codex.reconcile_provider_account_binding(account_home)

    def recover_interactive_provider_sessions(self) -> None:
        """Remove credential-bearing setup containers orphaned by a service restart."""

        store = self.provider_account_binder.store
        if store is None:
            return
        from .execution_profile import packaged_linux
        if packaged_linux():
            from .packaged_linux import recover_account_launches
            recover_account_launches(self.provider_account_binder)
            return
        homes = self.provider_account_binder.homes
        supported = {
            "codex-cli": {"codex", "openai"},
            "claude-code": {"claude", "anthropic"},
            "grok-build": {"grok", "xai"},
        }
        for lease in store.unreleased_interactive_provider_leases():
            lane = str(lease.get("lane") or "")
            profile = lane.removesuffix(":interactive")
            provider = store.get_provider_account(
                account_id=str(lease.get("account_id") or ""),
                tenant_id=str(lease.get("tenant_id") or ""),
                owner_id=str(lease.get("owner_id") or ""),
            )
            if (
                profile not in supported
                or provider is None
                or str(provider.get("provider") or "").strip().lower()
                not in supported[profile]
            ):
                raise RuntimeErrorBase(
                    "Interactive provider session recovery has inconsistent account metadata"
                )
            account_home = homes.account_home_path(
                tenant_id=str(lease.get("tenant_id") or ""),
                owner_id=str(lease.get("owner_id") or ""),
                account_id=str(lease.get("account_id") or ""),
            )
            runtime = self._runtime_for_profile(profile, "docker")
            runtime.release_provider_account_binding(
                {
                    "worker_id": str(lease.get("worker_id") or ""),
                    "tenant_id": str(lease.get("tenant_id") or ""),
                    "owner_id": str(lease.get("owner_id") or ""),
                    "profile": profile,
                    "execution_mode": "docker",
                    "_active_run_id": str(lease.get("run_id") or ""),
                    "_glasshive_provider_account_mount_host": str(account_home),
                }
            )
            store.release_provider_lease(
                lease_id=str(lease.get("lease_id") or ""),
                tenant_id=str(lease.get("tenant_id") or ""),
                owner_id=str(lease.get("owner_id") or ""),
            )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).ensure_worker_ready(worker)

    def prepare_worker_workspace(self, worker: dict) -> RuntimeInfo:
        runtime = self._runtime_for_worker(worker)
        prepare = getattr(runtime, "prepare_worker_workspace", None)
        if not callable(prepare):
            raise RuntimeErrorBase("The selected worker runtime cannot prepare a workspace without starting compute")
        return prepare(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        try:
            return self._runtime_for_worker(worker).pause_worker(worker)
        finally:
            try:
                self._revoke_active_capability_grant(worker)
            finally:
                self._revoke_active_inference_grant(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        runtime = self._runtime_for_worker(worker)
        try:
            return runtime.terminate_worker(worker)
        finally:
            try:
                self._revoke_active_capability_grant(worker)
            finally:
                self._revoke_active_inference_grant(worker)

    def detach_worker(self, worker: dict, run_id: str | None = None) -> bool:
        """End only this process's wait on the exact live generation; never stop it."""
        runtime = self._runtime_for_worker(worker)
        detach = getattr(runtime, "detach_worker", None)
        if not callable(detach):
            return False
        return bool(detach(worker, run_id=run_id))

    def compute_identity(self, worker: dict) -> dict[str, str]:
        runtime = self._runtime_for_worker(worker)
        if str(worker.get("execution_mode") or "docker") != "docker":
            return {"container_id": ""}
        sandbox = getattr(runtime, "sandbox", None)
        if sandbox is None:
            return {"container_id": ""}
        worker_id = str(worker.get("worker_id") or "")
        inspect_fresh = getattr(sandbox, "inspect_fresh", None)
        if callable(inspect_fresh):
            inspection = inspect_fresh(worker_id)
            status = str(getattr(inspection, "status", "") or "")
            if status == "unavailable":
                raise RuntimeErrorBase(
                    "The exact Docker generation could not be inspected"
                )
            inspected = (
                getattr(inspection, "sandbox", None)
                if status == "present"
                else None
            )
        else:
            inspected = sandbox.inspect(worker_id)
        return {
            "container_id": str(
                getattr(inspected, "container_id", None) if inspected else ""
            ).strip()
        }

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        runtime = self._runtime_for_worker(worker)
        try:
            if hasattr(runtime, "interrupt_worker"):
                try:
                    return runtime.interrupt_worker(worker, run_id=run_id)
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    return runtime.interrupt_worker(worker)
            return runtime.pause_worker(worker)
        finally:
            try:
                self._revoke_active_capability_grant(worker, run_id=run_id)
            finally:
                self._revoke_active_inference_grant(worker, run_id=run_id)

    def _revoke_active_capability_grant(
        self,
        worker: dict,
        *,
        run_id: str | None = None,
    ) -> None:
        self.capability_broker.revoke_active(
            tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
            owner_id=str(worker.get("owner_id") or "").strip(),
            worker_id=str(worker.get("worker_id") or "").strip(),
            run_id=str(run_id).strip() if run_id else None,
        )

    def _revoke_active_inference_grant(
        self,
        worker: dict,
        *,
        run_id: str | None = None,
    ) -> None:
        self.inference_broker.revoke_active(
            tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
            owner_id=str(worker.get("owner_id") or "").strip(),
            worker_id=str(worker.get("worker_id") or "").strip(),
            run_id=str(run_id).strip() if run_id else None,
        )

    def _run_observed_provider_account_task(
        self,
        *,
        runtime: WorkerRuntime,
        worker: dict,
        instruction: str,
        timeout_sec: float | None,
        run_id: str,
        account_id: str,
        selection: MissionProviderAccountSelection,
        reconnect_personal_account: bool,
    ) -> str:
        """Run with a selected account and persist only telemetry the harness observed."""

        started_at = time.monotonic()
        succeeded = False
        try:
            # The binder needs the active run identity while it authorizes the
            # native start. The runtime receives the run ID explicitly and
            # creates its own transient task worker, so do not leak this
            # binder-only marker into the provider call.
            runtime_worker = dict(worker)
            runtime_worker.pop("_active_run_id", None)
            result = runtime.run_task(
                runtime_worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=run_id,
            )
            succeeded = True
            return result
        except RuntimeErrorBase as exc:
            classification = getattr(exc, "failure_classification", None)
            if (
                reconnect_personal_account
                and isinstance(classification, FailureClassification)
                and classification.personal_account_reconnect
            ):
                personal_reconnect = self._personal_account_reconnect_failure(
                    runtime=runtime,
                    worker=worker,
                )
                exc.failure_classification = personal_reconnect  # type: ignore[attr-defined]
                try:
                    self.provider_account_binder.update_selected_account_status(
                        worker,
                        selection,
                        status="action_required",
                        reconnect_reason=(
                            "Connected AI account sign-in expired. Reconnect this account in Connections."
                        ),
                    )
                except Exception:
                    logger.warning(
                        "Could not mark an expired connected AI account for reconnection",
                        exc_info=True,
                        extra={"worker_id": worker.get("worker_id"), "run_id": run_id},
                    )
            raise
        finally:
            usage: dict[str, object] = {}
            reader = getattr(runtime, "run_usage", None)
            if callable(reader):
                try:
                    reported = reader(worker, run_id)
                    if isinstance(reported, dict):
                        usage = reported
                except Exception:
                    logger.warning(
                        "Could not read worker-reported provider account usage",
                        exc_info=True,
                        extra={"worker_id": worker.get("worker_id"), "run_id": run_id},
                    )

            def reported_token(name: str) -> int | None:
                value = usage.get(name)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    return None
                return value

            store = self.provider_account_binder.store
            if store is not None:
                try:
                    store.record_provider_account_usage(
                        account_id=account_id,
                        tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
                        owner_id=str(worker.get("owner_id") or "").strip(),
                        succeeded=succeeded,
                        duration_seconds=max(0.0, time.monotonic() - started_at),
                        input_tokens=reported_token("input_tokens"),
                        output_tokens=reported_token("output_tokens"),
                    )
                except Exception:
                    # Usage accounting must not replace the worker's result or original failure.
                    logger.warning(
                        "Could not persist GlassHive-observed provider account usage",
                        exc_info=True,
                        extra={"worker_id": worker.get("worker_id"), "run_id": run_id},
                    )

    @staticmethod
    def _provider_runtime_worker(worker: dict) -> dict:
        """Strip binder-only run identity before invoking the provider runtime."""

        runtime_worker = dict(worker)
        runtime_worker.pop("_active_run_id", None)
        return runtime_worker

    @staticmethod
    def _personal_account_reconnect_failure(
        *,
        runtime: WorkerRuntime,
        worker: dict,
    ) -> FailureClassification:
        runtime_name = str(
            getattr(runtime, "runtime_name", "")
            or worker.get("profile")
            or "worker"
        ).strip()
        return FailureClassification(
            failure_class="provider_auth_missing",
            retryable=False,
            user_message="The selected connected AI account must be reconnected.",
            recommended_recovery=(
                "Open Connections, reconnect the selected account, then continue the same workspace."
            ),
            diagnostic_summary=f"class=provider_auth_missing; runtime={runtime_name}",
        )

    def _personalize_completed_provider_auth_failure(
        self,
        *,
        runtime: WorkerRuntime,
        worker: dict,
        recovered: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if not recovered:
            return recovered
        candidate = dict(recovered)
        reconnect_required = bool(candidate.pop(_PERSONAL_ACCOUNT_RECONNECT_MARKER, False))
        if not reconnect_required:
            return candidate
        try:
            selection = mission_provider_account_selection(worker)
            if selection is None or not selection.account_id:
                return candidate
            account = self.provider_account_binder.selected_account_record(worker, selection)
            if (
                account is None
                or str(account.get("auth_method") or "").strip().lower()
                != "subscription"
            ):
                return candidate
            self.provider_account_binder.update_selected_account_status(
                worker,
                selection,
                status="action_required",
                reconnect_reason=(
                    "Connected AI account sign-in expired. Reconnect this account in Connections."
                ),
            )
        except Exception:
            logger.warning(
                "Could not mark a recovered expired connected AI account for reconnection",
                exc_info=True,
                extra={"worker_id": worker.get("worker_id")},
            )
            return candidate
        personal = self._personal_account_reconnect_failure(
            runtime=runtime,
            worker=worker,
        )
        return {
            **candidate,
            "error_text": personal.user_message,
            **personal.as_store_fields(),
        }

    def _run_unbound_selected_account_route(
        self,
        *,
        runtime: WorkerRuntime,
        worker: dict,
        run_id: str,
        account_id: str,
        execute: Callable[[dict], str],
    ) -> str:
        """Expose a broker/fallback route only after its initial substrate is ready."""

        worker = {
            **worker,
            "_active_run_id": str(run_id or worker.get("_active_run_id") or "").strip(),
        }

        runtime_name = str(
            getattr(runtime, "runtime_name", "") or worker.get("profile") or ""
        ).strip()
        with self.provider_account_binder.bind_unbound_route(
            worker,
            runtime_name=runtime_name,
            run_id=run_id,
            account_id=account_id,
        ) as routed_worker:
            runtime.ensure_worker_ready(routed_worker)
            self.provider_account_binder.mark_active_route_ready(
                routed_worker,
                runtime_name=runtime_name,
                run_id=run_id,
            )
            bundle = bootstrap_bundle_for(routed_worker)
            fallback = bool(
                routed_worker.get("_glasshive_provider_account_preferred_fallback")
            )
            receipt = self.provider_account_binder.native_connection_receipt(
                routed_worker,
                runtime_name=runtime_name,
                run_id=run_id,
                route_kind="deployment_fallback" if fallback else "broker",
                connection_id=(
                    str(
                        bundle.get("configured_route_id")
                        or bundle.get("route_id")
                        or bundle.get("connection_id")
                        or ""
                    ).strip()
                    if fallback
                    else account_id
                ),
            )
            self.provider_account_binder.authorize_native_start(
                routed_worker, receipt=receipt
            )
            return execute(routed_worker)

    def _run_with_inference_broker(
        self,
        *,
        runtime: WorkerRuntime,
        worker: dict,
        instruction: str,
        timeout_sec: float | None,
        run_id: str,
        account: dict,
        selection,
        preferred: bool,
    ) -> str:
        worker = {
            **worker,
            "_active_run_id": str(run_id or worker.get("_active_run_id") or "").strip(),
        }
        runtime_name = str(
            getattr(runtime, "runtime_name", "") or worker.get("profile") or ""
        ).strip()
        provider = str(account.get("provider") or "").strip().lower()
        status = str(account.get("status") or "").strip().lower()
        if runtime_name != "codex-cli" or provider not in {"codex", "openai"}:
            raise RuntimeErrorBase(
                "Selected provider account does not match this worker profile"
            )
        if status not in {"ready", "action_required", "unavailable", "error"}:
            if preferred:
                readiness, _status = deployment_provider_readiness(runtime_name)
                if readiness != "deployment_managed":
                    raise RuntimeErrorBase(
                        "Work AI is not set up for this workspace. Reconnect the personal account "
                        "or ask an administrator to finish provider setup."
                    )
                fallback_worker = {
                    **worker,
                    "_glasshive_provider_account_preferred_fallback": True,
                }
                return self._run_unbound_selected_account_route(
                    runtime=runtime,
                    worker=fallback_worker,
                    run_id=run_id,
                    account_id=selection.account_id,
                    execute=lambda routed_worker: runtime.run_task(
                        self._provider_runtime_worker(routed_worker),
                        instruction,
                        timeout_sec=timeout_sec,
                        run_id=run_id,
                    ),
                )
            raise RuntimeErrorBase(
                "Selected OpenAI connection is not ready; reconnect or verify it before running"
            )
        model = str(
            worker.get("model")
            or runtime.resolve_model(str(worker.get("profile") or "codex-cli"))
            or ""
        ).strip()

        def execute_reserved_route(routed_worker: dict) -> str:
            mission_dispatched = False
            try:
                with self.inference_broker.bind_run(
                    tenant_id=str(worker.get("tenant_id") or "local").strip()
                    or "local",
                    owner_id=str(worker.get("owner_id") or "").strip(),
                    worker_id=str(worker.get("worker_id") or "").strip(),
                    run_id=run_id,
                    auth_method=str(account.get("auth_method") or "").strip(),
                    models=[model],
                ) as projection:
                    if status != "ready":
                        self.provider_account_binder.update_selected_account_status(
                            worker,
                            selection,
                            status="ready",
                        )
                    bound_worker = {
                        **routed_worker,
                        "model": model,
                        "_glasshive_inference_broker_bound": True,
                        "_glasshive_inference_broker": projection,
                    }
                    self.provider_account_binder.mark_active_route_ready(
                        bound_worker,
                        runtime_name=runtime_name,
                        run_id=run_id,
                    )
                    receipt = self.provider_account_binder.native_connection_receipt(
                        bound_worker,
                        runtime_name=runtime_name,
                        run_id=run_id,
                        route_kind="broker",
                        connection_id=str(
                            account.get("account_id") or selection.account_id
                        ),
                    )
                    self.provider_account_binder.authorize_native_start(
                        bound_worker, receipt=receipt
                    )
                    mission_dispatched = True
                    return self._run_observed_provider_account_task(
                        runtime=runtime,
                        worker=bound_worker,
                        instruction=instruction,
                        timeout_sec=timeout_sec,
                        run_id=run_id,
                        account_id=str(
                            account.get("account_id") or selection.account_id
                        ),
                        selection=selection,
                        reconnect_personal_account=False,
                    )
            except InferenceBrokerError as exc:
                unavailable_codes = {
                    "broker_unavailable",
                    "enterprise_route_unavailable",
                    "proxy_route_unavailable",
                }
                self.provider_account_binder.update_selected_account_status(
                    worker,
                    selection,
                    status=(
                        "unavailable"
                        if exc.code in unavailable_codes
                        else "action_required"
                    ),
                    reconnect_reason=str(exc),
                )
                # A broker cleanup failure can be raised while leaving the context after the
                # worker already completed. Never run the user's mission twice as a fallback.
                if mission_dispatched or not preferred:
                    raise
                readiness, _status = deployment_provider_readiness(runtime_name)
                if readiness != "deployment_managed":
                    raise RuntimeErrorBase(
                        "Work AI is not set up for this workspace. Reconnect the personal account "
                        "or ask an administrator to finish provider setup."
                    ) from exc
                fallback_worker = {
                    **routed_worker,
                    "_glasshive_provider_account_preferred_fallback": True,
                }
                self.provider_account_binder.mark_active_route_ready(
                    fallback_worker,
                    runtime_name=runtime_name,
                    run_id=run_id,
                )
                fallback_bundle = bootstrap_bundle_for(fallback_worker)
                fallback_receipt = self.provider_account_binder.native_connection_receipt(
                    fallback_worker,
                    runtime_name=runtime_name,
                    run_id=run_id,
                    route_kind="deployment_fallback",
                    connection_id=str(
                        fallback_bundle.get("configured_route_id")
                        or fallback_bundle.get("route_id")
                        or fallback_bundle.get("connection_id")
                        or ""
                    ).strip(),
                )
                self.provider_account_binder.authorize_native_start(
                    fallback_worker, receipt=fallback_receipt
                )
                return runtime.run_task(
                    self._provider_runtime_worker(fallback_worker),
                    instruction,
                    timeout_sec=timeout_sec,
                    run_id=run_id,
                )

        return self._run_unbound_selected_account_route(
            runtime=runtime,
            worker=worker,
            run_id=run_id,
            account_id=selection.account_id,
            execute=execute_reserved_route,
        )

    def _run_task_with_provider_account(
        self,
        worker: dict,
        instruction: str,
        *,
        timeout_sec: float | None,
        run_id: str,
    ) -> str:
        worker = {
            **worker,
            "_active_run_id": str(run_id or worker.get("_active_run_id") or "").strip(),
        }
        worker = enforce_context_tools(worker)
        instruction = contextual_instruction(instruction, worker)
        runtime = self._runtime_for_worker(worker)
        selection = mission_provider_account_selection(worker)
        if selection is None:
            bundle = bootstrap_bundle_for(worker)
            if (
                str(bundle.get("run_mode") or "").strip().lower() == "conversation"
                and str(bundle.get("connection_id") or "").strip()
            ):
                raise RuntimeErrorBase(
                    "Selected foreground connection has no validated native account binding"
                )
            receipt = self.provider_account_binder.native_connection_receipt(
                worker,
                runtime_name=str(getattr(runtime, "runtime_name", "") or worker.get("profile") or ""),
                run_id=run_id,
                route_kind="configured_route",
                connection_id=str(
                    bootstrap_bundle_for(worker).get("configured_route_id")
                    or bootstrap_bundle_for(worker).get("route_id")
                    or bootstrap_bundle_for(worker).get("connection_id")
                    or ""
                ).strip(),
            )
            self.provider_account_binder.authorize_native_start(worker, receipt=receipt)
            return runtime.run_task(
                worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=run_id,
            )
        effective_run_id = run_id
        runtime_name = str(
            getattr(runtime, "runtime_name", "")
            or worker.get("profile")
            or ""
        ).strip()
        account = self.provider_account_binder.selected_account_record(worker, selection)
        from .native_api_keys import native_key_account
        if account is not None and account.get("auth_method") in {"api_key", "enterprise_route"} and not native_key_account(account):
            return self._run_with_inference_broker(
                runtime=runtime,
                worker=worker,
                instruction=instruction,
                timeout_sec=timeout_sec,
                run_id=effective_run_id,
                account=account,
                selection=selection,
                preferred=selection.policy == "personal_preferred",
            )
        projection_factory = getattr(getattr(runtime, "sandbox", None), "projection_factory", None)
        projection_options = ({"projection_factory": projection_factory,
                               "attempt_id": str(worker.get("_run_attempt_id") or "")}
                              if projection_factory is not None else {})
        with self.provider_account_binder.bind(
            worker,
            **projection_options,
            runtime_name=runtime_name,
            run_id=effective_run_id,
            timeout_sec=timeout_sec,
            release_binding=(
                getattr(runtime, "release_provider_account_binding", None)
                if str(worker.get("execution_mode") or "docker").strip().lower() == "docker"
                else None
            ),
            abort_binding=lambda bound_worker: runtime.terminate_worker(bound_worker),
            reconcile_binding=(
                getattr(runtime, "reconcile_provider_account_binding", None)
                if str(worker.get("execution_mode") or "docker").strip().lower() == "docker"
                else None
            ),
        ) as bound_worker:
            from .workspace_projection import native_binding
            with native_binding({**bound_worker, "_active_run_id": effective_run_id}):
                if bound_worker.get("_glasshive_provider_account_bound"):
                    bound_task_worker = {
                        **bound_worker,
                        "_active_run_id": effective_run_id,
                        "_glasshive_task_run": True,
                    }
                    runtime.ensure_worker_ready(bound_task_worker)
                    self.provider_account_binder.mark_active_route_ready(
                        bound_worker,
                        runtime_name=runtime_name,
                        run_id=effective_run_id,
                    )
                    receipt = self.provider_account_binder.native_connection_receipt(
                        bound_worker,
                        runtime_name=runtime_name,
                        run_id=effective_run_id,
                        connection_id=selection.account_id,
                    )
                    self.provider_account_binder.authorize_native_start(
                        bound_worker, receipt=receipt
                    )
                    return self._run_observed_provider_account_task(
                        runtime=runtime,
                        worker=bound_worker,
                        instruction=instruction,
                        timeout_sec=timeout_sec,
                        run_id=effective_run_id,
                        account_id=selection.account_id,
                        selection=selection,
                        reconnect_personal_account=True,
                    )
                return self._run_unbound_selected_account_route(
                    runtime=runtime,
                    worker=bound_worker,
                    run_id=effective_run_id,
                    account_id=selection.account_id,
                    execute=lambda routed_worker: runtime.run_task(
                        self._provider_runtime_worker(routed_worker),
                        instruction,
                        timeout_sec=timeout_sec,
                        run_id=effective_run_id,
                    ),
                )

    def _run_mode_from_worker(self, worker: dict) -> str:
        raw_bundle = worker.get("bootstrap_bundle_json")
        if isinstance(raw_bundle, str) and raw_bundle.strip():
            try:
                parsed = json.loads(raw_bundle)
            except json.JSONDecodeError:
                parsed = {}
        else:
            candidate = worker.get("bootstrap_bundle")
            parsed = candidate if isinstance(candidate, dict) else {}
        if not isinstance(parsed, dict):
            return "mission"
        return str(parsed.get("run_mode") or "mission").strip().lower()

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        """Bind fresh connected capabilities for every UI, scheduled, or MCP mission.

        The grant bundle exists only in this call stack. It is never written back to the worker,
        run database, public API response, or reusable workspace record. Direct Conversation
        workers already carry their signed, request-scoped broker bundle from LibreChat and must
        not be wrapped by the standalone mission issuer a second time.
        """

        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        if self._run_mode_from_worker(worker) == "conversation":
            return self._run_task_with_provider_account(
                worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=effective_run_id,
            )
        tenant_id = str(worker.get("tenant_id") or "local").strip() or "local"
        owner_id = str(worker.get("owner_id") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        execution_mode = str(worker.get("execution_mode") or "docker").strip().lower()
        with self.capability_broker.bind_run(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
            run_id=effective_run_id,
            execution_mode=execution_mode,
        ) as (bundle, _readiness):
            projected_worker = (
                worker_with_ephemeral_capability_bundle(worker, bundle)
                if bundle
                else worker
            )
            return self._run_task_with_provider_account(
                projected_worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=effective_run_id,
            )

    def run_usage(self, worker: dict, run_id: str) -> dict[str, int]:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "run_usage", None)
        return dict(reader(worker, run_id)) if callable(reader) else {}

    def run_telemetry(self, worker: dict, run_id: str) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "run_telemetry", None)
        return dict(reader(worker, run_id)) if callable(reader) else {}

    def live_telemetry(
        self,
        worker: dict,
        stdout: str,
        run_id: str | None = None,
    ) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "live_telemetry", None)
        if not callable(reader):
            return {}
        try:
            return dict(reader(worker, stdout, run_id=run_id))
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            if run_id:
                return {
                    "schema": "glasshive.claude-run-telemetry.v1",
                    "run_id": str(run_id),
                    "telemetry_scope": "active_run_unavailable",
                }
            return dict(reader(worker, stdout))

    def native_provider_authority_receipt(
        self, worker: dict, run_id: str
    ) -> dict[str, object] | None:
        reader = getattr(
            self._runtime_for_worker(worker),
            "native_provider_authority_receipt",
            None,
        )
        if not callable(reader):
            return None
        return reader(worker, run_id)

    def consume_provider_route_failure_evidence(
        self,
        worker: dict,
        run: dict,
        source: object,
    ) -> dict[str, object] | None:
        consumer = getattr(
            self._runtime_for_worker(worker),
            "consume_provider_route_failure_evidence",
            None,
        )
        if not callable(consumer):
            return None
        evidence = consumer(worker, run, source)
        return dict(evidence) if isinstance(evidence, dict) else None

    def clear_run_local_capability_grant(self, worker: dict) -> None:
        cleaner = getattr(
            self._runtime_for_worker(worker),
            "clear_run_local_capability_grant",
            None,
        )
        if callable(cleaner):
            cleaner(worker)

    def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        runtime = self._runtime_for_worker(worker)
        checker = getattr(runtime, "worker_capacity_error", None)
        if callable(checker):
            return checker(worker)
        return None

    def set_host_process_observer(self, observer) -> None:
        # The persisted admission ledger now bounds both host-native and
        # Docker mission roots. Docker therefore needs the same exact-run
        # process observation used to retain/reconcile a lease after restart.
        for runtime in (
            self.openclaw,
            self.codex,
            self.claude,
            self.host_openclaw,
            self.host_codex,
            self.host_claude,
            self.grok,
            self.host_grok,
        ):
            setter = getattr(runtime, "set_host_process_observer", None)
            if callable(setter):
                setter(observer)

    def set_run_start_observer(self, observer) -> None:
        for runtime in (
            self.openclaw,
            self.codex,
            self.claude,
            self.host_openclaw,
            self.host_codex,
            self.host_claude,
            self.grok,
            self.host_grok,
        ):
            setter = getattr(runtime, "set_run_start_observer", None)
            if callable(setter):
                setter(observer)

    def set_native_event_observer(self, observer) -> None:
        for runtime in (
            self.codex,
            self.claude,
            self.host_openclaw,
            self.host_codex,
            self.host_claude,
            self.grok,
            self.host_grok,
        ):
            setter = getattr(runtime, "set_native_event_observer", None)
            if callable(setter):
                setter(observer)

    def set_provider_liveness_observer(self, observer) -> None:
        for runtime in (
            self.codex,
            self.claude,
            self.host_codex,
            self.host_claude,
            self.grok,
            self.host_grok,
        ):
            setter = getattr(runtime, "set_provider_liveness_observer", None)
            if callable(setter):
                setter(observer)

    def host_process_identity(self, worker: dict, run_id: str) -> dict[str, object] | None:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "host_process_identity", None)
        if not callable(reader):
            return None
        return reader(worker, run_id)

    def host_process_absence(self, worker: dict, run_id: str) -> bool:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "host_process_absence", None)
        if not callable(reader):
            return False
        return reader(worker, run_id) is True

    def host_active_process_status(self, worker: dict) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "host_active_process_status", None)
        if not callable(reader):
            return {"state": "uncertain"}
        return reader(worker)

    def isolated_parallel_readiness(
        self, *, cached_only: bool = False
    ) -> dict[str, object]:
        """Bounded read-only proof that the isolated Docker substrate exists.

        This deliberately never builds or starts an image. Authoritative worker
        creation separately measures and reserves current per-mission capacity.
        """

        with self._isolated_readiness_lock:
            cached = self._isolated_readiness_cache
        if cached and cached[0] + 30.0 > time.monotonic():
            return dict(cached[1])
        if cached_only:
            return {"ready": False, "reason": "docker_readiness_snapshot_unavailable"}
        return self.refresh_isolated_parallel_readiness()

    def refresh_isolated_parallel_readiness(self) -> dict[str, object]:
        def store_result(result: dict[str, object]) -> dict[str, object]:
            with self._isolated_readiness_lock:
                self._isolated_readiness_cache = (time.monotonic(), result)
            return dict(result)

        sandbox = getattr(self.codex, "sandbox", None)
        if sandbox is None:
            return store_result(
                {"ready": False, "reason": "docker_runtime_unavailable"}
            )
        try:
            daemon = sandbox._docker(
                ["info", "--format", "{{.ServerVersion}}"],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            if daemon.returncode != 0:
                return store_result(
                    {"ready": False, "reason": "docker_unavailable"}
                )
            image = sandbox._docker(
                ["image", "inspect", str(sandbox.image)],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            if image.returncode != 0:
                return store_result(
                    {"ready": False, "reason": "workspace_image_unavailable"}
                )
        except Exception:
            return store_result(
                {"ready": False, "reason": "docker_probe_unavailable"}
            )

        policy_probe = getattr(sandbox, "parallel_clean_room_readiness", None)
        if not callable(policy_probe):
            return store_result(
                {
                    "ready": False,
                    "reason": "parallel_clean_room_policy_probe_unavailable",
                }
            )
        try:
            policy_result = policy_probe()
        except Exception:
            return store_result(
                {
                    "ready": False,
                    "reason": "parallel_clean_room_policy_probe_unavailable",
                }
            )
        if not isinstance(policy_result, dict) or policy_result.get("ready") is not True:
            result = (
                dict(policy_result)
                if isinstance(policy_result, dict)
                else {
                    "ready": False,
                    "reason": "parallel_clean_room_policy_probe_unavailable",
                }
            )
            result["ready"] = False
            return store_result(result)

        # Deployment isolation and momentary capacity are different authorities.
        # A container may be between create/start while a sibling is delegated,
        # making Docker top/stats temporarily unavailable. Caching that transient
        # capacity state here hid the otherwise healthy deployment for 30 seconds.
        # Durable delegation still performs a fresh capacity probe and reserves
        # the measured headroom before it accepts each mission.
        return store_result(dict(policy_result))

    def invalidate_isolated_capacity_snapshot(self) -> None:
        shared = getattr(self, "_shared_workspace_runtimes", None)
        if shared is not None:
            shared.resources.invalidate_capacity_snapshot()

    def release_idle_workspace_box(self, worker: dict) -> bool:
        shared = getattr(self, "_shared_workspace_runtimes", None)
        if shared is None:
            return False
        released = shared.release_idle_box(worker, self)
        if released:
            shared.resources.invalidate_capacity_snapshot()
        return released

    def isolated_resource_usage(
        self, *, cached_only: bool = False
    ) -> dict[str, object]:
        from .execution_profile import packaged_linux
        if packaged_linux():
            shared = getattr(self, "_shared_workspace_runtimes", None)
            if shared is None:
                from .workspace_resources import unavailable
                return unavailable()
            return shared.resources.usage(self, cached_only=cached_only)
        usage = (
            self.codex.sandbox.cached_resource_usage(max_age_seconds=30.0)
            if cached_only
            else self.codex.sandbox.resource_usage()
        )
        if usage is None:
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": 0,
                "available_disk_bytes": 0,
                "running_worker_containers": 0,
                "running_worker_ids": [],
                "process_probe_ok": False,
                "memory_probe_ok": False,
                "disk_probe_ok": False,
            }
        return {
            "child_processes": usage.child_processes,
            "threads": usage.threads,
            "available_memory_bytes": usage.available_memory_bytes,
            "available_disk_bytes": usage.available_disk_bytes,
            "running_worker_containers": usage.running_worker_containers,
            "running_worker_ids": list(usage.running_worker_ids),
            "worker_process_counts": {
                worker_id: {
                    "child_processes": child_processes,
                    "threads": threads,
                }
                for worker_id, child_processes, threads in usage.worker_process_counts
            },
            "process_probe_ok": usage.process_probe_ok,
            "memory_probe_ok": usage.memory_probe_ok,
            "disk_probe_ok": usage.disk_probe_ok,
        }

    def refresh_isolated_resource_usage(self) -> dict[str, object]:
        """Run the controlled startup/preflight capacity probe.

        Packaged shared workspaces keep active image, ACL, and native-guard
        checks in the service-owned readiness loop. Admission and request
        readiness only consume that cached proof.
        """

        from .execution_profile import packaged_linux
        if packaged_linux():
            shared = getattr(self, "_shared_workspace_runtimes", None)
            if shared is None:
                from .workspace_resources import unavailable
                return unavailable()
            return shared.resources.refresh(self)
        return self.isolated_resource_usage(cached_only=False)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).reconcile_worker(worker)

    def worker_compute_present(self, worker: dict) -> bool:
        runtime = self._runtime_for_worker(worker)
        checker = getattr(runtime, "worker_compute_present", None)
        if callable(checker):
            return bool(checker(worker))
        return bool(runtime.reconcile_worker(worker).pid)

    def terminal_target(self, worker: dict) -> TerminalTarget:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "terminal_target"):
            return runtime.terminal_target(worker)
        workspace_dir = str(worker.get("workspace_dir") or "")
        return TerminalTarget(
            command=["screen", "-xRR", f"wpr-{worker['worker_id']}"],
            cwd=workspace_dir,
            env={"TERM": "xterm-256color"},
            title=f"{worker['name']} terminal",
            subtitle="Host workspace terminal",
        )

    def describe_worker(self, worker: dict) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "describe_worker"):
            return runtime.describe_worker(worker)
        return {
            "mode": "host-process",
            "runtime": str(worker.get("runtime") or "openclaw"),
            "workspace_dir": str(worker.get("workspace_dir") or ""),
            "state_dir": str(worker.get("state_dir") or ""),
        }

    def effort_projection_for_worker(self, worker: dict) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        resolver = getattr(runtime, "effort_projection_for_worker", None)
        if not callable(resolver):
            return {}
        projection = resolver(worker)
        return dict(projection) if isinstance(projection, dict) else {}

    def provider_native_image_output(self, worker: dict, run: dict, output: str):
        runtime = self._runtime_for_worker(worker)
        resolver = getattr(runtime, "provider_native_image_output", None)
        return resolver(worker, run, output) if callable(resolver) else (output, [])

    def provider_tool_evidence_log(self, worker: dict, run_id: str, *, instruction_sha256: str = "") -> tuple[str, str]:
        profile, stdout = self.provider_activity_log(worker, run_id)
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "provider_rollout_tool_log", None)
        if callable(reader):
            stdout += "\n" + reader(worker, run_id, instruction_sha256=instruction_sha256)
        return profile, stdout

    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        """Return the private native JSONL log used for provider activity normalization."""

        clean_run_id = str(run_id or "").strip()
        if not clean_run_id or "/" in clean_run_id or "\\" in clean_run_id or clean_run_id in {".", ".."}:
            raise ValueError("invalid run id")
        runtime = self._runtime_for_worker(worker)
        attempt_id = str(worker.get("_provider_activity_attempt_id") or "").strip()
        run_root = (
            runtime._attempt_run_root(str(worker["worker_id"]), clean_run_id, attempt_id)
            if attempt_id else runtime._run_root(str(worker["worker_id"]), clean_run_id)
        )
        stdout_path = run_root / "stdout.log"
        if not stdout_path.is_file():
            return str(worker.get("profile") or ""), ""
        try:
            max_bytes = max(
                1024,
                int(
                    os.environ.get(
                        "GLASSHIVE_PROVIDER_LOG_WINDOW_BYTES",
                        str(8 * 1024 * 1024),
                    )
                    or str(8 * 1024 * 1024)
                ),
            )
        except ValueError:
            max_bytes = 8 * 1024 * 1024
        cache_key = (str(worker["worker_id"]), clean_run_id, attempt_id)
        stat = stdout_path.stat()
        with self._provider_log_cache_lock:
            cached = self._provider_log_cache.get(cache_key)
            if (
                cached
                and int(cached.get("size") or -1) == stat.st_size
                and int(cached.get("mtime_ns") or -1) == stat.st_mtime_ns
            ):
                return str(worker.get("profile") or ""), str(cached.get("rendered") or "")

            data = b""
            previous_size = int(cached.get("size") or 0) if cached else 0
            previous_data = cached.get("data") if cached else b""
            if (
                cached
                and isinstance(previous_data, bytes)
                and previous_size < stat.st_size
                and stat.st_size - previous_size <= max_bytes
            ):
                with stdout_path.open("rb") as handle:
                    handle.seek(previous_size)
                    data = previous_data + handle.read()
            else:
                with stdout_path.open("rb") as handle:
                    handle.seek(max(0, stat.st_size - max_bytes))
                    data = handle.read(max_bytes)

            if len(data) > max_bytes:
                data = data[-max_bytes:]
            excluded_bytes = max(0, stat.st_size - len(data))
            if excluded_bytes:
                newline = data.find(b"\n")
                if newline >= 0:
                    data = data[newline + 1 :]
                else:
                    data = b""
                excluded_bytes = stat.st_size - len(data)
            text = data.decode("utf-8", errors="ignore")
            if excluded_bytes:
                marker = json.dumps(
                    {
                        "type": "glasshive.log_compacted",
                        "excluded_prefix_bytes": excluded_bytes,
                    },
                    separators=(",", ":"),
                )
                text = f"{marker}\n{text}"
            self._provider_log_cache[cache_key] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "data": data,
                "rendered": text,
            }
            while len(self._provider_log_cache) > 16:
                self._provider_log_cache.pop(next(iter(self._provider_log_cache)))
        return str(worker.get("profile") or ""), text

    def collect_completed_run(
        self,
        worker: dict,
        run_id: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, object] | None:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "collect_completed_run"):
            try:
                recovered = runtime.collect_completed_run(
                    worker,
                    run_id=run_id,
                    instruction=instruction,
                )
            except TypeError as exc:
                if "instruction" in str(exc):
                    try:
                        recovered = runtime.collect_completed_run(worker, run_id=run_id)
                    except TypeError as run_id_exc:
                        if "run_id" not in str(run_id_exc):
                            raise
                        recovered = runtime.collect_completed_run(worker)
                elif "run_id" not in str(exc):
                    raise
                else:
                    recovered = runtime.collect_completed_run(worker)
            return self._personalize_completed_provider_auth_failure(
                runtime=runtime,
                worker=worker,
                recovered=recovered,
            )
        return None

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        clean_run_id = str(run_id or "").strip()
        if mission_provider_account_selection(worker) is not None and clean_run_id:
            with self.provider_account_binder.project_active_route(
                worker,
                runtime_name=str(
                    getattr(runtime, "runtime_name", "")
                    or worker.get("profile")
                    or ""
                ).strip(),
                run_id=clean_run_id,
            ) as projected_worker:
                if hasattr(runtime, "desktop_action"):
                    return runtime.desktop_action(
                        projected_worker,
                        action,
                        url=url,
                        run_id=run_id,
                    )
                raise RuntimeErrorBase(
                    f"Desktop actions are not supported for profile {worker.get('profile') or 'unknown'}"
                )
        runtime_name = str(
            getattr(runtime, "runtime_name", "")
            or worker.get("profile")
            or ""
        ).strip()
        expected_action = {
            "codex-cli": "codex",
            "claude-code": "claude",
        }.get(runtime_name)
        selection = mission_provider_account_selection(worker)
        if (
            not clean_run_id
            and expected_action == str(action or "").strip().lower()
            and selection is not None
            and str(worker.get("execution_mode") or "docker").strip().lower() == "docker"
        ):
            selected_account = self.provider_account_binder.selected_account_record(
                worker,
                selection,
            )
            if (
                selected_account is None
                or str(selected_account.get("auth_method") or "").strip().lower()
                == "subscription"
            ):
                return self._launch_interactive_provider_session(
                    runtime,
                    worker,
                    action,
                    url=url,
                    runtime_name=runtime_name,
                )
        # Browser, files, and shell actions never need provider credentials. Keep them on the
        # reusable workspace home without holding or exposing a personal-account lease.
        if hasattr(runtime, "desktop_action"):
            return runtime.desktop_action(
                worker,
                action,
                url=url,
                run_id=run_id,
            )
        raise RuntimeErrorBase(f"Desktop actions are not supported for profile {worker.get('profile') or 'unknown'}")

    @staticmethod
    def _interactive_provider_session_timeout() -> float:
        raw = str(
            os.environ.get("GLASSHIVE_INTERACTIVE_PROVIDER_SESSION_MAX_SECONDS")
            or "3600"
        ).strip()
        try:
            seconds = float(raw)
        except ValueError:
            seconds = 3600.0
        return max(300.0, min(seconds, 4 * 60 * 60.0))

    def _launch_interactive_provider_session(
        self,
        runtime,
        worker: dict,
        action: str,
        *,
        url: str | None,
        runtime_name: str,
    ) -> dict[str, object]:
        """Borrow only the selected account auth while the visible native CLI is open."""

        session_id = f"interactive_{secrets.token_hex(16)}"
        session_timeout = self._interactive_provider_session_timeout()
        binding = self.provider_account_binder.bind(
            worker,
            runtime_name=runtime_name,
            run_id=session_id,
            timeout_sec=session_timeout,
            lease_purpose="interactive",
            allow_preferred_fallback=False,
            release_binding=getattr(runtime, "release_provider_account_binding", None),
            abort_binding=lambda bound_worker: runtime.terminate_worker(bound_worker),
            reconcile_binding=getattr(runtime, "reconcile_provider_account_binding", None),
        )
        bound_worker = binding.__enter__()
        close_lock = Lock()
        closed = Event()

        def close_binding() -> None:
            if closed.is_set():
                return
            with close_lock:
                if closed.is_set():
                    return
                try:
                    binding.__exit__(None, None, None)
                finally:
                    closed.set()

        try:
            return runtime.desktop_action(
                bound_worker,
                action,
                url=url,
                run_id=None,
                on_exit=close_binding,
                max_runtime_seconds=session_timeout,
            )
        except BaseException as exc:
            if not closed.is_set():
                binding.__exit__(type(exc), exc, exc.__traceback__)
                closed.set()
            raise


    requires_run_start_identity = True

    def prepare_run_authority_context(
        self, worker: dict, run_id: str | None = None
    ) -> dict[str, str]:
        """Return an exact live container generation before Core mints a run bearer."""

        if (
            str(worker.get("execution_mode") or "docker") != "docker"
            or str(bootstrap_bundle_for(worker).get("execution_policy") or "").strip()
            != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        ):
            raise RuntimeErrorBase(
                "Run-local capability authority requires the Parallel clean-room policy"
            )
        runtime = self._runtime_for_worker(worker)
        # The queue claim moves the durable worker to `starting` before this
        # exact pre-run authority boundary. Permit only this trusted local copy
        # to replace a legacy/non-attesting sandbox; ordinary `starting` and
        # `running` workers remain non-replaceable.
        authority_worker = {
            **worker,
            "_pre_run_substrate_recreate_allowed": True,
        }
        runtime.ensure_worker_ready(authority_worker)
        sandbox_manager = getattr(runtime, "sandbox", None)
        if sandbox_manager is None:
            raise RuntimeErrorBase(
                "The exact mission container generation is unavailable"
            )
        inspection = sandbox_manager.inspect_fresh(str(worker.get("worker_id") or ""))
        sandbox = inspection.sandbox
        matches_policy = getattr(
            sandbox_manager, "_sandbox_matches_parallel_clean_room_policy", None
        )
        if (
            inspection.status != "present"
            or sandbox is None
            or str(sandbox.state or "").lower() != "running"
            or not callable(matches_policy)
            or not matches_policy(sandbox)
        ):
            raise RuntimeErrorBase(
                "The exact mission container generation is unavailable"
            )
        container_id = str(sandbox.container_id or "").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise RuntimeErrorBase(
                "The exact mission container generation is unavailable"
            )
        return {"container_generation_id": container_id}

    def repair_parallel_clean_room_mission_networks(self) -> tuple[str, ...]:
        return self.codex.sandbox.repair_parallel_clean_room_mission_networks()









    def pending_native_input(self, worker: dict, *, run_id: str) -> dict | None:
        method = getattr(self._runtime_for_worker(worker), "pending_native_input", None)
        return method(worker, run_id=run_id) if callable(method) else None

    def respond_native_input(self, worker: dict, **kwargs) -> dict:
        method = getattr(self._runtime_for_worker(worker), "respond_native_input", None)
        if not callable(method):
            raise RuntimeErrorBase("native_input_unavailable")
        return method(worker, **kwargs)







    def cleanup_orphaned_run(self, worker: dict, run_id: str) -> RuntimeInfo | None:
        runtime = self._runtime_for_worker(worker)
        cleanup = getattr(runtime, "cleanup_orphaned_run", None)
        if not callable(cleanup):
            return None
        return cleanup(worker, run_id)

    def cleanup_unconfirmed_run_start(
        self,
        worker: dict,
        run_id: str,
        lease_identity: dict[str, object],
    ) -> bool:
        runtime = self._runtime_for_worker(worker)
        cleanup = getattr(runtime, "cleanup_unconfirmed_run_start", None)
        if not callable(cleanup):
            return False
        return bool(cleanup(worker, run_id, lease_identity))

    def provider_citation_sources(self, worker: dict, run_id: str) -> list[dict[str, str]]:
        """Return structured public provenance retained by the native runtime."""

        runtime = self._runtime_for_worker(worker)
        collector = getattr(runtime, "provider_citation_sources", None)
        if not callable(collector):
            return []
        sources = collector(worker, run_id)
        return [dict(source) for source in sources if isinstance(source, dict)]


class BaseCliWorkerRuntime:
    requires_run_start_identity = True
    runtime_name = "cli"
    worker_root_name = "cli_runtime"
    binary_env_var = ""
    binary_name = ""

    def __init__(self, base_dir: str | None = None, *, create_directories: bool = True) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parents[2] / "data"
        self.runtime_root = self.base_dir / self.worker_root_name
        self.logs_dir = self.runtime_root / "logs"
        self.workers_dir = self.runtime_root / "workers"
        if create_directories:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            self.workers_dir.mkdir(parents=True, exist_ok=True)
        self.binary = os.environ.get(self.binary_env_var, self.binary_name)
        self._process_lock = Lock()
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._stop_reasons: dict[tuple[str, str | None, str], str] = {}
        self._live_telemetry_lock = Lock()
        self._live_telemetry_cache: dict[tuple[str, str], dict[str, object]] = {}
        self._host_process_observer = None
        self._run_start_observer = None
        self._native_event_observer = None
        self._provider_liveness_observer = None
        self._pending_native_authority_receipts: dict[
            tuple[str, str, str], dict[str, object]
        ] = {}
        self._provider_route_evidence_lock = Lock()
        self._provider_route_evidence: dict[str, dict[str, object]] = {}
        self.sandbox = DockerSandboxManager(base_dir=str(self.base_dir), create_directories=create_directories)

    def resolve_model(self, profile: str) -> str:
        raise NotImplementedError

    def _agent_type(self) -> str:
        if self.runtime_name == "codex-cli":
            return "codex"
        if self.runtime_name == "claude-code":
            return "claude"
        return "openclaw"

    def preflight_worker_profile(self, profile: str, execution_mode: str = "docker") -> None:
        return None

    def _default_session_key(self, worker: dict) -> str | None:
        return worker.get("session_key") or f"worker:{worker['worker_id']}"

    def _instruction_with_completion_contract(self, instruction: str) -> str:
        return _instruction_with_completion_contract(instruction)

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        return None

    def _issue_provider_route_failure_evidence(
        self,
        *,
        worker: dict,
        run_id: str,
        source: object,
        evidence: dict[str, object],
    ) -> None:
        failure_class = str(evidence.get("failure_class") or "")
        if (
            failure_class
            not in {"provider_quota_exhausted", "provider_rate_limited"}
            or evidence.get("failure_structured") is not True
        ):
            return
        capability = secrets.token_urlsafe(32)
        record = {
            **evidence,
            "worker_id": str(worker.get("worker_id") or ""),
            "run_id": str(run_id or ""),
            "runtime": self.runtime_name,
            "source": source,
        }
        if not record["worker_id"] or not record["run_id"]:
            return
        with self._provider_route_evidence_lock:
            self._provider_route_evidence[capability] = record
            while len(self._provider_route_evidence) > 128:
                self._provider_route_evidence.pop(next(iter(self._provider_route_evidence)))
        if isinstance(source, dict):
            source["_provider_route_health_capability"] = capability
        else:
            source._provider_route_health_capability = capability

    def consume_provider_route_failure_evidence(
        self,
        worker: dict,
        run: dict,
        source: object,
    ) -> dict[str, object] | None:
        capability = str(
            source.get("_provider_route_health_capability")
            if isinstance(source, dict)
            else getattr(source, "_provider_route_health_capability", "")
        ).strip()
        if not capability:
            return None
        with self._provider_route_evidence_lock:
            record = self._provider_route_evidence.pop(capability, None)
        if (
            not isinstance(record, dict)
            or str(record.get("worker_id") or "")
            != str(worker.get("worker_id") or "")
            or str(record.get("run_id") or "") != str(run.get("run_id") or "")
            or str(record.get("runtime") or "") != self.runtime_name
        ):
            return None
        return {
            key: value
            for key, value in record.items()
            if key not in {"worker_id", "run_id", "runtime", "source"}
        }

    def _supplemental_provider_failure(
        self,
        *,
        worker: dict,
        run_id: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> tuple[FailureClassification, dict[str, object]] | None:
        _ = (worker, run_id, stdout, stderr, exit_code)
        return None

    def _provider_failure_for_process(
        self,
        *,
        worker: dict,
        run_id: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> tuple[FailureClassification, dict[str, object] | None]:
        classification = classify_cli_failure(
            stdout=stdout,
            stderr=stderr,
            runtime_name=self.runtime_name,
            exit_code=exit_code,
        )
        evidence: dict[str, object] | None = None
        if (
            classification.structured
            and classification.failure_class
            in {"provider_quota_exhausted", "provider_rate_limited"}
            and classification.provider_event_source
        ):
            evidence_material = json.dumps(
                {
                    "worker_id": str(worker.get("worker_id") or ""),
                    "run_id": str(run_id or ""),
                    "runtime": self.runtime_name,
                    "source": classification.provider_event_source,
                    "failure_class": classification.failure_class,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            evidence = {
                "version": 1,
                "failure_class": classification.failure_class,
                "failure_structured": True,
                "retry_at": "",
                "retry_after_s": classification.retry_after_s,
                "evidence_kind": classification.provider_event_source,
                "evidence_id": hashlib.sha256(
                    evidence_material.encode("utf-8")
                ).hexdigest(),
            }
        elif not classification.structured:
            supplemental = self._supplemental_provider_failure(
                worker=worker,
                run_id=run_id,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
            )
            if supplemental is not None:
                classification, evidence = supplemental
        return classification, evidence

    def _provider_process_exit_error_for_run(
        self,
        *,
        worker: dict,
        run_id: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        message: str,
    ) -> RuntimeErrorBase:
        classification, evidence = self._provider_failure_for_process(
            worker=worker,
            run_id=run_id,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
        error = _provider_process_exit_error(
            runtime_name=self.runtime_name,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            message=message,
            classification=classification,
        )
        if evidence is not None:
            retry_at = str(evidence.get("retry_at") or "")
            retry_after_s = evidence.get("retry_after_s")
            if retry_at:
                error.provider_retry_at = retry_at
            if (
                isinstance(retry_after_s, (int, float))
                and not isinstance(retry_after_s, bool)
            ):
                error.provider_retry_after_s = float(retry_after_s)
                if not hasattr(error, "retry_after_s"):
                    error.retry_after_s = float(retry_after_s)
            self._issue_provider_route_failure_evidence(
                worker=worker,
                run_id=run_id,
                source=error,
                evidence=evidence,
            )
        return error

    def _worker_root(self, worker_id: str) -> Path:
        return self.sandbox.paths(worker_id)["worker_root"]

    def _state_dir(self, worker_id: str) -> Path:
        return self.sandbox.paths(worker_id)["state_dir"]

    def _workspace_dir(self, worker_id: str) -> Path:
        return self.sandbox.paths(worker_id)["workspace_dir"]

    def _home_dir(self, worker_id: str) -> Path:
        return self.sandbox.paths(worker_id)["home_dir"]

    def _session_meta_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "session.json"

    def _active_session_meta_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "active_terminal_session.json"

    @contextmanager
    def _active_session_file_lock(self, worker_id: str):
        lock_path = self._state_dir(worker_id) / "active_terminal_session.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            lock_path.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _run_root(self, worker_id: str, run_id: str) -> Path:
        return self._home_dir(worker_id) / ".glasshive-runs" / run_id

    def _container_run_root(self, run_id: str) -> str:
        return f"{self.sandbox.home_mount}/.glasshive-runs/{run_id}"

    @staticmethod
    def _run_attempt_ref(attempt_id: str) -> str:
        clean_attempt_id = str(attempt_id or "").strip()
        return (
            "attempt-sha256-"
            + hashlib.sha256(clean_attempt_id.encode("utf-8")).hexdigest()
        )

    def _attempt_run_root(
        self, worker_id: str, run_id: str, attempt_id: str | None
    ) -> Path:
        clean_attempt_id = str(attempt_id or "").strip()
        run_root = self._run_root(worker_id, run_id)
        if not clean_attempt_id:
            return run_root
        return run_root / "attempts" / self._run_attempt_ref(clean_attempt_id)

    def _attempt_container_run_root(
        self, run_id: str, attempt_id: str | None
    ) -> str:
        clean_attempt_id = str(attempt_id or "").strip()
        run_root = self._container_run_root(run_id)
        if not clean_attempt_id:
            return run_root
        return f"{run_root}/attempts/{self._run_attempt_ref(clean_attempt_id)}"

    def _direct_worker_native_authority(
        self, worker: dict
    ) -> tuple[dict[str, object], str] | None:
        if self.runtime_name not in {"claude-code", "codex-cli"}:
            return None
        bundle = canonicalize_viventium_feeling_projection(
            bootstrap_bundle_for(worker)
        )
        projection = bundle.get("viventium_feelings_projection")
        if not isinstance(projection, dict):
            return None
        configured_runtime = str(worker.get("runtime") or "").strip()
        if configured_runtime and configured_runtime != self.runtime_name:
            raise RuntimeErrorBase(
                "The configured native runtime differs from the selected direct Worker"
            )
        authority = str(glasshive_project_agents_md(bundle))
        placement = worker.get("_native_context_placement")
        project_path = (Path(placement["private_project_dir"]) if placement else self._workspace_dir(str(worker["worker_id"]))) / "AGENTS.md"
        try:
            materialized_project = project_path.read_text()
        except OSError as exc:
            raise RuntimeErrorBase(
                "The request-pinned direct Worker authority is unavailable"
            ) from exc
        if not hmac.compare_digest(
            materialized_project.encode("utf-8"), authority.encode("utf-8")
        ):
            raise RuntimeErrorBase(
                "The direct Worker project authority differs from its request-pinned snapshot"
            )
        try:
            _stable, capsules = _split_viventium_feeling_capsules(authority)
        except ValueError as exc:
            raise RuntimeErrorBase(
                "The direct Worker native authority contains malformed Feeling state"
            ) from exc
        expected_count = int(projection["expected_capsule_count"])
        if len(capsules) != expected_count:
            raise RuntimeErrorBase(
                "The direct Worker native authority violates its Feeling scope"
            )
        if capsules:
            digest = hashlib.sha256(capsules[0].encode("utf-8")).hexdigest()
            if not hmac.compare_digest(
                digest, str(projection["snapshot_sha256"])
            ) or not authority.rstrip().endswith(capsules[0]):
                raise RuntimeErrorBase(
                    "The direct Worker native Feeling capsule is not the exact final pinned state"
                )
        return dict(projection), authority

    def _materialize_direct_worker_native_authority(
        self, worker: dict
    ) -> str | None:
        resolved = self._direct_worker_native_authority(worker)
        if resolved is None:
            return None
        _projection, authority = resolved
        worker_id = str(worker["worker_id"])
        home_dir = self._home_dir(worker_id)
        if self.runtime_name == "claude-code":
            bundle = canonicalize_viventium_feeling_projection(
                bootstrap_bundle_for(worker)
            )
            expected_project = str(glasshive_project_claude_md(bundle))
            prefix = "@AGENTS.md\n"
            if not expected_project.startswith(prefix):
                raise RuntimeErrorBase(
                    "The Claude direct Worker project authority cannot be isolated"
                )
            isolated_project = expected_project[len(prefix) :].lstrip("\n")
            if "@AGENTS.md" in isolated_project:
                raise RuntimeErrorBase(
                    "The Claude direct Worker would receive duplicate Feeling authority"
                )
            placement = worker.get("_native_context_placement")
            workspace_dir = Path(placement["private_project_dir"]) if placement else self._workspace_dir(worker_id)
            for filename in ("CLAUDE.md", "claude.md"):
                try:
                    current = (workspace_dir / filename).read_text()
                except OSError as exc:
                    raise RuntimeErrorBase(
                        "The Claude direct Worker project authority is unavailable"
                    ) from exc
                if current not in {expected_project, isolated_project}:
                    raise RuntimeErrorBase(
                        "The Claude direct Worker project authority changed before launch"
                    )
                _write_clean_room_file(
                    workspace_dir,
                    Path(filename),
                    isolated_project.encode("utf-8"),
                    mode=0o644,
                )
            relative = Path(".glasshive/developer-instructions.txt")
            _write_clean_room_file(
                home_dir,
                relative,
                authority.encode("utf-8"),
                mode=0o600,
            )
            return f"{self.sandbox.home_mount}/{relative.as_posix()}"

        if self._worker_env_flag(worker, "WPR_CODEX_CLI_IGNORE_USER_CONFIG", False):
            raise RuntimeErrorBase(
                "The configured Codex direct Worker ignores its native developer authority"
            )
        relative = Path(".codex/config.toml")
        target = home_dir / relative
        if target.is_symlink():
            raise RuntimeErrorBase(
                "The Codex direct Worker config must not be a symbolic link"
            )
        try:
            existing = target.read_text() if target.exists() else ""
        except OSError as exc:
            raise RuntimeErrorBase(
                "The Codex direct Worker config is unavailable"
            ) from exc
        config = _apply_codex_developer_instructions(existing, authority)
        _write_clean_room_file(
            home_dir,
            relative,
            (str(config).rstrip() + "\n").encode("utf-8"),
            mode=0o600,
        )
        return str(target)

    def _native_provider_authority_receipt(
        self,
        worker: dict,
        *,
        command: list[str],
        run_id: str,
        model: str,
    ) -> dict[str, object] | None:
        resolved = self._direct_worker_native_authority(worker)
        if resolved is None:
            return None
        projection, expected = resolved
        configured_model = str(worker.get("model") or "").strip()
        if configured_model and configured_model != str(model):
            raise RuntimeErrorBase(
                "The direct Worker native model differs from its configured model"
            )
        native_commands = [command]
        wrapped_commands = worker.get("_glasshive_native_authority_commands")
        if wrapped_commands is not None:
            if (
                not isinstance(wrapped_commands, list)
                or len(wrapped_commands) != 2
                or any(
                    not isinstance(candidate, list)
                    or not candidate
                    or any(not isinstance(part, str) for part in candidate)
                    for candidate in wrapped_commands
                )
            ):
                raise RuntimeErrorBase(
                    "The direct Worker native resume authority evidence is malformed"
                )
            native_commands = [list(candidate) for candidate in wrapped_commands]
            expected_wrapper = (
                self._resume_with_fresh_fallback(
                    worker,
                    resume_command=native_commands[0],
                    fresh_command=native_commands[1],
                )
                if self.runtime_name == "claude-code"
                else self._codex_resume_with_fresh_fallback(
                    worker,
                    resume_command=native_commands[0],
                    fresh_command=native_commands[1],
                )
            )
            if command != expected_wrapper:
                raise RuntimeErrorBase(
                    "The direct Worker native resume wrapper differs from its verified commands"
                )
        worker_id = str(worker["worker_id"])
        if self.runtime_name == "claude-code":
            model_flag = "--model"
            flag = "--append-system-prompt-file"
            expected_path = (
                f"{self.sandbox.home_mount}/.glasshive/developer-instructions.txt"
            )
            for native_command in native_commands:
                if native_command.count(model_flag) != 1:
                    raise RuntimeErrorBase(
                        "The Claude direct Worker does not expose its configured native model"
                    )
                try:
                    native_model = str(
                        native_command[native_command.index(model_flag) + 1]
                    )
                except IndexError as exc:
                    raise RuntimeErrorBase(
                        "The Claude direct Worker does not expose its configured native model"
                    ) from exc
                if native_model != str(model):
                    raise RuntimeErrorBase(
                        "The Claude direct Worker command differs from its configured native model"
                    )
                if native_command.count(flag) != 1:
                    raise RuntimeErrorBase(
                        "The Claude direct Worker native authority is not attached exactly once"
                    )
                try:
                    observed_path = native_command[
                        native_command.index(flag) + 1
                    ]
                except IndexError as exc:
                    raise RuntimeErrorBase(
                        "The Claude direct Worker native authority path is unavailable"
                    ) from exc
                if observed_path != expected_path:
                    raise RuntimeErrorBase(
                        "The Claude direct Worker native authority uses an unexpected mount"
                    )
            path = (
                self._home_dir(worker_id)
                / ".glasshive"
                / "developer-instructions.txt"
            )
            try:
                materialized = path.read_text()
                project = (
                    self._workspace_dir(worker_id) / "CLAUDE.md"
                ).read_text()
            except OSError as exc:
                raise RuntimeErrorBase(
                    "The Claude direct Worker native authority is unreadable"
                ) from exc
            if "@AGENTS.md" in project:
                raise RuntimeErrorBase(
                    "The Claude direct Worker would load its Feeling authority twice"
                )
            placement = "append_system_prompt_file"
        else:
            for native_command in native_commands:
                native_models: list[str] = []
                for index, item in enumerate(native_command[:-1]):
                    if item in {"-m", "--model"}:
                        native_models.append(str(native_command[index + 1]))
                    elif item == "-c" and native_command[index + 1].startswith(
                        "model="
                    ):
                        try:
                            native_models.append(
                                str(
                                    tomllib.loads(native_command[index + 1]).get(
                                        "model"
                                    )
                                    or ""
                                )
                            )
                        except ValueError as exc:
                            raise RuntimeErrorBase(
                                "The Codex direct Worker does not expose its configured native model"
                            ) from exc
                if native_models != [str(model)]:
                    raise RuntimeErrorBase(
                        "The Codex direct Worker command differs from its configured native model"
                    )
                if "--ignore-user-config" in native_command:
                    raise RuntimeErrorBase(
                        "The Codex direct Worker would ignore its native developer authority"
                    )
                overrides = [
                    native_command[index + 1]
                    for index, item in enumerate(native_command[:-1])
                    if item == "-c"
                    and native_command[index + 1].startswith(
                        "project_doc_max_bytes="
                    )
                ]
                if overrides != ["project_doc_max_bytes=0"]:
                    raise RuntimeErrorBase(
                        "The Codex direct Worker would load its Feeling authority twice"
                    )
            path = self._home_dir(worker_id) / ".codex" / "config.toml"
            try:
                materialized = str(
                    tomllib.loads(path.read_text()).get("developer_instructions")
                    or ""
                )
            except (OSError, ValueError) as exc:
                raise RuntimeErrorBase(
                    "The Codex direct Worker native authority is unreadable"
                ) from exc
            placement = "codex_developer_instructions"
        try:
            insecure = path.is_symlink() or bool(path.stat().st_mode & 0o077)
        except OSError as exc:
            raise RuntimeErrorBase(
                "The direct Worker native authority permissions are unavailable"
            ) from exc
        if insecure or not hmac.compare_digest(
            materialized.encode("utf-8"), expected.encode("utf-8")
        ):
            raise RuntimeErrorBase(
                "The direct Worker native authority is insecure or differs from its pinned snapshot"
            )
        _stable, capsules = _split_viventium_feeling_capsules(materialized)
        if len(capsules) != int(projection["expected_capsule_count"]):
            raise RuntimeErrorBase(
                "The direct Worker native authority violates its Feeling scope"
            )
        attempt_id = str(worker.get("_run_attempt_id") or "").strip()
        return {
            "protocol": "glasshive.native_provider_authority_receipt.v1",
            "run_id": str(run_id),
            **({"attempt_id": attempt_id} if attempt_id else {}),
            "runtime": self.runtime_name,
            "model": str(model),
            "authority_sha256": hashlib.sha256(
                materialized.encode("utf-8")
            ).hexdigest(),
            "authority_chars": len(materialized),
            "feeling_capsule_count": len(capsules),
            "placement": placement,
            "materialized": True,
        }

    def _native_provider_authority_receipt_path(
        self,
        worker_id: str,
        run_id: str,
        *,
        attempt_id: str | None = None,
    ) -> Path:
        clean_attempt_id = str(attempt_id or "").strip()
        receipt_ref = hashlib.sha256(
            (
                f"{self.runtime_name}\x00{run_id}\x00{clean_attempt_id}"
                if clean_attempt_id
                else f"{self.runtime_name}\x00{run_id}"
            ).encode("utf-8")
        ).hexdigest()
        return (
            self._state_dir(worker_id)
            / "native-provider-authority-receipts"
            / f"{receipt_ref}.json"
        )

    def native_provider_authority_receipt(
        self, worker: dict, run_id: str
    ) -> dict[str, object] | None:
        attempt_id = str(worker.get("_run_attempt_id") or "").strip()
        path = self._native_provider_authority_receipt_path(
            str(worker.get("worker_id") or ""),
            str(run_id),
            attempt_id=attempt_id,
        )
        try:
            if (
                path.parent.is_symlink()
                or path.parent.stat().st_mode & 0o077
                or path.is_symlink()
                or path.stat().st_mode & 0o077
            ):
                return None
            parsed = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(parsed, dict)
            or parsed.get("protocol")
            != "glasshive.native_provider_authority_receipt.v1"
            or str(parsed.get("run_id") or "") != str(run_id)
            or (
                attempt_id
                and str(parsed.get("attempt_id") or "") != attempt_id
            )
            or parsed.get("runtime") != self.runtime_name
            or parsed.get("materialized") is not True
        ):
            return None
        allowed = {
            "protocol",
            "run_id",
            "runtime",
            "model",
            "authority_sha256",
            "authority_chars",
            "feeling_capsule_count",
            "placement",
            "materialized",
        }
        if attempt_id:
            allowed.add("attempt_id")
        if set(parsed) != allowed:
            return None
        return {key: parsed[key] for key in allowed}

    def _ensure_dirs(self, worker_id: str) -> None:
        self._workspace_dir(worker_id).mkdir(parents=True, exist_ok=True)
        self._home_dir(worker_id).mkdir(parents=True, exist_ok=True)

    def _usage_from_output(self, stdout: str) -> dict[str, int]:
        _ = stdout
        return {}

    def _telemetry_from_output(self, stdout: str) -> dict[str, object]:
        _ = stdout
        return {}

    def _record_run_usage(self, worker_id: str, run_id: str, stdout: str) -> dict[str, int]:
        usage = self._usage_from_output(stdout)
        if usage:
            path = self._run_root(worker_id, run_id) / "usage.json"
            _write_private_json(path, usage)
        return usage

    def _record_run_telemetry(self, worker_id: str, run_id: str, stdout: str) -> dict[str, object]:
        telemetry = _safe_run_telemetry(self._telemetry_from_output(stdout), run_id=run_id)
        if telemetry:
            path = self._run_root(worker_id, run_id) / "telemetry.json"
            if not _write_private_json(path, telemetry):
                logger.warning(
                    "Failed to persist GlassHive run telemetry",
                    extra={"worker_id": worker_id, "run_id": run_id},
                )
        return telemetry

    def _record_run_metrics(
        self,
        worker_id: str,
        run_id: str,
        stdout: str,
    ) -> tuple[dict[str, int], dict[str, object]]:
        return (
            self._record_run_usage(worker_id, run_id, stdout),
            self._record_run_telemetry(worker_id, run_id, stdout),
        )

    def run_usage(self, worker: dict, run_id: str) -> dict[str, int]:
        path = self._run_root(str(worker["worker_id"]), str(run_id)) / "usage.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): int(token_count) for key, token_count in value.items()} if isinstance(value, dict) else {}

    def run_telemetry(self, worker: dict, run_id: str) -> dict[str, object]:
        path = self._run_root(str(worker["worker_id"]), str(run_id)) / "telemetry.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return _safe_run_telemetry(value, run_id=str(run_id))

    def live_telemetry(
        self,
        worker: dict,
        stdout: str,
        run_id: str | None = None,
    ) -> dict[str, object]:
        telemetry_source = stdout
        scope = "console_tail"
        if run_id:
            run_stdout = self._run_root(str(worker["worker_id"]), str(run_id)) / "stdout.log"
            try:
                telemetry_source = run_stdout.read_text(errors="replace")
                scope = "full_active_run"
            except OSError:
                return {
                    "schema": "glasshive.claude-run-telemetry.v1",
                    "run_id": str(run_id),
                    "telemetry_scope": "active_run_unavailable",
                }
        telemetry = self._telemetry_from_output(telemetry_source)
        if telemetry:
            telemetry["telemetry_scope"] = scope
            if run_id:
                telemetry["run_id"] = str(run_id)
        return telemetry

    def _read_session_key(self, worker_id: str) -> str | None:
        path = self._session_meta_path(worker_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        session_runtime = str(data.get("runtime") or "").strip()
        if session_runtime and session_runtime != self.runtime_name:
            return None
        value = str(data.get("session_key") or "").strip()
        return value or None

    def _write_session_key(self, worker_id: str, session_key: str, *, context_epoch: str | None = None) -> None:
        path = self._session_meta_path(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if context_epoch is None:
            try:
                context_epoch = str(json.loads(path.read_text()).get("context_epoch") or "")
            except (OSError, ValueError, AttributeError):
                context_epoch = ""
        _atomic_write_private_text(
            path,
            json.dumps(
                {
                    "session_key": session_key,
                    "context_epoch": context_epoch,
                    "runtime": self.runtime_name,
                },
                indent=2,
            ),
        )

    def _read_provider_session_key(self, worker: dict) -> str | None:
        epoch = self._bootstrap_env_value(worker, GLASSHIVE_PROVIDER_SESSION_EPOCH_ENV)
        if epoch:
            try:
                data = json.loads(self._session_meta_path(worker["worker_id"]).read_text())
                if str(data.get("context_epoch") or "") != epoch:
                    return None
            except (OSError, ValueError, AttributeError):
                return None
        return self._read_session_key(worker["worker_id"])

    def _provider_session_starts_fresh(self, worker: dict) -> bool:
        return self._provider_session_is_stateless(worker) or bool(
            self._bootstrap_env_value(worker, GLASSHIVE_PROVIDER_SESSION_EPOCH_ENV)
            and not self._read_provider_session_key(worker)
        )

    def _provider_session_is_stateless(self, worker: dict) -> bool:
        return (
            self._bootstrap_env_value(worker, GLASSHIVE_PROVIDER_SESSION_MODE_ENV)
            == "stateless"
        )

    def _remember_native_session_key(
        self, worker: dict, session_key: str | None
    ) -> None:
        """Persist native continuity only for persistent provider turns."""

        if session_key and not self._provider_session_is_stateless(worker):
            self._write_session_key(str(worker["worker_id"]), session_key,
                context_epoch=self._bootstrap_env_value(worker, GLASSHIVE_PROVIDER_SESSION_EPOCH_ENV))

    def _read_active_session(self, worker_id: str) -> dict[str, object] | None:
        path = self._active_session_meta_path(worker_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        session_name = str(data.get("session_name") or "").strip()
        if not session_name:
            return None
        return {
            "session_name": session_name,
            "run_id": str(data.get("run_id") or "").strip(),
            "attempt_id": str(data.get("attempt_id") or "").strip(),
            "stdout_path": str(data.get("stdout_path") or "").strip(),
            "stderr_path": str(data.get("stderr_path") or "").strip(),
            "exit_path": str(data.get("exit_path") or "").strip(),
            "constraint_ledger_path": str(data.get("constraint_ledger_path") or "").strip(),
            "model": str(data.get("model") or "").strip(),
            "argv_for_evidence_json": str(data.get("argv_for_evidence_json") or "").strip(),
            "started_at": str(data.get("started_at") or "").strip(),
            "process_pid": data.get("process_pid"),
            "process_group": data.get("process_group"),
            "process_start_identity": str(
                data.get("process_start_identity") or ""
            ).strip(),
            "process_identity_sha256": str(data.get("process_identity_sha256") or "").strip(),
            "lease_pid": data.get("lease_pid"),
            "lease_process_group": data.get("lease_process_group"),
            "lease_process_start_identity": str(
                data.get("lease_process_start_identity") or ""
            ).strip(),
            "owner_pid": data.get("owner_pid"),
            "heartbeat_path": str(data.get("heartbeat_path") or "").strip(),
            "timeout_seconds": data.get("timeout_seconds"),
            "run_mode": str(data.get("run_mode") or "").strip(),
            "native_session_id": str(data.get("native_session_id") or "").strip(),
            "instruction_redacted": bool(data.get("instruction_redacted")),
            "workspace_dir": str(data.get("workspace_dir") or "").strip(),
            "run_mode": str(data.get("run_mode") or "").strip(),
            "native_session_id": str(data.get("native_session_id") or "").strip(),
            "native_image_scope": data.get("native_image_scope"),
            "host_slot_token": str(data.get("host_slot_token") or ""),
            "termination_unconfirmed": bool(data.get("termination_unconfirmed")),
            "container_id": str(data.get("container_id") or "").strip(),
            "startup_token_digest": str(
                data.get("startup_token_digest") or ""
            ).strip(),
        }

    def _write_active_session(
        self,
        worker_id: str,
        payload: dict[str, object],
        *,
        expected_session: dict[str, object] | None = None,
        publish_run_start: bool = False,
        worker: dict | None = None,
        spawned_process: subprocess.Popen[str] | None = None,
    ) -> bool:
        path = self._active_session_meta_path(worker_id)
        safe_payload = dict(payload)
        if publish_run_start and isinstance(worker, dict):
            token_digest = str(
                worker.get("_run_startup_token_digest") or ""
            ).strip()
            if re.fullmatch(r"[0-9a-f]{64}", token_digest):
                safe_payload["startup_token_digest"] = token_digest
        if safe_payload.get("process_pid") and not safe_payload.get("process_identity_sha256"):
            try:
                process_pid = int(safe_payload["process_pid"])
            except (TypeError, ValueError):
                process_pid = 0
            process_identity = self._process_identity_sha256(process_pid)
            if process_identity:
                safe_payload["process_identity_sha256"] = process_identity
        if safe_payload.get("process_pid"):
            try:
                process_pid = int(safe_payload["process_pid"])
            except (TypeError, ValueError):
                process_pid = 0
            if process_pid > 0:
                if not safe_payload.get("process_group"):
                    safe_payload["process_group"] = self._process_group_identity(
                        process_pid
                    )
                if not safe_payload.get("process_start_identity"):
                    process_start_identity = self._process_start_identity(process_pid)
                    if process_start_identity:
                        safe_payload["process_start_identity"] = process_start_identity
        if "instruction" in safe_payload:
            safe_payload.pop("instruction", None)
            safe_payload["instruction_redacted"] = True
        with self._active_session_file_lock(worker_id):
            if expected_session is not None:
                current = self._read_active_session(worker_id)
                if self._active_session_fingerprint(
                    current
                ) != self._active_session_fingerprint(expected_session):
                    return False
            _atomic_write_private_text(path, json.dumps(safe_payload, indent=2))
        observer = self._host_process_observer
        if callable(observer):
            try:
                pid = int(
                    safe_payload.get("lease_pid")
                    or safe_payload.get("process_pid")
                    or 0
                )
                process_group = int(
                    safe_payload.get("lease_process_group")
                    or safe_payload.get("process_group")
                    or 0
                )
            except (TypeError, ValueError):
                pid = 0
                process_group = 0
            run_id = str(safe_payload.get("run_id") or "").strip()
            start_identity = str(
                safe_payload.get("lease_process_start_identity")
                or safe_payload.get("process_start_identity")
                or ""
            ).strip()
            if run_id and pid > 0 and start_identity:
                container_id = str(safe_payload.get("container_id") or "").strip()
                session_id = str(safe_payload.get("session_name") or "").strip()
                try:
                    observer(
                        {
                            "worker_id": worker_id,
                            "run_id": run_id,
                            "identity_kind": (
                                "docker_session" if container_id else "host_process"
                            ),
                            "pid": pid,
                            "process_group": process_group or pid,
                            "process_start_identity": start_identity,
                            "container_id": container_id,
                            "session_id": session_id,
                        }
                    )
                except Exception:
                    logger.exception(
                        "Host process observer failed for worker %s run %s",
                        worker_id,
                        run_id,
                    )
        if publish_run_start:
            durable_session = self._read_active_session(worker_id)
            if self._active_session_fingerprint(
                durable_session
            ) != self._active_session_fingerprint(safe_payload):
                raise RunStartupRejectedError(
                    "The durable run startup identity changed before publication.",
                    termination_confirmed=False,
                )
            start_observer = self._run_start_observer
            if not callable(start_observer):
                self._cleanup_rejected_run_start(
                    worker_id=worker_id,
                    expected_session=durable_session,
                    expected_container_id=str(
                        durable_session.get("container_id") or ""
                    ).strip(),
                    spawned_process=spawned_process,
                    worker=worker,
                )
                raise RunStartupRejectedError(
                    "No durable run startup observer is configured.",
                    termination_confirmed=True,
                )
            assert durable_session is not None
            container_id = str(durable_session.get("container_id") or "").strip()
            try:
                pid = int(
                    durable_session.get("lease_pid")
                    or durable_session.get("process_pid")
                    or 0
                )
                process_group = int(
                    durable_session.get("lease_process_group")
                    or durable_session.get("process_group")
                    or pid
                    or 0
                )
            except (TypeError, ValueError):
                pid = 0
                process_group = 0
            start_identity = str(
                durable_session.get("lease_process_start_identity")
                or durable_session.get("process_start_identity")
                or ""
            ).strip()
            start_payload = {
                "worker_id": worker_id,
                "run_id": str(durable_session.get("run_id") or ""),
                "identity_kind": (
                    "docker_session" if container_id else "host_process"
                ),
                "pid": pid,
                "process_group": process_group,
                "process_start_identity": start_identity,
                "container_id": container_id,
                "session_id": str(durable_session.get("session_name") or ""),
            }
            try:
                start_observer(start_payload)
            except Exception as exc:
                self._cleanup_rejected_run_start(
                    worker_id=worker_id,
                    expected_session=durable_session,
                    expected_container_id=container_id,
                    spawned_process=spawned_process,
                    worker=worker,
                )
                raise RunStartupRejectedError(
                    str(exc), termination_confirmed=True
                ) from exc
        return True

    def _process_identity_sha256(self, pid: int) -> str | None:
        """Return a non-secret birth/command fingerprint for safe post-restart PID recovery."""
        if pid <= 1:
            return None
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        identity = result.stdout.strip() if result.returncode == 0 else ""
        if not identity:
            return None
        # Persist only the digest: a harness command line may contain private arguments.
        return hashlib.sha256(f"{pid}\n{identity}".encode("utf-8", errors="replace")).hexdigest()

    def _clear_active_session(
        self,
        worker_id: str,
        *,
        expected_session: dict[str, object] | None = None,
    ) -> bool:
        path = self._active_session_meta_path(worker_id)
        with self._active_session_file_lock(worker_id):
            if expected_session is not None:
                current = self._read_active_session(worker_id)
                if current is None and not path.exists():
                    return True
                if self._active_session_fingerprint(current) != self._active_session_fingerprint(
                    expected_session
                ):
                    return False
            try:
                path.unlink()
            except FileNotFoundError:
                return True
        return True

    def _run_root_candidates(self, worker_id: str) -> list[Path]:
        root = self._home_dir(worker_id) / ".glasshive-runs"
        if not root.exists():
            return []
        candidates = [path for path in root.iterdir() if path.is_dir() and path.name.startswith("run_")]
        return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)

    def _latest_run_root(self, worker_id: str) -> Path | None:
        candidates = self._run_root_candidates(worker_id)
        return candidates[0] if candidates else None

    def _session_name_for_run_id(self, run_id: str) -> str:
        return f"job-{run_id[:12]}"

    def _read_native_image_scope(self, worker_id: str, run_id: str) -> dict:
        try:
            data = _native_media_snapshot(
                self._run_root(worker_id, run_id), Path("native-image-scope.json"), 128 * 1024
            )
            scope = json.loads(data)
            return scope if isinstance(scope, dict) else {}
        except (OSError, ValueError):
            return {}

    def _run_payload(
        self,
        worker_id: str,
        run_id: str,
        attempt_id: str | None = None,
    ) -> dict[str, str] | None:
        clean_attempt_id = str(attempt_id or "").strip()
        run_root = self._attempt_run_root(worker_id, run_id, clean_attempt_id)
        if not run_root.exists():
            return None
        return {
            "session_name": self._session_name_for_run_id(run_id),
            "run_id": run_id,
            "attempt_id": clean_attempt_id,
            "stdout_path": str(run_root / "stdout.log"),
            "native_image_scope": self._read_native_image_scope(worker_id, run_id),
            "stderr_path": str(run_root / "stderr.log"),
            "exit_path": str(run_root / "exit_code"),
        }

    @staticmethod
    def _read_completed_exit_code(exit_path: Path) -> int | None:
        """Return a durable exit code, treating a private empty marker as unfinished."""

        try:
            raw_exit_code = exit_path.read_text().strip()
        except FileNotFoundError:
            return None
        if not raw_exit_code:
            return None
        try:
            return int(raw_exit_code)
        except ValueError:
            return 1

    def _infer_active_session(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        attempt_id = str(worker.get("_run_attempt_id") or "").strip()
        current = self._read_active_session(worker["worker_id"])
        if (
            current
            and (run_id is None or current.get("run_id") == run_id)
            and (
                not attempt_id
                or str(current.get("attempt_id") or "") == attempt_id
            )
        ):
            return current
        candidate_run_ids = [run_id] if run_id else [run_root.name for run_root in self._run_root_candidates(worker["worker_id"])]
        candidates: list[dict[str, str]] = []
        for candidate_run_id in candidate_run_ids:
            if not candidate_run_id:
                continue
            payload = self._run_payload(
                worker["worker_id"], candidate_run_id, attempt_id
            )
            if payload is not None:
                candidates.append(payload)
        if not candidates:
            # Session discovery must never materialize or repair a Docker sandbox merely because a
            # caller is terminating a synthetic, already-cleaned, or crash-recovered run.
            return None
        screen_sessions = set(self.sandbox.list_screen_sessions(worker["worker_id"], self.runtime_name, worker=worker))
        for payload in candidates:
            session_name = payload["session_name"]
            if session_name not in screen_sessions:
                continue
            return payload
        return None

    def _latest_completed_run_payload(
        self,
        worker_id: str,
        run_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, str] | None:
        clean_attempt_id = str(attempt_id or "").strip()
        current = self._read_active_session(worker_id)
        if (
            current
            and (run_id is None or current.get("run_id") == run_id)
            and (
                not clean_attempt_id
                or str(current.get("attempt_id") or "") == clean_attempt_id
            )
        ):
            current_exit = Path(str(current.get("exit_path") or ""))
            current_stdout = Path(str(current.get("stdout_path") or ""))
            if self._read_completed_exit_code(current_exit) is not None or self._stdout_has_complete_response(
                current_stdout
            ):
                return current
        if run_id:
            payload = self._run_payload(worker_id, run_id, clean_attempt_id)
            if payload:
                exit_path = Path(str(payload.get("exit_path") or ""))
                stdout_path = Path(str(payload.get("stdout_path") or ""))
                if self._read_completed_exit_code(exit_path) is not None or self._stdout_has_complete_response(
                    stdout_path
                ):
                    return payload
            return None
        for run_root in self._run_root_candidates(worker_id):
            exit_path = run_root / "exit_code"
            stdout_path = run_root / "stdout.log"
            if self._read_completed_exit_code(exit_path) is None and not self._stdout_has_complete_response(stdout_path):
                continue
            return {
                "session_name": self._session_name_for_run_id(run_root.name),
                "run_id": run_root.name,
                "stdout_path": str(run_root / "stdout.log"),
                "stderr_path": str(run_root / "stderr.log"),
                "exit_path": str(exit_path),
            }
        return None

    @staticmethod
    def _pid_is_live(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # A process owned by another account cannot normally be a Viventium
            # owner, but it is live. The owner PID + heartbeat checks still have
            # to pass before the child is accepted.
            return True
        except OSError:
            return False
        return True

    def _recorded_pid_is_proven_gone(
        self,
        pid: int,
        start_identity: str = "",
    ) -> bool:
        """Accept only affirmative death or a proven PID-incarnation mismatch."""

        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        if self._pid_is_zombie(pid):
            return True
        recorded_identity = str(start_identity or "").strip()
        if not recorded_identity.startswith("ps-lstart:"):
            return False
        current_identity = self._process_start_identity(pid)
        return bool(current_identity and current_identity != recorded_identity)

    @staticmethod
    def _process_start_identity(pid: int) -> str:
        """Return a stable identity for one PID incarnation, not merely the PID."""

        if pid <= 0:
            return ""
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        started = " ".join(completed.stdout.split())
        return f"ps-lstart:{started}" if completed.returncode == 0 and started else ""

    @staticmethod
    def _process_group_identity(pid: int) -> int:
        """Capture a new-session process group without failing on a fast exit."""

        try:
            return os.getpgid(pid)
        except (OSError, ProcessLookupError):
            # Host subprocesses are always launched with start_new_session=True,
            # so their initial PGID is their PID. A missing start identity keeps
            # this fallback from ever authorizing a later kill of a reused PID.
            return pid

    @staticmethod
    def _pid_is_zombie(pid: int) -> bool:
        try:
            completed = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        state = completed.stdout.strip().upper()
        return completed.returncode == 0 and state.startswith("Z")

    def _recorded_process_is_running(self, pid: int, start_identity: str) -> bool:
        if pid <= 0 or not start_identity or not self._pid_is_live(pid) or self._pid_is_zombie(pid):
            return False
        return self._process_start_identity(pid) == start_identity

    def _wait_for_recorded_process_exit(
        self,
        pid: int,
        start_identity: str,
        *,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._recorded_process_is_running(pid, start_identity):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    @staticmethod
    def _active_session_heartbeat_stale_seconds() -> float:
        raw = os.environ.get("GLASSHIVE_ACTIVE_SESSION_HEARTBEAT_STALE_S", "").strip()
        try:
            parsed = float(raw) if raw else 20.0
        except ValueError:
            parsed = 20.0
        return max(10.0, parsed)

    def _durable_active_session_pid(
        self,
        worker_id: str,
        *,
        expected_run_id: str | None = None,
    ) -> int | None:
        active_session = self._read_active_session(worker_id)
        recorded_run_id = str((active_session or {}).get("run_id") or "").strip()
        if not active_session or not recorded_run_id:
            return None
        if expected_run_id and recorded_run_id != expected_run_id:
            return None
        try:
            recorded_pid = int(active_session.get("process_pid") or 0)
            owner_pid = int(active_session.get("owner_pid") or 0)
        except (TypeError, ValueError):
            return None
        recorded_start_identity = str(
            active_session.get("process_start_identity") or ""
        ).strip()
        recorded_identity_sha256 = str(
            active_session.get("process_identity_sha256") or ""
        ).strip()
        has_owner_lease_metadata = bool(
            int(active_session.get("owner_pid") or 0)
            or str(active_session.get("heartbeat_path") or "").strip()
        )
        if (
            not has_owner_lease_metadata
            and recorded_pid > 0
            and recorded_start_identity
            and recorded_identity_sha256
            and self._recorded_process_is_running(
                recorded_pid, recorded_start_identity
            )
        ):
            current_identity_sha256 = self._process_identity_sha256(recorded_pid)
            if current_identity_sha256 and hmac.compare_digest(
                current_identity_sha256,
                recorded_identity_sha256,
            ):
                return recorded_pid
        # The child may have exited while its live owner is parsing and committing
        # the terminal result. A fresh owner heartbeat is the short finalization
        # lease; child liveness alone is neither necessary nor sufficient.
        if recorded_pid <= 0 or not self._pid_is_live(owner_pid):
            return None

        if recorded_start_identity and not self._recorded_process_is_running(
            recorded_pid, recorded_start_identity
        ):
            return None

        raw_heartbeat_path = str(active_session.get("heartbeat_path") or "").strip()
        if not raw_heartbeat_path:
            return None
        try:
            heartbeat = json.loads(Path(raw_heartbeat_path).read_text())
            heartbeat_run_id = str(heartbeat.get("run_id") or "").strip()
            heartbeat_state = str(heartbeat.get("state") or "").strip()
            heartbeat_pid = int(heartbeat.get("process_pid") or 0)
            heartbeat_at = datetime.fromisoformat(
                str(heartbeat.get("last_heartbeat_at") or "").replace("Z", "+00:00")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        age_seconds = max(
            0.0,
            datetime.now(timezone.utc).timestamp() - heartbeat_at.astimezone(timezone.utc).timestamp(),
        )
        if (
            heartbeat_run_id != recorded_run_id
            or heartbeat_state != "running"
            or heartbeat_pid != recorded_pid
            or age_seconds > self._active_session_heartbeat_stale_seconds()
        ):
            return None
        return recorded_pid

    def _active_pid(self, worker_id: str, expected_run_id: str | None = None) -> int | None:
        with self._process_lock:
            process = self._active_processes.get(worker_id)
            if process and process.poll() is None:
                if not expected_run_id:
                    return process.pid
                active_session = self._read_active_session(worker_id)
                if str((active_session or {}).get("run_id") or "").strip() == expected_run_id:
                    return process.pid
                return None
        # Host CLI runs are owned by the service process that launched them, but
        # reconciliation can run in another service process sharing the same
        # runtime root and database. The active-session record is the durable
        # cross-process ownership signal; do not orphan a run whose recorded host
        # process is still alive merely because it is absent from this instance's
        # in-memory Popen map.
        return self._durable_active_session_pid(
            worker_id,
            expected_run_id=expected_run_id,
        )

    def _note_stop_reason(
        self,
        worker_id: str,
        reason: str,
        run_id: str | None = None,
        *,
        attempt_id: str = "",
    ) -> None:
        with self._process_lock:
            self._stop_reasons[(worker_id, run_id, str(attempt_id or ""))] = reason

    def _pop_stop_reason(
        self,
        worker_id: str,
        run_id: str | None = None,
        *,
        attempt_id: str = "",
    ) -> str | None:
        with self._process_lock:
            clean_attempt_id = str(attempt_id or "")
            if run_id is not None and clean_attempt_id:
                return self._stop_reasons.pop(
                    (worker_id, run_id, clean_attempt_id), None
                )
            if run_id is not None:
                reason = self._stop_reasons.pop((worker_id, run_id, ""), None)
                if reason:
                    return reason
            return self._stop_reasons.pop((worker_id, None, ""), None)

    def _peek_stop_reason(self, worker_id: str, run_id: str | None = None) -> str | None:
        with self._process_lock:
            if run_id is not None:
                reason = self._stop_reasons.get((worker_id, run_id, ""))
                if reason:
                    return reason
            return self._stop_reasons.get((worker_id, None, ""))

    def detach_worker(self, worker: dict, run_id: str | None = None) -> bool:
        """Leave the exact live session running; only this process's wait on it ends.

        Unlike ``interrupt_worker`` this never signals the run's processes, never stops the
        screen session, and never clears the active-session record, so a restarted service
        can verify the same generation identity and collect its transcript exactly once.
        """
        worker_id = str(worker.get("worker_id") or "").strip()
        active_run_id = str(run_id or worker.get("_active_run_id") or "").strip() or None
        if not worker_id or not active_run_id:
            return False
        active_session = self._read_active_session(worker_id)
        if not active_session or str(active_session.get("run_id") or "") != active_run_id:
            return False
        self._note_stop_reason(worker_id, "detached", run_id=active_run_id)
        return True

    def _register_process(self, worker_id: str, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._active_processes[worker_id] = process

    def _clear_process(
        self,
        worker_id: str,
        *,
        expected_process: subprocess.Popen[str] | None = None,
    ) -> bool:
        with self._process_lock:
            if (
                expected_process is not None
                and self._active_processes.get(worker_id) is not expected_process
            ):
                return False
            self._active_processes.pop(worker_id, None)
            return True

    def _stop_active_process(
        self,
        worker_id: str,
        *,
        worker: dict | None = None,
        run_id: str | None = None,
        allow_stale_terminal_session: bool = False,
    ) -> bool:
        expected_container_id = str(
            (worker or {}).get("_compute_release_container_id") or ""
        ).strip()
        exact_container_absence = bool(
            worker is not None
            and "_compute_release_container_id" in worker
            and not expected_container_id
        )
        active_session = self._read_active_session(worker_id)
        if active_session and run_id and active_session.get("run_id") != run_id:
            active_session = None
        # A close already owns the whole member-compute teardown. Looking for a
        # historical run when none is active can call list_screen_sessions(),
        # which prepares the workspace again after the close intent was stored.
        closing_without_run = bool(
            worker
            and not run_id
            and str(worker.get("state") or "") in {"terminating", "termination_failed"}
        )
        if closing_without_run:
            # The confirmed container teardown below owns every process in an
            # idle workspace. A saved terminal session may belong to a run
            # that already ended; probing it can hang when Pause suspended
            # that session and needlessly block the container teardown.
            active_session = None
        if not active_session and not exact_container_absence and not closing_without_run:
            active_session = self._infer_active_session(worker or {"worker_id": worker_id}, run_id=run_id)
        if not active_session and run_id and not exact_container_absence:
            active_session = self._run_payload(worker_id, run_id)
        stop_errors: list[Exception] = []
        if exact_container_absence:
            # A saved screen receipt cannot prove a live process after the
            # captured container was confirmed absent. Still stop any local
            # process below; the sandbox will verify absence before release.
            active_session = None
        if active_session:
            try:
                self.sandbox.stop_screen_session(
                    worker_id,
                    self.runtime_name,
                    active_session["session_name"],
                    worker=worker,
                    missing_ok=True,
                    **(
                        {"expected_container_id": expected_container_id}
                        if expected_container_id
                        else {}
                    ),
                )
            except Exception as exc:
                stop_errors.append(exc)
            try:
                self.sandbox.terminate_run_processes(
                    worker_id,
                    self.runtime_name,
                    active_session["run_id"],
                    worker=worker,
                    missing_ok=True,
                    **(
                        {"expected_container_id": expected_container_id}
                        if expected_container_id
                        else {}
                    ),
                )
            except Exception as exc:
                stop_errors.append(exc)
            if not stop_errors:
                self._clear_active_session(worker_id)
        with self._process_lock:
            process = self._active_processes.get(worker_id)
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    stop_errors.append(exc)
            except OSError as exc:
                if process.poll() is None:
                    stop_errors.append(exc)
            if process.poll() is None:
                stop_errors.append(RuntimeError(f"Host process {process.pid} remained alive after TERM/KILL"))
            else:
                self._clear_process(worker_id)
        if stop_errors:
            detail = "; ".join(str(exc) or type(exc).__name__ for exc in stop_errors)
            raise RuntimeError(f"Failed to stop active {self.runtime_name} run: {detail}")

    def _runtime_info(self, worker: dict, *, pid: int | None = None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        self._ensure_dirs(worker_id)
        session_key = None
        if not self._provider_session_starts_fresh(worker):
            session_key = (
                self._read_session_key(worker_id)
                or worker.get("session_key")
                or self._default_session_key(worker)
            )
            self._remember_native_session_key(worker, session_key)
        return RuntimeInfo(
            runtime=self.runtime_name,
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=session_key,
            state_dir=str(self._state_dir(worker_id)),
            workspace_dir=str(self._workspace_dir(worker_id)),
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        fast_sandbox = (
            None
            if self._uses_parallel_clean_room(worker)
            else getattr(
                self.sandbox, "fast_sandbox_from_worker", lambda _worker: None
            )(worker)
        )
        sandbox = fast_sandbox or self.sandbox.ensure_ready(worker, self.runtime_name)
        return self._runtime_info(worker, pid=sandbox.pid)

    def prepare_worker_workspace(self, worker: dict) -> RuntimeInfo:
        """Materialize private worker directories without starting container compute."""
        return self._runtime_info(worker, pid=None)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        expected_container_id = str(
            worker.get("_compute_release_container_id") or ""
        ).strip()
        sandbox = (
            self.sandbox.pause(
                worker["worker_id"],
                expected_container_id=expected_container_id,
            )
            if expected_container_id
            else self.sandbox.pause(worker["worker_id"])
        )
        if str(sandbox.state or "").lower() != "paused":
            raise RuntimeErrorBase("Docker pause could not be confirmed")
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        active_run_id = str(run_id or worker.get("_active_run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker_id):
            self._note_stop_reason(worker_id, "interrupted", run_id=active_run_id)
            self._stop_active_process(worker_id, worker=worker, run_id=active_run_id)
        return self._runtime_info(worker, pid=None)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        active_run_id = str(worker.get("_active_run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker_id):
            self._note_stop_reason(worker_id, "terminated", run_id=active_run_id)
        self._stop_active_process(worker_id, worker=worker, run_id=active_run_id)
        if "_compute_release_container_id" in worker:
            captured_id = str(worker.get("_compute_release_container_id") or "").strip()
            self.sandbox.terminate(
                worker_id,
                expected_container_id=captured_id or None,
                expected_absent=not bool(captured_id),
            )
        else:
            self.sandbox.terminate(worker_id)
        self._clear_active_session(worker_id)
        return self._runtime_info(worker, pid=None)

    def release_provider_account_binding(self, worker: dict) -> None:
        """Remove the container that carries a mission-scoped credential bind mount."""
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            raise RuntimeErrorBase("Provider credential cleanup requires a worker id")
        try:
            self._stop_active_process(
                worker_id,
                worker=worker,
                run_id=str(worker.get("_active_run_id") or "") or None,
            )
        except Exception:
            # Completed-run metadata can outlive its screen process.  A stale
            # graceful-stop failure must never prevent removal of the whole
            # credential-bearing container, which is the authoritative fence.
            logger.warning(
                "Provider-bound worker process cleanup was stale; removing its sandbox",
                extra={"worker_id": worker_id, "runtime": self.runtime_name},
            )
        self.sandbox.terminate(worker_id)
        self._clear_active_session(worker_id)
        raw_account_home = str(worker.get("_glasshive_provider_account_mount_host") or "").strip()
        if not raw_account_home:
            raise RuntimeErrorBase("Provider credential cleanup requires its private account home")
        self.sandbox.repair_provider_account_access(Path(raw_account_home))

    def reconcile_provider_account_binding(self, account_home: Path) -> None:
        self.sandbox.terminate_containers_mounting_provider_account(account_home)
        self.sandbox.repair_provider_account_access(account_home)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        sandbox = self.sandbox.inspect(worker["worker_id"])
        active_run_id = str(worker.get("_active_run_id") or "").strip()
        if active_run_id:
            active_session = self._read_active_session(worker["worker_id"])
            if (
                active_session
                and str(active_session.get("run_id") or "") == active_run_id
                and bool(active_session.get("termination_unconfirmed"))
            ):
                pending_pid = (
                    sandbox.pid
                    if sandbox and str(sandbox.state or "").lower() == "running"
                    else None
                )
                return self._runtime_info(worker, pid=pending_pid)
            identity = self.host_process_identity(worker, active_run_id)
            pid = (
                int(identity.get("pid") or 0) or None
                if identity and bool(identity.get("verified"))
                else None
            )
        else:
            pid = sandbox.pid if sandbox and sandbox.state == "running" else None
        return self._runtime_info(worker, pid=pid)

    def worker_compute_present(self, worker: dict) -> bool:
        return self.sandbox.inspect(worker["worker_id"]) is not None

    def _log_paths(self, worker_id: str) -> tuple[Path, Path]:
        return (
            self.logs_dir / f"{worker_id}.stdout.log",
            self.logs_dir / f"{worker_id}.stderr.log",
        )

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError

    def _wait_for_exit_code(
        self,
        worker_id: str,
        exit_path: Path,
        timeout_sec: float | None,
        run_id: str | None = None,
        stdout_path: Path | None = None,
    ) -> int:
        deadline = time.monotonic() + float(timeout_sec) if timeout_sec and timeout_sec > 0 else None
        completed_seen_at: float | None = None
        early_grace_sec = self._early_completion_grace_sec()
        raw_inspect_interval = os.environ.get("WPR_RUN_WAIT_INSPECT_INTERVAL_SEC", "10").strip()
        try:
            inspect_interval_sec = max(float(raw_inspect_interval), 0.0) if raw_inspect_interval else 10.0
        except ValueError:
            inspect_interval_sec = 10.0
        next_inspect_at = 0.0
        paused = False
        while True:
            completed_exit_code = self._read_completed_exit_code(exit_path)
            if completed_exit_code is not None:
                return completed_exit_code
            if self._peek_stop_reason(worker_id, run_id=run_id) == "detached":
                self._pop_stop_reason(worker_id, run_id=run_id)
                raise WorkerDetachedError(
                    "Worker run detached for a managed restart; its generation keeps running"
                )
            if stdout_path and self._stdout_has_complete_response(stdout_path):
                now = time.monotonic()
                if completed_seen_at is None:
                    completed_seen_at = now
                elif now - completed_seen_at >= early_grace_sec:
                    exit_path.write_text("0")
                    self._stop_active_process(worker_id, run_id=run_id)
                    return 0
            else:
                completed_seen_at = None
            now = time.monotonic()
            if inspect_interval_sec == 0 or now >= next_inspect_at:
                sandbox = self.sandbox.inspect(worker_id)
                paused = bool(sandbox and sandbox.state == "paused")
                next_inspect_at = now + inspect_interval_sec
            if paused:
                time.sleep(0.25)
                continue
            time.sleep(0.25)
            if deadline is not None and time.monotonic() >= deadline:
                break
        self._note_stop_reason(worker_id, "terminated", run_id=run_id)
        self._stop_active_process(worker_id, run_id=run_id)
        raise RuntimeErrorBase(f"{self.runtime_name} timed out after {timeout_sec}s")

    def _early_completion_grace_sec(self) -> float:
        raw = (
            os.environ.get("GLASSHIVE_EARLY_COMPLETION_GRACE_SEC", "").strip()
            or os.environ.get("WPR_EARLY_COMPLETION_GRACE_SEC", "").strip()
        )
        if not raw:
            return 1.5
        try:
            parsed = float(raw)
        except ValueError:
            return 1.5
        return max(parsed, 0.0)

    def _stdout_has_complete_response(self, stdout_path: Path) -> bool:
        _ = stdout_path
        return False

    def _run_timeout_sec(
        self,
        timeout_sec: float | None = None,
        *,
        declared_long: bool = False,
    ) -> float | None:
        raw = os.environ.get("GLASSHIVE_RUN_TIMEOUT_SEC", "").strip()
        if not raw and not declared_long:
            raw = os.environ.get("GLASSHIVE_MAX_RUN_DURATION_S", "").strip()
        if not raw:
            raw = os.environ.get("WPR_RUN_TIMEOUT_SEC", "").strip()
        if not raw:
            return timeout_sec if timeout_sec and timeout_sec > 0 else None
        if raw.lower() in {"0", "none", "off", "false", "disabled"}:
            return None
        try:
            parsed = float(raw)
        except ValueError:
            return timeout_sec if timeout_sec and timeout_sec > 0 else None
        return parsed if parsed > 0 else None

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        raise NotImplementedError

    def _bootstrap_env_value(self, worker: dict, name: str) -> str:
        try:
            bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
        except json.JSONDecodeError:
            return ""
        if not isinstance(bundle, dict):
            return ""
        env = bundle.get("env")
        if not isinstance(env, dict):
            return ""
        return str(env.get(name) or "").strip()

    def _container_env(self, *keys: str) -> dict[str, str]:
        env: dict[str, str] = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "SHELL": "/bin/bash",
            "USER": "worker",
            "LOGNAME": "worker",
        }
        if os.environ.get("LANG"):
            env["LANG"] = str(os.environ["LANG"])
        for key, value in os.environ.items():
            if key.startswith("LC_"):
                env[key] = value
        for key in keys:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    @staticmethod
    def _uses_parallel_clean_room(worker: dict) -> bool:
        return (
            str(bootstrap_bundle_for(worker).get("execution_policy") or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        )

    def _container_env_for_worker(
        self, worker: dict, *legacy_ambient_keys: str
    ) -> dict[str, str]:
        if hasattr(self.sandbox, "box"):
            # Shared native homes use explicit account/tool projections. Ambient host
            # provider credentials must not cross this boundary before the guard.
            return {**self.sandbox._desktop_env(),
                    "USER": f"member-{self.sandbox.box.binding.uid}",
                    "LOGNAME": f"member-{self.sandbox.box.binding.uid}",
                    **{key: value for key, value in os.environ.items() if key == "LANG" or key.startswith("LC_")}}
        if not self._uses_parallel_clean_room(worker):
            return self._container_env(*legacy_ambient_keys)

        configuration, reason = self.sandbox._parallel_clean_room_configuration(
            require_proxy_containers=False
        )
        if configuration is None:
            raise RuntimeErrorBase(
                "Parallel clean-room provider proxy configuration is unavailable "
                f"({reason})"
            )
        # Automatic missions receive no ambient provider login/key/base-URL
        # authority. The internal network and its attested egress proxy own the
        # provider route; the capability grant is loaded from the run-only
        # private secret projection inside the script, never a docker-exec arg.
        env = self._container_env()
        provider_hostname = str(configuration["provider_proxy_hostname"])
        env.update(
            {
                "HTTP_PROXY": configuration["provider_proxy_url"],
                "HTTPS_PROXY": configuration["provider_proxy_url"],
                "NO_PROXY": (
                    f"{provider_hostname},host.docker.internal,localhost,127.0.0.1"
                ),
            }
        )
        return env

    def _parallel_clean_room_provider_base_url(
        self, worker: dict, provider: str
    ) -> str:
        if not self._uses_parallel_clean_room(worker):
            return ""
        configuration, reason = self.sandbox._parallel_clean_room_configuration(
            require_proxy_containers=False
        )
        if configuration is None:
            raise RuntimeErrorBase(
                "Parallel clean-room provider proxy configuration is unavailable "
                f"({reason})"
            )
        provider_path = {
            "openai": "openai/v1",
            "anthropic": "anthropic",
        }.get(str(provider or "").strip().lower())
        if not provider_path:
            raise RuntimeErrorBase("Parallel clean-room provider route is unsupported")
        return f"{str(configuration['provider_proxy_url']).rstrip('/')}/{provider_path}"

    def terminal_target(self, worker: dict) -> TerminalTarget:
        self.ensure_worker_ready(worker)
        active_session = self._infer_active_session(worker)
        session_name = str((active_session or {}).get("session_name") or "operator").strip() or "operator"
        return TerminalTarget(
            command=self.sandbox.terminal_attach_command(worker["worker_id"], self.runtime_name, session_name=session_name),
            cwd=str(self._workspace_dir(worker["worker_id"])),
            title=f"{worker['name']} live session" if active_session else f"{worker['name']} terminal",
            subtitle=f"{self.runtime_name} active run" if active_session else f"{self.runtime_name} sandbox",
        )

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
        on_exit: Callable[[], None] | None = None,
        max_runtime_seconds: float | None = None,
    ) -> dict[str, object]:
        session_name = self._session_name_for_run_id(run_id) if action == "terminal" and run_id else None
        session_options: dict[str, object] = {}
        if on_exit is not None:
            session_options.update(
                {
                    "on_exit": on_exit,
                    "max_runtime_seconds": max_runtime_seconds,
                }
            )
        launched = self.sandbox.desktop_action(
            worker["worker_id"],
            self.runtime_name,
            action,
            url=url,
            session_name=session_name,
            worker=worker,
            **session_options,
        )
        notes = {
            "terminal": (
                "Opened the exact live worker terminal session inside the workstation desktop."
                if session_name
                else "Opened a workstation shell inside the worker sandbox."
            ),
            "files": "Opened the workspace file manager inside the worker sandbox.",
            "browser": "Opened the sandbox browser in the live workstation.",
            "focus_browser": "Tried to raise the existing browser window to the front.",
            "codex": "Opened an interactive Codex CLI window inside the worker sandbox.",
            "claude": "Opened an interactive Claude Code window inside the worker sandbox.",
            "openclaw": "Opened an interactive OpenClaw terminal surface inside the worker sandbox.",
        }
        return {
            "action": action,
            "status": "launched",
            "mode": "workstation-desktop",
            "url": launched.get("view_url"),
            "view_url": launched.get("view_url"),
            "notes": notes.get(action, "Opened the requested workstation surface."),
        }

    def describe_worker(self, worker: dict) -> dict[str, object]:
        sandbox = self.sandbox.describe(worker["worker_id"])
        return {
            "mode": "workstation-desktop" if sandbox.get("view_url") else "docker-workstation",
            "runtime": self.runtime_name,
            "workspace_dir": sandbox["workspace_dir"],
            "home_dir": sandbox["home_dir"],
            "container_name": sandbox["container_name"],
            "container_id": sandbox["container_id"],
            "sandbox_state": sandbox["state"],
            "sandbox_image": sandbox["image"],
            "view_url": sandbox.get("view_url"),
            "view_available": bool(sandbox.get("view_available") or sandbox.get("view_url")),
            "view_health": sandbox.get("view_health"),
            "novnc_port": sandbox.get("novnc_port"),
            "selenium_port": sandbox.get("selenium_port"),
            "openclaw_port": sandbox.get("openclaw_port"),
            "desktop_prime": sandbox.get("desktop_prime"),
            "pid": sandbox["pid"],
        }

    def _active_session_argv_for_evidence(self, active_session: dict[str, object] | None) -> list[str]:
        raw = str((active_session or {}).get("argv_for_evidence_json") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(item or "") for item in parsed]
            except json.JSONDecodeError:
                pass
        return [self.runtime_name]

    def _route_failure_evidence_for_collected_run(
        self,
        *,
        worker: dict,
        run_id: str,
        classification,
    ) -> dict[str, object] | None:
        """Provider-native structured capacity evidence for a collected (non-host) run.

        Mirrors the host process-exit path so a docker/base run that exhausts its provider quota
        also records route health and can switch to the configured fallback worker.
        """
        if not (
            classification.structured
            and classification.failure_class in {"provider_quota_exhausted", "provider_rate_limited"}
            and classification.provider_event_source
        ):
            return None
        evidence_material = json.dumps(
            {
                "worker_id": str(worker.get("worker_id") or ""),
                "run_id": str(run_id or ""),
                "runtime": self.runtime_name,
                "source": classification.provider_event_source,
                "failure_class": classification.failure_class,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "version": 1,
            "failure_class": classification.failure_class,
            "failure_structured": True,
            "retry_at": "",
            "retry_after_s": classification.retry_after_s,
            "evidence_kind": classification.provider_event_source,
            "evidence_id": hashlib.sha256(evidence_material.encode("utf-8")).hexdigest(),
        }

    def collect_completed_run(
        self,
        worker: dict,
        run_id: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, object] | None:
        attempt_id = str(worker.get("_run_attempt_id") or "").strip()
        active_session = self._latest_completed_run_payload(
            worker["worker_id"],
            run_id=run_id,
            attempt_id=attempt_id,
        )
        if not active_session:
            return None
        exit_path = Path(str(active_session.get("exit_path") or "").strip())
        stdout_path = Path(str(active_session.get("stdout_path") or "").strip())
        stderr_path = Path(str(active_session.get("stderr_path") or "").strip())
        exit_code = self._read_completed_exit_code(exit_path)
        if exit_code is None:
            if not self._stdout_has_complete_response(stdout_path):
                return None
            effective_run_id = str(run_id or active_session.get("run_id") or "").strip()
            if self._stop_active_process(
                worker["worker_id"], worker=worker, run_id=effective_run_id
            ) is False:
                # A final response is not proof that its native generation stopped.
                # Do not publish a synthetic exit marker that a later collector could trust.
                return None
            try:
                exit_path.write_text("0")
            except OSError:
                return None
            exit_code = 0
        stdout = stdout_path.read_text() if stdout_path.exists() else ""
        stderr = stderr_path.read_text() if stderr_path.exists() else ""
        effective_run_id = str(run_id or active_session.get("run_id") or "").strip()
        usage, telemetry = self._record_run_metrics(worker["worker_id"], effective_run_id, stdout)
        if exit_code != 0:
            effective_run_id = str(
                run_id or active_session.get("run_id") or ""
            ).strip()
            classification, provider_evidence = self._provider_failure_for_process(
                worker=worker,
                run_id=effective_run_id,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
            )
            route_failure_evidence = self._route_failure_evidence_for_collected_run(
                worker=worker,
                run_id=effective_run_id,
                classification=classification,
            )
            failure_fields = classification.as_store_fields()
            if classification.personal_account_reconnect:
                failure_fields[_PERSONAL_ACCOUNT_RECONNECT_MARKER] = True
            if self.runtime_name == "claude-code":
                # stream-json stdout is the full model/tool transcript. It remains in the
                # operator-private run files, but must never become a durable/public job error.
                detail = classification.user_message
                failure_fields["failure_diagnostic_summary"] = compact_provider_failure_diagnostic(
                    stdout=stdout, stderr=stderr,
                    classification=classification, exit_code=exit_code,
                )
            else:
                detail = _redact_text((stderr or stdout or "").strip(), max_chars=2000)
            recovered = {
                "state": "failed",
                "output_text": "",
                "error_text": _redact_text(f"{self.runtime_name} exited with code {exit_code}: {detail}"),
                "usage": usage,
                "telemetry": telemetry,
                **failure_fields,
            }
            if classification.retry_after_s is not None:
                recovered["provider_retry_after_s"] = classification.retry_after_s
            if route_failure_evidence is not None:
                self._issue_provider_route_failure_evidence(
                    worker=worker,
                    run_id=effective_run_id,
                    source=recovered,
                    evidence=route_failure_evidence,
                )
            return recovered
        info = self.reconcile_worker(worker)
        try:
            session_key, output = self._parse_output(worker, stdout, stderr, info)
        except RuntimeErrorBase as exc:
            return {
                "state": "failed",
                "output_text": "",
                "error_text": str(exc),
                "usage": usage,
                "telemetry": telemetry,
            }
        self._remember_native_session_key(worker, session_key)
        workspace = Path(str(info.workspace_dir or self._workspace_dir(worker["worker_id"])))
        warning_message = ""
        run_mode = str(active_session.get("run_mode") or "").strip().lower()
        conversation_recovery = run_mode == "conversation" or bool(
            getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker)
        )
        if conversation_recovery and isinstance(self, HostNativeCliMixin):
            output = _redact_text(self._project_native_image_output(
                worker, info, output, active_session.get("native_image_scope"), effective_run_id
            ))
        if not conversation_recovery:
            try:
                _status, warning_message = _ensure_recovered_success_evidence(
                    worker=worker,
                    run_id=effective_run_id,
                    runtime_name=self.runtime_name,
                    model=str(active_session.get("model") or worker.get("model") or self.resolve_model(str(worker.get("profile") or ""))),
                    command=self._active_session_argv_for_evidence(active_session),
                    workspace=workspace,
                    stdout_text=stdout,
                    stderr_text=stderr,
                    output_text=output,
                    exit_code=exit_code,
                    active_session=active_session,
                    instruction=str(instruction or "").strip(),
                )
            except RuntimeErrorBase as exc:
                classification = classify_runtime_error(exc, runtime_name=self.runtime_name)
                return {
                    "state": "failed",
                    "output_text": "",
                    "error_text": str(exc),
                    **classification.as_store_fields(),
                }
        output_text = output.strip()
        if warning_message:
            suffix = f"\n\n{warning_message}"
            if len(output_text) + len(suffix) <= _HOST_RUN_OUTPUT_MAX_CHARS:
                output_text = f"{output_text}{suffix}"
        return {
            "state": "completed",
            "output_text": output_text,
            "error_text": "",
            "usage": usage,
            "telemetry": telemetry,
        }

    def _finalize_stop_reason(
        self,
        worker_id: str,
        run_id: str | None = None,
        *,
        attempt_id: str = "",
    ) -> None:
        reason = self._pop_stop_reason(
            worker_id, run_id=run_id, attempt_id=attempt_id
        )
        if reason == "paused":
            raise WorkerPausedError("Worker was paused while a run was active")
        if reason == "interrupted":
            raise WorkerInterruptedError("Worker run was interrupted by the operator")
        if reason == "terminated":
            raise WorkerTerminatedError("Worker was terminated while a run was active")

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        worker_for_run = {
            **worker,
            "_active_run_id": effective_run_id,
            "_glasshive_task_run": True,
        }
        clean_room = self._uses_parallel_clean_room(worker_for_run)
        attempt_id = str(worker_for_run.get("_run_attempt_id") or "").strip()
        info = self.ensure_worker_ready(worker_for_run)
        run_secret_paths: dict[str, str] | None = None
        run_secret_container_id = ""
        if clean_room:
            run_sandbox_inspection = self.sandbox.inspect_fresh(
                worker_for_run["worker_id"]
            )
            run_sandbox = run_sandbox_inspection.sandbox
            if (
                run_sandbox_inspection.status != "present"
                or run_sandbox is None
                or not str(run_sandbox.container_id or "").strip()
            ):
                raise RuntimeErrorBase(
                    "Parallel clean-room sandbox generation is unavailable for run authority"
                )
            run_secret_container_id = str(run_sandbox.container_id or "").strip()
            binding = worker_for_run.get("_run_local_capability_binding")
            bound_container_id = str(
                binding.get("containerGenerationId")
                if isinstance(binding, dict)
                else ""
            ).strip()
            if (
                not re.fullmatch(r"[a-f0-9]{64}", bound_container_id)
                or bound_container_id != run_secret_container_id
            ):
                raise RuntimeErrorBase(
                    "The run-local capability grant does not match the exact sandbox generation"
                )
            secret_root = f"/run/glasshive/{effective_run_id}"
            run_secret_paths = {
                "env_file": f"{secret_root}/secret-runtime.env",
                "keys_file": f"{secret_root}/secret-runtime.keys",
            }
        refresh_runtime_env_for_worker(
            self._home_dir(worker_for_run["worker_id"]),
            worker_for_run,
            self.runtime_name,
        )
        workspace = Path(str(info.workspace_dir or self._workspace_dir(worker_for_run["worker_id"])))
        refresh_project_runtime_files_for_worker(
            self._home_dir(worker_for_run["worker_id"]),
            workspace,
            worker_for_run,
        )
        command, env = self._build_command(worker_for_run, instruction, info)
        apply_bound_provider_account_environment(
            worker_for_run,
            env,
            runtime_name=self.runtime_name,
        )
        authority_receipt = self._native_provider_authority_receipt(
            worker_for_run,
            command=command,
            run_id=effective_run_id,
            model=str(info.model or ""),
        )
        stdin_text = self._command_stdin_text(worker_for_run, instruction, info)
        constraint_ledger, constraint_ledger_path = _write_constraint_ledger_for_run(
            worker=worker_for_run,
            instruction=instruction,
            workspace=workspace,
            run_id=effective_run_id,
        )
        stdout_path, stderr_path = self._log_paths(worker_for_run["worker_id"])
        with stderr_path.open("a") as handle:
            handle.write(f"$ {self.runtime_name} {_redacted_command_display(command)}\n")

        run_root = self._attempt_run_root(
            worker_for_run["worker_id"], effective_run_id, attempt_id
        )
        run_root.mkdir(parents=True, exist_ok=True)
        run_root.chmod(0o700)
        if authority_receipt is not None and self._native_provider_authority_receipt_path(
            str(worker_for_run["worker_id"]),
            effective_run_id,
            attempt_id=attempt_id,
        ).exists():
            raise RuntimeErrorBase(
                "A direct Worker native receipt already exists for this exact run"
            )

        host_stdout = run_root / "stdout.log"
        host_stderr = run_root / "stderr.log"
        host_exit = run_root / "exit_code"
        host_script = run_root / "run.sh"
        host_stdin = run_root / "instruction.stdin"
        # Preserve host ownership across the bind mount so the verifier can read
        # worker output without widening access to workspace or profile data.
        # The worker-specific ACL repairs below grant the container user write
        # access while the empty exit marker remains incomplete until populated.
        for transcript_path in (host_stdout, host_stderr, host_exit):
            transcript_path.write_text("")
            transcript_path.chmod(0o600)
        native_session_initial_offset = host_stdout.stat().st_size

        container_run_root = self._attempt_container_run_root(
            effective_run_id, attempt_id
        )
        container_stdout = f"{container_run_root}/stdout.log"
        container_stderr = f"{container_run_root}/stderr.log"
        container_exit = f"{container_run_root}/exit_code"
        container_script = f"{container_run_root}/run.sh"
        container_stdin = f"{container_run_root}/instruction.stdin"
        session_name = f"job-{effective_run_id[:12]}"
        if stdin_text is not None:
            host_stdin.write_text(stdin_text)
            host_stdin.chmod(0o600)
        command_invocation = shlex.join(command)
        if stdin_text is not None:
            command_invocation = f"{command_invocation} < {shlex.quote(container_stdin)}"
        response_deadline_at = str(
            worker_for_run.get("_provider_response_deadline_at") or ""
        ).strip()
        deadline_check = (
            "python3 -I -c "
            + shlex.quote(
                "import datetime as d,sys; "
                "t=d.datetime.fromisoformat(sys.argv[1]); "
                "t=t if t.tzinfo else t.replace(tzinfo=d.timezone.utc); "
                "sys.exit(0 if d.datetime.now(d.timezone.utc)<t else 75)"
            )
            + " " + shlex.quote(response_deadline_at)
            + " || abort_run 75"
            if response_deadline_at else ""
        )

        script = "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -o pipefail",
                "umask 077",
                f"mkdir -p {shlex.quote(container_run_root)}",
                (
                    "write_exit() { "
                    f"if [ ! -s {shlex.quote(container_exit)} ]; then "
                    f"printf '%s' \"$1\" > {shlex.quote(container_exit)}; "
                    "fi; "
                    "}"
                ),
                (
                    "GLASSHIVE_SECRET_ENV_KEYS_FILE="
                    + shlex.quote(str(run_secret_paths["keys_file"]))
                    if run_secret_paths is not None
                    else 'GLASSHIVE_SECRET_ENV_KEYS_FILE="$HOME/.glasshive/secret-runtime.keys"'
                ),
                (
                    "GLASSHIVE_SECRET_ENV_FILE="
                    + shlex.quote(str(run_secret_paths["env_file"]))
                    if run_secret_paths is not None
                    else 'GLASSHIVE_SECRET_ENV_FILE="$HOME/.glasshive/secret-runtime.env"'
                ),
                (
                    "GLASSHIVE_SECRET_ENV_DIR="
                    + shlex.quote(str(Path(run_secret_paths["env_file"]).parent))
                    if run_secret_paths is not None
                    else "GLASSHIVE_SECRET_ENV_DIR=''"
                ),
                (
                    "scrub_run_secrets() { "
                    "unset native_key CODEX_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY XAI_API_KEY "
                    "GLASSHIVE_NATIVE_API_KEY_FILE GLASSHIVE_NATIVE_API_KEY_PROVIDER; "
                    'if [ -f "$GLASSHIVE_SECRET_ENV_KEYS_FILE" ]; then '
                    'while IFS= read -r key; do [ -n "$key" ] && unset "$key"; done '
                    '< "$GLASSHIVE_SECRET_ENV_KEYS_FILE"; fi; '
                    'rm -f "$GLASSHIVE_SECRET_ENV_FILE" "$GLASSHIVE_SECRET_ENV_KEYS_FILE"; '
                    'if [ -n "$GLASSHIVE_SECRET_ENV_DIR" ]; then '
                    'rmdir "$GLASSHIVE_SECRET_ENV_DIR" 2>/dev/null || true; fi; '
                    "}"
                ),
                "abort_run() { scrub_run_secrets; write_exit \"${1:-130}\"; exit \"${1:-130}\"; }",
                "trap 'abort_run 130' HUP INT TERM",
                f"cd {shlex.quote(self.sandbox.workspace_mount)} || exit 1",
                f"export GLASSHIVE_ACTIVE_RUN_ID={shlex.quote(effective_run_id)}",
                f"export GLASSHIVE_RUN_ID={shlex.quote(effective_run_id)}",
                f"export GLASSHIVE_ACTIVE_WORKER_ID={shlex.quote(worker_for_run['worker_id'])}",
                'if [ -f "$HOME/.glasshive/runtime.env" ]; then set -a; source "$HOME/.glasshive/runtime.env"; set +a; fi',
                'if [ -f "$GLASSHIVE_SECRET_ENV_FILE" ]; then set -a; source "$GLASSHIVE_SECRET_ENV_FILE"; set +a; rm -f "$GLASSHIVE_SECRET_ENV_FILE"; fi',
                'if [ -n "${GLASSHIVE_NATIVE_API_KEY_FILE:-}" ]; then '
                'native_key="$(python3 -I -m workers_projects_runtime.native_key_reader "$GLASSHIVE_NATIVE_API_KEY_FILE")" || abort_run 1; '
                'case "${GLASSHIVE_NATIVE_API_KEY_PROVIDER:-}" in '
                'codex) export CODEX_API_KEY="$native_key" OPENAI_API_KEY="$native_key" ;; '
                'claude) export ANTHROPIC_API_KEY="$native_key" ;; '
                'grok) export XAI_API_KEY="$native_key" ;; '
                '*) abort_run 1 ;; esac; unset native_key; fi',
                *(
                    [
                        ': "${GLASSHIVE_CAPABILITY_BROKER_TOKEN:?missing run capability grant}"',
                        'export OPENAI_API_KEY="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"',
                        'export ANTHROPIC_API_KEY="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"',
                        'export ANTHROPIC_AUTH_TOKEN="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"',
                        (
                            "export OPENAI_BASE_URL="
                            + shlex.quote(
                                self._parallel_clean_room_provider_base_url(
                                    worker_for_run, "openai"
                                )
                            )
                        ),
                        (
                            "export ANTHROPIC_BASE_URL="
                            + shlex.quote(
                                self._parallel_clean_room_provider_base_url(
                                    worker_for_run, "anthropic"
                                )
                            )
                        ),
                    ]
                    if clean_room
                    else []
                ),
                'if [ -f "$HOME/.wpr-openclaw/openclaw.env" ]; then set -a; source "$HOME/.wpr-openclaw/openclaw.env"; set +a; fi',
                *([deadline_check] if deadline_check else []),
                f"{command_invocation} > >(tee -a {shlex.quote(container_stdout)}) 2> >(tee -a {shlex.quote(container_stderr)} >&2)",
                "status=$?",
                "scrub_run_secrets",
                "write_exit \"$status\"",
                *(
                    [
                        "printf '\\n[glasshive] run finished with exit code %s; credential-free session exiting.\\n' \"$status\"",
                        'exit "$status"',
                    ]
                    if clean_room
                    else [
                        "printf '\\n[glasshive] run finished with exit code %s. Interactive shell remains open for takeover.\\n' \"$status\"",
                        "exec bash --noprofile --norc",
                    ]
                ),
            ]
        )
        host_script.write_text(script + "\n")
        host_script.chmod(0o755)
        container_writable_paths = [container_run_root]
        if authority_receipt is not None:
            authority_directory = (
                ".glasshive" if self.runtime_name == "claude-code" else ".codex"
            )
            container_writable_paths.append(
                f"{self.sandbox.home_mount}/{authority_directory}"
            )
        self.sandbox.ensure_container_writable_paths(
            worker_for_run["worker_id"],
            self.runtime_name,
            container_writable_paths,
            worker=worker_for_run,
        )

        run_authority_projected = False
        with runtime_start_boundary(worker_for_run):
            self._stop_active_process(worker_for_run["worker_id"], worker=worker_for_run)
            if clean_room:
                projected_paths = self.sandbox.project_parallel_clean_room_run_secrets(
                    worker_for_run["worker_id"],
                    expected_container_id=run_secret_container_id,
                    run_id=effective_run_id,
                    env=bootstrap_env_for(worker_for_run),
                )
                if projected_paths != run_secret_paths:
                    raise RuntimeErrorBase(
                        "Parallel clean-room run authority projection path is invalid"
                    )
                run_authority_projected = True
            try:
                start_result = self.sandbox.start_screen_session(
                    worker_for_run["worker_id"],
                    self.runtime_name,
                    session_name,
                    ["bash", "--noprofile", "--norc", container_script],
                    env=env,
                    worker=worker_for_run,
                )
            except Exception:
                if run_authority_projected:
                    self.sandbox.clear_parallel_clean_room_run_secrets(
                        worker_for_run["worker_id"],
                        expected_container_id=run_secret_container_id,
                        run_id=effective_run_id,
                    )
                raise
            if start_result.returncode != 0:
                if run_authority_projected:
                    self.sandbox.clear_parallel_clean_room_run_secrets(
                        worker_for_run["worker_id"],
                        expected_container_id=run_secret_container_id,
                        run_id=effective_run_id,
                    )
                detail = (start_result.stderr or start_result.stdout or "").strip()[-1600:]
                raise RuntimeErrorBase(f"Failed to start attached {self.runtime_name} session: {detail}")
            process_pid = self.sandbox.screen_session_pid(
                worker_for_run["worker_id"],
                self.runtime_name,
                session_name,
                worker=worker_for_run,
            )
            notify_runtime_started(worker_for_run)

        try:
            sandbox_identity = self.sandbox.inspect(worker_for_run["worker_id"])
        except Exception:
            sandbox_identity = None
        container_id = str(
            (getattr(sandbox_identity, "container_id", None) if sandbox_identity else None)
            or ""
        ).strip()
        try:
            screen_pid = int(process_pid or 0)
            container_pid = int(
                (getattr(sandbox_identity, "pid", None) if sandbox_identity else None)
                or 0
            )
        except (TypeError, ValueError):
            screen_pid = 0
            container_pid = 0
        lease_process_identity = (
            f"docker:{container_id}:{session_name}:"
            f"{effective_run_id}:{screen_pid}"
            if container_id and screen_pid > 0
            else ""
        )
        try:
            sandbox_identity = self.sandbox.inspect(worker_for_run["worker_id"])
        except Exception:
            # The owning executor still heartbeats the lease. A transient
            # Docker inspect failure must not abandon an already-started run;
            # restart reconciliation will fail closed unless it can prove the
            # exact container/session later.
            sandbox_identity = None
        container_id = str(
            (getattr(sandbox_identity, "container_id", None) if sandbox_identity else None)
            or ""
        ).strip()
        try:
            screen_pid = int(process_pid or 0)
            container_pid = int(
                (getattr(sandbox_identity, "pid", None) if sandbox_identity else None)
                or 0
            )
        except (TypeError, ValueError):
            screen_pid = 0
            container_pid = 0
        lease_process_identity = (
            f"docker:{container_id}:{session_name}:"
            f"{effective_run_id}:{screen_pid}"
            if container_id and screen_pid > 0
            else ""
        )

        run_timeout_sec = self._run_timeout_sec(
            timeout_sec,
            declared_long=_declared_long_mission(worker_for_run),
        )
        transcript_paths = {
            "stdout": str(host_stdout),
            "stderr": str(host_stderr),
            "exit_code": str(host_exit),
            "constraint_ledger": constraint_ledger_path,
        }
        started_at = time.time()
        started_at_iso = _utc_iso()
        heartbeat_path = _active_run_status_path(workspace, effective_run_id)
        heartbeat_stop = Event()
        heartbeat_thread: Thread | None = None
        native_session_stop = Event()
        native_session_thread: Thread | None = None
        self._write_active_session(
            worker_for_run["worker_id"],
            {
                "session_name": session_name,
                "run_id": effective_run_id,
                "attempt_id": attempt_id,
                "stdout_path": str(host_stdout),
                "stderr_path": str(host_stderr),
                "exit_path": str(host_exit),
                "constraint_ledger_path": constraint_ledger_path,
                "model": str(info.model or ""),
                "argv_for_evidence_json": json.dumps([_redact_command_arg(part) for part in command]),
                "started_at": started_at_iso,
                "process_pid": process_pid,
                "lease_pid": container_pid or screen_pid or None,
                "lease_process_group": container_pid or screen_pid or None,
                "lease_process_start_identity": lease_process_identity,
                "container_id": container_id,
                "owner_pid": os.getpid(),
                "heartbeat_path": str(heartbeat_path),
                "timeout_seconds": run_timeout_sec,
                "instruction": instruction,
            },
            publish_run_start=callable(self._run_start_observer),
            worker=worker_for_run,
        )
        if authority_receipt is not None:
            with self._process_lock:
                self._pending_native_authority_receipts[
                    (
                        str(worker_for_run["worker_id"]),
                        effective_run_id,
                        attempt_id,
                    )
                ] = authority_receipt
        native_session_thread = Thread(
            target=self._observe_native_session_events,
            args=(
                worker_for_run["worker_id"],
                host_stdout,
                native_session_stop,
                effective_run_id,
                str(worker_for_run.get("_run_attempt_id") or ""),
                native_session_initial_offset,
                worker_for_run,
            ),
            name=f"glasshive-docker-native-session-{effective_run_id[:12]}",
            daemon=True,
        )
        native_session_thread.start()
        _write_active_run_status(
            path=heartbeat_path,
            worker=worker_for_run,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            state="running",
            transcript_paths=transcript_paths,
            started_at=started_at_iso,
            process_pid=process_pid,
            timeout_seconds=run_timeout_sec,
        )
        heartbeat_thread = _start_active_run_heartbeat(
            path=heartbeat_path,
            worker=worker_for_run,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            transcript_paths=transcript_paths,
            started_at=started_at_iso,
            process_pid=process_pid,
            timeout_seconds=run_timeout_sec,
            stop_event=heartbeat_stop,
        )
        try:
            exit_code = self._wait_for_exit_code(
                worker_for_run["worker_id"],
                host_exit,
                run_timeout_sec,
                run_id=effective_run_id,
                stdout_path=host_stdout,
            )
        except Exception as exc:
            if isinstance(exc, WorkerDetachedError):
                raise
            stdout = host_stdout.read_text() if host_stdout.exists() else ""
            stderr = host_stderr.read_text() if host_stderr.exists() else ""
            self._record_run_metrics(worker_for_run["worker_id"], effective_run_id, stdout)
            stop_reason = "timeout" if "timed out" in str(exc).lower() else "error"
            evidence_path = _write_evidence_for_run(
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=str(exc),
                exit_code=None,
                timeout_seconds=run_timeout_sec,
                stop_reason=stop_reason,
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _stop_active_run_heartbeat(heartbeat_stop, heartbeat_thread)
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="timeout" if stop_reason == "timeout" else "failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                stop_reason=stop_reason,
                evidence_path=evidence_path,
            )
            raise
        finally:
            native_session_stop.set()
            if native_session_thread:
                native_session_thread.join(timeout=1)
            _stop_active_run_heartbeat(heartbeat_stop, heartbeat_thread)
            if authority_receipt is not None:
                with self._process_lock:
                    self._pending_native_authority_receipts.pop(
                        (
                            str(worker_for_run["worker_id"]),
                            effective_run_id,
                            attempt_id,
                        ),
                        None,
                    )
            if run_authority_projected:
                self.sandbox.clear_parallel_clean_room_run_secrets(
                    worker_for_run["worker_id"],
                    expected_container_id=run_secret_container_id,
                    run_id=effective_run_id,
                )
        if authority_receipt is not None and (
            self.native_provider_authority_receipt(worker_for_run, effective_run_id)
            != authority_receipt
        ):
            raise RuntimeErrorBase(
                "The direct Worker produced no exact provider-native invocation receipt"
            )
        self.sandbox.ensure_container_writable_paths(
            worker_for_run["worker_id"],
            self.runtime_name,
            [self.sandbox.workspace_mount, container_run_root],
            worker=worker_for_run,
        )
        stdout = host_stdout.read_text() if host_stdout.exists() else ""
        stderr = host_stderr.read_text() if host_stderr.exists() else ""

        with stdout_path.open("a") as handle:
            if stdout:
                handle.write(stdout)
                if not stdout.endswith("\n"):
                    handle.write("\n")
            stdout_path.chmod(0o600)
        with stderr_path.open("a") as handle:
            if stderr:
                handle.write(stderr)
                if not stderr.endswith("\n"):
                    handle.write("\n")
            stderr_path.chmod(0o600)
        for transcript_path in (host_stdout, host_stderr):
            if transcript_path.exists():
                transcript_path.chmod(0o600)

        self._record_run_metrics(worker_for_run["worker_id"], effective_run_id, stdout)
        try:
            self._finalize_stop_reason(worker_for_run["worker_id"], run_id=effective_run_id)
        except RuntimeErrorBase as exc:
            if isinstance(exc, WorkerPausedError):
                active_state = "paused"
            elif isinstance(exc, WorkerInterruptedError):
                active_state = "interrupted"
            elif isinstance(exc, WorkerTerminatedError):
                active_state = "terminated"
            else:
                active_state = "failed"
            evidence_path = _write_evidence_for_run(
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=str(exc),
                exit_code=exit_code,
                timeout_seconds=run_timeout_sec,
                stop_reason=exc.__class__.__name__,
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state=active_state,
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason=exc.__class__.__name__,
                evidence_path=evidence_path,
            )
            raise

        if exit_code != 0:
            classification = classify_cli_failure(
                stdout=stdout,
                stderr=stderr,
                runtime_name=self.runtime_name,
                exit_code=exit_code,
            )
            if self.runtime_name == "claude-code":
                detail = classification.user_message
            else:
                detail = (stderr or stdout or "").strip()[-2000:]
            error_text = f"{self.runtime_name} exited with code {exit_code}: {detail}"
            evidence_path = _write_evidence_for_run(
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=error_text,
                exit_code=exit_code,
                timeout_seconds=run_timeout_sec,
                stop_reason="process_exit",
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="process_exit",
                evidence_path=evidence_path,
            )
            private_classification = _private_cli_failure_classification(
                classification,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
            error = RuntimeErrorBase(
                f"{self.runtime_name} exited with code {exit_code}: {detail}"
            )
            error.failure_classification = private_classification  # type: ignore[attr-defined]
            raise error

        session_key, output = self._parse_output(worker_for_run, stdout, stderr, info)
        self._remember_native_session_key(worker_for_run, session_key)
        evidence_path = _write_evidence_for_run(
            worker=worker_for_run,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            command=command,
            env=env,
            workspace=workspace,
            stdout_text=stdout,
            stderr_text=stderr,
            output_text=output.strip(),
            error_text="",
            exit_code=exit_code,
            timeout_seconds=run_timeout_sec,
            stop_reason="process_exit",
            constraint_ledger=constraint_ledger,
            transcript_paths=transcript_paths,
            started_at=started_at,
        )
        try:
            _status, warning_message = _require_successful_run_evidence(
                workspace=workspace,
                evidence_path=evidence_path,
                constraint_ledger_path=constraint_ledger_path,
                run_id=effective_run_id,
                attempt_id=attempt_id,
            )
        except RuntimeErrorBase:
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="evidence_check_failed",
                evidence_path=evidence_path,
            )
            raise
        output_text = output.strip()
        if warning_message:
            suffix = f"\n\n{warning_message}"
            if len(output_text) + len(suffix) <= _HOST_RUN_OUTPUT_MAX_CHARS:
                output_text = f"{output_text}{suffix}"
        _write_active_run_status(
            path=heartbeat_path,
            worker=worker_for_run,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            state="completed",
            transcript_paths=transcript_paths,
            started_at=started_at_iso,
            process_pid=process_pid,
            timeout_seconds=run_timeout_sec,
            exit_code=exit_code,
            stop_reason="process_exit",
            evidence_path=evidence_path,
        )
        return output_text


    requires_run_start_identity = True
















    def set_host_process_observer(self, observer) -> None:
        self._host_process_observer = observer

    def set_run_start_observer(self, observer) -> None:
        self._run_start_observer = observer

    def _mark_rejected_run_start_unconfirmed(
        self,
        worker_id: str,
        expected_session: dict[str, object],
    ) -> None:
        current = self._read_active_session(worker_id)
        if self._active_session_fingerprint(
            current
        ) != self._active_session_fingerprint(expected_session):
            return
        self._write_active_session(
            worker_id,
            {**expected_session, "termination_unconfirmed": True},
            expected_session=expected_session,
        )

    def _cleanup_rejected_run_start(
        self,
        *,
        worker_id: str,
        expected_session: dict[str, object],
        expected_container_id: str = "",
        spawned_process: subprocess.Popen[str] | None = None,
        worker: dict | None = None,
    ) -> None:
        """Stop only the exact just-published generation or fail closed."""

        try:
            if expected_container_id:
                self.sandbox.stop_screen_session(
                    worker_id,
                    self.runtime_name,
                    str(expected_session.get("session_name") or ""),
                    worker=worker,
                    missing_ok=True,
                    expected_container_id=expected_container_id,
                )
                self.sandbox.terminate_run_processes(
                    worker_id,
                    self.runtime_name,
                    str(expected_session.get("run_id") or ""),
                    worker=worker,
                    missing_ok=True,
                    expected_container_id=expected_container_id,
                )
            else:
                try:
                    expected_pid = int(expected_session.get("process_pid") or 0)
                    expected_group = int(
                        expected_session.get("process_group") or expected_pid or 0
                    )
                except (TypeError, ValueError):
                    expected_pid = 0
                    expected_group = 0
                expected_identity = str(
                    expected_session.get("process_start_identity") or ""
                ).strip()
                if (
                    spawned_process is None
                    or spawned_process.pid != expected_pid
                    or expected_pid <= 0
                    or not expected_identity
                ):
                    raise RuntimeErrorBase(
                        "The exact host startup process identity is ambiguous."
                    )
                if spawned_process.poll() is None:
                    current_identity = self._process_start_identity(expected_pid)
                    if current_identity != expected_identity:
                        raise RuntimeErrorBase(
                            "The exact host startup process identity changed."
                        )

                    def signal_exact(sig: signal.Signals) -> None:
                        try:
                            if expected_group and expected_group != os.getpgrp():
                                os.killpg(expected_group, sig)
                            else:
                                os.kill(expected_pid, sig)
                        except ProcessLookupError:
                            return

                    signal_exact(signal.SIGTERM)
                    try:
                        spawned_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        signal_exact(signal.SIGKILL)
                        spawned_process.wait(timeout=2)
                if spawned_process.poll() is None:
                    raise RuntimeErrorBase(
                        "The exact host startup process did not stop."
                    )
                if not self._clear_process(
                    worker_id, expected_process=spawned_process
                ):
                    raise RuntimeErrorBase(
                        "Host startup process ownership changed during cleanup."
                    )
            if not self._clear_active_session(
                worker_id, expected_session=expected_session
            ):
                raise RuntimeErrorBase(
                    "Run startup ownership changed during cleanup."
                )
            if not expected_container_id:
                release_slot = getattr(self, "_release_host_slot", None)
                if callable(release_slot):
                    release_slot(worker_id, expected_slot_token=(
                        str(expected_session.get("host_slot_token") or "").strip() or None
                    ))
        except Exception as exc:
            self._mark_rejected_run_start_unconfirmed(
                worker_id, expected_session
            )
            raise RunStartupRejectedError(
                str(exc), termination_confirmed=False
            ) from exc

    def set_native_event_observer(self, observer) -> None:
        self._native_event_observer = observer

    def set_provider_liveness_observer(self, observer) -> None:
        self._provider_liveness_observer = observer

    def host_process_identity(self, worker: dict, run_id: str) -> dict[str, object] | None:
        """Verify an exact Docker run without relying on an executor process.

        Docker screen PIDs live in the container namespace, while the lease
        ledger needs a stable restart identity. The exact run/session mapping
        plus the immutable container id is the process-start identity; a live
        screen lookup proves the session still exists before a stale lease is
        renewed.
        """

        worker_id = str(worker.get("worker_id") or "").strip()
        expected_run_id = str(run_id or "").strip()
        active_session = self._read_active_session(worker_id)
        if (
            not worker_id
            or not expected_run_id
            or str((active_session or {}).get("run_id") or "").strip()
            != expected_run_id
        ):
            return None
        session_name = str((active_session or {}).get("session_name") or "").strip()
        if not session_name:
            return None
        try:
            sandbox = self.sandbox.inspect(worker_id)
            if (
                sandbox is None
                or str(sandbox.state or "").lower() != "running"
                or not str(sandbox.container_id or "").strip()
            ):
                return None
            screen_pid = self.sandbox.screen_session_pid(
                worker_id,
                self.runtime_name,
                session_name,
                worker=worker,
            )
        except Exception:
            return None
        try:
            current_screen_pid = int(screen_pid or 0)
            recorded_screen_pid = int(
                (active_session or {}).get("process_pid") or 0
            )
        except (TypeError, ValueError):
            return None
        if current_screen_pid <= 0 or (
            recorded_screen_pid > 0 and recorded_screen_pid != current_screen_pid
        ):
            return None
        container_id = str(sandbox.container_id).strip()
        identity = (
            f"docker:{container_id}:{session_name}:"
            f"{expected_run_id}:{current_screen_pid}"
        )
        recorded_identity = str(
            (active_session or {}).get("lease_process_start_identity")
            or ""
        ).strip()
        if recorded_identity and recorded_identity != identity:
            return None
        lease_pid = int(sandbox.pid or 0) or current_screen_pid
        return {
            "identity_kind": "docker_session",
            "pid": lease_pid,
            "process_group": lease_pid,
            "process_start_identity": identity,
            "container_id": container_id,
            "session_id": session_name,
            "startup_token_digest": str(
                (active_session or {}).get("startup_token_digest") or ""
            ),
            "verified": True,
        }

    def host_process_absence(self, worker: dict, run_id: str) -> bool:
        """Prove that the canonical Docker generation is absent without guessing.

        This is intentionally narrower than a failed liveness read: timeouts,
        malformed inspect output, a present container, and every host-mode
        worker remain unproven so their durable safety fence stays active.
        """

        if str(worker.get("execution_mode") or "docker") != "docker" or not str(
            run_id or ""
        ).strip():
            return False
        try:
            inspection = self.sandbox.inspect_fresh(str(worker.get("worker_id") or ""))
        except Exception:
            return False
        return str(getattr(inspection, "status", "") or "") == "confirmed_absent"

    def cleanup_unconfirmed_run_start(
        self,
        worker: dict,
        run_id: str,
        lease_identity: dict[str, object],
    ) -> bool:
        """Clean only a pre-confirmation generation captured by the durable lease.

        A replacement active-session file is never cleanup authority. Docker is
        targeted by immutable container id + exact screen/run identity; host mode
        is targeted by PID start identity. Absence of that old generation is a
        successful cleanup proof, while ambiguity fails closed.
        """

        worker_id = str(worker.get("worker_id") or "").strip()
        expected_run_id = str(run_id or "").strip()
        identity_kind = str(lease_identity.get("identity_kind") or "").strip()
        try:
            expected_pid = int(lease_identity.get("pid") or 0)
            expected_group = int(
                lease_identity.get("process_group") or expected_pid or 0
            )
        except (TypeError, ValueError):
            return False
        expected_start_identity = str(
            lease_identity.get("process_start_identity") or ""
        ).strip()
        if not worker_id or not expected_run_id or not expected_start_identity:
            return False

        if identity_kind == "docker_session":
            container_id = str(lease_identity.get("container_id") or "").strip()
            session_id = str(lease_identity.get("session_id") or "").strip()
            if not container_id or not session_id:
                return False
            self.sandbox.stop_screen_session(
                worker_id,
                self.runtime_name,
                session_id,
                worker=worker,
                missing_ok=True,
                expected_container_id=container_id,
            )
            self.sandbox.terminate_run_processes(
                worker_id,
                self.runtime_name,
                expected_run_id,
                worker=worker,
                missing_ok=True,
                expected_container_id=container_id,
            )
        elif identity_kind == "host_process":
            if expected_pid <= 0 or not expected_start_identity.startswith("ps-lstart:"):
                return False
            if self._recorded_process_is_running(expected_pid, expected_start_identity):

                def signal_exact(sig: signal.Signals) -> None:
                    current_identity = self._process_start_identity(expected_pid)
                    if current_identity != expected_start_identity:
                        return
                    try:
                        if expected_group > 0 and expected_group != os.getpgrp():
                            os.killpg(expected_group, sig)
                        else:
                            os.kill(expected_pid, sig)
                    except ProcessLookupError:
                        return

                signal_exact(signal.SIGTERM)
                if not self._wait_for_recorded_process_exit(
                    expected_pid, expected_start_identity, timeout=5.0
                ):
                    signal_exact(signal.SIGKILL)
                    if not self._wait_for_recorded_process_exit(
                        expected_pid, expected_start_identity, timeout=2.0
                    ):
                        return False
            # A failed liveness probe is not itself death proof: permissions,
            # transient ps failure, or an unreadable fingerprint must keep the
            # startup fence. Only ESRCH/zombie or a different PID incarnation
            # proves the captured generation is gone.
            if not self._recorded_pid_is_proven_gone(
                expected_pid, expected_start_identity
            ):
                return False
        else:
            return False

        current = self._read_active_session(worker_id)
        current_matches_old = False
        old_slot_token: str | None = None
        if current:
            try:
                current_pid = int(
                    current.get("lease_pid")
                    or current.get("process_pid")
                    or 0
                )
            except (TypeError, ValueError):
                current_pid = 0
            current_identity = str(
                current.get("lease_process_start_identity")
                or current.get("process_start_identity")
                or ""
            ).strip()
            current_matches_old = bool(
                str(current.get("run_id") or "") == expected_run_id
                and current_pid == expected_pid
                and current_identity == expected_start_identity
            )
            if identity_kind == "docker_session":
                current_matches_old = bool(
                    current_matches_old
                    and str(current.get("container_id") or "")
                    == str(lease_identity.get("container_id") or "")
                    and str(current.get("session_name") or "")
                    == str(lease_identity.get("session_id") or "")
                )
            if current_matches_old:
                old_slot_token = (
                    str(current.get("host_slot_token") or "").strip() or None
                )
                if not self._clear_active_session(
                    worker_id, expected_session=current
                ):
                    return False
        if identity_kind == "host_process" and current_matches_old:
            release_slot = getattr(self, "_release_host_slot", None)
            if callable(release_slot):
                # A same-worker replacement can arrive after the old session's
                # compare-delete. Its generation token must keep its slot.
                release_slot(worker_id, expected_slot_token=old_slot_token)
        return True

    @staticmethod
    def _native_session_id_from_event_line(line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return ""
        if not isinstance(event, dict):
            return ""
        if str(event.get("type") or "") == "thread.started":
            session_id = str(event.get("thread_id") or "").strip()
        else:
            session_id = str(event.get("session_id") or "").strip()
        if not session_id or len(session_id) > 256 or any(ord(char) < 32 for char in session_id):
            return ""
        return session_id

    def _provider_liveness_observation(
        self,
        line: str,
        *,
        run_id: str,
        line_sequence: int,
        model: str,
        observed_at: str | None = None,
    ) -> dict[str, object] | None:
        """Project only native typed retry/progress events; never inspect prose."""

        try:
            event = json.loads(str(line or ""))
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("type") or "").strip()
        kind = ""
        failure_class = ""
        if self.runtime_name == "codex-cli":
            if event_type == "error":
                kind = "internal_retry"
                failure_class = "provider_internal_retry"
            elif event_type == "item.completed" and isinstance(event.get("item"), dict):
                item = event["item"]
                item_type = str(item.get("type") or "").strip().lower()
                item_status = str(item.get("status") or "").strip().lower()
                if item_type == "agent_message" and str(item.get("text") or "").strip():
                    kind = "meaningful_progress"
                elif item_type in {
                    "command_execution",
                    "file_change",
                    "file_changes",
                    "mcp_tool_call",
                    "dynamic_tool_call",
                    "web_search",
                    "web_search_call",
                } and item_status == "completed" and not item.get("error"):
                    kind = "meaningful_progress"
        elif self.runtime_name == "claude-code":
            if (
                event_type == "rate_limit_event"
                and isinstance(event.get("rate_limit_info"), dict)
                and str(event["rate_limit_info"].get("status") or "").strip().lower()
                == "rejected"
            ):
                kind = "internal_retry"
                failure_class = "provider_rate_limited"
            elif event_type == "assistant" and isinstance(event.get("message"), dict):
                content = event["message"].get("content")
                if isinstance(content, list) and any(
                    isinstance(block, dict)
                    and (
                        (
                            block.get("type") == "text"
                            and bool(str(block.get("text") or "").strip())
                        )
                        or (
                            block.get("type") == "tool_use"
                            and bool(str(block.get("id") or "").strip())
                            and bool(str(block.get("name") or "").strip())
                        )
                    )
                    for block in content
                ):
                    kind = "meaningful_progress"
            elif event_type == "user" and isinstance(event.get("message"), dict):
                content = event["message"].get("content")
                if isinstance(content, list) and any(
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error") is not True
                    and bool(block.get("content"))
                    for block in content
                ):
                    kind = "meaningful_progress"
        elif self.runtime_name == "grok-build" and event_type == "grok.session.update":
            update = event.get("update")
            if isinstance(update, dict):
                update_kind = update.get("sessionUpdate")
                if update_kind == "auto_recovery_started" or (update_kind == "retry_state" and update.get("type") == "retrying"):
                    kind = "internal_retry"
                    failure_class = "provider_internal_retry"
                elif update_kind == "agent_message_chunk" and isinstance(update.get("content"), dict) and update["content"].get("text"):
                    kind = "meaningful_progress"
                elif update_kind in {"tool_call", "tool_call_update"} and update.get("status") == "completed":
                    kind = "meaningful_progress"
        if not kind:
            return None
        clean_run_id = str(run_id or "").strip()
        clean_model = str(model or "").strip()
        if not clean_run_id or not clean_model or int(line_sequence) < 1:
            return None
        line_sha256 = hashlib.sha256(str(line).encode("utf-8")).hexdigest()
        material = json.dumps(
            {
                "lineSequence": int(line_sequence),
                "lineSha256": line_sha256,
                "runId": clean_run_id,
                "runtime": self.runtime_name,
                "model": clean_model,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "event_ref": "provider_liveness_sha256:"
            + hashlib.sha256(material.encode("utf-8")).hexdigest(),
            "kind": kind,
            "failure_class": failure_class,
            "runtime": self.runtime_name,
            "model": clean_model,
            "source_sequence": int(line_sequence),
            "source_digest": line_sha256,
            "observed_at": str(observed_at or _utc_iso()),
        }

    def _observe_native_session_events(
        self,
        worker_id: str,
        stdout_path: Path,
        stop_event: Event,
        run_id: str | None = None,
        attempt_id: str | None = None,
        initial_offset: int = 0,
        worker: dict | None = None,
    ) -> None:
        """Tail native JSONL and durably publish session identity as soon as it appears."""

        offset = max(0, int(initial_offset or 0))
        buffered = ""
        line_sequence = 0
        file_identity: tuple[int, int] | None = None

        def observe_line(line: str) -> None:
            nonlocal line_sequence
            line_sequence += 1
            session_id = self._native_session_id_from_event_line(line)
            if session_id:
                active_session = self._read_active_session(worker_id)
                if active_session and run_id and active_session.get("run_id") != run_id:
                    return
                effective_receipt_run = str(
                    run_id or (active_session or {}).get("run_id") or ""
                ).strip()
                with self._process_lock:
                    pending_receipt = self._pending_native_authority_receipts.get(
                        (
                            worker_id,
                            effective_receipt_run,
                            str(attempt_id or "").strip(),
                        )
                    )
                if pending_receipt is not None:
                    try:
                        native_event = json.loads(line)
                    except json.JSONDecodeError:
                        native_event = {}
                    provider_event = (
                        (
                            self.runtime_name == "codex-cli"
                            and native_event.get("type") == "thread.started"
                            and native_event.get("thread_id") == session_id
                        )
                        or (
                            self.runtime_name == "claude-code"
                            and native_event.get("type")
                            in {"system", "assistant", "result"}
                            and native_event.get("session_id") == session_id
                        )
                    )
                    if not provider_event:
                        return
                if worker is None:
                    self._write_session_key(worker_id, session_id)
                else:
                    self._remember_native_session_key(worker, session_id)
                if active_session and active_session.get("native_session_id") != session_id:
                    self._write_active_session(
                        worker_id,
                        {**active_session, "native_session_id": session_id},
                        expected_session=active_session,
                    )
                if pending_receipt is not None:
                    receipt_path = self._native_provider_authority_receipt_path(
                        worker_id,
                        effective_receipt_run,
                        attempt_id=str(attempt_id or "").strip(),
                    )
                    receipt_path.parent.mkdir(
                        mode=0o700, parents=True, exist_ok=True
                    )
                    receipt_path.parent.chmod(0o700)
                    _atomic_write_private_text(
                        receipt_path,
                        json.dumps(pending_receipt, sort_keys=True),
                    )
            active_session = self._read_active_session(worker_id)
            effective_run_id = str(
                run_id or (active_session or {}).get("run_id") or ""
            ).strip()
            liveness_observer = self._provider_liveness_observer
            effective_attempt_id = str(attempt_id or "").strip()
            if callable(liveness_observer) and effective_run_id and effective_attempt_id:
                liveness = self._provider_liveness_observation(
                    line,
                    run_id=effective_run_id,
                    line_sequence=line_sequence,
                    model=str((active_session or {}).get("model") or ""),
                )
                if liveness is not None:
                    try:
                        liveness_observer(
                            {
                                "worker_id": worker_id,
                                "run_id": effective_run_id,
                                "attempt_id": effective_attempt_id,
                                **liveness,
                            }
                        )
                    except Exception:
                        logger.exception(
                            "Provider liveness observer failed for worker %s run %s",
                            worker_id,
                            effective_run_id,
                        )
            observer = self._native_event_observer
            if not callable(observer):
                return
            if not effective_run_id:
                return
            provider = self._agent_type()
            for event in project_native_events(provider, line):
                try:
                    observer(
                        {
                            "worker_id": worker_id,
                            "run_id": effective_run_id,
                            "provider": provider,
                            "event": event,
                        }
                    )
                except Exception:
                    logger.exception(
                        "Native event observer failed for worker %s run %s",
                        worker_id,
                        effective_run_id,
                    )

        while True:
            chunk = ""
            try:
                with stdout_path.open("r") as handle:
                    stat_result = os.fstat(handle.fileno())
                    current_identity = (stat_result.st_dev, stat_result.st_ino)
                    if file_identity is not None and current_identity != file_identity:
                        offset = 0
                        buffered = ""
                    if stat_result.st_size < offset:
                        offset = 0
                        buffered = ""
                    file_identity = current_identity
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
            except FileNotFoundError:
                pass
            if chunk:
                buffered += chunk
                complete = buffered.split("\n")
                buffered = complete.pop()
                for line in complete:
                    observe_line(line.strip())
            if stop_event.is_set():
                if buffered.strip():
                    observe_line(buffered.strip())
                return
            stop_event.wait(0.05)

    @staticmethod
    def _active_session_fingerprint(session: dict[str, object] | None) -> tuple[object, ...]:
        if not session:
            return ()
        return (
            str(session.get("run_id") or ""),
            str(session.get("attempt_id") or ""),
            str(session.get("session_name") or ""),
            str(session.get("exit_path") or ""),
            session.get("process_pid"),
            session.get("process_group"),
            str(session.get("process_start_identity") or ""),
            str(session.get("process_identity_sha256") or ""),
            session.get("owner_pid"),
            session.get("lease_pid"),
            session.get("lease_process_group"),
            str(session.get("lease_process_start_identity") or ""),
            str(session.get("container_id") or ""),
            str(session.get("startup_token_digest") or ""),
            str(session.get("host_slot_token") or ""),
        )










    def _note_exact_termination_stop_reason(self, worker: dict) -> None:
        """Bind termination intent to the exact run, never to a future retry."""

        run_id = str(
            worker.get("_active_run_id")
            or worker.get("_terminal_run_id")
            or ""
        ).strip()
        if run_id:
            self._note_stop_reason(
                str(worker["worker_id"]), "terminated", run_id=run_id
            )

    def _stale_terminal_session_is_proven_dead(
        self,
        worker_id: str,
        active_session: dict[str, object],
        *,
        expected_run_id: str | None,
        allow_legacy_confirmed_absence: bool = False,
    ) -> bool:
        """Prove a completed Docker session is stale without trusting workspace paths."""

        run_id = str(active_session.get("run_id") or "").strip()
        if (
            not expected_run_id
            or run_id != expected_run_id
            or str(active_session.get("session_name") or "").strip()
            != self._session_name_for_run_id(run_id)
        ):
            return False

        canonical_exit = self._attempt_run_root(
            worker_id,
            run_id,
            str(active_session.get("attempt_id") or ""),
        ) / "exit_code"
        recorded_exit = Path(str(active_session.get("exit_path") or "").strip())
        try:
            if recorded_exit.resolve(strict=True) != canonical_exit.resolve(strict=True):
                return False
            raw_exit_code = canonical_exit.read_text().strip()
            exit_code = int(raw_exit_code)
        except (OSError, TypeError, ValueError):
            return False
        if str(exit_code) != raw_exit_code or not 0 <= exit_code <= 255:
            return False

        with self._process_lock:
            process = self._active_processes.get(worker_id)
        if process and process.poll() is None:
            return False

        lease_identity = str(
            active_session.get("lease_process_start_identity") or ""
        ).strip()
        identity_parts = lease_identity.split(":")
        if len(identity_parts) == 5 and identity_parts[0] == "docker":
            _, container_id, identity_session, identity_run, identity_screen_pid = (
                identity_parts
            )
            try:
                recorded_screen_pid = int(active_session.get("process_pid") or 0)
                recorded_owner_pid = int(active_session.get("owner_pid") or 0)
                recorded_container_pid = int(active_session.get("lease_pid") or 0)
                identity_screen_pid_value = int(identity_screen_pid or 0)
            except (TypeError, ValueError):
                return False
            if (
                not container_id
                or identity_session != str(active_session.get("session_name") or "")
                or identity_run != run_id
                or recorded_screen_pid <= 0
                or identity_screen_pid_value != recorded_screen_pid
                or recorded_owner_pid <= 0
                or recorded_container_pid <= 0
            ):
                return False
            try:
                sandbox = self.sandbox.inspect(worker_id)
            except Exception:
                return False
            if sandbox is not None:
                if (
                    str(sandbox.container_id or "").strip() != container_id
                    or str(sandbox.state or "").lower() != "running"
                    or int(sandbox.pid or 0) != recorded_container_pid
                ):
                    return False
                try:
                    live_screen_pid = self.sandbox.screen_session_pid(
                        worker_id,
                        self.runtime_name,
                        identity_session,
                        worker={"worker_id": worker_id, "state": "running"},
                    )
                except Exception:
                    return False
                if int(live_screen_pid or 0) > 0:
                    return False
                return self._recorded_pid_is_proven_gone(recorded_owner_pid)

        legacy_identity_fields = (
            "container_id",
            "process_pid",
            "process_group",
            "owner_pid",
            "lease_pid",
            "lease_process_group",
            "process_start_identity",
            "lease_process_start_identity",
            "heartbeat_path",
            "native_session_id",
            "startup_token_digest",
            "started_at",
        )
        if not any(
            str(active_session.get(field) or "").strip()
            for field in legacy_identity_fields
        ):
            if not allow_legacy_confirmed_absence:
                return False
            try:
                inspection = self.sandbox.inspect_fresh(
                    worker_id,
                    require_configured_image=False,
                )
            except Exception:
                return False
            return str(getattr(inspection, "status", "") or "") == "confirmed_absent"

        recorded_processes: list[tuple[int, str]] = []
        for key, identity_key in (
            ("process_pid", "process_start_identity"),
            ("owner_pid", ""),
            ("lease_pid", "lease_process_start_identity"),
        ):
            try:
                pid = int(active_session.get(key) or 0)
            except (TypeError, ValueError):
                return False
            if pid <= 0:
                return False
            start_identity = (
                str(active_session.get(identity_key) or "").strip()
                if identity_key
                else ""
            )
            recorded_processes.append((pid, start_identity))
        return all(
            self._recorded_pid_is_proven_gone(pid, start_identity)
            for pid, start_identity in recorded_processes
        )

    def clear_run_local_capability_grant(self, worker: dict) -> None:
        """Rewrite run env from durable metadata so an admitted bearer cannot linger."""

        refresh_runtime_env_for_worker(self._home_dir(str(worker["worker_id"])), worker)





class OpenClawWorkstationRuntime(BaseCliWorkerRuntime):
    runtime_name = "openclaw"
    worker_root_name = "openclaw_runtime"
    gateway_container_port = 18789

    def resolve_model(self, profile: str) -> str:
        general_default = os.environ.get("WPR_MODEL_OPENCLAW_GENERAL", "").strip() or self._preferred_general_model()
        desktop_default = os.environ.get("WPR_MODEL_OPENCLAW_DESKTOP", general_default)
        defaults = {
            "openclaw-general": general_default,
            "openclaw-codex": os.environ.get("WPR_MODEL_OPENCLAW_CODEX", "openai-codex/gpt-5.3-codex"),
            "openclaw-claude": os.environ.get("WPR_MODEL_OPENCLAW_CLAUDE", "anthropic/claude-sonnet-4-6"),
            "openclaw-desktop": desktop_default,
        }
        return defaults.get(profile, defaults["openclaw-general"])

    def _preferred_general_model(self) -> str:
        if self._compatible_provider_base_url():
            for env_name in ("OPENAI_MODELS", "WPR_MODEL_CODEX_CLI", "OTUC_LLM_MODEL"):
                configured = str(os.environ.get(env_name, "")).strip()
                if configured:
                    return configured.split(",", 1)[0].strip()
        if os.environ.get("OPENAI_API_KEY", "").strip():
            return "openai/gpt-5.2"
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return "anthropic/claude-sonnet-4-6"
        if (Path.home() / ".codex" / "auth.json").exists():
            return "openai/gpt-5.2"
        if (Path.home() / ".claude").exists() or (Path.home() / ".claude.json").exists():
            return "anthropic/claude-sonnet-4-6"
        return "openai/gpt-5.2"

    def _default_session_key(self, worker: dict) -> str | None:
        scope = os.environ.get("WPR_OPENCLAW_SESSION_SCOPE", "worker").strip().lower()
        run_id = str(worker.get("_active_run_id") or "").strip()
        if scope in {"run", "per-run", "per_run"} and run_id:
            return f"wpr-worker-{worker['worker_id']}-{run_id}"
        candidate = str(self._read_session_key(worker["worker_id"]) or worker.get("session_key") or "").strip()
        if candidate and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", candidate):
            return candidate
        return f"wpr-worker-{worker['worker_id']}"

    def _env_flag(self, name: str, default: bool = False) -> bool:
        raw = str(os.environ.get(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on", "enabled"}

    def _compatible_provider_base_url(self) -> str:
        return (
            os.environ.get("WPR_OPENCLAW_BASE_URL", "").strip()
            or os.environ.get("OPENAI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_API_BASE", "").strip()
            or os.environ.get("OPENAI_REVERSE_PROXY", "").strip()
            or os.environ.get("PORTKEY_BASE_URL", "").strip()
        ).rstrip("/")

    def _compatible_provider_enabled(self) -> bool:
        if self._env_flag("WPR_OPENCLAW_DISABLE_CUSTOM_PROVIDER", False):
            return False
        if self._env_flag("WPR_OPENCLAW_USE_CUSTOM_PROVIDER", False):
            return True
        return bool(self._compatible_provider_base_url())

    def _compatible_provider_id(self) -> str:
        default = "glasshive-portkey-compatible" if self._compatible_provider_env_key() == "PORTKEY_API_KEY" else "glasshive-openai-compatible"
        raw = os.environ.get("WPR_OPENCLAW_MODEL_PROVIDER", default).strip()
        provider_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-_").lower()
        return provider_id or default

    def _compatible_provider_env_key(self) -> str:
        configured = os.environ.get("WPR_OPENCLAW_ENV_KEY", "").strip()
        if configured:
            return configured
        if os.environ.get("PORTKEY_BASE_URL", "").strip() and not (
            os.environ.get("OPENAI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_API_BASE", "").strip()
            or os.environ.get("OPENAI_REVERSE_PROXY", "").strip()
            or os.environ.get("WPR_OPENCLAW_BASE_URL", "").strip()
        ):
            return "PORTKEY_API_KEY"
        return "OPENAI_API_KEY"

    def _compatible_provider_wire_api(self) -> str:
        return os.environ.get("WPR_OPENCLAW_WIRE_API", "openai-completions").strip() or "openai-completions"

    def _compatible_provider_model_compat(self) -> dict[str, object]:
        compat: dict[str, object] = {}
        max_tokens_field = (
            os.environ.get("WPR_OPENCLAW_MAX_TOKENS_FIELD", "").strip()
            or os.environ.get("WPR_OPENCLAW_COMPAT_MAX_TOKENS_FIELD", "").strip()
        )
        if max_tokens_field in {"max_completion_tokens", "max_tokens"}:
            compat["maxTokensField"] = max_tokens_field
        elif max_tokens_field:
            logger.warning("Ignoring unsupported WPR_OPENCLAW_MAX_TOKENS_FIELD value: %s", max_tokens_field)
        return compat

    def _compatible_model_local_id(self, model: str) -> str:
        configured = os.environ.get("WPR_OPENCLAW_MODEL_ID", "").strip()
        if configured:
            return configured
        provider_id = self._compatible_provider_id()
        if model.startswith(f"{provider_id}/"):
            return model[len(provider_id) + 1 :]
        if self._compatible_provider_env_key() != "PORTKEY_API_KEY" and (
            model.startswith("openai/") or model.startswith("openai-codex/")
        ):
            return model.split("/", 1)[1]
        return model

    def _openclaw_model_for_worker(self, worker: dict) -> str:
        model = str(worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general"))).strip()
        if not model or not self._compatible_provider_enabled() or not self._compatible_provider_base_url():
            return model
        provider_id = self._compatible_provider_id()
        if model.startswith(f"{provider_id}/"):
            return model
        return f"{provider_id}/{self._compatible_model_local_id(model)}"

    def _compatible_provider_config(self, model: str) -> dict[str, object] | None:
        if not self._compatible_provider_enabled():
            return None
        base_url = self._compatible_provider_base_url()
        if not base_url:
            return None
        local_model = self._compatible_model_local_id(model)
        env_key = self._compatible_provider_env_key()
        model_entry: dict[str, object] = {
            "id": local_model,
            "name": os.environ.get("WPR_OPENCLAW_MODEL_NAME", local_model).strip() or local_model,
            "api": self._compatible_provider_wire_api(),
            "reasoning": self._env_flag("WPR_OPENCLAW_MODEL_REASONING", False),
            "input": ["text", "image"] if self._env_flag("WPR_OPENCLAW_MODEL_IMAGE_INPUT", False) else ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": int(os.environ.get("WPR_OPENCLAW_CONTEXT_WINDOW", "128000")),
            "maxTokens": int(os.environ.get("WPR_OPENCLAW_MAX_TOKENS", "32000")),
        }
        compat = self._compatible_provider_model_compat()
        if compat:
            model_entry["compat"] = compat
        provider: dict[str, object] = {
            "baseUrl": base_url,
            "apiKey": {"source": "env", "provider": "default", "id": env_key},
            "api": self._compatible_provider_wire_api(),
            "authHeader": True,
            "timeoutSeconds": int(os.environ.get("WPR_OPENCLAW_PROVIDER_TIMEOUT_SECONDS", "300")),
            "models": [model_entry],
        }
        headers: dict[str, object] = {}
        if env_key == "PORTKEY_API_KEY":
            for env_name, header_name in (
                ("PORTKEY_VIRTUAL_KEY", "x-portkey-virtual-key"),
                ("PORTKEY_CONFIG", "x-portkey-config"),
                ("PORTKEY_PROVIDER", "x-portkey-provider"),
            ):
                if os.environ.get(env_name, "").strip():
                    headers[header_name] = {"source": "env", "provider": "default", "id": env_name}
        if headers:
            provider["headers"] = headers
        return provider

    def _openclaw_root(self, worker_id: str) -> Path:
        return self._home_dir(worker_id) / ".wpr-openclaw"

    def _container_openclaw_root(self) -> str:
        return f"{self.sandbox.home_mount}/.wpr-openclaw"

    def _container_openclaw_state_dir(self) -> str:
        return f"{self._container_openclaw_root()}/state"

    def _container_openclaw_config_path(self) -> str:
        return f"{self._container_openclaw_root()}/openclaw.json"

    def _openclaw_state_dir(self, worker_id: str) -> Path:
        return self._openclaw_root(worker_id) / "state"

    def _openclaw_config_path(self, worker_id: str) -> Path:
        return self._openclaw_root(worker_id) / "openclaw.json"

    def _openclaw_env_path(self, worker_id: str) -> Path:
        return self._openclaw_root(worker_id) / "openclaw.env"

    def _gateway_token(self, worker: dict) -> str:
        return str(worker.get("gateway_token") or "").strip() or secrets.token_urlsafe(24)

    def _ensure_openclaw_dirs(self, worker_id: str) -> None:
        self._openclaw_state_dir(worker_id).mkdir(parents=True, exist_ok=True)

    def _write_gateway_config(self, worker: dict, token: str) -> None:
        worker_id = worker["worker_id"]
        self._ensure_openclaw_dirs(worker_id)
        model = self._openclaw_model_for_worker(worker)
        config = {
            "gateway": {
                "mode": "local",
                "bind": "loopback",
                "port": self.gateway_container_port,
                "auth": {"mode": "none"},
            },
            "agents": {
                "defaults": {
                    "workspace": self.sandbox.workspace_mount,
                    "repoRoot": self.sandbox.workspace_mount,
                    "model": {"primary": model},
                    "cliBackends": {
                        "claude-cli": {"command": "claude"},
                        "codex-cli": {"command": "codex"},
                    },
                    "sandbox": {
                        "mode": "off",
                    },
                }
            },
            "session": {"dmScope": "per-channel-peer"},
            "tools": {
                "fs": {"workspaceOnly": True},
                "exec": {"applyPatch": {"workspaceOnly": True}},
                "elevated": {"enabled": False},
            },
            "plugins": {"enabled": True},
        }
        provider_config = self._compatible_provider_config(model)
        if provider_config:
            config["models"] = {
                "mode": "merge",
                "providers": {self._compatible_provider_id(): provider_config},
            }
        self._openclaw_config_path(worker_id).write_text(json.dumps(config, indent=2))
        env_lines = [
            f"export OPENCLAW_STATE_DIR={shlex.quote(self._container_openclaw_state_dir())}",
            f"export OPENCLAW_CONFIG_PATH={shlex.quote(self._container_openclaw_config_path())}",
            f"export OPENCLAW_MODEL={shlex.quote(model)}",
            f"export OPENCLAW_SESSION_ID={shlex.quote(self._default_session_key(worker) or worker_id)}",
            "export OPENCLAW_DISABLE_BONJOUR=1",
        ]
        self._openclaw_env_path(worker_id).write_text("\n".join(env_lines) + "\n")

    def _gateway_enabled(self) -> bool:
        return self._env_flag("WPR_OPENCLAW_START_GATEWAY", False)

    def _gateway_env(self, worker: dict) -> dict[str, str]:
        env = reviewed_openclaw_env(self._sandbox_env())
        env["OPENCLAW_STATE_DIR"] = self._container_openclaw_state_dir()
        env["OPENCLAW_CONFIG_PATH"] = self._container_openclaw_config_path()
        env["OPENCLAW_MODEL"] = self._openclaw_model_for_worker(worker)
        env["OPENCLAW_SESSION_ID"] = self._default_session_key(worker) or worker["worker_id"]
        return env

    def _start_openclaw_gateway(self, worker: dict, sandbox: object) -> None:
        if worker.get("_glasshive_task_run"):
            return
        if not self._gateway_enabled():
            return
        env = self._gateway_env(worker)
        result = self.sandbox.start_screen_session(
            worker["worker_id"],
            self.runtime_name,
            "openclaw-gateway",
            [
                "bash",
                "-lc",
                (
                    f"openclaw gateway --port {self.gateway_container_port} --bind loopback "
                    "--auth none --allow-unconfigured --force"
                ),
            ],
            env=env,
            worker=worker,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            logger.warning("OpenClaw gateway screen session failed for %s: %s", worker.get("worker_id"), detail)
            return
        container_name = str(getattr(sandbox, "container_name", "") or "")
        if not container_name:
            return
        wait_result = self.sandbox._docker_exec(
            container_name,
            [
                "bash",
                "-lc",
                (
                    f"for i in $(seq 1 20); do "
                    f"(echo >/dev/tcp/127.0.0.1/{self.gateway_container_port}) >/dev/null 2>&1 && exit 0; "
                    "sleep 0.25; "
                    "done; exit 1"
                ),
            ],
            env=env,
            cwd=self.sandbox.workspace_mount,
        )
        if wait_result.returncode != 0:
            detail = (wait_result.stderr or wait_result.stdout or "").strip()[-500:]
            logger.warning("OpenClaw gateway did not become ready for %s: %s", worker.get("worker_id"), detail)

    def _sandbox_env(self, worker: dict | None = None) -> dict[str, str]:
        provider_keys = list(_PROVIDER_ENV_KEYS)
        if multi_user_security_enabled():
            selected_key = self._compatible_provider_env_key()
            if selected_key == "PORTKEY_API_KEY":
                provider_keys = [key for key in provider_keys if key.startswith("PORTKEY_")]
            elif selected_key == "OPENAI_API_KEY":
                provider_keys = [key for key in provider_keys if key.startswith("OPENAI_")]
            else:
                provider_keys = []
        env = reviewed_openclaw_env(self._container_env(*provider_keys))
        env["HOME"] = self.sandbox.home_mount
        env["TERM"] = self.sandbox.term_value
        env["DISPLAY"] = self.sandbox.display_value
        return env

    def _runtime_info(self, worker: dict, *, pid: int | None = None) -> RuntimeInfo:
        session_key = self._default_session_key(worker)
        if session_key:
            self._write_session_key(worker["worker_id"], session_key)
        return RuntimeInfo(
            runtime=self.runtime_name,
            model=self._openclaw_model_for_worker(worker),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=session_key,
            state_dir=str(self._openclaw_state_dir(worker["worker_id"])),
            workspace_dir=str(self._workspace_dir(worker["worker_id"])),
            pid=pid,
        )

    def _neutralize_default_openclaw_bootstrap(self, worker: dict) -> None:
        bootstrap_path = self._workspace_dir(worker["worker_id"]) / "BOOTSTRAP.md"
        task_mode_text = "\n".join(
            [
                "# GlassHive Task Mode",
                "",
                "This workspace is running an assigned GlassHive task.",
                "Follow the latest runtime-provided instruction, success criteria, and AGENTS.md.",
                "Do not start first-run identity onboarding unless the operator explicitly asks for it.",
                "For local browser verification, prefer localhost HTTP URLs over file:// URLs because some worker browser tools block local file protocols.",
                "",
            ]
        )
        if not bootstrap_path.exists():
            bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
            bootstrap_path.write_text(task_mode_text)
            return
        try:
            text = bootstrap_path.read_text(errors="ignore")
        except OSError:
            return
        default_markers = (
            "# BOOTSTRAP.md - Hello, World",
            "You just woke up. Time to figure out who you are.",
            'Start with something like:\n\n> "Hey. I just came online. Who am I? Who are you?"',
        )
        if not all(marker in text for marker in default_markers):
            return
        archive_dir = bootstrap_path.parent / ".glasshive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / "archived-openclaw-default-bootstrap.md"
        if not archive_path.exists():
            archive_path.write_text(text)
        bootstrap_path.write_text(task_mode_text)

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        require_reviewed_image = getattr(self.sandbox, "require_reviewed_openclaw_image", None)
        if callable(require_reviewed_image):
            require_reviewed_image()
        fast_sandbox = (
            None
            if self._uses_parallel_clean_room(worker)
            else getattr(
                self.sandbox,
                "fast_sandbox_from_worker",
                lambda _worker: None,
            )(worker)
        )
        sandbox = fast_sandbox or self.sandbox.ensure_ready(worker, self.runtime_name)
        require_reviewed = getattr(self.sandbox, "require_reviewed_openclaw", None)
        if callable(require_reviewed):
            require_reviewed(sandbox.container_name)
        self._write_gateway_config(worker, self._gateway_token(worker))
        self._start_openclaw_gateway(worker, sandbox)
        return self._runtime_info(worker, pid=sandbox.pid)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        expected_container_id = str(
            worker.get("_compute_release_container_id") or ""
        ).strip()
        sandbox = (
            self.sandbox.pause(
                worker["worker_id"],
                expected_container_id=expected_container_id,
            )
            if expected_container_id
            else self.sandbox.pause(worker["worker_id"])
        )
        if str(sandbox.state or "").lower() != "paused":
            raise RuntimeErrorBase("Docker pause could not be confirmed")
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        active_run_id = str(run_id or worker.get("_active_run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker_id):
            self._note_stop_reason(worker_id, "interrupted", run_id=active_run_id)
            self._stop_active_process(worker_id, worker=worker, run_id=active_run_id)
        sandbox = self.sandbox.inspect(worker["worker_id"])
        pid = sandbox.pid if sandbox and sandbox.state == "running" else None
        return self._runtime_info(worker, pid=pid)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        active_run_id = str(worker.get("_active_run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker_id):
            self._note_stop_reason(worker_id, "terminated", run_id=active_run_id)
        self._stop_active_process(worker_id, worker=worker, run_id=active_run_id)
        if "_compute_release_container_id" in worker:
            captured_id = str(worker.get("_compute_release_container_id") or "").strip()
            self.sandbox.terminate(
                worker_id,
                expected_container_id=captured_id or None,
                expected_absent=not bool(captured_id),
            )
        else:
            self.sandbox.terminate(worker_id)
        if "_compute_release_container_id" in worker and not captured_id:
            self._clear_active_session(worker_id)
        return self._runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        sandbox = self.sandbox.inspect(worker["worker_id"])
        if sandbox is None:
            return self._runtime_info(worker, pid=None)
        if sandbox.state == "paused":
            return self._runtime_info(worker, pid=None)
        active_run_id = str(worker.get("_active_run_id") or "").strip()
        if active_run_id:
            active_session = self._read_active_session(worker["worker_id"])
            if (
                active_session
                and str(active_session.get("run_id") or "") == active_run_id
                and bool(active_session.get("termination_unconfirmed"))
            ):
                return self._runtime_info(worker, pid=sandbox.pid)
            identity = self.host_process_identity(worker, active_run_id)
            pid = (
                int(identity.get("pid") or 0) or None
                if identity and bool(identity.get("verified"))
                else None
            )
            return self._runtime_info(worker, pid=pid)
        return self._runtime_info(worker, pid=sandbox.pid)

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_id = info.session_key or self._default_session_key(worker) or f"agent:main:wpr:worker:{worker['worker_id']}"
        self._neutralize_default_openclaw_bootstrap(worker)
        env = self._sandbox_env(worker)
        env["OPENCLAW_STATE_DIR"] = self._container_openclaw_state_dir()
        env["OPENCLAW_CONFIG_PATH"] = self._container_openclaw_config_path()
        env["OPENCLAW_MODEL"] = self._openclaw_model_for_worker(worker)
        instruction_path = (
            f"{self.sandbox.home_mount}/.glasshive/current-instruction.stdin"
        )
        _atomic_write_private_text(
            self._home_dir(worker["worker_id"])
            / ".glasshive"
            / "current-instruction.stdin",
            self._instruction_with_completion_contract(instruction),
        )
        command = [
            "openclaw",
            "agent",
            "--local",
            "--session-id",
            session_id,
            "-m",
            _instruction_file_pointer_message(instruction_path),
            "--json",
        ]
        return command, env

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        return self._instruction_with_completion_contract(instruction)

    def _openclaw_json_payload(self, raw: str) -> dict[str, object]:
        text = raw.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                return {}
            try:
                value = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return value if isinstance(value, dict) else {}

    def _openclaw_final_text(self, data: dict[str, object]) -> str:
        direct = str(data.get("finalAssistantVisibleText") or data.get("finalAssistantRawText") or "").strip()
        if direct:
            return direct
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        return str(meta.get("finalAssistantVisibleText") or meta.get("finalAssistantRawText") or "").strip()

    def _openclaw_stop_reason(self, data: dict[str, object]) -> str:
        completion = data.get("completion") if isinstance(data.get("completion"), dict) else {}
        direct = str(completion.get("stopReason") or data.get("stopReason") or "").strip()
        if direct:
            return direct
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        meta_completion = meta.get("completion") if isinstance(meta.get("completion"), dict) else {}
        return str(meta_completion.get("stopReason") or meta.get("stopReason") or "").strip()

    def _stdout_has_complete_response(self, stdout_path: Path) -> bool:
        if not stdout_path.exists():
            return False
        try:
            data = self._openclaw_json_payload(stdout_path.read_text(errors="ignore"))
        except OSError:
            return False
        if not self._openclaw_final_text(data):
            return False
        return self._openclaw_stop_reason(data).lower() == "stop"

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        raw = stdout.strip()
        if not raw:
            detail = (stderr or "").strip()[-1000:]
            raise RuntimeErrorBase(f"OpenClaw returned no output{': ' + detail if detail else ''}")
        data = self._openclaw_json_payload(raw)
        if not data:
            raise RuntimeErrorBase(f"OpenClaw returned invalid JSON: {raw[-800:]}")
        output_parts: list[str] = []
        final_text = self._openclaw_final_text(data)
        if final_text:
            output_parts.append(final_text)
        else:
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            text = str(content.get("text") or "").strip()
                            if text:
                                output_parts.append(text)
                elif item.get("type") == "function_call":
                    name = str(item.get("name") or "function").strip()
                    output_parts.append(f"[Tool call: {name}]")
            for payload in data.get("payloads", []):
                text = str(payload.get("text") or "").strip()
                if text:
                    output_parts.append(text)
        output = _select_user_facing_agent_output(output_parts) or json.dumps(data, indent=2)
        session_id = str(((data.get("meta") or {}).get("agentMeta") or {}).get("sessionId") or info.session_key or "").strip() or None
        return session_id, output


def _codex_usage_from_output(stdout: str) -> dict[str, int]:
    """Project terminal Codex counts into the existing disjoint run-usage buckets."""
    latest = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            latest = event.get("usage")
    if not isinstance(latest, dict):
        return {}
    counts = {
        "input_tokens": latest.get("input_tokens"),
        "output_tokens": latest.get("output_tokens"),
        "cache_read_input_tokens": latest.get("cached_input_tokens", 0),
        "cache_creation_input_tokens": latest.get("cache_write_input_tokens", 0),
    }
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
        return {}
    # Codex input includes cache reads/writes; GlassHive sums four disjoint buckets.
    # Reasoning tokens are likewise already part of output_tokens.
    cached = counts["cache_read_input_tokens"] + counts["cache_creation_input_tokens"]
    if cached > counts["input_tokens"]:
        return {}
    counts["input_tokens"] -= cached
    return counts


class CodexCliRuntime(BaseCliWorkerRuntime):
    runtime_name = "codex-cli"
    worker_root_name = "codex_cli_runtime"
    binary_name = "codex"
    _default_compatible_provider_disabled_features: tuple[str, ...] = ()
    _native_child_limit = 64
    _native_rollout_tail_bytes = 1024 * 1024

    def _query_docker_codex_provider_control(
        self,
        worker: dict,
        run_id: str,
        thread_id: str,
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        """Read typed Codex state from the exact Docker generation that ran the turn."""

        worker_id = str(worker.get("worker_id") or "").strip()
        expected_run_id = str(run_id or "").strip()
        if (
            str(worker.get("execution_mode") or "docker").strip() != "docker"
            or not worker_id
            or not expected_run_id
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id)
        ):
            return None
        active_session = self._read_active_session(worker_id)
        if (
            not isinstance(active_session, dict)
            or str(active_session.get("run_id") or "").strip() != expected_run_id
        ):
            return None
        recorded_container_id = str(
            active_session.get("container_id") or ""
        ).strip()
        if not re.fullmatch(r"[a-f0-9]{64}", recorded_container_id):
            return None
        try:
            inspection = self.sandbox.inspect_fresh(worker_id)
        except Exception:
            return None
        sandbox = getattr(inspection, "sandbox", None)
        if (
            str(getattr(inspection, "status", "") or "") != "present"
            or sandbox is None
            or str(getattr(sandbox, "state", "") or "") != "running"
            or str(getattr(sandbox, "container_id", "") or "").strip()
            != recorded_container_id
        ):
            return None
        try:
            result = self.sandbox._docker_exec(
                recorded_container_id,
                [
                    "python3",
                    "-c",
                    _CODEX_PROVIDER_CONTROL_CLIENT,
                    self.binary,
                    thread_id,
                ],
                env={
                    "HOME": self.sandbox.home_mount,
                    "CODEX_HOME": f"{self.sandbox.home_mount}/.codex",
                },
                cwd=self.sandbox.workspace_mount,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError):
            return None
        raw_output = str(result.stdout or "")
        if (
            result.returncode != 0
            or not raw_output
            or len(raw_output.encode("utf-8", errors="ignore"))
            > _CODEX_PROVIDER_CONTROL_MAX_OUTPUT_BYTES
        ):
            return None
        try:
            payload = json.loads(raw_output)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {
            "thread_result",
            "rate_limits_result",
        }:
            return None
        thread_result = payload.get("thread_result")
        rate_limits_result = payload.get("rate_limits_result")
        if not isinstance(thread_result, dict) or not isinstance(
            rate_limits_result, dict
        ):
            return None
        return thread_result, rate_limits_result

    def _supplemental_provider_failure(
        self,
        *,
        worker: dict,
        run_id: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> tuple[FailureClassification, dict[str, object]] | None:
        _ = (stderr, exit_code)
        thread_id, authored = _codex_failed_thread(stdout)
        if not thread_id:
            return None
        control = self._query_docker_codex_provider_control(
            worker,
            run_id,
            thread_id,
        )
        if control is None:
            return None
        return _codex_typed_provider_failure(
            thread_id=thread_id,
            thread_result=control[0],
            rate_limits_result=control[1],
            expected_model_provider=self._compatible_provider_id(),
            authored=authored,
        )

    def _usage_from_output(self, stdout: str) -> dict[str, int]:
        return _codex_usage_from_output(stdout)

    def resolve_model(self, profile: str) -> str:
        if profile == "codex-cli":
            return os.environ.get("WPR_MODEL_CODEX_CLI", "gpt-5.4")
        return os.environ.get("WPR_MODEL_OPENCLAW_CODEX", "openai-codex/gpt-5.3-codex")

    def _default_session_key(self, worker: dict) -> str | None:
        return self._read_session_key(worker["worker_id"]) or worker.get("session_key") or f"codex-worker:{worker['worker_id']}"

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        return _instruction_with_completion_contract(instruction)

    @staticmethod
    def _native_rollout_path(codex_home: Path, rollout_value: object) -> Path | None:
        raw_value = str(rollout_value or "").strip()
        if not raw_value:
            return None
        candidates = [Path(raw_value)]
        codex_marker = f"{os.sep}.codex{os.sep}"
        if codex_marker in raw_value:
            candidates.append(codex_home / raw_value.split(codex_marker, 1)[1])
        try:
            codex_root = codex_home.resolve()
        except OSError:
            return None
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(codex_root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                return resolved
        return None

    def _native_rollout_completed(self, rollout_path: Path) -> bool:
        try:
            terminal_event = ""
            with rollout_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size > self._native_rollout_tail_bytes:
                    handle.seek(-self._native_rollout_tail_bytes, os.SEEK_END)
                    handle.readline()
                else:
                    handle.seek(0)
                for raw_line in handle:
                    try:
                        event = json.loads(raw_line.decode("utf-8", errors="ignore"))
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    payload = event.get("payload")
                    event_type = (
                        str(payload.get("type") or "")
                        if event.get("type") == "event_msg" and isinstance(payload, dict)
                        else ""
                    )
                    if event_type in {"task_started", "task_complete", "turn_aborted"}:
                        terminal_event = event_type
        except OSError:
            return False
        return terminal_event == "task_complete"

    def _require_native_children_completed(
        self,
        worker_id: str,
        parent_thread_id: str,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parent_thread_id):
            return
        codex_home = self._home_dir(worker_id) / ".codex"
        state_databases = list(codex_home.glob("state_*.sqlite"))
        try:
            state_databases.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        except OSError:
            state_databases.sort(reverse=True)

        for database_path in state_databases:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    f"{database_path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    timeout=0.1,
                )
                if connection.execute(
                    "SELECT 1 FROM threads WHERE id = ? LIMIT 1",
                    (parent_thread_id,),
                ).fetchone() is None:
                    continue

                pending = [parent_thread_id]
                descendants: list[str] = []
                seen = {parent_thread_id}
                while pending:
                    current_parent = pending.pop()
                    rows = connection.execute(
                        "SELECT child_thread_id FROM thread_spawn_edges "
                        "WHERE parent_thread_id = ?",
                        (current_parent,),
                    ).fetchall()
                    for row in rows:
                        child_thread_id = str(row[0] or "").strip()
                        if not child_thread_id or child_thread_id in seen:
                            continue
                        seen.add(child_thread_id)
                        descendants.append(child_thread_id)
                        pending.append(child_thread_id)
                        if len(descendants) > self._native_child_limit:
                            self._raise_unsettled_native_child()

                for child_thread_id in descendants:
                    row = connection.execute(
                        "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                        (child_thread_id,),
                    ).fetchone()
                    rollout_path = self._native_rollout_path(
                        codex_home,
                        row[0] if row is not None else "",
                    )
                    if rollout_path is None or not self._native_rollout_completed(
                        rollout_path
                    ):
                        self._raise_unsettled_native_child()
                return
            except (OSError, sqlite3.Error):
                continue
            finally:
                if connection is not None:
                    connection.close()

    @staticmethod
    def _raise_unsettled_native_child() -> None:
        raise RuntimeErrorBase(
            "GlassHive evidence check failed: Codex ended while a spawned child "
            "remained open or aborted; "
            "GlassHive stopped before mission evidence validation."
        )

    def _codex_native_session_is_available(
        self,
        worker_id: str,
        session_key: str,
    ) -> bool:
        """Return false only when local Codex state proves a resume target is gone."""

        codex_home = self._home_dir(worker_id) / ".codex"
        state_databases = sorted(codex_home.glob("state_*.sqlite"))
        queried_native_store = False
        for database_path in state_databases:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    f"{database_path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    timeout=0.1,
                )
                row = connection.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                    (session_key,),
                ).fetchone()
                queried_native_store = True
            except (OSError, sqlite3.Error):
                continue
            finally:
                if connection is not None:
                    connection.close()
            if row is None:
                continue
            rollout_value = str(row[0] or "").strip()
            if not rollout_value:
                return False
            rollout_path = Path(rollout_value)
            if rollout_path.is_file():
                return True
            codex_marker = f"{os.sep}.codex{os.sep}"
            if codex_marker in rollout_value:
                relative_rollout = rollout_value.split(codex_marker, 1)[1]
                return (codex_home / relative_rollout).is_file()
            # A row in a compatible future store is stronger evidence than a
            # host-side path that this runtime does not know how to translate.
            return True
        if queried_native_store:
            return False

        legacy_sessions = codex_home / "sessions"
        if legacy_sessions.is_dir():
            try:
                return any(legacy_sessions.rglob(f"*{session_key}.jsonl"))
            except OSError:
                return True
        # Older or externally managed Codex installations may not expose a
        # local index that GlassHive can safely inspect. Preserve resume there.
        return True

    def _resumable_codex_session_key(self, worker: dict) -> str:
        session_key = str(self._read_session_key(worker["worker_id"]) or "").strip()
        if not session_key or session_key.startswith("codex-worker:"):
            return ""
        if self._codex_native_session_is_available(worker["worker_id"], session_key):
            return session_key
        logger.warning(
            "Codex native session is unavailable; starting fresh in the durable workspace",
            extra={"worker_id": str(worker.get("worker_id") or "")},
        )
        return ""

    def provider_citation_sources(self, worker: dict, run_id: str) -> list[dict[str, str]]:
        """Resolve cited public URLs from the private Codex rollout ledger.

        ``codex exec --json`` exposes citation anchors in the assistant message but keeps the
        corresponding URL/title records in its private session rollout. Export only that small
        provenance tuple; snippets and other native-session content stay private.
        """

        clean_run_id = str(run_id or "").strip()
        if (
            not clean_run_id
            or "/" in clean_run_id
            or "\\" in clean_run_id
            or clean_run_id in {".", ".."}
        ):
            raise ValueError("invalid run id")
        stdout_path = self._run_root(str(worker["worker_id"]), clean_run_id) / "stdout.log"
        if not stdout_path.is_file():
            return []
        thread_id = ""
        try:
            with stdout_path.open(errors="ignore") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "thread.started":
                        thread_id = str(event.get("thread_id") or "").strip()
                        if thread_id:
                            break
        except OSError:
            return []
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            return []

        sessions_root = self._home_dir(str(worker["worker_id"])) / ".codex" / "sessions"
        if not sessions_root.is_dir():
            return []
        try:
            candidates = list(sessions_root.rglob(f"*{thread_id}.jsonl"))
        except OSError:
            return []
        if not candidates:
            return []
        try:
            rollout_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            return []

        sources: dict[str, dict[str, str]] = {}
        try:
            with rollout_path.open(errors="ignore") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict) or event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    if payload.get("type") != "web_search_end":
                        continue
                    results = payload.get("results") if isinstance(payload.get("results"), list) else []
                    for result in results:
                        if not isinstance(result, dict):
                            continue
                        ref_id = str(result.get("ref_id") or "").strip()
                        url = str(result.get("url") or "").strip()
                        try:
                            parsed_url = urlsplit(url)
                        except ValueError:
                            continue
                        if (
                            not re.fullmatch(r"turn\d+[A-Za-z_][A-Za-z0-9_-]*?\d+", ref_id)
                            or parsed_url.scheme not in {"http", "https"}
                            or not parsed_url.netloc
                            or any(character.isspace() or ord(character) < 32 for character in url)
                        ):
                            continue
                        raw_title = str(result.get("title") or result.get("domain") or parsed_url.netloc)
                        title = " ".join(raw_title.split())
                        sources[ref_id] = {
                            "ref_id": ref_id,
                            "title": title[:300] or parsed_url.netloc,
                            "url": url,
                        }
        except OSError:
            return []
        return list(sources.values())

    def _ensure_git_workspace(self, workspace_dir: str) -> None:
        git_dir = Path(workspace_dir) / ".git"
        if git_dir.exists():
            return
        subprocess.run(["git", "init", "-q"], cwd=workspace_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["git", "config", "user.email", "worker@glasshive.local"],
            cwd=workspace_dir,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.name", "GlassHive Runtime"],
            cwd=workspace_dir,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        info = super().ensure_worker_ready(worker)
        self._ensure_git_workspace(info.workspace_dir)
        return info

    def _env_flag(self, name: str, default: bool = False) -> bool:
        raw = str(os.environ.get(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on", "enabled"}

    def _worker_env_flag(self, worker: dict, name: str, default: bool = False) -> bool:
        raw = (
            self._bootstrap_env_value(worker, name)
            or str(os.environ.get(name, ""))
        ).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on", "enabled"}

    def _codex_model_for_worker(self, worker: dict, env_name: str) -> str:
        return str(
            self._bootstrap_env_value(worker, env_name)
            or worker.get("model")
            or self.resolve_model(worker.get("profile", "codex-cli"))
            or ""
        ).strip()

    def _compatible_provider_base_url(self, worker: dict | None = None) -> str:
        if worker is not None:
            projection = validated_codex_broker_projection(worker)
            if projection is not None:
                return str(projection["base_url"])
        return (
            os.environ.get("WPR_CODEX_CLI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_API_BASE", "").strip()
            or os.environ.get("OPENAI_REVERSE_PROXY", "").strip()
            or os.environ.get("PORTKEY_BASE_URL", "").strip()
        ).rstrip("/")

    def _compatible_provider_enabled(self, worker: dict | None = None) -> bool:
        if worker is not None and validated_codex_broker_projection(worker) is not None:
            return True
        if self._env_flag("WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER", False):
            return False
        if self._env_flag("WPR_CODEX_CLI_USE_CUSTOM_PROVIDER", False):
            return True
        return bool(self._compatible_provider_base_url())

    def _compatible_provider_id(self, worker: dict | None = None) -> str:
        if worker is not None and validated_codex_broker_projection(worker) is not None:
            return "glasshive_run_broker"
        raw = os.environ.get("WPR_CODEX_CLI_MODEL_PROVIDER", "glasshive_openai_compatible").strip()
        return re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "glasshive_openai_compatible"

    def _compatible_provider_env_key(self, worker: dict | None = None) -> str:
        if worker is not None and validated_codex_broker_projection(worker) is not None:
            return "OPENAI_API_KEY"
        configured = os.environ.get("WPR_CODEX_CLI_ENV_KEY", "").strip()
        if configured:
            return configured
        if os.environ.get("PORTKEY_BASE_URL", "").strip() and not any(
            os.environ.get(name, "").strip()
            for name in (
                "WPR_CODEX_CLI_BASE_URL",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
                "OPENAI_REVERSE_PROXY",
            )
        ):
            return "PORTKEY_API_KEY"
        return "OPENAI_API_KEY"

    def _compatible_provider_disabled_features(self) -> list[str]:
        raw = os.environ.get("WPR_CODEX_CLI_DISABLE_FEATURES", "").strip()
        if raw:
            return [item.strip() for item in raw.split(",") if item.strip()]
        return list(self._default_compatible_provider_disabled_features)

    def _compatible_provider_allowed_reasoning_efforts(self) -> set[str]:
        raw = os.environ.get("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "").strip()
        valid = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
        if not raw:
            # Keep the generic OpenAI-compatible route conservative. The GlassHive core
            # provider uses the native host CLI path and advertises its own richer effort set;
            # compatible gateways must explicitly declare anything beyond this proven set.
            allowed = {"none", "low", "medium", "high"}
            if self._codex_xhigh_route_proven():
                allowed.add("xhigh")
            return allowed
        configured = {item.strip().lower() for item in raw.split(",") if item.strip()}
        return configured & valid or set(valid)

    def _codex_xhigh_route_proven(self) -> bool:
        return self._env_flag("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", False) or self._env_flag(
            "GLASSHIVE_CODEX_XHIGH_ROUTE_PROVEN",
            False,
        )

    def _compatible_provider_reasoning_effort_fallback(self, allowed: set[str]) -> str:
        configured = os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT_FALLBACK", "medium").strip().lower()
        if configured in allowed:
            return configured
        if "medium" in allowed:
            return "medium"
        return sorted(allowed)[0] if allowed else ""

    def _codex_reasoning_effort_for_worker(self, worker: dict) -> str:
        reasoning_effort = (
            self._bootstrap_env_value(worker, "WPR_CODEX_CLI_REASONING_EFFORT")
            or os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT", "")
            or os.environ.get("WPR_CODEX_CLI_DEFAULT_REASONING_EFFORT", "")
        ).strip().lower()
        requested_effort = reasoning_effort
        allowed_efforts = self._compatible_provider_allowed_reasoning_efforts()
        fallback_reason = ""
        if reasoning_effort and reasoning_effort not in allowed_efforts:
            raise RuntimeErrorBase(
                f"Unsupported Codex provider-route effort: {reasoning_effort}; "
                f"supported efforts: {', '.join(sorted(allowed_efforts))}"
            )
        if requested_effort or reasoning_effort:
            worker["_effort_projection"] = {
                "requested": requested_effort or reasoning_effort,
                "effective": reasoning_effort,
                "allowed": sorted(allowed_efforts),
                "route_proven": self._codex_xhigh_route_proven(),
                "fallback_reason": fallback_reason,
            }
        return reasoning_effort

    def effort_projection_for_worker(self, worker: dict) -> dict[str, object]:
        if str(worker.get("profile") or "").strip() != "codex-cli":
            return {}
        candidate = dict(worker)
        self._codex_reasoning_effort_for_worker(candidate)
        projection = candidate.get("_effort_projection")
        return dict(projection) if isinstance(projection, dict) else {}

    def _append_codex_reasoning_effort_config(self, command: list[str], worker: dict) -> None:
        reasoning_effort = self._codex_reasoning_effort_for_worker(worker)
        if reasoning_effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if reasoning_effort == "minimal":
            command.extend(["-c", 'web_search="disabled"'])
            command.extend(["--disable", "image_generation"])

    def _append_codex_user_config_policy(self, command: list[str], worker: dict) -> None:
        if self._worker_env_flag(worker, "WPR_CODEX_CLI_IGNORE_USER_CONFIG", False):
            command.append("--ignore-user-config")

    def _append_codex_compatible_provider_config(
        self,
        command: list[str],
        worker: dict,
        *,
        include_reasoning_effort: bool = True,
    ) -> None:
        clean_room = self._uses_parallel_clean_room(worker)
        projection = validated_codex_broker_projection(worker)
        if not clean_room and not self._compatible_provider_enabled(worker):
            return
        base_url = (
            self._parallel_clean_room_provider_base_url(worker, "openai")
            if clean_room
            else self._compatible_provider_base_url(worker)
        )
        if not base_url:
            return
        provider_id = (
            self._compatible_provider_id()
            if clean_room
            else self._compatible_provider_id(worker)
        )
        provider_name = (
            "GlassHive run broker"
            if clean_room or projection is not None
            else os.environ.get("WPR_CODEX_CLI_PROVIDER_NAME", "GlassHive OpenAI-compatible").strip()
        )
        wire_api = (
            "responses"
            if clean_room or projection is not None
            else os.environ.get("WPR_CODEX_CLI_WIRE_API", "responses").strip() or "responses"
        )
        env_key = (
            "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
            if clean_room
            else self._compatible_provider_env_key(worker)
        )
        verbosity = os.environ.get("WPR_CODEX_CLI_MODEL_VERBOSITY", "medium").strip()
        for feature in self._compatible_provider_disabled_features():
            command.extend(["--disable", feature])
        command.extend(
            [
                "-c",
                f'model_provider="{provider_id}"',
                "-c",
                f'model_providers.{provider_id}.name="{provider_name}"',
                "-c",
                f'model_providers.{provider_id}.base_url="{base_url}"',
                "-c",
                f'model_providers.{provider_id}.env_key="{env_key}"',
                "-c",
                f'model_providers.{provider_id}.wire_api="{wire_api}"',
                "-c",
                f"model_providers.{provider_id}.requires_openai_auth=false",
                "-c",
                f"model_providers.{provider_id}.supports_websockets=false",
            ]
        )
        if projection is not None and not clean_room:
            worker_id = _toml_string(str(projection["worker_id"]))
            run_id = _toml_string(str(projection["run_id"]))
            command.extend(
                [
                    "-c",
                    (
                        f'model_providers.{provider_id}.http_headers='
                        f'{{ "X-GlassHive-Worker-Id" = {worker_id}, '
                        f'"X-GlassHive-Run-Id" = {run_id} }}'
                    ),
                ]
            )
        if verbosity:
            command.extend(["-c", f'model_verbosity="{verbosity}"'])
        if include_reasoning_effort:
            self._append_codex_reasoning_effort_config(command, worker)

    def _codex_exec_command(
        self,
        worker: dict,
        *,
        model: str,
        session_key: str | None = None,
        attach_native_authority: bool = False,
    ) -> list[str]:
        is_resume = bool(session_key)
        dangerous_mode = os.environ.get("WPR_CODEX_DANGEROUS", "1").strip().lower() in {"1", "true", "yes", "on"}
        if is_resume:
            command = [self.binary, "exec", "resume", "--json"]
        else:
            command = [self.binary, "exec", "--json", "--skip-git-repo-check", "-C", self.sandbox.workspace_mount]
        if model:
            if is_resume:
                command.extend(["-c", f'model="{model}"'])
            else:
                command.extend(["-m", model])
        self._append_codex_user_config_policy(command, worker)
        if attach_native_authority and not worker.get("_native_context_placement"):
            command.extend(["-c", "project_doc_max_bytes=0"])
        if self._uses_parallel_clean_room(worker) or not worker.get(
            "_glasshive_provider_account_bound"
        ):
            self._append_codex_compatible_provider_config(
                command,
                worker,
                include_reasoning_effort=False,
            )
        self._append_codex_reasoning_effort_config(command, worker)
        if dangerous_mode:
            if is_resume:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.extend(["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"])
        elif not is_resume:
            # Current Codex CLIs removed --full-auto; these overrides keep its
            # workspace-write sandbox without interactive approvals.
            command.extend(["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"'])
        if is_resume:
            command.append(str(session_key))
        command.append("-")
        return command

    def _codex_resume_with_fresh_fallback(
        self,
        worker: dict,
        *,
        resume_command: list[str],
        fresh_command: list[str],
    ) -> list[str]:
        run_id = str(worker.get("_active_run_id") or "").strip()
        if not run_id:
            return resume_command
        run_root = self._attempt_container_run_root(
            run_id, str(worker.get("_run_attempt_id") or "").strip()
        )
        instruction_path = f"{run_root}/instruction.stdin"
        resume_error_path = f"{run_root}/codex-resume.stderr"
        resume_invocation = shlex.join(resume_command)
        fresh_invocation = shlex.join(fresh_command)
        script = "\n".join(
            [
                "set -o pipefail",
                f"resume_error={shlex.quote(resume_error_path)}",
                'rm -f -- "$resume_error"',
                'trap \'rm -f -- "$resume_error"\' EXIT',
                (
                    f"if {resume_invocation} < {shlex.quote(instruction_path)} "
                    '2> "$resume_error"; then exit 0; else status=$?; fi'
                ),
                f"if grep -Fq -- {shlex.quote(_CODEX_STALE_THREAD_ERROR)} \"$resume_error\"; then",
                '  rm -f -- "$resume_error"',
                "  trap - EXIT",
                f"  exec {fresh_invocation} < {shlex.quote(instruction_path)}",
                "fi",
                'cat -- "$resume_error" >&2',
                'exit "$status"',
            ]
        )
        return ["bash", "-c", script]

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        worker.pop("_glasshive_native_authority_commands", None)
        native_authority = self._materialize_direct_worker_native_authority(worker)
        existing_session = (
            None
            if self._provider_session_starts_fresh(worker)
            else self._resumable_codex_session_key(worker)
        )
        model = self._codex_model_for_worker(worker, "WPR_MODEL_CODEX_CLI")
        is_resume = bool(existing_session and not existing_session.startswith("codex-worker:"))
        command = self._codex_exec_command(
            worker,
            model=model,
            session_key=existing_session if is_resume else None,
            attach_native_authority=native_authority is not None,
        )
        if is_resume:
            fresh_command = self._codex_exec_command(
                worker,
                model=model,
                attach_native_authority=native_authority is not None,
            )
            if native_authority is not None:
                worker["_glasshive_native_authority_commands"] = [
                    list(command),
                    list(fresh_command),
                ]
            command = self._codex_resume_with_fresh_fallback(
                worker,
                resume_command=command,
                fresh_command=fresh_command,
            )
        provider_keys = [
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "OPENAI_REVERSE_PROXY",
            "PORTKEY_API_KEY",
            "PORTKEY_BASE_URL",
            "PORTKEY_VIRTUAL_KEY",
            "PORTKEY_CONFIG",
        ]
        if multi_user_security_enabled():
            selected_key = self._compatible_provider_env_key(worker)
            if selected_key == "PORTKEY_API_KEY":
                provider_keys = [key for key in provider_keys if key.startswith("PORTKEY_")]
            elif selected_key == "OPENAI_API_KEY":
                provider_keys = [key for key in provider_keys if key.startswith("OPENAI_")]
            else:
                provider_keys = []
        env = self._container_env_for_worker(
            worker,
            *provider_keys,
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        projection = validated_codex_broker_projection(worker)
        if projection is not None and not self._uses_parallel_clean_room(worker):
            for key in _CODEX_BROKER_CONFLICTING_ENV:
                env.pop(key, None)
            env["OPENAI_API_KEY"] = str(projection["grant_token"])
        apply_bound_provider_account_environment(
            worker,
            env,
            runtime_name=self.runtime_name,
        )
        return command, env

    def _extract_plain_output(self, stdout: str, stderr: str) -> str:
        stripped = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not stripped:
            return (stderr.strip() or stdout.strip())[-4000:]

        assistant_index = max((idx for idx, line in enumerate(stripped) if line.lower() == "codex"), default=-1)
        if assistant_index >= 0:
            assistant_lines: list[str] = []
            for line in stripped[assistant_index + 1 :]:
                lowered = line.lower()
                if lowered == "tokens used":
                    break
                if line.isdigit():
                    continue
                assistant_lines.append(line)
            if assistant_lines:
                deduped: list[str] = []
                for line in assistant_lines:
                    if not deduped or deduped[-1] != line:
                        deduped.append(line)
                return "\n".join(deduped)[-4000:]

        filtered: list[str] = []
        skip_prefixes = (
            "openai codex",
            "workdir:",
            "model:",
            "provider:",
            "approval:",
            "sandbox:",
            "reasoning effort:",
            "reasoning summaries:",
            "session id:",
            "mcp:",
            "mcp startup:",
        )
        for line in stripped:
            lowered = line.lower()
            if line == "--------" or line.isdigit() or lowered == "user" or lowered == "tokens used":
                continue
            if any(lowered.startswith(prefix) for prefix in skip_prefixes):
                continue
            filtered.append(line)

        if filtered:
            deduped: list[str] = []
            for line in filtered:
                if not deduped or deduped[-1] != line:
                    deduped.append(line)
            return "\n".join(deduped)[-4000:]

        return (stdout.strip() or stderr.strip())[-4000:]

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        session_key = self._read_provider_session_key(worker) or info.session_key
        output_parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "thread.started":
                maybe_session = str(payload.get("thread_id") or "").strip()
                if maybe_session:
                    session_key = maybe_session
            item = payload.get("item") or {}
            if payload.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    output_parts.append(text)
        if session_key:
            self._require_native_children_completed(
                str(worker.get("worker_id") or ""),
                str(session_key),
            )
        if output_parts:
            return session_key, _select_user_facing_agent_output(output_parts)
        if getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker):
            return session_key, "The harness completed without a user-facing response."
        fallback = self._extract_plain_output(stdout, stderr)
        selected = _select_user_facing_agent_output([fallback])
        return session_key, (selected or fallback)[-4000:]


    _native_child_limit = 64

    _native_rollout_tail_bytes = 1024 * 1024









    def _codex_run_rollout_path(self, worker: dict, run_id: str) -> Path | None:
        """Resolve the session emitted by this exact run's owned stdout."""
        clean_run_id = str(run_id or "").strip()
        if (
            not clean_run_id
            or "/" in clean_run_id
            or "\\" in clean_run_id
            or clean_run_id in {".", ".."}
        ):
            raise ValueError("invalid run id")
        stdout_path = self._run_root(str(worker["worker_id"]), clean_run_id) / "stdout.log"
        if not stdout_path.is_file():
            return None
        thread_id = ""
        try:
            with stdout_path.open(errors="ignore") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "thread.started":
                        thread_id = str(event.get("thread_id") or "").strip()
                        if thread_id:
                            break
        except OSError:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            return None

        sessions_root = self._home_dir(str(worker["worker_id"])) / ".codex" / "sessions"
        if not sessions_root.is_dir():
            return None
        try:
            candidates = list(sessions_root.rglob(f"*{thread_id}.jsonl"))
        except OSError:
            return None
        if not candidates:
            return None
        try:
            rollout_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            return None

        if not rollout_path.resolve().is_relative_to(sessions_root.resolve()):
            return None
        return rollout_path

    def provider_rollout_tool_log(self, worker: dict, run_id: str, *, instruction_sha256: str) -> str:
        """Join native tool output to the exact admitted input and native turn."""
        path = self._codex_run_rollout_path(worker, run_id)
        unavailable = json.dumps({"type": "glasshive.tool_evidence_unavailable"})
        if path is None:
            return unavailable
        instruction_path = self._run_root(str(worker["worker_id"]), run_id) / "instruction.stdin"
        try:
            instruction = instruction_path.read_text()
            if not instruction or hashlib.sha256(instruction.encode("utf-8")).hexdigest() != instruction_sha256:
                return unavailable
            fingerprint = (path.stat().st_size, path.stat().st_mtime_ns)
            # Select by native turn identity plus exact input bytes, never by timing,
            # prose/tool-name matching, or the newest turn in a reused session.
            turns: dict[str, list[dict]] = {}
            matched: set[str] = set()
            current = ""
            retained_bytes = 0
            with path.open() as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    payload = event.get("payload") if isinstance(event, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    if event.get("type") == "turn_context":
                        current = str(payload.get("turn_id") or "")
                        if current:
                            turns.setdefault(current, [])
                        continue
                    if event.get("type") != "response_item" or not current:
                        continue
                    meta = payload.get("internal_chat_message_metadata_passthrough")
                    if not isinstance(meta, dict) or meta.get("turn_id") != current:
                        continue
                    content = payload.get("content")
                    if payload.get("type") == "message" and payload.get("role") == "user" and isinstance(content, list):
                        texts = [part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "input_text"]
                        if texts == [instruction]:
                            matched.add(current)
                    if current in matched and payload.get("type") in {
                        "custom_tool_call", "custom_tool_call_output", "function_call", "function_call_output"
                    }:
                        retained_bytes += len(line.encode("utf-8"))
                        if retained_bytes > 8 * 1024 * 1024:
                            return unavailable
                        turns[current].append({"type": "response_item", "payload": payload})
            if len(matched) != 1 or fingerprint != (path.stat().st_size, path.stat().st_mtime_ns):
                return unavailable
            if instruction_path.read_text() != instruction:
                return unavailable
            selected = turns[next(iter(matched))]
            return "\n".join(json.dumps(item, ensure_ascii=False) for item in selected)
        except (OSError, UnicodeError, ValueError):
            return unavailable



class ClaudeCodeRuntime(BaseCliWorkerRuntime):
    runtime_name = "claude-code"
    worker_root_name = "claude_code_runtime"
    binary_name = "claude"
    _workspace_effort_support_cache: dict[tuple[str, str, str], bool] = {}

    def resolve_model(self, profile: str) -> str:
        return os.environ.get("WPR_MODEL_CLAUDE_CODE", "opus")

    def _provider_model_for_worker(self, worker: dict) -> str:
        logical_model = worker.get("model") or self.resolve_model(
            worker.get("profile", "claude-code")
        )
        return os.environ.get("WPR_CLAUDE_CODE_PROVIDER_MODEL", "").strip() or str(
            logical_model
        )

    @staticmethod
    def _bedrock_enabled() -> bool:
        return os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _remove_conflicting_anthropic_credentials(self, env: dict[str, str]) -> None:
        if self._bedrock_enabled():
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("ANTHROPIC_API_KEY", None)

    def _default_session_key(self, worker: dict) -> str | None:
        existing = self._read_session_key(worker["worker_id"])
        if existing:
            return existing
        if worker.get("session_key") and not str(worker.get("session_key")).startswith("worker:"):
            return str(worker.get("session_key"))
        return f"claude-worker:{worker['worker_id']}"

    def _chrome_enabled(self) -> bool:
        raw = os.environ.get("WPR_CLAUDE_CODE_ENABLE_CHROME", "").strip().lower()
        return raw not in {"0", "false", "no", "off", "disabled"}

    def _effort_for_worker(self, worker: dict) -> str:
        return (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        return _instruction_with_completion_contract(instruction)

    def _preflight_workspace_effort_support(self, worker: dict) -> None:
        if str(worker.get("execution_mode") or "docker") != "docker":
            return
        effort = self._effort_for_worker(worker)
        if not effort or effort == "default":
            return
        cache_key = (str(self.sandbox.image), self.binary, effort)
        if self._workspace_effort_support_cache.get(cache_key):
            return
        try:
            self.sandbox._ensure_image()
            result = self.sandbox._docker(
                ["run", "--rm", "--entrypoint", self.binary, self.sandbox.image, "--help"],
                check=False,
                capture_output=True,
                timeout_sec=20,
            )
        except Exception as exc:
            raise RuntimeDependencyMissingError(
                f"Claude Code {effort} effort could not be preflighted in the GlassHive workspace image",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=str(worker.get("profile") or "claude-code"),
                execution_mode="docker",
                dependency_label="Claude Code --effort support",
                recovery_hint=(
                    "Use a GlassHive workspace image with a Claude Code CLI that supports `--effort`, "
                    "or use default effort until the image is upgraded."
                ),
            ) from exc
        help_text = f"{result.stdout or ''}\n{result.stderr or ''}"
        if result.returncode != 0 or not _claude_effort_help_supports(help_text, effort):
            actual = (help_text.strip() or f"exit {result.returncode}")[-400:]
            raise RuntimeDependencyMissingError(
                f"Claude Code {effort} effort requires workspace image support for `claude --effort`",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=str(worker.get("profile") or "claude-code"),
                execution_mode="docker",
                required_version="Claude Code CLI with --effort support",
                actual_version=actual,
                dependency_label="Claude Code --effort support",
                recovery_hint=(
                    "Upgrade the GlassHive workspace image or use default Claude effort for this run. "
                    "Do not silently project a native effort when the active image cannot prove support."
                ),
            )
        self._workspace_effort_support_cache[cache_key] = True

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        self._preflight_workspace_effort_support(worker)
        return super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)

    def _resume_with_fresh_fallback(
        self,
        worker: dict,
        *,
        resume_command: list[str],
        fresh_command: list[str],
    ) -> list[str]:
        """Retry only Claude's explicit missing-session result as a fresh task."""

        run_id = str(worker.get("_active_run_id") or "").strip()
        if not run_id:
            return resume_command
        run_root = self._attempt_container_run_root(
            run_id, str(worker.get("_run_attempt_id") or "").strip()
        )
        instruction_path = f"{run_root}/instruction.stdin"
        resume_stdout_path = f"{run_root}/claude-resume.stdout"
        resume_stderr_path = f"{run_root}/claude-resume.stderr"
        resume_invocation = shlex.join(resume_command)
        fresh_invocation = shlex.join(fresh_command)
        script = "\n".join(
            [
                "set -o pipefail",
                f"resume_stdout={shlex.quote(resume_stdout_path)}",
                f"resume_stderr={shlex.quote(resume_stderr_path)}",
                'rm -f -- "$resume_stdout" "$resume_stderr"',
                (
                    "flush_partial() { status=$?; trap - EXIT HUP INT TERM; "
                    '[ ! -s "$resume_stdout" ] || cat -- "$resume_stdout"; '
                    '[ ! -s "$resume_stderr" ] || cat -- "$resume_stderr" >&2; '
                    'rm -f -- "$resume_stdout" "$resume_stderr"; exit "$status"; }'
                ),
                'signal_exit() { trap - HUP INT TERM; exit "$1"; }',
                "trap flush_partial EXIT",
                "trap 'signal_exit 129' HUP",
                "trap 'signal_exit 130' INT",
                "trap 'signal_exit 143' TERM",
                (
                    f"if {resume_invocation} < {shlex.quote(instruction_path)} "
                    '> "$resume_stdout" 2> "$resume_stderr"; then'
                ),
                "  trap - EXIT HUP INT TERM",
                '  cat -- "$resume_stdout"',
                '  cat -- "$resume_stderr" >&2',
                '  rm -f -- "$resume_stdout" "$resume_stderr"',
                "  exit 0",
                "else status=$?; fi",
                f"if grep -Eq -- {shlex.quote('^' + _CLAUDE_STALE_SESSION_ERROR + ' ')} \"$resume_stderr\"; then",
                '  rm -f -- "$resume_stdout" "$resume_stderr"',
                "  trap - EXIT HUP INT TERM",
                f"  exec {fresh_invocation} < {shlex.quote(instruction_path)}",
                "fi",
                "trap - EXIT HUP INT TERM",
                'cat -- "$resume_stdout"',
                'cat -- "$resume_stderr" >&2',
                'rm -f -- "$resume_stdout" "$resume_stderr"',
                'exit "$status"',
            ]
        )
        return ["bash", "-c", script]

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        worker.pop("_glasshive_native_authority_commands", None)
        selectors = prepare_private_command(self, worker, bootstrap_bundle_for(worker))
        native_authority = self._materialize_direct_worker_native_authority(worker)
        session_key = (
            None
            if self._provider_session_starts_fresh(worker)
            else self._read_provider_session_key(worker)
        )
        model = self._provider_model_for_worker(worker)
        permission_mode = os.environ.get("WPR_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions")
        command = [
            self.binary,
            "-p",
            "--permission-mode",
            permission_mode,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
        ]
        if native_authority is not None:
            command.extend(["--append-system-prompt-file", native_authority])
        if self._chrome_enabled():
            command.insert(2, "--chrome")
        effort = self._effort_for_worker(worker)
        if effort and effort != "default":
            if effort not in CLAUDE_CODE_EFFORT_LEVELS:
                raise RuntimeErrorBase("The configured Claude effort is unsupported")
            command.extend(["--effort", effort])
        if selectors is not None:
            command = apply_claude_context(command, selectors)
            if native_authority is not None:
                command[command.index("--append-system-prompt-file") + 1] = native_authority
        if session_key and not session_key.startswith("claude-worker:"):
            resume_command = [*command, "--resume", session_key]
            if native_authority is not None:
                worker["_glasshive_native_authority_commands"] = [
                    list(resume_command),
                    list(command),
                ]
            command = self._resume_with_fresh_fallback(
                worker,
                resume_command=resume_command,
                fresh_command=command,
            )
        env = self._container_env(
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_EC2_METADATA_DISABLED",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
            "ANTHROPIC_BEDROCK_BASE_URL",
            "ANTHROPIC_BEDROCK_SERVICE_TIER",
            "API_TIMEOUT_MS",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        use_api_key = os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not use_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
        self._remove_conflicting_anthropic_credentials(env)
        return command, env

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        raw = stdout.strip()
        if not raw:
            if getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker):
                return info.session_key, "The harness completed without a user-facing response."
            return info.session_key, (stderr.strip() or "")[-4000:]
        if getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker):
            session_key = info.session_key
            assistant_parts: list[str] = []
            result_parts: list[str] = []
            structured_parts: list[str] = []
            for line in raw.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                maybe_session = str(event.get("session_id") or "").strip()
                if maybe_session:
                    session_key = maybe_session
                if str(event.get("type") or "") == "result":
                    structured = event.get(
                        "structured_output", event.get("structuredOutput")
                    )
                    if isinstance(structured, dict):
                        structured_parts.append(
                            json.dumps(
                                structured,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        )
                    result = str(event.get("result") or "").strip()
                    if result:
                        result_parts.append(result)
                if str(event.get("type") or "") != "assistant":
                    continue
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                content = message.get("content") if isinstance(message.get("content"), list) else []
                text = "".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and str(block.get("type") or "") == "text"
                ).strip()
                if text:
                    assistant_parts.append(text)
            selected = _select_user_facing_agent_output(
                structured_parts or result_parts or assistant_parts
            )
            return session_key, selected or "The harness completed without a user-facing response."
        payload = self._terminal_result_payload(raw)
        if not payload:
            return info.session_key, ""
        session_key = str(payload.get("session_id") or info.session_key or "").strip() or None
        result = str(payload.get("result") or "").strip()
        return session_key, _select_user_facing_agent_output([result]) or result

    @staticmethod
    def _terminal_result_payload(stdout: str) -> dict:
        for line in reversed(stdout.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and (event.get("type") == "result" or ("type" not in event and ("result" in event or "usage" in event))):
                return event
        return {}

    def _usage_from_output(self, stdout: str) -> dict[str, int]:
        payload = self._terminal_result_payload(stdout)
        source = payload.get("usage") if isinstance(payload, dict) else {}
        usage = source if isinstance(source, dict) else {}
        normalized: dict[str, int] = {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = usage.get(key, 0)
            if isinstance(value, bool):
                value = 0
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                parsed = 0
            normalized[key] = max(0, parsed)
        return normalized

    @staticmethod
    def _nonnegative_telemetry_int(value: object) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _finite_telemetry_float(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed == parsed and abs(parsed) != float("inf") else None

    @staticmethod
    def _new_telemetry_state() -> dict[str, object]:
        return {
            "init": {},
            "result": {},
            "event_count": 0,
            "assistant_event_count": 0,
            "malformed_line_count": 0,
            "oversized_line_count": 0,
            "api_retry_count": 0,
            "last_api_retry_event_sequence": 0,
            "api_retry_delay_ms": 0,
            "api_retry_statuses": set(),
            "tool_call_counts": {},
            "seen_tool_call_ids": set(),
            "seen_usage_message_ids": set(),
            "stream_input_tokens": 0,
            "stream_output_tokens": 0,
            "stream_cache_read_input_tokens": 0,
            "stream_cache_creation_input_tokens": 0,
            "first_timestamp": "",
            "last_timestamp": "",
        }

    def _consume_telemetry_line(self, state: dict[str, object], line: str) -> bool:
        if not line.strip():
            return False
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            state["malformed_line_count"] = int(state["malformed_line_count"]) + 1
            return True
        if not isinstance(value, dict):
            return True

        observed_at = datetime.now(timezone.utc).isoformat()
        if not state["first_timestamp"]:
            state["first_timestamp"] = observed_at
        state["last_timestamp"] = observed_at
        state["event_count"] = int(state["event_count"]) + 1
        event_type = value.get("type")
        subtype = value.get("subtype")
        if event_type == "system" and subtype == "init" and not state["init"]:
            state["init"] = {
                "claude_code_version": str(value.get("claude_code_version") or "").strip(),
                "model": str(value.get("model") or "").strip(),
            }
        if event_type == "result":
            usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
            result_state = str(value.get("subtype") or "").strip()
            explicit_error = value.get("is_error")
            state["result"] = {
                "subtype": result_state,
                "is_error": (
                    explicit_error
                    if isinstance(explicit_error, bool)
                    else result_state.startswith("error_")
                    or result_state in {"error", "failed", "timeout"}
                ),
                "stop_reason": str(value.get("stop_reason") or "").strip(),
                "duration_ms": value.get("duration_ms"),
                "duration_api_ms": value.get("duration_api_ms"),
                "ttft_ms": value.get("ttft_ms"),
                "ttft_stream_ms": value.get("ttft_stream_ms"),
                "time_to_request_ms": value.get("time_to_request_ms"),
                "num_turns": value.get("num_turns"),
                "total_cost_usd": value.get("total_cost_usd"),
                "service_tier": str(usage.get("service_tier") or "").strip(),
                "speed": str(usage.get("speed") or "").strip(),
            }
        if event_type == "api_retry" or subtype == "api_retry":
            state["api_retry_count"] = int(state["api_retry_count"]) + 1
            state["last_api_retry_event_sequence"] = int(state["event_count"])
            state["api_retry_delay_ms"] = int(state["api_retry_delay_ms"]) + self._nonnegative_telemetry_int(
                value.get("retry_delay_ms") or value.get("delay_ms")
            )
            status = str(
                value.get("error_status")
                or value.get("api_error_status")
                or value.get("status")
                or ""
            ).strip()
            if status:
                statuses = state["api_retry_statuses"]
                if isinstance(statuses, set):
                    statuses.add(status)
        if event_type == "assistant":
            state["assistant_event_count"] = int(state["assistant_event_count"]) + 1
            message = value.get("message")
            if isinstance(message, dict):
                message_id = str(message.get("id") or "").strip()
                seen_ids = state["seen_usage_message_ids"]
                if message_id and isinstance(seen_ids, set) and message_id not in seen_ids:
                    seen_ids.add(message_id)
                    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                    for source_key, state_key in (
                        ("input_tokens", "stream_input_tokens"),
                        ("output_tokens", "stream_output_tokens"),
                        ("cache_read_input_tokens", "stream_cache_read_input_tokens"),
                        ("cache_creation_input_tokens", "stream_cache_creation_input_tokens"),
                    ):
                        state[state_key] = int(state[state_key]) + self._nonnegative_telemetry_int(
                            usage.get(source_key)
                        )
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                counts = state["tool_call_counts"]
                if isinstance(counts, dict):
                    for item in content:
                        if not isinstance(item, dict) or item.get("type") != "tool_use":
                            continue
                        tool_call_id = str(item.get("id") or "").strip()
                        seen_tool_call_ids = state["seen_tool_call_ids"]
                        if (
                            tool_call_id
                            and isinstance(seen_tool_call_ids, set)
                            and tool_call_id in seen_tool_call_ids
                        ):
                            continue
                        if tool_call_id and isinstance(seen_tool_call_ids, set):
                            seen_tool_call_ids.add(tool_call_id)
                        name = str(item.get("name") or "unknown").strip() or "unknown"
                        counts[name] = int(counts.get(name, 0)) + 1
        return True

    def _telemetry_from_state(self, state: dict[str, object]) -> dict[str, object]:
        if (
            int(state["event_count"]) <= 0
            and int(state["malformed_line_count"]) <= 0
            and int(state["oversized_line_count"]) <= 0
        ):
            return {}
        init = state["init"] if isinstance(state["init"], dict) else {}
        result = state["result"] if isinstance(state["result"], dict) else {}
        tool_call_counts = (
            state["tool_call_counts"] if isinstance(state["tool_call_counts"], dict) else {}
        )
        retry_statuses = (
            sorted(str(item) for item in state["api_retry_statuses"])
            if isinstance(state["api_retry_statuses"], set)
            else []
        )
        telemetry: dict[str, object] = {
            "schema": "glasshive.claude-run-telemetry.v1",
            "claude_code_version": str(init.get("claude_code_version") or "").strip(),
            "model": str(init.get("model") or "").strip(),
            "service_tier": str(result.get("service_tier") or "").strip(),
            "speed": str(result.get("speed") or "").strip(),
            "result_state": str(result.get("subtype") or "").strip(),
            "is_error": bool(result.get("is_error")) if isinstance(result.get("is_error"), bool) else False,
            "stop_reason": str(result.get("stop_reason") or "").strip(),
            "duration_ms": self._nonnegative_telemetry_int(result.get("duration_ms")),
            "duration_api_ms": self._nonnegative_telemetry_int(result.get("duration_api_ms")),
            "ttft_ms": self._nonnegative_telemetry_int(result.get("ttft_ms")),
            "ttft_stream_ms": self._nonnegative_telemetry_int(result.get("ttft_stream_ms")),
            "time_to_request_ms": self._nonnegative_telemetry_int(result.get("time_to_request_ms")),
            "num_turns": self._nonnegative_telemetry_int(result.get("num_turns")),
            "api_retry_count": int(state["api_retry_count"]),
            "last_api_retry_event_sequence": int(
                state["last_api_retry_event_sequence"]
            ),
            "api_retry_delay_ms": int(state["api_retry_delay_ms"]),
            "api_retry_statuses": retry_statuses,
            "tool_call_count": sum(tool_call_counts.values()),
            "tool_call_counts": dict(sorted(tool_call_counts.items())),
            "event_count": int(state["event_count"]),
            "malformed_line_count": int(state["malformed_line_count"]),
            "oversized_line_count": int(state["oversized_line_count"]),
            "stream_input_tokens": int(state["stream_input_tokens"]),
            "stream_output_tokens": int(state["stream_output_tokens"]),
            "stream_cache_read_input_tokens": int(state["stream_cache_read_input_tokens"]),
            "stream_cache_creation_input_tokens": int(
                state["stream_cache_creation_input_tokens"]
            ),
            "first_timestamp": str(state["first_timestamp"]),
            "last_timestamp": str(state["last_timestamp"]),
        }
        total_cost_usd = self._finite_telemetry_float(result.get("total_cost_usd"))
        if total_cost_usd is not None:
            telemetry["total_cost_usd"] = total_cost_usd
        if telemetry["duration_ms"]:
            telemetry["duration_non_api_ms"] = max(
                int(telemetry["duration_ms"]) - int(telemetry["duration_api_ms"]),
                0,
            )
        return telemetry

    def _telemetry_from_output(self, stdout: str) -> dict[str, object]:
        state = self._new_telemetry_state()
        for line in stdout.splitlines():
            self._consume_telemetry_line(state, line)
        return self._telemetry_from_state(state)

    def live_telemetry(
        self,
        worker: dict,
        stdout: str,
        run_id: str | None = None,
    ) -> dict[str, object]:
        if not run_id:
            telemetry = self._telemetry_from_output(stdout)
            if telemetry:
                telemetry["telemetry_scope"] = "console_tail"
            return telemetry

        worker_id = str(worker["worker_id"])
        run_id = str(run_id)
        run_stdout = self._run_root(worker_id, run_id) / "stdout.log"
        key = (worker_id, run_id)
        sampled_at = datetime.now(timezone.utc)
        with self._live_telemetry_lock:
            try:
                stat = run_stdout.stat()
            except OSError:
                return {
                    "schema": "glasshive.claude-run-telemetry.v1",
                    "run_id": run_id,
                    "telemetry_scope": "active_run_unavailable",
                }
            cached = self._live_telemetry_cache.get(key)
            inode = int(getattr(stat, "st_ino", 0))
            if (
                cached is None
                or int(cached.get("inode") or -1) != inode
                or int(cached.get("offset") or 0) > stat.st_size
            ):
                cached = {
                    "inode": inode,
                    "offset": 0,
                    "partial": b"",
                    "discarding_oversized_line": False,
                    "state": self._new_telemetry_state(),
                    "sample_sequence": 0,
                    "first_observed_at": sampled_at,
                    "last_progress_at": None,
                }
                self._live_telemetry_cache[key] = cached

            offset = int(cached["offset"])
            state = cached["state"]
            pending = bytes(cached["partial"])
            discarding_oversized_line = bool(cached.get("discarding_oversized_line"))
            consumed_progress = False
            with run_stdout.open("rb") as handle:
                handle.seek(offset)
                while True:
                    appended = handle.read(1024 * 1024)
                    if not appended:
                        break
                    offset += len(appended)
                    if discarding_oversized_line:
                        newline = appended.find(b"\n")
                        if newline < 0:
                            continue
                        appended = appended[newline + 1 :]
                        discarding_oversized_line = False
                        if not appended:
                            continue
                    pieces = (pending + appended).split(b"\n")
                    pending = pieces.pop()
                    if isinstance(state, dict):
                        for raw_line in pieces:
                            if len(raw_line) > _MAX_TELEMETRY_LINE_BYTES:
                                state["malformed_line_count"] = (
                                    int(state["malformed_line_count"]) + 1
                                )
                                state["oversized_line_count"] = (
                                    int(state.get("oversized_line_count") or 0) + 1
                                )
                                consumed_progress = True
                                continue
                            consumed_progress = (
                                self._consume_telemetry_line(
                                    state,
                                    raw_line.decode("utf-8", errors="replace"),
                                )
                                or consumed_progress
                            )
                    if len(pending) > _MAX_TELEMETRY_LINE_BYTES:
                        if isinstance(state, dict):
                            state["malformed_line_count"] = int(state["malformed_line_count"]) + 1
                            state["oversized_line_count"] = (
                                int(state.get("oversized_line_count") or 0) + 1
                            )
                        pending = b""
                        discarding_oversized_line = True
                        consumed_progress = True
            cached["offset"] = offset
            cached["partial"] = pending
            cached["discarding_oversized_line"] = discarding_oversized_line
            if consumed_progress:
                cached["last_progress_at"] = sampled_at
            cached["sample_sequence"] = int(cached["sample_sequence"]) + 1

            telemetry = self._telemetry_from_state(state) if isinstance(state, dict) else {}
            try:
                current_log_bytes = max(offset, int(run_stdout.stat().st_size))
            except OSError:
                current_log_bytes = offset
            telemetry.update(
                {
                    "schema": "glasshive.claude-run-telemetry.v1",
                    "run_id": run_id,
                    "telemetry_scope": "full_active_run_incremental",
                    "sampled_at": sampled_at.isoformat().replace("+00:00", "Z"),
                    "sample_sequence": int(cached["sample_sequence"]),
                    "parsed_bytes": (
                        int(cached["offset"])
                        if cached["discarding_oversized_line"]
                        else int(cached["offset"]) - len(bytes(cached["partial"]))
                    ),
                    "log_bytes": current_log_bytes,
                    "partial_line_present": bool(cached["partial"])
                    or bool(cached["discarding_oversized_line"]),
                    "first_observed_at": cached["first_observed_at"].isoformat().replace("+00:00", "Z"),
                }
            )
            last_progress_at = cached["last_progress_at"]
            if isinstance(last_progress_at, datetime):
                telemetry["last_progress_at"] = last_progress_at.isoformat().replace("+00:00", "Z")
                telemetry["seconds_since_progress"] = max(
                    0.0,
                    round((sampled_at - last_progress_at).total_seconds(), 3),
                )
                telemetry["last_stream_activity_at"] = telemetry["last_progress_at"]
                telemetry["seconds_since_stream_activity"] = telemetry[
                    "seconds_since_progress"
                ]

            if len(self._live_telemetry_cache) > 128:
                oldest_key = min(
                    self._live_telemetry_cache,
                    key=lambda item: (
                        self._live_telemetry_cache[item].get("first_observed_at")
                        or sampled_at
                    ),
                )
                if oldest_key != key:
                    self._live_telemetry_cache.pop(oldest_key, None)
            return telemetry


_LOCAL_MARKDOWN_CITATION = re.compile(
    r"!?\[([^\]\r\n]+)\]\(\s*"
    r"(?:<(?:file://)?(?:/(?:Users|home|root|Volumes|private/var)/|~/)[^>\r\n]*>"
    r"|(?:file://)?(?:/(?:Users|home|root|Volumes|private/var)/|~/)(?:[^()\s]|\([^()\r\n]*\))*)"
    r"\s*\)"
)
_SECRET_REDACTIONS: tuple[RedactionRule, ...] = (
    *CREDENTIAL_REDACTIONS,
    (re.compile(r"/Users/[^/\s\"'`]+(?:/[^\s\"'`]*)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"/(?:home|root|Volumes|private/var)/[^\s\"'`]+(?:/[^\s\"'`]*)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"~/[^\s\"'`]+(?:/[^\s\"'`]*)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_ACCESS_KEY]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bghp_[A-Za-z0-9_]{8,}\b"), "ghp_[REDACTED]"),
    (re.compile(r"\bxoxb-[A-Za-z0-9-]{8,}\b"), "xoxb-[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*\Z"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{256,}"), "[REDACTED_IMAGE_BASE64]"),
    (re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/=])"), "[REDACTED_LONG_BASE64]"),
)
_FINAL_REPORT_PATTERN = re.compile(
    r"(?mi)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]*)?"
    r"(?:(?:[*_]{1,3}|`{1,3})[ \t]*)?FINAL REPORT\s*:\s*"
    r"(?:(?:[*_]{1,3}|`{1,3})[ \t]*)?"
)
_HOST_RUN_OUTPUT_MAX_CHARS = 64000


def _select_user_facing_agent_output(output_parts: list[str]) -> str:
    """Prefer an explicit final report; otherwise use the latest assistant result."""
    cleaned = [part.strip() for part in output_parts if str(part or "").strip()]
    if not cleaned:
        return ""
    for part in reversed(cleaned):
        marker_matches = list(FINAL_REPORT_PATTERN.finditer(part))
        if marker_matches:
            return part[marker_matches[-1].end() :].strip()
    return cleaned[-1]


def _redact_text(value: str, max_chars: int | None = None) -> str:
    # A hidden host path cannot be a usable link; keep its label before masking raw paths.
    text = _LOCAL_MARKDOWN_CITATION.sub(r"\1", value)
    for pattern, replacement in _SECRET_REDACTIONS:
        text = pattern.sub(replacement, text)
    if max_chars is not None and len(text) > max_chars:
        return text[-max_chars:]
    return text


def _redact_command_arg(value: object) -> str:
    text = str(value or "")
    if "\n" in text or len(text) > 600:
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"[REDACTED_LONG_ARG chars={len(text)} sha256={digest}]"
    if text.startswith("/") or text.startswith("~/"):
        return Path(text).name or "[REDACTED_PATH]"
    return _redact_text(text)


def _redacted_command_display(command: list[str]) -> str:
    return " ".join(shlex.quote(_redact_command_arg(part)) for part in command)


def _effort_projection_for_audit(worker: dict) -> dict[str, object]:
    raw = worker.get("_effort_projection")
    if not isinstance(raw, dict):
        raw = worker.get("effort_projection")
    if not isinstance(raw, dict):
        return {}
    allowed = raw.get("allowed")
    return {
        "requested": str(raw.get("requested") or ""),
        "effective": str(raw.get("effective") or ""),
        "allowed": [str(item) for item in allowed] if isinstance(allowed, list) else [],
        "route_proven": bool(raw.get("route_proven")),
        "fallback_reason": str(raw.get("fallback_reason") or ""),
    }


def _write_constraint_ledger_for_run(
    *,
    worker: dict,
    instruction: str,
    workspace: Path,
    run_id: str,
) -> tuple[dict[str, object] | None, str]:
    try:
        ledger = build_constraint_ledger(instruction=instruction, worker=worker, run_id=run_id)
        placement = worker.get("_native_context_placement") or {}
        path = write_constraint_ledger(workspace, ledger, run_id,
            publish_latest=placement.get("file_placement") != "common")
        try:
            relative = path.relative_to(workspace).as_posix()
        except ValueError:
            relative = str(path)
        return ledger, relative
    except Exception as exc:  # pragma: no cover - evidence must not mask the real worker result
        logger.warning(
            "Failed to write GlassHive constraint ledger",
            extra={"worker_id": str(worker.get("worker_id") or ""), "run_id": run_id, "error": str(exc)},
        )
        return None, ""


def _write_evidence_for_run(
    *,
    worker: dict,
    run_id: str,
    runtime_name: str,
    model: str,
    command: list[str],
    env: dict[str, str],
    workspace: Path,
    stdout_text: str,
    stderr_text: str,
    output_text: str,
    error_text: str,
    exit_code: int | None,
    timeout_seconds: float | None,
    stop_reason: str,
    constraint_ledger: dict[str, object] | None,
    transcript_paths: dict[str, str],
    started_at: float | None = None,
    ended_at: float | None = None,
) -> str:
    try:
        evidence = build_run_evidence(
            worker=worker,
            run_id=run_id,
            runtime_name=runtime_name,
            model=model,
            command=command,
            env=env,
            workspace_dir=workspace,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            output_text=output_text,
            error_text=error_text,
            exit_code=exit_code,
            timeout_seconds=timeout_seconds,
            stop_reason=stop_reason,
            constraint_ledger=constraint_ledger,
            started_at=started_at,
            ended_at=ended_at,
            transcript_paths=transcript_paths,
        )
        attempt_id = str(worker.get("_run_attempt_id") or "").strip()
        if attempt_id:
            evidence["attempt_id"] = attempt_id
        evidence["native_media"] = capture_native_media(workspace, run_id, stdout_text)
        path = write_run_evidence(workspace, evidence, run_id)
        try:
            return path.relative_to(workspace).as_posix()
        except ValueError:
            return str(path)
    except Exception as exc:  # pragma: no cover - evidence must not mask the real worker result
        logger.warning(
            "Failed to write GlassHive run evidence",
            extra={"worker_id": str(worker.get("worker_id") or ""), "run_id": run_id, "error": str(exc)},
        )
        return ""


def _read_workspace_json_object(workspace: Path, relative_path: str) -> dict[str, object] | None:
    if not relative_path:
        return None
    target = (workspace / relative_path).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text())
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_valid_constraint_ledger(
    workspace: Path,
    relative_path: str,
    *,
    run_id: str = "",
) -> dict[str, object]:
    payload = _read_workspace_json_object(workspace, relative_path)
    if payload is None:
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger was not readable")
    if str(payload.get("schema") or "") != "glasshive.run.constraint-ledger.v1":
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger is missing its canonical schema")
    if run_id and str(payload.get("run_id") or "") != run_id:
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger belongs to a different run")
    if not isinstance(payload.get("worker"), dict):
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger is missing worker metadata")
    if "original_request" not in payload:
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger is missing the original request")
    if not isinstance(payload.get("constraints"), dict) or not isinstance(payload.get("outputs"), dict):
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger is missing constraint/output sections")
    return payload


def _read_run_evidence_result(workspace: Path, evidence_path: str) -> dict[str, object]:
    payload = _read_workspace_json_object(workspace, evidence_path)
    if not payload:
        return {}
    result = payload.get("evidence_result") if isinstance(payload, dict) else {}
    return result if isinstance(result, dict) else {}


def _require_successful_run_evidence(
    *,
    workspace: Path,
    evidence_path: str,
    constraint_ledger_path: str,
    run_id: str = "",
    attempt_id: str = "",
) -> tuple[str, str]:
    """Fail a successful worker process when its completion evidence is not usable."""
    if not evidence_path:
        raise RuntimeErrorBase("GlassHive evidence check failed: run evidence was not written")
    evidence = _read_workspace_json_object(workspace, evidence_path)
    if (
        not evidence
        or evidence.get("schema") != "glasshive.run.evidence.v1"
        or (run_id and evidence.get("run_id") != run_id)
        or (attempt_id and evidence.get("attempt_id") != attempt_id)
    ):
        raise RuntimeErrorBase("GlassHive evidence check failed: run evidence identity is invalid")
    if constraint_ledger_path:
        _read_valid_constraint_ledger(workspace, constraint_ledger_path, run_id=run_id)
    # The runtime's prose-derived reminder is diagnostic, not an authorization
    # receipt. Its generation failure must not erase an otherwise valid result.
    result = evidence.get("evidence_result")
    if not isinstance(result, dict) or not result:
        raise RuntimeErrorBase("GlassHive evidence check failed: run evidence was not readable")
    status = str(result.get("status") or "").strip().lower()
    if status == "fail":
        raise RuntimeErrorBase(_evidence_result_message(result, failed=True))
    if status == "warn":
        return status, _evidence_result_message(result, failed=False)
    if status != "pass":
        raise RuntimeErrorBase("GlassHive evidence check failed: run evidence result is missing or invalid")
    if not constraint_ledger_path:
        return "warn", "GlassHive constraint diagnostic warning: internal constraint diagnostic was unavailable."
    return status, ""


def _session_timeout_seconds(active_session: dict[str, object] | None) -> float | None:
    try:
        value = (active_session or {}).get("timeout_seconds")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _session_started_at_epoch(active_session: dict[str, object] | None) -> float | None:
    raw = str((active_session or {}).get("started_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _process_start_identity_epoch(identity: str) -> float | None:
    raw = str(identity or "").removeprefix("ps-lstart:").strip()
    if not raw:
        return None
    try:
        return time.mktime(datetime.strptime(raw, "%a %b %d %H:%M:%S %Y").timetuple())
    except (OverflowError, ValueError):
        return None

def _transcript_paths_from_active_session(active_session: dict[str, object] | None) -> dict[str, str]:
    return {
        "stdout": str((active_session or {}).get("stdout_path") or ""),
        "stderr": str((active_session or {}).get("stderr_path") or ""),
        "exit_code": str((active_session or {}).get("exit_path") or ""),
        "constraint_ledger": str((active_session or {}).get("constraint_ledger_path") or ""),
    }


def _default_constraint_ledger_path(run_id: str) -> str:
    return f"glasshive-run/runs/{run_id}/constraint-ledger.json" if run_id else "glasshive-run/constraint-ledger.json"


def _default_evidence_path(run_id: str) -> str:
    return f"glasshive-run/runs/{run_id}/evidence.json" if run_id else "glasshive-run/evidence.json"


def _ensure_recovered_success_evidence(
    *,
    worker: dict,
    run_id: str,
    runtime_name: str,
    model: str,
    command: list[str],
    workspace: Path,
    stdout_text: str,
    stderr_text: str,
    output_text: str,
    exit_code: int | None,
    active_session: dict[str, object] | None,
    instruction: str,
) -> tuple[str, str]:
    if not run_id:
        raise RuntimeErrorBase("GlassHive evidence check failed: recovered run id was not available")
    constraint_ledger_path = str((active_session or {}).get("constraint_ledger_path") or "").strip()
    default_ledger_path = _default_constraint_ledger_path(run_id)
    if not constraint_ledger_path and (workspace / default_ledger_path).exists():
        constraint_ledger_path = default_ledger_path
    constraint_ledger = None
    if constraint_ledger_path:
        # A declared or present diagnostic must still belong to this exact run.
        constraint_ledger = _read_valid_constraint_ledger(workspace, constraint_ledger_path, run_id=run_id)
    elif instruction.strip():
        constraint_ledger, constraint_ledger_path = _write_constraint_ledger_for_run(
            worker=worker,
            instruction=instruction,
            workspace=workspace,
            run_id=run_id,
        )
    evidence_path = _default_evidence_path(run_id)
    attempt_id = str((active_session or {}).get("attempt_id") or worker.get("_run_attempt_id") or "").strip()
    existing_evidence = _read_workspace_json_object(workspace, evidence_path)
    if (
        not existing_evidence
        or not isinstance(existing_evidence.get("evidence_result"), dict)
        or existing_evidence.get("run_id") != run_id
        or (attempt_id and existing_evidence.get("attempt_id") != attempt_id)
    ):
        transcript_paths = _transcript_paths_from_active_session(active_session)
        if constraint_ledger_path:
            transcript_paths["constraint_ledger"] = constraint_ledger_path
        evidence_path = _write_evidence_for_run(
            worker={**worker, "_run_attempt_id": attempt_id} if attempt_id else worker,
            run_id=run_id,
            runtime_name=runtime_name,
            model=model,
            command=command,
            env={},
            workspace=workspace,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            output_text=output_text,
            error_text="",
            exit_code=exit_code,
            timeout_seconds=_session_timeout_seconds(active_session),
            stop_reason="process_exit",
            constraint_ledger=constraint_ledger,
            transcript_paths=transcript_paths,
            started_at=_session_started_at_epoch(active_session),
        )
        if not evidence_path:
            raise RuntimeErrorBase("GlassHive evidence check failed: run evidence was not written")
    return _require_successful_run_evidence(
        workspace=workspace,
        evidence_path=evidence_path,
        constraint_ledger_path=constraint_ledger_path,
        run_id=run_id,
        attempt_id=attempt_id,
    )


def _evidence_reason_preview(reason: object) -> str:
    if isinstance(reason, dict):
        label = str(reason.get("reason") or "").strip()
        extras: list[str] = []
        issues = reason.get("issues")
        if isinstance(issues, list):
            for issue in issues[:3]:
                if isinstance(issue, dict):
                    issue_reason = str(issue.get("reason") or "").strip()
                    if issue_reason:
                        extras.append(issue_reason)
                    missing = issue.get("missing_required_artifact_types")
                    if isinstance(missing, list) and missing:
                        extras.append("missing " + ", ".join(str(item) for item in missing[:5]))
                elif issue:
                    extras.append(str(issue))
        artifacts = reason.get("artifacts")
        if isinstance(artifacts, list):
            paths = [str(item.get("path") or "") for item in artifacts if isinstance(item, dict)]
            if paths:
                extras.append("invalid " + ", ".join(paths[:5]))
        if extras:
            return f"{label}: {'; '.join(extras)}"
        return label
    return str(reason or "").strip()


def _evidence_result_message(result: dict[str, object], *, failed: bool) -> str:
    key = "failure_reasons" if failed else "warning_reasons"
    reasons = result.get(key) if isinstance(result.get(key), list) else []
    previews = [_evidence_reason_preview(item) for item in reasons[:5]]
    previews = [item for item in previews if item]
    if not failed and previews == ["internal constraint diagnostic was unavailable"]:
        prefix = "GlassHive constraint diagnostic warning"
    else:
        prefix = "GlassHive evidence check failed" if failed else "GlassHive evidence check warning"
    detail = "; ".join(previews) if previews else "see glasshive-run/evidence.json"
    return f"{prefix}: {detail}"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _active_run_status_path(workspace: Path, run_id: str) -> Path:
    return workspace / "glasshive-run" / "runs" / run_id / "active-run.json"


def _active_run_workspace_from_status_path(path: Path) -> Path:
    try:
        return path.parents[3]
    except IndexError:
        return path.parent


def _active_run_resolve_transcript_path(raw_path: str, *, workspace: Path) -> Path:
    candidate = Path(str(raw_path or ""))
    if candidate.is_absolute():
        return candidate
    return workspace / candidate


def _active_run_tail_hash(path: Path, *, tail_bytes: int = 4096) -> str | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            return hashlib.sha256(handle.read(tail_bytes)).hexdigest()
    except OSError:
        return None


def _active_run_heartbeat_sequence(path: Path) -> int:
    try:
        existing = json.loads(path.read_text())
    except Exception:
        return 1
    try:
        return int(existing.get("heartbeat_sequence") or 0) + 1
    except Exception:
        return 1


def _active_run_transcript_progress(path: Path, transcript_paths: dict[str, str]) -> dict[str, object]:
    workspace = _active_run_workspace_from_status_path(path)
    now = time.time()
    files: dict[str, object] = {}
    latest_output_mtime: float | None = None
    for key, raw_path in sorted((transcript_paths or {}).items()):
        if key not in {"stdout", "stderr", "exit_code", "constraint_ledger"}:
            continue
        resolved = _active_run_resolve_transcript_path(str(raw_path or ""), workspace=workspace)
        try:
            stat = resolved.stat()
        except OSError:
            files[key] = {"exists": False, "bytes": 0}
            continue
        if key in {"stdout", "stderr"} and stat.st_size > 0:
            latest_output_mtime = max(latest_output_mtime or stat.st_mtime, stat.st_mtime)
        files[key] = {
            "exists": True,
            "bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "age_seconds": round(max(0.0, now - stat.st_mtime), 3),
            "tail_sha256": _active_run_tail_hash(resolved),
        }
    latest_output_at = (
        datetime.fromtimestamp(latest_output_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if latest_output_mtime is not None
        else None
    )
    return {
        "files": files,
        "last_output_at": latest_output_at,
        "quiet_seconds": round(max(0.0, now - latest_output_mtime), 3) if latest_output_mtime is not None else None,
    }


def _write_active_run_status(
    *,
    path: Path,
    worker: dict,
    run_id: str,
    runtime_name: str,
    model: str,
    state: str,
    transcript_paths: dict[str, str],
    started_at: str,
    process_pid: int | None,
    timeout_seconds: float | None,
    exit_code: int | None = None,
    stop_reason: str = "",
    evidence_path: str = "",
) -> None:
    try:
        with _ACTIVE_RUN_STATUS_LOCK:
            if state == "running":
                try:
                    previous = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    previous = {}
                if (
                    isinstance(previous, dict)
                    and str(previous.get("run_id") or "") == run_id
                    and str(previous.get("state") or "") in _ACTIVE_RUN_TERMINAL_STATES
                ):
                    return
            payload = {
                "schema": "glasshive.active_run.v1",
                "run_id": run_id,
                "state": state,
                "started_at": started_at,
                "last_heartbeat_at": _utc_iso(),
                "heartbeat_sequence": _active_run_heartbeat_sequence(path),
                "worker": {
                    "worker_id": str(worker.get("worker_id") or ""),
                    "profile": str(worker.get("profile") or ""),
                    "execution_mode": str(worker.get("execution_mode") or ""),
                    "runtime": str(worker.get("runtime") or ""),
                },
                "runtime": runtime_name,
                "model": model,
                "process_pid": process_pid,
                "timeout_seconds": timeout_seconds,
                "exit_code": exit_code,
                "stop_reason": stop_reason,
                "transcript_paths": transcript_paths,
                "transcript_progress": _active_run_transcript_progress(path, transcript_paths),
                "evidence_path": evidence_path,
            }
            if not _write_private_json(path, payload):
                raise OSError("active run status write failed")
    except Exception as exc:  # pragma: no cover - heartbeat must not mask the real worker result
        logger.warning(
            "Failed to write GlassHive active run status",
            extra={"worker_id": str(worker.get("worker_id") or ""), "run_id": run_id, "error": str(exc)},
        )


def _start_active_run_heartbeat(
    *,
    path: Path,
    worker: dict,
    run_id: str,
    runtime_name: str,
    model: str,
    transcript_paths: dict[str, str],
    started_at: str,
    process_pid: int | None,
    timeout_seconds: float | None,
    stop_event: Event,
    interval_sec: float = 5.0,
) -> Thread:
    def beat() -> None:
        while not stop_event.wait(interval_sec):
            _write_active_run_status(
                path=path,
                worker=worker,
                run_id=run_id,
                runtime_name=runtime_name,
                model=model,
                state="running",
                transcript_paths=transcript_paths,
                started_at=started_at,
                process_pid=process_pid,
                timeout_seconds=timeout_seconds,
            )

    thread = Thread(target=beat, name=f"glasshive-run-heartbeat-{run_id[:12]}", daemon=True)
    thread.start()
    return thread


def _stop_active_run_heartbeat(stop_event: Event, thread: Thread | None) -> None:
    stop_event.set()
    if thread:
        thread.join()


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug[:64] or "project"


class HostNativeCliMixin:
    execution_mode = "host"
    worker_root_name = "host_cli_runtime"

    def _release_host_slot_locked(
        self,
        worker_id: str,
        *,
        expected_slot_token: str | None | object = _HOST_SLOT_EXPECTATION_UNSET,
    ) -> bool:
        current_slot_token = self._host_slot_tokens().get(worker_id)
        if (
            expected_slot_token is not _HOST_SLOT_EXPECTATION_UNSET
            and current_slot_token != expected_slot_token
        ):
            if not (
                current_slot_token is None
                and self._host_slot_bookkeeping_is_absent_locked(worker_id)
            ):
                return False
            return True
        lane = self._host_worker_lanes().pop(worker_id, None)
        self._host_slot_tokens().pop(worker_id, None)
        if lane:
            raw_active = self._host_active_slots().get(lane)
            if isinstance(raw_active, set):
                raw_active.discard(worker_id)
                if not raw_active:
                    self._host_active_slots().pop(lane, None)
            elif raw_active == worker_id:
                self._host_active_slots().pop(lane, None)
        return True


    def _finalize_owned_host_generation(
        self,
        worker_id: str,
        *,
        expected_process: subprocess.Popen[str] | None,
        expected_session: dict[str, object] | None,
        expected_slot_token: str | None,
        clear_session: bool,
    ) -> bool:
        """Release one completed host generation only while both owners still match."""

        session_path = self._active_session_meta_path(worker_id)
        with self._active_session_file_lock(worker_id):
            current_session = self._read_active_session(worker_id)
            if current_session is None and session_path.exists():
                return False
            with self._process_lock:
                current_process = self._active_processes.get(worker_id)
                current_slot_token = self._host_slot_tokens().get(worker_id)
                if (
                    current_session is None
                    and not session_path.exists()
                    and current_process is None
                    and self._host_slot_bookkeeping_is_absent_locked(worker_id)
                ):
                    return True
                if self._active_session_fingerprint(
                    current_session
                ) != self._active_session_fingerprint(expected_session):
                    return False
                if current_process is not expected_process and not (
                    # The run thread reaps its own exited process record; that is
                    # not a newer generation taking ownership.
                    current_process is None
                    and expected_process is not None
                    and expected_process.poll() is not None
                ):
                    return False
                if current_slot_token != expected_slot_token and not (
                    current_slot_token is None
                    and self._host_slot_bookkeeping_is_absent_locked(worker_id)
                ):
                    return False
                try:
                    process_group = int((expected_session or {}).get("process_group") or 0)
                except (TypeError, ValueError):
                    return False
                # A concurrent run-finally must not clear ownership while Stop is
                # still terminating surviving children of the same generation.
                if process_group > 0 and self._host_process_group_alive(process_group):
                    return False
                # Nor while a descendant a Stop of this exact session identified
                # outside the group is not proven gone.
                remembered = self._read_stop_descendant_ledger(worker_id, expected_session)
                if remembered is None or any(
                    not self._recorded_pid_is_proven_gone(pid, identity)
                    for pid, identity in remembered.items()
                ):
                    return False
                if clear_session and current_session is not None:
                    try:
                        session_path.unlink()
                    except FileNotFoundError:
                        return False
                self._active_processes.pop(worker_id, None)
                if not self._release_host_slot_locked(
                    worker_id, expected_slot_token=expected_slot_token
                ):
                    return False
        return True


    def _confirmed_host_control_generation(
        self,
        worker: dict,
        *,
        run_id: str,
        operation: str,
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        """Return exact control identity plus the observed active-session generation."""

        lease = worker.get("_host_run_lease")
        if not isinstance(lease, dict):
            raise RuntimeErrorBase(
                "The exact host process identity is not confirmed"
            )
        worker_id = str(worker["worker_id"])
        with self._active_session_file_lock(worker_id):
            active_session = self._read_active_session(worker_id)
            if isinstance(active_session, dict):
                if not self._host_control_generation_matches(
                    worker=worker,
                    lease=lease,
                    session=active_session,
                    run_id=run_id,
                ):
                    raise RuntimeErrorBase(
                        "The exact host process identity is not confirmed"
                    )
                captured = dict(active_session)
                return captured, captured
            if self._active_session_meta_path(worker_id).exists():
                raise RuntimeErrorBase(
                    "The exact host process identity is not confirmed"
                )

            receipt = self._read_host_control_receipt(worker_id)
            receipt_session = (
                receipt.get("session") if isinstance(receipt, dict) else None
            )
            if (
                not isinstance(receipt, dict)
                or not isinstance(receipt_session, dict)
                or str(receipt.get("operation") or "") != operation
                or str(receipt.get("status") or "") not in {"requested", "confirmed"}
                or not self._host_control_generation_matches(
                    worker=worker,
                    lease=lease,
                    session=receipt_session,
                    run_id=run_id,
                )
            ):
                raise RuntimeErrorBase(
                    "The exact host process identity is not confirmed"
                )
            return dict(receipt_session), None


    def _pidless_host_session_has_exact_terminal_proof(
        self,
        worker_id: str,
        active_session: dict[str, object],
    ) -> bool:
        """Accept a PID-less host session as dead only from its canonical exit artifact."""

        run_id = str(active_session.get("run_id") or "").strip()
        attempt_id = str(active_session.get("attempt_id") or "").strip()
        if (
            not run_id
            or str(active_session.get("session_name") or "").strip()
            != self._session_name_for_run_id(run_id)
        ):
            return False
        canonical_exit = self._attempt_run_root(
            worker_id,
            run_id,
            attempt_id,
        ) / "exit_code"
        recorded_exit = Path(str(active_session.get("exit_path") or "").strip())
        try:
            if recorded_exit.resolve(strict=True) != canonical_exit.resolve(strict=True):
                return False
            raw_exit_code = canonical_exit.read_text().strip()
            exit_code = int(raw_exit_code)
        except (OSError, TypeError, ValueError):
            return False
        if str(exit_code) != raw_exit_code or not 0 <= exit_code <= 255:
            return False
        with self._process_lock:
            process = self._active_processes.get(worker_id)
        return not process or process.poll() is not None


    def _host_slot_bookkeeping_is_absent_locked(self, worker_id: str) -> bool:
        if (
            worker_id in self._host_slot_tokens()
            or worker_id in self._host_worker_lanes()
        ):
            return False
        for raw_active in self._host_active_slots().values():
            if isinstance(raw_active, (set, list, tuple)):
                if worker_id in raw_active:
                    return False
            elif raw_active == worker_id:
                return False
        return True


    def _host_slot_tokens(self) -> dict[str, str]:
        tokens = self.__dict__.get("_viventium_host_slot_tokens")
        if not isinstance(tokens, dict):
            tokens = {}
            self.__dict__["_viventium_host_slot_tokens"] = tokens
        return tokens


    def _host_active_slots(self) -> dict[str, str]:
        slots = self.__dict__.get("_viventium_host_active_slots")
        if not isinstance(slots, dict):
            slots = {}
            self.__dict__["_viventium_host_active_slots"] = slots
        return slots

    def _native_input_context(self, worker: dict) -> dict[str, object] | None:
        return None

    def pending_native_input(self, worker: dict, *, run_id: str) -> dict | None:
        if not self.host_process_identity(worker, run_id):
            return None
        try:
            return native_input.pending(self._run_root(str(worker["worker_id"]), run_id),
                                        worker_id=str(worker["worker_id"]), run_id=run_id)
        except FileNotFoundError:
            return None
        except native_input.NativeInputError as exc:
            raise RuntimeErrorBase(str(exc)) from exc

    def respond_native_input(self, worker: dict, *, run_id: str, request_id: str,
                             request_fingerprint: str, action: str, content: dict | None = None,
                             allow_new: bool = True) -> dict:
        live = allow_new and bool(self.host_process_identity(worker, run_id))
        try:
            return native_input.respond(self._run_root(str(worker["worker_id"]), run_id),
                                        worker_id=str(worker["worker_id"]), run_id=run_id,
                                        request_id=request_id, request_fingerprint=request_fingerprint,
                                        action=action, content=content, allow_new=live)
        except (native_input.NativeInputError, FileNotFoundError) as exc:
            raise RuntimeErrorBase(str(exc) if isinstance(exc, native_input.NativeInputError) else "native_input_stale") from exc

    def _durable_host_process_command(
        self,
        command: list[str],
        *,
        run_root: Path,
        exit_path: Path,
        timeout_sec: float | None = None,
        stdin_path: Path | None = None,
        native_input_context: dict[str, object] | None = None,
        response_deadline_at: str = "",
    ) -> list[str]:
        """Wrap a native CLI so the surviving process owns its terminal marker."""
        if native_input_context is not None:
            context_path = run_root / "native-input-context.json"
            native_input.publish(context_path, native_input_context)
            relay_path = run_root / "native-input-relay.py"
            relay_path.write_bytes(Path(native_input.__file__).read_bytes())
            relay_path.chmod(0o700)
            command = [sys.executable, str(relay_path), str(context_path), *command]
        wrapper_path = run_root / "native-process-supervisor.py"
        wrapper_path.write_text(
            """#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


status_path = Path(sys.argv[1])
start_path = Path(sys.argv[2])
ready_path = Path(sys.argv[3])
timeout_sec = float(sys.argv[4]) if sys.argv[4] else None
stdin_path = Path(sys.argv[5]) if sys.argv[5] else None
response_deadline_at = sys.argv[6]
command = sys.argv[7:]
child: subprocess.Popen[bytes] | None = None
requested_signal = 0


class SupervisorSignal(Exception):
    pass


def write_exit(exit_code: int) -> None:
    temp_path = status_path.with_name(f"{status_path.name}.tmp.{os.getpid()}")
    try:
        temp_path.write_text(f"{exit_code}\\n")
        temp_path.chmod(0o600)
        os.replace(temp_path, status_path)
        status_path.chmod(0o600)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def stop_child(signum: int) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        return


def await_child_stop() -> None:
    if child is None or child.poll() is not None:
        return
    try:
        child.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def response_deadline_expired() -> bool:
    if not response_deadline_at:
        return False
    try:
        deadline = datetime.fromisoformat(response_deadline_at)
    except ValueError:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= deadline


def handle_signal(signum: int, _frame: object) -> None:
    global requested_signal
    requested_signal = signum
    raise SupervisorSignal


for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled_signal, handle_signal)

exit_code = 70
try:
    ready_temp_path = ready_path.with_name(f"{ready_path.name}.tmp.{os.getpid()}")
    ready_temp_path.write_text(f"{os.getpid()}\\n")
    ready_temp_path.chmod(0o600)
    os.replace(ready_temp_path, ready_path)
    ready_path.chmod(0o600)
    handshake_deadline = time.monotonic() + 30
    while not start_path.exists():
        if time.monotonic() >= handshake_deadline:
            sys.stderr.write("GlassHive host-native launch handshake timed out before authoring began.\\n")
            exit_code = 75
            break
        time.sleep(0.05)
    else:
        stdin_handle = stdin_path.open("rb") if stdin_path is not None else open(os.devnull, "rb")
        try:
            if response_deadline_expired():
                sys.stderr.write("Native start was denied after the provider response deadline.\\n")
                exit_code = 75
            else:
                child = subprocess.Popen(command, stdin=stdin_handle, process_group=0)
                try:
                    exit_code = child.wait(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    sys.stderr.write(
                        f"GlassHive host-native process timed out after {timeout_sec:g}s.\\n"
                    )
                    sys.stderr.flush()
                    stop_child(signal.SIGTERM)
                    await_child_stop()
                    exit_code = 124
        finally:
            stdin_handle.close()
except SupervisorSignal:
    for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(handled, signal.SIG_IGN)
    stop_child(requested_signal)
    await_child_stop()
    exit_code = {
        signal.SIGHUP: 129,
        signal.SIGINT: 130,
        signal.SIGTERM: 143,
    }.get(requested_signal, 128 + requested_signal)
finally:
    write_exit(exit_code)

raise SystemExit(exit_code)
"""
        )
        wrapper_path.chmod(0o700)
        return [
            sys.executable,
            str(wrapper_path),
            str(exit_path),
            str(run_root / "start-permit"),
            str(run_root / "supervisor-ready"),
            f"{timeout_sec:g}" if timeout_sec is not None else "",
            str(stdin_path) if stdin_path is not None else "",
            str(response_deadline_at or ""),
            *command,
        ]

    def _wait_for_durable_host_supervisor(
        self,
        process: subprocess.Popen[str],
        *,
        run_root: Path,
    ) -> None:
        def abort_unready(message: str) -> None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
            raise RuntimeErrorBase(message)

        ready_path = run_root / "supervisor-ready"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                ready_pid = int(ready_path.read_text().strip())
            except (OSError, ValueError):
                ready_pid = 0
            if ready_pid == process.pid:
                return
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeErrorBase(
                    f"Host-native process supervisor exited before launch handshake (code {return_code})"
                )
            time.sleep(0.01)
        abort_unready("Host-native process supervisor did not become ready")

    def _release_durable_host_process(self, active_session: dict[str, object]) -> None:
        raw_exit_path = str(active_session.get("exit_path") or "").strip()
        if not raw_exit_path:
            raise RuntimeErrorBase("Host-native process metadata is missing its terminal path")
        exit_path = Path(raw_exit_path)
        if exit_path.exists():
            return
        start_path = exit_path.with_name("start-permit")
        if start_path.exists():
            return
        temp_path = start_path.with_name(
            f"{start_path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        )
        try:
            temp_path.write_text("start\n")
            temp_path.chmod(0o600)
            os.replace(temp_path, start_path)
            start_path.chmod(0o600)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def _read_durable_host_exit_code(self, exit_path: Path) -> int | None:
        try:
            return int(exit_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def _write_durable_host_exit_code(self, exit_path: Path, exit_code: int) -> None:
        """Atomically backfill a marker when the API process outlives the wrapper write."""
        temp_path = exit_path.with_name(
            f"{exit_path.name}.tmp.api-{os.getpid()}-{secrets.token_hex(4)}"
        )
        try:
            temp_path.write_text(f"{exit_code}\n")
            temp_path.chmod(0o600)
            os.replace(temp_path, exit_path)
            exit_path.chmod(0o600)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _final_host_exit_code(self, exit_path: Path, process_exit_code: int) -> int:
        durable_exit_code = self._read_durable_host_exit_code(exit_path)
        if durable_exit_code is not None:
            return durable_exit_code
        self._write_durable_host_exit_code(exit_path, process_exit_code)
        return process_exit_code

    def _host_worker_lanes(self) -> dict[str, str]:
        lanes = self.__dict__.get("_viventium_host_worker_lanes")
        if not isinstance(lanes, dict):
            lanes = {}
            self.__dict__["_viventium_host_worker_lanes"] = lanes
        return lanes

    def _host_capacity_lane(self, worker: dict | None) -> str:
        return "conversation" if self._conversation_mode_from_worker(worker) else "mission"

    def _instruction_with_completion_contract(self, instruction: str) -> str:
        return _instruction_with_completion_contract(instruction)

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        _ = info
        if self._conversation_mode_from_worker(worker):
            return str(instruction or "").strip()
        return self._instruction_with_completion_contract(instruction)

    def _native_image_output_files(self, worker: dict, workspace_dir: str, scope: object, run_id: str):
        image_transport = (
            isinstance(scope, dict)
            and scope.get("output_transport") == "artifact_sha256"
        )
        file_transport = (
            isinstance(scope, dict)
            and scope.get("file_output_transport") == "artifact_sha256"
        )
        if (
            not self._conversation_mode_from_worker(worker)
            or not isinstance(scope, dict) or not run_id or scope.get("run_id") != run_id
            or not (image_transport or file_transport)
            or scope.get("worker_id") != worker.get("worker_id")
            or not worker.get("owner_id") or scope.get("owner_id") != worker.get("owner_id")
            or scope.get("workspace_dir") != workspace_dir
            or (worker.get("_run_attempt_id") and scope.get("attempt_id") != worker["_run_attempt_id"])
        ):
            return []
        if file_transport:
            result: list[tuple[Path, str, bytes]] = []
            total_bytes = 0
            records = scope.get("output_files", [])
            if not isinstance(records, list) or len(records) > NATIVE_MEDIA_MAX_ITEMS:
                return []
            run_root = self._run_root(str(worker["worker_id"]), run_id)
            workspace = Path(workspace_dir)
            for item in records:
                try:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid output artifact descriptor")
                    digest = item.get("sha256")
                    relative = Path(str(item.get("workspace_path") or ""))
                    snapshot = Path(str(item.get("snapshot_path") or ""))
                    mime_type = item.get("mime_type")
                    if (
                        not isinstance(digest, str)
                        or not re.fullmatch(r"[a-f0-9]{64}", digest)
                        or relative.is_absolute()
                        or ".." in relative.parts
                        or any(part.startswith(".") for part in relative.parts)
                        or snapshot.parent != Path("output-files")
                        or snapshot.stem != digest
                        or not isinstance(mime_type, str)
                        or not mime_type
                        or not isinstance(item.get("bytes"), int)
                        or isinstance(item.get("bytes"), bool)
                    ):
                        raise ValueError("Invalid output artifact identity")
                    data = _native_media_snapshot(
                        run_root, snapshot, NATIVE_MEDIA_MAX_BYTES
                    )
                    if (
                        len(data) != item["bytes"]
                        or hashlib.sha256(data).hexdigest() != digest
                    ):
                        raise ValueError("Output artifact snapshot changed")
                    total_bytes += len(data)
                    if total_bytes > NATIVE_MEDIA_MAX_TOTAL_BYTES:
                        raise ValueError("Output artifacts exceed byte limit")
                    result.append((workspace / relative, mime_type, data))
                except (OSError, ValueError, TypeError):
                    return []
            return result
        bundle = self._bootstrap_bundle_for_worker(worker)
        scoped_worker = {**worker, "bootstrap_bundle_json": json.dumps({
            **bundle, "native_input_images": scope.get("images", []),
        })}
        try:
            return self._native_input_image_files(scoped_worker, workspace_dir)
        except RuntimeErrorBase:
            return []  # Existing privacy redaction keeps unavailable local paths hidden.

    def _capture_native_output_files(
        self,
        worker: dict,
        workspace_dir: str,
        scope: dict,
        run_id: str,
        output: str,
    ) -> list[tuple[Path, str, bytes]]:
        """Snapshot only files the model explicitly selected in Markdown output."""
        if (
            not self._conversation_mode_from_worker(worker)
            or scope.get("run_id") != run_id
            or scope.get("worker_id") != worker.get("worker_id")
            or not worker.get("owner_id")
            or scope.get("owner_id") != worker.get("owner_id")
            or scope.get("workspace_dir") != workspace_dir
            or (
                worker.get("_run_attempt_id")
                and scope.get("attempt_id") != worker["_run_attempt_id"]
            )
        ):
            return []
        workspace = Path(workspace_dir)
        selected: list[tuple[Path, str, bytes]] = []
        records: list[dict[str, object]] = []
        seen: set[Path] = set()
        total_bytes = 0
        for match in re.finditer(
            r"(!?\[(?:\\.|[^\]\r\n])*\])\((<[^>\r\n]*>|[^()\r\n]*)\)",
            output,
        ):
            destination = match.group(2).strip()
            if destination.startswith("<") and destination.endswith(">"):
                destination = destination[1:-1]
            destination = unquote(destination)
            if destination.startswith("file://"):
                try:
                    parsed = urlsplit(destination)
                except ValueError:
                    continue
                if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
                    continue
                destination = parsed.path
            candidate = Path(destination)
            if candidate.is_absolute():
                try:
                    relative = candidate.relative_to(workspace)
                except ValueError:
                    continue
            else:
                relative = candidate
            if (
                not relative.parts
                or ".." in relative.parts
                or any(part.startswith(".") for part in relative.parts)
                or relative.parts[0] == "glasshive-run"
                or relative in seen
            ):
                continue
            try:
                data = _native_media_snapshot(
                    workspace, relative, NATIVE_MEDIA_MAX_BYTES
                )
            except (OSError, ValueError):
                continue
            if len(selected) >= NATIVE_MEDIA_MAX_ITEMS:
                break
            total_bytes += len(data)
            if total_bytes > NATIVE_MEDIA_MAX_TOTAL_BYTES:
                break
            digest = hashlib.sha256(data).hexdigest()
            suffix = relative.suffix.lower()
            snapshot = Path("output-files") / f"{digest}{suffix}"
            run_root = self._run_root(str(worker["worker_id"]), run_id)
            snapshot_path = run_root / snapshot
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.parent.chmod(0o700)
            temporary = snapshot_path.with_name(
                f".{snapshot_path.name}.{secrets.token_hex(8)}.tmp"
            )
            try:
                temporary.write_bytes(data)
                temporary.chmod(0o600)
                os.replace(temporary, snapshot_path)
                snapshot_path.chmod(0o600)
            finally:
                temporary.unlink(missing_ok=True)
            mime_type = mimetypes.guess_type(relative.name)[0] or "application/octet-stream"
            records.append(
                {
                    "workspace_path": relative.as_posix(),
                    "snapshot_path": snapshot.as_posix(),
                    "mime_type": mime_type,
                    "bytes": len(data),
                    "sha256": digest,
                }
            )
            selected.append((workspace / relative, mime_type, data))
            seen.add(relative)
        if records:
            durable_scope = {**scope, "output_files": records}
            _atomic_write_private_text(
                self._run_root(str(worker["worker_id"]), run_id)
                / "native-image-scope.json",
                json.dumps(durable_scope, sort_keys=True),
            )
            scope.clear()
            scope.update(durable_scope)
        return selected

    def provider_native_image_output(self, worker: dict, run: dict, output: str):
        run_id = str(run.get("run_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id) or run.get("worker_id") != worker.get("worker_id"):
            return output, []
        scope = self._read_native_image_scope(str(worker["worker_id"]), run_id)
        if str(scope.get("attempt_id") or "") != str(run.get("active_attempt_id") or ""):
            return output, []
        workspace_dir = str(worker.get("workspace_dir") or "")
        images = self._native_image_output_files(worker, workspace_dir, scope, run_id)
        return self._render_native_image_paths(output, images, workspace_dir), images

    def _project_native_image_output(
        self, worker: dict, info: RuntimeInfo, output: str, scope: object, run_id: str
    ) -> str:
        """Resolve only selected destinations when this provider owns the ref transport."""
        if "](" not in output:
            return output
        images = self._native_image_output_files(worker, str(info.workspace_dir or ""), scope, run_id)
        if (
            not images
            and isinstance(scope, dict)
            and scope.get("file_output_transport") == "artifact_sha256"
        ):
            images = self._capture_native_output_files(
                worker,
                str(info.workspace_dir or ""),
                scope,
                run_id,
                output,
            )
        return self._render_native_image_paths(output, images, str(info.workspace_dir or ""))

    @staticmethod
    def _render_native_image_paths(output: str, images, workspace_dir: str) -> str:
        workspace = Path(workspace_dir)
        references: dict[str, str] = {}
        for path, _mime_type, data in images:
            reference = "artifact_sha256:" + hashlib.sha256(data).hexdigest()
            relative = path.relative_to(workspace).as_posix()
            for alias in (str(path), relative, "./" + relative):
                references[alias] = reference

        def render(match: re.Match[str]) -> str:
            destination = match.group(2).strip()
            if destination.startswith("<") and destination.endswith(">"):
                destination = destination[1:-1]
            destination = unquote(destination)
            if destination.startswith("file://"):
                try:
                    parsed = urlsplit(destination)
                except ValueError:
                    return match.group(0)
                if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
                    return match.group(0)
                destination = parsed.path
            reference = references.get(destination)
            return f"{match.group(1)}({reference})" if reference else match.group(0)

        return re.sub(r"(!?\[(?:\\.|[^\]\r\n])*\])\((<[^>\r\n]*>|[^()\r\n]*)\)", render, output)

    def _native_input_image_files(self, worker: dict, info: RuntimeInfo | str) -> list[tuple[Path, str, bytes]]:
        if not self._conversation_mode_from_worker(worker):
            return []
        images = self._bootstrap_bundle_for_worker(worker).get("native_input_images", [])
        if not isinstance(images, list) or len(images) > NATIVE_MEDIA_MAX_ITEMS:
            raise RuntimeErrorBase("Native image input descriptor is invalid")
        workspace = Path(info if isinstance(info, str) else str(info.workspace_dir or ""))
        result = []
        total_bytes = 0
        for item in images:
            try:
                if not isinstance(item, dict):
                    raise ValueError("Invalid image descriptor")
                digest = item.get("sha256")
                mime_type = item.get("type")
                relative = Path(str(item.get("path") or ""))
                if (
                    not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or not isinstance(mime_type, str) or mime_type not in NATIVE_IMAGE_SUFFIXES
                    or relative != Path("uploads/native-images") / (digest + NATIVE_IMAGE_SUFFIXES[mime_type])
                    or not isinstance(item.get("bytes"), int) or isinstance(item.get("bytes"), bool)
                ):
                    raise ValueError("Invalid image identity")
                data = _native_media_snapshot(workspace, relative, NATIVE_MEDIA_MAX_BYTES)
                if len(data) != item.get("bytes") or hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError("Native image input bytes changed")
                total_bytes += len(data)
                if total_bytes > NATIVE_MEDIA_MAX_TOTAL_BYTES:
                    raise ValueError("Native image input exceeds byte limit")
                result.append((workspace / relative, mime_type, data))
            except (OSError, ValueError) as exc:
                raise RuntimeErrorBase("Native image input is unavailable or changed") from exc
        return result

    def _run_mode_from_worker(self, worker: dict | None) -> str:
        if not isinstance(worker, dict):
            return "mission"
        return (
            "conversation"
            if str(worker.get("trusted_run_lane") or "").strip().lower()
            == "conversation"
            else "mission"
        )

    def _conversation_mode_from_worker(self, worker: dict | None) -> bool:
        return self._run_mode_from_worker(worker) == "conversation"

    def _conversation_evidence_workspace(self, worker: dict, run_id: str) -> Path:
        path = self._run_root(str(worker["worker_id"]), run_id) / "private-evidence"
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    def _conversation_workspace_side_effect_state(self, workspace: Path) -> dict[str, bool]:
        _ = workspace
        return {}

    def _cleanup_conversation_workspace_side_effects(
        self,
        workspace: Path,
        state: dict[str, bool],
    ) -> None:
        _ = (workspace, state)

    def _agent_type(self) -> str:
        if self.runtime_name == "codex-cli":
            return "codex"
        if self.runtime_name == "claude-code":
            return "claude"
        return "openclaw"

    def _state_dir(self, worker_id: str) -> Path:
        return self.workers_dir / worker_id / "state"

    def _home_dir(self, worker_id: str) -> Path:
        return self.workers_dir / worker_id / "home"

    def _workspace_dir(self, worker_id: str) -> Path:
        return self.workers_dir / worker_id / "workspace"

    def _worker_root(self, worker_id: str) -> Path:
        return self.workers_dir / worker_id

    def _container_run_root(self, run_id: str) -> str:
        return str(self._home_dir("unknown") / ".glasshive-runs" / run_id)

    def _host_workspace_root(self, worker: dict) -> Path:
        raw = (
            str(worker.get("workspace_root") or "").strip()
            or os.environ.get("WPR_HOST_WORKSPACE_ROOT", "").strip()
            or "~/viventium"
        )
        return Path(raw).expanduser()

    def _admitted_host_workspace(self, worker: dict) -> Path | None:
        """Read this worker's exact durable process admission, never model output."""
        worker_id = str(worker["worker_id"])
        active = self._read_active_session(worker_id) or {}
        expected_run = str(worker.get("_active_run_id") or worker.get("last_run_id") or "")
        run_id = str(active.get("run_id") or expected_run)
        if not run_id or (expected_run and run_id != expected_run):
            return None
        # The private session must still refer to this worker/run's owned transcript.
        expected_stdout = str(self._run_root(worker_id, run_id) / "stdout.log")
        if active and str(active.get("stdout_path") or "") != expected_stdout:
            return None
        raw = str(active.get("workspace_dir") or "")
        if raw:
            return Path(raw) if Path(raw).is_absolute() else None
        # Older admissions already record the process cwd in the action audit.
        # Require the exact run and its matching runtime-authored heartbeat path.
        try:
            with self._action_audit_path(worker_id).open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                start = max(0, handle.tell() - 1024 * 1024)
                handle.seek(start)
                if start:
                    handle.readline()
                lines = handle.read().splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or not (
                record.get("kind") == "run.started"
                and record.get("worker_id") == worker_id
                and record.get("run_id") == run_id
                and record.get("runtime") == self.runtime_name
                and record.get("execution_mode") == "host"
            ):
                continue
            raw = str(record.get("cwd") or "")
            path = Path(raw)
            if not path.is_absolute():
                continue
            heartbeat_path = _active_run_status_path(path, run_id)
            if active:
                if str(active.get("heartbeat_path") or "") == str(heartbeat_path):
                    return path
                continue
            # Idle release clears active process metadata, not the run's durable
            # admission. Require the owned heartbeat to agree before any path use.
            try:
                if heartbeat_path.stat().st_size > 128 * 1024:
                    continue
                heartbeat = json.loads(heartbeat_path.read_text())
            except (OSError, ValueError):
                continue
            if (isinstance(heartbeat, dict)
                    and heartbeat.get("schema") == "glasshive.active_run.v1"
                    and heartbeat.get("run_id") == run_id
                    and heartbeat.get("runtime") == self.runtime_name
                    and isinstance(heartbeat.get("worker"), dict)
                    and heartbeat["worker"].get("worker_id") == worker_id
                    and isinstance(heartbeat.get("transcript_paths"), dict)
                    and heartbeat["transcript_paths"].get("stdout") == expected_stdout):
                return path
        return None

    def _host_workspace_dir(self, worker: dict) -> Path:
        admitted = self._admitted_host_workspace(worker)
        if admitted is not None:
            return admitted
        existing = str(worker.get("workspace_dir") or "").strip()
        if existing:
            return Path(existing).expanduser()
        root = self._host_workspace_root(worker)
        if self._conversation_mode_from_worker(worker):
            return root
        # A mission receives its run ID at acceptance, before runtime setup.
        # Runtime identity, not queued work, proves an existing workspace.
        worker_id = str(worker["worker_id"])
        if worker.get("last_run_id") and (
                worker.get("state_dir") or worker.get("session_key")
                or self._active_session_meta_path(worker_id).exists()
                or self._action_audit_path(worker_id).exists()):
            raise RuntimeErrorBase("The existing native worker's admitted workspace is unavailable")
        created_at = str(worker.get("created_at") or "").strip()
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00")) if created_at else datetime.now()
        date_prefix = created.strftime("%Y-%m-%d")
        alias = str(worker.get("alias") or worker.get("name") or worker.get("worker_id") or "project")
        slug = _safe_slug(alias)
        worker_suffix = _safe_slug(str(worker.get("worker_id") or "worker"))
        return root / self._agent_type() / f"{date_prefix}-{slug}-{worker_suffix}"

    @worker_prompt_layer_producer("project_definition")
    def _host_project_definition(self, worker: dict) -> str:
        bundle = self._bootstrap_bundle_for_worker(worker)
        candidate = (
            bundle.get("project_definition")
            or bundle.get("task")
            or bundle.get("goal")
            or bundle.get("system_instructions")
            or ""
        )
        body = str(candidate or "").strip()
        if body:
            return body
        return (
            f"# {worker.get('name') or 'GlassHive host worker'}\n\n"
            f"- Worker: {worker.get('worker_id')}\n"
            f"- Agent type: {self._agent_type()}\n"
            f"- Role: {worker.get('role') or 'worker'}\n"
        )

    @worker_prompt_layer_producer("harness_prompt")
    def _host_harness_prompt(self, worker: dict) -> str:
        bundle = self._bootstrap_bundle_for_worker(worker)
        extra = str(bundle.get("system_instructions") or "").strip()
        prompt = HOST_NATIVE_HARNESS_PROMPT.rstrip()
        if extra:
            prompt += "\n\nHost-provided instructions:\n" + extra
        return prompt.strip() + "\n"

    def _bootstrap_bundle_for_worker(self, worker: dict) -> dict[str, object]:
        raw = worker.get("bootstrap_bundle_json")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                return (
                    canonicalize_viventium_feeling_projection(parsed)
                    if isinstance(parsed, dict)
                    else {}
                )
            except json.JSONDecodeError:
                return {}
        raw_bundle = worker.get("bootstrap_bundle")
        return (
            canonicalize_viventium_feeling_projection(raw_bundle)
            if isinstance(raw_bundle, dict)
            else {}
        )

    def _conversation_output_schema(self, worker: dict) -> dict[str, object] | None:
        if not self._conversation_mode_from_worker(worker):
            return None
        bundle = self._bootstrap_bundle_for_worker(worker)
        schema = conversation_output_schema(
            bundle.get("agent_builder_control"),
            bundle.get("messaging_delivery_control"),
        )
        return schema if isinstance(schema, dict) else None

    def _conversation_output_schema_path(
        self,
        worker: dict,
    ) -> Path | None:
        schema = self._conversation_output_schema(worker)
        if not schema:
            return None
        path = self._state_dir(str(worker["worker_id"])) / "conversation-output-schema.json"
        if not _write_private_json(path, schema):
            raise RuntimeErrorBase("Agent Builder control schema is unavailable")
        return path

    def _write_workspace_file(self, workspace: Path, relative_path: str, content: str, *, overwrite: bool = True) -> None:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeErrorBase(f"Unsafe bootstrap path: {relative_path}")
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not target.exists():
            target.write_text(content)

    def _write_workspace_bytes(self, workspace: Path, relative_path: str, content: bytes, *, overwrite: bool = True) -> None:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeErrorBase(f"Unsafe bootstrap path: {relative_path}")
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not target.exists():
            target.write_bytes(content)

    def _copy_workspace_source_file(self, workspace: Path, relative_path: str, source: Path) -> None:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeErrorBase(f"Unsafe bootstrap path: {relative_path}")
        source = resolve_bootstrap_source_path(source)
        if not source.exists():
            raise RuntimeErrorBase(f"Bootstrap source file not found: {source}")
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)

    def _bootstrap_file_allows_empty(self, item: dict[str, object]) -> bool:
        value = item.get("allow_empty")
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _require_non_empty_bootstrap_file(self, path: str, size: int, item: dict[str, object]) -> None:
        if size <= 0 and not self._bootstrap_file_allows_empty(item):
            raise RuntimeErrorBase(f"Bootstrap file {path} is empty; set allow_empty=true to materialize an empty file")

    def _source_path_from_bootstrap_file(self, item: dict[str, object]) -> Path | None:
        for key in ("source_path", "local_path", "upload_path", "absolute_path", "filepath"):
            value = str(item.get(key) or "").strip()
            if value:
                return Path(value).expanduser()
        return None

    def _host_codex_home(self, worker: dict) -> Path:
        return self._home_dir(worker["worker_id"]) / ".codex"

    def _host_plugin_denylist(self) -> tuple[str, ...]:
        return _host_plugin_denylist()

    def _host_codex_personality(self) -> str:
        return _host_codex_personality()

    def _source_host_codex_home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()

    def _copy_host_codex_auth(self, target_codex_home: Path) -> None:
        if native_installed_owner_id() is not None:
            self._link_native_codex_auth(target_codex_home)
            return
        source_auth = self._source_host_codex_home() / "auth.json"
        if not source_auth.exists() or not source_auth.is_file():
            return
        target_codex_home.mkdir(parents=True, exist_ok=True)
        target_auth = target_codex_home / "auth.json"
        shutil.copy2(source_auth, target_auth)
        target_auth.chmod(0o600)

    def _link_native_codex_auth(self, target_codex_home: Path) -> None:
        """Keep the CLI-owned credential file shared; worker authority stays isolated."""
        source_home = self._source_host_codex_home()
        source_auth = source_home / "auth.json"
        try:
            for directory in (source_home, target_codex_home):
                metadata = directory.lstat()
                if (not directory.is_absolute() or directory.resolve(strict=True) != directory
                    or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700):
                    raise ValueError("unsafe native auth directory")
            metadata = source_auth.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError("unsafe native auth file")
            target_auth = target_codex_home / "auth.json"
            if target_auth.is_symlink():
                if target_auth.readlink() != source_auth:
                    raise ValueError("unrelated native auth link")
            elif target_auth.exists():
                raise ValueError("worker retains a separate credential file")
            else:
                target_auth.symlink_to(source_auth)
        except (OSError, ValueError) as exc:
            raise RuntimeErrorBase("Installed Codex login is unavailable or unsafe; reconnect through the installed native owner.") from exc

    def _project_private_capability_directory(self, source: Path, target: Path) -> None:
        """Expose an installed, read-mostly capability catalog without copying it into LIFE.

        Existing worker-local state wins. A stale derived symlink is repaired, while a real file or
        directory is never replaced. The containing worker home is owner-only.
        """
        if not source.exists() or not source.is_dir():
            return
        resolved_source = source.resolve()
        if target.is_symlink():
            try:
                if target.resolve() == resolved_source:
                    return
            except OSError:
                pass
            target.unlink()
        elif target.exists():
            if not target.is_dir():
                return
            # Older worker homes may already contain harness-created catalog entries
            # (for example Codex's `.system` skills). Preserve those entries and project
            # only missing host capabilities so an upgrade enriches the worker instead of
            # replacing or silently skipping its local catalog.
            try:
                source_entries = sorted(source.iterdir(), key=lambda item: item.name)
            except OSError:
                return
            for source_entry in source_entries:
                target_entry = target / source_entry.name
                resolved_entry = source_entry.resolve()
                if target_entry.is_symlink():
                    try:
                        if target_entry.resolve() == resolved_entry:
                            continue
                    except OSError:
                        pass
                    target_entry.unlink()
                elif target_entry.exists():
                    continue
                target_entry.symlink_to(
                    resolved_entry,
                    target_is_directory=source_entry.is_dir(),
                )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(resolved_source, target_is_directory=True)

    def _project_host_codex_capability_roots(self, target_codex_home: Path) -> None:
        source_home = self._source_host_codex_home()
        self._project_private_capability_directory(
            source_home / "skills", target_codex_home / "skills"
        )
        self._project_private_capability_directory(
            source_home / "plugins" / "cache",
            target_codex_home / "plugins" / "cache",
        )

    def _source_host_claude_home(self) -> Path:
        raw = os.environ.get("GLASSHIVE_HOST_CLAUDE_CONFIG", "").strip()
        return Path(raw).expanduser() if raw else Path.home() / ".claude"

    def _merge_private_capability_registry(self, source: Path, target: Path) -> None:
        """Add host registry entries without replacing worker-local selections."""
        if not source.exists() or not source.is_file():
            return
        if target.is_symlink():
            target.unlink()
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            target.chmod(0o600)
            return
        if not target.is_file():
            return
        try:
            source_payload = json.loads(source.read_text())
            target_payload = json.loads(target.read_text())
        except (OSError, json.JSONDecodeError):
            return

        def merge_additive(current: object, incoming: object) -> object:
            if isinstance(current, dict) and isinstance(incoming, dict):
                merged = dict(current)
                for key, value in incoming.items():
                    merged[key] = merge_additive(merged[key], value) if key in merged else value
                return merged
            if isinstance(current, list) and isinstance(incoming, list):
                merged = list(current)
                for value in incoming:
                    if value not in merged:
                        merged.append(value)
                return merged
            return current

        merged_payload = merge_additive(target_payload, source_payload)
        if merged_payload == target_payload:
            return
        target.write_text(json.dumps(merged_payload, indent=2, sort_keys=True) + "\n")
        target.chmod(0o600)

    def _project_host_claude_capability_roots(self, target_claude_home: Path) -> None:
        source_home = self._source_host_claude_home()
        for relative in (Path("plugins/cache"), Path("plugins/marketplaces"), Path("skills")):
            self._project_private_capability_directory(
                source_home / relative,
                target_claude_home / relative,
            )
        for filename in ("installed_plugins.json", "known_marketplaces.json"):
            source = source_home / "plugins" / filename
            target = target_claude_home / "plugins" / filename
            self._merge_private_capability_registry(source, target)

    def _host_codex_native_mcp_allowlist(self) -> set[str]:
        raw = os.environ.get(
            "GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST",
            os.environ.get("WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST", ""),
        ).strip()
        if not raw:
            return set(_HOST_CODEX_NATIVE_MCP_ALLOWLIST)
        if raw.lower() in {"0", "false", "no", "off", "none", "disabled"}:
            return set()
        return {
            item.strip()
            for item in raw.split(",")
            if item.strip() and re.fullmatch(r"[A-Za-z0-9_.-]+", item.strip())
        }

    def _host_codex_plugin_cache_root(self) -> Path:
        raw = os.environ.get(
            "GLASSHIVE_HOST_CODEX_PLUGIN_CACHE",
            os.environ.get("WPR_HOST_CODEX_PLUGIN_CACHE", ""),
        ).strip()
        if raw:
            return Path(raw).expanduser()
        return self._source_host_codex_home() / "plugins" / "cache"

    def _host_codex_bundled_mcp_config(self, names: set[str], source_config: str = "") -> str:
        if not names:
            return ""
        cache_root = self._host_codex_plugin_cache_root()
        if not cache_root.exists():
            return ""
        try:
            selections = tomllib.loads(source_config).get("plugins", {})
        except (TypeError, tomllib.TOMLDecodeError):
            selections = {}
        if not isinstance(selections, dict):
            selections = {}
        blocks: list[str] = []
        found: set[str] = set()
        for manifest in sorted(cache_root.rglob(".mcp.json")):
            # A cached package is not an enabled installation. Only project a native
            # server from a plugin the user currently enabled in the source config.
            parts = manifest.relative_to(cache_root).parts
            if len(parts) < 4:
                continue
            plugin_id = f"{parts[1]}@{parts[0]}"
            selected = selections.get(plugin_id)
            if plugin_id in self._host_plugin_denylist():
                continue
            if not isinstance(selected, dict) or selected.get("enabled") is not True:
                continue
            try:
                payload = json.loads(manifest.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            servers = payload.get("mcpServers") if isinstance(payload, dict) else None
            if not isinstance(servers, dict):
                continue
            for name in sorted(names - found):
                if name not in servers:
                    continue
                rendered = _render_codex_mcp_server_from_json(name, servers[name], manifest.parent)
                if rendered:
                    blocks.append(rendered)
                    found.add(name)
            if found >= names:
                break
        return "\n\n".join(blocks).strip()

    def _host_codex_known_native_mcp_config(self, names: set[str]) -> str:
        blocks: list[str] = []
        if "computer-use" in names:
            computer_use_client = (
                self._source_host_codex_home()
                / "computer-use"
                / "Codex Computer Use.app"
                / "Contents"
                / "SharedSupport"
                / "SkyComputerUseClient.app"
                / "Contents"
                / "MacOS"
                / "SkyComputerUseClient"
            )
            if computer_use_client.exists():
                blocks.append(
                    "[mcp_servers.computer-use]\n"
                    f"command = {_toml_string(computer_use_client)}\n"
                    "args = [\"mcp\"]"
                )
        return "\n\n".join(blocks).strip()

    def _host_codex_worker_config(
        self,
        codex_config_append: str,
        *,
        developer_instructions: str | None = None,
        capability_bundle: dict[str, object] | None = None,
    ) -> str:
        if _native_tools_disabled(capability_bundle or {}):
            broker = capability_bundle.get("glasshive_capability_broker")
            env = capability_bundle.get("env")
            if (not isinstance(broker, dict) or broker.get("version") != 1
                or not isinstance(broker.get("name"), str) or not broker["name"].strip()
                or not isinstance(broker.get("url"), str) or not broker["url"].strip()
                or not isinstance(env, dict) or not env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN")):
                raise RuntimeErrorBase("Restricted native execution requires its signed capability broker")
            return _render_toml_document({
                "developer_instructions": developer_instructions or "",
                "sandbox_mode": "read-only",
                "approval_policy": "never",
                "web_search": "disabled",
                "project_doc_max_bytes": 0,
                # The tool engine forwards the signed broker's calls; it does not grant host tools.
                "features": {"code_mode_host": True,
                    **{name: False for name in _CODEX_RESTRICTED_NATIVE_FEATURES}},
                "mcp_servers": {broker["name"]: {
                    "url": broker["url"],
                    # The signed broker owns the admitted operation's authorization.
                    # Codex's annotation-based "auto" still prompts under read-only.
                    "default_tools_approval_mode": "approve",
                    "bearer_token_env_var": "GLASSHIVE_CAPABILITY_BROKER_TOKEN",
                }},
            })
        append = codex_config_append.strip()
        append_names = _codex_mcp_server_names(append)
        native_web_locked = _host_native_web_access() == "disabled"
        preserve_names = (
            set() if native_web_locked else self._host_codex_native_mcp_allowlist() - append_names
        )
        source_config_path = self._source_host_codex_home() / "config.toml"
        preserved = ""
        source_config = ""
        if (
            not native_web_locked
            and source_config_path.exists()
            and source_config_path.is_file()
        ):
            try:
                source_config = source_config_path.read_text()
            except OSError:
                source_config = ""
            preserved = _sanitize_codex_source_config(source_config, preserve_names, append_names)
        preserved_names = _codex_mcp_server_names(preserved)
        plugin_preserved = self._host_codex_bundled_mcp_config(
            preserve_names - preserved_names, source_config
        )
        plugin_names = _codex_mcp_server_names(plugin_preserved)
        known_native = self._host_codex_known_native_mcp_config(
            preserve_names - preserved_names - plugin_names
        )
        native = "\n\n".join(
            part for part in (preserved, plugin_preserved, known_native) if part.strip()
        ).strip()
        # Native stdio tools connect to services of the signed-in OS owner. Their
        # default HOME must not become the worker's isolated conversation home.
        # Preserve an explicitly configured server HOME; never change worker state.
        native_payload = tomllib.loads(native) if native else {}
        native_servers = native_payload.get("mcp_servers", {})
        for name in preserve_names:
            server = native_servers.get(name) if isinstance(native_servers, dict) else None
            if not isinstance(server, dict) or not server.get("command") or server.get("enabled") is False:
                continue
            server.setdefault("env", {}).setdefault("HOME", str(Path.home()))
        native = _render_toml_document(native_payload) if native_payload else ""
        personality = self._host_codex_personality()
        native = _apply_codex_personality(native, personality)
        native = _apply_codex_developer_instructions(native, developer_instructions)
        denied_plugins = self._host_plugin_denylist()
        native = _apply_codex_plugin_denylist(native, denied_plugins)
        if append_names:
            native = _strip_codex_mcp_server_blocks(native, append_names)
        config = "\n\n".join(part for part in (native, append) if part.strip()).strip()
        _assert_codex_worker_policy(
            config,
            plugin_ids=denied_plugins,
            personality=personality,
            developer_instructions=developer_instructions,
        )
        return config

    def _assert_host_codex_worker_policy(self, worker: dict) -> None:
        denied_plugins = self._host_plugin_denylist()
        personality = self._host_codex_personality()
        bundle = self._bootstrap_bundle_for_worker(worker)
        developer_instructions = (
            str(bundle.get("developer_instructions") or "")
            if self._conversation_mode_from_worker(worker)
            and "developer_instructions" in bundle
            else None
        )
        if _native_tools_disabled(bundle):
            config_path = self._host_codex_home(worker) / "config.toml"
            expected = self._host_codex_worker_config(
                "", developer_instructions=developer_instructions, capability_bundle=bundle,
            )
            try:
                actual = config_path.read_text()
            except OSError as exc:
                raise RuntimeErrorBase("Restricted native policy is unavailable; refusing to launch") from exc
            if actual.strip() != expected.strip():
                raise RuntimeErrorBase("Restricted native policy changed; refusing to launch")
            return
        if not denied_plugins and personality == "inherit" and developer_instructions is None:
            return
        config_path = self._host_codex_home(worker) / "config.toml"
        if not config_path.is_file():
            raise RuntimeErrorBase(
                "Host Codex worker policy config is missing; refusing to launch"
            )
        try:
            config_text = config_path.read_text()
        except OSError as exc:
            raise RuntimeErrorBase(
                "Host Codex worker policy config is unreadable; refusing to launch"
            ) from exc
        _assert_codex_worker_policy(
            config_text,
            plugin_ids=denied_plugins,
            personality=personality,
            developer_instructions=developer_instructions,
        )

    def _host_claude_project_mcp_payload(
        self, bundle: dict[str, object], project_mcp: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        from .native_mcp_tool_filter import selected_filter

        payload = claude_project_mcp_payload_for_bundle(bundle, project_mcp)
        filters: dict[str, object] = {}
        if (_native_tools_disabled(bundle)
            or str(bundle.get("access_mode") or "full").strip().lower() != "full"):
            return payload, filters
        native = tomllib.loads(self._host_codex_worker_config(""))
        servers = payload.setdefault("mcpServers", {})
        for name, server in native.get("mcp_servers", {}).items():
            if (name in servers or not isinstance(server, dict)
                or server.get("enabled") is False or not server.get("command")):
                continue
            # Claude has no per-server cwd selector. Tool restrictions are enforced
            # separately on the selected MCP child's stdio transport.
            if server.get("cwd"):
                logger.warning("Native MCP %s has settings unsupported by Claude; not projected", name)
                continue
            rule = selected_filter(server)
            if rule is not None:
                filters[name] = rule
            env = dict(server.get("env") or {})
            for key in server.get("env_vars") or []:
                if key in os.environ:
                    env.setdefault(key, os.environ[key])
            servers[name] = {"type": "stdio", "command": server["command"],
                             "args": list(server.get("args") or []), "env": env}
        return payload, filters

    def _write_host_claude_mcp_config(
        self, worker: dict, target: Path, bundle: dict[str, object], project_mcp: dict,
    ) -> None:
        payload, filters = self._host_claude_project_mcp_payload(bundle, project_mcp)
        state = self._state_dir(str(worker["worker_id"]))
        state.mkdir(parents=True, exist_ok=True)
        policies = {}
        transport = Path(__file__).with_name("native_mcp_tool_filter.py")
        for name, rule in filters.items():
            server = payload["mcpServers"][name]
            policy = state / ("native-mcp-tools-" + hashlib.sha256(name.encode()).hexdigest() + ".json")
            data = (json.dumps({"command": server["command"], "args": server["args"],
                "filter": rule}, sort_keys=True) + "\n").encode()
            policy.write_bytes(data)
            policy.chmod(0o600)
            digest = hashlib.sha256(data).hexdigest()
            server.update(command=sys.executable, args=[str(transport), str(policy), digest])
            policies[name] = {"path": str(policy), "sha256": digest}
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        target.write_bytes(data)
        target.chmod(0o600)
        identity = state / "native-mcp-tool-policy.json"
        identity.write_text(json.dumps({"mcp_path": str(target.resolve()),
            "mcp_sha256": hashlib.sha256(data).hexdigest(), "policies": policies}, sort_keys=True) + "\n")
        identity.chmod(0o600)

    def _assert_host_claude_mcp_config(self, worker: dict, mcp_path: Path) -> None:
        from .native_mcp_tool_filter import read_policy

        identity = self._state_dir(str(worker["worker_id"])) / "native-mcp-tool-policy.json"
        if not identity.exists():
            if mcp_path.is_file():
                raise RuntimeErrorBase("Claude native MCP tool policy is missing; prepare the workspace again")
            return
        try:
            metadata = json.loads(identity.read_bytes())
            if Path(metadata["mcp_path"]) != mcp_path.resolve():
                raise ValueError("Native MCP policy points to a different config")
            data = mcp_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != metadata["mcp_sha256"]:
                raise ValueError("Native MCP config changed after materialization")
            servers = json.loads(data)["mcpServers"]
            transport = Path(__file__).with_name("native_mcp_tool_filter.py")
            for name, policy in metadata["policies"].items():
                read_policy(Path(policy["path"]), policy["sha256"])
                if not transport.is_file() or servers[name]["command"] != sys.executable or servers[name]["args"] != [str(transport), policy["path"], policy["sha256"]]:
                    raise ValueError("Native MCP transport does not match its selected policy")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeErrorBase("Claude native MCP tool policy is unavailable or stale") from exc

    def _write_host_project_mcp_files(self, worker: dict, workspace: Path, bundle: dict[str, object]) -> None:
        """Project scoped MCP/client config for host-native workers.

        Example broker projection:

            {
                "claude_project_mcp": {"glasshive-user-capabilities": {"url": "..."}},
                "codex_config_append": "[mcp_servers.glasshive-user-capabilities]...",
                "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "..."}
            }

        Files are owner-only because they can contain scoped broker grants or local CLI config.
        """
        require_native_installed_owner(worker.get("owner_id"))
        project_mcp = bundle.get("claude_project_mcp")
        if isinstance(project_mcp, dict) or worker.get("profile") == "claude-code":
            target = workspace / ".mcp.json"
            if worker.get("profile") == "claude-code":
                self._write_host_claude_mcp_config(
                    worker, target, bundle, project_mcp if isinstance(project_mcp, dict) else {})
            else:
                payload = claude_project_mcp_payload_for_bundle(bundle, project_mcp)
                target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                target.chmod(0o600)

        settings_local = bundle.get("claude_settings_local")
        if isinstance(settings_local, dict):
            target = workspace / ".claude" / "settings.local.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(settings_local, indent=2, sort_keys=True) + "\n")
            target.chmod(0o600)

        codex_config_append = str(bundle.get("codex_config_append") or "").strip()
        developer_instructions = (
            str(bundle.get("developer_instructions") or "")
            if "developer_instructions" in bundle
            else None
        )
        if (
            codex_config_append
            or self._host_plugin_denylist()
            or self._host_codex_personality() != "inherit"
            or developer_instructions is not None
        ):
            codex_config = self._host_codex_worker_config(
                codex_config_append,
                developer_instructions=developer_instructions,
            )
            target = workspace / ".codex" / "config.toml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(codex_config + "\n")
            target.chmod(0o600)
            codex_home = self._host_codex_home(worker)
            codex_home.mkdir(parents=True, exist_ok=True)
            codex_home.chmod(0o700)
            codex_target = codex_home / "config.toml"
            codex_target.write_text(codex_config + "\n")
            codex_target.chmod(0o600)
            if native_current_account_allowed(worker):
                self._copy_host_codex_auth(codex_home)
            self._project_host_codex_capability_roots(codex_home)

    def _write_conversation_runtime_files(self, worker: dict, bundle: dict[str, object]) -> None:
        """Write harness configuration under private worker state, never inside LIFE."""

        require_native_installed_owner(worker.get("owner_id"))
        profile = str(worker.get("profile") or "").strip()
        if profile == "codex-cli":
            codex_home = self._host_codex_home(worker)
            codex_home.mkdir(parents=True, exist_ok=True)
            codex_home.chmod(0o700)
            codex_config = self._host_codex_worker_config(
                str(bundle.get("codex_config_append") or ""),
                capability_bundle=bundle,
                developer_instructions=(
                    str(bundle.get("developer_instructions") or "")
                    if "developer_instructions" in bundle
                    else None
                ),
            )
            config_path = codex_home / "config.toml"
            config_path.write_text((codex_config.rstrip() + "\n") if codex_config else "")
            config_path.chmod(0o600)
            if native_current_account_allowed(worker):
                self._copy_host_codex_auth(codex_home)
            if not _native_tools_disabled(bundle):
                self._project_host_codex_capability_roots(codex_home)
            return

        if profile != "claude-code":
            return
        claude_home = self._home_dir(str(worker["worker_id"])) / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        claude_home.chmod(0o700)
        self._project_host_claude_capability_roots(claude_home)
        project_mcp = bundle.get("claude_project_mcp")
        state_dir = self._state_dir(str(worker["worker_id"]))
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        authority_path = state_dir / "developer-instructions.txt"
        legacy_authority_path = state_dir / "conversation-developer-instructions.md"
        developer_instructions = (
            str(bundle.get("developer_instructions") or "")
            if "developer_instructions" in bundle
            else ""
        )
        if developer_instructions:
            _atomic_write_private_text(authority_path, developer_instructions)
        else:
            try:
                authority_path.unlink()
            except FileNotFoundError:
                pass
        try:
            legacy_authority_path.unlink()
        except FileNotFoundError:
            pass
        mcp_path = state_dir / "conversation-mcp.json"
        self._write_host_claude_mcp_config(
            worker, mcp_path, bundle, project_mcp if isinstance(project_mcp, dict) else {})


    def _materialize_workspace_files(
        self,
        worker: dict,
        workspace: Path,
        bundle: dict[str, object],
    ) -> None:
        """Materialize only declared workspace files for the current worker/run bundle."""

        for item in bundle.get("files", []) if isinstance(bundle.get("files"), list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("managed_upload_id"):
                from .workspace_files import materialize_managed_file
                materialize_managed_file(workspace, item, worker)
                continue
            if str(item.get("scope") or "workspace") != "workspace":
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            if str(item.get("encoding") or "").strip().lower() == "base64" or "content_base64" in item:
                raw = str(item.get("content_base64") or item.get("content") or "")
                try:
                    decoded = base64.b64decode(raw, validate=True)
                except Exception as exc:
                    raise RuntimeErrorBase(f"Invalid base64 bootstrap content for {path}") from exc
                self._require_non_empty_bootstrap_file(path, len(decoded), item)
                self._write_workspace_bytes(workspace, path, decoded, overwrite=True)
                continue
            if "content" in item:
                content = str(item.get("content") or "")
                self._require_non_empty_bootstrap_file(path, len(content.encode("utf-8")), item)
                self._write_workspace_file(workspace, path, content, overwrite=True)
                continue
            source = self._source_path_from_bootstrap_file(item)
            if source is not None:
                resolved_source = resolve_authorized_bootstrap_source_path(item, source, worker)
                if resolved_source.is_file():
                    self._require_non_empty_bootstrap_file(path, resolved_source.stat().st_size, item)
                self._copy_workspace_source_file(workspace, path, resolved_source)
            else:
                raise RuntimeErrorBase(f"Bootstrap file {path} is missing content or source_path")

    def _materialize_workspace(self, worker: dict, workspace: Path) -> None:
        root = self._host_workspace_root(worker)
        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise RuntimeErrorBase(f"Host workspace root is not writable: {root}")
        workspace.mkdir(parents=True, exist_ok=True)
        if self._conversation_mode_from_worker(worker):
            bundle = self._bootstrap_bundle_for_worker(worker)
            self._materialize_workspace_files(worker, workspace, bundle)
            self._write_conversation_runtime_files(
                worker,
                bundle,
            )
            return
        bundle = self._bootstrap_bundle_for_worker(worker)
        self._write_workspace_file(workspace, "project-definition.md", self._host_project_definition(worker), overwrite=False)
        if not (workspace / "work-log.md").exists():
            self._write_workspace_file(
                workspace,
                "work-log.md",
                f"# Work Log\n\n- {datetime.now().isoformat(timespec='seconds')}: Workspace initialized for {self._agent_type()}.\n",
                overwrite=False,
            )
        self._write_workspace_file(workspace, "harness-prompt.md", self._host_harness_prompt(worker), overwrite=True)

        agents_md = merge_glasshive_worker_instructions(HOST_DEFAULT_AGENTS_MD, bundle.get("agents_md"))
        claude_bundle = dict(bundle)
        if not str(claude_bundle.get("claude_md") or "").strip():
            claude_bundle["claude_md"] = HOST_DEFAULT_CLAUDE_MD
        claude_md = glasshive_project_claude_md(claude_bundle)
        codex_bundle = dict(bundle)
        if not str(codex_bundle.get("codex_md") or "").strip():
            codex_bundle["codex_md"] = HOST_DEFAULT_CODEX_MD
        codex_md = glasshive_project_codex_md(codex_bundle)
        for name, content in (
            ("agents.md", agents_md),
            ("AGENTS.md", agents_md),
            ("claude.md", claude_md),
            ("CLAUDE.md", claude_md),
            ("codex.md", codex_md),
            ("CODEX.md", codex_md),
        ):
            self._write_workspace_file(workspace, name, content, overwrite=True)
        self._write_host_project_mcp_files(worker, workspace, bundle)

        self._write_workspace_file(
            workspace,
            "glasshive-host-tools/capture-front-window.sh",
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "if [[ $# -lt 1 || $# -gt 2 ]]; then",
                    '  echo "Usage: $0 <output-path> [app-name]" >&2',
                    "  exit 1",
                    "fi",
                    "OUT_PATH=$1",
                    "APP_NAME=${2:-}",
                    'if [[ -z "$APP_NAME" ]]; then',
                    "  APP_NAME=$(/usr/bin/osascript -e 'tell application \"System Events\" to get name of first application process whose frontmost is true')",
                    "fi",
                    "BOUNDS=$(",
                    '  /usr/bin/osascript - "$APP_NAME" <<\'APPLESCRIPT\'',
                    "on run argv",
                    "  set appName to item 1 of argv",
                    '  tell application "System Events"',
                    "    tell process appName",
                    "      set frontmost to true",
                    "      set p to position of window 1",
                    "      set s to size of window 1",
                    '      return (item 1 of p as text) & "," & (item 2 of p as text) & "," & (item 1 of s as text) & "," & (item 2 of s as text)',
                    "    end tell",
                    "  end tell",
                    "end run",
                    "APPLESCRIPT",
                    ")",
                    'IFS=, read -r X Y W H <<<"$BOUNDS"',
                    'mkdir -p "$(dirname "$OUT_PATH")"',
                    'if [[ "${H:-0}" -lt 100 || "${W:-0}" -lt 100 ]]; then',
                    '  /usr/sbin/screencapture "$OUT_PATH"',
                    '  echo "captured full screen to $OUT_PATH (window bounds looked invalid for $APP_NAME: $BOUNDS)"',
                    "  exit 0",
                    "fi",
                    '/usr/sbin/screencapture -R"${X},${Y},${W},${H}" "$OUT_PATH"',
                    'echo "captured $APP_NAME window to $OUT_PATH"',
                ]
            )
            + "\n",
            overwrite=True,
        )
        self._write_workspace_file(
            workspace,
            "glasshive-host-tools/content-hygiene.py",
            HOST_CONTENT_HYGIENE_TOOL,
            overwrite=True,
        )
        capture_helper = workspace / "glasshive-host-tools" / "capture-front-window.sh"
        content_hygiene_helper = workspace / "glasshive-host-tools" / "content-hygiene.py"
        try:
            capture_helper.chmod(0o755)
            content_hygiene_helper.chmod(0o755)
        except OSError as exc:
            self._append_work_log(worker, f"WARNING: capture helper chmod failed: {exc}")
        try:
            subprocess.run(
                ["/usr/bin/xattr", "-d", "com.apple.quarantine", str(capture_helper)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._append_work_log(worker, "WARNING: capture helper quarantine cleanup could not run; invoke it through bash.")

        self._materialize_workspace_files(worker, workspace, bundle)

    def _append_work_log(self, worker: dict, message: str) -> None:
        if self._conversation_mode_from_worker(worker):
            return
        path = self._host_workspace_dir(worker) / "work-log.md"
        try:
            with path.open("a") as handle:
                handle.write(f"- {datetime.now().isoformat(timespec='seconds')}: {message}\n")
        except OSError:
            return

    def _action_audit_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "action-audit.jsonl"

    def _write_action_audit(self, worker: dict, payload: dict[str, object]) -> None:
        worker_id = worker["worker_id"]
        path = self._action_audit_path(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "worker_id": worker_id,
            "runtime": self.runtime_name,
            "execution_mode": "host",
            **payload,
        }
        with path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        path.chmod(0o600)

    def _host_env(self, worker: dict, run_id: str | None = None) -> dict[str, str]:
        require_native_installed_owner(worker.get("owner_id"))
        env: dict[str, str] = {}
        # USER/LOGNAME are required for macOS Keychain-backed CLIs (e.g. claude-code's
        # subscription auth resolves the keychain item by user); without them the worker
        # reports "Not logged in". Codex is unaffected because it uses a copied auth.json.
        for key in ("HOME", "PATH", "SHELL", "TERM", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        for key, value in os.environ.items():
            if key.startswith("LC_") and value:
                env[key] = value
        env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        env.setdefault("SHELL", os.environ.get("SHELL", "/bin/zsh"))
        env.update(bootstrap_env_for(worker))
        # Service-only authority must never cross into a mission process even if
        # a deployment accidentally lists it in a worker bootstrap allowlist.
        env.pop("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", None)
        env.pop("VIVENTIUM_GLASSHIVE_ADMISSION_URL", None)
        env.pop("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", None)
        worker_id = str(worker.get("worker_id") or "unknown")
        home = self._home_dir(worker_id)
        isolated_dirs = {
            "HOME": home,
            "CODEX_HOME": home / ".codex",
            "CLAUDE_CONFIG_DIR": home / ".claude",
            "TMPDIR": home / ".tmp",
            "XDG_CACHE_HOME": home / ".cache",
            "XDG_CONFIG_HOME": home / ".config",
            "XDG_STATE_HOME": home / ".local" / "state",
            "GLASSHIVE_LOG_DIR": self._state_dir(worker_id) / "logs",
        }
        for key, path in isolated_dirs.items():
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass
            env[key] = str(path)
        workspace = self._host_workspace_dir(worker)
        env["GLASSHIVE_WORKER_ID"] = str(worker.get("worker_id") or "")
        env["GLASSHIVE_WORKER_RUNTIME"] = self.runtime_name
        env["GLASSHIVE_EXECUTION_MODE"] = "host"
        env["GLASSHIVE_WORKSPACE_DIR"] = str(workspace)
        _project_workspace_dependency_env(env)
        if run_id:
            env["GLASSHIVE_RUN_ID"] = run_id
        return env

    def _host_runtime_info(self, worker: dict, *, pid: int | None = None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        session_key = None
        if not self._provider_session_starts_fresh(worker):
            session_key = (
                self._read_session_key(worker_id)
                or worker.get("session_key")
                or self._default_session_key(worker)
            )
            self._remember_native_session_key(worker, session_key)
        workspace = self._host_workspace_dir(worker)
        return RuntimeInfo(
            runtime=self.runtime_name,
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=session_key,
            state_dir=str(self._state_dir(worker_id)),
            workspace_dir=str(workspace),
            pid=pid,
        )

    def preflight_worker_profile(self, profile: str, execution_mode: str = "host") -> None:
        if shutil.which(self.binary) is None:
            raise RuntimeDependencyMissingError(
                f"{self.binary} CLI is not installed or not on PATH for host-native {self.runtime_name}",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=profile,
                execution_mode=execution_mode,
            )
        issue = host_runtime_requirement_issue(profile, self.runtime_name, configured_binary=self.binary)
        if issue is not None:
            raise RuntimeDependencyMissingError(
                issue.user_message,
                binary=issue.binary,
                runtime_name=self.runtime_name,
                profile=profile,
                execution_mode=execution_mode,
                required_version=issue.required_version,
                actual_version=issue.actual_version,
                dependency_label=issue.label,
                recovery_hint=issue.recommended_recovery,
            )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        # Service admission owns the durable preflight reservation. Repeating
        # version/help/auth subprocesses here would run outside that fence.
        worker_id = worker["worker_id"]
        self._state_dir(worker_id).mkdir(parents=True, exist_ok=True)
        self._home_dir(worker_id).mkdir(parents=True, exist_ok=True)
        workspace = self._host_workspace_dir(worker)
        worker = {**worker, "workspace_dir": str(workspace)}
        info = self._host_runtime_info(worker, pid=self._active_pid(worker_id))
        # Keep the selected path durable if materialization is interrupted.
        with runtime_start_boundary(worker):
            notify_runtime_info(worker, info)
        self._materialize_workspace(worker, workspace)
        self._write_action_audit(
            worker,
            {
                "kind": "worker.ready",
                "cwd": str(workspace),
                "env_keys": [],
                "message": f"Host-native {self.runtime_name} workspace ready.",
            },
        )
        return info

    def prepare_worker_workspace(self, worker: dict) -> RuntimeInfo:
        """Materialize a host workspace without launching an agent process."""
        worker_id = str(worker["worker_id"])
        self._state_dir(worker_id).mkdir(parents=True, exist_ok=True)
        self._home_dir(worker_id).mkdir(parents=True, exist_ok=True)
        workspace = self._host_workspace_dir(worker)
        worker = {**worker, "workspace_dir": str(workspace)}
        self._materialize_workspace(worker, workspace)
        return self._host_runtime_info(worker, pid=None)

    def _active_session_argv_for_evidence(self, active_session: dict[str, object] | None) -> list[str]:
        raw = str((active_session or {}).get("argv_for_evidence_json") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(item or "") for item in parsed]
            except json.JSONDecodeError:
                pass
        return [self.runtime_name]

    def _active_session_constraint_ledger(
        self,
        *,
        workspace: Path,
        active_session: dict[str, object] | None,
    ) -> dict[str, object] | None:
        raw = str((active_session or {}).get("constraint_ledger_path") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = workspace / raw
        try:
            payload = json.loads(path.read_text())
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _write_stopped_active_run_evidence(
        self,
        worker: dict,
        *,
        active_session: dict[str, object] | None,
        stop_reason: str,
        error_text: str,
    ) -> None:
        if self._conversation_mode_from_worker(worker) or not active_session:
            return
        run_id = str(active_session.get("run_id") or "").strip()
        if not run_id:
            return
        workspace = self._host_workspace_dir(worker)
        raw_stdout_path = str(active_session.get("stdout_path") or "").strip()
        raw_stderr_path = str(active_session.get("stderr_path") or "").strip()
        raw_exit_path = str(active_session.get("exit_path") or "").strip()
        stdout_path = Path(raw_stdout_path) if raw_stdout_path else None
        stderr_path = Path(raw_stderr_path) if raw_stderr_path else None
        exit_path = Path(raw_exit_path) if raw_exit_path else None
        stdout = stdout_path.read_text() if stdout_path and stdout_path.is_file() else ""
        stderr = stderr_path.read_text() if stderr_path and stderr_path.is_file() else ""
        self._record_run_metrics(worker["worker_id"], run_id, stdout)
        exit_code: int | None = None
        if exit_path and exit_path.is_file():
            try:
                exit_code = int(exit_path.read_text().strip())
            except ValueError:
                exit_code = None
        transcript_paths = {
            "stdout": raw_stdout_path,
            "stderr": raw_stderr_path,
            "exit_code": raw_exit_path,
            "constraint_ledger": str(active_session.get("constraint_ledger_path") or ""),
        }
        try:
            timeout_seconds = (
                float(active_session["timeout_seconds"])
                if active_session.get("timeout_seconds") is not None
                else None
            )
        except (TypeError, ValueError):
            timeout_seconds = None
        evidence_path = _write_evidence_for_run(
            worker=worker,
            run_id=run_id,
            runtime_name=self.runtime_name,
            model=str(active_session.get("model") or worker.get("model") or self.resolve_model(str(worker.get("profile") or ""))),
            command=self._active_session_argv_for_evidence(active_session),
            env={},
            workspace=workspace,
            stdout_text=stdout,
            stderr_text=stderr,
            output_text="",
            error_text=error_text,
            exit_code=exit_code,
            timeout_seconds=timeout_seconds,
            stop_reason=stop_reason,
            constraint_ledger=self._active_session_constraint_ledger(workspace=workspace, active_session=active_session),
            transcript_paths=transcript_paths,
        )
        raw_heartbeat_path = str(active_session.get("heartbeat_path") or "").strip()
        heartbeat_path = Path(raw_heartbeat_path) if raw_heartbeat_path else _active_run_status_path(workspace, run_id)
        started_at = str(active_session.get("started_at") or "").strip() or _utc_iso()
        _write_active_run_status(
            path=heartbeat_path,
            worker=worker,
            run_id=run_id,
            runtime_name=self.runtime_name,
            model=str(active_session.get("model") or worker.get("model") or self.resolve_model(str(worker.get("profile") or ""))),
            state=stop_reason,
            transcript_paths=transcript_paths,
            started_at=started_at,
            process_pid=None,
            timeout_seconds=timeout_seconds,
            exit_code=exit_code,
            stop_reason=stop_reason,
            evidence_path=evidence_path,
        )

    def _host_control_receipt_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "host_control_receipt.json"

    def _read_host_control_receipt(
        self, worker_id: str
    ) -> dict[str, object] | None:
        path = self._host_control_receipt_path(worker_id)
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_host_control_receipt(
        self,
        worker: dict,
        *,
        active_session: dict[str, object],
        run_id: str,
        operation: str,
        confirmed: bool,
    ) -> dict[str, object]:
        """Persist exact host-control intent before signaling its process.

        The receipt contains only runtime identity/evidence metadata already
        present in the private active-session record.  It lets crash recovery
        distinguish a proven-dead exact generation from an empty identity.
        """

        worker_id = str(worker["worker_id"])
        prior = self._read_host_control_receipt(worker_id) or {}
        same_generation = bool(
            str(prior.get("run_id") or "") == run_id
            and str(prior.get("operation") or "") == operation
            and str(prior.get("process_start_identity") or "")
            == str(active_session.get("process_start_identity") or "")
        )
        status = (
            "confirmed"
            if confirmed or (same_generation and prior.get("status") == "confirmed")
            else "requested"
        )
        payload: dict[str, object] = {
            "version": 1,
            "worker_id": worker_id,
            "run_id": run_id,
            "operation": operation,
            "status": status,
            "session_name": str(active_session.get("session_name") or ""),
            "process_pid": active_session.get("process_pid"),
            "process_group": active_session.get("process_group"),
            "process_start_identity": str(
                active_session.get("process_start_identity") or ""
            ),
            "requested_at": str(prior.get("requested_at") or "") or _utc_iso(),
            "confirmed_at": _utc_iso() if status == "confirmed" else "",
            "session": dict(active_session),
        }
        _atomic_write_private_text(
            self._host_control_receipt_path(worker_id),
            json.dumps(payload, indent=2, sort_keys=True),
        )
        return payload

    @staticmethod
    def _host_control_generation_matches(
        *,
        worker: dict,
        lease: dict,
        session: dict[str, object],
        run_id: str,
    ) -> bool:
        try:
            lease_pid = int(lease.get("pid") or 0)
            lease_group = int(lease.get("process_group") or 0)
            session_pid = int(session.get("process_pid") or 0)
            session_group = int(session.get("process_group") or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            str(lease.get("worker_id") or "") == str(worker["worker_id"])
            and str(lease.get("run_id") or "") == run_id
            and str(lease.get("status") or "") == "active"
            and str(lease.get("startup_state") or "") == "confirmed"
            and str(lease.get("startup_identity_kind") or "") == "host_process"
            and str(session.get("run_id") or "") == run_id
            and str(session.get("session_name") or "")
            == str(lease.get("startup_session_id") or "")
            and lease_pid > 0
            and lease_pid == session_pid
            and lease_group > 0
            and lease_group == session_group
            and str(lease.get("process_start_identity") or "").strip()
            and str(lease.get("process_start_identity") or "").strip()
            == str(session.get("process_start_identity") or "").strip()
        )


    def _confirmed_host_control_session(
        self,
        worker: dict,
        *,
        run_id: str,
        operation: str,
    ) -> dict[str, object]:
        """Compatibility wrapper for callers that need only the control identity."""

        session, _expected_session = self._confirmed_host_control_generation(
            worker,
            run_id=run_id,
            operation=operation,
        )
        return session

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        run_id = str(worker.get("_active_run_id") or "").strip()
        active_session, expected_session = self._confirmed_host_control_generation(
            worker, run_id=run_id, operation="pause_run"
        )
        attempt_id = str(active_session.get("attempt_id") or "").strip()
        self._write_host_control_receipt(
            worker,
            active_session=active_session,
            run_id=run_id,
            operation="pause_run",
            confirmed=False,
        )
        self._note_stop_reason(
            worker["worker_id"],
            "paused",
            run_id=run_id,
            attempt_id=attempt_id,
        )
        try:
            confirmed = self._stop_active_process(
                worker["worker_id"],
                worker=worker,
                run_id=run_id,
                expected_session=expected_session,
                control_session=active_session,
            )
        except Exception:
            self._pop_stop_reason(
                worker["worker_id"], run_id=run_id, attempt_id=attempt_id
            )
            raise
        if not confirmed:
            self._pop_stop_reason(
                worker["worker_id"], run_id=run_id, attempt_id=attempt_id
            )
            raise RuntimeErrorBase("Host run termination could not be confirmed")
        self._write_host_control_receipt(
            worker,
            active_session=active_session,
            run_id=run_id,
            operation="pause_run",
            confirmed=True,
        )
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="paused",
            error_text="Worker was paused by the operator",
        )
        self._append_work_log(worker, "Worker paused by operator.")
        return self._host_runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        exact_run_id = str(run_id or worker.get("_active_run_id") or "").strip()
        operation = (
            "steer_run"
            if str(worker.get("compute_release_kind") or "") == "steer_run"
            else "interrupt_run"
        )
        active_session, expected_session = self._confirmed_host_control_generation(
            worker, run_id=exact_run_id, operation=operation
        )
        attempt_id = str(active_session.get("attempt_id") or "").strip()
        self._write_host_control_receipt(
            worker,
            active_session=active_session,
            run_id=exact_run_id,
            operation=operation,
            confirmed=False,
        )
        self._note_stop_reason(
            worker["worker_id"],
            "interrupted",
            run_id=exact_run_id,
            attempt_id=attempt_id,
        )
        try:
            confirmed = self._stop_active_process(
                worker["worker_id"],
                worker=worker,
                run_id=exact_run_id,
                expected_session=expected_session,
                control_session=active_session,
            )
        except Exception:
            self._pop_stop_reason(
                worker["worker_id"],
                run_id=exact_run_id,
                attempt_id=attempt_id,
            )
            raise
        if not confirmed:
            self._pop_stop_reason(
                worker["worker_id"],
                run_id=exact_run_id,
                attempt_id=attempt_id,
            )
            raise RuntimeErrorBase("Host run termination could not be confirmed")
        self._write_host_control_receipt(
            worker,
            active_session=active_session,
            run_id=exact_run_id,
            operation=operation,
            confirmed=True,
        )
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="interrupted",
            error_text="Worker run was interrupted by the operator",
        )
        self._append_work_log(worker, "Active run interrupted by operator.")
        pending_pid = None
        return self._host_runtime_info(worker, pid=pending_pid)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        run_id = str(worker.get("_active_run_id") or "").strip()
        operation = str(worker.get("compute_release_kind") or "").strip() or "terminate_worker"
        active_session = self._read_active_session(worker["worker_id"])
        expected_session: dict[str, object] | None = (
            dict(active_session) if isinstance(active_session, dict) else None
        )
        control_session: dict[str, object] | None = active_session
        if run_id and isinstance(worker.get("_host_run_lease"), dict):
            active_session, expected_session = self._confirmed_host_control_generation(
                worker, run_id=run_id, operation=operation
            )
            control_session = active_session
        elif run_id:
            active_status = self.host_active_process_status(worker)
            if not active_session:
                if active_status.get("state") != "absent":
                    raise RuntimeErrorBase(
                        "The exact host process identity is not confirmed"
                    )
            elif (
                str(active_session.get("run_id") or "").strip() != run_id
                or str(active_status.get("run_id") or "").strip() != run_id
                or active_status.get("state") != "absent"
            ):
                raise RuntimeErrorBase(
                    "The exact host process identity is not confirmed"
                )
            else:
                with self._process_lock:
                    inactive_process = self._active_processes.get(
                        worker["worker_id"]
                    )
                if inactive_process is not None and inactive_process.poll() is None:
                    raise RuntimeErrorBase(
                        "The exact host process identity is not confirmed"
                    )
                if not self._finalize_owned_host_generation(
                    worker["worker_id"],
                    expected_process=inactive_process,
                    expected_session=active_session,
                    expected_slot_token=(
                        str(active_session.get("host_slot_token") or "").strip()
                        or None
                    ),
                    clear_session=True,
                ):
                    raise RuntimeErrorBase(
                        "Host run ownership changed during exact-run cleanup"
                    )
                active_session = None
                expected_session = None
                control_session = None
        if not run_id and active_session:
            raise RuntimeErrorBase(
                "The exact host process identity is not confirmed"
            )
        if run_id and active_session:
            self._write_host_control_receipt(
                worker,
                active_session=active_session,
                run_id=run_id,
                operation=operation,
                confirmed=False,
            )
        stop_attempt_id = str((active_session or {}).get("attempt_id") or "").strip()
        if run_id and active_session:
            self._note_stop_reason(
                worker["worker_id"],
                "terminated",
                run_id=run_id,
                attempt_id=stop_attempt_id,
            )
        try:
            confirmed = self._stop_active_process(
                worker["worker_id"],
                worker=worker,
                run_id=run_id or None,
                expected_session=expected_session,
                control_session=control_session,
            )
        except Exception:
            if run_id and active_session:
                self._pop_stop_reason(
                    worker["worker_id"],
                    run_id=run_id,
                    attempt_id=stop_attempt_id,
                )
            raise
        if not confirmed:
            if run_id and active_session:
                self._pop_stop_reason(
                    worker["worker_id"],
                    run_id=run_id,
                    attempt_id=stop_attempt_id,
                )
            raise RuntimeErrorBase("Host run termination could not be confirmed")
        if run_id and active_session:
            self._write_host_control_receipt(
                worker,
                active_session=active_session,
                run_id=run_id,
                operation=operation,
                confirmed=True,
            )
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="terminated",
            error_text="Worker was terminated by the operator",
        )
        self._append_work_log(worker, "Worker terminated by operator.")
        return self._host_runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        worker_id = str(worker["worker_id"])
        pid = self._active_pid(worker_id)
        if pid:
            active_session = self._read_active_session(worker_id)
            if active_session:
                try:
                    self._release_durable_host_process(active_session)
                except (OSError, RuntimeErrorBase) as exc:
                    logger.warning(
                        "Failed to release a recovered host-native process handshake",
                        extra={"worker_id": worker_id, "error": str(exc)},
                    )
        with self._process_lock:
            if pid:
                lane = self._host_capacity_lane(worker)
                active = self._host_active_slots().get(lane)
                if not active or active == worker_id:
                    self._host_active_slots()[lane] = worker_id
                    self._host_worker_lanes()[worker_id] = lane
            else:
                lane = self._host_worker_lanes().pop(worker_id, None)
                if lane and self._host_active_slots().get(lane) == worker_id:
                    self._host_active_slots().pop(lane, None)
        return self._host_runtime_info(worker, pid=pid)

    def worker_compute_present(self, worker: dict) -> bool:
        return self._active_pid(worker["worker_id"]) is not None



    def _host_process_group_members(self, process_group_id: int) -> dict[int, str] | None:
        """Read live group members; None means ownership cannot be established."""
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid=,pgid=,stat="],
                capture_output=True, text=True, check=False, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        members: dict[int, str] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 3 or fields[1] != str(process_group_id):
                continue
            if fields[2].upper().startswith("Z"):
                continue
            try:
                pid = int(fields[0])
            except ValueError:
                return None
            identity = self._process_start_identity(pid)
            if not identity:
                # A racing exit is safe, but inaccessible identity is not.
                if self._recorded_pid_is_proven_gone(pid):
                    continue
                return None
            members[pid] = identity
        return members

    def _stop_descendant_ledger_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "active_terminal_session.descendants.json"

    def _stop_descendant_binding(self, session: dict[str, object] | None) -> str:
        return hashlib.sha256(
            repr(self._active_session_fingerprint(session)).encode("utf-8")
        ).hexdigest()

    def _read_stop_descendant_ledger(
        self, worker_id: str, session: dict[str, object] | None
    ) -> dict[int, str] | None:
        """Escaped descendants an earlier Stop of this exact session identified.

        ``{}`` means no ledger, or one bound to a different session. ``None``
        means ownership evidence exists but cannot be read or validated; every
        caller must then keep the session and its fence.
        """

        try:
            payload = json.loads(self._stop_descendant_ledger_path(worker_id).read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("descendants"), dict):
            return None
        if payload.get("session") != self._stop_descendant_binding(session):
            return {}
        known: dict[int, str] = {}
        for pid, identity in payload["descendants"].items():
            try:
                clean_pid = int(pid)
            except (TypeError, ValueError):
                return None
            if clean_pid <= 0 or not isinstance(identity, str) or not identity.startswith("ps-lstart:"):
                return None
            known[clean_pid] = identity
        return known

    def _end_remembered_descendants(self, descendants: dict[int, str]) -> bool:
        """End remembered escaped descendants by exact identity; True only with proof."""

        def proven_gone(timeout: float) -> bool:
            deadline = time.monotonic() + max(0.0, timeout)
            while not all(
                self._recorded_pid_is_proven_gone(pid, identity)
                for pid, identity in descendants.items()
            ):
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            return True

        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            if proven_gone(0):
                return True
            for pid, identity in descendants.items():
                if self._recorded_pid_is_proven_gone(pid, identity):
                    continue
                if self._process_start_identity(pid) != identity:
                    continue  # never signal an unverified incarnation
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    continue
                except OSError:
                    return False
            if proven_gone(wait):
                return True
        return False

    def _clear_active_session(
        self,
        worker_id: str,
        *,
        expected_session: dict[str, object] | None = None,
    ) -> bool:
        """Every host release path: remembered escaped descendants of the session
        being released must be ended and proven gone first."""

        bound = expected_session if expected_session is not None else self._read_active_session(worker_id)
        if bound is not None:
            remembered = self._read_stop_descendant_ledger(worker_id, bound)
            if remembered is None or (remembered and not self._end_remembered_descendants(remembered)):
                return False
        released = super()._clear_active_session(worker_id, expected_session=expected_session)
        if released and bound is not None:
            self._clear_stop_descendant_ledger(worker_id)
        return released

    def _write_stop_descendant_ledger(
        self, worker_id: str, session: dict[str, object] | None, descendants: dict[int, str]
    ) -> None:
        path = self._stop_descendant_ledger_path(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        temp.write_text(json.dumps({
            "session": self._stop_descendant_binding(session),
            "descendants": {str(pid): identity for pid, identity in descendants.items()},
        }))
        temp.chmod(0o600)
        os.replace(temp, path)

    def _clear_stop_descendant_ledger(self, worker_id: str) -> None:
        try:
            self._stop_descendant_ledger_path(worker_id).unlink()
        except FileNotFoundError:
            pass

    def _host_escaped_descendants(
        self, members: dict[int, str], process_group_id: int
    ) -> dict[int, str] | None:
        """Exact identities of live descendants that left this process group.

        None means the tree or an identity could not be read, so ownership of
        the whole generation cannot be established.
        """

        try:
            result = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,pgid=,stat="],
                capture_output=True, text=True, check=False, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        children: dict[int, list[tuple[int, int, str]]] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            try:
                pid, ppid, pgid = int(fields[0]), int(fields[1]), int(fields[2])
            except ValueError:
                return None
            children.setdefault(ppid, []).append((pid, pgid, fields[3]))
        escaped: dict[int, str] = {}
        seen = set(members)
        frontier = list(members)
        while frontier:
            for pid, pgid, state in children.get(frontier.pop(), ()):
                if pid in seen:
                    continue
                seen.add(pid)
                frontier.append(pid)
                if pgid == process_group_id or state.upper().startswith("Z"):
                    continue
                identity = self._process_start_identity(pid)
                if not identity:
                    if self._recorded_pid_is_proven_gone(pid):
                        continue
                    return None
                escaped[pid] = identity
        return escaped

    def _host_process_group_alive(self, process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass
        members = self._host_process_group_members(process_group_id)
        return members is None or bool(members)

    def _wait_for_host_process_group_exit(self, process_group_id: int, timeout: float) -> bool:
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            if not self._host_process_group_alive(process_group_id):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _stop_active_process(
        self,
        worker_id: str,
        *,
        worker: dict | None = None,
        run_id: str | None = None,
        expected_session: dict[str, object] | None | object = (
            _ACTIVE_SESSION_EXPECTATION_UNSET
        ),
        control_session: dict[str, object] | None = None,
    ) -> bool:
        lease = worker.get("_host_run_lease") if isinstance(worker, dict) else None
        session_path = self._active_session_meta_path(worker_id)

        with self._active_session_file_lock(worker_id):
            active_session = self._read_active_session(worker_id)
            if active_session is None and session_path.exists():
                return False
            if expected_session is _ACTIVE_SESSION_EXPECTATION_UNSET:
                expected_snapshot = (
                    dict(active_session) if isinstance(active_session, dict) else None
                )
            else:
                expected_snapshot = (
                    dict(expected_session)
                    if isinstance(expected_session, dict)
                    else None
                )
            if self._active_session_fingerprint(
                active_session
            ) != self._active_session_fingerprint(expected_snapshot):
                return False

        captured_control_session = (
            dict(control_session)
            if isinstance(control_session, dict)
            else (dict(active_session) if isinstance(active_session, dict) else None)
        )
        if (
            active_session
            and captured_control_session
            and self._active_session_fingerprint(active_session)
            != self._active_session_fingerprint(captured_control_session)
        ):
            return False
        for candidate in (active_session, captured_control_session):
            if (
                candidate
                and run_id
                and str(candidate.get("run_id") or "") != run_id
            ):
                return False
        if isinstance(lease, dict):
            if not run_id or str(lease.get("run_id") or "") != run_id:
                return False
            lease_session = active_session or captured_control_session
            if not lease_session or not self._host_control_generation_matches(
                worker=worker or {},
                lease=lease,
                session=lease_session,
                run_id=run_id,
            ):
                return False

        with self._process_lock:
            process = self._active_processes.get(worker_id)
        local_process = process if process and process.poll() is None else None

        identity_session = active_session or captured_control_session
        try:
            recorded_pid = int(
                (identity_session or {}).get("process_pid")
                or (lease or {}).get("pid")
                or 0
            )
            recorded_group = int(
                (identity_session or {}).get("process_group")
                or (lease or {}).get("process_group")
                or 0
            )
        except (TypeError, ValueError):
            recorded_pid = 0
            recorded_group = 0
        recorded_identity = str(
            (identity_session or {}).get("process_start_identity")
            or (lease or {}).get("process_start_identity")
            or ""
        ).strip()
        expected_slot_token = (
            str((identity_session or {}).get("host_slot_token") or "").strip()
            or None
        )

        # Providers start user commands in their own sessions, outside the run's
        # process group. Each such descendant is tracked by exact start identity
        # while the tree still links it here, remembered across Stop attempts,
        # signalled only as that incarnation, and must be proven gone (affirmative
        # death or a readable, different incarnation) before ownership is released.
        ledger_session = expected_snapshot or captured_control_session
        remembered = self._read_stop_descendant_ledger(worker_id, ledger_session)
        if remembered is None:
            return False
        escaped_descendants: dict[int, str] = remembered

        def remember_escaped_descendants(found: dict[int, str]) -> None:
            if found and any(escaped_descendants.get(pid) != ident for pid, ident in found.items()):
                escaped_descendants.update(found)
                self._write_stop_descendant_ledger(worker_id, ledger_session, escaped_descendants)

        def signal_escaped_descendants(sig: signal.Signals) -> bool:
            """False when a signal to a verified known descendant failed.

            An unreadable identity is never signalled; whether that process is
            gone is decided only by affirmative proof while waiting.
            """

            delivered = True
            for pid, identity in list(escaped_descendants.items()):
                if self._recorded_pid_is_proven_gone(pid, identity):
                    continue
                if self._process_start_identity(pid) != identity:
                    continue
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    continue
                except OSError:
                    delivered = False
            return delivered

        def escaped_descendants_gone(timeout: float) -> bool:
            deadline = time.monotonic() + max(0.0, timeout)
            while not all(
                self._recorded_pid_is_proven_gone(pid, identity)
                for pid, identity in escaped_descendants.items()
            ):
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            return True

        def stop_remembered_descendants() -> bool:
            return self._end_remembered_descendants(escaped_descendants)

        if local_process is not None:
            try:
                local_pid = int(local_process.pid)
            except (TypeError, ValueError):
                return False
            if (
                not (active_session or lease)
                or recorded_pid <= 0
                or recorded_pid != local_pid
                or not recorded_identity
                or recorded_group <= 0
            ):
                return False
            current_identity = self._process_start_identity(local_pid)
            if not current_identity or current_identity != recorded_identity:
                return False
            target_pid = local_pid
            target_identity = recorded_identity
        elif recorded_pid > 0 and recorded_identity:
            if not self._recorded_process_is_running(recorded_pid, recorded_identity):
                # Without a live identity anchor a remaining group might be reused.
                # Keep ownership evidence rather than claim a successful stop.
                if recorded_group <= 0 or self._host_process_group_alive(recorded_group):
                    return False
                if not stop_remembered_descendants():
                    return False
                released = self._finalize_owned_host_generation(
                    worker_id,
                    expected_process=process,
                    expected_session=expected_snapshot,
                    expected_slot_token=expected_slot_token,
                    clear_session=True,
                )
                if released:
                    self._clear_stop_descendant_ledger(worker_id)
                return released
            if recorded_group <= 0:
                return False
            target_pid = recorded_pid
            target_identity = recorded_identity
        elif active_session or captured_control_session or lease:
            # A foreign process may only act on a persisted PID when the PID's
            # start identity is present. Legacy PID-only records fail closed.
            return False
        else:
            with self._process_lock:
                current_process = self._active_processes.get(worker_id)
                if current_process and current_process.poll() is None:
                    return False
            return True

        group_members = self._host_process_group_members(recorded_group)
        if (
            recorded_group == os.getpgrp()
            or group_members is None
            or group_members.get(target_pid) != target_identity
            or self._process_start_identity(target_pid) != target_identity
        ):
            return False

        def signal_exact_generation(sig: signal.Signals) -> str:
            """Signal only while durable and in-memory ownership is still exact."""

            nonlocal group_members
            with self._active_session_file_lock(worker_id):
                current_session = self._read_active_session(worker_id)
                if current_session is None and session_path.exists():
                    return "changed"
                if self._active_session_fingerprint(
                    current_session
                ) != self._active_session_fingerprint(expected_snapshot):
                    return "changed"
                signal_session = current_session or captured_control_session
                if isinstance(lease, dict):
                    if (
                        not signal_session
                        or not run_id
                        or not self._host_control_generation_matches(
                            worker=worker or {},
                            lease=lease,
                            session=signal_session,
                            run_id=run_id,
                        )
                    ):
                        return "changed"
                with self._process_lock:
                    current_process = self._active_processes.get(worker_id)
                    if local_process is not None:
                        if current_process is not local_process:
                            return "changed"
                    elif current_process and current_process.poll() is None:
                        return "changed"

                    current_members = self._host_process_group_members(recorded_group)
                    if current_members is None:
                        return "uncertain"
                    if not current_members:
                        return "gone" if signal_escaped_descendants(sig) else "uncertain"
                    # A surviving exact member anchors the group after its leader exits.
                    if not any(
                        current_members.get(pid) == identity
                        for pid, identity in group_members.items()
                    ):
                        return "uncertain"
                    group_members = current_members
                    escaped = self._host_escaped_descendants(
                        current_members, recorded_group
                    )
                    if escaped is None:
                        return "uncertain"
                    remember_escaped_descendants(escaped)
                    try:
                        os.killpg(recorded_group, sig)
                    except ProcessLookupError:
                        return "gone" if signal_escaped_descendants(sig) else "uncertain"
                    except OSError:
                        return "uncertain"
                    if not signal_escaped_descendants(sig):
                        return "uncertain"
                    return "sent"

        signal_state = signal_exact_generation(signal.SIGTERM)
        if signal_state in {"changed", "uncertain"}:
            return False
        group_gone = signal_state == "gone" or self._wait_for_host_process_group_exit(
            recorded_group, 5
        )
        descendants_gone = escaped_descendants_gone(5)
        if not (group_gone and descendants_gone):
            if not group_gone:
                kill_state = signal_exact_generation(signal.SIGKILL)
                if kill_state in {"changed", "uncertain"}:
                    return False
                group_gone = kill_state == "gone" or self._wait_for_host_process_group_exit(
                    recorded_group, 2
                )
            elif not signal_escaped_descendants(signal.SIGKILL):
                # The group is gone, so its own process bookkeeping may already
                # be reaped; remembered descendants escalate by exact identity.
                return False
            descendants_gone = escaped_descendants_gone(2)
        confirmed = group_gone and descendants_gone
        if confirmed and local_process is not None:
            try:
                local_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                return False
        if confirmed:
            if not self._finalize_owned_host_generation(
                worker_id,
                expected_process=process,
                expected_session=expected_snapshot,
                expected_slot_token=expected_slot_token,
                clear_session=True,
            ):
                raise RuntimeErrorBase(
                    "Host run ownership changed during exact-run stop"
                )
            self._clear_stop_descendant_ledger(worker_id)
        return confirmed

    def _acquire_host_slot(self, worker: dict) -> str:
        worker_id = worker["worker_id"]
        lane = self._host_capacity_lane(worker)
        slot_token = secrets.token_hex(16)
        with self._process_lock:
            error = self._host_capacity_error_locked(worker_id, lane)
            if error is not None:
                raise error
            active = self._host_active_slots().get(lane)
            if isinstance(active, set):
                workers = active
            elif isinstance(active, (list, tuple)):
                workers = {str(item) for item in active if str(item)}
            elif active:
                workers = {str(active)}
            else:
                workers = set()
            workers.add(worker_id)
            self._host_active_slots()[lane] = workers
            self._host_worker_lanes()[worker_id] = lane
            self._host_slot_tokens()[worker_id] = slot_token
        return slot_token

    def _host_capacity_error_locked(
        self,
        worker_id: str,
        lane: str = "mission",
    ) -> RuntimeErrorBase | None:
        raw_active = self._host_active_slots().get(lane)
        if isinstance(raw_active, set):
            candidates = set(raw_active)
        elif isinstance(raw_active, (list, tuple)):
            candidates = {str(item) for item in raw_active if str(item)}
        elif raw_active:
            candidates = {str(raw_active)}
        else:
            candidates = set()
        live: set[str] = set()
        for candidate in candidates:
            process = self._active_processes.get(candidate)
            if process is None or process.poll() is None:
                live.add(candidate)
        live.discard(worker_id)
        limit_name = (
            "WPR_HOST_CONVERSATION_SLOTS_PER_CLI"
            if lane == "conversation"
            else "WPR_HOST_MISSION_SLOTS_PER_CLI"
        )
        default = 2 if lane == "conversation" else 3
        try:
            limit = max(1, int(str(os.environ.get(limit_name, default))))
        except ValueError:
            limit = default
        if len(live) >= limit:
            return HostCapacityError(
                f"Host-native {self.runtime_name} {lane} lane is at capacity.",
                capacity_class="family_lane",
            )
        return None

    def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        with self._process_lock:
            return self._host_capacity_error_locked(
                str(worker["worker_id"]),
                self._host_capacity_lane(worker),
            )

    def _release_host_slot(
        self,
        worker_id: str,
        *,
        expected_slot_token: str | None | object = _HOST_SLOT_EXPECTATION_UNSET,
    ) -> bool:
        with self._process_lock:
            return self._release_host_slot_locked(
                worker_id, expected_slot_token=expected_slot_token
            )

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError

    def _native_provider_authority_receipt(
        self,
        worker: dict,
        *,
        command: list[str],
        run_id: str,
        model: str,
    ) -> dict[str, object] | None:
        """Prove the current server-owned authority reached the native CLI invocation."""

        if not self._conversation_mode_from_worker(worker):
            return None
        bundle = self._bootstrap_bundle_for_worker(worker)
        if "developer_instructions" not in bundle:
            return None
        expected = str(bundle.get("developer_instructions") or "")
        if not expected:
            return None

        placement = ""
        materialized = ""
        if self.runtime_name == "claude-code":
            flag = "--append-system-prompt-file"
            if flag not in command:
                raise RuntimeErrorBase(
                    "Native Claude developer instruction authority was not attached; refusing to launch"
                )
            try:
                prompt_path = Path(command[command.index(flag) + 1])
                materialized = prompt_path.read_text()
            except (IndexError, OSError) as exc:
                raise RuntimeErrorBase(
                    "Native Claude developer instruction authority is unreadable; refusing to launch"
                ) from exc
            placement = "append_system_prompt_file"
        elif self.runtime_name == "codex-cli":
            config_path = self._host_codex_home(worker) / "config.toml"
            try:
                parsed = tomllib.loads(config_path.read_text())
            except Exception as exc:
                raise RuntimeErrorBase(
                    "Native Codex developer instruction authority is unreadable; refusing to launch"
                ) from exc
            materialized = str(parsed.get("developer_instructions") or "")
            placement = "codex_developer_instructions"
        else:
            raise RuntimeErrorBase(
                "Native conversation runtime cannot receive developer instruction authority"
            )

        if materialized != expected:
            raise RuntimeErrorBase(
                "Native provider developer instruction authority differs from the request-pinned snapshot; refusing to launch"
            )
        feeling_capsule_count = materialized.count("<viventium_feeling_state")
        if feeling_capsule_count > 1:
            raise RuntimeErrorBase(
                "Native provider developer instruction authority contains duplicate Feeling capsules; refusing to launch"
            )
        return {
            "protocol": "glasshive.native_provider_authority_receipt.v1",
            "run_id": str(run_id),
            "runtime": self.runtime_name,
            "model": str(model),
            "authority_sha256": hashlib.sha256(materialized.encode("utf-8")).hexdigest(),
            "authority_chars": len(materialized),
            "feeling_capsule_count": feeling_capsule_count,
            "placement": placement,
            "materialized": True,
        }

    def native_provider_authority_receipt(
        self, worker: dict, run_id: str
    ) -> dict[str, object] | None:
        path = (
            self._run_root(str(worker.get("worker_id") or ""), str(run_id))
            / "native-provider-authority-receipt.json"
        )
        try:
            parsed = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if (
            parsed.get("protocol")
            != "glasshive.native_provider_authority_receipt.v1"
            or str(parsed.get("run_id") or "") != str(run_id)
            or parsed.get("materialized") is not True
        ):
            return None
        allowed = {
            "protocol",
            "run_id",
            "runtime",
            "model",
            "authority_sha256",
            "authority_chars",
            "feeling_capsule_count",
            "placement",
            "materialized",
        }
        return {key: parsed[key] for key in allowed if key in parsed}

    def _host_run_timeout_sec(
        self,
        timeout_sec: float | None = None,
        *,
        declared_long: bool = False,
    ) -> float | None:
        raw = (
            os.environ.get("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", "").strip()
            or os.environ.get("WPR_HOST_RUN_TIMEOUT_SEC", "").strip()
            or os.environ.get("GLASSHIVE_RUN_TIMEOUT_SEC", "").strip()
        )
        if not raw and not declared_long:
            raw = os.environ.get("GLASSHIVE_MAX_RUN_DURATION_S", "").strip()
        if not raw:
            raw = os.environ.get("WPR_RUN_TIMEOUT_SEC", "").strip()
        if not raw:
            return timeout_sec if timeout_sec and timeout_sec > 0 else None
        if raw.lower() in {"0", "none", "off", "false", "disabled"}:
            return None
        try:
            parsed = float(raw)
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    def _finish_host_run_generation(
        self, worker: dict, run_id: str, process, session: dict | None,
        slot_token: str, *, clear_session: bool,
    ) -> bool:
        """Clean up only the invocation's captured process, session, and capacity slot."""
        if process is not None and process.poll() is None:
            return self._stop_active_process(
                str(worker["worker_id"]), worker=worker, run_id=run_id,
                expected_session=session, control_session=session,
            )
        return self._finalize_owned_host_generation(
            str(worker["worker_id"]), expected_process=process,
            expected_session=session, expected_slot_token=slot_token,
            clear_session=clear_session,
        )

    def _run_conversation_task(
        self,
        worker: dict,
        instruction: str,
        *,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        """Run a natural harness turn without mission scaffolding or LIFE-side runtime files."""
        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        worker = {**worker, "_active_run_id": effective_run_id, "_glasshive_conversation_run": True}
        info = self.ensure_worker_ready(worker)
        worker = {**worker, "workspace_dir": info.workspace_dir}
        command, env = self._build_command(worker, instruction, info)
        native_image_bundle = self._bootstrap_bundle_for_worker(worker)
        native_output_capabilities = native_image_bundle.get("provider_capabilities")
        native_image_scope = {
            "run_id": effective_run_id, "worker_id": worker["worker_id"],
            "owner_id": worker.get("owner_id"), "workspace_dir": info.workspace_dir,
            "images": native_image_bundle.get("native_input_images", []),
            "attempt_id": str(worker.get("_run_attempt_id") or ""),
            "output_transport": native_output_capabilities.get("native_image_output") if isinstance(native_output_capabilities, dict) else None,
            "file_output_transport": "artifact_sha256",
        }
        authority_receipt = self._native_provider_authority_receipt(
            worker,
            command=command,
            run_id=effective_run_id,
            model=str(info.model or ""),
        )
        stdin_text = self._command_stdin_text(worker, instruction, info)
        env["GLASSHIVE_RUN_ID"] = effective_run_id
        env["GLASSHIVE_RUN_MODE"] = "conversation"
        workspace = Path(str(info.workspace_dir or self._host_workspace_dir(worker)))
        workspace_side_effect_state = self._conversation_workspace_side_effect_state(workspace)
        host_slot_token = self._acquire_host_slot(worker)

        run_root = self._run_root(str(worker["worker_id"]), effective_run_id)
        run_root.mkdir(parents=True, exist_ok=True)
        run_root.chmod(0o700)
        _atomic_write_private_text(
            run_root / "native-image-scope.json", json.dumps(native_image_scope, sort_keys=True)
        )
        raw_stdout = run_root / "stdout.log"
        raw_stderr = run_root / "stderr.log"
        host_stdin = run_root / "instruction.stdin"
        exit_path = run_root / "exit_code"
        if stdin_text is not None:
            host_stdin.write_text(stdin_text)
            host_stdin.chmod(0o600)

        self._write_action_audit(
            worker,
            {
                "kind": "conversation.started",
                "run_id": effective_run_id,
                "cwd": str(workspace),
                "argv_redacted": [_redact_command_arg(part) for part in command],
                "env_keys": sorted(env.keys()),
                "effort_projection": _effort_projection_for_audit(worker),
            },
        )
        run_timeout_sec = self._host_run_timeout_sec(timeout_sec)
        process: subprocess.Popen[str] | None = None
        owned_session: dict[str, object] | None = None
        startup_cleanup_unconfirmed = False
        native_session_stop = Event()
        native_session_thread: Thread | None = None
        try:
            with raw_stdout.open("w") as stdout_handle, raw_stderr.open("w") as stderr_handle:
                raw_stdout.chmod(0o600)
                raw_stderr.chmod(0o600)
                process_command = self._durable_host_process_command(
                    command,
                    run_root=run_root,
                    exit_path=exit_path,
                    timeout_sec=run_timeout_sec,
                    stdin_path=host_stdin if stdin_text is not None else None,
                    response_deadline_at=str(worker.get("_provider_response_deadline_at") or ""),
                )
                process = subprocess.Popen(
                    process_command,
                    cwd=str(workspace),
                    env=env,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
                self._register_process(str(worker["worker_id"]), process)
                self._wait_for_durable_host_supervisor(process, run_root=run_root)
                self._write_active_session(
                    str(worker["worker_id"]),
                    {
                        "session_name": f"conversation-{effective_run_id[:12]}",
                        "run_id": effective_run_id,
                        "attempt_id": str(worker.get("_run_attempt_id") or ""),
                        "host_slot_token": host_slot_token,
                        "workspace_dir": str(workspace),
                        "stdout_path": str(raw_stdout),
                        "stderr_path": str(raw_stderr),
                        "exit_path": str(exit_path),
                        "model": str(info.model or ""),
                        "argv_for_evidence_json": json.dumps([_redact_command_arg(part) for part in command]),
                        "started_at": _utc_iso(),
                        "process_pid": process.pid,
                        "timeout_seconds": run_timeout_sec,
                        "instruction": instruction,
                        "run_mode": "conversation",
                        "native_image_scope": native_image_scope,
                    },
                    publish_run_start=callable(self._run_start_observer),
                    worker=worker,
                    spawned_process=process,
                )
                active_session = self._read_active_session(str(worker["worker_id"]))
                owned_session = dict(active_session) if active_session else None
                if not active_session:
                    self._stop_active_process(
                        str(worker["worker_id"]),
                        worker=worker,
                        run_id=effective_run_id,
                    )
                    raise RuntimeErrorBase(
                        "Host-native process metadata was not durably recorded before launch"
                    )
                native_session_thread = Thread(
                    target=self._observe_native_session_events,
                    args=(
                        str(worker["worker_id"]), raw_stdout, native_session_stop,
                        effective_run_id, str(worker.get("_run_attempt_id") or ""), 0, worker,
                    ),
                    name=f"glasshive-conversation-session-{effective_run_id[:12]}",
                    daemon=True,
                )
                native_session_thread.start()
                with runtime_start_boundary(worker):
                    self._release_durable_host_process(active_session)
                    notify_runtime_started(worker)
                if authority_receipt is not None:
                    _atomic_write_private_text(
                        run_root / "native-provider-authority-receipt.json",
                        json.dumps(authority_receipt, sort_keys=True),
                    )
                    self._write_action_audit(
                        worker,
                        {
                            "kind": "conversation.provider_invoked",
                            "run_id": effective_run_id,
                            "process_pid": process.pid,
                            "native_provider_authority_receipt": authority_receipt,
                        },
                    )
                try:
                    exit_code = process.wait()
                except subprocess.TimeoutExpired as exc:
                    if not self._stop_active_process(
                        str(worker["worker_id"]), worker=worker, run_id=effective_run_id,
                        expected_session=owned_session, control_session=owned_session,
                    ):
                        raise RuntimeErrorBase("Host conversation ownership changed during timeout cleanup") from exc
                    raise RuntimeErrorBase(f"{self.runtime_name} timed out after {run_timeout_sec:g}s") from exc
        except RunStartupRejectedError as exc:
            startup_cleanup_unconfirmed = not exc.termination_confirmed
            raise
        finally:
            native_session_stop.set()
            if native_session_thread:
                native_session_thread.join(timeout=1)
            if not startup_cleanup_unconfirmed and not self._finish_host_run_generation(
                worker, effective_run_id, process, owned_session, host_slot_token,
                clear_session=True,
            ):
                raise RuntimeErrorBase("Host conversation ownership changed during cleanup")
            try:
                self._cleanup_conversation_workspace_side_effects(
                    workspace,
                    workspace_side_effect_state,
                )
            except OSError as exc:
                logger.warning(
                    "Failed to clean harness conversation workspace side effect",
                    extra={
                        "worker_id": str(worker.get("worker_id") or ""),
                        "workspace": str(workspace),
                        "error": str(exc),
                    },
                )

        exit_code = self._final_host_exit_code(exit_path, exit_code)
        stdout = raw_stdout.read_text() if raw_stdout.exists() else ""
        stderr = raw_stderr.read_text() if raw_stderr.exists() else ""
        self._finalize_stop_reason(str(worker["worker_id"]), run_id=effective_run_id,
                                   attempt_id=str((owned_session or {}).get("attempt_id") or ""))
        if exit_code != 0:
            detail = (_redact_text(stderr, max_chars=2000) or _redact_text(stdout, max_chars=2000)).strip()
            self._write_action_audit(
                worker,
                {
                    "kind": "conversation.failed",
                    "run_id": effective_run_id,
                    "exit_code": exit_code,
                    "detail": detail,
                },
            )
            raise RuntimeErrorBase(f"{self.runtime_name} exited with code {exit_code}: {detail}")

        session_key, output = self._parse_output(worker, stdout, stderr, info)
        self._remember_native_session_key(worker, session_key)
        output = self._project_native_image_output(
            worker, info, str(output or ""), native_image_scope, effective_run_id
        )
        redacted_output = _redact_text(str(output or "").strip())
        if len(redacted_output) > _HOST_RUN_OUTPUT_MAX_CHARS:
            redacted_output = f"{redacted_output[: _HOST_RUN_OUTPUT_MAX_CHARS - 3].rstrip()}..."
        self._write_action_audit(
            worker,
            {
                "kind": "conversation.completed",
                "run_id": effective_run_id,
                "exit_code": exit_code,
                "output_chars": len(redacted_output),
                **(
                    {"native_provider_authority_receipt": authority_receipt}
                    if authority_receipt is not None
                    else {}
                ),
            },
        )
        return redacted_output

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        binding = worker.get("_run_local_capability_binding")
        if binding is not None:
            lease_id = str(worker.get("_run_startup_token_digest") or "")
            if (
                not isinstance(binding, dict)
                or "containerGenerationId" in binding
                or not re.fullmatch(r"[a-f0-9]{64}", lease_id)
                or binding.get("hostStartupLeaseId") != lease_id
                or binding.get("runId") != run_id
                or binding.get("workerId") != worker.get("worker_id")
            ):
                raise RuntimeErrorBase(
                    "Host run capability authority does not match the exact startup lease"
                )
        if self._conversation_mode_from_worker(worker):
            return self._run_conversation_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)
        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        worker = {
            **worker,
            "_active_run_id": effective_run_id,
            "_glasshive_task_run": True,
        }
        info = self.ensure_worker_ready(worker)
        worker = {**worker, "workspace_dir": info.workspace_dir}
        command, env = self._build_command(worker, instruction, info)
        stdin_text = self._command_stdin_text(worker, instruction, info)
        env["GLASSHIVE_RUN_ID"] = effective_run_id
        workspace = Path(str(info.workspace_dir or self._host_workspace_dir(worker)))
        constraint_ledger, constraint_ledger_path = _write_constraint_ledger_for_run(
            worker=worker,
            instruction=instruction,
            workspace=workspace,
            run_id=effective_run_id,
        )
        host_slot_token = self._acquire_host_slot(worker)

        run_root = self._run_root(worker["worker_id"], effective_run_id)
        run_root.mkdir(parents=True, exist_ok=True)
        run_root.chmod(0o700)
        raw_stdout = run_root / "stdout.log"
        raw_stderr = run_root / "stderr.log"
        host_stdin = run_root / "instruction.stdin"
        exit_path = run_root / "exit_code"
        stdout_path, stderr_path = self._log_paths(worker["worker_id"])
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        if stdin_text is not None:
            host_stdin.write_text(stdin_text)
            host_stdin.chmod(0o600)

        command_display = _redacted_command_display(command)
        self._append_work_log(worker, f"Run {effective_run_id} started with host-native {self.runtime_name}.")
        self._write_action_audit(
            worker,
            {
                "kind": "run.started",
                "run_id": effective_run_id,
                "cwd": str(workspace),
                "argv_redacted": [_redact_command_arg(part) for part in command],
                "env_keys": sorted(env.keys()),
                "constraint_ledger_path": constraint_ledger_path,
                "effort_projection": _effort_projection_for_audit(worker),
            },
        )

        with stderr_path.open("a") as aggregate:
            aggregate.write(f"$ host {self.runtime_name} {command_display}\n")
            stderr_path.chmod(0o600)

        transcript_paths = {
            "stdout": str(raw_stdout),
            "stderr": str(raw_stderr),
            "exit_code": str(exit_path),
            "constraint_ledger": constraint_ledger_path,
        }
        started_at = time.time()
        started_at_iso = _utc_iso()
        run_timeout_sec = self._host_run_timeout_sec(
            timeout_sec,
            declared_long=_declared_long_mission(worker),
        )
        heartbeat_path = _active_run_status_path(workspace, effective_run_id)
        heartbeat_stop = Event()
        heartbeat_thread: Thread | None = None
        native_session_stop = Event()
        native_session_thread: Thread | None = None
        process: subprocess.Popen[str] | None = None
        owned_session: dict[str, object] | None = None
        startup_cleanup_unconfirmed = False
        try:
            with raw_stdout.open("w") as stdout_handle, raw_stderr.open("w") as stderr_handle:
                raw_stdout.chmod(0o600)
                raw_stderr.chmod(0o600)
                process_command = self._durable_host_process_command(
                    command,
                    run_root=run_root,
                    exit_path=exit_path,
                    timeout_sec=run_timeout_sec,
                    stdin_path=host_stdin if stdin_text is not None else None,
                    native_input_context=self._native_input_context(worker),
                    response_deadline_at=str(worker.get("_provider_response_deadline_at") or ""),
                )
                process = subprocess.Popen(
                    process_command,
                    cwd=str(workspace),
                    env=env,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
                self._register_process(worker["worker_id"], process)
                process_pid = process.pid
                self._wait_for_durable_host_supervisor(process, run_root=run_root)
                self._write_active_session(
                    worker["worker_id"],
                    {
                        "session_name": f"host-{effective_run_id[:12]}",
                        "run_id": effective_run_id,
                        "attempt_id": str(worker.get("_run_attempt_id") or ""),
                        "host_slot_token": host_slot_token,
                        "workspace_dir": str(workspace),
                        "stdout_path": str(raw_stdout),
                        "stderr_path": str(raw_stderr),
                        "exit_path": str(exit_path),
                        "constraint_ledger_path": constraint_ledger_path,
                        "model": str(info.model or ""),
                        "argv_for_evidence_json": json.dumps([_redact_command_arg(part) for part in command]),
                        "started_at": started_at_iso,
                        "process_pid": process.pid,
                        "heartbeat_path": str(heartbeat_path),
                        "timeout_seconds": run_timeout_sec,
                        "instruction": instruction,
                    },
                    publish_run_start=callable(self._run_start_observer),
                    worker=worker,
                    spawned_process=process,
                )
                active_session = self._read_active_session(str(worker["worker_id"]))
                owned_session = dict(active_session) if active_session else None
                if not active_session:
                    self._stop_active_process(
                        str(worker["worker_id"]),
                        worker=worker,
                        run_id=effective_run_id,
                    )
                    raise RuntimeErrorBase(
                        "Host-native process metadata was not durably recorded before launch"
                    )
                with runtime_start_boundary(worker):
                    self._release_durable_host_process(active_session)
                    notify_runtime_started(worker)
                _write_active_run_status(
                    path=heartbeat_path,
                    worker=worker,
                    run_id=effective_run_id,
                    runtime_name=self.runtime_name,
                    model=str(info.model or ""),
                    state="running",
                    transcript_paths=transcript_paths,
                    started_at=started_at_iso,
                    process_pid=process.pid,
                    timeout_seconds=run_timeout_sec,
                )
                heartbeat_thread = _start_active_run_heartbeat(
                    path=heartbeat_path,
                    worker=worker,
                    run_id=effective_run_id,
                    runtime_name=self.runtime_name,
                    model=str(info.model or ""),
                    transcript_paths=transcript_paths,
                    started_at=started_at_iso,
                    process_pid=process.pid,
                    timeout_seconds=run_timeout_sec,
                    stop_event=heartbeat_stop,
                )
                try:
                    native_session_thread = Thread(
                        target=self._observe_native_session_events,
                        args=(
                            worker["worker_id"], raw_stdout, native_session_stop,
                            effective_run_id, str(worker.get("_run_attempt_id") or ""), 0, worker,
                        ),
                        name=f"glasshive-host-native-session-{effective_run_id[:12]}",
                        daemon=True,
                    )
                    native_session_thread.start()
                    exit_code = process.wait()
                except subprocess.TimeoutExpired:
                    if not self._stop_active_process(
                        worker["worker_id"], worker=worker, run_id=effective_run_id,
                        expected_session=owned_session, control_session=owned_session,
                    ):
                        raise RuntimeErrorBase("Host mission ownership changed during timeout cleanup")
                    try:
                        stdout_handle.flush()
                        stderr_handle.flush()
                    except OSError:
                        pass
                    timeout_stdout = raw_stdout.read_text() if raw_stdout.exists() else ""
                    timeout_stderr = raw_stderr.read_text() if raw_stderr.exists() else ""
                    self._record_run_metrics(worker["worker_id"], effective_run_id, timeout_stdout)
                    evidence_path = _write_evidence_for_run(
                        worker=worker,
                        run_id=effective_run_id,
                        runtime_name=self.runtime_name,
                        model=str(info.model or ""),
                        command=command,
                        env=env,
                        workspace=workspace,
                        stdout_text=timeout_stdout,
                        stderr_text=timeout_stderr,
                        output_text="",
                        error_text=f"{self.runtime_name} timed out after {run_timeout_sec:g}s",
                        exit_code=None,
                        timeout_seconds=run_timeout_sec,
                        stop_reason="timeout",
                        constraint_ledger=constraint_ledger,
                        transcript_paths=transcript_paths,
                        started_at=started_at,
                    )
                    _stop_active_run_heartbeat(heartbeat_stop, heartbeat_thread)
                    _write_active_run_status(
                        path=heartbeat_path,
                        worker=worker,
                        run_id=effective_run_id,
                        runtime_name=self.runtime_name,
                        model=str(info.model or ""),
                        state="timeout",
                        transcript_paths=transcript_paths,
                        started_at=started_at_iso,
                        process_pid=process.pid,
                        timeout_seconds=run_timeout_sec,
                        stop_reason="timeout",
                        evidence_path=evidence_path,
                    )
                    self._append_work_log(
                        worker,
                        f"Run {effective_run_id} exceeded configured host timeout after {run_timeout_sec:g}s.",
                    )
                    raise RuntimeErrorBase(f"{self.runtime_name} timed out after {run_timeout_sec:g}s")
                finally:
                    native_session_stop.set()
                    if native_session_thread is not None:
                        native_session_thread.join(timeout=2.0)
                    _stop_active_run_heartbeat(heartbeat_stop, heartbeat_thread)
        except RunStartupRejectedError as exc:
            startup_cleanup_unconfirmed = not exc.termination_confirmed
            raise
        finally:
            if not startup_cleanup_unconfirmed and not self._finish_host_run_generation(
                worker, effective_run_id, process, owned_session, host_slot_token,
                clear_session=False,
            ):
                raise RuntimeErrorBase("Host mission ownership changed during cleanup")

        exit_code = self._final_host_exit_code(exit_path, exit_code)
        stdout = raw_stdout.read_text() if raw_stdout.exists() else ""
        stderr = raw_stderr.read_text() if raw_stderr.exists() else ""
        redacted_stdout = _redact_text(stdout, max_chars=16000)
        redacted_stderr = _redact_text(stderr, max_chars=16000)
        with stdout_path.open("a") as aggregate:
            if redacted_stdout:
                aggregate.write(redacted_stdout)
                if not redacted_stdout.endswith("\n"):
                    aggregate.write("\n")
            stdout_path.chmod(0o600)
        with stderr_path.open("a") as aggregate:
            if redacted_stderr:
                aggregate.write(redacted_stderr)
                if not redacted_stderr.endswith("\n"):
                    aggregate.write("\n")
            stderr_path.chmod(0o600)

        self._write_action_audit(
            worker,
            {
                "kind": "run.completed" if exit_code == 0 else "run.failed",
                "run_id": effective_run_id,
                "cwd": str(workspace),
                "exit_code": exit_code,
                "stdout_tail": redacted_stdout[-2000:],
                "stderr_tail": redacted_stderr[-2000:],
            },
        )

        self._record_run_metrics(worker["worker_id"], effective_run_id, stdout)
        try:
            self._finalize_stop_reason(worker["worker_id"], run_id=effective_run_id,
                                       attempt_id=str((owned_session or {}).get("attempt_id") or ""))
        except RuntimeErrorBase as exc:
            if isinstance(exc, WorkerPausedError):
                active_state = "paused"
            elif isinstance(exc, WorkerInterruptedError):
                active_state = "interrupted"
            elif isinstance(exc, WorkerTerminatedError):
                active_state = "terminated"
            else:
                active_state = "failed"
            evidence_path = _write_evidence_for_run(
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=str(exc),
                exit_code=exit_code,
                timeout_seconds=run_timeout_sec,
                stop_reason=exc.__class__.__name__,
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state=active_state,
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason=exc.__class__.__name__,
                evidence_path=evidence_path,
            )
            raise
        if exit_code != 0:
            if self.runtime_name == "claude-code":
                classification = classify_cli_failure(
                    stdout=stdout,
                    stderr=stderr,
                    runtime_name=self.runtime_name,
                    exit_code=exit_code,
                )
                detail = classification.user_message
            else:
                detail = (redacted_stderr or redacted_stdout or "").strip()[-2000:]
            self._append_work_log(worker, f"Run {effective_run_id} failed with exit code {exit_code}.")
            error_text = f"{self.runtime_name} exited with code {exit_code}: {detail}"
            evidence_path = _write_evidence_for_run(
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=error_text,
                exit_code=exit_code,
                timeout_seconds=run_timeout_sec,
                stop_reason="process_exit",
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="process_exit",
                evidence_path=evidence_path,
            )
            raise self._provider_process_exit_error_for_run(
                worker=worker,
                run_id=effective_run_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                message=f"{self.runtime_name} exited with code {exit_code}: {detail}",
            )

        session_key, output = self._parse_output(worker, stdout, stderr, info)
        self._remember_native_session_key(worker, session_key)
        if _FINAL_REPORT_PATTERN.search(stdout) and not _FINAL_REPORT_PATTERN.search(output):
            output = f"FINAL REPORT:\n{output.strip()}"
        redacted_output = _redact_text(output.strip())
        if len(redacted_output) > _HOST_RUN_OUTPUT_MAX_CHARS:
            redacted_output = f"{redacted_output[: _HOST_RUN_OUTPUT_MAX_CHARS - 3].rstrip()}..."
        evidence_path = _write_evidence_for_run(
            worker=worker,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            command=command,
            env=env,
            workspace=workspace,
            stdout_text=stdout,
            stderr_text=stderr,
            output_text=redacted_output,
            error_text="",
            exit_code=exit_code,
            timeout_seconds=run_timeout_sec,
            stop_reason="process_exit",
            constraint_ledger=constraint_ledger,
            transcript_paths=transcript_paths,
            started_at=started_at,
        )
        try:
            evidence_status, warning_message = _require_successful_run_evidence(
                workspace=workspace,
                evidence_path=evidence_path,
                constraint_ledger_path=constraint_ledger_path,
                run_id=effective_run_id,
                attempt_id=str(worker.get("_run_attempt_id") or "").strip(),
            )
        except RuntimeErrorBase as exc:
            evidence_message = str(exc)
            if evidence_path:
                self._write_action_audit(
                    worker,
                    {
                        "kind": "run.evidence_failed",
                        "run_id": effective_run_id,
                        "evidence_path": evidence_path,
                        "message": evidence_message,
                    },
                )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="evidence_check_failed",
                evidence_path=evidence_path,
            )
            self._append_work_log(worker, f"Run {effective_run_id} failed evidence check.")
            raise
        if evidence_status == "warn":
            warning_suffix = f"\n\n{warning_message}"
            if len(redacted_output) + len(warning_suffix) <= _HOST_RUN_OUTPUT_MAX_CHARS:
                redacted_output = f"{redacted_output}{warning_suffix}"
            self._append_work_log(worker, f"Run {effective_run_id} completed with evidence warning.")
        if evidence_path:
            self._write_action_audit(
                worker,
                {
                    "kind": "run.evidence",
                    "run_id": effective_run_id,
                    "evidence_path": evidence_path,
                },
            )
        _write_active_run_status(
            path=heartbeat_path,
            worker=worker,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            state="completed",
            transcript_paths=transcript_paths,
            started_at=started_at_iso,
            process_pid=process_pid,
            timeout_seconds=run_timeout_sec,
            exit_code=exit_code,
            stop_reason="process_exit",
            evidence_path=evidence_path,
        )
        self._append_work_log(worker, f"Run {effective_run_id} completed.")
        return redacted_output

    def terminal_target(self, worker: dict) -> TerminalTarget:
        info = self.ensure_worker_ready(worker)
        active = self._infer_active_session(worker)
        stdout = str((active or {}).get("stdout_path") or "")
        command = ["bash", "-lc", f"cd {shlex.quote(str(info.workspace_dir or ''))} && tail -n 80 -f {shlex.quote(stdout)}"] if stdout else ["bash", "-lc", f"cd {shlex.quote(str(info.workspace_dir or ''))} && exec ${{SHELL:-/bin/bash}}"]
        return TerminalTarget(
            command=command,
            cwd=str(info.workspace_dir or ""),
            env={"TERM": "xterm-256color"},
            title=f"{worker['name']} host session" if active else f"{worker['name']} host terminal",
            subtitle=f"{self.runtime_name} on host computer",
        )

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        info = self.ensure_worker_ready(worker)
        notes = {
            "terminal": "Host-native workers expose terminal takeover through the local terminal target.",
            "files": "Opened the host workspace in the system file browser.",
            "browser": "Opened the requested URL in the host browser.",
            "focus_browser": "Requested the host browser to open or focus.",
            "codex": "Host-native Codex runs use the installed Codex CLI on the main computer.",
            "claude": "Host-native Claude runs use the installed Claude CLI on the main computer.",
            "openclaw": "Host-native OpenClaw runs use the installed OpenClaw CLI on the main computer.",
        }
        if action == "files":
            subprocess.run(["open", str(info.workspace_dir or "")], check=False)
        elif action in {"browser", "focus_browser"} and url:
            subprocess.run(["open", url], check=False)
        self._write_action_audit(
            worker,
            {
                "kind": "desktop_action",
                "action": action,
                "url": _redact_text(url or ""),
                "cwd": str(info.workspace_dir or ""),
            },
        )
        return {
            "action": action,
            "status": "launched" if action in {"files", "browser", "focus_browser"} else "available",
            "mode": "host-computer",
            "url": url,
            "view_url": None,
            "notes": notes.get(action, "Host-native worker action recorded."),
        }

    def describe_worker(self, worker: dict) -> dict[str, object]:
        self._materialize_workspace(worker, self._host_workspace_dir(worker))
        info = self.reconcile_worker(worker)
        return {
            "mode": "host-computer",
            "runtime": self.runtime_name,
            "execution_mode": "host",
            "workspace_dir": info.workspace_dir or "",
            "state_dir": info.state_dir or "",
            "pid": info.pid,
            "host_workspace_root": str(self._host_workspace_root(worker)),
            "prompt_paths": {
                "project_definition": str(Path(info.workspace_dir or "") / "project-definition.md"),
                "work_log": str(Path(info.workspace_dir or "") / "work-log.md"),
                "harness_prompt": str(Path(info.workspace_dir or "") / "harness-prompt.md"),
                "agents_md": str(Path(info.workspace_dir or "") / "AGENTS.md"),
                "claude_md": str(Path(info.workspace_dir or "") / "CLAUDE.md"),
                "codex_md": str(Path(info.workspace_dir or "") / "CODEX.md"),
            },
        }


    def host_active_process_status(self, worker: dict) -> dict[str, object]:
        worker_id = str(worker.get("worker_id") or "")
        active_session = self._read_active_session(worker_id)
        if not active_session:
            with self._process_lock:
                process = self._active_processes.get(worker_id)
            if process and process.poll() is None:
                return {"state": "uncertain", "run_id": ""}
            return {"state": "absent"}
        start_identity = str(
            active_session.get("process_start_identity") or ""
        ).strip()
        run_id = str(active_session.get("run_id") or "").strip()
        raw_pid = active_session.get("process_pid")
        if raw_pid is None or raw_pid == "":
            pid = 0
        elif isinstance(raw_pid, bool):
            return {"state": "uncertain", "run_id": run_id}
        elif isinstance(raw_pid, int):
            pid = raw_pid
        elif isinstance(raw_pid, str) and re.fullmatch(
            r"(?:0|[1-9][0-9]*)", raw_pid
        ):
            pid = int(raw_pid)
        else:
            return {"state": "uncertain", "run_id": run_id}
        if pid < 0:
            return {"state": "uncertain", "run_id": run_id}
        if pid <= 0:
            if self._pidless_host_session_has_exact_terminal_proof(
                worker_id,
                active_session,
            ):
                return {"state": "absent", "run_id": run_id}
            with self._process_lock:
                process = self._active_processes.get(worker_id)
            live_in_process_handle = bool(process and process.poll() is None)
            return {
                "state": "uncertain",
                "run_id": run_id,
                "historical_record_only": not live_in_process_handle,
            }
        if not self._pid_is_live(pid) or self._pid_is_zombie(pid):
            return {"state": "absent", "run_id": run_id}
        current_identity = self._process_start_identity(pid)
        if not start_identity:
            recorded_started_at = _session_started_at_epoch(active_session)
            current_started_at = _process_start_identity_epoch(current_identity)
            if (
                recorded_started_at is not None
                and current_started_at is not None
                and current_started_at > recorded_started_at + 300
            ):
                return {"state": "absent", "run_id": run_id}
            return {"state": "uncertain", "run_id": run_id}
        if not current_identity:
            return {"state": "uncertain", "run_id": run_id}
        if current_identity != start_identity:
            return {"state": "absent", "run_id": run_id}
        return {
            "state": "active",
            "run_id": run_id,
            "pid": pid,
            "process_start_identity": start_identity,
        }

    def _agent_builder_output_schema(self, worker: dict) -> dict[str, object] | None:
        if not self._conversation_mode_from_worker(worker):
            return None
        bundle = self._bootstrap_bundle_for_worker(worker)
        schema = graph_transfer_output_schema(bundle.get("agent_builder_control"))
        return schema if isinstance(schema, dict) else None

    def _agent_builder_output_schema_path(
        self,
        worker: dict,
    ) -> Path | None:
        schema = self._agent_builder_output_schema(worker)
        if not schema:
            return None
        path = self._state_dir(str(worker["worker_id"])) / "agent-builder-output-schema.json"
        _atomic_write_private_text(
            path,
            json.dumps(schema, separators=(",", ":"), sort_keys=True),
        )
        return path







    def host_process_identity(self, worker: dict, run_id: str) -> dict[str, object] | None:
        active_session = self._read_active_session(str(worker.get("worker_id") or ""))
        if str((active_session or {}).get("run_id") or "") != str(run_id):
            return None
        try:
            pid = int((active_session or {}).get("process_pid") or 0)
            process_group = int((active_session or {}).get("process_group") or 0)
        except (TypeError, ValueError):
            return None
        start_identity = str(
            (active_session or {}).get("process_start_identity") or ""
        ).strip()
        verified = self._recorded_process_is_running(pid, start_identity)
        if not verified:
            return None
        return {
            "identity_kind": "host_process",
            "pid": pid,
            "process_group": process_group or pid,
            "process_start_identity": start_identity,
            "container_id": "",
            "session_id": str((active_session or {}).get("session_name") or ""),
            "startup_token_digest": str(
                (active_session or {}).get("startup_token_digest") or ""
            ),
            "verified": True,
        }

    def cleanup_orphaned_run(self, worker: dict, run_id: str) -> RuntimeInfo:
        worker_id = str(worker["worker_id"])
        active_session = self._read_active_session(worker_id)
        if not active_session:
            if self.host_active_process_status(worker).get("state") != "absent":
                raise RuntimeErrorBase(
                    "The orphaned host process identity is not confirmed"
                )
            return self._host_runtime_info(worker, pid=None)
        if str(active_session.get("run_id") or "").strip() != str(run_id):
            raise RuntimeErrorBase(
                "The orphaned host run does not own the active session"
            )
        active_status = self.host_active_process_status(worker)
        if active_status.get("state") == "uncertain":
            raise RuntimeErrorBase(
                "The orphaned host process identity is not confirmed"
            )
        if active_status.get("state") == "active":
            if not self._stop_active_process(
                worker_id,
                worker=worker,
                run_id=str(run_id),
                expected_session=active_session,
                control_session=active_session,
            ):
                raise RuntimeErrorBase(
                    "Orphaned host run termination could not be confirmed"
                )
            return self._host_runtime_info(worker, pid=None)
        with self._process_lock:
            inactive_process = self._active_processes.get(worker_id)
        if inactive_process is not None and inactive_process.poll() is None:
            raise RuntimeErrorBase(
                "The orphaned host process identity is not confirmed"
            )
        if not self._finalize_owned_host_generation(
            worker_id,
            expected_process=inactive_process,
            expected_session=active_session,
            expected_slot_token=(
                str(active_session.get("host_slot_token") or "").strip() or None
            ),
            clear_session=True,
        ):
            raise RuntimeErrorBase(
                "Host run ownership changed during orphan cleanup"
            )
        return self._host_runtime_info(worker, pid=None)




def _codex_binary_with_discoverable_companion(binary: str) -> str:
    """Resolve a Codex symlink only when its canonical bundle carries the required host."""
    resolved = shutil.which(binary)
    if not resolved:
        return binary
    invoked = Path(resolved)
    if not invoked.is_symlink():
        return binary
    canonical = invoked.resolve()
    companion = canonical.parent / "codex-code-mode-host"
    if (
        canonical.is_file()
        and os.access(canonical, os.X_OK)
        and companion.is_file()
        and os.access(companion, os.X_OK)
    ):
        return str(canonical)
    return binary

class HostCodexCliRuntime(HostNativeCliMixin, CodexCliRuntime):
    worker_root_name = "host_codex_cli_runtime"
    binary_env_var = "WPR_CODEX_BIN"

    def __init__(
        self,
        base_dir: str | None = None,
        *,
        create_directories: bool = True,
    ) -> None:
        super().__init__(base_dir=base_dir, create_directories=create_directories)
        # Standalone GlassHive installs may not pass through Viventium's config
        # compiler. Keep the runtime's own host-worker boundary capability-aware.
        self.binary = _codex_binary_with_discoverable_companion(self.binary)
        self._provider_control_cache_lock = Lock()
        self._provider_control_cache: dict[
            tuple[str, str], tuple[float, tuple[dict[str, object], dict[str, object]]]
        ] = {}

    def _native_openai_provider_configured(self, worker: dict) -> bool:
        config_path = self._host_codex_home(worker) / "config.toml"
        if not config_path.is_file():
            return True
        try:
            config = tomllib.loads(config_path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return False
        if not isinstance(config, dict):
            return False
        provider = str(config.get("model_provider") or "").strip()
        return provider in {"", "openai"}

    def _query_codex_provider_control(
        self,
        worker: dict,
        thread_id: str,
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        """Read typed thread/rate-limit state from the exact local Codex runtime."""

        if not self._native_openai_provider_configured(worker):
            return None
        source_env = self._host_env(worker)
        allowed_env = {
            "HOME",
            "PATH",
            "SHELL",
            "TERM",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "USER",
            "LOGNAME",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        env = {
            key: value
            for key, value in source_env.items()
            if key in allowed_env or key.startswith("LC_")
        }
        env["CODEX_HOME"] = str(self._host_codex_home(worker))
        process: subprocess.Popen[str] | None = None
        reader: Thread | None = None
        messages: queue.Queue[dict[str, object] | None] = queue.Queue()
        deadline = time.monotonic() + 8.0

        def read_messages() -> None:
            assert process is not None and process.stdout is not None
            try:
                for line in process.stdout:
                    if len(line) > 4 * 1024 * 1024:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        messages.put(payload)
            finally:
                messages.put(None)

        def send(payload: dict[str, object]) -> None:
            if process is None or process.stdin is None:
                raise OSError("Codex app-server input is unavailable")
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()

        def await_response(request_id: str) -> dict[str, object] | None:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    message = messages.get(timeout=remaining)
                except queue.Empty:
                    return None
                if message is None:
                    return None
                if str(message.get("id") or "") != request_id:
                    continue
                result = message.get("result")
                return result if isinstance(result, dict) else None

        try:
            process = subprocess.Popen(
                [self.binary, "app-server", "--stdio"],
                env=env,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=1,
                start_new_session=True,
            )
            reader = Thread(
                target=read_messages,
                name="glasshive-codex-provider-health",
                daemon=True,
            )
            reader.start()
            send(
                {
                    "id": "glasshive-provider-health-init",
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "glasshive-provider-health",
                            "title": "GlassHive Provider Health",
                            "version": "1",
                        },
                        "capabilities": {},
                    },
                }
            )
            if await_response("glasshive-provider-health-init") is None:
                return None
            send({"method": "initialized", "params": {}})
            send(
                {
                    "id": "glasshive-provider-thread",
                    "method": "thread/read",
                    "params": {"threadId": thread_id, "includeTurns": True},
                }
            )
            thread_result = await_response("glasshive-provider-thread")
            if thread_result is None:
                return None
            send(
                {
                    "id": "glasshive-provider-limits",
                    "method": "account/rateLimits/read",
                    "params": None,
                }
            )
            rate_limits_result = await_response("glasshive-provider-limits")
            if rate_limits_result is None:
                return None
            return thread_result, rate_limits_result
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        finally:
            if process is not None:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
            if reader is not None:
                reader.join(timeout=0.5)

    def _supplemental_provider_failure(
        self,
        *,
        worker: dict,
        run_id: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> tuple[FailureClassification, dict[str, object]] | None:
        _ = (run_id, stderr, exit_code)
        thread_id, authored = _codex_failed_thread(stdout)
        if not thread_id:
            return None
        cache_key = (str(worker.get("worker_id") or ""), thread_id)
        with self._provider_control_cache_lock:
            cached = self._provider_control_cache.get(cache_key)
        control = cached[1] if cached and cached[0] > time.monotonic() else None
        if control is None:
            control = self._query_codex_provider_control(worker, thread_id)
            if control is not None:
                with self._provider_control_cache_lock:
                    self._provider_control_cache[cache_key] = (
                        time.monotonic() + 30.0,
                        control,
                    )
                    while len(self._provider_control_cache) > 32:
                        self._provider_control_cache.pop(
                            next(iter(self._provider_control_cache))
                        )
        if control is None:
            return None
        return _codex_typed_provider_failure(
            thread_id=thread_id,
            thread_result=control[0],
            rate_limits_result=control[1],
            expected_model_provider="openai",
            authored=authored,
        )

    def resolve_model(self, profile: str) -> str:
        if profile != "codex-cli":
            return super().resolve_model(profile)
        host_model = os.environ.get("WPR_MODEL_HOST_CODEX_CLI", "").strip()
        if host_model:
            return host_model
        codex_model = os.environ.get("CODEX_MODEL", "").strip()
        if codex_model:
            return codex_model
        inherit_provider_model = os.environ.get(
            "GLASSHIVE_HOST_CODEX_INHERIT_PROVIDER_MODEL",
            os.environ.get("WPR_HOST_CODEX_INHERIT_PROVIDER_MODEL", ""),
        ).strip().lower() in {"1", "true", "yes", "on"}
        if inherit_provider_model:
            return os.environ.get("WPR_MODEL_CODEX_CLI", "").strip()
        return ""

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        info = HostNativeCliMixin.ensure_worker_ready(self, worker)
        # An explicit local host Codex worker has an isolated CODEX_HOME, so it
        # needs the same owner-local login baseline even when no optional MCP,
        # personality, or developer-instruction bundle was supplied.  Automatic
        # Parallel work is Docker-only and enterprise deployments must project
        # server-owned credentials instead of copying host authority.
        if not (
            self._env_flag("GLASSHIVE_ENTERPRISE_MODE", False)
            or self._env_flag("WPR_ENTERPRISE_MODE", False)
        ) and native_current_account_allowed(worker):
            self._copy_host_codex_auth(self._host_codex_home(worker))
        workspace = Path(str(info.workspace_dir or ""))
        if (
            not self._conversation_mode_from_worker(worker)
            and workspace.exists()
            and not (workspace / ".git").exists()
        ):
            self._ensure_git_workspace(str(workspace))
        return info

    def _codex_reasoning_effort_for_worker(self, worker: dict) -> str:
        requested = (
            self._bootstrap_env_value(worker, "WPR_CODEX_CLI_REASONING_EFFORT")
            or os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT", "")
            or os.environ.get("WPR_CODEX_CLI_DEFAULT_REASONING_EFFORT", "")
        ).strip().lower()
        allowed = {"low", "medium", "high", "xhigh", "max", "ultra"}
        if requested and requested not in allowed:
            raise RuntimeErrorBase(
                f"Unsupported native Codex effort: {requested}"
            )
        if requested:
            worker["_effort_projection"] = {
                "requested": requested,
                "effective": requested,
                "allowed": sorted(allowed),
                "route_proven": True,
                "fallback_reason": "",
            }
        return requested

    def _conversation_primary_workspace(
        self,
        worker: dict,
        workspace: str,
    ) -> tuple[str, str]:
        restricted = _native_tools_disabled(self._bootstrap_bundle_for_worker(worker))
        if not restricted and (
            not self._conversation_mode_from_worker(worker)
            or _host_codex_conversation_project_instructions() == "inherit"
        ):
            return workspace, ""
        primary = self._state_dir(str(worker["worker_id"])) / "conversation-workspace"
        primary.mkdir(parents=True, exist_ok=True)
        primary.chmod(0o700)
        return str(primary), "" if restricted else workspace

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        self._assert_host_codex_worker_policy(worker)
        restricted = _native_tools_disabled(self._bootstrap_bundle_for_worker(worker))
        existing_session = (
            None
            if restricted or self._provider_session_starts_fresh(worker)
            else self._read_provider_session_key(worker)
        )
        model = self._codex_model_for_worker(worker, "WPR_MODEL_HOST_CODEX_CLI")
        is_resume = bool(
            existing_session
            and not str(existing_session).startswith("codex-worker:")
        )
        dangerous_mode = os.environ.get("WPR_CODEX_DANGEROUS", "1").strip().lower() in {"1", "true", "yes", "on"}
        access_mode = "full"
        if self._conversation_mode_from_worker(worker):
            bundle = self._bootstrap_bundle_for_worker(worker)
            access_mode = str(bundle.get("access_mode") or "full").strip().lower()
            dangerous_mode = access_mode == "full"
        read_only_mode = access_mode == "read_only"
        conversation_mode = self._conversation_mode_from_worker(worker)
        workspace = str(info.workspace_dir or ".")
        primary_workspace, additional_workspace = self._conversation_primary_workspace(
            worker,
            workspace,
        )
        if is_resume:
            command = [self.binary, "exec", "resume"]
            if conversation_mode:
                command.extend(["--json", "--skip-git-repo-check"])
        else:
            command = [
                self.binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-C",
                primary_workspace,
            ]
            if additional_workspace:
                command.extend(["--add-dir", additional_workspace])
        if model:
            if is_resume:
                command.extend(["-c", f'model="{model}"'])
            else:
                command.extend(["-m", model])
        if not restricted:
            self._append_codex_user_config_policy(command, worker)
        if validated_codex_broker_projection(worker) is not None:
            self._append_codex_compatible_provider_config(
                command,
                worker,
                include_reasoning_effort=False,
            )
        self._append_codex_reasoning_effort_config(command, worker)
        native_web_locked = _host_native_web_access() == "disabled"
        if restricted:
            command.extend(["--strict-config", "--ignore-rules", "--enable", "code_mode_host", "-s", "read-only",
                "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", "project_doc_max_bytes=0"])
            for feature in _CODEX_RESTRICTED_NATIVE_FEATURES:
                command.extend(["--disable", feature])
        elif native_web_locked:
            command.extend(
                [
                    "-c",
                    'web_search="disabled"',
                    "-c",
                    f'sandbox_mode="{"read-only" if read_only_mode else "workspace-write"}"',
                    "-c",
                    'approval_policy="never"',
                ]
            )
            if not read_only_mode:
                command.extend(
                    ["-c", "sandbox_workspace_write.network_access=false"]
                )
            for feature in _CODEX_NATIVE_WEB_LOCKDOWN_FEATURES:
                command.extend(["--disable", feature])
        elif read_only_mode:
            command.extend(
                [
                    "-c",
                    'sandbox_mode="read-only"',
                    "-c",
                    'approval_policy="never"',
                ]
            )
        elif dangerous_mode:
            if is_resume:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.extend(["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"])
        elif is_resume:
            command.extend(
                [
                    "-c",
                    'sandbox_mode="workspace-write"',
                    "-c",
                    'approval_policy="never"',
                ]
            )
        else:
            # Current Codex CLIs removed --full-auto; use the resume path's overrides.
            command.extend(["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"'])
        output_schema_path = self._conversation_output_schema_path(worker)
        if output_schema_path:
            command.extend(["--output-schema", str(output_schema_path)])
        for image_path, _mime_type, _data in self._native_input_image_files(worker, info):
            command.extend(["--image", str(image_path)])
        if native_installed_owner_id() is not None or worker.get("_glasshive_provider_api_key_file"):
            command.extend(["-c", 'cli_auth_credentials_store="file"'])
        if is_resume:
            command.append(existing_session)
        command.append("-")
        env = self._host_env(worker)
        codex_home = self._host_codex_home(worker)
        if conversation_mode or (codex_home / "config.toml").exists():
            env["CODEX_HOME"] = str(codex_home)
        apply_bound_provider_account_environment(
            worker,
            env,
            runtime_name=self.runtime_name,
        )
        projection = validated_codex_broker_projection(worker)
        if projection is not None:
            for key in _CODEX_BROKER_CONFLICTING_ENV:
                env.pop(key, None)
            env["OPENAI_API_KEY"] = str(projection["grant_token"])
        return command, env







class HostClaudeCodeRuntime(HostNativeCliMixin, ClaudeCodeRuntime):
    worker_root_name = "host_claude_code_runtime"
    binary_env_var = "WPR_CLAUDE_CODE_BIN"

    def _conversation_output_schema(self, worker: dict) -> dict[str, object] | None:
        schema = super()._conversation_output_schema(worker)
        if not schema:
            return None
        # Claude Code rejects the canonical 2020-12 declaration while accepting
        # the same constraints in an unqualified schema.
        projected_schema = dict(schema)
        projected_schema.pop("$schema", None)
        return projected_schema

    def _conversation_workspace_side_effect_state(self, workspace: Path) -> dict[str, bool]:
        claude_dir = workspace / ".claude"
        marker = claude_dir / ".cc-writes"
        return {
            "claude_dir_existed": claude_dir.exists() or claude_dir.is_symlink(),
            "cc_writes_existed": marker.exists() or marker.is_symlink(),
        }

    def _cleanup_conversation_workspace_side_effects(
        self,
        workspace: Path,
        state: dict[str, bool],
    ) -> None:
        if state.get("cc_writes_existed"):
            return
        claude_dir = workspace / ".claude"
        marker = claude_dir / ".cc-writes"
        if marker.is_symlink() or marker.is_file():
            marker.unlink()
        elif marker.is_dir():
            shutil.rmtree(marker)
        if not state.get("claude_dir_existed"):
            try:
                claude_dir.rmdir()
            except FileNotFoundError:
                pass

    def _chrome_supported(self) -> bool:
        return self._help_supports("--chrome")

    def _effort_supported(self, effort: str = "") -> bool:
        help_text = self._help_text()
        return bool(help_text) and _claude_effort_help_supports(help_text, effort)

    def _help_text(self) -> str:
        resolved = shutil.which(self.binary)
        if not resolved:
            return ""
        try:
            completed = subprocess.run(
                [resolved, "--help"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except Exception:
            return ""
        return f"{completed.stdout}\n{completed.stderr}"

    def _help_supports(self, flag: str) -> bool:
        return flag in self._help_text()

    def _requires_max_effort(self, worker: dict | None = None) -> bool:
        worker = worker or {}
        effort = (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()
        return effort == "max"

    def _raise_missing_effort_support(self, profile: str, execution_mode: str, effort: str) -> None:
        raise RuntimeDependencyMissingError(
            f"Claude Code workers requested `{effort}` effort, but the configured Claude Code CLI "
            "does not advertise that native --effort capability.",
            binary=self.binary,
            runtime_name=self.runtime_name,
            profile=profile,
            execution_mode=execution_mode,
            dependency_label="Claude Code",
            recovery_hint=(
                "Update Claude Code to a version with native --effort support, or use default "
                "Claude effort only when that lower-effort mode is intended."
            ),
        )

    def preflight_worker_profile(self, profile: str, execution_mode: str = "host") -> None:
        super().preflight_worker_profile(profile, execution_mode)
        effort = os.environ.get("WPR_CLAUDE_CODE_EFFORT", "").strip().lower()
        if effort and effort != "default" and not self._effort_supported(effort):
            self._raise_missing_effort_support(profile, execution_mode, effort)
        if self._chrome_enabled() and not self._chrome_supported():
            raise RuntimeDependencyMissingError(
                "Claude Code host workers require a Claude Code CLI that supports --chrome, "
                "or WPR_CLAUDE_CODE_ENABLE_CHROME=0 for an explicit locked-down launch.",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=profile,
                execution_mode=execution_mode,
                dependency_label="Claude Code",
                recovery_hint=(
                    "Update Claude Code to a version with Chrome integration, or explicitly disable "
                    "host Claude Chrome support only when that locked-down mode is intended."
                ),
            )
        enterprise_mode = any(
            str(os.environ.get(name, "")).strip().lower()
            in {"1", "true", "yes", "on"}
            for name in ("GLASSHIVE_ENTERPRISE_MODE", "WPR_ENTERPRISE_MODE")
        )
        if not enterprise_mode:
            # Fail before a provider session, worker, or run is reserved. The
            # command builder repeats this check at execution time because
            # credentials can still be revoked after admission.
            self._inject_private_subscription_auth(dict(os.environ))

    def _inject_private_subscription_auth(self, env: dict[str, str]) -> str:
        """Project access-only auth or select the owner's managed local login boundary."""
        if native_installed_owner_id() is not None:
            selected = _native_cli_managed_auth_env(env)
            probe_env = dict(selected)
            probe_env.pop("CLAUDE_CONFIG_DIR", None)
            if not _claude_cli_managed_auth_available(
                self.binary, child_env=probe_env
            ):
                raise ProviderAuthenticationMissingError(
                    "Installed Claude Code login is unavailable; reconnect through the installed native owner.",
                    binary=self.binary,
                    runtime_name=self.runtime_name,
                    profile="claude-code",
                    execution_mode="host",
                    dependency_label="Claude Code authentication",
                    recovery_hint=(
                        "Reconnect the installed Claude Code login, then retry the same request."
                    ),
                )
            env.clear()
            env.update(selected)
            return "owner_managed"
        env.pop("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", None)
        # Scope metadata is meaningful only when it came from the same selected
        # access-token record. Never retain ambient or prior-projection scopes.
        env.pop("CLAUDE_CODE_OAUTH_SCOPES", None)
        projected_access_token = _usable_claude_oauth_token(
            env.get("CLAUDE_CODE_OAUTH_TOKEN")
        )
        if projected_access_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = projected_access_token
            return "projected_access_token"

        explicit_access_token = _usable_explicit_claude_oauth_token()
        if explicit_access_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = explicit_access_token
            return "explicit_access_token"

        keychain_oauth = _read_claude_keychain_oauth()
        if _claude_keychain_access_token_is_fresh(keychain_oauth):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = str(keychain_oauth.get("accessToken") or "").strip()
            scopes = keychain_oauth.get("scopes")
            if isinstance(scopes, list) and scopes and all(
                isinstance(scope, str)
                and re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", scope)
                for scope in scopes
            ):
                env["CLAUDE_CODE_OAUTH_SCOPES"] = " ".join(scopes)
            return "keychain_access_token"

        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        if _claude_cli_managed_auth_available(self.binary, child_env=env):
            return "isolated_managed"

        owner_env = _native_cli_managed_auth_env(env)
        owner_home = str(owner_env.get("HOME") or "").strip()
        if (
            owner_home
            and Path(owner_home).is_absolute()
            and _claude_cli_managed_auth_available(self.binary, child_env=owner_env)
        ):
            # Select the native owner's credential store while retaining the
            # worker's isolated session/config directory and capability ceiling.
            env.clear()
            env.update(owner_env)
            return "owner_managed"
        raise ProviderAuthenticationMissingError(
            "Claude Code authentication is unavailable for this host worker. "
            "Run `claude auth login` for managed local authentication or provision a supported "
            "headless token with `claude setup-token`, then try again.",
            binary=self.binary,
            runtime_name=self.runtime_name,
            profile="claude-code",
            execution_mode="host",
            dependency_label="Claude Code authentication",
            recovery_hint=(
                "Reconnect the configured Claude Code account, then retry the same request."
            ),
        )


    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        text = super()._command_stdin_text(worker, instruction, info)
        images = self._native_input_image_files(worker, info)
        if not images:
            return text
        # Conversation mode has no duplex mission wrapper. These bytes go directly
        # to the CLI's documented JSONL user frame, not through a text re-wrapper.
        content = [{"type": "text", "text": str(text or "")}]
        content.extend({"type": "image", "source": {"type": "base64", "media_type": mime_type,
            "data": base64.b64encode(data).decode("ascii")}} for _path, mime_type, data in images)
        return json.dumps({"type": "user", "session_id": "", "parent_tool_use_id": None,
            "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": content}}, separators=(",", ":")) + "\n"

    def _native_input_context(self, worker: dict) -> dict[str, object] | None:
        if self._conversation_mode_from_worker(worker):
            return None
        return {"version": 1, "workerId": str(worker["worker_id"]),
                "runId": str(worker["_active_run_id"]), "contextToken": secrets.token_hex(32)}

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_key = (
            None
            if self._provider_session_starts_fresh(worker)
            else self._read_provider_session_key(worker)
        )
        model = self._provider_model_for_worker(worker)
        native_web_locked = _host_native_web_access() == "disabled"
        permission_mode = os.environ.get("WPR_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions")
        bundle = self._bootstrap_bundle_for_worker(worker)
        selectors = prepare_private_command(self, worker, bundle)
        if self._conversation_mode_from_worker(worker):
            permission_mode = (
                "bypassPermissions"
                if str(bundle.get("access_mode") or "full").strip().lower() == "full"
                else "acceptEdits"
            )
        if native_web_locked:
            permission_mode = "acceptEdits"
        output_format = "stream-json"
        command = [
            self.binary,
            "-p",
            "--permission-mode",
            permission_mode,
            "--output-format",
            output_format,
            "--model",
            model,
        ]
        if not self._conversation_mode_from_worker(worker):
            command.extend(["--input-format", "stream-json", "--verbose"])
        elif self._native_input_image_files(worker, info):
            command.extend(["--input-format", "stream-json"])
        # Only explicit per-run settings cross the managed-login boundary.
        settings: dict[str, object] = json.loads(json.dumps(bundle.get("claude_settings_local") or {}))
        if self._conversation_mode_from_worker(worker):
            auto_memory = _host_claude_conversation_auto_memory()
            if auto_memory is not None:
                settings["autoMemoryEnabled"] = auto_memory
        denied_plugins = self._host_plugin_denylist()
        if denied_plugins:
            settings["enabledPlugins"] = {
                **(settings.get("enabledPlugins") or {}),
                **{plugin_id: False for plugin_id in denied_plugins},
            }
        if self._conversation_mode_from_worker(worker):
            command.append("--verbose")
            command.append("--include-partial-messages")
            developer_instructions = (
                str(bundle.get("developer_instructions") or "")
                if "developer_instructions" in bundle
                else ""
            )
            if developer_instructions:
                # Claude Code exposes request-time application authority through its native
                # system-prompt channel. Keep it private and out of both process argv and the
                # user-authored visible history.
                authority_path = (
                    self._state_dir(str(worker["worker_id"]))
                    / "developer-instructions.txt"
                )
                try:
                    actual = authority_path.read_text()
                except OSError as exc:
                    raise RuntimeErrorBase(
                        "Claude Code conversation developer instruction authority is unavailable"
                    ) from exc
                if actual != developer_instructions:
                    raise RuntimeErrorBase(
                        "Claude Code conversation developer instruction authority is stale"
                    )
                command.extend(["--append-system-prompt-file", str(authority_path)])
            mcp_path = self._state_dir(str(worker["worker_id"])) / "conversation-mcp.json"
            mcp_config = str(mcp_path) if mcp_path.is_file() else '{"mcpServers":{}}'
            command.extend(["--mcp-config", mcp_config, "--strict-mcp-config"])
            bundle = self._bootstrap_bundle_for_worker(worker)
            if str(bundle.get("access_mode") or "full").strip().lower() == "workspace":
                workspace = str(Path(str(info.workspace_dir or ".")).resolve())
                settings.update(
                    {
                        "permissions": {"defaultMode": "acceptEdits"},
                        "sandbox": {
                            "enabled": True,
                            "failIfUnavailable": True,
                            "allowUnsandboxedCommands": False,
                            "filesystem": {
                                "denyRead": [str(Path.home().resolve())],
                                "allowRead": [workspace],
                            },
                        },
                    }
                )
        if not self._conversation_mode_from_worker(worker):
            # Keep the already-materialized mission broker explicit when the CLI
            # uses the owner's managed login; no ambient owner MCPs may load.
            mcp_path = Path(selectors["host_root"]) / "mcp.json" if selectors else Path(str(info.workspace_dir)) / ".mcp.json"
            mcp_config = str(mcp_path) if mcp_path.is_file() else '{"mcpServers":{}}'
            command.extend(["--mcp-config", mcp_config, "--strict-mcp-config"])
        self._assert_host_claude_mcp_config(worker, mcp_path)
        if native_web_locked:
            sandbox = settings.setdefault("sandbox", {})
            if isinstance(sandbox, dict):
                sandbox.update(
                    {
                        "enabled": True,
                        "failIfUnavailable": True,
                        "allowUnsandboxedCommands": False,
                        "network": {
                            "allowedDomains": [],
                            "strictAllowlist": True,
                        },
                    }
                )
            command.extend(["--setting-sources", ""])
        if worker.get("_glasshive_provider_api_key_file") and "--setting-sources" not in command:
            command.extend(["--setting-sources", ""])
        from .current_native_account import MARKER, project_current_settings
        if MARKER in worker:
            project_current_settings(worker, settings)
        if settings:
            command.extend(
                ["--settings", json.dumps(settings, separators=(",", ":"))]
            )
        if native_web_locked:
            command.extend(["--disallowedTools", "WebSearch", "WebFetch"])
            command.insert(2, "--no-chrome")
        elif self._chrome_enabled():
            command.insert(2, "--chrome")
        effort = (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()
        if effort and effort != "default":
            if not self._effort_supported(effort):
                self._raise_missing_effort_support(
                    str(worker.get("profile") or "claude-code"), "host", effort
                )
            command.extend(["--effort", effort])
        output_schema = self._conversation_output_schema(worker)
        if output_schema:
            command.extend(
                [
                    "--json-schema",
                    json.dumps(output_schema, separators=(",", ":"), sort_keys=True),
                ]
            )
        if session_key and not session_key.startswith("claude-worker:"):
            command.extend(["--resume", session_key])
        env = self._host_env(worker)
        env.pop("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", None)
        use_api_key = os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not use_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
        self._remove_conflicting_anthropic_credentials(env)
        apply_bound_provider_account_environment(
            worker,
            env,
            runtime_name=self.runtime_name,
        )
        if MARKER in worker:
            # Its lease already projected and checked the exact current OS identity.
            command.extend(["--setting-sources", ""])
        selection = mission_provider_account_selection(worker)
        use_host_auth = selection is None or (
            selection.policy == "personal_preferred"
            and worker.get("_glasshive_provider_account_preferred_fallback")
        )
        env.pop("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", None)
        if use_host_auth and not self._bedrock_enabled():
            enterprise_mode = any(
                str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}
                for name in ("GLASSHIVE_ENTERPRISE_MODE", "WPR_ENTERPRISE_MODE")
            )
            if not enterprise_mode and not (
                use_api_key and str(env.get("ANTHROPIC_API_KEY") or "").strip()
            ):
                bootstrap_access_token = _usable_claude_oauth_token(
                    self._bootstrap_env_value(worker, "CLAUDE_CODE_OAUTH_TOKEN")
                )
                projected_scopes = (
                    self._bootstrap_env_value(worker, "CLAUDE_CODE_OAUTH_SCOPES")
                    if bootstrap_access_token
                    and bootstrap_access_token
                    == _usable_claude_oauth_token(env.get("CLAUDE_CODE_OAUTH_TOKEN"))
                    else ""
                )
                auth_source = self._inject_private_subscription_auth(env)
                scope_parts = projected_scopes.split(" ") if projected_scopes else []
                if (
                    auth_source == "projected_access_token"
                    and scope_parts
                    and projected_scopes == " ".join(scope_parts)
                    and all(
                        re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", scope)
                        for scope in scope_parts
                    )
                ):
                    env["CLAUDE_CODE_OAUTH_SCOPES"] = projected_scopes
                if auth_source == "owner_managed":
                    # Select the native login without inheriting the owner's runtime settings.
                    command.extend(["--setting-sources", ""])
            elif not (
                _usable_claude_oauth_token(env.get("CLAUDE_CODE_OAUTH_TOKEN"))
                or (use_api_key and str(env.get("ANTHROPIC_API_KEY") or "").strip())
            ):
                raise ProviderAuthenticationMissingError(
                    "Enterprise host worker server-owned Claude Code authentication is unavailable.",
                    binary=self.binary,
                    runtime_name=self.runtime_name,
                    profile=str(worker.get("profile") or "claude-code"),
                    execution_mode="host",
                    dependency_label="Claude Code authentication",
                    recovery_hint=(
                        "Reconnect or reproject the server-owned Claude provider authority, then "
                        "retry the same request."
                    ),
                )
        if selectors is not None:
            command = apply_claude_context(command, selectors)
        return command, env


    def _agent_builder_output_schema(self, worker: dict) -> dict[str, object] | None:
        schema = super()._agent_builder_output_schema(worker)
        if not schema:
            return None
        # Claude Code validates an unqualified JSON Schema with its bundled
        # dialect. Supplying the canonical 2020-12 metaschema URI makes the CLI
        # reject the request before execution. Remove only that declaration;
        # every control-envelope constraint remains identical to Codex.
        projected_schema = dict(schema)
        projected_schema.pop("$schema", None)
        return projected_schema


class HostOpenClawRuntime(HostNativeCliMixin, OpenClawWorkstationRuntime):
    worker_root_name = "host_openclaw_runtime"
    binary_env_var = "WPR_OPENCLAW_BIN"
    binary_name = "openclaw"

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_id = info.session_key or self._default_session_key(worker) or f"agent:main:wpr:worker:{worker['worker_id']}"
        model = worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general"))
        state_dir = self._state_dir(worker["worker_id"]) / "openclaw"
        state_dir.mkdir(parents=True, exist_ok=True)
        config_path = state_dir / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "model": {"primary": model},
                            "cliBackends": {
                                "claude-cli": {"command": "claude"},
                                "codex-cli": {"command": "codex"},
                            },
                            "sandbox": {"mode": "off"},
                        }
                    },
                    "session": {"dmScope": "per-channel-peer"},
                    "tools": {
                        "fs": {"workspaceOnly": False},
                        "exec": {"applyPatch": {"workspaceOnly": False}},
                        "elevated": {"enabled": True},
                    },
                    "plugins": {"enabled": True},
                },
                indent=2,
            )
        )
        config_path.chmod(0o600)
        env = reviewed_openclaw_env(self._host_env(worker))
        env["OPENCLAW_STATE_DIR"] = str(state_dir)
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
        env["OPENCLAW_MODEL"] = model
        env["OPENCLAW_SESSION_ID"] = session_id
        instruction_path = (
            Path(str(info.workspace_dir or self._host_workspace_dir(worker)))
            / ".glasshive"
            / "current-instruction.stdin"
        )
        _atomic_write_private_text(
            instruction_path,
            self._instruction_with_completion_contract(instruction),
        )
        return [
            self.binary,
            "agent",
            "--local",
            "--session-id",
            session_id,
            "-m",
            _instruction_file_pointer_message(".glasshive/current-instruction.stdin"),
            "--json",
        ], env
