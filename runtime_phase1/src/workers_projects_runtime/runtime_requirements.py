from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Native CLI values; launch-time help checks retain compatibility with installed versions.
CLAUDE_CODE_EFFORT_LEVELS = ("default", "low", "medium", "high", "xhigh", "max")


DEFAULT_HOST_RUNTIME_REQUIREMENTS: dict[str, list[dict[str, Any]]] = {
    "codex-cli": [
        {
            "binary": "codex",
            "use_configured_binary": True,
            "label": "Codex CLI",
            # Existing host-native Viventium conversations remain compatible with this reviewed
            # baseline. Fresh isolated GlassHive workstations pin the current release separately.
            "min_version": "0.144.1",
            "recovery_hint": "Run `codex update` when supported, or update the Codex app/CLI, then restart GlassHive.",
        }
    ],
    "claude-code": [
        {
            "binary": "claude",
            "use_configured_binary": True,
            "label": "Claude Code",
            "min_version": "2.1.178",
            "required_help_flags": ["--effort"],
            "recovery_hint": "Run `claude install latest` or `claude update`, then restart GlassHive.",
        }
    ],
    "openclaw-general": [
        {
            "binary": "openclaw",
            "use_configured_binary": True,
            "label": "OpenClaw",
            "exact_version": "2026.7.1-2",
            "recovery_hint": "Install GlassHive's reviewed OpenClaw 2026.7.1-2 runtime, then restart GlassHive.",
        }
    ],
    "openclaw": [
        {
            "binary": "openclaw",
            "use_configured_binary": True,
            "label": "OpenClaw",
            "exact_version": "2026.7.1-2",
            "recovery_hint": "Install GlassHive's reviewed OpenClaw 2026.7.1-2 runtime, then restart GlassHive.",
        }
    ],
}


class RuntimeRequirementConfigurationError(RuntimeError):
    """Raised when an explicit host-runtime requirements override is unusable."""


@dataclass(frozen=True)
class RuntimeRequirementIssue:
    profile: str
    runtime_name: str
    binary: str
    label: str
    problem: str
    required_version: str = ""
    actual_version: str = ""
    diagnostic_summary: str = ""
    recovery_hint: str = ""

    @property
    def user_message(self) -> str:
        profile_hint = f" for `{self.profile}`" if self.profile else ""
        if self.problem == "version_mismatch_exact":
            actual = f" Current version: {self.actual_version}." if self.actual_version else ""
            return (
                f"GlassHive cannot start the selected host worker{profile_hint} because {self.label} "
                f"must be exactly {self.required_version}.{actual}"
            )
        if self.problem == "version_mismatch":
            actual = f" Current version: {self.actual_version}." if self.actual_version else ""
            return (
                f"GlassHive cannot start the selected host worker{profile_hint} because {self.label} "
                f"must be >= {self.required_version}.{actual}"
            )
        if self.problem == "version_unverified":
            return (
                f"GlassHive cannot start the selected host worker{profile_hint} because it could not "
                f"verify the configured {self.label} version required by this host profile."
            )
        if self.problem == "capability_missing":
            return (
                f"GlassHive cannot start the selected host worker{profile_hint} because the configured "
                f"{self.label} does not expose a required native capability for this host profile."
            )
        if self.problem == "capability_unverified":
            return (
                f"GlassHive cannot start the selected host worker{profile_hint} because it could not "
                f"verify the native capability surface for {self.label}."
            )
        return (
            f"GlassHive cannot start the selected host worker{profile_hint} because the required "
            f"host dependency `{self.label}` is not installed or not available to the GlassHive service."
        )

    @property
    def recommended_recovery(self) -> str:
        if self.recovery_hint:
            return self.recovery_hint
        return (
            "Use a configured managed dependency, choose another available worker profile, or use "
            "sandbox/workstation execution when that still satisfies the user's request. Ask the "
            "operator to change the host service runtime only when no configured recovery path is "
            "available."
        )


def host_runtime_requirement_issue(
    profile: str,
    runtime_name: str,
    *,
    configured_binary: str = "",
) -> RuntimeRequirementIssue | None:
    for requirement in host_runtime_requirements_for(profile, runtime_name):
        if configured_binary and requirement.get("use_configured_binary"):
            requirement = {**requirement, "binary": configured_binary}
        issue = _evaluate_requirement(requirement, profile=profile, runtime_name=runtime_name)
        if issue is not None:
            return issue
    return None


