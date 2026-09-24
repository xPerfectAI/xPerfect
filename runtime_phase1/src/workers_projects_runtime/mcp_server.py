from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlparse

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .workspace_file_mcp import FileBindingKeyParam, FileUploadIdsParam
from .auth import AuthContext, multi_user_security_enabled, normalize_identity_segment, scoped_alias
from .bootstrap import (
    BOOTSTRAP_SOURCE_TOKEN_KEY,
    GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV,
    sign_bootstrap_source_path,
    worker_prompt_layer_producer,
)
from .deliverables import is_user_deliverable_relative_path
from .operator_urls import operator_base_url, surface_aware_watch_url, surface_can_open_operator_url
from .mcp_oauth import McpOAuthConfigurationError, oauth_from_env
from .mcp_internal_assertions import McpInternalAssertionError, signed_runtime_assertion
from .worker_context_mcp import register_owner_configuration_tools
from .peer_mcp import register_owner_peer_tools
from .models import CLOSED_WORKER_STATES
from .release_provenance import release_provenance
from .runtime_requirements import CLAUDE_CODE_EFFORT_LEVELS, host_runtime_requirement_issue
from .runtime_env import load_viventium_runtime_env
from .runtime_identity import derive_legacy_backend_label
from .workspace_continuation import (
    CONNECTED_ACCOUNT_NO_BROKER_NOTE,
    continuation_instruction,
)
from .signed_links import (
    append_signed_query,
    create_signed_link_ref,
    sign_link_token,
    signed_link_ref_url,
    signed_link_ttl_seconds,
)
from .store import canonical_parallel_clean_room_bootstrap
from .upload_projection import (
    intersect_upload_records,
    merge_projected_upload_files,
    project_upload_files as _project_request_upload_files,
    public_upload_ledger,
    trusted_selected_files,
)

import hashlib

from .bootstrap import (
    BOOTSTRAP_SOURCE_TOKEN_KEY,
    GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV,
    sign_bootstrap_source_path,
    worker_prompt_layer_producer,
)

from .models import worker_resource_memory_bytes

from .service_assertions import (
    SERVICE_ASSERTION_HEADER,
    mint_service_assertion,
    verify_service_assertion,
)

from .workspace_continuation import (
    CONNECTED_ACCOUNT_NO_BROKER_NOTE,
    build_workspace_continuation_context,
    continuation_instruction,
)

from .store import canonical_parallel_clean_room_bootstrap

from .upload_projection import (
    intersect_upload_records,
    merge_projected_upload_files,
    project_upload_files as _project_request_upload_files,
    public_upload_ledger,
    trusted_selected_files,
)

try:
    from fastmcp.server.dependencies import get_http_headers
except Exception:  # pragma: no cover - optional dependency path differs by FastMCP package
    get_http_headers = None  # type: ignore[assignment]

load_viventium_runtime_env()

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.environ.get("WPR_MCP_BASE_URL", "http://127.0.0.1:8766").rstrip("/")
DEFAULT_HOST = os.environ.get("WPR_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WPR_MCP_PORT", "8767"))
DEFAULT_TIMEOUT_SEC = float(os.environ.get("WPR_MCP_TIMEOUT_SEC", "120"))
DEFAULT_OWNER_ID = os.environ.get("WPR_DEFAULT_OWNER_ID", "").strip()
DEFAULT_API_TOKEN = os.environ.get("WPR_API_TOKEN", "").strip()
DEFAULT_MCP_API_TOKEN = os.environ.get("GLASSHIVE_MCP_API_KEY", "").strip()


