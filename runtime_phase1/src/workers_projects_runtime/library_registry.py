from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import tomllib
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse


LIBRARY_PROFILES = {"codex-cli", "claude-code", "grok-build"}
LIBRARY_STATUSES = {"available", "disabled", "removed"}
LIBRARY_BUNDLE_KEYS = {
    "claude_project_mcp",
    "codex_config_append",
    "files",
}
PROFILE_BUNDLE_KEYS = {
    "codex-cli": {"codex_config_append", "files"},
    "claude-code": {"claude_project_mcp", "files"},
    "grok-build": {"claude_project_mcp", "files"},
}
_STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,199}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SCOPE = re.compile(r"^[a-z][a-z0-9._-]{0,63}:[a-z][a-z0-9._-]{0,63}$")
_MCP_SERVER = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SECRET_MARKERS = (
    "access_key",
    "api_key",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session_token",
)


class LibraryManifestError(ValueError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def library_content_hash(activation: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(activation).encode("utf-8")).hexdigest()


def compare_semantic_versions(left: str, right: str) -> int:
    def parse(value: str) -> tuple[tuple[int, int, int], list[str] | None]:
        main, separator, prerelease = str(value).partition("-")
        return tuple(int(part) for part in main.split(".")), prerelease.split(".") if separator else None

    left_main, left_pre = parse(left)
    right_main, right_pre = parse(right)
    if left_main != right_main:
        return 1 if left_main > right_main else -1
    if left_pre is None or right_pre is None:
        if left_pre is right_pre:
            return 0
        return 1 if left_pre is None else -1
    for left_part, right_part in zip(left_pre, right_pre):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_part) > int(right_part) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_part > right_part else -1
    if len(left_pre) == len(right_pre):
        return 0
    return 1 if len(left_pre) > len(right_pre) else -1


def _clean_strings(values: object, *, label: str, maximum: int = 64) -> list[str]:
    if not isinstance(values, list) or not values:
        raise LibraryManifestError(f"Library {label} must be a non-empty list")
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or len(text) > maximum or text in result:
            raise LibraryManifestError(f"Library {label} contains an invalid or duplicate value")
        result.append(text)
    return result


def _contains_secret_shape(value: object, *, parent_key: str = "") -> bool:
    if any(marker in parent_key.lower() for marker in _SECRET_MARKERS):
        return True
    if isinstance(value, dict):
        return any(_contains_secret_shape(item, parent_key=str(key)) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret_shape(item, parent_key=parent_key) for item in value)
    return False


def _contains_high_confidence_credential(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_high_confidence_credential(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_high_confidence_credential(item) for item in value)
    if not isinstance(value, str):
        return False
    patterns = (
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_-]{16,}\b",
        r"\bghp_[A-Za-z0-9]{20,}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b",
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9+/_.~-]{16,}",
    )
    return any(re.search(pattern, value) for pattern in patterns)