def host_runtime_requirements_for(profile: str, runtime_name: str) -> list[dict[str, Any]]:
    profile = str(profile or "").strip()
    runtime_name = str(runtime_name or "").strip()
    payload = _load_requirements_payload()
    requirements: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in (profile, runtime_name, "*"):
            value = payload.get(key)
            if isinstance(value, dict):
                requirements.append(value)
            elif isinstance(value, list):
                requirements.extend(item for item in value if isinstance(item, dict))
        shared = payload.get("requirements")
        if isinstance(shared, list):
            for item in shared:
                if not isinstance(item, dict):
                    continue
                profiles = _csv_or_list(item.get("profiles") or item.get("profile") or item.get("worker_profiles"))
                runtimes = _csv_or_list(item.get("runtimes") or item.get("runtime") or item.get("runtime_names"))
                if profiles and profile not in profiles:
                    continue
                if runtimes and runtime_name not in runtimes:
                    continue
                requirements.append(item)
    elif isinstance(payload, list):
        requirements.extend(item for item in payload if isinstance(item, dict))
    return [_normalize_requirement(item) for item in requirements]


def _load_requirements_payload() -> Any:
    for env_name in ("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE", "WPR_HOST_RUNTIME_REQUIREMENTS_FILE"):
        path = os.environ.get(env_name, "").strip()
        if not path:
            continue
        source = Path(path).expanduser()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeRequirementConfigurationError(
                f"{env_name} does not point to a readable valid JSON requirements file"
            ) from exc
        if not isinstance(payload, (dict, list)):
            raise RuntimeRequirementConfigurationError(
                f"{env_name} must contain a JSON object or array"
            )
        return payload
    for env_name in ("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", "WPR_HOST_RUNTIME_REQUIREMENTS_JSON"):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeRequirementConfigurationError(
                f"{env_name} must contain valid JSON"
            ) from exc
        if not isinstance(payload, (dict, list)):
            raise RuntimeRequirementConfigurationError(
                f"{env_name} must contain a JSON object or array"
            )
        return payload
    return DEFAULT_HOST_RUNTIME_REQUIREMENTS


def _normalize_requirement(requirement: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(requirement)
    label = str(normalized.get("label") or normalized.get("name") or normalized.get("binary") or "").strip()
    if label:
        normalized["label"] = label
    min_version = str(
        normalized.get("min_version")
        or normalized.get("minimum_version")
        or normalized.get("version_at_least")
        or ""
    ).strip()
    if min_version:
        normalized["min_version"] = min_version
    exact_version = str(normalized.get("exact_version") or normalized.get("required_version") or "").strip()
    if exact_version:
        normalized["exact_version"] = exact_version
    return normalized


def _evaluate_requirement(
    requirement: dict[str, Any],
    *,
    profile: str,
    runtime_name: str,
) -> RuntimeRequirementIssue | None:
    binary = str(requirement.get("binary") or requirement.get("command") or "").strip()
    if not binary:
        return None
    label = str(requirement.get("label") or binary).strip()
    recovery_hint = str(requirement.get("recovery_hint") or "").strip()
    resolved = shutil.which(binary)
    if not resolved:
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="missing",
            diagnostic_summary=f"Missing host runtime dependency {label} ({binary})",
            recovery_hint=recovery_hint,
        )

    exact_version = str(requirement.get("exact_version") or "").strip()
    min_version = str(requirement.get("min_version") or "").strip()
    required_version = exact_version or min_version
    if not required_version:
        return _evaluate_capability_requirements(
            requirement,
            resolved_binary=resolved,
            binary=binary,
            label=label,
            profile=profile,
            runtime_name=runtime_name,
            recovery_hint=recovery_hint,
        )

    args = _version_args(requirement)
    try:
        completed = subprocess.run(
            [resolved, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(requirement.get("version_timeout_sec") or 5),
        )
    except Exception as exc:
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="version_unverified",
            required_version=required_version,
            diagnostic_summary=f"Could not verify {label} version: {type(exc).__name__}",
            recovery_hint=recovery_hint,
        )

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    actual_version = _extract_version(output)
    if completed.returncode != 0 or not actual_version:
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="version_unverified",
            required_version=required_version,
            actual_version=actual_version,
            diagnostic_summary=_truncate(output or f"{label} version command exited {completed.returncode}"),
            recovery_hint=recovery_hint,
        )
    if exact_version and actual_version != exact_version:
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="version_mismatch_exact",
            required_version=exact_version,
            actual_version=actual_version,
            diagnostic_summary=f"{label} version {actual_version} differs from reviewed {exact_version}",
            recovery_hint=recovery_hint,
        )
    if min_version and _version_tuple(actual_version) < _version_tuple(min_version):
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="version_mismatch",
            required_version=min_version,
            actual_version=actual_version,
            diagnostic_summary=f"{label} version {actual_version} is below required {min_version}",
            recovery_hint=recovery_hint,
        )
    return _evaluate_capability_requirements(
        requirement,
        resolved_binary=resolved,
        binary=binary,
        label=label,
        profile=profile,
        runtime_name=runtime_name,
        recovery_hint=recovery_hint,
    )


