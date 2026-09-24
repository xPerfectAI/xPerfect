"""Typed, owner-scoped Allowed AI policy records and option catalogues.

The policy is intentionally separate from the clean-room execution policy and
from the workspace peer-access revision.  This module owns only the durable
settings boundary; native admission enforcement remains in the owning start
paths.
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .conversation_provider import GLASSHIVE_MODELS
from .native_model_selection import ModelConfigurationRequired, selected_grok_model
from .profile_registry import PROFILES
from .service import allowed_worker_profiles, host_workers_enabled
from .store import AllowedAiPolicyRevisionConflict, AllowedAiStartFenceConflict


PolicyMode = Literal["inherit", "all_authorized", "selected"]
SelectionMode = Literal["all", "selected"]


class AllowedAiSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: SelectionMode
    ids: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_selection(self) -> "AllowedAiSelection":
        if self.mode == "all" and self.ids:
            raise ValueError("all selection must not contain ids")
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("selection ids must be unique")
        for value in self.ids:
            if not value.strip() or len(value) > 512 or any(ord(char) < 32 for char in value):
                raise ValueError("selection ids must be bounded printable strings")
        return self


class AllowedAiHarnessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile: str = Field(min_length=1, max_length=120)
    models: AllowedAiSelection
    connections: AllowedAiSelection

    @model_validator(mode="after")
    def validate_profile(self) -> "AllowedAiHarnessPolicy":
        if any(ord(char) < 32 for char in self.profile):
            raise ValueError("profile must be printable")
        return self


class AllowedAiPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    mode: PolicyMode
    harnesses: list[AllowedAiHarnessPolicy] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_policy(self) -> "AllowedAiPolicy":
        if self.mode == "inherit" and self.harnesses:
            raise ValueError("inherit policy must not contain harnesses")
        if self.mode != "selected" and self.harnesses:
            raise ValueError("default policy must not contain harnesses")
        profiles = [item.profile for item in self.harnesses]
        if len(set(profiles)) != len(profiles):
            raise ValueError("harness profiles must be unique")
        return self


class AllowedAiUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    policy: AllowedAiPolicy


class AllowedAiSelectionUnavailable(ValueError):
    """A selected model or connection is not in the owner's current catalog."""

    def __init__(self, message: str = "Selected Allowed AI option is unavailable") -> None:
        super().__init__(message)