def _callback_delivery_deadline_seconds() -> int:
    """Return the owner-declared upper bound for callback delivery.

    The runtime service dead-letters after a finite total-attempt budget and retries pending
    callbacks on a fixed interval.  Carry that structural horizon to the host so a durable voice
    continuation can fail truthfully when delivery is no longer possible.
    """

    try:
        max_attempts = int(os.environ.get("GLASSHIVE_CALLBACK_MAX_TOTAL_ATTEMPTS", "25"))
    except (TypeError, ValueError):
        max_attempts = 25
    try:
        retry_interval = int(os.environ.get("GLASSHIVE_CALLBACK_RETRY_INTERVAL_S", "30"))
    except (TypeError, ValueError):
        retry_interval = 30
    try:
        retry_attempts = int(os.environ.get("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "3"))
    except (TypeError, ValueError):
        retry_attempts = 3
    try:
        retry_base_delay = float(
            os.environ.get("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0.5")
        )
    except (TypeError, ValueError):
        retry_base_delay = 0.5
    try:
        stale_after = int(
            os.environ.get("GLASSHIVE_CALLBACK_DELIVERING_STALE_AFTER_S", "300")
        )
    except (TypeError, ValueError):
        stale_after = 300
    max_attempts = min(1000, max(1, max_attempts))
    retry_interval = min(3600, max(1, retry_interval))
    retry_attempts = min(25, max(1, retry_attempts))
    retry_base_delay = min(60.0, max(0.0, retry_base_delay))
    stale_after = min(24 * 60 * 60, max(1, stale_after))
    batches = math.ceil(max_attempts / retry_attempts)
    bounded_delivery_work = (
        max_attempts * 5.0
        + batches * retry_base_delay * sum(range(1, retry_attempts))
        + max(0, batches - 1) * retry_interval
        + stale_after
        + 120
    )
    conservative_interval_horizon = max_attempts * retry_interval + 120
    return min(
        45 * 24 * 60 * 60,
        max(60, math.ceil(bounded_delivery_work), conservative_interval_horizon),
    )

READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

HOST_SIDE_ORCHESTRATION_GUIDANCE = (
    "Preserve host-side GlassHive orchestration requirements as context, but do not turn them into "
    "workspace-internal deliverable blockers. Checks such as MCP tool selection, View / Steer link "
    "visibility, chat callback delivery, wait/status polling cadence, and post-run inspection from "
    "the host UI are verified by the host assistant/operator, not by the worker running inside the "
    "workspace."
)

WORKER_HOST_SIDE_ORCHESTRATION_RULE = (
    "- Host-side GlassHive orchestration checks such as MCP tool selection, View / Steer link "
    "visibility, chat callback delivery, wait/status polling cadence, or post-run inspection from "
    "the host UI are host-side responsibilities. Preserve that context, but do not mark the "
    "workspace blocked when the requested files, browser-visible result, research, code, or other "
    "workspace-internal deliverable is complete; report only blockers observable from inside this "
    "worker workspace."
)

CAPABILITY_BROKER_NAME = "glasshive-user-capabilities"
CAPABILITY_BROKER_CONTENT_READ_SCOPE = "content_read"
HIGH_EFFORT_SELECTION_GUIDANCE = (
    "For complex multi-source research, deep research, critical analysis, large file transformation, "
    "coding, comparison, or executive-quality deliverables, choose a higher effort setting: Codex "
    "high/xhigh, Claude max/xhigh, OpenClaw high/max, or the configured equivalent, unless the user clearly "
    "asks for a quick/cheap pass. For ordinary bounded tasks, omit effort unless the user explicitly "
    "asks for a cheaper/faster pass; user preferences and deployment defaults own the baseline."
)


class GlassHiveBlockedError(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("detail") or payload.get("failure_user_message") or "GlassHive blocked the request"))
        self.payload = payload


class GlassHiveApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        clean_detail = str(detail or "GlassHive rejected the request").strip()[:1000]
        super().__init__(clean_detail or "GlassHive rejected the request")
        self.status_code = int(status_code)


def _blocking_wait_max_seconds() -> float:
    return max(0.0, float(os.environ.get("WPR_MCP_BLOCKING_WAIT_MAX_SEC", "45") or "45"))


def _blocking_wait_default_seconds() -> float:
    configured = float(os.environ.get("WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC", "45") or "45")
    return max(0.0, min(configured, _blocking_wait_max_seconds()))


def _blocking_wait_default_poll_interval_seconds() -> float:
    try:
        configured = float(os.environ.get("WPR_MCP_BLOCKING_WAIT_POLL_INTERVAL_SEC", "5") or "5")
    except ValueError:
        configured = 5.0
    return max(1.0, min(configured, 30.0))


def _blocking_wait_sleep_interval_seconds(*, attempts: int, base_interval: float, adaptive: bool) -> float:
    if not adaptive:
        return base_interval
    # Keep early waits responsive, then back off to protect the API/control plane during long runs.
    # With the default 5s base this yields roughly 5s -> 10s -> 20s -> 30s.
    growth_steps = max(0, (max(1, attempts) - 1) // 6)
    multiplier = min(8, 2**growth_steps)
    return min(30.0, max(base_interval, base_interval * multiplier))


def _finite_tool_float(value: float | int | str, *, field_name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be a finite number")
    return parsed


def _safe_request_validation_failure(payload: object) -> dict[str, Any]:
    """Classify an HTTP 422 without copying rejected values or free-form messages."""
    summaries: list[str] = []
    details = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(details, list):
        for item in details[:8]:
            if not isinstance(item, dict):
                continue
            location = item.get("loc")
            field = "request"
            if isinstance(location, (list, tuple)):
                safe_parts = [
                    str(part)
                    for part in location
                    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(part))
                    and str(part) not in {"body", "query", "path"}
                ]
                if safe_parts:
                    field = ".".join(safe_parts[-3:])
            error_type = str(item.get("type") or "invalid")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", error_type):
                error_type = "invalid"
            summary = f"{field}:{error_type}"
            if summary not in summaries:
                summaries.append(summary)
    return {
        "status": "blocked",
        "failure_class": "glasshive_request_validation_failed",
        "failure_retryable": False,
        "failure_user_message": (
            "GlassHive could not accept this mission because its request metadata did not match "
            "the account API contract."
        ),
        "failure_recommended_recovery": (
            "Refresh the Viventium and GlassHive runtime artifacts so both sides use the same "
            "mission request contract."
        ),
        "failure_diagnostic_summary": ",".join(summaries) or "request:invalid",
    }

_CAPACITY_VECTOR_KEYS = (
    "childProcesses",
    "threads",
    "memoryBytes",
    "diskBytes",
)

def _safe_capacity_vector(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    vector: dict[str, int] = {}
    for key in _CAPACITY_VECTOR_KEYS:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            continue
        vector[key] = raw
    return vector

def _safe_capacity_timestamp(value: object) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 64:
        return ""
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return clean if parsed.tzinfo is not None else ""

def _safe_account_api_capacity(detail: dict[str, Any]) -> dict[str, Any] | None:
    capacity_class = str(detail.get("capacityClass") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", capacity_class):
        return None
    capacity: dict[str, Any] = {"class": capacity_class}
    vectors = {
        name: _safe_capacity_vector(detail.get(name))
        for name in ("available", "required", "shortage", "reservation")
    }
    capacity.update({name: value for name, value in vectors.items() if value})
    dimension = str(detail.get("dimension") or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", dimension):
        capacity["dimension"] = dimension
    next_retry_at = _safe_capacity_timestamp(detail.get("nextRetryAt"))
    if next_retry_at:
        capacity["nextRetryAt"] = next_retry_at
    retry_after = detail.get("retryAfter")
    if (
        not isinstance(retry_after, bool)
        and isinstance(retry_after, int)
        and 0 < retry_after <= 86400
    ):
        capacity["retryAfter"] = retry_after

    available = vectors["available"]
    required = vectors["required"]
    reservation = vectors["reservation"]
    complete_vectors = all(
        set(vector) == set(_CAPACITY_VECTOR_KEYS)
        for vector in (available, required, reservation)
    )
    standard_memory = reservation.get("memoryBytes", 0)
    light_memory = worker_resource_memory_bytes("light")
    if capacity_class == "resource_pressure" and complete_vectors and standard_memory > light_memory:
        recommended_required = dict(required)
        recommended_required["memoryBytes"] = max(
            0,
            required["memoryBytes"] - standard_memory + light_memory,
        )
        recommended_reservation = dict(reservation)
        recommended_reservation["memoryBytes"] = light_memory
        light_would_fit = all(
            available[key] >= recommended_required[key]
            for key in _CAPACITY_VECTOR_KEYS
        )
        capacity["lightWouldFit"] = light_would_fit
        if light_would_fit:
            capacity.update(
                {
                    "recommendedResourceClass": "light",
                    "recommendedRequired": recommended_required,
                    "recommendedReservation": recommended_reservation,
                }
            )
    return capacity

def _safe_account_api_failure(payload: object, *, status_code: int) -> dict[str, Any] | None:
    """Preserve a local account-API error code without copying free-form detail."""

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        return None
    code = str(detail.get("code") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,119}", code):
        return None
    retryable = int(status_code) in {408, 425, 429, 500, 502, 503, 504}
    diagnostic = f"http_{int(status_code)}:{code}"
    message = str(detail.get("message") or "").strip()
    bootstrap_prefix = "Automatic Parallel work rejected unsafe bootstrap authority: "
    safe_bootstrap_reasons = {
        "host bootstrap profiles are not allowed": "host_profile",
        "bootstrap bundle is invalid": "bundle_invalid",
        "execution policy is server-owned": "caller_execution_policy",
        "caller provider credentials are not allowed": "caller_provider_credentials",
        "caller bootstrap environment is not allowed": "caller_environment",
        "files must be a workspace-scoped list": "files_not_workspace_list",
        "every file must be workspace-scoped": "file_not_workspace_scoped",
        "home-scoped files are not allowed": "home_scoped_file",
        "workspace file path is invalid": "workspace_path_invalid",
        "workspace provider or credential config files are not allowed": "workspace_authority_file",
        "capability broker metadata is invalid": "broker_metadata_invalid",
        "caller broker credentials are not allowed": "caller_broker_credentials",
        "caller MCP config is not allowed": "caller_mcp_config",
        "caller Claude MCP config is not allowed": "caller_claude_mcp_config",
        "caller Codex MCP config is not allowed": "caller_codex_mcp_config",
    }
    structured_reason = str(detail.get("reason") or "").strip()
    if structured_reason in set(safe_bootstrap_reasons.values()):
        diagnostic = f"{diagnostic}:{structured_reason}"
    elif message.startswith(bootstrap_prefix) and message.endswith("."):
        reason = message[len(bootstrap_prefix) : -1]
        reason_code = safe_bootstrap_reasons.get(reason)
        if reason_code:
            diagnostic = f"{diagnostic}:{reason_code}"
    elif message == "Automatic Parallel work requires an isolated Docker/workstation runtime.":
        diagnostic = f"{diagnostic}:runtime_not_ready"
    elif message == "Existing host-native mission work blocks isolated Parallel admission.":
        diagnostic = f"{diagnostic}:host_missions_active"
    failure: dict[str, Any] = {
        "status": "blocked",
        "failure_class": code,
        "failure_retryable": retryable,
        "failure_user_message": "GlassHive could not accept this mission at the account boundary.",
        "failure_recommended_recovery": (
            "Retry after the reported dependency recovers."
            if retryable
            else "Refresh the Viventium and GlassHive runtime artifacts before launching again."
        ),
        "failure_diagnostic_summary": diagnostic,
    }
    if code == "host_capacity":
        capacity = _safe_account_api_capacity(detail)
        if capacity is not None:
            failure["capacity"] = capacity
            if capacity.get("recommendedResourceClass") == "light":
                failure["failure_recommended_recovery"] = (
                    "Retry this same bounded mission once with resource_class='light'. "
                    "Otherwise wait for the reported capacity dependency to recover."
                )
                failure["main_agent_next_action"] = (
                    "Retry this same bounded mission once with resource_class='light'. "
                    "Do not use light for an unbounded or memory-intensive mission."
                )
    return failure

def _configured_default_worker_profile() -> str:
    raw_configured = os.environ.get("GLASSHIVE_DEFAULT_WORKER_PROFILE", "").strip()
    configured = raw_configured or "codex-cli"
    raw_allowed = (
        os.environ.get("GLASSHIVE_ALLOWED_WORKER_PROFILES", "").strip()
        or os.environ.get("WPR_ALLOWED_WORKER_PROFILES", "").strip()
    )
    allowed = [item.strip() for item in raw_allowed.split(",") if item.strip()]
    if not allowed or configured in allowed:
        return configured
    if raw_configured:
        raise RuntimeError(
            "GLASSHIVE_DEFAULT_WORKER_PROFILE must be included in GLASSHIVE_ALLOWED_WORKER_PROFILES"
        )
    return "codex-cli" if "codex-cli" in allowed else sorted(allowed)[0]


def _host_profile_binary(profile: str) -> str:
    clean = str(profile or "").strip()
    if clean == "codex-cli":
        return os.environ.get("WPR_CODEX_BIN", "").strip() or "codex"
    if clean == "claude-code":
        return os.environ.get("WPR_CLAUDE_CODE_BIN", "").strip() or "claude"
    if clean == "grok-build":
        return os.environ.get("WPR_GROK_BIN", "").strip() or "grok"
    return os.environ.get("WPR_OPENCLAW_BIN", "").strip() or "openclaw"


def _host_profile_available(profile: str) -> bool:
    # Status rendering is non-admitting and therefore must not execute a CLI.
    # Exact version/help/auth checks run only behind the service's durable
    # preflight-capacity reservation.
    return shutil.which(_host_profile_binary(profile)) is not None


def _runtime_dependency_blocked_payload(*, profile: str, execution_mode: str) -> dict[str, Any] | None:
    if execution_mode != "host":
        return None
    if not _host_workers_enabled():
        profile_hint = f" for `{profile}`" if profile else ""
        return {
            "status": "blocked",
            "failure_class": "runtime_dependency_missing",
            "failure_retryable": False,
            "failure_user_message": (
                f"GlassHive cannot start the selected host worker{profile_hint} because "
                "host-native workers are disabled in this deployment."
            ),
            "failure_recommended_recovery": (
                "Use sandbox/workstation execution when that still satisfies the user's request. "
                "Ask the operator to enable host-native workers only when real local computer/session "
                "access is required."
            ),
            "failure_diagnostic_summary": f"Host-native workers disabled for profile={profile} execution_mode={execution_mode}",
            "profile": profile,
            "execution_mode": execution_mode,
        }
    binary = _host_profile_binary(profile)
    if shutil.which(binary) is None:
        label = binary.replace("\\", "/").rstrip("/").split("/")[-1] or binary
        profile_hint = f" for `{profile}`" if profile else ""
        return {
            "status": "blocked",
            "failure_class": "runtime_dependency_missing",
            "failure_retryable": False,
            "failure_user_message": (
                f"GlassHive cannot start the selected host worker{profile_hint} because the required "
                f"CLI `{label}` is not installed or not available to the GlassHive service."
            ),
            "failure_recommended_recovery": (
                "Use a configured managed dependency, choose another available worker profile, or use "
                "sandbox/workstation execution when that still satisfies the user's request. Ask the "
                "operator to change the host service runtime only when no configured recovery path is available."
            ),
            "failure_diagnostic_summary": f"Missing host CLI for profile={profile} execution_mode={execution_mode}",
            "profile": profile,
            "execution_mode": execution_mode,
        }
    return None


def _worker_capability_summary() -> str:
    allowed = _allowed_worker_profiles() or {"codex-cli", "claude-code", "grok-build", "openclaw-general"}
    default_profile = _configured_default_worker_profile()
    default_mode = _default_execution_mode()
    parts = [
        f"Configured default execution_mode is `{default_mode}`.",
        f"Configured default worker profile is `{default_profile}`.",
    ]
    if _host_workers_enabled():
        states = []
        for profile in ("codex-cli", "claude-code", "grok-build", "openclaw-general"):
            if profile not in allowed:
                continue
            states.append(f"{profile} host={'available' if _host_profile_available(profile) else 'unavailable'}")
        if states:
            parts.append("Host profile availability: " + ", ".join(states) + ".")
    else:
        parts.append("Host-native workers are disabled; use docker/workstation execution.")
    return " ".join(parts)


HEADER_USER_ID = "x-viventium-user-id"
HEADER_STORAGE_USER_ID = "x-viventium-storage-user-id"
HEADER_TENANT_ID = "x-viventium-tenant-id"
HEADER_USER_EMAIL = "x-viventium-user-email"
HEADER_USER_ROLE = "x-viventium-user-role"
HEADER_AGENT_ID = "x-viventium-agent-id"
HEADER_CONVERSATION_ID = "x-viventium-conversation-id"
HEADER_PARENT_MESSAGE_ID = "x-viventium-parent-message-id"
HEADER_MESSAGE_ID = "x-viventium-message-id"
HEADER_SURFACE = "x-viventium-surface"
HEADER_INPUT_MODE = "x-viventium-input-mode"
HEADER_STREAM_ID = "x-viventium-stream-id"
HEADER_VOICE_CALL_SESSION_ID = "x-viventium-voice-call-session-id"
HEADER_VOICE_REQUEST_ID = "x-viventium-voice-request-id"
HEADER_TELEGRAM_CHAT_ID = "x-viventium-telegram-chat-id"
HEADER_TELEGRAM_USER_ID = "x-viventium-telegram-user-id"
HEADER_TELEGRAM_MESSAGE_ID = "x-viventium-telegram-message-id"
HEADER_LOGICAL_TURN_ID = "x-viventium-logical-turn-id"

HEADER_LOGICAL_TURN_REVISION = "x-viventium-logical-turn-revision"

HEADER_REQUEST_FILES = "x-viventium-request-files"
HEADER_REQUEST_ATTACHMENTS = "x-viventium-request-attachments"
HEADER_TOOL_RESOURCES = "x-viventium-tool-resources"
HEADER_FILE_IDS = "x-viventium-file-ids"
HEADER_SERVICE_TOKEN = "x-wpr-token"
HEADER_ALIASES = {
    HEADER_USER_ID: ("x-glasshive-user-id", "x-librechat-user-id"),
    HEADER_STORAGE_USER_ID: ("x-glasshive-storage-user-id", "x-librechat-storage-user-id"),
    HEADER_TENANT_ID: ("x-glasshive-tenant-id", "x-librechat-tenant-id"),
    HEADER_USER_EMAIL: ("x-glasshive-user-email", "x-librechat-user-email"),
    HEADER_USER_ROLE: ("x-glasshive-user-role", "x-librechat-user-role"),
    HEADER_AGENT_ID: ("x-glasshive-agent-id", "x-librechat-agent-id"),
    HEADER_CONVERSATION_ID: ("x-glasshive-conversation-id", "x-librechat-conversation-id"),
    HEADER_PARENT_MESSAGE_ID: ("x-glasshive-parent-message-id", "x-librechat-parent-message-id"),
    HEADER_MESSAGE_ID: ("x-glasshive-message-id", "x-librechat-message-id"),
    HEADER_SURFACE: ("x-glasshive-surface", "x-librechat-surface"),
    HEADER_INPUT_MODE: ("x-glasshive-input-mode", "x-librechat-input-mode"),
    HEADER_STREAM_ID: ("x-glasshive-stream-id", "x-librechat-stream-id"),
    HEADER_VOICE_CALL_SESSION_ID: ("x-glasshive-voice-call-session-id", "x-librechat-voice-call-session-id"),
    HEADER_VOICE_REQUEST_ID: ("x-glasshive-voice-request-id", "x-librechat-voice-request-id"),
    HEADER_TELEGRAM_CHAT_ID: ("x-glasshive-telegram-chat-id",),
    HEADER_TELEGRAM_USER_ID: ("x-glasshive-telegram-user-id",),
    HEADER_TELEGRAM_MESSAGE_ID: ("x-glasshive-telegram-message-id",),
    HEADER_LOGICAL_TURN_ID: ("x-glasshive-logical-turn-id", "x-librechat-logical-turn-id"),
    HEADER_LOGICAL_TURN_REVISION: (
        "x-glasshive-logical-turn-revision",
        "x-librechat-logical-turn-revision",
    ),
    HEADER_REQUEST_FILES: ("x-glasshive-request-files", "x-librechat-request-files"),
    HEADER_REQUEST_ATTACHMENTS: ("x-glasshive-request-attachments", "x-librechat-request-attachments"),
    HEADER_TOOL_RESOURCES: ("x-glasshive-tool-resources", "x-librechat-tool-resources"),
    HEADER_FILE_IDS: ("x-glasshive-file-ids", "x-librechat-file-ids"),
    HEADER_SERVICE_TOKEN: ("x-glasshive-service-token", "x-glasshive-mcp-service-token"),
}
CALLBACK_REQUIRED_CONTEXT_KEYS = ("user_id", "conversation_id", "parent_message_id", "message_id")

ExecutionModeParam = Annotated[
    Literal["docker", "host"] | None,
    Field(
        description=(
            "Execution surface. Omit to use the configured default. Use 'host' only when GlassHive "
            "instructions say host-native workers are enabled and the task depends on the user's real "
            "computer/session: local browser profile, desktop apps, local files/projects, installed "
            "CLIs, or OS tools. Use 'docker' for isolated sandbox/disposable/risky work."
        )
    ),
]
ProfileParam = Annotated[
    str,
    Field(
        description=(
            "Worker CLI profile. Prefer 'codex-cli' for host browser/desktop/file/code execution when "
            "Codex is installed, 'claude-code' when Claude is explicitly requested, and "
            "'openclaw-general' only when OpenClaw is installed or explicitly requested."
        )
    ),
]
BackendParam = Annotated[
    str | None,
    Field(
        description=(
            "Legacy compatibility field. Prefer omitting it; GlassHive selects and reports workers "
            "from profile plus execution_mode. Only older clients should pass a backend value."
        )
    ),
]
DesktopActionParam = Annotated[
    Literal["terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"],
    Field(description="Worker surface/action to open or focus for takeover/visibility."),
]
BootstrapBundleParam = Annotated[
    str | dict[str, Any] | None,
    Field(
        description=(
            "Optional bootstrap bundle as either a JSON string or structured object. Use it to seed "
            "project_definition, prompt files, MCP config, env, and workspace files. For convenience, "
            "files may be either a list of file entries or an object mapping relative paths to content, "
            "for example {'project-definition.md': '# Task'}."
        )
    ),
]
UploadedFilesParam = Annotated[
    list[dict[str, Any]] | dict[str, Any] | None,
    Field(
        description=(
            "Optional attached/uploaded files to materialize in the workspace when the chat host does "
            "not project upload metadata through MCP headers. Use this only with file content or file "
            "references already visible in the current user request/model context. Prefer entries like "
            "[{'filename': 'brief.txt', 'text': '...'}]. If only a file name/id/reference is visible, "
            "pass that metadata so GlassHive can create an upload manifest and the worker can report a "
            "real blocker instead of pretending the file was read."
        )
    ),
]


def _default_execution_mode() -> str:
    if not _host_workers_enabled():
        return "docker"
    mode = (
        os.environ.get("GLASSHIVE_DEFAULT_EXECUTION_MODE", "").strip().lower()
        or os.environ.get("WPR_DEFAULT_EXECUTION_MODE", "docker").strip().lower()
    )
    return mode if mode in {"docker", "host"} else "docker"


def _allowed_worker_profiles() -> set[str]:
    raw = (
        os.environ.get("GLASSHIVE_ALLOWED_WORKER_PROFILES", "").strip()
        or os.environ.get("WPR_ALLOWED_WORKER_PROFILES", "").strip()
    )
    return {item.strip() for item in raw.split(",") if item.strip()}


def _profile_allowed(profile: str) -> bool:
    allowed = _allowed_worker_profiles()
    return bool(profile and (not allowed or profile in allowed))


def _normalize_preferences(raw: dict[str, Any] | None) -> dict[str, str]:
    data = raw or {}
    return {
        "default_worker_profile": str(data.get("default_worker_profile") or "").strip(),
        "codex_reasoning_effort": str(data.get("codex_reasoning_effort") or "").strip().lower(),
        "claude_effort": str(data.get("claude_effort") or "").strip().lower(),
        "openclaw_effort": str(data.get("openclaw_effort") or "").strip().lower(),
    }


def _resolve_profile_from_preferences(profile: str | None, preferences: dict[str, str] | None) -> str:
    requested = str(profile or "").strip()
    if requested:
        return requested
    preferred = str((preferences or {}).get("default_worker_profile") or "").strip()
    if _profile_allowed(preferred):
        return preferred
    return _configured_default_worker_profile()


def _resolve_effort_for_profile(
    profile: str,
    effort: str | None,
    preferences: dict[str, str] | None,
) -> str:
    requested = str(effort or "").strip().lower()
    if requested:
        return requested
    prefs = preferences or {}
    if profile == "codex-cli":
        return prefs.get("codex_reasoning_effort", "")
    if profile == "claude-code":
        return prefs.get("claude_effort", "")
    if profile == "openclaw-general":
        return prefs.get("openclaw_effort", "")
    return ""


def _apply_effort_to_bundle(bundle: dict[str, Any], *, profile: str, effort: str) -> dict[str, Any]:
    clean_effort = str(effort or "").strip().lower()
    if not clean_effort:
        return bundle
    next_bundle = dict(bundle or {})
    if profile == "codex-cli":
        if clean_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("Codex effort must be none, minimal, low, medium, high, or xhigh")
        env = dict(next_bundle.get("env") or {})
        env["WPR_CODEX_CLI_REASONING_EFFORT"] = clean_effort
        next_bundle["env"] = env
        return next_bundle
    if profile == "claude-code":
        if clean_effort not in CLAUDE_CODE_EFFORT_LEVELS:
            raise ValueError("Claude effort must be default, low, medium, high, xhigh, or max")
        if clean_effort == "default":
            return next_bundle
        env = dict(next_bundle.get("env") or {})
        env["WPR_CLAUDE_CODE_EFFORT"] = clean_effort
        next_bundle["env"] = env
        return next_bundle
    elif profile == "openclaw-general":
        if clean_effort not in {"default", "high", "max"}:
            raise ValueError("OpenClaw effort must be default, high, or max")
    else:
        return next_bundle
    if clean_effort == "default":
        return next_bundle
    current = str(next_bundle.get("system_instructions") or "").strip()
    addition = f"Worker effort preference for this run: {clean_effort}."
    next_bundle["system_instructions"] = f"{current}\n\n{addition}".strip()
    return next_bundle


def _host_workers_enabled() -> bool:
    value = os.environ.get("GLASSHIVE_HOST_WORKERS_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _enterprise_mode_enabled() -> bool:
    return multi_user_security_enabled()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return deduped


def _host_for_header(hostname: str) -> str:
    host = str(hostname or "").strip().lower()
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _allowed_host_values_from_setting(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if "://" in text:
        parsed = urlparse(text)
        if not parsed.hostname:
            return []
        host = _host_for_header(parsed.hostname)
        if parsed.port:
            return [f"{host}:{parsed.port}", f"{host}:*"]
        return [host, f"{host}:*"]
    if text.endswith(":*") or ":" in text.rsplit("@", 1)[-1]:
        host, _, port = text.rpartition(":")
        if not host or (":" in host and not host.startswith("[")):
            return [text.lower()]
        # Clients send a lowercase host and omit the default HTTPS port.
        host = host.lower().rstrip(".")
        return [f"{host}:{port}", host] if port == "443" else [f"{host}:{port}"]
    text = text.lower().rstrip(".")
    return [text, f"{text}:*"]


def _allowed_origin_values_from_url(value: str) -> list[str]:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return []
    host = _host_for_header(parsed.hostname)
    origin = f"{parsed.scheme}://{host}"
    if parsed.port:
        return [f"{origin}:{parsed.port}", f"{origin}:*"]
    return [origin, f"{origin}:*"]


def _csv_env_values(*names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        raw = os.environ.get(name, "")
        values.extend(part.strip() for part in raw.split(",") if part.strip())
    return values


def _mcp_transport_security_settings(host: str, port: int) -> TransportSecuritySettings:
    allowed_hosts = [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "testserver",
    ]
    allowed_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]

    listen_host = _host_for_header(host)
    if listen_host and listen_host not in {"0.0.0.0", "::", "[::]"}:
        allowed_hosts.extend([f"{listen_host}:{port}", f"{listen_host}:*"])

    for configured in _csv_env_values("WPR_MCP_ALLOWED_HOSTS", "GLASSHIVE_MCP_ALLOWED_HOSTS"):
        allowed_hosts.extend(_allowed_host_values_from_setting(configured))

    for configured_url in _csv_env_values("GLASSHIVE_MCP_URL", "WPR_MCP_PUBLIC_URL"):
        allowed_hosts.extend(_allowed_host_values_from_setting(configured_url))
        allowed_origins.extend(_allowed_origin_values_from_url(configured_url))

    for configured_url in _csv_env_values("LIBRECHAT_ENDPOINT", "VIVENTIUM_FRONTEND_URL", "GLASSHIVE_OPERATOR_BASE_URL"):
        allowed_origins.extend(_allowed_origin_values_from_url(configured_url))

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_dedupe(allowed_hosts),
        allowed_origins=_dedupe(allowed_origins),
    )


def _host_worker_mentions() -> tuple[str, str, str]:
    return (
        os.environ.get("WPR_HOST_MENTION_CODEX", "@codex").strip() or "@codex",
        os.environ.get("WPR_HOST_MENTION_CLAUDE", "@claude").strip() or "@claude",
        os.environ.get("WPR_HOST_MENTION_OPENCLAW", "@openclaw").strip() or "@openclaw",
    )

def _worker_execution_instruction() -> str:
    if _host_workers_enabled():
        if _default_execution_mode() == "host":
            return (
                "Default to host-native execution for the user's real Chrome/browser profile, "
                "desktop apps, OS tools, host files, local projects, and installed CLIs."
            )
        return (
            f"When execution_mode is omitted, MCP worker tools use the configured default "
            f"'{_default_execution_mode()}'. Set execution_mode='host' when the task depends "
            "on the user's real computer/session: logged-in browser profile, desktop apps, "
            "local files/projects, installed CLIs, or OS/window control."
        )
    return (
        "Host-native workers are disabled by GlassHive config; configured default 'docker'; "
        "do not request execution_mode='host'."
    )


def _worker_surface_summary() -> str:
    if _host_workers_enabled():
        return (
            "persistent projects, resumable workers, host-native workers for browser and desktop action, "
            "local files/projects, installed CLIs, workstation sandboxes, and live operator takeover"
        )
    return (
        "persistent projects, resumable workers, Docker/workstation sandboxes, generated artifacts, "
        "sandboxed browser/desktop action, and live operator takeover"
    )

def _worker_surface_routing_guidance() -> str:
    if _host_workers_enabled():
        return (
            "Use it when the user asks the host assistant to act in a real browser, desktop app, local file, "
            "local project, installed tool, or current computer session; the user does not need to say "
            "GlassHive, Codex, Computer Use, or local machine. Do not answer from memory or inference when "
            "real browser/desktop/local state must be inspected or changed. "
        )
    return (
        "Use it for advanced long-running workspace, file, code, research, artifact, sandboxed browser, "
        "or sandboxed desktop work; host-native access to the user's real computer/session is disabled in "
        "this deployment, so do not imply real local-browser, desktop-app, local-file, installed-CLI, or OS "
        "control unless the host explicitly exposes a separate capability. "
    )

@worker_prompt_layer_producer("mcp_server_instructions")
def glasshive_workers_server_instructions() -> str:
    return (
        "Use the one GlassHive tool whose action matches the user's request. Make one call when one "
        "call can complete it; never enumerate or summarize the tool catalog unless the user asks. "
        "For a fresh delegated task, use workspace_launch. When the user gives an exact saved workspace "
        "name, launch it directly without listing first; workspace_launch resolves the human name. "
        "Check or wait only when the user asks. "
        "Preserve the user's goal, constraints, files, and context without inventing plans, success "
        "criteria, tool results, or extra workflow. Use the exact callable tool id shown by the host. "
        f"{_worker_capability_summary()} {_worker_execution_instruction()}"
    )


def _resolve_execution_mode(value: str | None, *, allow_disabled_host: bool = False) -> str:
    mode = str(value or "").strip().lower()
    if not mode:
        mode = _default_execution_mode()
    if mode not in {"docker", "host"}:
        raise ValueError("execution_mode must be either 'docker' or 'host'")
    if mode == "host" and not allow_disabled_host and not _host_workers_enabled():
        raise ValueError("host-native GlassHive workers are disabled by GlassHive config")
    return mode


def _normalize_headers(raw_headers: object) -> dict[str, str]:
    if raw_headers is None:
        return {}
    if hasattr(raw_headers, "items"):
        items = raw_headers.items()
    elif isinstance(raw_headers, list):
        items = raw_headers
    else:
        return {}
    return {str(key).lower(): str(value) for key, value in items}


def _header_value(headers: dict[str, str], primary: str) -> str:
    for name in (primary, *HEADER_ALIASES.get(primary, ())):
        value = _sanitize_context_value(headers.get(name))
        if value:
            return value
    return ""


def _header_was_supplied(headers: dict[str, str], primary: str) -> bool:
    """Return whether a trusted identity header was present, even when its value is invalid."""

    return any(name in headers for name in (primary, *HEADER_ALIASES.get(primary, ())))

def _strict_event_identity_value(
    raw_value: object,
    *,
    label: str,
    max_bytes: int = 512,
) -> str:
    """Normalize one durable event identifier and reject ambiguous or unsafe forms."""

    if not isinstance(raw_value, str):
        raise ValueError(f"The trusted {label} source identity is invalid")
    value = _sanitize_context_value(raw_value)
    if (
        not value
        or len(value.encode("utf-8")) > max_bytes
        or any(ord(char) < 32 or ord(char) == 127 for char in raw_value)
    ):
        raise ValueError(f"The trusted {label} source identity is invalid")
    return value

def _trusted_identity_header(
    headers: dict[str, str],
    primary: str,
    *,
    label: str,
    max_bytes: int = 512,
) -> tuple[bool, str]:
    """Read one event-identity header without alias ambiguity or unsafe bytes."""

    names = (primary, *HEADER_ALIASES.get(primary, ()))
    present = [(name, str(headers.get(name) or "")) for name in names if name in headers]
    if not present:
        return False, ""
    values: list[str] = []
    for _name, raw_value in present:
        values.append(
            _strict_event_identity_value(
                raw_value,
                label=label,
                max_bytes=max_bytes,
            )
        )
    if any(value != values[0] for value in values[1:]):
        raise ValueError(f"The trusted {label} source identity is invalid")
    return True, values[0]

def _packaged_local_owner(owner_id: str | None = None, *, headers: dict | None = None) -> str | None:
    if os.environ.get("XPERFECT_EXECUTION_PROFILE") != "local-linux":
        return None
    configured = str(os.environ.get("WPR_DEFAULT_OWNER_ID") or "").strip()
    if not configured or os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID") != configured:
        raise PermissionError("Packaged MCP owner is not configured")
    supplied = str(owner_id or "").strip()
    if supplied and supplied != configured:
        raise PermissionError("Packaged MCP cannot change its local owner")
    for key, expected in ((HEADER_USER_ID, configured), (HEADER_STORAGE_USER_ID, configured),
                          (HEADER_TENANT_ID, "local"), (HEADER_USER_ROLE, "tenant_admin")):
        values = [str((headers or {}).get(name) or "").strip() for name in (key, *HEADER_ALIASES.get(key, ()))]
        if any(value and value != expected for value in values):
            raise PermissionError("Packaged MCP identity does not match its local owner")
    return configured


def _request_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if get_http_headers is not None:
        try:
            headers = _normalize_headers(get_http_headers())
        except Exception:
            headers = {}
    access_token = get_access_token()
    if access_token is not None:
        claims = access_token.claims or {}
        # Raw request identity is untrusted once OAuth is present. Remove every
        # primary and compatibility alias before projecting the verified principal.
        for primary in (
            HEADER_USER_ID,
            HEADER_STORAGE_USER_ID,
            HEADER_TENANT_ID,
            HEADER_USER_EMAIL,
            HEADER_USER_ROLE,
        ):
            headers.pop(primary, None)
            for alias in HEADER_ALIASES.get(primary, ()):
                headers.pop(alias, None)
        headers[HEADER_USER_ID] = str(access_token.subject or "").strip()
        headers[HEADER_STORAGE_USER_ID] = str(access_token.subject or "").strip()
        headers[HEADER_TENANT_ID] = str(claims.get("tenant_id") or "").strip()
        headers[HEADER_USER_EMAIL] = str(claims.get("email") or "").strip()
        headers[HEADER_USER_ROLE] = str(claims.get("role") or "viewer").strip()
    fixed_owner = _packaged_local_owner(headers=headers)
    if fixed_owner:
        for primary in (HEADER_USER_ID, HEADER_STORAGE_USER_ID, HEADER_TENANT_ID,
                        HEADER_USER_EMAIL, HEADER_USER_ROLE):
            headers.pop(primary, None)
            for alias in HEADER_ALIASES.get(primary, ()):
                headers.pop(alias, None)
        headers[HEADER_USER_ID] = fixed_owner
        headers[HEADER_STORAGE_USER_ID] = fixed_owner
        headers[HEADER_TENANT_ID] = "local"
        headers[HEADER_USER_ROLE] = "tenant_admin"
    return {key: value for key, value in headers.items() if value}


def _trusted_request_upload_scope() -> tuple[str | None, str | None, str | None]:
    headers = _request_headers()
    return (
        _header_value(headers, HEADER_TENANT_ID) or None,
        _header_value(headers, HEADER_USER_ID) or None,
        _header_value(headers, HEADER_STORAGE_USER_ID) or None,
    )



def _request_owner_id(owner_id: str | None) -> str | None:
    headers = _request_headers()
    fixed_owner = _packaged_local_owner(owner_id, headers=headers)
    if fixed_owner:
        return fixed_owner
    asserted_owner = _header_value(headers, HEADER_USER_ID)
    if _enterprise_mode_enabled():
        _require_enterprise_mcp_service_auth(headers)
        _require_enterprise_mcp_identity_assertion(headers)
        return asserted_owner or DEFAULT_OWNER_ID or None
    # A trusted transport identity always wins over model/tool arguments in
    # local mode too. The caller cannot forge a sibling owner just because the
    # deployment is personal rather than enterprise.
    if asserted_owner:
        return asserted_owner
    explicit = _sanitize_context_value(owner_id)
    if explicit:
        return explicit
    return None


def _account_request_scope(owner_id: str | None = None) -> tuple[str, str]:
    fixed_owner = _packaged_local_owner(owner_id, headers=_request_headers())
    if fixed_owner:
        return "local", fixed_owner
    if _enterprise_mode_enabled():
        return _enterprise_request_scope()
    headers = _request_headers()
    tenant_id = _header_value(headers, HEADER_TENANT_ID) or "local"
    resolved_owner = (
        _header_value(headers, HEADER_USER_ID)
        or _sanitize_context_value(owner_id)
        or DEFAULT_OWNER_ID
        or "demo-owner"
    )
    return tenant_id, resolved_owner


def _delegation_launch_payload_digest(payload: dict[str, Any]) -> str:
    """Fingerprint the normalized launch controls that Core actually authorized.

    Bootstrap security metadata is verified separately and clean-room bundle contents are validated
    by the account API. This digest binds the user objective and every scalar launch control while
    avoiding JSON-number canonicalization differences between JavaScript and Python.
    """

    canonical = json.dumps(
        {
            "alias": str(payload.get("alias") or "").strip(),
            "backend": str(payload.get("backend") or "").strip(),
            "bootstrap_profile": str(
                payload.get("bootstrap_profile") or payload.get("bootstrapProfile") or ""
            ).strip(),
            "connected_account_content_intent": bool(
                payload.get("connected_account_content_intent", False)
            ),
            "effort": str(payload.get("effort") or "").strip(),
            "execution_mode": str(
                payload.get("execution_mode") or payload.get("executionMode") or ""
            ).strip(),
            "expose_diagnostics": bool(payload.get("expose_diagnostics", False)),
            "goal": str(payload.get("goal") or "").strip(),
            "instruction": str(payload.get("instruction") or "").strip(),
            "owner_id": str(payload.get("owner_id") or "").strip(),
            "profile": str(payload.get("profile") or "").strip(),
            "project_id": str(payload.get("project_id") or "").strip(),
            "require_callback": bool(payload.get("require_callback", False)),
            "reuse_existing_workspace": bool(
                payload.get("reuse_existing_workspace", False)
            ),
            "title": str(payload.get("title") or "").strip(),
            "worker_name": str(
                payload.get("worker_name") or payload.get("workerName") or ""
            ).strip(),
            "worker_role": str(
                payload.get("worker_role") or payload.get("workerRole") or ""
            ).strip(),
            "workspace_root": str(
                payload.get("workspace_root") or payload.get("workspaceRoot") or ""
            ).strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _trusted_operation_idempotency_key(
    prefix: str,
    *,
    tenant_id: str,
    owner_id: str,
    payload: dict[str, Any],
    signed_launch_payload: dict[str, Any] | None = None,
    mcp_request_id: str | None = None,
) -> str:
    bootstrap_bundle = payload.get("bootstrapBundle")
    if isinstance(bootstrap_bundle, dict):
        identity = bootstrap_bundle.get("viventium_delegation_identity")
        assertion_value = bootstrap_bundle.get("viventium_delegation_assertion")
        if (identity is None) != (assertion_value is None):
            raise ValueError("viventium delegation identity and assertion must be supplied together")
        if identity is not None:
            if not isinstance(identity, dict) or set(identity) != {
                "version",
                "idempotency_key",
                "goal_digest",
                "launch_payload_digest",
                "call_identity_digest",
                "source_event_id",
                "objective_ordinal",
            }:
                raise ValueError("viventium_delegation_identity is invalid")
            version = identity.get("version")
            objective_ordinal = identity.get("objective_ordinal")
            idempotency_key = str(identity.get("idempotency_key") or "")
            goal_digest = str(identity.get("goal_digest") or "")
            launch_payload_digest = str(identity.get("launch_payload_digest") or "")
            call_identity_digest = str(identity.get("call_identity_digest") or "")
            try:
                source_event_id = _strict_event_identity_value(
                    identity.get("source_event_id"),
                    label="delegation",
                )
            except ValueError as exc:
                raise ValueError("viventium_delegation_identity is invalid") from exc
            if (
                isinstance(version, bool)
                or version != 2
                or not re.fullmatch(r"[0-9a-f]{64}", idempotency_key)
                or not re.fullmatch(r"[0-9a-f]{64}", goal_digest)
                or not re.fullmatch(r"[0-9a-f]{64}", launch_payload_digest)
                or not re.fullmatch(r"[0-9a-f]{64}", call_identity_digest)
                or isinstance(objective_ordinal, bool)
                or not isinstance(objective_ordinal, int)
                or objective_ordinal < 0
                or objective_ordinal > 1_000_000
            ):
                raise ValueError("viventium_delegation_identity is invalid")
            expected_idempotency_key = hashlib.sha256(
                (
                    f"{tenant_id}\0{owner_id}\0{source_event_id}\0"
                    f"call:{call_identity_digest}\0{goal_digest}"
                ).encode("utf-8")
            ).hexdigest()
            if not hmac.compare_digest(idempotency_key, expected_idempotency_key):
                raise ValueError("viventium delegation identity scope is invalid")
            expected_launch_payload_digest = _delegation_launch_payload_digest(
                signed_launch_payload if signed_launch_payload is not None else payload
            )
            if not hmac.compare_digest(
                launch_payload_digest, expected_launch_payload_digest
            ):
                raise ValueError("viventium delegation launch payload is invalid")
            assertion = str(assertion_value or "").strip().lower()
            load_viventium_runtime_env({"VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET"})
            assertion_secret = str(
                os.environ.get("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET") or ""
            ).strip()
            canonical_assertion = json.dumps(
                {
                    "identity": {
                        "call_identity_digest": call_identity_digest,
                        "goal_digest": goal_digest,
                        "idempotency_key": idempotency_key,
                        "launch_payload_digest": launch_payload_digest,
                        "objective_ordinal": objective_ordinal,
                        "source_event_id": source_event_id,
                        "version": version,
                    },
                    "owner_id": owner_id,
                    "tenant_id": tenant_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            expected_assertion = hmac.new(
                assertion_secret.encode("utf-8"),
                b"viventium.delegation-identity.v2\0" + canonical_assertion,
                hashlib.sha256,
            ).hexdigest()
            if (
                not assertion_secret
                or assertion_secret.startswith("${")
                or not re.fullmatch(r"[0-9a-f]{64}", assertion)
                or not hmac.compare_digest(assertion, expected_assertion)
            ):
                raise ValueError("viventium delegation identity assertion is invalid")
            return idempotency_key
    headers = _request_headers()
    logical_id_supplied, logical_turn_id = _trusted_identity_header(
        headers, HEADER_LOGICAL_TURN_ID, label="logical-turn"
    )
    logical_revision_supplied, logical_turn_revision = _trusted_identity_header(
        headers,
        HEADER_LOGICAL_TURN_REVISION,
        label="logical-turn",
        max_bytes=10,
    )
    message_supplied, message_id = _trusted_identity_header(
        headers, HEADER_MESSAGE_ID, label="message"
    )
    telegram_message_supplied, telegram_message_id = _trusted_identity_header(
        headers, HEADER_TELEGRAM_MESSAGE_ID, label="Telegram"
    )
    telegram_chat_supplied, telegram_chat_id = _trusted_identity_header(
        headers, HEADER_TELEGRAM_CHAT_ID, label="Telegram"
    )
    voice_request_supplied, voice_request_id = _trusted_identity_header(
        headers, HEADER_VOICE_REQUEST_ID, label="Voice"
    )
    voice_session_supplied, voice_call_session_id = _trusted_identity_header(
        headers, HEADER_VOICE_CALL_SESSION_ID, label="Voice"
    )
    stream_supplied, stream_id = _trusted_identity_header(
        headers, HEADER_STREAM_ID, label="stream"
    )
    logical_turn_supplied = logical_id_supplied or logical_revision_supplied
    telegram_supplied = telegram_message_supplied or telegram_chat_supplied
    voice_supplied = voice_request_supplied or voice_session_supplied

    if logical_turn_supplied:
        if not logical_id_supplied or not logical_revision_supplied or not re.fullmatch(
            r"[1-9][0-9]{0,9}", logical_turn_revision
        ):
            raise ValueError("The trusted logical-turn source identity is invalid")
        source: dict[str, Any] = {
            "kind": "logical_turn",
            "logical_turn_id": logical_turn_id,
            "revision": int(logical_turn_revision),
        }
    elif message_supplied:
        if not message_id:
            raise ValueError("The trusted message source identity is invalid")
        source = {"kind": "message", "message_id": message_id}
    elif telegram_supplied:
        if not telegram_message_id or not telegram_chat_id:
            raise ValueError("The trusted Telegram source identity is invalid")
        source = {
            "kind": "telegram_message",
            "chat_id": telegram_chat_id,
            "message_id": telegram_message_id,
        }
    elif voice_supplied:
        if not voice_request_id or not voice_call_session_id:
            raise ValueError("The trusted Voice source identity is invalid")
        source = {
            "kind": "voice_request",
            "call_session_id": voice_call_session_id,
            "request_id": voice_request_id,
        }
    elif stream_supplied:
        if not stream_id:
            raise ValueError("The trusted stream source identity is invalid")
        source = {"kind": "stream", "stream_id": stream_id}
    else:
        # JSON-RPC request ids restart independently for every client/session. They cannot both
        # distinguish independent mutations and remain stable across a reconnect, so they are never
        # accepted as durable mutation identity. Keep the parameter only for internal compatibility
        # while callers migrate; trusted source headers or the signed delegation identity are required.
        _ = mcp_request_id
        raise ValueError("A trusted source event identity is required")
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "owner_id": owner_id,
            "source": source,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(canonical).hexdigest()}"





def _enterprise_request_scope() -> tuple[str, str]:
    headers = _request_headers()
    _require_enterprise_mcp_service_auth(headers)
    _require_enterprise_mcp_identity_assertion(headers)
    tenant_id = _header_value(headers, HEADER_TENANT_ID) or _configured_enterprise_tenant_id()
    user_id = _header_value(headers, HEADER_USER_ID) or DEFAULT_OWNER_ID
    if not tenant_id or not user_id:
        raise PermissionError("GlassHive MCP requires authenticated tenant and user scope")
    return tenant_id, user_id


def _require_enterprise_payload_scope(
    payload: dict[str, Any],
    *,
    label: str,
    tenant_id: str,
    owner_id: str | None = None,
) -> None:
    payload_tenant = str(payload.get("tenant_id") or "").strip()
    if payload_tenant != tenant_id:
        raise PermissionError(f"GlassHive {label} is not scoped to the authenticated tenant")
    if owner_id is None:
        return
    payload_owner = str(payload.get("owner_id") or "").strip()
    if payload_owner != owner_id:
        raise PermissionError(f"GlassHive {label} is not owned by the authenticated user")


def _request_alias_input(alias: str) -> tuple[AuthContext | None, str]:
    clean_alias = alias.strip()
    if not clean_alias or not _enterprise_mode_enabled():
        return None, clean_alias
    tenant_id, user_id = _enterprise_request_scope()
    ctx = AuthContext(tenant_id=tenant_id, user_id=user_id, enterprise=True)
    prefix = (
        f"{normalize_identity_segment(tenant_id, 'tenant')}--"
        f"{normalize_identity_segment(user_id, 'user')}--"
    )
    if clean_alias.startswith(prefix):
        clean_alias = clean_alias[len(prefix) :]
    return ctx, clean_alias


def _request_unscoped_alias(alias: str) -> str:
    _, clean_alias = _request_alias_input(alias)
    return clean_alias


def _request_scoped_alias(alias: str) -> str:
    ctx, clean_alias = _request_alias_input(alias)
    return scoped_alias(ctx, clean_alias) if ctx is not None else clean_alias


def _sanitize_context_value(value: str | None) -> str:
    stripped = str(value or "").strip()
    if stripped.startswith("{{") and stripped.endswith("}}"):
        return ""
    if stripped.startswith("${") and stripped.endswith("}"):
        return ""
    return stripped


def _token_matches(candidate: str | None, expected: str | None) -> bool:
    candidate_text = str(candidate or "").strip()
    expected_text = str(expected or "").strip()
    if not candidate_text or not expected_text:
        return False
    return hmac.compare_digest(candidate_text, expected_text)


def _require_mcp_http_service_auth(headers: dict[str, str]) -> None:
    """Authenticate every network MCP request, including local loopback traffic."""

    expected = str(DEFAULT_API_TOKEN or os.environ.get("WPR_API_TOKEN", "")).strip()
    if not expected:
        raise PermissionError("GlassHive MCP service authentication is not configured")
    auth_header = str(headers.get("authorization") or "").strip()
    bearer = (
        auth_header.removeprefix("Bearer ").strip()
        if auth_header.lower().startswith("bearer ")
        else ""
    )
    header_token = _header_value(headers, HEADER_SERVICE_TOKEN)
    if not (
        _token_matches(header_token, expected) or _token_matches(bearer, expected)
    ):
        raise PermissionError("GlassHive MCP service authentication is required")

def _require_enterprise_mcp_service_auth(headers: dict[str, str]) -> None:
    if not _enterprise_mode_enabled():
        return

    _require_mcp_service_auth(headers)


def _require_mcp_service_auth(headers: dict[str, str]) -> None:
    if get_access_token() is not None:
        return
    expected = str(DEFAULT_MCP_API_TOKEN or os.environ.get("GLASSHIVE_MCP_API_KEY", "")).strip()
    if not expected:
        raise PermissionError("GlassHive MCP service authentication is not configured")
    auth_header = str(headers.get("authorization") or "").strip()
    bearer = (
        auth_header.removeprefix("Bearer ").strip()
        if auth_header.lower().startswith("bearer ")
        else ""
    )
    header_token = _header_value(headers, HEADER_SERVICE_TOKEN)
    if not (
        _token_matches(header_token, expected) or _token_matches(bearer, expected)
    ):
        raise PermissionError("GlassHive MCP service authentication is required")




def _configured_enterprise_tenant_id() -> str:
    return (
        _sanitize_context_value(os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID"))
        or _sanitize_context_value(os.environ.get("WPR_ENTERPRISE_TENANT_ID"))
    )


def _require_enterprise_mcp_identity_assertion(headers: dict[str, str]) -> None:
    if not _enterprise_mode_enabled():
        return
    configured_tenant = _configured_enterprise_tenant_id()
    asserted_tenant = _header_value(headers, HEADER_TENANT_ID)
    if not configured_tenant:
        raise PermissionError("GlassHive MCP enterprise tenant is not configured")
    if asserted_tenant and asserted_tenant != configured_tenant:
        raise PermissionError("GlassHive MCP tenant assertion does not match this deployment")
    if not _header_value(headers, HEADER_USER_ID):
        raise PermissionError("GlassHive MCP requires an authenticated user assertion")


class McpHttpAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        headers = {key.lower(): value for key, value in request.headers.items()}
        try:
            _require_mcp_service_auth(headers)
            _packaged_local_owner(headers=headers)
            if _enterprise_mode_enabled():
                _require_enterprise_mcp_identity_assertion(headers)
        except PermissionError as exc:
            return JSONResponse(status_code=401, content={"detail": str(exc)})
        return await call_next(request)


# Backward-compatible import name for deployments/tests that referenced the former enterprise-only
# middleware. HTTP service authentication now applies in both local and enterprise deployments.
def _require_mcp_http_identity_assertion(headers: dict[str, str]) -> None:
    if _enterprise_mode_enabled():
        _require_enterprise_mcp_identity_assertion(headers)
        return
    try:
        supplied, _user_id = _trusted_identity_header(
            headers,
            HEADER_USER_ID,
            label="user",
        )
    except ValueError as exc:
        raise PermissionError("GlassHive MCP requires an authenticated user assertion") from exc
    if not supplied:
        raise PermissionError("GlassHive MCP requires an authenticated user assertion")

EnterpriseMcpHttpAuthMiddleware = McpHttpAuthMiddleware


class McpOAuthScopeChallengeMiddleware:
    def __init__(self, app: ASGIApp, *, path: str, required_scopes: list[str]) -> None:
        self.app = app
        self.path = path
        self.required_scope = " ".join(required_scopes)
        if any(
            character in {'"', "\\"} or ord(character) < 0x21 or ord(character) > 0x7E
            for character in self.required_scope
            if character != " "
        ):
            raise McpOAuthConfigurationError("MCP OAuth scopes cannot be represented in a challenge")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_scope(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message.get("status") == 401
                and scope.get("path") == self.path
                and self.required_scope
            ):
                headers = list(message.get("headers", []))
                for index, (name, value) in enumerate(headers):
                    if name.lower() != b"www-authenticate":
                        continue
                    challenge = value.decode("latin-1")
                    if challenge.lower().startswith("bearer ") and not re.search(
                        r"(?:^|,)\s*scope=", challenge, flags=re.IGNORECASE
                    ):
                        headers[index] = (
                            name,
                            f'{challenge}, scope="{self.required_scope}"'.encode("latin-1"),
                        )
                    break
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_scope)


class GlassHiveFastMCP(FastMCP):
    def streamable_http_app(self):
        app = super().streamable_http_app()
        auth = self.settings.auth
        if auth and auth.required_scopes:
            app.add_middleware(
                McpOAuthScopeChallengeMiddleware,
                path=self.settings.streamable_http_path,
                required_scopes=auth.required_scopes,
            )
        return app


def _require_enterprise_mcp_transport(transport: str) -> None:
    if _enterprise_mode_enabled() and transport != "streamable-http":
        raise RuntimeError("GlassHive MCP enterprise mode requires streamable-http transport")


def _audit_preview(value: str, *, max_chars: int = 700) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[REDACTED]", text)
    secret_fields = r"api[_-]?key|token|secret|password|passwd|pwd|auth|credential|session[_-]?token|bearer|signature"
    text = re.sub(
        rf"(?i)([\"']?(?:{secret_fields})[\"']?\s*:\s*\")[^\"]+(\")",
        r"\1[REDACTED]\2",
        text,
    )
    text = re.sub(
        rf"(?i)([\"']?(?:{secret_fields})[\"']?\s*:\s*')[^']+(')",
        r"\1[REDACTED]\2",
        text,
    )
    text = re.sub(
        rf"(?i)((?:{secret_fields})\s*[:=]\s*)[^\s\"']+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-[REDACTED]", text)
    text = re.sub(r"\b[A-Za-z0-9_]{8,}:[A-Za-z0-9_./+=-]{20,}\b", "[REDACTED_CREDENTIAL]", text)
    text = re.sub(r"(?i)data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{256,}", "[REDACTED_IMAGE_BASE64]", text)
    text = re.sub(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/=])", "[REDACTED_LONG_BASE64]", text)
    text = re.sub(
        r"(?:~\/|\/Users\/|\/home\/|\/private\/var\/|\/var\/folders\/|[A-Za-z]:\\Users\\)[^\s`'\"<>]+",
        "[local path]",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return f"{text[: max_chars - 3].rstrip()}..."
    return text


def _view_steer_link(
    *,
    task: str,
    url: str | None,
    terminal: bool = False,
) -> dict[str, Any]:
    task_label = _audit_preview(task, max_chars=96) or "task"
    return {
        "label": f"View / Steer {task_label}",
        "url": url,
        "include_in_response": bool(url),
        "link_kind": "mission_control",
        "state": "terminal" if terminal else "nonterminal",
    }

def _decode_json_header(value: str | None) -> Any:
    sanitized = _sanitize_context_value(value)
    if not sanitized:
        return None
    raw = sanitized
    if raw.startswith("b64:"):
        try:
            raw = base64.b64decode(raw[4:]).decode("utf-8")
        except Exception:
            return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _safe_upload_filename(value: object, fallback: str) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip() or fallback
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return safe or fallback


def _path_list_env(name: str) -> list[Path]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    paths: list[Path] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if item:
            paths.append(Path(item).expanduser())
    return paths


def _upload_root_candidates() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("WPR_LIBRECHAT_UPLOADS_ROOT", "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    roots.extend(_path_list_env("WPR_BOOTSTRAP_SOURCE_ROOTS"))
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.fspath(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _trusted_virtual_upload_source(
    value: str,
    *,
    owner_id: str | None = None,
    storage_owner_id: str | None = None,
) -> str:
    roots = _upload_root_candidates()
    if not roots or not value.startswith("/uploads/"):
        return ""
    clean_value = value.split("?", 1)[0]
    relative = clean_value.split("/uploads/", 1)[1].strip("/")
    if not relative:
        return ""
    relative_path = os.path.normpath(relative)
    if relative_path == "." or relative_path.startswith("..") or os.path.isabs(relative_path):
        return ""
    if ".." in relative_path.split(os.path.sep):
        return ""
    asserted_owner_scope = bool(
        _sanitize_context_value(owner_id) or _sanitize_context_value(storage_owner_id)
    )
    if asserted_owner_scope:
        allowed_owners = _upload_owner_path_components(owner_id, storage_owner_id)
        first_part = relative_path.split(os.path.sep, 1)[0]
        if not allowed_owners or first_part not in allowed_owners:
            return ""
    candidates = [root / relative_path for root in roots]
    for candidate in candidates:
        if candidate.exists():
            return os.fspath(candidate)
    return ""


def _normalized_upload_filename_key(value: object) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if "__" in name:
        name = name.split("__", 1)[1]
    return re.sub(r"[^a-z0-9]+", "", name)


def _safe_owner_path_component(value: object) -> str:
    clean = str(value or "").strip()
    if not clean or clean in {".", ".."}:
        return ""
    if "\x00" in clean or "/" in clean or "\\" in clean or ".." in clean:
        return ""
    return clean


def _upload_owner_path_components(*values: object) -> list[str]:
    components: list[str] = []
    for value in values:
        clean = _safe_owner_path_component(value)
        if clean and clean not in components:
            components.append(clean)
    return components


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_float(name: str, *, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _progress_notify_timeout_seconds() -> float:
    return _env_float("WPR_MCP_PROGRESS_NOTIFY_TIMEOUT_SEC", default=1.0, minimum=0.0, maximum=10.0)


def _diagnostic_payloads_enabled() -> bool:
    for name in ("GLASSHIVE_MCP_DIAGNOSTIC_PAYLOADS_ENABLED", "WPR_MCP_DIAGNOSTIC_PAYLOADS_ENABLED"):
        if os.environ.get(name) is not None:
            return _env_truthy(name)
    return not _enterprise_mode_enabled()


def _effective_diagnostics_requested(requested: bool, *, tool_name: str) -> bool:
    if not requested:
        return False
    if _diagnostic_payloads_enabled():
        return True
    LOGGER.info(
        "%s diagnostic payload suppressed; set GLASSHIVE_MCP_DIAGNOSTIC_PAYLOADS_ENABLED=true to allow raw diagnostics",
        tool_name,
    )
    return False


def _legacy_upload_fallback_enabled() -> bool:
    return _env_truthy("GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_FALLBACK", default=False)


def _stored_upload_display_filename(value: object, fallback: str) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if "__" in name:
        prefix, suffix = name.split("__", 1)
        if len(prefix) >= 8 and re.fullmatch(r"[0-9A-Fa-f-]+", prefix):
            name = suffix
    return _safe_upload_filename(name, fallback)


def _dedupe_workspace_upload_path(filename: str, used_paths: set[str]) -> str:
    safe = _safe_upload_filename(filename, "upload")
    stem, ext = os.path.splitext(safe)
    candidate = f"uploads/{safe}"
    index = 2
    while candidate in used_paths:
        candidate = f"uploads/{stem}-{index}{ext}"
        index += 1
    used_paths.add(candidate)
    return candidate


def _owner_recent_upload_entries(
    *,
    tenant_id: str | None,
    owner_id: str | None,
    storage_owner_id: str | None,
    conversation_id: str | None,
    message_id: str | None,
) -> list[dict[str, Any]]:
    """Compatibility projection for older LibreChat builds that cannot header-project upload metadata."""
    if not _enterprise_mode_enabled() or not _legacy_upload_fallback_enabled():
        return []
    if not (storage_owner_id and conversation_id and message_id):
        return []
    roots = _upload_root_candidates()
    owner_components = _upload_owner_path_components(storage_owner_id)
    if not roots or not owner_components:
        return []

    window_seconds = _env_float(
        "GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_RECENT_SECONDS",
        default=900.0,
        minimum=5.0,
        maximum=24 * 60 * 60.0,
    )
    max_files = _env_int("GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_MAX_FILES", default=8, minimum=1, maximum=32)
    min_mtime = time.time() - window_seconds
    candidates: list[tuple[float, str, int, Path]] = []

    for root in roots:
        for owner_component in owner_components:
            owner_root = root / owner_component
            if not owner_root.exists() or not owner_root.is_dir():
                continue
            try:
                resolved_owner_root = owner_root.resolve()
            except OSError:
                continue
            try:
                for candidate in owner_root.rglob("*"):
                    try:
                        if not candidate.is_file():
                            continue
                        resolved_candidate = candidate.resolve()
                        resolved_candidate.relative_to(resolved_owner_root)
                        stat = candidate.stat()
                    except (OSError, ValueError):
                        continue
                    if stat.st_size <= 0 or stat.st_mtime < min_mtime:
                        continue
                    candidates.append((stat.st_mtime, os.fspath(candidate), stat.st_size, candidate))
            except OSError:
                continue

    if not candidates:
        return []

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    projected: list[dict[str, Any]] = []
    used_paths: set[str] = set()
    for _mtime, _path_key, size, candidate in candidates[:max_files]:
        filename = _stored_upload_display_filename(candidate.name, f"upload-{len(projected) + 1}")
        workspace_path = _dedupe_workspace_upload_path(filename, used_paths)
        source_path = os.fspath(candidate)
        token = sign_bootstrap_source_path(source_path, tenant_id=tenant_id, owner_id=owner_id)
        upload_ref = ""
        if "__" in candidate.name:
            prefix = candidate.name.split("__", 1)[0]
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", prefix):
                upload_ref = prefix
        projected.append(
            {
                "scope": "workspace",
                "path": workspace_path,
                "source_path": source_path,
                **({BOOTSTRAP_SOURCE_TOKEN_KEY: token} if token else {}),
                "filename": filename,
                **({"file_id": upload_ref} if upload_ref else {}),
                "bytes": size,
                "source": "librechat_owner_recent_upload_compat",
                "materialized_from": "librechat_owner_recent_upload_compat",
            }
        )
    return projected


def _origin_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalize_worker_backend(worker: dict[str, Any]) -> dict[str, Any]:
    safe = dict(worker or {})
    backend = derive_legacy_backend_label(
        profile=safe.get("profile"),
        runtime=safe.get("runtime"),
        backend=safe.get("backend"),
    )
    if backend:
        safe["backend"] = backend
    return safe


def _owner_scoped_upload_source_for_filename(
    filename: str,
    *,
    owner_id: str | None = None,
    storage_owner_id: str | None = None,
) -> str:
    roots = _upload_root_candidates()
    owner_components = _upload_owner_path_components(owner_id, storage_owner_id)
    target_key = _normalized_upload_filename_key(filename)
    if not roots or not owner_components or not target_key:
        return ""
    candidates: list[Path] = []
    for root in roots:
        for owner_component in owner_components:
            owner_root = root / owner_component
            if not owner_root.exists() or not owner_root.is_dir():
                continue
            try:
                for candidate in owner_root.rglob("*"):
                    if not candidate.is_file():
                        continue
                    if _normalized_upload_filename_key(candidate.name) == target_key:
                        candidates.append(candidate)
            except OSError:
                continue
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item.stat().st_mtime_ns, os.fspath(item)), reverse=True)
    return os.fspath(candidates[0])


def _signed_view_steer_url(worker: dict[str, Any], project_id: str | None, request_surface: str | None) -> str | None:
    if str(worker.get("state") or "") == "terminated":
        return None
    worker_id = str(worker.get("worker_id") or "").strip()
    if not worker_id:
        return None
    url = surface_aware_watch_url(
        worker_id,
        project_id,
        request_surface=request_surface,
        watch_surface="desktop",
    )
    if not url:
        return None
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker_id,
        tenant_id=str(worker.get("tenant_id") or ""),
        owner_id=str(worker.get("owner_id") or ""),
    )
    if token:
        target_url = append_signed_query(url, {"gh_token": token})
        ref_id = create_signed_link_ref(token=token, target_url=target_url)
        if not ref_id:
            return None
        return signed_link_ref_url(_origin_from_url(url), ref_id, route="/r")
    if _enterprise_mode_enabled():
        return None
    if str(worker.get("tenant_id") or "") not in {"", "local"}:
        return None
    return url


def _public_artifact_base_url() -> str:
    return (
        os.environ.get("GLASSHIVE_ARTIFACT_BASE_URL", "").strip()
        or os.environ.get("GLASSHIVE_OPERATOR_BASE_URL", "").strip()
        or os.environ.get("WPR_MCP_PUBLIC_URL", "").strip()
        or DEFAULT_BASE_URL
    ).rstrip("/")


def _clean_artifact_relative_path(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if raw.startswith("/"):
        return ""
    clean_path = raw.lstrip("/")
    if not clean_path:
        return ""
    relative = PurePosixPath(clean_path)
    if relative.is_absolute():
        return ""
    if any(part in {"", ".", "..", ".git"} for part in relative.parts):
        return ""
    if not is_user_deliverable_relative_path(relative):
        return ""
    return relative.as_posix()


def _signed_artifact_url(worker: dict[str, Any], path: str, *, kind: str, action: str) -> str | None:
    if str(worker.get("state") or "") == "terminated":
        return None
    worker_id = str(worker.get("worker_id") or "").strip()
    clean_path = _clean_artifact_relative_path(path)
    if not worker_id or not clean_path:
        return None
    token = sign_link_token(
        kind=kind,
        worker_id=worker_id,
        tenant_id=str(worker.get("tenant_id") or ""),
        owner_id=str(worker.get("owner_id") or ""),
        path=clean_path,
    )
    public_base = _public_artifact_base_url()
    if token:
        ref_id = create_signed_link_ref(token=token)
        if not ref_id:
            return None
        return signed_link_ref_url(public_base, ref_id)
    if _enterprise_mode_enabled():
        return None
    return f"{public_base}/v1/workers/{quote(worker_id)}/artifacts/{action}?path={quote(clean_path)}"


def _signed_artifact_open_url(worker: dict[str, Any], path: str) -> str | None:
    return _signed_artifact_url(worker, path, kind="artifact_open", action="open")


def _signed_artifact_download_url(worker: dict[str, Any], path: str) -> str | None:
    return _signed_artifact_url(worker, path, kind="artifact_download", action="download")


def _artifact_listing_payload(
    *,
    worker: dict[str, Any],
    artifacts: dict[str, Any],
    include_open_links: bool = True,
    include_download_links: bool = True,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    raw_items = artifacts.get("items", []) if isinstance(artifacts, dict) else []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        path = _clean_artifact_relative_path(str(item.get("path") or ""))
        if not path:
            continue
        open_url = _signed_artifact_open_url(worker, path) if include_open_links else None
        download_url = _signed_artifact_download_url(worker, path) if include_download_links else None
        if include_open_links:
            item["signed_open_url"] = open_url
        if include_download_links:
            item["signed_download_url"] = download_url
        if download_url:
            item["default_url"] = download_url
            item["default_link_kind"] = "download"
            item["default_link_label"] = "Download file"
        elif open_url:
            item["default_url"] = open_url
            item["default_link_kind"] = "open"
            item["default_link_label"] = "Open GlassHive file"
        items.append(item)
    return {
        "status": "ok",
        "worker_id": str(worker.get("worker_id") or "").strip() or None,
        "items": items,
        "open_links_signed": bool(include_open_links),
        "download_links_signed": bool(include_download_links),
        "download_link_ttl_seconds": signed_link_ttl_seconds(),
        "next_action_guidance": (
            "Use relevant signed_download_url/default_url values as the default user-facing file "
            "links when the user asked for files/artifacts or the worker output references "
            "user-facing files. Also include signed_open_url preview links or the View / Steer "
            "workspace link when useful so the user can inspect all deliveries. Do not invent file "
            "links, and do not paste whole generated files into chat when GlassHive provides "
            "scoped file links."
        ),
    }


_STATUS_ARTIFACT_LINK_ITEM_LIMIT = 5


def _output_referenced_artifact_paths(text: str) -> list[str]:
    """Return workspace-relative artifact paths mentioned by a worker final message."""
    if not text:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"/workspace/(?:project/)?([^\s)>\]\"']+)", text):
        clean = _clean_artifact_relative_path(match.group(1).rstrip(".,;:"))
        if clean and clean not in seen:
            seen.add(clean)
            paths.append(clean)
    return paths


def _professional_artifact_rank(path: str) -> int:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".pdf":
        return 0
    if suffix in {".docx", ".pptx", ".xlsx"}:
        return 1
    if suffix in {".html", ".htm"}:
        return 2
    if suffix in {".csv", ".json", ".md", ".txt"}:
        return 3
    return 4


def _prioritized_status_artifact_items(items: list[Any], preferred_paths: list[str] | None) -> list[Any]:
    preferred = {path: index for index, path in enumerate(preferred_paths or [])}

    def rank(item: Any) -> tuple[int, int, int, str]:
        if not isinstance(item, dict):
            return (9, 9, 9, "")
        path = _clean_artifact_relative_path(str(item.get("path") or ""))
        if path in preferred:
            return (0, preferred[path], _professional_artifact_rank(path), path)
        return (1, 0, _professional_artifact_rank(path), path)

    return sorted(items, key=rank)


def _compact_status_artifact_links(
    payload: dict[str, Any],
    *,
    preferred_paths: list[str] | None = None,
) -> dict[str, Any]:
    compact = {
        key: value
        for key, value in payload.items()
        if key not in {"worker_id", "next_action_guidance"}
    }
    items = compact.get("items")
    if not isinstance(items, list):
        return compact
    total_count = len(items)
    visible_items = _prioritized_status_artifact_items(items, preferred_paths)[:_STATUS_ARTIFACT_LINK_ITEM_LIMIT]
    compact["items"] = visible_items
    compact["count"] = total_count
    compact["visible_item_count"] = len(visible_items)
    compact["truncated"] = total_count > len(visible_items)
    if total_count > len(visible_items):
        compact["remaining_item_count"] = total_count - len(visible_items)
        compact["full_inventory_available"] = True
        compact["full_inventory_tool"] = "workspace_artifacts"
    return compact


_WAIT_DELIVERABLE_READY_DIRS = {"artifacts", "deliverables", "output", "outputs", "reports"}
_WAIT_DELIVERABLE_READY_OUT_DIRS = {"artifact", "artifacts", "data", "deliverable", "deliverables", "report", "reports"}
_WAIT_DELIVERABLE_READY_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".md",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".xls",
    ".xlsx",
}


def _wait_deliverable_ready_path(path: str) -> bool:
    clean = _clean_artifact_relative_path(path)
    if not clean:
        return False
    relative = PurePosixPath(clean)
    parts = [part.lower() for part in relative.parts]
    if not parts:
        return False
    if parts[0] in _WAIT_DELIVERABLE_READY_DIRS:
        return True
    if len(parts) >= 2 and parts[0] == "out" and parts[1] in _WAIT_DELIVERABLE_READY_OUT_DIRS:
        return True
    return len(parts) > 1 and relative.suffix.lower() in _WAIT_DELIVERABLE_READY_EXTENSIONS


async def _report_workspace_wait_progress(
    ctx: Context | None,
    *,
    elapsed_seconds: float,
    timeout_seconds: float,
    attempts: int,
    status: str,
    run_state: object,
) -> None:
    if ctx is None:
        return
    total = max(1.0, timeout_seconds)
    progress = total if status in {"completed", "terminal", "still_running"} else min(elapsed_seconds, total)
    state_text = str(run_state or "running").strip() or "running"
    message = (
        f"GlassHive workspace {status}; waited {int(max(0.0, elapsed_seconds))}s; "
        f"run state: {state_text}; check {attempts}."
    )
    notify_timeout = _progress_notify_timeout_seconds()
    if notify_timeout <= 0:
        LOGGER.debug("workspace_wait progress notification skipped because timeout is disabled")
        return
    task = asyncio.create_task(ctx.report_progress(progress=progress, total=total, message=message))
    try:
        done, _pending = await asyncio.wait({task}, timeout=notify_timeout)
        if task in done:
            task.result()
            return
        task.cancel()
        task.add_done_callback(_consume_progress_notification_result)
        LOGGER.warning(
            "workspace_wait progress notification timed out after %.3fs status=%s run_state=%s attempts=%s",
            notify_timeout,
            status,
            run_state,
            attempts,
        )
    except Exception:
        LOGGER.debug("workspace_wait progress notification failed", exc_info=True)


def _consume_progress_notification_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        LOGGER.debug("workspace_wait progress notification failed after timeout", exc_info=True)


def _dispatch_follow_up_context(
    *,
    worker: dict[str, Any],
    project_id: str,
    run: dict[str, Any],
    request_surface: str | None,
    task: str,
    expose_diagnostics: bool = False,
    explicit_follow_up_required: bool = False,
) -> dict[str, Any]:
    worker_id = str(worker.get("worker_id") or "").strip()
    run_id = str(run.get("run_id") or "").strip()
    view_steer_url = _signed_view_steer_url(worker, project_id, request_surface)
    payload: dict[str, Any] = {
        "run_state": run.get("state"),
        "result_tools": {
            "status": "workspace_status",
            "wait": "workspace_wait",
            "artifacts": "workspace_artifacts",
        },
        "completion_wait_timeout_seconds": _blocking_wait_default_seconds(),
        "completion_polling_guidance": (
            "When the user requested a completed result, deliverable, or answer in this turn, "
            "continue calling workspace_wait until it returns a terminal result, a non-retryable "
            "failure, or the host tool budget is genuinely exhausted. A bounded still_running "
            "response means the worker is healthy but not done yet; do not ask the user to say "
            "'keep waiting' merely because one wait chunk ended."
        ),
        "view_steer": _view_steer_link(
            task=task,
            url=view_steer_url,
            terminal=str(run.get("state") or "").strip().lower()
            in {"completed", "failed", "cancelled", "interrupted", "stopped"},
        ),
    }
    if view_steer_url:
        payload["view_steer_url"] = view_steer_url
    if expose_diagnostics or explicit_follow_up_required:
        payload["follow_up_context"] = {
            "project_id": project_id,
            "worker_id": worker_id,
            "run_id": run_id,
            "run_state": run.get("state"),
            "status_tool": "workspace_status",
            "blocking_wait_tool": "workspace_wait",
            "completion_wait_timeout_seconds": _blocking_wait_default_seconds(),
            "live_tool": "worker_live",
            "takeover_tool": "worker_takeover",
            "artifact_tool": "workspace_artifacts",
            "artifact_download_tool": "workspace_artifact_download",
        }
    return payload


@dataclass
class RecentDispatchContext:
    run_id: str
    worker_id: str
    project_id: str
    created_monotonic: float


def _blocked_dispatch_result(
    payload: dict[str, Any],
    *,
    profile: str,
    execution_mode: str,
    effort: str | None = None,
    alias: str | None = None,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "acknowledgement_guidance": (
            "Explain plainly that GlassHive did not start this workspace because the selected worker "
            "configuration is unavailable. Do not claim that work is running."
        ),
        "main_agent_next_action": (
            "Tell the user the blocker and, when it does not contradict their explicit choice, offer "
            "to retry with an available worker profile or sandbox/workstation execution mode. If the "
            "user already asked for sandbox/workstation mode, retry using execution_mode='docker'."
        ),
        "profile": profile,
        "execution_mode": execution_mode,
        "effort": effort or "",
        "alias": alias or "",
        "callback_ready": False,
        "view_steer_url": None,
        "view_steer": {
            "label": "View / Steer GlassHive workspace",
            "url": None,
            "include_in_acknowledgement": False,
        },
        **payload,
    }


def _can_recover_blocked_host_dispatch_to_docker(
    blocked: dict[str, Any] | None,
    *,
    requested_execution_mode: str | None,
    resolved_execution_mode: str,
    workspace_root: str | None,
) -> bool:
    if not blocked or blocked.get("failure_class") != "runtime_dependency_missing":
        return False
    if resolved_execution_mode != "host":
        return False
    if str(requested_execution_mode or "").strip() and _host_workers_enabled():
        return False
    if str(workspace_root or "").strip():
        return False
    return True


def _iter_upload_file_objects(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _iter_upload_file_objects(item)
        return
    if not isinstance(value, dict):
        return
    if any(key in value for key in ("file_id", "filename", "filepath", "source_path", "local_path", "text")):
        yield value
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _iter_upload_file_objects(child)


def _project_upload_file_entry(
    file_obj: dict[str, Any],
    index: int,
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
    storage_owner_id: str | None = None,
) -> dict[str, Any] | None:
    file_id = str(file_obj.get("file_id") or file_obj.get("id") or "").strip()
    filename = _safe_upload_filename(
        file_obj.get("filename") or file_obj.get("name") or file_obj.get("filepath") or file_id,
        f"upload-{index}",
    )
    metadata = {
        key: file_obj.get(key)
        for key in (
            "file_id",
            "filename",
            "source",
            "context",
            "type",
            "bytes",
            "media_group_index",
        )
        if file_obj.get(key) is not None
    }
    source_ref = ""
    for key in ("filepath", "path", "local_path", "upload_path", "absolute_path", "url", "uri"):
        candidate = str(file_obj.get(key) or "").strip()
        if candidate:
            source_ref = candidate
            break
    if source_ref:
        metadata["source_ref"] = source_ref
    trusted_source = _trusted_virtual_upload_source(
        source_ref,
        owner_id=owner_id,
        storage_owner_id=storage_owner_id,
    )
    if not trusted_source:
        trusted_source = _owner_scoped_upload_source_for_filename(
            filename,
            owner_id=owner_id,
            storage_owner_id=storage_owner_id,
        )
    if trusted_source:
        token = sign_bootstrap_source_path(trusted_source, tenant_id=tenant_id, owner_id=owner_id)
        return {
            "scope": "workspace",
            "path": f"uploads/{filename}",
            "source_path": trusted_source,
            **({BOOTSTRAP_SOURCE_TOKEN_KEY: token} if token else {}),
            **metadata,
        }
    text = file_obj.get("text")
    if isinstance(text, str) and text.strip():
        if filename.lower().endswith((".txt", ".md", ".csv", ".json", ".jsonl", ".tsv", ".yaml", ".yml", ".xml", ".html", ".htm", ".log")):
            return {
                "scope": "workspace",
                "path": f"uploads/{filename}",
                "content": text,
                **metadata,
            }
        metadata = {
            **metadata,
            "filename": metadata.get("filename") or filename,
            "source_status": "original_bytes_unavailable",
            "blocker": (
                "Original uploaded file bytes were not safely available to GlassHive. "
                "Do not substitute extracted text for this file unless the user explicitly asked for text extraction."
            ),
            "extracted_text_available": True,
        }
    if not metadata:
        return None
    manifest_name = f"{filename}.metadata.json"
    return {
        "scope": "workspace",
        "path": f"uploads/{manifest_name}",
        "content": json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        **({"upload_blocker": metadata.get("blocker")} if metadata.get("blocker") else {}),
        **{key: value for key, value in metadata.items() if key != "source_ref"},
    }


def _project_upload_files(
    upload_context: dict[str, Any],
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
    storage_owner_id: str | None = None,
) -> list[dict[str, Any]]:
    # One canonical projector owns foreground conversation and durable mission uploads. This keeps
    # owner/root checks, byte preservation, and same-name ordering identical across both paths.
    return _project_request_upload_files(
        upload_context,
        tenant_id=tenant_id,
        owner_id=owner_id,
        storage_owner_id=storage_owner_id,
    )


def _merge_bundle_files(existing: Any, projected: list[dict[str, Any]]) -> list[Any]:
    return merge_projected_upload_files(existing, projected)


def _append_materialized_uploads_instruction(
    bundle: dict[str, Any],
    projected: list[dict[str, Any]],
) -> None:
    paths = []
    blocker_paths = []
    for entry in projected:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip().lstrip("/")
        if path and path not in paths:
            paths.append(path)
        if path and entry.get("upload_blocker") and path not in blocker_paths:
            blocker_paths.append(path)
    if not paths:
        return

    existing = _safe_text_content(bundle.get("project_definition")).rstrip()
    if "## Attached workspace files" in existing:
        return
    lines = [
        "",
        "## Attached workspace files",
        "",
        "GlassHive has already prepared the uploaded/attached file records before this worker starts.",
        "Read materialized files directly from these workspace-relative paths. Metadata manifests mean the original bytes were not safely available; treat those as blockers for file-preserving work and report that plainly instead of substituting extracted text. Do not ask the user to re-attach unless a listed file is missing, unreadable, or inconsistent with the user's request.",
    ]
    lines.extend(f"- `{path}`" for path in paths)
    if blocker_paths:
        lines.extend(
            [
                "",
                "The following attachment records are metadata/blocker manifests, not source files:",
            ]
        )
        lines.extend(f"- `{path}`" for path in blocker_paths)
    bundle["project_definition"] = (existing + "\n".join(lines) + "\n").lstrip()


def _strip_materialized_uploads_instruction(bundle: dict[str, Any]) -> None:
    project_definition = str(bundle.get("project_definition") or "")
    marker = "## Attached workspace files"
    if marker in project_definition:
        bundle["project_definition"] = (
            project_definition.split(marker, 1)[0].rstrip() + "\n"
        )

def _apply_trusted_selected_file_scope(
    bundle: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]] | None]:
    selected = trusted_selected_files(bundle)
    if selected is None:
        return bundle, None
    scoped = dict(bundle)
    scoped_files = intersect_upload_records(scoped.get("files"), selected)
    if scoped_files:
        scoped["files"] = scoped_files
    else:
        scoped.pop("files", None)
    scoped.pop("glasshive_upload_context", None)
    scoped.pop("viventium_upload_context", None)
    _strip_materialized_uploads_instruction(scoped)
    return scoped, selected

def _selected_upload_context(
    upload_context: dict[str, Any],
    selected: list[dict[str, str]] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = intersect_upload_records(upload_context, selected)
    public_context: dict[str, Any] = {}
    for key, value in upload_context.items():
        public = public_upload_ledger(intersect_upload_records(value, selected))
        if public:
            public_context[key] = public
    return records, public_context

def _without_internal_storage_authority(value: Any) -> Any:
    """Remove transport-only owner authority from every worker-visible projection."""

    if isinstance(value, dict):
        return {
            key: _without_internal_storage_authority(item)
            for key, item in value.items()
            if str(key) not in {"storage_user_id", "storageUserId"}
        }
    if isinstance(value, list):
        return [_without_internal_storage_authority(item) for item in value]
    return value

def _safe_text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True) + "\n"
    return str(value)


def _coerce_bundle_file_entries(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    if any(key in value for key in ("path", "content", "source_path", "local_path", "upload_path", "absolute_path", "filepath")):
        return [value]
    entries: list[dict[str, Any]] = []
    for raw_path, raw_entry in value.items():
        path = str(raw_path or "").strip().lstrip("/")
        if not path:
            continue
        if isinstance(raw_entry, dict):
            entry = dict(raw_entry)
            entry.setdefault("scope", "workspace")
            entry.setdefault("path", path)
        else:
            entry = {"scope": "workspace", "path": path, "content": _safe_text_content(raw_entry)}
        entries.append(entry)
    return entries


def _normalize_bootstrap_bundle(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        parsed = json.loads(stripped)
    elif isinstance(value, dict):
        parsed = dict(value)
    else:
        raise ValueError("bootstrap_bundle_json must be a JSON object or JSON string")
    if not isinstance(parsed, dict):
        raise ValueError("bootstrap_bundle_json must decode to a JSON object")

    # Host protocol state is never accepted from a public tool argument.  The trusted delegation
    # courier may project it only after normalization has removed any caller-authored value.
    parsed.pop("viventium_constraint_source", None)
    parsed.pop("viventium_continuation_contract", None)
    parsed.pop("viventium_continuation_context", None)

    if isinstance(parsed.get("viventium_launch_authority"), dict):
        parsed = canonical_parallel_clean_room_bootstrap(parsed)

    files = _coerce_bundle_file_entries(parsed.get("files"))
    if "files" in parsed:
        parsed["files"] = files
    if files:
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip().lstrip("/").lower()
            if path in {"project-definition.md", "project_definition.md"} and "project_definition" not in parsed:
                parsed["project_definition"] = _safe_text_content(entry.get("content"))
                break
    return parsed


def _validate_viventium_feelings_projection(
    bundle: dict[str, Any], *, required: bool = False
) -> None:
    """Fail closed when a trusted Viventium delegation loses or contradicts its pinned capsule."""

    projection = bundle.get("viventium_feelings_projection")
    if not isinstance(projection, dict):
        if required:
            raise ValueError("viventium_feelings_projection is required")
        return
    enabled = projection.get("enabled")
    scope = str(projection.get("scope") or "").strip()
    expected_count = projection.get("expected_capsule_count")
    canonical_field = str(projection.get("canonical_instruction_field") or "").strip()
    snapshot_hash = str(projection.get("snapshot_sha256") or "").strip()
    if (
        projection.get("version") != 1
        or not isinstance(enabled, bool)
        or scope not in {"all_agents", "conscious_agent", "unknown"}
        or expected_count not in {0, 1}
        or canonical_field != "agents_md"
        or (snapshot_hash and not re.fullmatch(r"[a-f0-9]{64}", snapshot_hash))
    ):
        raise ValueError("viventium_feelings_projection is invalid")
    if enabled != (scope == "all_agents" and expected_count == 1):
        raise ValueError("viventium_feelings_projection contradicts its scope")
    if enabled and not snapshot_hash:
        raise ValueError("viventium_feelings_projection snapshot is missing")

    start_tag = "<viventium_feeling_state>"
    end_tag = "</viventium_feeling_state>"
    for field in ("agents_md", "claude_md", "codex_md"):
        instructions = str(bundle.get(field) or "")
        count = instructions.count(start_tag)
        required_count = expected_count if field == canonical_field else 0
        if count != required_count or instructions.count(end_tag) != required_count:
            raise ValueError("viventium_feelings_projection capsule count is invalid")
        if required_count == 1:
            start = instructions.index(start_tag)
            end = instructions.index(end_tag, start) + len(end_tag)
            capsule = instructions[start:end]
            if instructions.rstrip() != instructions[:end].rstrip():
                raise ValueError("viventium_feelings_projection capsule is not final")
            actual_hash = hashlib.sha256(capsule.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(actual_hash, snapshot_hash):
                raise ValueError("viventium_feelings_projection snapshot hash is invalid")

def _slugify_alias(*parts: str) -> str:
    raw = "-".join(part for part in parts if str(part or "").strip())
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug[:80] or "glasshive-task"


def _fresh_worker_alias(alias: str) -> str:
    base = (alias or "glasshive-task").strip("-")[:67].strip("-") or "glasshive-task"
    return f"{base}-{uuid.uuid4().hex[:12]}"


def _default_project_definition(*, title: str, goal: str, instruction: str) -> str:
    sections = [f"# {title.strip() or 'GlassHive Task'}"]
    clean_goal = goal.strip()
    clean_instruction = instruction.strip()
    if clean_goal:
        sections.extend(["", clean_goal])
    if clean_instruction and clean_instruction != clean_goal:
        sections.extend(["", "## Task", "", clean_instruction])
    return "\n".join(sections).strip() + "\n"


def _delegation_packet_block(
    bundle: dict[str, Any],
    source_segments: list[tuple[int, str, bool, str]],
) -> str:
    packet = bundle.get("viventium_delegation_packet")
    if not isinstance(packet, dict) or not set(packet).issubset(
        {
            "version",
            "task",
            "explicit_constraints",
            "selected_files",
            "authorized_tool_context",
        }
    ):
        raise ValueError("viventium_delegation_packet is invalid")
    if packet.get("version") != 1 or isinstance(packet.get("version"), bool):
        raise ValueError("viventium_delegation_packet is invalid")

    task = packet.get("task")
    if not isinstance(task, dict) or not set(task).issubset(
        {"title", "instruction", "goal", "source_segments"}
    ):
        raise ValueError("viventium_delegation_packet is invalid")
    task_text: dict[str, str] = {}
    for key, limit in (("title", 10_000), ("instruction", 100_000), ("goal", 100_000)):
        value = task.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
            raise ValueError("viventium_delegation_packet is invalid")
        task_text[key] = value.strip()

    packet_segments = task.get("source_segments", [])
    if not isinstance(packet_segments, list) or len(packet_segments) > 32:
        raise ValueError("viventium_delegation_packet is invalid")
    normalized_packet_segments: list[tuple[int, str, bool, str]] = []
    packet_source_bytes = 0
    for expected_ordinal, segment in enumerate(packet_segments):
        if not isinstance(segment, dict) or not set(segment).issubset(
            {"ordinal", "text", "truncated", "original_sha256"}
        ):
            raise ValueError("viventium_delegation_packet is invalid")
        ordinal = segment.get("ordinal")
        text = segment.get("text")
        truncated = segment.get("truncated") is True
        original_sha256 = str(segment.get("original_sha256") or "")
        if (
            isinstance(ordinal, bool)
            or ordinal != expected_ordinal
            or not isinstance(text, str)
            or not text
            or len(text.encode("utf-8")) > 32 * 1024
            or ("truncated" in segment and segment.get("truncated") is not True)
            or (truncated and not re.fullmatch(r"[0-9a-f]{64}", original_sha256))
            or (not truncated and "original_sha256" in segment)
        ):
            raise ValueError("viventium_delegation_packet is invalid")
        packet_source_bytes += len(text.encode("utf-8"))
        if packet_source_bytes > 64 * 1024:
            raise ValueError("viventium_delegation_packet is invalid")
        normalized_packet_segments.append(
            (expected_ordinal, text, truncated, original_sha256)
        )
    if normalized_packet_segments != source_segments:
        raise ValueError("viventium_delegation_packet is invalid")
    if not task_text.get("instruction") and not normalized_packet_segments:
        raise ValueError("viventium_delegation_packet is invalid")

    constraints = packet.get("explicit_constraints", [])
    if not isinstance(constraints, list) or len(constraints) > 8:
        raise ValueError("viventium_delegation_packet is invalid")
    normalized_constraints: list[tuple[str, str]] = []
    constraint_bytes = 0
    for constraint in constraints:
        if not isinstance(constraint, dict) or set(constraint) != {"kind", "text"}:
            raise ValueError("viventium_delegation_packet is invalid")
        kind = constraint.get("kind")
        text = constraint.get("text")
        if kind not in {"success_criteria", "additional_instructions", "context"}:
            raise ValueError("viventium_delegation_packet is invalid")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("viventium_delegation_packet is invalid")
        constraint_bytes += len(text.encode("utf-8"))
        if constraint_bytes > 128 * 1024:
            raise ValueError("viventium_delegation_packet is invalid")
        normalized_constraints.append((kind, text.strip()))

    selected_files = packet.get("selected_files", [])
    if not isinstance(selected_files, list) or len(selected_files) > 64:
        raise ValueError("viventium_delegation_packet is invalid")
    normalized_files: list[str] = []
    for expected_ordinal, selected in enumerate(selected_files):
        if not isinstance(selected, dict) or not set(selected).issubset(
            {"ordinal", "name", "ref"}
        ):
            raise ValueError("viventium_delegation_packet is invalid")
        ordinal = selected.get("ordinal")
        name = selected.get("name")
        ref = selected.get("ref")
        if (
            isinstance(ordinal, bool)
            or ordinal != expected_ordinal
            or (name is None) == (ref is None)
        ):
            raise ValueError("viventium_delegation_packet is invalid")
        value = name if name is not None else ref
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or (name is not None and ("/" in value or "\\" in value or value in {".", ".."}))
        ):
            raise ValueError("viventium_delegation_packet is invalid")
        normalized_files.append(value.strip())

    tool_context = packet.get("authorized_tool_context")
    normalized_tool_context: dict[str, Any] | None = None
    if tool_context is not None:
        if not isinstance(tool_context, dict) or set(tool_context) != {
            "broker",
            "status",
            "servers",
            "host_tools",
            "content_read",
        }:
            raise ValueError("viventium_delegation_packet is invalid")
        broker = tool_context.get("broker")
        status = tool_context.get("status")
        servers = tool_context.get("servers")
        host_tools = tool_context.get("host_tools")
        content_read = tool_context.get("content_read")
        if (
            not isinstance(broker, str)
            or not broker.strip()
            or status not in {"active", "available", "pending_admission", "ready", "unavailable"}
            or not isinstance(servers, list)
            or not isinstance(host_tools, list)
            or len(servers) > 64
            or len(host_tools) > 64
            or not isinstance(content_read, bool)
        ):
            raise ValueError("viventium_delegation_packet is invalid")
        for values in (servers, host_tools):
            if values != sorted(set(values)) or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 160
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                for value in values
            ):
                raise ValueError("viventium_delegation_packet is invalid")
        normalized_tool_context = {
            "broker": broker.strip(),
            "status": status,
            "servers": servers,
            "host_tools": host_tools,
            "content_read": content_read,
        }

    lines = ["## Delegation packet", ""]
    for label, key in (("Title", "title"), ("Task", "instruction"), ("Goal", "goal")):
        if key in task_text:
            lines.extend([f"{label}:", task_text[key], ""])
    if normalized_packet_segments:
        lines.extend(["Selected user task segments:", ""])
        for ordinal, text, truncated, _digest in normalized_packet_segments:
            suffix = " (truncated by Core)" if truncated else ""
            lines.extend(
                [
                    f"--- BEGIN USER TASK SEGMENT {ordinal}{suffix} ---",
                    text,
                    f"--- END USER TASK SEGMENT {ordinal} ---",
                    "",
                ]
            )
    if normalized_constraints:
        lines.extend(["Explicit constraints:", ""])
        for kind, text in normalized_constraints:
            lines.extend([f"- {kind}:", text])
        lines.append("")
    if normalized_files:
        lines.extend(["Selected files:", *[f"- {value}" for value in normalized_files], ""])
    if normalized_tool_context:
        lines.extend(
            [
                "Authorized tool context:",
                f"- Broker: {normalized_tool_context['broker']}",
                f"- Status: {normalized_tool_context['status']}",
                "- Servers: " + (", ".join(normalized_tool_context["servers"]) or "none"),
                "- Host tools: " + (", ".join(normalized_tool_context["host_tools"]) or "none"),
                "- Content read: " + ("authorized" if normalized_tool_context["content_read"] else "not authorized"),
            ]
        )
    return "\n".join(lines).strip()

def _trusted_triggering_source_block(bundle: dict[str, Any]) -> str:
    context = bundle.get("viventium_delegation_context")
    if context is None:
        return ""
    identity = bundle.get("viventium_delegation_identity")
    if not isinstance(context, dict) or not isinstance(identity, dict):
        raise ValueError("viventium_delegation_context is invalid")
    allowed_context_keys = {
        "version",
        "source_event_id",
        "logical_turn_id",
        "surface",
        "triggering_source_segments",
    }
    if (
        not {"version", "source_event_id", "triggering_source_segments"}.issubset(context)
        or not set(context).issubset(allowed_context_keys)
        or isinstance(context.get("version"), bool)
        or context.get("version") != 1
    ):
        raise ValueError("viventium_delegation_context is invalid")
    source_event_id = str(context.get("source_event_id") or "")
    if (
        not source_event_id
        or len(source_event_id) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in source_event_id)
        or source_event_id != str(identity.get("source_event_id") or "")
    ):
        raise ValueError("viventium_delegation_context is invalid")
    logical_turn_id = context.get("logical_turn_id")
    if logical_turn_id is not None and (
        not isinstance(logical_turn_id, str)
        or not logical_turn_id
        or len(logical_turn_id) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in logical_turn_id)
    ):
        raise ValueError("viventium_delegation_context is invalid")
    surface = context.get("surface")
    if surface is not None and surface not in {
        "librechat",
        "telegram",
        "voice",
        "workbench",
    }:
        raise ValueError("viventium_delegation_context is invalid")
    segments = context.get("triggering_source_segments")
    if not isinstance(segments, list) or len(segments) > 32:
        raise ValueError("viventium_delegation_context is invalid")
    total_chars = 0
    normalized: list[tuple[int, str, bool, str]] = []
    source_identities: set[tuple[str, int]] = set()
    for expected_ordinal, segment in enumerate(segments):
        if not isinstance(segment, dict) or not set(segment).issubset(
            {
                "ordinal",
                "source_event_id",
                "source_index",
                "text",
                "truncated",
                "original_sha256",
            }
        ):
            raise ValueError("viventium_delegation_context is invalid")
        ordinal = segment.get("ordinal")
        segment_source_event_id = segment.get("source_event_id")
        source_index = segment.get("source_index")
        text = segment.get("text")
        truncated = segment.get("truncated") is True
        original_sha256 = segment.get("original_sha256")
        source_identity = (str(segment_source_event_id or ""), source_index)
        if (
            isinstance(ordinal, bool)
            or ordinal != expected_ordinal
            or not isinstance(segment_source_event_id, str)
            or not segment_source_event_id
            or len(segment_source_event_id) > 160
            or any(
                ord(char) < 32 or ord(char) == 127
                for char in segment_source_event_id
            )
            or isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or source_index < 0
            or source_index > 1_000_000
            or source_identity in source_identities
            or not isinstance(text, str)
            or len(text.encode("utf-8")) > 32 * 1024
            or ("truncated" in segment and segment.get("truncated") is not True)
            or (
                truncated
                and (
                    not isinstance(original_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", original_sha256)
                )
            )
            or (not truncated and "original_sha256" in segment)
        ):
            raise ValueError("viventium_delegation_context is invalid")
        total_chars += len(text.encode("utf-8"))
        if total_chars > 64 * 1024:
            raise ValueError("viventium_delegation_context is invalid")
        source_identities.add(source_identity)
        normalized.append((expected_ordinal, text, truncated, str(original_sha256 or "")))
    return _delegation_packet_block(bundle, normalized)

def _project_trusted_triggering_source_segments(
    bundle: dict[str, Any],
    instruction: str,
    *,
    constraint_instruction: str,
) -> tuple[dict[str, Any], str]:
    # This is a host-derived protocol field, never caller-authored state. The evidence runtime
    # needs the current allowlisted task packet so model-supplied context cannot become a
    # completion requirement.
    bundle.pop("viventium_constraint_source", None)
    block = _trusted_triggering_source_block(bundle)
    if not block:
        return bundle, instruction
    clean_constraint_instruction = str(constraint_instruction or "").strip()
    if not clean_constraint_instruction:
        raise ValueError("constraint_instruction is required for trusted delegation context")
    bundle["viventium_constraint_source"] = {
        "version": 1,
        "instruction": clean_constraint_instruction,
    }
    project_definition = str(bundle.get("project_definition") or "").rstrip()
    bundle["project_definition"] = f"{project_definition}\n\n{block}\n".lstrip()
    return bundle, f"{instruction.rstrip()}\n\n{block}".lstrip()

def _with_worker_host_side_orchestration_rule(instruction: str) -> str:
    clean = instruction.strip()
    if not clean:
        return clean
    if (
        "Host-side GlassHive orchestration checks" in clean
        and "report only blockers observable from inside this worker workspace" in clean
    ):
        return clean
    if "Execution rules:" in clean:
        return "\n".join([clean, WORKER_HOST_SIDE_ORCHESTRATION_RULE])
    return "\n\n".join([clean, "Execution rules:", WORKER_HOST_SIDE_ORCHESTRATION_RULE])


def _append_worker_instruction_note(instruction: str, note: str) -> str:
    clean = str(instruction or "").strip()
    clean_note = str(note or "").strip()
    if not clean_note or clean_note in clean:
        return clean
    if "Execution rules:" in clean:
        return "\n".join([clean, f"- {clean_note}"])
    return "\n\n".join(part for part in (clean, clean_note) if part)


def _strip_worker_instruction_note(instruction: str, note: str) -> str:
    clean = str(instruction or "")
    clean_note = str(note or "").strip()
    if not clean_note:
        return clean.strip()
    return re.sub(r"\n{0,2}" + re.escape(clean_note), "", clean).strip()


def _append_bundle_system_instruction(bundle: dict[str, Any], note: str) -> None:
    clean_note = str(note or "").strip()
    if not clean_note:
        return
    current = str(bundle.get("system_instructions") or "").strip()
    if clean_note in current:
        return
    bundle["system_instructions"] = "\n\n".join(part for part in (current, clean_note) if part)


def _truthy_bundle_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


def _broker_has_content_read_scope(broker: dict[str, Any]) -> bool:
    scopes = broker.get("scopes")
    if not isinstance(scopes, dict):
        return False
    return _truthy_bundle_flag(scopes.get(CAPABILITY_BROKER_CONTENT_READ_SCOPE)) or _truthy_bundle_flag(
        scopes.get("contentRead")
    )


def _has_complete_capability_broker_bundle(bundle: dict[str, Any] | None) -> bool:
    if not isinstance(bundle, dict):
        return False
    broker = bundle.get("glasshive_capability_broker")
    if not isinstance(broker, dict):
        return False
    try:
        if int(broker.get("version")) != 1:
            return False
    except (TypeError, ValueError):
        return False
    if str(broker.get("name") or "").strip() != CAPABILITY_BROKER_NAME:
        return False
    if not str(broker.get("url") or "").strip():
        return False
    expires_at = broker.get("grant_expires_at") or broker.get("grantExpiresAt")
    if expires_at not in (None, ""):
        try:
            if float(expires_at) <= time.time():
                return False
        except (TypeError, ValueError):
            return False
    if not _broker_has_content_read_scope(broker):
        return False

    env = bundle.get("env")
    token_env_present = isinstance(env, dict) and bool(str(env.get(GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV) or "").strip())
    codex_config = str(bundle.get("codex_config_append") or "")
    codex_config_present = (
        f"[mcp_servers.{CAPABILITY_BROKER_NAME}]" in codex_config
        and "bearer_token_env_var" in codex_config
        and GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV in codex_config
    )
    claude_mcp = bundle.get("claude_project_mcp")
    claude_config = claude_mcp.get(CAPABILITY_BROKER_NAME) if isinstance(claude_mcp, dict) else None
    claude_headers = claude_config.get("headers") if isinstance(claude_config, dict) else None
    claude_config_present = isinstance(claude_headers, dict) and bool(
        str(claude_headers.get("Authorization") or "").strip()
    )
    return bool(token_env_present and (codex_config_present or claude_config_present))


def _apply_connected_account_intent_guard(
    bundle: dict[str, Any] | None,
    instruction: str | None = None,
    *,
    connected_account_content_intent: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    if not connected_account_content_intent or _has_complete_capability_broker_bundle(bundle):
        return bundle, instruction
    guarded_bundle: dict[str, Any] = dict(bundle or {})
    _append_bundle_system_instruction(guarded_bundle, CONNECTED_ACCOUNT_NO_BROKER_NOTE)
    guarded_instruction = (
        _append_worker_instruction_note(instruction, CONNECTED_ACCOUNT_NO_BROKER_NOTE)
        if instruction is not None
        else instruction
    )
    return guarded_bundle, guarded_instruction


def _merge_request_context(bundle: dict[str, Any] | None) -> dict[str, Any] | None:
    load_viventium_runtime_env()
    headers = _request_headers()
    callback_url = (
        os.environ.get("GLASSHIVE_EVENTS_WEBHOOK_URL", "").strip()
        or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "").strip()
    )
    callback_secret = (
        os.environ.get("GLASSHIVE_EVENTS_HMAC_SECRET", "").strip()
        or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "").strip()
    )
    context = {
        "tenant_id": _header_value(headers, HEADER_TENANT_ID),
        "user_id": _header_value(headers, HEADER_USER_ID),
        "storage_user_id": _header_value(headers, HEADER_STORAGE_USER_ID),
        "agent_id": _header_value(headers, HEADER_AGENT_ID),
        "conversation_id": _header_value(headers, HEADER_CONVERSATION_ID),
        "parent_message_id": _header_value(headers, HEADER_PARENT_MESSAGE_ID),
        "message_id": _header_value(headers, HEADER_MESSAGE_ID),
        "surface": _header_value(headers, HEADER_SURFACE),
        "input_mode": _header_value(headers, HEADER_INPUT_MODE),
        "stream_id": _header_value(headers, HEADER_STREAM_ID),
        "voice_call_session_id": _header_value(headers, HEADER_VOICE_CALL_SESSION_ID),
        "voice_request_id": _header_value(headers, HEADER_VOICE_REQUEST_ID),
        "telegram_chat_id": _header_value(headers, HEADER_TELEGRAM_CHAT_ID),
        "telegram_user_id": _header_value(headers, HEADER_TELEGRAM_USER_ID),
        "telegram_message_id": _header_value(headers, HEADER_TELEGRAM_MESSAGE_ID),
        "logical_turn_id": _header_value(headers, HEADER_LOGICAL_TURN_ID),
        "logical_turn_revision": _header_value(headers, HEADER_LOGICAL_TURN_REVISION),
    }
    context = {key: value for key, value in context.items() if value}
    public_context = {
        key: value for key, value in context.items() if key != "storage_user_id"
    }
    upload_context = {
        "request_files": _decode_json_header(_header_value(headers, HEADER_REQUEST_FILES)),
        "request_attachments": _decode_json_header(_header_value(headers, HEADER_REQUEST_ATTACHMENTS)),
        "tool_resources": _decode_json_header(_header_value(headers, HEADER_TOOL_RESOURCES)),
        "file_ids": _decode_json_header(_header_value(headers, HEADER_FILE_IDS)),
    }
    upload_context = {key: value for key, value in upload_context.items() if value not in (None, "", [], {})}
    merged, selected_files = _apply_trusted_selected_file_scope(
        dict(bundle or {})
    )
    if not context and not callback_url and not upload_context:
        if selected_files is not None and merged.get("files"):
            _append_materialized_uploads_instruction(merged, merged["files"])
        return _without_internal_storage_authority(merged)
    trusted_launch = isinstance(merged.get("viventium_delegation_identity"), dict) and isinstance(
        merged.get("viventium_delegation_context"), dict
    )
    existing_callbacks = merged.get("callbacks")
    has_existing_callbacks = isinstance(existing_callbacks, dict) and bool(existing_callbacks)
    callbacks = dict(existing_callbacks) if has_existing_callbacks else {}
    if trusted_launch:
        origin_ref = str(callbacks.get("origin_ref") or "").strip()
        callbacks = {"origin_ref": origin_ref} if origin_ref else {}
    callback_context = dict(callbacks)
    if not trusted_launch:
        callback_context.update(public_context)
    has_callback_anchor = all(
        str(callback_context.get(key) or "").strip() for key in CALLBACK_REQUIRED_CONTEXT_KEYS
    )
    should_auto_attach_callback = bool(callback_url and callback_secret and has_callback_anchor)
    if (has_existing_callbacks or should_auto_attach_callback) and not trusted_launch:
        callbacks.update(public_context)
    if should_auto_attach_callback and not trusted_launch:
        callbacks.setdefault("events_webhook_url", callback_url)
        callbacks.setdefault("hmac_secret", callback_secret)
    if callbacks:
        merged["callbacks"] = callbacks
    elif trusted_launch:
        merged.pop("callbacks", None)
    if public_context and not trusted_launch:
        merged.setdefault("glasshive_context", public_context)
        merged.setdefault("viventium_context", public_context)
    elif trusted_launch:
        merged.pop("glasshive_context", None)
        merged.pop("viventium_context", None)

    projected: list[dict[str, Any]] = []
    if upload_context:
        if selected_files is not None:
            intersect_upload_records(
                [merged.get("files"), upload_context],
                selected_files,
                require_all=True,
            )
        selected_records, upload_context = _selected_upload_context(
            upload_context, selected_files
        )
        projected = _project_upload_files(
            {"selected_uploads": selected_records},
            tenant_id=context.get("tenant_id"),
            owner_id=context.get("user_id"),
            storage_owner_id=context.get("storage_user_id"),
        )
    if not projected and selected_files:
        recent = _owner_recent_upload_entries(
            tenant_id=context.get("tenant_id"),
            owner_id=context.get("user_id"),
            storage_owner_id=context.get("storage_user_id"),
            conversation_id=context.get("conversation_id"),
            message_id=context.get("message_id"),
        )
        if selected_files is not None:
            intersect_upload_records(
                [merged.get("files"), recent],
                selected_files,
                require_all=True,
            )
        projected = intersect_upload_records(recent, selected_files)
        if projected:
            LOGGER.info(
                "GlassHive legacy LibreChat upload compatibility fallback materialized %d file(s); "
                "prefer request-file headers when supported",
                len(projected),
            )
            upload_context = dict(upload_context)
            upload_context["legacy_owner_recent_uploads"] = [
                {
                    key: entry.get(key)
                    for key in (
                        "path",
                        "filename",
                        "bytes",
                        "source",
                        "materialized_from",
                    )
                    if entry.get(key) is not None
                }
                for entry in projected
            ]
    if upload_context:
        merged["glasshive_upload_context"] = upload_context
        merged["viventium_upload_context"] = upload_context
    if projected:
        merged["files"] = _merge_bundle_files(merged.get("files"), projected)
    if selected_files is not None and merged.get("files"):
        _append_materialized_uploads_instruction(merged, merged["files"])
    elif projected:
        _append_materialized_uploads_instruction(merged, projected)
    return _without_internal_storage_authority(merged)


def _merge_explicit_uploaded_files(
    bundle: dict[str, Any] | None,
    uploaded_files: Any,
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
    storage_owner_id: str | None = None,
) -> dict[str, Any]:
    if uploaded_files in (None, "", [], {}):
        merged, _selected = _apply_trusted_selected_file_scope(dict(bundle or {}))
        return _without_internal_storage_authority(merged)
    merged, selected_files = _apply_trusted_selected_file_scope(dict(bundle or {}))
    if selected_files is not None:
        intersect_upload_records(
            [merged.get("files"), uploaded_files],
            selected_files,
            require_all=True,
        )
    selected_records = intersect_upload_records(uploaded_files, selected_files)
    public_records = public_upload_ledger(selected_records)
    upload_context = (
        {"tool_uploaded_files": public_records} if public_records else {}
    )

    if upload_context:
        for key in ("glasshive_upload_context", "viventium_upload_context"):
            existing = merged.get(key)
            context = dict(existing) if isinstance(existing, dict) else {}
            context.update(upload_context)
            merged[key] = context

    projected = _project_upload_files(
        {"tool_uploaded_files": selected_records},
        tenant_id=tenant_id,
        owner_id=owner_id,
        storage_owner_id=storage_owner_id,
    )
    if projected:
        for entry in projected:
            if isinstance(entry, dict):
                entry.setdefault("source", "model_context_uploaded_files")
                entry.setdefault("materialized_from", "model_context_uploaded_files")
        merged["files"] = _merge_bundle_files(merged.get("files"), projected)
    if selected_files is not None and merged.get("files"):
        _append_materialized_uploads_instruction(merged, merged["files"])
    elif projected:
        _append_materialized_uploads_instruction(merged, projected)
    return _without_internal_storage_authority(merged)


def _callback_missing_fields(bundle: dict[str, Any] | None) -> list[str]:
    callbacks = bundle.get("callbacks") if isinstance(bundle, dict) else None
    if not isinstance(callbacks, dict):
        callbacks = {}
    required = {
        "events_webhook_url": callbacks.get("events_webhook_url") or callbacks.get("url"),
        "hmac_secret": callbacks.get("hmac_secret") or callbacks.get("secret"),
        "user_id": callbacks.get("user_id"),
        "conversation_id": callbacks.get("conversation_id"),
        "parent_message_id": callbacks.get("parent_message_id"),
        "message_id": callbacks.get("message_id"),
    }
    return [key for key, value in required.items() if not str(value or "").strip()]


def _callback_state(bundle: dict[str, Any] | None, *, required: bool) -> tuple[bool, list[str]]:
    callbacks = bundle.get("callbacks") if isinstance(bundle, dict) else None
    callback_configured = isinstance(callbacks, dict) and bool(callbacks)
    trusted_launch = (
        isinstance(bundle, dict)
        and isinstance(bundle.get("viventium_delegation_identity"), dict)
        and isinstance(bundle.get("viventium_delegation_context"), dict)
        and isinstance(callbacks, dict)
        and bool(str(callbacks.get("origin_ref") or "").strip())
    )
    if trusted_launch:
        load_viventium_runtime_env()
        missing: list[str] = []
        if not (
            os.environ.get("GLASSHIVE_EVENTS_WEBHOOK_URL", "").strip()
            or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "").strip()
        ):
            missing.append("events_webhook_url")
        if not (
            os.environ.get("GLASSHIVE_EVENTS_HMAC_SECRET", "").strip()
            or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "").strip()
        ):
            missing.append("hmac_secret")
        return not missing, missing
    if not callback_configured and not required:
        return False, []
    missing = _callback_missing_fields(bundle)
    return callback_configured and not missing, missing


@dataclass
class WorkersProjectsApiClient:
    base_url: str = DEFAULT_BASE_URL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    api_token: str = DEFAULT_API_TOKEN

    def _validated_request_path(self, path: str) -> str:
        clean = str(path or "")
        route = clean.split("?", 1)[0]
        if not clean.startswith("/") or "\\" in route:
            raise ValueError("GlassHive API path must be an absolute local route")
        if any(segment in {"", ".", ".."} for segment in route.split("/")[1:]):
            raise ValueError("GlassHive API path contains an invalid empty or relative segment")
        return clean

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        require_write_scope: bool = False,
        file_export_prepare: bool = False,
    ) -> Any:
        path = self._validated_request_path(path)
        if file_export_prepare:
            parts = path.split("/")
            worker_prepare = (
                len(parts) == 7 and parts[1:3] == ["v1", "workers"]
                and parts[4:] == ["files", "export", "prepare"]
                and self._path_id(parts[3], "worker_id") == parts[3]
            )
            if method.upper() != "POST" or require_write_scope or not (
                path == "/v1/file-uploads/export/prepare" or worker_prepare
            ):
                raise ValueError("Read-scope POST is restricted to canonical file export preparation")
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request_headers = _request_headers()
        _require_enterprise_mcp_service_auth(request_headers)
        write_requested = require_write_scope or (not file_export_prepare and method.upper() not in {"GET", "HEAD", "OPTIONS"})
        signed_hop = (
            str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user"
            or str(os.environ.get("GLASSHIVE_AUTH_MODE") or "").strip().lower() == "signed_internal_assertion"
        )
        if signed_hop:
            if get_access_token() is None:
                raise PermissionError(
                    "GlassHive multi-user MCP requires a verified OAuth principal"
                )
            try:
                headers["X-GlassHive-User-Assertion"] = signed_runtime_assertion(
                    subject=_header_value(request_headers, HEADER_USER_ID),
                    tenant_id=_header_value(request_headers, HEADER_TENANT_ID),
                    email=_header_value(request_headers, HEADER_USER_EMAIL),
                    role=_header_value(request_headers, HEADER_USER_ROLE),
                    write=write_requested,
                )
            except McpInternalAssertionError as exc:
                raise PermissionError(str(exc)) from exc
        else:
            for name in (HEADER_USER_ID, HEADER_TENANT_ID, HEADER_USER_EMAIL, HEADER_USER_ROLE):
                value = _header_value(request_headers, name)
                if value:
                    headers[name] = value
        for name, value in (extra_headers or {}).items():
            if name.lower() not in {"idempotency-key", SERVICE_ASSERTION_HEADER.lower()}:
                raise ValueError("Unsupported GlassHive account API request header")
            if str(value or "").strip():
                headers[name] = str(value).strip()
        with httpx.Client(timeout=self.timeout_sec) as client:
            response = client.request(method, url, json=json_body, headers=headers)
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                if response.status_code == 422:
                    raise GlassHiveBlockedError(_safe_request_validation_failure(payload))
                if isinstance(payload, dict) and payload.get("failure_class"):
                    raise GlassHiveBlockedError(payload)
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if (
                    isinstance(detail, dict)
                    and isinstance(detail.get("code"), str)
                    and re.fullmatch(r"[a-z0-9_.:-]{1,120}", detail["code"])
                ):
                    # Preserve the account API's typed rejection across MCP. A
                    # temporary capacity rejection is not a permanent tool error.
                    raise GlassHiveBlockedError({
                        "failure_class": detail["code"],
                        "failure_user_message": str(detail.get("message") or "")[:1000],
                        "failure_retryable": response.status_code in {429, 502, 503, 504},
                        "retry_after": response.headers.get("retry-after"),
                    })
                if 400 <= response.status_code < 500 and isinstance(detail, str) and detail.strip():
                    raise GlassHiveApiError(response.status_code, detail)
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()
            return response.text

    def _owner_id(self, owner_id: str | None) -> str:
        fixed_owner = _packaged_local_owner(owner_id, headers=_request_headers())
        if fixed_owner:
            return fixed_owner
        resolved = (owner_id or DEFAULT_OWNER_ID).strip()
        if not resolved:
            raise ValueError("owner_id is required for this operation")
        return resolved

    def _path_id(self, value: str, label: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError(f"{label} is required")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", clean):
            raise ValueError(f"{label} must be a simple id")
        return quote(clean, safe="")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def get_preferences(self) -> dict[str, Any]:
        return self._request("GET", "/v1/preferences")

    def update_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", "/v1/preferences", json_body=payload)

    def current_user(self) -> dict[str, Any]:
        return self._request("GET", "/v1/me")

    def workspace_catalog(
        self,
        *,
        search: str = "",
        kind: str = "named",
        tags: list[str] | None = None,
        favorite: bool | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        query: dict[str, str] = {
            "kind": kind,
            "search": search,
            "tags": ",".join(tags or []),
            "limit": str(max(1, min(int(limit), 100))),
        }
        if favorite is not None:
            query["favorite"] = "true" if favorite else "false"
        if cursor:
            query["cursor"] = cursor
        return self._request("GET", f"/v1/workspaces?{httpx.QueryParams(query)}")

    def create_execution_workspace(
        self,
        project_id: str,
        *,
        execution_mode: Literal["docker", "host"] = "docker",
        file_placement: Literal["common", "member_private"] = "common",
    ) -> dict[str, Any]:
        project_path_id = self._path_id(project_id, "project_id")
        return self._request(
            "POST",
            f"/v1/projects/{project_path_id}/execution-workspaces",
            json_body={
                "mode": "shared",
                "execution_mode": execution_mode,
                "file_placement": file_placement,
            },
        )

    def execution_workspace_members(self, workspace_id: str) -> dict[str, Any]:
        workspace_path_id = self._path_id(workspace_id, "workspace_id")
        return self._request("GET", f"/v1/workspaces/{workspace_path_id}/members")

    def add_execution_workspace_member(
        self,
        workspace_id: str,
        *,
        name: str = "",
        role: str = "member",
        profile: str | None = None,
        effort: str | None = None,
        provider_account_policy: Literal[
            "legacy", "personal_preferred", "personal_required"
        ] | None = None,
        provider_account_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_path_id = self._path_id(workspace_id, "workspace_id")
        payload: dict[str, Any] = {
            "name": name,
            "role": role,
        }
        # An omitted profile must follow the project's configured default.  A
        # client-side Codex default would silently change a Claude/Grok project.
        if profile is not None:
            payload["profile"] = profile
        if effort is not None:
            payload["effort"] = effort
        if provider_account_policy is not None:
            payload["provider_account_policy"] = provider_account_policy
        if provider_account_id is not None:
            payload["provider_account_id"] = provider_account_id
        return self._request(
            "POST", f"/v1/workspaces/{workspace_path_id}/members", json_body=payload
        )

    def update_workspace(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("PATCH", f"/v1/workspaces/{worker_path_id}", json_body=payload)

    def duplicate_workspace(
        self,
        worker_id: str,
        *,
        idempotency_key: str,
        name: str = "",
    ) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        payload: dict[str, Any] = {"idempotency_key": idempotency_key}
        if name:
            payload["name"] = name
        return self._request(
            "POST",
            f"/v1/workspaces/{worker_path_id}/duplicate",
            json_body=payload,
        )

    def workspace_templates(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/workspace-templates").get("items", [])

    def save_workspace_template(
        self,
        worker_id: str,
        *,
        name: str,
        description: str = "",
        lineage_id: str = "",
    ) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        payload: dict[str, Any] = {"name": name, "description": description}
        if lineage_id:
            payload["lineage_id"] = lineage_id
        return self._request(
            "POST",
            f"/v1/workspaces/{worker_path_id}/templates",
            json_body=payload,
        )

    def instantiate_workspace_template(
        self,
        template_id: str,
        *,
        idempotency_key: str,
        name: str = "",
    ) -> dict[str, Any]:
        template_path_id = self._path_id(template_id, "template_id")
        payload: dict[str, Any] = {"idempotency_key": idempotency_key}
        if name:
            payload["name"] = name
        return self._request(
            "POST",
            f"/v1/workspace-templates/{template_path_id}/instantiate",
            json_body=payload,
        )

    def provider_accounts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/provider-accounts").get("items", [])

    def create_provider_account(
        self,
        *,
        provider: str,
        label: str,
        auth_method: str,
        make_default: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/provider-accounts",
            json_body={
                "provider": provider,
                "label": label,
                "auth_method": auth_method,
                "platform_support": "server_evaluated",
                "secret_locator": (
                    "native-home://auto"
                    if auth_method in {"subscription", "api_key"}
                    else "broker://librechat-openai"
                ),
                "make_default": bool(make_default),
            },
        )

    def start_provider_account_setup(self, account_id: str) -> dict[str, Any]:
        account_path_id = self._path_id(account_id, "account_id")
        return self._request("POST", f"/v1/provider-accounts/{account_path_id}/setup")

    def verify_provider_account(self, account_id: str) -> dict[str, Any]:
        account_path_id = self._path_id(account_id, "account_id")
        return self._request("POST", f"/v1/provider-accounts/{account_path_id}/verify")

    def disconnect_provider_account(self, account_id: str) -> dict[str, Any]:
        account_path_id = self._path_id(account_id, "account_id")
        return self._request("POST", f"/v1/provider-accounts/{account_path_id}/disconnect")

    def forget_provider_account(self, account_id: str) -> dict[str, Any]:
        account_path_id = self._path_id(account_id, "account_id")
        return self._request("DELETE", f"/v1/provider-accounts/{account_path_id}")

    def connections(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/connections").get("items", [])

    def library(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/library").get("items", [])

    def propose_library_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/library/proposals",
            json_body={"manifest": manifest},
        )

    def create_pending_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/pending-changes", json_body=payload)

    def workspace_grants(self, worker_id: str) -> list[dict[str, Any]]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request(
            "GET", f"/v1/workspaces/{worker_path_id}/capability-grants"
        ).get("items", [])

    def revoke_workspace_grant(self, worker_id: str, grant_id: str) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        grant_path_id = self._path_id(grant_id, "grant_id")
        return self._request(
            "DELETE",
            f"/v1/workspaces/{worker_path_id}/capability-grants/{grant_path_id}",
        )

    def activity(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 200))
        return self._request("GET", f"/v1/activity?limit={bounded_limit}").get("items", [])

    def list_projects(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        projects = self._request("GET", "/v1/projects").get("items", [])
        if owner_id:
            return [project for project in projects if project.get("owner_id") == owner_id]
        return projects

    def create_project(self, *, owner_id: str | None, title: str, goal: str, default_worker_profile: str = "codex-cli") -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/projects",
            json_body={
                "owner_id": self._owner_id(owner_id),
                "title": title,
                "goal": goal,
                "default_worker_profile": default_worker_profile,
            },
        )

    def get_project(self, project_id: str) -> dict[str, Any]:
        project_path_id = self._path_id(project_id, "project_id")
        return self._request("GET", f"/v1/projects/{project_path_id}")

    def allowed_ai_policy(self, scope: Literal["project", "workspace"], scope_id: str) -> dict[str, Any]:
        path_id = self._path_id(scope_id, f"{scope}_id")
        collection = "projects" if scope == "project" else "workspaces"
        return self._request("GET", f"/v1/{collection}/{path_id}/execution-policy")

    def allowed_ai_options(self, scope: Literal["project", "workspace"], scope_id: str) -> dict[str, Any]:
        path_id = self._path_id(scope_id, f"{scope}_id")
        collection = "projects" if scope == "project" else "workspaces"
        return self._request("GET", f"/v1/{collection}/{path_id}/execution-options")

    def update_allowed_ai_policy(
        self,
        scope: Literal["project", "workspace"],
        scope_id: str,
        *,
        expected_revision: int,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        path_id = self._path_id(scope_id, f"{scope}_id")
        collection = "projects" if scope == "project" else "workspaces"
        return self._request(
            "PUT",
            f"/v1/{collection}/{path_id}/execution-policy",
            json_body={"expected_revision": expected_revision, "policy": policy},
        )

    def list_project_runs(self, project_id: str) -> list[dict[str, Any]]:
        project_path_id = self._path_id(project_id, "project_id")
        return self._request("GET", f"/v1/projects/{project_path_id}/runs").get("items", [])

    def list_project_events(self, project_id: str) -> list[dict[str, Any]]:
        project_path_id = self._path_id(project_id, "project_id")
        return self._request("GET", f"/v1/projects/{project_path_id}/events").get("items", [])

    def list_workers(self, project_id: str) -> list[dict[str, Any]]:
        project_path_id = self._path_id(project_id, "project_id")
        return self._request("GET", f"/v1/projects/{project_path_id}/workers").get("items", [])

    def find_worker_by_alias_across_projects(
        self,
        *,
        owner_id: str | None,
        alias: str,
        execution_mode: str | None = None,
    ) -> dict[str, Any] | None:
        scoped = _request_scoped_alias(alias)
        if not scoped:
            return None
        for project in self.list_projects(owner_id):
            project_id = str(project.get("project_id") or "").strip()
            if not project_id:
                continue
            for worker in self.list_workers(project_id):
                if str(worker.get("state") or "") in CLOSED_WORKER_STATES:
                    continue
                if execution_mode and worker.get("execution_mode") and worker.get("execution_mode") != execution_mode:
                    continue
                if str(worker.get("alias") or "").strip() == scoped:
                    return {"project": project, "worker": worker}
        return None

    def create_worker(
        self,
        *,
        project_id: str,
        owner_id: str | None,
        name: str,
        role: str,
        profile: str = "codex-cli",
        backend: str | None = None,
        execution_mode: str | None = None,
        resource_class: Literal["standard", "light"] = "standard",
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict[str, Any] | None = None,
        start_synchronously: bool = True,
        workspace_kind: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "owner_id": self._owner_id(owner_id),
            "name": name,
            "role": role,
            "profile": profile,
            "execution_mode": _resolve_execution_mode(execution_mode),
            "resource_class": resource_class,
            "alias": alias,
            "workspace_root": workspace_root,
            "bootstrap_profile": bootstrap_profile,
            "bootstrap_bundle": bootstrap_bundle,
            "start_synchronously": start_synchronously,
        }
        if backend:
            payload["backend"] = backend
        if workspace_kind:
            payload["workspace_kind"] = workspace_kind
        project_path_id = self._path_id(project_id, "project_id")
        return self._request("POST", f"/v1/projects/{project_path_id}/workers", json_body=payload)

    def find_or_resume_worker(
        self,
        *,
        project_id: str,
        owner_id: str | None,
        name: str,
        role: str,
        alias: str,
        profile: str = "codex-cli",
        backend: str | None = None,
        execution_mode: str | None = None,
        resource_class: Literal["standard", "light"] = "standard",
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict[str, Any] | None = None,
        start_synchronously: bool = True,
        workspace_kind: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "owner_id": self._owner_id(owner_id),
            "name": name,
            "role": role,
            "profile": profile,
            "execution_mode": _resolve_execution_mode(execution_mode),
            "resource_class": resource_class,
            "alias": alias,
            "workspace_root": workspace_root,
            "bootstrap_profile": bootstrap_profile,
            "bootstrap_bundle": bootstrap_bundle,
            "start_synchronously": start_synchronously,
        }
        if backend:
            payload["backend"] = backend
        if workspace_kind:
            payload["workspace_kind"] = workspace_kind
        project_path_id = self._path_id(project_id, "project_id")
        return self._request("POST", f"/v1/projects/{project_path_id}/workers/find-or-resume", json_body=payload)

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("GET", f"/v1/workers/{worker_path_id}")

    def worker_live(self, worker_id: str) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("GET", f"/v1/workers/{worker_path_id}/live")

    def list_artifacts(self, worker_id: str) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("GET", f"/v1/workers/{worker_path_id}/artifacts")

    def worker_runs(self, worker_id: str) -> list[dict[str, Any]]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("GET", f"/v1/workers/{worker_path_id}/runs").get("items", [])

    def worker_events(self, worker_id: str) -> list[dict[str, Any]]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("GET", f"/v1/workers/{worker_path_id}/events").get("items", [])

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict[str, Any] | None = None,
        continuation_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"instruction": instruction}
        if effort:
            payload["effort"] = effort
        if bootstrap_bundle is not None:
            payload["bootstrap_bundle"] = bootstrap_bundle
        if continuation_context is not None:
            payload["continuationContext"] = continuation_context
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("POST", f"/v1/workers/{worker_path_id}/assign", json_body=payload)

    def send_message(self, worker_id: str, message: str) -> dict[str, Any]:
        clean_worker_id = str(worker_id or "").strip()
        clean_message = str(message or "").strip()
        if not clean_worker_id:
            raise ValueError("worker_id is required to send a worker message")
        if not clean_message:
            raise ValueError("message is required to send a worker message")
        worker_path_id = self._path_id(clean_worker_id, "worker_id")
        return self._request("POST", f"/v1/workers/{worker_path_id}/message", json_body={"message": clean_message})

    def schedule_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        run_at: str | None = None,
        schedule_text: str | None = None,
        delay_seconds: int | None = None,
        bootstrap_bundle: dict[str, Any] | None = None,
        file_upload_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"instruction": instruction}
        if run_at:
            payload["run_at"] = run_at
        if schedule_text:
            payload["schedule_text"] = schedule_text
        if delay_seconds is not None:
            payload["delay_seconds"] = delay_seconds
        if bootstrap_bundle is not None:
            payload["bootstrap_bundle"] = bootstrap_bundle
        if file_upload_ids is not None:
            payload["file_upload_ids"] = file_upload_ids
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("POST", f"/v1/workers/{worker_path_id}/schedule", json_body=payload)

    def worker_schedules(self, worker_id: str, include_done: bool = False) -> list[dict[str, Any]]:
        suffix = "?include_done=true" if include_done else ""
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("GET", f"/v1/workers/{worker_path_id}/schedules{suffix}").get("items", [])

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        schedule_path_id = self._path_id(schedule_id, "schedule_id")
        return self._request("GET", f"/v1/schedules/{schedule_path_id}")

    def create_recurring_schedule(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request(
            "POST",
            f"/v1/workers/{worker_path_id}/recurring-schedules",
            json_body=payload,
        )

    def recurring_schedules(
        self,
        worker_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        suffix = "?include_inactive=true" if include_inactive else ""
        if worker_id:
            worker_path_id = self._path_id(worker_id, "worker_id")
            path = f"/v1/workers/{worker_path_id}/recurring-schedules{suffix}"
        else:
            path = f"/v1/recurring-schedules{suffix}"
        return self._request("GET", path).get("items", [])

    def recurring_schedule_occurrences(
        self,
        definition_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        definition_path_id = self._path_id(definition_id, "definition_id")
        bounded_limit = max(1, min(int(limit), 100))
        return self._request(
            "GET",
            f"/v1/recurring-schedules/{definition_path_id}/occurrences?limit={bounded_limit}",
        ).get("items", [])

    def update_recurring_schedule(
        self,
        definition_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        definition_path_id = self._path_id(definition_id, "definition_id")
        return self._request(
            "PATCH",
            f"/v1/recurring-schedules/{definition_path_id}",
            json_body=payload,
        )

    def deactivate_recurring_schedule(self, definition_id: str) -> dict[str, Any]:
        definition_path_id = self._path_id(definition_id, "definition_id")
        return self._request(
            "POST",
            f"/v1/recurring-schedules/{definition_path_id}/deactivate",
        )

    def retire_recurring_schedule(self, definition_id: str) -> dict[str, Any]:
        definition_path_id = self._path_id(definition_id, "definition_id")
        return self._request("DELETE", f"/v1/recurring-schedules/{definition_path_id}")

    def run_recurring_schedule_now(
        self,
        definition_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        definition_path_id = self._path_id(definition_id, "definition_id")
        return self._request(
            "POST",
            f"/v1/recurring-schedules/{definition_path_id}/run-now",
            json_body={"idempotency_key": idempotency_key},
        )

    def native_control(self, worker_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("POST" if payload is not None else "GET", f"/v1/workers/{worker_path_id}/native-control", json_body=payload)

    def lifecycle(self, worker_id: str, action: str) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        clean_action = str(action or "").strip()
        if clean_action not in {"pause", "resume", "interrupt", "terminate"}:
            raise ValueError("action must be a supported worker lifecycle action")
        return self._request("POST", f"/v1/workers/{worker_path_id}/{clean_action}")

    def desktop_action(self, worker_id: str, action: str, url: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action}
        if url:
            payload["url"] = url
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("POST", f"/v1/workers/{worker_path_id}/desktop-action", json_body=payload)

    def takeover(self, worker_id: str) -> dict[str, Any]:
        worker_path_id = self._path_id(worker_id, "worker_id")
        return self._request("GET", f"/v1/workers/{worker_path_id}/takeover")

    def get_run(self, run_id: str) -> dict[str, Any]:
        run_path_id = self._path_id(run_id, "run_id")
        return self._request("GET", f"/v1/runs/{run_path_id}")

    def metrics(self) -> dict[str, Any]:
        return self._request("GET", "/v1/metrics/summary")


    def _account_headers(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        load_viventium_runtime_env({"VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET"})
        secret = str(
            os.environ.get("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET") or ""
        ).strip()
        assertion = mint_service_assertion(
            secret,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        headers = {SERVICE_ASSERTION_HEADER: assertion}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key).strip()
        return headers

    def create_delegation(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/delegations",
            json_body=payload,
            extra_headers=self._account_headers(
                tenant_id=tenant_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
            ),
        )

    def list_active_work(
        self,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/v1/active-work",
            extra_headers=self._account_headers(tenant_id=tenant_id, owner_id=owner_id),
        )
        if not isinstance(response, dict):
            return {"snapshot": "unavailable", "work": [], "overflowCount": 0}
        return {
            "snapshot": str(response.get("snapshot") or "fresh"),
            "work": list(response.get("work", [])),
            "overflowCount": int(response.get("overflowCount") or 0),
            **({"cursor": response["cursor"]} if response.get("cursor") else {}),
        }

    def get_active_work(
        self,
        work_ref: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        ref = self._path_id(work_ref, "work_ref")
        return self._request(
            "GET",
            f"/v1/work/{ref}",
            extra_headers=self._account_headers(tenant_id=tenant_id, owner_id=owner_id),
        )

    def active_work_action(
        self,
        work_ref: str,
        *,
        tenant_id: str,
        owner_id: str,
        action: str,
        instruction: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        ref = self._path_id(work_ref, "work_ref")
        payload: dict[str, Any] = {
            "action": action,
            "idempotencyKey": idempotency_key,
        }
        if instruction is not None:
            payload["instruction"] = instruction
        return self._request(
            "POST",
            f"/v1/work/{ref}/actions",
            json_body=payload,
            extra_headers=self._account_headers(
                tenant_id=tenant_id,
                owner_id=owner_id,
            ),
        )


@worker_prompt_layer_producer("tool_schemas")
def create_mcp_server(
    *,
    base_url: str = DEFAULT_BASE_URL,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    api_client: WorkersProjectsApiClient | None = None,
) -> FastMCP:
    from .workspace_file_mcp import WorkspaceFilesMcpClient, validate_file_transport

    client = api_client or WorkersProjectsApiClient(base_url=base_url)
    oauth = oauth_from_env()
    if multi_user_security_enabled() and oauth is None:
        raise McpOAuthConfigurationError(
            "GlassHive multi-user MCP requires configured OAuth issuer and public URL"
        )
    recent_dispatches: dict[tuple[str, str, str, str, str], RecentDispatchContext] = {}
    recent_dispatches_lock = threading.RLock()
    server = GlassHiveFastMCP(
        name="glass-hive",
        instructions=glasshive_workers_server_instructions(),
        host=host,
        port=port,
        streamable_http_path="/mcp",
        transport_security=_mcp_transport_security_settings(host, port),
        token_verifier=oauth[0] if oauth else None,
        auth=oauth[1] if oauth else None,
    )

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def mcp_health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "release": release_provenance()})

    def _recent_dispatch_ttl_seconds() -> float:
        try:
            configured = float(os.environ.get("WPR_MCP_RECENT_DISPATCH_TTL_SEC", "14400") or "14400")
        except ValueError:
            configured = 14400.0
        return max(60.0, configured)

    def _recent_dispatch_max_entries() -> int:
        try:
            configured = int(os.environ.get("WPR_MCP_RECENT_DISPATCH_MAX_ENTRIES", "1024") or "1024")
        except ValueError:
            configured = 1024
        return max(16, configured)

    def _prune_recent_dispatches() -> None:
        with recent_dispatches_lock:
            ttl = _recent_dispatch_ttl_seconds()
            now = time.monotonic()
            for key, context in list(recent_dispatches.items()):
                if now - context.created_monotonic > ttl:
                    recent_dispatches.pop(key, None)
            max_entries = _recent_dispatch_max_entries()
            if len(recent_dispatches) <= max_entries:
                return
            oldest = sorted(
                recent_dispatches.items(),
                key=lambda item: item[1].created_monotonic,
            )
            for key, _context in oldest[: len(recent_dispatches) - max_entries]:
                recent_dispatches.pop(key, None)

    def _recent_dispatch_scope_keys(
        *,
        owner_id: str | None = None,
        for_remember: bool = False,
    ) -> list[tuple[str, str, str, str, str]]:
        headers = _request_headers()
        if _enterprise_mode_enabled():
            tenant_id, user_id = _enterprise_request_scope()
            tenant_values = [tenant_id]
            user_values = [user_id]
            mode = "enterprise"
        else:
            tenant_values = [_header_value(headers, HEADER_TENANT_ID) or "local"]
            header_user = _header_value(headers, HEADER_USER_ID)
            default_user = DEFAULT_OWNER_ID
            explicit_owner = _sanitize_context_value(owner_id)
            primary_user = header_user or default_user or explicit_owner or "local"
            user_values = [primary_user]
            if for_remember and not header_user and not default_user:
                for fallback_user in (explicit_owner, "local"):
                    if fallback_user and fallback_user not in user_values:
                        user_values.append(fallback_user)
            mode = "local"

        conversation_id = _header_value(headers, HEADER_CONVERSATION_ID)
        if mode == "enterprise" and for_remember and not conversation_id:
            return []
        keys: list[tuple[str, str, str, str, str]] = []
        for tenant_id in tenant_values:
            for user_id in user_values:
                if conversation_id:
                    keys.append((mode, tenant_id, user_id, "conversation", conversation_id))
                else:
                    keys.append((mode, tenant_id, user_id, "user", ""))
        return keys

    def _remember_recent_dispatch_context(
        *,
        worker: dict[str, Any],
        project_id: str,
        run: dict[str, Any],
    ) -> None:
        run_id = str(run.get("run_id") or "").strip()
        worker_id = str(worker.get("worker_id") or run.get("worker_id") or "").strip()
        resolved_project_id = str(project_id or worker.get("project_id") or run.get("project_id") or "").strip()
        if not run_id or not worker_id:
            return
        context = RecentDispatchContext(
            run_id=run_id,
            worker_id=worker_id,
            project_id=resolved_project_id,
            created_monotonic=time.monotonic(),
        )
        with recent_dispatches_lock:
            _prune_recent_dispatches()
            for key in _recent_dispatch_scope_keys(owner_id=str(worker.get("owner_id") or ""), for_remember=True):
                recent_dispatches[key] = context

    def _resolve_recent_dispatch_context() -> RecentDispatchContext | None:
        with recent_dispatches_lock:
            _prune_recent_dispatches()
            for key in _recent_dispatch_scope_keys(for_remember=False):
                context = recent_dispatches.get(key)
                if context:
                    return context
        return None

    def _provider_account_selection(
        *,
        profile: str,
        policy: str | None,
        account_id: str | None,
    ) -> dict[str, str] | None:
        requested_policy = str(policy or "").strip().lower()
        requested_account_id = str(account_id or "").strip()
        if not requested_policy and not requested_account_id:
            return None
        resolved_policy = requested_policy or "personal_required"
        if resolved_policy not in {
            "legacy",
            "personal_preferred",
            "personal_required",
        }:
            raise ValueError(
                "provider_account_policy must be legacy, personal_preferred, or personal_required"
            )
        if resolved_policy == "legacy":
            if requested_account_id:
                raise ValueError(
                    "Deployment account policy cannot include a personal account id"
                )
            return {"policy": "legacy"}
        profile_providers = {
            "codex-cli": {"codex", "openai"},
            "claude-code": {"claude", "anthropic"},
            "grok-build": {"grok", "xai"},
        }.get(profile)
        if profile_providers is None:
            raise ValueError(
                "Personal worker accounts are available only for Codex and Claude Code profiles"
            )
        accounts = list(client.provider_accounts())
        matching = [
            account
            for account in accounts
            if str(account.get("provider") or "").strip().lower()
            in profile_providers
        ]
        selected = next(
            (
                account
                for account in matching
                if str(account.get("account_id") or "") == requested_account_id
            ),
            None,
        ) if requested_account_id else next(
            (
                account
                for account in matching
                if bool(account.get("is_default"))
                and str(account.get("status") or "").lower() == "ready"
            ),
            None,
        )
        if requested_account_id and selected is None:
            raise ValueError("Selected provider account is unavailable for this worker profile")
        if selected is not None and str(selected.get("status") or "").lower() != "ready":
            raise ValueError("Selected provider account is not ready")
        if selected is None and resolved_policy == "personal_required":
            raise ValueError(
                "Personal-only policy requires a ready selected or default provider account"
            )
        selection = {"policy": resolved_policy}
        if selected is not None:
            selection["account_id"] = str(selected.get("account_id") or "")
        return selection

    @server.tool(
        name="projects_list",
        title="List Projects",
        description=(
            "List current projects from the standalone Workers & Projects runtime. Use for explicit status, audit, or resume requests. "
            "For a fresh delegation, prefer worker_delegate_once instead of listing every project or chaining low-level tools. "
            "Returns project records with project_id, owner, title, goal, and runtime metadata when available."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def projects_list(owner_id: str | None = None) -> list[dict[str, Any]]:
        return client.list_projects(owner_id=owner_id)

    @server.tool(
        name="project_create",
        title="Create Project",
        description=(
            "Low-level compatibility tool to create a GlassHive project record with title and goal. Use only when the user explicitly asks to set up a reusable project record "
            "or when a diagnostic/orchestration workflow already chose low-level project and worker IDs. Do not use this for normal LibreChat task delegation, file work, browser work, "
            "desktop work, or workspace launch; prefer workspace_launch, then worker_delegate_once. Requires title and goal, optionally owner_id and default_worker_profile. "
            "Returns the project record including project_id for later worker or run tools. If project creation fails, surface the blocker instead of inventing a project id."
        ),
        structured_output=True,
    )
    def project_create(
        title: str,
        goal: str,
        owner_id: str | None = None,
        default_worker_profile: str = "",
    ) -> dict[str, Any]:
        return client.create_project(
            owner_id=_request_owner_id(owner_id),
            title=title,
            goal=goal,
            default_worker_profile=default_worker_profile.strip() or _configured_default_worker_profile(),
        )

    @server.tool(
        name="project_allowed_ai_get",
        title="Get Project Allowed AI",
        description=(
            "Read the authenticated owner's versioned Allowed AI project policy. "
            "The response includes the requested policy, independent revision, and effective ceiling."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def project_allowed_ai_get(project_id: str) -> dict[str, Any]:
        return client.allowed_ai_policy("project", project_id)

    @server.tool(
        name="project_allowed_ai_options",
        title="List Project Allowed AI Options",
        description=(
            "List descriptor-only harness, exact model, and owner connection options for a project. "
            "The catalog never returns credentials or secret locators."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def project_allowed_ai_options(project_id: str) -> dict[str, Any]:
        return client.allowed_ai_options("project", project_id)

    @server.tool(
        name="project_allowed_ai_set",
        title="Set Project Allowed AI",
        description=(
            "Save the authenticated owner's exact Allowed AI project checkmarks with an expected "
            "revision. A stale revision is rejected so settings changes cannot overwrite each other."
        ),
        structured_output=True,
    )
    def project_allowed_ai_set(
        project_id: str,
        expected_revision: int,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        return client.update_allowed_ai_policy(
            "project", project_id, expected_revision=expected_revision, policy=policy
        )

    @server.tool(
        name="workspace_allowed_ai_get",
        title="Get Workspace Allowed AI",
        description=(
            "Read the authenticated owner's versioned Allowed AI workspace policy. Workspace "
            "inheritance and the effective project ceiling are returned separately."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_allowed_ai_get(workspace_id: str) -> dict[str, Any]:
        return client.allowed_ai_policy("workspace", workspace_id)

    @server.tool(
        name="workspace_allowed_ai_options",
        title="List Workspace Allowed AI Options",
        description=(
            "List descriptor-only harness, exact model, and owner connection options for a workspace. "
            "The catalog never returns credentials or secret locators."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_allowed_ai_options(workspace_id: str) -> dict[str, Any]:
        return client.allowed_ai_options("workspace", workspace_id)

    @server.tool(
        name="workspace_allowed_ai_set",
        title="Set Workspace Allowed AI",
        description=(
            "Save the authenticated owner's exact Allowed AI workspace checkmarks with an expected "
            "revision. A workspace cannot widen its project's effective ceiling."
        ),
        structured_output=True,
    )
    def workspace_allowed_ai_set(
        workspace_id: str,
        expected_revision: int,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        return client.update_allowed_ai_policy(
            "workspace", workspace_id, expected_revision=expected_revision, policy=policy
        )

    @server.tool(
        name="workspace_preferences_get",
        title="Get GlassHive Preferences",
        description=(
            "Return the authenticated user's saved GlassHive defaults: preferred worker profile and "
            "per-profile effort settings. Use before changing defaults or when a user asks what worker "
            "GlassHive will use."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_preferences_get() -> dict[str, Any]:
        return client.get_preferences()

    @server.tool(
        name="workspace_preferences_set",
        title="Set GlassHive Preferences",
        description=(
            "Save the authenticated user's default GlassHive worker and effort preferences. Use this "
            "when the user says to make Codex, Claude Code, or OpenClaw their default, or asks future "
            "GlassHive runs to use a specific effort. Allowed Codex efforts: none, low, medium, "
            "high, xhigh; minimal is for explicitly allowlisted deployments only. Claude: max or xhigh. "
            "OpenClaw: high or max."
        ),
        structured_output=True,
    )
    def workspace_preferences_set(
        default_worker_profile: Annotated[
            str | None,
            Field(description="Optional default worker profile: codex-cli, claude-code, or openclaw-general. Empty clears the saved default."),
        ] = None,
        codex_reasoning_effort: Annotated[
            str | None,
            Field(description="Optional Codex default effort: none, low, medium, high, xhigh, or empty to use deployment default; use minimal only when the deployment explicitly allowlists it."),
        ] = None,
        claude_effort: Annotated[
            str | None,
            Field(description="Optional Claude Code default effort: low, medium, high, xhigh, max, or empty/default to use deployment default."),
        ] = None,
        openclaw_effort: Annotated[
            str | None,
            Field(description="Optional OpenClaw default effort: high, max, or empty/default to use deployment default."),
        ] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if default_worker_profile is not None:
            payload["default_worker_profile"] = default_worker_profile
        if codex_reasoning_effort is not None:
            payload["codex_reasoning_effort"] = codex_reasoning_effort
        if claude_effort is not None:
            payload["claude_effort"] = claude_effort
        if openclaw_effort is not None:
            payload["openclaw_effort"] = openclaw_effort
        return client.update_preferences(payload)

    @server.tool(
        name="workspace_list",
        title="Find My Workspaces",
        description=(
            "List, search, and rediscover the authenticated user's persisted GlassHive workspaces by "
            "human name, tag, favorite state, or workspace kind. Use when the user asks to browse/list, "
            "or when a workspace name is missing or ambiguous. Do not call this before workspace_launch "
            "when the user already supplied one exact saved human name; workspace_launch resolves it. "
            "Returns an opaque next_cursor for pagination."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_list(
        search: str = "",
        kind: str = "named",
        tags: list[str] | None = None,
        favorite: bool | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        return client.workspace_catalog(
            search=search,
            kind=kind,
            tags=tags,
            favorite=favorite,
            cursor=cursor,
            limit=max(1, min(int(limit), 100)),
        )

    @server.tool(
        name="workspace_shared_create",
        title="Create Shared Workspace",
        description=(
            "Create an owner-scoped shared Linux container workspace for a project. "
            "The common file placement is the default; member_private is an explicit separate choice. "
            "Creation only saves metadata and returns typed runtime readiness. It does not start a blank worker. "
            "Shared host and OpenClaw execution remain unavailable when the runtime does not advertise them."
        ),
        structured_output=True,
    )
    def workspace_shared_create(
        project_id: str,
        execution_mode: Literal["docker", "host"] = "docker",
        file_placement: Literal["common", "member_private"] = "common",
    ) -> dict[str, Any]:
        return client.create_execution_workspace(
            project_id,
            execution_mode=execution_mode,
            file_placement=file_placement,
        )

    @server.tool(
        name="workspace_shared_add_member",
        title="Add Shared Workspace Member",
        description=(
            "Add one owner-scoped native Codex, Claude Code, or Grok Build member to an existing shared workspace. "
            "The member is prepared and paused; compute starts only when a run is queued. "
            "Optionally select a ready personal provider account and native reasoning effort. "
            "The runtime validates account ownership, profile compatibility, capacity, and recovery before admission."
        ),
        structured_output=True,
    )
    def workspace_shared_add_member(
        workspace_id: str,
        name: str = "",
        role: str = "member",
        profile: str | None = None,
        effort: str | None = None,
        provider_account_policy: Literal[
            "legacy", "personal_preferred", "personal_required"
        ] | None = None,
        provider_account_id: str | None = None,
    ) -> dict[str, Any]:
        return client.add_execution_workspace_member(
            workspace_id,
            name=name,
            role=role,
            profile=profile,
            effort=effort,
            provider_account_policy=provider_account_policy,
            provider_account_id=provider_account_id,
        )

    @server.tool(
        name="workspace_shared_members",
        title="List Shared Workspace Members",
        description=(
            "Read the owner-scoped shared workspace roster, runtime readiness, selected safe provider-account policy, "
            "and exact model/effort metadata. Credential paths and bootstrap contents are never returned."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_shared_members(workspace_id: str) -> dict[str, Any]:
        return client.execution_workspace_members(workspace_id)

    @server.tool(
        name="workspace_rename",
        title="Rename Workspace",
        description="Rename one authenticated-user workspace using its worker_id and a clear human name.",
        structured_output=True,
    )
    def workspace_rename(worker_id: str, name: str) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("name is required")
        return client.update_workspace(worker_id, {"name": clean_name})

    @server.tool(
        name="workspace_duplicate",
        title="Duplicate Workspace",
        description=(
            "Create a separate persisted workspace from an existing workspace. GlassHive copies only "
            "approved project files; credentials, account homes, browser sessions, and VCS internals are never copied."
        ),
        structured_output=True,
    )
    def workspace_duplicate(
        worker_id: str,
        idempotency_key: Annotated[str, Field(min_length=8, max_length=128)],
        name: str = "",
        file_upload_ids: FileUploadIdsParam = None,
    ) -> dict[str, Any]:
        validate_file_transport(file_upload_ids)
        duplicated = client.duplicate_workspace(
            worker_id,
            idempotency_key=idempotency_key,
            name=str(name or "").strip(),
        )
        if file_upload_ids:
            destination = str((duplicated.get("workspace") or {}).get("worker_id") or "")
            WorkspaceFilesMcpClient(client).bind(destination, file_upload_ids, idempotency_key)
        return duplicated

    @server.tool(
        name="workspace_template_list",
        title="List My Workspace Templates",
        description=(
            "List the authenticated user's immutable, versioned workspace templates and their exact "
            "Library references. A same-owner opaque provider-account reference may be retained and is "
            "revalidated before use; source files, credentials, provider homes, grants, schedules, and audit "
            "history are never included."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_template_list() -> list[dict[str, Any]]:
        return client.workspace_templates()

    @server.tool(
        name="workspace_template_save",
        title="Save Workspace as Template",
        description=(
            "Save one owned workspace as a new immutable template version. Only reusable project intent, "
            "safe worker settings, and exact Library version/hash/scope references are retained."
        ),
        structured_output=True,
    )
    def workspace_template_save(
        worker_id: str,
        name: str,
        description: str = "",
        lineage_id: str = "",
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("name is required")
        return client.save_workspace_template(
            worker_id,
            name=clean_name,
            description=str(description or "").strip(),
            lineage_id=str(lineage_id or "").strip(),
        )

    @server.tool(
        name="workspace_template_instantiate",
        title="Start Workspace from Template",
        description=(
            "Create a fresh paused workspace from one exact immutable template without rereading its source. "
            "Returns Library capabilities that require fresh human approval; retry with the same idempotency key."
        ),
        structured_output=True,
    )
    def workspace_template_instantiate(
        template_id: str,
        idempotency_key: Annotated[str, Field(min_length=8, max_length=128)],
        name: str = "",
    ) -> dict[str, Any]:
        return client.instantiate_workspace_template(
            template_id,
            idempotency_key=idempotency_key,
            name=str(name or "").strip(),
        )

    @server.tool(
        name="worker_accounts_list",
        title="List My Worker Accounts",
        description=(
            "List the authenticated user's Codex, Claude, or API/enterprise worker-account metadata and "
            "connection status. Credential bytes and secret locators are never returned."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def worker_accounts_list() -> list[dict[str, Any]]:
        return client.provider_accounts()

    @server.tool(
        name="worker_account_connect",
        title="Connect My Worker Account",
        description=(
            "Create owner-scoped Codex or Claude worker-account metadata. Subscription accounts must "
            "then use worker_account_setup_start; API/enterprise routes reuse the user's managed "
            "connected-account secret without copying it into GlassHive."
        ),
        structured_output=True,
    )
    def worker_account_connect(
        provider: Literal["codex", "claude"],
        label: Annotated[str, Field(min_length=1, max_length=160)],
        auth_method: Literal["subscription", "api_key", "enterprise_route"] = "subscription",
        make_default: bool = False,
    ) -> dict[str, Any]:
        return client.create_provider_account(
            provider=provider,
            label=label,
            auth_method=auth_method,
            make_default=make_default,
        )

    @server.tool(
        name="worker_account_setup_start",
        title="Start My Worker Account Sign-in",
        description=(
            "Start the provider-native sign-in for one owner-scoped subscription account and return "
            "its bounded device/browser instructions. Call worker_account_test to check completion."
        ),
        structured_output=True,
    )
    def worker_account_setup_start(account_id: str) -> dict[str, Any]:
        return client.start_provider_account_setup(account_id)

    @server.tool(
        name="worker_account_test",
        title="Test My Worker Account",
        description=(
            "Recheck native subscription status or the managed inference-broker binding for one "
            "authenticated user's worker account without returning credentials."
        ),
        structured_output=True,
    )
    def worker_account_test(account_id: str) -> dict[str, Any]:
        return client.verify_provider_account(account_id)

    @server.tool(
        name="worker_account_disconnect",
        title="Disconnect My Worker Account",
        description=(
            "Disconnect one authenticated user's worker account, release its active leases, and remove "
            "only that account's isolated provider credential home."
        ),
        structured_output=True,
    )
    def worker_account_disconnect(account_id: str) -> dict[str, Any]:
        return client.disconnect_provider_account(account_id)

    @server.tool(
        name="worker_account_forget",
        title="Forget My Disconnected Worker Account",
        description=(
            "Remove only the authenticated user's disconnected account metadata after its isolated "
            "provider credentials have already been removed."
        ),
        structured_output=True,
    )
    def worker_account_forget(account_id: str) -> dict[str, Any]:
        return client.forget_provider_account(account_id)

    @server.tool(
        name="connections_list",
        title="List My Connections",
        description=(
            "List brokered connected services available to the authenticated user, including their "
            "approved scopes and readiness. Use this before proposing a workspace capability change."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def connections_list() -> list[dict[str, Any]]:
        return client.connections()

    @server.tool(
        name="library_list",
        title="Browse GlassHive Library",
        description=(
            "List administrator-curated skills, tools, and connector manifests that may be enabled for "
            "workspaces. This does not install arbitrary model-supplied code."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def library_list() -> list[dict[str, Any]]:
        return client.library()

    @server.tool(
        name="library_manifest_propose",
        title="Propose a GlassHive Library Manifest",
        description=(
            "Submit a complete non-secret, hash-pinned Library manifest for administrator review. "
            "This never publishes, installs, confirms, or grants the proposal. Arbitrary shell "
            "activation and credential-bearing manifests are rejected."
        ),
        structured_output=True,
    )
    def library_manifest_propose(manifest: dict[str, Any]) -> dict[str, Any]:
        return client.propose_library_manifest(manifest)

    @server.tool(
        name="workspace_capability_prepare",
        title="Prepare Workspace Capability",
        description=(
            "Prepare, but do not apply, one curated Library item for a workspace. Return a browser "
            "confirmation URL that the authenticated human must explicitly review and approve. "
            "Connected services use their brokered workspace bundle; personal worker accounts use the "
            "workspace execution policy. Never claim success before confirmation completes."
        ),
        structured_output=True,
    )
    def workspace_capability_prepare(
        worker_id: str,
        library_id: str,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_library_id = str(library_id or "").strip()
        if not normalized_library_id:
            raise ValueError("Choose one library_id")
        payload: dict[str, Any] = {"library_id": normalized_library_id}
        payload["scopes"] = [str(scope).strip() for scope in (scopes or []) if str(scope).strip()]
        pending = client.create_pending_change(
            {
                "change_type": "library_enable",
                "target_id": worker_id,
                "payload": payload,
            }
        )
        confirmation_token = str(pending.pop("confirmation_token", "") or "")
        if not confirmation_token:
            raise RuntimeError("GlassHive did not return a human confirmation token")
        change_id = str(pending.get("change_id") or "").strip()
        confirmation_url = (
            f"{operator_base_url()}/confirm-change"
            f"#change_id={quote(change_id, safe='')}&token={quote(confirmation_token, safe='')}"
        )
        return {**pending, "confirmation_url": confirmation_url, "requires_human_confirmation": True}

    @server.tool(
        name="workspace_capability_upgrade_prepare",
        title="Prepare Workspace Capability Upgrade",
        description=(
            "Propose a newer version of one already approved Library capability. This cannot widen "
            "the current workspace scopes and never publishes or applies content itself. The "
            "authenticated human must review and confirm the immutable upgrade in the browser."
        ),
        structured_output=True,
    )
    def workspace_capability_upgrade_prepare(
        worker_id: str,
        library_id: str,
        replaces_grant_id: str,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_library_id = str(library_id or "").strip()
        normalized_grant_id = str(replaces_grant_id or "").strip()
        if not normalized_library_id or not normalized_grant_id:
            raise ValueError("Choose the newer library_id and current replaces_grant_id")
        pending = client.create_pending_change(
            {
                "change_type": "library_upgrade",
                "target_id": worker_id,
                "payload": {
                    "library_id": normalized_library_id,
                    "replaces_grant_id": normalized_grant_id,
                    "scopes": [
                        str(scope).strip() for scope in (scopes or []) if str(scope).strip()
                    ],
                },
            }
        )
        confirmation_token = str(pending.pop("confirmation_token", "") or "")
        if not confirmation_token:
            raise RuntimeError("GlassHive did not return a human confirmation token")
        change_id = str(pending.get("change_id") or "").strip()
        confirmation_url = (
            f"{operator_base_url()}/confirm-change"
            f"#change_id={quote(change_id, safe='')}&token={quote(confirmation_token, safe='')}"
        )
        return {**pending, "confirmation_url": confirmation_url, "requires_human_confirmation": True}

    @server.tool(
        name="workspace_provider_account_prepare",
        title="Prepare Workspace Account Switch",
        description=(
            "Prepare a user-scoped worker-account selection for future runs in one workspace. "
            "This never changes queued or running work and returns a browser confirmation URL "
            "that the authenticated human must explicitly approve. Use policy legacy with no "
            "account_id for the deployment-managed account, or personal_required with a ready "
            "account_id to opt out of global credentials."
        ),
        structured_output=True,
    )
    def workspace_provider_account_prepare(
        worker_id: str,
        policy: Literal["legacy", "personal_required", "personal_preferred"],
        account_id: str = "",
    ) -> dict[str, Any]:
        normalized_policy = str(policy or "").strip().lower()
        normalized_account_id = str(account_id or "").strip()
        if normalized_policy == "legacy" and normalized_account_id:
            raise ValueError("legacy policy cannot include account_id")
        if normalized_policy != "legacy" and not normalized_account_id:
            raise ValueError("a ready personal account_id is required")
        pending = client.create_pending_change(
            {
                "change_type": "workspace_provider_account",
                "target_id": worker_id,
                "payload": {
                    "policy": normalized_policy,
                    **({"account_id": normalized_account_id} if normalized_account_id else {}),
                },
            }
        )
        confirmation_token = str(pending.pop("confirmation_token", "") or "")
        if not confirmation_token:
            raise RuntimeError("GlassHive did not return a human confirmation token")
        change_id = str(pending.get("change_id") or "").strip()
        confirmation_url = (
            f"{operator_base_url()}/confirm-change"
            f"#change_id={quote(change_id, safe='')}&token={quote(confirmation_token, safe='')}"
        )
        return {**pending, "confirmation_url": confirmation_url, "requires_human_confirmation": True}

    @server.tool(
        name="workspace_capabilities_list",
        title="List Workspace Capabilities",
        description="List the authenticated user's active capability grants for one workspace.",
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_capabilities_list(worker_id: str) -> list[dict[str, Any]]:
        return client.workspace_grants(worker_id)

    @server.tool(
        name="workspace_capability_remove",
        title="Remove Workspace Capability",
        description=(
            "Remove the newest active capability from a workspace. GlassHive refuses removal if a newer "
            "capability or later workspace configuration change would make rollback unsafe."
        ),
        structured_output=True,
    )
    def workspace_capability_remove(worker_id: str, grant_id: str) -> dict[str, Any]:
        return client.revoke_workspace_grant(worker_id, grant_id)

    @server.tool(
        name="workspace_activity",
        title="Workspace Activity",
        description="List recent user-scoped workspace events for discovery, audit, and resume decisions.",
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_activity(limit: Annotated[int, Field(ge=1, le=200)] = 50) -> list[dict[str, Any]]:
        return client.activity(limit=limit)

    @server.tool(
        name="project_get",
        title="Get Project",
        description=(
            "Fetch a single GlassHive project by project_id. Use for explicit inspection, status, audit, or resume flows when the project id is already known. "
            "Do not use as the first step for ordinary fresh delegation; prefer worker_delegate_once. Returns the project record and high-signal ownership/title/goal fields."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def project_get(project_id: str) -> dict[str, Any]:
        return client.get_project(project_id)

    @server.tool(
        name="worker_delegate_once",
        title="Delegate Task",
        description=(
            "One-call task delegation for GlassHive when the caller already has a precise title and instruction. For normal user-facing launches, prefer workspace_launch because it mirrors the documented GlassHive UI fields. Use this for new host/browser/desktop/local-file tasks "
            "instead of manually listing projects and chaining project_create, worker_create, and worker_run. "
            "It creates a human-named project when project_id is omitted, creates a fresh worker by default, "
            "and finds/resumes by alias only when reuse_existing_workspace is true. "
            "merges optional callback/upload context, queues the run, and returns one clean non-blocking dispatch result. "
            "Callbacks are optional; plain LibreChat or standalone deployments can use workspace_status for non-blocking checks and workspace_wait when the user explicitly wants to wait. "
            "For attached/uploaded-file tasks, use uploaded_files when the file content is visible in the current model context, for example [{'filename':'brief.txt','text':'...'}]. "
            "If the chat model cannot read the file body, still call this with file names/references and requested transformation so GlassHive can use request upload metadata supplied by the host or return an honest blocker. "
            "Write your own short acknowledgement and include the View / Steer link when view_steer_url is present. Use result_tools for later user status/result questions; request diagnostics only when raw ids are needed. "
            "Do not shorten, summarize, paraphrase, or water down the user's request: pass the full available brief, background, constraints, examples, links, file references, exclusions, and success criteria through instruction/goal/bootstrap context so the worker receives the full picture. "
            "For connected-account facts or actions, prefer MCP/tools over browser or computer UI when available. Connected-account read authorization comes from the host-signed broker grant; connected_account_content_intent is only a compatibility hint for missing-broker warnings, not a required authorization switch. "
            "Pass MCP/tool availability as context, not as a made-up project goal or success criterion, unless the user explicitly asked to prove tool usage. "
            f"{HIGH_EFFORT_SELECTION_GUIDANCE} "
            f"{HOST_SIDE_ORCHESTRATION_GUIDANCE} "
            "Preserve the user's requested final-answer format in the instruction, especially short/exact-answer constraints. "
            "Use delegation_audit to self-check the dispatched instruction, but do not expose it unless diagnostics were requested. "
            "Omit execution_mode to use the configured default. Set execution_mode='host' only when GlassHive instructions say host-native workers are enabled and the task depends on the user's real computer/session; set execution_mode='docker' for isolated sandbox/Codex Workspace/workstation work, disposable browsers, or risky untrusted browsing. "
            "If this returns status='blocked' with a failure_class such as runtime_dependency_missing, do not say work is running; explain the blocker and retry only with an available profile or sandbox mode that matches the user's request. "
            "Set expose_diagnostics=true only when the user explicitly asked for run/project/worker diagnostics."
        ),
        structured_output=True,
    )
    def worker_delegate_once(
        title: str,
        instruction: str,
        ctx: Context,
        goal: str | None = None,
        project_id: str | None = None,
        owner_id: str | None = None,
        worker_name: str | None = None,
        worker_role: str | None = None,
        alias: str | None = None,
        reuse_existing_workspace: Annotated[
            bool,
            Field(
                description=(
                    "Set true only when the user explicitly asked to resume/reuse the named alias. "
                    "Leave false for fresh one-off tasks so stale worker history cannot slow or distort the result."
                )
            ),
        ] = False,
        favorite: Annotated[
            bool,
            Field(
                description=(
                    "Set true when the user explicitly asks to favorite or pin this reusable workspace. "
                    "GlassHive applies it before the run is queued."
                )
            ),
        ] = False,
        profile: ProfileParam = "",
        backend: BackendParam = None,
        execution_mode: ExecutionModeParam = None,
        resource_class: Annotated[
            Literal["standard", "light"],
            Field(
                description=(
                    "Worker memory class. Use light for explicitly requested small, bounded jobs; "
                    "omit for the standard class."
                )
            ),
        ] = "standard",
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        connected_account_content_intent: Annotated[
            bool,
            Field(
                description=(
                    "Optional compatibility hint for hosts that want GlassHive to warn when "
                    "connected-account content was requested but no complete broker grant/config "
                    "was supplied. Authorization comes only from a host-signed broker grant with "
                    "content-read scope; this flag alone does not unlock reads or writes."
                )
            ),
        ] = False,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        uploaded_files: UploadedFilesParam = None,
        file_upload_ids: FileUploadIdsParam = None,
        file_binding_key: FileBindingKeyParam = None,

        effort: Annotated[
            str | None,
            Field(
                description=(
                    "Optional per-run effort override. Codex accepts none/low/medium/high/xhigh; "
                    "minimal is for explicitly allowlisted deployments only. "
                    "Claude Code accepts default/low/medium/high/xhigh/max; OpenClaw accepts high/max. Omit to use the user's saved default. "
                    + HIGH_EFFORT_SELECTION_GUIDANCE
                )
            ),
        ] = None,
        provider_account_policy: Literal[
            "legacy", "personal_preferred", "personal_required"
        ] | None = None,
        provider_account_id: str | None = None,
        require_callback: bool = False,
        expose_diagnostics: bool = False,
    ) -> dict[str, Any]:
        signed_launch_payload = {
            "title": title,
            "instruction": instruction,
            "goal": goal,
            "project_id": project_id,
            "owner_id": owner_id,
            "worker_name": worker_name,
            "worker_role": worker_role,
            "alias": alias,
            "reuse_existing_workspace": reuse_existing_workspace,
            "profile": profile,
            "backend": backend,
            "execution_mode": execution_mode,
            "workspace_root": workspace_root,
            "bootstrap_profile": bootstrap_profile,
            "connected_account_content_intent": connected_account_content_intent,
            "effort": effort,
            "require_callback": require_callback,
            "expose_diagnostics": expose_diagnostics,
        }
        expose_diagnostics = _effective_diagnostics_requested(
            expose_diagnostics,
            tool_name="worker_delegate_once",
        )
        resolved_owner_id = _request_owner_id(owner_id)
        requested_execution_mode = str(execution_mode or "").strip()
        resolved_execution_mode = _resolve_execution_mode(execution_mode, allow_disabled_host=True)
        try:
            preferences = _normalize_preferences(client.get_preferences())
        except Exception:
            preferences = {}
        resolved_profile = _resolve_profile_from_preferences(profile, preferences)
        resolved_effort = _resolve_effort_for_profile(resolved_profile, effort, preferences)
        clean_title = title.strip() or "GlassHive task"
        clean_goal = (goal or clean_title).strip()
        clean_instruction = instruction.strip()
        if not clean_instruction:
            raise ValueError("instruction is required")
        explicit_goal = str(goal or "").strip()
        worker_instruction_body = clean_instruction
        if explicit_goal and explicit_goal != clean_instruction:
            worker_instruction_body = (
                f"{clean_instruction}\n\n"
                f"User-visible success condition:\n{explicit_goal}"
            )
        worker_instruction = _with_worker_host_side_orchestration_rule(worker_instruction_body)
        reusable_alias = _request_unscoped_alias(
            alias or _slugify_alias(resolved_profile, clean_title)
        )
        resolved_alias = reusable_alias if reuse_existing_workspace else _fresh_worker_alias(reusable_alias)
        blocked = _runtime_dependency_blocked_payload(
            profile=resolved_profile,
            execution_mode=resolved_execution_mode,
        )
        runtime_recovery: dict[str, Any] | None = None
        if _can_recover_blocked_host_dispatch_to_docker(
            blocked,
            requested_execution_mode=requested_execution_mode,
            resolved_execution_mode=resolved_execution_mode,
            workspace_root=workspace_root,
        ):
            runtime_recovery = {
                "from_execution_mode": "host",
                "to_execution_mode": "docker",
                "reason_class": blocked.get("failure_class"),
                "reason_summary": blocked.get("failure_user_message"),
            }
            resolved_execution_mode = "docker"
            blocked = _runtime_dependency_blocked_payload(
                profile=resolved_profile,
                execution_mode=resolved_execution_mode,
            )
        if blocked:
            return _blocked_dispatch_result(
                blocked,
                profile=resolved_profile,
                execution_mode=resolved_execution_mode,
                effort=resolved_effort,
                alias=resolved_alias,
            )

        bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json) or {}
        default_project_definition = _default_project_definition(
            title=clean_title,
            goal=clean_goal,
            instruction=clean_instruction,
        )
        if isinstance(bundle.get("viventium_delegation_packet"), dict):
            bundle["project_definition"] = default_project_definition
        else:
            bundle.setdefault("project_definition", default_project_definition)
        bundle = _merge_request_context(bundle)
        bundle, worker_instruction = _project_trusted_triggering_source_segments(
            bundle,
            worker_instruction,
            constraint_instruction=worker_instruction_body,
        )
        _validate_viventium_feelings_projection(
            bundle,
            required=isinstance(bundle.get("viventium_delegation_packet"), dict),
        )
        trusted_tenant_id, trusted_owner_id, trusted_storage_owner_id = (
            _trusted_request_upload_scope()
        )
        bundle = _merge_explicit_uploaded_files(
            bundle,
            uploaded_files,
            tenant_id=trusted_tenant_id,
            owner_id=trusted_owner_id,
            storage_owner_id=trusted_storage_owner_id,
        )
        validate_file_transport(file_upload_ids, uploaded_files, bundle)
        bundle = _apply_effort_to_bundle(bundle, profile=resolved_profile, effort=resolved_effort)
        provider_selection = _provider_account_selection(
            profile=resolved_profile,
            policy=provider_account_policy,
            account_id=provider_account_id,
        )
        if provider_selection is not None:
            bundle["provider_account"] = provider_selection
        bundle, worker_instruction = _apply_connected_account_intent_guard(
            bundle,
            worker_instruction,
            connected_account_content_intent=connected_account_content_intent,
        )
        worker_instruction = worker_instruction or clean_instruction
        callback_ready, missing_callback_fields = _callback_state(bundle, required=require_callback)
        if require_callback and not callback_ready:
            return {
                "status": "blocked",
                "acknowledgement_guidance": (
                    "Explain in your own voice that this cannot be background-dispatched yet, "
                    "because the callback context is incomplete. Name the missing callback fields "
                    "without exposing internal worker/run/project ids."
                ),
                "main_agent_next_action": (
                    "Write one short blocked-status reply in your own voice using "
                    "missing_callback_fields. Do not quote a canned template."
                ),
                "execution_mode": resolved_execution_mode,
                "profile": resolved_profile,
                "effort": resolved_effort,
                "alias": resolved_alias,
                "callback_ready": False,
                "missing_callback_fields": missing_callback_fields,
            }

        atomic_delegate = getattr(client, "create_delegation", None)
        if callable(atomic_delegate) and not project_id and not reuse_existing_workspace and not favorite:
            tenant_id, account_owner_id = _account_request_scope(resolved_owner_id)
            request_surface = _header_value(_request_headers(), HEADER_SURFACE).lower()
            origin_surface = (
                request_surface
                if request_surface in {"telegram", "voice", "web", "workbench", "scheduler"}
                else "web"
            )
            delegation_payload = {
                "title": clean_title,
                "goal": clean_goal,
                "instruction": worker_instruction,
                "profile": resolved_profile,
                "executionMode": resolved_execution_mode,
                "resourceClass": resource_class,
                "workerName": (worker_name or clean_title).strip(),
                "workerRole": (worker_role or "General intelligent worker").strip(),
                "workspaceRoot": workspace_root,
                "bootstrapProfile": bootstrap_profile,
                "bootstrapBundle": bundle,
                "originSurface": origin_surface,
            }
            if file_upload_ids:
                delegation_payload["fileUploadIds"] = file_upload_ids
            idempotency_key = _trusted_operation_idempotency_key(
                "ghd",
                tenant_id=tenant_id,
                owner_id=account_owner_id,
                payload=delegation_payload,
                signed_launch_payload=signed_launch_payload,
            )
            try:
                accepted = atomic_delegate(
                    tenant_id=tenant_id,
                    owner_id=account_owner_id,
                    idempotency_key=idempotency_key,
                    payload=delegation_payload,
                )
            except GlassHiveBlockedError as exc:
                return _blocked_dispatch_result(
                    exc.payload,
                    profile=resolved_profile,
                    execution_mode=resolved_execution_mode,
                    effort=resolved_effort,
                    alias=resolved_alias,
                )
            if file_upload_ids and accepted.get("acceptedFileUploadIds") != file_upload_ids:
                raise ValueError("Runtime did not acknowledge the exact stored file IDs; inspect the delegation before retrying")
            work_ref = str(accepted.get("workRef") or "").strip()
            if not work_ref:
                raise ValueError("GlassHive atomic delegation did not return workRef")
            view_steer_url = str(accepted.get("viewRef") or "").strip()
            operator_base = (
                os.environ.get("GLASSHIVE_OPERATOR_BASE_URL", "").strip()
                or os.environ.get("WPR_OPERATOR_BASE_URL", "").strip()
            ).rstrip("/")
            if view_steer_url.startswith("/") and operator_base:
                view_steer_url = f"{operator_base}{view_steer_url}"
            state = str(accepted.get("state") or "queued")
            result: dict[str, Any] = {
                "status": "dispatched",
                "work_ref": work_ref,
                "run_state": state,
                "resource_class": str(accepted.get("resourceClass") or resource_class),
                "callback_ready": callback_ready,
                "callback_delivery": (
                    "optional"
                    if callback_ready
                    else "not_configured_standalone_polling_available"
                ),
                **(
                    {"callback_delivery_deadline_seconds": _callback_delivery_deadline_seconds()}
                    if callback_ready
                    else {}
                ),
                "missing_callback_fields": missing_callback_fields,
                "idempotent_replay": bool(accepted.get("idempotentReplay")),
                "result_tools": {
                    "roster": "active_work_list",
                    "status": "active_work_list",
                    "control": "active_work_action",
                },
                "view_steer": _view_steer_link(
                    task=clean_title,
                    url=view_steer_url,
                    terminal=state.strip().lower()
                    in {"completed", "failed", "cancelled", "interrupted", "stopped"},
                ),
            }
            if view_steer_url:
                result["view_steer_url"] = view_steer_url
            if runtime_recovery:
                result["runtime_recovery"] = runtime_recovery
            if expose_diagnostics:
                result.update(
                    {
                        "execution_mode": resolved_execution_mode,
                        "profile": resolved_profile,
                        "effort": resolved_effort,
                        "alias": resolved_alias,
                        "submitted_instruction": worker_instruction,
                        "delegation_audit": {
                            "title": _audit_preview(clean_title, max_chars=180),
                            "goal": _audit_preview(clean_goal, max_chars=360),
                            "instruction_preview": _audit_preview(worker_instruction),
                        },
                    }
                )
            return result

        validate_file_transport(file_upload_ids, binding_key=file_binding_key, bind=True)
        existing_workspace = None
        if reuse_existing_workspace and not project_id and alias:
            existing_workspace = client.find_worker_by_alias_across_projects(
                owner_id=resolved_owner_id,
                alias=reusable_alias,
                execution_mode=resolved_execution_mode,
            )
            if existing_workspace is None:
                raise ValueError(
                    f"Could not resolve existing workspace alias {reusable_alias!r} for "
                    f"execution_mode={resolved_execution_mode!r}; no workspace was created"
                )
        if existing_workspace and provider_selection is not None:
            raise ValueError(
                "Existing workspaces keep their saved provider account policy"
            )
        project = (
            existing_workspace["project"]
            if existing_workspace
            else client.get_project(project_id)
            if project_id
            else client.create_project(
                owner_id=resolved_owner_id,
                title=clean_title,
                goal=clean_goal,
                default_worker_profile=resolved_profile,
            )
        )
        project_owner = _sanitize_context_value(project.get("owner_id"))
        if resolved_owner_id and project_owner != resolved_owner_id:
            raise ValueError(
                "The requested GlassHive project is not available to the authenticated owner."
            )
        resolved_project_id = str(project.get("project_id") or project_id or "").strip()
        if not resolved_project_id:
            raise ValueError("GlassHive project creation did not return project_id")

        try:
            worker = client.find_or_resume_worker(
                project_id=resolved_project_id,
                owner_id=resolved_owner_id,
                name=(worker_name or clean_title).strip(),
                role=(worker_role or clean_goal or clean_instruction).strip(),
                alias=resolved_alias,
                profile=resolved_profile,
                backend=backend,
                execution_mode=resolved_execution_mode,
                resource_class=resource_class,
                workspace_root=workspace_root,
                bootstrap_profile=bootstrap_profile,
                bootstrap_bundle=bundle,
                start_synchronously=False,
                workspace_kind="named",
            )
        except GlassHiveBlockedError as exc:
            return _blocked_dispatch_result(
                exc.payload,
                profile=resolved_profile,
                execution_mode=resolved_execution_mode,
                effort=resolved_effort,
                alias=resolved_alias,
            )
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            raise ValueError("GlassHive worker create/resume did not return worker_id")
        persisted_resource_class = str(worker.get("resource_class") or "").strip()
        if persisted_resource_class not in {"standard", "light"}:
            raise ValueError(
                "GlassHive worker create/resume did not return persisted resource_class"
            )
        if persisted_resource_class != resource_class:
            raise ValueError(
                "GlassHive worker create/resume persisted resource_class "
                f"'{persisted_resource_class}' does not match requested resource_class "
                f"'{resource_class}'."
            )

        if favorite:
            worker = {
                **worker,
                **client.update_workspace(worker_id, {"favorite": True}),
            }

        if file_upload_ids:
            WorkspaceFilesMcpClient(client).bind(worker_id, file_upload_ids, file_binding_key)
        try:
            run = client.assign_run(
                worker_id,
                worker_instruction,
                effort=resolved_effort,
                bootstrap_bundle=bundle,
            )
        except GlassHiveBlockedError as exc:
            return _blocked_dispatch_result(
                exc.payload,
                profile=resolved_profile,
                execution_mode=resolved_execution_mode,
                effort=resolved_effort,
                alias=resolved_alias,
            )
        request_surface = _header_value(_request_headers(), HEADER_SURFACE)
        dispatch_context = _dispatch_follow_up_context(
            worker=worker,
            project_id=resolved_project_id,
            run=run,
            request_surface=request_surface,
            task=clean_title,
            expose_diagnostics=expose_diagnostics,
            explicit_follow_up_required=not bool(
                _recent_dispatch_scope_keys(
                    owner_id=str(worker.get("owner_id") or ""),
                    for_remember=True,
                )
            ),
        )
        _remember_recent_dispatch_context(
            worker=worker,
            project_id=resolved_project_id,
            run=run,
        )
        result: dict[str, Any] = {
            "status": "dispatched",
            "resource_class": persisted_resource_class,
            "callback_ready": callback_ready,
            "callback_delivery": "optional" if callback_ready else "not_configured_standalone_polling_available",
            **(
                {"callback_delivery_deadline_seconds": _callback_delivery_deadline_seconds()}
                if callback_ready
                else {}
            ),
            "missing_callback_fields": missing_callback_fields,
            **dispatch_context,
        }
        if runtime_recovery:
            result["runtime_recovery"] = runtime_recovery
        if expose_diagnostics:
            result.update(
                {
                    "project_id": resolved_project_id,
                    "worker_id": worker_id,
                    "run_id": run.get("run_id"),
                    "run_state": run.get("state"),
                    "execution_mode": resolved_execution_mode,
                    "profile": resolved_profile,
                    "effort": resolved_effort,
                    "alias": resolved_alias,
                    "submitted_instruction": worker_instruction,
                    "delegation_audit": {
                        "title": _audit_preview(clean_title, max_chars=180),
                        "goal": _audit_preview(clean_goal, max_chars=360),
                        "instruction_preview": _audit_preview(worker_instruction),
                    },
                }
            )
        return result

    @server.tool(
        name="workspace_launch",
        title="Launch GlassHive Workspace",
        description=(
            "Primary user-facing GlassHive launch tool. Use this for ordinary LibreChat requests that need a resumable workspace, browser/desktop work, local files, generated artifacts, or a long-running worker. "
            "Its inputs intentionally mirror the documented GlassHive UI: description, optional success_criteria, and optional context. "
            "Use a short natural workspace name on the first line of description; keep the full request on following lines or in context. "
            "Do not shorten, summarize, paraphrase, or water down the user's request. Use description for the outcome, success_criteria for hard gates, and context for the full available background, constraints, examples, links, file references, exclusions, and any original wording that matters. "
            "For connected-account facts or actions, include broker/tool availability as context and let the GlassHive worker choose how to satisfy the user's goal; do not turn tool choice into a success criterion unless the user explicitly asked for that. Missing, unavailable, revoked, approval-blocked, or auth-blocked broker authority must become a needs_input blocker when required; browser, computer, filesystem, shell, and native connectors cannot bypass that protected-provider boundary. A separately authorized user-explicit UI task remains valid only when it does not bypass connected-account authority. "
            "The host assistant must not fabricate MCP/tool results or force a downloadable artifact; only pass real data/capabilities and let the worker decide whether a file, chat answer, browser action, or other output is appropriate. "
            f"{HIGH_EFFORT_SELECTION_GUIDANCE} "
            "If the user did not specify acceptance criteria, omit success_criteria; leave omitted criteria empty. Do not invent provider lists, output schemas, artifacts, ranking rules, workflow steps, memory-derived priorities, active-thread/contact/deal lists, or guessed urgency rubrics. For vague user adjectives like urgent or important, pass the adjective through instead of defining a rubric unless the user defines it. "
            "Connected-account read authorization comes from the host-signed broker grant when reviewed host policy projects content-read scope; connected_account_content_intent is only a compatibility hint for missing-broker warnings, not a required authorization switch. The flag alone does not unlock content reads or writes. "
            f"{HOST_SIDE_ORCHESTRATION_GUIDANCE} "
            "Do not chain project_create, worker_create, and worker_run for routine tasks. Raw IDs stay hidden when the host supplies stable conversation context; a minimal follow_up_context is returned when an external client does not, so status/wait needs no discovery call. "
        "Returns a clean non-blocking dispatch result with view_steer_url and result_tools. "
        "For ordinary fresh launches, create a fresh workspace even if the host suggests a convenient alias. "
        "Reuse a stable alias only when the user explicitly asks to resume/reuse an existing workspace and "
        "reuse_existing_workspace is true; otherwise stale worker history can slow or distort one-off work. "
        "Callbacks are optional; for plain LibreChat or standalone deployments without callback wiring, "
            "use workspace_status for non-blocking follow-up checks and workspace_wait when the user "
            "explicitly wants a blocking wait. For attached/uploaded-file requests, set uploaded_files "
            "when file text is visible to the current model context, and include file names/references "
            "in context so GlassHive can also use request upload metadata when the host supplies it. "
            "If the user asks you to wait for the result, first show the View / Steer link to the "
            "user when the chat protocol supports assistant text before another tool call, then "
            "call workspace_wait with the returned completion_wait_timeout_seconds in the same "
            "turn instead of asking them to confirm waiting. Before a long same-turn wait, surface "
            "the View / Steer link to the user first whenever the chat protocol supports text before "
            "the next tool call; always include it in the final answer."
        ),
        structured_output=True,
    )
    def workspace_launch(
        description: Annotated[
            str,
            Field(
                description=(
                    "Describe the project in the user's own outcome language. Use a short, natural workspace "
                    "name on the first line and preserve the complete request on following lines or in context."
                )
            ),
        ],
        ctx: Context,
        success_criteria: Annotated[
            str | None,
            Field(
                description=(
                    "Optional workspace-internal acceptance criteria. Use explicit user requirements only; "
                    "when the user did not provide distinct acceptance criteria, omit this field. Treat supplied "
                    "criteria as hard gates before reporting completion; put host-side orchestration checks, "
                    "broker/tool availability, and other helpful background in context, not as worker blockers."
                )
            ),
        ] = None,
        context: Annotated[
            str | None,
            Field(
                description=(
                    "Optional background, constraints, uploaded file names/references, links, and preferences "
                    "that help the worker execute well. Include only user-provided, retrieved, or explicitly "
                    "requested background; do not add memory-derived priorities, active-thread/contact/deal "
                    "lists, or guessed urgency rubrics unless the user asked for them. For vague user "
                    "adjectives like urgent or important, pass the adjective through instead of defining a "
                    "rubric unless the user defines it."
                )
            ),
        ] = None,
        workspace_alias: Annotated[
            str | None,
            Field(
                description=(
                    "Optional stable workspace alias or exact saved human name. Honored only when "
                    "reuse_existing_workspace is true; omit for a new one-off workspace."
                )
            ),
        ] = None,
        reuse_existing_workspace: Annotated[
            bool,
            Field(
                description=(
                    "Set true only when the user explicitly asked to resume/reuse the named workspace. "
                    "When workspace_alias is omitted, GlassHive resolves one exact saved workspace name "
                    "from the first description line; zero or multiple exact matches fail without creating anything. "
                    "Leave false for fresh one-off tasks."
                )
            ),
        ] = False,
        favorite: Annotated[
            bool,
            Field(
                description=(
                    "Set true when the user explicitly asks to favorite or pin this reusable workspace."
                )
            ),
        ] = False,
        profile: ProfileParam = "",
        execution_mode: ExecutionModeParam = None,
        resource_class: Annotated[
            Literal["standard", "light"],
            Field(
                description=(
                    "Worker memory class. Use light for explicitly requested small, bounded jobs; "
                    "omit for the standard class."
                )
            ),
        ] = "standard",
        connected_account_content_intent: Annotated[
            bool,
            Field(
                description=(
                    "Optional compatibility hint for hosts that want GlassHive to warn when "
                    "connected-account content was requested but no complete broker grant/config "
                    "was supplied. Authorization comes only from a host-signed broker grant with "
                    "content-read scope; this flag alone does not unlock reads or writes."
                )
            ),
        ] = False,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        uploaded_files: UploadedFilesParam = None,
        file_upload_ids: FileUploadIdsParam = None,
        file_binding_key: FileBindingKeyParam = None,

        effort: Annotated[
            str | None,
            Field(
                description=(
                    "Optional per-run effort override. Codex accepts none/low/medium/high/xhigh; "
                    "minimal is for explicitly allowlisted deployments only. "
                    "Claude Code accepts default/low/medium/high/xhigh/max; OpenClaw accepts high/max. Omit to use saved user preferences or deployment default. "
                    + HIGH_EFFORT_SELECTION_GUIDANCE
                )
            ),
        ] = None,
        provider_account_policy: Literal[
            "legacy", "personal_preferred", "personal_required"
        ] | None = None,
        provider_account_id: str | None = None,
        require_callback: bool = False,
        expose_diagnostics: bool = False,
    ) -> dict[str, Any]:
        clean_description = description.strip()
        explicit_success_criteria = bool((success_criteria or "").strip())
        clean_success_criteria = (success_criteria or "").strip()
        clean_context = (context or "").strip()
        if not clean_description:
            raise ValueError("description is required")
        brief_sections = [
            "Project description:",
            clean_description,
        ]
        if explicit_success_criteria:
            brief_sections.extend(["", "Explicit success criteria:", clean_success_criteria])
        if clean_context:
            brief_sections.extend(["", "Context:", clean_context])
        success_rules = (
            ["- Treat explicit success criteria as hard acceptance gates."]
            if explicit_success_criteria else []
        )
        brief_sections.extend(
            [
                "",
                "Execution rules:",
                *success_rules,
                "- Keep working until the user's request is satisfied or a real blocker appears.",
                WORKER_HOST_SIDE_ORCHESTRATION_RULE,
                "- Preserve files, browser state, and workspace continuity for follow-up work.",
                "- If the result is visual or browser-visible, open it in the workspace browser before finishing.",
                "- Before finishing, inspect the actual output, files/artifacts, tool results, or visible state; compare it with the user's request, explicit success criteria when supplied, constraints, and files; continue or fix if it does not match.",
                "- Finish with a concise FINAL REPORT in the user's requested form; mention artifacts only when you intentionally created user-facing files, and mention blockers only when they remain.",
            ]
        )
        title = clean_description.splitlines()[0].strip()[:120] or "GlassHive workspace"
        delegate_alias = workspace_alias if reuse_existing_workspace else None
        resolved_catalog_item: dict[str, Any] | None = None
        if reuse_existing_workspace:
            supplied_alias = str(delegate_alias or "").strip()
            lookup_name = supplied_alias or title
            supplied_unscoped_alias = (
                _request_unscoped_alias(supplied_alias) if supplied_alias else ""
            )
            resolved_catalog_alias = ""
            catalog = client.workspace_catalog(
                search=lookup_name,
                kind="",
                limit=100,
            )
            items = catalog.get("items") if isinstance(catalog, dict) else None
            next_cursor = catalog.get("next_cursor") if isinstance(catalog, dict) else None
            active_items = [
                item
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, dict)
                and str(item.get("state") or "").strip().lower()
                not in CLOSED_WORKER_STATES
                and str(item.get("alias") or "").strip()
            ]
            direct_alias_matches = [
                item
                for item in active_items
                if supplied_unscoped_alias
                and _request_unscoped_alias(
                    str(item.get("alias") or "").strip()
                )
                == supplied_unscoped_alias
            ]
            if direct_alias_matches and (len(direct_alias_matches) != 1 or next_cursor):
                raise ValueError(
                    f"Could not resolve exactly one saved workspace matching {lookup_name!r}; "
                    "use workspace_list once and retry with its workspace_alias"
                )
            if len(direct_alias_matches) == 1:
                resolved_catalog_item = direct_alias_matches[0]
                resolved_catalog_alias = str(resolved_catalog_item["alias"]).strip()
            else:
                exact_name_matches = [
                    item
                    for item in active_items
                    if str(item.get("name") or "").strip().casefold()
                    == lookup_name.casefold()
                ]
                if exact_name_matches and (len(exact_name_matches) != 1 or next_cursor):
                    raise ValueError(
                        f"Could not resolve exactly one saved workspace matching {lookup_name!r}; "
                        "use workspace_list once and retry with its workspace_alias"
                    )
                if len(exact_name_matches) == 1:
                    resolved_catalog_item = exact_name_matches[0]
                    resolved_catalog_alias = str(resolved_catalog_item["alias"]).strip()
            if not resolved_catalog_alias:
                raise ValueError(
                    f"Could not resolve exactly one saved workspace matching {lookup_name!r}; "
                    "use workspace_list once and retry with its workspace_alias"
                )
            delegate_alias = resolved_catalog_alias
        effective_profile = profile
        effective_execution_mode = execution_mode
        if resolved_catalog_item is not None:
            if not str(profile or "").strip():
                effective_profile = str(resolved_catalog_item.get("profile") or "").strip()
            if execution_mode is None:
                inherited_execution_mode = str(
                    resolved_catalog_item.get("execution_mode") or ""
                ).strip()
                effective_execution_mode = inherited_execution_mode or None
        return worker_delegate_once(
            title=title,
            instruction="\n".join(brief_sections),
            ctx=ctx,
            goal=clean_success_criteria,
            alias=delegate_alias,
            reuse_existing_workspace=reuse_existing_workspace,
            favorite=favorite,
            profile=effective_profile,
            execution_mode=effective_execution_mode,
            bootstrap_bundle_json=bootstrap_bundle_json,
            uploaded_files=uploaded_files,
            file_upload_ids=file_upload_ids,
            file_binding_key=file_binding_key,
            effort=effort,
            provider_account_policy=provider_account_policy,
            provider_account_id=provider_account_id,
            connected_account_content_intent=connected_account_content_intent,
            require_callback=require_callback,
            expose_diagnostics=expose_diagnostics,
        )

    @server.tool(
        name="workspace_schedule",
        title="Schedule GlassHive Workspace",
        description=(
            "Schedule a GlassHive workspace to run later without relying on LibreChat Scheduling Cortex. "
            "Use this when the user asks GlassHive or MCP to do work in 20 minutes, on a weekday, or at a specific run_at time. "
            "Inputs mirror workspace_launch plus schedule_text/run_at/delay_seconds. It creates or resumes the worker now, persists the schedule in GlassHive, and queues the run when due. "
            "For scheduled attached-file work, set uploaded_files when visible file text needs to be materialized into the future workspace. "
            "For scheduled connected-account facts or actions, include broker/tool availability as context and prefer MCP/tools when they can satisfy the task. Read authorization comes from the host-signed broker grant, while connected_account_content_intent is only a compatibility hint for missing-broker warnings. "
            f"{HIGH_EFFORT_SELECTION_GUIDANCE} "
            "Do not invent schedule success criteria, provider lists, output schemas, artifacts, ranking rules, workflow steps, memory-derived priorities, active-thread/contact/deal lists, or guessed urgency rubrics. For vague user adjectives like urgent or important, pass the adjective through instead of defining a rubric unless the user defines it. Trust the scheduled GlassHive worker to choose the best path from the user's request and available context. "
            "Callbacks are optional; the schedule can be checked later through GlassHive MCP status tools."
        ),
        structured_output=True,
    )
    def workspace_schedule(
        description: Annotated[str, Field(description="Describe the later project or task in the user's outcome language.")],
        success_criteria: Annotated[
            str | None,
            Field(
                description=(
                    "Optional acceptance criteria. Use explicit user requirements only; when the user did not "
                    "provide distinct acceptance criteria, omit this field. Treat supplied criteria as hard gates before "
                    "reporting completion."
                )
            ),
        ] = None,
        schedule_text: Annotated[
            str | None,
            Field(description="Human schedule expression, for example 'in 20 minutes' or 'on Mondays'."),
        ] = None,
        run_at: Annotated[
            str | None,
            Field(description="Optional ISO datetime. Prefer this when the caller already resolved the due time."),
        ] = None,
        delay_seconds: Annotated[int | None, Field(description="Optional relative delay in seconds for deterministic automation/QA.")] = None,
        context: Annotated[
            str | None,
            Field(
                description=(
                    "Optional background, constraints, files, links, and preferences that help the worker execute "
                    "well. Include only user-provided, retrieved, or explicitly requested background; do not add "
                    "memory-derived priorities, active-thread/contact/deal lists, or guessed urgency rubrics unless "
                    "the user asked for them. For vague user adjectives like urgent or important, pass the "
                    "adjective through instead of defining a rubric unless the user defines it."
                )
            ),
        ] = None,
        workspace_alias: Annotated[
            str | None,
            Field(description="Optional stable workspace alias to resume. Omit for a new one-off scheduled workspace."),
        ] = None,
        profile: ProfileParam = "",
        execution_mode: ExecutionModeParam = None,
        connected_account_content_intent: Annotated[
            bool,
            Field(
                description=(
                    "Optional compatibility hint for hosts that want GlassHive to warn when "
                    "connected-account content was requested but no complete broker grant/config "
                    "was supplied. Authorization comes only from a host-signed broker grant with "
                    "content-read scope; this flag alone does not unlock reads or writes."
                )
            ),
        ] = False,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        uploaded_files: UploadedFilesParam = None,
        file_upload_ids: FileUploadIdsParam = None,

        effort: Annotated[
            str | None,
            Field(description="Optional per-run effort override for the scheduled workspace. " + HIGH_EFFORT_SELECTION_GUIDANCE),
        ] = None,
        provider_account_policy: Literal[
            "legacy", "personal_preferred", "personal_required"
        ] | None = None,
        provider_account_id: str | None = None,
        require_callback: bool = False,
        expose_diagnostics: bool = False,
    ) -> dict[str, Any]:
        clean_description = description.strip()
        explicit_success_criteria = bool((success_criteria or "").strip())
        clean_success_criteria = (success_criteria or "").strip()
        clean_context = (context or "").strip()
        if not clean_description:
            raise ValueError("description is required")
        requested_execution_mode = str(execution_mode or "").strip()
        brief_sections = [
            "Scheduled project description:",
            clean_description,
        ]
        if explicit_success_criteria:
            brief_sections.extend(["", "Explicit success criteria:", clean_success_criteria])
        if clean_context:
            brief_sections.extend(["", "Context:", clean_context])
        success_rules = (
            ["- Treat explicit success criteria as hard acceptance gates."]
            if explicit_success_criteria else []
        )
        brief_sections.extend(
            [
                "",
                "Execution rules:",
                *success_rules,
                "- Keep working until the user's request is satisfied or a real blocker appears.",
                WORKER_HOST_SIDE_ORCHESTRATION_RULE,
                "- Preserve files, browser state, and workspace continuity for follow-up work.",
                "- Finish with a concise FINAL REPORT in the user's requested form; mention artifacts only when you intentionally created user-facing files, and mention blockers only when they remain.",
            ]
        )
        scheduled_instruction = "\n".join(brief_sections)

        resolved_owner_id = _request_owner_id(None)
        resolved_execution_mode = _resolve_execution_mode(execution_mode, allow_disabled_host=True)
        try:
            preferences = _normalize_preferences(client.get_preferences())
        except Exception:
            preferences = {}
        resolved_profile = _resolve_profile_from_preferences(profile, preferences)
        resolved_effort = _resolve_effort_for_profile(resolved_profile, effort, preferences)
        title = clean_description.splitlines()[0].strip()[:120] or "GlassHive scheduled workspace"
        blocked = _runtime_dependency_blocked_payload(
            profile=resolved_profile,
            execution_mode=resolved_execution_mode,
        )
        runtime_recovery: dict[str, Any] | None = None
        if _can_recover_blocked_host_dispatch_to_docker(
            blocked,
            requested_execution_mode=requested_execution_mode,
            resolved_execution_mode=resolved_execution_mode,
            workspace_root=None,
        ):
            runtime_recovery = {
                "from_execution_mode": "host",
                "to_execution_mode": "docker",
                "reason_class": blocked.get("failure_class"),
                "reason_summary": blocked.get("failure_user_message"),
            }
            resolved_execution_mode = "docker"
            blocked = _runtime_dependency_blocked_payload(
                profile=resolved_profile,
                execution_mode=resolved_execution_mode,
            )
        if blocked:
            return _blocked_dispatch_result(
                blocked,
                profile=resolved_profile,
                execution_mode=resolved_execution_mode,
                effort=resolved_effort,
                alias=_request_unscoped_alias(
                    workspace_alias or _slugify_alias(resolved_profile, title)
                ),
            )
        bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json) or {}
        bundle.setdefault(
            "project_definition",
            _default_project_definition(title=title, goal=clean_success_criteria, instruction=scheduled_instruction),
        )
        bundle = _merge_request_context(bundle)
        trusted_tenant_id, trusted_owner_id, trusted_storage_owner_id = (
            _trusted_request_upload_scope()
        )
        bundle = _merge_explicit_uploaded_files(
            bundle,
            uploaded_files,
            tenant_id=trusted_tenant_id,
            owner_id=trusted_owner_id,
            storage_owner_id=trusted_storage_owner_id,
        )
        validate_file_transport(file_upload_ids, uploaded_files, bundle)
        bundle = _apply_effort_to_bundle(bundle, profile=resolved_profile, effort=resolved_effort)
        provider_selection = _provider_account_selection(
            profile=resolved_profile,
            policy=provider_account_policy,
            account_id=provider_account_id,
        )
        if provider_selection is not None:
            bundle["provider_account"] = provider_selection
        bundle, scheduled_instruction = _apply_connected_account_intent_guard(
            bundle,
            scheduled_instruction,
            connected_account_content_intent=connected_account_content_intent,
        )
        scheduled_instruction = scheduled_instruction or "\n".join(brief_sections)
        callback_ready, missing_callback_fields = _callback_state(bundle, required=require_callback)
        if require_callback and not callback_ready:
            return {
                "status": "blocked",
                "callback_ready": False,
                "missing_callback_fields": missing_callback_fields,
                "acknowledgement_guidance": (
                    "Explain in your own voice that the scheduled workspace cannot be accepted yet "
                    "because the callback context is incomplete."
                ),
            }

        resolved_alias = _request_unscoped_alias(
            workspace_alias or _slugify_alias(resolved_profile, title)
        )
        existing_workspace = None
        if workspace_alias:
            existing_workspace = client.find_worker_by_alias_across_projects(
                owner_id=resolved_owner_id,
                alias=resolved_alias,
                execution_mode=resolved_execution_mode,
            )
        if existing_workspace and provider_selection is not None:
            raise ValueError(
                "Existing workspaces keep their saved provider account policy"
            )
        project = existing_workspace["project"] if existing_workspace else client.create_project(
            owner_id=resolved_owner_id,
            title=title,
            goal=clean_success_criteria,
            default_worker_profile=resolved_profile,
        )
        project_id = str(project.get("project_id") or "")
        worker = client.find_or_resume_worker(
            project_id=project_id,
            owner_id=resolved_owner_id,
            name=title,
            role=clean_success_criteria,
            alias=resolved_alias,
            profile=resolved_profile,
            backend=None,
            execution_mode=resolved_execution_mode,
            bootstrap_bundle=bundle,
            start_synchronously=False,
            workspace_kind="named",
        )
        worker_id = str(worker.get("worker_id") or "")
        if not worker_id:
            raise ValueError("GlassHive worker create/resume did not return worker_id")
        schedule = client.schedule_run(
            worker_id,
            scheduled_instruction,
            run_at=run_at,
            schedule_text=schedule_text,
            delay_seconds=delay_seconds,
            bootstrap_bundle=bundle,
            **({"file_upload_ids": file_upload_ids} if file_upload_ids else {}),
        )
        result: dict[str, Any] = {
            "status": "scheduled",
            "callback_ready": callback_ready,
            "callback_delivery": "optional" if callback_ready else "not_configured_standalone_polling_available",
            "missing_callback_fields": missing_callback_fields,
            "scheduled_for": schedule.get("run_at"),
            "schedule_state": schedule.get("state"),
            "acknowledgement_guidance": (
                "Write one short acknowledgement in your own voice that the workspace is scheduled. "
                "Callbacks are optional; status can be checked through GlassHive MCP. Do not expose "
                "worker/run/provider plumbing unless diagnostics were requested."
            ),
            "delegation_audit": {
                "title": _audit_preview(title, max_chars=180),
                "schedule": _audit_preview(schedule_text or run_at or str(delay_seconds or ""), max_chars=180),
                "instruction_preview": _audit_preview(scheduled_instruction),
                "use_for": "self-check only; do not show the user unless diagnostics were requested",
            },
        }
        if expose_diagnostics:
            result.update(
                {
                    "project_id": project_id,
                    "worker_id": worker_id,
                    "schedule_id": schedule.get("schedule_id"),
                    "execution_mode": resolved_execution_mode,
                    "profile": resolved_profile,
                    "effort": resolved_effort,
                }
            )
        if runtime_recovery:
            result["runtime_recovery"] = runtime_recovery
        return result

    @server.tool(
        name="project_runs",
        title="Project Runs",
        description=(
            "List recent runs for a project. Use for diagnostics, audit, or explicit status requests about an existing project. "
            "For ordinary follow-up on a previously launched task, prefer workspace_status or workspace_wait. "
            "Returns run ids, states, and recent execution metadata."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def project_runs(project_id: str) -> list[dict[str, Any]]:
        return client.list_project_runs(project_id)

    @server.tool(
        name="project_events",
        title="Project Events",
        description=(
            "List recent lifecycle events for a project. Use only for diagnostics, audit trails, or explicit investigation of what happened. "
            "Do not include raw event logs in a normal user-facing answer unless they explain a blocker. Returns event ids, event types, and project-linked timestamps/details when available."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def project_events(project_id: str) -> list[dict[str, Any]]:
        return client.list_project_events(project_id)

    @server.tool(
        name="workers_list",
        title="List Workers",
        description=(
            "List workers belonging to a project. Use for explicit worker inventory, resume, or diagnostic requests on an existing project. "
            "Do not list workers as boilerplate before a fresh task; prefer worker_delegate_once. Returns worker ids, states, profiles, aliases, and execution metadata when available."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workers_list(project_id: str) -> list[dict[str, Any]]:
        return [_normalize_worker_backend(worker) for worker in client.list_workers(project_id)]

    @server.tool(
        name="worker_create",
        title="Create Worker",
        description=(
            "Create a new worker in an existing project. Optionally pass bootstrap_profile and "
            "bootstrap_bundle_json as a JSON string or object to seed auth, MCP config, instructions, env, and project files. "
            "Use this lower-level tool for explicit orchestration or diagnostics; for a fresh one-off task, prefer worker_delegate_once. "
            "When seeding connected-account read access for an immediately related run, pass the host-signed broker grant/config; connected_account_content_intent is only a compatibility hint for missing-broker warnings. "
            "Omit execution_mode to use the configured default. Set execution_mode='host' only when GlassHive instructions say host-native workers are enabled and the task depends on the user's real computer/session; set execution_mode='docker' for isolated work. "
            "Returns the worker record with worker_id, execution mode, profile, alias, and bootstrap result metadata."
        ),
        structured_output=True,
    )
    def worker_create(
        project_id: str,
        name: str,
        role: str,
        owner_id: str | None = None,
        profile: ProfileParam = "",
        backend: BackendParam = None,
        execution_mode: ExecutionModeParam = None,
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        connected_account_content_intent: Annotated[
            bool,
            Field(
                description=(
                    "Optional compatibility hint for hosts that want GlassHive to warn when "
                    "connected-account content was requested but no complete broker grant/config "
                    "was supplied. Authorization comes only from a host-signed broker grant with "
                    "content-read scope; this flag alone does not unlock reads or writes."
                )
            ),
        ] = False,
        bootstrap_bundle_json: BootstrapBundleParam = None,
    ) -> dict[str, Any]:
        parsed_bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json)
        parsed_bundle = _merge_request_context(parsed_bundle)
        parsed_bundle, _ = _apply_connected_account_intent_guard(
            parsed_bundle,
            connected_account_content_intent=connected_account_content_intent,
        )
        resolved_execution_mode = _resolve_execution_mode(execution_mode)
        return _normalize_worker_backend(client.create_worker(
            project_id=project_id,
            owner_id=_request_owner_id(owner_id),
            name=name,
            role=role,
            profile=profile.strip() or _configured_default_worker_profile(),
            backend=backend,
            execution_mode=resolved_execution_mode,
            alias=alias,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=parsed_bundle,
        ))

    @server.tool(
        name="worker_find_or_resume",
        title="Find Or Resume Worker",
        description=(
            "Find an existing open worker by alias for a project/owner, or create one. "
            "Omit execution_mode to use the configured default. Set execution_mode='host' only when GlassHive instructions say host-native workers are enabled and the task depends on the user's real computer/session: signed-in browser profile, desktop apps, local files/projects, installed CLIs, or OS/window control. "
            "Use execution_mode='docker' for isolated sandbox, disposable browser, or risky untrusted work. "
            "Do not use for fresh one-off tasks when worker_delegate_once can create/resume and queue the run in one call. "
            "When seeding connected-account read access for an immediately related run, pass the host-signed broker grant/config; connected_account_content_intent is only a compatibility hint for missing-broker warnings. "
            "bootstrap_bundle_json may be a JSON string or object. Returns the existing or newly created worker record."
        ),
        structured_output=True,
    )
    def worker_find_or_resume(
        project_id: str,
        name: str,
        role: str,
        alias: str,
        owner_id: str | None = None,
        profile: ProfileParam = "",
        backend: BackendParam = None,
        execution_mode: ExecutionModeParam = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        connected_account_content_intent: Annotated[
            bool,
            Field(
                description=(
                    "Optional compatibility hint for hosts that want GlassHive to warn when "
                    "connected-account content was requested but no complete broker grant/config "
                    "was supplied. Authorization comes only from a host-signed broker grant with "
                    "content-read scope; this flag alone does not unlock reads or writes."
                )
            ),
        ] = False,
        bootstrap_bundle_json: BootstrapBundleParam = None,
    ) -> dict[str, Any]:
        parsed_bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json)
        parsed_bundle = _merge_request_context(parsed_bundle)
        parsed_bundle, _ = _apply_connected_account_intent_guard(
            parsed_bundle,
            connected_account_content_intent=connected_account_content_intent,
        )
        resolved_execution_mode = _resolve_execution_mode(execution_mode)
        return _normalize_worker_backend(client.find_or_resume_worker(
            project_id=project_id,
            owner_id=_request_owner_id(owner_id),
            name=name,
            role=role,
            alias=alias,
            profile=profile.strip() or _configured_default_worker_profile(),
            backend=backend,
            execution_mode=resolved_execution_mode,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=parsed_bundle,
        ))

    @server.tool(
        name="worker_get",
        title="Get Worker",
        description=(
            "Fetch a worker by worker_id. Use for explicit worker inspection or when a previous tool result already supplied the id. "
            "Do not expose worker ids to the user unless diagnostics were requested. Returns the worker record with project, state, profile, alias, and execution mode when available."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def worker_get(worker_id: str) -> dict[str, Any]:
        return _normalize_worker_backend(client.get_worker(worker_id))

    @server.tool(
        name="worker_live",
        title="Worker Live State",
        description=(
            "Fetch rich live worker diagnostics, including runtime details, runs, logs, and artifacts. "
            "Use only when the user asks for status, diagnostics, takeover detail, or live workspace evidence. "
            "Returns worker state, runtime details, recent runs, events, artifacts, and blocker evidence when available."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def worker_live(worker_id: str) -> dict[str, Any]:
        return client.worker_live(worker_id)

    @server.tool(
        name="worker_run",
        title="Queue Worker Run",
        description=(
            "Queue a new instruction for an existing worker. Use for explicit steering/resume/reuse. "
            "For fresh one-off host/browser/desktop/local tasks, prefer worker_delegate_once. "
            "bootstrap_bundle_json may be passed to refresh run-scoped auth, MCP config, or instructions before the run starts. "
            "For connected-account content refreshes, pass the host-signed broker grant/config; connected_account_content_intent is only a compatibility hint for missing-broker warnings. "
            "Returns the queued run record with run_id/state and later completion delivered by callback when configured."
        ),
        structured_output=True,
    )
    def worker_run(
        worker_id: str,
        instruction: str,
        connected_account_content_intent: Annotated[
            bool,
            Field(
                description=(
                    "Optional compatibility hint for hosts that want GlassHive to warn when "
                    "connected-account content was requested but no complete broker grant/config "
                    "was supplied. Authorization comes only from a host-signed broker grant with "
                    "content-read scope; this flag alone does not unlock reads or writes."
                )
            ),
        ] = False,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        file_upload_ids: FileUploadIdsParam = None,
        file_binding_key: FileBindingKeyParam = None,

    ) -> dict[str, Any]:
        parsed_bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json)
        parsed_bundle = _merge_request_context(parsed_bundle)
        parsed_bundle, instruction = _apply_connected_account_intent_guard(
            parsed_bundle,
            instruction,
            connected_account_content_intent=connected_account_content_intent,
        )
        instruction = instruction or ""
        validate_file_transport(file_upload_ids, bundle=parsed_bundle, binding_key=file_binding_key, bind=True)
        if file_upload_ids:
            WorkspaceFilesMcpClient(client).bind(worker_id, file_upload_ids, file_binding_key)
        if parsed_bundle is not None:
            return client.assign_run(worker_id, instruction, bootstrap_bundle=parsed_bundle)
        return client.assign_run(worker_id, instruction)

    @server.tool(
        name="worker_schedule",
        title="Schedule Worker Run",
        description=(
            "Schedule a run for an existing GlassHive worker using GlassHive's own scheduler. "
            "Use when the user asks raw MCP/GlassHive to do something later. Provide run_at, schedule_text such as 'in 20 minutes', or delay_seconds. "
            "For scheduled connected-account content refreshes, pass the host-signed broker grant/config; connected_account_content_intent is only a compatibility hint for missing-broker warnings."
        ),
        structured_output=True,
    )
    def worker_schedule(
        worker_id: str,
        instruction: str,
        connected_account_content_intent: Annotated[
            bool,
            Field(
                description=(
                    "Optional compatibility hint for hosts that want GlassHive to warn when "
                    "connected-account content was requested but no complete broker grant/config "
                    "was supplied. Authorization comes only from a host-signed broker grant with "
                    "content-read scope; this flag alone does not unlock reads or writes."
                )
            ),
        ] = False,
        schedule_text: str | None = None,
        run_at: str | None = None,
        delay_seconds: int | None = None,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        file_upload_ids: FileUploadIdsParam = None,

    ) -> dict[str, Any]:
        worker = client.get_worker(worker_id)
        worker_profile = str(worker.get("profile") or _configured_default_worker_profile())
        worker_execution_mode = str(worker.get("execution_mode") or "docker")
        blocked = _runtime_dependency_blocked_payload(
            profile=worker_profile,
            execution_mode=worker_execution_mode,
        )
        if blocked:
            return _blocked_dispatch_result(
                blocked,
                profile=worker_profile,
                execution_mode=worker_execution_mode,
                alias=str(worker.get("alias") or ""),
            )
        parsed_bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json)
        parsed_bundle = _merge_request_context(parsed_bundle)
        parsed_bundle, instruction = _apply_connected_account_intent_guard(
            parsed_bundle,
            instruction,
            connected_account_content_intent=connected_account_content_intent,
        )
        instruction = instruction or ""
        kwargs = {
            "run_at": run_at,
            "schedule_text": schedule_text,
            "delay_seconds": delay_seconds,
        }
        if parsed_bundle is not None:
            kwargs["bootstrap_bundle"] = parsed_bundle
        validate_file_transport(file_upload_ids, bundle=parsed_bundle)
        if file_upload_ids:
            kwargs["file_upload_ids"] = file_upload_ids
        return client.schedule_run(worker_id, instruction, **kwargs)

    @server.tool(
        name="worker_schedules",
        title="List Worker Schedules",
        description="List pending or completed GlassHive-native schedules for a worker. Use for explicit scheduling status or QA.",
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def worker_schedules(worker_id: str, include_done: bool = False) -> list[dict[str, Any]]:
        return client.worker_schedules(worker_id, include_done=include_done)

    @server.tool(
        name="worker_recurring_schedule_create",
        title="Create Recurring Worker Schedule",
        description=(
            "Create a durable, structured schedule for an existing worker. Supported forms are once, "
            "elapsed interval, backward-compatible daily wall clock, cron, and RFC 5545 RRULE. Supply "
            "an IANA timezone and explicit start/end, overlap, misfire, bounded catch-up, and jitter policy "
            "when needed. Natural-language timing is not parsed here. The returned schedule_owner and "
            "owner_action identify the dispatch authority; only that owner fires it. "
            "This is additive to the unchanged one-shot worker_schedule tool."
        ),
        structured_output=True,
    )
    def worker_recurring_schedule_create(
        worker_id: str,
        instruction: str,
        recurrence_type: Literal["once", "daily", "interval", "cron", "rfc5545"],
        interval_seconds: int | None = None,
        local_time: str = "",
        timezone_name: str = "UTC",
        dst_policy: Literal["next_valid_earliest", "next_valid_latest"] = "next_valid_earliest",
        first_run_at: str | None = None,
        cron_expression: str = "",
        rrule: str = "",
        starts_at: str | None = None,
        ends_at: str | None = None,
        enabled: bool = True,
        overlap_policy: Literal["skip", "queue"] = "skip",
        misfire_grace_seconds: Annotated[int, Field(ge=0, le=604800)] = 300,
        catch_up_policy: Literal["skip", "bounded", "coalesce"] = "skip",
        max_catch_up_occurrences: Annotated[int, Field(ge=1, le=10)] = 1,
        jitter_seconds: Annotated[int, Field(ge=0, le=900)] = 0,
        schedule_text: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instruction": instruction,
            "recurrence_type": recurrence_type,
            "interval_seconds": interval_seconds,
            "local_time": local_time,
            "timezone_name": timezone_name,
            "dst_policy": dst_policy,
            "first_run_at": first_run_at,
            "cron_expression": cron_expression,
            "rrule": rrule,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "enabled": enabled,
            "overlap_policy": overlap_policy,
            "misfire_grace_seconds": misfire_grace_seconds,
            "catch_up_policy": catch_up_policy,
            "max_catch_up_occurrences": max_catch_up_occurrences,
            "jitter_seconds": jitter_seconds,
            "schedule_text": schedule_text,
        }
        return client.create_recurring_schedule(worker_id, payload)

    @server.tool(
        name="worker_recurring_schedules",
        title="List Recurring Worker Schedules",
        description=(
            "List the current user's durable recurring schedules. Optionally scope the result to one "
            "worker or include deactivated definitions. Each item includes the user's human "
            "workspace_name; display that name instead of inferring one from its project."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def worker_recurring_schedules(
        worker_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        return client.recurring_schedules(worker_id, include_inactive=include_inactive)

    @server.tool(
        name="worker_recurring_schedule_occurrences",
        title="List Recurring Schedule Occurrences",
        description="Inspect the latest immutable occurrences and run outcomes for one recurring schedule.",
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def worker_recurring_schedule_occurrences(
        definition_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> list[dict[str, Any]]:
        return client.recurring_schedule_occurrences(definition_id, limit=limit)

    @server.tool(
        name="worker_recurring_schedule_update",
        title="Update Recurring Worker Schedule",
        description=(
            "Update a durable schedule definition without changing immutable occurrence history. "
            "Use enabled=false to pause and enabled=true to resume; owner and dispatch action remain fixed."
        ),
        structured_output=True,
    )
    def worker_recurring_schedule_update(
        definition_id: str,
        instruction: str | None = None,
        recurrence_type: Literal["once", "daily", "interval", "cron", "rfc5545"] | None = None,
        interval_seconds: int | None = None,
        local_time: str | None = None,
        timezone_name: str | None = None,
        dst_policy: Literal["next_valid_earliest", "next_valid_latest"] | None = None,
        cron_expression: str | None = None,
        rrule: str | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        enabled: bool | None = None,
        overlap_policy: Literal["skip", "queue"] | None = None,
        misfire_grace_seconds: Annotated[int | None, Field(ge=0, le=604800)] = None,
        catch_up_policy: Literal["skip", "bounded", "coalesce"] | None = None,
        max_catch_up_occurrences: Annotated[int | None, Field(ge=1, le=10)] = None,
        jitter_seconds: Annotated[int | None, Field(ge=0, le=900)] = None,
        schedule_text: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in {
                "instruction": instruction,
                "recurrence_type": recurrence_type,
                "interval_seconds": interval_seconds,
                "local_time": local_time,
                "timezone_name": timezone_name,
                "dst_policy": dst_policy,
                "cron_expression": cron_expression,
                "rrule": rrule,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "enabled": enabled,
                "overlap_policy": overlap_policy,
                "misfire_grace_seconds": misfire_grace_seconds,
                "catch_up_policy": catch_up_policy,
                "max_catch_up_occurrences": max_catch_up_occurrences,
                "jitter_seconds": jitter_seconds,
                "schedule_text": schedule_text,
            }.items()
            if value is not None
        }
        return client.update_recurring_schedule(definition_id, payload)

    @server.tool(
        name="worker_recurring_schedule_deactivate",
        title="Deactivate Recurring Worker Schedule",
        description=(
            "Stop future occurrences for one recurring schedule while retaining its definition and history."
        ),
        structured_output=True,
    )
    def worker_recurring_schedule_deactivate(definition_id: str) -> dict[str, Any]:
        return client.deactivate_recurring_schedule(definition_id)

    @server.tool(
        name="worker_recurring_schedule_run_now",
        title="Run Recurring Worker Schedule Now",
        description=(
            "Request one owner-scoped occurrence now without advancing the recurring cursor. "
            "Retry with the same idempotency key to get the same occurrence; delegated schedules "
            "return the required Viventium owner action instead of dispatching twice."
        ),
        structured_output=True,
    )
    def worker_recurring_schedule_run_now(
        definition_id: str,
        idempotency_key: Annotated[str, Field(min_length=8, max_length=128)],
    ) -> dict[str, Any]:
        return client.run_recurring_schedule_now(definition_id, idempotency_key)

    @server.tool(
        name="worker_recurring_schedule_retire",
        title="Retire Recurring Worker Schedule",
        description=(
            "Permanently retire a recurring definition so it cannot be resumed. Past occurrences "
            "and their outcomes remain available as immutable history."
        ),
        structured_output=True,
    )
    def worker_recurring_schedule_retire(definition_id: str) -> dict[str, Any]:
        return client.retire_recurring_schedule(definition_id)

    @server.tool(
        name="worker_message",
        title="Send Worker Message",
        description=(
            "Send an operator message into the current worker session. Use when the user gives follow-up guidance, approval, correction, or an answer to a worker blocker. "
            "Do not use for a fresh task; prefer worker_delegate_once or worker_run depending on whether a reusable worker already exists. Returns the queued message/run state."
        ),
        structured_output=True,
    )
    def worker_message(worker_id: str, message: str) -> dict[str, Any]:
        clean_worker_id = str(worker_id or "").strip()
        clean_message = str(message or "").strip()
        if not clean_worker_id:
            raise ValueError("worker_id is required to send a worker message")
        if not clean_message:
            raise ValueError("message is required to send a worker message")
        return client.send_message(clean_worker_id, clean_message)

    @server.tool(
        name="worker_pause",
        title="Pause Worker",
        description=(
            "Pause a worker. Use only when the user asks to pause/hold work or when an explicit safety checkpoint requires stopping active execution. "
            "Docker workers are frozen; host-native workers stop the active process. Returns the lifecycle state. Do not pause routine completed or callback-ready work."
        ),
        structured_output=True,
    )
    def worker_pause(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "pause")

    @server.tool(
        name="worker_resume",
        title="Resume Worker",
        description=(
            "Resume a paused persistent worker. Use when the user explicitly wants held work to continue or after a confirmed checkpoint. "
            "Do not create a new worker for the same task if a paused worker is the intended continuation. Returns the lifecycle state and any blocker from the runtime."
        ),
        structured_output=True,
    )
    def worker_resume(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "resume")

    @server.tool(name="worker_native_control", title="Native Worker Control", description="Read native pending requests, or submit an exact run/attempt interjection, cancellation or permission response. A queued receipt means accepted by the native harness, not consumed.", structured_output=True)
    def worker_native_control(worker_id: str, run_id: str = "", attempt_id: str = "", action: str = "", text: str = "", request_id: str = "", option_id: str = "", response: dict[str, Any] | None = None) -> dict[str, Any]:
        if not action:
            return client.native_control(worker_id)
        payload = {"run_id":run_id,"attempt_id":attempt_id,"action":action}
        if text: payload["text"] = text
        if request_id: payload["request_id"] = request_id
        if option_id: payload["option_id"] = option_id
        if response is not None: payload["response"] = response
        return client.native_control(worker_id, payload)

    @server.tool(
        name="worker_interrupt",
        title="Interrupt Worker",
        description=(
            "Interrupt the active task while keeping the worker available. Use when the user changes direction, cancels the current step, or requests immediate steering without destroying the worker. "
            "Do not terminate persistent context unless the user asks. Returns lifecycle state and runtime blocker details when available."
        ),
        structured_output=True,
    )
    def worker_interrupt(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "interrupt")

    @server.tool(
        name="worker_terminate",
        title="Terminate Worker",
        description=(
            "Terminate a worker and cancel active or queued runs. Use only for explicit stop/shutdown/delete-style requests or unrecoverable diagnostics. "
            "Do not terminate when pause, interrupt, or resume would preserve useful context. Returns the final lifecycle state."
        ),
        structured_output=True,
    )
    def worker_terminate(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "terminate")

    @server.tool(
        name="worker_desktop_action",
        title="Launch Worker Desktop Action",
        description=(
            "Launch or focus a worker surface such as terminal, files, browser, codex, claude, or openclaw inside a sandbox or on the host computer. "
            "Use for explicit live viewing, steering, approval, browser opening, or workstation diagnostics. Do not add this to routine worker_delegate_once handoffs. "
            "Returns surface URLs or launch state; raw desktop URLs are diagnostic and should not be user-facing unless a watch/takeover surface is requested."
        ),
        structured_output=True,
    )
    def worker_desktop_action(worker_id: str, action: DesktopActionParam, url: str | None = None) -> dict[str, Any]:
        return client.desktop_action(worker_id, action, url=url)

    @server.tool(
        name="worker_takeover",
        title="Get Worker Takeover URLs",
        description=(
            "Return human-facing GlassHive operator URLs for watch, steer, and takeover. "
            "Use only when the user asks to watch, steer, approve, or take over live work; do not call for routine background delegation. "
            "Use operator_url/watch_url when present; direct_desktop_url is raw diagnostic noVNC only."
        ),
        structured_output=True,
    )
    def worker_takeover(worker_id: str) -> dict[str, Any]:
        live = client.worker_live(worker_id)
        takeover = client.takeover(worker_id)
        runtime_details = live.get("runtime_details", {})
        worker = live.get("worker") if isinstance(live.get("worker"), dict) else {}
        project_id = str((worker or {}).get("project_id") or "").strip()
        request_surface = _header_value(_request_headers(), HEADER_SURFACE)
        can_return_local_urls = surface_can_open_operator_url(request_surface)
        operator_url = surface_aware_watch_url(
            worker_id,
            project_id,
            request_surface=request_surface,
            watch_surface="desktop",
        )
        runtime_takeover_url = takeover.get("url") if can_return_local_urls else None
        direct_desktop_url = runtime_details.get("view_url") if can_return_local_urls else None
        view_url = operator_url or runtime_takeover_url or direct_desktop_url
        takeover_payload = (
            takeover
            if can_return_local_urls
            else {
                "supported": bool(takeover.get("supported")),
                "mode": takeover.get("mode"),
                "url_available": False,
            }
        )
        return {
            "takeover": takeover_payload,
            "operator_url": operator_url,
            "watch_url": operator_url,
            "view_url": view_url,
            "runtime_takeover_url": runtime_takeover_url,
            "direct_desktop_url": direct_desktop_url,
            "terminal_url": f"{base_url}/ui/workers/{worker_id}/terminal" if can_return_local_urls else None,
            "worker_url": operator_url,
            "project_runs": live.get("project_runs", []),
            "operator_url_available": bool(operator_url),
            "operator_url_surface": request_surface or "web",
        }

    @server.tool(
        name="run_get",
        title="Get Run",
        description=(
            "Fetch an individual run by run_id. Use for explicit diagnostics, status, or result inspection when the run id came from a previous tool result. "
            "For user follow-up, prefer workspace_status for a non-blocking check or workspace_wait for a blocking wait. "
            "Returns run state, output, and blocker/error details."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def run_get(run_id: str) -> dict[str, Any]:
        run = client.get_run(run_id)
        if _enterprise_mode_enabled():
            tenant_id, owner_id = _enterprise_request_scope()
            _require_enterprise_payload_scope(run, label="run", tenant_id=tenant_id)
            scoped_worker_id = str(run.get("worker_id") or "").strip()
            if not scoped_worker_id:
                raise PermissionError("GlassHive run is missing worker scope")
            worker = client.get_worker(scoped_worker_id)
            _require_enterprise_payload_scope(worker, label="worker", tenant_id=tenant_id, owner_id=owner_id)
        return run

    def _terminal_run_state(run: dict[str, Any]) -> bool:
        return str(run.get("state") or "").strip().lower() in {
            "completed",
            "failed",
            "cancelled",
            "canceled",
            "interrupted",
            "timed_out",
            "timeout",
        }

    def _run_state(run: dict[str, Any] | None) -> str:
        return str((run or {}).get("state") or "").strip().lower()

    def _run_failure_payload(run: dict[str, Any] | None) -> dict[str, Any]:
        if not run or str(run.get("state") or "").strip().lower() != "failed":
            return {
                "failure_class": None,
                "failure_retryable": False,
                "failure_user_message": None,
                "failure_recommended_recovery": None,
                "failure_diagnostic_summary": None,
            }
        failure_class = str(run.get("failure_class") or "").strip() or "unknown"
        retryable = bool(run.get("failure_retryable"))
        user_message = str(run.get("failure_user_message") or "").strip()
        recommended_recovery = str(run.get("failure_recommended_recovery") or "").strip()
        diagnostic_summary = _audit_preview(str(run.get("failure_diagnostic_summary") or run.get("error_text") or ""))
        if not user_message:
            user_message = "The GlassHive worker failed before it could finish."
        if not recommended_recovery:
            recommended_recovery = (
                "Use workspace_continue only when the user wants to preserve this workspace and try "
                "again from the current files/state; otherwise explain the blocker and include the "
                "View / Steer link."
            )
        return {
            "failure_class": failure_class,
            "failure_retryable": retryable,
            "failure_user_message": user_message,
            "failure_recommended_recovery": recommended_recovery,
            "failure_diagnostic_summary": diagnostic_summary,
        }

    def _run_order_key(run: dict[str, Any] | None) -> tuple[float, str]:
        if not run:
            return (0.0, "")
        for key in ("queued_at", "started_at", "ended_at", "created_at", "updated_at"):
            value = str(run.get(key) or "").strip()
            if value:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return (parsed.astimezone(timezone.utc).timestamp(), str(run.get("run_id") or "").strip())
                except ValueError:
                    continue
        return (0.0, str(run.get("run_id") or "").strip())

    def _run_is_newer(candidate: dict[str, Any] | None, requested_run: dict[str, Any] | None) -> bool:
        if not candidate:
            return False
        if not requested_run:
            return True
        candidate_id = str(candidate.get("run_id") or "").strip()
        requested_id = str(requested_run.get("run_id") or "").strip()
        return bool(candidate_id and candidate_id != requested_id and _run_order_key(candidate) > _run_order_key(requested_run))

    def _newer_worker_run(
        *,
        requested_run: dict[str, Any] | None,
        worker: dict[str, Any],
        live: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        latest_run_id = str((worker or {}).get("last_run_id") or "").strip()
        if latest_run_id and latest_run_id != str((requested_run or {}).get("run_id") or "").strip():
            try:
                latest = client.get_run(latest_run_id)
            except Exception:
                latest = None
            if _run_is_newer(latest, requested_run):
                return latest

        for key in ("runs", "project_runs"):
            raw_runs = (live or {}).get(key) if isinstance(live, dict) else None
            if not isinstance(raw_runs, list):
                continue
            for item in raw_runs:
                if not isinstance(item, dict):
                    continue
                candidate_id = str(item.get("run_id") or "").strip()
                if not candidate_id:
                    continue
                if candidate_id == str((requested_run or {}).get("run_id") or "").strip():
                    continue
                try:
                    candidate = client.get_run(candidate_id)
                except Exception:
                    candidate = item
                if _run_is_newer(candidate, requested_run):
                    return candidate
        return None

    def _workspace_status_payload(
        *,
        run_id: str | None = None,
        worker_id: str | None = None,
        include_live: bool = True,
        include_diagnostics: bool = False,
        prefer_active_newer_run: bool = False,
    ) -> dict[str, Any]:
        clean_run_id = str(run_id or "").strip()
        clean_worker_id = str(worker_id or "").strip()
        recent_context: RecentDispatchContext | None = None
        if not clean_run_id and not clean_worker_id:
            recent_context = _resolve_recent_dispatch_context()
            if not recent_context:
                raise ValueError(
                    "run_id or worker_id is required when no recent GlassHive launch is available "
                    "for this authenticated user/conversation"
                )
            clean_run_id = recent_context.run_id
            clean_worker_id = recent_context.worker_id
        run: dict[str, Any] | None = client.get_run(clean_run_id) if clean_run_id else None
        run_worker_id = str((run or {}).get("worker_id") or "").strip()
        if run_worker_id and clean_worker_id and run_worker_id != clean_worker_id:
            raise PermissionError("workspace_status worker_id must match the requested run")
        if run and not clean_worker_id:
            clean_worker_id = run_worker_id
        enterprise_scope: tuple[str, str] | None = None
        worker: dict[str, Any] = {}
        if _enterprise_mode_enabled():
            enterprise_scope = _enterprise_request_scope()
            tenant_id, owner_id = enterprise_scope
            if run:
                _require_enterprise_payload_scope(run, label="run", tenant_id=tenant_id)
                if not clean_worker_id:
                    raise PermissionError("GlassHive run is missing worker scope")
            if clean_worker_id:
                worker = client.get_worker(clean_worker_id)
                _require_enterprise_payload_scope(worker, label="worker", tenant_id=tenant_id, owner_id=owner_id)
        live: dict[str, Any] | None = None
        if include_live and clean_worker_id:
            live = client.worker_live(clean_worker_id)
        live_worker = live.get("worker") if isinstance(live, dict) and isinstance(live.get("worker"), dict) else {}
        if enterprise_scope and live_worker:
            tenant_id, owner_id = enterprise_scope
            _require_enterprise_payload_scope(live_worker, label="live worker", tenant_id=tenant_id, owner_id=owner_id)
        if live_worker:
            worker = live_worker
        live_requested_run: dict[str, Any] | None = None
        if run and clean_run_id and isinstance(live, dict):
            for key in ("runs", "project_runs"):
                raw_runs = live.get(key)
                if not isinstance(raw_runs, list):
                    continue
                live_requested_run = next(
                    (
                        item
                        for item in raw_runs
                        if isinstance(item, dict) and str(item.get("run_id") or "").strip() == clean_run_id
                    ),
                    None,
                )
                if live_requested_run:
                    break
        if live_requested_run and _run_state(live_requested_run) != _run_state(run):
            # Fetching live state can heal a completed run from runtime evidence.
            # Refetch before computing terminal status so the same wait/status call does
            # not return a stale pre-heal "running" row.
            refreshed_run = client.get_run(clean_run_id)
            if enterprise_scope:
                tenant_id, _owner_id = enterprise_scope
                _require_enterprise_payload_scope(refreshed_run, label="run", tenant_id=tenant_id)
            run = refreshed_run
        newer_run = _newer_worker_run(requested_run=run, worker=worker, live=live) if clean_worker_id else None
        if enterprise_scope and newer_run:
            tenant_id, _owner_id = enterprise_scope
            _require_enterprise_payload_scope(newer_run, label="latest run", tenant_id=tenant_id)
            newer_worker_id = str(newer_run.get("worker_id") or "").strip()
            if newer_worker_id and clean_worker_id and newer_worker_id != clean_worker_id:
                raise PermissionError("GlassHive latest run is not scoped to the requested worker")
        requested_run_stale = bool(run and _run_is_newer(newer_run, run))
        if newer_run and (not run or _run_is_newer(newer_run, run)):
            if (
                run
                and _terminal_run_state(run)
                and not _terminal_run_state(newer_run)
                and _run_state(run) != "interrupted"
                and not prefer_active_newer_run
            ):
                effective_run = run
            else:
                effective_run = newer_run
        else:
            effective_run = run
        if effective_run and not clean_worker_id:
            clean_worker_id = str(effective_run.get("worker_id") or "").strip()
        project_id = str((worker or {}).get("project_id") or (effective_run or run or {}).get("project_id") or "").strip()
        view_steer_url = None
        if clean_worker_id:
            worker_for_link = worker if worker else {"worker_id": clean_worker_id}
            view_steer_url = _signed_view_steer_url(
                worker_for_link,
                project_id or None,
                _header_value(_request_headers(), HEADER_SURFACE),
            )
        run_state = str((effective_run or {}).get("state") or "").strip() or None
        worker_state = str(
            (worker or {}).get("close_state") or (worker or {}).get("state") or ""
        ).strip() or None
        terminal = bool(effective_run and _terminal_run_state(effective_run))
        failure_payload = _run_failure_payload(effective_run)
        artifact_links: dict[str, Any] | None = None
        if terminal and clean_worker_id:
            try:
                artifact_worker = worker if worker else client.get_worker(clean_worker_id)
                artifacts = client.list_artifacts(clean_worker_id)
                artifact_links = _artifact_listing_payload(
                    worker=artifact_worker,
                    artifacts=artifacts,
                    include_open_links=True,
                    include_download_links=True,
                )
            except Exception as exc:
                artifact_links = {
                    "status": "unavailable",
                    "items": [],
                    "error": _audit_preview(str(exc), max_chars=220),
                }
        if artifact_links and not include_diagnostics:
            artifact_links = _compact_status_artifact_links(
                artifact_links,
                preferred_paths=_output_referenced_artifact_paths(str((effective_run or {}).get("output_text") or "")),
            )
        payload: dict[str, Any] = {
            "status": "ok",
            "mode": "non_blocking",
            "terminal": terminal,
            "run_state": run_state,
            "worker_state": worker_state,
            "output_text": (effective_run or {}).get("output_text") if effective_run else None,
            "error_text": (effective_run or {}).get("error_text") if effective_run else None,
            **failure_payload,
            "view_steer_url": view_steer_url,
            "view_steer": {
                "label": "View / Steer GlassHive workspace",
                "url": view_steer_url,
                "include_in_response": bool(view_steer_url),
            },
            "artifact_links": artifact_links,
        }
        if include_diagnostics:
            payload.update(
                {
                    "run_id": str((effective_run or {}).get("run_id") or clean_run_id or "").strip() or None,
                    "requested_run_id": clean_run_id or None,
                    "requested_run_state": str((run or {}).get("state") or "").strip() or None,
                    "requested_run_stale": requested_run_stale,
                    "latest_run_id": str((newer_run or effective_run or {}).get("run_id") or "").strip() or None,
                    "latest_run_state": str((newer_run or effective_run or {}).get("state") or "").strip() or None,
                    "worker_id": clean_worker_id or None,
                    "project_id": project_id or None,
                    "resolved_from_recent_dispatch": bool(recent_context),
                    "recent_dispatch_age_seconds": (
                        max(0, int(time.monotonic() - recent_context.created_monotonic)) if recent_context else None
                    ),
                    "run": effective_run,
                    "requested_run": run,
                    "latest_run": newer_run or effective_run,
                    "worker_live": live,
                }
            )
        return payload

    def _deliverable_ready_quiet_seconds() -> float:
        raw = os.environ.get("GLASSHIVE_DELIVERABLE_READY_QUIET_SEC", "2.0").strip()
        try:
            parsed = float(raw)
        except ValueError:
            return 2.0
        return max(parsed, 0.0)

    def _artifact_modified_at_epoch(item: dict[str, Any]) -> float | None:
        for key in ("modified_at", "mtime", "updated_at", "created_at"):
            raw = item.get(key)
            if raw is None:
                continue
            if isinstance(raw, (int, float)):
                return float(raw)
            text = str(raw).strip()
            if not text:
                continue
            try:
                return float(text)
            except ValueError:
                pass
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        return None

    def _ready_artifact_is_fresh_for_wait(
        item: dict[str, Any],
        *,
        wait_started_at: float | None,
        now_epoch: float | None = None,
    ) -> bool:
        if wait_started_at is None:
            return True
        modified_at = _artifact_modified_at_epoch(item)
        if modified_at is None:
            return False
        now = time.time() if now_epoch is None else now_epoch
        return (
            modified_at >= wait_started_at - 2.0
            and modified_at <= now - _deliverable_ready_quiet_seconds()
        )

    def _deliverable_ready_artifact_links(
        *,
        worker_id: str,
        include_diagnostics: bool = False,
        wait_started_at: float | None = None,
    ) -> dict[str, Any] | None:
        clean_worker_id = str(worker_id or "").strip()
        if not clean_worker_id:
            return None
        try:
            artifact_worker = client.get_worker(clean_worker_id)
            if _enterprise_mode_enabled():
                tenant_id, owner_id = _enterprise_request_scope()
                _require_enterprise_payload_scope(
                    artifact_worker,
                    label="artifact worker",
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            artifacts = client.list_artifacts(clean_worker_id)
            raw_items = artifacts.get("items", []) if isinstance(artifacts, dict) else []
            ready_items = [
                item
                for item in raw_items
                if isinstance(item, dict) and _wait_deliverable_ready_path(str(item.get("path") or ""))
                and _ready_artifact_is_fresh_for_wait(item, wait_started_at=wait_started_at)
            ]
            if not ready_items:
                return None
            artifacts = dict(artifacts or {})
            artifacts["items"] = ready_items
            artifact_links = _artifact_listing_payload(
                worker=artifact_worker,
                artifacts=artifacts,
                include_open_links=True,
                include_download_links=True,
            )
        except Exception as exc:
            LOGGER.debug(
                "workspace_wait deliverable readiness artifact listing failed worker_id=%s error=%s",
                clean_worker_id,
                exc,
            )
            return None
        artifact_links = dict(artifact_links)
        artifact_links["status"] = "ok"
        artifact_links["ready_before_run_terminal"] = True
        artifact_links["ready_item_count"] = len(artifact_links.get("items", []))
        if not include_diagnostics:
            artifact_links = _compact_status_artifact_links(artifact_links)
        return artifact_links

    @server.tool(
        name="workspace_status",
        title="Check GlassHive Workspace Status",
        description=(
            "Standalone non-blocking status/result check for GlassHive. Use this after workspace_launch, "
            "worker_delegate_once, or worker_run when the user asks whether the work is done, wants the "
            "latest result, or needs a quick status check. Do not call it immediately after launch "
            "unless the user asked for status/diagnostics. When workspace_launch returns "
            "follow_up_context, pass its run_id and worker_id here. Omit ids only when no "
            "follow_up_context was returned and the host preserves stable conversation context; "
            "GlassHive then resolves the most recent launch scoped "
            "to the authenticated user/conversation. This does not require LibreChat or host-app "
            "callback wiring. Returns run state, worker state, output/error text, and View / Steer "
            "link data when available."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_status(
        run_id: Annotated[str | None, Field(description="Run id from workspace_launch follow_up_context when returned. Omit only when the launch returned no follow_up_context and scoped recent dispatch is available.")] = None,
        worker_id: Annotated[str | None, Field(description="Worker id from workspace_launch follow_up_context when returned. Omit only when the launch returned no follow_up_context and scoped recent dispatch is available.")] = None,
        include_live: Annotated[bool, Field(description="Include worker live details when worker_id is available.")] = True,
        include_diagnostics: Annotated[bool, Field(description="Include raw run/workspace ids and live diagnostic snapshots.")] = False,
    ) -> dict[str, Any]:
        include_diagnostics = _effective_diagnostics_requested(
            include_diagnostics,
            tool_name="workspace_status",
        )
        return _workspace_status_payload(
            run_id=run_id,
            worker_id=worker_id,
            include_live=include_live,
            include_diagnostics=include_diagnostics,
        )

    @server.tool(
        name="workspace_wait",
        title="Wait For GlassHive Workspace Result",
        description=(
            "Standalone blocking wait for a GlassHive run. Use only when the user explicitly asks you "
            "to wait/check until done, or when a single-turn answer is more important than returning "
            "immediately. For normal long-running work, launch non-blocking first and use "
            "workspace_status later. If you just launched and the user asked you to wait in the same "
            "turn, first surface the View / Steer link returned by workspace_launch/worker_delegate_once "
            "when the chat protocol allows assistant text before another tool call, then call "
            "workspace_wait so the user is not left guessing where to watch progress. When timeout_seconds is omitted, GlassHive uses "
            "WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC capped by WPR_MCP_BLOCKING_WAIT_MAX_SEC. The default is "
            "intentionally bounded below common chat/proxy request timeouts; if this tool returns "
            "status=still_running and the user asked you to wait, immediately call workspace_wait again "
            "in the same conversation instead of leaving the user with a stale spinner. If the "
            "launch result includes follow_up_context, pass its returned run_id and worker_id to every "
            "workspace_wait call. Omit ids only when no follow_up_context was returned and the host "
            "preserves stable conversation context; GlassHive then resolves the most recent launch "
            "scoped to the authenticated user/conversation. This does not require "
            "LibreChat or host-app callback wiring. Omit poll_interval_seconds for normal work; "
            "do not invent polling values unless the user/operator gave a concrete override. "
            "GlassHive uses the configured efficient polling cadence, keeps early checks responsive, "
            "and backs off toward the configured cap during long waits. The runtime enforces that "
            "cadence as a floor, so very low polling intervals cannot create long-run status loops."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def workspace_wait(
        run_id: Annotated[str | None, Field(description="Run id from workspace_launch follow_up_context when returned. Omit only when the launch returned no follow_up_context and scoped recent dispatch is available.")] = None,
        worker_id: Annotated[str | None, Field(description="Worker id from workspace_launch follow_up_context when returned. Omit only when the launch returned no follow_up_context and scoped recent dispatch is available.")] = None,
        timeout_seconds: Annotated[float | None, Field(description="Maximum seconds to block before returning timeout status. Omit to use the configured GlassHive completion wait default.")] = None,
        poll_interval_seconds: Annotated[float | None, Field(description="Optional polling interval in seconds. Omit for the configured efficient default.")] = None,
        include_live: Annotated[bool, Field(description="Include worker live details in the final response when available.")] = True,
        include_diagnostics: Annotated[bool, Field(description="Include raw run/workspace ids and live diagnostic snapshots.")] = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        include_diagnostics = _effective_diagnostics_requested(
            include_diagnostics,
            tool_name="workspace_wait",
        )
        clean_run_id = str(run_id or "").strip()
        clean_worker_id = str(worker_id or "").strip()
        resolved_recent_context: RecentDispatchContext | None = None
        if not clean_run_id and not clean_worker_id:
            resolved_recent_context = _resolve_recent_dispatch_context()
            if not resolved_recent_context:
                raise ValueError(
                    "run_id or worker_id is required when no recent GlassHive launch is available "
                    "for this authenticated user/conversation"
                )
            clean_run_id = resolved_recent_context.run_id
            clean_worker_id = resolved_recent_context.worker_id
        max_wait = _blocking_wait_max_seconds()
        requested_timeout = (
            _blocking_wait_default_seconds()
            if timeout_seconds is None
            else _finite_tool_float(timeout_seconds, field_name="timeout_seconds")
        )
        timeout = max(0.0, min(requested_timeout, max_wait))
        default_interval = _blocking_wait_default_poll_interval_seconds()
        requested_interval = (
            default_interval
            if poll_interval_seconds is None
            else _finite_tool_float(poll_interval_seconds, field_name="poll_interval_seconds")
        )
        if requested_interval <= 0:
            raise ValueError("poll_interval_seconds must be greater than 0")
        interval = max(default_interval, min(requested_interval, 30.0))
        adaptive_polling = poll_interval_seconds is None
        deadline = time.monotonic() + timeout
        wait_started = time.monotonic()
        wait_started_wall = time.time()
        attempts = 0
        LOGGER.info(
            "workspace_wait start run_id=%s worker_id=%s timeout_seconds=%.3f requested_timeout_seconds=%.3f "
            "poll_interval_seconds=%.3f include_live=%s include_diagnostics=%s",
            clean_run_id,
            clean_worker_id,
            timeout,
            requested_timeout,
            interval,
            include_live,
            include_diagnostics,
        )
        while True:
            attempts += 1
            payload = await asyncio.to_thread(
                _workspace_status_payload,
                run_id=clean_run_id,
                worker_id=clean_worker_id,
                include_live=include_live,
                include_diagnostics=include_diagnostics,
                prefer_active_newer_run=True,
            )
            if resolved_recent_context and include_diagnostics:
                payload.update(
                    {
                        "resolved_from_recent_dispatch": True,
                        "recent_dispatch_age_seconds": max(
                            0,
                            int(time.monotonic() - resolved_recent_context.created_monotonic),
                        ),
                    }
                )
            if payload.get("terminal"):
                elapsed = time.monotonic() - wait_started
                terminal_status = "completed" if payload.get("run_state") == "completed" else "terminal"
                await _report_workspace_wait_progress(
                    ctx,
                    elapsed_seconds=elapsed,
                    timeout_seconds=timeout,
                    attempts=attempts,
                    status=terminal_status,
                    run_state=payload.get("run_state"),
                )
                LOGGER.info(
                    "workspace_wait terminal run_id=%s worker_id=%s status=%s run_state=%s attempts=%s elapsed_seconds=%.3f",
                    clean_run_id,
                    clean_worker_id,
                    terminal_status,
                    payload.get("run_state"),
                    attempts,
                    elapsed,
                )
                payload.update(
                    {
                        "status": terminal_status,
                        "mode": "blocking_wait",
                        "waited": True,
                        "attempts": attempts,
                        "timed_out": False,
                    }
                )
                return payload
            ready_artifact_links = _deliverable_ready_artifact_links(
                worker_id=clean_worker_id or str(payload.get("worker_id") or ""),
                include_diagnostics=include_diagnostics,
                wait_started_at=wait_started_wall,
            )
            if ready_artifact_links:
                elapsed = time.monotonic() - wait_started
                ready_count = ready_artifact_links.get("count") or ready_artifact_links.get("ready_item_count")
                await _report_workspace_wait_progress(
                    ctx,
                    elapsed_seconds=elapsed,
                    timeout_seconds=timeout,
                    attempts=attempts,
                    status="deliverable_ready",
                    run_state=payload.get("run_state"),
                )
                LOGGER.info(
                    "workspace_wait deliverable_ready run_id=%s worker_id=%s run_state=%s attempts=%s elapsed_seconds=%.3f ready_items=%s",
                    clean_run_id,
                    clean_worker_id,
                    payload.get("run_state"),
                    attempts,
                    elapsed,
                    ready_count,
                )
                payload.update(
                    {
                        "status": "deliverable_ready",
                        "mode": "blocking_wait",
                        "terminal": True,
                        "waited": True,
                        "attempts": attempts,
                        "timed_out": False,
                        "wait_deadline_reached": False,
                        "worker_finalization_pending": True,
                        "completion_class": "deliverables_ready_worker_finalizing",
                        "artifact_links": ready_artifact_links,
                        "output_text": payload.get("output_text")
                        or (
                            "GlassHive has produced user-facing deliverables and signed download "
                            "links are ready. The worker may still be finishing final self-checks; "
                            "deliver these files to the user now instead of leaving the chat waiting."
                        ),
                        "recommended_next_tool": "workspace_status",
                        "recommended_next_action": (
                            "Deliver the signed artifact links to the user now. Mention that the "
                            "workspace may still be finalizing, and use workspace_status later only "
                            "if the user asks for a final status refresh."
                        ),
                    }
                )
                return payload
            elapsed = time.monotonic() - wait_started
            await _report_workspace_wait_progress(
                ctx,
                elapsed_seconds=elapsed,
                timeout_seconds=timeout,
                attempts=attempts,
                status="running",
                run_state=payload.get("run_state"),
            )
            if time.monotonic() >= deadline:
                LOGGER.info(
                    "workspace_wait timeout run_id=%s worker_id=%s run_state=%s attempts=%s elapsed_seconds=%.3f",
                    clean_run_id,
                    clean_worker_id,
                    payload.get("run_state"),
                    attempts,
                    elapsed,
                )
                await _report_workspace_wait_progress(
                    ctx,
                    elapsed_seconds=elapsed,
                    timeout_seconds=timeout,
                    attempts=attempts,
                    status="still_running",
                    run_state=payload.get("run_state"),
                )
                ready_artifact_links = _deliverable_ready_artifact_links(
                    worker_id=clean_worker_id or str(payload.get("worker_id") or ""),
                    include_diagnostics=include_diagnostics,
                    wait_started_at=wait_started_wall,
                )
                if ready_artifact_links:
                    ready_count = ready_artifact_links.get("count") or ready_artifact_links.get("ready_item_count")
                    LOGGER.info(
                        "workspace_wait deliverable_ready run_id=%s worker_id=%s run_state=%s attempts=%s ready_items=%s",
                        clean_run_id,
                        clean_worker_id,
                        payload.get("run_state"),
                        attempts,
                        ready_count,
                    )
                    payload.update(
                        {
                            "status": "deliverable_ready",
                            "mode": "blocking_wait",
                            "terminal": True,
                            "waited": True,
                            "attempts": attempts,
                            "timed_out": False,
                            "wait_deadline_reached": True,
                            "worker_finalization_pending": True,
                            "completion_class": "deliverables_ready_worker_finalizing",
                            "artifact_links": ready_artifact_links,
                            "output_text": payload.get("output_text")
                            or (
                                "GlassHive has produced user-facing deliverables and signed download "
                                "links are ready. The worker may still be finishing final self-checks; "
                                "deliver these files to the user now instead of leaving the chat waiting."
                            ),
                            "recommended_next_tool": "workspace_status",
                            "recommended_next_action": (
                                "Deliver the signed artifact links to the user now. Mention that the "
                                "workspace may still be finalizing, and use workspace_status later only "
                                "if the user asks for a final status refresh."
                            ),
                        }
                    )
                    return payload
                payload.update(
                    {
                        "status": "still_running",
                        "mode": "blocking_wait",
                        "waited": True,
                        "attempts": attempts,
                        "timed_out": True,
                        "wait_again_recommended": True,
                        "completion_still_pending": True,
                        "do_not_ask_user_to_keep_waiting": True,
                        "recommended_next_tool": "workspace_wait",
                        "recommended_next_action": (
                            "The worker is still running. If the user asked to wait, call "
                            "workspace_wait again in this same conversation so the chat gets the "
                            "completed result without holding one HTTP request past infrastructure "
                            "timeouts. Do not ask the user to say 'keep waiting' unless the host tool "
                            "budget is genuinely exhausted or a non-retryable failure is returned. "
                            "Include the View / Steer link if you answer before waiting again."
                        ),
                    }
                )
                return payload
            sleep_interval = _blocking_wait_sleep_interval_seconds(
                attempts=attempts,
                base_interval=interval,
                adaptive=adaptive_polling,
            )
            await asyncio.sleep(min(sleep_interval, max(0.0, deadline - time.monotonic())))

    @server.tool(
        name="workspace_continue",
        title="Continue GlassHive Workspace",
        description=(
            "Queue a continuation run on the same GlassHive worker after workspace_status or workspace_wait "
            "shows a failed, interrupted, cancelled, paused, or completed run that the user wants to continue. "
            "Use this when the user says retry, continue, finish it, or resume from the same workspace. "
            "This is an explicit user-requested recovery path, not an automatic retry loop. It preserves "
            "the original instruction and current workspace files/state instead of relaunching from scratch. "
            "Returns a fresh run, View / Steer link, and result_tools for status/wait."
        ),
        structured_output=True,
    )
    def workspace_continue(
        run_id: Annotated[str, Field(description="Previous run id from diagnostics or an explicit workspace_status/workspace_wait result.")],
        worker_id: Annotated[str | None, Field(description="Optional worker id. If omitted, GlassHive derives it from the previous run.")] = None,
        continuation_goal: Annotated[
            str | None,
            Field(description="Optional extra instruction from the user, such as 'continue but avoid web search loops'."),
        ] = None,
        connected_account_content_intent: Annotated[
            bool,
            Field(
                description=(
                    "Optional compatibility hint for hosts that want GlassHive to warn when "
                    "connected-account content is requested but no complete broker grant/config "
                    "is supplied. Prior broker-absence warnings are re-evaluated against any "
                    "fresh bootstrap bundle."
                )
            ),
        ] = False,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        file_upload_ids: FileUploadIdsParam = None,
        file_binding_key: FileBindingKeyParam = None,

        effort: Annotated[
            str | None,
            Field(
                description=(
                    "Optional effort override for this continuation. Codex accepts none/low/medium/high/xhigh; "
                    "minimal is for explicitly allowlisted deployments only. "
                    "Claude Code accepts default/low/medium/high/xhigh/max; OpenClaw accepts high/max. Omit to use the user's saved default. "
                    + HIGH_EFFORT_SELECTION_GUIDANCE
                )
            ),
        ] = None,
        include_diagnostics: Annotated[bool, Field(description="Include raw run/workspace ids and continuation diagnostics.")] = False,
    ) -> dict[str, Any]:
        include_diagnostics = _effective_diagnostics_requested(
            include_diagnostics,
            tool_name="workspace_continue",
        )
        clean_run_id = str(run_id or "").strip()
        if not clean_run_id:
            raise ValueError("run_id is required")
        previous_run = client.get_run(clean_run_id)
        previous_state = str(previous_run.get("state") or "").strip().lower()
        if previous_state in {"queued", "running"}:
            raise ValueError("workspace_continue is only for terminal, paused, or completed runs; use workspace_status/workspace_wait for active runs")
        clean_worker_id = str(worker_id or previous_run.get("worker_id") or "").strip()
        if not clean_worker_id:
            raise ValueError("worker_id is required when the previous run does not include it")
        previous_worker_id = str(previous_run.get("worker_id") or "").strip()
        if not previous_worker_id:
            raise PermissionError("workspace_continue requires the previous run to include a worker_id")
        if previous_worker_id != clean_worker_id:
            raise PermissionError("workspace_continue worker_id must match the previous run")
        worker = client.get_worker(clean_worker_id)
        previous_project_id = str(previous_run.get("project_id") or "").strip()
        worker_project_id = str(worker.get("project_id") or "").strip()
        if previous_project_id and worker_project_id and previous_project_id != worker_project_id:
            raise PermissionError("workspace_continue worker project must match the previous run")
        if _enterprise_mode_enabled():
            tenant_id, owner_id = _enterprise_request_scope()
            _require_enterprise_payload_scope(previous_run, label="previous run", tenant_id=tenant_id)
            _require_enterprise_payload_scope(worker, label="worker", tenant_id=tenant_id, owner_id=owner_id)
        try:
            preferences = _normalize_preferences(client.get_preferences())
        except Exception:
            preferences = {}
        worker_profile = str(worker.get("profile") or "").strip()
        resolved_effort = _resolve_effort_for_profile(worker_profile, effort, preferences)
        continuation_context = build_workspace_continuation_context(
            previous_run=previous_run,
            continuation_goal=continuation_goal,
        )
        instruction = continuation_instruction(
            previous_run=previous_run,
            continuation_context=continuation_context,
        )
        parsed_bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json)
        parsed_bundle = _merge_request_context(parsed_bundle)
        prior_connected_account_guard = CONNECTED_ACCOUNT_NO_BROKER_NOTE in str(previous_run.get("instruction") or "")
        should_send_bootstrap_bundle = (
            bootstrap_bundle_json is not None or connected_account_content_intent or prior_connected_account_guard
        )
        parsed_bundle, instruction = _apply_connected_account_intent_guard(
            parsed_bundle,
            instruction,
            connected_account_content_intent=connected_account_content_intent or prior_connected_account_guard,
        )
        instruction = instruction or ""
        validate_file_transport(file_upload_ids, bundle=parsed_bundle, binding_key=file_binding_key, bind=True)
        if file_upload_ids:
            WorkspaceFilesMcpClient(client).bind(clean_worker_id, file_upload_ids, file_binding_key)
        if should_send_bootstrap_bundle and parsed_bundle is not None:
            new_run = client.assign_run(
                clean_worker_id,
                instruction,
                effort=resolved_effort or None,
                bootstrap_bundle=parsed_bundle,
                continuation_context=continuation_context,
            )
        else:
            new_run = client.assign_run(
                clean_worker_id,
                instruction,
                effort=resolved_effort or None,
                continuation_context=continuation_context,
            )
        if _enterprise_mode_enabled():
            _require_enterprise_payload_scope(new_run, label="continued run", tenant_id=tenant_id)
        request_surface = _header_value(_request_headers(), HEADER_SURFACE)
        dispatch_context = _dispatch_follow_up_context(
            worker=worker,
            project_id=str(worker.get("project_id") or previous_run.get("project_id") or ""),
            run=new_run,
            request_surface=request_surface,
            task=(
                str(worker.get("name") or "").strip()
                or "Continue GlassHive workspace"
            ),
            explicit_follow_up_required=not bool(
                _recent_dispatch_scope_keys(
                    owner_id=str(worker.get("owner_id") or ""),
                    for_remember=True,
                )
            ),
        )
        _remember_recent_dispatch_context(
            worker=worker,
            project_id=str(worker.get("project_id") or previous_run.get("project_id") or ""),
            run=new_run,
        )
        payload = {
            "status": "queued",
            "previous_run_state": previous_state,
            "previous_failure_class": str(previous_run.get("failure_class") or "") or None,
            "effort": resolved_effort,
            "acknowledgement_guidance": (
                "Tell the user GlassHive is continuing in the same workspace, include the View / Steer "
                "link when present, and use the returned result_tools for later status or wait."
            ),
            **dispatch_context,
        }
        if include_diagnostics:
            payload.update(
                {
                    "previous_run_id": clean_run_id,
                    "run": new_run,
                    "continuation_instruction_preview": _audit_preview(instruction, max_chars=900),
                }
            )
        return payload

    @server.tool(
        name="workspace_artifacts",
        title="List GlassHive Workspace Artifacts",
        description=(
            "Use after workspace_status/workspace_wait or when the user asks for generated files, "
            "downloads, artifacts, or delivery files from a GlassHive worker. Returns workspace file "
            "artifacts with short-lived signed_open_url preview links and signed_download_url raw-download "
            "links when GlassHive can safely expose them. Prefer signed_download_url/default_url as the "
            "default user-facing file link, and include signed_open_url or the View / Steer workspace "
            "link when the user needs to inspect previews or all deliveries. Prefer this instead of "
            "pasting generated file contents into chat. Do not use it "
            "before the worker has produced files unless the user asks for diagnostics."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_artifacts(
        worker_id: Annotated[str, Field(description="Worker id from diagnostics, an explicit status result, or a known GlassHive workspace.")],
        include_download_links: Annotated[bool, Field(description="Include short-lived signed download URLs for each file artifact.")] = True,
    ) -> dict[str, Any]:
        clean_worker_id = str(worker_id or "").strip()
        if not clean_worker_id:
            raise ValueError("worker_id is required")
        worker = client.get_worker(clean_worker_id)
        if _enterprise_mode_enabled():
            tenant_id, owner_id = _enterprise_request_scope()
            _require_enterprise_payload_scope(worker, label="worker", tenant_id=tenant_id, owner_id=owner_id)
        artifacts = client.list_artifacts(clean_worker_id)
        return _artifact_listing_payload(
            worker=worker,
            artifacts=artifacts,
            include_download_links=include_download_links,
        )

    @server.tool(
        name="workspace_artifact_download",
        title="Get GlassHive Artifact Download Link",
        description=(
            "Use when the user asks to download, open, receive, or inspect one specific file generated "
            "inside a GlassHive workspace. Returns a short-lived signed_open_url for a GlassHive file "
            "preview/landing page and a signed_download_url for the raw file download, each scoped to that "
            "worker, tenant, user, and path. Prefer workspace_artifacts first when the path is unknown. "
            "Do not expose local filesystem paths or ask the user to manually save pasted content when "
            "a scoped GlassHive file link is available."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def workspace_artifact_download(
        worker_id: Annotated[str, Field(description="Worker id from diagnostics, an explicit status result, or a known GlassHive workspace.")],
        path: Annotated[str, Field(description="Workspace-relative file path, for example index.html or output/report.pdf.")],
    ) -> dict[str, Any]:
        clean_worker_id = str(worker_id or "").strip()
        if not clean_worker_id:
            raise ValueError("worker_id is required")
        worker = client.get_worker(clean_worker_id)
        if _enterprise_mode_enabled():
            tenant_id, owner_id = _enterprise_request_scope()
            _require_enterprise_payload_scope(worker, label="worker", tenant_id=tenant_id, owner_id=owner_id)
        clean_path = _clean_artifact_relative_path(path)
        if not clean_path:
            raise ValueError("path is required or invalid")
        open_url = _signed_artifact_open_url(worker, clean_path)
        download_url = _signed_artifact_download_url(worker, clean_path)
        return {
            "status": "ok" if open_url or download_url else "unavailable",
            "worker_id": clean_worker_id,
            "path": clean_path,
            "signed_open_url": open_url,
            "signed_download_url": download_url,
            "download_link_ttl_seconds": signed_link_ttl_seconds(),
            "next_action_guidance": (
                "Use signed_download_url as the default user-facing file link when present. Also include "
                "signed_open_url or the View / Steer link when the user needs preview/manual access or "
                "wants to inspect all workspace deliveries. If unavailable, call workspace_artifacts."
            ),
        }

    @server.tool(
        name="metrics_summary",
        title="Metrics Summary",
        description=(
            "Fetch runtime-level project, worker, run, and event counts. Use for diagnostics, health checks, capacity checks, or admin-style visibility. "
            "Do not use for ordinary task delegation or user-facing task completion. Returns aggregate counts only, not project content or worker outputs."
        ),
        structured_output=True,
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def metrics_summary() -> dict[str, Any]:
        return client.metrics()

    @server.resource(
        "wpr://projects",
        name="projects",
        title="GlassHive Workspace Projects",
        description="Current projects visible to the MCP server.",
        mime_type="application/json",
    )
    def projects_resource() -> str:
        return json.dumps(client.list_projects(), indent=2)

    @server.resource(
        "wpr://projects/{project_id}",
        name="project",
        title="GlassHive Workspace Project",
        description="A single project record.",
        mime_type="application/json",
    )
    def project_resource(project_id: str) -> str:
        return json.dumps(client.get_project(project_id), indent=2)

    @server.resource(
        "wpr://projects/{project_id}/workers",
        name="project-workers",
        title="GlassHive Workspaces For Project",
        description="Workspaces belonging to a GlassHive project.",
        mime_type="application/json",
    )
    def project_workers_resource(project_id: str) -> str:
        return json.dumps(client.list_workers(project_id), indent=2)

    @server.resource(
        "wpr://workers/{worker_id}",
        name="worker",
        title="GlassHive Workspace Record",
        description="The current workspace record.",
        mime_type="application/json",
    )
    def worker_resource(worker_id: str) -> str:
        return json.dumps(client.get_worker(worker_id), indent=2)

    @server.resource(
        "wpr://workers/{worker_id}/live",
        name="worker-live",
        title="GlassHive Workspace Live State",
        description="Rich live state for a workspace, including recent runs, events, and runtime details.",
        mime_type="application/json",
    )
    def worker_live_resource(worker_id: str) -> str:
        return json.dumps(client.worker_live(worker_id), indent=2)

    @server.resource(
        "wpr://runs/{run_id}",
        name="run",
        title="GlassHive Run Record",
        description="A single GlassHive run record.",
        mime_type="application/json",
    )
    def run_resource(run_id: str) -> str:
        return json.dumps(client.get_run(run_id), indent=2)

    @server.resource(
        "wpr://schedules/{schedule_id}",
        name="schedule",
        title="Schedule Record",
        description="A single GlassHive-native schedule record.",
        mime_type="application/json",
    )
    def schedule_resource(schedule_id: str) -> str:
        return json.dumps(client.get_schedule(schedule_id), indent=2)

    @server.resource(
        "wpr://metrics/summary",
        name="metrics-summary",
        title="Runtime Metrics Summary",
        description="Runtime-level metrics snapshot.",
        mime_type="application/json",
    )
    def metrics_resource() -> str:
        return json.dumps(client.metrics(), indent=2)

    @server.prompt(
        name="delegate_project_goal",
        title="Delegate Project Goal",
        description="Generate an operator-ready brief for a project/worker delegation run.",
    )
    def delegate_project_goal(
        project_id: str,
        worker_id: str,
        task: str,
        checkpoint_instruction: str | None = None,
    ) -> str:
        project = client.get_project(project_id)
        worker = client.get_worker(worker_id)
        checkpoint = checkpoint_instruction or "Pause before risky external writes so a human can review."
        return (
            f"Project: {project['title']}\n"
            f"Goal: {project['goal']}\n"
            f"Worker: {worker['name']} ({worker['profile']})\n"
            f"Task: {task}\n"
            f"Checkpoint: {checkpoint}\n"
            "When useful, fetch worker_live or worker_takeover first so you can monitor the worker and hand off control."
        )

    register_owner_peer_tools(server, client)
    register_owner_configuration_tools(server, client)
    from .workspace_file_mcp import install_workspace_file_tools

    install_workspace_file_tools(server, client)
    return server


def _mcp_tls_files() -> dict[str, str]:
    """Serve HTTPS directly when a package supplies both certificate and key."""

    certificate = str(os.environ.get("GLASSHIVE_MCP_TLS_CERT_FILE") or "").strip()
    key = str(os.environ.get("GLASSHIVE_MCP_TLS_KEY_FILE") or "").strip()
    if bool(certificate) != bool(key):
        raise RuntimeError("MCP TLS requires both a certificate and a key file")
    return {"ssl_certfile": certificate, "ssl_keyfile": key} if certificate else {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Workers & Projects Runtime MCP wrapper")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default=os.environ.get("WPR_MCP_TRANSPORT", "stdio"))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    _require_enterprise_mcp_transport(args.transport)
    server = create_mcp_server(base_url=args.base_url.rstrip("/"), host=args.host, port=args.port)
    if args.transport == "streamable-http":
        import uvicorn

        app = server.streamable_http_app()
        if server.settings.auth is None:
            app.add_middleware(McpHttpAuthMiddleware)
        uvicorn.run(app, host=args.host, port=args.port, access_log=False, **_mcp_tls_files())
        return
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