def _evaluate_capability_requirements(
    requirement: dict[str, Any],
    *,
    resolved_binary: str,
    binary: str,
    label: str,
    profile: str,
    runtime_name: str,
    recovery_hint: str,
) -> RuntimeRequirementIssue | None:
    help_contains = _csv_or_list(
        requirement.get("help_contains")
        or requirement.get("required_help_contains")
        or requirement.get("required_help_flags")
    )
    if help_contains:
        args = _args_for(requirement, "help_args", default=["--help"])
        issue = _run_requirement_probe(
            [resolved_binary, *args],
            required=help_contains,
            binary=binary,
            label=label,
            profile=profile,
            runtime_name=runtime_name,
            recovery_hint=recovery_hint,
            probe_label="help output",
        )
        if issue is not None:
            return issue

    mcp_servers = _csv_or_list(requirement.get("required_mcp_servers") or requirement.get("mcp_servers"))
    if mcp_servers:
        args = _args_for(requirement, "mcp_list_args", default=["mcp", "list"])
        issue = _run_requirement_probe(
            [resolved_binary, *args],
            required=mcp_servers,
            binary=binary,
            label=label,
            profile=profile,
            runtime_name=runtime_name,
            recovery_hint=recovery_hint,
            probe_label="MCP server list",
        )
        if issue is not None:
            return issue

    return None


def _args_for(requirement: dict[str, Any], key: str, *, default: list[str]) -> list[str]:
    value = requirement.get(key)
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(default)


def _run_requirement_probe(
    command: list[str],
    *,
    required: set[str],
    binary: str,
    label: str,
    profile: str,
    runtime_name: str,
    recovery_hint: str,
    probe_label: str,
) -> RuntimeRequirementIssue | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="capability_unverified",
            diagnostic_summary=f"Could not verify {label} {probe_label}: {type(exc).__name__}",
            recovery_hint=recovery_hint,
        )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    lowered = output.lower()
    missing = sorted(item for item in required if item.lower() not in lowered)
    if completed.returncode != 0 or missing:
        detail = f"missing required values: {', '.join(missing)}" if missing else f"exited {completed.returncode}"
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="capability_missing",
            diagnostic_summary=_truncate(f"{label} {probe_label} {detail}. {output}"),
            recovery_hint=recovery_hint,
        )
    return None


def _version_args(requirement: dict[str, Any]) -> list[str]:
    value = requirement.get("version_args")
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    value = requirement.get("version_arg")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return ["--version"]


def _csv_or_list(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _extract_version(text: str) -> str:
    match = re.search(r"\bv?(\d+(?:\.\d+){0,3}(?:-[0-9A-Za-z.-]+)?)\b", str(text or ""))
    return match.group(1) if match else ""


def _version_tuple(value: str) -> tuple[int, int, int, int]:
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:4]]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _label_for_binary(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "runtime"
    return clean.replace("\\", "/").rstrip("/").split("/")[-1] if "/" in clean else clean


def _truncate(value: str, *, limit: int = 600) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."