class AllowedAiAdmissionError(RuntimeError):
    """A new run does not fit the current owner policy ceiling.

    This is deliberately separate from provider readiness.  A denied route is
    a policy decision and must be surfaced as such; it must not be turned into
    a preferred/ambient fallback by the runtime.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        scope_id: str = "",
        policy_revision: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "allowed_ai_denied")
        self.scope_id = str(scope_id or "")
        self.policy_revision = int(policy_revision or 0)


class AllowedAiOption(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=512)
    label: str = Field(min_length=1, max_length=200)
    status: Literal["available", "busy", "unavailable", "denied", "unknown"]
    reason: str | None = Field(default=None, max_length=240)
    kind: Literal["subscription", "api_key", "configured_route", "other"] | None = None


class AllowedAiHarnessOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=200)
    models: list[AllowedAiOption] = Field(default_factory=list, max_length=256)
    connections: list[AllowedAiOption] = Field(default_factory=list, max_length=256)


class AllowedAiOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope_id: str = Field(min_length=1, max_length=512)
    harnesses: list[AllowedAiHarnessOptions] = Field(default_factory=list, max_length=64)


def default_allowed_ai_policy(scope: str) -> AllowedAiPolicy:
    if scope == "project":
        return AllowedAiPolicy(version=1, mode="all_authorized", harnesses=[])
    if scope == "workspace":
        return AllowedAiPolicy(version=1, mode="inherit", harnesses=[])
    raise ValueError("Unknown Allowed AI policy scope")


def _selection_intersection(
    left: AllowedAiSelection, right: AllowedAiSelection
) -> AllowedAiSelection:
    if left.mode == "all":
        return right.model_copy(deep=True)
    if right.mode == "all":
        return left.model_copy(deep=True)
    return AllowedAiSelection(
        mode="selected",
        ids=[value for value in left.ids if value in set(right.ids)],
    )


def effective_allowed_ai_policy(
    project: AllowedAiPolicy, workspace: AllowedAiPolicy | None
) -> AllowedAiPolicy:
    """Return the workspace ceiling without widening project authority."""

    if workspace is None or workspace.mode == "inherit":
        return project.model_copy(deep=True)
    if project.mode == "all_authorized":
        return workspace.model_copy(deep=True)
    if workspace.mode == "all_authorized":
        return project.model_copy(deep=True)
    project_harnesses = {item.profile: item for item in project.harnesses}
    narrowed: list[AllowedAiHarnessPolicy] = []
    for item in workspace.harnesses:
        parent = project_harnesses.get(item.profile)
        if parent is None:
            continue
        narrowed.append(
            AllowedAiHarnessPolicy(
                profile=item.profile,
                models=_selection_intersection(parent.models, item.models),
                connections=_selection_intersection(parent.connections, item.connections),
            )
        )
    return AllowedAiPolicy(version=1, mode="selected", harnesses=narrowed)


def intersect_allowed_ai_policies(
    left: AllowedAiPolicy, right: AllowedAiPolicy
) -> AllowedAiPolicy:
    """Intersect two already-resolved ceilings without widening either scope."""

    if left.mode == "all_authorized":
        return right.model_copy(deep=True)
    if right.mode == "all_authorized":
        return left.model_copy(deep=True)
    right_by_profile = {item.profile: item for item in right.harnesses}
    narrowed: list[AllowedAiHarnessPolicy] = []
    for item in left.harnesses:
        other = right_by_profile.get(item.profile)
        if other is None:
            continue
        narrowed.append(
            AllowedAiHarnessPolicy(
                profile=item.profile,
                models=_selection_intersection(item.models, other.models),
                connections=_selection_intersection(item.connections, other.connections),
            )
        )
    return AllowedAiPolicy(version=1, mode="selected", harnesses=narrowed)


def _status(value: object) -> tuple[str, str | None]:
    state = str(value or "").strip().lower()
    if state in {"ready", "available", "connected", "verified"}:
        return "available", None
    if state in {"connecting", "busy", "queued", "pending"}:
        return "busy", "Connection setup is still in progress."
    if state in {"disconnected", "action_required", "unavailable", "error", "failed"}:
        return "unavailable", "Reconnect this connection before starting new work."
    return "unknown", "Connection readiness is not recorded."


def _connection_kind(auth_method: object) -> str:
    value = str(auth_method or "").strip().lower()
    if value == "subscription":
        return "subscription"
    if value == "api_key":
        return "api_key"
    if value in {"enterprise_route", "configured_route"}:
        return "configured_route"
    return "other"


class AllowedAiPolicyService:
    """Store-backed settings and descriptor-only option catalogue."""

    def __init__(self, store, control_plane, runtime) -> None:
        self.store = store
        self.control_plane = control_plane
        self.runtime = runtime

    def _model_id(self, profile: str, model: str, *, tenant_id: str = "local", owner_id: str = "") -> str:
        """Map a persisted native model to the catalog's exact model ID."""

        clean_profile = str(profile or "").strip()
        clean_model = str(model or "").strip()
        if not clean_profile or not clean_model:
            return ""
        # Callers sometimes already carry the public catalog ID.  Preserve it
        # only when it is an actual catalog entry for this harness.
        direct = GLASSHIVE_MODELS.get(clean_model)
        if direct is not None and direct.harness_profile == clean_profile:
            return direct.id
        for descriptor in GLASSHIVE_MODELS.values():
            if (
                descriptor.harness_profile == clean_profile
                and descriptor.native_model == clean_model
            ):
                return descriptor.id
        # ACP harnesses have a deployment-selected exact model rather than a
        # Conversation API registry row. Only recognize the configured route.
        native = next((item for item in PROFILES if item.profile == clean_profile), None)
        if native is not None and native.native_transport == "acp":
            try:
                configured, _ = selected_grok_model(self.store, tenant_id, owner_id)
            except ModelConfigurationRequired:
                configured = ""
            if configured and clean_model in {configured, f"{clean_profile}:{configured}"}:
                return f"{clean_profile}:{configured}"
        return ""

    @staticmethod
    def _worker_connection_id(worker: dict[str, Any]) -> str:
        """Return only a server-recognized connection/account reference."""

        raw_bundle = worker.get("bootstrap_bundle")
        if not isinstance(raw_bundle, dict):
            raw_bundle = worker.get("bootstrap_bundle_json")
            if isinstance(raw_bundle, str):
                try:
                    raw_bundle = json.loads(raw_bundle)
                except (TypeError, ValueError, json.JSONDecodeError):
                    raw_bundle = {}
        bundle = raw_bundle if isinstance(raw_bundle, dict) else {}
        provider = bundle.get("provider_account")
        if isinstance(provider, dict):
            account_id = str(provider.get("account_id") or "").strip()
            if account_id:
                return account_id
        # Configured route IDs are server-owned references.  They are accepted
        # only as opaque IDs here; ownership/readiness still belongs to the
        # option catalogue and the native binder.
        for key in ("connection_id", "configured_route_id", "route_id"):
            value = str(bundle.get(key) or "").strip()
            if value:
                return value
        return ""

    def _origin_scope_for_worker(self, worker: dict[str, Any]) -> dict[str, str | int]:
        """Resolve the immutable coordinator origin copied onto a child worker."""

        raw = worker.get("origin_scope")
        if not isinstance(raw, dict):
            encoded = worker.get("origin_scope_json")
            if isinstance(encoded, str) and encoded.strip():
                try:
                    parsed = json.loads(encoded)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise AllowedAiAdmissionError(
                        "scope_missing",
                        "The originating Allowed AI scope is unavailable.",
                    ) from exc
                raw = parsed if isinstance(parsed, dict) else None
        if raw in (None, {}):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise AllowedAiAdmissionError(
                "scope_missing",
                "The originating Allowed AI scope is unavailable.",
            )
        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        origin_tenant = str(raw.get("tenant_id") or "")
        origin_owner = str(raw.get("owner_id") or "")
        project_id = str(raw.get("project_id") or "").strip()
        workspace_id = str(raw.get("workspace_id") or "").strip()
        if (
            not project_id
            or origin_tenant != tenant_id
            or origin_owner != owner_id
            or (workspace_id and not str(workspace_id).strip())
        ):
            raise AllowedAiAdmissionError(
                "scope_missing",
                "The originating Allowed AI scope is unavailable.",
            )
        project = self.store.get_project(
            project_id, tenant_id=tenant_id, owner_id=owner_id
        )
        if project is None:
            raise AllowedAiAdmissionError(
                "scope_missing",
                "The originating Allowed AI project is unavailable.",
            )
        if workspace_id:
            workspace = self.store.get_execution_workspace(
                workspace_id, tenant_id, owner_id
            )
            if workspace is None or str(workspace.get("project_id") or "") != project_id:
                raise AllowedAiAdmissionError(
                    "scope_missing",
                    "The originating Allowed AI workspace is unavailable.",
                )
        source_revision = raw.get("source_revision", 0)
        if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 0:
            raise AllowedAiAdmissionError(
                "scope_missing",
                "The originating Allowed AI scope is unavailable.",
            )
        if str(raw.get("execution_mode") or "host") not in {"host", "docker"}:
            raise AllowedAiAdmissionError(
                "scope_missing",
                "The originating Allowed AI scope is unavailable.",
            )
        return {
            "version": 1,
            "tenant_id": tenant_id,
            "owner_id": owner_id,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "connection_id": str(raw.get("connection_id") or ""),
            "execution_mode": str(raw.get("execution_mode") or "host"),
            "ref": str(raw.get("ref") or ""),
            "source_event_id": str(raw.get("source_event_id") or ""),
            "source_revision": source_revision,
            "surface": str(raw.get("surface") or "internal"),
        }

    def _policy_scope_for_worker(
        self, worker: dict[str, Any]
    ) -> tuple[str, str, str, str, AllowedAiPolicy, int, int, dict[str, str | int], int, int]:
        """Read the project/workspace ceiling through the worker owner scope."""

        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        project_id = str(worker.get("project_id") or "").strip()
        if not owner_id or not project_id:
            raise AllowedAiAdmissionError(
                "scope_missing", "Allowed AI scope is unavailable for this workspace."
            )
        project = self.store.get_project(
            project_id, tenant_id=tenant_id, owner_id=owner_id
        )
        if project is None:
            raise AllowedAiAdmissionError(
                "scope_missing", "Allowed AI scope is unavailable for this workspace."
            )
        project_snapshot = self.get(
            scope="project",
            scope_id=project_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        project_policy = AllowedAiPolicy.model_validate(project_snapshot["policy"])
        project_revision = int(project_snapshot.get("revision") or 0)
        workspace_id = str(worker.get("workspace_id") or "").strip()
        workspace_policy = None
        workspace_revision = 0
        if workspace_id:
            workspace = self.store.get_execution_workspace(
                workspace_id, tenant_id, owner_id
            )
            if workspace is None or str(workspace.get("project_id") or "") != project_id:
                raise AllowedAiAdmissionError(
                    "scope_missing", "Allowed AI workspace scope is unavailable."
                )
            workspace_snapshot = self.get(
                scope="workspace",
                scope_id=workspace_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            workspace_policy = AllowedAiPolicy.model_validate(workspace_snapshot["policy"])
            workspace_revision = int(workspace_snapshot.get("revision") or 0)

        destination_policy = effective_allowed_ai_policy(project_policy, workspace_policy)
        origin = self._origin_scope_for_worker(worker)
        origin_project_revision = 0
        origin_workspace_revision = 0
        if origin:
            origin_project_id = str(origin["project_id"])
            origin_project_snapshot = self.get(
                scope="project",
                scope_id=origin_project_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            origin_policy = AllowedAiPolicy.model_validate(origin_project_snapshot["policy"])
            origin_project_revision = int(origin_project_snapshot.get("revision") or 0)
            origin_workspace_id = str(origin.get("workspace_id") or "")
            if origin_workspace_id:
                origin_workspace_snapshot = self.get(
                    scope="workspace",
                    scope_id=origin_workspace_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                origin_workspace_policy = AllowedAiPolicy.model_validate(
                    origin_workspace_snapshot["policy"]
                )
                origin_workspace_revision = int(
                    origin_workspace_snapshot.get("revision") or 0
                )
                origin_policy = effective_allowed_ai_policy(
                    origin_policy, origin_workspace_policy
                )
            destination_policy = intersect_allowed_ai_policies(
                origin_policy, destination_policy
            )
        return (
            project_id,
            workspace_id,
            tenant_id,
            owner_id,
            destination_policy,
            project_revision,
            workspace_revision,
            origin,
            origin_project_revision,
            origin_workspace_revision,
        )

    def admission_snapshot(self, worker: dict[str, Any]) -> dict[str, Any]:
        """Validate one worker's exact route against the current policy.

        The check is intentionally deterministic and route based.  It does not
        probe provider CLIs or choose a first eligible model.  The native
        binder remains authoritative for account readiness and the final
        connection receipt.
        """

        (
            project_id,
            workspace_id,
            tenant_id,
            owner_id,
            policy,
            project_revision,
            workspace_revision,
            origin,
            origin_project_revision,
            origin_workspace_revision,
        ) = self._policy_scope_for_worker(worker)
        profile = str(worker.get("profile") or "").strip()
        model = str(worker.get("model") or "").strip()
        raw_bundle = worker.get("bootstrap_bundle_json")
        if isinstance(raw_bundle, str):
            try:
                parsed_bundle = json.loads(raw_bundle)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_bundle = {}
            if isinstance(parsed_bundle, dict):
                model = str(parsed_bundle.get("provider_model") or model).strip()
        model_id = self._model_id(profile, model, tenant_id=tenant_id, owner_id=owner_id)
        connection_id = self._worker_connection_id(worker)
        if policy.mode == "all_authorized":
            return {
                "scope_id": workspace_id or project_id,
                "project_id": project_id,
                "workspace_id": workspace_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "profile": profile,
                "model_id": model_id,
                "connection_id": connection_id,
                "project_revision": project_revision,
                "workspace_revision": workspace_revision,
                "origin_project_id": str(origin.get("project_id") or ""),
                "origin_workspace_id": str(origin.get("workspace_id") or ""),
                "origin_project_revision": origin_project_revision,
                "origin_workspace_revision": origin_workspace_revision,
                "origin_scope": dict(origin),
                "policy": policy.model_dump(mode="json"),
            }

        selected = next(
            (item for item in policy.harnesses if item.profile == profile), None
        )
        scope_id = workspace_id or project_id
        revision = workspace_revision or project_revision
        if selected is None:
            raise AllowedAiAdmissionError(
                "allowed_ai_denied",
                "This workspace's Allowed AI policy does not permit the selected harness.",
                scope_id=scope_id,
                policy_revision=revision,
            )
        if selected.models.mode == "selected" and (
            not model_id or model_id not in selected.models.ids
        ):
            raise AllowedAiAdmissionError(
                "allowed_ai_model_denied",
                "This workspace's Allowed AI policy does not permit the selected model.",
                scope_id=scope_id,
                policy_revision=revision,
            )
        if selected.connections.mode == "selected" and (
            not connection_id or connection_id not in selected.connections.ids
        ):
            raise AllowedAiAdmissionError(
                "allowed_ai_connection_denied",
                "This workspace's Allowed AI policy does not permit the selected connection.",
                scope_id=scope_id,
                policy_revision=revision,
            )
        return {
            "scope_id": scope_id,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "tenant_id": tenant_id,
            "owner_id": owner_id,
            "profile": profile,
            "model_id": model_id,
            "connection_id": connection_id,
            "project_revision": project_revision,
            "workspace_revision": workspace_revision,
            "origin_project_id": str(origin.get("project_id") or ""),
            "origin_workspace_id": str(origin.get("workspace_id") or ""),
            "origin_project_revision": origin_project_revision,
            "origin_workspace_revision": origin_workspace_revision,
            "origin_scope": dict(origin),
            "policy": policy.model_dump(mode="json"),
        }

    def native_start_fence(
        self,
        worker: dict[str, Any],
        *,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Revalidate the bound native route and claim the shared start fence."""

        if not isinstance(receipt, dict):
            raise AllowedAiAdmissionError(
                "allowed_ai_receipt_invalid",
                "The native provider did not return a valid connection receipt.",
            )
        if str(receipt.get("protocol") or "") != "glasshive.native_connection_receipt.v1":
            raise AllowedAiAdmissionError(
                "allowed_ai_receipt_invalid",
                "The native provider connection receipt protocol is invalid.",
            )
        expected_worker_id = str(worker.get("worker_id") or "").strip()
        receipt_worker_id = str(receipt.get("worker_id") or "").strip()
        if expected_worker_id and receipt_worker_id != expected_worker_id:
            raise AllowedAiAdmissionError(
                "allowed_ai_route_mismatch",
                "The native provider connection belongs to another workspace.",
            )
        expected_run_id = str(worker.get("_active_run_id") or worker.get("run_id") or "").strip()
        receipt_run_id = str(receipt.get("run_id") or "").strip()
        if not expected_run_id or receipt_run_id != expected_run_id:
            raise AllowedAiAdmissionError(
                "allowed_ai_receipt_invalid",
                "The native provider connection is not bound to this run.",
            )
        profile = str(worker.get("profile") or "").strip()
        receipt_profile = str(receipt.get("profile") or profile).strip()
        if receipt_profile != profile:
            raise AllowedAiAdmissionError(
                "allowed_ai_route_mismatch",
                "The native provider connection does not match the selected harness.",
            )
        native_model = str(
            receipt.get("native_model")
            or receipt.get("model")
            or worker.get("model")
            or ""
        ).strip()
        actual_model_id = str(receipt.get("model_id") or "").strip() or self._model_id(
            profile, native_model, tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        actual_connection_id = str(receipt.get("connection_id") or "").strip()
        expected = worker.get("_allowed_ai_admission")
        if not isinstance(expected, dict):
            expected = worker.get("allowed_ai_admission")
        if isinstance(expected, dict):
            expected_model_id = str(expected.get("model_id") or "").strip()
            expected_connection_id = str(expected.get("connection_id") or "").strip()
            if expected_model_id and actual_model_id != expected_model_id:
                raise AllowedAiAdmissionError(
                    "allowed_ai_route_mismatch",
                    "The native provider model changed after this run was admitted.",
                )
            if expected_connection_id and actual_connection_id != expected_connection_id:
                raise AllowedAiAdmissionError(
                    "allowed_ai_route_mismatch",
                    "The native provider connection changed after this run was admitted.",
                )
        # Rebuild the policy candidate from the binder-produced route. This
        # prevents a selected account in the request bundle from authorizing a
        # different account or deployment fallback at the native boundary.
        route_worker = dict(worker)
        route_worker["model"] = native_model
        raw_bundle = route_worker.get("bootstrap_bundle_json")
        bundle: dict[str, Any]
        if isinstance(raw_bundle, str):
            try:
                parsed = json.loads(raw_bundle)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = {}
            bundle = dict(parsed) if isinstance(parsed, dict) else {}
        elif isinstance(raw_bundle, dict):
            bundle = dict(raw_bundle)
        else:
            bundle = {}
        if native_model:
            bundle["provider_model"] = native_model
        if actual_connection_id:
            bundle["connection_id"] = actual_connection_id
            provider_account = bundle.get("provider_account")
            if isinstance(provider_account, dict):
                bundle["provider_account"] = {
                    **provider_account,
                    "account_id": actual_connection_id,
                }
        else:
            bundle.pop("connection_id", None)
            bundle.pop("configured_route_id", None)
            bundle.pop("route_id", None)
            if isinstance(bundle.get("provider_account"), dict):
                bundle["provider_account"] = {"policy": "legacy"}
        route_worker["bootstrap_bundle_json"] = json.dumps(
            bundle, sort_keys=True, separators=(",", ":")
        )
        current = self.admission_snapshot(route_worker)
        run_id = expected_run_id
        attempt_id = str(worker.get("_run_attempt_id") or worker.get("attempt_id") or "").strip()
        if not run_id:
            raise AllowedAiAdmissionError(
                "allowed_ai_receipt_invalid",
                "The native provider start is missing its durable run identity.",
            )
        try:
            self.store.record_allowed_ai_start_fence(
                run_id=run_id,
                attempt_id=attempt_id,
                worker_id=str(worker.get("worker_id") or ""),
                project_id=str(current.get("project_id") or ""),
                workspace_id=str(current.get("workspace_id") or ""),
                tenant_id=str(current.get("tenant_id") or "local"),
                owner_id=str(current.get("owner_id") or ""),
                admission=current,
                receipt={
                    **receipt,
                    "profile": profile,
                    "model_id": actual_model_id,
                    "native_model": native_model,
                    "connection_id": actual_connection_id,
                },
            )
        except AllowedAiStartFenceConflict as exc:
            raise AllowedAiAdmissionError(
                "allowed_ai_start_fenced",
                "Allowed AI settings changed before the native provider started; retry this run.",
                scope_id=str(current.get("scope_id") or ""),
                policy_revision=int(
                    current.get("workspace_revision") or current.get("project_revision") or 0
                ),
            ) from exc
        return current

    def get(self, *, scope: str, scope_id: str, tenant_id: str, owner_id: str) -> dict[str, Any]:
        row = self.store.get_allowed_ai_policy(
            scope, scope_id, tenant_id=tenant_id, owner_id=owner_id
        )
        if row is None:
            policy = default_allowed_ai_policy(scope)
            revision = 0
        else:
            try:
                policy = AllowedAiPolicy.model_validate(json.loads(row["policy_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("Stored Allowed AI policy is invalid") from exc
            revision = int(row["revision"])
        return {"scope_id": scope_id, "revision": revision, "policy": policy.model_dump(mode="json")}

    def put(
        self,
        *,
        scope: str,
        scope_id: str,
        tenant_id: str,
        owner_id: str,
        expected_revision: int,
        policy: AllowedAiPolicy,
    ) -> dict[str, Any]:
        if scope == "project" and policy.mode == "inherit":
            raise ValueError("Project policy cannot inherit")
        self._validate_selected_options(
            scope=scope,
            scope_id=scope_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            policy=policy,
        )
        encoded = json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 128 * 1024:
            raise ValueError("Allowed AI policy is too large")
        try:
            revision = self.store.put_allowed_ai_policy(
                scope,
                scope_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                expected_revision=expected_revision,
                policy_json=encoded,
            )
        except AllowedAiPolicyRevisionConflict as exc:
            raise exc
        return {"scope_id": scope_id, "revision": revision, "policy": policy.model_dump(mode="json")}

    def _validate_selected_options(
        self,
        *,
        scope: str,
        scope_id: str,
        tenant_id: str,
        owner_id: str,
        policy: AllowedAiPolicy,
    ) -> None:
        if policy.mode != "selected":
            return
        previous = self.get(
            scope=scope,
            scope_id=scope_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        previous_policy = AllowedAiPolicy.model_validate(previous["policy"])
        previous_by_profile = {item.profile: item for item in previous_policy.harnesses}
        catalog = self.options(
            scope_id=scope_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        catalog_by_profile = {item["profile"]: item for item in catalog["harnesses"]}
        for harness in policy.harnesses:
            available = catalog_by_profile.get(harness.profile)
            prior = previous_by_profile.get(harness.profile)
            if available is None and prior is None:
                raise AllowedAiSelectionUnavailable()
            for selection, field in (
                (harness.models, "models"),
                (harness.connections, "connections"),
            ):
                if selection.mode != "selected":
                    continue
                available_ids = {
                    str(option.get("id") or "")
                    for option in (available or {}).get(field, [])
                }
                prior_ids: set[str] = set()
                if prior is not None:
                    prior_ids = set(
                        prior.models.ids if field == "models" else prior.connections.ids
                    )
                if any(value not in available_ids and value not in prior_ids for value in selection.ids):
                    raise AllowedAiSelectionUnavailable()

    def options(self, *, scope_id: str, tenant_id: str, owner_id: str) -> dict[str, Any]:
        allowed = allowed_worker_profiles()
        account_rows = self.control_plane.list_provider_accounts(
            tenant_id=tenant_id, owner_id=owner_id
        )
        connection_rows = self.control_plane.list_connections(
            tenant_id=tenant_id, owner_id=owner_id
        )
        options: list[dict[str, Any]] = []
        for descriptor in PROFILES:
            if not descriptor.primary or (allowed and descriptor.profile not in allowed):
                continue
            models = [
                model for model in GLASSHIVE_MODELS.values()
                if model.harness_profile == descriptor.profile and model.native_model
            ]
            adapter_modes = [
                mode
                for mode in ("docker", "host")
                if mode != "host" or host_workers_enabled()
                if getattr(self.runtime, descriptor.runtime_for(mode), None) is not None
            ]
            model_options = [
                AllowedAiOption(
                    id=model.id,
                    label=model.display_name,
                    status="available" if adapter_modes else "unavailable",
                    reason=None if adapter_modes else "This harness adapter is unavailable in this runtime.",
                ).model_dump(mode="json", exclude_none=True)
                for model in models
            ]
            if not model_options and descriptor.native_transport == "acp":
                try:
                    configured, _ = selected_grok_model(self.store, tenant_id, owner_id)
                except ModelConfigurationRequired:
                    configured = ""
                if configured:
                    model_options.append(AllowedAiOption(
                        id=f"{descriptor.profile}:{configured}",
                        label=f"{descriptor.label} / {configured}",
                        status="available" if adapter_modes else "unavailable",
                        reason=None if adapter_modes else "This harness adapter is unavailable in this runtime.",
                    ).model_dump(mode="json", exclude_none=True))
            # A registry entry alone is not an executable exact-model option.
            if not model_options:
                continue
            supported = {
                value
                for value in {
                    "codex-cli": {"codex", "openai"},
                    "claude-code": {"claude", "anthropic"},
                    "grok-build": {"grok", "xai"},
                }.get(descriptor.profile, set())
            }
            connection_options: list[dict[str, Any]] = []
            for row in account_rows:
                if str(row.get("provider") or "").strip().lower() not in supported:
                    continue
                status, reason = _status(row.get("status"))
                connection_options.append(
                    AllowedAiOption(
                        id=str(row.get("account_id") or "").strip(),
                        label=(str(row.get("label") or "").strip() or "Provider account")[:200],
                        status=status,
                        reason=reason,
                        kind=_connection_kind(row.get("auth_method")),
                    ).model_dump(mode="json", exclude_none=True)
                )
            for row in connection_rows:
                adapter = str(row.get("adapter") or "").strip().lower()
                if adapter and adapter not in supported:
                    continue
                status, reason = _status(row.get("status"))
                connection_options.append(
                    AllowedAiOption(
                        id=str(row.get("connection_id") or "").strip(),
                        label=(str(row.get("label") or "").strip() or "Configured route")[:200],
                        status=status,
                        reason=reason,
                        kind="configured_route",
                    ).model_dump(mode="json", exclude_none=True)
                )
            options.append(
                AllowedAiHarnessOptions(
                    profile=descriptor.profile,
                    label=descriptor.label,
                    models=model_options,
                    connections=connection_options,
                ).model_dump(mode="json")
            )
        return AllowedAiOptions(scope_id=scope_id, harnesses=options).model_dump(
            mode="json", exclude_none=True
        )


def validate_scope_policy(policy: AllowedAiPolicy, scope: str) -> None:
    if scope == "project" and policy.mode == "inherit":
        raise ValueError("Project policy cannot inherit")
    if scope not in {"project", "workspace"}:
        raise ValueError("Unknown Allowed AI policy scope")