def _validate_configuration_schema(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("type") != "object":
        raise LibraryManifestError("Library configuration_schema must be a JSON object schema")
    if value.get("additionalProperties") is not False:
        raise LibraryManifestError("Library configuration_schema must reject undeclared properties")
    properties = value.get("properties", {})
    if not isinstance(properties, dict):
        raise LibraryManifestError("Library configuration_schema properties must be an object")
    if _contains_secret_shape(properties):
        raise LibraryManifestError("Library configuration_schema must not describe credentials or secrets")
    encoded = _canonical(value).encode("utf-8")
    if len(encoded) > 32 * 1024:
        raise LibraryManifestError("Library configuration_schema exceeds the size limit")
    return value


def _validate_provenance(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise LibraryManifestError("Library provenance must be structured")
    source = str(value.get("source") or "").strip()
    publisher = str(value.get("publisher") or "").strip()
    revision = str(value.get("revision") or "").strip()
    try:
        parsed = urlparse(source)
    except ValueError as exc:
        raise LibraryManifestError("Library provenance source is invalid") from exc
    if (
        parsed.scheme not in {"https", "curated"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise LibraryManifestError("Library provenance source must be an HTTPS or curated URI")
    if not publisher or len(publisher) > 160 or not revision or len(revision) > 200:
        raise LibraryManifestError("Library provenance requires bounded publisher and revision values")
    return {"source": source, "publisher": publisher, "revision": revision}


def _validate_file_entry(entry: object) -> None:
    if not isinstance(entry, dict):
        raise LibraryManifestError("Library bootstrap files must be objects")
    if set(entry) - {"scope", "path", "content", "allow_empty"}:
        raise LibraryManifestError("Library bootstrap files may contain only inline non-secret content")
    if str(entry.get("scope") or "workspace").strip().lower() != "workspace":
        raise LibraryManifestError("Library bootstrap files must remain workspace scoped")
    path = str(entry.get("path") or "").strip()
    candidate = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or len(path) > 240
    ):
        raise LibraryManifestError("Library bootstrap file path must be a safe relative path")
    reserved = {
        ".env",
        ".mcp.json",
        ".claude/settings.local.json",
        ".codex/config.toml",
        "agents.md",
        "claude.md",
        "codex.md",
    }
    if path.casefold() in reserved or candidate.name.casefold() in {"agents.md", "claude.md", "codex.md"}:
        raise LibraryManifestError("Library bootstrap file cannot replace worker authority or credential config")
    if "content" not in entry or not isinstance(entry.get("content"), str):
        raise LibraryManifestError("Library bootstrap files require inline UTF-8 text content")
    if len(str(entry["content"]).encode("utf-8")) > 1024 * 1024:
        raise LibraryManifestError("Library bootstrap file exceeds the size limit")


def _activation_bundle_for_profile(activation: dict[str, Any], profile: str) -> dict[str, Any]:
    common = activation.get("bundle")
    profiles = activation.get("profiles")
    if common is not None and profiles is not None:
        raise LibraryManifestError("Library activation must use one common bundle or profile bundles")
    if profiles is not None:
        if not isinstance(profiles, dict) or set(profiles) - LIBRARY_PROFILES:
            raise LibraryManifestError("Library activation profiles are invalid")
        bundle = profiles.get(profile)
    else:
        bundle = common
    if not isinstance(bundle, dict) or not bundle:
        raise LibraryManifestError(f"Library activation has no bundle for profile {profile}")
    return bundle


def _validate_bundle(bundle: dict[str, Any], *, profile: str) -> None:
    unsupported = set(bundle) - LIBRARY_BUNDLE_KEYS
    profile_unsupported = set(bundle) - PROFILE_BUNDLE_KEYS[profile]
    if unsupported or profile_unsupported:
        raise LibraryManifestError(f"Library activation contains unsupported {profile} bootstrap fields")
    if _contains_secret_shape(bundle):
        raise LibraryManifestError("Library activation must not contain credentials or secrets")
    if _contains_high_confidence_credential(bundle):
        raise LibraryManifestError("Library activation contains credential-shaped content")
    files = bundle.get("files", [])
    if files:
        if not isinstance(files, list) or len(files) > 128:
            raise LibraryManifestError("Library activation files are invalid")
        seen_paths: set[str] = set()
        for entry in files:
            _validate_file_entry(entry)
            path = str(entry.get("path") or "").casefold()
            if path in seen_paths:
                raise LibraryManifestError("Library activation contains duplicate bootstrap file paths")
            seen_paths.add(path)
    if "codex_config_append" in bundle:
        raw = bundle.get("codex_config_append")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64 * 1024:
            raise LibraryManifestError("Library Codex MCP configuration is invalid")
        try:
            parsed = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            raise LibraryManifestError("Library Codex MCP configuration must be valid TOML") from exc
        if set(parsed) != {"mcp_servers"} or not isinstance(parsed.get("mcp_servers"), dict):
            raise LibraryManifestError("Library Codex configuration may declare only remote MCP servers")
        for server, config in parsed["mcp_servers"].items():
            if not _MCP_SERVER.fullmatch(str(server)) or not isinstance(config, dict):
                raise LibraryManifestError("Library Codex MCP server configuration is invalid")
            if set(config) - {"url", "enabled", "startup_timeout_sec", "tool_timeout_sec"}:
                raise LibraryManifestError("Library Codex MCP server may contain only reviewed remote fields")
            _validate_remote_mcp_url(config.get("url"))
            if "enabled" in config and not isinstance(config["enabled"], bool):
                raise LibraryManifestError("Library Codex MCP enabled flag must be boolean")
            for field, maximum in (("startup_timeout_sec", 300), ("tool_timeout_sec", 3600)):
                if field in config and (
                    isinstance(config[field], bool)
                    or not isinstance(config[field], (int, float))
                    or not 1 <= float(config[field]) <= maximum
                ):
                    raise LibraryManifestError(f"Library Codex MCP {field} is outside the safe range")
    if "claude_project_mcp" in bundle:
        raw = bundle.get("claude_project_mcp")
        if not isinstance(raw, dict):
            raise LibraryManifestError("Library Claude MCP configuration is invalid")
        servers = raw.get("mcpServers", raw)
        if not isinstance(servers, dict) or not servers:
            raise LibraryManifestError("Library Claude MCP configuration requires remote servers")
        for server, config in servers.items():
            if not _MCP_SERVER.fullmatch(str(server)) or not isinstance(config, dict):
                raise LibraryManifestError("Library Claude MCP server configuration is invalid")
            if set(config) - {"type", "url"} or str(config.get("type") or "http") not in {"http", "sse"}:
                raise LibraryManifestError("Library Claude MCP server may contain only reviewed remote fields")
            _validate_remote_mcp_url(config.get("url"))
    if len(_canonical(bundle).encode("utf-8")) > 4 * 1024 * 1024:
        raise LibraryManifestError("Library activation exceeds the size limit")


def _validate_remote_mcp_url(value: object) -> None:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError as exc:
        raise LibraryManifestError("Library remote MCP URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise LibraryManifestError("Library MCP adapters require a credential-free HTTPS URL")
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if (
        hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa"))
    ):
        raise LibraryManifestError("Library MCP adapters cannot target local or link-private hosts")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise LibraryManifestError("Library MCP adapters cannot target local or link-private hosts")


def _validate_probe(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("type") != "bootstrap_contract":
        raise LibraryManifestError("Library health_probe must use the bootstrap_contract adapter")
    allowed = {"type", "required_files", "required_bundle_keys", "required_mcp_servers"}
    if set(value) - allowed:
        raise LibraryManifestError("Library health_probe contains unsupported fields")
    required_files = [str(item or "").strip() for item in value.get("required_files", [])]
    required_keys = [str(item or "").strip() for item in value.get("required_bundle_keys", [])]
    required_servers = [str(item or "").strip() for item in value.get("required_mcp_servers", [])]
    if any(not item or len(item) > 240 for item in required_files):
        raise LibraryManifestError("Library health_probe required_files are invalid")
    if any(item not in LIBRARY_BUNDLE_KEYS for item in required_keys):
        raise LibraryManifestError("Library health_probe required_bundle_keys are invalid")
    if any(not _MCP_SERVER.fullmatch(item) for item in required_servers):
        raise LibraryManifestError("Library health_probe required_mcp_servers are invalid")
    if not (required_files or required_keys or required_servers):
        raise LibraryManifestError("Library health_probe must verify at least one installed capability")
    return {
        "type": "bootstrap_contract",
        "required_files": sorted(set(required_files)),
        "required_bundle_keys": sorted(set(required_keys)),
        "required_mcp_servers": sorted(set(required_servers)),
    }


def validate_library_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise LibraryManifestError("Library manifest must be an object")
    required = {
        "schema_version",
        "stable_id",
        "version",
        "content_hash",
        "provenance",
        "supported_profiles",
        "requested_scopes",
        "configuration_schema",
        "dependencies",
        "health_probe",
        "lifecycle",
        "activation",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise LibraryManifestError("Library manifest is missing required fields: " + ", ".join(missing))
    allowed_manifest_fields = required | {"description", "homepage", "kind", "label", "license", "name"}
    if set(manifest) - allowed_manifest_fields:
        raise LibraryManifestError("Library manifest contains unsupported or potentially secret fields")
    if manifest.get("schema_version") != 1:
        raise LibraryManifestError("Library manifest schema_version must be 1")
    stable_id = str(manifest.get("stable_id") or "").strip().lower()
    version = str(manifest.get("version") or "").strip()
    if not _STABLE_ID.fullmatch(stable_id):
        raise LibraryManifestError("Library stable_id is invalid")
    if not _VERSION.fullmatch(version):
        raise LibraryManifestError("Library version must be semantic versioning")
    profiles = _clean_strings(manifest.get("supported_profiles"), label="supported_profiles")
    if not set(profiles).issubset(LIBRARY_PROFILES):
        raise LibraryManifestError("Library supported_profiles contains an unsupported worker profile")
    raw_scopes = manifest.get("requested_scopes")
    if not isinstance(raw_scopes, list):
        raise LibraryManifestError("Library requested_scopes must be a list")
    scopes = []
    for value in raw_scopes:
        text = str(value or "").strip()
        if not text or len(text) > 129 or text in scopes:
            raise LibraryManifestError("Library requested_scopes contains an invalid or duplicate value")
        scopes.append(text)
    if any(not _SCOPE.fullmatch(scope) for scope in scopes):
        raise LibraryManifestError("Library requested_scopes are invalid")
    provenance = _validate_provenance(manifest.get("provenance"))
    configuration_schema = _validate_configuration_schema(manifest.get("configuration_schema"))
    activation = manifest.get("activation")
    if not isinstance(activation, dict) or activation.get("type") != "bootstrap_bundle":
        raise LibraryManifestError("Library activation must use a bootstrap_bundle profile adapter")
    if set(activation) - {"type", "bundle", "profiles"}:
        raise LibraryManifestError("Library activation contains unsupported fields")
    for profile in profiles:
        _validate_bundle(_activation_bundle_for_profile(activation, profile), profile=profile)
    expected_hash = library_content_hash(activation)
    if str(manifest.get("content_hash") or "").strip().lower() != expected_hash:
        raise LibraryManifestError("Library content_hash does not match the reviewed activation content")
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, list) or len(dependencies) > 32:
        raise LibraryManifestError("Library dependencies must be a bounded list")
    normalized_dependencies: list[dict[str, Any]] = []
    dependency_ids: set[str] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"stable_id", "version", "content_hash", "scopes"}:
            raise LibraryManifestError("Library dependencies must pin stable_id, version, content_hash, and scopes")
        dependency_id = str(dependency.get("stable_id") or "").strip().lower()
        dependency_version = str(dependency.get("version") or "").strip()
        dependency_hash = str(dependency.get("content_hash") or "").strip().lower()
        dependency_scopes = dependency.get("scopes")
        if not _STABLE_ID.fullmatch(dependency_id) or not _VERSION.fullmatch(dependency_version):
            raise LibraryManifestError("Library dependency identity is invalid")
        if dependency_id == stable_id or dependency_id in dependency_ids:
            raise LibraryManifestError("Library dependencies cannot be self-referential or duplicated")
        dependency_ids.add(dependency_id)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", dependency_hash):
            raise LibraryManifestError("Library dependency content_hash is invalid")
        if not isinstance(dependency_scopes, list) or any(not _SCOPE.fullmatch(str(scope)) for scope in dependency_scopes):
            raise LibraryManifestError("Library dependency scopes are invalid")
        normalized_dependencies.append(
            {
                "stable_id": dependency_id,
                "version": dependency_version,
                "content_hash": dependency_hash,
                "scopes": sorted(set(str(scope) for scope in dependency_scopes)),
            }
        )
    lifecycle = manifest.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle != {
        "upgrade": "replace_same_stable_id",
        "remove": "restore_prior_bundle",
    }:
        raise LibraryManifestError("Library lifecycle must declare safe upgrade and removal behavior")
    probe = _validate_probe(manifest.get("health_probe"))
    normalized = dict(manifest)
    normalized.update(
        {
            "schema_version": 1,
            "stable_id": stable_id,
            "version": version,
            "content_hash": expected_hash,
            "provenance": provenance,
            "supported_profiles": sorted(profiles),
            "requested_scopes": sorted(scopes),
            "configuration_schema": configuration_schema,
            "dependencies": normalized_dependencies,
            "health_probe": probe,
            "lifecycle": lifecycle,
            "activation": activation,
        }
    )
    if _contains_high_confidence_credential(normalized):
        raise LibraryManifestError("Library manifest contains credential-shaped content")
    if len(_canonical(normalized).encode("utf-8")) > 5 * 1024 * 1024:
        raise LibraryManifestError("Library manifest exceeds the size limit")
    return normalized


def activation_bundle_for_profile(manifest: dict[str, Any], profile: str) -> dict[str, Any]:
    normalized = validate_library_manifest(manifest)
    if profile not in normalized["supported_profiles"]:
        raise LibraryManifestError("Library item is incompatible with this workspace profile")
    return _activation_bundle_for_profile(normalized["activation"], profile)


def probe_activation(manifest: dict[str, Any], *, profile: str, merged_bundle: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_library_manifest(manifest)
    probe = normalized["health_probe"]
    files = {
        str(entry.get("path") or "").strip()
        for entry in merged_bundle.get("files", [])
        if isinstance(entry, dict)
    }
    missing_files = sorted(set(probe["required_files"]) - files)
    missing_keys = sorted(key for key in probe["required_bundle_keys"] if key not in merged_bundle)
    missing_servers: list[str] = []
    for server in probe["required_mcp_servers"]:
        if profile in {"claude-code", "grok-build"}:
            raw = merged_bundle.get("claude_project_mcp")
            servers = raw.get("mcpServers", raw) if isinstance(raw, dict) else {}
            if not isinstance(servers, dict) or server not in servers:
                missing_servers.append(server)
        else:
            config = str(merged_bundle.get("codex_config_append") or "")
            if f"[mcp_servers.{server}]" not in config and f'[mcp_servers."{server}"]' not in config:
                missing_servers.append(server)
    if missing_files or missing_keys or missing_servers:
        missing = missing_files + missing_keys + missing_servers
        raise LibraryManifestError("Library health probe failed for: " + ", ".join(missing))
    return {
        "status": "healthy",
        "adapter": f"bootstrap_bundle:{profile}",
        "checked_files": len(probe["required_files"]),
        "checked_bundle_keys": len(probe["required_bundle_keys"]),
        "checked_mcp_servers": len(probe["required_mcp_servers"]),
    }
