"""Thin authenticated owner routes. Principal/configuration come from the host service."""
from __future__ import annotations

from collections.abc import Callable
import asyncio
import hashlib
import hmac
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from .openclaw_runtime import RuntimeErrorBase
from .personal_library import LibraryRef
from .models import CreateDelegationRequest, ActiveWorkActionRequest
from .conversation_provider import ChatCompletionRequest
from .workspace_files import FileAdmissionError

from .coordinator import (
    Control, CoordinatorConfig, CoordinatorConflict, CoordinatorScope,
    CoordinatorScopeError, Route,
    Dispatch, Goal, StrictModel,
)


class TurnInput(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=524288)
    goals: list[Goal] = Field(default_factory=list, max_length=1000)
    library_refs: list[LibraryRef] | None = Field(default=None, max_length=128)


class ConversationCreateInput(StrictModel):
    scope: CoordinatorScope | None = None
    account_id: str = Field(default="", max_length=512)
    model_id: str = Field(default="", max_length=1024)
    application_config: dict | None = None


class ApplicationWorkActionInput(ActiveWorkActionRequest):
    application_bootstrap_bundle_b64: str = ""


class ApplicationGoalInput(StrictModel):
    dispatch_digest: str = Field(min_length=64, max_length=64)
    notification_ref: str | None = None
    delegation: CreateDelegationRequest


class GoalInput(StrictModel):
    turn_id: str
    goals: list[Goal] = Field(max_length=1000)


class RestoreResumeInput(StrictModel):
    expected_hold_set_hash: str = Field(min_length=64, max_length=64)
    approved_request_ids: list[str] = Field(default_factory=list, max_length=1000)
    approved_turn_ids: list[str] = Field(default_factory=list, max_length=1000)
    approved_goal_ids: list[str] = Field(default_factory=list, max_length=1000)


