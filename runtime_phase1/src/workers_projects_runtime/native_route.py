"""Optional service-owned checks of generated native launch configuration.

Existing runtime builders still own route resolution and session policy. This
contract checks their result; it supplies neither credentials nor instructions.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from urllib.parse import urlsplit

from .openclaw_runtime import RuntimeConfigurationError, RuntimeErrorBase


NATIVE_TRANSPORT_ENV_KEYS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "SSLKEYLOGFILE",
})


class NativeRouteContractError(RuntimeConfigurationError):
    failure_class = "native_route_contract_mismatch"


class NativeTurnIncompleteError(RuntimeErrorBase):
    failure_class = "native_turn_incomplete"


def protected_provider_environment_keys() -> set[str]:
    from .bootstrap import RUN_BOUND_PROVIDER_ENV_KEYS
    from .current_native_account import _CURRENT_NATIVE_AUTH_ENV
    from .mission_provider_accounts import _CLAUDE_CONFLICTING_ENV
    from .openclaw_runtime import _PROVIDER_ENV_KEYS
    return set(_PROVIDER_ENV_KEYS) | RUN_BOUND_PROVIDER_ENV_KEYS | _CLAUDE_CONFLICTING_ENV | _CURRENT_NATIVE_AUTH_ENV


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate native route field")
        result[key] = value
    return result


def codex_route_contract() -> dict | None:
    raw = os.environ.get("WPR_CODEX_CLI_ROUTE_CONTRACT", "").strip()
    if not raw:
        return None
    fields = {"version", "execution_mode", "model", "effort", "provider_id", "base_url",
              "env_key", "wire_api", "session_mode", "max_capacity_retries", "require_completed_turn"}
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != fields
                or type(value["version"]) is not int or value["version"] != 1
                or value["execution_mode"] not in {"host", "docker"}
                or value["session_mode"] != "stateless"
                or type(value["require_completed_turn"]) is not bool
                or type(value["max_capacity_retries"]) is not int
                or not 0 <= value["max_capacity_retries"] <= 1000
                or any(not isinstance(value[key], str) or not value[key].strip()
                       for key in ("model", "effort", "provider_id", "base_url", "env_key", "wire_api"))
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value["env_key"])
                or not re.fullmatch(r"[A-Za-z0-9_-]+", value["provider_id"])):
            raise ValueError
        endpoint = urlsplit(value["base_url"])
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError
        endpoint.port  # Validate a supplied port before a process can launch.
        from .bootstrap import deployment_provider_env_projection_enabled
        if deployment_provider_env_projection_enabled():
            raise NativeRouteContractError(
                "A service-owned native route requires GLASSHIVE_PROJECT_PROVIDER_ENV=false; "
                "deployment credentials must not be re-sourced from runtime.env"
            )
        return value
    except (ValueError, TypeError, KeyError):
        raise NativeRouteContractError("The configured native route contract is invalid") from None


def _merge(target, incoming):
    for key, value in incoming.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _merge(target[key], value)
        else:
            target[key] = value


def codex_command_route(command: list[str]) -> dict:
    if len(command) < 2 or command[1] != "exec" or (len(command) > 2 and command[2] == "resume"):
        raise NativeRouteContractError("The native route contract requires a fresh Codex exec command")
    if any(part.startswith(("-p", "--profile", "--oss", "--local-provider")) for part in command[2:]):
        raise NativeRouteContractError("Client profile or local-provider overrides conflict with the service-owned route")
    overrides, models = {}, []
    try:
        for index, part in enumerate(command):
            if part in {"-c", "--config"}:
                _merge(overrides, tomllib.loads(command[index + 1]))
            elif part in {"-m", "--model"}:
                models.append(command[index + 1])
        if len(models) != 1 or ("model" in overrides and overrides["model"] != models[0]):
            raise ValueError
        provider_id = overrides["model_provider"]
        provider = overrides["model_providers"][provider_id]
        if provider.get("requires_openai_auth") is not False:
            raise ValueError
        return {"model": models[0], "effort": overrides["model_reasoning_effort"],
                "provider_id": provider_id, "base_url": provider["base_url"],
                "env_key": provider["env_key"], "wire_api": provider["wire_api"]}
    except (ValueError, TypeError, KeyError, IndexError):
        raise NativeRouteContractError("The generated native command does not expose its exact route") from None


def validate_codex_route(
    contract: dict, *, command: list[str], environment: dict[str, str],
    bootstrap_environment: dict[str, str], bootstrap_settings: str,
    execution_mode: str, stateless: bool, protected_provider_keys: set[str],
) -> None:
    if execution_mode != contract["execution_mode"] or not stateless:
        raise NativeRouteContractError("The worker does not match the native route execution and session policy")
    observed = codex_command_route(command)
    if any(observed[key] != contract[key] for key in observed):
        raise NativeRouteContractError("The generated native command differs from the configured route contract")
    try:
        retries = int(os.environ.get("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", "5"))
        settings = tomllib.loads(bootstrap_settings) if bootstrap_settings.strip() else {}
    except (ValueError, TypeError):
        raise NativeRouteContractError("The native route contract settings are invalid") from None
    if retries != contract["max_capacity_retries"]:
        raise NativeRouteContractError("The native route capacity retry setting differs from its contract")
    providers = settings.get("model_providers")
    if providers is not None:
        selected = providers.get(contract["provider_id"]) if isinstance(providers, dict) else None
        if (not isinstance(providers, dict) or set(providers) != {contract["provider_id"]}
                or not isinstance(selected, dict) or set(selected) - {"stream_idle_timeout_ms"}
                or any(type(value) is not int or value <= 0 for value in selected.values())):
            raise NativeRouteContractError("Invocation provider settings cannot redefine native route authority")
    if any(key in settings for key in ("model", "model_provider", "profile", "profiles")):
        raise NativeRouteContractError("Invocation native settings cannot redefine the service-owned route")
    if any(key in protected_provider_keys | {contract["env_key"]} or str(key).upper() in NATIVE_TRANSPORT_ENV_KEYS
           for key in bootstrap_environment):
        raise NativeRouteContractError("Invocation environment cannot redefine native route authority or transport")
    key = contract["env_key"]
    if not str(environment.get(key) or "").strip():
        raise NativeRouteContractError("The configured native route credential is unavailable")
    # Leave application variables to their existing bootstrap owner. Only the
    # selected provider's authority crosses the native exec environment.
    for candidate in protected_provider_keys - {key}:
        environment.pop(candidate, None)


def require_completed_codex_turn(stdout: str) -> None:
    types, output = [], False
    try:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError
            kind = event.get("type")
            types.append(kind)
            item = event.get("item")
            if (kind == "item.completed" and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and isinstance(item.get("text"), str) and item["text"].strip()):
                output = True
        if (not output or not types or types[-1] != "turn.completed"
                or types.count("turn.completed") != 1 or "turn.failed" in types
                or types.count("turn.started") > 1 or types.count("thread.started") > 1):
            raise ValueError
    except (ValueError, TypeError):
        raise NativeTurnIncompleteError("The native process ended without one completed turn and an agent response") from None
