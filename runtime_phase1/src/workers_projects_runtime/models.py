from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .recurrence import canonical_recurrence_owner
from .runtime_identity import derive_legacy_backend_label

ProjectStatus = Literal["active", "paused", "completed", "archived", "failed"]
WorkerState = Literal[
    "created",
    "starting",
    "ready",
    "running",
    "stopping",
    "paused",
    "needs_input",
    "failed",
    "terminated",
]
PublicWorkerState = Literal[
    "created",
    "starting",
    "ready",
    "running",
    "paused",
    "failed",
    "terminated",
]
WorkerCloseState = Literal["terminating", "termination_failed", "terminated"]
CLOSED_WORKER_STATES = frozenset({"terminating", "termination_failed", "terminated"})
RunState = Literal[
    "queued",
    "running",
    "interrupted",
    "paused",
    "completed",
    "failed",
    "cancelled",
]
ScheduleState = Literal[
    "pending",
    "running",
    "queued",
    "completed",
    "failed",
    "cancelled",
]
RecurringScheduleOccurrenceState = Literal[
    "pending",
    "claimed",
    "running",
    "queued",
    "completed",
    "failed",
    "cancelled",
    "skipped",
    "retryable",
    "action_required",
]
RecurrenceType = Literal["once", "daily", "interval", "cron", "rfc5545"]
RecurrenceDstPolicy = Literal["next_valid_earliest", "next_valid_latest"]
RecurrenceOverlapPolicy = Literal["skip", "queue"]
RecurrenceCatchUpPolicy = Literal["skip", "bounded", "coalesce"]
DesktopActionName = Literal["terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"]
ExecutionMode = Literal["docker", "host"]
WorkspaceKind = Literal["named", "ephemeral", "legacy"]
WORKSPACE_KINDS = {"named", "ephemeral", "legacy"}


def normalize_workspace_kind(value: object) -> WorkspaceKind:
    normalized = str(value or "legacy").strip().lower()
    if normalized not in WORKSPACE_KINDS:
        raise ValueError("workspace kind must be named, ephemeral, or legacy")
    return cast(WorkspaceKind, normalized)


