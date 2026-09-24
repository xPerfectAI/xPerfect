"""Exact native model selection shared by Connections and worker admission."""
from __future__ import annotations

import os
import shutil
import subprocess

from .openclaw_runtime import RuntimeConfigurationError


class ModelConfigurationRequired(RuntimeConfigurationError):
    code = "model_configuration_required"


def valid_model_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 180 and all(ord(char) >= 33 for char in value)


def native_grok_models() -> list[str]:
    """Read the installed native CLI's catalog; never infer an ID from its default."""
    configured = os.environ.get("WPR_GROK_BIN") or "grok"
    binary = configured if os.path.isabs(configured) else shutil.which(configured)
    if not binary:
        return []
    try:
        result = subprocess.run(
            [binary, "models"], capture_output=True, text=True, timeout=12, check=False,
            env={**os.environ, "GROK_DISABLE_AUTOUPDATER": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
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
    return models


def selected_grok_model(store, tenant_id: str = "local", owner_id: str = "") -> tuple[str, str]:
    """Deployment selection wins; a saved owner choice is used only when absent."""
    deployment = os.environ.get("WPR_MODEL_GROK_BUILD", "")
    if deployment:
        if not valid_model_id(deployment):
            raise ModelConfigurationRequired("The deployment Grok model is invalid. Set an exact model with --model grok-build=<id>.")
        return deployment, "deployment"
    if owner_id:
        saved = (store.get_user_preferences(tenant_id, owner_id) or {}).get("grok_model", "")
        if saved:
            if not valid_model_id(saved):
                raise ModelConfigurationRequired("Your saved Grok model is invalid. Choose an exact model in Connections.")
            return saved, "owner"
    raise ModelConfigurationRequired("Choose an exact Grok model in Connections, or set --model grok-build=<id> when starting xPerfect.")
