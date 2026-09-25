from __future__ import annotations

import base64
import binascii
import json
import fcntl
import hashlib
import hmac
import logging
import math
import os
import re
import signal
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread, Timer, current_thread
from urllib.parse import urlencode, urlparse

import httpx

from .runtime_requirements import CLAUDE_CODE_EFFORT_LEVELS

from .peer_collaboration import PeerCollaboration
from .auth import multi_user_security_enabled
from .bootstrap import GLASSHIVE_PROVIDER_SESSION_MODE_ENV

from .deliverables import (
    is_unmodified_user_input,
    PROFESSIONAL_ARTIFACT_EXTENSIONS,
    SUPPORT_ARTIFACT_DIR_NAMES,
    deliverable_payload,
    is_user_deliverable_relative_path,
    is_valid_professional_artifact,
    native_media_observations,
    NATIVE_MEDIA_PREFIX, NATIVE_IMAGE_SUFFIXES, _publish_native_media, _native_media_snapshot,
)
from .control_plane import (
    PROFILE_ACCOUNT_PROVIDERS,
    WORKSPACE_ACCOUNT_POLICIES,
    ControlPlaneConflict,
    ControlPlaneStore,
)
from .failure_classification import classify_runtime_error
from .models import (
    CLOSED_WORKER_STATES,
    WorkspaceKind,
    normalize_workspace_kind,
    normalize_workspace_tags,
    utc_now,
)
from .native_model_selection import ModelConfigurationRequired, selected_grok_model
from .mission_provider_accounts import (
    ProviderAccountBusyError,
    deployment_provider_readiness,
    mission_provider_account_selection,
    other_live_provider_lease,
)
from .openclaw_runtime import (
    HostCapacityError,
    ProviderRateLimitError,
    RuntimeErrorBase,
    RuntimeInfo,
    RunStartupRejectedError,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
)
from .operator_urls import surface_aware_watch_url
from .recurrence import (
    DELEGATED_RECURRENCE_OWNER,
    NATIVE_RECURRENCE_OWNER,
    RECURRENCE_OWNERS,
    canonical_recurrence_owner,
    due_occurrences_and_next,
    first_occurrence_at,
    normalize_recurrence_spec,
    parse_aware_utc,
    recurrence_owner_storage_value,
)
from .runtime_env import load_viventium_runtime_env
from .runtime_identity import derive_legacy_backend_label
from .run_actions import (
    RunActionError,
    mint_run_action_capability,
    unverified_run_action_claims,
    verify_run_action_capability,
)
from .scheduling_owner import SchedulingOwnerIdentity, ViventiumSchedulingOwnerClient
from .signed_links import (
    append_signed_query,
    create_signed_link_ref,
    is_worker_signed_link_revoked,
    revoke_signed_link_refs_for_worker,
    sign_link_params,
    signed_link_ref_url,
    sign_link_token,
)
from .store import (
    MANAGED_SHUTDOWN_LEASE_RELEASE_REASON,
    ProviderAccountBusyStoreError,
    SchedulePrincipalAuthorityStoreError,
    Store,
    WorkerClosedStoreError,
)
from .workspace_continuation import continuation_instruction

import fcntl

import math

import signal

import subprocess

import sys

from dataclasses import dataclass

from threading import Condition, Event, Lock, Thread

from typing import Any, Callable

from urllib.parse import quote, urlencode, urlparse

from .broker_admission import (
    BrokerAdmissionError,
    admit_capability_grant,
    prepare_scheduled_provider_authorization,
    preflight_provider_authorization,
    revoke_capability_grant,
)

from .failure_classification import classify_runtime_error, is_user_resumable_failure

from .local_qa_control import (
    AUTHORITY_KEYS as LOCAL_QA_AUTHORITY_KEYS,
    CANDIDATE_DIGEST_ENV as LOCAL_QA_CANDIDATE_DIGEST_ENV,
    COMPONENT_ARTIFACT_DIGEST_ENV as LOCAL_QA_COMPONENT_DIGEST_ENV,
    RUN_SCOPED_FAULTS as LOCAL_QA_RUN_SCOPED_FAULTS,
    LocalQAControlPlane,
    LocalQAFaultDirective,
)

from .models import (
    normalize_worker_resource_class,
    utc_now,
    worker_resource_memory_bytes,
)

from .native_team import NativeTeamProjection

from .openclaw_runtime import (
    HostCapacityError,
    ProviderRateLimitError,
    RuntimeErrorBase,
    RuntimeInfo,
    RunStartupRejectedError,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
)

from .run_states import TERMINAL_RUN_STATES

from .signed_links import (
    append_signed_query,
    create_signed_link_ref,
    is_worker_signed_link_revoked,
    revoke_signed_link_refs_for_worker,
    sign_link_params,
    signed_link_ref_url,
    sign_link_token,
)

from .store import (
    CallbackIntentGenerationConflictError,
    canonical_parallel_clean_room_bootstrap,
    is_parallel_clean_room_bootstrap,
    DelegationIdempotencyConflictError,
    HostRunLeaseCapacityError,
    IsolatedParallelAdmissionConflictError,
    STEER_REPLACEMENT_SUPPRESSED_ERROR,
    Store,
    WorkAdmissionError,
    work_artifact_observation,
)

from .workspace_continuation import (
    accepted_run_input,
    build_workspace_continuation_context,
    continuation_instruction,
)

from .worker_isolation_audit import capture_worker_isolation_audit


logger = logging.getLogger(__name__)
TERMINAL_CALLBACK_MESSAGE_LIMIT = 4000
FINAL_REPORT_PATTERN = re.compile(
    r"(?mi)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]*)?"
    r"(?:(?:[*_]{1,3}|`{1,3})[ \t]*)?FINAL REPORT\s*:\s*"
    r"(?:(?:[*_]{1,3}|`{1,3})[ \t]*)?"
)
VIVENTIUM_CALLBACK_PATH = "/api/viventium/glasshive/callback"
SCHEDULING_CORTEX_CALLBACK_PATH = "/internal/scheduled-prompts/glasshive-callback"
ACTIONABLE_CALLBACK_LINK_EVENTS = {
    "run.failed",
    "run.paused",
    "run.interrupted",
    "run.cancelled",
}
PARENT_VISIBLE_CALLBACK_FIELDS = ("user_id", "conversation_id", "parent_message_id", "message_id")
CALLBACK_DEAD_LETTER_IMMEDIATE_STATUS_CODES = {400, 401, 403, 404, 409, 410, 422, 501}
CALLBACK_RETRYABLE_STATUS_CODES = {408, 425, 429}
RUN_STATE_BY_EVENT = {
    "run.queued": "queued",
    "run.waiting_on_capacity": "queued",
    "run.started": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.paused": "paused",
    "run.interrupted": "interrupted",
    "run.cancelled": "cancelled",
}
_UNSET = object()

WORK_TRACE_SCHEMA_DIGEST = (
    "sha256:ba9b15e022a451c62be0c0f30a02d6615bea83e868b2ffdd349beff75002e790"
)
WORK_TRACE_PRODUCER_SOURCE_IDENTITY = "workers_projects_runtime.api:get_active_work"
# Pinned by the existing Python-to-TypeScript golden fixture, which proves the
# exact recursive emitted key set.
WORK_TRACE_EMITTED_KEY_SET_DIGEST = (
    "sha256:3a109b0f41a08755252a050e444dd6780e7bf95aec194ad95628e4e7a5c3a253"
)


def work_trace_emitted_key_set_digest(value: object) -> str:
    paths: set[str] = set()

    def visit(current: object, prefix: str) -> None:
        if isinstance(current, list):
            for item in current:
                visit(item, f"{prefix}[]")
            return
        if not isinstance(current, dict):
            return
        for key in sorted(str(item) for item in current):
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            visit(current[key], path)

    visit(value, "")
    encoded = json.dumps(sorted(paths), separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


WORKER_PROMPT_LAYER_CONTRACT_VERSION = 1
WORKER_PROMPT_LAYER_PRODUCER_SCOPE = "glasshive.worker_prompt_registry"
WORKER_PROMPT_LAYER_REGISTRY = frozenset(
    {
        "agents_md",
        "claude_md",
        "codex_md",
        "developer_instructions",
        "glasshive_worker_project_contract",
        "harness_prompt",
        "mcp_server_instructions",
        "project_definition",
        "run_instruction",
        "system_instructions",
        "tool_schemas",
        "viventium_feeling_state",
    }
)


def worker_prompt_layer_actual_producers() -> tuple[object, ...]:
    """Enumerate the real prompt-producing code boundary independently."""

    from .bootstrap import (
        canonicalize_viventium_feeling_projection,
        glasshive_project_agents_md,
        glasshive_project_claude_md,
        glasshive_project_codex_md,
        merge_glasshive_worker_instructions,
    )
    from .mcp_server import create_mcp_server, glasshive_workers_server_instructions
    from .coordinator import prompt_manifest
    from .profile_runtime import (
        HostNativeCliMixin,
        _apply_codex_developer_instructions,
        _instruction_with_completion_contract,
    )

    return (
        prompt_manifest,
        canonicalize_viventium_feeling_projection,
        glasshive_project_agents_md,
        glasshive_project_claude_md,
        glasshive_project_codex_md,
        merge_glasshive_worker_instructions,
        glasshive_workers_server_instructions,
        create_mcp_server,
        _apply_codex_developer_instructions,
        _instruction_with_completion_contract,
        HostNativeCliMixin._host_project_definition,
        HostNativeCliMixin._host_harness_prompt,
    )


def _worker_prompt_producer_ref(producer: object) -> str:
    declared = str(
        getattr(producer, "__glasshive_worker_prompt_producer_ref__", "") or ""
    )
    if declared:
        return declared
    return f"{getattr(producer, '__module__', '')}.{getattr(producer, '__qualname__', '')}"


def worker_prompt_layer_producer_bindings() -> dict[str, tuple[str, ...]]:
    """Read declarations from the independently enumerated producer boundary."""

    from .bootstrap import WORKER_PROMPT_LAYER_DECLARATION_ATTRIBUTE

    bindings: dict[str, tuple[str, ...]] = {}
    for producer in worker_prompt_layer_actual_producers():
        producer_ref = _worker_prompt_producer_ref(producer)
        declared = getattr(producer, WORKER_PROMPT_LAYER_DECLARATION_ATTRIBUTE, None)
        if (
            isinstance(declared, tuple)
            and len(declared) == 2
            and declared[0] == producer_ref
            and isinstance(declared[1], tuple)
            and all(isinstance(name, str) and name for name in declared[1])
        ):
            bindings[producer_ref] = tuple(declared[1])
    return dict(sorted(bindings.items()))


def worker_prompt_layer_registration_errors() -> tuple[str, ...]:
    from .bootstrap import WORKER_PROMPT_LAYER_PRODUCER_BINDINGS

    bindings = worker_prompt_layer_producer_bindings()
    actual_refs = {
        _worker_prompt_producer_ref(producer)
        for producer in worker_prompt_layer_actual_producers()
    }
    bound_refs = set(bindings)
    registered_refs = set(WORKER_PROMPT_LAYER_PRODUCER_BINDINGS)
    errors: list[str] = []
    mismatched_refs = {
        producer_ref
        for producer_ref in actual_refs & bound_refs & registered_refs
        if tuple(WORKER_PROMPT_LAYER_PRODUCER_BINDINGS[producer_ref])
        != bindings[producer_ref]
    }
    if actual_refs - bound_refs or actual_refs - registered_refs or mismatched_refs:
        errors.append("unregistered_prompt_producer")
    if registered_refs - actual_refs:
        errors.extend(
            name
            for producer_ref in sorted(registered_refs - actual_refs)
            for name in WORKER_PROMPT_LAYER_PRODUCER_BINDINGS[producer_ref]
        )
    return tuple(sorted(set(errors)))


def worker_prompt_layer_producer_names() -> tuple[str, ...]:
    bindings = worker_prompt_layer_producer_bindings()
    return tuple(
        sorted(
            {
                str(layer_name).strip()
                for layer_names in bindings.values()
                for layer_name in layer_names
            }
        )
    )


class _WorkerPromptLayerProducerNames:
    """Read-only compatibility view backed by actual producer registrations."""

    def __iter__(self):
        return iter(worker_prompt_layer_producer_names())


WORKER_PROMPT_LAYER_PRODUCERS = _WorkerPromptLayerProducerNames()


def worker_prompt_layer_integrity_snapshot(
    *, include_producer_scope: bool = False
) -> dict[str, object]:
    produced = set(worker_prompt_layer_producer_names())
    unknown = sorted(
        {
            *(name for name in produced if name not in WORKER_PROMPT_LAYER_REGISTRY),
            *worker_prompt_layer_registration_errors(),
        }
    )[:128]
    snapshot: dict[str, object] = {
        "contractVersion": WORKER_PROMPT_LAYER_CONTRACT_VERSION,
        "unknownLayerNames": unknown,
    }
    if include_producer_scope:
        snapshot["producerScope"] = WORKER_PROMPT_LAYER_PRODUCER_SCOPE
        return {
            "contractVersion": snapshot["contractVersion"],
            "producerScope": snapshot["producerScope"],
            "unknownLayerNames": snapshot["unknownLayerNames"],
        }
    return snapshot


def valid_worker_prompt_layer_capability(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    unknown = value.get("unknownLayerNames")
    return bool(
        set(value)
        == {"contractVersion", "producerScope", "unknownLayerNames"}
        and value.get("contractVersion") == WORKER_PROMPT_LAYER_CONTRACT_VERSION
        and value.get("producerScope") == WORKER_PROMPT_LAYER_PRODUCER_SCOPE
        and isinstance(unknown, list)
        and not unknown
    )


class _WorkerLifecycleGuard:
    """One idempotently releasable cross-process worker lifecycle flock."""

    def __init__(self, handle) -> None:
        self._handle = handle
        self._released = False
        self._release_lock = Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()


class ParallelExecutionIsolationError(RuntimeError):
    """Automatic conversation-orchestrated missions may not enter the host lane."""

    def __init__(self, message: str, *, reason_code: str = "") -> None:
        super().__init__(message)
        self.reason_code = str(reason_code or "").strip()


PARALLEL_CLEAN_ROOM_EXECUTION_POLICY = "parallel-clean-room-v1"
PARALLEL_CLEAN_ROOM_BOOTSTRAP_PROFILE = "clean-room"
PROMPT_WORKBENCH_SCHEDULED_BOOTSTRAP_PROFILE = "prompt-workbench-scheduled-v1"
PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_KIND = "prompt_workbench_scheduled"
PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_REQUEST = (
    "viventium_execution_authority_request"
)
PROMPT_WORKBENCH_SCHEDULED_ALIAS_NAMESPACE = "prompt-workbench-scheduled"
PARALLEL_CLEAN_ROOM_BROKER_NAME = "glasshive-user-capabilities"
PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
PARALLEL_CLEAN_ROOM_EFFORT_ENV_VALUES = {
    "WPR_CODEX_CLI_REASONING_EFFORT": {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    },
    "WPR_CLAUDE_CODE_EFFORT": {"default", "max"},
}
PARALLEL_CLEAN_ROOM_FORBIDDEN_BUNDLE_KEYS = {
    "anthropicapikey",
    "apikey",
    "authtoken",
    "bearertoken",
    "claudecodeoauthtoken",
    "claudesettingslocal",
    "credential",
    "credentials",
    "credentialspath",
    "openaiapikey",
    "providerapikey",
    "providerauth",
    "providercredentials",
    "providerenv",
    "providerheaders",
    "providertoken",
    "providertokens",
}

PARALLEL_CLEAN_ROOM_REJECTION_CODES = {
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


def _parallel_clean_room_rejected(reason: str) -> ParallelExecutionIsolationError:
    return ParallelExecutionIsolationError(
        f"Automatic Parallel work rejected unsafe bootstrap authority: {reason}.",
        reason_code=PARALLEL_CLEAN_ROOM_REJECTION_CODES.get(reason, "bundle_invalid"),
    )


def _canonical_authority_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _validate_parallel_clean_room_files(bundle: dict) -> None:
    raw_files = bundle.get("files")
    if raw_files is None:
        return
    if not isinstance(raw_files, list):
        raise _parallel_clean_room_rejected("files must be a workspace-scoped list")
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise _parallel_clean_room_rejected("every file must be workspace-scoped")
        scope = str(entry.get("scope") or "workspace").strip().lower()
        if scope != "workspace":
            raise _parallel_clean_room_rejected("home-scoped files are not allowed")
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            filename = str(entry.get("filename") or entry.get("file_id") or "").strip()
            raw_path = f"uploads/{filename}" if filename else ""
        relative = Path(raw_path.lstrip("/"))
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise _parallel_clean_room_rejected("workspace file path is invalid")
        normalized_path = relative.as_posix().lower()
        first_part = relative.parts[0].lower() if relative.parts else ""
        if (
            first_part in {".claude", ".codex", ".glasshive", ".git", ".ssh"}
            or normalized_path
            in {
                ".mcp.json",
                ".netrc",
                ".npmrc",
                ".pypirc",
                ".gitconfig",
                ".git-credentials",
                ".config/gh/hosts.yml",
                ".config/glab-cli/config.yml",
            }
            or normalized_path == ".env"
            or normalized_path.startswith(".env.")
        ):
            raise _parallel_clean_room_rejected(
                "workspace provider or credential config files are not allowed"
            )


def _contains_parallel_forbidden_authority_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _canonical_authority_key(key) in PARALLEL_CLEAN_ROOM_FORBIDDEN_BUNDLE_KEYS:
                return True
            if _contains_parallel_forbidden_authority_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_parallel_forbidden_authority_key(item) for item in value)
    return False


def _parallel_clean_room_broker_url(bundle: dict) -> str:
    broker = bundle.get("glasshive_capability_broker")
    if broker is None:
        return ""
    if not isinstance(broker, dict):
        raise _parallel_clean_room_rejected("capability broker metadata is invalid")
    broker_url = str(broker.get("url") or "").strip()
    parsed = urlparse(broker_url)
    if (
        str(broker.get("name") or "").strip() != PARALLEL_CLEAN_ROOM_BROKER_NAME
        or broker.get("version") != 1
        or isinstance(broker.get("version"), bool)
        or str(broker.get("status") or "").strip() != "pending_admission"
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _parallel_clean_room_rejected("capability broker metadata is invalid")
    forbidden_broker_keys = {
        "authorization",
        "bearer",
        "bearertoken",
        "grant",
        "granttoken",
        "password",
        "secret",
        "token",
    }
    if any(
        _canonical_authority_key(key) in forbidden_broker_keys
        for key in broker
    ):
        raise _parallel_clean_room_rejected("caller broker credentials are not allowed")
    return broker_url


def _validate_parallel_clean_room_mcp(bundle: dict) -> None:
    project_mcp = bundle.get("claude_project_mcp")
    codex_config = bundle.get("codex_config_append")
    if project_mcp is None and codex_config is None:
        return
    broker_url = _parallel_clean_room_broker_url(bundle)
    if not broker_url:
        raise _parallel_clean_room_rejected("caller MCP config is not allowed")
    expected_server = {
        "type": "http",
        "transport": "http",
        "url": broker_url,
        "headers": {
            "Authorization": f"Bearer ${{{PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV}}}"
        },
    }
    if project_mcp is not None and project_mcp != {
        PARALLEL_CLEAN_ROOM_BROKER_NAME: expected_server
    }:
        raise _parallel_clean_room_rejected("caller Claude MCP config is not allowed")
    expected_codex_config = "\n".join(
        (
            f"[mcp_servers.{PARALLEL_CLEAN_ROOM_BROKER_NAME}]",
            f"url = {json.dumps(broker_url, ensure_ascii=False)}",
            f"bearer_token_env_var = {json.dumps(PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV)}",
        )
    )
    if codex_config is not None and (
        not isinstance(codex_config, str)
        or codex_config.strip() != expected_codex_config
    ):
        raise _parallel_clean_room_rejected("caller Codex MCP config is not allowed")


def _validate_parallel_clean_room_environment(bundle: dict) -> None:
    """Allow only bounded, nonsecret worker quality preferences from trusted Core."""

    env = bundle.get("env")
    if env is None:
        return
    if not isinstance(env, dict):
        raise _parallel_clean_room_rejected("caller bootstrap environment is not allowed")
    for raw_key, raw_value in env.items():
        key = str(raw_key or "")
        value = str(raw_value or "")
        allowed_values = PARALLEL_CLEAN_ROOM_EFFORT_ENV_VALUES.get(key)
        if allowed_values is None or value not in allowed_values:
            raise _parallel_clean_room_rejected("caller bootstrap environment is not allowed")


def derive_parallel_clean_room_bootstrap(
    bootstrap_profile: str | None,
    bootstrap_bundle: dict | None,
) -> tuple[str, dict]:
    """Validate Core's automatic launch envelope and add immutable host policy."""

    requested_profile = str(bootstrap_profile or "").strip()
    if requested_profile and requested_profile != PARALLEL_CLEAN_ROOM_BOOTSTRAP_PROFILE:
        raise _parallel_clean_room_rejected("host bootstrap profiles are not allowed")
    if not isinstance(bootstrap_bundle, dict):
        raise _parallel_clean_room_rejected("bootstrap bundle is invalid")
    if "execution_policy" in bootstrap_bundle:
        raise _parallel_clean_room_rejected("execution policy is server-owned")
    if _contains_parallel_forbidden_authority_key(bootstrap_bundle):
        raise _parallel_clean_room_rejected("caller provider credentials are not allowed")
    _validate_parallel_clean_room_environment(bootstrap_bundle)
    _validate_parallel_clean_room_files(bootstrap_bundle)
    if bootstrap_bundle.get("glasshive_capability_broker") is not None:
        _parallel_clean_room_broker_url(bootstrap_bundle)
    _validate_parallel_clean_room_mcp(bootstrap_bundle)
    canonical_bundle = canonical_parallel_clean_room_bootstrap(bootstrap_bundle)
    return (
        PARALLEL_CLEAN_ROOM_BOOTSTRAP_PROFILE,
        {
            **canonical_bundle,
            "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        },
    )


@dataclass(frozen=True)
class HostResourceUsage:
    child_processes: int
    threads: int
    available_memory_bytes: int
    available_disk_bytes: int = 2**63 - 1
    process_probe_ok: bool = True
    memory_probe_ok: bool = True
    disk_probe_ok: bool = True


class _DurablePreflightProbeLease:
    """Own and abort the exact subprocess protected by a preflight lease."""

    def __init__(self) -> None:
        self._lost = Event()
        self._process_lock = Lock()
        self._process: subprocess.Popen | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @staticmethod
    def _kill_process_group(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            try:
                process.kill()
            except OSError:
                return

    def mark_lost(self) -> None:
        self._lost.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            self._kill_process_group(process)

    def run_subprocess(self, command, **kwargs) -> subprocess.CompletedProcess:
        """Run one process group that cannot outlive reservation ownership."""

        if self.lost:
            raise HostCapacityError(
                "CLI preflight reservation ownership was lost before process start.",
                capacity_class="preflight_reservation",
            )
        options = dict(kwargs)
        check = bool(options.pop("check", False))
        timeout = options.pop("timeout", None)
        input_value = options.pop("input", None)
        capture_output = bool(options.pop("capture_output", False))
        if capture_output:
            if options.get("stdout") is not None or options.get("stderr") is not None:
                raise ValueError("stdout and stderr may not be used with capture_output")
            options["stdout"] = subprocess.PIPE
            options["stderr"] = subprocess.PIPE
        options["start_new_session"] = True
        process = subprocess.Popen(command, **options)
        with self._process_lock:
            self._process = process
            lost_before_registration = self.lost
        if lost_before_registration:
            self._kill_process_group(process)
        try:
            try:
                stdout, stderr = process.communicate(input=input_value, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self._kill_process_group(process)
                stdout, stderr = process.communicate()
                exc.output = stdout
                exc.stderr = stderr
                raise
            if self.lost:
                raise HostCapacityError(
                    "CLI preflight reservation ownership was lost during the external probe.",
                    capacity_class="preflight_reservation",
                )
            completed = subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout,
                stderr,
            )
            if check:
                completed.check_returncode()
            return completed
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None


def host_resource_usage(active_leases: list[dict]) -> HostResourceUsage:
    """Measure only leased Viventium process trees plus global memory headroom."""

    pids = {
        int(lease.get("pid") or 0)
        for lease in active_leases
        if int(lease.get("pid") or 0) > 0
    }
    descendants: set[int] = set()
    threads = 0
    process_probe_ok = True
    if pids:
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,ppid="],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        rows: list[tuple[int, int]] = []
        if completed and completed.returncode == 0:
            for line in completed.stdout.splitlines():
                parts = line.split()
                if len(parts) != 2:
                    continue
                try:
                    rows.append(tuple(int(part) for part in parts))
                except ValueError:
                    continue
            descendants = set(pids)
            changed = True
            while changed:
                changed = False
                for pid, parent_pid in rows:
                    if parent_pid in descendants and pid not in descendants:
                        descendants.add(pid)
                        changed = True
            process_ids = ",".join(str(pid) for pid in sorted(descendants))
            try:
                if sys.platform == "darwin":
                    thread_probe = subprocess.run(
                        ["ps", "-M", "-p", process_ids],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=2,
                    )
                    thread_rows = [
                        line
                        for line in thread_probe.stdout.splitlines()[1:]
                        if line.strip()
                    ]
                    threads = len(thread_rows)
                else:
                    thread_probe = subprocess.run(
                        ["ps", "-o", "nlwp=", "-p", process_ids],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=2,
                    )
                    threads = sum(
                        max(0, int(line.strip()))
                        for line in thread_probe.stdout.splitlines()
                        if line.strip()
                    )
                if thread_probe.returncode != 0 or threads < len(descendants):
                    process_probe_ok = False
            except (OSError, subprocess.TimeoutExpired, ValueError):
                process_probe_ok = False
        else:
            process_probe_ok = False
    available_memory = 0
    memory_probe_ok = True
    try:
        if sys.platform.startswith("linux"):
            from .linux_memory import available_memory_bytes
            available_memory = available_memory_bytes()
        else:
            completed = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            if completed.returncode != 0:
                raise ValueError("memory size probe failed")
            total_memory = int(completed.stdout.strip())
            vm_stat = subprocess.run(
                ["vm_stat"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            if vm_stat.returncode != 0:
                raise ValueError("memory headroom probe failed")
            page_match = re.search(r"page size of (\d+) bytes", vm_stat.stdout)
            page_size = int(page_match.group(1)) if page_match else 4096
            free_pages = 0
            for name in ("Pages free", "Pages inactive", "Pages speculative"):
                match = re.search(rf"^{re.escape(name)}:\s+([0-9.]+)\.", vm_stat.stdout, re.MULTILINE)
                if match:
                    free_pages += int(match.group(1))
            available_memory = free_pages * page_size
            if not available_memory:
                available_memory = total_memory
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        memory_probe_ok = False
        available_memory = 0
    available_disk = 0
    disk_probe_ok = True
    try:
        disk_root = Path(
            os.environ.get("WPR_HOST_RUNTIME_DIR", "").strip()
            or os.environ.get("WPR_HOST_WORKSPACE_ROOT", "").strip()
            or os.getcwd()
        ).expanduser()
        while not disk_root.exists() and disk_root != disk_root.parent:
            disk_root = disk_root.parent
        available_disk = int(shutil.disk_usage(disk_root).free)
    except (OSError, ValueError):
        disk_probe_ok = False
        available_disk = 0
    return HostResourceUsage(
        child_processes=len(descendants),
        threads=threads,
        available_memory_bytes=available_memory,
        available_disk_bytes=available_disk,
        process_probe_ok=process_probe_ok,
        memory_probe_ok=memory_probe_ok,
        disk_probe_ok=disk_probe_ok,
    )


class SchedulePrincipalAuthorityError(ValueError):
    """A current principal no longer authorizes unattended schedule execution."""

    failure_class = "principal_disabled"


class ScheduleActionRequiredError(ValueError):
    """A user-owned account or capability must be repaired before scheduled work can run."""

    def __init__(self, failure_class: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.user_message = message
        self.recovery = recovery


_DUPLICATE_EXCLUDED_PATH_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".cache",
        ".claude",
        ".claude.json",
        ".codex",
        ".config",
        ".git",
        ".glasshive",
        ".glasshive-runs",
        ".hg",
        ".local",
        ".mcp.json",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        ".svn",
        ".venv",
        "auth.json",
        "cookies",
        "cookies.sqlite",
        "credentials",
        "credentials.json",
        "local state",
        "login data",
        "mcp.json",
        "node_modules",
        "session",
        "session.json",
        "sessions",
        "token.json",
        "web data",
    }
)


def _encode_workspace_cursor(worker: dict) -> str:
    payload = {
        "v": 1,
        "favorite": 1 if bool(worker.get("favorite")) else 0,
        "activity": str(worker.get("last_activity_at") or worker.get("updated_at") or ""),
        "worker_id": str(worker.get("worker_id") or ""),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_workspace_cursor(cursor: str | None) -> tuple[int | None, str, str]:
    clean_cursor = str(cursor or "").strip()
    if not clean_cursor:
        return None, "", ""
    try:
        padded = clean_cursor + ("=" * (-len(clean_cursor) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        favorite = int(payload["favorite"])
        activity = str(payload["activity"])
        worker_id = str(payload["worker_id"])
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("workspace catalog cursor is invalid") from exc
    if payload.get("v") != 1 or favorite not in {0, 1} or not activity or not worker_id:
        raise ValueError("workspace catalog cursor is invalid")
    return favorite, activity, worker_id


def _workspace_duplicate_path_is_excluded(relative_path: Path) -> bool:
    for part in relative_path.parts:
        normalized = part.casefold()
        if normalized in _DUPLICATE_EXCLUDED_PATH_NAMES:
            return True
        if normalized == ".env" or normalized.startswith(".env."):
            return True
    return False


def _require_path_within_root(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(label) from exc
    return resolved


def _optional_duplicate_limit(name: str) -> int | None:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if limit < 1:
        raise ValueError(f"{name} must be a positive integer")
    return limit


def _workspace_copy_plan(source_root: Path) -> tuple[list[tuple[Path, Path]], int, str]:
    if source_root.is_symlink():
        raise ValueError("unsafe workspace symlink: source root")
    if not source_root.exists():
        return [], 0, "missing"
    if not source_root.is_dir():
        raise ValueError("workspace duplicate source must be a directory")
    files: list[tuple[Path, Path]] = []
    max_files = _optional_duplicate_limit("GLASSHIVE_DUPLICATE_MAX_FILES")
    max_bytes = _optional_duplicate_limit("GLASSHIVE_DUPLICATE_MAX_BYTES")
    max_depth = _bounded_int_env(
        "GLASSHIVE_DUPLICATE_MAX_DEPTH",
        64,
        min_value=1,
        max_value=1_024,
    )
    timeout_seconds = _bounded_float_env(
        "GLASSHIVE_DUPLICATE_TIMEOUT_SECONDS",
        30.0,
        min_value=1.0,
        max_value=300.0,
    )
    deadline = time.monotonic() + timeout_seconds
    total_bytes = 0
    skipped_items = 0
    found_items = False
    pending = [source_root]
    while pending:
        if time.monotonic() > deadline:
            raise ValueError("workspace duplicate preflight exceeded its time limit")
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda value: value.name.casefold())
        except OSError as exc:
            relative_directory = directory.relative_to(source_root)
            raise ValueError(
                f"workspace directory could not be inspected: {relative_directory or Path('.')}"
            ) from exc
        for item in children:
            found_items = True
            relative = item.relative_to(source_root)
            if _workspace_duplicate_path_is_excluded(relative):
                skipped_items += 1
                continue
            if len(relative.parts) > max_depth:
                raise ValueError("workspace duplicate exceeds the configured depth limit")
            try:
                item_stat = item.lstat()
            except OSError as exc:
                raise ValueError(f"workspace item could not be inspected: {relative}") from exc
            if stat.S_ISLNK(item_stat.st_mode):
                try:
                    link_target = Path(os.readlink(item))
                    # Path.resolve(strict=False) stopped raising for symlink loops in
                    # Python 3.13. Follow the link once with stat so dangling and
                    # looping links fail closed on every supported interpreter.
                    item.stat()
                    resolved_target = item.resolve(strict=False)
                    resolved_root = source_root.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ValueError(f"unsafe workspace symlink: {relative}") from exc
                if link_target.is_absolute():
                    raise ValueError(f"unsafe workspace symlink: {relative}")
                try:
                    resolved_target.relative_to(resolved_root)
                except ValueError as exc:
                    raise ValueError(f"unsafe workspace symlink: {relative}") from exc
                skipped_items += 1
                continue
            mode = item_stat.st_mode
            if stat.S_ISDIR(mode):
                pending.append(item)
            elif stat.S_ISREG(mode):
                if item_stat.st_nlink > 1:
                    raise ValueError(f"workspace item is not an independent regular file: {relative}")
                files.append((item, relative))
                if max_files is not None and len(files) > max_files:
                    raise ValueError("workspace duplicate exceeds the configured file limit")
                total_bytes += int(item_stat.st_size)
                if max_bytes is not None and total_bytes > max_bytes:
                    raise ValueError("workspace duplicate exceeds the configured byte limit")
            else:
                raise ValueError(f"workspace item is not a regular file or directory: {relative}")
    if files:
        source_state = "copied"
    elif found_items:
        source_state = "filtered"
    else:
        source_state = "empty"
    return files, skipped_items, source_state


def _copy_regular_workspace_file(
    source: Path,
    target: Path,
    source_root: Path,
    *,
    max_bytes: int | None,
    deadline: float,
    target_root: Path | None = None,
) -> int:
    root = source_root.resolve(strict=True)
    if source.is_symlink():
        raise ValueError(f"unsafe workspace file changed during duplicate: {source.relative_to(source_root)}")
    resolved_source = _require_path_within_root(
        source,
        root,
        label=f"unsafe workspace file changed during duplicate: {source.relative_to(source_root)}",
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(resolved_source, flags)
    temporary_target = target.with_name(f".{target.name}.glasshive-copy-{uuid.uuid4().hex}")
    published = False
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink > 1:
            raise ValueError(f"workspace item is not a regular file: {source.relative_to(source_root)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        copied_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=False) as source_handle, temporary_target.open("xb") as target_handle:
            while True:
                if time.monotonic() > deadline:
                    raise ValueError("workspace duplicate copy exceeded its time limit")
                remaining = None if max_bytes is None else max_bytes - copied_bytes
                if remaining is not None and remaining < 0:
                    raise ValueError("workspace duplicate exceeds the configured byte limit")
                chunk = source_handle.read(
                    1024 * 1024 if remaining is None else min(1024 * 1024, remaining + 1)
                )
                if not chunk:
                    break
                copied_bytes += len(chunk)
                if max_bytes is not None and copied_bytes > max_bytes:
                    raise ValueError("workspace duplicate exceeds the configured byte limit")
                target_handle.write(chunk)
        os.replace(temporary_target, target)
        published = True
        return copied_bytes
    finally:
        os.close(descriptor)
        try:
            temporary_target.unlink()
        except FileNotFoundError:
            pass
        if not published and target_root is not None:
            parent = target.parent
            while parent != target_root and parent.is_relative_to(target_root):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent


def _duplicate_bootstrap_bundle(bundle: dict | None) -> dict | None:
    if not isinstance(bundle, dict):
        return None
    project_definition = bundle.get("project_definition")
    if not isinstance(project_definition, str) or not project_definition.strip():
        return None
    return {"project_definition": project_definition}


WORK_TRACE_SCHEMA_DIGEST = (
    "sha256:ba9b15e022a451c62be0c0f30a02d6615bea83e868b2ffdd349beff75002e790"
)

WORK_TRACE_PRODUCER_SOURCE_IDENTITY = "workers_projects_runtime.api:get_active_work"

WORK_TRACE_EMITTED_KEY_SET_DIGEST = (
    "sha256:3a109b0f41a08755252a050e444dd6780e7bf95aec194ad95628e4e7a5c3a253"
)


WORKER_PROMPT_LAYER_CONTRACT_VERSION = 1

WORKER_PROMPT_LAYER_PRODUCER_SCOPE = "glasshive.worker_prompt_registry"

WORKER_PROMPT_LAYER_REGISTRY = frozenset(
    {
        "agents_md",
        "claude_md",
        "codex_md",
        "developer_instructions",
        "glasshive_worker_project_contract",
        "harness_prompt",
        "mcp_server_instructions",
        "project_definition",
        "run_instruction",
        "system_instructions",
        "tool_schemas",
        "viventium_feeling_state",
    }
)







WORKER_PROMPT_LAYER_PRODUCERS = _WorkerPromptLayerProducerNames()




class BackgroundWorkerConfigurationError(ValueError):
    """A trusted background preference is unsupported by its exact model."""



PARALLEL_CLEAN_ROOM_EXECUTION_POLICY = "parallel-clean-room-v1"

PARALLEL_CLEAN_ROOM_BOOTSTRAP_PROFILE = "clean-room"

PROMPT_WORKBENCH_SCHEDULED_BOOTSTRAP_PROFILE = "prompt-workbench-scheduled-v1"

PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_KIND = "prompt_workbench_scheduled"

PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_REQUEST = (
    "viventium_execution_authority_request"
)

PROMPT_WORKBENCH_SCHEDULED_ALIAS_NAMESPACE = "prompt-workbench-scheduled"

PARALLEL_CLEAN_ROOM_BROKER_NAME = "glasshive-user-capabilities"

PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"

PARALLEL_CLEAN_ROOM_EFFORT_ENV_VALUES = {
    "WPR_CODEX_CLI_REASONING_EFFORT": {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    },
    "WPR_CLAUDE_CODE_EFFORT": frozenset(CLAUDE_CODE_EFFORT_LEVELS),
}

PARALLEL_CLEAN_ROOM_FORBIDDEN_BUNDLE_KEYS = {
    "anthropicapikey",
    "apikey",
    "authtoken",
    "bearertoken",
    "claudecodeoauthtoken",
    "claudesettingslocal",
    "credential",
    "credentials",
    "credentialspath",
    "openaiapikey",
    "providerapikey",
    "providerauth",
    "providercredentials",
    "providerenv",
    "providerheaders",
    "providertoken",
    "providertokens",
}

PARALLEL_CLEAN_ROOM_REJECTION_CODES = {
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












def _bounded_int_env(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return max(min_value, min(value, max_value))


def _configured_worker_resource_memory_bytes(resource_class: object) -> int:
    return worker_resource_memory_bytes(
        normalize_worker_resource_class(resource_class),
        standard_memory_mib=_bounded_int_env(
            "WPR_DOCKER_MEMORY_RESERVATION_MB",
            3072,
            min_value=1,
            max_value=1048576,
        ),
    )

def _worker_resource_memory_reservation(worker: dict | None) -> int:
    resource_class = normalize_worker_resource_class(
        (worker or {}).get("resource_class") or "standard"
    )
    raw_memory = (worker or {}).get("resource_memory_bytes")
    if raw_memory in {None, ""}:
        return _configured_worker_resource_memory_bytes(resource_class)
    if isinstance(raw_memory, bool) or int(raw_memory) <= 0:
        raise ValueError("Worker resource memory must be a positive byte count")
    memory_bytes = int(raw_memory)
    if (
        resource_class == "light"
        and memory_bytes != worker_resource_memory_bytes("light")
    ):
        raise ValueError("Light worker memory must use the trusted server value")
    return memory_bytes

def _bounded_float_env(name: str, default: float, *, min_value: float, max_value: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return max(min_value, min(value, max_value))


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _is_local_scheduling_cortex_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.path == SCHEDULING_CORTEX_CALLBACK_PATH
        and parsed.scheme in {"http", "https"}
        and host in {"localhost", "127.0.0.1", "::1"}
    )


def _is_callback_status_retryable(status_code: int, url: str) -> bool:
    if status_code in CALLBACK_RETRYABLE_STATUS_CODES:
        return True
    return status_code == 404 and _is_local_scheduling_cortex_callback_url(url)


def _is_callback_status_immediate_dead_letter(status_code: int, url: str) -> bool:
    if _is_callback_status_retryable(status_code, url):
        return False
    return status_code in CALLBACK_DEAD_LETTER_IMMEDIATE_STATUS_CODES


def _enterprise_mode_enabled() -> bool:
    return multi_user_security_enabled()


def _reconcile_on_startup_enabled() -> bool:
    configured = str(os.environ.get("GLASSHIVE_RECONCILE_ON_STARTUP") or "").strip().lower()
    if not configured:
        return not _enterprise_mode_enabled()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    raise ValueError("GLASSHIVE_RECONCILE_ON_STARTUP must be true or false")


def _background_consumers_enabled() -> bool:
    configured = str(os.environ.get("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED") or "true").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    raise ValueError("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED must be true or false")


def _recurring_schedule_owner() -> str:
    load_viventium_runtime_env()
    configured = str(os.environ.get("GLASSHIVE_RECURRING_SCHEDULE_OWNER") or "").strip().lower()
    viventium_deployment = bool(
        str(os.environ.get("VIVENTIUM_ENV_FILE") or "").strip()
        or str(os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL") or "").strip()
    )
    if configured and configured not in RECURRENCE_OWNERS:
        raise ValueError(
            "GLASSHIVE_RECURRING_SCHEDULE_OWNER must be glasshive_native or viventium_cortex"
        )
    configured_owner = canonical_recurrence_owner(configured) if configured else ""
    if viventium_deployment:
        if configured_owner == NATIVE_RECURRENCE_OWNER:
            raise ValueError("This deployment delegates recurrence to its configured host scheduler")
        return DELEGATED_RECURRENCE_OWNER
    return configured_owner or NATIVE_RECURRENCE_OWNER


def isolated_parallel_policy_enabled() -> bool:
    """Whether same-UID host missions are excluded while Parallel Main is available."""

    return _env_truthy("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY")


def native_parallel_policy_enabled() -> bool:
    return (
        os.environ.get("VIVENTIUM_PARALLEL_WORK_EXECUTION_MODE") == "host"
        and not isolated_parallel_policy_enabled()
        and host_workers_enabled()
        and _env_truthy("GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS")
    )

class HostWorkersDisabledError(RuntimeError):
    pass


class GlassHiveQuotaExceededError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        env_name: str = "",
        label: str = "",
        limit: int = 0,
        current_count: int = 0,
        available_workspace_options: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.env_name = env_name
        self.label = label
        self.limit = limit
        self.current_count = current_count
        self.available_workspace_options = available_workspace_options or []


class GlassHiveProfileNotAllowedError(RuntimeError):
    pass


def host_workers_enabled() -> bool:
    value = os.environ.get("GLASSHIVE_HOST_WORKERS_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def allowed_worker_profiles() -> set[str]:
    raw = (
        os.environ.get("GLASSHIVE_ALLOWED_WORKER_PROFILES", "").strip()
        or os.environ.get("WPR_ALLOWED_WORKER_PROFILES", "").strip()
    )
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def terminal_callback_full_message(output_text: str, *, fallback: str = "Run completed") -> str:
    text = str(output_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return fallback

    marker_matches = list(FINAL_REPORT_PATTERN.finditer(text))
    if marker_matches:
        text = text[marker_matches[-1].end() :].strip()
    return text or fallback


def terminal_callback_message(output_text: str, *, fallback: str = "Run completed") -> str:
    text = str(output_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return fallback

    marker_matches = list(FINAL_REPORT_PATTERN.finditer(text))
    has_final_report = bool(marker_matches)
    if marker_matches:
        text = text[marker_matches[-1].end() :].strip()

    if len(text) <= TERMINAL_CALLBACK_MESSAGE_LIMIT:
        return text or fallback

    if has_final_report:
        return f"{text[: TERMINAL_CALLBACK_MESSAGE_LIMIT - 3].rstrip()}..."

    prefix = "...\n\n"
    paragraph_budget = TERMINAL_CALLBACK_MESSAGE_LIMIT - len(prefix)
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    selected: list[str] = []
    current_len = 0
    for paragraph in reversed(paragraphs):
        next_len = current_len + len(paragraph) + (2 if selected else 0)
        if selected and next_len > paragraph_budget:
            break
        selected.insert(0, paragraph)
        current_len = next_len

    if selected:
        message = "\n\n".join(selected).strip()
        if len(message) <= paragraph_budget:
            return f"{prefix}{message}" if len(message) < len(text) else message

    tail_prefix = "..."
    tail = text[-(TERMINAL_CALLBACK_MESSAGE_LIMIT - len(tail_prefix)) :].lstrip()
    if " " in tail[:120]:
        tail = tail[tail.find(" ") + 1 :].lstrip()
    return f"{tail_prefix}{tail}" if tail else fallback


def public_callback_message_text(message: str) -> str:
    text = str(message or "")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-[REDACTED]", text)
    text = re.sub(r"\b(?:wrk|run|prj)_[A-Za-z0-9_-]{6,}\b", "[glasshive-id]", text)
    text = re.sub(
        r"(?:~\/|\/Users\/|\/home\/|\/private\/var\/|\/var\/folders\/|[A-Za-z]:\\Users\\)[^\s`'\"<>]+",
        "[local path]",
        text,
    )
    return text.strip()


def runtime_failure_callback_message(failure_fields: dict[str, object], fallback: str) -> str:
    message = str(failure_fields.get("failure_user_message") or "").strip() or str(fallback or "Run failed")
    diagnostic = str(failure_fields.get("failure_diagnostic_summary") or "").strip()
    if diagnostic and diagnostic not in message:
        return f"{message}\n\nDetails: {diagnostic}"
    return message


def _attach_failure_guidance(payload: dict, run: dict | None) -> None:
    """Copy the run's user-facing failure guidance strings onto a payload."""

    for key in ("failure_user_message", "failure_recommended_recovery"):
        value = str((run or {}).get(key) or "").strip()
        if value:
            payload[key] = value


def _is_viventium_callback_url(url: str) -> bool:
    return VIVENTIUM_CALLBACK_PATH in str(url or "")


def _missing_parent_callback_fields(callbacks: dict[str, object]) -> list[str]:
    return [key for key in PARENT_VISIBLE_CALLBACK_FIELDS if not str(callbacks.get(key) or "").strip()]


def callback_run_state(event_type: str, run: dict | None) -> object:
    # The public mapping remains backward compatible, while a terminal
    # interruption is delivered with the same cancelled state used by the
    # authoritative terminal-result wire contract.
    if (
        str(event_type or "") == "run.interrupted"
        and str((run or {}).get("state") or "") == "interrupted"
    ):
        return "cancelled"
    return RUN_STATE_BY_EVENT.get(str(event_type or ""), (run or {}).get("state"))


def _merge_file_entries(existing: object, incoming: object) -> object:
    if not isinstance(existing, list) and not isinstance(incoming, list):
        return incoming
    existing_entries = existing if isinstance(existing, list) else []
    incoming_entries = incoming if isinstance(incoming, list) else []
    merged: list[object] = []
    indexes_by_identity: dict[tuple[str, str], int] = {}
    used_paths: set[str] = set()

    def identity(item: dict[str, object]) -> tuple[str, str] | None:
        file_id = str(item.get("file_id") or item.get("id") or "").strip()
        if file_id:
            return ("file_id", file_id)
        source_path = str(item.get("source_path") or "").strip()
        if source_path:
            return ("source_path", source_path)
        path = str(item.get("path") or "").strip()
        return ("workspace_path", path) if path else None

    def collision_safe_path(path: str) -> str:
        if not path or path not in used_paths:
            return path
        parent, filename = os.path.split(path)
        stem, extension = os.path.splitext(filename)
        suffix = 2
        candidate = os.path.join(parent, f"{stem}-{suffix}{extension}")
        while candidate in used_paths:
            suffix += 1
            candidate = os.path.join(parent, f"{stem}-{suffix}{extension}")
        return candidate

    def merge_item(item: object) -> None:
        if not isinstance(item, dict):
            merged.append(item)
            return
        entry = dict(item)
        item_identity = identity(entry)
        existing_index = (
            indexes_by_identity.get(item_identity) if item_identity else None
        )
        if existing_index is not None:
            prior = merged[existing_index]
            prior_path = (
                str(prior.get("path") or "").strip()
                if isinstance(prior, dict)
                else ""
            )
            if prior_path:
                used_paths.discard(prior_path)
            path = collision_safe_path(str(entry.get("path") or "").strip())
            if path:
                entry["path"] = path
                used_paths.add(path)
            merged[existing_index] = entry
            return
        path = collision_safe_path(str(entry.get("path") or "").strip())
        if path:
            entry["path"] = path
            used_paths.add(path)
        if item_identity:
            indexes_by_identity[item_identity] = len(merged)
        merged.append(entry)

    for item in existing_entries:
        merge_item(item)
    for item in incoming_entries:
        merge_item(item)
    return merged


def merge_bootstrap_bundle(existing: dict | None, incoming: dict | None) -> dict | None:
    if incoming is None:
        return existing
    existing_bundle = existing or {}
    merged = dict(existing_bundle)
    if (
        "execution_policy" in existing_bundle
        and "execution_policy" in incoming
        and incoming["execution_policy"] != existing_bundle["execution_policy"]
    ):
        raise ParallelExecutionIsolationError(
            "The server-owned worker execution policy is immutable."
        )
    for key, value in incoming.items():
        current = merged.get(key)
        if key == "files":
            merged[key] = _merge_file_entries(current, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_bootstrap_bundle(current, value) or {}
        else:
            merged[key] = value
    if is_parallel_clean_room_bootstrap(
        None, existing_bundle
    ) or is_parallel_clean_room_bootstrap(None, merged):
        return canonical_parallel_clean_room_bootstrap(merged)
    return merged


def _required_capability_servers(bundle: dict | None) -> list[str]:
    if not isinstance(bundle, dict):
        return []
    broker = bundle.get("glasshive_capability_broker")
    values = broker.get("allowed_servers") if isinstance(broker, dict) else None
    if not isinstance(values, list):
        return []
    return sorted(
        {
            normalized
            for value in values
            if (normalized := str(value or "").strip())
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", normalized)
        }
    )[:32]


class WorkersProjectsService:
    def __init__(
        self,
        store: Store,
        runtime: WorkerRuntime,
        max_workers: int = 8,
        reconcile_on_startup: bool | None = None,
        control_plane_store: ControlPlaneStore | None = None,
        scheduling_owner_client: ViventiumSchedulingOwnerClient | None = None,
        local_qa_control_plane: LocalQAControlPlane | None = None,
        start_background_consumers: bool = True,
    ) -> None:
        self.store = store
        self.peers = PeerCollaboration(store, self)
        self.runtime = runtime
        configure_workspaces = getattr(runtime, "configure_execution_workspaces", None)
        from .storage_runtime import OwnerStorageRuntime
        self.storage_runtime = OwnerStorageRuntime.from_environment(store)
        from .workspace_files import WorkspaceFiles
        storage = self.storage_runtime
        self.files = WorkspaceFiles(store, root_resolver=self.file_workspace_root,
            scope_resolver=self.file_workspace_scope if callable(configure_workspaces) else None,
            scope_access=self.file_scope_access if callable(configure_workspaces) else None,
            quota_backend=storage,
            managed_root_resolver=storage.managed_root if storage else None,
            native_coverage=storage.native_coverage if storage else None,
            policy_updater=storage.update_policy if storage else None,
            control_root=storage.control_root / "files" if storage else None)
        if storage is not None:
            storage.files = self.files
            storage.runtime = runtime
            configure_storage = getattr(runtime, "configure_owner_storage", None)
            if not callable(configure_storage):
                raise RuntimeErrorBase("This runtime cannot enforce owner storage")
            configure_storage(storage)
        if callable(configure_workspaces):
            configure_workspaces(store)
        self.control_plane_store = control_plane_store
        # Bound by create_app after both Store and the owner control plane are
        # ready.  Keeping this optional preserves direct service callers and
        # old deployments that have no Allowed AI rows yet.
        self.allowed_ai_policy = None
        self.scheduling_owner_client = scheduling_owner_client or ViventiumSchedulingOwnerClient()
        self._local_qa_control_plane = local_qa_control_plane
        if self._local_qa_control_plane is None:
            qa_keys = (
                *LOCAL_QA_AUTHORITY_KEYS,
                LOCAL_QA_CANDIDATE_DIGEST_ENV,
                LOCAL_QA_COMPONENT_DIGEST_ENV,
            )
            qa_values = [str(os.environ.get(key, "") or "").strip() for key in qa_keys]
            if all(qa_values):
                try:
                    self._local_qa_control_plane = LocalQAControlPlane(store.db_path)
                except Exception:
                    logger.warning(
                        "GlassHive local-QA authority was rejected; fault controls remain inactive"
                    )
        self._configured_host_capacity = {
            "conversation_limit": _bounded_int_env(
                "WPR_HOST_CONVERSATION_SLOTS_PER_CLI", 2, min_value=1, max_value=64
            ),
            "mission_limit": _bounded_int_env(
                "WPR_HOST_MISSION_SLOTS_PER_CLI", 3, min_value=1, max_value=64
            ),
            "account_mission_limit": _bounded_int_env(
                "WPR_HOST_ACCOUNT_ACTIVE_LIMIT", 4, min_value=1, max_value=256
            ),
            "tenant_mission_limit": _bounded_int_env(
                "WPR_HOST_TENANT_ACTIVE_LIMIT", 12, min_value=1, max_value=1024
            ),
        }
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wpr-runner")
        # Interactive provider turns have a separate dispatch lane so autonomous mission workers
        # cannot occupy every service thread before a conversation reaches the host CLI's own
        # profile-isolated capacity lane.
        conversation_workers = max(
            1,
            min(
                max_workers,
                self._configured_host_capacity["account_mission_limit"],
            ),
        )
        self.conversation_executor = ThreadPoolExecutor(
            max_workers=conversation_workers,
            thread_name_prefix="wpr-conversation",
        )
        self._shutdown_event = Event()
        self._scheduler_wake_event = Event()
        self._processors_lock = Lock()
        self._active_processors: set[str] = set()
        self._retained_restart_run_ids: set[str] = set()
        self._managed_shutdown_lease_ids: set[str] = set()
        self._processor_generations: dict[str, int] = {}
        self._runtime_start_locks: dict[str, Lock] = {}
        self._worker_create_lock = Lock()
        self._deliverable_promotions_lock = Lock()
        self._deliverable_promotions: set[str] = set()
        self._pending_run_starts_lock = Lock()
        self._pending_run_starts: dict[str, dict[str, object]] = {}
        self._run_local_bundles_lock = Condition()
        self._run_local_bundles: dict[str, dict] = {}
        self._run_local_grant_waiters: set[str] = set()
        self._provider_request_reconciler: Callable[[str], int] | None = None
        self._provider_run_start_fence: Callable[[str], bool] | None = None
        self._executor_id = f"executor-{os.getpid()}-{uuid.uuid4().hex}"
        observer_setter = getattr(self.runtime, "set_host_process_observer", None)
        if callable(observer_setter):
            observer_setter(self._observe_host_process)
        start_observer_setter = getattr(self.runtime, "set_run_start_observer", None)
        self._run_start_observer_supported = callable(start_observer_setter)
        if callable(start_observer_setter):
            start_observer_setter(self._observe_run_start)
        native_observer_setter = getattr(self.runtime, "set_native_event_observer", None)
        if callable(native_observer_setter):
            native_observer_setter(self._observe_native_event)
        liveness_observer_setter = getattr(
            self.runtime, "set_provider_liveness_observer", None
        )
        if callable(liveness_observer_setter):
            liveness_observer_setter(self._observe_provider_liveness)
        self._startup_recovery_cutoff = utc_now()
        self._startup_recovery_thread: Thread | None = None
        self._callback_retry_thread: Thread | None = None
        self._idle_reaper_thread: Thread | None = None
        self._scheduler_thread: Thread | None = None
        self._host_lease_heartbeat_thread: Thread | None = None
        self._isolated_readiness_thread: Thread | None = None
        self._background_consumer_lifecycle_lock = Lock()
        self._background_consumers_started = False
        self._background_consumers_enabled = _background_consumers_enabled()
        if reconcile_on_startup is None:
            reconcile_on_startup = _reconcile_on_startup_enabled()
        self._reconcile_on_startup = bool(reconcile_on_startup)
        if start_background_consumers:
            self.start_background_consumers()

    def start_background_consumers(self) -> None:
        """Start service-owned loops only after their enclosing lifecycle is active."""

        with self._background_consumer_lifecycle_lock:
            if self._background_consumers_started or not self._background_consumers_enabled:
                return
            if self._shutdown_event.is_set():
                raise RuntimeError("GlassHive service cannot restart after shutdown")
            recover_interactive = getattr(
                self.runtime,
                "recover_interactive_provider_sessions",
                None,
            )
            if callable(recover_interactive):
                recover_interactive()
            if self._reconcile_on_startup:
                mission_network_repair = getattr(
                    self.runtime, "repair_parallel_clean_room_mission_networks", None
                )
                if callable(mission_network_repair):
                    try:
                        mission_network_repair()
                    except Exception:
                        logger.warning(
                            "Failed to repair Parallel clean-room mission networks",
                            exc_info=True,
                        )
                self.reconcile_host_run_leases()
            refresh_isolated_resources = getattr(
                self.runtime, "refresh_isolated_resource_usage", None
            )
            if callable(refresh_isolated_resources):
                try:
                    # Warm the exact snapshot consumed by first admission while
                    # the controlled service startup boundary is still active.
                    refresh_isolated_resources()
                except Exception:
                    logger.warning(
                        "Initial isolated runtime resource probe failed",
                        exc_info=True,
                    )
            self._startup_recovery_thread = Thread(
                target=self._replay_startup_recovery,
                name="wpr-startup-recovery",
                daemon=True,
            )
            self._startup_recovery_thread.start()
            self._callback_retry_thread = Thread(
                target=self._callback_retry_loop,
                name="wpr-callback-retry",
                daemon=True,
            )
            self._callback_retry_thread.start()
            if self._lifecycle_reaper_enabled():
                self._idle_reaper_thread = Thread(
                    target=self._idle_reaper_loop,
                    name="wpr-idle-reaper",
                    daemon=True,
                )
                self._idle_reaper_thread.start()
            self._scheduler_thread = Thread(
                target=self._scheduler_loop,
                name="wpr-scheduler",
                daemon=True,
            )
            self._scheduler_thread.start()
            self._host_lease_heartbeat_thread = Thread(
                target=self._host_lease_heartbeat_loop,
                name="wpr-host-lease-heartbeat",
                daemon=True,
            )
            self._host_lease_heartbeat_thread.start()
            if callable(
                getattr(self.runtime, "refresh_isolated_parallel_readiness", None)
            ) or callable(refresh_isolated_resources):
                self._isolated_readiness_thread = Thread(
                    target=self._isolated_readiness_loop,
                    name="wpr-isolated-readiness",
                    daemon=True,
                )
                self._isolated_readiness_thread.start()
            if self.store.has_compute_release_claims():
                self.executor.submit(self.recover_expired_compute_release_claims_once)
            self._background_consumers_started = True

    def release_owned_host_run_leases(
        self, *, reason: str = MANAGED_SHUTDOWN_LEASE_RELEASE_REASON
    ) -> int:
        """Stop owned running generations and release only leases proven safe to retry.

        A managed stop ends this executor's heartbeat on purpose. Releasing the
        leases of its running runs with a typed reason keeps the
        provider-liveness monitor from reading the silent heartbeat as a
        stalled provider, and lets startup reconciliation re-queue the same run
        with managed-restart wording. Pre-accept reservations and not-yet-running
        generations keep their existing restart contract (transfer or expiry),
        and a lease fenced by an unfinished control stays with that control.
        Idempotent and never raises: shutdown must continue regardless.
        """

        released = 0
        try:
            leases = self.store.list_active_host_run_leases()
        except Exception:
            logger.exception("Managed shutdown could not list host run leases")
            return released
        for lease in leases:
            if str(lease.get("executor_id") or "") != self._executor_id:
                continue
            lease_id = str(lease.get("lease_id") or "")
            try:
                run = self.store.get_run(str(lease.get("run_id") or ""))
                if run is None or str(run.get("state") or "") != "running":
                    continue
                worker = self.store.get_worker(str(lease.get("worker_id") or ""))
                if self._lease_is_fenced_by_lifecycle_claim(worker, lease):
                    # An unfinished control still owns this exact lease; its
                    # claim recovery, not shutdown, decides when it ends.
                    continue
                if self._retain_live_generation_for_restart(worker, run, lease):
                    continue
                # Claim only this dispatch generation before stopping it. Its processor may
                # unwind during cleanup; it must not terminalize or release an unproved stop.
                with self._processors_lock:
                    self._managed_shutdown_lease_ids.add(lease_id)
                if not self._stop_managed_shutdown_generation(worker, run, lease):
                    logger.warning(
                        "Managed shutdown retained lease %s: generation absence is unproved",
                        lease_id,
                    )
                    continue
                record = self.store.release_host_run_lease(
                    lease_id,
                    executor_id=self._executor_id,
                    reason=reason,
                )
            except Exception:
                logger.exception(
                    "Managed shutdown could not release host run lease %s", lease_id
                )
                continue
            if (
                record
                and str(record.get("status") or "") == "released"
                and str(record.get("release_reason") or "") == str(reason)
            ):
                released += 1
        return released

    def _managed_restart_lease_grace_s(self) -> float:
        """How long a retained generation's lease stays exact while the service restarts."""
        return _bounded_float_env(
            "WPR_MANAGED_RESTART_LEASE_GRACE_S",
            180.0,
            min_value=30.0,
            max_value=3600.0,
        )

    @staticmethod
    def _restart_survivor_adoption_enabled() -> bool:
        """Restart survivor adoption is opt-in; the bounded retry/resume path owns recovery.

        Adoption keeps a live docker generation across a managed restart and collects its
        result in the restarted service. It is disabled by default because its collection
        path still has evidence-parity gaps; the default recovery shape is the proven one:
        the same durable run is requeued after restart and its retry resumes the same
        provider session, completing exactly once.
        """
        return str(os.environ.get("WPR_RESTART_SURVIVOR_ADOPTION") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _retain_live_generation_for_restart(
        self, worker: dict | None, run: dict | None, lease: dict | None
    ) -> bool:
        """Keep a verified live docker mission generation running across a managed restart.

        The durable worker process is the container's screen session, not this executor.
        When the runtime can verify that exact generation, this shutdown only detaches its
        local wait, extends the same lease for the restart window, and leaves the run
        running: the restarted service adopts the survivor through its existing startup
        reconciliation and collects one terminal result. Nothing is released or requeued
        until that generation's exit is confirmed. Anything unverifiable keeps the
        stop-and-release contract.
        """
        if not self._restart_survivor_adoption_enabled():
            return False
        if not worker or not run or not lease:
            return False
        if str(worker.get("execution_mode") or "docker") != "docker":
            return False
        if self._trusted_run_lane(worker) != "mission":
            return False
        if (
            str(lease.get("startup_state") or "") != "confirmed"
            or str(lease.get("startup_identity_kind") or "") != "docker_session"
        ):
            return False
        identity_reader = getattr(self.runtime, "host_process_identity", None)
        detach = getattr(self.runtime, "detach_worker", None)
        if not callable(identity_reader) or not callable(detach):
            return False
        run_id = str(run.get("run_id") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        with self._processors_lock:
            if run_id in self._retained_restart_run_ids:
                # The lifespan and shutdown() both release owned leases; the
                # generation was already retained by the first pass.
                return True
            if worker_id not in self._active_processors:
                return False
        runtime_worker = {
            **worker,
            "_active_run_id": run_id,
            "_run_attempt_id": str(run.get("active_attempt_id") or ""),
        }
        try:
            identity = identity_reader(runtime_worker, run_id)
        except Exception:
            logger.warning(
                "Managed shutdown could not verify the live generation of run %s", run_id
            )
            return False
        if (
            not isinstance(identity, dict)
            or identity.get("verified") is not True
            or str(identity.get("identity_kind") or "") != "docker_session"
            or str(identity.get("container_id") or "")
            != str(lease.get("startup_container_id") or "")
        ):
            return False
        renewed = self.store.heartbeat_host_run_lease(
            str(lease["lease_id"]),
            executor_id=self._executor_id,
            lease_ttl_s=self._managed_restart_lease_grace_s(),
        )
        if not renewed:
            return False
        try:
            detached = bool(detach(runtime_worker, run_id=run_id))
        except Exception:
            logger.exception(
                "Managed shutdown could not detach from the live generation of run %s", run_id
            )
            detached = False
        if not detached:
            return False
        with self._processors_lock:
            self._retained_restart_run_ids.add(run_id)
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            run_id,
            "run.generation_retained",
            "Managed restart left the live worker generation running for adoption",
            payload={
                "containerId": str(identity.get("container_id") or ""),
                "sessionId": str(identity.get("session_id") or ""),
            },
        )
        logger.info(
            "Managed shutdown retained the live generation of run %s for restart adoption",
            run_id,
        )
        return True

    def _released_for_managed_shutdown(self, run: dict, terminal_generation: dict | None) -> bool:
        """True when managed shutdown owns this exact generation's restart handoff.

        The in-memory lease claim covers the interval while native cleanup is still running;
        afterward the typed durable release keeps the same handoff visible to late exits.
        """
        with self._processors_lock:
            if str(run.get("run_id") or "") in self._retained_restart_run_ids:
                return True
        lease = None
        lease_id = str((terminal_generation or {}).get("expected_lease_id") or "").strip()
        with self._processors_lock:
            if lease_id in self._managed_shutdown_lease_ids:
                return True
        if lease_id:
            lease = self.store.get_host_run_lease(lease_id)
        if lease is None:
            latest = getattr(self.store, "latest_host_run_lease_for_run", None)
            if callable(latest):
                lease = latest(str(run.get("run_id") or ""))
        return (
            bool(lease)
            and str(lease.get("status") or "") == "released"
            and str(lease.get("release_reason") or "") == MANAGED_SHUTDOWN_LEASE_RELEASE_REASON
        )

    def _stop_managed_shutdown_generation(
        self, worker: dict | None, run: dict, lease: dict
    ) -> bool:
        """Keep the exact lease until its recorded generation is stopped and proven absent."""
        absence_reader = getattr(self.runtime, "host_process_absence", None)
        if self._reserved_start_identity(lease):
            # Reuse the restart owner's identity-bound cleanup. A mutable worker pid or
            # a successful interrupt alone cannot prove this lease's generation absent.
            return self._stale_lease_generation_proven_absent(
                worker, run, lease, absence_reader=absence_reader
            )
        if not worker:
            return False
        run_id = str(run.get("run_id") or "")
        interrupt = getattr(self.runtime, "interrupt_worker", None)
        if str(run.get("runtime_invoked_at") or "") and callable(interrupt):
            try:
                # Legacy adapters without an exact run target cannot safely stop a generation.
                interrupt(worker, run_id=run_id)
            except Exception:
                logger.exception(
                    "Managed shutdown could not stop run %s; retaining its lease", run_id
                )
                return False
        return self._stale_lease_generation_proven_absent(
            worker, run, lease, absence_reader=absence_reader
        )

    def shutdown(self, *, timeout_seconds: float = 10.0) -> None:
        with self._processors_lock:
            self._shutdown_event.set()
        # Stop owned running generations before releasing their dispatch fences. Unknown
        # termination keeps the exact lease for startup reconciliation to prove safe later.
        self.release_owned_host_run_leases()
        with self._run_local_bundles_lock:
            self._run_local_bundles.clear()
            self._run_local_grant_waiters.clear()
            self._run_local_bundles_lock.notify_all()
        self.store.release_active_work_action_leases(self._executor_id)
        self._scheduler_wake_event.set()
        background_threads = tuple(
            thread
            for thread in (
                self._startup_recovery_thread,
                self._callback_retry_thread,
                self._idle_reaper_thread,
                self._scheduler_thread,
                self._host_lease_heartbeat_thread,
                self._isolated_readiness_thread,
            )
            if thread is not None
        )
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        for thread in background_threads:
            if thread is current_thread():
                continue
            if thread and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        active_background_threads = [
            thread.name for thread in background_threads if thread.is_alive()
        ]
        if active_background_threads:
            raise RuntimeError(
                "GlassHive background loops did not stop: "
                + ", ".join(active_background_threads)
            )
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.conversation_executor.shutdown(wait=True, cancel_futures=False)

    def set_provider_request_reconciler(
        self,
        reconciler: Callable[[str], int],
    ) -> None:
        """Register the provider-owned run-to-request projection recovery pass."""

        if not callable(reconciler):
            raise TypeError("Provider request reconciler must be callable")
        self._provider_request_reconciler = reconciler

    def set_provider_run_start_fence(self, fence: Callable[[str], bool]) -> None:
        """Register the provider's exact-run deadline check at native launch."""

        if not callable(fence):
            raise TypeError("Provider run start fence must be callable")
        self._provider_run_start_fence = fence

    def _callback_config_for(self, worker: dict) -> dict:
        bundle = self._bootstrap_bundle_for(worker) or {}
        callbacks = bundle.get("callbacks")
        if not isinstance(callbacks, dict) or not callbacks:
            return {}
        resolved = dict(callbacks)
        authority = bundle.get("viventium_launch_authority")
        if (
            isinstance(authority, dict)
            and authority.get("version") == 1
            and authority.get("kind")
            == PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_KIND
            and authority.get("execution_mode") == "docker"
        ):
            origin_ref = str(resolved.get("origin_ref") or "").strip()
            if origin_ref:
                resolved.update(
                    {
                        "user_id": str(worker.get("owner_id") or "").strip(),
                        "message_id": origin_ref,
                        "scheduled_prompt_run_id": origin_ref,
                        "surface": "workbench",
                    }
                )
        load_viventium_runtime_env()
        callback_url = (
            os.environ.get("GLASSHIVE_EVENTS_WEBHOOK_URL", "").strip()
            or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "").strip()
        )
        callback_secret = (
            os.environ.get("GLASSHIVE_EVENTS_HMAC_SECRET", "").strip()
            or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "").strip()
        )
        recovered: list[str] = []
        if callback_url and not (resolved.get("events_webhook_url") or resolved.get("url")):
            resolved["events_webhook_url"] = callback_url
            recovered.append("endpoint")
        if callback_secret and not (resolved.get("hmac_secret") or resolved.get("secret")):
            resolved["hmac_secret"] = callback_secret
            recovered.append("secret")
        if recovered:
            logger.warning(
                "Recovered GlassHive callback %s from canonical runtime env for worker %s; "
                "check MCP/bootstrap request-context propagation.",
                ", ".join(recovered),
                worker.get("worker_id"),
            )
        return resolved

    def scheduling_cortex_callback_config(self, occurrence_id: str) -> dict:
        occurrence_id = str(occurrence_id or "").strip()
        if not occurrence_id:
            return {}
        load_viventium_runtime_env()
        owner_url = str(os.environ.get("GLASSHIVE_SCHEDULING_OWNER_URL") or "").strip()
        secret = str(os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET") or "").strip()
        try:
            parsed = urlparse(owner_url)
        except ValueError:
            return {}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not secret:
            return {}
        return {
            "events_webhook_url": f"{parsed.scheme}://{parsed.netloc}{SCHEDULING_CORTEX_CALLBACK_PATH}",
            "hmac_secret": secret,
            "message_id": occurrence_id,
            "callback_kind": "scheduling_cortex",
        }

    def _scheduling_cortex_callback_config_for_run(self, run: dict | None) -> dict:
        run_id = str((run or {}).get("run_id") or "").strip()
        if not run_id:
            return {}
        return self.scheduling_cortex_callback_config(
            self.store.scheduling_cortex_occurrence_for_run(run_id)
        )

    def _callback_config_for_event(self, worker: dict, run: dict | None) -> dict:
        run_id = str((run or {}).get("run_id") or "").strip()
        if run_id and self.store.scheduling_cortex_occurrence_for_run(run_id):
            return self._scheduling_cortex_callback_config_for_run(run)
        return self._callback_config_for(worker)

    def _derive_callback_secret(self, secret: str, worker_id: str, run_id: str | None) -> bytes:
        binding = f"{worker_id}:{run_id or ''}".encode("utf-8")
        return hmac.new(secret.encode("utf-8"), binding, hashlib.sha256).hexdigest().encode("utf-8")

    def _encode_callback_payload(self, payload: dict) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _callback_headers(self, callbacks: dict, payload: dict, encoded: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        result_revision = payload.get("result_revision")
        if (
            isinstance(result_revision, int)
            and not isinstance(result_revision, bool)
            and result_revision > 0
        ):
            headers["X-GlassHive-Callback-Id"] = str(
                payload.get("callback_id") or ""
            )
            headers["X-GlassHive-Result-Revision"] = str(result_revision)
            headers["X-GlassHive-Result-Digest"] = str(
                payload.get("result_digest") or ""
            )
        secret = str(callbacks.get("hmac_secret") or callbacks.get("secret") or "")
        if secret:
            derived_secret = self._derive_callback_secret(
                secret,
                str(payload.get("worker_id") or ""),
                str(payload.get("run_id") or "") or None,
            )
            headers["X-GlassHive-Signature"] = "sha256=" + hmac.new(derived_secret, encoded, hashlib.sha256).hexdigest()
        return headers

    def _callback_action_capabilities(
        self,
        worker: dict,
        payload: dict,
        callbacks: dict,
    ) -> list[dict[str, object]]:
        event_type = str(payload.get("event") or "")
        run_id = str(payload.get("run_id") or "")
        if event_type not in {"run.started", "run.failed"} or not run_id:
            return []
        run = self.store.get_run(run_id)
        if not run:
            return []
        action = ""
        if event_type == "run.started" and str(run.get("state") or "") == "running":
            action = "cancel"
        elif (
            event_type == "run.failed"
            and str(run.get("state") or "") == "failed"
            and bool(run.get("failure_retryable"))
        ):
            action = "retry"
        if not action:
            return []
        secret = str(callbacks.get("hmac_secret") or callbacks.get("secret") or "")
        if not secret:
            return []
        try:
            return [mint_run_action_capability(secret, worker=worker, run=run, action=action)]
        except RunActionError:
            logger.warning(
                "GlassHive action capability was not minted",
                extra={
                    "worker_id": str(worker.get("worker_id") or ""),
                    "run_id": run_id,
                    "event_type": event_type,
                },
            )
            return []

    def _callback_max_total_attempts(self) -> int:
        return _bounded_int_env("GLASSHIVE_CALLBACK_MAX_TOTAL_ATTEMPTS", 25, min_value=1, max_value=1000)

    def _dead_letter_callback(
        self,
        worker: dict,
        record: dict,
        *,
        callback_id: str,
        attempts: int,
        payload_json: str,
        reason: str,
    ) -> None:
        updated = self.store.mark_callback_dead_lettered(
            callback_id,
            lease_token=str(record.get("delivery_lease_token") or ""),
            delivery_generation=int(record.get("delivery_generation") or 0),
            attempts=attempts,
            payload_json=payload_json,
            last_error=reason,
        )
        if updated is None:
            return
        try:
            self.store.add_event(
                str(record.get("project_id") or worker.get("project_id") or ""),
                str(record.get("worker_id") or worker.get("worker_id") or ""),
                record.get("run_id"),
                "callback.dead_lettered",
                f"{record.get('event_type')}: {reason}",
            )
        except Exception:
            pass

    def _finish_failed_callback_delivery(
        self,
        worker: dict,
        record: dict,
        *,
        callback_id: str,
        attempts: int,
        payload_json: str,
        reason: str,
    ) -> None:
        stored_attempts = int(record.get("attempts") or 0)
        total_attempts = stored_attempts + attempts
        max_total_attempts = self._callback_max_total_attempts()
        if total_attempts >= max_total_attempts:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=callback_id,
                attempts=attempts,
                payload_json=payload_json,
                reason=f"callback retry budget exhausted after {total_attempts} attempts: {reason}",
            )
            return
        updated = self.store.mark_callback_pending(
            callback_id,
            lease_token=str(record.get("delivery_lease_token") or ""),
            delivery_generation=int(record.get("delivery_generation") or 0),
            attempts=attempts,
            payload_json=payload_json,
            last_error=reason,
        )
        if updated is None:
            return
        try:
            self.store.add_event(
                str(record.get("project_id") or worker.get("project_id") or ""),
                str(record.get("worker_id") or worker.get("worker_id") or ""),
                record.get("run_id"),
                "callback.failed",
                f"{record.get('event_type')}: {reason}",
            )
        except Exception:
            pass

    def _deliver_callback_record_parallel(self, worker: dict, record: dict, callbacks: dict) -> None:
        callback_id = str(record.get("callback_id") or "")
        if callback_id:
            claimed = self.store.claim_pending_callback(callback_id)
            if not claimed:
                return
            record = claimed
        stored_attempts = int(record.get("attempts") or 0)
        max_total_attempts = self._callback_max_total_attempts()
        if stored_attempts >= max_total_attempts:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=callback_id,
                attempts=0,
                payload_json=str(record.get("payload_json") or "{}"),
                reason=f"callback retry budget exhausted before delivery after {stored_attempts} attempts",
            )
            return
        self._deliver_claimed_callback_record(worker, record, callbacks)

    def recover_terminal_callback(self, **identity: Any) -> bool:
        record = self.store.claim_terminal_callback_recovery(**identity)
        if record is None:
            return False
        worker = self.store.get_worker(str(record["worker_id"]))
        run = self.store.get_run(str(record["run_id"]))
        callbacks = self._callback_config_for_event(worker, run)
        self.executor.submit(
            self._deliver_claimed_callback_record, worker, record, callbacks,
            delivery_attempts=1,
        )
        return True

    def _deliver_claimed_callback_record(
        self, worker: dict, record: dict, callbacks: dict, *, delivery_attempts: int | None = None
    ) -> None:
        callback_id = str(record.get("callback_id") or "")
        url = str(callbacks.get("events_webhook_url") or callbacks.get("url") or record.get("url") or "").strip()
        if not url:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=callback_id,
                attempts=1,
                payload_json=str(record.get("payload_json") or "{}"),
                reason="missing callback url",
            )
            return
        try:
            payload = json.loads(str(record.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=str(record.get("callback_id") or ""),
                attempts=1,
                payload_json=str(record.get("payload_json") or "{}"),
                reason="invalid callback payload json",
            )
            return
        if not isinstance(payload, dict):
            payload = {}
        payload_callback_id = str(payload.get("callback_id") or "").strip()
        payload_attempt_number = payload.get("attempt_number")
        payload_result_revision = payload.get("result_revision")
        payload_callback_ts = payload.get("callback_ts")
        stored_attempt_number = int(record.get("attempt_number") or 0)
        stored_result_revision = int(record.get("result_revision") or 0)
        exact_attempt_identity = (
            isinstance(payload_attempt_number, int)
            and not isinstance(payload_attempt_number, bool)
            and payload_attempt_number > 0
            and payload_attempt_number == stored_attempt_number
            if stored_attempt_number > 0
            else payload_attempt_number is None
        )
        exact_result_revision = (
            isinstance(payload_result_revision, int)
            and not isinstance(payload_result_revision, bool)
            and payload_result_revision == stored_result_revision
            if stored_result_revision > 0
            else payload_result_revision in (None, 0)
            and not isinstance(payload_result_revision, bool)
        )
        exact_payload_identity = bool(
            callback_id
            and payload_callback_id == callback_id
            and exact_attempt_identity
            and exact_result_revision
            and isinstance(payload_callback_ts, (int, float))
            and not isinstance(payload_callback_ts, bool)
            and math.isfinite(float(payload_callback_ts))
            and float(payload_callback_ts) > 0
            and str(payload.get("event") or "")
            == str(record.get("event_type") or "")
            and str(payload.get("worker_id") or "")
            == str(record.get("worker_id") or "")
            and str(payload.get("run_id") or "")
            == str(record.get("run_id") or "")
            and str(payload.get("result_digest") or "")
            == str(record.get("result_digest") or "")
        )
        if not exact_payload_identity:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=callback_id,
                attempts=0,
                payload_json=str(record.get("payload_json") or "{}"),
                reason="callback payload identity is incomplete or mismatched",
            )
            return
        stored_payload = dict(payload)
        action_capabilities = self._callback_action_capabilities(worker, payload, callbacks)
        if action_capabilities:
            payload["actionCapabilities"] = action_capabilities

        retry_attempts = delivery_attempts if delivery_attempts is not None else _bounded_int_env(
            "GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", 3, min_value=1, max_value=25
        )
        retry_base_delay_s = _bounded_float_env(
            "GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S",
            0.5,
            min_value=0.0,
            max_value=60.0,
        )
        last_exc: Exception | None = None
        attempts = 0
        stored_payload_json = json.dumps(stored_payload, ensure_ascii=False)
        for attempt in range(retry_attempts):
            callback_run = self.store.get_run(str(record.get("run_id") or ""))
            qa_expired_lease = self._consume_local_qa(
                "expired_sender_lease_race", worker, callback_run
            )
            if qa_expired_lease is not None:
                expired = self.store.expire_callback_delivery_lease_for_local_qa(
                    callback_id,
                    lease_token=str(record.get("delivery_lease_token") or ""),
                    delivery_generation=int(
                        record.get("delivery_generation") or 0
                    ),
                )
                reclaimed = self.store.reclaim_stale_delivering_callbacks(
                    stale_before=utc_now(), limit=1
                )
                newer = self.store.claim_pending_callback(callback_id)
                self._record_local_qa_effect(
                    qa_expired_lease,
                    "newer_delivery_lease_won"
                    if expired and reclaimed == 1 and newer is not None
                    else "sender_lease_race_not_applicable",
                )
                if not expired or reclaimed != 1 or newer is None:
                    return
                record = newer
            if not self.store.callback_delivery_is_current(
                callback_id,
                lease_token=str(record.get("delivery_lease_token") or ""),
                delivery_generation=int(record.get("delivery_generation") or 0),
            ):
                return
            attempts += 1
            delivery_payload = {**payload, "callback_ts": int(time.time())}
            encoded = self._encode_callback_payload(delivery_payload)
            headers = self._callback_headers(callbacks, delivery_payload, encoded)
            qa_transport = self._consume_local_qa(
                "callback_transport_interruption", worker, callback_run
            )
            try:
                if qa_transport is not None:
                    self._record_local_qa_effect(
                        qa_transport, "transport_interrupted_before_http"
                    )
                    raise httpx.TransportError(
                        "Synthetic callback transport interruption"
                    )
                response = httpx.post(url, content=encoded, headers=headers, timeout=5.0)
                receiver_decision, receiver_revision = (
                    self._terminal_callback_response_decision(
                        response, delivery_payload
                    )
                )
                qa_duplicate = None
                if receiver_decision == "accepted":
                    qa_duplicate = self._consume_local_qa(
                        "duplicate_callback_replay", worker, callback_run
                    )
                if qa_duplicate is not None:
                    replay = httpx.post(
                        url, content=encoded, headers=headers, timeout=5.0
                    )
                    replay_decision, replay_revision = (
                        self._terminal_callback_response_decision(
                            replay, delivery_payload
                        )
                    )
                    if replay_decision not in {"accepted", "superseded"}:
                        replay.raise_for_status()
                        raise RuntimeError(
                            "duplicate callback replay did not return a durable receipt"
                        )
                    self._record_local_qa_effect(
                        qa_duplicate,
                        "duplicate_receiver_replay_suppressed"
                        if replay_decision == "accepted"
                        else "duplicate_receiver_newer_terminal_won",
                    )
                    receiver_revision = max(
                        receiver_revision, replay_revision
                    )
                if receiver_decision == "superseded":
                    self.store.mark_callback_receiver_superseded(
                        payload["callback_id"],
                        lease_token=str(
                            record.get("delivery_lease_token") or ""
                        ),
                        delivery_generation=int(
                            record.get("delivery_generation") or 0
                        ),
                        attempts=attempts,
                        current_result_revision=receiver_revision,
                    )
                    return
                if receiver_decision == "conflict":
                    self._dead_letter_callback(
                        worker,
                        record,
                        callback_id=payload["callback_id"],
                        attempts=attempts,
                        payload_json=stored_payload_json,
                        reason="receiver_result_conflict",
                    )
                    return
                if receiver_decision == "invalid":
                    response.raise_for_status()
                    raise RuntimeError(
                        "callback receiver did not return an exact result CAS receipt"
                    )
                response.raise_for_status()
                accepted = self.store.mark_callback_http_accepted(
                    payload["callback_id"],
                    lease_token=str(record.get("delivery_lease_token") or ""),
                    delivery_generation=int(record.get("delivery_generation") or 0),
                    attempts=attempts,
                    payload_json=stored_payload_json,
                )
                if accepted is None:
                    return
                if str(accepted.get("status") or "") == "superseded":
                    return
                return
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status_code = exc.response.status_code if exc.response is not None else 0
                # A bare 409 is not proof that this exact result already won.
                # Keep it pending unless the typed receipt above proved an
                # exact conflict or a newer terminal result.
                if status_code == 409:
                    break
                if _is_callback_status_immediate_dead_letter(status_code, url):
                    self._dead_letter_callback(
                        worker,
                        record,
                        callback_id=payload["callback_id"],
                        attempts=attempts,
                        payload_json=stored_payload_json,
                        reason=f"callback endpoint returned terminal HTTP {status_code}",
                    )
                    return
                if 400 <= status_code < 500 and not _is_callback_status_retryable(status_code, url):
                    break
            except Exception as exc:
                last_exc = exc
            if attempt < retry_attempts - 1 and retry_base_delay_s > 0:
                time.sleep(retry_base_delay_s * (attempt + 1))
        self._finish_failed_callback_delivery(
            worker,
            record,
            callback_id=payload["callback_id"],
            attempts=attempts,
            payload_json=stored_payload_json,
            reason=str(last_exc or "callback delivery failed"),
        )

    def _deliver_callback_record(self, worker: dict, record: dict, callbacks: dict) -> None:
        self._deliver_callback_record_parallel(worker, record, callbacks)

    def _replay_pending_callbacks(self, *, created_before: str | None = None) -> None:
        if self._shutdown_event.is_set():
            return
        stale_after_s = _bounded_int_env(
            "GLASSHIVE_CALLBACK_DELIVERING_STALE_AFTER_S",
            300,
            min_value=1,
            max_value=24 * 3600,
        )
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)).isoformat()
        try:
            self.store.reclaim_stale_delivering_callbacks(stale_before=stale_before, limit=50)
        except Exception:
            pass
        try:
            pending = self.store.list_pending_callbacks(
                limit=50,
                created_before=created_before,
            )
        except Exception:
            return
        for record in pending:
            if self._shutdown_event.is_set():
                return
            worker = self.store.get_worker(str(record.get("worker_id") or ""))
            if not worker:
                continue
            run_id = str(record.get("run_id") or "").strip()
            callbacks = self._callback_config_for_event(
                worker,
                self.store.get_run(run_id) if run_id else None,
            )
            self._deliver_callback_record(worker, record, callbacks)

    def _callback_retry_loop(self) -> None:
        interval = _bounded_int_env(
            "GLASSHIVE_CALLBACK_RETRY_INTERVAL_S",
            30,
            min_value=1,
            max_value=3600,
        )
        while not self._shutdown_event.wait(interval):
            self._callback_retry_tick()

    def _ensure_execution_allowed(
        self,
        worker_or_mode: dict | str,
        *,
        trusted_run_lane: str = "mission",
    ) -> None:
        if isinstance(worker_or_mode, dict) and str(worker_or_mode.get("state") or "") in CLOSED_WORKER_STATES:
            raise ControlPlaneConflict(
                "Workspace is closed; create a new workspace for new work"
            )
        if isinstance(worker_or_mode, dict):
            unresolved = self._unresolved_duplication_reapprovals(worker_or_mode)
            if unresolved:
                raise ControlPlaneConflict(
                    "Copied workspace needs capability review before it can run"
                )
            self._ensure_allowed_ai(worker_or_mode)
        execution_mode = (
            str(worker_or_mode.get("execution_mode") or "docker")
            if isinstance(worker_or_mode, dict)
            else str(worker_or_mode or "docker")
        )
        from .execution_profile import packaged_linux
        if packaged_linux() and execution_mode != "docker":
            raise HostWorkersDisabledError("The packaged Linux profile requires contained workers")
        lane = (
            self._trusted_run_lane(worker_or_mode)
            if isinstance(worker_or_mode, dict)
            else (
                "conversation"
                if str(trusted_run_lane or "").strip().lower() == "conversation"
                else "mission"
            )
        )
        if (
            execution_mode == "host"
            and lane == "mission"
            and isolated_parallel_policy_enabled()
        ):
            raise ParallelExecutionIsolationError(
                "Host-native mission roots are unavailable while isolated Parallel policy is enabled."
            )
        if execution_mode == "host" and not host_workers_enabled():
            raise HostWorkersDisabledError(
                "GlassHive host-native workers are disabled by Viventium config"
            )
        if (
            execution_mode == "host"
            and isinstance(worker_or_mode, dict)
            and self._is_native_conversation_delegation(worker_or_mode)
            and not _env_truthy("GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS")
        ):
            raise HostWorkersDisabledError(
                "Full host access was revoked for this native mission."
            )

    def bind_allowed_ai_policy(self, policy_service) -> None:
        """Attach the owner-scoped Allowed AI service used at admission time."""

        self.allowed_ai_policy = policy_service

    @staticmethod
    def _allowed_ai_snapshot_json(snapshot: dict | None) -> str:
        if not isinstance(snapshot, dict) or not snapshot:
            return "{}"
        try:
            encoded = json.dumps(
                snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeErrorBase("Allowed AI admission snapshot is invalid") from exc
        if len(encoded.encode("utf-8")) > 128 * 1024:
            raise RuntimeErrorBase("Allowed AI admission snapshot is too large")
        return encoded

    def _record_allowed_ai_admission(
        self, worker: dict, snapshot: dict | None
    ) -> dict | None:
        if not isinstance(snapshot, dict) or not snapshot:
            return snapshot
        # Keep the snapshot on the in-memory worker used by the exact run and
        # on the owner-scoped worker lineage.  The snapshot contains only typed
        # IDs/revisions, never provider secrets or home paths.
        worker["_allowed_ai_admission"] = dict(snapshot)
        worker["allowed_ai_admission"] = dict(snapshot)
        worker_id = str(worker.get("worker_id") or "").strip()
        if worker_id:
            encoded = self._allowed_ai_snapshot_json(snapshot)
            if str(worker.get("allowed_ai_admission_json") or "{}") != encoded:
                updated = self.store.update_worker(
                    worker_id, allowed_ai_admission_json=encoded,
                )
                if updated is None:
                    raise RuntimeErrorBase("Worker unavailable during Allowed AI admission")
                # The admission write advances the lifecycle generation. Keep
                # the worker used by an immediate exact control/start on that
                # same persisted generation instead of failing its own CAS.
                worker.update(updated)
            else:
                worker["allowed_ai_admission_json"] = encoded
        return snapshot

    def _ensure_allowed_ai(self, worker: dict) -> dict | None:
        """Re-read and enforce the current project/workspace AI ceiling."""

        policy_service = self.allowed_ai_policy
        if policy_service is None:
            return None
        try:
            return self._record_allowed_ai_admission(
                worker, policy_service.admission_snapshot(worker)
            )
        except Exception as exc:
            # Preserve typed policy denials for the API and MCP callers.  Any
            # malformed/missing policy state fails closed without exposing
            # another owner's labels or storage details.
            from .allowed_ai_policy import AllowedAiAdmissionError

            if isinstance(exc, AllowedAiAdmissionError):
                raise
            raise AllowedAiAdmissionError(
                "allowed_ai_unavailable",
                "Allowed AI settings could not be verified before this start.",
                scope_id=str(worker.get("workspace_id") or worker.get("project_id") or ""),
            ) from exc

    def _record_run_allowed_ai_admission(
        self,
        run: dict,
        snapshot: dict | None,
        *,
        worker: dict | None = None,
        origin_trace: dict | None = None,
        origin_ref: str = "",
    ) -> dict:
        if isinstance(snapshot, dict) and snapshot:
            run["allowed_ai_admission"] = dict(snapshot)
            run["_allowed_ai_admission"] = dict(snapshot)
            run_id = str(run.get("run_id") or "").strip()
            if run_id:
                self.store.update_run(
                    run_id,
                    allowed_ai_admission_json=self._allowed_ai_snapshot_json(snapshot),
                )
                current_worker = worker or self.store.get_worker(str(run.get("worker_id") or "")) or {}
                bundle = self._bootstrap_bundle_for(current_worker) if current_worker else {}
                env = bundle.get("env") if isinstance(bundle, dict) else {}
                effort = str(current_worker.get("reasoning_effort") or "").strip()
                if not effort and isinstance(env, dict):
                    for env_name in (
                        "WPR_CODEX_CLI_REASONING_EFFORT",
                        "WPR_CLAUDE_CODE_EFFORT",
                        "WPR_GROK_REASONING_EFFORT",
                        "WPR_OPENCLAW_REASONING_EFFORT",
                    ):
                        effort = str(env.get(env_name) or "").strip()
                        if effort:
                            break
                trace = origin_trace if isinstance(origin_trace, dict) else {}
                destination = {
                    "project_id": str(snapshot.get("project_id") or run.get("project_id") or ""),
                    "workspace_id": str(snapshot.get("workspace_id") or current_worker.get("workspace_id") or ""),
                    "tenant_id": str(snapshot.get("tenant_id") or run.get("tenant_id") or "local"),
                    "owner_id": str(snapshot.get("owner_id") or current_worker.get("owner_id") or ""),
                    "execution_mode": str(current_worker.get("execution_mode") or "docker"),
                }
                # Origin is explicit when a coordinator/follow-up supplies one;
                # direct worker work remains self-originating and is still bound
                # to the same owner and project scope.
                origin_scope = snapshot.get("origin_scope")
                if isinstance(origin_scope, dict) and str(origin_scope.get("project_id") or ""):
                    origin = {
                        "scope_type": "project",
                        "resolved": True,
                        "project_id": str(origin_scope.get("project_id") or ""),
                        "workspace_id": str(origin_scope.get("workspace_id") or ""),
                        "tenant_id": str(origin_scope.get("tenant_id") or destination["tenant_id"]),
                        "owner_id": str(origin_scope.get("owner_id") or destination["owner_id"]),
                        "ref": str(origin_scope.get("ref") or origin_ref or trace.get("origin_ref") or ""),
                        "source_event_id": str(origin_scope.get("source_event_id") or trace.get("source_event_id") or ""),
                        "source_revision": origin_scope.get("source_revision", trace.get("source_revision", 0)),
                        "surface": str(origin_scope.get("surface") or trace.get("surface") or "internal"),
                        "project_revision": int(snapshot.get("origin_project_revision") or 0),
                        "workspace_revision": int(snapshot.get("origin_workspace_revision") or 0),
                    }
                else:
                    origin = {
                        "scope_type": "project",
                        "resolved": (
                            not bool(origin_ref or trace.get("origin_ref"))
                            or bool(trace.get("project_id"))
                            or str(origin_ref).startswith("schedule:")
                        ),
                        "project_id": str(trace.get("project_id") or destination["project_id"]),
                        "workspace_id": str(trace.get("workspace_id") or ""),
                        "tenant_id": str(trace.get("tenant_id") or destination["tenant_id"]),
                        "owner_id": str(trace.get("owner_id") or destination["owner_id"]),
                        "ref": str(origin_ref or trace.get("origin_ref") or ""),
                        "source_event_id": str(trace.get("source_event_id") or ""),
                        "source_revision": trace.get("source_revision", 0),
                        "surface": str(trace.get("surface") or "internal"),
                        "project_revision": 0,
                        "workspace_revision": 0,
                    }
                binding = {
                    "version": 1,
                    "origin": origin,
                    "destination": destination,
                    "route": {
                        "profile": str(snapshot.get("profile") or current_worker.get("profile") or ""),
                        "model_id": str(snapshot.get("model_id") or ""),
                        "native_model": str(current_worker.get("model") or ""),
                        "effort": effort,
                        "connection_id": str(snapshot.get("connection_id") or ""),
                    },
                    "policy": {
                        "project_revision": int(snapshot.get("project_revision") or 0),
                        "workspace_revision": int(snapshot.get("workspace_revision") or 0),
                        "origin_project_revision": int(snapshot.get("origin_project_revision") or 0),
                        "origin_workspace_revision": int(snapshot.get("origin_workspace_revision") or 0),
                    },
                }
                self.store.bind_execution_binding(
                    run_id=run_id,
                    worker_id=str(run.get("worker_id") or current_worker.get("worker_id") or ""),
                    project_id=str(run.get("project_id") or destination["project_id"]),
                    tenant_id=str(run.get("tenant_id") or destination["tenant_id"]),
                    owner_id=str(current_worker.get("owner_id") or destination["owner_id"]),
                    binding=binding,
                )
                run["execution_binding"] = binding
                run["execution_binding_json"] = self._allowed_ai_snapshot_json(binding)
        return run

    def native_start_fence(self, worker: dict, *, receipt: dict) -> dict | None:
        """Claim the shared Allowed AI fence at the native binder boundary."""

        policy_service = self.allowed_ai_policy
        if policy_service is None:
            return None
        return policy_service.native_start_fence(worker, receipt=receipt)

    def _ensure_dispatch_provider_ready(self, worker: dict) -> None:
        if self._selected_account_has_interactive_lease(worker):
            raise ControlPlaneConflict(
                "Finish or close the open AI setup window before starting new work."
            )
        uses_deployment_route = self._uses_deployment_provider_route(worker)
        if not uses_deployment_route:
            return
        readiness, _status = deployment_provider_readiness(
            str(worker.get("profile") or "")
        )
        if readiness != "deployment_managed":
            raise ControlPlaneConflict(
                "Work AI is not set up for this workspace. Ask an administrator to finish "
                "provider setup or connect a personal account in Connections."
            )
    def _provider_dispatch_fence(self, worker: dict) -> tuple[str, str, bool] | None:
        if self.control_plane_store is None:
            return None
        selection = mission_provider_account_selection(worker)
        if selection is None:
            return None
        fallback_ready = bool(
            selection.policy == "personal_preferred"
            and deployment_provider_readiness(str(worker.get("profile") or ""))[0]
            == "deployment_managed"
        )
        return (
            selection.account_id,
            f"{str(worker.get('profile') or '').strip()}:mission",
            fallback_ready,
        )

    @staticmethod
    def _provider_account_busy_conflict(
        exc: ProviderAccountBusyStoreError,
    ) -> ControlPlaneConflict:
        if exc.interactive:
            return ControlPlaneConflict(
                "Finish or close the open AI setup window before starting new work."
            )
        return ControlPlaneConflict(
            "The selected personal AI account is already in use. Wait for that work to finish."
        )

    def _selected_account_has_interactive_lease(self, worker: dict) -> bool:
        bundle = self._bootstrap_bundle_for(worker) or {}
        raw = bundle.get("provider_account")
        selection = raw if isinstance(raw, dict) else {}
        account_id = str(selection.get("account_id") or "").strip()
        if not account_id or self.control_plane_store is None:
            return False
        account = self.control_plane_store.get_provider_account(
            account_id=account_id,
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if account is None:
            return False
        lease = self.control_plane_store.active_provider_account_lease(account_id)
        return bool(
            lease
            and str(lease.get("lane") or "")
            == f"{str(worker.get('profile') or '').strip()}:interactive"
        )

    def _uses_deployment_provider_route(self, worker: dict) -> bool:
        bundle = self._bootstrap_bundle_for(worker) or {}
        if str(bundle.get("run_mode") or "mission").strip().lower() == "conversation":
            return False
        raw = bundle.get("provider_account")
        selection = raw if isinstance(raw, dict) else {}
        policy = str(selection.get("policy") or "legacy").strip().lower()
        account_id = str(selection.get("account_id") or "").strip()
        if policy in {"", "legacy", "personal_optional"} and not account_id:
            return True
        if policy != "personal_preferred":
            return False
        if not account_id or self.control_plane_store is None:
            return True
        account = self.control_plane_store.get_provider_account(
            account_id=account_id,
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if account is None or str(account.get("status") or "").strip().lower() != "ready":
            return True
        lease = self.control_plane_store.active_provider_account_lease(account_id)
        if lease is None:
            return False
        worker_id = str(worker.get("worker_id") or "")
        active_run = self.store.get_active_run(worker_id) if worker_id else None
        owns_active_mission_lease = bool(
            active_run
            and str(lease.get("worker_id") or "") == worker_id
            and str(lease.get("run_id") or "") == str(active_run.get("run_id") or "")
            and str(lease.get("lane") or "")
            == f"{str(worker.get('profile') or '').strip()}:mission"
        )
        # A steer replaces the current worker's own active mission, whose binder
        # releases this exact run lease during interruption. Every other lease makes
        # personal_preferred fall back to the deployment route, so prove that route
        # before creating or interrupting any run.
        return not owns_active_mission_lease

    def _unresolved_duplication_reapprovals(self, worker: dict) -> list[dict]:
        report = worker.get("duplication_report")
        if not isinstance(report, dict):
            return []
        if str(report.get("duplication_state") or "").strip() == "pending":
            return [{"action_id": "duplication_pending", "kind": "duplication"}]
        items = [
            item
            for item in (report.get("reapproval_items") or [])
            if isinstance(item, dict)
            and str(item.get("action_id") or "").strip()
            and str(item.get("reference") or "").strip()
        ]
        if not items:
            return []
        waived = {
            str(item).strip()
            for item in (report.get("waived_reapprovals") or [])
            if str(item).strip()
        }
        if self.control_plane_store is None:
            return [item for item in items if str(item.get("action_id") or "") not in waived]
        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        worker_id = str(worker.get("worker_id") or "")
        grants = self.control_plane_store.list_workspace_grants(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
        )
        selection = mission_provider_account_selection(worker)

        def resolved(item: dict) -> bool:
            reference = str(item.get("reference") or "")
            action_id = str(item.get("action_id") or "")
            if action_id in waived:
                return True
            resolution = str(item.get("resolution") or "")
            policy = str(item.get("policy") or "")
            if resolution == "provider_selection" and policy:
                return bool(
                    selection is not None
                    and selection.policy == policy
                    and selection.account_id == reference
                )
            grant_key = {
                "library_grant": "library_id",
                "connection_grant": "connection_id",
                "provider_grant": "account_id",
            }.get(resolution)
            expected_scopes = {
                str(scope).strip()
                for scope in (item.get("scopes") or [])
                if str(scope).strip()
            }
            return bool(grant_key and any(
                str(grant.get(grant_key) or "") == reference
                and {
                    str(scope).strip()
                    for scope in (grant.get("scopes") or [])
                    if str(scope).strip()
                } == expected_scopes
                for grant in grants
            ))

        return [item for item in items if not resolved(item)]

    def _runtime_start_lock(self, worker_id: str) -> Lock:
        with self._processors_lock:
            return self._runtime_start_locks.setdefault(worker_id, Lock())

    def _coordinator_retry_dispatch_ready(self, worker: dict, run: dict) -> bool:
        """Keep a reserved child Retry queued until its exact goal owns the run."""

        worker_id = str(worker.get("worker_id") or "")
        run_id = str(run.get("run_id") or "")
        if not run_id.startswith("run_idem_"):
            return True
        with self.store._connect() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinator_actions'"
            ).fetchone():
                return True
            actions = conn.execute(
                "SELECT a.conversation_id,a.goal_id,a.idempotency_key,g.work_ref,"
                "g.run_id,g.intent_state,g.restore_hold,c.restore_hold AS conversation_hold,"
                "c.tenant_id,c.owner_id FROM coordinator_actions a "
                "JOIN coordinator_goals g ON g.conversation_id=a.conversation_id "
                "AND g.goal_id=a.goal_id "
                "JOIN coordinator_conversations c ON c.conversation_id=a.conversation_id "
                "WHERE g.worker_id=? AND json_valid(a.payload_json) "
                "AND json_extract(a.payload_json,'$.action')='retry'",
                (worker_id,),
            ).fetchall()
        for action in actions:
            effect_key = (
                f"coordinator-retry:{action['conversation_id']}:"
                f"{action['goal_id']}:{action['idempotency_key']}"
            )
            expected_id = self._idempotent_run_id(
                worker_id,
                self._active_work_effect_idempotency_key(
                    str(action["work_ref"] or ""), effect_key,
                ),
            )
            if expected_id != run_id:
                continue
            return bool(
                action["run_id"] == run_id
                and action["intent_state"] == "accepted"
                and not action["restore_hold"]
                and not action["conversation_hold"]
                and str(action["tenant_id"] or "local") == str(worker.get("tenant_id") or "local")
                and str(action["owner_id"] or "") == str(worker.get("owner_id") or "")
            )
        return True

    @contextmanager
    def _runtime_lifecycle_start_guard(self, worker_id: str):
        """Fence non-run readiness starts against permanent workspace Close."""

        with self._runtime_start_lock(worker_id):
            current = self.require_worker(worker_id)
            self._ensure_execution_allowed(current)
            storage_guard = (self.storage_runtime.owner_lock(current["tenant_id"], current["owner_id"])
                             if self.storage_runtime else nullcontext())
            with storage_guard, self.files.native_start_guard(current):
                yield

    def _ensure_worker_ready_with_lifecycle_fence(self, worker: dict) -> RuntimeInfo:
        worker_id = str(worker["worker_id"])

        def persist_runtime_info(info: RuntimeInfo) -> None:
            updated = self._apply_runtime_info(
                worker_id,
                info,
                state="starting",
                last_error="",
            )
            if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                raise ControlPlaneConflict(
                    "Workspace is closed; create a new workspace for new work"
                )

        return self.runtime.ensure_worker_ready(
            {
                **worker,
                "_runtime_start_guard": lambda: self._runtime_lifecycle_start_guard(
                    worker_id
                ),
                "_runtime_info_callback": persist_runtime_info,
            }
        )

    def _finalize_worker_ready_after_start(
        self,
        worker: dict,
        info: RuntimeInfo,
        *,
        event_type: str,
        message: str,
        context: str,
        emit_callback: bool = False,
    ) -> dict:
        worker_id = str(worker["worker_id"])
        try:
            with self._runtime_lifecycle_start_guard(worker_id):
                updated = self._apply_runtime_info(
                    worker_id,
                    info,
                    state="ready",
                    last_error="",
                    compute_released_at=None,
                )
                if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                    raise ControlPlaneConflict(
                        "Workspace is closed; create a new workspace for new work"
                    )
                self.store.add_event(
                    str(worker["project_id"]),
                    worker_id,
                    None,
                    event_type,
                    message,
                )
                if emit_callback:
                    self._emit_callback(
                        updated or worker,
                        event_type,
                        message="Worker ready",
                    )
                return updated or worker
        except ControlPlaneConflict:
            self._reject_closed_after_runtime_activity(
                worker_id,
                fallback_worker=worker,
                context=context,
            )
            raise

    @contextmanager
    def _runtime_execution_start_guard(
        self,
        worker_id: str,
        generation: int,
        run_id: str,
    ):
        """Serialize the real external start boundary with durable Close ownership."""

        with self._runtime_start_lock(worker_id):
            current = self.store.get_worker(worker_id)
            run = self.store.get_run(run_id)
            if (
                not self._processor_is_current(worker_id, generation)
                or not current
                or str(current.get("state") or "") in CLOSED_WORKER_STATES
                or not run
                or str(run.get("state") or "") != "running"
            ):
                current_state = str((current or {}).get("state") or "")
                run_state = str((run or {}).get("state") or "")
                if current and current_state in CLOSED_WORKER_STATES:
                    try:
                        self.runtime.terminate_worker(
                            {**current, "_active_run_id": run_id}
                        )
                    except Exception as cleanup_error:
                        message = public_callback_message_text(str(cleanup_error)) or "Worker close-race cleanup failed"
                        self.store.record_worker_termination_cleanup_failure(worker_id, message)
                elif current and (
                    current_state == "paused" or (run and run_state != "running")
                ):
                    try:
                        if current_state == "paused":
                            self.runtime.pause_worker(
                                {**current, "_active_run_id": run_id}
                            )
                        else:
                            try:
                                self.runtime.interrupt_worker(current, run_id=run_id)
                            except TypeError as exc:
                                if "run_id" not in str(exc):
                                    raise
                                self.runtime.interrupt_worker(current)
                    except Exception:
                        logger.error(
                            "Failed to clean up a staged runtime after %s control won for %s",
                            current_state or run_state or "operator",
                            worker_id,
                        )
                raise WorkerTerminatedError("Workspace was closed before the run could start")
            storage_guard = (self.storage_runtime.owner_lock(current["tenant_id"], current["owner_id"])
                             if self.storage_runtime else nullcontext())
            with storage_guard, self.files.native_start_guard(current):
                if not self._coordinator_retry_dispatch_ready(current, run):
                    raise RuntimeErrorBase(
                        "The exact child Retry was stopped or changed before native start"
                    )
                if (
                    self._provider_run_start_fence is not None
                    and not self._provider_run_start_fence(run_id)
                ):
                    raise RuntimeErrorBase(
                        "The provider response deadline expired before native work could start"
                    )
                yield

    def _reject_closed_after_runtime_activity(
        self,
        worker_id: str,
        *,
        fallback_worker: dict,
        active_run_id: str = "",
        context: str,
    ) -> dict:
        """Compensate runtime work that lost a race to permanent workspace closure."""

        current = self.store.get_worker(worker_id) or fallback_worker
        if str(current.get("state") or "") not in CLOSED_WORKER_STATES:
            return current
        try:
            cleanup_info = self.runtime.terminate_worker(
                {**current, "_active_run_id": active_run_id}
            )
            if cleanup_info.pid:
                raise RuntimeError(
                    f"Worker compute is still active after {context} close-race cleanup "
                    f"(pid={cleanup_info.pid})"
                )
        except Exception as cleanup_error:
            message = public_callback_message_text(str(cleanup_error)) or "Worker close-race cleanup failed"
            self.store.record_worker_termination_cleanup_failure(worker_id, message)
            logger.error(
                "Failed to clean up runtime recreated by %s after workspace close for %s",
                context,
                worker_id,
            )
        raise ControlPlaneConflict(
            "Workspace is closed; create a new workspace for new work"
        )

    def orchestration_capabilities(self) -> dict[str, object]:
        storage_pressure = self._storage_pressure_v1()
        policy_enabled = isolated_parallel_policy_enabled()
        active_worker_ids = self.store.active_host_mission_worker_ids()
        terminal_history = self.store.conclusively_terminal_host_mission_history()
        process_status_reader = getattr(
            self.runtime, "host_active_process_status", None
        )
        process_state_uncertain = False
        for worker in self.store.list_host_mission_workers():
            worker_id = str(worker.get("worker_id") or "")
            if not callable(process_status_reader):
                process_state_uncertain = True
                continue
            try:
                status = process_status_reader(worker)
            except Exception:
                logger.exception(
                    "Failed to prove host mission process absence for worker %s",
                    worker_id,
                )
                process_state_uncertain = True
                continue
            if not isinstance(status, dict):
                process_state_uncertain = True
                continue
            state = str(status.get("state") or "uncertain")
            if state == "active":
                active_worker_ids.add(worker_id)
            elif state == "uncertain":
                observed_run_id = str(status.get("run_id") or "").strip()
                historical_record_only = (
                    status.get("historical_record_only") is True
                )
                if (
                    not historical_record_only
                    or (worker_id, observed_run_id) not in terminal_history
                ):
                    process_state_uncertain = True
            elif state != "absent":
                process_state_uncertain = True
        isolated_runtime_ready = False
        isolated_runtime_reason = "isolated_runtime_readiness_unavailable"
        isolated_readiness_probe = getattr(
            self.runtime, "isolated_parallel_readiness", None
        )
        if callable(isolated_readiness_probe):
            try:
                try:
                    readiness = isolated_readiness_probe(cached_only=True)
                except TypeError:
                    readiness = isolated_readiness_probe()
                isolated_runtime_ready = bool((readiness or {}).get("ready"))
                raw_reason = str((readiness or {}).get("reason") or "").strip()
                if raw_reason and re.fullmatch(r"[a-z0-9_.-]{1,120}", raw_reason):
                    isolated_runtime_reason = raw_reason
            except Exception:
                logger.exception("Failed to probe isolated Parallel runtime readiness")
        active_host_missions = len(active_worker_ids)
        prompt_layers = worker_prompt_layer_integrity_snapshot(
            include_producer_scope=True
        )
        prompt_layers_ready = valid_worker_prompt_layer_capability(prompt_layers)
        isolated_parallel_ready = bool(
            policy_enabled
            and active_host_missions == 0
            and not process_state_uncertain
            and isolated_runtime_ready
            and bool(storage_pressure.get("healthy"))
            and prompt_layers_ready
        )
        if isolated_parallel_ready:
            isolated_parallel_reason = ""
        elif not policy_enabled:
            isolated_parallel_reason = "isolated_parallel_policy_disabled"
        elif active_host_missions > 0:
            isolated_parallel_reason = "host_missions_active"
        elif process_state_uncertain:
            isolated_parallel_reason = "host_mission_state_uncertain"
        elif storage_pressure.get("errorCode"):
            isolated_parallel_reason = "storage_pressure_unavailable"
        elif not bool(storage_pressure.get("healthy")):
            isolated_parallel_reason = "storage_pressure_critical"
        elif not prompt_layers_ready:
            isolated_parallel_reason = (
                "prompt_layers_unknown"
                if prompt_layers.get("unknownLayerNames")
                else "prompt_layer_capability_invalid"
            )
        else:
            isolated_parallel_reason = isolated_runtime_reason
        native_parallel_ready = bool(
            native_parallel_policy_enabled()
            and bool(storage_pressure.get("healthy"))
            and prompt_layers_ready
        )
        native_parallel_reason = (
            ""
            if native_parallel_ready
            else "native_parallel_not_authorized"
            if not native_parallel_policy_enabled()
            else "storage_pressure_unavailable"
            if storage_pressure.get("errorCode")
            else "storage_pressure_critical"
            if not storage_pressure.get("healthy")
            else "prompt_layer_capability_invalid"
        )
        return {
            "policyVersion": 1,
            "readinessScope": {
                "contractVersion": 1,
                "scope": "deployment",
                "ownerCredentialRole": "transport_auth",
            },
            "isolatedParallelReady": isolated_parallel_ready,
            "isolatedParallelReason": isolated_parallel_reason,
            "nativeParallelReady": native_parallel_ready,
            "nativeParallelReason": native_parallel_reason,
            "sharedHostDesktop": native_parallel_ready,
            "hostMissionsAllowed": not policy_enabled,
            "hostMissionsActive": active_host_missions,
            "storagePressure": storage_pressure,
            "promptLayers": prompt_layers,
            "workTraceContract": self.work_trace_contract_capability(),
        }

    @staticmethod
    def work_trace_contract_capability() -> dict[str, object]:
        return {
            "contractVersion": 1,
            "schemaDigest": WORK_TRACE_SCHEMA_DIGEST,
            "producerSourceIdentity": WORK_TRACE_PRODUCER_SOURCE_IDENTITY,
            "emittedKeySetDigest": WORK_TRACE_EMITTED_KEY_SET_DIGEST,
        }

    def worker_prompt_layer_trace(self) -> dict[str, object]:
        """Return the exact worker prompt-layer fact consumed by Core traceability."""

        snapshot = worker_prompt_layer_integrity_snapshot(
            include_producer_scope=True
        )
        return {
            "contractVersion": snapshot["contractVersion"],
            "producerScope": snapshot["producerScope"],
            "layerNames": sorted(
                worker_prompt_layer_producer_names()
            ),
            "unknownLayerNames": snapshot["unknownLayerNames"],
        }

    def _storage_pressure_v1(self) -> dict[str, object]:
        threshold = _bounded_float_env(
            "GLASSHIVE_STORAGE_PRESSURE_CRITICAL_PERCENT",
            90.0,
            min_value=50.0,
            max_value=99.9,
        )
        warning_margin = _bounded_float_env(
            "GLASSHIVE_STORAGE_PRESSURE_WARNING_MARGIN_PERCENT",
            10.0,
            min_value=1.0,
            max_value=25.0,
        )
        try:
            usage = shutil.disk_usage(self.store.db_path.parent)
            total = int(usage.total)
            used = int(usage.used)
            available = int(usage.free)
            if total <= 0 or used < 0 or available < 0 or used > total:
                raise ValueError("invalid storage probe")
            used_percent = round((used * 100.0) / total, 3)
        except Exception:
            logger.warning(
                "GlassHive storage pressure probe failed closed",
                extra={"error_code": "storage_probe_unavailable"},
            )
            return {
                "version": 1,
                "state": "critical",
                "healthy": False,
                "usedPercent": None,
                "availableBytes": None,
                "thresholdPercent": float(threshold),
                "errorCode": "storage_probe_unavailable",
            }
        if used_percent >= threshold:
            state = "critical"
        elif used_percent >= max(0.0, threshold - warning_margin):
            state = "warning"
        else:
            state = "healthy"
        return {
            "version": 1,
            "state": state,
            "healthy": state != "critical",
            "usedPercent": float(used_percent),
            "availableBytes": available,
            "thresholdPercent": float(threshold),
        }

    def _ensure_profile_allowed(self, profile: str) -> None:
        allowed = allowed_worker_profiles()
        if allowed and str(profile or "").strip() not in allowed:
            raise GlassHiveProfileNotAllowedError(
                f"GlassHive worker profile '{profile}' is not allowed by GLASSHIVE_ALLOWED_WORKER_PROFILES"
            )

    def _ensure_runtime_available(self, profile: str, execution_mode: str) -> None:
        if hasattr(self.runtime, "preflight_worker_profile"):
            self.runtime.preflight_worker_profile(profile, execution_mode)

    @contextmanager
    def _durable_preflight_capacity(
        self,
        profile: str,
        execution_mode: str,
        *,
        tenant_id: str,
        owner_id: str,
        lane: str = "mission",
        worker: dict | None = None,
        trusted_delegation: bool = False,
    ):
        """Hold one durable capacity claim around an external runtime probe."""

        prospective_worker = {
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "profile": profile,
            "runtime": self._initial_runtime_label(profile, execution_mode),
            "execution_mode": execution_mode,
            "trusted_run_lane": (
                "conversation" if lane == "conversation" else "mission"
            ),
            **(worker or {}),
        }
        retry_after_s = self._retry_base_delay_s("host_capacity")
        next_retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_after_s)
        ).isoformat()
        pressure, capacity_snapshot = self._host_resource_capacity_error(
            prospective_worker,
            docker_cached_only=False,
            _include_snapshot=True,
        )
        if pressure:
            pressure, recovered_snapshot = self._relieve_docker_resource_pressure(
                prospective_worker, pressure
            )
            if recovered_snapshot is not None:
                capacity_snapshot = recovered_snapshot
        if pressure:
            pressure.next_retry_at = next_retry_at
            pressure.retry_after_s = retry_after_s
            raise pressure
        policy = self._host_capacity_policy()
        try:
            reservation = self.store.acquire_preflight_capacity_reservation(
                runtime_family=self._host_runtime_family(prospective_worker),
                lane=str(prospective_worker["trusted_run_lane"]),
                tenant_id=str(prospective_worker.get("tenant_id") or "local"),
                owner_id=str(prospective_worker.get("owner_id") or ""),
                profile=profile,
                execution_mode=execution_mode,
                executor_id=self._executor_id,
                **policy,
                mutation_scope=(
                    self._host_mutation_scope(
                        prospective_worker,
                        trusted_delegation=trusted_delegation,
                    )
                    if execution_mode == "host"
                    else ""
                ),
                lease_ttl_s=self._host_lease_ttl_s(),
                capacity_available=dict(capacity_snapshot.get("available") or {}),
                capacity_required=dict(capacity_snapshot.get("required") or {}),
                capacity_reservation=dict(
                    capacity_snapshot.get("reservation") or {}
                ),
                capacity_observed_lease_ids=list(
                    capacity_snapshot.get("observedLeaseIds") or []
                ),
                capacity_next_retry_at=next_retry_at,
            )
        except HostRunLeaseCapacityError as exc:
            error = HostCapacityError(
                str(exc),
                capacity_class=exc.capacity_class,
                dimension=exc.dimension,
                configured=exc.configured,
                used=exc.used,
            )
            error.available = dict(exc.available or {})
            error.required = dict(exc.required or {})
            error.shortage = dict(exc.shortage or {})
            error.reservation = dict(exc.reservation or {})
            error.next_retry_at = str(exc.next_retry_at or next_retry_at)
            error.retry_after_s = retry_after_s
            raise error from exc
        if not isinstance(reservation, dict) or not str(
            reservation.get("reservation_id") or ""
        ):
            error = HostCapacityError(
                "Durable CLI preflight capacity could not be reserved.",
                capacity_class="preflight_reservation",
            )
            error.next_retry_at = next_retry_at
            error.retry_after_s = retry_after_s
            raise error
        reservation_id = str(reservation["reservation_id"])
        lease_ttl_s = max(1.0, float(self._host_lease_ttl_s()))
        expected_expires_at = str(reservation.get("expires_at") or "")
        probe_lease = _DurablePreflightProbeLease()
        heartbeat_stop = Event()

        def is_live() -> bool:
            return not probe_lease.lost and self.store.preflight_capacity_reservation_is_live(
                reservation_id,
                executor_id=self._executor_id,
                profile=profile,
                execution_mode=execution_mode,
            )

        def renew_until_stopped() -> None:
            nonlocal expected_expires_at
            interval = max(0.1, min(10.0, lease_ttl_s / 3.0))
            next_renewal = time.monotonic() + interval
            while True:
                remaining = max(0.0, next_renewal - time.monotonic())
                if heartbeat_stop.wait(remaining):
                    return
                try:
                    renewed = self.store.renew_preflight_capacity_reservation(
                        reservation_id,
                        executor_id=self._executor_id,
                        profile=profile,
                        execution_mode=execution_mode,
                        expected_expires_at=expected_expires_at,
                        lease_ttl_s=lease_ttl_s,
                    )
                except Exception:
                    renewed = None
                if not renewed:
                    probe_lease.mark_lost()
                    return
                expected_expires_at = str(renewed.get("expires_at") or "")
                next_renewal += interval
                observed = time.monotonic()
                if next_renewal <= observed:
                    next_renewal = observed + interval

        heartbeat_thread: Thread | None = None
        release_reason = "preflight_failed"
        try:
            if not is_live():
                release_reason = "invalid_before_preflight"
                raise HostCapacityError(
                    "CLI preflight capacity expired before adapter invocation.",
                    capacity_class="preflight_reservation",
                )
            heartbeat_thread = Thread(
                target=renew_until_stopped,
                name=f"wpr-preflight-lease-{reservation_id[-8:]}",
                daemon=True,
            )
            heartbeat_thread.start()
            yield capacity_snapshot, probe_lease
            if probe_lease.lost:
                release_reason = "preflight_lease_lost"
                raise HostCapacityError(
                    "CLI preflight reservation ownership was lost during the external probe.",
                    capacity_class="preflight_reservation",
                )
            if not is_live():
                raise HostCapacityError(
                    "CLI preflight capacity expired before acceptance.",
                    capacity_class="preflight_reservation",
                )
            release_reason = "preflight_succeeded"
        except BaseException:
            if probe_lease.lost:
                release_reason = "preflight_lease_lost"
            raise
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=max(1.0, min(2.0, lease_ttl_s)))
            self.store.release_preflight_capacity_reservation(
                reservation_id, reason=release_reason
            )

    def _reserved_runtime_preflight(
        self,
        profile: str,
        execution_mode: str,
        *,
        tenant_id: str,
        owner_id: str,
        lane: str = "mission",
        worker: dict | None = None,
        require_capacity_snapshot: bool = False,
        trusted_delegation: bool = False,
    ) -> dict[str, object]:
        """Fence every adapter CLI preflight behind durable, live capacity."""

        has_preflight = hasattr(self.runtime, "preflight_worker_profile")
        uses_cli_subprocess = bool(
            getattr(self.runtime, "preflight_uses_cli_subprocess", True)
        )
        if not has_preflight and not require_capacity_snapshot:
            return {}
        if has_preflight and not uses_cli_subprocess and not require_capacity_snapshot:
            self._ensure_runtime_available(profile, execution_mode)
            return {}
        with self._durable_preflight_capacity(
            profile,
            execution_mode,
            tenant_id=tenant_id,
            owner_id=owner_id,
            lane=lane,
            worker=worker,
            trusted_delegation=trusted_delegation,
        ) as (capacity_snapshot, _probe_lease):
            if has_preflight:
                self._ensure_runtime_available(profile, execution_mode)
            return capacity_snapshot

    def run_reserved_host_subprocess_probe(
        self,
        profile: str,
        probe,
        *,
        tenant_id: str,
        owner_id: str,
        lane: str = "conversation",
    ):
        """Run one host probe only while its provisional capacity is durable."""

        with self._durable_preflight_capacity(
            profile,
            "host",
            tenant_id=tenant_id,
            owner_id=owner_id,
            lane=lane,
        ) as (_capacity_snapshot, probe_lease):
            return probe(probe_lease)

    def _resolve_worker_model(self, profile: str, execution_mode: str = "docker", *,
                              tenant_id: str = "local", owner_id: str = "") -> str:
        if profile == "grok-build":
            return selected_grok_model(self.store, tenant_id, owner_id)[0]
        try:
            return str(self.runtime.resolve_model(profile, execution_mode=execution_mode) or "")
        except TypeError:
            return str(self.runtime.resolve_model(profile) or "")

    @staticmethod
    def prompt_workbench_scheduled_alias(alias: str, fingerprint: str) -> str:
        clean_alias = str(alias or "prompt-workbench-scheduled").strip()
        clean_fingerprint = str(fingerprint or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", clean_fingerprint):
            raise ParallelExecutionIsolationError(
                "Prompt Workbench scheduled authority fingerprint is invalid."
            )
        return (
            f"{clean_alias[:160]}--{PROMPT_WORKBENCH_SCHEDULED_ALIAS_NAMESPACE}-"
            f"{clean_fingerprint[:24]}"
        )

    @staticmethod
    def _configured_prompt_workbench_effort(profile: str) -> str:
        clean_profile = str(profile or "").strip()
        if clean_profile == "codex-cli":
            value = str(
                os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT") or ""
            ).strip().lower()
            return value if value in PARALLEL_CLEAN_ROOM_EFFORT_ENV_VALUES[
                "WPR_CODEX_CLI_REASONING_EFFORT"
            ] else ""
        if clean_profile == "claude-code":
            value = str(
                os.environ.get("WPR_CLAUDE_CODE_EFFORT") or "default"
            ).strip().lower()
            return value if value in PARALLEL_CLEAN_ROOM_EFFORT_ENV_VALUES[
                "WPR_CLAUDE_CODE_EFFORT"
            ] else ""
        return ""

    def derive_prompt_workbench_scheduled_bootstrap(
        self,
        *,
        owner_id: str,
        profile: str,
        execution_mode: str,
        bootstrap_profile: str | None,
        bootstrap_bundle: dict | None,
    ) -> tuple[str, dict, str]:
        """Validate a service request and mint one isolated scheduled authority."""

        def reject(reason: str) -> None:
            raise ParallelExecutionIsolationError(
                f"Prompt Workbench scheduled authority rejected: {reason}.",
                reason_code="scheduled_authority_invalid",
            )

        if (
            str(bootstrap_profile or "").strip()
            != PROMPT_WORKBENCH_SCHEDULED_BOOTSTRAP_PROFILE
            or str(execution_mode or "").strip().lower() != "docker"
            or not isinstance(bootstrap_bundle, dict)
        ):
            reject("the bootstrap profile or execution mode is invalid")
        request = bootstrap_bundle.get(
            PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_REQUEST
        )
        if not isinstance(request, dict) or set(request) not in (
            {"version", "kind", "execution_mode", "primary"},
            {"version", "kind", "execution_mode", "primary", "fallback"},
        ):
            reject("the structured authority request is invalid")
        if (
            request.get("version") != 1
            or isinstance(request.get("version"), bool)
            or request.get("kind") != PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_KIND
            or request.get("execution_mode") != "docker"
        ):
            reject("the structured authority request is invalid")
        if any(
            key in bootstrap_bundle
            for key in (
                "execution_policy",
                "viventium_launch_authority",
                "glasshive_capability_authorization",
                "glasshive_capability_broker",
                "glasshive_capability_requirement",
                "claude_project_mcp",
                "codex_config_append",
            )
        ):
            reject("caller-authored policy or capability authority is not allowed")

        def exact_route(value: object, *, label: str) -> dict[str, str]:
            if not isinstance(value, dict) or set(value) != {
                "worker_profile",
                "model",
                "reasoning_effort",
            }:
                reject(f"the {label} route tuple is invalid")
            route = {
                key: str(value.get(key) or "").strip()
                for key in ("worker_profile", "model", "reasoning_effort")
            }
            if not all(route.values()):
                reject(f"the {label} route tuple is incomplete")
            return route

        primary = exact_route(request.get("primary"), label="primary")
        if primary["worker_profile"] != str(profile or "").strip():
            reject("the primary profile does not match the worker request")
        try:
            self._ensure_profile_allowed(primary["worker_profile"])
        except GlassHiveProfileNotAllowedError:
            reject("the primary profile is not allowed")
        primary_model = self._resolve_worker_model(
            primary["worker_profile"], "docker"
        ).strip()
        primary_effort = self._configured_prompt_workbench_effort(
            primary["worker_profile"]
        )
        if (
            primary["model"] != primary_model
            or primary["reasoning_effort"] != primary_effort
        ):
            reject("the primary tuple does not match the compiled route")

        fallback: dict[str, str] | None = None
        if "fallback" in request:
            fallback = exact_route(request.get("fallback"), label="fallback")
            if fallback["worker_profile"] == primary["worker_profile"]:
                reject("the fallback profile must be distinct")
            try:
                self._ensure_profile_allowed(fallback["worker_profile"])
            except GlassHiveProfileNotAllowedError:
                reject("the fallback profile is not allowed")
            fallback_model = self._resolve_worker_model(
                fallback["worker_profile"], "docker"
            ).strip()
            fallback_effort = self._configured_prompt_workbench_effort(
                fallback["worker_profile"]
            )
            if (
                fallback["model"] != fallback_model
                or fallback["reasoning_effort"] != fallback_effort
            ):
                reject("the fallback tuple does not match the compiled route")
        configured_fallback_profile = str(
            os.environ.get("GLASSHIVE_DEFAULT_FALLBACK_WORKER_PROFILE") or ""
        ).strip()
        requested_fallback_profile = (
            fallback["worker_profile"] if fallback else ""
        )
        if requested_fallback_profile != configured_fallback_profile:
            reject("the fallback profile does not match the compiled route")

        callbacks = bootstrap_bundle.get("callbacks")
        callback_keys = {
            "events_webhook_url",
            "hmac_secret",
            "user_id",
            "conversation_id",
            "parent_message_id",
            "message_id",
            "surface",
            "scheduled_prompt_run_id",
            "scheduled_prompt_task_id",
        }
        if not isinstance(callbacks, dict) or set(callbacks) != callback_keys:
            reject("the callback envelope is invalid")
        callback_url = str(callbacks.get("events_webhook_url") or "").strip()
        callback_secret = str(callbacks.get("hmac_secret") or "").strip()
        task_id = str(callbacks.get("scheduled_prompt_task_id") or "").strip()
        run_id = str(callbacks.get("scheduled_prompt_run_id") or "").strip()
        load_viventium_runtime_env({"VIVENTIUM_GLASSHIVE_CALLBACK_SECRET"})
        canonical_callback_secret = str(
            os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET") or ""
        ).strip()
        if (
            not _is_local_scheduling_cortex_callback_url(callback_url)
            or not callback_secret
            or not canonical_callback_secret
            or not hmac.compare_digest(
                callback_secret.encode("utf-8"),
                canonical_callback_secret.encode("utf-8"),
            )
            or not task_id
            or not run_id
            or str(callbacks.get("user_id") or "").strip()
            != str(owner_id or "").strip()
            or str(callbacks.get("conversation_id") or "").strip()
            != f"workbench-scheduled-prompt:{task_id}"
            or str(callbacks.get("parent_message_id") or "").strip()
            != f"scheduled-prompt:{task_id}"
            or str(callbacks.get("message_id") or "").strip() != run_id
            or callbacks.get("surface") != "workbench"
        ):
            reject("the callback envelope is invalid")

        expected_env: dict[str, str] = {}
        for route in (primary, fallback):
            if not route:
                continue
            env_name = (
                "WPR_CODEX_CLI_REASONING_EFFORT"
                if route["worker_profile"] == "codex-cli"
                else "WPR_CLAUDE_CODE_EFFORT"
            )
            expected_env[env_name] = route["reasoning_effort"]
        if bootstrap_bundle.get("env") != expected_env:
            reject("the bootstrap environment does not match the route tuples")
        try:
            _validate_parallel_clean_room_files(bootstrap_bundle)
        except ParallelExecutionIsolationError:
            reject("the workspace file projection is invalid")
        if _contains_parallel_forbidden_authority_key(bootstrap_bundle):
            reject("caller provider credentials are not allowed")

        sanitized_bundle = {
            key: value
            for key, value in bootstrap_bundle.items()
            if key != PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_REQUEST
        }
        sanitized_bundle["callbacks"] = {
            "events_webhook_url": callback_url,
            "hmac_secret": callback_secret,
            "origin_ref": run_id,
        }
        sanitized_bundle["env"] = expected_env
        clean_profile, canonical_bundle = derive_parallel_clean_room_bootstrap(
            None, sanitized_bundle
        )
        canonical_bundle["viventium_launch_authority"] = {
            "version": 1,
            "kind": PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_KIND,
            "execution_mode": "docker",
            **(
                {"fallback_worker_profile": fallback["worker_profile"]}
                if fallback
                else {}
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return clean_profile, canonical_bundle, fingerprint

    def _refresh_worker_model_for_profile(self, worker: dict) -> dict:
        profile = str(worker.get("profile") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        if not profile or not worker_id:
            return worker
        bootstrap_bundle = self._bootstrap_bundle_for(worker) or {}
        provider_model = str(bootstrap_bundle.get("provider_model") or "").strip()
        try:
            resolved_model = provider_model or self._resolve_worker_model(
                profile, str(worker.get("execution_mode") or "docker"),
                tenant_id=str(worker.get("tenant_id") or "local"), owner_id=str(worker.get("owner_id") or ""),
            ).strip()
        except Exception as exc:
            logger.warning("Could not resolve model for worker %s profile %s: %s", worker_id, profile, exc)
            return worker
        current_model = str(worker.get("model") or "").strip()
        if not resolved_model or resolved_model == current_model:
            return worker
        updated = self.store.update_worker(worker_id, model=resolved_model) or worker
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            "worker.model_refreshed",
            f"Worker model refreshed from {current_model or '<unset>'} to {resolved_model}",
        )
        return updated

    def _operator_base_url(self) -> str:
        return (
            os.environ.get("GLASSHIVE_OPERATOR_BASE_URL", "").strip()
            or os.environ.get("WPR_OPERATOR_BASE_URL", "").strip()
            or os.environ.get("WPR_PUBLIC_BASE_URL", "").strip()
        ).rstrip("/")

    def _artifact_base_url(self) -> str:
        return (
            os.environ.get("GLASSHIVE_ARTIFACT_BASE_URL", "").strip()
            or os.environ.get("WPR_ARTIFACT_BASE_URL", "").strip()
            or os.environ.get("GLASSHIVE_RUNTIME_PUBLIC_BASE_URL", "").strip()
            or self._operator_base_url()
        ).rstrip("/")

    def _signed_link_params(self, worker: dict, *, kind: str, path: str = "") -> dict[str, str]:
        if str(worker.get("state") or "") == "terminated":
            return {}
        return sign_link_params(
            kind=kind,
            worker_id=str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
            path=path,
        )

    def _signed_artifact_url(self, worker: dict, workspace_path: str, *, kind: str, action: str) -> str:
        base_url = self._artifact_base_url()
        worker_id = str(worker.get("worker_id") or "")
        path = str(workspace_path or "").strip().lstrip("/")
        if (
            str(worker.get("state") or "") == "terminated"
            or not base_url
            or not worker_id
            or not path
            or not is_user_deliverable_relative_path(path)
        ):
            return ""
        token = sign_link_token(
            kind=kind,
            worker_id=worker_id,
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
            path=path,
        )
        if token:
            ref_id = create_signed_link_ref(token=token)
            return signed_link_ref_url(base_url, ref_id) if ref_id else ""
        if str(worker.get("tenant_id") or "") not in {"", "local"}:
            return ""
        return f"{base_url}/v1/workers/{worker_id}/artifacts/{action}?{urlencode({'path': path})}"

    def _signed_artifact_open_url(self, worker: dict, workspace_path: str) -> str:
        return self._signed_artifact_url(worker, workspace_path, kind="artifact_open", action="open")

    def _signed_artifact_download_url(self, worker: dict, workspace_path: str) -> str:
        return self._signed_artifact_url(worker, workspace_path, kind="artifact_download", action="download")

    def render_provider_native_images(self, request: dict, run: dict, output: str) -> str:
        """Resolve model-selected input images through the existing owner artifact boundary."""
        if "](" not in output:
            return output
        run_id = str(run.get("run_id") or "")
        worker = self.store.get_worker(str(run.get("worker_id") or ""))
        resolver = getattr(self.runtime, "provider_native_image_output", None)
        if not worker or not callable(resolver):
            if re.search(r"\]\(\s*<?artifact_sha256:", output):
                raise RuntimeErrorBase("Selected native image is unavailable for this request")
            return output
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id)
            or request.get("run_id") != run_id
            or not request.get("owner_id") or request.get("owner_id") != worker.get("owner_id")
            or request.get("tenant_id") != worker.get("tenant_id")
        ):
            raise RuntimeErrorBase("Native image output does not match the request owner and run")
        projected, images = resolver(worker, run, output)
        authorized = {"artifact_sha256:" + hashlib.sha256(data).hexdigest(): (mime, data)
                      for _path, mime, data in images}
        selected_urls: dict[str, str] = {}

        def render(match: re.Match[str]) -> str:
            reference = match.group(2).strip().removeprefix("<").removesuffix(">")
            if not reference.startswith("artifact_sha256:"):
                return match.group(0)
            if reference not in authorized:
                raise RuntimeErrorBase("Selected native image is unavailable for this request")
            if reference not in selected_urls:
                mime, data = authorized[reference]
                relative = NATIVE_MEDIA_PREFIX / run_id / (reference.split(":", 1)[1] + NATIVE_IMAGE_SUFFIXES[mime])
                workspace = Path(str(worker.get("workspace_dir") or ""))
                try:
                    published = _native_media_snapshot(workspace, relative, len(data))
                except (OSError, ValueError):
                    published = None
                if published != data:
                    _publish_native_media(workspace, relative, data)
                url = self._signed_artifact_download_url(worker, relative.as_posix())
                if not url:
                    raise RuntimeErrorBase("Selected native image download is unavailable")
                selected_urls[reference] = url
            return f"{match.group(1)}({selected_urls[reference]})"

        return re.sub(r"(!?\[(?:\\.|[^\]\r\n])*\])\((<[^>\r\n]*>|[^()\r\n]*)\)", render, projected)

    def _native_media_callback_observations(self, worker: dict, run: dict) -> dict[str, object]:
        media = native_media_observations(worker, run)
        observations = []
        omitted = media["omitted_count"]
        for item in media["observations"]:
            path = str(item["workspace_path"])
            download_url = self._signed_artifact_download_url(worker, path)
            open_url = self._signed_artifact_open_url(worker, path)
            if not download_url or not open_url:
                omitted += 1
                continue
            observations.append({
                **{key: value for key, value in item.items() if key != "workspace_path"},
                "download_url": download_url, "open_url": open_url,
            })
        return {"observations": observations, "omitted_count": omitted}

    def _signed_watch_url(self, worker: dict, callbacks: dict[str, object] | None = None) -> str:
        callbacks = callbacks or {}
        if str(worker.get("state") or "") == "terminated":
            return ""
        worker_id = str(worker.get("worker_id") or "").strip()
        project_id = str(worker.get("project_id") or "").strip()
        base_url = self._operator_base_url()
        if not worker_id or not base_url or not surface_aware_watch_url(
            worker_id,
            project_id,
            request_surface=str(callbacks.get("surface") or ""),
            watch_surface="desktop",
            base_url=base_url,
        ):
            return ""
        token = sign_link_token(
            kind="worker_view",
            worker_id=worker_id,
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
        )
        watch_url = surface_aware_watch_url(
            worker_id,
            project_id,
            request_surface=str(callbacks.get("surface") or ""),
            watch_surface="desktop",
            base_url=base_url,
        )
        if not watch_url:
            return ""
        if token:
            target_url = append_signed_query(watch_url, {"gh_token": token})
            ref_id = create_signed_link_ref(token=token, target_url=target_url)
            return signed_link_ref_url(base_url, ref_id, route="/r") if ref_id else ""
        if str(worker.get("tenant_id") or "") not in {"", "local"}:
            return ""
        return watch_url

    def _callback_message_with_links(
        self,
        worker: dict,
        message: str,
        deliverable: dict[str, object] | None,
        callbacks: dict[str, object] | None = None,
        *,
        include_watch_link: bool = False,
    ) -> str:
        text = public_callback_message_text(message)
        if not deliverable and not include_watch_link:
            return text
        links: list[str] = []
        workspace_path = str((deliverable or {}).get("workspace_path") or "").strip()
        if deliverable and workspace_path:
            download_url = self._signed_artifact_download_url(worker, workspace_path)
            if download_url:
                links.append(f"File: [Download file]({download_url})")
            open_url = self._signed_artifact_open_url(worker, workspace_path)
            if open_url:
                links.append(f"Preview: [Open GlassHive file]({open_url})")
        watch_url = self._signed_watch_url(worker, callbacks)
        if watch_url:
            links.append(f"View / Steer: [Open GlassHive workspace]({watch_url})")
        if not links:
            return text
        suffix = "\n".join(links)
        return f"{text}\n\n{suffix}" if text else suffix

    def _emit_callback_parallel(
        self,
        worker: dict,
        event_type: str,
        *,
        run: dict | None = None,
        message: str = "",
        full_message: str = "",
        deliverable: dict[str, object] | None = None,
        callback_id: str = "",
        insert_once: bool = False,
        submit_delivery: bool = True,
        persist_callback: bool = True,
    ) -> dict | None:
        requested_callback_id = str(callback_id or "").strip()
        callbacks = self._callback_config_for_event(worker, run)
        url = str(callbacks.get("events_webhook_url") or callbacks.get("url") or "").strip()
        if not url:
            return None
        run_id = str((run or {}).get("run_id") or "").strip()
        # The durable callback producer is run-scoped. Worker-only lifecycle
        # notices have no exact attempt identity and must not enter this
        # transport channel.
        if not run_id:
            return None
        durable_run = self.store.get_run(run_id) if run_id else None
        if run_id and (
            durable_run is None
            or str(durable_run.get("worker_id") or "")
            != str(worker.get("worker_id") or "")
            or str(durable_run.get("project_id") or "")
            != str(worker.get("project_id") or "")
        ):
            return None
        delegation = self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if _is_viventium_callback_url(url):
            if not self._viventium_callback_context_ready(worker, callbacks):
                missing_parent_fields = _missing_parent_callback_fields(callbacks)
                logger.info(
                    "Skipping GlassHive parent callback for worker %s because callback context is incomplete: %s",
                    worker.get("worker_id"),
                    ", ".join(missing_parent_fields),
                )
                return None
            if (
                not run_id
                or durable_run is None
                or not delegation
                or not str(delegation.get("origin_ref") or "").strip()
                or not str(delegation.get("work_ref") or "").strip()
            ):
                return None
        durable_state = str((durable_run or {}).get("state") or "").strip()
        durable_ended_at = str((durable_run or {}).get("ended_at") or "").strip()
        delegation_work_state = ""
        delegation_work_terminal = False
        if delegation:
            delegation_work_state, delegation_work_terminal = (
                self._delegation_callback_state(
                    delegation,
                    callback_run=run,
                )
            )
        terminal_result_state = Store.terminal_callback_wire_state(
            durable_state=durable_state,
            event_type=event_type,
        )
        terminal_event = bool(
            terminal_result_state
            and durable_ended_at
        )
        if terminal_event:
            supplied_run = run or {}
            exact_terminal_result = all(
                str(supplied_run.get(key) or "")
                == str((durable_run or {}).get(key) or "")
                for key in (
                    "active_attempt_id",
                    "state",
                    "ended_at",
                    "output_text",
                    "error_text",
                    "terminal_result_revision",
                )
            )
            if not exact_terminal_result:
                return None
            run = dict(durable_run or {})
            durable_output = str(run.get("output_text") or "")
            durable_error = str(run.get("error_text") or "")
            if durable_state == "completed":
                message = terminal_callback_message(durable_output) or "Run completed"
                full_message = terminal_callback_full_message(durable_output)
                if full_message == message:
                    full_message = ""
                deliverable = self._completion_deliverable(
                    worker, run, durable_output, durable_error
                )
            elif durable_state == "failed":
                message = runtime_failure_callback_message(
                    run, durable_error or "Run failed"
                )
                full_message = ""
                deliverable = self._completion_deliverable(
                    worker, run, durable_output, durable_error
                )
                if deliverable and self._fresh_user_artifact_deliverable(worker, run, deliverable):
                    # Deliver the preserved file with the typed failure; never as completion.
                    message = self._partial_artifact_failure_message(deliverable, message)
            elif durable_state == "cancelled":
                message = "Work stop confirmed."
                if durable_error:
                    message = f"{message} {durable_error}"
                full_message = ""
                deliverable = None
            else:
                message = durable_error or f"Run {durable_state}"
                full_message = ""
                deliverable = None
        attempt_number = self._callback_attempt_number(durable_run or run)
        replay_callback_timestamp: int | float | None = None
        if requested_callback_id and insert_once:
            existing_callback = self.store.get_callback_outbox(
                requested_callback_id
            )
            if existing_callback is not None:
                try:
                    existing_payload = json.loads(
                        str(existing_callback.get("payload_json") or "{}")
                    )
                except (TypeError, json.JSONDecodeError):
                    existing_payload = {}
                existing_timestamp = (
                    existing_payload.get("callback_ts")
                    if isinstance(existing_payload, dict)
                    else None
                )
                if (
                    isinstance(existing_timestamp, (int, float))
                    and not isinstance(existing_timestamp, bool)
                    and math.isfinite(float(existing_timestamp))
                    and float(existing_timestamp) > 0
                ):
                    replay_callback_timestamp = existing_timestamp
        terminal_generation = bool(
            not requested_callback_id
            and terminal_result_state
            and durable_ended_at
            and (not delegation or delegation_work_terminal)
        )
        run_terminal_observation = bool(
            not requested_callback_id
            and terminal_result_state
            and durable_ended_at
            and delegation
            and not delegation_work_terminal
        )
        if terminal_generation:
            terminal_result_revision = int(
                (durable_run or {}).get("terminal_result_revision") or 0
            )
            if terminal_result_revision < 1:
                return None
            terminal_result_digest = Store.terminal_result_digest(
                durable_run or {}
            )
            resolved_callback_id = Store.terminal_callback_id(
                run_id=run_id,
                state=terminal_result_state,
                ended_at=durable_ended_at,
                attempt_number=attempt_number,
                result_revision=terminal_result_revision,
                result_digest=terminal_result_digest,
            )
            try:
                callback_timestamp = int(
                    self._normalized_datetime(durable_ended_at).timestamp()
                )
            except (TypeError, ValueError):
                callback_timestamp = int(time.time())
        elif run_terminal_observation:
            observed_result_revision = int(
                (durable_run or {}).get("terminal_result_revision") or 0
            )
            if observed_result_revision < 1:
                return None
            observed_result_digest = Store.terminal_result_digest(
                durable_run or {}
            )
            canonical_callback_id = Store.terminal_callback_id(
                run_id=run_id,
                state=terminal_result_state,
                ended_at=durable_ended_at,
                attempt_number=attempt_number,
                result_revision=observed_result_revision,
                result_digest=observed_result_digest,
            )
            resolved_callback_id = canonical_callback_id.replace(
                "cb_terminal_", "cb_run_terminal_", 1
            )
            terminal_result_revision = 0
            terminal_result_digest = ""
            try:
                callback_timestamp = int(
                    self._normalized_datetime(durable_ended_at).timestamp()
                )
            except (TypeError, ValueError):
                callback_timestamp = int(time.time())
        else:
            terminal_result_revision = 0
            terminal_result_digest = ""
            resolved_callback_id = requested_callback_id or f"cb_{uuid.uuid4().hex}"
            callback_timestamp = (
                replay_callback_timestamp
                if replay_callback_timestamp is not None
                else int(time.time())
            )
        link_safe = event_type != "worker.terminated"
        operator_url = self._signed_watch_url(worker, callbacks) if link_safe else ""
        include_watch_link = link_safe and (
            event_type in ACTIONABLE_CALLBACK_LINK_EVENTS
            or event_type == "run.needs_input"
        )
        payload = {
            "callback_id": resolved_callback_id,
            "callback_ts": callback_timestamp,
            "attempt_number": attempt_number if attempt_number > 0 else None,
            "event": event_type,
            "project_id": worker.get("project_id"),
            "worker_id": worker.get("worker_id"),
            "run_id": run_id or None,
            "run_state": callback_run_state(event_type, run),
            "message": self._callback_message_with_links(
                worker,
                message,
                deliverable,
                callbacks,
                include_watch_link=include_watch_link,
            ),
            "full_message": self._callback_message_with_links(
                worker,
                full_message,
                deliverable,
                callbacks,
                include_watch_link=include_watch_link,
            )
            if full_message
            else "",
            "user_id": callbacks.get("user_id"),
            "agent_id": callbacks.get("agent_id"),
            "conversation_id": callbacks.get("conversation_id"),
            "parent_message_id": callbacks.get("parent_message_id"),
            "message_id": callbacks.get("message_id"),
            "surface": callbacks.get("surface"),
            "input_mode": callbacks.get("input_mode"),
            "stream_id": callbacks.get("stream_id"),
            "voice_call_session_id": callbacks.get("voice_call_session_id"),
            "voice_request_id": callbacks.get("voice_request_id"),
            "telegram_chat_id": callbacks.get("telegram_chat_id"),
            "telegram_user_id": callbacks.get("telegram_user_id"),
            "telegram_message_id": callbacks.get("telegram_message_id"),
            "logical_turn_id": callbacks.get("logical_turn_id"),
            "logical_turn_revision": callbacks.get("logical_turn_revision"),
        }
        if terminal_result_digest:
            payload["result_revision"] = terminal_result_revision
            payload["result_digest"] = terminal_result_digest
            payload["result_state"] = terminal_result_state
            payload["result_ended_at"] = durable_ended_at
        if event_type in {"run.waiting_on_capacity", "run.queue_status"} or (
            event_type == "run.failed"
            and str((run or {}).get("failure_class") or "")
            == "queue_wait_timeout"
        ):
            first_queued_at = str((run or {}).get("first_queued_at") or "")
            queue_age_seconds = 0
            if first_queued_at:
                try:
                    queue_age_seconds = max(
                        0,
                        int(
                            (
                                self._now_datetime()
                                - self._normalized_datetime(first_queued_at)
                            ).total_seconds()
                        ),
                    )
                except (TypeError, ValueError):
                    queue_age_seconds = 0
            payload.update(
                {
                    "queueAgeSeconds": queue_age_seconds,
                    "blocker": {
                        "class": str(
                            (run or {}).get("queue_blocker_class")
                            or (run or {}).get("failure_class")
                            or "admission_pending"
                        )
                    },
                    "nextRetryAt": str(
                        (run or {}).get("capacity_next_retry_at")
                        or (run or {}).get("retry_after")
                        or ""
                    )
                    or None,
                    "timeoutAt": str(
                        (run or {}).get("queue_deadline_at") or ""
                    )
                    or None,
                }
            )
        if delegation:
            # These identifiers come from the durable delegation reservation,
            # never from callback input supplied by the caller.
            payload["origin_ref"] = str(delegation.get("origin_ref") or "")
            payload["work_ref"] = str(delegation.get("work_ref") or "")
            payload["work_state"] = delegation_work_state
            payload["work_terminal"] = delegation_work_terminal
        failure_class = str((run or {}).get("failure_class") or "").strip()
        if failure_class:
            payload["failure_code"] = failure_class
            payload["failure_class"] = failure_class
            payload["failure_retryable"] = bool((run or {}).get("failure_retryable"))
            _attach_failure_guidance(payload, run)
            provider_route_decision = str(
                (run or {}).get("provider_route_decision") or ""
            ).strip()
            if provider_route_decision:
                payload["provider_route_decision"] = provider_route_decision
        projection_resolver = getattr(self.runtime, "effort_projection_for_worker", None)
        if callable(projection_resolver):
            try:
                effort_projection = projection_resolver(worker)
            except Exception:
                effort_projection = {}
            if isinstance(effort_projection, dict) and effort_projection:
                payload["effort_projection"] = {
                    "requested": str(effort_projection.get("requested") or "")[:32],
                    "effective": str(effort_projection.get("effective") or "")[:32],
                    "fallback_reason": str(effort_projection.get("fallback_reason") or "")[:64],
                }
        if deliverable:
            payload["deliverable"] = deliverable
        if terminal_result_state and durable_ended_at:
            retained_callback = self.store.get_callback_outbox(resolved_callback_id)
            if retained_callback is None:
                payload["run_input"] = accepted_run_input(durable_run or {})
            else:
                # The outbox owns replay bytes. Do not retrofit an older
                # accepted callback or reinterpret its input after an upgrade.
                retained_payload = json.loads(retained_callback["payload_json"])
                if "run_input" in retained_payload:
                    payload["run_input"] = retained_payload["run_input"]
            media = self._native_media_callback_observations(worker, durable_run or run or {})
            if media["observations"] or media["omitted_count"]:
                payload["native_media"] = media
        if operator_url:
            payload["operator_url"] = operator_url
            payload["watch_url"] = operator_url
        intent = {
            "callback_id": str(payload["callback_id"]),
            "project_id": str(worker.get("project_id") or ""),
            "worker_id": str(worker.get("worker_id") or ""),
            "tenant_id": str(worker.get("tenant_id") or "local"),
            "run_id": (run or {}).get("run_id"),
            "attempt_number": attempt_number if attempt_number > 0 else None,
            "event_type": event_type,
            "url": url,
            "payload_json": json.dumps(payload, ensure_ascii=False),
        }
        if not persist_callback:
            return intent
        persisted_intent = {
            key: value for key, value in intent.items() if key != "tenant_id"
        }
        if terminal_generation:
            record = self.store.insert_terminal_callback_outbox_if_current(
                **persisted_intent,
                expected_state=durable_state,
                expected_ended_at=durable_ended_at,
                expected_attempt_id=str(
                    (durable_run or {}).get("active_attempt_id") or ""
                ),
                expected_result_revision=terminal_result_revision,
                expected_result_digest=terminal_result_digest,
            )
            if record is None:
                return None
        else:
            insert_callback = (
                self.store.insert_callback_outbox_once
                if insert_once
                else self.store.upsert_callback_outbox
            )
            try:
                record = insert_callback(**persisted_intent)
            except CallbackIntentGenerationConflictError:
                # The run advanced after this nonterminal event was built. A
                # newer attempt now owns callback identity, so discard the
                # stale intent instead of attaching it to that generation.
                return None
        if submit_delivery:
            self.executor.submit(
                self._deliver_callback_record, dict(worker), record, callbacks
            )
        return record

    def _emit_callback(
        self,
        worker: dict,
        event_type: str,
        *,
        run: dict | None = None,
        message: str = "",
        full_message: str = "",
        deliverable: dict[str, object] | None = None,
        callback_id: str = "",
        insert_once: bool = False,
        submit_delivery: bool = True,
        persist_callback: bool = True,
    ) -> dict | None:
        return self._emit_callback_parallel(
            worker,
            event_type,
            run=run,
            message=message,
            full_message=full_message,
            deliverable=deliverable,
            callback_id=callback_id,
            insert_once=insert_once,
            submit_delivery=submit_delivery,
            persist_callback=persist_callback,
        )

    def _submit_persisted_callback(self, worker: dict, record: dict | None) -> None:
        if not record:
            return
        callbacks = self._callback_config_for(worker)
        self.executor.submit(
            self._deliver_callback_record,
            dict(worker),
            record,
            callbacks,
        )

    def _completion_deliverable(self, worker: dict, run: dict, output_text: str, error_text: str = "") -> dict[str, object] | None:
        return deliverable_payload(self.files.artifact_worker(worker), run, output_text, output_text, error_text)

    def _promote_completed_deliverable(
        self,
        worker: dict,
        run: dict,
        deliverable: dict[str, object] | None,
    ) -> None:
        if not deliverable:
            return
        if deliverable.get("kind") != "webpage" or deliverable.get("preferred_surface") != "desktop":
            return
        if str(worker.get("execution_mode") or "docker") == "host":
            return
        browser_url = str(deliverable.get("browser_url") or "").strip()
        run_id = str(run.get("run_id") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        project_id = str(worker.get("project_id") or "").strip()
        if not browser_url or not run_id or not worker_id or not project_id:
            return
        promotion_key = f"{run_id}:{browser_url}"
        if not hasattr(self.runtime, "desktop_action"):
            return
        with self._deliverable_promotions_lock:
            if promotion_key in self._deliverable_promotions:
                return
            existing_events = self.store.list_events(worker_id)
            if any(
                event.get("event_type") == "deliverable.opened"
                and str(event.get("message") or "") == promotion_key
                for event in existing_events
            ):
                self._deliverable_promotions.add(promotion_key)
                return
            self._deliverable_promotions.add(promotion_key)
            try:
                self.desktop_action(worker_id, "browser", url=browser_url, run_id=run_id)
                self.store.add_event(project_id, worker_id, run_id, "deliverable.opened", promotion_key)
            except Exception as exc:
                self._deliverable_promotions.discard(promotion_key)
                logger.warning("Failed to promote GlassHive deliverable %s: %s", promotion_key, exc)

    def _idle_terminate_after_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_IDLE_TERMINATE_AFTER_S", 0, min_value=0, max_value=30 * 24 * 3600)

    def _paused_terminate_after_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_PAUSED_TERMINATE_AFTER_S", 0, min_value=0, max_value=30 * 24 * 3600)

    def _max_run_duration_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_MAX_RUN_DURATION_S", 0, min_value=0, max_value=30 * 24 * 3600)

    def _idle_reaper_interval_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_IDLE_REAPER_INTERVAL_S", 60, min_value=1, max_value=3600)

    def _ephemeral_retention_s(self) -> int:
        return _bounded_int_env(
            "GLASSHIVE_EPHEMERAL_RETENTION_S",
            7 * 24 * 3600,
            min_value=60,
            max_value=365 * 24 * 3600,
        )

    def _ephemeral_gc_enabled(self) -> bool:
        return str(os.environ.get("GLASSHIVE_EPHEMERAL_GC_ENABLED", "true")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
            "disabled",
        }

    def _ephemeral_gc_claim_ttl_s(self) -> int:
        return _bounded_int_env(
            "GLASSHIVE_EPHEMERAL_GC_CLAIM_TTL_S",
            60,
            min_value=10,
            max_value=3600,
        )

    def _lifecycle_reaper_enabled(self) -> bool:
        orphan_reaper_enabled = str(os.environ.get("GLASSHIVE_ORPHAN_REAPER_ENABLED", "true")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
            "disabled",
        }
        return (
            orphan_reaper_enabled
            or self._ephemeral_gc_enabled()
            or self._idle_terminate_after_s() > 0
            or self._paused_terminate_after_s() > 0
            or self._max_run_duration_s() > 0
            or self.store.has_compute_release_claims()
        )

    def _managed_ephemeral_storage_root(self, worker: dict) -> Path | None:
        """Attest a canonical managed Docker root without trusting persisted paths."""

        if str(worker.get("execution_mode") or "docker").strip().lower() != "docker":
            return None
        if str(worker.get("workspace_root") or "").strip():
            return None
        worker_id = str(worker.get("worker_id") or "").strip()
        if (
            not worker_id
            or worker_id in {".", ".."}
            or Path(worker_id).name != worker_id
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", worker_id)
        ):
            return None

        runtime_manager = self.runtime
        selector = getattr(runtime_manager, "_runtime_for_worker", None)
        if callable(selector):
            try:
                runtime_manager = selector(worker)
            except Exception:
                return None

        path_map: dict[str, Path] = {}
        root_value: object | None = None
        managed_root = getattr(runtime_manager, "managed_worker_root", None)
        if callable(managed_root):
            try:
                root_value = managed_root(worker)
            except TypeError:
                root_value = managed_root(worker_id)
            except Exception:
                return None
        if root_value is None:
            sandbox = getattr(runtime_manager, "sandbox", None)
            paths = getattr(sandbox, "paths", None)
            if callable(paths):
                try:
                    raw_paths = paths(worker_id)
                except Exception:
                    return None
                if isinstance(raw_paths, dict):
                    path_map = {
                        str(key): Path(value).expanduser()
                        for key, value in raw_paths.items()
                        if value is not None
                    }
                    root_value = path_map.get("worker_root")
        if root_value is None:
            workers_dir = getattr(runtime_manager, "workers_dir", None)
            if workers_dir is not None:
                root_value = Path(workers_dir).expanduser() / worker_id
        if root_value is None:
            return None

        configured_root = Path(root_value).expanduser()
        try:
            canonical_parent = configured_root.parent.resolve(strict=True)
            if not canonical_parent.is_dir():
                return None
            root = canonical_parent / worker_id
            if configured_root.resolve(strict=False) != root:
                return None
            if os.path.lexists(root):
                if root.is_symlink() or not root.is_dir() or root.resolve(strict=True).parent != canonical_parent:
                    return None
        except (OSError, RuntimeError):
            return None

        expected_state = path_map.get("state_dir", root / "state").resolve(strict=False)
        expected_workspace = path_map.get("workspace_dir", expected_state / "workspace").resolve(strict=False)
        for persisted_name, expected in (
            ("state_dir", expected_state),
            ("workspace_dir", expected_workspace),
        ):
            raw = str(worker.get(persisted_name) or "").strip()
            if not raw:
                return None
            try:
                if Path(raw).expanduser().resolve(strict=False) != expected:
                    return None
            except (OSError, RuntimeError):
                return None
        return root

    def _cleanup_workspace_gc_tombstone(
        self,
        tombstone: dict[str, object],
        *,
        claim_token: str,
        now_epoch: float,
    ) -> bool:
        worker_id = str(tombstone.get("worker_id") or "")
        audit_root = str(tombstone.get("managed_storage_root") or "").strip()
        if not worker_id:
            return False
        try:
            if audit_root:
                attested_root = self._managed_ephemeral_storage_root(tombstone)
                if attested_root is None or str(attested_root) != audit_root:
                    raise RuntimeError("managed workspace storage can no longer be attested")
                if os.path.lexists(attested_root):
                    shutil.rmtree(attested_root)
            return self.store.record_workspace_gc_cleanup(
                worker_id,
                claim_token=claim_token,
                now_epoch=now_epoch,
            )
        except Exception as exc:
            message = public_callback_message_text(str(exc)) or "workspace storage cleanup failed"
            self.store.record_workspace_gc_cleanup(
                worker_id,
                claim_token=claim_token,
                now_epoch=now_epoch,
                error=message,
            )
            logger.warning("Failed to clean expired workspace storage %s: %s", worker_id, message)
            return False

    def reap_ephemeral_workspaces_once(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        """Claim, tombstone, and clean expired one-off workspaces without path trust."""

        if not self._ephemeral_gc_enabled():
            return []
        effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = effective_now - timedelta(seconds=self._ephemeral_retention_s())
        now_epoch = effective_now.timestamp()
        claim_ttl_s = self._ephemeral_gc_claim_ttl_s()

        for pending in self.store.list_workspace_gc_cleanup_candidates(now_epoch=now_epoch, limit=50):
            cleanup_token = f"gc_{uuid.uuid4().hex}"
            claimed_cleanup = self.store.claim_workspace_gc_cleanup(
                str(pending.get("worker_id") or ""),
                claim_token=cleanup_token,
                now_epoch=now_epoch,
                claim_ttl_s=claim_ttl_s,
            )
            if claimed_cleanup is not None:
                self._cleanup_workspace_gc_tombstone(
                    claimed_cleanup,
                    claim_token=cleanup_token,
                    now_epoch=now_epoch,
                )

        candidates = self.store.list_recoverable_workspace_gc_claims(
            now_epoch=now_epoch,
            limit=50,
        )
        candidates.extend(self.store.list_ephemeral_workspace_gc_candidates(
            updated_before=cutoff.isoformat(),
            limit=50,
        ))
        reaped: list[dict[str, object]] = []
        seen: set[str] = set()
        for worker in candidates:
            worker_id = str(worker.get("worker_id") or "")
            if not worker_id or worker_id in seen:
                continue
            seen.add(worker_id)
            storage_root = self._managed_ephemeral_storage_root(worker)
            claim_token = f"gc_{uuid.uuid4().hex}"
            claimed = self.store.claim_ephemeral_workspace_gc(
                worker_id,
                updated_before=cutoff.isoformat(),
                now_epoch=now_epoch,
                claim_token=claim_token,
                claim_ttl_s=claim_ttl_s,
                managed_storage_root=str(storage_root or ""),
            )
            if claimed is None:
                continue
            try:
                info = self.runtime.terminate_worker({**claimed, "_active_run_id": ""})
                if info.pid:
                    raise RuntimeError("ephemeral workspace compute is still active")
                deleted = self.store.finalize_ephemeral_workspace_gc(
                    worker_id,
                    claim_token=claim_token,
                    updated_before=cutoff.isoformat(),
                    now_epoch=now_epoch,
                )
                if deleted is None:
                    self.store.release_ephemeral_workspace_gc_claim(worker_id, claim_token=claim_token)
                    continue
                try:
                    revoke_signed_link_refs_for_worker(worker_id)
                except Exception as revoke_exc:
                    logger.warning("Failed to revoke expired workspace links for %s: %s", worker_id, revoke_exc)
                tombstone = self.store.claim_workspace_gc_cleanup(
                    worker_id,
                    claim_token=claim_token,
                    now_epoch=now_epoch,
                    claim_ttl_s=claim_ttl_s,
                )
                if tombstone is not None:
                    self._cleanup_workspace_gc_tombstone(
                        tombstone,
                        claim_token=claim_token,
                        now_epoch=now_epoch,
                    )
                reaped.append(
                    {
                        "worker_id": worker_id,
                        "project_id": deleted.get("project_id"),
                        "tenant_id": deleted.get("tenant_id"),
                        "owner_id": deleted.get("owner_id"),
                        "workspace_kind": "ephemeral",
                    }
                )
            except Exception as exc:
                self.store.release_ephemeral_workspace_gc_claim(worker_id, claim_token=claim_token)
                logger.warning("Failed to garbage-collect ephemeral GlassHive workspace %s: %s", worker_id, exc)
        return reaped

    def _worker_idle_seconds(self, worker: dict) -> float:
        # Reconciliation and presentation bookkeeping may refresh the worker
        # row long after its last run became terminal.  The terminal run end is
        # the durable compute-idle boundary; using worker.updated_at alone can
        # postpone cleanup after every restart and strand unrelated capacity.
        raw = str(worker.get("updated_at") or "")
        last_run_id = str(worker.get("last_run_id") or "").strip()
        if last_run_id:
            last_run = self.store.get_run(last_run_id)
            if (
                last_run
                and str(last_run.get("state") or "") in TERMINAL_RUN_STATES
                and str(last_run.get("ended_at") or "").strip()
            ):
                raw = str(last_run["ended_at"])
        try:
            updated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return 0.0
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return max(0.0, datetime.now(timezone.utc).timestamp() - updated.astimezone(timezone.utc).timestamp())

    def reap_needs_input_workers_once(self) -> list[dict[str, object]]:
        reaped: list[dict[str, object]] = []
        for worker in self.store.list_all_workers():
            worker_id = str(worker.get("worker_id") or "")
            if (
                not worker_id
                or str(worker.get("state") or "") != "needs_input"
                or worker.get("compute_released_at")
            ):
                continue
            nonterminal = self.store.list_nonterminal_runs_for_worker(worker_id)
            needs_input_runs = [
                run
                for run in nonterminal
                if str(run.get("state") or "") == "needs_input"
            ]
            executing = [
                run
                for run in nonterminal
                if str(run.get("state") or "") in {"running", "settling", "paused"}
            ]
            if len(needs_input_runs) != 1 or executing:
                continue
            item = self._release_needs_input_compute(worker, needs_input_runs[0])
            if item:
                reaped.append(item)
        return reaped

    def reap_idle_workers_once(self) -> list[dict[str, object]]:
        reaped = self.recover_expired_compute_release_claims_once()
        reaped.extend(self.reap_needs_input_workers_once())
        threshold = self._idle_terminate_after_s()
        if threshold <= 0:
            return reaped
        terminal_states = TERMINAL_RUN_STATES
        for worker in self.store.list_all_workers():
            worker_id = str(worker.get("worker_id") or "")
            if not worker_id or worker.get("state") in {"terminating", "termination_failed", "terminated", "paused", "running", "starting"}:
                continue
            if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
                continue
            idle_seconds = self._worker_idle_seconds(worker)
            if idle_seconds < threshold:
                continue
            try:
                item = self._release_worker_compute(
                    worker,
                    idle_seconds=idle_seconds,
                )
                if item:
                    reaped.append(item)
            except Exception as exc:
                logger.warning("Failed to reap idle GlassHive worker %s: %s", worker_id, exc)
        return reaped

    def _reconcile_terminated_worker_compute(self, worker: dict) -> dict[str, object] | None:
        worker_id = str(worker.get("worker_id") or "")
        worker_state = str(worker.get("state") or "")
        if not worker_id or worker_state not in {"terminated", "failed"}:
            return None
        if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
            return None
        compute_checker = getattr(self.runtime, "worker_compute_present", None)
        compute_present = bool(compute_checker(worker)) if callable(compute_checker) else bool(self.runtime.reconcile_worker(worker).pid)
        if not compute_present:
            return None
        runtime_worker = {
            **worker,
            "_active_run_id": "",
        }
        self._invalidate_worker_processor(worker_id)
        self.store.cancel_pending_runs(
            worker_id,
            error_text="Worker terminated by operator",
            state="cancelled",
        )
        info = self.runtime.terminate_worker(runtime_worker)
        if info.pid:
            raise RuntimeError(f"Worker compute is still active after termination (pid={info.pid})")
        self._apply_runtime_info(
            worker_id,
            info,
            state=worker_state,
            last_error=str(worker.get("last_error") or ""),
            compute_released_at=worker.get("compute_released_at") or utc_now(),
        )
        event_type = "worker.terminated_compute_reconciled" if worker_state == "terminated" else "worker.failed_compute_reconciled"
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            event_type,
            f"Orphaned compute removed for a worker already marked {worker_state}.",
        )
        return {"worker_id": worker_id, "project_id": worker.get("project_id")}

    def reap_terminated_workers_once(self) -> list[dict[str, object]]:
        reaped: list[dict[str, object]] = []
        for worker in self.store.list_all_workers():
            try:
                reconciled = self._reconcile_terminated_worker_compute(worker)
                if reconciled:
                    reaped.append(reconciled)
            except Exception as exc:
                logger.warning(
                    "Failed to reconcile terminated GlassHive worker compute %s: %s",
                    str(worker.get("worker_id") or ""),
                    exc,
                )
        return reaped

    def reap_paused_workers_once(self) -> list[dict[str, object]]:
        threshold = self._paused_terminate_after_s()
        if threshold <= 0:
            return []
        reaped: list[dict[str, object]] = []
        for worker in self.store.list_all_workers():
            worker_id = str(worker.get("worker_id") or "")
            if not worker_id or worker.get("state") != "paused":
                continue
            if worker.get("compute_released_at"):
                continue
            nonterminal = self.store.list_nonterminal_runs_for_worker(worker_id)
            paused_runs = [
                run for run in nonterminal if str(run.get("state") or "") == "paused"
            ]
            disallowed = [
                run
                for run in nonterminal
                if str(run.get("state") or "")
                in {"running", "settling", "needs_input"}
            ]
            if disallowed or len(paused_runs) > 1:
                continue
            paused_target = paused_runs[0] if paused_runs else None
            idle_seconds = self._worker_idle_seconds(worker)
            if idle_seconds < threshold:
                continue
            try:
                item = self._release_worker_compute(
                    worker,
                    idle_seconds=idle_seconds,
                    kind="paused",
                    target_run_id=str((paused_target or {}).get("run_id") or ""),
                    target_started_at=str(
                        (paused_target or {}).get("started_at") or ""
                    ),
                )
                if item:
                    reaped.append(item)
            except Exception as exc:
                logger.warning("Failed to stop paused GlassHive worker compute %s: %s", worker_id, exc)
        return reaped

    def _invalidate_worker_processor(self, worker_id: str) -> None:
        with self._processors_lock:
            self._processor_generations[worker_id] = self._processor_generations.get(worker_id, 0) + 1
            self._active_processors.discard(worker_id)

    def _run_age_seconds(self, run: dict) -> float:
        raw = str(run.get("started_at") or run.get("queued_at") or "")
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            logger.warning(
                "GlassHive run %s has an unparseable timestamp for max-duration reaping; treating it as expired.",
                run.get("run_id") or "",
            )
            return float("inf")
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, datetime.now(timezone.utc).timestamp() - started.astimezone(timezone.utc).timestamp())

    def reap_expired_runs_once(self) -> list[dict[str, object]]:
        threshold = self._max_run_duration_s()
        if threshold <= 0:
            return []
        reaped: list[dict[str, object]] = []
        for run in self.store.list_runs_by_state("running"):
            run_id = str(run.get("run_id") or "")
            worker_id = str(run.get("worker_id") or "")
            if not run_id or not worker_id:
                continue
            age_seconds = self._run_age_seconds(run)
            if age_seconds < threshold:
                continue
            if str(run.get("liveness_mode") or "standard") == "declared_long":
                if int(run.get("meaningful_progress_sequence") or 0) <= 0:
                    attempt_started_at = str(
                        run.get("liveness_started_at")
                        or run.get("runtime_invoked_at")
                        or ""
                    )
                    try:
                        attempt_age = max(
                            0.0,
                            self._now_datetime().timestamp()
                            - self._normalized_datetime(attempt_started_at).timestamp(),
                        )
                    except ValueError:
                        attempt_age = float("inf")
                    if attempt_age < threshold:
                        continue
                else:
                    progress_at = str(run.get("meaningful_progress_at") or "")
                    try:
                        progress_age = max(
                            0.0,
                            self._now_datetime().timestamp()
                            - self._normalized_datetime(progress_at).timestamp(),
                        )
                    except ValueError:
                        progress_age = float("inf")
                    if progress_age < self._provider_no_progress_timeout_s():
                        continue
                    try:
                        self.process_provider_liveness_once()
                    except Exception:
                        logger.exception(
                            "Failed to settle stale declared-long GlassHive run %s",
                            run_id,
                        )
                    # A run that earned long-mission extension is governed only by
                    # typed progress freshness. Never convert its stale-attention
                    # path back into the ordinary max-duration cancellation path.
                    continue
            worker = self.store.get_worker(worker_id)
            if not worker:
                continue
            error_text = f"Run exceeded GLASSHIVE_MAX_RUN_DURATION_S={threshold}; compute was stopped and workspace state was preserved."
            try:
                self._invalidate_worker_processor(worker_id)
                item = self._release_worker_compute(
                    worker,
                    idle_seconds=(
                        float(threshold)
                        if age_seconds == float("inf")
                        else age_seconds
                    ),
                    kind="max_duration",
                    target_run_id=run_id,
                    target_started_at=str(run.get("started_at") or ""),
                    target_error_text=error_text,
                )
                if not item:
                    continue
                finalized = bool(item.get("target_transitioned"))
                if finalized:
                    self._emit_callback(
                        worker,
                        "run.cancelled",
                        run={**run, "state": "cancelled", "error_text": error_text},
                        message=error_text,
                    )
                reaped.append(
                    {
                        **item,
                        "run_id": run_id,
                        "run_age_seconds": (
                            threshold
                            if age_seconds == float("inf")
                            else int(age_seconds)
                        ),
                    }
                )
            except Exception as exc:
                logger.warning("Failed to stop expired GlassHive run %s for worker %s: %s", run_id, worker_id, exc)
        return reaped

    def _idle_reaper_loop(self) -> None:
        interval = self._idle_reaper_interval_s()
        while not self._shutdown_event.wait(interval):
            self.reap_terminated_workers_once()
            self.reap_ephemeral_workspaces_once()
            self.reap_idle_workers_once()
            self.reap_paused_workers_once()
            self.reap_expired_runs_once()

    def _scheduler_interval_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_SCHEDULER_INTERVAL_S", 5, min_value=1, max_value=3600)

    def _queue_status_refresh_interval_s(self) -> int:
        return _bounded_int_env(
            "GLASSHIVE_QUEUE_STATUS_REFRESH_INTERVAL_S",
            120,
            min_value=10,
            max_value=24 * 60 * 60,
        )

    def _now_datetime(self) -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalized_datetime(value: str | datetime) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _queue_callback_id(kind: str, run: dict, *, sequence: int = 0) -> str:
        material = ":".join(
            (
                str(kind),
                str(run.get("run_id") or ""),
                str(
                    run.get("queue_wait_generation")
                    or run.get("queue_wait_episode")
                    or 0
                ),
                str(sequence),
                str(run.get("queue_deadline_at") or ""),
            )
        )
        return f"cb_queue_{kind}_" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()

    def _record_queue_callback_result(
        self,
        run_id: str,
        record: dict | None,
    ) -> None:
        self.store.mark_queue_callback_state(
            run_id,
            state="enqueued" if record is not None else "unavailable",
        )

    def _emit_queue_timeout_callback(self, run: dict) -> None:
        worker = self.store.get_worker(str(run.get("worker_id") or ""))
        if not worker:
            return
        message = str(run.get("failure_user_message") or "").strip() or (
            "This work left the queue after its bounded admission wait expired. "
            "The workspace is preserved and the work can be retried."
        )
        record = self._emit_callback(
            worker,
            "run.failed",
            run=run,
            message=message,
            insert_once=True,
        )
        self._record_queue_callback_result(str(run["run_id"]), record)

    def process_queued_work_status_once(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> dict[str, int]:
        current = self._normalized_datetime(now or self._now_datetime())
        now_iso = current.isoformat()
        result = {"timedOut": 0, "refreshed": 0, "callbacksRecovered": 0}

        for candidate in self.store.list_due_queue_timeouts(
            now=now_iso,
            limit=limit,
        ):
            worker = self.store.get_worker(str(candidate.get("worker_id") or ""))
            callback_id = str(candidate.get("queue_terminal_callback_id") or "")
            terminal_message = (
                "This work left the queue after its bounded admission wait expired. "
                "The workspace is preserved and the work can be retried."
            )
            terminal_snapshot = {
                **candidate,
                "state": "failed",
                "failure_class": "queue_wait_timeout",
                "failure_retryable": 1,
                "failure_user_message": terminal_message,
            }
            callback_intent = None
            if worker:
                callback_intent = self._emit_callback(
                    worker,
                    "run.failed",
                    run=terminal_snapshot,
                    message=terminal_message,
                    callback_id=callback_id,
                    insert_once=True,
                    submit_delivery=False,
                    persist_callback=False,
                )
            expired = self.store.expire_queued_run_if_due(
                str(candidate.get("run_id") or ""),
                expected_deadline=str(candidate.get("queue_deadline_at") or ""),
                expected_generation=int(
                    candidate.get("queue_wait_generation") or 0
                ),
                expected_callback_id=callback_id,
                callback_intent=callback_intent,
                now=now_iso,
            )
            if not expired:
                continue
            result["timedOut"] += 1
            self.store.finalize_schedule_for_run(
                str(expired.get("run_id") or ""),
                state="failed",
                last_error=str(expired.get("failure_user_message") or ""),
            )
            worker_id = str(expired.get("worker_id") or "")
            if not self.store.list_nonterminal_runs_for_worker(worker_id):
                self.store.update_worker_state(
                    worker_id,
                    "ready",
                    last_error=str(expired.get("failure_user_message") or ""),
                )
            if worker:
                self._submit_persisted_callback(worker, expired.get("_callback"))

        for terminal in self.store.list_queue_timeout_callbacks_pending(
            limit=limit
        ):
            before = str(terminal.get("queue_callback_state") or "")
            self._emit_queue_timeout_callback(terminal)
            after = self.store.get_run(str(terminal.get("run_id") or "")) or {}
            if str(after.get("queue_callback_state") or "") != before:
                result["callbacksRecovered"] += 1

        refresh_interval = self._queue_status_refresh_interval_s()
        for queued in self.store.list_due_queue_status(
            now=now_iso,
            limit=limit,
        ):
            sequence = int(queued.get("queue_status_sequence") or 0) + 1
            callback_id = self._queue_callback_id(
                "refresh", queued, sequence=sequence
            )
            worker = self.store.get_worker(str(queued.get("worker_id") or ""))
            record = None
            if worker:
                record = self._emit_callback(
                    worker,
                    "run.queue_status",
                    run=queued,
                    message="This work is still queued.",
                    callback_id=callback_id,
                    insert_once=True,
                    submit_delivery=False,
                    persist_callback=False,
                )
            next_status_at = (
                current + timedelta(seconds=refresh_interval)
            ).isoformat()
            qa_refresh_race = self._consume_local_qa(
                "status_refresh_timeout_race", worker or {}, queued
            )
            if qa_refresh_race is not None:
                terminal_callback_id = str(
                    queued.get("queue_terminal_callback_id") or ""
                )
                terminal_message = (
                    "This work left the queue after its bounded admission wait expired. "
                    "The workspace is preserved and the work can be retried."
                )
                terminal_intent = None
                if worker:
                    terminal_intent = self._emit_callback(
                        worker,
                        "run.failed",
                        run={
                            **queued,
                            "state": "failed",
                            "failure_class": "queue_wait_timeout",
                            "failure_retryable": 1,
                            "failure_user_message": terminal_message,
                        },
                        message=terminal_message,
                        callback_id=terminal_callback_id,
                        insert_once=True,
                        submit_delivery=False,
                        persist_callback=False,
                    )
                timeout_won = self.store.expire_queued_run_if_due(
                    str(queued.get("run_id") or ""),
                    expected_deadline=str(queued.get("queue_deadline_at") or ""),
                    expected_generation=int(
                        queued.get("queue_wait_generation") or 0
                    ),
                    expected_callback_id=terminal_callback_id,
                    callback_intent=terminal_intent,
                    now=str(queued.get("queue_deadline_at") or now_iso),
                )
                self._record_local_qa_effect(
                    qa_refresh_race,
                    "timeout_cas_won_before_status_refresh"
                    if timeout_won is not None
                    else "timeout_cas_already_settled",
                )
            advanced = self.store.advance_queue_status_refresh(
                str(queued.get("run_id") or ""),
                expected_next_status_at=str(
                    queued.get("queue_next_status_at") or ""
                ),
                expected_generation=int(
                    queued.get("queue_wait_generation") or 0
                ),
                expected_deadline=str(queued.get("queue_deadline_at") or ""),
                callback_id=callback_id,
                callback_intent=record,
                now=now_iso,
                next_status_at=next_status_at,
            )
            if advanced is None:
                continue
            if worker:
                self._submit_persisted_callback(worker, advanced.get("_callback"))
            result["refreshed"] += 1
        return result

    def _scheduler_loop(self) -> None:
        interval = self._scheduler_interval_s()
        while not self._shutdown_event.is_set():
            self._scheduler_wake_event.clear()
            self._process_scheduler_cycle()
            if self._shutdown_event.is_set():
                return
            wait_s = self._safe_next_scheduler_wait_s(interval)
            if self._shutdown_event.is_set():
                return
            self._scheduler_wake_event.wait(wait_s)

    def _retry_base_delay_s(self, failure_class: str) -> float:
        if failure_class in {"host_worker_busy", "host_capacity"}:
            return _bounded_float_env(
                "GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S",
                _bounded_float_env("GLASSHIVE_RETRY_BASE_DELAY_S", 5.0, min_value=0.1, max_value=3600.0),
                min_value=0.1,
                max_value=3600.0,
            )
        return _bounded_float_env("GLASSHIVE_RETRY_BASE_DELAY_S", 5.0, min_value=0.1, max_value=3600.0)

    def _retry_max_delay_s(self, failure_class: str) -> float:
        if failure_class in {"host_worker_busy", "host_capacity"}:
            return _bounded_float_env(
                "GLASSHIVE_HOST_BUSY_RETRY_MAX_DELAY_S",
                15.0,
                min_value=0.1,
                max_value=60.0,
            )
        return _bounded_float_env("GLASSHIVE_RETRY_MAX_DELAY_S", 300.0, min_value=0.1, max_value=86400.0)

    def _retry_delay_s(self, failure_class: str, attempts: int) -> float:
        base = self._retry_base_delay_s(failure_class)
        max_delay = self._retry_max_delay_s(failure_class)
        exponent = min(max(0, attempts - 1), 8)
        return min(max_delay, base * (2**exponent))

    def _capacity_retry_max_attempts(self, failure_class: str = "") -> int:
        if failure_class == "host_worker_busy":
            return _bounded_int_env(
                "GLASSHIVE_HOST_BUSY_MAX_RETRY_ATTEMPTS",
                240,
                min_value=0,
                max_value=10000,
            )
        return _bounded_int_env("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", 6, min_value=0, max_value=1000)

    def _runtime_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        checker = getattr(self.runtime, "worker_capacity_error", None)
        if not callable(checker):
            return None
        error = checker(worker)
        if not error:
            return None
        if isinstance(error, RuntimeErrorBase):
            return error
        return RuntimeErrorBase(str(error))

    def _provider_account_capacity_error(self, worker: dict, run: dict) -> HostCapacityError | None:
        """Queue a second run while its selected account has a live lease.

        Expired leases and unfinished projections are recovery states, not capacity:
        the credential binder must still fail closed on those states.
        """
        if self.control_plane_store is None:
            return None
        selection = mission_provider_account_selection(worker)
        if selection is None:
            return None
        lease = other_live_provider_lease(
            self.control_plane_store, account_id=selection.account_id,
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
            run_id=str(run.get("run_id") or ""),
        )
        if not lease:
            return None
        lease_run = self.store.get_run(str(lease.get("run_id") or ""))
        if lease_run and str(lease_run.get("state") or "") in TERMINAL_RUN_STATES:
            return None
        return ProviderAccountBusyError()

    def _requeue_retryable_run_parallel(
        self,
        worker: dict,
        run: dict,
        exc: RuntimeErrorBase,
        *,
        failure_fields: dict[str, object] | None = None,
    ) -> dict | None:
        failure_fields = dict(failure_fields or {})
        if not failure_fields:
            failure_fields = classify_runtime_error(
                exc,
                runtime_name=str(worker.get("profile") or worker.get("runtime") or "worker"),
            ).as_store_fields()
        failure_class = str(failure_fields.get("failure_class") or "runtime_retryable")
        capacity_wait = failure_class in {"host_capacity", "host_worker_busy"}
        attempts = int(
            run.get("capacity_retry_count" if capacity_wait else "retry_attempts")
            or 0
        ) + 1
        max_attempts = self._capacity_retry_max_attempts()
        indefinite_wait_classes = {
            "host_capacity",
            "host_worker_busy",
            "provider_rate_limited",
            "provider_quota_exhausted",
        }
        consume_retry_budget = failure_class not in indefinite_wait_classes
        if consume_retry_budget and attempts > max_attempts:
            message = (
                "This work could not finish. You can retry it."
            )
            exhausted_fields = {
                **failure_fields,
                # Terminal state ends automatic scheduling. Keep the failure's
                # recoverability so an explicit Retry can continue after repair.
                "failure_user_message": message,
                "failure_recommended_recovery": (
                    "Resolve the reported issue, then retry this work."
                ),
                "failure_diagnostic_summary": (
                    f"Automatic retry budget exhausted after {max_attempts} attempts for {failure_class}: {str(exc)}"
                ),
            }
            failed_run = self._finalize_run_if_state(
                str(run["run_id"]),
                str(run.get("state") or "running"),
                state="failed",
                error_text=str(exc),
                **self._terminal_generation_for_run(run),
                **exhausted_fields,
            )
            if not failed_run:
                self._record_late_processor_terminal_ignored(
                    worker, run, "retry-exhaustion"
                )
                return None
            self.store.finalize_schedule_for_run(str(run["run_id"]), state="failed", last_error=message)
            self.store.update_worker_state(str(worker["worker_id"]), "ready", last_error=message)
            self.store.add_event(
                str(worker.get("project_id") or ""),
                str(worker["worker_id"]),
                str(run["run_id"]),
                "run.failed",
                message,
            )
            self._emit_callback(
                self.store.get_worker(str(worker["worker_id"])) or worker,
                "run.failed",
                run=failed_run,
                message=message,
            )
            return failed_run
        delay_s = self._retry_delay_s(failure_class, attempts)
        if capacity_wait:
            # One durable capacity episode owns one stable retry clock. The
            # bounded jitter spreads probes without creating attempt records.
            jitter_seed = int(
                hashlib.sha256(
                    (
                        f"{run.get('run_id')}:{run.get('queue_wait_generation')}:"
                        f"{attempts}:capacity-wait"
                    ).encode("utf-8")
                ).hexdigest()[:8],
                16,
            )
            jitter_ceiling = min(
                delay_s * 0.10,
                max(0.0, self._retry_max_delay_s(failure_class) - delay_s),
            )
            delay_s += jitter_ceiling * (jitter_seed / 0xFFFFFFFF)
        provider_retry_after_s = getattr(exc, "retry_after_s", None)
        if failure_class == "provider_rate_limited" and provider_retry_after_s is not None:
            authoritative_delay = max(
                0.1, min(float(provider_retry_after_s), 86_400.0)
            )
            delay_s = max(delay_s, authoritative_delay)
            # Stable per-run jitter prevents a provider reset stampede while
            # preserving Retry-After as a hard lower bound.
            jitter_seed = int(
                hashlib.sha256(
                    f"{run.get('run_id')}:{attempts}:provider-rate-limit".encode("utf-8")
                ).hexdigest()[:8],
                16,
            )
            jitter_ceiling = min(30.0, delay_s * 0.10)
            delay_s = min(
                86_400.0,
                delay_s + jitter_ceiling * (jitter_seed / 0xFFFFFFFF),
            )
        retry_after = (
            self._now_datetime() + timedelta(seconds=delay_s)
        ).isoformat()
        retry_generation = self.store.get_run_retry_generation(
            str(run["run_id"])
        )
        if retry_generation is None or str(
            retry_generation.get("expected_attempt_id") or ""
        ) != str(run.get("active_attempt_id") or ""):
            return None
        updated_run = self.store.requeue_run_for_retry(
            str(run["run_id"]),
            retry_after=retry_after,
            **retry_generation,
            error_text=str(exc),
            last_retry_class=failure_class,
            consume_retry_budget=consume_retry_budget,
            capacity_class=str(getattr(exc, "capacity_class", "") or ""),
            capacity_available=getattr(exc, "available", None),
            capacity_required=getattr(exc, "required", None),
            capacity_shortage=getattr(exc, "shortage", None),
            capacity_reservation=getattr(exc, "reservation", None),
            capacity_next_retry_at=(
                retry_after if failure_class == "host_capacity" else ""
            ),
            queue_status_refresh_interval_s=self._queue_status_refresh_interval_s(),
            **failure_fields,
        )
        if updated_run is None:
            return None
        self.store.update_worker_state(str(worker["worker_id"]), "ready", last_error="")
        if capacity_wait:
            self._release_capacity_wait_compute(
                self.store.get_worker(str(worker["worker_id"])) or worker,
                updated_run,
            )
        message = str(failure_fields.get("failure_user_message") or "").strip() or (
            "The worker is waiting for host capacity."
        )
        # One continuous admission wait has one transition identity. Blocker
        # changes update the same generation; only exact runtime invocation
        # closes it and permits a later generation.
        if bool(updated_run.get("queue_transition_emitted")):
            self._scheduler_wake_event.set()
            return updated_run
        transition_id = self._queue_callback_id("transition", updated_run)
        callback_worker = self.store.get_worker(str(worker["worker_id"])) or worker
        callback_intent = self._emit_callback(
            callback_worker,
            "run.waiting_on_capacity",
            run={**run, **updated_run, "state": "queued"},
            message=message,
            callback_id=transition_id,
            insert_once=True,
            submit_delivery=False,
            persist_callback=False,
        )
        transitioned = self.store.claim_queue_transition(
            str(run["run_id"]),
            expected_generation=int(updated_run.get("queue_wait_generation") or 0),
            expected_deadline=str(updated_run.get("queue_deadline_at") or ""),
            callback_id=transition_id,
            callback_intent=callback_intent,
            event_message=f"{message} Retrying after {retry_after}.",
            now=self._now_datetime().isoformat(),
        )
        if transitioned:
            self._submit_persisted_callback(
                callback_worker, transitioned.get("_callback")
            )
        self._scheduler_wake_event.set()
        return updated_run

    def _requeue_retryable_run(
        self,
        worker: dict,
        run: dict,
        exc: RuntimeErrorBase,
        *,
        failure_fields: dict[str, object] | None = None,
    ) -> dict | None:
        return self._requeue_retryable_run_parallel(
            worker,
            run,
            exc,
            failure_fields=failure_fields,
        )

    def _requeue_retryable_run_legacy(
        self,
        worker: dict,
        run: dict,
        exc: RuntimeErrorBase,
        *,
        failure_fields: dict[str, object] | None = None,
    ) -> dict | None:
        failure_fields = dict(failure_fields or {})
        if not failure_fields:
            failure_fields = classify_runtime_error(
                exc,
                runtime_name=str(worker.get("profile") or worker.get("runtime") or "worker"),
            ).as_store_fields()
        failure_class = str(failure_fields.get("failure_class") or "runtime_retryable")
        capacity_wait = failure_class in {"host_capacity", "host_worker_busy"}
        attempts = int(
            run.get("capacity_retry_count" if capacity_wait else "retry_attempts")
            or 0
        ) + 1
        max_attempts = self._capacity_retry_max_attempts()
        indefinite_wait_classes = {
            "host_capacity",
            "host_worker_busy",
            "provider_rate_limited",
            "provider_quota_exhausted",
        }
        consume_retry_budget = failure_class not in indefinite_wait_classes
        if consume_retry_budget and attempts > max_attempts:
            message = (
                "This work could not finish. You can retry it."
            )
            exhausted_fields = {
                **failure_fields,
                # Terminal state ends automatic scheduling. Keep the failure's
                # recoverability so an explicit Retry can continue after repair.
                "failure_user_message": message,
                "failure_recommended_recovery": (
                    "Resolve the reported issue, then retry this work."
                ),
                "failure_diagnostic_summary": (
                    f"Automatic retry budget exhausted after {max_attempts} attempts for {failure_class}: {str(exc)}"
                ),
            }
            failed_run = self._finalize_run_if_state(
                str(run["run_id"]),
                str(run.get("state") or "running"),
                state="failed",
                error_text=str(exc),
                **self._terminal_generation_for_run(run),
                **exhausted_fields,
            )
            if not failed_run:
                self._record_late_processor_terminal_ignored(
                    worker, run, "retry-exhaustion"
                )
                return None
            self.store.finalize_schedule_for_run(str(run["run_id"]), state="failed", last_error=message)
            self.store.update_worker_state(str(worker["worker_id"]), "ready", last_error=message)
            self.store.add_event(
                str(worker.get("project_id") or ""),
                str(worker["worker_id"]),
                str(run["run_id"]),
                "run.failed",
                message,
            )
            self._emit_callback(
                self.store.get_worker(str(worker["worker_id"])) or worker,
                "run.failed",
                run=failed_run,
                message=message,
            )
            return failed_run
        delay_s = self._retry_delay_s(failure_class, attempts)
        if capacity_wait:
            # One durable capacity episode owns one stable retry clock. The
            # bounded jitter spreads probes without creating attempt records.
            jitter_seed = int(
                hashlib.sha256(
                    (
                        f"{run.get('run_id')}:{run.get('queue_wait_generation')}:"
                        f"{attempts}:capacity-wait"
                    ).encode("utf-8")
                ).hexdigest()[:8],
                16,
            )
            jitter_ceiling = min(
                delay_s * 0.10,
                max(0.0, self._retry_max_delay_s(failure_class) - delay_s),
            )
            delay_s += jitter_ceiling * (jitter_seed / 0xFFFFFFFF)
        provider_retry_after_s = getattr(exc, "retry_after_s", None)
        if failure_class == "provider_rate_limited" and provider_retry_after_s is not None:
            authoritative_delay = max(
                0.1, min(float(provider_retry_after_s), 86_400.0)
            )
            delay_s = max(delay_s, authoritative_delay)
            # Stable per-run jitter prevents a provider reset stampede while
            # preserving Retry-After as a hard lower bound.
            jitter_seed = int(
                hashlib.sha256(
                    f"{run.get('run_id')}:{attempts}:provider-rate-limit".encode("utf-8")
                ).hexdigest()[:8],
                16,
            )
            jitter_ceiling = min(30.0, delay_s * 0.10)
            delay_s = min(
                86_400.0,
                delay_s + jitter_ceiling * (jitter_seed / 0xFFFFFFFF),
            )
        retry_after = (
            self._now_datetime() + timedelta(seconds=delay_s)
        ).isoformat()
        retry_generation = self.store.get_run_retry_generation(
            str(run["run_id"])
        )
        if retry_generation is None or str(
            retry_generation.get("expected_attempt_id") or ""
        ) != str(run.get("active_attempt_id") or ""):
            return None
        updated_run = self.store.requeue_run_for_retry(
            str(run["run_id"]),
            retry_after=retry_after,
            **retry_generation,
            error_text=str(exc),
            last_retry_class=failure_class,
            consume_retry_budget=consume_retry_budget,
            capacity_class=str(getattr(exc, "capacity_class", "") or ""),
            capacity_available=getattr(exc, "available", None),
            capacity_required=getattr(exc, "required", None),
            capacity_shortage=getattr(exc, "shortage", None),
            capacity_reservation=getattr(exc, "reservation", None),
            capacity_next_retry_at=(
                retry_after if failure_class == "host_capacity" else ""
            ),
            queue_status_refresh_interval_s=self._queue_status_refresh_interval_s(),
            **failure_fields,
        )
        if updated_run is None:
            return None
        self.store.update_worker_state(str(worker["worker_id"]), "ready", last_error="")
        if capacity_wait:
            self._release_capacity_wait_compute(
                self.store.get_worker(str(worker["worker_id"])) or worker,
                updated_run,
            )
        message = str(failure_fields.get("failure_user_message") or "").strip() or (
            "The worker is waiting for host capacity."
        )
        # One continuous admission wait has one transition identity. Blocker
        # changes update the same generation; only exact runtime invocation
        # closes it and permits a later generation.
        if bool(updated_run.get("queue_transition_emitted")):
            self._scheduler_wake_event.set()
            return updated_run
        transition_id = self._queue_callback_id("transition", updated_run)
        callback_worker = self.store.get_worker(str(worker["worker_id"])) or worker
        callback_intent = self._emit_callback(
            callback_worker,
            "run.waiting_on_capacity",
            run={**run, **updated_run, "state": "queued"},
            message=message,
            callback_id=transition_id,
            insert_once=True,
            submit_delivery=False,
            persist_callback=False,
        )
        transitioned = self.store.claim_queue_transition(
            str(run["run_id"]),
            expected_generation=int(updated_run.get("queue_wait_generation") or 0),
            expected_deadline=str(updated_run.get("queue_deadline_at") or ""),
            callback_id=transition_id,
            callback_intent=callback_intent,
            event_message=f"{message} Retrying after {retry_after}.",
            now=self._now_datetime().isoformat(),
        )
        if transitioned:
            self._submit_persisted_callback(
                callback_worker, transitioned.get("_callback")
            )
        self._scheduler_wake_event.set()
        return updated_run

    def _trusted_parallel_fallback_profile(
        self, worker: dict, *, preflight: bool = True
    ) -> str:
        bundle = self._bootstrap_bundle_for(worker) or {}
        authority = bundle.get("viventium_launch_authority")
        execution_mode = str(worker.get("execution_mode") or "docker")
        policy_ready = (
            str(bundle.get("execution_policy") or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            if execution_mode == "docker"
            else self._has_native_delegation_authority(worker)
        )
        if (
            not policy_ready
            or not isinstance(authority, dict)
            or authority.get("version") != 1
            or authority.get("kind")
            not in {
                "conversation_orchestrator",
                PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_KIND,
            }
            or authority.get("execution_mode") != execution_mode
        ):
            return ""
        fallback_profile = str(authority.get("fallback_worker_profile") or "").strip()
        if not fallback_profile or fallback_profile == str(worker.get("profile") or "").strip():
            return ""
        self._ensure_profile_allowed(fallback_profile)
        if not preflight:
            return fallback_profile
        self._reserved_runtime_preflight(
            fallback_profile,
            execution_mode,
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
            lane=self._trusted_run_lane(worker),
            worker={**worker, "profile": fallback_profile, "execution_mode": execution_mode},
        )
        return fallback_profile

    def _provider_health_default_cooldown_s(self) -> float:
        return _bounded_float_env(
            "GLASSHIVE_PROVIDER_HEALTH_DEFAULT_COOLDOWN_S",
            300.0,
            min_value=1.0,
            max_value=86_400.0,
        )

    def _provider_route(self, worker: dict) -> dict[str, str]:
        profile = str(worker.get("profile") or "").strip()
        execution_mode = str(worker.get("execution_mode") or "docker").strip()
        return {
            "tenant_id": str(worker.get("tenant_id") or "local").strip() or "local",
            "owner_id": str(worker.get("owner_id") or "").strip(),
            "profile": profile,
            "runtime": (
                self._initial_runtime_label(profile, execution_mode)
                or str(worker.get("runtime") or "").strip()
            ),
            "model": str(worker.get("model") or "").strip(),
        }

    @staticmethod
    def _provider_exact_retry_at(source: object) -> str:
        for name in ("retry_at", "reset_at", "provider_retry_at", "provider_reset_at"):
            value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
            if value in (None, ""):
                continue
            if isinstance(value, datetime):
                parsed = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
            else:
                try:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except (TypeError, ValueError, OSError):
                    continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        return ""

    @staticmethod
    def _provider_retry_after_s(source: object) -> float | None:
        names = ("retry_after_s", "provider_retry_after_s")
        for name in names:
            value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0.0, float(value))
        return None

    def _record_provider_route_failure(
        self,
        worker: dict,
        run: dict,
        failure_fields: dict[str, object],
        source: object,
    ) -> dict | None:
        route = self._provider_route(worker)
        runtime_family = str(route.get("runtime") or "").strip()
        if runtime_family not in {"codex-cli", "claude-code", "grok-build", "openclaw"}:
            return None
        if not all(route.values()):
            return None
        consumer = getattr(
            self.runtime,
            "consume_provider_route_failure_evidence",
            None,
        )
        if not callable(consumer):
            return None
        evidence = consumer(worker, run, source)
        if not isinstance(evidence, dict):
            return None
        evidence_source = str(evidence.get("evidence_kind") or "").strip()
        evidence_failure_class = str(evidence.get("failure_class") or "").strip()
        if (
            int(evidence.get("version") or 0) != 1
            or evidence.get("failure_structured") is not True
            or evidence_failure_class
            not in {"provider_quota_exhausted", "provider_rate_limited"}
            or evidence_failure_class
            != str(failure_fields.get("failure_class") or "").strip()
            or not bool(failure_fields.get("failure_structured"))
            or not evidence_source
        ):
            return None
        attempt_id = str(run.get("active_attempt_id") or "").strip()
        explicit_evidence_id = str(evidence.get("evidence_id") or "").strip()
        if attempt_id:
            evidence_id = f"run_attempt:{attempt_id}"
        elif explicit_evidence_id:
            evidence_id = f"provider_evidence:{explicit_evidence_id}"
        else:
            run_id = str(run.get("run_id") or "").strip()
            evidence_material = json.dumps(
                {
                    "run_id": run_id,
                    "runtime": runtime_family,
                    "evidence_kind": evidence_source,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            evidence_id = "run_evidence:" + hashlib.sha256(
                evidence_material.encode("utf-8")
            ).hexdigest()
        return self.store.record_provider_route_failure(
            **route,
            failure_class=evidence_failure_class,
            failure_structured=True,
            retry_at=self._provider_exact_retry_at(evidence),
            retry_after_s=self._provider_retry_after_s(evidence),
            default_cooldown_s=self._provider_health_default_cooldown_s(),
            run_id=str(run.get("run_id") or ""),
            evidence_id=evidence_id,
            attempt_id=attempt_id,
            evidence_kind=evidence_source,
        )

    def _clear_provider_route_health(self, worker: dict, run: dict) -> None:
        route = self._provider_route(worker)
        attempt_id = str(run.get("active_attempt_id") or "")
        attempt = self.store.get_run_attempt(attempt_id) if attempt_id else None
        observed_last_failed_at = str(
            (attempt or {}).get("provider_health_observed_last_failed_at") or ""
        )
        observed_generation = int(
            (attempt or {}).get("provider_health_observed_generation") or 0
        )
        if all(route.values()) and observed_last_failed_at and observed_generation > 0:
            self.store.clear_provider_route_health(
                **route,
                expected_last_failed_at=observed_last_failed_at,
                expected_generation=observed_generation,
            )

    @staticmethod
    def _configured_conversation_fallback(request: dict | None) -> dict[str, object]:
        configured = str((request or {}).get("fallback_model_id") or "").strip()
        profile, separator, model = configured.partition(":")
        eligible = bool(separator and profile.strip() and model.strip())
        return {
            "fallbackEligible": eligible,
            "fallbackProfile": profile.strip() if eligible else "",
            "fallbackModel": model.strip() if eligible else "",
        }

    def _handle_unhealthy_provider_route(self, worker: dict, run: dict) -> bool:
        route_locked = bool(int(run.get("provider_liveness_route_locked") or 0))
        route = (
            {
                "tenant_id": str(worker.get("tenant_id") or "local").strip()
                or "local",
                "owner_id": str(worker.get("owner_id") or "").strip(),
                "profile": str(run.get("provider_route_profile") or "").strip(),
                "runtime": str(run.get("provider_route_runtime") or "").strip(),
                "model": str(run.get("provider_route_model") or "").strip(),
            }
            if route_locked
            else self._provider_route(worker)
        )
        if not all(route.values()):
            return False
        qa_quota = self._consume_local_qa(
            "provider_quota_cooldown_fallback", worker, run
        )
        if qa_quota is not None:
            self.store.record_provider_route_failure(
                **route,
                failure_class="provider_quota_exhausted",
                failure_structured=True,
                retry_after_s=float(
                    qa_quota.parameters.get("cooldownSeconds") or 120
                ),
                default_cooldown_s=float(
                    qa_quota.parameters.get("cooldownSeconds") or 120
                ),
                run_id=str(run.get("run_id") or ""),
                evidence_id=qa_quota.control_ref,
                attempt_id=str(run.get("active_attempt_id") or ""),
                evidence_kind="local_qa_control",
            )
            self._record_local_qa_effect(
                qa_quota, "provider_cooldown_persisted_before_route_selection"
            )
        if not str(run.get("provider_route_decision") or ""):
            selected = self.store.update_run(
                str(run["run_id"]),
                provider_route_profile=route["profile"],
                provider_route_runtime=route["runtime"],
                provider_route_model=route["model"],
                provider_route_decision="primary_selected",
            )
            if selected:
                run.update(selected)
        health = self.store.get_provider_route_health(**route)
        attempt_id = str(run.get("active_attempt_id") or "")
        if attempt_id:
            self.store.record_run_attempt_provider_health_observation(
                run_id=str(run["run_id"]),
                attempt_id=attempt_id,
                observed_last_failed_at=str(
                    (health or {}).get("last_failed_at") or ""
                ),
                observed_generation=int(
                    (health or {}).get("failure_generation") or 0
                ),
            )
        if not health:
            return False

        cooldown_until = str(health.get("cooldown_until") or "")
        failure_class = str(health.get("failure_class") or "provider_rate_limited")
        skip_payload = {
            "profile": route["profile"],
            "runtime": route["runtime"],
            "model": route["model"],
            "failureClass": failure_class,
            "cooldownUntil": cooldown_until,
        }
        self.store.add_event(
            str(worker.get("project_id") or ""),
            str(worker["worker_id"]),
            str(run["run_id"]),
            "run.provider_route_skipped",
            "GlassHive skipped a known-unhealthy provider route before compute admission.",
            payload=skip_payload,
        )

        if not route_locked and self._trusted_run_lane(worker) == "conversation":
            request = self.store.get_provider_request_for_run(str(run["run_id"]))
            fallback = self._configured_conversation_fallback(request)
            self.store.update_run(
                str(run["run_id"]),
                retry_after=cooldown_until or None,
                provider_route_decision="skipped_unhealthy",
                provider_route_failure_class=failure_class,
                provider_route_cooldown_until=cooldown_until or None,
            )
            failed = self._finalize_run_if_state(
                str(run["run_id"]),
                "claimed",
                "failed",
                error_text="GlassHive skipped a provider route during its active cooldown.",
                **self._terminal_generation_for_run(run),
                failure_class=failure_class,
                failure_retryable=1,
                failure_structured=1,
                failure_user_message=(
                    "The selected provider route is in a known quota or rate-limit cooldown."
                ),
                failure_recommended_recovery=(
                    "GlassHive will use the configured fallback or retry after the provider reset."
                ),
                failure_diagnostic_summary=(
                    "Provider circuit was open before runtime invocation."
                ),
            )
            self.store.update_worker_state(
                str(worker["worker_id"]), "ready", last_error=""
            )
            if request and failed:
                self.store.add_provider_activity(
                    str(request["request_id"]),
                    "route-skipped",
                    "Skipped a known-unhealthy provider route before invocation.",
                    {**skip_payload, **fallback},
                )
            return True

        try:
            execution_mode = str(worker.get("execution_mode") or "docker").strip()
            bootstrap_bundle = self._bootstrap_bundle_for(worker) or {}
            fallback_profile = (
                self._trusted_parallel_fallback_profile(worker)
                if not route_locked
                else ""
            )
            fallback_model, fallback_bootstrap_bundle = (
                self._configured_parallel_worker_route(
                    fallback_profile,
                    execution_mode,
                    bootstrap_bundle,
                    fallback=True,
                    tenant_id=str(worker.get("tenant_id") or "local"),
                    owner_id=str(worker.get("owner_id") or ""),
                )
                if fallback_profile
                else ("", bootstrap_bundle)
            )
            fallback_runtime = self._initial_runtime_label(
                fallback_profile, execution_mode
            )
            fallback_route = {
                "tenant_id": route["tenant_id"],
                "owner_id": route["owner_id"],
                "profile": fallback_profile,
                "runtime": fallback_runtime,
                "model": fallback_model,
            }
            fallback_health = (
                self.store.get_provider_route_health(**fallback_route)
                if all(fallback_route.values())
                else None
            )
        except Exception:
            fallback_profile = ""
            fallback_runtime = ""
            fallback_model = ""
            fallback_health = None
        if fallback_profile and not fallback_health:
            switched = self.store.switch_worker_profile_and_requeue_run(
                worker_id=str(worker["worker_id"]),
                run_id=str(run["run_id"]),
                expected_profile=route["profile"],
                fallback_profile=fallback_profile,
                fallback_backend=self._legacy_backend_label(
                    fallback_profile, execution_mode, ""
                ),
                fallback_runtime=fallback_runtime,
                fallback_model=fallback_model,
                fallback_bootstrap_bundle=fallback_bootstrap_bundle,
                retry_after=(
                    datetime.now(timezone.utc) + timedelta(milliseconds=100)
                ).isoformat(),
                error_text="Primary provider route skipped during cooldown.",
                route_cooldown_until=cooldown_until,
                route_failure_class=failure_class,
                route_source_runtime=route["runtime"],
                route_source_model=route["model"],
                failure_class=failure_class,
                failure_retryable=1,
                failure_structured=1,
                failure_user_message=(
                    "The primary provider route is cooling down; the configured fallback was selected."
                ),
                failure_recommended_recovery="No user action is required.",
                failure_diagnostic_summary=(
                    "Provider circuit selected an explicit healthy mission fallback."
                ),
            )
            if switched:
                self.store.add_event(
                    str(worker.get("project_id") or ""),
                    str(worker["worker_id"]),
                    str(run["run_id"]),
                    "run.provider_route_switched",
                    "The durable mission switched to its configured healthy fallback route.",
                    payload={
                        "fromProfile": route["profile"],
                        "fromRuntime": route["runtime"],
                        "fromModel": route["model"],
                        "toProfile": fallback_profile,
                        "toRuntime": fallback_runtime,
                        "toModel": fallback_model,
                        "failureClass": failure_class,
                        "cooldownUntil": cooldown_until,
                    },
                )
                self._scheduler_wake_event.set()
                return True

        self._wait_for_exact_provider_route(
            worker,
            run,
            cooldown_until=cooldown_until,
            failure_class=failure_class,
        )
        return True

    def _wait_for_exact_provider_route(
        self,
        worker: dict,
        run: dict,
        *,
        cooldown_until: str,
        failure_class: str,
    ) -> dict | None:
        """Requeue one exact route without consuming retry or selecting fallback."""

        run_id = str(run.get("run_id") or "")
        retry_generation = self.store.get_run_retry_generation(run_id)
        if retry_generation is None or str(
            retry_generation.get("expected_attempt_id") or ""
        ) != str(run.get("active_attempt_id") or ""):
            return None
        updated = self.store.requeue_run_for_retry(
            run_id,
            retry_after=str(cooldown_until or ""),
            **retry_generation,
            error_text="Provider route remains in cooldown.",
            last_retry_class=str(failure_class or "provider_rate_limited"),
            consume_retry_budget=False,
            failure_class=str(failure_class or "provider_rate_limited"),
            failure_retryable=1,
            failure_structured=1,
            failure_user_message="The configured provider route is cooling down.",
            failure_recommended_recovery="Wait for the exact provider reset time.",
            failure_diagnostic_summary=(
                "The exact provider route remains pinned while its circuit is open."
            ),
        )
        if updated is not None:
            updated = self.store.update_run(
                run_id,
                provider_route_decision="waiting_primary_health",
                provider_route_failure_class=str(failure_class or ""),
                provider_route_cooldown_until=str(cooldown_until or "") or None,
            ) or updated
            self.store.update_worker_state(
                str(worker["worker_id"]), "ready", last_error=""
            )
            self._scheduler_wake_event.set()
        return updated

    def _record_unavailable_provider_fallback(
        self,
        worker: dict,
        run: dict,
        failure_fields: dict[str, object],
        *,
        reason: str,
        fallback_profile: str = "",
        fallback_health: dict | None = None,
    ) -> None:
        if self._trusted_run_lane(worker) != "mission":
            return

        failure_fields["failure_recommended_recovery"] = (
            "Restore the configured provider quota or explicitly authorize a healthy fallback provider."
            if reason == "fallback_not_authorized"
            else (
                "Restore the configured provider quota or wait until the authorized fallback provider cooldown ends."
                if reason == "fallback_in_cooldown"
                else "Restore the configured provider quota or repair the explicitly authorized fallback provider."
            )
        )
        route = self._provider_route(worker)
        if not all(route.values()):
            return

        try:
            primary_health = self.store.get_provider_route_health(**route)
            cooldown_until = str(
                (primary_health or {}).get("cooldown_until") or ""
            )
            self.store.update_run(
                str(run["run_id"]),
                provider_route_profile=route["profile"],
                provider_route_runtime=route["runtime"],
                provider_route_model=route["model"],
                provider_route_decision="fallback_unavailable",
                provider_route_failure_class=str(
                    failure_fields.get("failure_class") or "provider_quota_exhausted"
                ),
                provider_route_cooldown_until=cooldown_until or None,
            )
            self.store.add_event(
                str(worker.get("project_id") or ""),
                str(worker["worker_id"]),
                str(run["run_id"]),
                "run.provider_fallback_unavailable",
                "The configured provider failed and no authorized healthy fallback was available.",
                payload={
                    "profile": route["profile"],
                    "runtime": route["runtime"],
                    "model": route["model"],
                    "failureClass": str(
                        failure_fields.get("failure_class") or "provider_quota_exhausted"
                    ),
                    "reason": reason,
                    "cooldownUntil": cooldown_until,
                    **(
                        {"fallbackProfile": fallback_profile}
                        if fallback_profile
                        else {}
                    ),
                    **(
                        {
                            "fallbackCooldownUntil": str(
                                fallback_health.get("cooldown_until") or ""
                            )
                        }
                        if fallback_health
                        else {}
                    ),
                },
            )
        except Exception:
            logger.warning(
                "Provider fallback availability telemetry could not be persisted; preserving the primary provider failure"
            )

    def _switch_quota_exhausted_run_to_fallback(
        self,
        worker: dict,
        run: dict,
        exc: RuntimeErrorBase,
        failure_fields: dict[str, object],
    ) -> dict | None:
        if (
            str(failure_fields.get("failure_class") or "")
            != "provider_quota_exhausted"
            or not bool(failure_fields.get("failure_retryable"))
            or not bool(failure_fields.get("failure_structured"))
            or str(run.get("output_text") or "").strip()
        ):
            return None
        primary_health = self.store.get_provider_route_health(
            **self._provider_route(worker)
        )
        if (
            not primary_health
            or str(primary_health.get("last_run_id") or "")
            != str(run.get("run_id") or "")
        ):
            return None
        if bool(int(run.get("provider_liveness_route_locked") or 0)):
            return self._wait_for_exact_provider_route(
                worker,
                run,
                cooldown_until=str(primary_health.get("cooldown_until") or ""),
                failure_class=str(
                    failure_fields.get("failure_class")
                    or "provider_quota_exhausted"
                ),
            )
        try:
            fallback_profile = self._trusted_parallel_fallback_profile(worker)
        except HostCapacityError:
            # Capacity is re-evaluated by normal admission after the durable
            # route switch. It is not evidence that the authorized fallback
            # configuration is invalid.
            fallback_profile = self._trusted_parallel_fallback_profile(
                worker,
                preflight=False,
            )
        except Exception:
            # A broken optional fallback must not replace the authoritative
            # primary-provider failure or escape into the scheduler loop.
            logger.warning(
                "Configured Parallel worker fallback preflight failed; preserving the primary provider failure"
            )
            self._record_unavailable_provider_fallback(
                worker,
                run,
                failure_fields,
                reason="fallback_preflight_failed",
            )
            return None
        try:
            if not fallback_profile:
                self._record_unavailable_provider_fallback(
                    worker,
                    run,
                    failure_fields,
                    reason="fallback_not_authorized",
                )
                return None
            execution_mode = str(worker.get("execution_mode") or "docker").strip()
            fallback_model, fallback_bootstrap_bundle = (
                self._configured_parallel_worker_route(
                    fallback_profile,
                    execution_mode,
                    self._bootstrap_bundle_for(worker) or {},
                    fallback=True,
                    tenant_id=str(worker.get("tenant_id") or "local"),
                    owner_id=str(worker.get("owner_id") or ""),
                )
            )
            fallback_runtime = self._initial_runtime_label(
                fallback_profile, execution_mode
            )
            fallback_health = self.store.get_provider_route_health(
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
                profile=fallback_profile,
                runtime=fallback_runtime,
                model=fallback_model,
            )
            if fallback_health:
                self._record_unavailable_provider_fallback(
                    worker,
                    run,
                    failure_fields,
                    reason="fallback_in_cooldown",
                    fallback_profile=fallback_profile,
                    fallback_health=fallback_health,
                )
                return None
        except Exception:
            # The fallback is optional recovery.  A stale/invalid fallback
            # configuration must not replace the authoritative primary-provider
            # failure with an unrelated processor exception or expose provider
            # preflight details to the user surface.
            logger.warning(
                "Configured Parallel worker fallback is unavailable; preserving the primary provider failure"
            )
            self._record_unavailable_provider_fallback(
                worker,
                run,
                failure_fields,
                reason="fallback_preflight_failed",
            )
            return None
        current_profile = str(worker.get("profile") or "").strip()
        switched = self.store.switch_worker_profile_and_requeue_run(
            worker_id=str(worker["worker_id"]),
            run_id=str(run["run_id"]),
            expected_profile=current_profile,
            fallback_profile=fallback_profile,
            fallback_backend=self._legacy_backend_label(
                fallback_profile, execution_mode, ""
            ),
            fallback_runtime=fallback_runtime,
            fallback_model=fallback_model,
            fallback_bootstrap_bundle=fallback_bootstrap_bundle,
            retry_after=(datetime.now(timezone.utc) + timedelta(milliseconds=100)).isoformat(),
            error_text=str(exc),
            route_cooldown_until=str(
                (primary_health or {}).get("cooldown_until") or ""
            ),
            route_failure_class=str(
                failure_fields.get("failure_class") or "provider_quota_exhausted"
            ),
            route_source_runtime=self._provider_route(worker)["runtime"],
            route_source_model=self._provider_route(worker)["model"],
            **failure_fields,
        )
        if not switched:
            return None
        self.store.add_event(
            str(worker.get("project_id") or ""),
            str(worker["worker_id"]),
            str(run["run_id"]),
            "run.provider_fallback",
            "The primary worker provider quota was exhausted; the same durable mission is continuing with its configured fallback worker.",
            payload={"fromProfile": current_profile, "toProfile": fallback_profile},
        )
        self.store.add_event(
            str(worker.get("project_id") or ""),
            str(worker["worker_id"]),
            str(run["run_id"]),
            "run.provider_route_switched",
            "The durable mission switched to its configured healthy fallback route.",
            payload={
                "fromProfile": current_profile,
                "fromRuntime": str(worker.get("runtime") or ""),
                "fromModel": str(worker.get("model") or ""),
                "toProfile": fallback_profile,
                "toRuntime": self._initial_runtime_label(fallback_profile, "docker"),
                "toModel": fallback_model,
                "failureClass": str(failure_fields.get("failure_class") or ""),
                "cooldownUntil": str(
                    (primary_health or {}).get("cooldown_until") or ""
                ),
            },
        )
        self._scheduler_wake_event.set()
        return switched

    def _active_worker_states(self) -> set[str]:
        return {"created", "starting", "ready", "running", "resuming", "interrupting"}

    def _limit_env(self, name: str) -> int:
        return _bounded_int_env(name, 0, min_value=0, max_value=100000)

    def _quota_workspace_options(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        states: set[str] | None = None,
        exclude_states: set[str] | None = None,
    ) -> list[dict]:
        options: list[dict] = []
        for worker in self.store.list_worker_options(
            tenant_id=tenant_id,
            owner_id=owner_id,
            states=states,
            exclude_states=exclude_states,
            limit=5,
        ):
            options.append(
                {
                    "project_id": worker.get("project_id"),
                    "worker_id": worker.get("worker_id"),
                    "project_title": worker.get("project_title") or "",
                    "workspace_name": worker.get("name") or "",
                    "alias": worker.get("alias") or "",
                    "state": worker.get("state") or "",
                    "profile": worker.get("profile") or "",
                    "execution_mode": worker.get("execution_mode") or "",
                    "updated_at": worker.get("updated_at") or "",
                    "last_run_id": worker.get("last_run_id") or "",
                }
            )
        return options

    def _enforce_worker_limits(self, *, tenant_id: str, owner_id: str) -> None:
        active_states = self._active_worker_states()
        limits = [
            (
                "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER",
                self.store.count_workers(tenant_id=tenant_id, owner_id=owner_id, states=active_states),
                "active workers for this user",
                active_states,
                None,
            ),
            (
                "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_TENANT",
                self.store.count_workers(tenant_id=tenant_id, states=active_states),
                "active workers for this tenant",
                active_states,
                None,
            ),
            (
                "GLASSHIVE_MAX_WORKSPACES_PER_USER",
                self.store.count_workers(tenant_id=tenant_id, owner_id=owner_id, exclude_states={"terminated"}),
                "workspaces for this user",
                None,
                {"terminated"},
            ),
            (
                "GLASSHIVE_MAX_WORKSPACES_PER_TENANT",
                self.store.count_workers(tenant_id=tenant_id, exclude_states={"terminated"}),
                "workspaces for this tenant",
                None,
                {"terminated"},
            ),
        ]
        for env_name, current_count, label, option_states, option_exclude_states in limits:
            limit = self._limit_env(env_name)
            if limit and current_count >= limit:
                options = self._quota_workspace_options(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    states=option_states,
                    exclude_states=option_exclude_states,
                )
                raise GlassHiveQuotaExceededError(
                    f"GlassHive quota exceeded: {label} is limited by {env_name}={limit}",
                    env_name=env_name,
                    label=label,
                    limit=limit,
                    current_count=current_count,
                    available_workspace_options=options,
                )

    def _parse_run_at(
        self,
        *,
        run_at: str | None = None,
        schedule_text: str | None = None,
        delay_seconds: int | None = None,
    ) -> str:
        if delay_seconds is not None:
            return (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(delay_seconds)))).isoformat()
        raw_run_at = str(run_at or "").strip()
        if raw_run_at:
            normalized = raw_run_at.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise ValueError("run_at must be an ISO datetime") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()

        text = str(schedule_text or "").strip().lower()
        now = datetime.now(timezone.utc)
        match = re.search(r"\bin\s+(\d+)\s*(second|seconds|minute|minutes|hour|hours|day|days)\b", text)
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            if unit.startswith("second"):
                delta = timedelta(seconds=value)
            elif unit.startswith("minute"):
                delta = timedelta(minutes=value)
            elif unit.startswith("hour"):
                delta = timedelta(hours=value)
            else:
                delta = timedelta(days=value)
            return (now + delta).isoformat()

        weekdays = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        for label, weekday in weekdays.items():
            if re.search(rf"\b{label}s?\b", text):
                days = (weekday - now.weekday()) % 7
                if days == 0:
                    days = 7
                return (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()

        raise ValueError("schedule_text must be explicit, for example 'in 20 minutes', or run_at must be provided")

    def create_project(
        self,
        owner_id: str,
        title: str,
        goal: str,
        default_worker_profile: str,
        tenant_id: str = "local",
        project_id: str | None = None,
        origin_scope: dict | None = None,
    ) -> dict:
        return self.store.create_project(
            owner_id,
            title,
            goal,
            default_worker_profile,
            tenant_id=tenant_id,
            project_id=project_id,
            origin_scope=origin_scope,
        )

    def reserve_delegation(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        request_digest: str,
        origin_ref: str,
        title: str,
        goal: str,
        instruction: str,
        origin_surface: str,
        origin_scope: dict | None = None,
        worker_name: str,
        worker_role: str,
        profile: str,
        execution_mode: str,
        resource_class: str = "standard",
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
        file_upload_ids: list[str] | None = None,
        trusted_coordinator_model: str | None = None,
        start_run: bool = True,
        emit_callback: bool = True,
    ) -> dict:
        """Atomically reserve a durable project, worker, and first run."""

        clean_resource_class = normalize_worker_resource_class(resource_class)
        resource_memory_bytes = _configured_worker_resource_memory_bytes(
            clean_resource_class
        )

        committed = self.store.get_delegation_by_idempotency_key(
            tenant_id=tenant_id,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
        )
        if committed is not None:
            if str(committed.get("request_digest") or "") != request_digest:
                raise DelegationIdempotencyConflictError(
                    "The delegation idempotency key was reused with a different request."
                )
            # The first transaction already durably accepted this exact request.
            # A lost-response replay must not be invalidated by a later profile,
            # runtime, policy, or capacity change. The scheduler owns recovery of
            # any accepted queued work after a crash.
            return {**committed, "idempotent_replay": True}

        if file_upload_ids and bootstrap_bundle and bootstrap_bundle.get("files"):
            raise ValueError("Choose stored file IDs or bootstrap files for a delegation")
        file_manifest = self.files.manifest(tenant_id, owner_id, file_upload_ids or [])

        prospective_worker = {
            "tenant_id": tenant_id,
            "owner_id": owner_id,
            "profile": profile,
            "runtime": self._initial_runtime_label(profile, execution_mode),
            "execution_mode": execution_mode,
            "trusted_run_lane": "mission",
            "resource_class": clean_resource_class,
            "resource_memory_bytes": resource_memory_bytes,
        }

        launch_authority = (
            bootstrap_bundle.get("viventium_launch_authority")
            if isinstance(bootstrap_bundle, dict)
            else None
        )
        if launch_authority is not None:
            launch_authority_keys = (
                set(launch_authority) if isinstance(launch_authority, dict) else set()
            )
            valid_launch_authority = (
                isinstance(launch_authority, dict)
                and {"version", "kind", "execution_mode"} <= launch_authority_keys
                and launch_authority_keys <= {
                    "version", "kind", "execution_mode", "fallback_worker_profile",
                    "worker_model", "worker_reasoning_effort",
                    "fallback_worker_model", "fallback_worker_reasoning_effort",
                }
                and all(
                    isinstance(launch_authority[key], str)
                    and bool(launch_authority[key].strip())
                    for key in launch_authority_keys
                    - {"version", "kind", "execution_mode"}
                )
                and (
                    not launch_authority_keys.intersection(
                        {"fallback_worker_model", "fallback_worker_reasoning_effort"}
                    )
                    or "fallback_worker_profile" in launch_authority_keys
                )
                and launch_authority.get("version") == 1
                and not isinstance(launch_authority.get("version"), bool)
                and launch_authority.get("kind") == "conversation_orchestrator"
                and launch_authority.get("execution_mode") == execution_mode
                and execution_mode in {"docker", "host"}
                and (
                    "fallback_worker_profile" not in launch_authority
                    or bool(str(launch_authority.get("fallback_worker_profile") or "").strip())
                )
            )
            capabilities = self.orchestration_capabilities()
            if (
                not valid_launch_authority
                or (
                    execution_mode == "docker"
                    and capabilities["isolatedParallelReady"] is not True
                )
                or (
                    execution_mode == "host"
                    and capabilities["nativeParallelReady"] is not True
                )
            ):
                raise ParallelExecutionIsolationError(
                    "Automatic Parallel work requires an authorized ready worker runtime."
                )
            model, bootstrap_bundle = self._configured_parallel_worker_route(
                profile, execution_mode, bootstrap_bundle,
                tenant_id=tenant_id, owner_id=owner_id,
            )
            if launch_authority.get("fallback_worker_profile"):
                self._configured_parallel_worker_route(
                    launch_authority["fallback_worker_profile"],
                    execution_mode,
                    bootstrap_bundle,
                    fallback=True,
                    tenant_id=tenant_id, owner_id=owner_id,
                )
            if execution_mode == "docker":
                bootstrap_profile, bootstrap_bundle = derive_parallel_clean_room_bootstrap(
                    bootstrap_profile,
                    bootstrap_bundle,
                )
            fallback_worker_profile = str(
                launch_authority.get("fallback_worker_profile") or ""
            ).strip()
            if fallback_worker_profile:
                self._ensure_profile_allowed(fallback_worker_profile)
                self._queued_runtime_preflight(
                    fallback_worker_profile,
                    execution_mode,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    lane="mission",
                    trusted_delegation=True,
                    worker={
                        **prospective_worker,
                        "profile": fallback_worker_profile,
                        "runtime": self._initial_runtime_label(
                            fallback_worker_profile,
                            execution_mode,
                        ),
                    },
                )
        self._ensure_execution_allowed(execution_mode)
        self._ensure_profile_allowed(profile)
        capacity_snapshot = self._queued_runtime_preflight(
            profile,
            execution_mode,
            tenant_id=tenant_id,
            owner_id=owner_id,
            lane="mission",
            worker=prospective_worker,
            require_capacity_snapshot=True,
            trusted_delegation=launch_authority is not None,
        )
        if launch_authority is None:
            model = self._resolve_worker_model(profile, execution_mode, tenant_id=tenant_id, owner_id=owner_id)
            if trusted_coordinator_model is not None:
                from .execution_profile import packaged_linux
                from .conversation_provider import GLASSHIVE_MODELS, _configured_grok_conversation_model
                configured_grok = _configured_grok_conversation_model(model) if profile == "grok-build" else None
                catalog = list(GLASSHIVE_MODELS.values()) + ([configured_grok] if configured_grok else [])
                selected = [item for item in catalog
                            if item.harness_profile == profile
                            and item.native_model == trusted_coordinator_model]
                if (not packaged_linux() or origin_surface != "coordinator"
                        or not origin_ref or execution_mode != "docker"
                        or bootstrap_profile is not None
                        or not isinstance(bootstrap_bundle, dict)
                        or bootstrap_bundle.get("provider_model") != trusted_coordinator_model
                        or len(selected) != 1):
                    raise BackgroundWorkerConfigurationError(
                        "The standalone child route is unavailable or does not match its exact model."
                    )
                model = trusted_coordinator_model
        if origin_scope and self.allowed_ai_policy is not None:
            # Validate the inherited origin ceiling before the child project is
            # created. The destination project may start with all-authorized,
            # but it must never widen a selected origin route.
            origin_worker = {
                **prospective_worker,
                "project_id": str(origin_scope.get("project_id") or ""),
                "workspace_id": str(origin_scope.get("workspace_id") or ""),
                "model": model,
                "origin_scope": dict(origin_scope),
                "bootstrap_bundle_json": json.dumps(
                    bootstrap_bundle or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            self.allowed_ai_policy.admission_snapshot(origin_worker)
        capacity_next_retry_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self._retry_base_delay_s("host_capacity"))
        ).isoformat()
        capacity_policy = self._host_capacity_policy()
        with self._worker_create_lock:
            self._enforce_worker_limits(tenant_id=tenant_id, owner_id=owner_id)
            try:
                record = self.store.reserve_delegation(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    origin_ref=origin_ref,
                    origin_scope=origin_scope,
                    title=title,
                    goal=goal,
                    instruction=instruction,
                    origin_surface=origin_surface,
                    worker_name=worker_name,
                    worker_role=worker_role,
                    profile=profile,
                    backend=self._legacy_backend_label(profile, execution_mode, ""),
                    runtime=self._initial_runtime_label(profile, execution_mode),
                    model=model,
                    execution_mode=execution_mode,
                    resource_class=clean_resource_class,
                    resource_memory_bytes=resource_memory_bytes,
                    workspace_root=workspace_root,
                    bootstrap_profile=bootstrap_profile,
                    bootstrap_bundle=bootstrap_bundle,
                    file_manifest=file_manifest,
                    require_isolated_parallel_ready=(
                        launch_authority is not None and execution_mode == "docker"
                    ),
                    queue_on_capacity=True,
                    preaccept_host_lease=None if capacity_snapshot is None else {
                        "runtime_family": self._host_runtime_family(
                            prospective_worker
                        ),
                        "lane": "mission",
                        "executor_id": self._executor_id,
                        **capacity_policy,
                        "mutation_scope": (
                            self._host_mutation_scope(
                                prospective_worker,
                                trusted_delegation=launch_authority is not None,
                            )
                            if execution_mode == "host"
                            else ""
                        ),
                        "lease_ttl_s": self._host_lease_ttl_s(),
                        "capacity_available": dict(
                            capacity_snapshot.get("available") or {}
                        ),
                        "capacity_required": dict(
                            capacity_snapshot.get("required") or {}
                        ),
                        "capacity_reservation": dict(
                            capacity_snapshot.get("reservation") or {}
                        ),
                        "capacity_observed_lease_ids": list(
                            capacity_snapshot.get("observedLeaseIds") or []
                        ),
                        "capacity_next_retry_at": capacity_next_retry_at,
                    },
                    trace_context={
                        "promptLayers": self.worker_prompt_layer_trace()
                    },
                )
            except IsolatedParallelAdmissionConflictError as exc:
                raise ParallelExecutionIsolationError(str(exc)) from exc
            except HostRunLeaseCapacityError as exc:
                error = HostCapacityError(
                    str(exc),
                    capacity_class=exc.capacity_class,
                    dimension=exc.dimension,
                    configured=exc.configured,
                    used=exc.used,
                )
                available = dict(capacity_snapshot.get("available") or {})
                required_headroom = dict(
                    capacity_snapshot.get("required") or {}
                )
                reservation = dict(
                    capacity_snapshot.get("reservation") or {}
                )
                total_required = {
                    key: max(0, int(required_headroom.get(key) or 0))
                    + max(0, int(reservation.get(key) or 0))
                    for key in (
                        "childProcesses",
                        "threads",
                        "memoryBytes",
                        "diskBytes",
                    )
                }
                error.available = dict(exc.available or available)
                error.required = dict(exc.required or total_required)
                error.shortage = dict(
                    exc.shortage
                    or {
                        key: max(
                            0,
                            int(error.required.get(key) or 0)
                            - int(error.available.get(key) or 0),
                        )
                        for key in total_required
                    }
                )
                error.reservation = dict(exc.reservation or reservation)
                error.next_retry_at = str(
                    exc.next_retry_at or capacity_next_retry_at
                )
                error.retry_after_s = self._retry_base_delay_s("host_capacity")
                raise error from exc
        if not bool(record.get("idempotent_replay")):
            worker = self.store.get_worker(str(record.get("worker_id") or ""))
            run = self.store.get_run(str(record.get("initial_run_id") or ""))
            if worker and run:
                # Reservation owns the project/worker transaction, so attach
                # its immutable policy lineage immediately after commit. A
                # replay never refreshes authority or rewrites this binding.
                try:
                    admission = self._ensure_allowed_ai(worker)
                except Exception:
                    admission = None
                if admission:
                    self._record_run_allowed_ai_admission(
                        run,
                        admission,
                        worker=worker,
                        origin_ref=origin_ref,
                    )
                if emit_callback:
                    self._emit_reserved_queue_callback(worker, run)
        if start_run:
            self.start_assigned_run(str(record.get("worker_id") or ""))
        return record

    @staticmethod
    def _reserved_queue_callback_id(run: dict) -> str:
        run_id = str(run.get("run_id") or "")
        return "cb_reserved_queue_" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()

    def _emit_reserved_queue_callback(self, worker: dict, run: dict) -> dict | None:
        return self._emit_callback(
            worker,
            "run.queued",
            run=run,
            message=str(run.get("instruction") or ""),
            callback_id=self._reserved_queue_callback_id(run),
            insert_once=True,
        )

    def recover_queued_reservations_once(self, *, limit: int = 1000) -> list[str]:
        """Wake durable queued reservations after a response or process crash."""

        recovered: list[str] = []
        for worker_id in self.store.list_due_retry_worker_ids(limit=limit):
            if self._shutdown_event.is_set():
                break
            worker = self.store.get_worker(str(worker_id))
            run = self.store.peek_next_queued_run(str(worker_id))
            if not worker or not run:
                continue
            if str(worker.get("state") or "") in {
                "paused",
                "needs_input",
                "stopping",
                "terminated",
            }:
                continue
            self._emit_reserved_queue_callback(worker, run)
            self.start_assigned_run(str(worker_id))
            recovered.append(str(worker_id))
        return recovered

    def start_reserved_delegation(self, record: dict) -> None:
        """Deliver the queued lifecycle callback and start after the account response."""

        worker = self.store.get_worker(str(record.get("worker_id") or ""))
        run = self.store.get_run(str(record.get("initial_run_id") or ""))
        if not worker or not run or str(run.get("state") or "") != "queued":
            return
        self._emit_reserved_queue_callback(worker, run)
        self.start_assigned_run(str(record.get("worker_id") or ""))

    def _apply_capability_reauthorization(
        self,
        worker: dict,
        refresh: dict[str, object],
    ) -> dict:
        """Persist only Core's safe, scope-preserving authorization horizon refresh."""

        bundle = self._bootstrap_bundle_for(worker) or {}
        authorization = bundle.get("glasshive_capability_authorization")
        invalid = RuntimeError("capability_reauthorization_invalid")
        if not isinstance(authorization, dict) or set(refresh) != {
            "version",
            "authorization_ref",
            "max_expires_at",
            "scope_fingerprint",
        }:
            raise invalid
        if isinstance(refresh.get("version"), bool) or refresh.get("version") != 1:
            raise invalid
        existing_ref = str(authorization.get("authorization_ref") or "")
        refreshed_ref = str(refresh.get("authorization_ref") or "")
        existing_scope = str(authorization.get("scope_fingerprint") or "")
        refreshed_scope = str(refresh.get("scope_fingerprint") or "")
        if (
            not existing_ref
            or not existing_scope
            or not hmac.compare_digest(existing_ref, refreshed_ref)
            or not hmac.compare_digest(existing_scope, refreshed_scope)
        ):
            raise invalid
        try:
            existing_max = datetime.fromisoformat(
                str(authorization.get("max_expires_at") or "").replace("Z", "+00:00")
            )
            refreshed_text = str(refresh.get("max_expires_at") or "")
            refreshed_max = datetime.fromisoformat(
                refreshed_text.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise invalid from exc
        if existing_max.tzinfo is None or refreshed_max.tzinfo is None:
            raise invalid
        now = datetime.now(timezone.utc)
        existing_utc = existing_max.astimezone(timezone.utc)
        refreshed_utc = refreshed_max.astimezone(timezone.utc)
        if (
            refreshed_utc <= existing_utc
            or refreshed_utc <= now + timedelta(seconds=60)
            or refreshed_utc > now + timedelta(hours=24, seconds=60)
        ):
            raise invalid
        updated_authorization = {
            **authorization,
            "max_expires_at": refreshed_text,
        }
        updated_bundle = {
            **bundle,
            "glasshive_capability_authorization": updated_authorization,
        }
        updated = self.store.update_worker(
            str(worker["worker_id"]),
            bootstrap_bundle_json=json.dumps(updated_bundle, ensure_ascii=False),
        )
        self.store.add_event(
            str(worker.get("project_id") or ""),
            str(worker["worker_id"]),
            None,
            "capability.authorization_refreshed",
            "Connected capability authorization was explicitly refreshed",
        )
        return updated or worker

    def _active_work_follow_up_authority(
        self, action_record: dict[str, object]
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        try:
            source_context = json.loads(
                str(action_record.get("source_context_json") or "{}")
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("active_work_source_context_invalid") from exc
        if not isinstance(source_context, dict):
            raise RuntimeError("active_work_source_context_invalid")
        if not source_context:
            return None, None
        output_contract = source_context.get("output_contract")
        prompt_layers = source_context.get("prompt_layers")
        if not isinstance(output_contract, dict) or not isinstance(prompt_layers, dict):
            raise RuntimeError("active_work_source_context_invalid")
        origin_trace: dict[str, object] = {
            "origin_ref": str(source_context.get("origin_ref") or ""),
            "source_event_id": str(source_context.get("source_event_id") or ""),
            "source_revision": source_context.get("source_revision"),
            "surface": str(source_context.get("surface") or ""),
            "prompt_layers": dict(prompt_layers),
        }
        return origin_trace, {
            "version": 1,
            "run_id": "",
            "source": {
                "source_event_id": origin_trace["source_event_id"],
                "source_revision": origin_trace["source_revision"],
                "surface": origin_trace["surface"],
            },
            "output": output_contract,
        }

    def execute_active_work_action(
        self,
        delegation: dict,
        *,
        action: str,
        instruction: str = "",
        idempotency_key: str,
        expected_run_id: str = "",
        start_processor: bool = True,
        capability_reauthorization: dict[str, object] | None = None,
        action_use_id: str = "",
        native_input: dict[str, object] | None = None,
    ) -> dict[str, object]:
        worker_id = str(delegation.get("worker_id") or "")
        project_id = str(delegation.get("project_id") or "")
        run_id = str(delegation.get("run_id") or delegation.get("current_run_id") or "")
        worker = self.require_worker(worker_id)
        # Re-read mission truth at execution time. The roster payload used to
        # render the action can race a completion, pause, or queued sibling.
        live_delegation = self.store.get_delegation(
            str(delegation.get("work_ref") or ""),
            tenant_id=str(delegation.get("tenant_id") or ""),
            owner_id=str(delegation.get("owner_id") or ""),
        )
        if not live_delegation:
            raise RuntimeError("active_work_not_found")
        run_id = str(live_delegation.get("run_id") or live_delegation.get("current_run_id") or run_id)
        if expected_run_id and run_id != expected_run_id:
            raise RuntimeError("active_work_generation_changed")
        if action_use_id:
            action_record = self.store.get_active_work_action(action_use_id) or {}
            bound_run_id = str(action_record.get("source_run_id") or "")
            if bound_run_id and bound_run_id != run_id:
                if native_input is not None:
                    raise RuntimeError("native_input_stale")
                recovered = self.reconcile_active_work_action(
                    live_delegation,
                    action=action,
                    instruction=instruction,
                    idempotency_key=idempotency_key,
                    source_run_id=bound_run_id,
                    capability_reauthorization=capability_reauthorization,
                    action_use_id=action_use_id,
                )
                if recovered is not None:
                    return recovered
                raise RuntimeError("active_work_generation_changed")
        else:
            action_record = {}
        run = self.require_run(run_id)
        run_state = str(run.get("state") or "")
        public_state = self._active_work_service_state(live_delegation)
        if native_input is not None:
            if action != "resume" or capability_reauthorization is not None:
                raise RuntimeError("native_input_stale")
            method = getattr(self.runtime, "respond_native_input", None)
            if not callable(method):
                raise RuntimeError("native_input_unavailable")
            try:
                result = method(
                    worker,
                    run_id=run_id,
                    request_id=str(native_input.get("request_id") or ""),
                    request_fingerprint=str(
                        native_input.get("request_fingerprint") or ""
                    ),
                    action=str(native_input.get("action") or ""),
                    content=native_input.get("content"),
                    allow_new=run_state in {"running", "paused"},
                )
            except RuntimeErrorBase as exc:
                if str(exc) == "native_input_invalid":
                    raise ValueError(
                        "Native input does not match the requested form"
                    ) from exc
                raise
            if result["status"] == "accepted":
                self.store.add_event(
                    project_id,
                    worker_id,
                    run_id,
                    "worker.native_input_answered",
                    "Owner responded to native input",
                    payload={
                        "requestId": native_input.get("request_id"),
                        "action": native_input.get("action"),
                    },
                )
            return {
                "status": result["status"],
                "state": run_state,
                "run_id": run_id,
                "confirmation_pending": False,
            }
        allowed_actions = self._active_work_service_actions(live_delegation, public_state)
        if action not in allowed_actions:
            recovered = self.reconcile_active_work_action(
                live_delegation,
                action=action,
                instruction=instruction,
                idempotency_key=idempotency_key,
                source_run_id=(
                    str(action_record.get("source_run_id") or run_id)
                    if action_use_id
                    else run_id
                ),
                capability_reauthorization=capability_reauthorization,
                action_use_id=action_use_id,
            )
            if recovered is not None:
                return recovered
            raise RuntimeError("active_work_action_not_available")

        if capability_reauthorization is not None and not (
            action == "resume" and run_state == "needs_input"
        ):
            raise RuntimeError("capability_reauthorization_invalid")

        if action in {"queue", "message", "steer"}:
            clean_instruction = str(instruction or "").strip()
            if not clean_instruction:
                raise ValueError("active_work_instruction_required")
            effect_idempotency_key = self._active_work_effect_idempotency_key(
                str(delegation.get("work_ref") or ""),
                idempotency_key,
            )
            origin_trace, continuation_contract = (
                self._active_work_follow_up_authority(action_record)
            )
            if action == "queue":
                queue_context = build_workspace_continuation_context(
                    previous_run=run,
                    continuation_goal=clean_instruction,
                )
                created = self.assign_run(
                    worker_id,
                    clean_instruction,
                    event_type="run.followup_queued",
                    idempotency_key=effect_idempotency_key,
                    resume_paused_worker=False,
                    origin_trace=origin_trace,
                    continuation_contract=continuation_contract,
                    continuation_context=queue_context,
                )
            elif action == "message":
                # Current host adapters have no proven live-message primitive.
                # Queue at the next safe run boundary and report that truthfully. A Message adds
                # guidance to the durable mission; it must not replace the original request or its
                # output/verification contract when the new run builds its constraint ledger.
                message_context = build_workspace_continuation_context(
                    previous_run=run,
                    continuation_goal=clean_instruction,
                )
                message_instruction = continuation_instruction(
                    previous_run=run,
                    continuation_context=message_context,
                )
                created = None
                created_now = False
                if run_state == "queued":
                    with self._worker_compute_release_lock(worker_id):
                        worker = self.require_worker(worker_id)
                        self._ensure_execution_allowed(worker)
                        self._queued_runtime_preflight(
                            str(worker.get("profile") or ""),
                            str(worker.get("execution_mode") or "docker"),
                            tenant_id=str(worker.get("tenant_id") or "local"),
                            owner_id=str(worker.get("owner_id") or ""),
                            lane=self._trusted_run_lane(worker),
                            worker=worker,
                        )
                        created, created_now = (
                            self.store.replace_queued_run_idempotently(
                                source_run_id=run_id,
                                replacement_run_id=self._idempotent_run_id(
                                    worker_id, effect_idempotency_key
                                ),
                                worker_id=worker_id,
                                project_id=str(worker["project_id"]),
                                instruction=message_instruction,
                                origin_trace=origin_trace,
                                continuation_contract=(
                                    {
                                        **continuation_contract,
                                        "run_id": self._idempotent_run_id(
                                            worker_id, effect_idempotency_key
                                        ),
                                    }
                                    if isinstance(continuation_contract, dict)
                                    else None
                                ),
                                continuation_context=message_context,
                            )
                        )
                    if created is not None:
                        if created_now:
                            self.store.add_event(
                                project_id,
                                worker_id,
                                run_id,
                                "run.cancelled",
                                "Queued run coalesced into message guidance",
                            )
                            self.store.add_event(
                                project_id,
                                worker_id,
                                str(created["run_id"]),
                                "worker.message_queued",
                                message_instruction,
                            )
                            self._emit_callback(
                                worker,
                                "worker.message_queued",
                                run=created,
                                message=message_instruction,
                            )
                        self._ensure_worker_processor(worker_id)
                if created is None:
                    created = self.assign_run(
                        worker_id,
                        message_instruction,
                        event_type="worker.message_queued",
                        idempotency_key=effect_idempotency_key,
                        resume_paused_worker=False,
                        origin_trace=origin_trace,
                        continuation_contract=continuation_contract,
                        continuation_context=message_context,
                    )
            else:
                created = self.steer_worker(
                    worker_id,
                    clean_instruction,
                    run_id=run_id,
                    idempotency_key=effect_idempotency_key,
                    action_use_id=action_use_id,
                    origin_trace=origin_trace,
                    continuation_contract=continuation_contract,
                )
                if str(created.get("_control_outcome") or "") == "terminal_won":
                    authoritative = dict(created.get("_control_run") or created)
                    authoritative_state = str(
                        authoritative.get("state") or "completed"
                    )
                    return {
                        "status": "accepted",
                        "state": (
                            "cancelled"
                            if authoritative_state == "interrupted"
                            else authoritative_state
                        ),
                        "run_id": str(authoritative.get("run_id") or run_id),
                        "confirmation_pending": False,
                        "control_outcome": "terminal_won",
                    }
            return {
                "status": "queued",
                "state": "queued",
                "run_id": str(created.get("run_id") or ""),
                "confirmation_pending": False,
                "delivery_mode": (
                    "queued_next_boundary" if action == "message" else "queued"
                ),
            }

        if action == "pause":
            if run_state not in {"queued", "running", "paused"}:
                raise RuntimeError("active_work_not_active")
            paused = self.pause_worker(
                worker_id, run_id=run_id, action_use_id=action_use_id
            )
            if str(paused.get("_control_outcome") or "") == "terminal_won":
                authoritative = dict(paused.get("_control_run") or {})
                authoritative_state = str(
                    authoritative.get("state") or "completed"
                )
                return {
                    "status": "accepted",
                    "state": (
                        "cancelled"
                        if authoritative_state == "interrupted"
                        else authoritative_state
                    ),
                    "run_id": str(authoritative.get("run_id") or run_id),
                    "confirmation_pending": False,
                    "control_outcome": "terminal_won",
                }
            return {
                "status": "accepted",
                "state": "paused",
                "run_id": run_id,
                "confirmation_pending": False,
                "worker": paused,
            }

        if action == "resume":
            if run_state == "needs_input":
                provider_attention = bool(
                    str(run.get("failure_class") or "")
                    == "provider_progress_stalled"
                )
                if provider_attention and not worker.get("compute_released_at"):
                    self._release_needs_input_compute(worker, run)
                    worker = self.require_worker(worker_id)
                    if not worker.get("compute_released_at"):
                        raise RuntimeError("active_work_attention_still_settling")
                if capability_reauthorization is not None:
                    worker = self._apply_capability_reauthorization(
                        worker,
                        capability_reauthorization,
                    )
                if action_use_id:
                    resumed = self.store.resume_needs_input_active_work_action(
                        action_use_id,
                        worker_id=worker_id,
                        run_id=run_id,
                        executor_id=self._executor_id,
                    )
                    if not resumed:
                        raise RuntimeError("active_work_not_waiting_for_input")
                    self._replay_pending_lifecycle_effects()
                else:
                    resumed_run = self.store.transition_run_if_state(
                        run_id,
                        "needs_input",
                        "queued",
                        ended_at=None,
                        error_text="",
                        retry_after=None,
                    )
                    if not resumed_run:
                        raise RuntimeError("active_work_not_waiting_for_input")
                    self.store.update_worker_state(worker_id, "starting", last_error="")
                    self.store.add_event(
                        project_id,
                        worker_id,
                        run_id,
                        "run.resumed" if provider_attention else "run.authorization_resumed",
                        (
                            "Provider attention cleared; exact run queued for execution restart"
                            if provider_attention
                            else "Authorization attention cleared; exact run queued for re-admission"
                        ),
                    )
                    self._emit_callback(
                        worker,
                        "run.queued",
                        run=resumed_run,
                        message=(
                            "Provider attention cleared; run queued for execution restart"
                            if provider_attention
                            else "Authorization attention cleared; run queued for re-admission"
                        ),
                    )
                self._ensure_worker_processor(worker_id)
                return {
                    "status": "queued",
                    "state": "queued",
                    "run_id": run_id,
                    "confirmation_pending": False,
                    "resume_mode": (
                        "provider_restart_same_run"
                        if provider_attention
                        else "authorization_re_admission"
                    ),
                }
            if str(worker.get("state") or "") != "paused":
                raise RuntimeError("active_work_not_paused")
            resumed = self.resume_worker(
                worker_id, run_id=run_id, action_use_id=action_use_id
            )
            if str(resumed.get("_control_outcome") or "") == "terminal_won":
                authoritative = dict(resumed.get("_control_run") or {})
                authoritative_state = str(
                    authoritative.get("state") or "completed"
                )
                return {
                    "status": "accepted",
                    "state": (
                        "cancelled"
                        if authoritative_state == "interrupted"
                        else authoritative_state
                    ),
                    "run_id": str(authoritative.get("run_id") or run_id),
                    "confirmation_pending": False,
                    "control_outcome": "terminal_won",
                }
            durable_run = self.require_run(run_id)
            resumed_state = str(durable_run.get("state") or "queued")
            return {
                "status": "accepted",
                "state": resumed_state,
                "run_id": run_id,
                "confirmation_pending": False,
                "worker": resumed,
                "resume_mode": (
                    "provider_restart_same_run"
                    if resumed_state == "queued" and bool(run.get("started_at"))
                    else "in_place"
                    if resumed_state == "running"
                    else "queued_same_run"
                ),
            }

        if action == "stop":
            if run_state in {
                "queued",
                "running",
                "settling",
                "paused",
                "needs_input",
            }:
                stopped = self.stop_run(
                    worker_id, run_id, action_use_id=action_use_id
                )
                if not bool(stopped.get("accepted")) and not bool(
                    stopped.get("confirmation_pending")
                ):
                    raise RuntimeError("active_work_stop_not_accepted")
                stopped_run = stopped.get("run") if isinstance(stopped, dict) else None
                response = {
                    "status": "pending" if stopped.get("confirmation_pending") else "accepted",
                    "state": "stopping"
                    if stopped.get("confirmation_pending")
                    else str((stopped_run or {}).get("state") or "cancelled"),
                    "run_id": run_id,
                    "confirmation_pending": bool(stopped.get("confirmation_pending")),
                }
                if str(stopped.get("work_stop_outcome") or "") == "completion_won":
                    response["control_outcome"] = "terminal_won"
                return response
            if run_state == "cancelled":
                return {
                    "status": "accepted",
                    "state": "cancelled",
                    "run_id": run_id,
                    "confirmation_pending": False,
                }
            raise RuntimeError("active_work_not_active")

        if action == "retry":
            if run_state != "failed" or not is_user_resumable_failure(
                failure_class=run.get("failure_class"),
                retryable=run.get("failure_retryable"),
                runtime_invoked_at=run.get("runtime_invoked_at", ...),
                started_at=run.get("started_at", ...),
            ):
                raise RuntimeError("active_work_not_retryable")
            if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
                raise RuntimeError("active_work_has_active_run")
            retry_guidance = str(instruction or "").strip()
            origin_trace, continuation_contract = (
                self._active_work_follow_up_authority(action_record)
            )
            retry_context = build_workspace_continuation_context(
                previous_run=run,
                continuation_goal=retry_guidance or None,
            )
            created = self.assign_run(
                worker_id,
                continuation_instruction(
                    previous_run=run,
                    continuation_context=retry_context,
                ),
                event_type="run.queued",
                idempotency_key=self._active_work_effect_idempotency_key(
                    str(delegation.get("work_ref") or ""),
                    idempotency_key,
                ),
                origin_trace=origin_trace,
                continuation_contract=continuation_contract,
                continuation_context=retry_context,
                start_processor=start_processor,
            )
            return {
                "status": "queued",
                "state": "queued",
                "run_id": str(created.get("run_id") or ""),
                "confirmation_pending": False,
            }

        if action == "dismiss":
            if run_state not in {"completed", "failed", "cancelled", "interrupted"}:
                raise RuntimeError("active_work_not_terminal")
            self.store.dismiss_delegation(
                str(delegation.get("work_ref") or ""),
                tenant_id=str(delegation.get("tenant_id") or ""),
                owner_id=str(delegation.get("owner_id") or ""),
            )
            return {
                "status": "accepted",
                "state": "cancelled" if run_state == "interrupted" else run_state,
                "run_id": run_id,
                "confirmation_pending": False,
            }

        raise ValueError("active_work_action_invalid")

    @staticmethod
    def _active_work_effect_idempotency_key(work_ref: str, idempotency_key: str) -> str:
        return f"active-work:{str(work_ref or '').strip()}:{str(idempotency_key or '').strip()}"

    @staticmethod
    def _idempotent_run_id(worker_id: str, idempotency_key: str) -> str:
        return "run_idem_" + hashlib.sha256(
            f"{worker_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]

    def active_work_effect_run_id(
        self,
        delegation: dict,
        *,
        idempotency_key: str,
    ) -> str:
        return self._idempotent_run_id(
            str(delegation.get("worker_id") or ""),
            self._active_work_effect_idempotency_key(
                str(delegation.get("work_ref") or ""),
                idempotency_key,
            ),
        )

    def active_work_action_claim_is_pending(self, action_record: dict) -> bool:
        """Whether one receipt is still fenced by its exact bound control claim."""

        action_use_id = str(action_record.get("action_use_id") or "").strip()
        current_action = (
            self.store.get_active_work_action(action_use_id) if action_use_id else None
        ) or {}
        if (
            str(current_action.get("status") or "") != "pending"
            or str(current_action.get("executor_id") or "") != self._executor_id
        ):
            return False
        operation_id = str(
            current_action.get("lifecycle_operation_id") or ""
        ).strip()
        operation_kind = str(
            current_action.get("lifecycle_operation_kind") or ""
        ).strip()
        target_run_id = str(
            current_action.get("lifecycle_target_run_id") or ""
        ).strip()
        expected_kind = {
            "pause": "pause_run",
            "resume": "resume_run",
            "steer": "steer_run",
            "stop": "stop_run",
        }.get(str(current_action.get("action") or ""), "")
        source_run = self.store.get_run(
            str(current_action.get("source_run_id") or "")
        )
        worker = (
            self.store.get_worker(str(source_run.get("worker_id") or ""))
            if source_run
            else None
        ) or {}
        return bool(
            operation_id
            and operation_kind == expected_kind
            and target_run_id == str(current_action.get("source_run_id") or "")
            and str(worker.get("compute_release_token") or "").strip()
            and str(worker.get("compute_release_operation_id") or "")
            == operation_id
            and str(worker.get("compute_release_kind") or "") == operation_kind
            and str(worker.get("compute_release_target_run_id") or "")
            == target_run_id
        )

    def _finish_bound_steer_action(
        self,
        *,
        operation_id: str,
        target_run_id: str,
        replacement_run: dict,
    ) -> dict | None:
        """Finish only the action receipt bound to one proven Steer lifecycle."""

        action = self.store.get_pending_active_work_action_for_lifecycle(
            operation_id=operation_id,
            operation_kind="steer_run",
            target_run_id=target_run_id,
        )
        if not action:
            return None
        replacement_run_id = str(replacement_run.get("run_id") or "").strip()
        replacement_worker_id = str(replacement_run.get("worker_id") or "").strip()
        if not replacement_run_id or not replacement_worker_id:
            return None
        delegation = self.store.get_delegation(
            str(action.get("work_ref") or ""),
            tenant_id=str(action.get("tenant_id") or ""),
            owner_id=str(action.get("owner_id") or ""),
        )
        source_run = self.store.get_run(target_run_id)
        if (
            not delegation
            or not source_run
            or str(delegation.get("worker_id") or "") != replacement_worker_id
            or str(source_run.get("worker_id") or "") != replacement_worker_id
            or str(delegation.get("current_run_id") or "") != replacement_run_id
        ):
            return None
        replacement_state = str(replacement_run.get("state") or "queued")
        response: dict[str, object] = {
            "workRef": str(action.get("work_ref") or ""),
            "action": "steer",
            "status": "queued" if replacement_state == "queued" else "accepted",
            "state": replacement_state,
            "confirmationPending": False,
            "idempotentReplay": False,
            "updatedAt": str(delegation.get("updated_at") or ""),
            "deliveryMode": "queued",
        }
        return self.store.finish_active_work_action(
            str(action.get("action_use_id") or ""),
            response=response,
            current_run_id=replacement_run_id,
            executor_id=str(action.get("executor_id") or ""),
        )

    def _settle_interrupted_steer_claim(
        self,
        worker_id: str,
        target_run_id: str,
    ) -> dict | None:
        """Advance one exact replacement after its source interruption is durable."""

        clean_worker_id = str(worker_id or "").strip()
        clean_target_run_id = str(target_run_id or "").strip()
        if not clean_worker_id or not clean_target_run_id:
            return None
        with self._worker_compute_release_lock(clean_worker_id):
            worker = self.store.get_worker(clean_worker_id) or {}
            target = self.store.get_run(clean_target_run_id) or {}
            replacement_run_id = str(
                worker.get("compute_release_replacement_run_id") or ""
            ).strip()
            replacement = self.store.get_run(replacement_run_id) or {}
            if (
                str(worker.get("compute_release_kind") or "") != "steer_run"
                or str(worker.get("compute_release_scope") or "") != "run"
                or str(worker.get("compute_release_target_run_id") or "")
                != clean_target_run_id
                or not str(worker.get("compute_release_token") or "").strip()
                or str(target.get("worker_id") or "") != clean_worker_id
                or str(target.get("state") or "") != "interrupted"
                or str(target.get("started_at") or "")
                != str(worker.get("compute_release_target_started_at") or "")
                or str(replacement.get("worker_id") or "") != clean_worker_id
                or str(replacement.get("run_id") or "") != replacement_run_id
                or str(replacement.get("state") or "")
                not in ({"queued"} | TERMINAL_RUN_STATES)
            ):
                return None
            operation_id = str(
                worker.get("compute_release_operation_id")
                or worker.get("compute_release_token")
                or ""
            )
            operation = self.store.finalize_worker_steer_claim(
                clean_worker_id,
                str(worker["compute_release_token"]),
                int(worker.get("compute_release_epoch") or 0),
                target_run_id=clean_target_run_id,
                target_expected_state="running",
                replacement_run_id=replacement_run_id,
                replacement_instruction=str(replacement.get("instruction") or ""),
                runtime_fields={},
            )
        if not operation:
            return None
        self._replay_pending_lifecycle_effects()
        self._finish_bound_steer_action(
            operation_id=operation_id,
            target_run_id=clean_target_run_id,
            replacement_run=dict(operation.get("replacement_run") or replacement),
        )
        return operation

    @staticmethod
    def _active_work_service_state(record: dict) -> str:
        worker_state = str(record.get("worker_state") or "")
        run_state = str(record.get("run_state") or "")
        if worker_state == "stopping":
            return "stopping"
        if worker_state == "paused" and run_state in {
            "queued",
            "running",
            "settling",
            "paused",
        }:
            return "paused"
        if run_state == "queued" and worker_state == "created":
            return "accepted"
        if run_state == "queued" and worker_state in {"starting", "resuming"}:
            return "starting"
        if run_state == "interrupted":
            return "cancelled"
        if run_state in {
            "queued",
            "running",
            "settling",
            "paused",
            "needs_input",
            "completed",
            "failed",
            "cancelled",
        }:
            return run_state
        return "failed" if worker_state == "failed" else "queued"

    @staticmethod
    def _active_work_service_actions(record: dict, state: str) -> set[str]:
        if state in {"accepted", "queued", "starting", "running"}:
            return {"queue", "message", "steer", "pause", "stop"}
        if state == "settling":
            return {"queue", "message", "stop"}
        if state == "paused":
            return {"queue", "message", "resume", "stop"}
        if state == "needs_input":
            return {"queue", "message", "resume", "stop"}
        if state == "failed" and is_user_resumable_failure(
            failure_class=record.get("run_failure_class"),
            retryable=record.get("run_failure_retryable"),
            runtime_invoked_at=record.get("run_runtime_invoked_at", ...),
            started_at=record.get("run_started_at", ...),
        ):
            return {"retry", "queue", "message", "dismiss"}
        if state in {"completed", "failed", "cancelled"}:
            return {"queue", "message", "dismiss"}
        return set()

    def reconcile_active_work_action(
        self,
        delegation: dict,
        *,
        action: str,
        instruction: str = "",
        idempotency_key: str,
        source_run_id: str,
        capability_reauthorization: dict[str, object] | None = None,
        action_use_id: str = "",
    ) -> dict[str, object] | None:
        """Recover an action receipt from its durable effect after a lost response."""

        worker_id = str(delegation.get("worker_id") or "")
        project_id = str(delegation.get("project_id") or "")
        tenant_id = str(delegation.get("tenant_id") or "")
        source_run = self.store.get_run(str(source_run_id or ""))
        if (
            not source_run
            or str(source_run.get("worker_id") or "") != worker_id
            or str(source_run.get("project_id") or "") != project_id
            or str(source_run.get("tenant_id") or "") != tenant_id
        ):
            return None

        action_record = (
            self.store.get_active_work_action(action_use_id) if action_use_id else None
        )
        action_operation_id = str(
            (action_record or {}).get("lifecycle_operation_id") or ""
        )
        action_operation_kind = str(
            (action_record or {}).get("lifecycle_operation_kind") or ""
        )
        action_operation_target = str(
            (action_record or {}).get("lifecycle_target_run_id") or ""
        )

        def action_proves(kind: str, event_type: str) -> bool:
            return bool(
                action_operation_id
                and action_operation_kind == kind
                and action_operation_target == str(source_run["run_id"])
                and self.store.has_lifecycle_operation_event(
                    operation_id=action_operation_id,
                    operation_kind=kind,
                    event_type=event_type,
                    worker_id=worker_id,
                    run_id=str(source_run["run_id"]),
                )
            )

        worker = self.store.get_worker(worker_id) or {}
        claim_kind = str(worker.get("compute_release_kind") or "")
        claim_target = str(worker.get("compute_release_target_run_id") or "")
        if claim_kind and claim_target == str(source_run.get("run_id") or ""):
            claim_action = {
                "pause_run": "pause",
                "resume_run": "resume",
                "steer_run": "steer",
                "stop_run": "stop",
            }.get(claim_kind)
            if claim_action == action:
                raw_expiry = str(worker.get("compute_release_expires_at") or "")
                try:
                    claim_expired = bool(raw_expiry) and datetime.fromisoformat(
                        raw_expiry
                    ) <= datetime.now(timezone.utc)
                except ValueError:
                    claim_expired = False
                if claim_expired:
                    self.recover_expired_compute_release_claims_once()
                    worker = self.store.get_worker(worker_id) or {}
                    source_run = self.store.get_run(
                        str(source_run["run_id"])
                    ) or source_run
                if str(worker.get("compute_release_token") or ""):
                    return None

        if action in {"queue", "message", "steer", "retry"}:
            effect_run_id = self.active_work_effect_run_id(
                delegation,
                idempotency_key=idempotency_key,
            )
            effect_run = self.store.get_run(effect_run_id)
            if (
                not effect_run
                or str(effect_run.get("worker_id") or "") != worker_id
                or str(effect_run.get("project_id") or "") != project_id
                or str(effect_run.get("tenant_id") or "") != tenant_id
            ):
                return None
            effect_state = str(effect_run.get("state") or "queued")
            source_state = str(source_run.get("state") or "")
            if (
                action == "steer"
                and source_state in TERMINAL_RUN_STATES
                and effect_state == "cancelled"
                and str(effect_run.get("error_text") or "")
                == STEER_REPLACEMENT_SUPPRESSED_ERROR
                and action_proves("steer_run", "control.terminal_won")
            ):
                current_delegation = self.store.get_delegation(
                    str(delegation.get("work_ref") or ""),
                    tenant_id=tenant_id,
                    owner_id=str(delegation.get("owner_id") or ""),
                )
                if (
                    not current_delegation
                    or str(current_delegation.get("current_run_id") or "")
                    != str(source_run["run_id"])
                ):
                    return None
                return {
                    "status": "accepted",
                    "state": (
                        "cancelled" if source_state == "interrupted" else source_state
                    ),
                    "run_id": str(source_run["run_id"]),
                    "replacement_run_id": effect_run_id,
                    "confirmation_pending": False,
                    "control_outcome": "terminal_won",
                    "advance_current_run": False,
                }
            if action == "steer" and not (
                source_state in {"interrupted", "cancelled"}
                and action_proves("steer_run", f"run.{source_state}")
            ):
                return None
            return {
                "status": "queued" if effect_state == "queued" else "accepted",
                "state": effect_state,
                "run_id": effect_run_id,
                "confirmation_pending": False,
                "delivery_mode": (
                    "queued_next_boundary" if action == "message" else "queued"
                ),
            }

        source_state = str(source_run.get("state") or "")
        if (
            action == "pause"
            and source_state in TERMINAL_RUN_STATES
            and action_proves("pause_run", "control.terminal_won")
        ):
            return {
                "status": "accepted",
                "state": "cancelled" if source_state == "interrupted" else source_state,
                "run_id": str(source_run["run_id"]),
                "confirmation_pending": False,
                "control_outcome": "terminal_won",
                "advance_current_run": False,
            }
        if action == "pause" and source_state == "paused":
            if not action_proves("pause_run", "run.paused"):
                return None
            return {
                "status": "accepted",
                "state": "paused",
                "run_id": str(source_run["run_id"]),
                "confirmation_pending": False,
            }
        if (
            action == "resume"
            and source_state in TERMINAL_RUN_STATES
            and action_proves("resume_run", "control.terminal_won")
        ):
            return {
                "status": "accepted",
                "state": "cancelled" if source_state == "interrupted" else source_state,
                "run_id": str(source_run["run_id"]),
                "confirmation_pending": False,
                "control_outcome": "terminal_won",
                "advance_current_run": False,
            }
        if action == "resume" and source_state in {"queued", "running"}:
            authorization_re_admitted = bool(
                str((action_record or {}).get("effect_phase") or "")
                == "authorization_re_admitted"
                and action_proves("resume_run", "run.authorization_resumed")
            )
            provider_progress_re_admitted = bool(
                str((action_record or {}).get("effect_phase") or "")
                == "provider_progress_re_admitted"
                and action_proves("resume_run", "run.resumed")
            )
            if not (
                authorization_re_admitted
                or provider_progress_re_admitted
                or action_proves("resume_run", "run.resumed")
            ):
                return None
            if authorization_re_admitted or provider_progress_re_admitted:
                self._replay_pending_lifecycle_effects()
                self._ensure_worker_processor(worker_id)
                resume_mode = (
                    "provider_restart_same_run"
                    if provider_progress_re_admitted
                    else "authorization_re_admission"
                )
            elif capability_reauthorization is not None:
                resume_mode = "authorization_re_admission"
            elif source_state == "running":
                resume_mode = "in_place"
            elif bool(source_run.get("started_at")):
                resume_mode = "provider_restart_same_run"
            else:
                resume_mode = "queued_same_run"
            return {
                "status": "queued" if source_state == "queued" else "accepted",
                "state": source_state,
                "run_id": str(source_run["run_id"]),
                "confirmation_pending": False,
                "resume_mode": resume_mode,
            }
        if action == "stop":
            work_stop_outcome = str(worker.get("work_stop_outcome") or "")
            stop_settled = bool(
                action_operation_id
                and action_operation_kind == "stop_run"
                and action_operation_target == str(source_run["run_id"])
                and str(worker.get("work_stop_id") or "") == action_operation_id
                and worker.get("work_stop_settled_at")
                and work_stop_outcome in {"cancelled", "completion_won"}
                and not self.store.list_nonterminal_runs_for_worker(worker_id)
                and action_proves(
                    "stop_run",
                    "run.cancelled"
                    if work_stop_outcome == "cancelled"
                    else "work.stop_completion_won",
                )
            )
            if stop_settled:
                return {
                    "status": "accepted",
                    "state": (
                        "cancelled"
                        if work_stop_outcome == "cancelled"
                        else "cancelled"
                        if source_state == "interrupted"
                        else source_state
                    ),
                    "run_id": str(source_run["run_id"]),
                    "confirmation_pending": False,
                    "control_outcome": (
                        "terminal_won"
                        if work_stop_outcome == "completion_won"
                        else "work_stopped"
                    ),
                    "advance_current_run": False,
                }
            if (
                str(worker.get("state") or "") == "stopping"
                and str(worker.get("compute_release_kind") or "") == "stop_run"
                and str(worker.get("compute_release_scope") or "") == "work"
                and str(worker.get("compute_release_target_run_id") or "")
                == str(source_run["run_id"])
                and str(worker.get("compute_release_operation_id") or "")
                == action_operation_id
                and str(worker.get("work_stop_id") or "") == action_operation_id
            ):
                return {
                    "status": "pending",
                    "state": "stopping",
                    "run_id": str(source_run["run_id"]),
                    "confirmation_pending": True,
                }
            return None
        if action == "dismiss":
            refreshed = self.store.get_delegation(
                str(delegation.get("work_ref") or ""),
                tenant_id=tenant_id,
                owner_id=str(delegation.get("owner_id") or ""),
            )
            if refreshed and refreshed.get("dismissed_at"):
                return {
                    "status": "accepted",
                    "state": "cancelled" if source_state == "interrupted" else source_state,
                    "run_id": str(source_run["run_id"]),
                    "confirmation_pending": False,
                }
        return None

    def _initial_runtime_label(self, profile: str, execution_mode: str) -> str:
        return derive_legacy_backend_label(profile=profile, execution_mode=execution_mode, default="worker")

    def _legacy_backend_label(self, profile: str, execution_mode: str, requested_backend: str) -> str:
        runtime_label = self._initial_runtime_label(profile, execution_mode)
        return derive_legacy_backend_label(
            profile=profile,
            runtime=runtime_label,
            backend=requested_backend,
            execution_mode=execution_mode,
            default="worker",
        )

    def _validate_shared_workspace_admission(
        self,
        *,
        project_id: str,
        workspace_id: str,
        tenant_id: str,
        owner_id: str,
        profile: str,
        execution_mode: str,
        bootstrap_bundle: dict | None,
        defer_capacity_busy: bool = False,
    ) -> dict:
        """Check every owner and runtime authority before a shared member row exists."""

        workspace = self.store.get_execution_workspace(workspace_id, tenant_id, owner_id)
        if workspace is None:
            raise ParallelExecutionIsolationError(
                "Shared workspace is unavailable for this owner.",
                reason_code="shared_workspace_not_found",
            )
        if str(workspace.get("project_id") or "") != str(project_id or ""):
            raise ParallelExecutionIsolationError(
                "Shared workspace is unavailable for this project.",
                reason_code="shared_workspace_project_mismatch",
            )
        if str(workspace.get("mode") or "") != "shared":
            raise ParallelExecutionIsolationError(
                "Only an explicitly shared workspace can admit shared members.",
                reason_code="shared_workspace_required",
            )
        if str(workspace.get("execution_mode") or "") != "docker" or str(execution_mode or "") != "docker":
            raise ParallelExecutionIsolationError(
                "Shared host execution is unavailable; choose the Linux container placement.",
                reason_code="shared_host_execution_unavailable",
            )
        clean_profile = str(profile or "").strip()
        if clean_profile not in PROFILE_ACCOUNT_PROVIDERS:
            raise ParallelExecutionIsolationError(
                "This worker profile has no shared-workspace adapter.",
                reason_code="shared_profile_unavailable",
            )

        readiness = self.shared_workspace_readiness(workspace)
        if not isinstance(readiness, dict) or readiness.get("available") is not True:
            code = str((readiness or {}).get("code") or "shared_runtime_unavailable")
            if not (defer_capacity_busy and code == "shared_capacity_busy"):
                raise ParallelExecutionIsolationError(
                    f"Shared workspace admission is not available ({code}).",
                    reason_code=code,
                )

        candidate_bundle = bootstrap_bundle if isinstance(bootstrap_bundle, dict) else {}
        try:
            selection = mission_provider_account_selection({
                "profile": clean_profile,
                "bootstrap_bundle": candidate_bundle,
            })
        except RuntimeErrorBase as exc:
            raise ParallelExecutionIsolationError(
                str(exc), reason_code="shared_provider_selection_invalid"
            ) from exc
        if selection is None or not selection.account_id:
            return workspace
        if self.control_plane_store is None:
            raise ParallelExecutionIsolationError(
                "Shared provider account selection is unavailable.",
                reason_code="shared_provider_selection_unavailable",
            )
        account = self.control_plane_store.get_provider_account(
            account_id=selection.account_id,
            tenant_id=tenant_id or "local",
            owner_id=owner_id,
        )
        supported = PROFILE_ACCOUNT_PROVIDERS.get(clean_profile, set())
        if account is None:
            raise ParallelExecutionIsolationError(
                "The selected personal provider account is unavailable for this owner.",
                reason_code="shared_provider_account_unavailable",
            )
        if str(account.get("provider") or "").strip().lower() not in supported:
            raise ParallelExecutionIsolationError(
                "The selected personal provider account does not match this worker profile.",
                reason_code="shared_provider_account_mismatch",
            )
        if str(account.get("status") or "").strip().lower() != "ready":
            raise ParallelExecutionIsolationError(
                "The selected personal provider account is not ready.",
                reason_code="shared_provider_account_not_ready",
            )
        return workspace

    def create_worker(
        self,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
        profile: str,
        backend: str,
        execution_mode: str = "docker",
        resource_class: str = "standard",
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
        tenant_id: str = "local",
        start_synchronously: bool = True,
        workspace_kind: WorkspaceKind | str = "legacy",
        tags: list[str] | None = None,
        duplication_report: dict[str, object] | None = None,
        workspace_id: str | None = None,
        _trusted_run_lane: str = "mission",
    ) -> dict:
        shared_workspace = None
        if workspace_id is not None:
            shared_workspace = self._validate_shared_workspace_admission(
                project_id=project_id,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                profile=profile,
                execution_mode=execution_mode,
                bootstrap_bundle=bootstrap_bundle,
                defer_capacity_busy=True,
            )
        clean_trusted_lane = (
            "conversation" if _trusted_run_lane == "conversation" else "mission"
        )
        if clean_trusted_lane == "conversation" and start_synchronously:
            raise ParallelExecutionIsolationError(
                "A conversation worker must be durably linked before its host runtime starts."
            )
        self._ensure_execution_allowed(
            execution_mode,
            trusted_run_lane=clean_trusted_lane,
        )
        self._ensure_profile_allowed(profile)
        clean_resource_class = normalize_worker_resource_class(resource_class)
        resource_memory_bytes = _configured_worker_resource_memory_bytes(
            clean_resource_class
        )
        self._reserved_runtime_preflight(
            profile,
            execution_mode,
            tenant_id=tenant_id,
            owner_id=owner_id,
            lane=clean_trusted_lane,
            worker={
                "resource_class": clean_resource_class,
                "resource_memory_bytes": resource_memory_bytes,
                **({
                    "workspace_id": shared_workspace["workspace_id"],
                    "project_id": shared_workspace["project_id"],
                } if shared_workspace is not None else {}),
            },
        )
        bundle_model = (
            str((bootstrap_bundle or {}).get("provider_model") or "").strip()
            if isinstance(bootstrap_bundle, dict)
            else ""
        )
        model = bundle_model or self._resolve_worker_model(profile, execution_mode, tenant_id=tenant_id, owner_id=owner_id)
        if start_synchronously:
            prospective = {
                "project_id": project_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "workspace_id": (
                    str(shared_workspace.get("workspace_id") or "")
                    if shared_workspace is not None
                    else ""
                ),
                "profile": profile,
                "model": model,
                "execution_mode": execution_mode,
                "bootstrap_bundle_json": json.dumps(
                    bootstrap_bundle or {}, sort_keys=True, separators=(",", ":")
                ),
            }
            self._ensure_allowed_ai(prospective)
        with self._worker_create_lock:
            # Recheck the owner-scoped workspace, provider selection, recovery,
            # and live substrate after preflight and immediately before the
            # transactional member row is written.
            if shared_workspace is not None:
                shared_workspace = self._validate_shared_workspace_admission(
                    project_id=project_id,
                    workspace_id=workspace_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    profile=profile,
                    execution_mode=execution_mode,
                    bootstrap_bundle=bootstrap_bundle,
                )
            self._enforce_worker_limits(tenant_id=tenant_id or "local", owner_id=owner_id)
            worker = self.store.create_worker(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                name=name,
                role=role,
                profile=profile,
                backend=self._legacy_backend_label(profile, execution_mode, backend),
                runtime=self._initial_runtime_label(profile, execution_mode),
                model=model,
                execution_mode=execution_mode,
                resource_class=clean_resource_class,
                resource_memory_bytes=resource_memory_bytes,
                alias=alias,
                workspace_root=workspace_root,
                workspace_id=workspace_id,
                bootstrap_profile=bootstrap_profile,
                bootstrap_bundle=bootstrap_bundle,
                workspace_kind=normalize_workspace_kind(workspace_kind),
                tags=normalize_workspace_tags(tags),
                duplication_report=duplication_report,
            )
        if start_synchronously:
            self._ensure_allowed_ai(worker)
        if not start_synchronously:
            prepare_workspace = getattr(self.runtime, "prepare_worker_workspace", None)
            if callable(prepare_workspace):
                info = prepare_workspace(worker)
                prepared = self.store.update_worker(
                    worker["worker_id"],
                    state="paused",
                    last_error="",
                    compute_released_at=utc_now(),
                    state_dir=info.state_dir,
                    workspace_dir=info.workspace_dir,
                    session_key=info.session_key,
                    pid=None,
                )
            else:
                prepared = self.store.update_worker_state(worker["worker_id"], "paused", last_error="")
            self.store.add_event(
                project_id,
                worker["worker_id"],
                None,
                "worker.prepared",
                "Worker workspace is prepared and compute will start when a run is queued",
            )
            return prepared or self.store.get_worker(worker["worker_id"]) or worker
        starting_worker = self.store.begin_worker_compute_start(worker["worker_id"])
        if starting_worker is None:
            raise RuntimeErrorBase("Worker compute release is in progress; retry shortly")
        worker = starting_worker
        try:
            info = self._ensure_worker_ready_with_lifecycle_fence(worker)
        except Exception as exc:
            updated = self.store.update_worker(
                worker["worker_id"],
                state="failed",
                last_error=str(exc),
            )
            if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                self._reject_closed_after_runtime_activity(
                    worker["worker_id"],
                    fallback_worker=updated,
                    context="workspace creation",
                )
            self.store.add_event(project_id, worker["worker_id"], None, "worker.failed", str(exc))
            return updated or worker
        return self._finalize_worker_ready_after_start(
            worker,
            info,
            event_type="worker.ready",
            message=f"Worker ready on {info.gateway_url}",
            context="workspace creation",
            emit_callback=True,
        )

    def shared_workspace_readiness(self, workspace: dict) -> dict:
        probe = getattr(self.runtime, "shared_workspace_readiness", None)
        if callable(probe):
            return probe(workspace)
        return {"available": False, "code": "shared_runtime_unavailable"}

    def execution_workspace_members(
        self, workspace_id: str, *, tenant_id: str, owner_id: str
    ) -> dict[str, object]:
        workspace = self.store.get_execution_workspace(workspace_id, tenant_id, owner_id)
        if workspace is None:
            raise KeyError("Workspace not found")
        members = self.store.list_execution_workspace_members(workspace_id, tenant_id, owner_id)
        public_fields = (
            "worker_id", "workspace_id", "name", "role", "profile", "model", "state",
            "execution_mode", "last_run_id", "created_at", "updated_at",
        )
        public_members = []
        for member in members:
            view = {key: member.get(key) for key in public_fields}
            bundle = self._bootstrap_bundle_for(member) or {}
            raw_selection = bundle.get("provider_account")
            if isinstance(raw_selection, dict):
                selection = {
                    "policy": str(raw_selection.get("policy") or "legacy").strip().lower()
                }
                account_id = str(raw_selection.get("account_id") or "").strip()
                if account_id:
                    selection["account_id"] = account_id
                view["provider_account"] = selection
            env = bundle.get("env") if isinstance(bundle.get("env"), dict) else {}
            effort = next((str(env.get(name) or "").strip()
                           for name in ("WPR_CODEX_CLI_REASONING_EFFORT",
                                        "WPR_CLAUDE_CODE_EFFORT",
                                        "WPR_GROK_REASONING_EFFORT")
                           if str(env.get(name) or "").strip()), "")
            if effort:
                view["effort"] = effort
            public_members.append(view)
        return {
            "workspace_id": workspace["workspace_id"],
            "runtime_readiness": self.shared_workspace_readiness(workspace),
            "project_id": workspace["project_id"],
            "mode": workspace["mode"],
            "execution_mode": workspace["execution_mode"],
            "default_worker_id": workspace["default_worker_id"],
            "policy_revision": workspace["policy_revision"],
            "file_placement": workspace["file_placement"],
            "members": public_members,
        }

    def file_workspace_scope(self, worker_id: str, *, tenant_id: str, owner_id: str, connection=None) -> dict:
        worker = (dict(row) if (row := connection.execute(
            "SELECT * FROM workers WHERE worker_id=? AND tenant_id=? AND owner_id=?",
            (worker_id, tenant_id, owner_id)).fetchone()) else None) if connection is not None else self.store.get_worker(worker_id, tenant_id, owner_id)
        if worker is None:
            raise ValueError("Worker is unavailable for this owner")
        workspace = (dict(row) if (row := connection.execute(
            "SELECT * FROM execution_workspaces WHERE workspace_id=? AND tenant_id=? AND owner_id=?",
            (worker["workspace_id"], tenant_id, owner_id)).fetchone()) else None) if connection is not None else self.store.get_execution_workspace(worker["workspace_id"], tenant_id, owner_id)
        if workspace is None or workspace["project_id"] != worker["project_id"]:
            raise ValueError("Worker workspace is unavailable for this owner")
        private_member = workspace["mode"] == "shared" and workspace["file_placement"] == "member_private"
        raw = str(worker.get("workspace_dir") or "").strip()
        managed_box = workspace["mode"] == "shared" or self.storage_runtime is not None
        if managed_box:
            if connection is None:
                runtime = self.runtime._runtime_for_worker(worker)
            else:
                identity = self.store.reserve_workspace_member_identity(worker_id,
                    tenant_id=tenant_id, owner_id=owner_id, connection=connection)
                template = self.runtime._runtime_for_profile(worker["profile"], worker["execution_mode"])
                runtime = self.runtime._shared_workspace_runtimes.for_worker(worker, template,
                    workspace=workspace, identity=identity)
            root = runtime.sandbox.box.paths()["workspace_dir"]
            if raw and Path(raw) != root:
                raise RuntimeErrorBase("Persisted workspace placement differs from its declared scope")
        elif raw:
            root = Path(raw)
        else:
            runtime = self.runtime._runtime_for_worker(worker)
            if worker.get("execution_mode") == "host" and hasattr(runtime, "_host_workspace_root"):
                root = runtime._host_workspace_root(worker)
            elif hasattr(runtime, "sandbox"):
                root = runtime.sandbox.paths(worker_id)["workspace_dir"]
            else:
                raise RuntimeErrorBase("Worker workspace placement is unavailable")
        if not root.is_absolute():
            raise RuntimeErrorBase("Workspace placement must be absolute")
        return {
            "scope_id": "member:" + worker_id if private_member else workspace["workspace_id"],
            "kind": "member" if private_member else "workspace",
            "workspace_id": workspace["workspace_id"],
            "member_id": worker_id,
            "root": root,
        }

    def file_scope_access(self, worker_id: str, *, tenant_id: str, owner_id: str,
                          scope_id: str, descriptor: int, is_directory: bool) -> None:
        scope = self.file_workspace_scope(worker_id, tenant_id=tenant_id, owner_id=owner_id)
        if scope["scope_id"] != scope_id:
            raise RuntimeErrorBase("File publication scope changed")
        worker = self.store.get_worker(worker_id, tenant_id, owner_id)
        workspace = self.store.get_execution_workspace(worker["workspace_id"], tenant_id, owner_id)
        if workspace["mode"] != "shared" and self.storage_runtime is None:
            return
        from .workspace_box import WorkspaceBox
        runtime = self.runtime._runtime_for_worker(worker)
        subject = "group:20000" if workspace["file_placement"] == "common" else f"user:{runtime.sandbox.box.binding.uid}"
        WorkspaceBox._publication_acl(descriptor, subject=subject, is_directory=is_directory)

    def file_workspace_root(
        self, worker_id: str, *, tenant_id: str, owner_id: str
    ) -> Path:
        """Return only an already admitted, owner-scoped worker file root."""
        worker = self.store.get_worker(worker_id, tenant_id, owner_id)
        if worker is None:
            raise ValueError("Worker is unavailable for this owner")
        workspace_id = str(worker.get("workspace_id") or "")
        workspace = self.store.get_execution_workspace(
            workspace_id, tenant_id, owner_id
        )
        if workspace is None or workspace.get("project_id") != worker.get("project_id"):
            raise ValueError("Worker workspace is unavailable for this owner")
        raw = str(worker.get("workspace_dir") or "").strip()
        if not raw:
            raise RuntimeErrorBase("Worker workspace is not prepared yet")
        root = Path(raw).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise RuntimeErrorBase("Worker workspace is not available")
        return root.resolve()

    def activate_prepared_conversation_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        session = self.store.get_provider_session_by_worker(worker_id)
        if (
            not session
            or self._trusted_run_lane(worker) != "conversation"
            or str(session.get("tenant_id") or "")
            != str(worker.get("tenant_id") or "")
            or str(session.get("owner_id") or "")
            != str(worker.get("owner_id") or "")
        ):
            raise ParallelExecutionIsolationError(
                "The conversation worker does not have a durable provider-session binding."
            )
        self._ensure_execution_allowed(worker)
        return self._start_worker_again(
            worker,
            "worker.ready",
            "Conversation worker ready",
        )

    def find_or_create_worker(
        self,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
        profile: str,
        backend: str,
        alias: str,
        execution_mode: str = "docker",
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
        tenant_id: str = "local",
        start_synchronously: bool = True,
        resource_class: str = "standard",
        replace_bootstrap_bundle: bool = False,
        scheduled_authority_fingerprint: str = "",
        workspace_kind: WorkspaceKind | str = "legacy",
        tags: list[str] | None = None,
    ) -> dict:
        self._ensure_execution_allowed(execution_mode)
        self._ensure_profile_allowed(profile)
        clean_resource_class = normalize_worker_resource_class(resource_class)
        resource_memory_bytes = _configured_worker_resource_memory_bytes(
            clean_resource_class
        )
        existing = self.store.find_worker_by_alias(
            project_id,
            owner_id,
            alias,
            execution_mode=execution_mode,
            tenant_id=tenant_id,
        )
        reusable_existing = (
            existing if existing and existing.get("state") != "terminated" else None
        )
        if reusable_existing is not None and start_synchronously:
            effective_existing = dict(reusable_existing)
            effective_existing.update(
                {
                    "profile": profile,
                    "execution_mode": execution_mode,
                    "model": str(
                        (bootstrap_bundle or {}).get("provider_model")
                        or reusable_existing.get("model")
                        or ""
                    ).strip(),
                }
            )
            if bootstrap_bundle is not None:
                effective_existing["bootstrap_bundle_json"] = json.dumps(
                    merge_bootstrap_bundle(
                        self._bootstrap_bundle_for(reusable_existing), bootstrap_bundle
                    )
                    or {},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            self._ensure_allowed_ai(effective_existing)
        if reusable_existing:
            persisted_resource_class = normalize_worker_resource_class(
                reusable_existing.get("resource_class")
            )
            if persisted_resource_class != clean_resource_class:
                raise WorkAdmissionError(
                    "worker_resource_class_mismatch",
                    "Existing worker stored resource_class "
                    f"'{persisted_resource_class}' does not match requested "
                    f"resource_class '{clean_resource_class}'."
                )
            if scheduled_authority_fingerprint:
                expected_suffix = (
                    f"--{PROMPT_WORKBENCH_SCHEDULED_ALIAS_NAMESPACE}-"
                    f"{scheduled_authority_fingerprint[:24]}"
                )
                expected_bundle = (
                    bootstrap_bundle if isinstance(bootstrap_bundle, dict) else {}
                )
                expected_model = self._resolve_worker_model(
                    profile, execution_mode
                ).strip()
                expected_backend = self._legacy_backend_label(
                    profile, execution_mode, backend
                )
                expected_runtime = self._initial_runtime_label(
                    profile, execution_mode
                )
                expected_authority = expected_bundle.get(
                    "viventium_launch_authority"
                )
                fallback_profile = (
                    str(expected_authority.get("fallback_worker_profile") or "")
                    .strip()
                    if isinstance(expected_authority, dict)
                    else ""
                )
                fallback_model = (
                    self._resolve_worker_model(
                        fallback_profile, execution_mode
                    ).strip()
                    if fallback_profile
                    else ""
                )
                fallback_backend = (
                    self._legacy_backend_label(
                        fallback_profile, execution_mode, ""
                    )
                    if fallback_profile
                    else ""
                )
                fallback_runtime = (
                    self._initial_runtime_label(
                        fallback_profile, execution_mode
                    )
                    if fallback_profile
                    else ""
                )

                def exact_scheduled_common(
                    candidate: dict,
                    *,
                    allow_legacy_claude_default_omission: bool = False,
                ) -> bool:
                    candidate_bundle = self._bootstrap_bundle_for(candidate) or {}
                    candidate_env = candidate_bundle.get("env")
                    expected_env = expected_bundle.get("env")
                    env_matches = candidate_env == expected_env
                    if (
                        not env_matches
                        and allow_legacy_claude_default_omission
                        and fallback_profile == "claude-code"
                        and isinstance(expected_env, dict)
                        and expected_env.get("WPR_CLAUDE_CODE_EFFORT")
                        == "default"
                    ):
                        legacy_env = dict(expected_env)
                        legacy_env.pop("WPR_CLAUDE_CODE_EFFORT", None)
                        env_matches = candidate_env == legacy_env
                    return bool(
                        str(alias or "").endswith(expected_suffix)
                        and str(candidate.get("alias") or "") == str(alias)
                        and str(candidate.get("execution_mode") or "") == "docker"
                        and str(candidate.get("bootstrap_profile") or "")
                        == PARALLEL_CLEAN_ROOM_BOOTSTRAP_PROFILE
                        and candidate_bundle.get("execution_policy")
                        == expected_bundle.get("execution_policy")
                        == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                        and candidate_bundle.get("viventium_launch_authority")
                        == expected_authority
                        and env_matches
                        and "glasshive_capability_authorization"
                        not in candidate_bundle
                        and "glasshive_capability_broker" not in candidate_bundle
                    )

                def exact_route(
                    candidate: dict,
                    *,
                    route_profile: str,
                    route_backend: str,
                    route_runtime: str,
                    route_model: str,
                ) -> bool:
                    return bool(
                        route_profile
                        and str(candidate.get("profile") or "") == route_profile
                        and str(candidate.get("backend") or "") == route_backend
                        and str(candidate.get("runtime") or "") == route_runtime
                        and str(candidate.get("model") or "") == route_model
                    )

                primary_matches = exact_scheduled_common(
                    reusable_existing
                ) and exact_route(
                    reusable_existing,
                    route_profile=profile,
                    route_backend=expected_backend,
                    route_runtime=expected_runtime,
                    route_model=expected_model,
                )
                fallback_matches = exact_scheduled_common(
                    reusable_existing,
                    allow_legacy_claude_default_omission=True,
                ) and exact_route(
                    reusable_existing,
                    route_profile=fallback_profile,
                    route_backend=fallback_backend,
                    route_runtime=fallback_runtime,
                    route_model=fallback_model,
                )
                if fallback_matches:
                    with self._worker_compute_release_lock(
                        str(reusable_existing["worker_id"])
                    ):
                        locked_existing = self.store.get_worker(
                            str(reusable_existing["worker_id"])
                        )
                        locked_fallback_matches = bool(
                            locked_existing
                            and exact_scheduled_common(
                                locked_existing,
                                allow_legacy_claude_default_omission=True,
                            )
                            and exact_route(
                                locked_existing,
                                route_profile=fallback_profile,
                                route_backend=fallback_backend,
                                route_runtime=fallback_runtime,
                                route_model=fallback_model,
                            )
                            and not self.store.list_nonterminal_runs_for_worker(
                                str(reusable_existing["worker_id"])
                            )
                        )
                        if locked_fallback_matches:
                            reusable_existing = self.store.update_worker(
                                str(reusable_existing["worker_id"]),
                                profile=profile,
                                backend=expected_backend,
                                runtime=expected_runtime,
                                model=expected_model,
                                bootstrap_bundle_json=json.dumps(
                                    expected_bundle
                                ),
                            ) or locked_existing
                        else:
                            fallback_matches = False
                if not primary_matches and not fallback_matches:
                    raise WorkAdmissionError(
                        "scheduled_authority_fingerprint_mismatch",
                        "The stored Prompt Workbench worker authority does not match "
                        "its route fingerprint.",
                    )
        self._reserved_runtime_preflight(
            profile,
            execution_mode,
            tenant_id=tenant_id,
            owner_id=owner_id,
            lane="mission",
            worker=reusable_existing
            or {
                "resource_class": clean_resource_class,
                "resource_memory_bytes": resource_memory_bytes,
            },
        )
        if reusable_existing:
            existing_worker_id = str(reusable_existing.get("worker_id") or "")
            with self._runtime_start_lock(existing_worker_id):
                locked_existing = self.store.get_worker(existing_worker_id)
                if (
                    not locked_existing
                    or str(locked_existing.get("state") or "") in CLOSED_WORKER_STATES
                ):
                    reusable_existing = None
                else:
                    existing = locked_existing
                    updates: dict[str, object] = {
                        "name": name,
                        "role": role,
                        "profile": profile,
                        "backend": self._legacy_backend_label(profile, execution_mode, backend),
                        "runtime": self._initial_runtime_label(profile, execution_mode),
                    }
                    if scheduled_authority_fingerprint:
                        updates["model"] = self._resolve_worker_model(
                            profile, execution_mode
                        ).strip()
                    if workspace_root is not None:
                        updates["workspace_root"] = workspace_root
                    if bootstrap_profile is not None:
                        updates["bootstrap_profile"] = bootstrap_profile
                    if bootstrap_bundle is not None:
                        updates["bootstrap_bundle_json"] = json.dumps(
                            bootstrap_bundle
                            if replace_bootstrap_bundle
                            else merge_bootstrap_bundle(
                                self._bootstrap_bundle_for(existing), bootstrap_bundle
                            )
                        )
                    existing = self.store.update_worker(existing["worker_id"], **updates) or existing
                    self.store.add_event(
                        project_id,
                        existing["worker_id"],
                        None,
                        "worker.resumed_by_alias",
                        f"Reusing worker alias {alias}",
                    )
                    existing = self._refresh_worker_model_for_profile(existing)
                    self._emit_callback(existing, "worker.resumed_by_alias", message=f"Reusing worker alias {alias}")
                    return existing
        return self.create_worker(
            project_id=project_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            name=name,
            role=role,
            profile=profile,
            backend=backend,
            execution_mode=execution_mode,
            alias=alias,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=bootstrap_bundle,
            start_synchronously=start_synchronously,
            resource_class=clean_resource_class,
            workspace_kind=workspace_kind,
            tags=tags,
        )

    def update_worker_metadata(
        self,
        worker_id: str,
        *,
        favorite: bool | None = None,
        name: str | None = None,
        workspace_kind: WorkspaceKind | str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        worker = self.require_worker(worker_id)
        updates: dict[str, object] = {}
        if favorite is not None:
            updates["favorite"] = 1 if favorite else 0
            if favorite and workspace_kind is None and normalize_workspace_kind(
                worker.get("workspace_kind")
            ) == "ephemeral":
                updates["workspace_kind"] = "named"
        if name is not None:
            clean_name = str(name or "").strip()
            if not clean_name:
                raise ValueError("worker name cannot be empty")
            updates["name"] = clean_name[:160]
        if workspace_kind is not None:
            updates["workspace_kind"] = normalize_workspace_kind(workspace_kind)
        if tags is not None:
            updates["workspace_tags_json"] = json.dumps(
                normalize_workspace_tags(tags),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if not updates:
            return worker
        updated = self.store.update_worker_unless_gc_claimed(worker_id, **updates)
        if updated is None:
            raise RuntimeErrorBase("Workspace is being garbage-collected")
        self.store.add_event(worker["project_id"], worker_id, None, "worker.metadata_updated", "Worker metadata updated")
        return updated

    def list_workspace_catalog(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        workspace_kinds: set[str] | None = None,
        search: str = "",
        tags: list[str] | None = None,
        favorite: bool | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, object]:
        page_size = max(1, min(int(limit), 100))
        normalized_kinds = {normalize_workspace_kind(value) for value in workspace_kinds or set()}
        cursor_favorite, cursor_activity_at, cursor_worker_id = _decode_workspace_cursor(cursor)
        rows = self.store.list_workspace_catalog(
            tenant_id=tenant_id or "local",
            owner_id=owner_id,
            workspace_kinds=normalized_kinds,
            search=search,
            tags=normalize_workspace_tags(tags),
            favorite=favorite,
            cursor_favorite=cursor_favorite,
            cursor_activity_at=cursor_activity_at,
            cursor_worker_id=cursor_worker_id,
            limit=page_size + 1,
        )
        items = rows[:page_size]
        worker_ids = [str(item.get("worker_id") or "") for item in items]

        accounts: dict[str, dict] = {}
        if self.control_plane_store is not None:
            accounts = {
                str(account.get("account_id") or ""): account
                for account in self.control_plane_store.list_provider_accounts(
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
            }
        try:
            capability_readiness = (
                self.control_plane_store.workspace_capability_readiness(
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                    worker_ids=worker_ids,
                )
                if self.control_plane_store is not None
                else {
                    worker_id: {
                        "active_grants": 0,
                        "unavailable_grants": 0,
                        "readiness": "ready",
                    }
                    for worker_id in worker_ids
                }
            )
        except Exception as exc:
            logger.warning("Workspace catalog capability readiness is unavailable: %s", exc)
            capability_readiness = {
                worker_id: {
                    "active_grants": 0,
                    "unavailable_grants": 0,
                    "readiness": "unavailable",
                }
                for worker_id in worker_ids
            }

        schedule_readiness = "ready"
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            try:
                definitions = self.list_recurring_schedules(
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                    include_inactive=False,
                    limit=100,
                )
                next_schedule_by_worker: dict[str, str] = {}
                for definition in definitions:
                    schedule_worker_id = str(definition.get("worker_id") or "")
                    next_at = str(
                        definition.get("next_occurrence_at")
                        or definition.get("next_run_at")
                        or ""
                    )
                    current = next_schedule_by_worker.get(schedule_worker_id, "")
                    if next_at and (not current or next_at < current):
                        next_schedule_by_worker[schedule_worker_id] = next_at
                if len(definitions) >= 100:
                    schedule_readiness = "partial"
            except Exception as exc:
                logger.warning("Workspace catalog schedule readiness is unavailable: %s", exc)
                next_schedule_by_worker = {}
                schedule_readiness = "unavailable"
        else:
            next_schedule_by_worker = self.store.next_scheduled_occurrence_by_worker(
                tenant_id=tenant_id or "local",
                owner_id=owner_id,
                worker_ids=worker_ids,
            )

        for item in items:
            worker_id = str(item.get("worker_id") or "")
            raw_bundle = item.get("bootstrap_bundle_json")
            if isinstance(raw_bundle, str) and raw_bundle.strip():
                try:
                    bundle = json.loads(raw_bundle)
                except json.JSONDecodeError:
                    bundle = {}
            else:
                bundle = raw_bundle if isinstance(raw_bundle, dict) else {}
            selection = bundle.get("provider_account") if isinstance(bundle, dict) else None
            policy = str(selection.get("policy") or "legacy") if isinstance(selection, dict) else "legacy"
            account_id = str(selection.get("account_id") or "") if isinstance(selection, dict) else ""
            account = accounts.get(account_id)
            if policy == "legacy":
                readiness, status = deployment_provider_readiness(str(item.get("profile") or ""))
                item["provider_readiness"] = {
                    "readiness": readiness,
                    "policy": "legacy",
                }
                if status:
                    item["provider_readiness"]["status"] = status
            elif policy == "personal_preferred" and not account_id:
                readiness, status = deployment_provider_readiness(str(item.get("profile") or ""))
                item["provider_readiness"] = {
                    "readiness": readiness,
                    "policy": "personal_preferred",
                    "fallback": True,
                }
                if status:
                    item["provider_readiness"]["status"] = status
            elif not account_id:
                item["provider_readiness"] = {
                    "readiness": "action_required",
                    "policy": policy,
                    "status": "missing",
                }
            elif account is None:
                item["provider_readiness"] = {
                    "readiness": "action_required",
                    "policy": policy,
                    "account_id": account_id,
                    "status": "missing",
                }
            elif policy == "personal_preferred" and self._uses_deployment_provider_route(item):
                readiness, status = deployment_provider_readiness(str(item.get("profile") or ""))
                item["provider_readiness"] = {
                    "readiness": readiness,
                    "policy": "personal_preferred",
                    "account_id": account_id,
                    "provider": str(account.get("provider") or ""),
                    "label": str(account.get("label") or ""),
                    "fallback": True,
                }
                if status:
                    item["provider_readiness"]["status"] = status
            else:
                status = str(account.get("status") or "unknown")
                item["provider_readiness"] = {
                    "readiness": "ready" if status == "ready" else "action_required",
                    "policy": policy,
                    "account_id": account_id,
                    "provider": str(account.get("provider") or ""),
                    "label": str(account.get("label") or ""),
                    "status": status,
                }
            item["capability_readiness"] = capability_readiness.get(
                worker_id,
                {"active_grants": 0, "unavailable_grants": 0, "readiness": "ready"},
            )
            item["next_schedule_at"] = next_schedule_by_worker.get(worker_id, "")
            item["schedule_readiness"] = schedule_readiness
        return {
            "items": items,
            "next_cursor": _encode_workspace_cursor(items[-1]) if len(rows) > page_size and items else None,
        }

    def recurring_schedule_owner(self) -> str:
        return _recurring_schedule_owner()

    def _delegated_schedule_call(
        self,
        action: str,
        payload: dict[str, object],
        *,
        tenant_id: str,
        owner_id: str,
    ) -> object:
        return self.scheduling_owner_client.call(
            action,
            payload,
            identity=SchedulingOwnerIdentity(
                tenant_id=tenant_id or "local",
                owner_id=owner_id,
                agent_id=str(os.environ.get("VIVENTIUM_MAIN_AGENT_ID") or "scheduling-cortex"),
            ),
        )

    def _deactivate_delegated_schedules_for_closed_worker(self, worker: dict) -> int:
        """Deactivate every authoritative delegated definition before Close is complete."""

        if self.recurring_schedule_owner() != DELEGATED_RECURRENCE_OWNER:
            return 0
        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        worker_id = str(worker.get("worker_id") or "")
        deactivated = 0
        for _ in range(100):
            result = self._delegated_schedule_call(
                "list",
                {"worker_id": worker_id, "include_inactive": False, "limit": 100},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid schedule data")
            definition_ids = [
                str(item.get("definition_id") or "").strip()
                for item in result
                if str(item.get("definition_id") or "").strip()
            ]
            if not definition_ids:
                return deactivated
            for definition_id in definition_ids:
                response = self._delegated_schedule_call(
                    "deactivate",
                    {"definition_id": definition_id},
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                if not isinstance(response, dict):
                    raise RuntimeError("Viventium Scheduling Cortex returned invalid schedule data")
                deactivated += 1
            if len(definition_ids) < 100:
                return deactivated
        raise RuntimeError("Delegated workspace schedule cleanup did not converge")

    def list_recurring_schedules(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str | None = None,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "list",
                {
                    "worker_id": worker_id or "",
                    "include_inactive": include_inactive,
                    "limit": max(1, min(int(limit), 100)),
                },
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid schedule data")
            schedules = result
        elif worker_id:
            schedules = self.store.list_recurring_schedule_definitions(
                worker_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                include_inactive=include_inactive,
                limit=limit,
            )
        else:
            schedules = self.store.list_recurring_schedule_definitions_for_owner(
                tenant_id=tenant_id,
                owner_id=owner_id,
                include_inactive=include_inactive,
                limit=limit,
            )

        enriched: list[dict] = []
        for schedule in schedules:
            item = dict(schedule)
            if self.recurring_schedule_owner() != DELEGATED_RECURRENCE_OWNER:
                latest = self.store.list_recurring_schedule_occurrences(
                    str(item.get("definition_id") or ""),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    limit=1,
                )
                if latest:
                    occurrence = latest[0]
                    outcome = str(occurrence.get("outcome") or "").strip()
                    if outcome in {"", "pending", "manual_pending"}:
                        outcome = str(occurrence.get("state") or "pending").strip()
                    item["last_occurrence_at"] = occurrence.get("scheduled_for")
                    item["last_outcome"] = outcome
                    item["last_error"] = str(occurrence.get("last_error") or "")
            scheduled_worker_id = str(item.get("worker_id") or "")
            worker = self.store.get_worker(
                scheduled_worker_id,
                tenant_id=tenant_id or "local",
                owner_id=owner_id,
            )
            if worker:
                item["workspace_name"] = str(worker.get("name") or "")
            enriched.append(item)
        return enriched

    def get_recurring_schedule(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict | None:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "get",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            definition = result if isinstance(result, dict) else None
        else:
            definition = self.store.get_recurring_schedule_definition(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        if not definition:
            return None
        enriched = dict(definition)
        if self.recurring_schedule_owner() != DELEGATED_RECURRENCE_OWNER:
            latest = self.store.list_recurring_schedule_occurrences(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                limit=1,
            )
            if latest:
                occurrence = latest[0]
                outcome = str(occurrence.get("outcome") or "").strip()
                if outcome in {"", "pending", "manual_pending"}:
                    outcome = str(occurrence.get("state") or "pending").strip()
                enriched["last_occurrence_at"] = occurrence.get("scheduled_for")
                enriched["last_outcome"] = outcome
                enriched["last_error"] = str(occurrence.get("last_error") or "")
        worker = self.store.get_worker(
            str(enriched.get("worker_id") or ""),
            tenant_id=tenant_id or "local",
            owner_id=owner_id,
        )
        if worker:
            enriched["workspace_name"] = str(worker.get("name") or "")
        return enriched

    def list_recurring_schedule_occurrences(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict]:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "occurrences",
                {"definition_id": definition_id, "limit": max(1, min(int(limit), 100))},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid occurrence data")
            return result
        return self.store.list_recurring_schedule_occurrences(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            limit=limit,
        )

    def create_recurring_schedule(
        self,
        worker_id: str,
        instruction: str,
        *,
        recurrence_type: str,
        interval_seconds: int | None = None,
        local_time: str = "",
        timezone_name: str = "UTC",
        dst_policy: str = "next_valid_earliest",
        first_run_at: str | None = None,
        cron_expression: str = "",
        rrule: str = "",
        starts_at: str | None = None,
        ends_at: str | None = None,
        enabled: bool = True,
        overlap_policy: str = "skip",
        misfire_grace_seconds: int = 300,
        catch_up_policy: str = "coalesce",
        max_catch_up_occurrences: int = 1,
        jitter_seconds: int = 0,
        schedule_text: str = "",
        runtime_bundle: dict | None = None,
    ) -> dict:
        schedule_owner = self.recurring_schedule_owner()
        normalized_instruction = str(instruction or "").strip()
        if not normalized_instruction:
            raise ValueError("instruction is required")
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        effective_starts_at = starts_at or (first_run_at if recurrence_type == "once" else None)
        spec = normalize_recurrence_spec(
            recurrence_type=recurrence_type,
            interval_seconds=interval_seconds,
            local_time=local_time,
            timezone_name=timezone_name,
            dst_policy=dst_policy,
            cron_expression=cron_expression,
            rrule=rrule,
            starts_at=effective_starts_at,
            ends_at=ends_at,
            enabled=enabled,
            overlap_policy=overlap_policy,
            misfire_grace_seconds=misfire_grace_seconds,
            catch_up_policy=catch_up_policy,
            max_catch_up_occurrences=max_catch_up_occurrences,
            jitter_seconds=jitter_seconds,
        )
        creation_time = datetime.now(timezone.utc).replace(microsecond=0)
        if not spec.get("starts_at") and spec["recurrence_type"] in {"cron", "rfc5545"}:
            spec["starts_at"] = creation_time.isoformat()
        first_occurrence = first_occurrence_at(
            spec,
            now=creation_time,
            first_run_at=first_run_at,
        )
        self._require_schedule_principal_authority(
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
            establish=True,
        )
        if schedule_owner == DELEGATED_RECURRENCE_OWNER:
            effective_bundle = merge_bootstrap_bundle(
                self._bootstrap_bundle_for(worker),
                runtime_bundle,
            )
            delegated_payload = {
                "definition_id": f"rsd_{uuid.uuid4().hex}",
                "worker_id": worker_id,
                "project_id": str(worker.get("project_id") or ""),
                "instruction": normalized_instruction,
                "schedule_text": str(schedule_text or ""),
                "execution_mode": str(worker.get("execution_mode") or "docker"),
                "required_capability_servers": _required_capability_servers(effective_bundle),
                **spec,
                "next_run_at": first_occurrence.isoformat(),
            }
            result = self._delegated_schedule_call(
                "create",
                delegated_payload,
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
            )
            if not isinstance(result, dict):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid schedule data")
            try:
                self._require_schedule_principal_authority(
                    tenant_id=str(worker.get("tenant_id") or "local"),
                    owner_id=str(worker.get("owner_id") or ""),
                    establish=False,
                )
            except SchedulePrincipalAuthorityError:
                definition_id = str(result.get("definition_id") or "")
                if definition_id:
                    try:
                        self._delegated_schedule_call(
                            "deactivate",
                            {"definition_id": definition_id},
                            tenant_id=str(worker.get("tenant_id") or "local"),
                            owner_id=str(worker.get("owner_id") or ""),
                        )
                    except Exception as cleanup_error:
                        logger.warning(
                            "Could not compensate delegated schedule creation after authority revocation: %s",
                            cleanup_error,
                        )
                raise
            try:
                self._ensure_execution_allowed(self.require_worker(worker_id))
            except ControlPlaneConflict:
                definition_id = str(result.get("definition_id") or "")
                if definition_id:
                    try:
                        self._delegated_schedule_call(
                            "deactivate",
                            {"definition_id": definition_id},
                            tenant_id=str(worker.get("tenant_id") or "local"),
                            owner_id=str(worker.get("owner_id") or ""),
                        )
                    except Exception as cleanup_error:
                        logger.warning(
                            "Could not compensate delegated schedule creation after workspace closure: %s",
                            cleanup_error,
                        )
                        self.store.record_worker_termination_cleanup_failure(
                            worker_id,
                            "Delegated schedule cleanup failed after workspace closure",
                        )
                raise
            if runtime_bundle is not None:
                self.store.update_worker(
                    worker_id,
                    bootstrap_bundle_json=json.dumps(
                        merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle)
                    ),
                )
            self.store.add_event(
                str(worker.get("project_id") or ""),
                worker_id,
                None,
                "recurrence.created",
                f"Recurring schedule owned by {schedule_owner} starts at {first_occurrence.isoformat()}",
            )
            return result
        if runtime_bundle is not None:
            worker = self.store.update_worker(
                worker_id,
                bootstrap_bundle_json=json.dumps(
                    merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle)
                ),
            ) or worker
        try:
            definition = self.store.create_recurring_schedule_definition(
                worker_id=worker_id,
                project_id=str(worker.get("project_id") or ""),
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
                scheduler_owner=recurrence_owner_storage_value(schedule_owner),
                instruction=normalized_instruction,
                schedule_text=str(schedule_text or ""),
                recurrence_type=str(spec["recurrence_type"]),
                interval_seconds=(int(spec["interval_seconds"]) if spec["interval_seconds"] is not None else None),
                local_time=str(spec["local_time"]),
                timezone_name=str(spec["timezone_name"]),
                dst_policy=str(spec["dst_policy"]),
                next_run_at=first_occurrence.isoformat(),
                cron_expression=str(spec["cron_expression"]),
                rrule=str(spec["rrule"]),
                starts_at=str(spec["starts_at"]) if spec["starts_at"] else None,
                ends_at=str(spec["ends_at"]) if spec["ends_at"] else None,
                enabled=bool(spec["enabled"]),
                overlap_policy=str(spec["overlap_policy"]),
                misfire_grace_seconds=int(spec["misfire_grace_seconds"]),
                catch_up_policy=str(spec["catch_up_policy"]),
                max_catch_up_occurrences=int(spec["max_catch_up_occurrences"]),
                jitter_seconds=int(spec["jitter_seconds"]),
                require_principal_authority=multi_user_security_enabled(),
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            "recurrence.created",
            f"Recurring schedule owned by {schedule_owner} starts at {first_occurrence.isoformat()}",
        )
        return definition

    def deactivate_recurring_schedule(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict | None:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "deactivate",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            return result if isinstance(result, dict) else None
        return self.store.deactivate_recurring_schedule_definition(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    def update_recurring_schedule(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        updates: dict[str, object],
    ) -> dict | None:
        if updates.get("enabled") is True:
            self._require_schedule_principal_authority(
                tenant_id=tenant_id,
                owner_id=owner_id,
                establish=True,
            )
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            current = self._delegated_schedule_call(
                "get",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            current_worker = (
                self.store.get_worker(
                    str(current.get("worker_id") or ""),
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
                if isinstance(current, dict)
                else None
            )
            if current_worker:
                self._ensure_execution_allowed(current_worker)
            result = self._delegated_schedule_call(
                "update",
                {"definition_id": definition_id, "updates": updates},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if updates.get("enabled") is True:
                try:
                    self._require_schedule_principal_authority(
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        establish=False,
                    )
                except SchedulePrincipalAuthorityError:
                    try:
                        self._delegated_schedule_call(
                            "deactivate",
                            {"definition_id": definition_id},
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                        )
                    except Exception as cleanup_error:
                        logger.warning(
                            "Could not compensate delegated schedule enable after authority revocation: %s",
                            cleanup_error,
                        )
                    raise
            if current_worker:
                latest_worker = self.store.get_worker(
                    str(current_worker.get("worker_id") or ""),
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
                try:
                    if latest_worker:
                        self._ensure_execution_allowed(latest_worker)
                except ControlPlaneConflict:
                    if updates.get("enabled") is True:
                        try:
                            self._delegated_schedule_call(
                                "deactivate",
                                {"definition_id": definition_id},
                                tenant_id=tenant_id,
                                owner_id=owner_id,
                            )
                        except Exception as cleanup_error:
                            logger.warning(
                                "Could not compensate delegated schedule update after workspace close: %s",
                                cleanup_error,
                            )
                            self.store.record_worker_termination_cleanup_failure(
                                str(current_worker.get("worker_id") or ""),
                                "Delegated schedule cleanup failed after workspace closure",
                            )
                    raise
            return result if isinstance(result, dict) else None
        current = self.store.get_recurring_schedule_definition(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if current is None:
            return None
        if current.get("retired_at"):
            raise ValueError("retired schedule cannot be changed")
        merged = {**current, **{key: value for key, value in updates.items() if value is not None}}
        spec = normalize_recurrence_spec(
            recurrence_type=str(merged.get("recurrence_type") or ""),
            interval_seconds=merged.get("interval_seconds"),
            local_time=str(merged.get("local_time") or ""),
            timezone_name=str(merged.get("timezone_name") or "UTC"),
            dst_policy=str(merged.get("dst_policy") or "next_valid_earliest"),
            cron_expression=str(merged.get("cron_expression") or ""),
            rrule=str(merged.get("rrule") or ""),
            starts_at=str(merged.get("starts_at") or "") or None,
            ends_at=str(merged.get("ends_at") or "") or None,
            enabled=bool(merged.get("enabled", True)),
            overlap_policy=str(merged.get("overlap_policy") or "skip"),
            misfire_grace_seconds=int(merged.get("misfire_grace_seconds") or 0),
            catch_up_policy=str(merged.get("catch_up_policy") or "skip"),
            max_catch_up_occurrences=int(merged.get("max_catch_up_occurrences") or 1),
            jitter_seconds=int(merged.get("jitter_seconds") or 0),
        )
        shape_fields = {
            "recurrence_type",
            "interval_seconds",
            "local_time",
            "timezone_name",
            "dst_policy",
            "cron_expression",
            "rrule",
            "starts_at",
            "ends_at",
        }
        next_run_at = str(current.get("next_run_at") or "")
        if bool(spec["enabled"]) and (shape_fields.intersection(updates) or not bool(current.get("enabled"))):
            next_run_at = first_occurrence_at(
                spec,
                now=datetime.now(timezone.utc).replace(microsecond=0),
            ).isoformat()
        normalized_updates = {
            "instruction": str(merged.get("instruction") or "").strip(),
            "schedule_text": str(merged.get("schedule_text") or ""),
            **spec,
            "next_run_at": next_run_at,
        }
        if not normalized_updates["instruction"]:
            raise ValueError("instruction is required")
        try:
            return self.store.update_recurring_schedule_definition(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                fields=normalized_updates,
                require_principal_authority=(
                    bool(normalized_updates.get("enabled")) and multi_user_security_enabled()
                ),
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc

    def retire_recurring_schedule(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict | None:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "retire",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            return result if isinstance(result, dict) else None
        return self.store.retire_recurring_schedule_definition(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    def run_recurring_schedule_now(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_token: str,
    ) -> dict | None:
        self._require_schedule_principal_authority(
            tenant_id=tenant_id,
            owner_id=owner_id,
            establish=True,
        )
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            current = self._delegated_schedule_call(
                "get",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            current_worker = (
                self.store.get_worker(
                    str(current.get("worker_id") or ""),
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
                if isinstance(current, dict)
                else None
            )
            if current_worker:
                self._ensure_execution_allowed(current_worker)
            result = self._delegated_schedule_call(
                "run_now",
                {"definition_id": definition_id, "idempotency_key": idempotency_token},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if current_worker:
                latest_worker = self.store.get_worker(
                    str(current_worker.get("worker_id") or ""),
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
                if latest_worker:
                    self._ensure_execution_allowed(latest_worker)
            return result if isinstance(result, dict) else None
        definition = self.store.get_recurring_schedule_definition(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if definition is None or definition.get("retired_at"):
            return None
        self._revalidate_recurring_schedule_fire(definition)
        scheduled_for = datetime.now(timezone.utc).isoformat()
        try:
            schedule = self.store.create_recurring_schedule_run_now(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                idempotency_token=idempotency_token,
                scheduled_for=scheduled_for,
                require_principal_authority=multi_user_security_enabled(),
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc
        if schedule is not None:
            schedule.update(
                {
                    "status": "scheduled",
                    "schedule_owner": NATIVE_RECURRENCE_OWNER,
                    "owner_action": "dispatch_here",
                }
            )
        return schedule

    def _revalidate_worker_schedule_fire(
        self,
        worker: dict,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict:
        self._require_schedule_principal_authority(
            tenant_id=tenant_id,
            owner_id=owner_id,
            establish=False,
        )
        if str(worker.get("tenant_id") or "local") != str(tenant_id or "local"):
            raise ValueError("schedule tenant no longer matches its workspace")
        if str(worker.get("owner_id") or "") != str(owner_id or ""):
            raise ValueError("schedule owner no longer has access to its workspace")
        self._ensure_execution_allowed(worker)
        if str(worker.get("state") or "") == "failed":
            raise ValueError("scheduled workspace is unavailable")
        self._ensure_profile_allowed(str(worker.get("profile") or ""))
        if (
            str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user"
            and self.control_plane_store is None
        ):
            raise ValueError("standalone multi-user schedule revalidation is unavailable")
        if self.control_plane_store is None:
            return worker

        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        selection = mission_provider_account_selection(worker)
        if selection is not None:
            account = self.control_plane_store.get_provider_account(
                account_id=selection.account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if account is None or str(account.get("status") or "") != "ready":
                if selection.policy == "personal_required":
                    raise ScheduleActionRequiredError(
                        "provider_account_reconnect_required",
                        "The selected worker account needs to be reconnected before this schedule can run.",
                        "Open Connections, reconnect the account, and then try Run now again.",
                    )

        grants = self.control_plane_store.list_workspace_grants(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=str(worker.get("worker_id") or ""),
        )
        accounts = {
            str(item.get("account_id") or ""): item
            for item in self.control_plane_store.list_provider_accounts(
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        }
        connections = {
            str(item.get("connection_id") or ""): item
            for item in self.control_plane_store.list_connections(
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        }
        for grant in grants:
            account_id = str(grant.get("account_id") or "")
            connection_id = str(grant.get("connection_id") or "")
            if account_id and str(accounts.get(account_id, {}).get("status") or "") != "ready":
                raise ScheduleActionRequiredError(
                    "capability_account_reconnect_required",
                    "A worker account used by this workspace needs to be reconnected.",
                    "Open Connections, reconnect the account, and then try Run now again.",
                )
            if connection_id and str(connections.get(connection_id, {}).get("status") or "") != "ready":
                raise ScheduleActionRequiredError(
                    "connection_reconnect_required",
                    "A connected service used by this workspace needs attention.",
                    "Open Connections, repair the service connection, and then try Run now again.",
                )
        return worker

    def _require_schedule_principal_authority(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        establish: bool,
    ) -> dict | None:
        if not multi_user_security_enabled():
            return None
        authority = self.store.get_schedule_principal_authority(
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if authority is None and establish:
            authority = self.store.ensure_schedule_principal_authority(
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        if authority is None:
            raise SchedulePrincipalAuthorityError(
                "scheduled principal authority must be renewed before this schedule can run"
            )
        if not bool(authority.get("enabled")):
            raise SchedulePrincipalAuthorityError(
                "scheduled principal has been disabled"
            )
        return authority

    def set_schedule_principal_authority(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        enabled: bool,
    ) -> dict:
        result = self.store.set_schedule_principal_authority(
            tenant_id=tenant_id,
            owner_id=owner_id,
            enabled=enabled,
        )
        result["deactivated_delegated_definitions"] = 0
        if not enabled and self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            delegated = self._delegated_schedule_call(
                "deactivate_owner",
                {},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if not isinstance(delegated, dict):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid authority data")
            result["deactivated_delegated_definitions"] = int(
                delegated.get("deactivated") or 0
            )
        return result

    def _revalidate_recurring_schedule_fire(self, definition: dict[str, object]) -> dict:
        worker = self.require_worker(str(definition.get("worker_id") or ""))
        return self._revalidate_worker_schedule_fire(
            worker,
            tenant_id=str(definition.get("tenant_id") or "local"),
            owner_id=str(definition.get("owner_id") or ""),
        )

    def revalidate_scheduling_cortex_workspace_fire(
        self,
        worker: dict,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict:
        """Recheck the current reusable workspace authority before a delegated fire mutates state."""

        return self._revalidate_worker_schedule_fire(
            worker,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    @staticmethod
    def _recurrence_dispatch_at(
        definition: dict[str, object],
        *,
        scheduled_for: datetime,
        detected_at: datetime,
    ) -> datetime:
        jitter_bound = max(0, int(definition.get("jitter_seconds") or 0))
        if jitter_bound == 0:
            return max(scheduled_for, detected_at)
        digest = hashlib.sha256(
            f"{definition.get('definition_id')}\0{scheduled_for.isoformat()}".encode("utf-8")
        ).digest()
        jitter = int.from_bytes(digest[:8], "big") % (jitter_bound + 1)
        return max(scheduled_for, detected_at) + timedelta(seconds=jitter)

    def _materialize_due_recurring_schedules(self, now: datetime) -> list[dict[str, object]]:
        try:
            owner = self.recurring_schedule_owner()
        except ValueError as exc:
            logger.error("GlassHive native recurrence is disabled by scheduler ownership configuration: %s", exc)
            return []
        if owner != NATIVE_RECURRENCE_OWNER:
            return []
        now_iso = now.astimezone(timezone.utc).isoformat()
        materialized: list[dict[str, object]] = []
        definitions = self.store.list_due_recurring_schedule_definitions(
            now_iso,
            scheduler_owner=recurrence_owner_storage_value(NATIVE_RECURRENCE_OWNER),
            limit=50,
        )
        for definition in definitions:
            try:
                occurrences, next_occurrence = due_occurrences_and_next(definition, now=now)
            except (TypeError, ValueError, OverflowError) as exc:
                logger.error(
                    "Skipped invalid recurring schedule definition %s: %s",
                    definition.get("definition_id"),
                    exc,
                )
                continue
            if not occurrences:
                continue
            expected_next = str(definition.get("next_run_at") or "")
            overlap = str(definition.get("overlap_policy") or "skip")
            for index, occurrence_decision in enumerate(occurrences):
                occurrence = occurrence_decision["scheduled_for"]
                assert isinstance(occurrence, datetime)
                state = str(occurrence_decision.get("state") or "pending")
                outcome = str(occurrence_decision.get("outcome") or "pending")
                if state == "pending" and overlap == "skip":
                    worker_id = str(definition.get("worker_id") or "")
                    if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
                        state, outcome = "skipped", "overlap_skipped"
                if state == "pending":
                    try:
                        self._revalidate_recurring_schedule_fire(definition)
                    except (KeyError, RuntimeError, ValueError) as exc:
                        state, outcome = "action_required", str(exc)
                following = (
                    occurrences[index + 1]["scheduled_for"]
                    if index + 1 < len(occurrences)
                    else next_occurrence
                )
                deactivate_after = following is None or state == "action_required"
                stored_next = occurrence if following is None else following
                assert isinstance(stored_next, datetime)
                dispatch_at = self._recurrence_dispatch_at(
                    definition,
                    scheduled_for=occurrence,
                    detected_at=now,
                )
                try:
                    schedule = self.store.materialize_recurring_schedule_occurrence(
                        str(definition.get("definition_id") or ""),
                        expected_next_run_at=expected_next,
                        scheduled_for=occurrence.isoformat(),
                        next_run_at=stored_next.isoformat(),
                        detected_at=now_iso,
                        dispatch_at=dispatch_at.isoformat(),
                        occurrence_state=state,
                        outcome=outcome,
                        deactivate_after=deactivate_after,
                    )
                except WorkerClosedStoreError:
                    break
                if schedule:
                    materialized.append(schedule)
                expected_next = stored_next.isoformat()
                if state == "action_required":
                    break
        return materialized

    def schedule_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        run_at: str | None = None,
        schedule_text: str | None = None,
        delay_seconds: int | None = None,
        runtime_bundle: dict | None = None,
        file_upload_ids: list[str] | None = None,
        file_upload_revisions: list[str] | None = None,
    ) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        file_manifest = self.files.manifest(
            str(worker.get("tenant_id") or "local"),
            str(worker.get("owner_id") or ""),
            file_upload_ids or [],
            file_upload_revisions or None,
        )
        if runtime_bundle is not None:
            worker = self.store.update_worker(
                worker_id,
                bootstrap_bundle_json=json.dumps(
                    merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle)
                ),
            ) or worker
        resolved_run_at = self._parse_run_at(
            run_at=run_at,
            schedule_text=schedule_text,
            delay_seconds=delay_seconds,
        )
        self._require_schedule_principal_authority(
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
            establish=True,
        )
        try:
            schedule = self.store.create_scheduled_run(
                worker_id=worker_id,
                project_id=str(worker.get("project_id") or ""),
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
                instruction=instruction,
                schedule_text=str(schedule_text or ""),
                run_at=resolved_run_at,
                require_principal_authority=multi_user_security_enabled(),
                file_manifest=file_manifest,
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            "schedule.created",
            f"Scheduled run for {resolved_run_at}",
        )
        self._emit_callback(worker, "schedule.created", message=f"Scheduled run for {resolved_run_at}")
        return schedule

    def _recurring_schedule_capacity_issue(self, schedule: dict[str, object]) -> str:
        schedule_id = str(schedule.get("schedule_id") or "")
        if not self.store.recurring_occurrence_for_schedule(schedule_id):
            return ""
        tenant_id = str(schedule.get("tenant_id") or "local")
        owner_id = str(schedule.get("owner_id") or "")
        user_limit = _bounded_int_env(
            "GLASSHIVE_MAX_CONCURRENT_RECURRING_RUNS_PER_USER",
            4,
            min_value=1,
            max_value=1000,
        )
        tenant_limit = _bounded_int_env(
            "GLASSHIVE_MAX_CONCURRENT_RECURRING_RUNS_PER_TENANT",
            32,
            min_value=1,
            max_value=10000,
        )
        if self.store.count_active_runs(tenant_id=tenant_id, owner_id=owner_id) >= user_limit:
            return "user_concurrency_deferred"
        if self.store.count_active_runs(tenant_id=tenant_id) >= tenant_limit:
            return "tenant_concurrency_deferred"
        return ""

    def process_due_schedules_once(self, now_iso: str | None = None) -> list[dict[str, object]]:
        processed: list[dict[str, object]] = []
        now = (
            parse_aware_utc(now_iso, label="now_iso")
            if now_iso is not None
            else datetime.now(timezone.utc)
        )
        self.store.recover_stale_recurring_occurrence_claims(now.isoformat())
        self._materialize_due_recurring_schedules(now)
        due = self.store.list_due_schedules(now.isoformat(), limit=50)
        for item in due:
            schedule_id = str(item.get("schedule_id") or "")
            capacity_issue = self._recurring_schedule_capacity_issue(item)
            if capacity_issue:
                self.store.mark_recurring_occurrence_retryable(schedule_id, capacity_issue)
                continue
            claimed = self.store.claim_schedule(schedule_id)
            if not claimed:
                continue
            try:
                run = self.assign_scheduled_run(claimed)
                run_id = str(run.get("run_id") or "")
                updated = self.store.get_schedule(schedule_id)
                current_run = self.store.get_run(run_id) if run_id else None
                current_state = str((current_run or {}).get("state") or "")
                if current_state in {"completed", "failed", "cancelled", "interrupted", "paused"}:
                    schedule_state = current_state if current_state in {"completed", "cancelled"} else "failed"
                    updated = self.store.finalize_schedule_for_run(
                        run_id,
                        state=schedule_state,
                        last_error=str((current_run or {}).get("error_text") or ""),
                    ) or updated
                processed.append(updated or claimed)
            except SchedulePrincipalAuthorityError:
                processed.append(self.store.get_schedule(schedule_id) or claimed)
            except ControlPlaneConflict as exc:
                # The schedule was already claimed. A provider/capability
                # conflict must become a visible terminal outcome; leaving the
                # row in `running` strands one-shot work forever and makes a
                # recurring occurrence churn through stale-claim recovery.
                updated = self.store.finalize_schedule(
                    schedule_id,
                    state="failed",
                    last_error=str(exc),
                )
                processed.append(updated or claimed)
            except Exception as exc:
                updated = self.store.finalize_schedule(schedule_id, state="failed", last_error=str(exc))
                processed.append(updated or claimed)
        return processed

    def assign_scheduled_run(
        self,
        schedule: dict[str, object],
        *,
        runtime_bundle: dict | None = None,
    ) -> dict:
        """Create-or-get the one stable run reserved for a claimed schedule."""

        schedule_id = str(schedule.get("schedule_id") or "").strip()
        worker_id = str(schedule.get("worker_id") or "").strip()
        if not schedule_id or not worker_id:
            raise ValueError("Scheduled dispatch requires schedule and worker ids")
        worker = self.require_worker(worker_id)
        self._revalidate_worker_schedule_fire(
            worker,
            tenant_id=str(schedule.get("tenant_id") or "local"),
            owner_id=str(schedule.get("owner_id") or ""),
        )
        effective_worker = dict(worker)
        if runtime_bundle is not None:
            effective_worker["bootstrap_bundle_json"] = json.dumps(
                merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle) or {},
                sort_keys=True,
                separators=(",", ":"),
            )
        self._ensure_allowed_ai(effective_worker)
        self._ensure_dispatch_provider_ready(effective_worker)
        provider_account_fence = self._provider_dispatch_fence(effective_worker)
        self._ensure_runtime_available(
            str(worker.get("profile") or ""),
            str(worker.get("execution_mode") or "docker"),
        )
        worker = self._refresh_worker_model_for_profile(worker)
        self._ensure_allowed_ai(worker)
        resumed = worker["state"] == "paused"
        try:
            run, created = self.store.create_or_get_run_for_schedule(
                schedule_id,
                runtime_bundle=runtime_bundle,
                require_principal_authority=multi_user_security_enabled(),
                provider_account_fence=provider_account_fence,
            )
        except ProviderAccountBusyStoreError as exc:
            raise self._provider_account_busy_conflict(exc) from exc
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            self.store.finalize_schedule(schedule_id, state="cancelled", last_error=str(exc))
            raise ControlPlaneConflict(str(exc)) from exc
        if created:
            self._record_run_allowed_ai_admission(
                run,
                self._ensure_allowed_ai(worker),
                worker=worker,
                origin_ref=f"schedule:{schedule_id}",
            )
        if resumed:
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.resumed",
                "Worker resume queued for the next run",
            )
            worker = self.store.get_worker(worker_id) or worker
        if created:
            instruction = str(schedule.get("instruction") or "")
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run["run_id"],
                "schedule.queued",
                instruction,
            )
            self._emit_callback(
                worker,
                "schedule.queued",
                run=run,
                message=instruction,
            )
        self._ensure_worker_processor(worker_id)
        return run

    def duplicate_worker(
        self,
        source_worker_id: str,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
        reapproval_items: list[dict[str, object]] | None = None,
    ) -> dict:
        source_worker = self.require_worker(source_worker_id)
        required_items = [dict(item) for item in (reapproval_items or []) if isinstance(item, dict)]
        initial_report: dict[str, object] = {
            "duplication_state": "pending",
            "source_state": "pending",
            "copied_files": 0,
            "skipped_items": 0,
            "capabilities_requiring_reapproval": len(required_items),
            "reapproval_items": required_items,
        }
        bootstrap_bundle = _duplicate_bootstrap_bundle(self._bootstrap_bundle_for(source_worker))
        profile = str(source_worker.get("profile") or "codex-cli")
        execution_mode = str(source_worker.get("execution_mode") or "docker")
        duplicated = self.create_worker(
            project_id=project_id,
            tenant_id=str(source_worker.get("tenant_id") or "local"),
            owner_id=owner_id,
            name=name,
            role=role,
            profile=profile,
            backend=self._legacy_backend_label(profile, execution_mode, str(source_worker.get("backend") or "")),
            execution_mode=execution_mode,
            alias=None,
            workspace_root=None,
            bootstrap_profile=str(source_worker.get("bootstrap_profile") or "") or None,
            bootstrap_bundle=bootstrap_bundle,
            workspace_kind="named",
            tags=normalize_workspace_tags(source_worker.get("tags") if isinstance(source_worker.get("tags"), list) else []),
            duplication_report=initial_report,
            start_synchronously=False,
        )
        try:
            copy_report = self._copy_workspace_contents(source_worker, duplicated)
        except Exception as exc:
            current_duplicate = self.store.get_worker(str(duplicated["worker_id"])) or duplicated
            cleanup_ready = False
            try:
                stopped = self.runtime.terminate_worker(current_duplicate)
                if stopped.pid:
                    raise RuntimeError("duplicate cleanup left worker compute active")
                self._apply_runtime_info(
                    str(duplicated["worker_id"]),
                    stopped,
                    state="failed",
                    last_error=str(exc),
                    compute_released_at=utc_now(),
                )
                cleanup_ready = True
            except Exception as cleanup_exc:
                cleanup_message = public_callback_message_text(str(cleanup_exc)) or "duplicate cleanup failed"
                self.store.update_worker(
                    duplicated["worker_id"],
                    state="failed",
                    last_error=f"{exc}; {cleanup_message}",
                )
            self.store.add_event(
                project_id,
                duplicated["worker_id"],
                None,
                "worker.duplicate_failed",
                str(exc),
            )
            if cleanup_ready:
                from .execution_profile import packaged_linux

                physical_cleanup_ready = True
                if packaged_linux() and execution_mode == "docker":
                    try:
                        native = self.runtime._runtime_for_worker(current_duplicate)
                        box = getattr(getattr(native, "sandbox", None), "box", None)
                        physical_cleanup_ready = bool(
                            box is not None and box.discard_empty_prepared_workspace()
                        )
                    except Exception:
                        physical_cleanup_ready = False
                if physical_cleanup_ready:
                    self.store.delete_unstarted_worker(
                        str(duplicated["worker_id"]),
                        project_id=project_id,
                        tenant_id=str(source_worker.get("tenant_id") or "local"),
                        owner_id=owner_id,
                    )
            if isinstance(exc, OSError) and getattr(self, "files", None) is not None:
                raise self.files.storage_adapter.failure(
                    str(source_worker.get("tenant_id") or "local"),
                    str(source_worker.get("owner_id") or ""),
                    exc,
                ) from exc
            raise
        duplication_report = dict(copy_report)
        if required_items:
            duplication_report.update(
                {
                    "duplication_state": "complete",
                    "capabilities_requiring_reapproval": len(required_items),
                    "reapproval_items": required_items,
                }
            )
        self.store.update_worker(
            duplicated["worker_id"],
            duplication_report_json=json.dumps(duplication_report, sort_keys=True, separators=(",", ":")),
        )
        self.store.add_event(
            project_id,
            duplicated["worker_id"],
            None,
            "worker.duplicated",
            "Workspace duplicated: "
            f"{duplication_report['copied_files']} files copied, "
            f"{duplication_report['skipped_items']} items skipped",
        )
        updated = self.store.get_worker(duplicated["worker_id"]) or duplicated
        return {**updated, "duplication_report": duplication_report}

    def save_workspace_template(
        self,
        worker_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        name: str,
        description: str = "",
        lineage_id: str | None = None,
    ) -> dict[str, object]:
        if self.control_plane_store is None:
            raise RuntimeError("Workspace templates require the user control plane")
        worker = self.require_worker(worker_id)
        if str(worker.get("tenant_id") or "local") != str(tenant_id or "local") or str(
            worker.get("owner_id") or ""
        ) != str(owner_id or ""):
            raise KeyError("Workspace not found")
        self.require_project(str(worker.get("project_id") or ""))
        library_refs = self.control_plane_store.workspace_template_library_refs(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
        )
        source_bootstrap = self._bootstrap_bundle_for(worker)
        sanitized_bootstrap = _duplicate_bootstrap_bundle(source_bootstrap) or {}
        provider_account_ref: dict[str, str] | None = None
        raw_provider_ref = source_bootstrap.get("provider_account") if isinstance(source_bootstrap, dict) else None
        if isinstance(raw_provider_ref, dict):
            policy = str(raw_provider_ref.get("policy") or "").strip().lower()
            account_id = str(raw_provider_ref.get("account_id") or "").strip()
            if policy == "legacy" and not account_id:
                provider_account_ref = {"policy": "legacy"}
            elif policy in {"personal_preferred", "personal_required"}:
                if not account_id:
                    provider_account_ref = {"policy": policy}
                else:
                    account = self.control_plane_store.get_provider_account(
                        account_id=account_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                    )
                    supported = PROFILE_ACCOUNT_PROVIDERS.get(str(worker.get("profile") or ""), set())
                    if account is not None and str(account.get("provider") or "").lower() in supported:
                        provider_account_ref = {
                            "policy": policy,
                            "account_id": account_id,
                            "provider": str(account.get("provider") or ""),
                        }
        content: dict[str, object] = {
            "schema_version": 1,
            "project": {
                "title": str(name or "Workspace template").strip()[:200],
                "goal": str(description or "").strip()[:1000],
            },
            "worker": {
                "name": str(worker.get("name") or name).strip()[:160],
                "role": str(worker.get("role") or "main").strip()[:160],
                "profile": str(worker.get("profile") or "codex-cli").strip(),
                "execution_mode": str(worker.get("execution_mode") or "docker").strip(),
                "bootstrap_profile": str(worker.get("bootstrap_profile") or "").strip(),
                "bootstrap_bundle": sanitized_bootstrap,
                **({"provider_account_ref": provider_account_ref} if provider_account_ref else {}),
                "tags": normalize_workspace_tags(
                    worker.get("tags") if isinstance(worker.get("tags"), list) else []
                ),
            },
            "library_refs": library_refs,
        }
        return self.control_plane_store.create_workspace_template(
            tenant_id=tenant_id,
            owner_id=owner_id,
            name=name,
            description=description,
            content=content,
            lineage_id=lineage_id,
        )

    def list_workspace_templates(self, *, tenant_id: str, owner_id: str) -> list[dict[str, object]]:
        if self.control_plane_store is None:
            return []
        return self.control_plane_store.list_workspace_templates(
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    def instantiate_workspace_template(
        self,
        template_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        name: str | None = None,
    ) -> dict[str, object] | None:
        if self.control_plane_store is None:
            raise RuntimeError("Workspace templates require the user control plane")
        template = self.control_plane_store.get_workspace_template(
            template_id=template_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if template is None:
            return None
        content = template.get("content")
        if not isinstance(content, dict) or int(content.get("schema_version") or 0) != 1:
            raise ValueError("Workspace template schema is unsupported")
        project_spec = content.get("project")
        worker_spec = content.get("worker")
        library_refs = content.get("library_refs")
        if not isinstance(project_spec, dict) or not isinstance(worker_spec, dict) or not isinstance(library_refs, list):
            raise ValueError("Workspace template content is invalid")
        profile = str(worker_spec.get("profile") or "").strip()
        execution_mode = str(worker_spec.get("execution_mode") or "").strip()
        self._ensure_execution_allowed(execution_mode)
        self._ensure_profile_allowed(profile)
        approvals_required = self.control_plane_store.validate_workspace_template_libraries(
            library_refs=[dict(item) for item in library_refs if isinstance(item, dict)],
            profile=profile,
        )
        validated_library_refs = {
            str(item.get("library_id") or ""): item
            for item in approvals_required
            if isinstance(item, dict) and str(item.get("library_id") or "")
        }
        reapproval_items = [
            {
                "action_id": "rea_" + hashlib.sha256(
                    f"library_grant\0{str(reference.get('library_id') or '')}".encode("utf-8")
                ).hexdigest()[:24],
                "kind": "library",
                "resolution": "library_grant",
                "reference": str(reference.get("library_id") or ""),
                "label": str(
                    validated_library_refs.get(str(reference.get("library_id") or ""), {}).get("stable_id")
                    or reference.get("stable_id")
                    or "Library capability"
                )[:160],
                "route": "library",
                "scopes": sorted(
                    {str(scope) for scope in (reference.get("scopes") or []) if str(scope)}
                ),
            }
            for reference in library_refs
            if isinstance(reference, dict)
            and str(reference.get("library_id") or "") in validated_library_refs
        ]
        provider_account_ref = worker_spec.get("provider_account_ref")
        provider_account_selection: dict[str, str] | None = None
        if provider_account_ref is not None:
            if not isinstance(provider_account_ref, dict):
                raise ValueError("Workspace template provider account reference is invalid")
            policy = str(provider_account_ref.get("policy") or "").strip().lower()
            account_id = str(provider_account_ref.get("account_id") or "").strip()
            if policy not in WORKSPACE_ACCOUNT_POLICIES:
                raise ValueError("Workspace template provider account policy is invalid")
            if policy == "legacy":
                if account_id:
                    raise ValueError("Workspace template deployment account policy is invalid")
                provider_account_selection = {"policy": "legacy"}
            elif not account_id:
                if policy == "personal_required":
                    raise ValueError("Workspace template requires a selected personal provider account")
                provider_account_selection = {"policy": policy}
            else:
                account = self.control_plane_store.get_provider_account(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                supported = PROFILE_ACCOUNT_PROVIDERS.get(profile, set())
                if account is None:
                    raise ValueError("Workspace template provider account is not available for this user")
                if str(account.get("provider") or "").strip().lower() not in supported:
                    raise ValueError("Workspace template provider account does not match the worker profile")
                if str(account.get("status") or "").strip().lower() != "ready":
                    raise ValueError("Workspace template provider account must be reconnected before use")
                provider_account_selection = {"policy": policy, "account_id": account_id}
        requested_name = str(name or worker_spec.get("name") or template.get("name") or "Workspace").strip()[:160]
        request_payload = {"template_id": template_id, "name": requested_name}
        reservation = self.control_plane_store.reserve_workspace_template_instantiation(
            template_id=template_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        if reservation.get("idempotent_replay"):
            project = self.store.get_project(str(reservation.get("project_id") or ""))
            worker = self.store.get_worker(str(reservation.get("worker_id") or ""))
            if not project or not worker:
                raise RuntimeError("Completed template instantiation record is inconsistent")
            return {
                "template": {key: value for key, value in template.items() if key != "content"},
                "project": project,
                "workspace": worker,
                "approvals_required": approvals_required,
                "idempotent_replay": True,
            }
        reserved_project_id = str(reservation.get("project_id") or "").strip()
        if not reserved_project_id:
            raise RuntimeError("Template instantiation reservation has no project identity")
        try:
            project = self.create_project(
                owner_id,
                str(project_spec.get("title") or template.get("name") or "Workspace template")[:200],
                str(project_spec.get("goal") or "")[:10000],
                profile,
                tenant_id=tenant_id,
                project_id=reserved_project_id,
            )
            template_bootstrap = (
                dict(worker_spec.get("bootstrap_bundle"))
                if isinstance(worker_spec.get("bootstrap_bundle"), dict)
                else {}
            )
            if provider_account_selection is not None:
                template_bootstrap["provider_account"] = provider_account_selection
            worker = self.create_worker(
                project_id=str(project["project_id"]),
                tenant_id=tenant_id,
                owner_id=owner_id,
                name=requested_name,
                role=str(worker_spec.get("role") or "main")[:160],
                profile=profile,
                backend="",
                execution_mode=execution_mode,
                alias=None,
                workspace_root=None,
                bootstrap_profile=str(worker_spec.get("bootstrap_profile") or "") or None,
                bootstrap_bundle=template_bootstrap or None,
                workspace_kind="named",
                tags=normalize_workspace_tags(
                    worker_spec.get("tags") if isinstance(worker_spec.get("tags"), list) else []
                ),
                duplication_report={
                    "duplication_state": "complete",
                    "source_state": "template",
                    "copied_files": 0,
                    "skipped_items": 0,
                    "capabilities_requiring_reapproval": len(reapproval_items),
                    "reapproval_items": reapproval_items,
                },
                start_synchronously=False,
            )
            self.control_plane_store.complete_workspace_template_instantiation(
                tenant_id=tenant_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
                project_id=str(project["project_id"]),
                worker_id=str(worker["worker_id"]),
            )
        except Exception:
            current_project = self.store.get_project(
                reserved_project_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            current_workers = (
                self.store.list_workers(
                    reserved_project_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                if current_project
                else []
            )
            failed_worker_id = (
                str(current_workers[0].get("worker_id") or "")
                if len(current_workers) == 1
                else ""
            )
            try:
                self.control_plane_store.fail_workspace_template_instantiation(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    idempotency_key=idempotency_key,
                    project_id=reserved_project_id,
                    worker_id=failed_worker_id or None,
                )
                worker_cleaned = not current_workers
                if failed_worker_id:
                    current_worker = current_workers[0]
                    stopped = self.runtime.terminate_worker(current_worker)
                    if not stopped.pid:
                        self._apply_runtime_info(
                            failed_worker_id,
                            stopped,
                            state="failed",
                            last_error="Template instantiation was rolled back",
                            compute_released_at=utc_now(),
                        )
                        worker_cleaned = self.store.delete_unstarted_worker(
                            failed_worker_id,
                            project_id=reserved_project_id,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                        )
                project_cleaned = current_project is None or (
                    worker_cleaned
                    and self.store.delete_project_if_empty(
                        reserved_project_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                    )
                )
                if worker_cleaned and project_cleaned:
                    self.control_plane_store.complete_workspace_template_cleanup(
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        idempotency_key=idempotency_key,
                        project_id=reserved_project_id,
                        worker_id=failed_worker_id or None,
                    )
            except Exception as cleanup_exc:
                logger.error(
                    "Template instantiation rollback could not be completed for project %s: %s",
                    reserved_project_id,
                    public_callback_message_text(str(cleanup_exc)) or "cleanup failed",
                )
            raise
        return {
            "template": {key: value for key, value in template.items() if key != "content"},
            "project": project,
            "workspace": worker,
            "approvals_required": approvals_required,
            "idempotent_replay": False,
        }

    def _can_restart_released_idle_worker(self, worker: dict) -> bool:
        """Compute reclamation does not carry the user's Pause intent."""
        worker_id = str(worker.get("worker_id") or "")
        return bool(
            worker.get("state") == "paused"
            and worker.get("compute_released_at")
            and not worker.get("compute_release_token")
            and not self.store.has_active_operator_pause(worker_id)
            and all(
                run.get("state") == "queued"
                for run in self.store.list_nonterminal_runs_for_worker(worker_id)
            )
        )

    def _assign_run_parallel(
        self,
        worker_id: str,
        instruction: str,
        event_type: str = "run.queued",
        runtime_bundle: dict | None = None,
        run_local_bundle: dict | None = None,
        start_processor: bool = True,
        idempotency_key: str | None = None,
        provider_request_id: str | None = None,
        resume_paused_worker: bool = True,
        origin_trace: dict[str, object] | None = None,
        continuation_contract: dict[str, object] | None = None,
        continuation_context: dict[str, object] | None = None,
    ) -> dict:
        normalized_key = str(idempotency_key or "").strip()
        while True:
            needs_resume = False
            with self._worker_compute_release_lock(worker_id):
                worker = self.require_worker(worker_id)
                policy_candidate = dict(worker)
                if runtime_bundle is not None:
                    policy_candidate["bootstrap_bundle_json"] = json.dumps(
                        merge_bootstrap_bundle(
                            self._bootstrap_bundle_for(worker), runtime_bundle
                        )
                        or {},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                self._ensure_execution_allowed(policy_candidate)
                self._queued_runtime_preflight(
                    str(worker.get("profile") or ""),
                    str(worker.get("execution_mode") or "docker"),
                    tenant_id=str(worker.get("tenant_id") or "local"),
                    owner_id=str(worker.get("owner_id") or ""),
                    lane=self._trusted_run_lane(worker),
                    worker=worker,
                )
                paused_control_run = (
                    next(
                        (
                            candidate
                            for candidate in self.store.list_nonterminal_runs_for_worker(
                                worker_id
                            )
                            if str(candidate.get("state") or "") == "paused"
                        ),
                        None,
                    )
                    if worker["state"] == "paused"
                    else None
                )
                if (
                    worker["state"] == "paused"
                    and resume_paused_worker
                    and paused_control_run is not None
                ):
                    needs_resume = True
                else:
                    worker = self._refresh_worker_model_for_profile(worker)
                    if runtime_bundle is not None:
                        worker = self.store.update_worker(
                            worker_id,
                            bootstrap_bundle_json=json.dumps(
                                merge_bootstrap_bundle(
                                    self._bootstrap_bundle_for(worker), runtime_bundle
                                )
                            ),
                        ) or worker
                    self._ensure_execution_allowed(worker)
                    allowed_ai_admission = self._ensure_allowed_ai(worker)
                    created = True
                    if str(provider_request_id or "").strip():
                        run, created = self.store.create_and_attach_provider_run(
                            request_id=str(provider_request_id).strip(),
                            worker_id=worker_id,
                            project_id=str(worker["project_id"]),
                            instruction=instruction,
                        )
                    elif normalized_key:
                        run_id = self._idempotent_run_id(worker_id, normalized_key)
                        stored_continuation = (
                            {**continuation_contract, "run_id": run_id}
                            if isinstance(continuation_contract, dict)
                            else None
                        )
                        run, created = self.store.create_idempotent_run(
                            run_id=run_id,
                            worker_id=worker_id,
                            project_id=str(worker["project_id"]),
                            instruction=instruction,
                            origin_trace=origin_trace,
                            continuation_contract=stored_continuation,
                            continuation_context=continuation_context,
                        )
                    else:
                        run_id = f"run_{uuid.uuid4().hex[:10]}"
                        stored_continuation = (
                            {**continuation_contract, "run_id": run_id}
                            if isinstance(continuation_contract, dict)
                            else None
                        )
                        run = self.store.create_run(
                            worker_id,
                            worker["project_id"],
                            instruction,
                            state="queued",
                            run_id=run_id,
                            origin_trace=origin_trace,
                            continuation_contract=stored_continuation,
                            continuation_context=continuation_context,
                        )
                    if created:
                        self._record_run_allowed_ai_admission(
                            run,
                            allowed_ai_admission,
                            worker=worker,
                            origin_trace=origin_trace,
                        )
                    if worker["state"] == "paused" and (
                        resume_paused_worker or self._can_restart_released_idle_worker(worker)
                    ):
                        worker = (
                            self.store.update_worker_state(
                                worker_id, "starting", last_error=""
                            )
                            or worker
                        )
                        self.store.add_event(
                            str(worker.get("project_id") or ""),
                            worker_id,
                            None,
                            "worker.resumed",
                            "Idle paused worker queued for startup",
                        )
            if not needs_resume:
                break
            # Never recurse into the non-reentrant lifecycle flock. Resume and
            # its startup handshake own the next generation; only after it
            # succeeds do we retry the run reservation under a fresh guard.
            self.resume_worker(worker_id)
        if run_local_bundle is not None and str(run.get("state") or "") == "queued":
            with self._run_local_bundles_lock:
                self._run_local_bundles[str(run["run_id"])] = dict(run_local_bundle)
                self._run_local_bundles_lock.notify_all()
        if not created:
            if start_processor and str(run.get("state") or "") == "queued":
                self._ensure_worker_processor(worker_id)
            return run
        self.store.add_event(
            worker["project_id"], worker_id, run["run_id"], event_type, instruction
        )
        self._emit_callback(worker, event_type, run=run, message=instruction)
        if start_processor:
            self._ensure_worker_processor(worker_id)
        return run

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        event_type: str = "run.queued",
        runtime_bundle: dict | None = None,
        run_local_bundle: dict | None = None,
        start_processor: bool = True,
        idempotency_key: str | None = None,
        provider_request_id: str | None = None,
        resume_paused_worker: bool = True,
        origin_trace: dict[str, object] | None = None,
        continuation_contract: dict[str, object] | None = None,
        continuation_context: dict[str, object] | None = None,
    ) -> dict:
        existing_worker = self.store.get_worker(worker_id)
        existing_bundle = (
            self._bootstrap_bundle_for(existing_worker)
            if existing_worker is not None
            else None
        )
        parallel_clean_room = bool(
            isinstance(existing_bundle, dict)
            and str(existing_bundle.get("execution_policy") or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        )
        if any(
            (
                parallel_clean_room,
                str((existing_worker or {}).get("state") or "") == "paused",
                bool(str((existing_worker or {}).get("work_stop_id") or "")),
                run_local_bundle is not None,
                bool(str(idempotency_key or "").strip()),
                bool(str(provider_request_id or "").strip()),
                not resume_paused_worker,
                origin_trace is not None,
                continuation_contract is not None,
                continuation_context is not None,
            )
        ):
            return self._assign_run_parallel(
                worker_id,
                instruction,
                event_type=event_type,
                runtime_bundle=runtime_bundle,
                run_local_bundle=run_local_bundle,
                start_processor=start_processor,
                idempotency_key=idempotency_key,
                provider_request_id=provider_request_id,
                resume_paused_worker=resume_paused_worker,
                origin_trace=origin_trace,
                continuation_contract=continuation_contract,
                continuation_context=continuation_context,
            )
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        effective_worker = dict(worker)
        effective_bundle_json = ""
        if runtime_bundle is not None:
            effective_bundle_json = json.dumps(
                merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle) or {},
                sort_keys=True,
                separators=(",", ":"),
            )
            effective_worker["bootstrap_bundle_json"] = effective_bundle_json
        self._ensure_allowed_ai(effective_worker)
        self._ensure_dispatch_provider_ready(effective_worker)
        provider_account_fence = self._provider_dispatch_fence(effective_worker)
        self._ensure_runtime_available(
            str(worker.get("profile") or ""),
            str(worker.get("execution_mode") or "docker"),
        )
        worker = self._refresh_worker_model_for_profile(worker)
        allowed_ai_admission = self._ensure_allowed_ai(worker)
        resumed = worker["state"] == "paused"
        try:
            run = self.store.create_run(
                worker_id,
                worker["project_id"],
                instruction,
                state="queued",
                resume_paused=True,
                provider_account_fence=provider_account_fence,
                bootstrap_bundle_json=(
                    effective_bundle_json if runtime_bundle is not None else None
                ),
            )
        except ProviderAccountBusyStoreError as exc:
            raise self._provider_account_busy_conflict(exc) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc
        self._record_run_allowed_ai_admission(
            run,
            allowed_ai_admission,
            worker=worker,
        )
        if runtime_bundle is not None:
            worker = self.store.get_worker(worker_id) or worker
        if resumed:
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.resumed",
                "Worker resume queued for the next run",
            )
            worker = self.store.get_worker(worker_id) or worker
        self.store.add_event(worker["project_id"], worker_id, run["run_id"], event_type, instruction)
        self._emit_callback(worker, event_type, run=run, message=instruction)
        if start_processor:
            self._ensure_worker_processor(worker_id)
        return run

    def reconcile_restart_authority_backlog_once(self) -> list[str]:
        """Unblock only restart-orphaned conversation turns and their siblings."""

        worker_ids = self.store.reconcile_restart_authority_blocked_workers()
        for worker_id in worker_ids:
            self._ensure_worker_processor(worker_id)
        return worker_ids

    def start_assigned_run(self, worker_id: str) -> None:
        """Start processing after an external owner has durably attached a queued run."""

        self._ensure_worker_processor(worker_id)

    def verify_action_capability(self, capability: str) -> dict[str, object]:
        unverified = unverified_run_action_claims(capability)
        worker_id = str(unverified["workerId"])
        run_id = str(unverified["runId"])
        worker = self.store.get_worker(worker_id)
        run = self.store.get_run(run_id)
        if not worker or not run:
            raise RunActionError(
                "capability_invalid",
                "The action capability is invalid.",
                status_code=401,
            )
        callbacks = self._callback_config_for(worker)
        secret = str(callbacks.get("hmac_secret") or callbacks.get("secret") or "")
        claims = verify_run_action_capability(capability, secret=secret)
        if (
            str(worker.get("project_id") or "") != str(claims["projectId"])
            or str(run.get("project_id") or "") != str(claims["projectId"])
            or str(run.get("worker_id") or "") != worker_id
            or str(worker.get("tenant_id") or "") != str(claims["tenantId"])
            or str(run.get("tenant_id") or "") != str(claims["tenantId"])
            or str(worker.get("owner_id") or "") != str(claims["ownerId"])
        ):
            raise RunActionError(
                "capability_scope_mismatch",
                "The action capability does not match this workspace run.",
                status_code=403,
            )
        return claims

    def execute_run_action(
        self,
        claims: dict[str, object],
        *,
        capability_id: str,
        action: str,
        project_id: str,
        worker_id: str,
        run_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        request_scope = {
            "capabilityId": capability_id,
            "action": action,
            "projectId": project_id,
            "workerId": worker_id,
            "runId": run_id,
        }
        if any(str(claims.get(key) or "") != str(value or "") for key, value in request_scope.items()):
            raise RunActionError(
                "capability_scope_mismatch",
                "The action request does not match its capability.",
                status_code=403,
            )
        tenant_id = str(claims["tenantId"])
        owner_id = str(claims["ownerId"])
        worker = self.require_worker(worker_id)
        if str(worker.get("state") or "") in CLOSED_WORKER_STATES:
            raise RunActionError("worker_ended", "The workspace has ended.", status_code=409)

        if action == "retry":
            self._ensure_execution_allowed(worker)
            self._reserved_runtime_preflight(
                str(worker.get("profile") or ""),
                str(worker.get("execution_mode") or "docker"),
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
                lane=self._trusted_run_lane(worker),
                worker=worker,
            )
            source_run = self.require_run(run_id)
            retry_context = build_workspace_continuation_context(
                previous_run=source_run
            )
            instruction = continuation_instruction(
                previous_run=source_run,
                continuation_context=retry_context,
            )
            result = self.store.create_retry_run_action(
                capability_id=capability_id,
                idempotency_key=idempotency_key,
                project_id=project_id,
                worker_id=worker_id,
                source_run_id=run_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                instruction=instruction,
                continuation_context=retry_context,
            )
            new_run = dict(result["run"])
            current_worker = self.store.get_worker(worker_id) or worker
            if current_worker.get("state") == "paused":
                self.store.update_worker_state(worker_id, "starting", last_error="")
                self.store.add_event(
                    project_id,
                    worker_id,
                    None,
                    "worker.resumed",
                    "Worker resume queued for the next run",
                )
            if not result["idempotent_replay"]:
                self.store.add_event(project_id, worker_id, new_run["run_id"], "run.queued", instruction)
                self._emit_callback(
                    self.store.get_worker(worker_id) or worker,
                    "run.queued",
                    run=new_run,
                    message=instruction,
                )
            self._ensure_worker_processor(worker_id)
            return {
                "version": 1,
                "status": "queued",
                "action": "retry",
                "projectId": project_id,
                "workerId": worker_id,
                "sourceRunId": run_id,
                "newRun": {
                    "projectId": project_id,
                    "workerId": worker_id,
                    "runId": str(new_run["run_id"]),
                },
                "confirmationPending": False,
                "idempotentReplay": bool(result["idempotent_replay"]),
            }

        if action != "cancel":
            raise RunActionError("action_invalid", "The requested action is invalid.", status_code=400)
        result = self.store.reserve_cancel_run_action(
            capability_id=capability_id,
            idempotency_key=idempotency_key,
            project_id=project_id,
            worker_id=worker_id,
            source_run_id=run_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if result["should_execute"]:
            try:
                stop_result = self.stop_run(worker_id, run_id)
            except Exception as exc:
                self.store.update_run_action_result(
                    capability_id,
                    status="failed",
                    result_code="owner_interrupt_failed",
                )
                raise RunActionError(
                    "cancellation_not_accepted",
                    "The workspace did not accept the cancellation request.",
                    status_code=503,
                ) from exc
            if stop_result.get("termination_error"):
                self.store.update_run_action_result(
                    capability_id,
                    status="failed",
                    result_code="owner_interrupt_failed",
                )
                raise RunActionError(
                    "cancellation_not_accepted",
                    "The workspace did not accept the cancellation request.",
                    status_code=503,
                )
            if bool(stop_result.get("confirmation_pending")):
                return {
                    "version": 1,
                    "status": "pending",
                    "action": "cancel",
                    "projectId": project_id,
                    "workerId": worker_id,
                    "sourceRunId": run_id,
                    "newRun": None,
                    "confirmationPending": True,
                    "idempotentReplay": bool(result["idempotent_replay"]),
                }
            settled = self.store.get_run(run_id)
            settled_state = str((settled or {}).get("state") or "")
            if settled_state != "cancelled":
                result_code = "run_already_completed" if settled_state == "completed" else "run_not_active"
                self.store.update_run_action_result(
                    capability_id,
                    status="conflict",
                    result_code=result_code,
                )
                raise RunActionError(
                    result_code,
                    "The run completed before cancellation could be accepted."
                    if settled_state == "completed"
                    else "The run is no longer active.",
                    status_code=409,
                    details={"state": settled_state},
                )
            self.store.update_run_action_result(
                capability_id,
                status="accepted",
                result_code="cancellation_confirmed",
            )
        action_record = self.store.get_run_action(capability_id) or result["action"]
        accepted = str(action_record.get("status") or "") == "accepted"
        return {
            "version": 1,
            "status": "accepted" if accepted else "pending",
            "action": "cancel",
            "projectId": project_id,
            "workerId": worker_id,
            "sourceRunId": run_id,
            "newRun": None,
            "confirmationPending": True,
            "idempotentReplay": bool(result["idempotent_replay"]),
        }


    @staticmethod
    def _runtime_worker_for_run(worker: dict, run: dict) -> dict:
        """Overlay one run's ephemeral authority without mutating the reusable workspace."""

        raw_bundle = str(run.get("runtime_bundle_json") or "").strip()
        if not raw_bundle:
            return worker
        try:
            runtime_bundle = json.loads(raw_bundle)
        except json.JSONDecodeError as exc:
            raise ValueError("Run-scoped bootstrap authority is invalid") from exc
        if not isinstance(runtime_bundle, dict):
            raise ValueError("Run-scoped bootstrap authority is invalid")
        try:
            persistent_bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
        except json.JSONDecodeError:
            persistent_bundle = {}
        if not isinstance(persistent_bundle, dict):
            persistent_bundle = {}
        runtime_worker = dict(worker)
        runtime_worker["bootstrap_bundle_json"] = json.dumps(
            merge_bootstrap_bundle(persistent_bundle, runtime_bundle) or {},
            sort_keys=True,
            separators=(",", ":"),
        )
        return runtime_worker

    def record_launch_failed(self, worker_id: str, reason: str) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        self.store.cancel_pending_runs(worker_id, error_text=reason, state="failed")
        updated = self.store.update_worker(worker_id, state="failed", last_error=reason)
        self.store.add_event(worker["project_id"], worker_id, None, "worker.launch_failed", reason)
        return updated or worker

    def send_message(self, worker_id: str, message: str) -> dict:
        worker = self.require_worker(worker_id)
        previous_run = self.store.get_active_run(worker_id)
        if previous_run is None:
            last_run_id = str(worker.get("last_run_id") or "").strip()
            previous_run = self.store.get_run(last_run_id) if last_run_id else None
        continuation_context = (
            build_workspace_continuation_context(
                previous_run=previous_run,
                continuation_goal=message,
            )
            if previous_run is not None
            else None
        )
        instruction = (
            continuation_instruction(
                previous_run=previous_run,
                continuation_context=continuation_context,
            )
            if previous_run is not None
            else str(message or "").strip()
        )
        return self.assign_run(
            worker_id,
            instruction,
            event_type="worker.message",
            continuation_context=continuation_context,
        )

    def _steer_worker_parallel(
        self,
        worker_id: str,
        message: str,
        *,
        run_id: str = "",
        idempotency_key: str | None = None,
        action_use_id: str = "",
        _prepared_instruction: str = "",
        _replacement_run_id: str = "",
        origin_trace: dict[str, object] | None = None,
        continuation_contract: dict[str, object] | None = None,
    ) -> dict:
        instruction = str(_prepared_instruction or "") or self._instruction_for_steer(
            message
        )
        normalized_key = str(idempotency_key or uuid.uuid4().hex).strip()
        replacement_run_id = str(_replacement_run_id or "") or self._idempotent_run_id(
            worker_id, normalized_key
        )
        existing = self.store.get_run(replacement_run_id)
        pending_worker = self.store.get_worker(worker_id) or {}
        pending_same_steer = bool(
            str(pending_worker.get("compute_release_kind") or "") == "steer_run"
            and str(
                pending_worker.get("compute_release_replacement_run_id") or ""
            )
            == replacement_run_id
        )
        if existing:
            if (
                str(existing.get("worker_id") or "") != worker_id
                or str(existing.get("instruction") or "") != instruction
            ):
                raise ValueError(
                    "GlassHive idempotency key was reused with a different steer"
                )
            if not pending_same_steer:
                return existing

        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            admission = worker.get("_allowed_ai_admission")
            if self.allowed_ai_policy is not None and not isinstance(admission, dict):
                raise RuntimeErrorBase("Steer replacement has no Allowed AI admission")
            if pending_same_steer and existing and isinstance(admission, dict):
                persisted_admission = existing.get("allowed_ai_admission")
                if not isinstance(persisted_admission, dict) or not persisted_admission:
                    raise RuntimeErrorBase(
                        "Steer replacement has no immutable Allowed AI admission"
                    )
                admission = persisted_admission
            persisted_target_run_id = str(
                worker.get("compute_release_target_run_id") or ""
            )
            target = (
                self.require_run(run_id)
                if str(run_id or "").strip()
                else self.require_run(persisted_target_run_id)
                if pending_same_steer and persisted_target_run_id
                else self.store.get_active_run(worker_id)
                or self.store.get_controllable_run(worker_id)
            )
            if (
                not target
                or str(target.get("worker_id") or "") != worker_id
                or str(target.get("state") or "")
                not in {"queued", "running", "settling"}
            ):
                raise RuntimeErrorBase("No exact active run is available to steer")
            target_state = str(target.get("state") or "")
            target_run_id = str(target["run_id"])
            continuation_context = build_workspace_continuation_context(
                previous_run=target,
                continuation_goal=message,
            )
            runtime_worker = worker
            if target_state != "queued":
                runtime_worker = self._require_confirmed_host_control_identity(
                    worker, target_run_id
                )
            queued_at = str((existing or {}).get("queued_at") or utc_now())
            replacement_data: dict[str, object] = {
                "run_id": replacement_run_id,
                "worker_id": worker_id,
                "project_id": str(worker["project_id"]),
                "tenant_id": str(worker.get("tenant_id") or "local"),
                "instruction": instruction,
                "state": "queued",
                "queued_at": queued_at,
                "started_at": None,
                "ended_at": None,
                "output_text": "",
                "error_text": "",
                "failure_class": "",
                "failure_retryable": 0,
                "failure_structured": 0,
                "failure_user_message": "",
                "failure_recommended_recovery": "",
                "failure_diagnostic_summary": "",
                "retry_after": None,
                "retry_attempts": 0,
                "last_retry_class": "",
                "native_session_id": "",
                "native_capabilities_json": "{}",
                "native_child_summary_json": "{}",
                "allowed_ai_admission_json": self._allowed_ai_snapshot_json(admission),
                "_origin_trace": origin_trace,
                "continuation_contract_json": json.dumps(
                    (
                        {**continuation_contract, "run_id": replacement_run_id}
                        if isinstance(continuation_contract, dict)
                        else {}
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "continuation_context_json": json.dumps(
                    continuation_context,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            claim = self._claim_exact_run_control(
                worker,
                target,
                kind="steer_run",
                replacement_run=replacement_data,
                action_use_id=action_use_id,
            )
            claimed_worker = dict(claim.get("worker") or worker)
            replacement = self.store.get_run(replacement_run_id)
            if replacement is None:
                raise RuntimeErrorBase("Steer replacement was not durably reserved")
            if (
                isinstance(admission, dict)
                and admission
                and not replacement.get("execution_binding")
            ):
                self._record_run_allowed_ai_admission(
                    replacement,
                    admission,
                    worker=claimed_worker,
                    origin_trace=origin_trace,
                )
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            info = None
            runtime_already_confirmed = self.store.worker_control_runtime_proof_matches(
                claimed_worker
            )
            if target_state != "queued" and not runtime_already_confirmed:
                if str(worker.get("execution_mode") or "docker") != "host":
                    runtime_worker = self._worker_with_host_lease(
                        claimed_worker, target_run_id
                    )
                else:
                    runtime_worker = {**runtime_worker, **claimed_worker}
                runtime_worker = self._require_claimed_container_generation(
                    runtime_worker
                )
                try:
                    info = self.runtime.interrupt_worker(
                        runtime_worker, run_id=target_run_id
                    )
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(runtime_worker)
                if not self._runtime_control_info_is_confirmed(info):
                    raise RuntimeErrorBase(
                        "Runtime steer did not confirm the exact process stopped"
                    )
                if not self.store.confirm_worker_control_runtime_effect(
                    worker_id,
                    token,
                    epoch,
                    kind="steer_run",
                    target_run_id=target_run_id,
                ):
                    raise RuntimeErrorBase(
                        "Steer runtime proof lost durable lifecycle ownership"
                    )
                self._invalidate_worker_processor(worker_id)
            operation = self.store.finalize_worker_steer_claim(
                worker_id,
                token,
                epoch,
                target_run_id=target_run_id,
                target_expected_state=target_state,
                replacement_run_id=replacement_run_id,
                replacement_instruction=instruction,
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
            )
            if not operation:
                raise RuntimeErrorBase(
                    "Steer lost the exact run lifecycle generation before replacement"
                )
            target_run = dict(operation.get("target_run") or target)
            replacement = dict(operation.get("replacement_run") or {})
            if action_use_id:
                self.store.checkpoint_active_work_action(
                    action_use_id,
                    "source_interrupted",
                    executor_id=self._executor_id,
                )
        self._replay_pending_lifecycle_effects()
        if bool(operation.get("terminal_won")):
            return {
                **target_run,
                "_control_outcome": "terminal_won",
                "_control_run": target_run,
            }
        self._ensure_worker_processor(worker_id)
        return replacement

    def native_worker_control(self, worker_id: str, *, run_id: str = "", attempt_id: str = "", action: str | None = None, payload: dict | None = None) -> dict:
        # The public caller performs owner authorization; this fence binds every
        # native control to the current durable run and attempt.
        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            if action is not None:
                self._ensure_execution_allowed(worker)
            run = self.store.get_active_run(worker_id)
            if not run or run.get("state") != "running":
                raise ValueError("There is no running native turn")
            current_run = str(run.get("run_id") or "")
            current_attempt = str(run.get("active_attempt_id") or "")
            if not current_attempt or (action is not None and (run_id != current_run or attempt_id != current_attempt)):
                raise ValueError("Native control targets a stale run attempt")
            runtime_worker = self._runtime_worker_for_run(worker, run)
            if action is None:
                return self.runtime.native_control_state(runtime_worker, run_id=current_run, attempt_id=current_attempt)
            return self.runtime.native_control(runtime_worker, run_id=current_run, attempt_id=current_attempt, action=action, payload=payload)

    def steer_worker(
        self,
        worker_id: str,
        message: str,
        *,
        run_id: str = "",
        idempotency_key: str | None = None,
        action_use_id: str = "",
        _prepared_instruction: str = "",
        _replacement_run_id: str = "",
        origin_trace: dict[str, object] | None = None,
        continuation_contract: dict[str, object] | None = None,
    ) -> dict:
        if run_id or idempotency_key or action_use_id:
            return self._steer_worker_parallel(
                worker_id,
                message,
                run_id=run_id,
                idempotency_key=idempotency_key,
                action_use_id=action_use_id,
                _prepared_instruction=_prepared_instruction,
                _replacement_run_id=_replacement_run_id,
                origin_trace=origin_trace,
                continuation_contract=continuation_contract,
            )
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        # Steering replaces the active run. Prove the replacement route before
        # interrupting useful work so missing deployment credentials are a
        # fail-closed, non-mutating error.
        self._ensure_dispatch_provider_ready(worker)
        active_run = self.store.get_active_run(worker_id)
        if active_run:
            interrupted = self.interrupt_worker(worker_id)
            worker = self.store.get_worker(worker_id) or interrupted or worker
            self.store.add_event(
                worker["project_id"],
                worker_id,
                active_run["run_id"],
                "worker.steer",
                "Active run interrupted so the workspace can follow the new steer instruction.",
            )
        instruction = self._instruction_for_steer(message)
        return self.assign_run(worker_id, instruction, event_type="worker.steer")

    def desktop_action(
        self,
        worker_id: str,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        if not hasattr(self.runtime, "desktop_action"):
            raise RuntimeErrorBase("Desktop actions are not supported by the configured runtime")
        active_run = self.store.get_active_run(worker_id)
        effective_run_id = str(run_id or (active_run or {}).get("run_id") or "").strip() or None
        if worker.get("state") != "running":
            self.store.update_worker_state(worker_id, "starting", last_error="")
        try:
            launched = self.runtime.desktop_action(
                worker,
                action,
                url=url,
                run_id=effective_run_id,
            )
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            launched = self.runtime.desktop_action(worker, action, url=url)
        except Exception as exc:
            self.store.update_worker(worker_id, state=str(worker.get("state") or "failed"), last_error=str(exc))
            raise
        self._reject_closed_after_runtime_activity(
            worker_id,
            fallback_worker=worker,
            active_run_id=str((active_run or {}).get("run_id") or ""),
            context="a desktop action",
        )
        target_state = "running" if active_run else "ready"
        self._refresh_runtime_info(worker_id, state=target_state, last_error="")
        self.store.add_event(
            worker["project_id"],
            worker_id,
            active_run["run_id"] if active_run else None,
            "worker.desktop_action",
            f"{action}: {launched.get('notes') or launched.get('status') or 'launched'}",
        )
        return launched

    def _pause_worker_parallel(
        self,
        worker_id: str,
        *,
        run_id: str = "",
        action_use_id: str = "",
    ) -> dict:
        if not str(run_id or "").strip() and not self.store.get_controllable_run(
            worker_id
        ):
            return self._pause_worker_without_run(worker_id)
        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            active_run = (
                self.require_run(run_id)
                if str(run_id or "").strip()
                else self.store.get_active_run(worker_id)
                or self.store.get_controllable_run(worker_id)
            )
            if active_run and str(active_run.get("worker_id") or "") != worker_id:
                raise RuntimeError("active_work_run_scope_mismatch")
            active_state = str((active_run or {}).get("state") or "")
            if active_state == "paused":
                return (
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    or worker
                )
            if not active_run or active_state not in {"queued", "running", "settling"}:
                raise RuntimeErrorBase("No exact active run is available to pause")
            target_run_id = str(active_run["run_id"])
            runtime_worker = worker
            if active_state != "queued":
                runtime_worker = self._require_confirmed_host_control_identity(
                    worker, target_run_id
                )
            claim = self._claim_exact_run_control(
                worker,
                active_run,
                kind="pause_run",
                action_use_id=action_use_id,
            )
            claimed_worker = dict(claim.get("worker") or worker)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            info = None
            runtime_already_confirmed = self.store.worker_control_runtime_proof_matches(
                claimed_worker
            )
            if active_state != "queued" and not runtime_already_confirmed:
                runtime_worker = (
                    self._worker_with_host_lease(claimed_worker, target_run_id)
                    if str(worker.get("execution_mode") or "docker") != "host"
                    else {**runtime_worker, **claimed_worker}
                )
                runtime_worker = self._require_claimed_container_generation(
                    runtime_worker
                )
                info = self.runtime.pause_worker(runtime_worker)
                if not self._runtime_control_info_is_confirmed(info):
                    raise RuntimeErrorBase(
                        "Runtime pause did not confirm the exact process stopped"
                    )
                if not self.store.confirm_worker_control_runtime_effect(
                    worker_id,
                    token,
                    epoch,
                    kind="pause_run",
                    target_run_id=target_run_id,
                ):
                    raise RuntimeErrorBase(
                        "Pause runtime proof lost durable lifecycle ownership"
                    )
                if str(worker.get("execution_mode") or "docker") == "host":
                    self._invalidate_worker_processor(worker_id)
                if action_use_id:
                    self.store.checkpoint_active_work_action(
                        action_use_id,
                        "runtime_paused",
                        executor_id=self._executor_id,
                    )
            operation = self.store.finalize_worker_run_control_claim(
                worker_id,
                token,
                epoch,
                kind="pause_run",
                target_run_id=target_run_id,
                target_expected_states=(active_state,),
                target_state="paused",
                worker_state="paused",
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
                error_text="Paused by operator",
                release_lease=(
                    active_state != "queued"
                    and str(worker.get("execution_mode") or "docker") == "host"
                ),
            )
            if not operation:
                raise RuntimeErrorBase(
                    "Pause lost the exact run lifecycle generation before finalization"
                )
            updated = dict(operation.get("worker") or claimed_worker)
            paused_run = dict(operation.get("run") or active_run)
            target_transitioned = bool(operation.get("target_transitioned"))
        if not target_transitioned:
            if str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                raise ControlPlaneConflict(
                    "Workspace is closed; create a new workspace for new work"
                )
            return {
                **updated,
                "_control_outcome": "terminal_won",
                "_control_run": paused_run,
            }
        self._replay_pending_lifecycle_effects()
        self._wake_host_capacity_waiters(updated)
        return updated

    def pause_worker(
        self,
        worker_id: str,
        *,
        run_id: str = "",
        action_use_id: str = "",
    ) -> dict:
        candidate_run = (
            self.store.get_run(str(run_id))
            if str(run_id or "").strip()
            else self.store.get_active_run(worker_id)
        )
        if run_id or action_use_id or candidate_run is None:
            return self._pause_worker_parallel(
                worker_id,
                run_id=run_id or str((candidate_run or {}).get("run_id") or ""),
                action_use_id=action_use_id,
            )
        if str((candidate_run or {}).get("active_attempt_id") or ""):
            # An attempt can exist before the provider crosses the final start
            # boundary. Read its started evidence under the same lock used by
            # that boundary so Pause can still win without stranding a late
            # processor write.
            with self._runtime_start_lock(worker_id):
                candidate_run = self.store.get_active_run(worker_id)
                run_started = bool(
                    candidate_run
                    and self.store.has_run_event(
                        str(candidate_run["run_id"]), "run.started"
                    )
                )
                if not run_started:
                    self._invalidate_worker_processor(worker_id)
            if run_started:
                return self._pause_worker_parallel(
                    worker_id,
                    run_id=str((candidate_run or {}).get("run_id") or ""),
                    action_use_id=action_use_id,
                )
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        # Decide whether this is an already-started, freeze-capable run while
        # holding the same boundary lock used by the external runtime start.
        # Otherwise a start could publish run.started between this decision and
        # processor invalidation, turning a real running task into a stranded
        # pre-start pause.
        with self._runtime_start_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            active_run = self.store.get_active_run(worker_id)
            run_started = bool(
                active_run
                and self.store.has_run_event(active_run["run_id"], "run.started")
            )
            if not run_started:
                self._invalidate_worker_processor(worker_id)
        runtime_worker = {
            **worker,
            "_active_run_id": str((active_run or {}).get("run_id") or ""),
        }
        info = self.runtime.pause_worker(runtime_worker)
        paused_run = None
        terminal_won = False
        if active_run:
            paused_run = self._finalize_run_if_state(
                active_run["run_id"],
                str(active_run.get("state") or "running"),
                "paused",
                output_text=active_run.get("output_text", ""),
                error_text="Paused by operator",
                **self._terminal_generation_for_run(active_run),
            )
            if paused_run is None:
                current_run = self.store.get_run(str(active_run["run_id"])) or {}
                current_state = str(current_run.get("state") or "")
                terminal_won = current_state in TERMINAL_RUN_STATES
                if current_state == "paused":
                    paused_run = self.store.update_run(
                        str(active_run["run_id"]),
                        error_text="Paused by operator",
                    )
            if paused_run:
                self.store.finalize_schedule_for_run(
                    active_run["run_id"],
                    state="failed",
                    last_error="Paused by operator",
                )
        updated = self._apply_runtime_info(
            worker_id,
            info,
            state="ready" if terminal_won else "paused",
            last_error=worker.get("last_error") or "",
        )
        if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
            raise ControlPlaneConflict(
                "Workspace is closed; create a new workspace for new work"
            )
        self._wake_host_capacity_waiters(updated or worker)
        if terminal_won:
            return updated or worker
        self.store.add_event(worker["project_id"], worker_id, active_run["run_id"] if active_run else None, "worker.paused", "Worker paused")
        self._emit_callback(worker, "worker.paused", run=paused_run or active_run, message="Worker paused")
        return updated or worker

    def _interrupt_worker_parallel(self, worker_id: str, run_id: str | None = None) -> dict:
        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            active_run = self.store.get_active_run(worker_id)
            if run_id and (
                not active_run
                or str(active_run.get("run_id") or "") != str(run_id)
            ):
                return worker
            if not active_run or str(active_run.get("state") or "") not in {
                "running",
                "settling",
            }:
                return worker
            target_run_id = str(active_run["run_id"])
            runtime_worker = self._require_confirmed_host_control_identity(
                worker, target_run_id
            )
            try:
                claim = self._claim_exact_run_control(
                    worker, active_run, kind="interrupt_run"
                )
            except RuntimeErrorBase as exc:
                if str(exc) != "active_work_generation_changed":
                    raise
                # A concurrent status write can stale the worker snapshot after
                # the exact run was read. Retry only that same active run; a
                # newer run or competing control must remain a conflict.
                worker = self.require_worker(worker_id)
                current_run = self.store.get_active_run(worker_id)
                if (
                    not current_run
                    or str(current_run.get("run_id") or "") != target_run_id
                    or str(current_run.get("state") or "") not in {"running", "settling"}
                ):
                    raise
                active_run = current_run
                runtime_worker = self._require_confirmed_host_control_identity(
                    worker, target_run_id
                )
                claim = self._claim_exact_run_control(
                    worker, active_run, kind="interrupt_run"
                )
            claimed_worker = dict(claim.get("worker") or worker)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            if str(worker.get("execution_mode") or "docker") != "host":
                runtime_worker = self._worker_with_host_lease(
                    claimed_worker, target_run_id
                )
            else:
                runtime_worker = {**runtime_worker, **claimed_worker}
            info = None
            if not self.store.worker_control_runtime_proof_matches(claimed_worker):
                runtime_worker = self._require_claimed_container_generation(
                    runtime_worker
                )
                try:
                    info = self.runtime.interrupt_worker(
                        runtime_worker, run_id=target_run_id
                    )
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(runtime_worker)
                if not self._runtime_control_info_is_confirmed(info):
                    raise RuntimeErrorBase(
                        "Runtime interrupt did not confirm the exact process stopped"
                    )
                if not self.store.confirm_worker_control_runtime_effect(
                    worker_id,
                    token,
                    epoch,
                    kind="interrupt_run",
                    target_run_id=target_run_id,
                ):
                    raise RuntimeErrorBase(
                        "Interrupt runtime proof lost durable lifecycle ownership"
                    )
            self._invalidate_worker_processor(worker_id)
            operation = self.store.finalize_worker_run_control_claim(
                worker_id,
                token,
                epoch,
                kind="interrupt_run",
                target_run_id=target_run_id,
                target_expected_states=(str(active_run.get("state") or "running"),),
                target_state="interrupted",
                worker_state="ready",
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
                error_text="Interrupted by operator",
                release_lease=True,
            )
            if not operation:
                raise RuntimeErrorBase(
                    "Interrupt lost the exact run lifecycle generation before finalization"
                )
            updated = dict(operation.get("worker") or claimed_worker)
            finalized_run = dict(operation.get("run") or active_run)
            target_transitioned = bool(operation.get("target_transitioned"))
        if not target_transitioned:
            if str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                raise ControlPlaneConflict(
                    "Workspace is closed; create a new workspace for new work"
                )
            return {
                **updated,
                "_control_outcome": "terminal_won",
                "_control_run": finalized_run,
            }
        self._replay_pending_lifecycle_effects()
        self._wake_host_capacity_waiters(updated)
        return updated

    def interrupt_worker(self, worker_id: str, run_id: str | None = None) -> dict:
        active_run = (
            self.store.get_run(str(run_id))
            if str(run_id or "").strip()
            else self.store.get_active_run(worker_id)
        )
        if active_run is not None and str(active_run.get("active_attempt_id") or ""):
            return self._interrupt_worker_parallel(worker_id, run_id=run_id)
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        self._invalidate_worker_processor(worker_id)
        with self._runtime_start_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
        active_run = self.store.get_active_run(worker_id)
        if run_id and (not active_run or str(active_run.get("run_id") or "") != str(run_id)):
            return worker
        try:
            try:
                info = self.runtime.interrupt_worker(
                    worker,
                    run_id=str(active_run["run_id"]) if active_run else None,
                )
            except TypeError as exc:
                if "run_id" not in str(exc):
                    raise
                info = self.runtime.interrupt_worker(worker)
        except Exception as exc:
            message = public_callback_message_text(str(exc)) or "Worker interruption failed"
            # Update the error only: completion may have changed the worker state.
            self.store.update_worker(worker_id, last_error=message)
            self.store.add_event(
                worker["project_id"], worker_id,
                active_run["run_id"] if active_run else None,
                "worker.interruption_failed", message,
            )
            raise
        interrupted_run = None
        if active_run:
            interrupted_run = self.store.finalize_run_if_state(
                active_run["run_id"],
                "running",
                "interrupted",
                output_text=active_run.get("output_text", ""),
                error_text="Interrupted by operator",
            )
            if interrupted_run is None:
                current = self.store.get_worker(worker_id)
                if current and str(current.get("state") or "") in CLOSED_WORKER_STATES:
                    raise ControlPlaneConflict(
                        "Workspace is closed; create a new workspace for new work"
                    )
                current_run = self.store.get_run(active_run["run_id"])
                current_state = str((current_run or {}).get("state") or "unknown")
                self.store.add_event(
                    worker["project_id"], worker_id, active_run["run_id"],
                    "worker.interruption_not_applied",
                    f"Run reached {current_state} before interruption finalization; terminal result preserved.",
                )
                return current or worker
        updated = self._apply_runtime_info(worker_id, info, state="ready", last_error="")
        if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
            raise ControlPlaneConflict(
                "Workspace is closed; create a new workspace for new work"
            )
        self._wake_host_capacity_waiters(updated or worker)
        self.store.add_event(worker["project_id"], worker_id, active_run["run_id"] if active_run else None, "worker.interrupted", "Worker interrupted")
        self._emit_callback(
            worker,
            "worker.interrupted",
            run=interrupted_run or active_run,
            message="Worker interrupted",
        )
        if interrupted_run:
            self.store.add_event(
                worker["project_id"],
                worker_id,
                interrupted_run["run_id"],
                "run.interrupted",
                "Run interruption accepted",
            )
            self._emit_callback(
                worker,
                "run.interrupted",
                run=interrupted_run,
                message="Run interruption accepted",
            )
        return updated or worker

    def cancel_run(self, worker_id: str, run_id: str) -> dict:
        """Cancel one exact pending run without affecting a newer turn."""

        worker = self.require_worker(worker_id)
        run = self.store.get_run(run_id)
        if not run or str(run.get("worker_id") or "") != str(worker_id):
            return worker
        state = str(run.get("state") or "")
        if state in {"completed", "failed", "cancelled", "interrupted"}:
            return worker
        cancelled = None
        released_running_capacity = state in {"running", "needs_input"}
        if state == "needs_input":
            if self.store.get_active_host_run_lease_for_run(run_id):
                self._release_needs_input_compute(worker, run)
            # Reconcile only this cancelled provider turn after its compute is
            # released; stopping the whole mission would discard later turns.
            reconciled = self.store.reconcile_cancelled_provider_needs_input_runs_for_worker(
                worker_id, run_id=run_id
            )
            cancelled = next((item for item in reconciled if item["run_id"] == run_id), None)
        if state == "queued":
            cancelled = self.store.finalize_run_if_state(
                run_id,
                expected_state="queued",
                state="cancelled",
                error_text="Cancelled by provider client",
            )
            if not cancelled:
                run = self.store.get_run(run_id) or run
                state = str(run.get("state") or "")

        if state == "running":
            active_run = self.store.get_active_run(worker_id)
            if not active_run or str(active_run.get("run_id") or "") != str(run_id):
                return worker
        if state == "running" and self._requires_exact_host_control(worker):
            outcome = self._cancel_running_host_run(worker_id, active_run)
            if outcome.get("state_changed"):
                # The run left ``running`` while Stop waited for the launch flock;
                # cancel it from the state it is in now.
                return self.cancel_run(worker_id, run_id)
            # The exact cancellation CAS committed its event and callback effect
            # with terminal truth; deliver them from the durable outbox.
            self._replay_pending_lifecycle_effects()
            if outcome.get("run"):
                self.store.finalize_schedule_for_run(
                    run_id,
                    state="cancelled",
                    last_error="Cancelled by provider client",
                )
                self._wake_host_capacity_waiters(
                    self.store.get_worker(worker_id) or worker
                )
            if not outcome.get("pending") and self.store.has_queued_runs(worker_id):
                self._ensure_worker_processor(worker_id)
            return self.store.get_worker(worker_id) or worker
        elif state == "running":
            terminal_generation = self._terminal_generation_for_run(active_run)
            # Stop the processor generation before interrupting the host process. A late
            # runtime return can then never overwrite the durable cancellation with completion.
            self._invalidate_worker_processor(worker_id)
            try:
                try:
                    info = self.runtime.interrupt_worker(worker, run_id=run_id)
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(worker)
                worker = self._apply_runtime_info(
                    worker_id,
                    info,
                    state="ready",
                    last_error="",
                ) or worker
            finally:
                cancelled = self.store.finalize_run_if_state(
                    run_id,
                    expected_state="running",
                    state="cancelled",
                    output_text="",
                    error_text="Cancelled by provider client",
                    **terminal_generation,
                )

        if cancelled:
            self.store.finalize_schedule_for_run(
                run_id,
                state="cancelled",
                last_error="Cancelled by provider client",
            )
            cancelled_run = {**run, **cancelled, "state": "cancelled"}
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run_id,
                "run.cancelled",
                "Run cancelled by provider client",
            )
            self._emit_callback(
                worker,
                "run.cancelled",
                run=cancelled_run,
                message="Run cancelled by provider client",
            )
            if released_running_capacity:
                self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
        if self.store.has_queued_runs(worker_id):
            self._ensure_worker_processor(worker_id)
        return self.store.get_worker(worker_id) or worker

    def _requires_exact_host_control(self, worker: dict) -> bool:
        return bool(
            str(worker.get("execution_mode") or "docker") == "host"
            and getattr(self.runtime, "requires_run_start_identity", True) is not False
        )

    def _cancel_running_host_run(
        self, worker_id: str, active_run: dict
    ) -> dict[str, object]:
        """Cancel one exact host run only after its generation is proven stopped.

        The typed ``cancel_run`` claim is the durable Stop intent and the start
        fence: a later start confirmation is refused, and an expired claim
        re-enters this same cancellation. The proof follows the lease:

        - confirmed: signal only the exact confirmed identity;
        - reserved by this live executor with nothing published: no process
          exists, because launch publishes and confirms under this flock;
        - anything else (a published PID or start identity, unconfirmed
          termination, another executor's reservation): exact cleanup of the
          published identity or runtime absence proof. Without proof the
          lease and fence stay and the cancellation remains pending.

        The terminal CAS commits the one ``run.cancelled`` event and callback
        effect with terminal truth; claim settlement then only clears the fence.
        """

        run_id = str(active_run["run_id"])
        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            # Stop may have waited for a launch holding this flock; act only on
            # the run as it is now.
            active_run = self.store.get_run(run_id) or {}
            if (
                str(active_run.get("worker_id") or "") != str(worker_id)
                or str(active_run.get("state") or "") != "running"
            ):
                return {"run": None, "pending": False, "state_changed": True}
            claim = None
            if self._cancel_claim_pending(worker, run_id):
                # A repeated Stop resumes this executor's own live cancellation;
                # another executor's live cancellation stays its own.
                claim = self._owned_cancel_claim(worker, active_run)
                if claim is None:
                    return {"run": None, "pending": True}
            fresh_claim = False
            if claim is None:
                # A new claim, or the takeover of an expired one, refreshes the fence.
                claim = self._claim_exact_run_control(
                    worker, active_run, kind="cancel_run"
                )
                fresh_claim = not bool(claim.get("takeover"))
            claimed_worker = dict(claim.get("worker") or worker)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            terminal_generation = self._terminal_generation_for_run(active_run)
            lease = self.store.get_active_host_run_lease_for_run(run_id)
            if lease and str(lease.get("startup_state") or "") == "confirmed":
                runtime_worker = {
                    **self._require_confirmed_host_control_identity(worker, run_id),
                    **claimed_worker,
                }
                # Stop the processor generation before any signal so a late runtime
                # return can never overwrite the durable cancellation.
                self._invalidate_worker_processor(worker_id)
                try:
                    info = self.runtime.interrupt_worker(runtime_worker, run_id=run_id)
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(runtime_worker)
                if not self._runtime_control_info_is_confirmed(info):
                    raise RuntimeErrorBase(
                        "Runtime interrupt did not confirm the exact process stopped"
                    )
            elif lease and not self._host_lease_never_published(lease):
                if not self._stale_lease_generation_proven_absent(
                    claimed_worker,
                    self.store.get_run(run_id) or active_run,
                    lease,
                    absence_reader=getattr(self.runtime, "host_process_absence", None),
                ):
                    logger.warning(
                        "GlassHive run cancellation is pending until its published "
                        "startup generation is proven absent",
                        extra={"worker_id": worker_id, "run_id": run_id},
                    )
                    if fresh_claim:
                        self.store.add_event(
                            worker["project_id"],
                            worker_id,
                            run_id,
                            "run.stopping",
                            "Stop requested; waiting for proof that the started process has exited",
                        )
                    return {"run": None, "pending": True}
                self._invalidate_worker_processor(worker_id)
            else:
                self._invalidate_worker_processor(worker_id)
            if self.store.confirm_worker_control_runtime_effect(
                worker_id,
                token,
                epoch,
                kind="cancel_run",
                target_run_id=run_id,
            ) is None:
                raise RuntimeErrorBase(
                    "Run cancellation ownership changed before its proof was recorded"
                )
            cancelled = self.store.finalize_run_if_state(
                run_id,
                expected_state="running",
                state="cancelled",
                output_text="",
                error_text="Cancelled by provider client",
                cancellation_claim={
                    "token": token,
                    "epoch": epoch,
                    "message": "Run cancelled by provider client",
                },
                **terminal_generation,
            )
            settled = self.store.finalize_worker_run_control_claim(
                worker_id,
                token,
                epoch,
                kind="cancel_run",
                target_run_id=run_id,
                target_expected_states=("running",),
                target_state="cancelled",
                worker_state="ready",
                runtime_fields={},
                error_text="Cancelled by provider client",
                release_lease=True,
            )
            if settled is None:
                raise RuntimeErrorBase(
                    "Run cancellation could not settle its exact fence; it remains pending"
                )
        return {"run": cancelled, "pending": False}

    def _owned_cancel_claim(
        self, worker: dict, run: dict
    ) -> dict[str, object] | None:
        """This executor's own live cancellation claim, resumed under the worker flock."""

        if not (
            str(worker.get("compute_release_token") or "")
            and str(worker.get("compute_release_kind") or "") == "cancel_run"
            and str(worker.get("compute_release_owner") or "") == self._executor_id
            and str(worker.get("compute_release_target_run_id") or "")
            == str(run.get("run_id") or "")
            and str(worker.get("compute_release_target_started_at") or "")
            == str(run.get("started_at") or "")
        ):
            return None
        return {
            "token": str(worker["compute_release_token"]),
            "epoch": int(worker.get("compute_release_epoch") or 0),
            "worker": dict(worker),
        }

    @staticmethod
    def _cancel_claim_pending(worker: dict, run_id: str) -> bool:
        return bool(
            str(worker.get("compute_release_token") or "")
            and str(worker.get("compute_release_kind") or "") == "cancel_run"
            and str(worker.get("compute_release_target_run_id") or "") == run_id
            and str(worker.get("compute_release_expires_at") or "") > utc_now()
        )

    def _host_lease_never_published(self, lease: dict) -> bool:
        """A reservation this live executor holds with no startup identity yet."""

        return bool(
            str(lease.get("startup_state") or "") == "reserved"
            and str(lease.get("executor_id") or "") == self._executor_id
            and not int(lease.get("pid") or 0)
            and not int(lease.get("process_group") or 0)
            and not str(lease.get("process_start_identity") or "").strip()
            and not str(lease.get("startup_identity_kind") or "").strip()
            and not str(lease.get("startup_container_id") or "").strip()
            and not str(lease.get("startup_session_id") or "").strip()
        )

    def _resume_worker_parallel(
        self,
        worker_id: str,
        *,
        run_id: str = "",
        action_use_id: str = "",
    ) -> dict:
        current = self.require_worker(worker_id)
        if (
            str(current.get("compute_release_token") or "")
            and str(current.get("compute_release_expires_at") or "") > utc_now()
        ):
            raise RuntimeErrorBase("Worker compute release is in progress")
        if not str(run_id or "").strip():
            if (
                str(current.get("state") or "") == "paused"
                and not self.store.get_controllable_run(worker_id)
            ):
                return self._resume_worker_without_run(worker_id)
        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            worker = self._refresh_worker_model_for_profile(worker)
            if str(run_id or "").strip():
                paused_run = self.require_run(run_id)
            else:
                controllable_runs = self.store.list_nonterminal_runs_for_worker(
                    worker_id
                )
                paused_run = next(
                    (
                        candidate
                        for candidate in controllable_runs
                        if str(candidate.get("state") or "") == "paused"
                    ),
                    None,
                )
                if paused_run is None:
                    paused_run = (
                        self.store.get_active_run(worker_id)
                        or self.store.get_controllable_run(worker_id)
                    )
            if paused_run and str(paused_run.get("worker_id") or "") != worker_id:
                raise RuntimeError("active_work_run_scope_mismatch")
            if not paused_run or str(paused_run.get("state") or "") != "paused":
                if paused_run and str(paused_run.get("state") or "") in {
                    "queued",
                    "running",
                    "settling",
                }:
                    return worker
                updated = self._start_worker_again(
                    worker, event_type="worker.resumed", message="Worker resumed"
                )
                active_run = self.store.get_active_run(worker_id)
                if active_run:
                    return (
                        self.store.update_worker_state(
                            worker_id, "running", last_error=""
                        )
                        or updated
                    )
                self._ensure_worker_processor(worker_id)
                return updated

            target_run_id = str(paused_run["run_id"])
            try:
                claim = self._claim_exact_run_control(
                    worker,
                    paused_run,
                    kind="resume_run",
                    action_use_id=action_use_id,
                )
            except RuntimeErrorBase as exc:
                if str(exc) != "active_work_generation_changed":
                    raise
                # Queue/account status can touch a paused worker between the
                # first read and the exact lifecycle claim. Retry once with
                # fresh durable generations; the store still fences any real
                # competing operation or changed run.
                worker = self.require_worker(worker_id)
                paused_run = self.require_run(target_run_id)
                if (
                    str(worker.get("state") or "") != "paused"
                    or str(paused_run.get("worker_id") or "") != worker_id
                    or str(paused_run.get("state") or "") != "paused"
                ):
                    raise
                claim = self._claim_exact_run_control(
                    worker,
                    paused_run,
                    kind="resume_run",
                    action_use_id=action_use_id,
                )
            claimed_worker = dict(claim.get("worker") or worker)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            compute_was_released = bool(worker.get("compute_released_at"))
            info = None
            if not self.store.worker_control_runtime_proof_matches(claimed_worker):
                try:
                    info = self.runtime.ensure_worker_ready(claimed_worker)
                except Exception as exc:
                    self._restore_failed_resume_claim(
                        worker=claimed_worker,
                        token=token,
                        epoch=epoch,
                        kind="resume_run",
                        target_run_id=target_run_id,
                        startup_error=exc,
                    )
                    raise
                if not self.store.confirm_worker_control_runtime_effect(
                    worker_id,
                    token,
                    epoch,
                    kind="resume_run",
                    target_run_id=target_run_id,
                ):
                    raise RuntimeErrorBase(
                        "Resume startup proof lost durable lifecycle ownership"
                    )
            captured_container_id = str(
                claimed_worker.get("compute_release_container_id") or ""
            ).strip()
            current_container_id = (
                self._runtime_compute_container_id(claimed_worker)
                if str(worker.get("execution_mode") or "docker") == "docker"
                else ""
            )
            external_runtime_requires_identity = bool(
                getattr(self.runtime, "requires_run_start_identity", False)
            )
            exact_paused_generation_resumed = bool(
                not external_runtime_requires_identity
                or (
                    captured_container_id
                    and current_container_id == captured_container_id
                )
            )
            resume_state = (
                "queued"
                if compute_was_released
                or str(worker.get("execution_mode") or "docker") == "host"
                or not paused_run.get("started_at")
                or not exact_paused_generation_resumed
                else "running"
            )
            operation = self.store.finalize_worker_run_control_claim(
                worker_id,
                token,
                epoch,
                kind="resume_run",
                target_run_id=target_run_id,
                target_expected_states=("paused",),
                target_state=resume_state,
                worker_state="starting" if resume_state == "queued" else "running",
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
                error_text="",
                release_lease=False,
            )
            if not operation:
                raise RuntimeErrorBase(
                    "Resume lost the exact run lifecycle generation before finalization"
                )
            updated = dict(operation.get("worker") or claimed_worker)
            resumed_run = dict(operation.get("run") or paused_run)
            target_transitioned = bool(operation.get("target_transitioned"))
            if action_use_id:
                self.store.checkpoint_active_work_action(
                    action_use_id,
                    "runtime_resumed",
                    executor_id=self._executor_id,
                )
        if not target_transitioned:
            return {
                **updated,
                "_control_outcome": "terminal_won",
                "_control_run": resumed_run,
            }
        self._replay_pending_lifecycle_effects()
        if resume_state == "queued":
            self._ensure_worker_processor(worker_id)
        return updated

    def resume_worker(
        self,
        worker_id: str,
        *,
        run_id: str = "",
        action_use_id: str = "",
    ) -> dict:
        current_worker = self.store.get_worker(worker_id) or {}
        paused_run = next(
            (
                candidate
                for candidate in self.store.list_nonterminal_runs_for_worker(
                    worker_id
                )
                if str(candidate.get("state") or "") == "paused"
            ),
            None,
        )
        if (
            run_id
            or action_use_id
            or paused_run is not None
            or (
                str(current_worker.get("state") or "") == "paused"
                and self.store.has_active_operator_pause(worker_id)
            )
        ):
            return self._resume_worker_parallel(
                worker_id,
                run_id=run_id or str((paused_run or {}).get("run_id") or ""),
                action_use_id=action_use_id,
            )
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        self._ensure_dispatch_provider_ready(worker)
        worker = self._refresh_worker_model_for_profile(worker)
        updated = self._start_worker_again(worker, event_type="worker.resumed", message="Worker resumed")
        active_run = self.store.get_active_run(worker_id)
        if active_run:
            refreshed = self.store.update_worker_state(worker_id, "running", last_error="")
            return refreshed or updated
        else:
            self._ensure_worker_processor(worker_id)
        return updated

    def terminate_worker(self, worker_id: str, *, _reclaim_existing: bool = False) -> dict:
        self._invalidate_worker_processor(worker_id)
        observed = self.require_worker(worker_id)
        if (
            not _reclaim_existing
            and str(observed.get("state") or "") in {"terminating", "terminated"}
        ):
            return observed
        with self._runtime_start_lock(worker_id):
            return self._terminate_worker_with_start_fence(
                worker_id,
                reclaim_existing=_reclaim_existing,
            )

    def _terminate_worker_with_start_fence(
        self,
        worker_id: str,
        *,
        reclaim_existing: bool,
    ) -> dict:
        worker = self.require_worker(worker_id)
        claimed_worker, owns_termination = self.store.begin_worker_termination(worker_id)
        worker = claimed_worker or worker
        if not owns_termination and not (
            reclaim_existing and str(worker.get("state") or "") == "terminating"
        ):
            return worker
        active_run = self.store.get_active_run(worker_id)
        runtime_worker = {
            **worker,
            "_active_run_id": str((active_run or {}).get("run_id") or ""),
        }
        self.store.cancel_pending_runs(worker_id, error_text="Worker terminated by operator", state="cancelled")
        try:
            info = self.runtime.terminate_worker(runtime_worker)
            if info.pid:
                raise RuntimeError(f"Worker compute is still active after termination (pid={info.pid})")
        except Exception as exc:
            logger.exception(
                "Worker compute termination failed for %s", worker_id
            )
            message = public_callback_message_text(str(exc)) or "Worker compute termination failed"
            self.store.fail_worker_termination(worker_id, message)
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.termination_failed",
                message,
            )
            raise
        # Paused/admitted generations and their leases remain fenced until the
        # runtime has confirmed the exact worker compute is gone.
        self.store.cancel_pending_runs(
            worker_id,
            error_text="Worker terminated by operator",
            state="cancelled",
            compute_terminated=True,
        )
        try:
            self._deactivate_delegated_schedules_for_closed_worker(worker)
        except Exception as exc:
            logger.exception(
                "Delegated schedule cleanup failed during worker termination for %s",
                worker_id,
            )
            message = public_callback_message_text(str(exc)) or "Delegated schedule cleanup failed"
            self.store.fail_worker_termination(worker_id, message)
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.termination_failed",
                message,
            )
            raise
        updated = self.store.complete_worker_termination(
            worker_id,
            runtime=info.runtime,
            model=info.model,
            gateway_url=info.gateway_url,
            gateway_port=info.gateway_port,
            gateway_token=info.gateway_token,
            session_key=info.session_key,
            state_dir=info.state_dir,
            workspace_dir=info.workspace_dir,
            pid=info.pid,
            takeover_url=f"/ui/workers/{worker_id}",
            control_url=f"/ui/workers/{worker_id}",
            compute_released_at=utc_now(),
        )
        if updated and str(updated.get("state") or "") == "termination_failed":
            raise RuntimeErrorBase("Workspace close needs attention before cleanup can complete")
        # A verified last-member close can release its idle container now.  The
        # runtime rechecks every durable member, lease, process and generation;
        # failure to prove idleness leaves the box for normal capacity recovery.
        release_idle_box = getattr(self.runtime, "release_idle_workspace_box", None)
        if callable(release_idle_box):
            try:
                release_idle_box(updated or worker)
            except Exception as exc:
                logger.warning(
                    "Idle workspace release after close could not be confirmed for %s: %s",
                    worker_id,
                    type(exc).__name__,
                )
        self._wake_host_capacity_waiters(updated or worker)
        self._replay_pending_lifecycle_effects()
        return updated or worker

    def reconcile_all_workers(self) -> None:
        self.store.reconcile_invalid_running_runs()
        self.reconcile_terminal_artifact_observations()
        for worker in self.store.list_all_workers():
            if self._shutdown_event.is_set():
                return
            try:
                self._reconcile_worker_row(worker)
            except Exception as exc:
                worker_id = str(worker.get("worker_id") or "")
                project_id = str(worker.get("project_id") or "")
                logger.warning("Failed to reconcile GlassHive worker %s", worker_id, exc_info=True)
                self.store.add_event(
                    project_id,
                    worker_id,
                    None,
                    "worker.reconcile_failed",
                    public_callback_message_text(str(exc)) or "Worker reconcile failed",
                )

    def _local_processor_owns(self, worker_id: str) -> bool:
        with self._processors_lock:
            return worker_id in self._active_processors

    def _reconcile_worker_row(self, worker: dict) -> None:
        if str(worker.get("compute_release_token") or ""):
            return
        if worker["state"] == "terminating":
            self.terminate_worker(str(worker["worker_id"]), _reclaim_existing=True)
            return
        if worker["state"] == "termination_failed":
            self.terminate_worker(str(worker["worker_id"]))
            return
        active_run = self.store.get_active_run(worker["worker_id"])
        worker_state = str(worker.get("state") or "")
        automatic_retry_pending = (
            not active_run
            and worker_state
            not in {"needs_input", "stopping", "terminated"}
            and (
                self.store.has_queued_capacity_retry(str(worker["worker_id"]))
                or self.store.has_queued_running_invariant_retry(
                    str(worker["worker_id"])
                )
            )
        )
        if automatic_retry_pending:
            reconciled = self.store.reconcile_automatic_retry_worker(
                str(worker["worker_id"])
            )
            if reconciled is not None:
                # Reconciliation only repairs the durable projection. The normal
                # scheduler owns the later claim/start transition; continuing
                # here can immediately move a capacity-queued worker to
                # ``starting`` and erase the truthful recovered ``ready`` state.
                return
        if worker["state"] in {"terminated", "failed"}:
            self._reconcile_terminated_worker_compute(worker)
            return
        # A local processor owns provider completion and its final durable CAS. Reconciliation
        # must not parse the same just-finished transcript concurrently; that race previously
        # applied mission evidence rules to a valid conversation result and replaced it with a
        # false `glasshive_evidence_check_failed` terminal state.
        if active_run and self._local_processor_owns(str(worker["worker_id"])):
            return
        if active_run:
            recovered = self._collect_completed_run(worker, active_run)
            if recovered:
                self._apply_recovered_run(worker, active_run, recovered)
                return
        if not active_run and self.store.has_queued_runs(worker["worker_id"]):
            if worker["state"] == "paused":
                with self._worker_compute_release_lock(worker["worker_id"]):
                    current = self.require_worker(worker["worker_id"])
                    if not self._can_restart_released_idle_worker(current):
                        return
                    self.store.update_worker_state(
                        worker["worker_id"], "starting", last_error=""
                    )
                self._ensure_worker_processor(worker["worker_id"])
                return
            self.store.update_worker_state(
                worker["worker_id"], "starting", last_error=""
            )
            self._ensure_worker_processor(worker["worker_id"])
            return
        if worker["state"] == "paused":
            if (
                not active_run
                and worker.get("execution_mode") == "host"
                and worker.get("last_run_id")
            ):
                self._refresh_runtime_info(
                    str(worker["worker_id"]),
                    state="paused",
                    last_error=str(worker.get("last_error") or ""),
                )
            # Paused is a durable non-terminal run state. Older/crash-split
            # rows can have the worker pause committed while the exact run is
            # still marked running/settling. Enforce the runtime pause first,
            # then repair that run with a terminal-wins CAS so resume can
            # safely restart the same durable mission.
            if active_run:
                runtime_worker = {
                    **worker,
                    "_active_run_id": str(active_run["run_id"]),
                    "_run_attempt_id": str(active_run.get("active_attempt_id") or ""),
                }
                info = self.runtime.pause_worker(runtime_worker)
                paused_run = self.store.transition_run_if_state(
                    str(active_run["run_id"]),
                    str(active_run.get("state") or "running"),
                    "paused",
                    ended_at=None,
                    error_text="",
                    retry_after=None,
                )
                if paused_run:
                    self._apply_runtime_info(
                        worker["worker_id"],
                        info,
                        state="paused",
                        last_error="",
                        touch_updated_at=False,
                    )
                    self.store.add_event(
                        worker["project_id"],
                        worker["worker_id"],
                        paused_run["run_id"],
                        "run.paused",
                        "Recovered an incomplete pause transition",
                    )
                    self._emit_callback(
                        worker,
                        "run.paused",
                        run=paused_run,
                        message="Worker pause recovered",
                    )
            return
        runtime_worker = (
            {
                **worker,
                "_active_run_id": str(active_run["run_id"]),
                "_run_attempt_id": str(active_run.get("active_attempt_id") or ""),
            }
            if active_run
            else worker
        )
        info = self.runtime.reconcile_worker(runtime_worker)
        if worker["state"] == "stopping":
            if info.pid:
                self._apply_runtime_info(
                    worker["worker_id"],
                    info,
                    state="stopping",
                    last_error=worker.get("last_error") or "",
                    touch_updated_at=False,
                )
                return
            if active_run:
                cancelled_run = self._finalize_run_if_state(
                    active_run["run_id"],
                    str(active_run.get("state") or "running"),
                    "cancelled",
                    error_text="Stopped by operator",
                    **self._terminal_generation_for_run(active_run),
                )
                if cancelled_run:
                    self.store.finalize_schedule_for_run(
                        active_run["run_id"],
                        state="cancelled",
                        last_error="Stopped by operator",
                    )
                    self.store.accept_cancel_actions_for_run(active_run["run_id"])
                    self.store.add_event(
                        worker["project_id"],
                        worker["worker_id"],
                        active_run["run_id"],
                        "run.cancelled",
                        "Run stop confirmed during reconciliation",
                    )
                    self._emit_callback(
                        worker,
                        "run.cancelled",
                        run=cancelled_run,
                        message="Run stop confirmed",
                    )
            self._apply_runtime_info(
                worker["worker_id"],
                info,
                state="ready",
                last_error="",
                touch_updated_at=False,
            )
            return
        # The host process can exit just before the local processor parses and persists its
        # successful result. In that narrow local finalization window there is no live PID, but
        # the current processor still owns the run. A foreign service instance instead discovers
        # a live owner through the durable active-session PID in the host runtime.
        if active_run and not info.pid and self._local_processor_owns(str(worker["worker_id"])):
            self._apply_runtime_info(
                worker["worker_id"],
                info,
                state=worker["state"],
                last_error=worker.get("last_error") or "",
                touch_updated_at=False,
            )
            return
        if (
            active_run
            and info.pid
            and str(active_run.get("state") or "") in {"running", "settling"}
        ):
            self._apply_runtime_info(
                worker["worker_id"],
                info,
                state="running",
                last_error=worker.get("last_error") or "",
                touch_updated_at=False,
            )
            self._ensure_surviving_run_monitor(
                str(worker["worker_id"]), str(active_run["run_id"])
            )
            return
        state = worker["state"]
        compute_released_at: object = _UNSET
        if state in {"running", "ready", "starting"}:
            if active_run and info.pid:
                state = "running"
            elif info.pid:
                state = "ready"
            else:
                state = "paused"
                # No live compute remains (for example after a host restart).
                # This is reclamation, not an operator Pause: the next queued
                # run may restart it.
                compute_released_at = utc_now()
        if not info.pid:
            if active_run:
                orphaned_run = self._finalize_run_if_state(
                    active_run["run_id"],
                    str(active_run.get("state") or "running"),
                    "interrupted",
                    error_text="Worker process was not running during reconcile",
                    **self._terminal_generation_for_run(active_run),
                    failure_class="provider_temporarily_unavailable",
                    failure_retryable=1,
                    failure_structured=1,
                    failure_user_message=(
                        "The provider worker stopped unexpectedly before completing the response."
                    ),
                    failure_recommended_recovery=(
                        "Retry the request or use the configured provider fallback."
                    ),
                    failure_diagnostic_summary=(
                        "Reconciliation found no live process for the active host run."
                    ),
                )
                if orphaned_run:
                    logger.warning(
                        "Interrupted orphaned GlassHive host run during reconciliation",
                        extra={
                            "reconciler_pid": os.getpid(),
                            "worker_id": str(worker["worker_id"]),
                            "run_id": str(active_run["run_id"]),
                        },
                    )
                    cleanup_orphaned_run = getattr(self.runtime, "cleanup_orphaned_run", None)
                    if callable(cleanup_orphaned_run):
                        try:
                            cleanup_orphaned_run(runtime_worker, str(active_run["run_id"]))
                        except Exception as exc:
                            logger.warning(
                                "Failed to clean up orphaned GlassHive host process",
                                extra={
                                    "reconciler_pid": os.getpid(),
                                    "worker_id": str(worker["worker_id"]),
                                    "run_id": str(active_run["run_id"]),
                                    "error": str(exc),
                                },
                            )
                    self.store.add_event(
                        worker["project_id"],
                        worker["worker_id"],
                        active_run["run_id"],
                        "run.orphaned",
                        "Active run interrupted because the worker process was not running",
                    )
                    self._emit_callback(
                        worker,
                        "run.interrupted",
                        run=orphaned_run,
                        message="Worker process was not running during reconcile",
                    )
        self._apply_runtime_info(
            worker["worker_id"],
            info,
            state=state,
            last_error=worker.get("last_error") or "",
            compute_released_at=compute_released_at,
            touch_updated_at=False,
        )

    def require_project(self, project_id: str) -> dict:
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError("Project not found")
        return project

    def require_worker(self, worker_id: str) -> dict:
        worker = self.store.get_worker(worker_id)
        if not worker:
            raise KeyError("Worker not found")
        if self.store.workspace_gc_claim_active(worker_id):
            raise RuntimeErrorBase("Workspace is being garbage-collected")
        return worker

    def require_run(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError("Run not found")
        return run

    def _terminal_generation_for_run(
        self,
        run: dict,
        lease: dict | None = None,
    ) -> dict[str, str]:
        """Derive a terminal fence only from one captured durable attempt."""

        attempt_id = str(run.get("active_attempt_id") or "")
        if not attempt_id:
            return {
                "expected_attempt_id": "",
                "expected_lease_id": "",
                "expected_executor_id": "",
                "expected_startup_token": "",
                "expected_runtime_invoked_at": "",
            }
        attempt = self.store.get_run_attempt(attempt_id)
        if (
            attempt is None
            or str(attempt.get("run_id") or "") != str(run.get("run_id") or "")
        ):
            return {}
        bound_lease = lease or self.store.get_host_run_lease(
            str(attempt.get("lease_id") or "")
        )
        if not str(attempt.get("lease_id") or ""):
            return {
                "expected_attempt_id": attempt_id,
                "expected_lease_id": "",
                "expected_executor_id": "",
                "expected_startup_token": "",
                "expected_runtime_invoked_at": str(
                    run.get("runtime_invoked_at") or ""
                ),
            }
        if (
            not bound_lease
            or str(bound_lease.get("run_id") or "") != str(run.get("run_id") or "")
            or str(bound_lease.get("attempt_id") or "") != attempt_id
        ):
            return {}
        return {
            "expected_attempt_id": attempt_id,
            "expected_lease_id": str(bound_lease.get("lease_id") or ""),
            "expected_executor_id": str(bound_lease.get("executor_id") or ""),
            "expected_startup_token": str(bound_lease.get("startup_token") or ""),
            "expected_runtime_invoked_at": str(run.get("runtime_invoked_at") or ""),
        }

    def _collect_completed_run(self, worker: dict, run: dict) -> dict[str, object] | None:
        if not hasattr(self.runtime, "collect_completed_run"):
            return None
        terminal_generation = self._terminal_generation_for_run(run)
        runtime_worker = {
            **worker,
            "_run_attempt_id": str(run.get("active_attempt_id") or ""),
        }
        runtime_worker["bootstrap_bundle_json"] = json.dumps(
            self._exact_provider_session_bundle_for_run(
                self._bootstrap_bundle_for(runtime_worker) or {},
                str(run["run_id"]),
            ),
            ensure_ascii=False,
        )

        def bind_generation(recovered):
            if not isinstance(recovered, dict):
                return recovered
            return {**recovered, "_terminal_generation": terminal_generation}

        try:
            return bind_generation(
                self.runtime.collect_completed_run(
                    runtime_worker,
                    run_id=run["run_id"],
                    instruction=str(run.get("instruction") or ""),
                )
            )
        except TypeError as exc:
            if "instruction" in str(exc):
                try:
                    return bind_generation(
                        self.runtime.collect_completed_run(
                            runtime_worker, run_id=run["run_id"]
                        )
                    )
                except TypeError as run_id_exc:
                    if "run_id" not in str(run_id_exc):
                        raise
                    return bind_generation(
                        self.runtime.collect_completed_run(runtime_worker)
                    )
            if "run_id" not in str(exc):
                raise
            return bind_generation(
                self.runtime.collect_completed_run(runtime_worker)
            )

    def _run_usage(self, worker: dict, run_id: str) -> dict[str, int]:
        reader = getattr(self.runtime, "run_usage", None)
        if not callable(reader):
            return {}
        try:
            value = reader(worker, run_id)
        except (OSError, TypeError, ValueError):
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def _fresh_user_artifact_deliverable(self, worker: dict, run: dict, deliverable: dict[str, object] | None) -> bool:
        if not deliverable:
            return False
        failure_class = str(run.get("failure_class") or "").strip()
        if failure_class not in {"provider_response_failed", "provider_rate_limited", "runtime_io_failed"}:
            return False
        workspace_path = Path(str(deliverable.get("workspace_path") or "").strip())
        if not workspace_path.parts:
            return False
        if not is_user_deliverable_relative_path(workspace_path):
            return False
        raw_root = str(worker.get("workspace_dir") or "").strip()
        if not raw_root:
            return False
        root = Path(raw_root)
        artifact_path = (root / workspace_path).resolve()
        try:
            artifact_path.relative_to(root.resolve())
        except ValueError:
            return False
        if not artifact_path.is_file():
            return False
        if is_unmodified_user_input(self.files.artifact_worker(worker), root / workspace_path):
            return False
        if any(part.lower() in SUPPORT_ARTIFACT_DIR_NAMES for part in workspace_path.parts[:-1]):
            return False
        if not self._looks_like_completed_user_deliverable(workspace_path, artifact_path):
            return False
        started_at = str(run.get("started_at") or run.get("queued_at") or "").strip()
        if not started_at:
            return False
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return artifact_path.stat().st_mtime >= started - 5

    def _looks_like_completed_user_deliverable(self, workspace_path: Path, artifact_path: Path) -> bool:
        suffix = artifact_path.suffix.lower()
        name = workspace_path.name.lower()
        if suffix in PROFESSIONAL_ARTIFACT_EXTENSIONS:
            return is_valid_professional_artifact(artifact_path)
        if suffix in {".html", ".htm"}:
            return True
        if suffix not in {".csv", ".json", ".md", ".tsv", ".txt"}:
            return False
        token_pattern = r"(?:^|[^a-z0-9]){}(?:[^a-z0-9]|$)"
        if re.search(token_pattern.format(r"(?:partial|draft|scratch|notes?|research|batch)"), name):
            return False
        if suffix in {".csv", ".json", ".tsv"}:
            try:
                return artifact_path.stat().st_size > 0
            except OSError:
                return False
        return bool(re.search(token_pattern.format(r"(?:final|finished|complete|completed|report|deliverable|summary|brief|workbook|deck)"), name))

    def _partial_artifact_failure_message(self, deliverable: dict[str, object], failure_message: str) -> str:
        """Name the preserved artifact without claiming the run finished.

        Only the worker's own terminal success evidence completes a run. A file that appeared
        before the provider ended the worker is delivered as a partial artifact next to the
        typed failure, so the user gets the work that exists and a truthful status.
        """

        path = str(deliverable.get("workspace_path") or deliverable.get("label") or "generated artifact").strip()
        lines = [
            f"GlassHive preserved a partial artifact before the model provider ended the worker: `{path}`.",
            "The run did not complete; review the artifact before relying on it.",
        ]
        if failure_message.strip():
            lines.append(failure_message.strip())
        return "\n".join(lines)

    def _recovered_run_lease_is_active(self, run: dict, terminal_generation: dict | None) -> bool:
        """A result bound to a released host run lease must never be applied.

        Managed shutdown releases running-dispatch leases with a typed reason so the same run is
        re-queued on restart; a result that arrives afterwards from the old execution would
        otherwise duplicate the run's external effects.
        """
        expected_lease_id = str((terminal_generation or {}).get("expected_lease_id") or "").strip()
        if not expected_lease_id:
            return True
        lease = self.store.get_host_run_lease(expected_lease_id)
        return bool(lease) and str(lease.get("status") or "") == "active"

    def _apply_recovered_run(self, worker: dict, run: dict, recovered: dict[str, object]) -> dict | None:
        worker_id = worker["worker_id"]
        terminal_generation = recovered.get("_terminal_generation")
        if not isinstance(terminal_generation, dict):
            terminal_generation = self._terminal_generation_for_run(run)
        if not self._recovered_run_lease_is_active(run, terminal_generation):
            logger.warning(
                "Ignoring a late result for run %s: its host run lease is no longer active",
                run.get("run_id"),
            )
            return None
        state = str(recovered.get("state") or "failed")
        output_text = str(recovered.get("output_text") or "")
        error_text = str(recovered.get("error_text") or "")
        failure_fields = {
            key: recovered.get(key)
            for key in (
                "failure_class",
                "failure_retryable",
                "failure_structured",
                "failure_user_message",
                "failure_recommended_recovery",
                "failure_diagnostic_summary",
            )
            if key in recovered
        }
        if state == "failed":
            self._record_provider_route_failure(
                worker, run, failure_fields, recovered
            )
        usage = recovered.get("usage") if isinstance(recovered.get("usage"), dict) else {}
        if state == "completed":
            finalized_run = self._finalize_run_if_state(
                run["run_id"],
                str(run.get("state") or "running"),
                "completed",
                output_text=output_text,
                usage=usage,
                **terminal_generation,
            )
            if not finalized_run:
                return self.store.get_worker(worker_id)
            self._clear_provider_route_health(worker, run)
            self.store.finalize_schedule_for_run(run["run_id"], state="completed")
            self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
            message = terminal_callback_message(output_text)
            full_message = terminal_callback_full_message(output_text)
            self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
            recovered_run = {**run, **finalized_run, "state": "completed", "output_text": output_text}
            refreshed_worker = self._refresh_runtime_info(worker_id, state="ready", last_error="") or self.store.get_worker(worker_id) or worker
            deliverable = self._completion_deliverable(refreshed_worker, recovered_run, output_text, error_text)
            self._promote_completed_deliverable(refreshed_worker, recovered_run, deliverable)
            self._emit_callback(
                refreshed_worker,
                "run.completed",
                run=recovered_run,
                message=message or "Run completed",
                full_message=full_message if full_message != message else "",
                deliverable=deliverable,
            )
            self._wake_host_capacity_waiters(refreshed_worker)
        else:
            recovered_run = {**run, "state": "failed", "error_text": error_text, **failure_fields}
            refreshed_worker = self._refresh_runtime_info(worker_id, state="ready", last_error=error_text) or self.store.get_worker(worker_id) or worker
            deliverable = self._completion_deliverable(refreshed_worker, recovered_run, output_text, error_text)
            # A fresh user-facing file that appeared before the provider ended the worker is
            # preserved and delivered with the typed failure. It never upgrades the run to
            # completed: only the worker's own terminal success evidence does that.
            partial_artifact = self._fresh_user_artifact_deliverable(refreshed_worker, recovered_run, deliverable)
            recovered_error = RuntimeErrorBase(
                error_text or "Structured provider quota exhausted"
            )
            if self._switch_quota_exhausted_run_to_fallback(
                refreshed_worker,
                {**run, "output_text": output_text},
                recovered_error,
                failure_fields,
            ):
                return self.store.get_worker(worker_id)
            finalized_run = self._finalize_run_if_state(
                run["run_id"],
                str(run.get("state") or "running"),
                "failed",
                output_text=output_text,
                error_text=error_text,
                usage=usage,
                **terminal_generation,
                **failure_fields,
            )
            if not finalized_run:
                return self.store.get_worker(worker_id)
            self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=error_text)
            self.store.update_worker(worker_id, state="ready", last_error=error_text, last_run_id=run["run_id"])
            self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", error_text or "Run failed")
            failure_message = runtime_failure_callback_message(failure_fields, error_text or "Run failed")
            if partial_artifact:
                self.store.add_event(
                    worker["project_id"],
                    worker_id,
                    run["run_id"],
                    "run.partial_artifact_preserved",
                    str((deliverable or {}).get("workspace_path") or "")[:TERMINAL_CALLBACK_MESSAGE_LIMIT],
                )
            self._emit_callback(
                refreshed_worker,
                "run.failed",
                run=finalized_run,
                message=failure_message,
                deliverable=deliverable,
            )
            self._wake_host_capacity_waiters(refreshed_worker)
        return self.store.get_worker(worker_id)

    def heal_worker(self, worker_id: str) -> dict | None:
        worker = self.store.get_worker(worker_id)
        if (
            not worker
            or worker.get("state") == "paused"
            or str(worker.get("state") or "") in CLOSED_WORKER_STATES
        ):
            return worker
        active_run = self.store.get_active_run(worker_id)
        if not active_run:
            if worker.get("state") in {"starting", "running"}:
                info = self.runtime.reconcile_worker(worker)
                state = "ready" if info.pid else "paused"
                return self._apply_runtime_info(worker_id, info, state=state, last_error=worker.get("last_error") or "") or worker
            return worker
        # A normal authenticated read can call heal_worker while the local queue
        # processor is still writing its exact attempt evidence. Let that
        # processor finish its durable state transition; collecting the native
        # exit marker here can falsely finalize the run before evidence lands.
        if self._local_processor_owns(worker_id):
            return worker
        recovered = self._collect_completed_run(worker, active_run)
        if not recovered:
            return worker
        self._apply_recovered_run(worker, active_run, recovered)
        self._release_host_run_lease(
            str(active_run["run_id"]), reason="healed_terminal"
        )
        with self._processors_lock:
            # Stale processors also check active membership before every state write,
            # so dropping membership here is enough to make an externally healed
            # processor stop touching worker state until a replacement generation is spawned.
            self._active_processors.discard(worker_id)
        refreshed = self.store.get_worker(worker_id)
        if (
            refreshed
            and refreshed["state"] != "paused"
            and str(refreshed["state"] or "") not in CLOSED_WORKER_STATES
            and self.store.has_queued_runs(worker_id)
        ):
            self._ensure_worker_processor(worker_id)
        return refreshed

    def _start_worker_again(self, worker: dict, event_type: str, message: str) -> dict:
        starting = self.store.update_worker_unless_gc_claimed(worker["worker_id"], state="starting")
        if starting is None:
            current = self.store.get_worker(worker["worker_id"])
            if current and str(current.get("state") or "") in CLOSED_WORKER_STATES:
                raise ControlPlaneConflict(
                    "Workspace is closed; create a new workspace for new work"
                )
            raise RuntimeErrorBase("Workspace is being garbage-collected")
        worker = starting
        try:
            info = self._ensure_worker_ready_with_lifecycle_fence(worker)
        except Exception as exc:
            updated = self.store.update_worker(worker["worker_id"], state="failed", last_error=str(exc))
            if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                self._reject_closed_after_runtime_activity(
                    worker["worker_id"],
                    fallback_worker=updated,
                    context="workspace readiness",
                )
            self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.failed", str(exc))
            return updated or worker
        return self._finalize_worker_ready_after_start(
            worker,
            info,
            event_type=event_type,
            message=message,
            context="workspace readiness",
        )

    def _apply_runtime_info(
        self,
        worker_id: str,
        info: RuntimeInfo,
        state: str,
        last_error: str,
        compute_released_at: str | None | object = _UNSET,
        *,
        touch_updated_at: bool = True,
    ) -> dict | None:
        fields = self._runtime_info_fields(worker_id, info, last_error=last_error)
        fields["state"] = state
        if compute_released_at is not _UNSET:
            fields["compute_released_at"] = compute_released_at
        return self.store.update_worker(
            worker_id,
            touch_updated_at=touch_updated_at,
            **fields,
        )

    def _bootstrap_bundle_for(self, worker: dict) -> dict | None:
        raw = str(worker.get("bootstrap_bundle_json") or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _copy_workspace_contents(self, source_worker: dict, target_worker: dict) -> dict[str, object]:
        source_root_raw = str(source_worker.get("workspace_dir") or "").strip()
        target_root_raw = str(target_worker.get("workspace_dir") or "").strip()
        if not source_root_raw or not target_root_raw:
            return {"source_state": "missing", "copied_files": 0, "skipped_items": 0}
        source_root = Path(source_root_raw)
        target_root = Path(target_root_raw)
        if target_root.is_symlink():
            raise ValueError("workspace duplicate target must not be a symlink")
        if source_root.exists():
            resolved_source = source_root.resolve(strict=True)
            resolved_target = target_root.resolve(strict=False)
            if resolved_target == resolved_source or resolved_source in resolved_target.parents:
                raise ValueError("workspace duplicate target must be separate from the source")
        files, skipped_items, source_state = _workspace_copy_plan(source_root)
        target_root.mkdir(parents=True, exist_ok=True)
        copied_files = 0
        copied_bytes = 0
        copied_targets: list[Path] = []
        max_bytes = _optional_duplicate_limit("GLASSHIVE_DUPLICATE_MAX_BYTES")
        deadline = time.monotonic() + _bounded_float_env(
            "GLASSHIVE_DUPLICATE_TIMEOUT_SECONDS",
            30.0,
            min_value=1.0,
            max_value=300.0,
        )
        try:
            for source, relative in files:
                if time.monotonic() > deadline:
                    raise ValueError("workspace duplicate copy exceeded its time limit")
                target = target_root / relative
                copied_bytes += _copy_regular_workspace_file(
                    source,
                    target,
                    source_root,
                    max_bytes=None if max_bytes is None else max_bytes - copied_bytes,
                    deadline=deadline,
                    target_root=target_root,
                )
                copied_targets.append(target)
                copied_files += 1
            if copied_targets and getattr(self, "files", None) is not None:
                self.files.copy_exact_input_versions(
                    source_worker, target_worker,
                    {path.relative_to(target_root).as_posix() for path in copied_targets},
                )
        except Exception:
            for copied_target in reversed(copied_targets):
                try:
                    copied_target.unlink()
                except FileNotFoundError:
                    pass
                parent = copied_target.parent
                while parent != target_root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            raise
        return {
            "source_state": source_state,
            "copied_files": copied_files,
            "skipped_items": skipped_items,
        }

    def _refresh_runtime_info(self, worker_id: str, state: str, last_error: str = "") -> dict | None:
        worker = self.store.get_worker(worker_id)
        if not worker:
            return None
        try:
            info = self.runtime.reconcile_worker(worker)
        except Exception:
            return worker
        return self._apply_runtime_info(worker_id, info, state=state, last_error=last_error) or worker

    def _instruction_for_message(self, message: str, *, previous_run: dict) -> str:
        context = build_workspace_continuation_context(
            previous_run=previous_run,
            continuation_goal=message,
        )
        return continuation_instruction(
            previous_run=previous_run,
            continuation_context=context,
        )

    def _instruction_for_steer(self, message: str) -> str:
        return (
            "Operator steer instruction for the current worker session.\n\n"
            "Treat this as the new highest-priority direction and continue from the current workspace state.\n\n"
            "Execution requirements:\n"
            "- Act on this steer inside the workspace immediately.\n"
            "- Do not stop at an acknowledgement, summary, or plan when the steer requires concrete action.\n"
            "- If the steer redirects or cancels earlier work, perform that interruption first, then carry out the new action.\n"
            "- Use the terminal, files, browser, and available tools as needed.\n"
            "- Remain in execution mode until the steer instruction is satisfied or a real blocker requires operator help.\n\n"
            f"{message}"
        )

    def _ensure_worker_processor(self, worker_id: str) -> None:
        worker = self.store.get_worker(worker_id) or {}
        if not worker or str(worker.get("state") or "") in {
            "paused",
            "needs_input",
            "stopping",
            "terminated",
        } or self.store.has_active_operator_pause(worker_id):
            return
        if (
            str(worker.get("compute_release_token") or "").strip()
            or self.store.has_unconfirmed_host_run_start(worker_id)
        ):
            return
        executor = (
            self.conversation_executor
            if self._trusted_run_lane(worker) == "conversation"
            else self.executor
        )
        with self._processors_lock:
            if self._shutdown_event.is_set() or worker_id in self._active_processors:
                return
            generation = self._processor_generations.get(worker_id, 0) + 1
            self._processor_generations[worker_id] = generation
            self._active_processors.add(worker_id)
        worker = self.store.get_worker(worker_id) or {}
        bundle = self._bootstrap_bundle_for(worker) or {}
        executor = (
            self.conversation_executor
            if str(bundle.get("run_mode") or "mission").strip().lower() == "conversation"
            else self.executor
        )
        executor.submit(self._process_worker_queue, worker_id, generation)

    def _wake_host_capacity_waiters(self, released_worker: dict) -> None:
        """Wake one free host CLI/auth lane without disturbing unrelated queues."""

        if str(released_worker.get("execution_mode") or "docker").strip().lower() != "host":
            return
        # Capacity checks intentionally allow the currently registered worker to
        # re-enter its own lane. Probe as a distinct waiter so a stale/held slot is
        # not mistaken for released capacity.
        capacity_probe = {
            **released_worker,
            "worker_id": f"{released_worker.get('worker_id') or 'host'}:capacity-probe",
        }
        try:
            if self._runtime_capacity_error(capacity_probe) is not None:
                return
        except Exception as exc:
            logger.warning(
                "Failed to verify released host capacity for worker %s: %s",
                released_worker.get("worker_id") or "",
                exc,
            )
            return
        bundle = self._bootstrap_bundle_for(released_worker) or {}
        run_mode = (
            "conversation"
            if str(bundle.get("run_mode") or "").strip().lower() == "conversation"
            else "mission"
        )
        try:
            waiting_worker_ids = self.store.release_host_capacity_waiters(
                profile=str(released_worker.get("profile") or "").strip(),
                execution_mode="host",
                run_mode=run_mode,
            )
        except Exception as exc:
            logger.warning(
                "Failed to release queued host-capacity waiters for worker %s: %s",
                released_worker.get("worker_id") or "",
                exc,
            )
            return
        for waiting_worker_id in waiting_worker_ids:
            self._ensure_worker_processor(waiting_worker_id)

    def _processor_is_current(self, worker_id: str, generation: int) -> bool:
        with self._processors_lock:
            return worker_id in self._active_processors and self._processor_generations.get(worker_id) == generation

    def _release_processor(self, worker_id: str, generation: int) -> bool:
        with self._processors_lock:
            if worker_id not in self._active_processors:
                return False
            if self._processor_generations.get(worker_id) != generation:
                return False
            self._active_processors.discard(worker_id)
            return True

    def _process_worker_queue_parallel(self, worker_id: str, generation: int) -> None:
        current_run: dict | None = None
        runtime_invoked = False
        preserve_start_fence = False
        terminal_generation: dict[str, str] = {}
        try:
            while True:
                current_run = None
                runtime_invoked = False
                preserve_start_fence = False
                terminal_generation = {}
                if not self._processor_is_current(worker_id, generation):
                    return
                worker = self.store.get_worker(worker_id)
                if not worker or worker["state"] in {
                    "paused",
                    "needs_input",
                    "stopping",
                    "terminated",
                }:
                    return

                queued_run = self.store.peek_next_queued_run(worker_id)
                if queued_run:
                    if not self._coordinator_retry_dispatch_ready(worker, queued_run):
                        # Another scheduler may wake this worker between the
                        # durable retry reservation and goal rebinding. Leave
                        # the run queued; the coordinator wakes it after bind.
                        return
                    capacity_error = (
                        self._runtime_capacity_error(worker)
                        or self._provider_account_capacity_error(worker, queued_run)
                    )
                    if capacity_error:
                        failure_fields = (
                            {
                                "failure_class": "host_capacity",
                                "failure_retryable": True,
                                "failure_structured": True,
                                "failure_user_message": str(capacity_error),
                                "failure_recommended_recovery": "No action is required; this work will start when the selected AI account is free.",
                                "failure_diagnostic_summary": "Selected provider account has a live lease for another run.",
                            }
                            if isinstance(capacity_error, HostCapacityError)
                            and capacity_error.capacity_class == "provider_account"
                            else None
                        )
                        self._requeue_retryable_run(
                            worker, queued_run, capacity_error, failure_fields=failure_fields
                        )
                        return
                    self._mark_run_local_grant_waiter(
                        worker, str(queued_run["run_id"])
                    )
                    try:
                        # Reserve host/resource capacity while the accepted work
                        # is still queued. Only a real execution admission may
                        # mint an immutable run attempt.
                        self._acquire_host_run_lease(worker, queued_run)
                    except HostCapacityError as exc:
                        self._clear_run_local_grant_waiter(
                            str(queued_run["run_id"])
                        )
                        self._requeue_retryable_run(
                            worker,
                            queued_run,
                            exc,
                            failure_fields=classify_runtime_error(
                                exc,
                                runtime_name=str(
                                    worker.get("profile")
                                    or worker.get("runtime")
                                    or "worker"
                                ),
                            ).as_store_fields(),
                        )
                        return

                run = self.store.claim_next_queued_run(
                    worker_id,
                    executor_id=self._executor_id,
                    lease_ttl_s=self._host_lease_ttl_s(),
                )
                if not run:
                    self._schedule_worker_retry_after(
                        worker_id,
                        self.store.next_retry_after_for_worker(worker_id),
                    )
                    if queued_run:
                        self._clear_run_local_grant_waiter(
                            str(queued_run["run_id"])
                        )
                        self._release_host_run_lease(
                            str(queued_run["run_id"]),
                            reason="preclaim_generation_lost",
                        )
                    current = self.store.get_worker(worker_id)
                    if (
                        self._processor_is_current(worker_id, generation)
                        and current
                        and current["state"] not in {
                            "paused",
                            "needs_input",
                            "stopping",
                            "terminated",
                            "failed",
                        }
                        and not self.store.get_active_run(worker_id)
                    ):
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                    return

                current_run = run
                worker = self.store.get_worker(worker_id) or worker
                if queued_run and str(queued_run.get("run_id") or "") != str(
                    run.get("run_id") or ""
                ):
                    self._clear_run_local_grant_waiter(
                        str(queued_run.get("run_id") or "")
                    )
                    self._mark_run_local_grant_waiter(
                        worker, str(run["run_id"])
                    )
                qa_claimed_stall = self._consume_local_qa(
                    "claimed_queue_stall", worker, run
                )
                if qa_claimed_stall is not None:
                    stalled = self.store.force_queue_deadline_for_local_qa(
                        str(run["run_id"]),
                        expected_state="claimed",
                        expected_generation=int(
                            run.get("queue_wait_generation") or 0
                        ),
                        expected_deadline=str(run.get("queue_deadline_at") or ""),
                        now=self._now_datetime().isoformat(),
                    )
                    self._record_local_qa_effect(
                        qa_claimed_stall,
                        "claimed_wait_moved_to_deadline"
                        if stalled is not None
                        else "claimed_wait_generation_lost",
                    )
                    return
                if self._handle_unhealthy_provider_route(worker, run):
                    self._clear_run_local_grant_waiter(str(run["run_id"]))
                    return
                try:
                    lease = self._acquire_host_run_lease(worker, run)
                    if not lease:
                        raise RuntimeErrorBase(
                            "GlassHive could not reserve the exact run startup generation."
                        )
                    terminal_generation = self._terminal_generation_for_run(
                        run, lease
                    )
                    # The startup lease is the host generation. Keep its raw CAS
                    # token local; this digest already fences active-session identity.
                    startup_token = str(lease.get("startup_token") or "")
                    startup_token_digest = hashlib.sha256(
                        startup_token.encode("utf-8")
                    ).hexdigest()
                    authority_context: dict[str, str] = {}
                    if (
                        self._deferred_capability_authorization(worker) is not None
                        or self._prompt_workbench_scheduled_authority(worker)
                        is not None
                    ):
                        if str(worker.get("execution_mode") or "docker") == "host":
                            if not startup_token:
                                raise BrokerAdmissionError(
                                    "broker_admission_generation_unavailable",
                                    "The exact mission startup lease is unavailable.",
                                    retryable=True,
                                )
                            authority_context = {
                                "host_startup_lease_id": startup_token_digest
                            }
                        else:
                            prepare_authority = getattr(
                                self.runtime, "prepare_run_authority_context", None
                            )
                            if not callable(prepare_authority):
                                raise BrokerAdmissionError(
                                    "broker_admission_generation_unavailable",
                                    "The exact mission container generation is unavailable.",
                                    retryable=True,
                                )
                            prepared = prepare_authority(worker, run_id=str(run["run_id"]))
                            if isinstance(prepared, dict):
                                authority_context = {
                                    str(key): str(value)
                                    for key, value in prepared.items()
                                }

                    runtime_info_run_id = str(run["run_id"])
                    runtime_info_lease_id = str(lease.get("lease_id") or "")
                    runtime_info_startup_token = str(
                        lease.get("startup_token") or ""
                    )

                    def persist_runtime_info(
                        info: RuntimeInfo,
                        *,
                        exact_run_id: str = runtime_info_run_id,
                        exact_lease_id: str = runtime_info_lease_id,
                        exact_startup_token: str = runtime_info_startup_token,
                    ) -> None:
                        # OpenClaw publishes its live gateway PID while holding
                        # the final runtime-start fence. Persist that identity
                        # before readiness waits so Close can terminate it.
                        updated = self.store.persist_worker_runtime_info_for_run_start(
                            worker_id=worker_id,
                            run_id=exact_run_id,
                            lease_id=exact_lease_id,
                            startup_token=exact_startup_token,
                            executor_id=self._executor_id,
                            runtime=info.runtime,
                            model=info.model,
                            gateway_url=info.gateway_url,
                            gateway_port=info.gateway_port,
                            gateway_token=info.gateway_token,
                            session_key=info.session_key,
                            state_dir=info.state_dir,
                            workspace_dir=info.workspace_dir,
                            pid=info.pid,
                        )
                        if updated is None:
                            raise RuntimeErrorBase(
                                "The exact run generation changed before runtime identity persistence"
                            )

                    run_worker = {
                        **self._run_local_worker(
                            worker, run, authority_context=authority_context
                        ),
                        "_runtime_start_guard": lambda: self._runtime_execution_start_guard(
                            worker_id,
                            generation,
                            str(run["run_id"]),
                        ),
                        "_runtime_info_callback": persist_runtime_info,
                        # Persist only a one-way binding in private active-session
                        # state. The raw startup CAS token remains in SQLite.
                        "_run_startup_token_digest": startup_token_digest,
                    }
                    provider_request = self.store.get_provider_request_for_run(
                        str(run["run_id"])
                    )
                    if (
                        provider_request is not None
                        and str(provider_request.get("run_id") or "")
                        == str(run["run_id"])
                    ):
                        run_worker["_provider_response_deadline_at"] = str(
                            provider_request.get("response_deadline_at") or ""
                        )
                    admitted = self.store.admit_claimed_run(
                        str(run["run_id"]),
                        lease_id=str(lease.get("lease_id") or ""),
                        executor_id=self._executor_id,
                    )
                    if admitted is None:
                        raise RuntimeErrorBase(
                            "GlassHive lost the exact claimed run before compute admission."
                        )
                    run = {**run, **admitted}
                    qa_admitted_stall = self._consume_local_qa(
                        "admitted_queue_stall", worker, run
                    )
                    if qa_admitted_stall is not None:
                        stalled = self.store.force_queue_deadline_for_local_qa(
                            str(run["run_id"]),
                            expected_state="admitted",
                            expected_generation=int(
                                run.get("queue_wait_generation") or 0
                            ),
                            expected_deadline=str(
                                run.get("queue_deadline_at") or ""
                            ),
                            now=self._now_datetime().isoformat(),
                        )
                        self._record_local_qa_effect(
                            qa_admitted_stall,
                            "admitted_wait_moved_to_deadline"
                            if stalled is not None
                            else "admitted_wait_generation_lost",
                        )
                        return
                except HostCapacityError as exc:
                    self._clear_run_local_grant_waiter(str(run["run_id"]))
                    self._release_host_run_lease(
                        str(run["run_id"]), reason="capacity_wait"
                    )
                    self._requeue_retryable_run(
                        worker,
                        run,
                        exc,
                        failure_fields=classify_runtime_error(
                            exc,
                            runtime_name=str(
                                worker.get("profile")
                                or worker.get("runtime")
                                or "worker"
                            ),
                        ).as_store_fields(),
                    )
                    return
                except BrokerAdmissionError as exc:
                    self._clear_run_local_grant_waiter(str(run["run_id"]))
                    self._release_host_run_lease(
                        str(run["run_id"]),
                        reason=(
                            "broker_admission_retry"
                            if exc.retryable and not exc.needs_input
                            else "broker_admission_rejected"
                        ),
                    )
                    failure_fields = {
                        "failure_class": exc.code,
                        "failure_retryable": exc.retryable,
                        "failure_structured": True,
                        "failure_user_message": str(exc),
                        "failure_recommended_recovery": (
                            "Provide the requested authorization, then resume this work."
                            if exc.needs_input
                            else "Retry this work after the broker admission service recovers."
                            if exc.retryable
                            else "Review the capability authorization and retry this work."
                        ),
                        "failure_diagnostic_summary": "Deferred broker admission rejected the exact run binding.",
                    }
                    if exc.retryable and not exc.needs_input:
                        self._requeue_retryable_run(
                            worker,
                            run,
                            exc,
                            failure_fields=failure_fields,
                        )
                    elif exc.needs_input:
                        blocked_run = self.store.mark_run_needs_input(
                            str(run["run_id"]),
                            expected_state=str(run.get("state") or "claimed"),
                            error_text=str(exc),
                            failure_class=exc.code,
                            failure_user_message=str(exc),
                        ) or self.store.get_run(str(run["run_id"])) or run
                        self.store.finalize_schedule_for_run(
                            str(run["run_id"]),
                            state="needs_input",
                            last_error=str(exc),
                        )
                        self.store.update_worker_state(
                            worker_id, "needs_input", last_error=str(exc)
                        )
                        needs_input_worker = self.store.get_worker(worker_id) or worker
                        self._release_needs_input_compute(
                            needs_input_worker,
                            {**run, **blocked_run},
                        )
                        self.store.add_event(
                            str(worker["project_id"]),
                            worker_id,
                            str(run["run_id"]),
                            "run.needs_input",
                            str(exc),
                            payload={"failureCode": exc.code},
                        )
                        self._emit_callback(
                            worker,
                            "run.needs_input",
                            run={**run, **blocked_run},
                            message=str(exc),
                        )
                    else:
                        failed_run = self._finalize_run_if_state(
                            str(run["run_id"]),
                            str(run.get("state") or "claimed"),
                            "failed",
                            error_text=str(exc),
                            **terminal_generation,
                            **failure_fields,
                        ) or self.store.get_run(str(run["run_id"])) or run
                        self.store.finalize_schedule_for_run(
                            str(run["run_id"]), state="failed", last_error=str(exc)
                        )
                        self.store.update_worker_state(
                            worker_id, "ready", last_error=str(exc)
                        )
                        self.store.add_event(
                            str(worker["project_id"]),
                            worker_id,
                            str(run["run_id"]),
                            "run.failed",
                            str(exc),
                            payload={"failureCode": exc.code},
                        )
                        self._emit_callback(
                            worker,
                            "run.failed",
                            run={**run, **failed_run},
                            message=str(exc),
                        )
                    return
                callback_record, callbacks = self._run_start_callback_record(
                    worker,
                    run,
                    str(lease.get("startup_token") or ""),
                )
                lifecycle_guard = self._acquire_worker_lifecycle_guard(worker_id)
                reservation = self.store.validate_host_run_start_reservation(
                    worker_id=worker_id,
                    run_id=str(run["run_id"]),
                    run_started_at=str(run.get("started_at") or ""),
                    lease_id=str(lease.get("lease_id") or ""),
                    startup_token=str(lease.get("startup_token") or ""),
                    executor_id=self._executor_id,
                )
                if reservation is None:
                    lifecycle_guard.release()
                    clear_run_grant = getattr(
                        self.runtime, "clear_run_local_capability_grant", None
                    )
                    if callable(clear_run_grant):
                        clear_run_grant(worker)
                    self._release_host_run_lease(
                        str(run["run_id"]), reason="startup_fenced"
                    )
                    return
                pending_start: dict[str, object] = {
                    "worker_id": worker_id,
                    "run_id": str(run["run_id"]),
                    "run_started_at": str(run.get("started_at") or ""),
                    "lease_id": str(lease.get("lease_id") or ""),
                    "startup_token": str(lease.get("startup_token") or ""),
                    "worker": dict(worker),
                    "callback_record": callback_record,
                    "callbacks": callbacks,
                    "guard": lifecycle_guard,
                    "confirmed": False,
                }
                with self._pending_run_starts_lock:
                    self._pending_run_starts[str(run["run_id"])] = pending_start
                try:
                    try:
                        requires_identity = bool(
                            getattr(
                                self.runtime,
                                "requires_run_start_identity",
                                True,
                            )
                        )
                        if requires_identity and not self._run_start_observer_supported:
                            raise RunStartupRejectedError(
                                "The runtime cannot publish an exact startup identity.",
                                termination_confirmed=True,
                            )
                        invocation = self.store.mark_run_runtime_invoked(
                            str(run["run_id"]),
                            lease_id=str(lease.get("lease_id") or ""),
                            executor_id=self._executor_id,
                        )
                        if invocation is None:
                            raise RunStartupRejectedError(
                                "The run lost its exact live lease before runtime dispatch.",
                                termination_confirmed=True,
                            )
                        run = {**run, **invocation}
                        # Settle any prelaunch failure against the generation just
                        # made durable; the reservation fence is stale from here on.
                        terminal_generation = self._terminal_generation_for_run(
                            run, lease
                        )
                        run_worker = self.peers.project_native_tools(run_worker, run)
                        coordinator = getattr(self, "coordinator", None)
                        if coordinator is not None:
                            run_worker = coordinator.bind_native_worker(
                                run_worker, run, self.peers,
                                os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "").rstrip("/") + "/v1/native/coordinator/",
                            )
                        configuration = getattr(self, "worker_configuration", None)
                        if configuration is not None:
                            from .worker_context_mcp import bind_context_projection
                            run_worker = bind_context_projection(configuration, run_worker, run)
                        placement_reader = getattr(self.runtime, "native_workspace_placement", None)
                        if callable(placement_reader):
                            run_worker["_native_context_placement"] = placement_reader(run_worker)
                        terminal_generation = self._terminal_generation_for_run(
                            run, lease
                        )
                        runtime_invoked = True
                        pending_start["run"] = dict(run)
                        pending_start["run_started_at"] = str(
                            run.get("runtime_invoked_at") or ""
                        )
                        if not requires_identity:
                            def confirm_in_process_start() -> None:
                                self._confirm_in_process_run_start(pending_start)

                            # An in-process adapter has no external process
                            # observer. It must publish its durable start from
                            # the same final runtime boundary used by Close,
                            # Pause, and Interrupt—not before adapter dispatch.
                            # Its final local boundary also replaces the
                            # cross-process launch flock, so controls may win
                            # before the adapter reaches that boundary.
                            lifecycle_guard.release()
                            run_worker["_runtime_started_callback"] = (
                                confirm_in_process_start
                            )
                        qa_provider_unavailable = self._consume_local_qa(
                            "provider_unavailable", worker, run
                        )
                        if qa_provider_unavailable is not None:
                            self._record_local_qa_effect(
                                qa_provider_unavailable,
                                "provider_unavailable_before_adapter_call",
                            )
                            unavailable = RuntimeErrorBase(
                                "The configured model provider is temporarily unavailable."
                            )
                            unavailable.failure_class = "provider_unavailable"
                            unavailable.retryable = True
                            raise unavailable
                        try:
                            output = self.runtime.run_task(
                                run_worker,
                                self._runtime_instruction_for_run(
                                    run_worker, run["instruction"]
                                ),
                                run_id=run["run_id"],
                            )
                        except TypeError as exc:
                            if "run_id" not in str(exc):
                                raise
                            output = self.runtime.run_task(
                                run_worker,
                                self._runtime_instruction_for_run(
                                    run_worker, run["instruction"]
                                ),
                            )
                        with self._pending_run_starts_lock:
                            confirmed = bool(
                                (
                                    self._pending_run_starts.get(
                                        str(run["run_id"])
                                    )
                                    or {}
                                ).get("confirmed")
                            )
                        if not confirmed:
                            raise RunStartupRejectedError(
                                "The runtime returned without publishing its exact startup identity.",
                                termination_confirmed=False,
                            )
                        confirmed_run = self.store.get_run(str(run["run_id"]))
                        if confirmed_run:
                            run = {**run, **confirmed_run}
                        runtime_invoked = bool(run.get("runtime_invoked_at"))
                    except RunStartupRejectedError as exc:
                        preserve_start_fence = not exc.termination_confirmed
                        if preserve_start_fence:
                            self.store.mark_host_run_start_termination_unconfirmed(
                                lease_id=str(lease.get("lease_id") or ""),
                                run_id=str(run["run_id"]),
                                executor_id=self._executor_id,
                                startup_token=str(lease.get("startup_token") or ""),
                            )
                        raise
                    finally:
                        durable_run = self.store.get_run(str(run["run_id"]))
                        if durable_run:
                            run = {**run, **durable_run}
                            runtime_invoked = bool(
                                durable_run.get("runtime_invoked_at")
                            )
                        with self._pending_run_starts_lock:
                            self._pending_run_starts.pop(
                                str(run["run_id"]), None
                            )
                        lifecycle_guard.release()
                        if self._run_retained_for_restart(str(run["run_id"])):
                            # The generation keeps running across the managed restart and
                            # still needs its run-local authority; the restarted service
                            # revokes it once that generation is terminal.
                            clear_run_grant = None
                        else:
                            clear_run_grant = getattr(
                                self.runtime, "clear_run_local_capability_grant", None
                            )
                        if callable(clear_run_grant):
                            try:
                                clear_run_grant(worker)
                            except Exception:
                                logger.exception(
                                    "Failed to clear run-local capability grant for worker %s",
                                    worker_id,
                                )
                        try:
                            self._revoke_run_local_capability_grant(
                                run_worker, run_id=str(run["run_id"])
                            )
                        except Exception:
                            logger.exception(
                                "Failed to revoke run-local capability grant for worker %s",
                                worker_id,
                            )
                        if not preserve_start_fence:
                            self._release_host_run_lease(
                                str(run["run_id"]), reason="runtime_returned"
                            )
                except RunStartupRejectedError as exc:
                    if not exc.termination_confirmed:
                        return
                    retry_error = RuntimeErrorBase(
                        "GlassHive safely stopped a startup attempt that lost durable ownership."
                    )
                    self._requeue_retryable_run(
                        self.store.get_worker(worker_id) or worker,
                        self.store.get_run(str(run["run_id"])) or run,
                        retry_error,
                        failure_fields={
                            "failure_class": "service_startup_fenced",
                            "failure_retryable": 1,
                            "failure_structured": 1,
                            "failure_user_message": (
                                "GlassHive safely recovered an interrupted worker startup and will retry."
                            ),
                            "failure_recommended_recovery": (
                                "No action is required unless this work remains queued."
                            ),
                            "failure_diagnostic_summary": (
                                "The provider startup was stopped before its durable identity was accepted."
                            ),
                        },
                    )
                    return
                except WorkerPausedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    paused_run = self.store.transition_run_if_state(
                        str(run["run_id"]),
                        "running",
                        "paused",
                        ended_at=None,
                        error_text=str(exc),
                    )
                    durable = paused_run or self.store.get_run(str(run["run_id"])) or run
                    durable_state = str(durable.get("state") or "")
                    current_worker = self.store.get_worker(worker_id) or worker
                    if (
                        not paused_run
                        and str(current_worker.get("compute_release_token") or "")
                        and str(
                            current_worker.get("compute_release_target_run_id") or ""
                        )
                        == str(run["run_id"])
                    ):
                        return
                    if durable_state in TERMINAL_RUN_STATES:
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                        return
                    if durable_state == "queued":
                        # A host resume may requeue the exact run before the
                        # killed provider unwinds. Preserve that newer CAS; the
                        # processor-finally path starts its replacement.
                        self.store.update_worker_state(worker_id, "starting", last_error="")
                        return
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.paused", str(exc))
                    self._emit_callback(worker, "run.paused", run={**run, "state": "paused", "error_text": str(exc)}, message=str(exc))
                    self._wake_host_capacity_waiters(
                        self.store.get_worker(worker_id) or worker
                    )
                    return
                except WorkerInterruptedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    if self._released_for_managed_shutdown(run, terminal_generation):
                        # Managed shutdown released this run's lease and stopped its generation;
                        # the same run resumes in the restarted service, so the old generation's
                        # exit must not become a terminal state or a callback.
                        logger.info(
                            "Managed shutdown stopped the generation of run %s; leaving it for restart requeue",
                            run["run_id"],
                        )
                        return
                    current_worker = self.store.get_worker(worker_id) or worker
                    stop_requested = current_worker.get("state") == "stopping"
                    final_state = "cancelled" if stop_requested else "interrupted"
                    finalized_run = self._finalize_run_if_state(
                        run["run_id"],
                        "running",
                        state=final_state,
                        error_text=str(exc),
                        **terminal_generation,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            worker,
                            run,
                            "interruption",
                        )
                        continue
                    self.store.finalize_schedule_for_run(
                        run["run_id"],
                        state="cancelled" if stop_requested else "failed",
                        last_error=str(exc),
                    )
                    self.store.update_worker_state(worker_id, "ready", last_error="")
                    if stop_requested:
                        self.store.accept_cancel_actions_for_run(run["run_id"])
                    event_type = f"run.{final_state}"
                    self.store.add_event(
                        worker["project_id"], worker_id, run["run_id"], event_type, str(exc)
                    )
                    self._emit_callback(
                        worker,
                        event_type,
                        run={**run, **finalized_run},
                        message=str(exc),
                    )
                    if final_state == "interrupted":
                        try:
                            self._settle_interrupted_steer_claim(
                                worker_id, str(run["run_id"])
                            )
                        except Exception:
                            # The interrupted source remains authoritative. Keep
                            # the exact fence for scheduler recovery instead of
                            # dispatching an unproven replacement.
                            logger.exception(
                                "Failed to settle exact interrupted Steer for worker %s",
                                worker_id,
                            )
                    self._wake_host_capacity_waiters(
                        self.store.get_worker(worker_id) or worker
                    )
                    continue
                except WorkerTerminatedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    recovered = self._collect_completed_run(worker, run)
                    if recovered:
                        self._apply_recovered_run(worker, run, recovered)
                        continue
                    finalized_run = self._finalize_run_if_state(
                        run["run_id"],
                        str(run.get("state") or "running"),
                        state="cancelled",
                        error_text=str(exc),
                        **terminal_generation,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            worker, run, "termination"
                        )
                        return
                    self.store.finalize_schedule_for_run(run["run_id"], state="cancelled", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "terminated", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.cancelled", str(exc))
                    self._emit_callback(worker, "run.cancelled", run={**run, "state": "cancelled", "error_text": str(exc)}, message=str(exc))
                    self._wake_host_capacity_waiters(
                        self.store.get_worker(worker_id) or worker
                    )
                    return
                except RuntimeErrorBase as exc:
                    durable_liveness_run = self.store.get_run(str(run["run_id"]))
                    if (
                        durable_liveness_run
                        and str(durable_liveness_run.get("state") or "")
                        == "needs_input"
                        and str(
                            durable_liveness_run.get("failure_class") or ""
                        )
                        == "provider_progress_stalled"
                    ):
                        return
                    if not self._processor_is_current(worker_id, generation):
                        return
                    if self._released_for_managed_shutdown(run, terminal_generation):
                        logger.info(
                            "Managed shutdown stopped the generation of run %s; leaving it for restart requeue",
                            run["run_id"],
                        )
                        return
                    current_worker = self.store.get_worker(worker_id) or worker
                    worker_state = current_worker["state"]
                    final_state = "failed"
                    if worker_state == "paused":
                        final_state = "interrupted"
                    elif worker_state == "terminated":
                        final_state = "cancelled"
                    if (isinstance(exc, ProviderAccountBusyError)
                            and worker_state not in {"paused", "stopping", "terminated"}
                            and str((durable_liveness_run or {}).get("state") or "") == "running"):
                        self._requeue_retryable_run(
                            current_worker,
                            durable_liveness_run or run,
                            exc,
                            failure_fields={
                                "failure_class": "host_capacity",
                                "failure_retryable": True,
                                "failure_structured": True,
                                "failure_user_message": str(exc),
                                "failure_recommended_recovery": "No action is required; this work will start when the selected AI account is free.",
                                "failure_diagnostic_summary": "Selected provider account acquired a live lease before native launch.",
                            },
                        )
                        return
                    refreshed_worker = (
                        self._refresh_runtime_info(
                            worker_id,
                            state=worker_state if worker_state in {"paused", "terminated"} else "ready",
                            last_error=str(exc),
                        )
                        or self.store.get_worker(worker_id)
                        or current_worker
                    )
                    failure_fields = (
                        classify_runtime_error(
                            exc,
                            runtime_name=str(refreshed_worker.get("profile") or refreshed_worker.get("runtime") or "worker"),
                        ).as_store_fields()
                        if final_state == "failed"
                        else {}
                    )
                    if final_state == "failed":
                        self._record_provider_route_failure(
                            refreshed_worker, run, failure_fields, exc
                        )
                    if (
                        final_state == "failed"
                        and str(failure_fields.get("failure_class") or "") != "glasshive_evidence_check_failed"
                    ):
                        recovered = self._collect_completed_run(refreshed_worker, run)
                        if recovered:
                            self._apply_recovered_run(refreshed_worker, run, recovered)
                            continue
                    if final_state == "failed" and self._switch_quota_exhausted_run_to_fallback(
                        refreshed_worker,
                        run,
                        exc,
                        failure_fields,
                    ):
                        return
                    if (
                        final_state == "failed"
                        and bool(failure_fields.get("failure_retryable"))
                        and str(failure_fields.get("failure_class") or "")
                        in {"host_worker_busy", "host_capacity", "provider_rate_limited"}
                        and (
                            str(failure_fields.get("failure_class") or "")
                            != "provider_rate_limited"
                            or getattr(exc, "retry_after_s", None) is not None
                        )
                    ):
                        self._requeue_retryable_run(refreshed_worker, run, exc, failure_fields=failure_fields)
                        return
                    finalized_run = self._finalize_run_if_state(
                        run["run_id"],
                        "running",
                        final_state,
                        error_text=str(exc),
                        **terminal_generation,
                        **failure_fields,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            current_worker,
                            run,
                            final_state,
                        )
                        if worker_state in {"paused", "terminated"}:
                            return
                        continue
                    self.store.finalize_schedule_for_run(
                        run["run_id"],
                        state="cancelled" if final_state == "cancelled" else "failed",
                        last_error=str(exc),
                    )
                    self.store.update_worker_state(worker_id, worker_state if worker_state in {"paused", "terminated"} else "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], f"run.{final_state}", str(exc))
                    failed_run = {
                        **run,
                        **finalized_run,
                        "state": final_state,
                        "error_text": str(exc),
                        **failure_fields,
                    }
                    callback_worker = self.store.get_worker(worker_id) or refreshed_worker
                    deliverable = (
                        self._completion_deliverable(callback_worker, failed_run, "", str(exc))
                        if final_state == "failed"
                        else None
                    )
                    failure_message = runtime_failure_callback_message(failure_fields, str(exc))
                    self._emit_callback(
                        callback_worker,
                        f"run.{final_state}",
                        run=failed_run,
                        message=failure_message,
                        deliverable=deliverable,
                    )
                    self._wake_host_capacity_waiters(callback_worker)
                    if worker_state in {"paused", "terminated"}:
                        return
                    continue
                except Exception as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    failure_fields = classify_runtime_error(
                        exc,
                        runtime_name=str(worker.get("profile") or worker.get("runtime") or "worker"),
                    ).as_store_fields()
                    self._record_provider_route_failure(
                        worker, run, failure_fields, exc
                    )
                    finalized_run = self._finalize_run_if_state(
                        run["run_id"],
                        "running",
                        "failed",
                        error_text=str(exc),
                        **terminal_generation,
                        **failure_fields,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(worker, run, "failed")
                        continue
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", str(exc))
                    failed_run = {
                        **run,
                        **finalized_run,
                        "state": "failed",
                        "error_text": str(exc),
                        **failure_fields,
                    }
                    failure_message = runtime_failure_callback_message(failure_fields, str(exc))
                    self._emit_callback(worker, "run.failed", run=failed_run, message=failure_message)
                    self._wake_host_capacity_waiters(
                        self.store.get_worker(worker_id) or worker
                    )
                    continue

                if not self._processor_is_current(worker_id, generation):
                    return
                completion_expected_state = self._settle_native_children(
                    worker,
                    run,
                    output,
                    terminal_generation=terminal_generation,
                )
                if completion_expected_state not in {"running", "settling"}:
                    self._record_late_processor_terminal_ignored(
                        worker,
                        run,
                        "completion",
                    )
                    continue
                completed_run = self._finalize_run_if_state(
                    run["run_id"],
                    completion_expected_state,
                    "completed",
                    output_text=output,
                    usage=self._run_usage(worker, run["run_id"]),
                    **terminal_generation,
                )
                if not completed_run:
                    self._record_late_processor_terminal_ignored(worker, run, "completion")
                    continue
                self._clear_provider_route_health(worker, completed_run)
                self.store.finalize_schedule_for_run(run["run_id"], state="completed")
                self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
                message = terminal_callback_message(output)
                full_message = terminal_callback_full_message(output)
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
                completed_run = {**run, **completed_run, "state": "completed", "output_text": output}
                refreshed_worker = self._refresh_runtime_info(worker_id, state="ready", last_error="") or self.store.get_worker(worker_id) or worker
                deliverable = self._completion_deliverable(refreshed_worker, completed_run, output)
                self._promote_completed_deliverable(refreshed_worker, completed_run, deliverable)
                self._emit_callback(
                    refreshed_worker,
                    "run.completed",
                    run=completed_run,
                    message=message or "Run completed",
                    full_message=full_message if full_message != message else "",
                    deliverable=deliverable,
                )
                self._wake_host_capacity_waiters(refreshed_worker)
                current_run = None
                runtime_invoked = False
        except Exception as exc:
            logger.exception(
                "Unexpected GlassHive worker processor failure",
                extra={
                    "worker_id": worker_id,
                    "run_id": str((current_run or {}).get("run_id") or ""),
                },
            )
            if current_run and not preserve_start_fence:
                try:
                    durable_run = self.store.get_run(str(current_run["run_id"]))
                    if durable_run and str(durable_run.get("state") or "") in {
                        "claimed",
                        "admitted",
                        "running",
                    }:
                        recovered = (
                            self._collect_completed_run(
                                self.store.get_worker(worker_id) or {}, durable_run
                            )
                            if runtime_invoked
                            else None
                        )
                        if recovered:
                            self._apply_recovered_run(
                                self.store.get_worker(worker_id) or {},
                                durable_run,
                                recovered,
                            )
                        elif not runtime_invoked:
                            recovery_error = RuntimeErrorBase(
                                "GlassHive recovered an internal processor interruption "
                                "before provider execution started."
                            )
                            self._requeue_retryable_run(
                                self.store.get_worker(worker_id) or {
                                    "worker_id": worker_id,
                                    "project_id": str(
                                        durable_run.get("project_id") or ""
                                    ),
                                },
                                durable_run,
                                recovery_error,
                                failure_fields={
                                    "failure_class": "service_processor_unexpected",
                                    "failure_retryable": 1,
                                    "failure_structured": 1,
                                    "failure_user_message": (
                                        "GlassHive recovered an internal worker interruption and "
                                        "will retry this work."
                                    ),
                                    "failure_recommended_recovery": (
                                        "No action is required unless the work remains queued."
                                    ),
                                    "failure_diagnostic_summary": (
                                        "The processor exited before invoking the provider runtime."
                                    ),
                                },
                            )
                        else:
                            failure_message = (
                                "GlassHive could not safely confirm the provider result after "
                                "an internal processor interruption."
                            )
                            failed_run = self._finalize_run_if_state(
                                str(durable_run["run_id"]),
                                "running",
                                "failed",
                                error_text=failure_message,
                                **terminal_generation,
                                failure_class="service_processor_unexpected",
                                failure_retryable=1,
                                failure_structured=1,
                                failure_user_message=failure_message,
                                failure_recommended_recovery=(
                                    "Retry the work after reviewing any partial workspace output."
                                ),
                                failure_diagnostic_summary=(
                                    "The provider runtime returned, but processor finalization "
                                    "did not complete."
                                ),
                            )
                            if failed_run:
                                self.store.finalize_schedule_for_run(
                                    str(durable_run["run_id"]),
                                    state="failed",
                                    last_error=failure_message,
                                )
                                failed_worker = (
                                    self.store.update_worker_state(
                                        worker_id,
                                        "ready",
                                        last_error=failure_message,
                                    )
                                    or self.store.get_worker(worker_id)
                                    or {}
                                )
                                self.store.add_event(
                                    str(failed_worker.get("project_id") or ""),
                                    worker_id,
                                    str(durable_run["run_id"]),
                                    "run.failed",
                                    failure_message,
                                )
                                self._emit_callback(
                                    failed_worker,
                                    "run.failed",
                                    run={**durable_run, **failed_run},
                                    message=failure_message,
                                )
                except Exception:
                    logger.exception(
                        "Failed to durably recover unexpected worker processor failure",
                        extra={"worker_id": worker_id},
                    )
        finally:
            if current_run and not preserve_start_fence:
                try:
                    self._release_host_run_lease(
                        str(current_run["run_id"]), reason="processor_exit"
                    )
                except Exception:
                    logger.exception(
                        "Failed to release run lease after worker processor exit",
                        extra={
                            "worker_id": worker_id,
                            "run_id": str(current_run.get("run_id") or ""),
                        },
                    )
            try:
                if self._release_processor(worker_id, generation):
                    pending = self.store.get_worker(worker_id)
                    if (
                        pending
                        and pending["state"]
                        not in {"paused", "needs_input", "stopping", "terminated"}
                        and not str(pending.get("compute_release_token") or "").strip()
                        and not self.store.has_unconfirmed_host_run_start(worker_id)
                    ):
                        if self.store.peek_next_queued_run(worker_id):
                            self._ensure_worker_processor(worker_id)
            except Exception:
                logger.exception(
                    "Failed to finalize GlassHive worker processor ownership",
                    extra={"worker_id": worker_id},
                )

    def _process_worker_queue(self, worker_id: str, generation: int) -> None:
        return self._process_worker_queue_parallel(worker_id, generation)

    def _process_worker_queue_legacy(self, worker_id: str, generation: int) -> None:
        current_run: dict | None = None
        runtime_invoked = False
        preserve_start_fence = False
        terminal_generation: dict[str, str] = {}
        try:
            while True:
                current_run = None
                runtime_invoked = False
                preserve_start_fence = False
                terminal_generation = {}
                if not self._processor_is_current(worker_id, generation):
                    return
                worker = self.store.get_worker(worker_id)
                if not worker or worker["state"] in {
                    "paused",
                    "needs_input",
                    "stopping",
                    "terminated",
                }:
                    return

                queued_run = self.store.peek_next_queued_run(worker_id)
                if queued_run:
                    capacity_error = self._runtime_capacity_error(worker)
                    if capacity_error:
                        self._requeue_retryable_run(worker, queued_run, capacity_error)
                        return
                    self._mark_run_local_grant_waiter(
                        worker, str(queued_run["run_id"])
                    )
                    try:
                        # Reserve host/resource capacity while the accepted work
                        # is still queued. Only a real execution admission may
                        # mint an immutable run attempt.
                        self._acquire_host_run_lease(worker, queued_run)
                    except HostCapacityError as exc:
                        self._clear_run_local_grant_waiter(
                            str(queued_run["run_id"])
                        )
                        self._requeue_retryable_run(
                            worker,
                            queued_run,
                            exc,
                            failure_fields=classify_runtime_error(
                                exc,
                                runtime_name=str(
                                    worker.get("profile")
                                    or worker.get("runtime")
                                    or "worker"
                                ),
                            ).as_store_fields(),
                        )
                        return

                run = self.store.claim_next_queued_run(
                    worker_id,
                    executor_id=self._executor_id,
                    lease_ttl_s=self._host_lease_ttl_s(),
                )
                if not run:
                    if queued_run:
                        self._clear_run_local_grant_waiter(
                            str(queued_run["run_id"])
                        )
                        self._release_host_run_lease(
                            str(queued_run["run_id"]),
                            reason="preclaim_generation_lost",
                        )
                    current = self.store.get_worker(worker_id)
                    if (
                        self._processor_is_current(worker_id, generation)
                        and current
                        and current["state"] not in {
                            "paused",
                            "needs_input",
                            "stopping",
                            "terminated",
                            "failed",
                        }
                        and not self.store.get_active_run(worker_id)
                    ):
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                    return

                current_run = run
                worker = self.store.get_worker(worker_id) or worker
                if queued_run and str(queued_run.get("run_id") or "") != str(
                    run.get("run_id") or ""
                ):
                    self._clear_run_local_grant_waiter(
                        str(queued_run.get("run_id") or "")
                    )
                    self._mark_run_local_grant_waiter(
                        worker, str(run["run_id"])
                    )
                qa_claimed_stall = self._consume_local_qa(
                    "claimed_queue_stall", worker, run
                )
                if qa_claimed_stall is not None:
                    stalled = self.store.force_queue_deadline_for_local_qa(
                        str(run["run_id"]),
                        expected_state="claimed",
                        expected_generation=int(
                            run.get("queue_wait_generation") or 0
                        ),
                        expected_deadline=str(run.get("queue_deadline_at") or ""),
                        now=self._now_datetime().isoformat(),
                    )
                    self._record_local_qa_effect(
                        qa_claimed_stall,
                        "claimed_wait_moved_to_deadline"
                        if stalled is not None
                        else "claimed_wait_generation_lost",
                    )
                    return
                if self._handle_unhealthy_provider_route(worker, run):
                    self._clear_run_local_grant_waiter(str(run["run_id"]))
                    return
                try:
                    lease = self._acquire_host_run_lease(worker, run)
                    if not lease:
                        raise RuntimeErrorBase(
                            "GlassHive could not reserve the exact run startup generation."
                        )
                    terminal_generation = self._terminal_generation_for_run(
                        run, lease
                    )
                    authority_context: dict[str, str] = {}
                    if (
                        self._deferred_capability_authorization(worker) is not None
                        or self._prompt_workbench_scheduled_authority(worker)
                        is not None
                    ):
                        prepare_authority = getattr(
                            self.runtime, "prepare_run_authority_context", None
                        )
                        if not callable(prepare_authority):
                            raise BrokerAdmissionError(
                                "broker_admission_generation_unavailable",
                                "The exact mission container generation is unavailable.",
                                retryable=True,
                            )
                        prepared = prepare_authority(worker, run_id=str(run["run_id"]))
                        if isinstance(prepared, dict):
                            authority_context = {
                                str(key): str(value)
                                for key, value in prepared.items()
                            }
                    run_worker = {
                        **self._run_local_worker(
                            worker, run, authority_context=authority_context
                        ),
                        # Persist only a one-way binding in private active-session
                        # state. The raw startup CAS token remains in SQLite.
                        "_run_startup_token_digest": hashlib.sha256(
                            str(lease.get("startup_token") or "").encode("utf-8")
                        ).hexdigest(),
                    }
                    admitted = self.store.admit_claimed_run(
                        str(run["run_id"]),
                        lease_id=str(lease.get("lease_id") or ""),
                        executor_id=self._executor_id,
                    )
                    if admitted is None:
                        raise RuntimeErrorBase(
                            "GlassHive lost the exact claimed run before compute admission."
                        )
                    run = {**run, **admitted}
                    qa_admitted_stall = self._consume_local_qa(
                        "admitted_queue_stall", worker, run
                    )
                    if qa_admitted_stall is not None:
                        stalled = self.store.force_queue_deadline_for_local_qa(
                            str(run["run_id"]),
                            expected_state="admitted",
                            expected_generation=int(
                                run.get("queue_wait_generation") or 0
                            ),
                            expected_deadline=str(
                                run.get("queue_deadline_at") or ""
                            ),
                            now=self._now_datetime().isoformat(),
                        )
                        self._record_local_qa_effect(
                            qa_admitted_stall,
                            "admitted_wait_moved_to_deadline"
                            if stalled is not None
                            else "admitted_wait_generation_lost",
                        )
                        return
                except HostCapacityError as exc:
                    self._clear_run_local_grant_waiter(str(run["run_id"]))
                    self._release_host_run_lease(
                        str(run["run_id"]), reason="capacity_wait"
                    )
                    self._requeue_retryable_run(
                        worker,
                        run,
                        exc,
                        failure_fields=classify_runtime_error(
                            exc,
                            runtime_name=str(
                                worker.get("profile")
                                or worker.get("runtime")
                                or "worker"
                            ),
                        ).as_store_fields(),
                    )
                    return
                except BrokerAdmissionError as exc:
                    self._clear_run_local_grant_waiter(str(run["run_id"]))
                    self._release_host_run_lease(
                        str(run["run_id"]),
                        reason=(
                            "broker_admission_retry"
                            if exc.retryable and not exc.needs_input
                            else "broker_admission_rejected"
                        ),
                    )
                    failure_fields = {
                        "failure_class": exc.code,
                        "failure_retryable": exc.retryable,
                        "failure_structured": True,
                        "failure_user_message": str(exc),
                        "failure_recommended_recovery": (
                            "Provide the requested authorization, then resume this work."
                            if exc.needs_input
                            else "Retry this work after the broker admission service recovers."
                            if exc.retryable
                            else "Review the capability authorization and retry this work."
                        ),
                        "failure_diagnostic_summary": "Deferred broker admission rejected the exact run binding.",
                    }
                    if exc.retryable and not exc.needs_input:
                        self._requeue_retryable_run(
                            worker,
                            run,
                            exc,
                            failure_fields=failure_fields,
                        )
                    elif exc.needs_input:
                        conversation_grant_refresh_required = bool(
                            exc.code == "conversation_capability_grant_required"
                            and self._trusted_run_lane(worker) == "conversation"
                        )
                        blocked_run = self.store.mark_run_needs_input(
                            str(run["run_id"]),
                            expected_state=str(run.get("state") or "claimed"),
                            error_text=str(exc),
                            failure_class=exc.code,
                            failure_user_message=str(exc),
                        ) or self.store.get_run(str(run["run_id"])) or run
                        self.store.finalize_schedule_for_run(
                            str(run["run_id"]),
                            state="needs_input",
                            last_error=str(exc),
                        )
                        self.store.update_worker_state(
                            worker_id, "needs_input", last_error=str(exc)
                        )
                        needs_input_worker = self.store.get_worker(worker_id) or worker
                        self._release_needs_input_compute(
                            needs_input_worker,
                            {**run, **blocked_run},
                        )
                        self.store.add_event(
                            str(worker["project_id"]),
                            worker_id,
                            str(run["run_id"]),
                            "run.needs_input",
                            str(exc),
                            payload={"failureCode": exc.code},
                        )
                        self._emit_callback(
                            worker,
                            "run.needs_input",
                            run={**run, **blocked_run},
                            message=str(exc),
                        )
                        if self._provider_request_reconciler is not None:
                            try:
                                self._provider_request_reconciler(str(run["run_id"]))
                            except Exception:
                                logger.error(
                                    "GlassHive needs-input provider request reconciliation faulted safely",
                                    extra={"error_code": "transient_dependency"},
                                )
                        if conversation_grant_refresh_required:
                            # A full restart destroys invocation-local bearer
                            # authority. Keep this turn replayable, then use the
                            # durable, pause-aware transaction to unblock only a
                            # queued sibling. A later scheduler pass retries if a
                            # compute-release claim still owns the worker.
                            self.reconcile_restart_authority_backlog_once()
                    else:
                        failed_run = self._finalize_run_if_state(
                            str(run["run_id"]),
                            str(run.get("state") or "claimed"),
                            "failed",
                            error_text=str(exc),
                            **terminal_generation,
                            **failure_fields,
                        ) or self.store.get_run(str(run["run_id"])) or run
                        self.store.finalize_schedule_for_run(
                            str(run["run_id"]), state="failed", last_error=str(exc)
                        )
                        self.store.update_worker_state(
                            worker_id, "ready", last_error=str(exc)
                        )
                        self.store.add_event(
                            str(worker["project_id"]),
                            worker_id,
                            str(run["run_id"]),
                            "run.failed",
                            str(exc),
                            payload={"failureCode": exc.code},
                        )
                        self._emit_callback(
                            worker,
                            "run.failed",
                            run={**run, **failed_run},
                            message=str(exc),
                        )
                    return
                callback_record, callbacks = self._run_start_callback_record(
                    worker,
                    run,
                    str(lease.get("startup_token") or ""),
                )
                lifecycle_guard = self._acquire_worker_lifecycle_guard(worker_id)
                reservation = self.store.validate_host_run_start_reservation(
                    worker_id=worker_id,
                    run_id=str(run["run_id"]),
                    run_started_at=str(run.get("started_at") or ""),
                    lease_id=str(lease.get("lease_id") or ""),
                    startup_token=str(lease.get("startup_token") or ""),
                    executor_id=self._executor_id,
                )
                if reservation is None:
                    lifecycle_guard.release()
                    clear_run_grant = getattr(
                        self.runtime, "clear_run_local_capability_grant", None
                    )
                    if callable(clear_run_grant):
                        clear_run_grant(worker)
                    self._release_host_run_lease(
                        str(run["run_id"]), reason="startup_fenced"
                    )
                    return
                pending_start: dict[str, object] = {
                    "worker_id": worker_id,
                    "run_id": str(run["run_id"]),
                    "run_started_at": str(run.get("started_at") or ""),
                    "lease_id": str(lease.get("lease_id") or ""),
                    "startup_token": str(lease.get("startup_token") or ""),
                    "worker": dict(worker),
                    "callback_record": callback_record,
                    "callbacks": callbacks,
                    "guard": lifecycle_guard,
                    "confirmed": False,
                }
                with self._pending_run_starts_lock:
                    self._pending_run_starts[str(run["run_id"])] = pending_start
                try:
                    try:
                        requires_identity = bool(
                            getattr(
                                self.runtime,
                                "requires_run_start_identity",
                                True,
                            )
                        )
                        if requires_identity and not self._run_start_observer_supported:
                            raise RunStartupRejectedError(
                                "The runtime cannot publish an exact startup identity.",
                                termination_confirmed=True,
                            )
                        invocation = self.store.mark_run_runtime_invoked(
                            str(run["run_id"]),
                            lease_id=str(lease.get("lease_id") or ""),
                            executor_id=self._executor_id,
                        )
                        if invocation is None:
                            raise RunStartupRejectedError(
                                "The run lost its exact live lease before runtime dispatch.",
                                termination_confirmed=True,
                            )
                        run = {**run, **invocation}
                        # Settle any prelaunch failure against the generation just
                        # made durable; the reservation fence is stale from here on.
                        terminal_generation = self._terminal_generation_for_run(
                            run, lease
                        )
                        run_worker = self.peers.project_native_tools(run_worker, run)
                        coordinator = getattr(self, "coordinator", None)
                        if coordinator is not None:
                            run_worker = coordinator.bind_native_worker(
                                run_worker, run, self.peers,
                                os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "").rstrip("/") + "/v1/native/coordinator/",
                            )
                        configuration = getattr(self, "worker_configuration", None)
                        if configuration is not None:
                            from .worker_context_mcp import bind_context_projection
                            run_worker = bind_context_projection(configuration, run_worker, run)
                        placement_reader = getattr(self.runtime, "native_workspace_placement", None)
                        if callable(placement_reader):
                            run_worker["_native_context_placement"] = placement_reader(run_worker)
                        terminal_generation = self._terminal_generation_for_run(
                            run, lease
                        )
                        runtime_invoked = True
                        pending_start["run"] = dict(run)
                        pending_start["run_started_at"] = str(
                            run.get("runtime_invoked_at") or ""
                        )
                        if not requires_identity:
                            self._confirm_in_process_run_start(pending_start)
                            confirmed_run = pending_start.get("run")
                            if not isinstance(confirmed_run, dict):
                                raise RunStartupRejectedError(
                                    "The run lost durable admission before execution started.",
                                    termination_confirmed=True,
                                )
                            run = {**run, **confirmed_run}
                            runtime_invoked = bool(run.get("runtime_invoked_at"))
                            worker = (
                                self._refresh_runtime_info(
                                    worker_id,
                                    state="running",
                                    last_error="",
                                )
                                or self.store.get_worker(worker_id)
                                or worker
                            )
                        qa_provider_unavailable = self._consume_local_qa(
                            "provider_unavailable", worker, run
                        )
                        if qa_provider_unavailable is not None:
                            self._record_local_qa_effect(
                                qa_provider_unavailable,
                                "provider_unavailable_before_adapter_call",
                            )
                            unavailable = RuntimeErrorBase(
                                "The configured model provider is temporarily unavailable."
                            )
                            unavailable.failure_class = "provider_unavailable"
                            unavailable.retryable = True
                            raise unavailable
                        try:
                            output = self.runtime.run_task(
                                run_worker,
                                self._runtime_instruction_for_run(
                                    run_worker, run["instruction"]
                                ),
                                run_id=run["run_id"],
                            )
                        except TypeError as exc:
                            if "run_id" not in str(exc):
                                raise
                            output = self.runtime.run_task(
                                run_worker,
                                self._runtime_instruction_for_run(
                                    run_worker, run["instruction"]
                                ),
                            )
                        with self._pending_run_starts_lock:
                            confirmed = bool(
                                (
                                    self._pending_run_starts.get(
                                        str(run["run_id"])
                                    )
                                    or {}
                                ).get("confirmed")
                            )
                        if not confirmed:
                            raise RunStartupRejectedError(
                                "The runtime returned without publishing its exact startup identity.",
                                termination_confirmed=False,
                            )
                        confirmed_run = self.store.get_run(str(run["run_id"]))
                        if confirmed_run:
                            run = {**run, **confirmed_run}
                        runtime_invoked = bool(run.get("runtime_invoked_at"))
                    except RunStartupRejectedError as exc:
                        preserve_start_fence = not exc.termination_confirmed
                        if preserve_start_fence:
                            self.store.mark_host_run_start_termination_unconfirmed(
                                lease_id=str(lease.get("lease_id") or ""),
                                run_id=str(run["run_id"]),
                                executor_id=self._executor_id,
                                startup_token=str(lease.get("startup_token") or ""),
                            )
                        raise
                    finally:
                        durable_run = self.store.get_run(str(run["run_id"]))
                        if durable_run:
                            run = {**run, **durable_run}
                            runtime_invoked = bool(
                                durable_run.get("runtime_invoked_at")
                            )
                        with self._pending_run_starts_lock:
                            self._pending_run_starts.pop(
                                str(run["run_id"]), None
                            )
                        lifecycle_guard.release()
                        clear_run_grant = getattr(
                            self.runtime, "clear_run_local_capability_grant", None
                        )
                        if callable(clear_run_grant):
                            try:
                                clear_run_grant(worker)
                            except Exception:
                                logger.exception(
                                    "Failed to clear run-local capability grant for worker %s",
                                    worker_id,
                                )
                        try:
                            self._revoke_run_local_capability_grant(run_worker)
                        except Exception:
                            logger.exception(
                                "Failed to revoke run-local capability grant for worker %s",
                                worker_id,
                            )
                        if not preserve_start_fence:
                            self._release_host_run_lease(
                                str(run["run_id"]), reason="runtime_returned"
                            )
                except RunStartupRejectedError as exc:
                    if not exc.termination_confirmed:
                        return
                    retry_error = RuntimeErrorBase(
                        "GlassHive safely stopped a startup attempt that lost durable ownership."
                    )
                    self._requeue_retryable_run(
                        self.store.get_worker(worker_id) or worker,
                        self.store.get_run(str(run["run_id"])) or run,
                        retry_error,
                        failure_fields={
                            "failure_class": "service_startup_fenced",
                            "failure_retryable": 1,
                            "failure_structured": 1,
                            "failure_user_message": (
                                "GlassHive safely recovered an interrupted worker startup and will retry."
                            ),
                            "failure_recommended_recovery": (
                                "No action is required unless this work remains queued."
                            ),
                            "failure_diagnostic_summary": (
                                "The provider startup was stopped before its durable identity was accepted."
                            ),
                        },
                    )
                    return
                except WorkerPausedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    paused_run = self.store.transition_run_if_state(
                        str(run["run_id"]),
                        "running",
                        "paused",
                        ended_at=None,
                        error_text=str(exc),
                    )
                    durable = paused_run or self.store.get_run(str(run["run_id"])) or run
                    durable_state = str(durable.get("state") or "")
                    current_worker = self.store.get_worker(worker_id) or worker
                    if (
                        not paused_run
                        and str(current_worker.get("compute_release_token") or "")
                        and str(
                            current_worker.get("compute_release_target_run_id") or ""
                        )
                        == str(run["run_id"])
                    ):
                        return
                    if durable_state in TERMINAL_RUN_STATES:
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                        return
                    if durable_state == "queued":
                        # A host resume may requeue the exact run before the
                        # killed provider unwinds. Preserve that newer CAS; the
                        # processor-finally path starts its replacement.
                        self.store.update_worker_state(worker_id, "starting", last_error="")
                        return
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.paused", str(exc))
                    self._emit_callback(worker, "run.paused", run={**run, "state": "paused", "error_text": str(exc)}, message=str(exc))
                    self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
                    return
                except WorkerInterruptedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    current_worker = self.store.get_worker(worker_id) or worker
                    stop_requested = current_worker.get("state") == "stopping"
                    final_state = "cancelled" if stop_requested else "interrupted"
                    finalized_run = self._finalize_run_if_state(
                        run["run_id"],
                        "running",
                        state=final_state,
                        error_text=str(exc),
                        **terminal_generation,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            worker,
                            run,
                            "interruption",
                        )
                        continue
                    self.store.finalize_schedule_for_run(
                        run["run_id"],
                        state="cancelled" if stop_requested else "failed",
                        last_error=str(exc),
                    )
                    self.store.update_worker_state(worker_id, "ready", last_error="")
                    if stop_requested:
                        self.store.accept_cancel_actions_for_run(run["run_id"])
                    event_type = f"run.{final_state}"
                    self.store.add_event(
                        worker["project_id"], worker_id, run["run_id"], event_type, str(exc)
                    )
                    self._emit_callback(
                        worker,
                        event_type,
                        run={**run, **finalized_run},
                        message=str(exc),
                    )
                    if final_state == "interrupted":
                        try:
                            self._settle_interrupted_steer_claim(
                                worker_id, str(run["run_id"])
                            )
                        except Exception:
                            # The interrupted source remains authoritative. Keep
                            # the exact fence for scheduler recovery instead of
                            # dispatching an unproven replacement.
                            logger.exception(
                                "Failed to settle exact interrupted Steer for worker %s",
                                worker_id,
                            )
                    continue
                except WorkerTerminatedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    recovered = self._collect_completed_run(worker, run)
                    if recovered:
                        self._apply_recovered_run(worker, run, recovered)
                        continue
                    finalized_run = self._finalize_run_if_state(
                        run["run_id"],
                        str(run.get("state") or "running"),
                        state="cancelled",
                        error_text=str(exc),
                        **terminal_generation,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            worker, run, "termination"
                        )
                        return
                    self.store.finalize_schedule_for_run(run["run_id"], state="cancelled", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "terminated", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.cancelled", str(exc))
                    self._emit_callback(worker, "run.cancelled", run={**run, "state": "cancelled", "error_text": str(exc)}, message=str(exc))
                    self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
                    return
                except RuntimeErrorBase as exc:
                    durable_liveness_run = self.store.get_run(str(run["run_id"]))
                    if (
                        durable_liveness_run
                        and str(durable_liveness_run.get("state") or "")
                        == "needs_input"
                        and str(
                            durable_liveness_run.get("failure_class") or ""
                        )
                        == "provider_progress_stalled"
                    ):
                        return
                    if not self._processor_is_current(worker_id, generation):
                        return
                    current_worker = self.store.get_worker(worker_id) or worker
                    worker_state = current_worker["state"]
                    final_state = "failed"
                    if worker_state == "paused":
                        final_state = "interrupted"
                    elif worker_state in CLOSED_WORKER_STATES:
                        final_state = "cancelled"
                    refreshed_worker = (
                        self._refresh_runtime_info(
                            worker_id,
                            state=(
                                worker_state
                                if worker_state == "paused" or worker_state in CLOSED_WORKER_STATES
                                else "ready"
                            ),
                            last_error=str(exc),
                        )
                        or self.store.get_worker(worker_id)
                        or current_worker
                    )
                    failure_fields = (
                        classify_runtime_error(
                            exc,
                            runtime_name=str(refreshed_worker.get("profile") or refreshed_worker.get("runtime") or "worker"),
                        ).as_store_fields()
                        if final_state == "failed"
                        else {}
                    )
                    if final_state == "failed":
                        self._record_provider_route_failure(
                            refreshed_worker, run, failure_fields, exc
                        )
                    if (
                        final_state == "failed"
                        and str(failure_fields.get("failure_class") or "") != "glasshive_evidence_check_failed"
                    ):
                        recovered = self._collect_completed_run(refreshed_worker, run)
                        if recovered:
                            self._apply_recovered_run(refreshed_worker, run, recovered)
                            continue
                    if final_state == "failed" and self._switch_quota_exhausted_run_to_fallback(
                        refreshed_worker,
                        run,
                        exc,
                        failure_fields,
                    ):
                        return
                    if (
                        final_state == "failed"
                        and bool(failure_fields.get("failure_retryable"))
                        and str(failure_fields.get("failure_class") or "")
                        in {"host_worker_busy", "host_capacity", "provider_rate_limited"}
                        and (
                            str(failure_fields.get("failure_class") or "")
                            != "provider_rate_limited"
                            or getattr(exc, "retry_after_s", None) is not None
                        )
                    ):
                        self._requeue_retryable_run(refreshed_worker, run, exc, failure_fields=failure_fields)
                        return
                    finalized_run = self._finalize_run_if_state(
                        run["run_id"],
                        "running",
                        final_state,
                        error_text=str(exc),
                        **terminal_generation,
                        **failure_fields,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            current_worker,
                            run,
                            final_state,
                        )
                        if worker_state in {"paused", "terminated"}:
                            return
                        continue
                    self.store.finalize_schedule_for_run(
                        run["run_id"],
                        state="cancelled" if final_state == "cancelled" else "failed",
                        last_error=str(exc),
                    )
                    self.store.update_worker_state(
                        worker_id,
                        worker_state
                        if worker_state == "paused" or worker_state in CLOSED_WORKER_STATES
                        else "ready",
                        last_error=str(exc),
                    )
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], f"run.{final_state}", str(exc))
                    failed_run = {
                        **run,
                        **finalized_run,
                        "state": final_state,
                        "error_text": str(exc),
                        **failure_fields,
                    }
                    callback_worker = self.store.get_worker(worker_id) or refreshed_worker
                    deliverable = (
                        self._completion_deliverable(callback_worker, failed_run, "", str(exc))
                        if final_state == "failed"
                        else None
                    )
                    failure_message = runtime_failure_callback_message(failure_fields, str(exc))
                    self._emit_callback(
                        callback_worker,
                        f"run.{final_state}",
                        run=failed_run,
                        message=failure_message,
                        deliverable=deliverable,
                    )
                    self._wake_host_capacity_waiters(callback_worker)
                    if worker_state == "paused" or worker_state in CLOSED_WORKER_STATES:
                        return
                    continue
                except Exception as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    failure_fields = classify_runtime_error(
                        exc,
                        runtime_name=str(worker.get("profile") or worker.get("runtime") or "worker"),
                    ).as_store_fields()
                    self._record_provider_route_failure(
                        worker, run, failure_fields, exc
                    )
                    finalized_run = self._finalize_run_if_state(
                        run["run_id"],
                        "running",
                        "failed",
                        error_text=str(exc),
                        **terminal_generation,
                        **failure_fields,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(worker, run, "failed")
                        continue
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", str(exc))
                    failed_run = {
                        **run,
                        **finalized_run,
                        "state": "failed",
                        "error_text": str(exc),
                        **failure_fields,
                    }
                    failure_message = runtime_failure_callback_message(failure_fields, str(exc))
                    self._emit_callback(worker, "run.failed", run=failed_run, message=failure_message)
                    self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
                    continue

                if not self._processor_is_current(worker_id, generation):
                    return
                completion_expected_state = self._settle_native_children(
                    worker,
                    run,
                    output,
                    terminal_generation=terminal_generation,
                )
                if completion_expected_state not in {"running", "settling"}:
                    self._record_late_processor_terminal_ignored(
                        worker,
                        run,
                        "completion",
                    )
                    continue
                completed_run = self._finalize_run_if_state(
                    run["run_id"],
                    completion_expected_state,
                    "completed",
                    output_text=output,
                    **terminal_generation,
                )
                if not completed_run:
                    self._record_late_processor_terminal_ignored(worker, run, "completion")
                    continue
                self._clear_provider_route_health(worker, completed_run)
                self.store.finalize_schedule_for_run(run["run_id"], state="completed")
                self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
                message = terminal_callback_message(output)
                full_message = terminal_callback_full_message(output)
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
                completed_run = {**run, **completed_run, "state": "completed", "output_text": output}
                refreshed_worker = self._refresh_runtime_info(worker_id, state="ready", last_error="") or self.store.get_worker(worker_id) or worker
                deliverable = self._completion_deliverable(refreshed_worker, completed_run, output)
                self._promote_completed_deliverable(refreshed_worker, completed_run, deliverable)
                self._emit_callback(
                    refreshed_worker,
                    "run.completed",
                    run=completed_run,
                    message=message or "Run completed",
                    full_message=full_message if full_message != message else "",
                    deliverable=deliverable,
                )
                current_run = None
                runtime_invoked = False
        except Exception as exc:
            logger.exception(
                "Unexpected GlassHive worker processor failure",
                extra={
                    "worker_id": worker_id,
                    "run_id": str((current_run or {}).get("run_id") or ""),
                },
            )
            if current_run and not preserve_start_fence:
                try:
                    durable_run = self.store.get_run(str(current_run["run_id"]))
                    if durable_run and str(durable_run.get("state") or "") in {
                        "claimed",
                        "admitted",
                        "running",
                    }:
                        recovered = (
                            self._collect_completed_run(
                                self.store.get_worker(worker_id) or {}, durable_run
                            )
                            if runtime_invoked
                            else None
                        )
                        if recovered:
                            self._apply_recovered_run(
                                self.store.get_worker(worker_id) or {},
                                durable_run,
                                recovered,
                            )
                        elif not runtime_invoked:
                            recovery_error = RuntimeErrorBase(
                                "GlassHive recovered an internal processor interruption "
                                "before provider execution started."
                            )
                            self._requeue_retryable_run(
                                self.store.get_worker(worker_id) or {
                                    "worker_id": worker_id,
                                    "project_id": str(
                                        durable_run.get("project_id") or ""
                                    ),
                                },
                                durable_run,
                                recovery_error,
                                failure_fields={
                                    "failure_class": "service_processor_unexpected",
                                    "failure_retryable": 1,
                                    "failure_structured": 1,
                                    "failure_user_message": (
                                        "GlassHive recovered an internal worker interruption and "
                                        "will retry this work."
                                    ),
                                    "failure_recommended_recovery": (
                                        "No action is required unless the work remains queued."
                                    ),
                                    "failure_diagnostic_summary": (
                                        "The processor exited before invoking the provider runtime."
                                    ),
                                },
                            )
                        else:
                            failure_message = (
                                "GlassHive could not safely confirm the provider result after "
                                "an internal processor interruption."
                            )
                            failed_run = self._finalize_run_if_state(
                                str(durable_run["run_id"]),
                                "running",
                                "failed",
                                error_text=failure_message,
                                **terminal_generation,
                                failure_class="service_processor_unexpected",
                                failure_retryable=1,
                                failure_structured=1,
                                failure_user_message=failure_message,
                                failure_recommended_recovery=(
                                    "Retry the work after reviewing any partial workspace output."
                                ),
                                failure_diagnostic_summary=(
                                    "The provider runtime returned, but processor finalization "
                                    "did not complete."
                                ),
                            )
                            if failed_run:
                                self.store.finalize_schedule_for_run(
                                    str(durable_run["run_id"]),
                                    state="failed",
                                    last_error=failure_message,
                                )
                                failed_worker = (
                                    self.store.update_worker_state(
                                        worker_id,
                                        "ready",
                                        last_error=failure_message,
                                    )
                                    or self.store.get_worker(worker_id)
                                    or {}
                                )
                                self.store.add_event(
                                    str(failed_worker.get("project_id") or ""),
                                    worker_id,
                                    str(durable_run["run_id"]),
                                    "run.failed",
                                    failure_message,
                                )
                                self._emit_callback(
                                    failed_worker,
                                    "run.failed",
                                    run={**durable_run, **failed_run},
                                    message=failure_message,
                                )
                except Exception:
                    logger.exception(
                        "Failed to durably recover unexpected worker processor failure",
                        extra={"worker_id": worker_id},
                    )
        finally:
            if current_run and not preserve_start_fence:
                try:
                    self._release_host_run_lease(
                        str(current_run["run_id"]), reason="processor_exit"
                    )
                except Exception:
                    logger.exception(
                        "Failed to release run lease after worker processor exit",
                        extra={
                            "worker_id": worker_id,
                            "run_id": str(current_run.get("run_id") or ""),
                        },
                    )
            try:
                if self._release_processor(worker_id, generation):
                    pending = self.store.get_worker(worker_id)
                    if (
                        pending
                        and pending["state"]
                        not in {"paused", "needs_input", "stopping", "terminated"}
                        and not str(pending.get("compute_release_token") or "").strip()
                        and not self.store.has_unconfirmed_host_run_start(worker_id)
                    ):
                        if self.store.peek_next_queued_run(worker_id):
                            self._ensure_worker_processor(worker_id)
            except Exception:
                logger.exception(
                    "Failed to finalize GlassHive worker processor ownership",
                    extra={"worker_id": worker_id},
                )

    def _consume_local_qa(
        self,
        boundary: str,
        worker: dict,
        run: dict | None = None,
        *,
        artifact_id: str = "",
    ) -> LocalQAFaultDirective | None:
        plane = self._local_qa_control_plane
        if plane is None:
            return None
        owner_id = str(worker.get("owner_id") or "").strip()
        tenant_id = str(worker.get("tenant_id") or "local").strip() or "local"
        worker_id = str(worker.get("worker_id") or "").strip()
        if not owner_id or not worker_id:
            return None
        delegation = self.store.get_delegation_for_worker(
            worker_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if not delegation:
            return None
        run_id = (
            str((run or {}).get("run_id") or "").strip()
            if boundary in LOCAL_QA_RUN_SCOPED_FAULTS
            else ""
        )
        return plane.consume(
            boundary,
            owner_id=owner_id,
            work_id=str(delegation.get("work_ref") or ""),
            run_id=run_id,
            artifact_id=artifact_id,
        )

    def _record_local_qa_effect(
        self,
        directive: LocalQAFaultDirective | None,
        outcome: str,
    ) -> None:
        if directive is not None and self._local_qa_control_plane is not None:
            self._local_qa_control_plane.record_effect(directive, outcome=outcome)

    def local_qa_artifact_fault(
        self,
        worker: dict,
        artifact_path: Path,
        *,
        boundary: str = "artifact_unavailable_restart_recovery",
    ) -> str:
        """Consume one exact traced artifact fault immediately before serving bytes."""

        if boundary not in {
            "artifact_link_expired",
            "artifact_unavailable_restart_recovery",
        }:
            return ""
        try:
            descriptor=os.open(artifact_path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0))
            with os.fdopen(descriptor,"rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    return ""
                content_hash=hashlib.sha256()
                while chunk:=source.read(256*1024):
                    content_hash.update(chunk)
                digest=content_hash.hexdigest()
        except OSError:
            return ""
        run_id = ""
        delegation = self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if delegation:
            run_id = str(delegation.get("current_run_id") or "")
        run = self.store.get_run(run_id) if run_id else None
        directive = self._consume_local_qa(
            boundary,
            worker,
            run,
            artifact_id="artifact_sha256:" + digest,
        )
        if directive is None:
            return ""
        self._record_local_qa_effect(
            directive,
            "signed_link_rejected_before_artifact_serve"
            if boundary == "artifact_link_expired"
            else "artifact_temporarily_unavailable_before_serve",
        )
        return boundary

    @property
    def executor_id(self) -> str:
        """Stable owner for short-lived durable action execution leases."""

        return self._executor_id

    def _host_capacity_policy(self) -> dict[str, int]:
        """Return the immutable policy shared by dispatch and lease admission."""

        return dict(self._configured_host_capacity)

    def _viventium_callback_context_ready(
        self,
        worker: dict,
        callbacks: dict[str, object],
    ) -> bool:
        """Accept either legacy parent identity or Core's opaque durable origin binding."""

        if not _missing_parent_callback_fields(callbacks):
            return True
        origin_ref = str(callbacks.get("origin_ref") or "").strip()
        if not origin_ref:
            return False
        delegation = self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        return bool(
            delegation
            and str(delegation.get("origin_ref") or "").strip() == origin_ref
            and str(delegation.get("work_ref") or "").strip()
        )

    def _callback_attempt_number(self, run: dict | None) -> int:
        if not run or not str(run.get("run_id") or "").strip():
            return 0
        attempt_id = str(run.get("active_attempt_id") or "").strip()
        if attempt_id:
            attempt = self.store.get_run_attempt(attempt_id)
            if attempt is not None:
                number = int(attempt.get("attempt_number") or 0)
                if number > 0:
                    return number
        attempts = self.store.list_run_attempts(str(run.get("run_id") or ""))
        if attempts:
            number = int(attempts[-1].get("attempt_number") or 0)
            if number > 0:
                return number
        return 0

    @staticmethod
    def _terminal_callback_response_decision(
        response: httpx.Response | object,
        payload: dict[str, Any],
    ) -> tuple[str, int]:
        """Validate the receiver's exact monotonic CAS acknowledgement."""

        revision = payload.get("result_revision")
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            return "accepted", 0
        try:
            body = response.json()
        except Exception:
            return "invalid", 0
        if not isinstance(body, dict):
            return "invalid", 0
        status = str(body.get("callback_status") or "")
        exact_incoming = bool(
            str(body.get("callback_id") or "")
            == str(payload.get("callback_id") or "")
            and str(body.get("run_id") or "")
            == str(payload.get("run_id") or "")
            and body.get("result_revision") == revision
            and str(body.get("result_digest") or "")
            == str(payload.get("result_digest") or "")
        )
        current_revision = body.get("current_result_revision")
        if (
            not exact_incoming
            or not isinstance(current_revision, int)
            or isinstance(current_revision, bool)
        ):
            return "invalid", 0
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status in {"accepted", "idempotent"}:
            exact_current = bool(
                200 <= status_code < 300
                and current_revision == revision
                and str(body.get("current_callback_id") or "")
                == str(payload.get("callback_id") or "")
                and str(body.get("current_result_digest") or "")
                == str(payload.get("result_digest") or "")
            )
            return ("accepted", revision) if exact_current else ("invalid", 0)
        if status == "superseded":
            newer_current = bool(
                status_code == 409
                and current_revision > revision
                and re.fullmatch(
                    r"cb_terminal_[0-9a-f]{64}",
                    str(body.get("current_callback_id") or ""),
                )
                and re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(body.get("current_result_digest") or ""),
                )
            )
            return (
                ("superseded", current_revision)
                if newer_current
                else ("invalid", 0)
            )
        if status == "conflict":
            current_callback_id = str(body.get("current_callback_id") or "")
            current_result_digest = str(body.get("current_result_digest") or "")
            exact_conflict = bool(
                status_code == 409
                and current_revision == revision
                and re.fullmatch(
                    r"cb_terminal_[0-9a-f]{64}", current_callback_id
                )
                and re.fullmatch(
                    r"sha256:[0-9a-f]{64}", current_result_digest
                )
                and (
                    current_callback_id != str(payload.get("callback_id") or "")
                    or current_result_digest
                    != str(payload.get("result_digest") or "")
                )
            )
            return ("conflict", revision) if exact_conflict else ("invalid", 0)
        return "invalid", 0

    def _reconcile_terminal_callback_intents(
        self,
        *,
        created_before: str | None = None,
        limit: int = 50,
    ) -> int:
        """Create missing terminal outbox rows from exact durable generations."""

        batch_limit = max(1, min(int(limit), 1000))
        scan_budget = min(batch_limit * 8, 1000)
        scanned = 0
        inserted = 0
        while scanned < scan_budget and inserted < batch_limit:
            remaining = min(batch_limit, scan_budget - scanned)
            runs = self.store.list_terminal_runs_missing_callback_intent(
                created_before=created_before,
                limit=remaining,
            )
            if not runs:
                break
            scanned += len(runs)
            removed_legacy_sink = False
            for run in runs:
                worker = self.store.get_worker(str(run.get("worker_id") or ""))
                if not worker:
                    continue
                callbacks = self._callback_config_for(worker)
                callback_url = str(
                    callbacks.get("events_webhook_url")
                    or callbacks.get("url")
                    or ""
                ).strip()
                state = str(run.get("state") or "")
                attempt_number = self._callback_attempt_number(run)
                unavailable_reason = ""
                if not callback_url:
                    unavailable_reason = "callback_config_missing"
                elif _is_viventium_callback_url(
                    callback_url
                ) and not self._viventium_callback_context_ready(worker, callbacks):
                    unavailable_reason = "callback_context_incomplete"
                elif attempt_number < 1:
                    existing_callbacks = self.store.list_callback_outbox_for_run(
                        str(run.get("run_id") or ""),
                        tenant_id=str(worker.get("tenant_id") or "local"),
                        owner_id=str(worker.get("owner_id") or ""),
                    )
                    if any(
                        str(record.get("event_type") or "") == f"run.{state}"
                        and int(record.get("attempt_number") or 0) == 0
                        for record in existing_callbacks
                    ):
                        unavailable_reason = "callback_context_incomplete"
                        removed_legacy_sink = True
                if unavailable_reason:
                    self.store.mark_terminal_callback_reconciliation_unavailable(
                        str(run.get("run_id") or ""),
                        expected_state=state,
                        expected_ended_at=str(run.get("ended_at") or ""),
                        expected_attempt_number=attempt_number,
                        expected_callback_contract_digest=Store.callback_contract_digest(
                            worker
                        ),
                        reason_code=unavailable_reason,
                    )
                    continue
                output_text = str(run.get("output_text") or "")
                error_text = str(run.get("error_text") or "")
                if state == "completed":
                    message = terminal_callback_message(output_text) or "Run completed"
                    full_message = terminal_callback_full_message(output_text)
                    if full_message == message:
                        full_message = ""
                elif state == "failed":
                    failure_fields = {
                        key: run.get(key)
                        for key in (
                            "failure_class",
                            "failure_retryable",
                            "failure_structured",
                            "failure_user_message",
                            "failure_recommended_recovery",
                            "failure_diagnostic_summary",
                        )
                    }
                    message = runtime_failure_callback_message(
                        failure_fields, error_text or "Run failed"
                    )
                    full_message = ""
                else:
                    message = error_text or f"Run {state}"
                    full_message = ""
                deliverable = (
                    self._completion_deliverable(
                        worker, run, output_text, error_text
                    )
                    if state in {"completed", "failed"}
                    else None
                )
                record = self._emit_callback(
                    worker,
                    f"run.{state}",
                    run=run,
                    message=message,
                    full_message=full_message,
                    deliverable=deliverable,
                    submit_delivery=False,
                )
                if record is None:
                    self.store.mark_terminal_callback_reconciliation_unavailable(
                        str(run.get("run_id") or ""),
                        expected_state=state,
                        expected_ended_at=str(run.get("ended_at") or ""),
                        expected_attempt_number=attempt_number,
                        expected_callback_contract_digest=Store.callback_contract_digest(
                            worker
                        ),
                        reason_code="callback_context_incomplete",
                    )
                    continue
                if bool(record.get("_inserted")):
                    inserted += 1
            if len(runs) < remaining or not removed_legacy_sink:
                break
        return inserted

    def _replay_startup_recovery(self) -> None:
        if self._shutdown_event.is_set():
            return
        # The retained workspace inventory may be large. Its per-worker recovery
        # already uses current run ownership and durable lease fences; run it on
        # the existing lifespan-owned recovery thread so it cannot hold HTTP
        # startup hostage. Credential cleanup and host lease recovery still run
        # synchronously before this thread starts.
        if self._reconcile_on_startup:
            try:
                self.reconcile_all_workers()
            except Exception:
                logger.error(
                    "GlassHive startup worker recovery faulted safely",
                    extra={"error_code": "transient_dependency"},
                )
        if self._shutdown_event.is_set():
            return
        try:
            self.reap_needs_input_workers_once()
        except Exception:
            logger.error(
                "GlassHive startup needs-input compute recovery faulted safely",
                extra={"error_code": "transient_dependency"},
            )
        if self._shutdown_event.is_set():
            return
        try:
            self.peers.recover_pending()
        except Exception:
            logger.error("Peer message recovery faulted safely", extra={"error_code": "transient_dependency"})
        try:
            self.reconcile_restart_authority_backlog_once()
        except Exception:
            logger.error("GlassHive startup restart-authority recovery faulted safely",
                         extra={"error_code": "transient_dependency"})
        if self._shutdown_event.is_set():
            return
        try:
            self._replay_pending_capability_grant_revocations(
                created_before=self._startup_recovery_cutoff
            )
        except Exception:
            logger.error(
                "GlassHive startup capability revocation recovery faulted safely",
                extra={"error_code": "transient_dependency"},
            )
        if self._shutdown_event.is_set():
            return
        try:
            self._replay_pending_lifecycle_effects(
                created_before=self._startup_recovery_cutoff
            )
        except Exception:
            logger.error(
                "GlassHive startup lifecycle recovery faulted safely",
                extra={"error_code": "transient_dependency"},
            )
        if self._shutdown_event.is_set():
            return
        try:
            self._reconcile_terminal_callback_intents(
                created_before=self._startup_recovery_cutoff
            )
        except Exception:
            logger.error(
                "GlassHive startup terminal callback reconciliation faulted safely",
                extra={"error_code": "transient_dependency"},
            )
        if self._shutdown_event.is_set():
            return
        self._replay_pending_callbacks(
            created_before=self._startup_recovery_cutoff
        )

    def _callback_retry_tick(self) -> None:
        """Reconcile and replay one bounded callback recovery pass."""

        operations: list[tuple[Callable[[], object], str]] = [
            (
                self._reconcile_terminal_callback_intents,
                "GlassHive terminal callback reconciliation faulted safely",
            ),
            (
                self._replay_pending_capability_grant_revocations,
                "GlassHive capability revocation replay faulted safely",
            ),
            (
                self._replay_pending_lifecycle_effects,
                "GlassHive lifecycle replay faulted safely",
            ),
            (
                self._replay_pending_callbacks,
                "GlassHive callback replay faulted safely",
            ),
        ]
        if self._provider_request_reconciler is not None:
            operations.append(
                (
                    lambda: self._provider_request_reconciler(""),
                    "GlassHive provider request reconciliation faulted safely",
                )
            )
        for operation, message in operations:
            try:
                operation()
            except Exception:
                logger.error(
                    message,
                    extra={"error_code": "transient_dependency"},
                )

    @staticmethod
    def _capability_revocation_retry_delay_s(record: dict) -> float:
        attempts = max(1, int(record.get("attempts") or 1))
        return float(min(300, 2 ** min(attempts, 8)))

    def _retry_capability_grant_revocation(
        self, record: dict, error_code: str
    ) -> None:
        safe_code = str(error_code or "").strip().lower()
        if safe_code not in {
            "broker_revocation_rejected",
            "broker_revocation_unavailable",
            "transient_dependency",
        }:
            safe_code = "unknown"
        try:
            self.store.retry_capability_grant_revocation(
                str(record.get("revocation_id") or ""),
                self._executor_id,
                lease_epoch=int(record.get("lease_epoch") or 0),
                error_code=safe_code,
                retry_delay_s=self._capability_revocation_retry_delay_s(record),
            )
        except Exception:
            logger.error(
                "GlassHive capability revocation retry persistence unavailable",
                extra={"error_code": "transient_dependency"},
            )

    def _apply_capability_grant_revocation(self, record: dict) -> None:
        body = {
            "authorizationRef": str(record.get("authorization_ref") or ""),
            "originRef": str(record.get("origin_ref") or ""),
            "workRef": str(record.get("work_ref") or ""),
            "workerId": str(record.get("worker_id") or ""),
            "runId": str(record.get("run_id") or ""),
            "grantId": str(record.get("grant_id") or ""),
            **{
                key: str(record[column])
                for key, column in (
                    ("containerGenerationId", "container_generation_id"),
                    ("hostStartupLeaseId", "host_startup_lease_id"),
                )
                if record.get(column)
            },
        }
        load_viventium_runtime_env(
            {
                "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
                "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET",
            }
        )
        try:
            revoke_capability_grant(
                str(os.environ.get("VIVENTIUM_GLASSHIVE_ADMISSION_URL") or ""),
                secret=str(
                    os.environ.get("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET") or ""
                ),
                body=body,
                timeout_seconds=_bounded_float_env(
                    "VIVENTIUM_GLASSHIVE_ADMISSION_TIMEOUT_S",
                    5.0,
                    min_value=0.1,
                    max_value=30.0,
                ),
            )
        except BrokerAdmissionError as exc:
            self._retry_capability_grant_revocation(record, exc.code)
            return
        except Exception:
            self._retry_capability_grant_revocation(record, "unknown")
            return
        self.store.mark_capability_grant_revocation_applied(
            str(record.get("revocation_id") or ""),
            self._executor_id,
            lease_epoch=int(record.get("lease_epoch") or 0),
        )

    def _replay_pending_capability_grant_revocations(
        self,
        *,
        created_before: str | None = None,
        revocation_id: str | None = None,
    ) -> None:
        if self._shutdown_event.is_set():
            return
        self.store.activate_due_capability_grant_revocations()
        for _ in range(100):
            if self._shutdown_event.is_set():
                return
            record = self.store.claim_next_capability_grant_revocation(
                self._executor_id,
                ttl_s=60,
                created_before=created_before,
                revocation_id=revocation_id,
            )
            if not record:
                return
            self._apply_capability_grant_revocation(record)
            if revocation_id:
                return

    def _replay_pending_lifecycle_effects(
        self, *, created_before: str | None = None
    ) -> None:
        """Drain every durable sink kind independently with leased ownership."""

        effect_kinds = (
            # Revocation must commit before a terminal callback is visible.
            "signed_links.revoke_worker",
            "callback.worker_terminated",
            "callback.work_stopped",
            "callback.run_cancelled",
            "callback.run_paused",
            "callback.run_resumed",
            "callback.run_resumed_in_place",
            "callback.run_resumed_queued",
            "callback.run_interrupted",
            "callback.run_steered",
            "callback.run_needs_input",
            "callback.worker_paused",
            "callback.worker_resumed",
        )
        # Visit one row per kind per round. A retry-wait row cannot block a
        # different effect kind, worker, or sink.
        faulted_kinds: set[str] = set()
        for _round in range(100):
            if self._shutdown_event.is_set():
                return
            claimed_any = False
            for effect_kind in effect_kinds:
                if self._shutdown_event.is_set():
                    return
                if effect_kind in faulted_kinds:
                    continue
                try:
                    effect = self.store.claim_next_lifecycle_effect(
                        self._executor_id,
                        ttl_s=60,
                        effect_kinds=(effect_kind,),
                        created_before=created_before,
                    )
                except Exception:
                    faulted_kinds.add(effect_kind)
                    logger.error(
                        "GlassHive lifecycle effect claim will retry",
                        extra={
                            "effect_kind": effect_kind,
                            "error_code": "transient_dependency",
                        },
                    )
                    continue
                if not effect:
                    continue
                claimed_any = True
                try:
                    self._apply_lifecycle_effect(effect)
                except Exception:
                    # A single malformed dependency or sink must not kill the
                    # recurring recovery thread or globally HOL-block kinds.
                    self._retry_lifecycle_effect(effect, "unknown")
                    faulted_kinds.add(effect_kind)
                    logger.error(
                        "GlassHive lifecycle effect application faulted safely",
                        extra={
                            "effect_kind": effect_kind,
                            "worker_id": str(effect.get("worker_id") or ""),
                            "error_code": "unknown",
                        },
                    )
            if not claimed_any:
                return

    @staticmethod
    def _lifecycle_effect_retry_delay_s(effect: dict) -> float:
        attempts = max(1, int(effect.get("attempts") or 1))
        return float(min(300, 2 ** min(attempts, 8)))

    def _retry_lifecycle_effect(self, effect: dict, error_code: str) -> bool:
        try:
            retried = self.store.retry_lifecycle_effect(
                str(effect.get("effect_id") or ""),
                self._executor_id,
                lease_epoch=int(effect.get("lease_epoch") or 0),
                error_code=error_code,
                retry_delay_s=self._lifecycle_effect_retry_delay_s(effect),
            )
        except Exception:
            logger.error(
                "GlassHive lifecycle effect retry persistence unavailable",
                extra={
                    "effect_kind": str(effect.get("effect_kind") or ""),
                    "worker_id": str(effect.get("worker_id") or ""),
                    "error_code": "transient_dependency",
                },
            )
            return False
        if not retried:
            return False
        logger.warning(
            "GlassHive lifecycle effect will retry",
            extra={
                "effect_kind": str(effect.get("effect_kind") or ""),
                "worker_id": str(effect.get("worker_id") or ""),
                "error_code": error_code,
            },
        )
        return True

    def _apply_lifecycle_effect(self, effect: dict) -> None:
        effect_id = str(effect.get("effect_id") or "")
        lease_epoch = int(effect.get("lease_epoch") or 0)
        effect_kind = str(effect.get("effect_kind") or "")
        worker = self.store.get_worker(str(effect.get("worker_id") or ""))
        if not worker:
            self._retry_lifecycle_effect(effect, "transient_dependency")
            return

        if effect_kind == "signed_links.revoke_worker":
            try:
                revoke_signed_link_refs_for_worker(str(worker["worker_id"]))
            except Exception:
                self._retry_lifecycle_effect(effect, "signed_link_revoke_failed")
                return
            self.store.mark_lifecycle_effect_applied(
                effect_id,
                self._executor_id,
                lease_epoch=lease_epoch,
            )
            return

        if (
            effect_kind == "callback.worker_terminated"
            and (
                not is_worker_signed_link_revoked(
                    str(worker.get("worker_id") or "")
                )
                or not self.store.paired_lifecycle_effect_is_applied(
                    effect,
                    required_effect_kind="signed_links.revoke_worker",
                )
            )
        ):
            self._retry_lifecycle_effect(effect, "transient_dependency")
            return

        run_id = str(effect.get("run_id") or "")
        run = self.store.get_run(run_id) if run_id else None
        run_required = effect_kind not in {
            "callback.worker_paused",
            "callback.worker_resumed",
            "callback.worker_terminated",
        }
        if run_required and not run:
            self._retry_lifecycle_effect(effect, "transient_dependency")
            return
        event_type, message = {
            "callback.run_cancelled": ("run.cancelled", "Run cancelled"),
            "callback.run_paused": ("run.paused", "Worker paused"),
            "callback.run_resumed": (
                "run.started"
                if str((run or {}).get("state") or "") == "running"
                else "run.queued",
                "Paused run resumed",
            ),
            "callback.run_resumed_in_place": (
                "run.started",
                "Paused run resumed",
            ),
            "callback.run_resumed_queued": (
                "run.queued",
                "Paused run queued for execution restart",
            ),
            "callback.run_interrupted": (
                "run.interrupted",
                "Run interruption accepted",
            ),
            "callback.run_steered": (
                "run.queued",
                "Replacement steer instruction queued",
            ),
            "callback.run_needs_input": (
                "run.needs_input",
                "Mission needs attention because provider progress stopped",
            ),
            "callback.work_stopped": ("run.cancelled", "Work stop confirmed"),
            "callback.worker_paused": ("worker.paused", "Worker paused"),
            "callback.worker_resumed": ("worker.resumed", "Worker resumed"),
            "callback.worker_terminated": (
                "worker.terminated",
                "Worker terminated",
            ),
        }[effect_kind]
        if effect_kind == "callback.run_needs_input":
            # The durable run row owns the attention wording (provider stall,
            # missing account, ...). Keep the generic text only when no typed
            # user message was recorded.
            attention_message = str(
                (run or {}).get("failure_user_message") or ""
            ).strip()
            if attention_message:
                message = attention_message
        callback_id = "cb_effect_" + effect_id
        existing_record = self.store.get_callback_outbox(callback_id)
        if existing_record is not None:
            expected_run_id = str((run or {}).get("run_id") or "")
            if (
                str(existing_record.get("worker_id") or "")
                != str(worker.get("worker_id") or "")
                or str(existing_record.get("event_type") or "") != event_type
                or str(existing_record.get("run_id") or "") != expected_run_id
            ):
                self._retry_lifecycle_effect(effect, "callback_enqueue_failed")
                return
            # A deterministic callback row is the durable sink. Its own retry
            # loop owns delivery even if callback configuration later vanishes.
            self.store.mark_lifecycle_effect_applied(
                effect_id,
                self._executor_id,
                lease_epoch=lease_epoch,
            )
            return

        # A worker without a declared callback contract has no callback sink to recover. Retrying
        # that permanent no-op can never succeed and previously created an unbounded journal/log
        # storm for ordinary conversation workers. A declared but incomplete callback contract
        # still retries below because its endpoint or origin binding can be repaired later.
        callback_contract = (self._bootstrap_bundle_for(worker) or {}).get("callbacks")
        if not isinstance(callback_contract, dict) or not callback_contract:
            self.store.mark_lifecycle_effect_applied(
                effect_id,
                self._executor_id,
                lease_epoch=lease_epoch,
            )
            return

        callbacks = self._callback_config_for(worker)
        callback_url = str(
            callbacks.get("events_webhook_url") or callbacks.get("url") or ""
        ).strip()
        if not callback_url or (
            _is_viventium_callback_url(callback_url)
            and not self._viventium_callback_context_ready(worker, callbacks)
        ):
            self._retry_lifecycle_effect(effect, "callback_config_missing")
            return
        try:
            record = self._emit_callback(
                worker,
                event_type,
                run=run,
                message=message,
                callback_id=callback_id,
                insert_once=True,
                submit_delivery=False,
            )
        except Exception:
            self._retry_lifecycle_effect(effect, "callback_enqueue_failed")
            return
        if record is None:
            self._retry_lifecycle_effect(effect, "callback_enqueue_failed")
            return
        # HTTP delivery is owned by callback_outbox. This sink completes once
        # its immutable outbox row durably exists.
        applied = self.store.mark_lifecycle_effect_applied(
            effect_id,
            self._executor_id,
            lease_epoch=lease_epoch,
        )
        if applied and record and not self._shutdown_event.is_set():
            delivery_record = {
                key: value for key, value in record.items() if key != "_inserted"
            }
            self.executor.submit(
                self._deliver_callback_record,
                dict(worker),
                delivery_record,
                callbacks,
            )

    @staticmethod
    def _trusted_run_lane(worker: dict | None) -> str:
        return (
            "conversation"
            if isinstance(worker, dict)
            and str(worker.get("trusted_run_lane") or "").strip().lower()
            == "conversation"
            else "mission"
        )











    def _delegation_callback_state(
        self,
        delegation: dict,
        *,
        callback_run: dict | None,
    ) -> tuple[str, bool]:
        """Project authoritative mission truth without hiding per-run evidence."""

        worker_id = str(delegation.get("worker_id") or "")
        active = self.store.list_nonterminal_runs_for_worker(worker_id)
        callback_run_id = str((callback_run or {}).get("run_id") or "")
        callback_state = str((callback_run or {}).get("state") or "")
        if callback_run_id and callback_state in TERMINAL_RUN_STATES:
            active = [
                item
                for item in active
                if str(item.get("run_id") or "") != callback_run_id
            ]
        if active:
            state_priority = (
                "running",
                "settling",
                "paused",
                "needs_input",
                "queued",
            )
            states = {str(item.get("state") or "") for item in active}
            state = next((item for item in state_priority if item in states), "queued")
            return state, False

        latest = callback_run
        if not latest:
            current_run_id = str(delegation.get("current_run_id") or "")
            latest = self.store.get_run(current_run_id) if current_run_id else None
        state = str((latest or {}).get("state") or "failed")
        if state == "interrupted":
            state = "cancelled"
        if state not in TERMINAL_RUN_STATES:
            # Missing/unknown mission truth must fail nonterminal, never cause
            # Core to terminalize a WorkRef accidentally.
            return state or "queued", False
        return state, True


    def _run_start_callback_record(
        self,
        worker: dict,
        run: dict,
        startup_token: str,
    ) -> tuple[dict[str, object] | None, dict]:
        """Build the bounded durable callback row published by the startup CAS.

        This deliberately does not mint signed links or perform network I/O while
        the lifecycle flock is held. Delivery may add an exact run-action
        capability after the transaction commits.
        """

        callbacks = self._callback_config_for(worker)
        url = str(
            callbacks.get("events_webhook_url") or callbacks.get("url") or ""
        ).strip()
        if not url:
            return None, callbacks
        if _is_viventium_callback_url(url):
            if not self._viventium_callback_context_ready(worker, callbacks):
                return None, callbacks
        attempt_number = self._callback_attempt_number(run)
        if attempt_number < 1:
            return None, callbacks
        callback_id = "cb_start_" + hashlib.sha256(
            str(startup_token).encode("utf-8")
        ).hexdigest()
        payload: dict[str, object] = {
            "callback_id": callback_id,
            "callback_ts": int(time.time()),
            "attempt_number": attempt_number,
            "event": "run.started",
            "project_id": worker.get("project_id"),
            "worker_id": worker.get("worker_id"),
            "run_id": run.get("run_id"),
            "run_state": "running",
            "message": public_callback_message_text(
                str(run.get("instruction") or "")
            )[:TERMINAL_CALLBACK_MESSAGE_LIMIT],
            "full_message": "",
        }
        for field in (
            "user_id",
            "agent_id",
            "conversation_id",
            "parent_message_id",
            "message_id",
            "surface",
            "input_mode",
            "stream_id",
            "voice_call_session_id",
            "voice_request_id",
            "telegram_chat_id",
            "telegram_user_id",
            "telegram_message_id",
            "logical_turn_id",
            "logical_turn_revision",
        ):
            payload[field] = callbacks.get(field)
        delegation = self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if delegation:
            payload["origin_ref"] = str(delegation.get("origin_ref") or "")
            payload["work_ref"] = str(delegation.get("work_ref") or "")
            work_state, work_terminal = self._delegation_callback_state(
                delegation,
                callback_run={**run, "state": "running"},
            )
            payload["work_state"] = work_state
            payload["work_terminal"] = work_terminal
        elif _is_viventium_callback_url(url):
            return None, callbacks
        return {
            "url": url,
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "attempt_number": attempt_number,
        }, callbacks

    def _compute_release_claim_ttl_s(self) -> int:
        return _bounded_int_env(
            "GLASSHIVE_COMPUTE_RELEASE_CLAIM_TTL_S",
            600,
            min_value=30,
            max_value=3600,
        )

    def _worker_lifecycle_lock_timeout_s(self) -> float:
        return _bounded_float_env(
            "GLASSHIVE_WORKER_LIFECYCLE_LOCK_TIMEOUT_S",
            30.0,
            min_value=0.1,
            max_value=300.0,
        )

    def _acquire_worker_lifecycle_guard(
        self, worker_id: str
    ) -> _WorkerLifecycleGuard:
        digest = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:24]
        canonical_db_path = Path(self.store.db_path).resolve(strict=False)
        lock_path = Path(f"{canonical_db_path}.compute-release-{digest}.lock")
        handle = lock_path.open("a+")
        lock_path.chmod(0o600)
        deadline = time.monotonic() + self._worker_lifecycle_lock_timeout_s()
        while True:
            try:
                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                return _WorkerLifecycleGuard(handle)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise RuntimeErrorBase(
                        "Worker lifecycle control is busy; retry shortly"
                    )
                time.sleep(0.01)

    @contextmanager
    def _worker_compute_release_lock(self, worker_id: str):
        guard = self._acquire_worker_lifecycle_guard(worker_id)
        try:
            yield
        finally:
            guard.release()

    def _runtime_compute_container_id(self, worker: dict) -> str:
        resolver = getattr(self.runtime, "compute_identity", None)
        if not callable(resolver):
            return ""
        identity = resolver(worker)
        return str((identity or {}).get("container_id") or "").strip()

    def _require_claimed_container_generation(self, worker: dict) -> dict:
        """Re-probe and bind one captured Docker generation before control RPC."""

        captured_container_id = str(
            worker.get("compute_release_container_id") or ""
        ).strip()
        if not captured_container_id:
            return worker
        current_container_id = self._runtime_compute_container_id(worker)
        if current_container_id != captured_container_id:
            raise RuntimeErrorBase(
                "Worker sandbox generation changed before exact lifecycle control; "
                "the claim remains fenced"
            )
        return {
            **worker,
            "_compute_release_container_id": captured_container_id,
        }

    def _runtime_control_info_is_confirmed(self, info: RuntimeInfo) -> bool:
        # The deterministic/in-process adapter has no external compute identity;
        # its synchronous return is the confirmation boundary. Real process or
        # Docker adapters must report no live target PID after destructive
        # Interrupt/Steer, and Docker Pause proves suspension in its own call.
        return bool(
            getattr(self.runtime, "requires_run_start_identity", True) is False
            or info.pid is None
        )

    def _require_confirmed_host_control_identity(
        self,
        worker: dict,
        run_id: str,
    ) -> dict:
        """Return one exact confirmed host lease before a destructive signal."""

        if str(worker.get("execution_mode") or "docker") != "host":
            return worker
        if getattr(self.runtime, "requires_run_start_identity", True) is False:
            # Deterministic/in-process adapters have no external PID generation to prove. Their
            # synchronous control call is already the declared identity boundary; the lifecycle
            # claim and per-worker flock below still fence the exact durable run. Never extend this
            # path to a real host adapter, which must publish the full confirmed lease tuple.
            return {**worker, "_active_run_id": run_id}
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        if (
            not lease
            or str(lease.get("worker_id") or "") != str(worker.get("worker_id") or "")
            or str(lease.get("startup_state") or "") != "confirmed"
            or str(lease.get("startup_identity_kind") or "") != "host_process"
            or int(lease.get("pid") or 0) <= 0
            or int(lease.get("process_group") or 0) <= 0
            or not str(lease.get("process_start_identity") or "").strip()
            or not str(lease.get("startup_session_id") or "").strip()
        ):
            raise RuntimeErrorBase(
                "The exact host process identity is not confirmed; control remains pending"
            )
        return {**worker, "_active_run_id": run_id, "_host_run_lease": lease}

    def _claim_exact_run_control(
        self,
        worker: dict,
        run: dict,
        *,
        kind: str,
        replacement_run: dict[str, object] | None = None,
        action_use_id: str = "",
    ) -> dict[str, object]:
        target_run_id = str(run.get("run_id") or "")
        expected_container_id = self._runtime_compute_container_id(worker)
        claim = self.store.try_claim_worker_compute_release(
            str(worker["worker_id"]),
            expected_updated_at=str(worker.get("updated_at") or ""),
            expected_last_run_id=str(worker.get("last_run_id") or ""),
            expected_state=str(worker.get("state") or ""),
            expected_container_id=expected_container_id,
            owner=self._executor_id,
            ttl_s=self._compute_release_claim_ttl_s(),
            kind=kind,
            target_run_id=target_run_id,
            expected_target_started_at=str(run.get("started_at") or ""),
            replacement_run=replacement_run,
            action_use_id=action_use_id,
            action_executor_id=self._executor_id if action_use_id else "",
        )
        if claim is None:
            raise RuntimeErrorBase("active_work_generation_changed")
        return claim

    def _restore_failed_resume_claim(
        self,
        *,
        worker: dict,
        token: str,
        epoch: int,
        kind: str,
        target_run_id: str,
        startup_error: BaseException,
    ) -> None:
        """Restore paused truth only after compensating runtime proof.

        Startup may have made compute live before raising.  Until a compensating
        pause is explicitly confirmed, the durable claim remains as the safety
        fence and no resumed truth is published.
        """

        if getattr(self.runtime, "requires_run_start_identity", True) is False:
            restored = self.store.abandon_worker_run_control_claim(
                str(worker["worker_id"]),
                token,
                epoch,
                kind=kind,
                target_run_id=target_run_id,
                worker_state="paused",
                last_error=str(startup_error),
            )
            if restored is None:
                raise RuntimeErrorBase(
                    "Resume startup failed and paused ownership could not be restored"
                ) from startup_error
            return
        try:
            info = self.runtime.pause_worker(
                self._require_claimed_container_generation(worker)
            )
            if not self._runtime_control_info_is_confirmed(info):
                raise RuntimeErrorBase(
                    "Resume compensation did not confirm paused compute"
                )
        except Exception as compensation_error:
            raise RuntimeErrorBase(
                "Resume startup failed and compensating pause was not confirmed; "
                "the lifecycle claim remains fenced"
            ) from startup_error
        restored = self.store.abandon_worker_run_control_claim(
            str(worker["worker_id"]),
            token,
            epoch,
            kind=kind,
            target_run_id=target_run_id,
            worker_state="paused",
            last_error=str(startup_error),
        )
        if restored is None:
            raise RuntimeErrorBase(
                "Resume startup failed and paused ownership could not be restored"
            ) from startup_error

    def _release_worker_compute(
        self,
        worker: dict,
        *,
        idle_seconds: float,
        kind: str = "idle",
        target_run_id: str = "",
        target_started_at: str = "",
        target_error_text: str = "",
    ) -> dict[str, object] | None:
        worker_id = str(worker.get("worker_id") or "")
        with self._worker_compute_release_lock(worker_id):
            current = self.store.get_worker(worker_id)
            if not current or current.get("compute_released_at"):
                return None
            existing_token = str(current.get("compute_release_token") or "").strip()
            requested_kind = str(
                current.get("compute_release_kind") or kind or "idle"
            ).strip().lower()
            if existing_token and requested_kind not in {
                "idle",
                "needs_input",
                "paused",
                "max_duration",
            }:
                # Active-work controls own their exact target and, for steer,
                # their durable replacement.  An idle/paused reaper must not
                # reinterpret that fence as a broad compute release.
                return None
            requested_target = str(
                current.get("compute_release_target_run_id") or target_run_id or ""
            ).strip()
            requested_target_started_at = str(
                current.get("compute_release_target_started_at")
                or target_started_at
                or ""
            ).strip()
            capacity_wait_release = False
            if requested_kind == "idle" and requested_target:
                target = self.store.get_run(requested_target)
                capacity_wait_release = bool(
                    target
                    and str(target.get("worker_id") or "") == worker_id
                    and str(target.get("state") or "") == "queued"
                )
            if (
                not existing_token
                and requested_kind == "idle"
                and (
                    self.store.get_active_run(worker_id)
                    or self.store.has_queued_runs(worker_id)
                )
                and not capacity_wait_release
            ):
                return None
            last_run_id = str(current.get("last_run_id") or "").strip()
            expected_container_id = (
                str(current.get("compute_release_container_id") or "").strip()
                if existing_token
                else self._runtime_compute_container_id(current)
            )
            claim = self.store.try_claim_worker_compute_release(
                worker_id,
                expected_updated_at=str(current.get("updated_at") or ""),
                expected_last_run_id=last_run_id,
                expected_state=str(current.get("state") or ""),
                expected_container_id=expected_container_id,
                owner=self._executor_id,
                ttl_s=self._compute_release_claim_ttl_s(),
                kind=requested_kind,
                target_run_id=requested_target,
                expected_target_started_at=requested_target_started_at,
            )
            if claim is None:
                return None
            claimed_worker = dict(claim.get("worker") or current)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            terminal_run_id = str(
                claimed_worker.get("compute_release_terminal_run_id")
                or last_run_id
                or ""
            ).strip()
            if not self.store.worker_compute_release_claim_matches(
                worker_id, token, epoch
            ):
                return None
            if (
                requested_kind == "idle"
                and (
                    self.store.get_active_run(worker_id)
                    or self.store.has_queued_runs(worker_id)
                )
                and not capacity_wait_release
            ):
                abandoned = self.store.abandon_stale_worker_compute_release_claim(
                    worker_id,
                    token,
                    epoch,
                    kind=requested_kind,
                )
                if abandoned is None:
                    raise RuntimeErrorBase(
                        "Idle release ownership changed before current work could be re-evaluated"
                    )
                if self.store.has_queued_runs(worker_id):
                    self._ensure_worker_processor(worker_id)
                return None
            captured_container_id = str(
                claimed_worker.get("compute_release_container_id") or ""
            ).strip()
            if str(claimed_worker.get("execution_mode") or "docker") == "docker":
                current_container_id = self._runtime_compute_container_id(
                    claimed_worker
                )
                if current_container_id != captured_container_id:
                    rebound = (
                        self.store.rebind_worker_compute_release_claim_generation(
                            worker_id,
                            token,
                            epoch,
                            kind=requested_kind,
                            container_id=current_container_id,
                        )
                    )
                    if rebound is None:
                        raise RuntimeErrorBase(
                            "Compute release generation changed before it could be rebound"
                        )
                    claimed_worker = rebound
                    epoch = int(rebound["compute_release_epoch"])
            claimed_runtime_worker = (
                self._worker_with_host_lease(claimed_worker, requested_target)
                if requested_kind == "max_duration" and requested_target
                else claimed_worker
            )
            runtime_worker = {
                **claimed_runtime_worker,
                "_compute_release_container_id": str(
                    claimed_worker.get("compute_release_container_id") or ""
                ).strip(),
            }
            terminal_run = (
                self.store.get_run(terminal_run_id) if terminal_run_id else None
            )
            if terminal_run and str(terminal_run.get("state") or "") in TERMINAL_RUN_STATES:
                runtime_worker["_terminal_run_id"] = terminal_run_id
            info = self.runtime.terminate_worker(runtime_worker)
            idle_state = str(current.get("state") or "")
            if idle_state not in TERMINAL_RUN_STATES:
                idle_state = "paused"
            target_result = None
            if requested_kind == "max_duration":
                target_result = self.store.finalize_worker_operation_claim(
                    worker_id,
                    token,
                    epoch,
                    kind=requested_kind,
                    target_run_id=requested_target,
                    target_expected_states=("running", "settling"),
                    target_state="cancelled",
                    target_error_text=target_error_text,
                    runtime_fields=self._runtime_info_fields(
                        worker_id,
                        info,
                        last_error=target_error_text,
                    ),
                    idle_state="paused",
                    compute_released_at=utc_now(),
                )
                updated = dict((target_result or {}).get("worker") or {})
            else:
                updated = self.store.finalize_worker_compute_release(
                    worker_id,
                    token,
                    epoch,
                    expected_kind=requested_kind,
                    target_run_id=requested_target,
                    compute_released_at=utc_now(),
                    runtime_fields=self._runtime_info_fields(
                        worker_id,
                        info,
                        last_error="",
                    ),
                    idle_state=idle_state,
                )
            if not updated:
                raise RuntimeError("Compute release ownership changed before finalization")
        if requested_kind in {"idle", "needs_input", "paused"}:
            capacity_wait_release = bool(
                requested_kind == "idle" and requested_target
            )
            event_type = (
                "worker.paused_compute_terminated"
                if requested_kind == "paused"
                else "worker.needs_input_compute_terminated"
                if requested_kind == "needs_input"
                else "worker.capacity_wait_compute_terminated"
                if capacity_wait_release
                else "worker.idle_terminated"
            )
            label = (
                "Paused worker"
                if requested_kind == "paused"
                else "Needs-input worker"
                if requested_kind == "needs_input"
                else "Capacity-wait worker"
                if capacity_wait_release
                else "Idle worker"
            )
            self.store.add_event(
                str(worker.get("project_id") or ""),
                worker_id,
                None,
                event_type,
                f"{label} compute stopped after {int(idle_seconds)} seconds; workspace state preserved.",
            )
        if (
            updated.get("state") not in {"paused", "terminated", "needs_input"}
            and self.store.has_queued_runs(worker_id)
            and not capacity_wait_release
        ):
            self._ensure_worker_processor(worker_id)
        return {
            "worker_id": worker_id,
            "project_id": worker.get("project_id"),
            "tenant_id": worker.get("tenant_id"),
            "owner_id": worker.get("owner_id"),
            "state": updated.get("state"),
            "idle_seconds": int(idle_seconds),
            "kind": requested_kind,
            "target_run_id": requested_target,
            "target_transitioned": bool(
                (target_result or {}).get("target_transitioned")
            ),
        }

    def _release_needs_input_compute(
        self,
        worker: dict,
        run: dict,
    ) -> dict[str, object] | None:
        try:
            return self._release_worker_compute(
                worker,
                idle_seconds=0,
                kind="needs_input",
                target_run_id=str(run.get("run_id") or ""),
                target_started_at=str(run.get("started_at") or ""),
            )
        except Exception as exc:
            # The needs-input truth is already authoritative. Keep the exact
            # release fence for takeover/restart recovery instead of turning a
            # compute cleanup fault into a false failed run.
            logger.warning(
                "Failed to release needs-input GlassHive worker compute %s: %s",
                str(worker.get("worker_id") or ""),
                exc,
            )
            return None

    def _release_capacity_wait_compute(
        self,
        worker: dict,
        run: dict,
    ) -> dict[str, object] | None:
        try:
            return self._release_worker_compute(
                worker,
                idle_seconds=0,
                kind="idle",
                target_run_id=str(run.get("run_id") or ""),
            )
        except Exception as exc:
            # The durable queue episode remains authoritative. Preserve the
            # exact release fence for recovery instead of losing accepted work.
            logger.warning(
                "Failed to release capacity-wait GlassHive worker compute %s: %s",
                str(worker.get("worker_id") or ""),
                exc,
            )
            return None

    def recover_expired_compute_release_claims_once(self) -> list[dict[str, object]]:
        recovered: list[dict[str, object]] = []
        # Older exact-run execution records may retain the internal
        # ``interrupted`` state.  It is settled proof for steer recovery even
        # though current public terminal projection uses ``cancelled``.
        steer_settled_states = {*TERMINAL_RUN_STATES, "interrupted"}
        for worker_id in self.store.list_expired_compute_release_claim_worker_ids():
            worker = self.store.get_worker(worker_id)
            if not worker:
                continue
            try:
                kind = str(worker.get("compute_release_kind") or "idle").strip()
                target_run_id = str(
                    worker.get("compute_release_target_run_id") or ""
                ).strip()
                target_started_at = str(
                    worker.get("compute_release_target_started_at") or ""
                ).strip()
                target = self.store.get_run(target_run_id) if target_run_id else None
                if (
                    kind
                    in {"pause_run", "resume_run", "interrupt_run", "cancel_run", "steer_run"}
                    and target
                    and str(target.get("state") or "")
                    in (
                        steer_settled_states
                        if kind == "steer_run"
                        else TERMINAL_RUN_STATES
                    )
                ):
                    replacement = None
                    if kind == "steer_run":
                        replacement = self.store.get_run(
                            str(worker.get("compute_release_replacement_run_id") or "")
                        )
                        if not replacement:
                            raise RuntimeErrorBase(
                                "Terminal-won steer has no exact fenced replacement"
                            )
                    claim = self.store.try_claim_worker_compute_release(
                        worker_id,
                        expected_updated_at=str(worker.get("updated_at") or ""),
                        expected_last_run_id=str(worker.get("last_run_id") or ""),
                        expected_state=str(worker.get("state") or ""),
                        expected_container_id=str(
                            worker.get("compute_release_container_id") or ""
                        ),
                        expected_session_fingerprint=str(
                            worker.get("compute_release_session_fingerprint") or ""
                        ),
                        owner=self._executor_id,
                        ttl_s=self._compute_release_claim_ttl_s(),
                        kind=kind,
                        target_run_id=target_run_id,
                        expected_target_started_at=target_started_at,
                        replacement_run=replacement,
                    )
                    if not claim:
                        raise RuntimeErrorBase(
                            "Terminal-won control claim generation changed"
                        )
                    if kind == "steer_run":
                        operation = self.store.finalize_worker_steer_claim(
                            worker_id,
                            str(claim["token"]),
                            int(claim["epoch"]),
                            target_run_id=target_run_id,
                            target_expected_state="running",
                            replacement_run_id=str(replacement["run_id"]),
                            replacement_instruction=str(replacement["instruction"]),
                            runtime_fields={},
                        )
                    else:
                        operation = self.store.finalize_worker_run_control_claim(
                            worker_id,
                            str(claim["token"]),
                            int(claim["epoch"]),
                            kind=kind,
                            target_run_id=target_run_id,
                            target_expected_states=("running",),
                            target_state={
                                "pause_run": "paused",
                                "cancel_run": "cancelled",
                            }.get(kind, "interrupted"),
                            worker_state="ready",
                            runtime_fields={},
                            release_lease=True,
                        )
                    if not operation:
                        raise RuntimeErrorBase(
                            "Terminal-won control could not clear its exact fence"
                        )
                    if kind == "cancel_run":
                        # A cancellation whose terminal CAS committed before a
                        # crash already owns its durable event and callback
                        # effect; settlement only clears the fence.
                        self._replay_pending_lifecycle_effects()
                        self._wake_host_capacity_waiters(
                            dict(operation.get("worker") or worker)
                        )
                    if kind == "steer_run":
                        self._replay_pending_lifecycle_effects()
                        self._finish_bound_steer_action(
                            operation_id=str(
                                worker.get("compute_release_operation_id")
                                or claim.get("token")
                                or ""
                            ),
                            target_run_id=target_run_id,
                            replacement_run=dict(
                                operation.get("replacement_run") or replacement or {}
                            ),
                        )
                    recovered.append(
                        {
                            "worker_id": worker_id,
                            "project_id": worker.get("project_id"),
                            "tenant_id": worker.get("tenant_id"),
                            "owner_id": worker.get("owner_id"),
                            "state": (operation.get("worker") or {}).get("state"),
                            "kind": kind,
                            "target_run_id": target_run_id,
                            "replacement_run_id": (
                                (operation.get("replacement_run") or {}).get("run_id")
                                if kind == "steer_run"
                                else None
                            ),
                            "target_transitioned": bool(
                                operation.get("target_transitioned")
                            ),
                            "terminal_won": bool(operation.get("terminal_won")),
                        }
                    )
                    continue
                if kind == "stop_run":
                    item = self._recover_stop_run_claim(worker, target_run_id)
                elif kind == "cancel_run":
                    # Re-enter the same exact cancellation: its proof, one
                    # cancellation event and callback, never an interrupt.
                    updated = self.cancel_run(worker_id, target_run_id)
                    durable_target = self.store.get_run(target_run_id) or {}
                    item = (
                        {
                            "worker_id": worker_id,
                            "project_id": worker.get("project_id"),
                            "tenant_id": worker.get("tenant_id"),
                            "owner_id": worker.get("owner_id"),
                            "state": updated.get("state"),
                            "kind": "cancel_run",
                            "target_run_id": target_run_id,
                            "target_transitioned": str(
                                durable_target.get("state") or ""
                            )
                            == "cancelled",
                        }
                        if not str(updated.get("compute_release_token") or "")
                        else None
                    )
                elif kind == "steer_run":
                    replacement_run_id = str(
                        worker.get("compute_release_replacement_run_id") or ""
                    ).strip()
                    replacement = self.store.get_run(replacement_run_id)
                    if (
                        not replacement
                        or str(replacement.get("worker_id") or "") != worker_id
                        or str(replacement.get("state") or "")
                        not in {"queued", *steer_settled_states}
                    ):
                        raise RuntimeErrorBase(
                            "Expired steer claim has no exact fenced replacement"
                        )
                    recovered_replacement = self.steer_worker(
                        worker_id,
                        "",
                        run_id=target_run_id,
                        _prepared_instruction=str(
                            replacement.get("instruction") or ""
                        ),
                        _replacement_run_id=replacement_run_id,
                    )
                    self._finish_bound_steer_action(
                        operation_id=str(
                            worker.get("compute_release_operation_id")
                            or worker.get("compute_release_token")
                            or ""
                        ),
                        target_run_id=target_run_id,
                        replacement_run=recovered_replacement,
                    )
                    item = {
                        "worker_id": worker_id,
                        "project_id": worker.get("project_id"),
                        "tenant_id": worker.get("tenant_id"),
                        "owner_id": worker.get("owner_id"),
                        "state": (self.store.get_worker(worker_id) or {}).get(
                            "state"
                        ),
                        "kind": "steer_run",
                        "target_run_id": target_run_id,
                        "replacement_run_id": recovered_replacement.get("run_id"),
                        "target_transitioned": True,
                    }
                elif kind in {
                    "pause_run",
                    "resume_run",
                    "interrupt_run",
                    "pause_worker",
                    "resume_worker",
                }:
                    # Re-enter the same public control path so takeover must
                    # satisfy the persisted exact target/start identity and
                    # the adapter must prove (or idempotently re-prove) the
                    # requested runtime state before the durable CAS commits.
                    if kind == "pause_run":
                        updated = self.pause_worker(
                            worker_id, run_id=target_run_id
                        )
                    elif kind == "resume_run":
                        updated = self.resume_worker(
                            worker_id, run_id=target_run_id
                        )
                    elif kind == "interrupt_run":
                        updated = self.interrupt_worker(
                            worker_id, run_id=target_run_id
                        )
                    elif kind == "pause_worker":
                        updated = self._pause_worker_without_run(worker_id)
                    else:
                        updated = self._resume_worker_without_run(worker_id)
                    durable_target = self.store.get_run(target_run_id)
                    item = (
                        {
                            "worker_id": worker_id,
                            "project_id": worker.get("project_id"),
                            "tenant_id": worker.get("tenant_id"),
                            "owner_id": worker.get("owner_id"),
                            "state": updated.get("state"),
                            "kind": kind,
                            "target_run_id": target_run_id,
                            "target_transitioned": bool(
                                durable_target
                                and str(durable_target.get("state") or "")
                                != str(
                                    {
                                        "pause_run": "running",
                                        "resume_run": "paused",
                                        "interrupt_run": "running",
                                    }.get(kind, "")
                                )
                            ),
                        }
                        if updated
                        else None
                    )
                elif kind == "terminate_worker":
                    termination = self._execute_worker_termination_claim(worker)
                    updated = dict((termination or {}).get("worker") or {})
                    item = (
                        {
                            "worker_id": worker_id,
                            "project_id": worker.get("project_id"),
                            "tenant_id": worker.get("tenant_id"),
                            "owner_id": worker.get("owner_id"),
                            "state": updated.get("state"),
                            "kind": "terminate_worker",
                            "target_run_id": target_run_id,
                            "target_transitioned": True,
                        }
                        if updated and bool((termination or {}).get("target_transitioned"))
                        else None
                    )
                elif kind in {"idle", "needs_input", "paused", "max_duration"}:
                    target_error = ""
                    if kind == "max_duration":
                        target_error = (
                            "Run exceeded its configured maximum duration; compute was "
                            "stopped and workspace state was preserved."
                        )
                    item = self._release_worker_compute(
                        worker,
                        idle_seconds=0,
                        kind=kind,
                        target_run_id=target_run_id,
                        target_started_at=target_started_at,
                        target_error_text=target_error,
                    )
                else:
                    # Unknown/future control claims remain fenced.  Never turn
                    # an unrecognised exact operation into broad termination.
                    logger.error(
                        "Unsupported expired GlassHive lifecycle claim %s for worker %s; retaining fence",
                        kind,
                        worker_id,
                    )
                    item = None
                durable_worker = self.store.get_worker(worker_id) or {}
                if item and str(durable_worker.get("compute_release_token") or ""):
                    raise RuntimeErrorBase(
                        "Recovered control did not clear its exact lifecycle fence"
                    )
                if item:
                    recovered.append(item)
            except Exception as exc:
                logger.warning(
                    "Failed to recover compute release for GlassHive worker %s: %s",
                    worker_id,
                    exc,
                )
        return recovered









    def _process_scheduler_cycle(self) -> None:
        for phase_name, phase in (
            ("scheduled runs", self.process_due_schedules_once),
            ("queued work status", self.process_queued_work_status_once),
            (
                "expired lifecycle claims",
                self.recover_expired_compute_release_claims_once,
            ),
            ("restart authority backlog", self.reconcile_restart_authority_backlog_once),
            ("worker retries", self.process_due_worker_retries_once),
            ("provider liveness", self.process_provider_liveness_once),
            ("conversation coordinator", lambda: self.coordinator.reconcile_once() if getattr(self, "coordinator", None) is not None else None),
        ):
            if self._shutdown_event.is_set():
                return
            try:
                phase()
            except Exception:
                logger.exception("GlassHive scheduler phase failed: %s", phase_name)

    def _next_scheduler_wait_s(self, interval: float) -> float:
        now = self._now_datetime()
        if self.store.list_due_retry_worker_ids(
            now_iso=now.isoformat(),
            limit=1,
        ):
            # A due retry can briefly collide with the processor that just
            # requeued it. Keep the scheduler eligible to retry after that
            # local owner exits instead of sleeping the full idle interval.
            return 0.01
        candidates = [
            value
            for value in (
                self.store.next_queued_retry_after(now_iso=now.isoformat()),
                self.store.next_queue_maintenance_at(now=now.isoformat()),
            )
            if value
        ]
        if not candidates:
            return interval
        try:
            parsed = min(self._normalized_datetime(value) for value in candidates)
        except ValueError:
            return interval
        delay_s = parsed.timestamp() - now.timestamp()
        if delay_s <= 0:
            return 0.01
        return min(interval, max(0.01, delay_s))

    def _safe_next_scheduler_wait_s(self, interval: float) -> float:
        try:
            return self._next_scheduler_wait_s(interval)
        except Exception:
            logger.exception("GlassHive scheduler wait calculation failed")
            return interval

    def _host_lease_ttl_s(self) -> float:
        return _bounded_float_env(
            "WPR_HOST_LEASE_TTL_S",
            30.0,
            min_value=5.0,
            max_value=3600.0,
        )

    def _host_runtime_family(self, worker: dict) -> str:
        profile = str(worker.get("profile") or worker.get("runtime") or "host").lower()
        if profile.startswith("codex"):
            return "codex"
        if profile.startswith("claude"):
            return "claude"
        if profile == "grok-build":
            return "grok"
        return "openclaw"

    def _host_run_lane(self, worker: dict) -> str:
        return self._trusted_run_lane(worker)

    def _worker_with_host_lease(self, worker: dict, run_id: str) -> dict:
        if not run_id:
            return worker
        scoped_worker = {**worker, "_active_run_id": run_id}
        if str(worker.get("execution_mode") or "docker") != "host":
            return scoped_worker
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        return {**scoped_worker, "_host_run_lease": lease} if lease else scoped_worker

    def _prompt_workbench_scheduled_authority(
        self, worker: dict
    ) -> dict[str, str] | None:
        bundle = self._bootstrap_bundle_for(worker) or {}
        authority = bundle.get("viventium_launch_authority")
        if not (
            str(bundle.get("execution_policy") or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            and isinstance(authority, dict)
            and authority.get("version") == 1
            and not isinstance(authority.get("version"), bool)
            and authority.get("kind")
            == PROMPT_WORKBENCH_SCHEDULED_AUTHORITY_KIND
            and authority.get("execution_mode") == "docker"
            and str(worker.get("execution_mode") or "").strip() == "docker"
        ):
            return None
        callbacks = bundle.get("callbacks")
        origin_ref = (
            str(callbacks.get("origin_ref") or "").strip()
            if isinstance(callbacks, dict)
            else ""
        )
        return {"origin_ref": origin_ref}

    def _deferred_capability_authorization(self, worker: dict) -> dict[str, str] | None:
        bundle = self._bootstrap_bundle_for(worker) or {}
        raw = bundle.get("glasshive_capability_authorization")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            )
        if set(raw) != {
            "version",
            "status",
            "authorization_ref",
            "origin_ref",
            "max_expires_at",
            "scope_fingerprint",
        }:
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            )
        if (
            isinstance(raw.get("version"), bool)
            or raw.get("version") != 1
            or raw.get("status") != "pending_admission"
        ):
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            )
        normalized = {
            "authorization_ref": str(raw.get("authorization_ref") or "").strip(),
            "origin_ref": str(raw.get("origin_ref") or "").strip(),
            "max_expires_at": str(raw.get("max_expires_at") or "").strip(),
            "scope_fingerprint": str(raw.get("scope_fingerprint") or "").strip(),
        }
        if any(not value or len(value) > 512 for value in normalized.values()):
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            )
        env = bundle.get("env")
        if isinstance(env, dict) and str(
            env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or ""
        ).strip():
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "A deferred capability authorization must not contain a bearer grant.",
            )
        try:
            maximum = datetime.fromisoformat(
                normalized["max_expires_at"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            ) from exc
        if maximum.tzinfo is None or maximum <= datetime.now(timezone.utc):
            raise BrokerAdmissionError(
                "capability_authorization_horizon_expired",
                "The deferred capability authorization has expired.",
                needs_input=True,
            )
        return normalized

    def _run_local_admitted_worker(
        self,
        worker: dict,
        run: dict,
        *,
        authority_context: dict[str, str] | None = None,
    ) -> dict:
        qa_auth = self._consume_local_qa(
            "provider_auth_missing", worker, run
        )
        if qa_auth is not None:
            self._record_local_qa_effect(
                qa_auth, "blocked_before_runtime_admission"
            )
            raise BrokerAdmissionError(
                "provider_auth_missing",
                "The configured model provider authorization is unavailable.",
                needs_input=True,
            )
        bundle = self._bootstrap_bundle_for(worker) or {}
        launch_authority = bundle.get("viventium_launch_authority")
        automatic_clean_room = bool(
            str(bundle.get("execution_policy") or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            and isinstance(launch_authority, dict)
            and launch_authority.get("version") == 1
            and launch_authority.get("kind") == "conversation_orchestrator"
            and launch_authority.get("execution_mode") == "docker"
        )
        scheduled_authority = self._prompt_workbench_scheduled_authority(worker)
        requirement = bundle.get("glasshive_capability_requirement")
        if isinstance(requirement, dict):
            try:
                requirement_version = int(requirement.get("version"))
            except (TypeError, ValueError):
                requirement_version = 0
            if (
                requirement_version == 1
                and requirement.get("required") is True
                and str(requirement.get("status") or "").strip() == "unavailable"
            ):
                raise BrokerAdmissionError(
                    "required_capability_unavailable",
                    "Required connected-account authorization is unavailable. Reauthorize or restore the protected capability to continue.",
                    needs_input=True,
                )
        authorization = self._deferred_capability_authorization(worker)
        if automatic_clean_room and authorization is None:
            raise BrokerAdmissionError(
                "capability_authorization_missing",
                "Connect or reauthorize the model account, then resume this work.",
                needs_input=True,
            )
        if scheduled_authority is not None and authorization is not None:
            raise BrokerAdmissionError(
                "scheduled_provider_authorization_persisted",
                "Scheduled provider authorization must be prepared for the exact live sandbox.",
            )
        if authorization is None and scheduled_authority is None:
            return worker
        load_viventium_runtime_env(
            {
                "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
                "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET",
            }
        )
        admission_url = str(
            os.environ.get("VIVENTIUM_GLASSHIVE_ADMISSION_URL") or ""
        ).strip()
        admission_secret = str(
            os.environ.get("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET") or ""
        ).strip()
        prepared_broker_url = ""
        if scheduled_authority is not None:
            container_generation_id = str(
                (authority_context or {}).get("container_generation_id") or ""
            ).strip()
            prepare_body = {
                "ownerId": str(worker.get("owner_id") or "").strip(),
                "originRef": scheduled_authority["origin_ref"],
                "workRef": scheduled_authority["origin_ref"],
                "workerId": str(worker.get("worker_id") or "").strip(),
                "runId": str(run.get("run_id") or "").strip(),
                "containerGenerationId": container_generation_id,
            }
            prepared = prepare_scheduled_provider_authorization(
                admission_url,
                secret=admission_secret,
                body=prepare_body,
                timeout_seconds=_bounded_float_env(
                    "VIVENTIUM_GLASSHIVE_ADMISSION_TIMEOUT_S",
                    5.0,
                    min_value=0.1,
                    max_value=30.0,
                ),
            )
            authorization = {
                "authorization_ref": prepared.authorization_ref,
                "origin_ref": prepared.origin_ref,
                "max_expires_at": prepared.max_expires_at,
                "scope_fingerprint": prepared.scope_fingerprint,
            }
            origin_ref = prepared.origin_ref
            work_ref = prepared.work_ref
            prepared_broker_url = prepared.broker_url
            generation_key = "containerGenerationId"
            generation_id = container_generation_id
        else:
            delegation = self.store.get_delegation_for_worker(
                str(worker.get("worker_id") or ""),
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
            )
            if not delegation:
                raise BrokerAdmissionError(
                    "broker_admission_binding_missing",
                    "The deferred capability authorization is not bound to durable work.",
                )
            origin_ref = str(delegation.get("origin_ref") or "").strip()
            if not origin_ref or origin_ref != authorization["origin_ref"]:
                raise BrokerAdmissionError(
                    "broker_admission_binding_mismatch",
                    "The deferred capability authorization binding is invalid.",
                )
            host_mode = str(worker.get("execution_mode") or "docker") == "host"
            generation_key = "hostStartupLeaseId" if host_mode else "containerGenerationId"
            context_key = "host_startup_lease_id" if host_mode else "container_generation_id"
            generation_id = str((authority_context or {}).get(context_key) or "").strip()
            if (
                set(authority_context or {}) != {context_key}
                or not re.fullmatch(r"[a-f0-9]{64}", generation_id)
            ):
                raise BrokerAdmissionError(
                    "broker_admission_generation_unavailable",
                    "The exact mission runtime generation is unavailable.",
                    retryable=True,
                )
            work_ref = str(delegation.get("work_ref") or "")
        if authorization is None:
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred provider authorization is unavailable.",
            )
        admission_body = {
            "authorizationRef": authorization["authorization_ref"],
            "originRef": origin_ref,
            "runId": str(run.get("run_id") or ""),
            "workRef": work_ref,
            "workerId": str(worker.get("worker_id") or ""),
            generation_key: generation_id,
        }
        grant = admit_capability_grant(
            admission_url,
            secret=admission_secret,
            body=admission_body,
            expected_scope_fingerprint=authorization["scope_fingerprint"],
            expected_max_expires_at=authorization["max_expires_at"],
            timeout_seconds=_bounded_float_env(
                "VIVENTIUM_GLASSHIVE_ADMISSION_TIMEOUT_S",
                5.0,
                min_value=0.1,
                max_value=30.0,
            ),
        )
        revocation_binding = {
            **admission_body,
            "grantId": grant.grant_id,
        }
        revocation = self.store.enqueue_capability_grant_revocation(
            revocation_binding
        )
        revocation_id = str(revocation.get("revocation_id") or "")
        if prepared_broker_url and grant.broker_url != prepared_broker_url:
            self.store.activate_capability_grant_revocation(revocation_id)
            self._replay_pending_capability_grant_revocations(
                revocation_id=revocation_id
            )
            raise BrokerAdmissionError(
                "scheduled_provider_authorization_response_invalid",
                "The admitted provider authorization changed its broker binding.",
            )
        # Only clean-room providers use Core's provider broker. Native host
        # runtimes validate their selected account through the existing local
        # account binder before launch; this grant authorizes connected tools.
        if str(worker.get("execution_mode") or "docker") == "docker":
            provider = (
                "anthropic"
                if str(worker.get("profile") or "").strip() == "claude-code"
                else "openai"
            )
            try:
                preflight_provider_authorization(
                    admission_url,
                    grant_token=grant.grant_token,
                    provider=provider,
                    expected_worker_id=str(worker.get("worker_id") or ""),
                    expected_run_id=str(run.get("run_id") or ""),
                    timeout_seconds=_bounded_float_env(
                        "VIVENTIUM_GLASSHIVE_PROVIDER_PREFLIGHT_TIMEOUT_S",
                        3.0,
                        min_value=0.1,
                        max_value=30.0,
                    ),
                )
                self.store.record_provider_authorization_preflight(
                    str(run.get("run_id") or ""),
                    provider=provider,
                    status="authorized",
                    failure_class="",
                )
            except BrokerAdmissionError as exc:
                self.store.record_provider_authorization_preflight(
                    str(run.get("run_id") or ""),
                    provider=provider,
                    status=(
                        "needs_input"
                        if exc.needs_input
                        else "retryable_failure"
                        if exc.retryable
                        else "rejected"
                    ),
                    failure_class=exc.code,
                )
                self.store.activate_capability_grant_revocation(revocation_id)
                self._replay_pending_capability_grant_revocations(
                    revocation_id=revocation_id
                )
                raise
            except Exception:
                self.store.record_provider_authorization_preflight(
                    str(run.get("run_id") or ""),
                    provider=provider,
                    status="rejected",
                    failure_class="provider_auth_preflight_unexpected",
                )
                self.store.activate_capability_grant_revocation(revocation_id)
                self._replay_pending_capability_grant_revocations(
                    revocation_id=revocation_id
                )
                raise
        bundle = self._bootstrap_bundle_for(worker) or {}
        run_bundle = merge_bootstrap_bundle(
            bundle,
            {
                "glasshive_capability_broker": grant.broker_projection(),
            },
        ) or {}
        # Clean-room canonicalization deliberately removes bearer material from
        # every durable/public bundle. Overlay the exact admitted run grant only
        # after that projection so ProfiledWorkerRuntime can place it in the
        # generation-bound tmpfs secret file without ever persisting it.
        run_bundle["env"] = {
            **(
                run_bundle.get("env")
                if isinstance(run_bundle.get("env"), dict)
                else {}
            ),
            "GLASSHIVE_CAPABILITY_BROKER_TOKEN": grant.grant_token,
        }
        # The bearer exists only on this in-memory run object. It never updates
        # workers.bootstrap_bundle_json or another durable row.
        return {
            **worker,
            "bootstrap_bundle_json": json.dumps(run_bundle, ensure_ascii=False),
            "_run_local_capability_binding": revocation_binding,
            "_run_local_capability_revocation_id": str(
                revocation_id
            ),
        }

    def _run_retained_for_restart(self, run_id: str) -> bool:
        with self._processors_lock:
            return str(run_id or "") in self._retained_restart_run_ids

    def _revoke_run_local_capability_grant(
        self, worker: dict, *, run_id: str = ""
    ) -> None:
        revocation_id = str(
            worker.get("_run_local_capability_revocation_id") or ""
        ).strip()
        if not revocation_id:
            return
        retained_run_id = str(run_id or worker.get("_active_run_id") or "").strip()
        if retained_run_id and self._run_retained_for_restart(retained_run_id):
            # A retained generation keeps its armed revocation until it is terminal;
            # activating it here would strip the live worker's provider authority.
            return
        self.store.activate_capability_grant_revocation(revocation_id)
        self._replay_pending_capability_grant_revocations(
            revocation_id=revocation_id
        )

    def _host_resource_capacity_error(
        self,
        prospective_worker: dict | None = None,
        *,
        docker_cached_only: bool = False,
        _include_snapshot: bool = False,
    ) -> HostCapacityError | None | tuple[HostCapacityError | None, dict[str, object]]:
        leases = self.store.list_active_host_run_leases()
        observed_lease_ids = [
            str(lease.get("lease_id") or "")
            for lease in leases
            if str(lease.get("lease_id") or "")
        ]
        host_leases: list[dict] = []
        active_docker_worker_ids: set[str] = set()
        active_docker_memory_reservations: dict[str, int] = {}
        unknown_lease_worker = False
        for lease in leases:
            lease_worker = self.store.get_worker(str(lease.get("worker_id") or ""))
            if not lease_worker:
                unknown_lease_worker = True
                continue
            if str(lease_worker.get("execution_mode") or "docker") == "docker":
                lease_worker_id = str(lease_worker.get("worker_id") or "")
                active_docker_worker_ids.add(lease_worker_id)
                active_docker_memory_reservations[lease_worker_id] = max(
                    0,
                    int(lease.get("reserved_memory_bytes") or 0)
                    or _worker_resource_memory_reservation(lease_worker),
                )
            else:
                host_leases.append(lease)
        usage = host_resource_usage(host_leases)
        child_processes = usage.child_processes
        threads = usage.threads
        available_memory = usage.available_memory_bytes
        available_disk = usage.available_disk_bytes
        process_probe_ok = usage.process_probe_ok and not unknown_lease_worker
        memory_probe_ok = usage.memory_probe_ok
        disk_probe_ok = usage.disk_probe_ok
        host_probe_healthy = process_probe_ok and memory_probe_ok and disk_probe_ok
        workspace_pressure = False
        docker_probe_error_code = ""
        prospective_docker = (
            isinstance(prospective_worker, dict)
            and str(prospective_worker.get("execution_mode") or "docker") == "docker"
        )
        prospective_memory_reservation = (
            _worker_resource_memory_reservation(prospective_worker)
            if prospective_docker
            else 0
        )
        prospective_reservation = {
            "childProcesses": (
                _bounded_int_env(
                    "WPR_DOCKER_PROCESS_RESERVATION",
                    20,
                    min_value=1,
                    max_value=100000,
                )
                if prospective_docker
                else 0
            ),
            "threads": (
                _bounded_int_env(
                    "WPR_DOCKER_THREAD_RESERVATION",
                    512,
                    min_value=1,
                    max_value=1000000,
                )
                if prospective_docker
                else 0
            ),
            "memoryBytes": (
                prospective_memory_reservation
                if prospective_docker
                else 0
            ),
            "diskBytes": (
                _bounded_int_env(
                    "WPR_DOCKER_DISK_RESERVATION_MB",
                    4096,
                    min_value=1,
                    max_value=1048576,
                )
                * 1024**2
                if prospective_docker
                else 0
            ),
        }
        if prospective_docker or active_docker_worker_ids:
            docker_probe = getattr(self.runtime, "isolated_resource_usage", None)
            try:
                if callable(docker_probe):
                    try:
                        docker_usage = docker_probe(cached_only=docker_cached_only)
                    except TypeError:
                        docker_usage = docker_probe()
                else:
                    docker_usage = None
            except Exception:
                logger.exception("Docker resource admission probe failed")
                docker_usage = None
            if not isinstance(docker_usage, dict):
                process_probe_ok = False
                memory_probe_ok = False
                disk_probe_ok = False
            else:
                candidate_probe_error_code = str(
                    docker_usage.get("probe_error_code") or ""
                ).strip()
                if re.fullmatch(r"[a-z][a-z0-9_]{1,79}", candidate_probe_error_code):
                    docker_probe_error_code = candidate_probe_error_code
                try:
                    running_containers = int(
                        docker_usage.get("running_worker_containers") or 0
                    )
                    docker_child_processes = int(
                        docker_usage.get("child_processes") or 0
                    )
                    docker_threads = int(docker_usage.get("threads") or 0)
                    available_memory = min(
                        available_memory,
                        int(docker_usage.get("available_memory_bytes") or 0),
                    )
                    available_disk = min(
                        available_disk,
                        int(docker_usage.get("available_disk_bytes") or 0),
                    )
                except (TypeError, ValueError):
                    running_containers = 0
                    process_probe_ok = False
                    memory_probe_ok = False
                    disk_probe_ok = False
                process_probe_ok = process_probe_ok and bool(
                    docker_usage.get("process_probe_ok")
                )
                memory_probe_ok = memory_probe_ok and bool(
                    docker_usage.get("memory_probe_ok")
                )
                disk_probe_ok = disk_probe_ok and bool(
                    docker_usage.get("disk_probe_ok")
                )
                # Reserve a conservative envelope for accepted Docker leases
                # whose container has not started yet, plus this prospective
                # mission. Actual container usage replaces its reservation on
                # subsequent admissions.
                running_worker_ids_value = docker_usage.get("running_worker_ids")
                if running_worker_ids_value is None:
                    # Backward-compatible fail-safe for runtime adapters that
                    # have not yet published per-worker accounting evidence.
                    pending_active_containers = max(
                        0,
                        len(active_docker_worker_ids) - max(0, running_containers),
                    )
                    pending_containers = pending_active_containers + (
                        1 if prospective_docker else 0
                    )
                    pending_memory_reservation = sum(
                        sorted(
                            active_docker_memory_reservations.values(),
                            reverse=True,
                        )[:pending_active_containers]
                    ) + prospective_memory_reservation
                    child_processes += docker_child_processes
                    threads += docker_threads
                else:
                    running_worker_ids = {
                        str(worker_id).strip()
                        for worker_id in running_worker_ids_value
                        if str(worker_id).strip()
                    }
                    workspace_accounting = docker_usage.get("accounting_version") == "workspace-v1"
                    extra_processes = int(docker_usage.get("unattributed_child_processes") or 0) if workspace_accounting else 0
                    extra_threads = int(docker_usage.get("unattributed_threads") or 0) if workspace_accounting else 0
                    if min(extra_processes, extra_threads) < 0:
                        process_probe_ok = False
                    if not workspace_accounting and len(running_worker_ids) != max(0, running_containers):
                        process_probe_ok = False
                        memory_probe_ok = False
                        disk_probe_ok = False
                    worker_process_counts_value = docker_usage.get(
                        "worker_process_counts"
                    )
                    if isinstance(worker_process_counts_value, dict):
                        measured_worker_counts: dict[str, tuple[int, int]] = {}
                        try:
                            for worker_id, counts_value in worker_process_counts_value.items():
                                normalized_worker_id = str(worker_id or "").strip()
                                if not normalized_worker_id or not isinstance(
                                    counts_value, dict
                                ):
                                    raise ValueError("invalid worker process measurement")
                                measured_worker_counts[normalized_worker_id] = (
                                    int(counts_value.get("child_processes") or 0),
                                    int(counts_value.get("threads") or 0),
                                )
                            if (
                                set(measured_worker_counts) != running_worker_ids
                                or any(
                                    process_count < 0 or thread_count < 0
                                    for process_count, thread_count in measured_worker_counts.values()
                                )
                                or sum(
                                    process_count
                                    for process_count, _thread_count in measured_worker_counts.values()
                                )
                                != docker_child_processes - extra_processes
                                or sum(
                                    thread_count
                                    for _process_count, thread_count in measured_worker_counts.values()
                                )
                                != docker_threads - extra_threads
                            ):
                                raise ValueError("inconsistent worker process measurement")
                        except (TypeError, ValueError):
                            process_probe_ok = False
                            measured_worker_counts = {}
                        prospective_worker_id = str(
                            (prospective_worker or {}).get("worker_id") or ""
                        ).strip()
                        counted_worker_ids = (running_worker_ids if workspace_accounting
                                              else active_docker_worker_ids & running_worker_ids)
                        child_processes += extra_processes
                        threads += extra_threads
                        if prospective_docker and prospective_worker_id in running_worker_ids:
                            counted_worker_ids.add(prospective_worker_id)
                        child_processes += sum(
                            measured_worker_counts.get(worker_id, (0, 0))[0]
                            for worker_id in counted_worker_ids
                        )
                        threads += sum(
                            measured_worker_counts.get(worker_id, (0, 0))[1]
                            for worker_id in counted_worker_ids
                        )
                    else:
                        # Older runtime adapters expose only aggregate Docker
                        # usage, so retain the conservative legacy behavior.
                        child_processes += docker_child_processes
                        threads += docker_threads
                    pending_worker_ids = active_docker_worker_ids - running_worker_ids
                    prospective_worker_id = str(
                        (prospective_worker or {}).get("worker_id") or ""
                    ).strip()
                    if prospective_docker and (
                        not prospective_worker_id
                        or prospective_worker_id not in running_worker_ids
                    ):
                        pending_id = prospective_worker_id or "__prospective_worker__"
                        pending_worker_ids.add(pending_id)
                        active_docker_memory_reservations[pending_id] = (
                            prospective_memory_reservation
                        )
                    pending_containers = len(pending_worker_ids)
                    pending_memory_reservation = sum(
                        active_docker_memory_reservations.get(
                            worker_id,
                            _configured_worker_resource_memory_bytes("standard"),
                        )
                        for worker_id in pending_worker_ids
                    )
                    if workspace_accounting:
                        from .workspace_resources import pending_memory
                        try:
                            # A prospective member already has a box only when
                            # its persisted workspace is in the measured set.
                            all_reservations = dict(active_docker_memory_reservations)
                            if prospective_docker and prospective_worker_id:
                                all_reservations[prospective_worker_id] = prospective_memory_reservation
                            pending_memory_reservation, workspace_pressure = pending_memory(
                                docker_usage, pending_worker_ids, all_reservations,
                                prospective_worker, self.store.get_worker,
                            )
                        except (KeyError, TypeError, ValueError):
                            memory_probe_ok = False
                child_processes += pending_containers * _bounded_int_env(
                    "WPR_DOCKER_PROCESS_RESERVATION",
                    20,
                    min_value=1,
                    max_value=100000,
                )
                threads += pending_containers * _bounded_int_env(
                    "WPR_DOCKER_THREAD_RESERVATION",
                    512,
                    min_value=1,
                    max_value=1000000,
                )
                available_memory = max(
                    0,
                    available_memory - pending_memory_reservation,
                )
                available_disk = max(
                    0,
                    available_disk
                    - pending_containers
                    * _bounded_int_env(
                        "WPR_DOCKER_DISK_RESERVATION_MB",
                        4096,
                        min_value=1,
                        max_value=1048576,
                    )
                    * 1024**2,
                )
        process_limit = _bounded_int_env(
            "WPR_HOST_MAX_CHILD_PROCESSES", 64, min_value=1, max_value=100000
        )
        thread_limit = _bounded_int_env(
            "WPR_HOST_MAX_THREADS", 2048, min_value=1, max_value=1000000
        )
        memory_headroom = _bounded_int_env(
            "WPR_HOST_MIN_AVAILABLE_MEMORY_MB",
            2048,
            min_value=0,
            max_value=1048576,
        ) * 1024**2
        disk_headroom = _bounded_int_env(
            "WPR_HOST_MIN_AVAILABLE_DISK_MB",
            4096,
            min_value=0,
            max_value=1048576,
        ) * 1024**2
        available_after_reservation = {
            "childProcesses": max(0, process_limit - child_processes),
            "threads": max(0, thread_limit - threads),
            "memoryBytes": max(0, available_memory),
            "diskBytes": max(0, available_disk),
        }
        required_headroom = {
            "childProcesses": 1,
            "threads": 1,
            "memoryBytes": memory_headroom,
            "diskBytes": disk_headroom,
        }
        shortage = {
            key: max(0, required_headroom[key] - available_after_reservation[key])
            for key in required_headroom
        }
        available_before_reservation = {
            key: available_after_reservation[key] + prospective_reservation[key]
            for key in required_headroom
        }
        total_required = {
            key: required_headroom[key] + prospective_reservation[key]
            for key in required_headroom
        }
        snapshot: dict[str, object] = {
            "available": available_before_reservation,
            "availableAfterReservation": available_after_reservation,
            "required": required_headroom,
            "shortage": shortage,
            "reservation": prospective_reservation,
            "observedLeaseIds": observed_lease_ids,
        }
        if docker_probe_error_code:
            snapshot["dockerProbeErrorCode"] = docker_probe_error_code
        error: HostCapacityError | None = None
        if not (
            process_probe_ok
            and memory_probe_ok
            and disk_probe_ok
        ):
            error = HostCapacityError(
                "Host resource admission is waiting for a healthy resource probe.",
                capacity_class="resource_probe_unavailable",
            )
        elif (
            workspace_pressure
            or child_processes >= process_limit
            or threads >= thread_limit
            or available_memory < memory_headroom
            or available_disk < disk_headroom
        ):
            error = HostCapacityError(
                "Host resource headroom is below its configured admission guard.",
                capacity_class="resource_pressure",
            )
        if error is not None:
            # Only a known Docker proof state may defer durable acceptance.
            # An unrelated failed host probe must never inherit its reason.
            error.probe_error_code = (
                docker_probe_error_code
                if prospective_docker and host_probe_healthy
                else ""
            )
            error.available = dict(available_before_reservation)
            error.required = dict(total_required)
            error.shortage = dict(shortage)
            error.reservation = dict(prospective_reservation)
            error.next_retry_at = ""
        if _include_snapshot:
            return error, snapshot
        return error

    def _is_native_conversation_delegation(self, worker: dict) -> bool:
        authority = (self._bootstrap_bundle_for(worker) or {}).get("viventium_launch_authority")
        if (
            not isinstance(authority, dict)
            or authority.get("version") != 1
            or authority.get("kind") != "conversation_orchestrator"
            or authority.get("execution_mode") != "host"
        ):
            return False
        return self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
        ) is not None

    def _has_native_delegation_authority(self, worker: dict) -> bool:
        return native_parallel_policy_enabled() and self._is_native_conversation_delegation(worker)

    def _host_mutation_scope(self, worker: dict, *, trusted_delegation: bool = False) -> str:
        """Keep the conservative fence for manual host missions.

        Core-owned native missions use distinct workspaces and model coordination.
        This fence does not give them exclusive control of the shared desktop.
        """

        if (
            str(worker.get("execution_mode") or "docker") != "host"
            or self._host_run_lane(worker) != "mission"
        ):
            return ""
        if native_parallel_policy_enabled() and (
            trusted_delegation or self._has_native_delegation_authority(worker)
        ):
            return ""
        # Model-authored bootstrap fields cannot turn a manual worker into a
        # Core-owned mission or buy an independent mutation lease.
        return hashlib.sha256(b"host-mission:unscoped-mutation-target").hexdigest()

    def _relieve_docker_resource_pressure(
        self,
        prospective_worker: dict,
        pressure: HostCapacityError,
    ) -> tuple[HostCapacityError | None, dict[str, object] | None]:
        """Release only eligible idle compute, then re-measure before queueing.

        Durable workspaces live outside the container generation. Retaining an
        idle workstation is useful for fast takeover, but it must not strand a
        newly accepted Parallel mission when the Docker VM is otherwise below
        its admission headroom. Exact compute-release claims keep this recovery
        safe against a concurrent resume, retry, or action.
        """

        if (
            pressure.capacity_class != "resource_pressure"
            or str(prospective_worker.get("execution_mode") or "docker") != "docker"
        ):
            return pressure, None
        # Admission intentionally consults the background snapshot first, but releasing retained
        # compute is a destructive lifecycle transition.  Confirm the pressure with a fresh probe
        # immediately before selecting or releasing the first candidate.
        current_pressure, current_snapshot = self._host_resource_capacity_error(
            prospective_worker,
            docker_cached_only=False,
            _include_snapshot=True,
        )
        if (
            current_pressure is None
            or current_pressure.capacity_class != "resource_pressure"
        ):
            return current_pressure, current_snapshot
        workers = self.store.list_all_workers()
        release_idle_box = getattr(self.runtime, "release_idle_workspace_box", None)
        if callable(release_idle_box):
            checked_workspaces: set[str] = set()
            for worker in workers:
                workspace_id = str(worker.get("workspace_id") or "")
                if (
                    not workspace_id
                    or workspace_id in checked_workspaces
                    or not worker.get("compute_released_at")
                ):
                    continue
                checked_workspaces.add(workspace_id)
                try:
                    reclaimed = release_idle_box(worker)
                except Exception as exc:
                    logger.warning(
                        "Failed to release an idle workspace box: %s",
                        type(exc).__name__,
                    )
                    continue
                if reclaimed:
                    current_pressure, current_snapshot = self._host_resource_capacity_error(
                        prospective_worker,
                        docker_cached_only=False,
                        _include_snapshot=True,
                    )
                    if (
                        current_pressure is None
                        or current_pressure.capacity_class != "resource_pressure"
                    ):
                        return current_pressure, current_snapshot
        candidates: list[tuple[float, dict]] = []
        prospective_worker_id = str(
            prospective_worker.get("worker_id") or ""
        ).strip()
        for worker in workers:
            worker_id = str(worker.get("worker_id") or "").strip()
            if (
                not worker_id
                or worker_id == prospective_worker_id
                or str(worker.get("execution_mode") or "docker") != "docker"
                or worker.get("compute_released_at")
                or str(worker.get("state") or "")
                in {"terminated", "paused", "running", "starting", "needs_input"}
                or self.store.get_active_run(worker_id)
                or self.store.has_queued_runs(worker_id)
            ):
                continue
            candidates.append((self._worker_idle_seconds(worker), worker))
        candidates.sort(key=lambda item: item[0], reverse=True)

        for idle_seconds, worker in candidates:
            try:
                released = self._release_worker_compute(
                    worker,
                    idle_seconds=idle_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to release idle compute during capacity recovery: %s",
                    type(exc).__name__,
                )
                continue
            if not released:
                continue
            if callable(release_idle_box):
                try:
                    release_idle_box(worker)
                except Exception as exc:
                    logger.warning(
                        "Failed to release an idle workspace box: %s",
                        type(exc).__name__,
                    )
            invalidate_capacity = getattr(
                self.runtime, "invalidate_isolated_capacity_snapshot", None
            )
            if callable(invalidate_capacity):
                invalidate_capacity()
            current_pressure, current_snapshot = self._host_resource_capacity_error(
                prospective_worker,
                docker_cached_only=False,
                _include_snapshot=True,
            )
            if (
                current_pressure is None
                or current_pressure.capacity_class != "resource_pressure"
            ):
                return current_pressure, current_snapshot
        return current_pressure, current_snapshot

    def _acquire_host_run_lease(self, worker: dict, run: dict) -> dict | None:
        execution_mode = str(worker.get("execution_mode") or "docker")
        lane = self._host_run_lane(worker)
        if (
            execution_mode == "host"
            and lane == "mission"
            and isolated_parallel_policy_enabled()
        ):
            raise HostCapacityError(
                "Host-native mission admission is disabled by isolated Parallel policy.",
                capacity_class="isolated_parallel_policy",
            )
        existing_lease = self.store.get_active_host_run_lease_for_run(
            str(run.get("run_id") or "")
        )
        if existing_lease is not None:
            exact_lease_owner = bool(
                str(existing_lease.get("executor_id") or "") == self._executor_id
                and str(existing_lease.get("worker_id") or "")
                == str(worker.get("worker_id") or "")
                and str(existing_lease.get("startup_state") or "") == "reserved"
            )
            exact_preaccept_reservation = bool(
                exact_lease_owner
                and str(run.get("state") or "") == "queued"
                and not str(existing_lease.get("attempt_id") or "")
                and not str(run.get("active_attempt_id") or "")
            )
            exact_claimed_handoff = bool(
                exact_lease_owner
                and str(existing_lease.get("attempt_id") or "")
                == str(run.get("active_attempt_id") or "")
                and str(run.get("state") or "") == "claimed"
            )
            if exact_preaccept_reservation or exact_claimed_handoff:
                return {**existing_lease, "idempotent_replay": True}
        # Docker isolates each mission's filesystem/process namespace, but all
        # containers still consume the same workstation memory, disk and
        # process budget. Apply the global guard before either substrate starts.
        # Admission consumes only the background-refreshed Docker snapshot. A
        # missing/stale snapshot queues fail-closed; it never cold-runs Docker
        # CLI probes on the durable-acceptance critical path.
        pressure, capacity_snapshot = self._host_resource_capacity_error(
            worker,
            docker_cached_only=True,
            _include_snapshot=True,
        )
        qa_capacity: tuple[str, LocalQAFaultDirective] | None = None
        for qa_boundary in (
            "maximum_capacity_overflow",
            "measured_memory_4_3_gib_vs_5_gib",
            "last_reservation_competition",
            "low_disk",
        ):
            directive = self._consume_local_qa(qa_boundary, worker, run)
            if directive is not None:
                qa_capacity = (qa_boundary, directive)
                break
        if qa_capacity is not None:
            qa_boundary, directive = qa_capacity
            parameters = directive.parameters
            retry_seconds = int(parameters.get("nextRetrySeconds") or 5)
            next_retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)
            ).isoformat()
            if qa_boundary == "maximum_capacity_overflow":
                pressure = HostCapacityError(
                    "The mission lane is at its configured maximum capacity.",
                    capacity_class="mission_slots",
                    retry_after_s=retry_seconds,
                    dimension="missionSlots",
                    configured={"missionSlots": 1},
                    used={"missionSlots": 1},
                )
                pressure.available = {"missionSlots": 0}
                pressure.required = {"missionSlots": 1}
                pressure.shortage = {"missionSlots": 1}
                pressure.reservation = {"missionSlots": 1}
                pressure.next_retry_at = next_retry_at
                self._record_local_qa_effect(
                    directive, "maximum_capacity_rejected_before_lease"
                )
            elif qa_boundary == "measured_memory_4_3_gib_vs_5_gib":
                available = int(parameters["availableMemoryBytes"])
                required = int(parameters["requiredMemoryBytes"])
                reservation = int(parameters["reservationMemoryBytes"])
                pressure = HostCapacityError(
                    "Measured memory is below the exact admission requirement.",
                    capacity_class="resource_pressure",
                    retry_after_s=retry_seconds,
                    dimension="memoryBytes",
                )
                pressure.available = {"memoryBytes": available}
                pressure.required = {"memoryBytes": required}
                pressure.shortage = {
                    "memoryBytes": int(parameters["shortageMemoryBytes"])
                }
                pressure.reservation = {"memoryBytes": reservation}
                pressure.next_retry_at = next_retry_at
                self._record_local_qa_effect(
                    directive, "measured_memory_rejected_before_lease"
                )
            elif qa_boundary == "low_disk":
                available = int(parameters["availableDiskBytes"])
                required = max(
                    int((capacity_snapshot.get("required") or {}).get("diskBytes") or 0),
                    4 * 1024**3,
                )
                reservation = int(
                    (capacity_snapshot.get("reservation") or {}).get("diskBytes") or 0
                )
                pressure = HostCapacityError(
                    "Measured disk headroom is below the admission guard.",
                    capacity_class="resource_pressure",
                    retry_after_s=retry_seconds,
                    dimension="diskBytes",
                )
                pressure.available = {"diskBytes": available}
                pressure.required = {"diskBytes": required}
                pressure.shortage = {"diskBytes": max(0, required - available)}
                pressure.reservation = {"diskBytes": reservation}
                pressure.next_retry_at = next_retry_at
                self._record_local_qa_effect(
                    directive, "low_disk_rejected_before_lease"
                )
            else:
                capacity_snapshot = {
                    **capacity_snapshot,
                    "available": {
                        **dict(capacity_snapshot.get("available") or {}),
                        "memoryBytes": int(parameters["availableMemoryBytes"]),
                    },
                    "required": {
                        **dict(capacity_snapshot.get("required") or {}),
                        "memoryBytes": int(parameters["requiredMemoryBytes"]),
                    },
                    "reservation": {
                        **dict(capacity_snapshot.get("reservation") or {}),
                        "memoryBytes": int(parameters["reservationMemoryBytes"]),
                    },
                }
                self._record_local_qa_effect(
                    directive, "last_reservation_competition_entered_atomic_lease"
                )
        if pressure and qa_capacity is None:
            pressure, recovered_snapshot = self._relieve_docker_resource_pressure(
                worker, pressure
            )
            if recovered_snapshot is not None:
                capacity_snapshot = recovered_snapshot
        if pressure:
            raise pressure
        capacity_next_retry_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self._retry_base_delay_s("host_capacity"))
        ).isoformat()
        capacity_policy = self._host_capacity_policy()
        try:
            return self.store.acquire_host_run_lease(
                runtime_family=self._host_runtime_family(worker),
                lane=lane,
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
                worker_id=str(worker.get("worker_id") or ""),
                run_id=str(run.get("run_id") or ""),
                executor_id=self._executor_id,
                **capacity_policy,
                mutation_scope=(
                    self._host_mutation_scope(worker)
                    if execution_mode == "host"
                    else ""
                ),
                lease_ttl_s=self._host_lease_ttl_s(),
                capacity_available=dict(
                    capacity_snapshot.get("available") or {}
                ),
                capacity_required=dict(
                    capacity_snapshot.get("required") or {}
                ),
                capacity_reservation=dict(
                    capacity_snapshot.get("reservation") or {}
                ),
                capacity_observed_lease_ids=list(
                    capacity_snapshot.get("observedLeaseIds") or []
                ),
                capacity_next_retry_at=capacity_next_retry_at,
            )
        except HostRunLeaseCapacityError as exc:
            error = HostCapacityError(
                str(exc),
                capacity_class=exc.capacity_class,
                dimension=exc.dimension,
                configured=exc.configured,
                used=exc.used,
            )
            error.available = dict(exc.available)
            error.required = dict(exc.required)
            error.shortage = dict(exc.shortage)
            error.reservation = dict(exc.reservation)
            error.next_retry_at = str(exc.next_retry_at or capacity_next_retry_at)
            raise error from exc

    def _observe_host_process(self, payload: dict[str, object]) -> None:
        run_id = str(payload.get("run_id") or "")
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        if not lease:
            return
        self.store.heartbeat_host_run_lease(
            str(lease["lease_id"]),
            executor_id=self._executor_id,
            pid=int(payload.get("pid") or 0) or None,
            process_group=int(payload.get("process_group") or 0) or None,
            process_start_identity=str(payload.get("process_start_identity") or ""),
            startup_identity_kind=str(payload.get("identity_kind") or ""),
            startup_container_id=str(payload.get("container_id") or ""),
            startup_session_id=str(payload.get("session_id") or ""),
            lease_ttl_s=self._host_lease_ttl_s(),
        )

    def _observe_run_start(self, payload: dict[str, object]) -> None:
        """Accept one exact durable runtime identity and release its launch flock."""

        run_id = str(payload.get("run_id") or "").strip()
        worker_id = str(payload.get("worker_id") or "").strip()
        with self._pending_run_starts_lock:
            pending = dict(self._pending_run_starts.get(run_id) or {})
        if not pending or worker_id != str(pending.get("worker_id") or ""):
            raise RunStartupRejectedError(
                "The exact run startup reservation is no longer active.",
                termination_confirmed=False,
            )
        pending_worker = pending.get("worker")
        if not isinstance(pending_worker, dict):
            raise RunStartupRejectedError(
                "The exact run startup reservation has no worker identity.",
                termination_confirmed=False,
            )
        identity_kind = str(payload.get("identity_kind") or "").strip().lower()
        if getattr(self.runtime, "requires_run_start_identity", True) is False:
            expected_identity_kind = "in_process"
        else:
            execution_mode = str(
                pending_worker.get("execution_mode") or "docker"
            ).strip().lower()
            expected_identity_kind = {
                "host": "host_process",
                "docker": "docker_session",
            }.get(execution_mode, "")
        if identity_kind != expected_identity_kind:
            raise RunStartupRejectedError(
                "The reported runtime identity grade does not match the reserved execution mode.",
                termination_confirmed=False,
            )
        result = self.store.confirm_host_run_start(
            worker_id=worker_id,
            run_id=run_id,
            run_started_at=str(pending.get("run_started_at") or ""),
            lease_id=str(pending.get("lease_id") or ""),
            startup_token=str(pending.get("startup_token") or ""),
            executor_id=self._executor_id,
            identity_kind=identity_kind,
            pid=int(payload.get("pid") or 0) or None,
            process_group=int(payload.get("process_group") or 0) or None,
            process_start_identity=str(
                payload.get("process_start_identity") or ""
            ),
            container_id=str(payload.get("container_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            allow_runtime_invocation=False,
            callback_record=pending.get("callback_record")
            if isinstance(pending.get("callback_record"), dict)
            else None,
        )
        if result is None:
            raise RunStartupRejectedError(
                "The exact run startup reservation changed before confirmation.",
                termination_confirmed=False,
            )
        with self._pending_run_starts_lock:
            current = self._pending_run_starts.get(run_id)
            if current is not None:
                current["confirmed"] = True
                confirmed_run = result.get("run")
                if isinstance(confirmed_run, dict):
                    current["run"] = dict(confirmed_run)
                    current["run_started_at"] = str(
                        confirmed_run.get("runtime_invoked_at") or ""
                    )
        guard = pending.get("guard")
        if isinstance(guard, _WorkerLifecycleGuard):
            guard.release()
        if identity_kind == "docker_session" and is_parallel_clean_room_bootstrap(
            None, self._bootstrap_bundle_for(pending_worker) or {}
        ):
            audit_worker = (
                self._refresh_runtime_info(worker_id, state="running", last_error="")
                or pending_worker
            )
            try:
                capture_worker_isolation_audit(
                    store=self.store,
                    runtime=self.runtime,
                    worker=audit_worker,
                    run_id=run_id,
                    container_id=str(payload.get("container_id") or ""),
                )
            except Exception:
                logger.warning(
                    "Confirmed worker isolation could not produce an audited observation",
                    exc_info=True,
                )
        callback = result.get("callback")
        callbacks = pending.get("callbacks")
        worker = pending.get("worker")
        if (
            isinstance(callback, dict)
            and isinstance(callbacks, dict)
            and isinstance(worker, dict)
        ):
            # Network delivery happens only after the durable CAS and flock release.
            self.executor.submit(
                self._deliver_callback_record,
                dict(worker),
                callback,
                dict(callbacks),
            )

    def _confirm_in_process_run_start(self, pending: dict[str, object]) -> None:
        self._observe_run_start(
            {
                "worker_id": str(pending.get("worker_id") or ""),
                "run_id": str(pending.get("run_id") or ""),
                "identity_kind": "in_process",
                "pid": 0,
                "process_group": 0,
                "process_start_identity": "",
                "container_id": "",
                "session_id": "in-process",
            }
        )

    @staticmethod
    def _json_object(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return dict(value)
        try:
            decoded = json.loads(str(value or "{}"))
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _observe_native_event(self, observation: dict[str, object]) -> None:
        """Persist one prompt-free provider lifecycle projection for an exact run."""

        worker_id = str(observation.get("worker_id") or "").strip()
        run_id = str(observation.get("run_id") or "").strip()
        provider = str(observation.get("provider") or "").strip().lower()
        event = observation.get("event")
        if provider not in {"codex", "claude", "grok"} or not isinstance(event, dict):
            return
        event_type = str(event.get("event_type") or "").strip()
        payload = event.get("payload")
        if not event_type.startswith("provider.") or not isinstance(payload, dict):
            return
        run = self.store.get_run(run_id)
        if not run or str(run.get("worker_id") or "") != worker_id:
            return
        worker = self.store.get_worker(worker_id)
        if not worker:
            return

        capabilities = self._json_object(run.get("native_capabilities_json"))
        child_projection_observed = event_type.startswith("provider.child.") or (
            event_type == "provider.team.message"
        )
        capabilities.update(
            {
                "provider": provider,
                "providerStream": True,
            }
        )
        if child_projection_observed:
            capabilities["childProjection"] = True
        else:
            capabilities.setdefault("childProjection", False)
        existing_summary = self._json_object(run.get("native_child_summary_json"))
        projection = NativeTeamProjection.from_summary(existing_summary)
        if not projection.observable:
            projection = NativeTeamProjection(provider=provider, observable=True)
        projection.apply(event)

        updates: dict[str, object] = {
            "native_capabilities_json": json.dumps(capabilities, sort_keys=True),
            "native_child_summary_json": json.dumps(projection.summary() or {}, sort_keys=True),
        }
        if event_type == "provider.session.started":
            updates["native_session_id"] = str(payload.get("sessionId") or "")[:256]
        self.store.update_run(run_id, **updates)
        self.store.add_event(
            str(worker.get("project_id") or run.get("project_id") or ""),
            worker_id,
            run_id,
            event_type,
            "Native provider lifecycle updated",
            payload=payload,
        )

    def _provider_internal_retry_limit(self) -> int:
        return _bounded_int_env(
            "GLASSHIVE_PROVIDER_INTERNAL_RETRY_LIMIT",
            3,
            min_value=1,
            max_value=100,
        )

    def _provider_no_progress_timeout_s(self) -> float:
        return _bounded_float_env(
            "GLASSHIVE_PROVIDER_NO_PROGRESS_TIMEOUT_S",
            900.0,
            min_value=1.0,
            max_value=30 * 24 * 3600.0,
        )

    def _settle_provider_liveness_attention(self, run: dict[str, object]) -> None:
        if run.get("_attention_transitioned") is not True:
            return
        run_id = str(run.get("run_id") or "")
        worker_id = str(run.get("worker_id") or "")
        worker = self.store.get_worker(worker_id)
        if not run_id or not worker:
            return
        self._invalidate_worker_processor(worker_id)
        self.store.finalize_schedule_for_run(
            run_id,
            state="needs_input",
            last_error=str(run.get("error_text") or ""),
        )
        current_worker = self.store.get_worker(worker_id) or worker
        self._release_needs_input_compute(current_worker, run)
        self._replay_pending_lifecycle_effects()

    def _observe_provider_liveness(
        self, observation: dict[str, object]
    ) -> dict[str, object] | None:
        """Persist one prompt-free provider retry or progress observation."""

        worker_id = str(observation.get("worker_id") or "").strip()
        run_id = str(observation.get("run_id") or "").strip()
        attempt_id = str(observation.get("attempt_id") or "").strip()
        run = self.store.get_run(run_id)
        if (
            not worker_id
            or not run_id
            or not attempt_id
            or not run
            or str(run.get("worker_id") or "") != worker_id
            or str(run.get("active_attempt_id") or "") != attempt_id
        ):
            return None
        updated = self.store.observe_provider_liveness(
            run_id=run_id,
            expected_attempt_id=attempt_id,
            kind=str(observation.get("kind") or ""),
            failure_class=str(observation.get("failure_class") or ""),
            runtime=str(observation.get("runtime") or ""),
            model=str(observation.get("model") or ""),
            source_sequence=int(observation.get("source_sequence") or 0),
            source_digest=str(observation.get("source_digest") or ""),
            observed_at=str(observation.get("observed_at") or utc_now()),
            retry_limit=self._provider_internal_retry_limit(),
        )
        if updated and updated.get("_attention_transitioned") is True:
            self._settle_provider_liveness_attention(updated)
        return updated

    def process_provider_liveness_once(self) -> list[dict[str, object]]:
        transitioned = self.store.transition_stalled_provider_liveness_runs(
            now=self._now_datetime().isoformat(),
            inactivity_seconds=self._provider_no_progress_timeout_s(),
        )
        for run in transitioned:
            self._settle_provider_liveness_attention(run)
        return transitioned

    def _native_child_reconcile_seconds(self) -> float:
        return _bounded_float_env(
            "GLASSHIVE_NATIVE_CHILD_RECONCILE_SECONDS",
            120.0,
            min_value=0.01,
            max_value=120.0,
        )

    def _settle_native_children(
        self,
        worker: dict,
        run: dict,
        output: str,
        *,
        terminal_generation: dict[str, str] | None = None,
    ) -> str | None:
        """Hold terminal truth while a proven native child remains live.

        The root result is persisted before waiting. Unknown or unobservable
        provider schemas never enter settling. A missing child bookend is
        bounded by the configured window (120 seconds maximum) and becomes an
        explicit degraded projection rather than pinning the mission forever.
        """

        run_id = str(run.get("run_id") or "")
        generation = (
            dict(terminal_generation)
            if isinstance(terminal_generation, dict)
            else self._terminal_generation_for_run(run)
        )
        durable = self.store.get_run(run_id) or run
        capabilities = self._json_object(durable.get("native_capabilities_json"))
        summary = self._json_object(durable.get("native_child_summary_json"))
        projection = NativeTeamProjection.from_summary(summary)
        current_summary = projection.summary()
        if (
            capabilities.get("childProjection") is not True
            or not current_summary
            or int(current_summary.get("activeCount") or 0) <= 0
        ):
            return str(durable.get("state") or "running")

        root_exited_at = datetime.now(timezone.utc)
        summary_with_root = {
            **current_summary,
            "rootExitedAt": root_exited_at.isoformat(),
            "degraded": False,
        }
        transitioned = self.store.settle_run_if_current(
            run_id,
            output_text=output,
            native_child_summary_json=json.dumps(summary_with_root, sort_keys=True),
            **generation,
        )
        if not transitioned:
            return None
        self.store.add_event(
            str(worker.get("project_id") or run.get("project_id") or ""),
            str(worker.get("worker_id") or run.get("worker_id") or ""),
            run_id,
            "run.settling",
            "Root result is ready while native child reconciliation continues",
            payload={"activeChildCount": int(current_summary.get("activeCount") or 0)},
        )

        timeout_seconds = self._native_child_reconcile_seconds()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not self._shutdown_event.is_set():
            latest = self.store.get_run(run_id) or {}
            if str(latest.get("state") or "") != "settling":
                return str(latest.get("state") or "")
            latest_summary = self._json_object(latest.get("native_child_summary_json"))
            latest_projection = NativeTeamProjection.from_summary(latest_summary)
            reduced = latest_projection.summary()
            if not reduced or int(reduced.get("activeCount") or 0) <= 0:
                return "settling"
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))

        latest = self.store.get_run(run_id) or {}
        if str(latest.get("state") or "") != "settling":
            return str(latest.get("state") or "")
        latest_summary = self._json_object(latest.get("native_child_summary_json"))
        projection = NativeTeamProjection.from_summary(
            latest_summary,
            reconcile_seconds=max(1, int(timeout_seconds)),
        )
        decision = projection.settlement(
            root_exited_at=root_exited_at,
            now=root_exited_at + timedelta(seconds=max(1, int(timeout_seconds))),
        )
        if decision.state == "degraded":
            degraded_summary = {
                **(projection.summary() or {}),
                "rootExitedAt": root_exited_at.isoformat(),
                "degraded": True,
                "lostChildRefs": list(decision.lost_child_refs),
            }
            updated = self.store.update_native_settling_summary_if_current(
                run_id,
                native_child_summary_json=json.dumps(degraded_summary, sort_keys=True),
                **generation,
            )
            if not updated:
                return None
            self.store.add_event(
                str(worker.get("project_id") or run.get("project_id") or ""),
                str(worker.get("worker_id") or run.get("worker_id") or ""),
                run_id,
                "provider.child.reconciliation_lost",
                "Native child state became unknown after the bounded reconciliation window",
                payload={"lostChildRefs": list(decision.lost_child_refs)},
            )
        return "settling"

    def _release_host_run_lease(self, run_id: str, *, reason: str) -> None:
        if self._run_retained_for_restart(str(run_id)):
            # The lease now belongs to the live generation retained across this
            # managed restart; the restarted service releases it after adoption.
            return
        if reason == "runtime_returned":
            run = self.store.get_run(str(run_id))
            if run and str(run.get("state") or "") in {"running", "settling"}:
                # The terminal run CAS releases this lease in the same SQLite
                # transaction. Releasing it when the adapter returns would
                # briefly expose a running row without exact dispatch
                # ownership, allowing a concurrent reconciliation read to
                # queue and retry a completed single-use provider turn.
                return
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        worker = (
            self.store.get_worker(str(lease.get("worker_id") or ""))
            if lease
            else None
        )
        with self._processors_lock:
            if lease and str(lease.get("lease_id") or "") in self._managed_shutdown_lease_ids:
                # Only shutdown's absence proof may release this generation's fence.
                return
        if lease and self._lease_is_fenced_by_lifecycle_claim(worker, lease):
            return
        if lease and str(lease.get("executor_id") or "") == self._executor_id:
            self.store.release_host_run_lease(
                str(lease["lease_id"]),
                executor_id=self._executor_id,
                reason=reason,
            )

    def _host_lease_heartbeat_loop(self) -> None:
        interval = max(1.0, min(10.0, self._host_lease_ttl_s() / 3))
        while not self._shutdown_event.wait(interval):
            self._heartbeat_host_run_leases_once()

    def _heartbeat_host_run_leases_once(self) -> None:
        """Renew only live-run leases; one store failure must not kill the daemon."""

        try:
            leases = self.store.list_active_host_run_leases()
        except Exception:
            logger.exception("Host lease heartbeat pass failed")
            return
        for lease in leases:
            if str(lease.get("executor_id") or "") != self._executor_id:
                continue
            try:
                lease_worker = self.store.get_worker(
                    str(lease.get("worker_id") or "")
                )
                if self._lease_is_fenced_by_lifecycle_claim(
                    lease_worker, lease
                ):
                    self.store.heartbeat_host_run_lease(
                        str(lease["lease_id"]),
                        executor_id=self._executor_id,
                        lease_ttl_s=self._host_lease_ttl_s(),
                    )
                    continue
                run = self.store.get_run(str(lease.get("run_id") or ""))
                if run is None or str(run.get("state") or "") in TERMINAL_RUN_STATES:
                    self.store.release_host_run_lease(
                        str(lease["lease_id"]),
                        executor_id=self._executor_id,
                        reason="run_terminal",
                    )
                    continue
                self.store.heartbeat_host_run_lease(
                    str(lease["lease_id"]),
                    executor_id=self._executor_id,
                    lease_ttl_s=self._host_lease_ttl_s(),
                )
            except Exception:
                logger.exception(
                    "Host lease heartbeat failed for lease %s",
                    str(lease.get("lease_id") or ""),
                )
        try:
            self.reconcile_host_run_leases(stale_after_s=self._host_lease_ttl_s())
        except Exception:
            logger.exception("Host lease heartbeat reconciliation failed")

    def _isolated_readiness_loop(self) -> None:
        refresher = getattr(
            self.runtime, "refresh_isolated_parallel_readiness", None
        )
        resource_refresher = getattr(
            self.runtime, "refresh_isolated_resource_usage", None
        )
        if not callable(refresher) and not callable(resource_refresher):
            return
        while not self._shutdown_event.is_set():
            if callable(refresher):
                try:
                    refresher()
                except Exception:
                    logger.exception("Background isolated runtime readiness probe failed")
            if callable(resource_refresher):
                try:
                    resource_refresher()
                except Exception:
                    logger.exception("Background isolated runtime resource probe failed")
            if self._shutdown_event.wait(10.0):
                return

    @staticmethod
    def _lease_is_fenced_by_lifecycle_claim(
        worker: dict | None, lease: dict
    ) -> bool:
        """Keep the exact runtime lease owned by an unfinished control."""

        if not worker or not str(worker.get("compute_release_token") or ""):
            return False
        return bool(
            str(worker.get("compute_release_target_run_id") or "")
            == str(lease.get("run_id") or "")
            and str(worker.get("compute_release_kind") or "")
            in {
                "paused",
                "max_duration",
                "pause_run",
                "resume_run",
                "interrupt_run",
                "cancel_run",
                "steer_run",
                "stop_run",
                "terminate_worker",
            }
        )

    def reconcile_host_run_leases(self, *, stale_after_s: float | None = None) -> dict[str, int]:
        threshold = datetime.now(timezone.utc) - timedelta(
            seconds=(self._host_lease_ttl_s() if stale_after_s is None else max(0, stale_after_s))
        )
        result = {"renewed": 0, "released": 0, "unchanged": 0}
        identity_reader = getattr(self.runtime, "host_process_identity", None)
        absence_reader = getattr(self.runtime, "host_process_absence", None)
        for lease in self.store.list_stale_host_run_leases(
            heartbeat_before=threshold.isoformat()
        ):
            worker = self.store.get_worker(str(lease.get("worker_id") or ""))
            if self._lease_is_fenced_by_lifecycle_claim(worker, lease):
                result["unchanged"] += 1
                continue
            run = self.store.get_run(str(lease.get("run_id") or ""))
            if run is None or str(run.get("state") or "") in TERMINAL_RUN_STATES:
                released = self.store.release_host_run_lease(
                    str(lease["lease_id"]),
                    executor_id=None,
                    reason="run_terminal",
                )
                result["released" if released else "unchanged"] += 1
                continue
            if str(lease.get("startup_state") or "") == "reserved":
                expired_preaccept_without_runtime = bool(
                    str(run.get("state") or "") == "queued"
                    and not str(run.get("active_attempt_id") or "")
                    and not str(lease.get("attempt_id") or "")
                    and not int(lease.get("pid") or 0)
                    and not int(lease.get("process_group") or 0)
                    and not str(lease.get("process_start_identity") or "")
                    and not str(lease.get("startup_identity_kind") or "")
                    and not str(lease.get("startup_container_id") or "")
                    and not str(lease.get("startup_session_id") or "")
                )
                if expired_preaccept_without_runtime:
                    released = self.store.release_expired_preaccept_host_run_lease(
                        lease_id=str(lease["lease_id"]),
                        run_id=str(lease.get("run_id") or ""),
                        reason="expired_preaccept_no_runtime",
                    )
                    result["released" if released else "unchanged"] += 1
                    continue
                outcome = self._reconcile_reserved_host_run_start(
                    lease,
                    worker=worker,
                    identity_reader=identity_reader,
                )
                result[outcome] += 1
                continue
            if str(lease.get("startup_state") or "") == "termination_unconfirmed":
                outcome = self._reconcile_termination_unconfirmed_host_run_start(
                    lease,
                    absence_reader=absence_reader,
                )
                result[outcome] += 1
                continue
            identity = (
                identity_reader(worker, str(lease.get("run_id") or ""))
                if worker and callable(identity_reader)
                else None
            )
            if identity and bool(identity.get("verified")):
                # A launcher restart kills the executor without a graceful shutdown and
                # takes longer than the lease TTL. The generation it fenced (a container
                # screen session) keeps running. When the live identity matches the one
                # recorded on the lease exactly, revive the expired lease so startup adopts
                # that generation instead of downgrading the run into a duplicate.
                lease_expired = str(lease.get("expires_at") or "") <= utc_now()
                revive = bool(
                    self._restart_survivor_adoption_enabled()
                    and lease_expired
                    and self._survivor_identity_matches_lease(identity, lease)
                )
                updated = self.store.heartbeat_host_run_lease(
                    str(lease["lease_id"]),
                    executor_id=None,
                    pid=int(identity.get("pid") or 0) or None,
                    process_group=int(identity.get("process_group") or 0) or None,
                    process_start_identity=str(identity.get("process_start_identity") or ""),
                    lease_ttl_s=self._host_lease_ttl_s(),
                    reconciled=True,
                    allow_expired=revive,
                )
                if updated and revive:
                    logger.info(
                        "Revived the expired lease of run %s for its verified live generation",
                        str(lease.get("run_id") or ""),
                    )
                elif lease_expired and not updated and self._restart_survivor_adoption_enabled():
                    logger.warning(
                        "Expired lease of run %s has a live generation whose identity does not match; leaving it fenced",
                        str(lease.get("run_id") or ""),
                    )
                result["renewed" if updated else "unchanged"] += 1
            else:
                # A generation that finished while no executor was watching (for example
                # during a managed restart) publishes its terminal transcript before its
                # session disappears. Collect that single result under the still-active
                # lease instead of requeueing the exact run into a duplicate generation.
                recovered = None
                if (
                    self._restart_survivor_adoption_enabled()
                    and worker
                    and str(run.get("state") or "") == "running"
                ):
                    try:
                        recovered = self._collect_completed_run(worker, run)
                    except Exception:
                        logger.exception(
                            "Stale lease terminal-evidence collection failed",
                            extra={"run_id": str(lease.get("run_id") or "")},
                        )
                        recovered = None
                if recovered:
                    self._apply_recovered_run(worker, run, recovered)
                    self._release_reconciled_run_lease(
                        str(lease.get("run_id") or ""), reason="survivor_terminal"
                    )
                    result["released"] += 1
                    continue
                if not self._stale_lease_generation_proven_absent(
                    worker, run, lease, absence_reader=absence_reader
                ):
                    logger.warning(
                        "Stale lease of run %s stays fenced: its recorded generation could not be stopped or proven absent",
                        str(lease.get("run_id") or ""),
                    )
                    result["unchanged"] += 1
                    continue
                released = self.store.reconcile_dead_host_run_lease(
                    lease_id=str(lease["lease_id"]),
                    run_id=str(lease.get("run_id") or ""),
                    expected_attempt_id=str(lease.get("attempt_id") or ""),
                    expected_executor_id=str(lease.get("executor_id") or ""),
                    reason="stale_owner_no_verified_process",
                )
                result["released" if released else "unchanged"] += 1
        return result

    def _stale_lease_generation_proven_absent(
        self,
        worker: dict | None,
        run: dict,
        lease: dict,
        *,
        absence_reader,
    ) -> bool:
        """Stop the exact generation a stale lease fenced and prove it absent before any requeue.

        A lease whose executor stopped heartbeating is not evidence that the generation it fenced
        is dead: a launcher restart kills the executor first while the container screen session or
        host process it started keeps working. Releasing the lease there requeues the same run into
        a second, concurrent generation of the same work. Cleanup targets only the identity the
        lease durably recorded (immutable container id plus exact screen session, or exact pid
        start identity); absence of that generation is the only release proof, and ambiguity keeps
        the fence for the next pass. Survivor adoption stays off: nothing here collects a result.
        """

        run_id = str(lease.get("run_id") or "")
        if not worker or not run_id:
            return False
        if not str(run.get("runtime_invoked_at") or "").strip():
            # Nothing was ever launched for this attempt, so there is no generation to stop.
            return True
        captured_identity = self._reserved_start_identity(lease)
        cleanup = getattr(self.runtime, "cleanup_unconfirmed_run_start", None)
        if captured_identity and callable(cleanup):
            try:
                if cleanup(worker, run_id, captured_identity) is True:
                    return True
            except Exception:
                logger.exception(
                    "Stale lease generation cleanup failed", extra={"run_id": run_id}
                )
        if callable(absence_reader):
            try:
                return absence_reader(worker, run_id) is True
            except Exception:
                logger.exception(
                    "Stale lease absence proof failed", extra={"run_id": run_id}
                )
        return False

    @staticmethod
    def _survivor_identity_matches_lease(identity: dict, lease: dict) -> bool:
        """True only when the live generation is exactly the one this lease fenced."""
        kind = str(identity.get("identity_kind") or "").strip()
        if not kind or kind != str(lease.get("startup_identity_kind") or "").strip():
            return False
        recorded = str(lease.get("process_start_identity") or "").strip()
        if not recorded or recorded != str(identity.get("process_start_identity") or "").strip():
            return False
        if kind == "docker_session":
            return (
                str(identity.get("container_id") or "").strip()
                == str(lease.get("startup_container_id") or "").strip() != ""
                and str(identity.get("session_id") or "").strip()
                == str(lease.get("startup_session_id") or "").strip() != ""
            )
        return True

    def _reconcile_termination_unconfirmed_host_run_start(
        self,
        stale_lease: dict,
        *,
        absence_reader,
    ) -> str:
        """Retry exact cleanup until the fenced startup generation is proven absent."""

        worker_id = str(stale_lease.get("worker_id") or "")
        run_id = str(stale_lease.get("run_id") or "")
        try:
            guard = self._acquire_worker_lifecycle_guard(worker_id)
        except RuntimeErrorBase:
            return "unchanged"
        try:
            lease = self.store.get_host_run_lease(
                str(stale_lease.get("lease_id") or "")
            )
            worker = self.store.get_worker(worker_id)
            run = self.store.get_run(run_id)
            if (
                not lease
                or not worker
                or not run
                or str(lease.get("status") or "") != "active"
                or str(lease.get("startup_state") or "")
                != "termination_unconfirmed"
                or str(lease.get("startup_token") or "")
                != str(stale_lease.get("startup_token") or "")
                or str(run.get("state") or "") in TERMINAL_RUN_STATES
            ):
                return "unchanged"

            captured_identity = self._reserved_start_identity(lease)
            cleanup = getattr(
                self.runtime, "cleanup_unconfirmed_run_start", None
            )
            cleaned = False
            if captured_identity and callable(cleanup):
                try:
                    cleaned = cleanup(worker, run_id, captured_identity) is True
                except Exception:
                    cleaned = False

            confirmed_absent = False
            if not cleaned and callable(absence_reader):
                try:
                    confirmed_absent = absence_reader(worker, run_id) is True
                except Exception:
                    confirmed_absent = False
            if not cleaned and not confirmed_absent:
                return "unchanged"

            recovered = self.store.requeue_unconfirmed_host_run_start(
                worker_id=worker_id,
                run_id=run_id,
                lease_id=str(lease.get("lease_id") or ""),
                startup_token=str(lease.get("startup_token") or ""),
                retry_after=(
                    datetime.now(timezone.utc) + timedelta(seconds=1)
                ).isoformat(),
                error_text=(
                    "GlassHive proved the interrupted startup generation absent "
                    "and queued the exact run for retry."
                ),
            )
            if recovered is None:
                return "unchanged"
            self._scheduler_wake_event.set()
            return "released"
        finally:
            guard.release()

    @staticmethod
    def _reserved_start_identity(lease: dict) -> dict[str, object] | None:
        """Return only an identity durably published before startup confirmation."""

        kind = str(lease.get("startup_identity_kind") or "").strip()
        start_identity = str(lease.get("process_start_identity") or "").strip()
        session_id = str(lease.get("startup_session_id") or "").strip()
        container_id = str(lease.get("startup_container_id") or "").strip()
        try:
            pid = int(lease.get("pid") or 0)
            process_group = int(lease.get("process_group") or pid or 0)
        except (TypeError, ValueError):
            return None
        if kind == "host_process":
            valid = bool(
                pid > 0
                and start_identity.startswith("ps-lstart:")
                and session_id
                and not container_id
            )
        elif kind == "docker_session":
            valid = bool(
                pid > 0
                and container_id
                and session_id
                and start_identity.startswith(
                    f"docker:{container_id}:{session_id}:"
                )
            )
        else:
            valid = False
        if not valid:
            return None
        return {
            "identity_kind": kind,
            "pid": pid,
            "process_group": process_group or pid,
            "process_start_identity": start_identity,
            "container_id": container_id,
            "session_id": session_id,
        }

    @staticmethod
    def _restart_identity_matches_reserved(
        captured: dict[str, object], current: dict[str, object] | None
    ) -> bool:
        if not current or current.get("verified") is not True:
            return False
        fields = (
            "identity_kind",
            "pid",
            "process_group",
            "process_start_identity",
            "container_id",
            "session_id",
        )
        return all(
            str(current.get(field) or "") == str(captured.get(field) or "")
            for field in fields
        )

    @staticmethod
    def _restart_identity_candidate(
        current: dict[str, object] | None,
        *,
        run_id: str,
    ) -> dict[str, object] | None:
        """Validate a token-bound active-session generation without lease copy."""

        if not current or current.get("verified") is not True:
            return None
        kind = str(current.get("identity_kind") or "").strip()
        start_identity = str(
            current.get("process_start_identity") or ""
        ).strip()
        container_id = str(current.get("container_id") or "").strip()
        session_id = str(current.get("session_id") or "").strip()
        try:
            pid = int(current.get("pid") or 0)
            process_group = int(current.get("process_group") or pid or 0)
        except (TypeError, ValueError):
            return None
        if kind == "host_process":
            valid = bool(
                pid > 0
                and process_group > 0
                and session_id
                and not container_id
                and start_identity.startswith("ps-lstart:")
            )
        elif kind == "docker_session":
            valid = bool(
                pid > 0
                and process_group > 0
                and container_id
                and session_id
                and start_identity.startswith(
                    f"docker:{container_id}:{session_id}:{run_id}:"
                )
            )
        else:
            valid = False
        if not valid:
            return None
        return {
            "identity_kind": kind,
            "pid": pid,
            "process_group": process_group,
            "process_start_identity": start_identity,
            "container_id": container_id,
            "session_id": session_id,
        }

    @staticmethod
    def _restart_identity_has_startup_binding(
        lease: dict,
        current: dict[str, object] | None,
    ) -> bool:
        startup_token = str(lease.get("startup_token") or "")
        actual_digest = str(
            (current or {}).get("startup_token_digest") or ""
        ).strip()
        if not startup_token or not re.fullmatch(r"[0-9a-f]{64}", actual_digest):
            return False
        expected_digest = hashlib.sha256(startup_token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual_digest, expected_digest)

    def _reconcile_reserved_host_run_start(
        self,
        stale_lease: dict,
        *,
        worker: dict | None,
        identity_reader,
    ) -> str:
        """Confirm or safely retire one restart-surviving startup reservation."""

        worker_id = str(stale_lease.get("worker_id") or "")
        run_id = str(stale_lease.get("run_id") or "")
        callback_delivery: tuple[dict, dict, dict] | None = None
        try:
            guard = self._acquire_worker_lifecycle_guard(worker_id)
        except RuntimeErrorBase:
            return "unchanged"
        try:
            lease = self.store.get_host_run_lease(
                str(stale_lease.get("lease_id") or "")
            )
            worker = self.store.get_worker(worker_id)
            run = self.store.get_run(run_id)
            if (
                not lease
                or not worker
                or not run
                or str(lease.get("status") or "") != "active"
                or str(lease.get("startup_state") or "") != "reserved"
                or str(lease.get("startup_token") or "")
                != str(stale_lease.get("startup_token") or "")
                or str(run.get("state") or "") in TERMINAL_RUN_STATES
            ):
                return "unchanged"

            expired_claim_without_dispatch = bool(
                str(lease.get("expires_at") or "")
                <= datetime.now(timezone.utc).isoformat()
                and str(run.get("state") or "") == "claimed"
                and not str(run.get("runtime_invoked_at") or "")
                and self._reserved_start_identity(lease) is None
            )
            if expired_claim_without_dispatch:
                requeued = self.store.requeue_unconfirmed_host_run_start(
                    worker_id=worker_id,
                    run_id=run_id,
                    lease_id=str(lease.get("lease_id") or ""),
                    startup_token=str(lease.get("startup_token") or ""),
                    retry_after=datetime.now(timezone.utc).isoformat(),
                    error_text=(
                        "GlassHive retired an expired pre-dispatch lease and "
                        "queued the exact run for retry."
                    ),
                )
                if requeued is not None:
                    self._scheduler_wake_event.set()
                    return "released"
                return "unchanged"

            # This is deliberately a fresh durable-session read under the same
            # cross-process lifecycle flock used by launch and compute release.
            current_identity = (
                identity_reader(worker, run_id)
                if callable(identity_reader)
                else None
            )
            captured_identity = self._reserved_start_identity(lease)
            current_candidate = self._restart_identity_candidate(
                current_identity,
                run_id=run_id,
            )
            token_bound = self._restart_identity_has_startup_binding(
                lease, current_identity
            )
            if captured_identity is None and token_bound:
                captured_identity = current_candidate
            if (
                token_bound
                and captured_identity
                and self._restart_identity_matches_reserved(
                captured_identity, current_identity
                )
            ):
                callback_record, callbacks = self._run_start_callback_record(
                    worker,
                    run,
                    str(lease.get("startup_token") or ""),
                )
                confirmed = self.store.confirm_host_run_start(
                    worker_id=worker_id,
                    run_id=run_id,
                    run_started_at=str(
                        run.get("runtime_invoked_at")
                        or run.get("started_at")
                        or ""
                    ),
                    lease_id=str(lease.get("lease_id") or ""),
                    startup_token=str(lease.get("startup_token") or ""),
                    executor_id=str(lease.get("executor_id") or ""),
                    identity_kind=str(current_identity.get("identity_kind") or ""),
                    pid=int(current_identity.get("pid") or 0) or None,
                    process_group=int(current_identity.get("process_group") or 0)
                    or None,
                    process_start_identity=str(
                        current_identity.get("process_start_identity") or ""
                    ),
                    container_id=str(current_identity.get("container_id") or ""),
                    session_id=str(current_identity.get("session_id") or ""),
                    callback_record=callback_record,
                )
                if confirmed is None:
                    return "unchanged"
                callback = confirmed.get("callback")
                if isinstance(callback, dict):
                    callback_delivery = (dict(worker), callback, callbacks)
                return "renewed"

            cleanup = getattr(
                self.runtime, "cleanup_unconfirmed_run_start", None
            )
            cleaned = bool(
                captured_identity
                and callable(cleanup)
                and cleanup(worker, run_id, captured_identity)
            )
            if cleaned:
                requeued = self.store.requeue_unconfirmed_host_run_start(
                    worker_id=worker_id,
                    run_id=run_id,
                    lease_id=str(lease.get("lease_id") or ""),
                    startup_token=str(lease.get("startup_token") or ""),
                    retry_after=(
                        datetime.now(timezone.utc) + timedelta(seconds=1)
                    ).isoformat(),
                    error_text=(
                        "GlassHive safely cleaned an interrupted startup generation "
                        "and queued the exact run for retry."
                    ),
                )
                if requeued is not None:
                    self._scheduler_wake_event.set()
                    return "released"

            self.store.mark_host_run_start_termination_unconfirmed(
                lease_id=str(lease.get("lease_id") or ""),
                run_id=run_id,
                executor_id=str(lease.get("executor_id") or ""),
                startup_token=str(lease.get("startup_token") or ""),
            )
            return "unchanged"
        except Exception:
            logger.exception(
                "Reserved run startup reconciliation failed",
                extra={"worker_id": worker_id, "run_id": run_id},
            )
            return "unchanged"
        finally:
            guard.release()
            if callback_delivery is not None:
                callback_worker, callback, callbacks = callback_delivery
                self.executor.submit(
                    self._deliver_callback_record,
                    callback_worker,
                    callback,
                    callbacks,
                )

    def _finalize_run_if_state(
        self,
        run_id: str,
        expected_state: str,
        state: str,
        output_text: str = "",
        error_text: str = "",
        **fields: Any,
    ) -> dict | None:
        run = self.store.get_run(str(run_id))
        worker = (
            self.store.get_worker(str(run.get("worker_id") or "")) if run else None
        )
        artifact_refs = work_artifact_observation(
            self.files.artifact_worker(worker) if worker else None,
            run,
            output_text=output_text,
            error_text=error_text,
        )
        finalized = self.store.finalize_run_if_state(
            str(run_id),
            expected_state,
            state,
            output_text=output_text,
            error_text=error_text,
            artifact_refs=artifact_refs,
            **fields,
        )
        if (
            finalized is not None
            and (state in TERMINAL_RUN_STATES or state == "interrupted")
            and self._provider_request_reconciler is not None
        ):
            try:
                self._provider_request_reconciler(str(run_id))
            except Exception:
                logger.error(
                    "GlassHive terminal provider request reconciliation faulted safely",
                    extra={"error_code": "transient_dependency"},
                )
        return finalized

    def reconcile_terminal_artifact_observations(self, *, limit: int = 100) -> int:
        repaired = 0
        for candidate in self.store.list_terminal_runs_missing_artifact_observation(
            limit=limit
        ):
            run = self.store.get_run(str(candidate.get("run_id") or ""))
            worker = self.store.get_worker(str(candidate.get("worker_id") or ""))
            if not run or not worker:
                continue
            artifact_refs = work_artifact_observation(
                self.files.artifact_worker(worker),
                run,
                output_text=str(run.get("output_text") or ""),
                error_text=str(run.get("error_text") or ""),
            )
            if self.store.reconcile_terminal_artifact_observation(
                run_id=str(run["run_id"]), artifact_refs=artifact_refs
            ):
                repaired += 1
        return repaired

    def _configured_parallel_worker_route(
        self, profile: str, execution_mode: str, bundle: dict | None, *, fallback: bool = False,
        tenant_id: str = "local", owner_id: str = "",
    ) -> tuple[str, dict]:
        """Resolve explicit trusted preferences through the existing model catalog."""
        from .conversation_provider import GLASSHIVE_MODELS, _configured_grok_conversation_model

        result = dict(bundle or {})
        authority = result.get("viventium_launch_authority") or {}
        prefix = "fallback_worker" if fallback else "worker"
        model_id = authority.get(f"{prefix}_model")
        effort = authority.get(f"{prefix}_reasoning_effort")
        try:
            grok_native, _ = selected_grok_model(self.store, tenant_id, owner_id)
        except ModelConfigurationRequired:
            grok_native = ""
        configured_grok = _configured_grok_conversation_model(grok_native)
        catalog = {**GLASSHIVE_MODELS}
        if configured_grok is not None:
            catalog[configured_grok.id] = configured_grok
        selected = None
        if model_id is not None:
            selected = catalog.get(model_id)
            if selected is None or selected.harness_profile != profile:
                raise BackgroundWorkerConfigurationError("Configured background model is unknown or incompatible with its worker profile.")
            model = selected.native_model
        else:
            model = self._resolve_worker_model(profile, execution_mode, tenant_id=tenant_id, owner_id=owner_id)
        if effort is not None:
            if selected is None:
                selected = next((item for item in catalog.values()
                                 if item.harness_profile == profile and item.native_model == model), None)
            if selected is None or effort not in selected.effort_choices:
                raise BackgroundWorkerConfigurationError("Configured background reasoning effort is unsupported by its exact model.")
            effort_env = {"codex-cli": "WPR_CODEX_CLI_REASONING_EFFORT",
                          "claude-code": "WPR_CLAUDE_CODE_EFFORT"}.get(profile)
            if effort_env is None and not (profile == "grok-build" and effort == "default"):
                raise BackgroundWorkerConfigurationError("Configured background worker does not support reasoning effort.")
            if effort_env is not None:
                result["env"] = {**dict(result.get("env") or {}), effort_env: effort}
        result["provider_model"] = model
        return model, result













    def _queued_runtime_preflight(self, *args, **kwargs):
        try:
            return self._reserved_runtime_preflight(*args, **kwargs)
        except HostCapacityError as exc:
            if exc.capacity_class == "resource_probe_unavailable":
                if getattr(exc, "probe_error_code", "") not in {
                    "shared_substrate_proof_unavailable",
                    "native_network_membership_unverified",
                }:
                    raise
                # A container can leave the native network while a new run is
                # admitted. Keep the intent queued; launch still requires a
                # fresh healthy proof after the membership settles.
                return None
            if exc.capacity_class not in {
                "resource_pressure",
                "family_lane", "account", "tenant", "mutation_scope"
            }:
                raise
            # Acceptance is durable; the existing dispatcher acquires capacity
            # before running any worker or CLI probe. Do not reserve compute
            # for a queued objective or make the caller resubmit its goal.
            return None




    def active_work_pending_native_input(self, delegation: dict) -> dict | None:
        run_id = str(delegation.get("run_id") or delegation.get("current_run_id") or "")
        run = self.store.get_run(run_id)
        worker = self.store.get_worker(str(delegation.get("worker_id") or ""))
        if not run or not worker or str(run.get("state") or "") not in {"running", "paused"}:
            return None
        method = getattr(self.runtime, "pending_native_input", None)
        return method(worker, run_id=run_id) if callable(method) else None












    def _wake_worker_processor_later(self, worker_id: str, delay_s: float) -> None:
        if self._shutdown_event.is_set():
            return

        def wake() -> None:
            if not self._shutdown_event.is_set():
                self._ensure_worker_processor(worker_id)

        timer = Timer(max(0.1, float(delay_s)), wake)
        timer.daemon = True
        timer.start()

    def _schedule_worker_retry_after(self, worker_id: str, retry_after: str | None) -> None:
        if not retry_after:
            return
        try:
            parsed = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00"))
        except ValueError:
            self._wake_worker_processor_later(worker_id, self._scheduler_interval_s())
            return
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delay_s = max(
            0.1,
            parsed.astimezone(timezone.utc).timestamp()
            - datetime.now(timezone.utc).timestamp(),
        )
        self._wake_worker_processor_later(worker_id, delay_s)

    def process_due_worker_retries_once(self, *, limit: int = 1000) -> list[str]:
        if self._shutdown_event.is_set():
            return []
        worker_ids = self.store.list_due_retry_worker_ids(limit=limit)
        from .execution_profile import packaged_linux

        refresh_isolated_usage = getattr(
            self.runtime, "isolated_resource_usage", None
        )
        if callable(refresh_isolated_usage) and not packaged_linux():
            needs_capacity_refresh = any(
                str(
                    (self.store.get_worker(worker_id) or {}).get("execution_mode")
                    or "docker"
                )
                .strip()
                .lower()
                == "docker"
                and str(
                    (self.store.peek_next_queued_run(worker_id) or {}).get(
                        "last_retry_class"
                    )
                    or ""
                )
                == "host_capacity"
                for worker_id in worker_ids
            )
            if needs_capacity_refresh:
                try:
                    # Durable admission remains cached-only. The background
                    # retry scheduler refreshes a stale or expired Docker
                    # snapshot once before dispatching this due batch.
                    refresh_isolated_usage(cached_only=False)
                except Exception:
                    logger.warning(
                        "Due Docker capacity retry could not refresh resource usage",
                        exc_info=True,
                    )
        dispatched: list[str] = []
        for worker_id in worker_ids:
            if self._shutdown_event.is_set():
                break
            self._ensure_worker_processor(worker_id)
            dispatched.append(worker_id)
        return dispatched

    def discard_run_local_bundle(self, run_id: str) -> None:
        with self._run_local_bundles_lock:
            self._run_local_bundles.pop(str(run_id), None)

    def attach_run_local_bundle(
        self,
        run_id: str,
        bundle: dict | None,
        *,
        provider_request_id: str = "",
        response_timeout_s: float | None = None,
        response_deadline_at: str = "",
    ) -> bool:
        """Refresh one queued run's invocation-local authority after a retry.

        The exact bearer remains memory-only. A direct authorization retry may
        atomically reopen its blocked provider request and run; an invocation
        that already entered the provider must finish or fail without having
        authority changed underneath it.
        """

        if not bundle:
            return False
        run = self.store.get_run(str(run_id))
        if not run or str(run.get("state") or "") not in {
            "queued",
            "claimed",
            "admitted",
            "running",
            "needs_input",
        }:
            return False
        with self._run_local_bundles_lock:
            current = self.store.get_run(str(run_id))
            current_state = str((current or {}).get("state") or "")
            if not current or current_state not in {
                "queued",
                "claimed",
                "admitted",
                "running",
                "needs_input",
            }:
                return False
            if (
                current_state in {"claimed", "admitted", "running"}
                and bool(current.get("runtime_invoked_at"))
                and current_state != "running"
            ):
                return False
            if (
                current_state in {"claimed", "admitted", "running"}
                and str(run_id) not in self._run_local_grant_waiters
            ):
                return False
            if current_state == "needs_input":
                if str(provider_request_id or "").strip():
                    reopened = self.store.reopen_direct_provider_needs_input(
                        str(provider_request_id),
                        run_id=str(run_id),
                        response_timeout_s=response_timeout_s,
                        response_deadline_at=str(response_deadline_at or ""),
                    )
                    refreshed = (reopened or {}).get("run")
                else:
                    refreshed = self.store.transition_run_if_state(
                        str(run_id),
                        "needs_input",
                        "queued",
                        ended_at=None,
                        retry_after=None,
                        error_text="",
                        failure_class="",
                        failure_retryable=0,
                        failure_structured=0,
                        failure_user_message="",
                        failure_recommended_recovery="",
                        failure_diagnostic_summary="",
                    )
                if not refreshed:
                    return False
            self._run_local_bundles[str(run_id)] = dict(bundle)
            self._run_local_bundles_lock.notify_all()
            if current_state == "queued":
                self.store.update_run(
                    str(run_id),
                    retry_after=None,
                    error_text="",
                    failure_class="",
                    failure_retryable=0,
                    failure_structured=0,
                    failure_user_message="",
                    failure_recommended_recovery="",
                    failure_diagnostic_summary="",
                )
        return True

    def has_run_local_bundle(self, run_id: str) -> bool:
        with self._run_local_bundles_lock:
            return str(run_id) in self._run_local_bundles

    def _requires_conversation_invocation_bearer(self, worker: dict) -> bool:
        bundle = self._bootstrap_bundle_for(worker) or {}
        broker = bundle.get("glasshive_capability_broker")
        return bool(
            self._trusted_run_lane(worker) == "conversation"
            and isinstance(broker, dict)
            and str(broker.get("authority_kind") or "").strip()
            == "conversation_orchestrator"
        )

    def _mark_run_local_grant_waiter(self, worker: dict, run_id: str) -> None:
        if not self._requires_conversation_invocation_bearer(worker):
            return
        with self._run_local_bundles_lock:
            self._run_local_grant_waiters.add(str(run_id))

    def _clear_run_local_grant_waiter(self, run_id: str) -> None:
        with self._run_local_bundles_lock:
            self._run_local_grant_waiters.discard(str(run_id))

    def _exact_provider_session_bundle_for_run(
        self, bundle: dict[str, object], run_id: str
    ) -> dict[str, object]:
        """Project one request's durable native-session mode onto its run."""

        provider_request = self.store.get_provider_request_for_run(str(run_id))
        if provider_request is None:
            return bundle
        try:
            replay_decision = json.loads(
                str(provider_request.get("replay_decision_json") or "{}")
            )
        except (TypeError, json.JSONDecodeError):
            replay_decision = {}
        provider_session_mode = (
            str(replay_decision.get("provider_session_mode") or "persistent")
            if isinstance(replay_decision, dict)
            else "persistent"
        )
        exact_bundle = dict(bundle)
        exact_env = dict(exact_bundle.get("env") or {})
        exact_env.pop(GLASSHIVE_PROVIDER_SESSION_MODE_ENV, None)
        if provider_session_mode == "stateless":
            exact_env[GLASSHIVE_PROVIDER_SESSION_MODE_ENV] = "stateless"
        exact_bundle["env"] = exact_env
        return exact_bundle

    def _run_local_worker(
        self,
        worker: dict,
        run: dict,
        *,
        authority_context: dict[str, str] | None = None,
    ) -> dict:
        if bool(int(run.get("provider_liveness_route_locked") or 0)):
            locked_profile = str(run.get("provider_route_profile") or "").strip()
            locked_runtime = str(run.get("provider_route_runtime") or "").strip()
            locked_model = str(run.get("provider_route_model") or "").strip()
            if not locked_profile or not locked_runtime or not locked_model:
                raise RuntimeError("provider_liveness_route_lock_invalid")
            worker = {
                **worker,
                "profile": locked_profile,
                "backend": self._legacy_backend_label(
                    locked_profile,
                    str(worker.get("execution_mode") or "docker"),
                    "",
                ),
                "runtime": locked_runtime,
                "model": locked_model,
            }
        try:
            run_worker = self._run_local_admitted_worker(
                worker, run, authority_context=authority_context
            )
        except Exception:
            self._clear_run_local_grant_waiter(str(run["run_id"]))
            raise
        admission = run.get("allowed_ai_admission")
        if not isinstance(admission, dict):
            try:
                parsed_admission = json.loads(
                    str(run.get("allowed_ai_admission_json") or "{}")
                )
            except (TypeError, json.JSONDecodeError):
                parsed_admission = {}
            admission = parsed_admission if isinstance(parsed_admission, dict) else {}
        if admission:
            run_worker["_allowed_ai_admission"] = dict(admission)
            run_worker["allowed_ai_admission"] = dict(admission)
        admitted_bundle = self._bootstrap_bundle_for(run_worker) or {}
        admitted_env = (
            admitted_bundle.get("env")
            if isinstance(admitted_bundle.get("env"), dict)
            else {}
        )
        admitted_bearer = str(
            admitted_env.get(PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV) or ""
        ).strip()
        requires_invocation_bearer = self._requires_conversation_invocation_bearer(
            run_worker
        )
        run_id = str(run["run_id"])
        with self._run_local_bundles_lock:
            if requires_invocation_bearer:
                self._run_local_grant_waiters.add(run_id)
            transient = self._run_local_bundles.pop(run_id, None)
        transient_env = (
            transient.get("env")
            if isinstance(transient, dict) and isinstance(transient.get("env"), dict)
            else {}
        )
        has_invocation_bearer = bool(
            str(
                transient_env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or ""
            ).strip()
        )
        if requires_invocation_bearer and not has_invocation_bearer:
            deadline = time.monotonic() + 0.5
            with self._run_local_bundles_lock:
                try:
                    while not self._shutdown_event.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._run_local_bundles_lock.wait(timeout=remaining)
                        transient = self._run_local_bundles.pop(run_id, None)
                        transient_env = (
                            transient.get("env")
                            if isinstance(transient, dict)
                            and isinstance(transient.get("env"), dict)
                            else {}
                        )
                        if str(
                            transient_env.get(
                                "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
                            )
                            or ""
                        ).strip():
                            break
                finally:
                    self._run_local_grant_waiters.discard(run_id)
            has_invocation_bearer = bool(
                str(
                    transient_env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or ""
                ).strip()
            )
        elif requires_invocation_bearer:
            with self._run_local_bundles_lock:
                self._run_local_grant_waiters.discard(run_id)
        if requires_invocation_bearer and not has_invocation_bearer:
            raise BrokerAdmissionError(
                "conversation_capability_grant_required",
                "The conversation capability grant must be refreshed before this queued turn can start.",
                needs_input=True,
            )
        try:
            continuation_contract = json.loads(
                str(run.get("continuation_contract_json") or "{}")
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("continuation_contract_invalid") from exc
        if not isinstance(continuation_contract, dict):
            raise RuntimeError("continuation_contract_invalid")
        try:
            continuation_context = json.loads(
                str(run.get("continuation_context_json") or "{}")
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("continuation_context_invalid") from exc
        if not isinstance(continuation_context, dict):
            raise RuntimeError("continuation_context_invalid")
        if continuation_context:
            continuation_context = build_workspace_continuation_context(
                previous_run=run,
            )
        continuation_projection: dict[str, object] = {}
        if continuation_contract:
            continuation_projection["viventium_continuation_contract"] = (
                continuation_contract
            )
        if continuation_context:
            continuation_projection["viventium_continuation_context"] = (
                continuation_context
            )
        run_bundle = self._bootstrap_bundle_for(run_worker) or {}
        if continuation_projection:
            run_bundle = merge_bootstrap_bundle(
                run_bundle,
                continuation_projection,
            ) or {}
        if transient:
            run_bundle = merge_bootstrap_bundle(run_bundle, transient) or {}
        run_bundle = self._exact_provider_session_bundle_for_run(run_bundle, run_id)
        # Clean-room canonicalization deliberately excludes per-run continuation
        # state and bearer material from every durable/public merge. Apply only
        # these exact server-owned facts after all canonical merges, on the
        # in-memory run object that is handed directly to the runtime.
        run_bundle.update(continuation_projection)
        if continuation_context and "viventium_constraint_source" not in run_bundle:
            run_bundle["viventium_constraint_source"] = {
                "version": 1,
                "instruction": str(continuation_context["base_instruction"]),
            }
        run_bundle.pop("viventium_run_liveness", None)
        if str(run.get("liveness_mode") or "standard") == "declared_long":
            run_bundle["viventium_run_liveness"] = {
                "version": 1,
                "long_mission": True,
            }
        if admitted_bearer:
            run_bundle["env"] = {
                **(
                    run_bundle.get("env")
                    if isinstance(run_bundle.get("env"), dict)
                    else {}
                ),
                PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV: admitted_bearer,
            }
        # Attach server-owned accepted inputs after admission and transient
        # canonicalization, only to this invocation. Never mutate worker defaults.
        file_manifest = self.store.get_run_file_manifest(
            run_id,
            worker_id=str(run_worker["worker_id"]),
            tenant_id=str(run_worker.get("tenant_id") or "local"),
            owner_id=str(run_worker.get("owner_id") or ""),
        )
        run_input_manifest_path = ""
        if file_manifest:
            self.files.register_run_input_versions(run_worker, file_manifest)
            run_bundle["files"] = _merge_file_entries(
                run_bundle.get("files"),
                self.files.bootstrap_entries(
                    str(run_worker.get("tenant_id") or "local"),
                    str(run_worker.get("owner_id") or ""),
                    file_manifest,
                ),
            )
            # A reused workspace can contain another file with the same display
            # name. Give the native worker one bounded, run-local index of the
            # accepted paths instead of relying on filename search or expanding
            # an arbitrarily large file list into the prompt.
            run_input_manifest_path = (
                ".xperfect/run-inputs/"
                + hashlib.sha256(run_id.encode("utf-8")).hexdigest()
                + ".json"
            )
            run_bundle["files"] = _merge_file_entries(
                run_bundle["files"],
                [{
                    "scope": "workspace",
                    "path": run_input_manifest_path,
                    "content": json.dumps({
                        "version": 1,
                        "files": [
                            {
                                "name": item["name"],
                                "path": item["path"],
                                "sha256": item["sha256"],
                                "size_bytes": item["size_bytes"],
                            }
                            for item in file_manifest
                        ],
                    }, ensure_ascii=False, sort_keys=True),
                    "allow_empty": False,
                }],
            )
        return {
            **self.files.artifact_worker(run_worker),
            # The binder's native-start receipt must be tied to this durable
            # run. Keep the identity on the in-memory runtime worker; it is
            # never included in the public bootstrap bundle.
            "_active_run_id": run_id,
            "_run_attempt_id": str(run.get("active_attempt_id") or ""),
            "_run_input_manifest_path": run_input_manifest_path,
            "bootstrap_bundle_json": json.dumps(run_bundle, ensure_ascii=False),
        }

    @staticmethod
    def _run_inputs_prompt() -> dict:
        raw = (Path(__file__).with_name("prompts") / "worker-run-inputs.json").read_bytes()
        return json.loads(raw) | {"sha256": hashlib.sha256(raw).hexdigest()}

    @classmethod
    def _runtime_instruction_for_run(cls, run_worker: dict, instruction: str) -> str:
        path = str(run_worker.get("_run_input_manifest_path") or "")
        if not path:
            return instruction
        template = cls._run_inputs_prompt()["instruction"]
        return instruction + "\n\n" + template.format(index_path=path)

    def _finish_paused_worker_transition(
        self,
        worker: dict,
        active_run: dict | None,
        *,
        runtime_info: RuntimeInfo | None,
    ) -> dict:
        """Commit the durable half of Pause after the runtime is suspended."""

        worker_id = str(worker["worker_id"])
        active_state = str((active_run or {}).get("state") or "")
        paused_run = None
        if active_run and active_state in {"queued", "running", "settling"}:
            paused_run = self.store.transition_run_if_state(
                str(active_run["run_id"]),
                active_state,
                "paused",
                ended_at=None,
                error_text="Paused by operator",
            )
            if not paused_run:
                durable = self.store.get_run(str(active_run["run_id"])) or {}
                if str(durable.get("state") or "") in TERMINAL_RUN_STATES:
                    # Completion/cancellation that commits while the runtime
                    # pause RPC is in flight is authoritative.
                    return self.store.update_worker_state(
                        worker_id, "ready", last_error=""
                    ) or worker
                if str(durable.get("state") or "") == "paused":
                    return self.store.update_worker_state(
                        worker_id, "paused", last_error=""
                    ) or worker
                return worker

        updated = (
            self._apply_runtime_info(
                worker_id,
                runtime_info,
                state="paused",
                last_error=worker.get("last_error") or "",
            )
            if runtime_info is not None
            else self.store.update_worker_state(worker_id, "paused", last_error="")
        )
        if paused_run:
            self.store.add_event(
                worker["project_id"], worker_id, paused_run["run_id"], "run.paused", "Run paused by operator"
            )
            self.store.add_event(
                worker["project_id"], worker_id, paused_run["run_id"], "worker.paused", "Worker paused"
            )
            self._emit_callback(
                worker,
                "run.paused",
                run=paused_run,
                message="Worker paused",
            )
        elif active_run is None:
            self.store.add_event(
                worker["project_id"], worker_id, None, "worker.paused", "Worker paused"
            )
            self._emit_callback(
                worker,
                "worker.paused",
                run=None,
                message="Worker paused",
            )
        return updated or worker

    def _pause_worker_without_run(self, worker_id: str) -> dict:
        """Pause idle compute under the same durable lifecycle ownership."""

        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            if self.store.get_controllable_run(worker_id):
                raise RuntimeErrorBase(
                    "The worker lifecycle generation changed; retry Pause"
                )
            record_operator_pause_only = bool(
                str(worker.get("state") or "") == "paused"
                and worker.get("compute_released_at")
                and not self.store.has_active_operator_pause(worker_id)
            )
            if (
                str(worker.get("state") or "") == "paused"
                and not record_operator_pause_only
            ):
                return worker
            if (
                not record_operator_pause_only
                and str(worker.get("execution_mode") or "docker") == "host"
            ):
                raise RuntimeErrorBase(
                    "The exact host process identity is not confirmed; control remains pending"
                )
            claim = self.store.try_claim_worker_compute_release(
                worker_id,
                expected_updated_at=str(worker.get("updated_at") or ""),
                expected_last_run_id=str(worker.get("last_run_id") or ""),
                expected_state=str(worker.get("state") or ""),
                expected_container_id=self._runtime_compute_container_id(worker),
                owner=self._executor_id,
                ttl_s=self._compute_release_claim_ttl_s(),
                kind="pause_worker",
            )
            if claim is None:
                raise RuntimeErrorBase(
                    "The exact worker lifecycle generation changed; retry Pause"
                )
            claimed_worker = dict(claim.get("worker") or worker)
            info = None
            if not record_operator_pause_only:
                runtime_worker = self._require_claimed_container_generation(
                    claimed_worker
                )
                info = self.runtime.pause_worker(runtime_worker)
                if not self._runtime_control_info_is_confirmed(info):
                    raise RuntimeErrorBase(
                        "Runtime pause did not confirm the exact compute state"
                    )
            updated = self.store.finalize_worker_compute_release(
                worker_id,
                str(claim["token"]),
                int(claim["epoch"]),
                expected_kind="pause_worker",
                compute_released_at=worker.get("compute_released_at"),
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
                idle_state="paused",
            )
            if not updated:
                durable = self.store.get_worker(worker_id) or {}
                if str(durable.get("state") or "") in CLOSED_WORKER_STATES:
                    raise ControlPlaneConflict(
                        "Workspace is closed; create a new workspace for new work"
                    )
                raise RuntimeErrorBase(
                    "Pause lost the exact worker lifecycle generation before finalization"
                )
            if str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                raise ControlPlaneConflict(
                    "Workspace is closed; create a new workspace for new work"
                )
        self._replay_pending_lifecycle_effects()
        return updated

    def _execute_stop_run_claim(
        self,
        worker: dict,
        run_id: str,
        *,
        action_use_id: str = "",
    ) -> dict[str, object] | None:
        """Own, execute, and atomically finalize one exact run stop."""

        worker_id = str(worker.get("worker_id") or "")
        with self._worker_compute_release_lock(worker_id):
            current = self.store.get_worker(worker_id)
            target = self.store.get_run(run_id)
            existing_token = str(
                (current or {}).get("compute_release_token") or ""
            ).strip()
            if (
                not current
                or not target
                or str(target.get("worker_id") or "") != worker_id
                or (
                    str(target.get("state") or "") in TERMINAL_RUN_STATES
                    and not existing_token
                )
            ):
                return None
            expected_container_id = (
                str(current.get("compute_release_container_id") or "").strip()
                if existing_token
                else self._runtime_compute_container_id(current)
            )
            claim = self.store.try_claim_worker_compute_release(
                worker_id,
                expected_updated_at=str(current.get("updated_at") or ""),
                expected_last_run_id=str(current.get("last_run_id") or ""),
                expected_state=str(current.get("state") or ""),
                expected_container_id=expected_container_id,
                owner=self._executor_id,
                ttl_s=self._compute_release_claim_ttl_s(),
                kind="stop_run",
                target_run_id=run_id,
                expected_target_started_at=str(target.get("started_at") or ""),
                action_use_id=action_use_id,
                action_executor_id=self._executor_id if action_use_id else "",
            )
            if claim is None:
                return None
            claimed_worker = dict(claim.get("worker") or current)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            if not self.store.worker_compute_release_claim_matches(
                worker_id, token, epoch
            ):
                return None
            durable_target = self.store.get_run(run_id) or target
            if str(durable_target.get("state") or "") in TERMINAL_RUN_STATES:
                # Terminal truth that committed before recovery is authoritative.
                # Settle only the durable Stop/tombstone transaction; there is
                # no exact live target left that this control may signal.
                result = self.store.finalize_worker_work_stop_claim(
                    worker_id,
                    token,
                    epoch,
                    target_run_id=run_id,
                    runtime_fields={},
                    compute_released_at=claimed_worker.get("compute_released_at"),
                    error_text="Stopped by operator",
                )
                if result is None:
                    raise RuntimeError(
                        "Terminal-won run stop ownership changed before finalization"
                    )
                return {**result, "confirmation_pending": False}
            runtime_worker = self._worker_with_host_lease(claimed_worker, run_id)
            runtime_worker = {
                **runtime_worker,
                "_compute_release_container_id": str(
                    claimed_worker.get("compute_release_container_id") or ""
                ).strip(),
            }
            captured_container_id = str(
                claimed_worker.get("compute_release_container_id") or ""
            ).strip()
            if captured_container_id:
                current_container_id = self._runtime_compute_container_id(
                    claimed_worker
                )
                if current_container_id != captured_container_id:
                    raise RuntimeErrorBase(
                        "Worker sandbox generation changed before exact-run stop"
                    )
            active_lease = self.store.get_active_host_run_lease_for_run(run_id)
            prelaunch_reservation = bool(
                active_lease
                and str(active_lease.get("startup_state") or "") == "reserved"
                and not int(active_lease.get("pid") or 0)
                and not str(
                    active_lease.get("process_start_identity") or ""
                ).strip()
                and not str(
                    active_lease.get("startup_container_id") or ""
                ).strip()
                and not str(
                    active_lease.get("startup_session_id") or ""
                ).strip()
            )
            no_started_compute = bool(
                str(target.get("state") or "") in {"queued", "needs_input"}
                and not active_lease
            )
            if prelaunch_reservation or no_started_compute:
                # The work-stop tombstone won before any external identity was
                # accepted. The waiting processor will fail its reservation
                # re-read under the same flock, so there is nothing to signal.
                info = RuntimeInfo(
                    runtime=str(claimed_worker.get("runtime") or ""),
                    model=str(claimed_worker.get("model") or ""),
                    gateway_url=str(claimed_worker.get("gateway_url") or ""),
                    gateway_port=claimed_worker.get("gateway_port"),
                    gateway_token=claimed_worker.get("gateway_token"),
                    session_key=claimed_worker.get("session_key"),
                    state_dir=claimed_worker.get("state_dir"),
                    workspace_dir=claimed_worker.get("workspace_dir"),
                    pid=None,
                )
            else:
                try:
                    info = self.runtime.interrupt_worker(
                        runtime_worker, run_id=run_id
                    )
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(runtime_worker)
            if info.pid is not None:
                updated = self._apply_runtime_info(
                    worker_id,
                    info,
                    state="stopping",
                    last_error="",
                )
                return {
                    "worker": updated or claimed_worker,
                    "run": target,
                    "confirmation_pending": True,
                    "target_transitioned": False,
                }
            result = self.store.finalize_worker_work_stop_claim(
                worker_id,
                token,
                epoch,
                target_run_id=run_id,
                runtime_fields=self._runtime_info_fields(
                    worker_id,
                    info,
                    last_error="",
                ),
                compute_released_at=claimed_worker.get("compute_released_at"),
                error_text="Stopped by operator",
            )
            if result is None:
                raise RuntimeError("Run stop ownership changed before finalization")
            return {
                **result,
                "confirmation_pending": False,
            }

    def _recover_stop_run_claim(
        self,
        worker: dict,
        run_id: str,
    ) -> dict[str, object] | None:
        result = self._execute_stop_run_claim(worker, run_id)
        if not result or result.get("confirmation_pending"):
            return None
        updated = dict(result.get("worker") or {})
        if updated.get("state") not in {"paused", "terminated", "needs_input"}:
            if self.store.has_queued_runs(str(worker.get("worker_id") or "")):
                self._ensure_worker_processor(str(worker.get("worker_id") or ""))
        return {
            "worker_id": worker.get("worker_id"),
            "project_id": worker.get("project_id"),
            "tenant_id": worker.get("tenant_id"),
            "owner_id": worker.get("owner_id"),
            "state": updated.get("state"),
            "kind": "stop_run",
            "target_run_id": run_id,
            "target_transitioned": bool(result.get("target_transitioned")),
        }

    def stop_run(
        self,
        worker_id: str,
        run_id: str,
        *,
        action_use_id: str = "",
    ) -> dict[str, object]:
        """Stop one exact run without claiming cancellation before process death is proven."""

        worker = self.require_worker(worker_id)
        target_run = self.store.get_run(run_id)
        if (
            not target_run
            or str(target_run.get("worker_id") or "") != worker_id
            or str(target_run.get("state") or "") in TERMINAL_RUN_STATES
        ):
            return {
                "worker": worker,
                "run": target_run,
                "confirmation_pending": False,
                "accepted": False,
            }
        try:
            operation = self._execute_stop_run_claim(
                worker, run_id, action_use_id=action_use_id
            )
        except Exception as exc:
            error_text = (
                public_callback_message_text(str(exc))
                or "Run termination could not be confirmed"
            )
            logger.warning(
                "GlassHive exact-run stop remains pending because termination was not confirmed",
                extra={"worker_id": worker_id, "run_id": run_id},
                exc_info=True,
            )
            updated = self.store.update_worker_state(
                worker_id,
                "stopping",
                last_error=error_text,
            )
            message = "Run stop requested; termination confirmation is pending"
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run_id,
                "run.stopping",
                f"{message}: {error_text}",
            )
            return {
                "worker": updated or worker,
                "run": target_run,
                "confirmation_pending": True,
                "accepted": True,
                "termination_error": error_text,
            }
        if operation is None:
            current = self.store.get_worker(worker_id) or worker
            pending = bool(
                current.get("compute_release_token")
                and str(current.get("compute_release_kind") or "") == "stop_run"
                and str(current.get("compute_release_scope") or "") == "work"
                and str(current.get("compute_release_target_run_id") or "") == run_id
                and str(current.get("work_stop_id") or "")
                == str(current.get("compute_release_operation_id") or "")
            )
            if pending and action_use_id:
                action_record = self.store.get_active_work_action(action_use_id) or {}
                pending = bool(
                    str(action_record.get("lifecycle_operation_id") or "")
                    == str(current.get("work_stop_id") or "")
                    and str(action_record.get("lifecycle_operation_kind") or "")
                    == "stop_run"
                    and str(action_record.get("lifecycle_target_run_id") or "")
                    == run_id
                )
            return {
                "worker": current,
                "run": self.store.get_run(run_id),
                "confirmation_pending": pending,
                "accepted": pending,
            }
        confirmation_pending = bool(operation.get("confirmation_pending"))
        updated = dict(operation.get("worker") or worker)
        if confirmation_pending:
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run_id,
                "run.stopping",
                "Run stop requested; termination confirmation is pending",
            )
            self._emit_callback(
                worker,
                "run.stopping",
                run={**target_run, "state": "stopping"},
                message="Run stop requested; termination confirmation is pending",
            )
            return {
                "worker": updated,
                "run": target_run,
                "confirmation_pending": True,
                "accepted": True,
            }
        cancelled_run = operation.get("run")
        transitioned = bool(operation.get("target_transitioned"))
        self._replay_pending_lifecycle_effects()
        if (
            updated.get("state") not in {"paused", "terminated", "needs_input"}
            and self.store.has_queued_runs(worker_id)
        ):
            self._ensure_worker_processor(worker_id)
        return {
            "worker": updated,
            "run": cancelled_run or self.store.get_run(run_id),
            "confirmation_pending": False,
            "accepted": bool(operation.get("work_stop_outcome")) or transitioned,
            "work_stop_outcome": str(operation.get("work_stop_outcome") or ""),
        }

    def _finish_resumed_worker_transition(
        self,
        worker: dict,
        paused_run: dict,
        runtime_updated_worker: dict,
    ) -> dict:
        """Commit the exact paused run after the runtime resume RPC succeeds."""

        worker_id = str(worker["worker_id"])
        # Docker pause freezes the live provider process, so unpausing can
        # continue it in place. Host pause terminates the provider process;
        # requeue the same durable run for the same isolated workspace.
        resume_state = (
            "queued"
            if str(worker.get("execution_mode") or "docker") == "host"
            or not paused_run.get("started_at")
            else "running"
        )
        resumed_run = self.store.transition_run_if_state(
            str(paused_run["run_id"]),
            "paused",
            resume_state,
            ended_at=None,
            error_text="",
            retry_after=None,
        )
        if resumed_run:
            refreshed = self.store.update_worker_state(
                worker_id,
                "running" if resume_state == "running" else "starting",
                last_error="",
            )
            self.store.add_event(
                str(worker["project_id"]),
                worker_id,
                str(paused_run["run_id"]),
                "run.resumed",
                "Paused run resumed",
            )
            self._emit_callback(
                worker,
                "run.started" if resume_state == "running" else "run.queued",
                run=resumed_run,
                message=(
                    "Paused run resumed"
                    if resume_state == "running"
                    else "Paused run queued for execution restart"
                ),
            )
            if resume_state == "queued":
                self._ensure_worker_processor(worker_id)
            return refreshed or runtime_updated_worker
        durable = self.store.get_run(str(paused_run["run_id"])) or {}
        if str(durable.get("state") or "") in TERMINAL_RUN_STATES:
            return self.store.update_worker_state(
                worker_id, "ready", last_error=""
            ) or runtime_updated_worker
        if str(durable.get("state") or "") in {"queued", "running"}:
            return self.store.update_worker_state(
                worker_id,
                "running" if str(durable.get("state")) == "running" else "starting",
                last_error="",
            ) or runtime_updated_worker
        return runtime_updated_worker

    def _resume_worker_without_run(self, worker_id: str) -> dict:
        """Resume an idle paused worker only after its startup handshake succeeds."""

        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            worker = self._refresh_worker_model_for_profile(worker)
            if str(worker.get("state") or "") != "paused":
                return worker
            if self.store.get_controllable_run(worker_id):
                raise RuntimeErrorBase(
                    "The worker lifecycle generation changed; retry Resume"
                )
            expected_container_id = (
                str(worker.get("compute_release_container_id") or "").strip()
                if str(worker.get("compute_release_token") or "").strip()
                else self._runtime_compute_container_id(worker)
            )
            claim = self.store.try_claim_worker_compute_release(
                worker_id,
                expected_updated_at=str(worker.get("updated_at") or ""),
                expected_last_run_id=str(worker.get("last_run_id") or ""),
                expected_state=str(worker.get("state") or ""),
                expected_container_id=expected_container_id,
                owner=self._executor_id,
                ttl_s=self._compute_release_claim_ttl_s(),
                kind="resume_worker",
            )
            if claim is None:
                raise RuntimeErrorBase(
                    "The exact worker lifecycle generation changed; retry Resume"
                )
            claimed_worker = dict(claim.get("worker") or worker)
            try:
                info = self.runtime.ensure_worker_ready(claimed_worker)
            except Exception as exc:
                self._restore_failed_resume_claim(
                    worker=claimed_worker,
                    token=str(claim["token"]),
                    epoch=int(claim["epoch"]),
                    kind="resume_worker",
                    target_run_id="",
                    startup_error=exc,
                )
                raise
            updated = self.store.finalize_worker_compute_release(
                worker_id,
                str(claim["token"]),
                int(claim["epoch"]),
                expected_kind="resume_worker",
                compute_released_at=None,
                runtime_fields=self._runtime_info_fields(
                    worker_id, info, last_error=""
                ),
                idle_state="ready",
            )
            if not updated:
                raise RuntimeErrorBase(
                    "Resume lost the exact worker lifecycle generation before finalization"
                )
        self._replay_pending_lifecycle_effects()
        return updated

    def _execute_worker_termination_claim(
        self, worker: dict
    ) -> dict[str, object] | None:
        """Terminate a whole worker only while the exact durable claim owns it."""

        worker_id = str(worker.get("worker_id") or "")
        with self._worker_compute_release_lock(worker_id):
            claim: dict[str, object] | None = None
            current: dict = {}
            target_run_id = ""
            # A queue processor can atomically promote queued -> running just
            # before this whole-worker tombstone is reserved. That benign CAS
            # loss must not turn an explicit Terminate into a false conflict.
            # Re-read the durable generation while the lifecycle guard keeps
            # the promoted run behind its final pre-launch boundary.
            for _attempt in range(3):
                current = self.store.get_worker(worker_id) or {}
                if not current:
                    return None
                if str(current.get("state") or "") == "terminated" and not str(
                    current.get("compute_release_token") or ""
                ).strip():
                    return {"worker": current, "target_transitioned": False}
                existing_token = str(
                    current.get("compute_release_token") or ""
                ).strip()
                if existing_token:
                    target_run_id = str(
                        current.get("compute_release_target_run_id") or ""
                    ).strip()
                    target_started_at = str(
                        current.get("compute_release_target_started_at") or ""
                    ).strip()
                    expected_container_id = str(
                        current.get("compute_release_container_id") or ""
                    ).strip()
                else:
                    target = self.store.get_active_run(
                        worker_id
                    ) or self.store.get_controllable_run(worker_id)
                    target_run_id = str((target or {}).get("run_id") or "").strip()
                    target_started_at = str(
                        (target or {}).get("started_at") or ""
                    ).strip()
                    expected_container_id = self._runtime_compute_container_id(current)
                claim = self.store.try_claim_worker_compute_release(
                    worker_id,
                    expected_updated_at=str(current.get("updated_at") or ""),
                    expected_last_run_id=str(current.get("last_run_id") or ""),
                    expected_state=str(current.get("state") or ""),
                    expected_container_id=expected_container_id,
                    owner=self._executor_id,
                    ttl_s=self._compute_release_claim_ttl_s(),
                    kind="terminate_worker",
                    target_run_id=target_run_id,
                    expected_target_started_at=target_started_at,
                )
                if claim is not None:
                    break
                refreshed = self.store.get_worker(worker_id) or {}
                if str(refreshed.get("compute_release_token") or "").strip():
                    return None
            if claim is None:
                return None
            claimed_worker = dict(claim.get("worker") or current)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            if not self.store.worker_compute_release_claim_matches(
                worker_id, token, epoch
            ):
                return None
            if str(claimed_worker.get("execution_mode") or "docker") == "docker":
                captured_container_id = str(
                    claimed_worker.get("compute_release_container_id") or ""
                ).strip()
                current_container_id = self._runtime_compute_container_id(
                    claimed_worker
                )
                if current_container_id != captured_container_id:
                    rebound = self.store.rebind_worker_termination_claim_generation(
                        worker_id,
                        token,
                        epoch,
                        container_id=current_container_id,
                    )
                    if rebound is None:
                        raise RuntimeErrorBase(
                            "Worker termination generation changed before rebinding"
                        )
                    claimed_worker = rebound
                    epoch = int(rebound["compute_release_epoch"])
            runtime_worker = (
                self._worker_with_host_lease(claimed_worker, target_run_id)
                if target_run_id
                else claimed_worker
            )
            runtime_worker = {
                **runtime_worker,
                "_compute_release_container_id": str(
                    claimed_worker.get("compute_release_container_id") or ""
                ).strip(),
            }
            terminal_run_id = str(
                claimed_worker.get("last_run_id") or ""
            ).strip()
            terminal_run = (
                self.store.get_run(terminal_run_id) if terminal_run_id else None
            )
            if terminal_run and str(terminal_run.get("state") or "") in TERMINAL_RUN_STATES:
                runtime_worker["_terminal_run_id"] = terminal_run_id
            active_host_lease = (
                self.store.get_active_host_run_lease_for_run(target_run_id)
                if target_run_id
                else None
            )
            no_started_host_compute = bool(
                str(claimed_worker.get("execution_mode") or "docker") == "host"
                and (
                    active_host_lease is None
                    or str(active_host_lease.get("startup_state") or "")
                    == "reserved"
                )
            )
            if no_started_host_compute:
                info = RuntimeInfo(
                    runtime=str(claimed_worker.get("runtime") or ""),
                    model=str(claimed_worker.get("model") or ""),
                    gateway_url=str(claimed_worker.get("gateway_url") or ""),
                    gateway_port=claimed_worker.get("gateway_port"),
                    gateway_token=claimed_worker.get("gateway_token"),
                    session_key=claimed_worker.get("session_key"),
                    state_dir=claimed_worker.get("state_dir"),
                    workspace_dir=claimed_worker.get("workspace_dir"),
                    pid=None,
                )
            else:
                info = self.runtime.terminate_worker(runtime_worker)
            updated = self.store.finalize_worker_termination_claim(
                worker_id,
                token,
                epoch,
                compute_released_at=utc_now(),
                runtime_fields=self._runtime_info_fields(
                    worker_id,
                    info,
                    last_error="",
                ),
                error_text="Worker terminated by operator",
            )
            if updated is None:
                raise RuntimeError("Worker termination ownership changed before finalization")
            return {"worker": updated, "target_transitioned": True}



    def _runtime_info_fields(
        self,
        worker_id: str,
        info: RuntimeInfo,
        *,
        last_error: str,
    ) -> dict[str, object]:
        fields: dict[str, object] = {
            "runtime": info.runtime,
            "model": info.model,
            "gateway_url": info.gateway_url,
            "gateway_port": info.gateway_port,
            "gateway_token": info.gateway_token,
            "session_key": info.session_key,
            "state_dir": info.state_dir,
            "workspace_dir": info.workspace_dir,
            "pid": info.pid,
            "takeover_url": f"/ui/workers/{worker_id}",
            "control_url": f"/ui/workers/{worker_id}",
            "last_error": last_error,
        }
        worker = self.store.get_worker(worker_id)
        bundle = self._bootstrap_bundle_for(worker) if worker else None
        env = bundle.get("env") if isinstance(bundle, dict) else None
        if (
            info.session_key is None
            and isinstance(env, dict)
            and str(env.get(GLASSHIVE_PROVIDER_SESSION_MODE_ENV) or "")
            == "stateless"
        ):
            fields.pop("session_key")
        return fields

    def _ensure_surviving_run_monitor(self, worker_id: str, run_id: str) -> None:
        """Adopt one verified live restart survivor without launching a duplicate run."""

        worker = self.store.get_worker(worker_id) or {}
        executor = (
            self.conversation_executor
            if self._trusted_run_lane(worker) == "conversation"
            else self.executor
        )
        with self._processors_lock:
            if self._shutdown_event.is_set() or worker_id in self._active_processors:
                return
            generation = self._processor_generations.get(worker_id, 0) + 1
            self._processor_generations[worker_id] = generation
            self._active_processors.add(worker_id)
            try:
                executor.submit(
                    self._monitor_surviving_run,
                    worker_id,
                    run_id,
                    generation,
                )
            except Exception:
                self._active_processors.discard(worker_id)
                raise

    def _release_reconciled_run_lease(self, run_id: str, *, reason: str) -> None:
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        if lease:
            self.store.release_host_run_lease(
                str(lease["lease_id"]),
                executor_id=None,
                reason=reason,
            )

    def _monitor_surviving_run(
        self,
        worker_id: str,
        run_id: str,
        generation: int,
    ) -> None:
        """Collect or truthfully requeue a process adopted after service restart."""

        interval = _bounded_float_env(
            "WPR_SURVIVOR_MONITOR_INTERVAL_S",
            0.5,
            min_value=0.01,
            max_value=10.0,
        )
        try:
            while (
                not self._shutdown_event.is_set()
                and self._processor_is_current(worker_id, generation)
            ):
                try:
                    worker = self.store.get_worker(worker_id)
                    run = self.store.get_run(run_id)
                    if not worker or not run:
                        return
                    if str(run.get("state") or "") in TERMINAL_RUN_STATES:
                        self._release_reconciled_run_lease(
                            run_id, reason="survivor_terminal"
                        )
                        return

                    recovered = self._collect_completed_run(worker, run)
                    if recovered:
                        self._apply_recovered_run(worker, run, recovered)
                        self._release_reconciled_run_lease(
                            run_id, reason="survivor_terminal"
                        )
                        return

                    runtime_worker = {
                        **worker,
                        "_active_run_id": run_id,
                        "_run_attempt_id": str(run.get("active_attempt_id") or ""),
                    }
                    info = self.runtime.reconcile_worker(runtime_worker)
                    if info.pid:
                        self._apply_runtime_info(
                            worker_id,
                            info,
                            state="running",
                            last_error=worker.get("last_error") or "",
                        )
                    else:
                        # The process may have written its terminal evidence just
                        # before disappearing. Collect once more before retrying.
                        recovered = self._collect_completed_run(worker, run)
                        if recovered:
                            self._apply_recovered_run(worker, run, recovered)
                            self._release_reconciled_run_lease(
                                run_id, reason="survivor_terminal"
                            )
                            return
                        self._release_reconciled_run_lease(
                            run_id, reason="survivor_process_exited"
                        )
                        retry_after = (
                            datetime.now(timezone.utc) + timedelta(seconds=1)
                        ).isoformat()
                        retry_generation = self.store.get_run_retry_generation(
                            run_id
                        )
                        if retry_generation is None or str(
                            retry_generation.get("expected_attempt_id") or ""
                        ) != str(run.get("active_attempt_id") or ""):
                            return
                        requeued = self.store.requeue_run_for_retry(
                            run_id,
                            retry_after=retry_after,
                            **retry_generation,
                            error_text=(
                                "The adopted provider process exited before GlassHive could "
                                "confirm a terminal result."
                            ),
                            last_retry_class="provider_temporarily_unavailable",
                            failure_class="provider_temporarily_unavailable",
                            failure_retryable=1,
                            failure_structured=1,
                            failure_user_message=(
                                "The provider worker stopped before its result was confirmed; "
                                "GlassHive will retry this work."
                            ),
                            failure_recommended_recovery=(
                                "No action is required unless the retry remains queued."
                            ),
                            failure_diagnostic_summary=(
                                "A restart-adopted process disappeared without terminal evidence."
                            ),
                        )
                        self.store.update_worker_state(
                            worker_id, "ready", last_error=""
                        )
                        self.store.add_event(
                            str(worker.get("project_id") or ""),
                            worker_id,
                            run_id,
                            "run.requeued",
                            "Restart survivor exited without terminal evidence; retry queued",
                        )
                        self._emit_callback(
                            worker,
                            "run.requeued",
                            run={**run, **(requeued or {}), "state": "queued"},
                            message=(
                                "The provider worker stopped before its result was confirmed; "
                                "GlassHive will retry this work."
                            ),
                        )
                        self._scheduler_wake_event.set()
                        return
                except Exception:
                    logger.exception(
                        "Restart survivor monitor pass failed",
                        extra={"worker_id": worker_id, "run_id": run_id},
                    )
                if self._shutdown_event.wait(interval):
                    return
        finally:
            try:
                released = self._release_processor(worker_id, generation)
                if released:
                    worker = self.store.get_worker(worker_id)
                    if (
                        worker
                        and worker["state"]
                        not in {"paused", "needs_input", "stopping", "terminated"}
                        and self.store.peek_next_queued_run(worker_id)
                    ):
                        self._ensure_worker_processor(worker_id)
            except Exception:
                logger.exception(
                    "Failed to release restart survivor monitor ownership",
                    extra={"worker_id": worker_id, "run_id": run_id},
                )

    def _record_late_processor_terminal_ignored(
        self,
        worker: dict,
        run: dict,
        attempted_state: str,
    ) -> dict:
        durable_run = self.store.get_run(str(run["run_id"])) or run
        self.store.add_event(
            str(worker["project_id"]),
            str(worker["worker_id"]),
            str(run["run_id"]),
            "run.late_completion_ignored",
            (
                f"Ignored late processor {attempted_state} because the durable run state is "
                f"{str(durable_run.get('state') or 'unknown')}"
            ),
        )
        return durable_run
