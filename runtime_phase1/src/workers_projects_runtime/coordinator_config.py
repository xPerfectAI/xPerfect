"""Resolve coordinator defaults from the runtime's exact configured model catalog."""
from __future__ import annotations

import os

from .coordinator import CoordinatorConfig, CoordinatorConflict, CoordinatorScope, Route


def validate_configured_coordinator(value: object) -> CoordinatorConfig:
    """Check a coordinator configuration as conversation creation will use it."""
    try:
        config = (CoordinatorConfig.model_validate_json(value) if isinstance(value, str)
                  else CoordinatorConfig.model_validate(value))
    except ValueError as exc:  # includes pydantic's ValidationError
        raise CoordinatorConflict("The deployment's coordinator configuration is invalid") from exc
    if len({route.id for route in config.routes}) != len(config.routes):
        raise CoordinatorConflict("The deployment's coordinator route IDs must be unique")
    return config


def explicit_coordinator_config() -> CoordinatorConfig | None:
    """The deployment's explicit coordinator configuration and helper routes, if set."""
    explicit = os.environ.get("GLASSHIVE_COORDINATOR_CONFIG_JSON", "").strip()
    return validate_configured_coordinator(explicit) if explicit else None


def configured_coordinator(service, provider, tenant_id: str = "local", owner_id: str = "",
                           selected_profile: str = "") -> CoordinatorConfig:
    # Optional advanced deployment/owner configuration is explicit. It never becomes
    # a model-routing heuristic and never falls back after an invalid selection.
    explicit = explicit_coordinator_config()
    if explicit is not None:
        if explicit.routes:
            return explicit
        # Without configured helper routes, one route uses the conversation's own exact model.
        model = provider._model(explicit.model, tenant_id=tenant_id, owner_id=owner_id)
        return explicit.model_copy(update={"routes": [Route(
            id="default", profile=model.harness_profile, model=model.id,
            effort=explicit.effort, execution_mode=explicit.scope.execution_mode)]})
    preferences = service.store.get_user_preferences(tenant_id, owner_id) if owner_id else {}
    preferences = preferences or {}
    profile = selected_profile or preferences.get("default_worker_profile") or os.environ.get("GLASSHIVE_DEFAULT_WORKER_PROFILE") or os.environ.get("WPR_DEFAULT_WORKER_PROFILE") or "codex-cli"
    mode = "docker" if selected_profile else os.environ.get("WPR_DEFAULT_EXECUTION_MODE", "docker")
    model = service._resolve_worker_model(profile, mode, tenant_id=tenant_id, owner_id=owner_id)
    from .conversation_provider import GLASSHIVE_MODELS, _configured_grok_conversation_model
    configured_grok = _configured_grok_conversation_model(model) if profile == "grok-build" else None
    catalog = list(GLASSHIVE_MODELS.values()) + ([configured_grok] if configured_grok else [])
    matches = [item for item in catalog if item.harness_profile == profile and item.native_model == model]
    if len(matches) != 1:
        raise CoordinatorConflict("The configured assistant is unavailable for conversations")
    selected = matches[0]
    # Grok's native effort is applied by its ACP runner, not the conversation
    # model catalog, whose only route effort is "default".
    effort_variable = {"codex-cli": "WPR_CODEX_CLI_REASONING_EFFORT", "claude-code": "WPR_CLAUDE_CODE_EFFORT"}.get(profile)
    preference_key = {"codex-cli": "codex_reasoning_effort", "claude-code": "claude_effort"}.get(profile, "")
    effort = preferences.get(preference_key) or (os.environ.get(effort_variable, "") if effort_variable else "")
    effort = effort or selected.recommended_effort
    if effort not in selected.effort_choices:
        raise CoordinatorConflict("The configured assistant effort is unavailable")
    return CoordinatorConfig(model=selected.id, effort=effort,
        scope=CoordinatorScope(execution_mode=mode), routes=[Route(
        id="default", profile=profile, model=selected.id, effort=effort, execution_mode=mode)])
