"""Private native selectors, supplied trusted placement by the runtime owner.

The common cwd is never an output target. Existing shared project instructions remain
native project context. Generated member instructions use each harness's private channel.
"""

from __future__ import annotations

import json
from pathlib import Path

from .bootstrap import (
    _write_private_native_file,
    _read_private_native_file,
    _write_codex_config,
    claude_project_mcp_payload_for_bundle,
    glasshive_project_claude_md,
    glasshive_project_agents_md,
)


def materialize_native_context(
    placement: dict, bundle: dict, *, projection: dict | None = None
) -> dict:
    home = Path(placement["home_dir"])
    native_home = Path(placement["native_home"])
    private = home / ".glasshive" / "native"
    native_private = native_home / ".glasshive" / "native"
    # Private replacement never truncates an existing (possibly hard-linked) inode.
    mcp = claude_project_mcp_payload_for_bundle(
        bundle, bundle.get("claude_project_mcp") or {}
    )
    settings = dict(bundle.get("claude_settings_local") or {})
    settings["autoMemoryDirectory"] = str(native_private / "memory")
    authority = "\n\n".join(
        str(v)
        for v in (
            glasshive_project_agents_md(bundle),
            bundle.get("developer_instructions"),
        )
        if v
    )
    values = {
        "mcp.json": json.dumps(mcp, ensure_ascii=False),
        "settings.json": json.dumps(settings, ensure_ascii=False),
        "claude-instructions.md": authority
        + "\n\n"
        + glasshive_project_claude_md(bundle),
        "codex-instructions.md": authority,
        "context.json": json.dumps(projection or {}, ensure_ascii=False),
    }
    _write_private_native_file(home, Path(".grok/AGENTS.md"), authority.encode())
    for name, text in values.items():
        _write_private_native_file(
            home, Path(".glasshive/native") / name, text.encode()
        )
    codex_path = home / ".codex/config.toml"
    if (home / ".codex").is_symlink() or codex_path.is_symlink():
        raise PermissionError(
            "Private native configuration must not use symbolic links"
        )
    from .worker_configuration import configured_connections, restrict_codex_connections

    # Track only runtime-managed declarations. Unknown native/app/plugin entries are
    # outside this selector's authority and are never reported as disabled.
    registry = private / "managed-mcp.json"
    if registry.is_symlink():
        raise PermissionError("Private configuration must not use symbolic links")
    registry_text = _read_private_native_file(
        home, Path(".glasshive/native/managed-mcp.json")
    )
    previous = set(json.loads(registry_text)) if registry_text else set()
    current = set(configured_connections(bundle))
    removed = set(bundle.get("_configured_removed_mcp_servers") or []) | (
        previous - current
    )
    if removed and codex_path.exists():
        if codex_path.is_symlink():
            raise PermissionError("Private configuration must not be a symbolic link")
        _write_private_native_file(
            home,
            Path(".codex/config.toml"),
            restrict_codex_connections(
                _read_private_native_file(home, Path(".codex/config.toml")), removed
            ).encode(),
        )
    _write_codex_config(home, bundle, private=True)
    from .profile_runtime import _apply_codex_developer_instructions

    existing = _read_private_native_file(home, Path(".codex/config.toml"))
    _write_private_native_file(
        home,
        Path(".codex/config.toml"),
        str(_apply_codex_developer_instructions(existing, authority)).encode(),
    )
    _write_private_native_file(
        home,
        Path(".glasshive/native/managed-mcp.json"),
        json.dumps(sorted(current)).encode(),
    )
    return {
        "settings_data": settings,
        "host_root": str(private),
        "native_root": str(native_private),
        "mcp_config": str(native_private / "mcp.json"),
        "settings": str(native_private / "settings.json"),
        "claude_instructions": str(native_private / "claude-instructions.md"),
        "codex_instructions": str(native_private / "codex-instructions.md"),
        "context_manifest": str(native_private / "context.json"),
        "claude_config_dir": str(native_home / ".claude"),
        "codex_home": str(native_home / ".codex"),
    }


def apply_claude_context(command: list[str], selectors: dict) -> list[str]:
    """Replace existing selectors, preserving all other native model/control arguments."""
    output = []
    index = 0
    effective_settings = dict(selectors["settings_data"])
    if "--settings" in command:
        value = command[command.index("--settings") + 1]
        try:
            overrides = json.loads(value)
        except json.JSONDecodeError:
            overrides = json.loads(Path(value).read_text())
        if not isinstance(overrides, dict):
            raise ValueError("Native settings must be an object")
        effective_settings.update(overrides)
        effective_settings["autoMemoryDirectory"] = selectors["settings_data"][
            "autoMemoryDirectory"
        ]
    replaced = {"--mcp-config", "--settings", "--append-system-prompt-file"}
    while index < len(command):
        if command[index] in replaced:
            index += 2
        elif command[index] == "--strict-mcp-config":
            index += 1
        else:
            output.append(command[index])
            index += 1
    output.extend(
        [
            "--mcp-config",
            selectors["mcp_config"],
            "--strict-mcp-config",
            "--settings",
            json.dumps(effective_settings, separators=(",", ":")),
            "--append-system-prompt-file",
            selectors["claude_instructions"],
        ]
    )
    return output


def private_refresh(
    home_dir: Path, workspace_dir: Path, worker: dict, bundle: dict
) -> bool:
    """True means the typed native placement owns all generated per-member configuration."""
    placement = worker.get("_native_context_placement")
    if not placement:
        return False
    from .bootstrap import _write_claude_project_files

    # Legacy direct-authority validators inspect these same private files. Shared cwd
    # stays unchanged, and native CLI selectors point at private home files.
    _write_claude_project_files(
        Path(placement["private_project_dir"]), bundle, private=True,
        grok_acp=str(worker.get("profile") or "") == "grok-build",
    )
    worker["_native_context_selectors"] = materialize_native_context(
        placement, bundle, projection=worker.get("_context_projection")
    )
    return True


def prepare_private_command(runtime, worker: dict, bundle: dict) -> dict | None:
    placement = worker.get("_native_context_placement")
    if not placement:
        return None
    selectors = worker.get("_native_context_selectors")
    if selectors is None:
        private_refresh(
            Path(placement["home_dir"]),
            Path(placement["workspace_dir"]),
            worker,
            bundle,
        )
        selectors = worker["_native_context_selectors"]
    return selectors
