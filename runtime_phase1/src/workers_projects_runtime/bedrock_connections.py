"""Declared Claude Bedrock routes and protected owner-scoped credentials.

Configuration checks do not discover accounts or invoke a cloud model.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .control_plane import ControlPlaneError

CREDENTIAL_FILE = "bedrock-credentials.json"
SECRET_SELECTOR = "XPERFECT_BEDROCK_CREDENTIALS"
AWS_CREDENTIAL_KEYS = frozenset({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"})
AWS_ROUTE_KEYS = AWS_CREDENTIAL_KEYS | frozenset({"AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"})


def connection_config(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict) or set(value) - {"region", "model_ids", "default_model", "credential_mode", "model_provider", "model_contracts"}:
        raise ControlPlaneError("Enter a Bedrock region and declared Claude model choices")
    region = value.get("region", "")
    models = value.get("model_ids", [])
    default = value.get("default_model", "")
    mode = value.get("credential_mode", "protected_file")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", region) or len(region) > 64:
        raise ControlPlaneError("Enter a valid Bedrock region")
    if (not isinstance(models, list) or not 1 <= len(models) <= 100
            or any(not isinstance(item, str) or not 1 <= len(item) <= 512
                   or any(ord(char) < 33 or ord(char) > 126 for char in item) for item in models)
            or len(set(models)) != len(models)):
        raise ControlPlaneError("Declare distinct exact Bedrock model or inference-profile IDs")
    if default and default not in models:
        raise ControlPlaneError("The default model must be one of this connection's declared choices")
    if mode not in {"protected_file", "workload_identity"} or value.get("model_provider", "anthropic") != "anthropic":
        raise ControlPlaneError("Claude Code Bedrock connections support declared Anthropic models only")
    contracts = value.get("model_contracts", {})
    from .runtime_requirements import CLAUDE_CODE_EFFORT_LEVELS
    if not isinstance(contracts, dict) or set(contracts) - set(models):
        raise ControlPlaneError("Conversation metadata must identify a declared Bedrock model")
    for contract in contracts.values():
        if (not isinstance(contract, dict) or set(contract) != {"context_window", "effort_choices", "recommended_effort"}
                or type(contract.get("context_window")) is not int or not 8192 <= contract["context_window"] <= 10_000_000
                or not isinstance(contract.get("effort_choices"), list) or not contract["effort_choices"]
                or any(item not in CLAUDE_CODE_EFFORT_LEVELS for item in contract["effort_choices"])
                or len(set(contract["effort_choices"])) != len(contract["effort_choices"])
                or contract.get("recommended_effort") not in contract["effort_choices"]):
            raise ControlPlaneError("Declare a valid native Claude context window and effort contract for conversation use")
    return {"region": region, "model_ids": models, "default_model": default,
            "credential_mode": mode, "model_provider": "anthropic", **({"model_contracts": contracts} if contracts else {})}


def conversation_model_id(account_id: str, native_model: str) -> str:
    return f"claude-code:bedrock:{account_id}:{native_model}"


def conversation_models(account: dict):
    """Reuse the existing catalog descriptor only for explicitly supplied contracts."""
    from .conversation_provider import HarnessModel
    config = account_config(account)
    return [HarnessModel(id=conversation_model_id(account["account_id"], native_model),
        display_name=f"{account.get('label') or 'Bedrock'} / {native_model}", harness_profile="claude-code",
        native_model=native_model, context_window=contract["context_window"],
        effort_choices=tuple(contract["effort_choices"]), recommended_effort=contract["recommended_effort"],
        connection_id=account["account_id"])
        for native_model, contract in config.get("model_contracts", {}).items()]


def declared_conversation_models(control, tenant_id: str, owner_id: str):
    reader = getattr(control, "list_provider_accounts", None)
    if not callable(reader) or not owner_id:
        return []
    return [model for account in reader(tenant_id=tenant_id, owner_id=owner_id)
            if account.get("auth_method") == "bedrock" and account.get("status") != "disconnected"
            for model in conversation_models(account)]


def account_config(account: dict) -> dict:
    return connection_config(account.get("connection_config") or account.get("connection_config_json"))


def selected_model(worker: dict, config: dict) -> str:
    selected = worker.get("model") or config.get("default_model") or os.environ.get("WPR_CLAUDE_CODE_PROVIDER_MODEL", "").strip()
    if not isinstance(selected, str) or selected not in config["model_ids"]:
        raise ControlPlaneError("Choose an exact model or inference profile declared by this Bedrock connection")
    return selected


def credentials(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - AWS_CREDENTIAL_KEYS:
        raise ControlPlaneError("Enter only the supported AWS credential fields")
    if any(not isinstance(item, str) or not 1 <= len(item) <= 8192
           or any(ord(char) <= 32 or ord(char) == 127 for char in item) for item in value.values()):
        raise ControlPlaneError("AWS credentials are invalid")
    bearer = "AWS_BEARER_TOKEN_BEDROCK" in value
    pair = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}.issubset(value)
    if bearer == pair or (bearer and len(value) != 1) or (not pair and "AWS_SESSION_TOKEN" in value):
        raise ControlPlaneError("Use a Bedrock bearer token or an AWS access key pair with an optional session token")
    return dict(value)


def read_credentials(path: Path, *, member: bool = False) -> dict[str, str]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32768
                    or info.st_mode & (0o007 if member else 0o077)
                    or (not member and info.st_uid != os.geteuid())):
                raise ValueError("unsafe credential artifact")
            return credentials(json.loads(stream.read(32769)))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ControlPlaneError("Bedrock credentials are missing or unsafe; reconnect this account") from exc


def workload_identity_enabled() -> bool:
    return os.environ.get("XPERFECT_BEDROCK_WORKLOAD_IDENTITY_ENABLED", "").lower() in {"1", "true", "yes", "on"}


def route_environment(config: dict) -> dict[str, str]:
    return {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": config["region"],
            "AWS_DEFAULT_REGION": config["region"]}


def connect(store, homes, *, account_id: str, tenant_id: str, owner_id: str, value: str | None = None) -> dict:
    lease = store.acquire_provider_lease(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id,
        lane="bedrock-configuration", worker_id="bedrock-configuration", run_id=f"configuration:{account_id}",
        ttl_seconds=60, allowed_statuses=("disconnected", "ready", "action_required", "error", "unavailable"))
    try:
        return _connect_leased(store, homes, account_id=account_id, tenant_id=tenant_id, owner_id=owner_id, value=value)
    finally:
        store.release_provider_lease(lease_id=lease["lease_id"], tenant_id=tenant_id, owner_id=owner_id)


def _connect_leased(store, homes, *, account_id: str, tenant_id: str, owner_id: str, value: str | None) -> dict:
    account = store.get_provider_account_record(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
    if account is None or account.get("auth_method") != "bedrock":
        raise ControlPlaneError("Bedrock connection not found for this user")
    config = account_config(account)
    home = homes.ensure_home(tenant_id=tenant_id, owner_id=owner_id, account_id=account_id, provider=account["provider"])
    homes.tighten_permissions(account_home=home)
    if config["credential_mode"] == "workload_identity":
        if value is not None or not workload_identity_enabled():
            raise ControlPlaneError("This deployment has not enabled Bedrock workload identity")
    elif value is not None:
        try:
            protected = credentials(json.loads(value))
        except (ValueError, TypeError) as exc:
            raise ControlPlaneError("Enter AWS credentials as a JSON object") from exc
        fd, temporary = tempfile.mkstemp(dir=home, prefix=".bedrock-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(protected, stream); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, home / CREDENTIAL_FILE)
            directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        read_credentials(home / CREDENTIAL_FILE)
    result = store.update_provider_account_status(account_id=account_id, tenant_id=tenant_id,
        owner_id=owner_id, status="ready", reconnect_reason="")
    return {**result, "verification": "configuration_only", "message": "Configuration ready. AWS access is checked when work starts."}