def install_coordinator_routes(app, coordinator, principal: Callable, default_config: Callable,
                               *, application_delegation_values: Callable | None = None,
                               application_work_response: Callable | None = None,
                               application_work_action: Callable | None = None,
                               application_action_receipt: Callable | None = None,
                               application_action_digest: Callable | None = None,
                               application_work_view_ref: Callable | None = None):
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
        except FileAdmissionError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)}) from exc
        except RuntimeErrorBase as exc:
            if str(exc) != "active_work_generation_changed":
                raise
            raise HTTPException(409, {
                "code": "active_work_generation_changed",
                "message": "Current work changed. Check its status and try again.",
            }) from exc

    def connected_config(tenant: str, owner: str, account_id: str, model_id: str = ""):
        account = coordinator.service.control_plane_store.get_provider_account(
            account_id=account_id, tenant_id=tenant, owner_id=owner,
        )
        profile = {
            "codex": "codex-cli", "openai": "codex-cli",
            "claude": "claude-code", "anthropic": "claude-code",
            "grok": "grok-build", "xai": "grok-build",
        }.get(str((account or {}).get("provider") or "").lower())
        if not account or account.get("status") != "ready" or not profile:
            raise HTTPException(409, "Choose a connected assistant before starting a conversation")
        from .coordinator_config import explicit_coordinator_config, connected_account_execution_mode
        explicit = invoke(explicit_coordinator_config)
        if model_id and account.get("auth_method") != "bedrock":
            raise HTTPException(409, "Model selection is unavailable for this connection")
        if explicit is not None and model_id and model_id != explicit.model:
            raise HTTPException(409, "The selected model differs from the configured conversation model")
        if explicit is not None:
            # A configured conversation model and effort are exact choices: the
            # chosen account must be for that AI; they are never swapped silently.
            try:
                model = coordinator.provider._model(explicit.model, tenant_id=tenant, owner_id=owner)
            except HTTPException as exc:
                raise HTTPException(409, "The configured conversation model is unavailable") from exc
            from .direct_runtime import DIRECT_PROVIDERS
            if model.harness_profile in DIRECT_PROVIDERS:
                from .control_plane import PROFILE_ACCOUNT_PROVIDERS
                from .native_api_keys import native_key_account, enabled
                protected = coordinator.service.control_plane_store.get_provider_account_record(
                    account_id=account_id, tenant_id=tenant, owner_id=owner,
                )
                if (str(account.get("provider")) not in PROFILE_ACCOUNT_PROVIDERS[model.harness_profile]
                        or protected is None or not native_key_account(protected) or not enabled()):
                    raise HTTPException(409, "Direct conversation requires this provider's protected API-key connection on a supported local host")
                profile = model.harness_profile
            if model.harness_profile != profile:
                raise HTTPException(409, "The chosen account is not for the configured conversation model")
            if getattr(model, "connection_id", "") and model.connection_id != account_id:
                raise HTTPException(409, "The configured model belongs to a different connection")
            if account.get("auth_method") == "bedrock" and getattr(model, "connection_id", "") != account_id:
                raise HTTPException(409, "Choose a declared conversation model for this Bedrock connection")
            if explicit.effort not in tuple(model.effort_choices):
                raise HTTPException(409, "The configured conversation effort is unavailable for its model")
            effort = explicit.effort
        elif account.get("auth_method") == "bedrock":
            from .bedrock_connections import account_config, conversation_model_id, selected_model
            native_model = selected_model({}, account_config(account)) if not model_id else ""
            try:
                model = coordinator.provider._model(model_id or conversation_model_id(account_id, native_model), tenant_id=tenant, owner_id=owner)
            except HTTPException as exc:
                raise HTTPException(409, "Declare this Bedrock model's context and effort details in Connections before using Chat") from exc
            if model.connection_id != account_id:
                raise HTTPException(409, "The selected model belongs to a different connection")
            effort = model.recommended_effort
        else:
            native_model = coordinator.service._resolve_worker_model(profile, connected_account_execution_mode(profile), tenant_id=tenant, owner_id=owner)
            try:
                model = coordinator.provider._model(f"{profile}:{native_model}", tenant_id=tenant, owner_id=owner)
            except HTTPException as exc:
                raise HTTPException(409, "The selected assistant model is unavailable for conversations") from exc
            if model.harness_profile != profile or model.native_model != native_model:
                raise HTTPException(409, "The selected assistant model is unavailable for conversations")
            effort = model.recommended_effort
        config = (CoordinatorConfig(model=model.id, effort=effort,
                    scope=CoordinatorScope(execution_mode=connected_account_execution_mode(profile)))
                  if account.get("auth_method") == "bedrock" and explicit is None
                  else invoke(default_config, tenant, owner, profile))
        if config is None:
            raise HTTPException(409, {"code": "coordinator_not_configured", "message": "Connect an assistant in Connections to start a conversation."})
        selected = CoordinatorConfig.model_validate(config)
        # The chosen account answers in the conversation. Configured helper routes,
        # each with its own account, stay; without any, one route uses the same account.
        mode = connected_account_execution_mode(profile)
        routes = explicit.routes if explicit is not None and explicit.routes else [
            Route(id="default", profile=profile, model=model.id, effort=effort,
                  execution_mode=mode, connection_id=account_id)]
        selected = selected.model_copy(update={
            "model": model.id,
            "effort": effort,
            "scope": selected.scope.model_copy(update={"connection_id": account_id, "execution_mode": mode}),
            "routes": routes,
        })

        from .control_plane import profile_provider_account_issue
        if account.get("auth_method") and profile_provider_account_issue(profile, account):
            raise HTTPException(409, "The connected account does not support this assistant")
        return selected, account, profile

    @router.post("/conversations")
    def create(request: Request, payload: ConversationCreateInput | None = None):
        tenant, owner = principal(request)
        if payload is not None and payload.application_config is not None:
            from .conversation_provider import _decode_bootstrap_bundle
            from .coordinator import canonical
            signed = _decode_bootstrap_bundle(request).get("coordinator_application_config")
            if (payload.scope is not None or payload.account_id or payload.model_id or signed is None
                    or canonical(signed) != canonical(payload.application_config)):
                raise HTTPException(403, "Application coordinator configuration requires its exact signed host declaration")
            selected = CoordinatorConfig.model_validate(payload.application_config)
            if selected.application is None:
                raise HTTPException(400, "Application coordinator configuration needs its destination and reference")
            return invoke(coordinator.create, tenant, owner, selected)
        if payload is not None and payload.account_id:
            selected, _, _ = connected_config(tenant, owner, payload.account_id, payload.model_id)
        else:
            if payload is not None and payload.model_id:
                raise HTTPException(422, "Select the model's owned connection")
            config = invoke(default_config, tenant, owner)
            if config is None:
                raise HTTPException(409, {"code": "coordinator_not_configured", "message": "Connect an assistant in Connections to start a conversation."})
            selected = CoordinatorConfig.model_validate(config)
        if payload is not None and payload.scope is not None:
            scope_update = payload.scope.model_dump(exclude_unset=True)
            if payload.account_id and scope_update.get("execution_mode", selected.scope.execution_mode) != selected.scope.execution_mode:
                # Placement is selected by the deployment, not an owner request.
                raise HTTPException(409, "This account uses the deployment's configured conversation placement")
            if "connection_id" in scope_update and selected.scope.connection_id and (
                scope_update["connection_id"] != selected.scope.connection_id
            ):
                raise HTTPException(409, "The selected assistant account conflicts with this project")
            selected = selected.model_copy(update={
                "scope": selected.scope.model_copy(update=scope_update),
            })
        return invoke(coordinator.create, tenant, owner, selected)

    @router.get("/readiness")
    def readiness(request: Request):
        """What a new conversation would meet on this deployment, read before anything is sent.

        Connected accounts use ordinary local host admission where the deployment enables
        it, otherwise the existing shared-container placement. These checks start nothing.
        Capacity and sign-in are still decided when the turn runs.
        """
        tenant, owner = principal(request)
        from .service import standalone_coordinator_host_enabled
        placement = ({"available": True, "code": "ready"} if standalone_coordinator_host_enabled()
                     else coordinator.service.shared_workspace_deployment_readiness(
                         {"mode": "shared", "execution_mode": "docker"}))
        default = {"available": True, "code": "ready"}
        try:
            config = invoke(default_config, tenant, owner)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            config = None
        if config is None:
            default = {"available": False, "code": "coordinator_not_configured"}
        else:
            selected = CoordinatorConfig.model_validate(config)
            installed = getattr(coordinator.service.runtime, "host_cli_installed", None)
            if selected.scope.execution_mode == "host" and callable(installed):
                try:
                    profile = coordinator.provider._model(
                        selected.model, tenant_id=tenant, owner_id=owner
                    ).harness_profile
                except Exception:
                    profile = ""  # an unavailable model is reported when the turn runs
                if profile and profile not in {"direct-openai", "direct-anthropic", "direct-xai"} and installed(profile) is False:
                    default = {"available": False, "code": "native_cli_missing"}
        connected = {"available": placement.get("available") is True,
                     "code": str(placement.get("code") or "")}
        list_accounts = getattr(getattr(coordinator.service, "control_plane_store", None), "list_provider_accounts", None)
        if connected["available"] and callable(list_accounts):
            supported = []
            installed = getattr(coordinator.service.runtime, "host_cli_installed", None)
            for account in list_accounts(tenant_id=tenant, owner_id=owner):
                if account.get("status") != "ready":
                    continue
                try:
                    declared_models = []
                    if account.get("auth_method") == "bedrock":
                        from .bedrock_connections import conversation_models
                        declared_models = conversation_models(account)
                        from .coordinator_config import explicit_coordinator_config
                        configured = explicit_coordinator_config()
                        if configured is not None:
                            declared_models = [item for item in declared_models if item.id == configured.model]
                    selected, account, profile = connected_config(tenant, owner, account["account_id"], declared_models[0].id if declared_models else "")
                except (HTTPException, CoordinatorConflict, ValueError):
                    continue
                if (selected.scope.execution_mode == "host" and profile not in {"direct-openai", "direct-anthropic", "direct-xai"}
                        and callable(installed) and installed(profile) is False):
                    continue
                supported.append({"account_id": account["account_id"], "profile": profile,
                                  "auth_method": str(account.get("auth_method") or ""),
                                  **({"models": [{"id": item.id, "native_model": item.native_model} for item in declared_models]} if declared_models else {})})
            connected["supported_account_ids"] = [item["account_id"] for item in supported]
            connected["supported_accounts"] = supported
        return {"connected_account": connected, "default": default}

    @router.get("/conversations/{conversation_id}")
    def status(conversation_id: str, request: Request, include_history: bool = True):
        snapshot = invoke(coordinator.refresh, *principal(request), conversation_id, include_history)
        if application_work_view_ref is not None:
            for goal in snapshot.get("goals", []):
                work_ref = str(goal.get("work_ref") or "")
                if not work_ref:
                    continue
                view_ref = application_work_view_ref(work_ref, request)
                if view_ref:
                    goal["view_ref"] = view_ref
        return snapshot

    @router.post("/conversations/{conversation_id}/turns")
    def submit(conversation_id: str, payload: TurnInput, request: Request):
        identity = (*principal(request), conversation_id)
        invoke(coordinator.accept_turn, *identity, payload.idempotency_key, payload.message, payload.goals,
               None if payload.library_refs is None else [ref.model_dump() for ref in payload.library_refs])
        return invoke(coordinator.start_turn, *identity, payload.idempotency_key)

    @router.get("/conversations/{conversation_id}/turns/{turn_id}")
    def application_turn_status(conversation_id: str, turn_id: str, request: Request):
        from .coordinator_application import turn_status
        return invoke(turn_status, coordinator, *principal(request), conversation_id, turn_id)

    @router.get("/conversations/{conversation_id}/turns/{turn_id}/result")
    def application_turn_result(conversation_id: str, turn_id: str, request: Request,
                                request_id: str, run_id: str, offset: int = 0, max_chars: int = 12000):
        from .coordinator_application import turn_result
        return invoke(turn_result, coordinator, *principal(request), conversation_id, turn_id,
                      request_id, run_id, offset, max_chars)

    @router.post("/conversations/{conversation_id}/turns/{turn_id}/prepare")
    async def prepare_application_turn(conversation_id: str, turn_id: str,
                                       payload: ChatCompletionRequest, request: Request):
        from .coordinator_application import prepare_turn
        tenant, owner = principal(request)
        hydrate = getattr(coordinator.provider, "hydrate_application_request", None)
        if not callable(hydrate):
            raise HTTPException(503, "Application provider preparation is unavailable")
        payload = hydrate(payload, request, tenant_id=tenant, owner_id=owner)
        metadata = payload.metadata
        if bool(metadata.native_invocation_id) != bool(metadata.native_body_sha256):
            raise HTTPException(400, "Native invocation requires its exact body digest")
        if metadata.native_invocation_id and not hmac.compare_digest(
                hashlib.sha256(await request.body()).hexdigest(), metadata.native_body_sha256):
            raise HTTPException(400, "Native invocation body digest mismatch")
        return await asyncio.to_thread(invoke, prepare_turn, coordinator, tenant, owner,
                                      conversation_id, turn_id, payload)

    @router.post("/conversations/{conversation_id}/turns/{turn_id}/stop")
    def stop_turn(conversation_id: str, turn_id: str, request: Request):
        return invoke(coordinator.cancel_turn, *principal(request), conversation_id, turn_id)

    @router.post("/conversations/{conversation_id}/turns/{turn_id}/retry-result")
    def retry_result_turn(conversation_id: str, turn_id: str, request: Request):
        return invoke(coordinator.retry_result_turn, *principal(request), conversation_id, turn_id)

    @router.post("/conversations/{conversation_id}/turns/{turn_id}/retry")
    def retry_interactive_turn(conversation_id: str, turn_id: str, request: Request):
        return invoke(coordinator.retry_interactive_turn, *principal(request), conversation_id, turn_id)

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

    @router.get("/conversations/{conversation_id}/publications")
    def result_publications(conversation_id: str, request: Request, cursor: str = "", limit: int = 100):
        from .coordinator_application import publications
        return invoke(publications, coordinator, *principal(request), conversation_id, cursor, limit)

    @router.get("/conversations/{conversation_id}/goals/{goal_id}/result")
    def result(conversation_id: str, goal_id: str, request: Request, offset: int = 0, max_chars: int = 12000, run_id: str = ""):
        return invoke(coordinator.read_result, *principal(request), conversation_id, goal_id, offset, max_chars, run_id)

    @router.post("/conversations/{conversation_id}/dispatch")
    def dispatch(conversation_id: str, payload: Dispatch, request: Request):
        return invoke(coordinator.dispatch, *principal(request), conversation_id, payload)

    @router.get("/conversations/{conversation_id}/goals/{goal_id}/dispatch")
    def application_dispatch(conversation_id: str, goal_id: str, request: Request):
        from .coordinator_application import dispatch_input
        return invoke(dispatch_input, coordinator, *principal(request), conversation_id, goal_id)

    @router.post("/conversations/{conversation_id}/goals/{goal_id}/prepare")
    def prepare_application_goal(conversation_id: str, goal_id: str,
                                 payload: ApplicationGoalInput, request: Request):
        from .coordinator_application import prepare_goal
        tenant, owner = principal(request)
        if application_delegation_values is None or application_work_response is None:
            raise HTTPException(503, "Application delegation preparation is unavailable")
        values = application_delegation_values(payload.delegation, request, preserve_source=True)
        reservation = invoke(prepare_goal, coordinator, tenant, owner, conversation_id, goal_id,
                             payload.dispatch_digest, values, payload.notification_ref)
        return {"goal": invoke(coordinator.goal_snapshot, tenant, owner, conversation_id, goal_id),
                "work": application_work_response(reservation, request)}

    @router.get("/conversations/{conversation_id}/goals/{goal_id}/actions/{action_key}/preparation")
    def application_action_preparation(conversation_id:str,goal_id:str,action_key:str,request:Request):
        from .coordinator_application import action_input
        return invoke(action_input,coordinator,*principal(request),conversation_id,goal_id,action_key)

    @router.post("/conversations/{conversation_id}/goals/{goal_id}/actions/preflight")
    def application_action_preflight(conversation_id:str,goal_id:str,payload:ActiveWorkActionRequest,request:Request,run_id:str):
        from .coordinator_application import action_preflight, dispatch_retry_receipt
        replay = invoke(dispatch_retry_receipt, coordinator, *principal(request), conversation_id, goal_id, payload)
        if replay:
            return {"state": "completed", "receipt": replay}
        if application_action_digest is None:raise HTTPException(503,"Application action owner unavailable")
        return invoke(action_preflight,coordinator,*principal(request),conversation_id,goal_id,run_id,payload,application_action_digest(payload))

    @router.post("/conversations/{conversation_id}/goals/{goal_id}/actions", status_code=202)
    def application_goal_action(conversation_id: str, goal_id: str, payload: ApplicationWorkActionInput,
                                request: Request, run_id: str):
        from .coordinator_application import (goal_action_binding, finish_goal_action, self_goal_stop,
            action_preflight, validate_action_bundle, stage_action_context)
        tenant, owner = principal(request)
        from .coordinator_application import dispatch_retry_receipt
        replay = invoke(dispatch_retry_receipt, coordinator, tenant, owner, conversation_id, goal_id, payload)
        if replay:
            return replay
        goal = invoke(coordinator.goal_snapshot, tenant, owner, conversation_id, goal_id)
        if not goal["work_ref"]:
            return invoke(self_goal_stop, coordinator, tenant, owner, conversation_id, goal_id, run_id, payload)
        if application_work_action is None or application_action_digest is None:
            raise HTTPException(503, "Application work actions are unavailable")
        preflight = invoke(action_preflight,coordinator,tenant,owner,conversation_id,goal_id,run_id,payload,application_action_digest(payload))
        if preflight["state"] in {"completed","failed","pending"}:
            binding = invoke(goal_action_binding,coordinator,tenant,owner,conversation_id,goal_id,run_id,payload,application_action_digest(payload),False)
            response = application_action_receipt(binding["work_ref"],payload,request)
            return invoke(finish_goal_action,coordinator,tenant,owner,conversation_id,goal_id,payload.idempotency_key,response)
        from .conversation_provider import _decode_bootstrap_bundle
        bundle = _decode_bootstrap_bundle(request,payload.application_bootstrap_bundle_b64)
        from .upload_projection import project_bootstrap_upload_files
        try:
            bundle = project_bootstrap_upload_files(bundle, tenant_id=tenant, owner_id=owner)
        except ValueError as exc:
            raise HTTPException(400, {"code": "application_upload_projection_invalid",
                                      "message": "Selected upload inputs could not be projected."}) from exc
        if payload.action in {"queue","message","steer","retry","resume"}:
            invoke(validate_action_bundle,coordinator,tenant,owner,conversation_id,goal_id,bundle)
        binding = invoke(goal_action_binding, coordinator, tenant, owner, conversation_id, goal_id,
                         run_id, payload, application_action_digest(payload), True)
        staged = [False]
        def attach(response):
            if bundle:
                staged[0] = bool(invoke(stage_action_context,coordinator,tenant,owner,conversation_id,goal_id,payload.idempotency_key,bundle,response))
        response = application_work_action(binding["work_ref"], payload, request,
                                           start_processor=False, expected_run_id=binding["source_run_id"],
                                           application_effect=attach if bundle else None)
        attached = bool(bundle and not staged[0] and invoke(stage_action_context,coordinator,tenant,owner,conversation_id,goal_id,payload.idempotency_key,bundle,response))
        result = invoke(finish_goal_action,coordinator,tenant,owner,conversation_id,goal_id,payload.idempotency_key,response)
        if attached and preflight["state"] == "unprepared":
            current=invoke(coordinator.goal_snapshot,tenant,owner,conversation_id,goal_id)
            if current["run_id"] == response.get("runId"):
                coordinator.service.start_assigned_run(current["worker_id"])
        return result

    @router.post("/conversations/{conversation_id}/goals/{goal_id}/actions/receipt", status_code=202)
    def application_goal_action_receipt(conversation_id: str, goal_id: str, payload: ActiveWorkActionRequest,
                                        request: Request, run_id: str):
        from .coordinator_application import goal_action_binding, finish_goal_action, self_goal_stop, dispatch_retry_receipt
        tenant, owner = principal(request)
        replay = invoke(dispatch_retry_receipt, coordinator, tenant, owner, conversation_id, goal_id, payload)
        if replay:
            return replay
        goal = invoke(coordinator.goal_snapshot, tenant, owner, conversation_id, goal_id)
        if not goal["work_ref"]:
            return invoke(lambda: self_goal_stop(coordinator, tenant, owner, conversation_id, goal_id, run_id, payload, receipt_only=True))
        if application_action_receipt is None or application_action_digest is None:
            raise HTTPException(503, "Application work action receipts are unavailable")
        binding = invoke(goal_action_binding, coordinator, tenant, owner, conversation_id, goal_id,
                         run_id, payload, application_action_digest(payload), False)
        response = application_action_receipt(binding["work_ref"], payload, request)
        return invoke(finish_goal_action, coordinator, tenant, owner, conversation_id, goal_id,
                      payload.idempotency_key, response)

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
