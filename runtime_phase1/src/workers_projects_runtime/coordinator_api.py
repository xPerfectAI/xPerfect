"""Thin authenticated owner routes. Principal/configuration come from the host service."""
from __future__ import annotations

from collections.abc import Callable
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from .coordinator import (
    Control, CoordinatorConfig, CoordinatorConflict, CoordinatorScope,
    CoordinatorScopeError, Route,
    Dispatch, Goal, StrictModel,
)


class TurnInput(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=524288)
    goals: list[Goal] = Field(default_factory=list, max_length=1000)


class ConversationCreateInput(StrictModel):
    scope: CoordinatorScope | None = None
    account_id: str = Field(default="", max_length=512)


class GoalInput(StrictModel):
    turn_id: str
    goals: list[Goal] = Field(max_length=1000)


class RestoreResumeInput(StrictModel):
    expected_hold_set_hash: str = Field(min_length=64, max_length=64)
    approved_request_ids: list[str] = Field(default_factory=list, max_length=1000)
    approved_turn_ids: list[str] = Field(default_factory=list, max_length=1000)
    approved_goal_ids: list[str] = Field(default_factory=list, max_length=1000)


def install_coordinator_routes(app, coordinator, principal: Callable, default_config: Callable):
    """principal(request)->(tenant,owner); default_config(tenant,owner,profile)->validated config.

    The owning API must apply its existing owner authentication and CSRF controls.
    Defaults remain trusted owner settings. A caller may select only an owned
    origin scope; model, effort, routes, and bootstrap remain server-owned.
    """
    router = APIRouter(prefix="/v1/coordinator")

    def invoke(fn, *args):
        try:
            return fn(*args)
        except CoordinatorScopeError as exc:
            raise HTTPException(403, str(exc)) from exc
        except CoordinatorConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/conversations")
    def create(request: Request, payload: ConversationCreateInput | None = None):
        tenant, owner = principal(request)
        if payload is not None and payload.account_id:
            account = coordinator.service.control_plane_store.get_provider_account(
                account_id=payload.account_id, tenant_id=tenant, owner_id=owner,
            )
            profile = {
                "codex": "codex-cli", "openai": "codex-cli",
                "claude": "claude-code", "anthropic": "claude-code",
                "grok": "grok-build", "xai": "grok-build",
            }.get(str((account or {}).get("provider") or "").lower())
            if not account or account.get("status") != "ready" or not profile:
                raise HTTPException(409, "Choose a connected assistant before starting a conversation")
            native_model = coordinator.service._resolve_worker_model(profile, "docker", tenant_id=tenant, owner_id=owner)
            try:
                model = coordinator.provider._model(f"{profile}:{native_model}", tenant_id=tenant, owner_id=owner)
            except HTTPException as exc:
                raise HTTPException(409, "The selected assistant model is unavailable for conversations") from exc
            if model.harness_profile != profile or model.native_model != native_model:
                raise HTTPException(409, "The selected assistant model is unavailable for conversations")
            config = invoke(default_config, tenant, owner, profile)
            if config is None:
                raise HTTPException(409, {"code": "coordinator_not_configured", "message": "Connect an assistant in Connections to start a conversation."})
            selected = CoordinatorConfig.model_validate(config)
            # The chosen account answers in the conversation. Configured helper routes,
            # each with its own account, stay; without them one route uses the same account.
            from .coordinator_config import explicit_coordinator_config
            routes = (selected.routes if explicit_coordinator_config() is not None else
                      [Route(id="default", profile=profile, model=model.id,
                             effort=model.recommended_effort, execution_mode="docker",
                             connection_id=payload.account_id)])
            selected = selected.model_copy(update={
                "model": model.id,
                "effort": model.recommended_effort,
                "scope": selected.scope.model_copy(update={"connection_id": payload.account_id, "execution_mode": "docker"}),
                "routes": routes,
            })
        else:
            config = invoke(default_config, tenant, owner)
            if config is None:
                raise HTTPException(409, {"code": "coordinator_not_configured", "message": "Connect an assistant in Connections to start a conversation."})
            selected = CoordinatorConfig.model_validate(config)
        if payload is not None and payload.scope is not None:
            scope_update = payload.scope.model_dump(exclude_unset=True)
            if "connection_id" in scope_update and selected.scope.connection_id and (
                scope_update["connection_id"] != selected.scope.connection_id
            ):
                raise HTTPException(409, "The selected assistant account conflicts with this project")
            selected = selected.model_copy(update={
                "scope": selected.scope.model_copy(update=scope_update),
            })
        return invoke(coordinator.create, tenant, owner, selected)

    @router.get("/conversations/{conversation_id}")
    def status(conversation_id: str, request: Request):
        return invoke(coordinator.refresh, *principal(request), conversation_id)

    @router.post("/conversations/{conversation_id}/turns")
    def submit(conversation_id: str, payload: TurnInput, request: Request):
        identity = (*principal(request), conversation_id)
        invoke(coordinator.accept_turn, *identity, payload.idempotency_key, payload.message, payload.goals)
        return invoke(coordinator.start_turn, *identity, payload.idempotency_key)

    @router.post("/conversations/{conversation_id}/turns/{turn_id}/stop")
    def stop_turn(conversation_id: str, turn_id: str, request: Request):
        return invoke(coordinator.cancel_turn, *principal(request), conversation_id, turn_id)

    @router.post("/conversations/{conversation_id}/turns/{turn_id}/retry-result")
    def retry_result_turn(conversation_id: str, turn_id: str, request: Request):
        return invoke(coordinator.retry_result_turn, *principal(request), conversation_id, turn_id)

    @router.post("/conversations/{conversation_id}/restore-resume")
    def restore_resume(conversation_id: str, payload: RestoreResumeInput, request: Request):
        return invoke(
            coordinator.resume_restored,
            *principal(request),
            conversation_id,
            payload.expected_hold_set_hash,
            payload.approved_request_ids,
            payload.approved_turn_ids,
            payload.approved_goal_ids,
        )

    @router.post("/conversations/{conversation_id}/goals")
    def accept(conversation_id: str, payload: GoalInput, request: Request):
        return invoke(coordinator.accept_goals, *principal(request), conversation_id, payload.turn_id, payload.goals)

    @router.get("/conversations/{conversation_id}/goals/{goal_id}/result")
    def result(conversation_id: str, goal_id: str, request: Request, offset: int = 0, max_chars: int = 12000):
        return invoke(coordinator.read_result, *principal(request), conversation_id, goal_id, offset, max_chars)

    @router.post("/conversations/{conversation_id}/dispatch")
    def dispatch(conversation_id: str, payload: Dispatch, request: Request):
        return invoke(coordinator.dispatch, *principal(request), conversation_id, payload)

    @router.post("/conversations/{conversation_id}/goals/{goal_id}/control")
    def control(conversation_id: str, goal_id: str, payload: Control, request: Request):
        return invoke(coordinator.control, *principal(request), conversation_id, goal_id, payload)

    app.include_router(router)
    return coordinator


def coordinator_owner_scope(context, tenant: str, owner: str) -> tuple[str, str]:
    """A worker-view identity is never authority for the owner's whole conversation."""
    if context.role.strip().lower() == "viewer" or context.auth_mode == "signed_link" or not owner:
        raise HTTPException(403, "An authenticated conversation owner is required")
    return tenant, owner