def normalize_workspace_tags(values: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        tag = str(value or "").strip().casefold()
        if not tag or tag in seen:
            continue
        if len(tag) > 64:
            raise ValueError("workspace tags must be 64 characters or fewer")
        seen.add(tag)
        normalized.append(tag)
    if len(normalized) > 32:
        raise ValueError("a workspace can have at most 32 tags")
    return normalized


class WorkspaceDuplicateReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_state: Literal["pending", "copied", "empty", "filtered", "missing", "template"]
    copied_files: int = Field(ge=0)
    skipped_items: int = Field(ge=0)


WorkerResourceClass = Literal["standard", "light"]

STANDARD_WORKER_MEMORY_MIB = 3072

LIGHT_WORKER_MEMORY_MIB = 1536

def normalize_worker_resource_class(value: object) -> WorkerResourceClass:
    clean = str(value or "standard").strip().lower()
    if clean not in {"standard", "light"}:
        raise ValueError("Worker resource class must be standard or light")
    return cast(WorkerResourceClass, clean)

def worker_resource_memory_bytes(
    resource_class: object,
    *,
    standard_memory_mib: int = STANDARD_WORKER_MEMORY_MIB,
) -> int:
    clean = normalize_worker_resource_class(resource_class)
    memory_mib = (
        LIGHT_WORKER_MEMORY_MIB
        if clean == "light"
        else int(standard_memory_mib)
    )
    if memory_mib <= 0:
        raise ValueError("Worker memory reservation must be positive")
    return memory_mib * 1024**2

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateProjectRequest(BaseModel):
    owner_id: str
    title: str
    goal: str
    default_worker_profile: str = ""


class ProjectResponse(BaseModel):
    project_id: str
    tenant_id: str = "local"
    owner_id: str
    title: str
    goal: str
    status: ProjectStatus
    summary: str = ""
    default_worker_profile: str
    created_at: str
    updated_at: str
    origin_surface: str = ""

    @model_validator(mode="before")
    @classmethod
    def project_origin_surface(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        scope = value.get("origin_scope")
        if not isinstance(scope, dict):
            return value
        surface = scope.get("surface")
        return {**value, "origin_surface": surface if isinstance(surface, str) else ""}


class CreateWorkerRequest(BaseModel):
    owner_id: str
    name: str
    role: str
    profile: str = Field(
        default="",
        description="Worker profile selector. Empty means use the project/deployment default.",
    )
    backend: str = Field(
        default="",
        description="Deprecated compatibility field. Runtime is derived from profile and execution_mode.",
    )
    execution_mode: str = Field(
        default="",
        description="Execution mode, host or docker. Empty means use the deployment default.",
    )
    resource_class: WorkerResourceClass = "standard"
    alias: str | None = None
    workspace_root: str | None = None
    bootstrap_profile: str | None = None
    bootstrap_bundle: dict[str, object] | None = None
    start_synchronously: bool = True
    workspace_kind: WorkspaceKind = "legacy"
    tags: list[str] = Field(default_factory=list)


    resource_class: WorkerResourceClass = "standard"


class DuplicateWorkerRequest(BaseModel):
    owner_id: str
    source_worker_id: str
    name: str
    role: str


class WorkerResponse(BaseModel):
    worker_id: str
    workspace_id: str | None = None
    project_id: str
    tenant_id: str = "local"
    owner_id: str
    name: str
    role: str
    profile: str
    backend: str
    execution_mode: ExecutionMode = "docker"
    resource_class: WorkerResourceClass = "standard"
    resource_memory_bytes: int | None = None
    alias: str | None = None
    runtime: str = ""
    model: str = ""
    state: PublicWorkerState
    close_state: WorkerCloseState | None = None
    bootstrap_profile: str | None = None
    gateway_url: str | None = None
    takeover_url: str | None = None
    control_url: str | None = None
    gateway_port: int | None = None
    session_key: str | None = None
    state_dir: str | None = None
    workspace_dir: str | None = None
    workspace_root: str | None = None
    favorite: bool = False
    workspace_kind: WorkspaceKind = "legacy"
    tags: list[str] = Field(default_factory=list)
    last_activity_at: str = ""
    duplication_report: WorkspaceDuplicateReport | None = None
    compute_released_at: str | None = None
    last_run_id: str | None = None
    current_run_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="before")
    @classmethod
    def derive_legacy_backend_from_profile(cls, data):
        if not isinstance(data, dict):
            return data
        raw_state = str(data.get("state") or "")
        if raw_state in {"terminating", "termination_failed"}:
            data = dict(data)
            data["close_state"] = raw_state
            # Keep the frozen public state enum compatible while the optional close-state field
            # carries truthful close progress for modern clients.
            data["state"] = "terminated"
        elif raw_state == "stopping":
            data = dict(data)
            data["state"] = "running"
        elif raw_state == "needs_input":
            data = dict(data)
            data["state"] = "paused"
        backend = derive_legacy_backend_label(
            profile=data.get("profile"),
            runtime=data.get("runtime"),
            backend=data.get("backend"),
        )
        if backend:
            data = dict(data)
            data["backend"] = backend
        if not data.get("current_run_id") and data.get("state") in {"running", "paused"}:
            data = dict(data)
            data["current_run_id"] = data.get("last_run_id") or None
        return data


    resource_class: WorkerResourceClass = "standard"

    resource_memory_bytes: int | None = None


class WorkspaceContinuationContextRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: Literal[1]
    base_instruction: str = Field(
        alias="baseInstruction", min_length=1, max_length=131072
    )
    guidance: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_guidance(self) -> "WorkspaceContinuationContextRequest":
        if any(
            not item.strip()
            or len(item.encode("utf-8")) > 100 * 1024
            for item in self.guidance
        ) or sum(len(item.encode("utf-8")) for item in self.guidance) > 512 * 1024:
            raise ValueError("Workspace continuation context is invalid")
        return self

class AssignRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    instruction: str = Field(min_length=1)
    effort: str | None = None
    bootstrap_bundle: dict[str, object] | None = None
    continuation_context: WorkspaceContinuationContextRequest | None = Field(
        default=None, alias="continuationContext"
    )

class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1)


class CreateDelegationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=10000)
    instruction: str = Field(min_length=1, max_length=100000)
    profile: str = Field(default="", max_length=100)
    execution_mode: str = Field(default="", alias="executionMode", max_length=20)
    worker_name: str = Field(default="", alias="workerName", max_length=200)
    worker_role: str = Field(default="", alias="workerRole", max_length=500)
    workspace_root: str | None = Field(default=None, alias="workspaceRoot", max_length=4096)
    bootstrap_profile: str | None = Field(default=None, alias="bootstrapProfile", max_length=200)
    bootstrap_bundle: dict[str, object] | None = Field(default=None, alias="bootstrapBundle")
    file_upload_ids: list[str] = Field(default_factory=list, alias="fileUploadIds")
    origin_ref: str | None = Field(
        default=None,
        alias="originRef",
        min_length=8,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$",
    )
    origin_surface: Literal["web", "telegram", "voice", "workbench", "scheduler"] = Field(
        default="web",
        alias="originSurface",
    )
    resource_class: WorkerResourceClass = Field(
        default="standard",
        alias="resourceClass",
    )

class CallbackAssociationVerifyRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    origin_ref: str = Field(alias="originRef", min_length=8, max_length=192)
    work_ref: str = Field(alias="workRef", min_length=8, max_length=192)
    worker_id: str = Field(alias="workerId", min_length=8, max_length=192)
    run_id: str = Field(alias="runId", min_length=8, max_length=192)

class TerminalCallbackRecoveryRequest(CallbackAssociationVerifyRequest):
    callback_id: str = Field(alias="callbackId", pattern=r"^cb_terminal_[a-f0-9]{64}$")
    result_revision: int = Field(alias="resultRevision", ge=1, strict=True)
    result_digest: str = Field(alias="resultDigest", pattern=r"^sha256:[a-f0-9]{64}$")


class LifecycleCallbackRecoveryRequest(CallbackAssociationVerifyRequest):
    callback_ref: str = Field(alias="callbackRef", pattern=r"^callback_sha256:[a-f0-9]{64}$")
    payload_sha256: str = Field(alias="payloadSha256", pattern=r"^sha256:[a-f0-9]{64}$")
    authority_sha256: str = Field(alias="authoritySha256", pattern=r"^sha256:[a-f0-9]{64}$")


class CapabilityReauthorizationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: Literal[1]
    authorization_ref: str = Field(
        alias="authorizationRef",
        min_length=8,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$",
    )
    max_expires_at: str = Field(alias="maxExpiresAt", min_length=20, max_length=64)
    scope_fingerprint: str = Field(
        alias="scopeFingerprint", min_length=8, max_length=256
    )

class ContinuationOutputContractRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    mode: Literal["inherit", "replace"]
    required: list[str] = Field(default_factory=list, max_length=32)
    forbidden: list[str] = Field(default_factory=list, max_length=32)
    formats: list[
        Literal["md", "pdf", "docx", "xlsx", "csv", "pptx", "json", "txt"]
    ] = Field(default_factory=list, max_length=16)
    forbidden_formats: list[
        Literal["md", "pdf", "docx", "xlsx", "csv", "pptx", "json", "txt"]
    ] = Field(default_factory=list, alias="forbiddenFormats", max_length=16)

    @model_validator(mode="after")
    def validate_declaration(self) -> "ContinuationOutputContractRequest":
        declarations = [*self.required, *self.forbidden]
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 1000
            or any(ord(character) < 32 and character not in "\n\t" for character in value)
            for value in declarations
        ):
            raise ValueError("Continuation output declarations are invalid")
        if self.mode == "inherit" and (
            declarations or self.formats or self.forbidden_formats
        ):
            raise ValueError("An inherited output contract cannot add declarations")
        return self

class ActiveWorkSourceContextRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: Literal[1]
    origin_ref: str = Field(
        alias="originRef",
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    )
    source_event_id: str = Field(
        alias="sourceEventId",
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    )
    source_revision: int = Field(alias="sourceRevision", ge=0)
    surface: Literal[
        "web", "chat", "desktop", "telegram", "voice", "workbench", "scheduler"
    ]
    output_contract: ContinuationOutputContractRequest = Field(alias="outputContract")

class NativeInputResponseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: Literal[1]
    request_id: str = Field(alias="requestId", min_length=1, max_length=512)
    request_fingerprint: str = Field(alias="requestFingerprint", pattern=r"^[a-f0-9]{64}$")
    action: Literal["accept", "decline", "cancel"]
    content: dict[str, object] | None = None


class ActiveWorkActionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    action: Literal[
        "queue",
        "message",
        "steer",
        "pause",
        "resume",
        "stop",
        "retry",
        "dismiss",
    ]
    native_input: NativeInputResponseRequest | None = Field(default=None, alias="nativeInput")
    instruction: str | None = Field(default=None, max_length=100000)
    capability_reauthorization: CapabilityReauthorizationRequest | None = Field(
        default=None, alias="capabilityReauthorization"
    )
    source_context: ActiveWorkSourceContextRequest | None = Field(
        default=None, alias="sourceContext"
    )
    idempotency_key: str = Field(
        alias="idempotencyKey",
        min_length=8,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$",
    )

    @model_validator(mode="after")
    def validate_source_context_action(self) -> "ActiveWorkActionRequest":
        if self.native_input is not None and (
            self.action != "resume" or self.capability_reauthorization is not None or self.source_context is not None
        ):
            raise ValueError("Native input is valid only for an explicit input response")
        if self.source_context is not None and self.action not in {
            "queue", "message", "steer", "retry"
        }:
            raise ValueError("Source context is valid only for a run-producing action")
        return self

class RunActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    capabilityId: str = Field(min_length=5, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$")
    action: Literal["retry", "cancel"]
    projectId: str = Field(min_length=1, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
    workerId: str = Field(min_length=1, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
    runId: str = Field(min_length=1, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
    idempotencyKey: str = Field(min_length=8, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$")


class RunActionCorrelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projectId: str
    workerId: str
    runId: str


class RunActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    status: Literal["queued", "pending", "accepted"]
    action: Literal["retry", "cancel"]
    projectId: str
    workerId: str
    sourceRunId: str
    newRun: RunActionCorrelation | None
    confirmationPending: bool
    idempotentReplay: bool


class ScheduleRunRequest(BaseModel):
    instruction: str = Field(min_length=1)
    run_at: str | None = None
    schedule_text: str | None = None
    delay_seconds: int | None = Field(default=None, ge=0)
    bootstrap_bundle: dict[str, object] | None = None

    file_upload_ids: list[str] = Field(default_factory=list)
    file_upload_revisions: list[str] = Field(default_factory=list)

class CreateRecurringScheduleRequest(BaseModel):
    instruction: str = Field(min_length=1)
    recurrence_type: RecurrenceType
    interval_seconds: int | None = None
    local_time: str = ""
    timezone_name: str = "UTC"
    dst_policy: RecurrenceDstPolicy = "next_valid_earliest"
    first_run_at: str | None = None
    cron_expression: str = ""
    rrule: str = ""
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool = True
    overlap_policy: RecurrenceOverlapPolicy = "skip"
    misfire_grace_seconds: int = Field(default=300, ge=0, le=604800)
    catch_up_policy: RecurrenceCatchUpPolicy = "skip"
    max_catch_up_occurrences: int = Field(default=1, ge=1, le=10)
    jitter_seconds: int = Field(default=0, ge=0, le=900)
    schedule_text: str = ""
    bootstrap_bundle: dict[str, object] | None = None


class UpdateRecurringScheduleRequest(BaseModel):
    instruction: str | None = Field(default=None, min_length=1)
    recurrence_type: RecurrenceType | None = None
    interval_seconds: int | None = None
    local_time: str | None = None
    timezone_name: str | None = None
    dst_policy: RecurrenceDstPolicy | None = None
    cron_expression: str | None = None
    rrule: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool | None = None
    overlap_policy: RecurrenceOverlapPolicy | None = None
    misfire_grace_seconds: int | None = Field(default=None, ge=0, le=604800)
    catch_up_policy: RecurrenceCatchUpPolicy | None = None
    max_catch_up_occurrences: int | None = Field(default=None, ge=1, le=10)
    jitter_seconds: int | None = Field(default=None, ge=0, le=900)
    schedule_text: str | None = None


class RunRecurringScheduleNowRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)


class SchedulingCortexWorkspaceRunRequest(BaseModel):
    occurrence_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")
    task_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,200}$")
    owner_id: str = Field(pattern=r"^[^\x00-\x1f\x7f]{1,512}$")
    project_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    worker_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    execution_mode: Literal["host", "docker"]
    instruction: str = Field(min_length=1, max_length=200_000)
    # Delegated scheduling authority is assertion-bound. Credential-bearing bootstrap
    # bundles are minted inside the runtime immediately before execution and never cross
    # or persist at the queue boundary.
    bootstrap_bundle: None = None


class SchedulePrincipalAuthorityRequest(BaseModel):
    enabled: bool


class UpdateWorkerMetadataRequest(BaseModel):
    favorite: bool | None = None
    name: str | None = None


class UserPreferencesResponse(BaseModel):
    tenant_id: str = "local"
    owner_id: str
    default_worker_profile: str = ""
    codex_reasoning_effort: str = ""
    claude_effort: str = ""
    openclaw_effort: str = ""
    grok_model: str = ""
    updated_at: str


class UpdateUserPreferencesRequest(BaseModel):
    default_worker_profile: str | None = None
    codex_reasoning_effort: str | None = None
    claude_effort: str | None = None
    openclaw_effort: str | None = None
    grok_model: str | None = None


class DesktopActionRequest(BaseModel):
    action: DesktopActionName
    url: str | None = None
    run_id: str | None = None


class LaunchFailureRequest(BaseModel):
    reason: str = Field(min_length=1)


class RunResponse(BaseModel):
    run_id: str
    worker_id: str
    project_id: str
    tenant_id: str = "local"
    instruction: str
    state: RunState
    queued_at: str
    first_queued_at: str
    queue_deadline_at: str
    queue_blocker_class: str = "admission_pending"
    queue_next_status_at: str | None = None
    queue_wait_episode: int = 1
    queue_wait_open: bool = True
    queue_wait_generation: int = 1
    queue_wait_started_at: str
    queue_wait_closed_at: str | None = None
    queue_wait_duration_seconds: int | None = None
    queue_transition_emitted: bool = False
    queue_status_sequence: int = 0
    queue_callback_state: Literal[
        "unknown", "pending", "enqueued", "unavailable"
    ] = "unknown"
    queue_terminal_callback_id: str = ""
    claimed_at: str | None = None
    admitted_at: str | None = None
    started_at: str | None = None
    runtime_invoked_at: str | None = None
    active_attempt_id: str = ""
    ended_at: str | None = None
    output_text: str = ""
    error_text: str = ""
    failure_class: str = ""
    failure_retryable: bool = False
    failure_user_message: str = ""
    failure_recommended_recovery: str = ""
    failure_diagnostic_summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_tokens: int = 0
    effort: str = Field(
        default="",
        description="Normalized per-assignment effort accepted by the runtime.",
    )

    @model_validator(mode="before")
    @classmethod
    def project_internal_state_to_legacy_contract(cls, data):
        if not isinstance(data, dict):
            return data
        public_state = {
            "claimed": "queued",
            "admitted": "queued",
            "settling": "running",
            "needs_input": "paused",
        }.get(str(data.get("state") or ""))
        if not public_state:
            return data
        projected = dict(data)
        projected["state"] = public_state
        return projected

    @model_validator(mode="after")
    def calculate_total_tokens(self):
        self.total_tokens = sum(
            max(0, int(value))
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_input_tokens,
                self.cache_creation_input_tokens,
            )
        )
        return self


    first_queued_at: str = ""

    queue_deadline_at: str = ""

    queue_blocker_class: str = ""

    queue_next_status_at: str | None = None

    queue_wait_episode: int = 0

    queue_wait_open: bool = False

    queue_wait_generation: int = 0

    queue_wait_started_at: str = ""

    queue_wait_closed_at: str | None = None

    queue_wait_duration_seconds: int | None = None

    queue_transition_emitted: bool = False

    queue_status_sequence: int = 0

    queue_callback_state: Literal[
        "unknown", "pending", "enqueued", "unavailable"
    ] = "unknown"

    queue_terminal_callback_id: str = ""

    claimed_at: str | None = None

    admitted_at: str | None = None

    runtime_invoked_at: str | None = None

    active_attempt_id: str = ""

    native_session_id: str = ""

    native_capabilities_json: str = "{}"

    native_child_summary_json: str = "{}"

    capacity_class: str = ""

    capacity_available_json: str = "{}"

    capacity_required_json: str = "{}"

    capacity_shortage_json: str = "{}"

    capacity_reservation_json: str = "{}"

    capacity_next_retry_at: str | None = None

    provider_route_profile: str = ""

    provider_route_runtime: str = ""

    provider_route_model: str = ""

    provider_route_decision: str = ""

    provider_route_from_profile: str = ""

    provider_route_from_runtime: str = ""

    provider_route_from_model: str = ""

    provider_route_failure_class: str = ""

    provider_route_cooldown_until: str | None = None

    continuation_context_json: str = "{}"


class ScheduleResponse(BaseModel):
    schedule_id: str
    worker_id: str
    project_id: str
    tenant_id: str = "local"
    owner_id: str
    instruction: str
    schedule_text: str = ""
    run_at: str
    state: ScheduleState
    queued_run_id: str | None = None
    last_error: str = ""
    created_at: str
    updated_at: str

    @model_validator(mode="before")
    @classmethod
    def project_internal_state_to_legacy_contract(cls, data):
        if not isinstance(data, dict) or str(data.get("state") or "") != "needs_input":
            return data
        projected = dict(data)
        projected["state"] = "failed"
        return projected


class RecurringScheduleDefinitionResponse(BaseModel):
    definition_id: str
    project_id: str
    worker_id: str
    workspace_name: str = ""
    tenant_id: str = "local"
    owner_id: str
    scheduler_owner: str
    schedule_owner: str = ""
    owner_action: str = ""
    instruction: str
    schedule_text: str = ""
    recurrence_type: RecurrenceType
    interval_seconds: int | None = None
    local_time: str = ""
    timezone_name: str
    dst_policy: str
    cron_expression: str = ""
    rrule: str = ""
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool = True
    overlap_policy: str = "skip"
    misfire_grace_seconds: int = 300
    catch_up_policy: str = "coalesce"
    max_catch_up_occurrences: int = 1
    jitter_seconds: int = 0
    next_run_at: str
    next_occurrence_at: str = ""
    last_occurrence_at: str | None = None
    last_outcome: str = ""
    last_error: str = ""
    last_delivery_outcome: str | None = None
    last_delivery_reason: str | None = None
    last_delivery_at: str | None = None
    retired_at: str | None = None
    active: bool
    created_at: str
    updated_at: str

    @model_validator(mode="before")
    @classmethod
    def canonicalize_scheduler_owner(cls, value):
        if isinstance(value, dict) and value.get("scheduler_owner"):
            value = dict(value)
            owner = canonical_recurrence_owner(value["scheduler_owner"])
            value["scheduler_owner"] = owner
            value["schedule_owner"] = owner
            value["owner_action"] = (
                "dispatch_here" if owner == "glasshive_native" else "dispatch_via_viventium_cortex"
            )
            value["enabled"] = bool(value.get("enabled", value.get("active", True)))
            value["next_occurrence_at"] = str(value.get("next_run_at") or "")
        return value


class RecurringScheduleOccurrenceResponse(BaseModel):
    occurrence_id: str
    definition_id: str
    tenant_id: str = "local"
    owner_id: str
    scheduled_for: str
    detected_at: str
    scheduled_run_id: str
    idempotency_key: str = ""
    claimant: str = ""
    claimed_at: str | None = None
    claim_expires_at: str | None = None
    attempt_count: int = 0
    outcome: str = "pending"
    terminal_at: str | None = None
    created_at: str
    state: RecurringScheduleOccurrenceState
    queued_run_id: str | None = None
    last_error: str = ""


class EventResponse(BaseModel):
    event_id: str
    project_id: str
    worker_id: str
    tenant_id: str = "local"
    run_id: str | None = None
    event_type: str
    message: str
    payload_json: str = "{}"
    created_at: str


    payload_json: str = "{}"


class TakeoverInfo(BaseModel):
    supported: bool
    url: str | None = None
    mode: str | None = None
    notes: str | None = None


class DesktopActionResponse(BaseModel):
    action: str
    status: str
    mode: str
    url: str | None = None
    view_url: str | None = None
    notes: str | None = None


class MetricsSummary(BaseModel):
    projects: int
    workers: int
    runs: int
    queued_runs: int
    active_runs: int
    events: int
    callback_pending: int = 0
    callback_delivering: int = 0
    callback_dead_lettered: int = 0
    callback_max_attempts: int = 0
    callback_oldest_pending_age_seconds: int = 0


class NativeControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    action: Literal["interject", "cancel", "permission"]
    text: str | None = Field(default=None, max_length=65536)
    request_id: str | None = Field(default=None, max_length=128)
    option_id: str | None = Field(default=None, max_length=128)
    response: dict | None = None
