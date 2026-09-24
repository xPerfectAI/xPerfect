from __future__ import annotations

from typing import Any

from workers_projects_runtime.library_registry import library_content_hash


def library_manifest(
    *,
    stable_id: str,
    version: str = "1.0.0",
    profiles: list[str] | None = None,
    scopes: list[str] | None = None,
    files: list[dict[str, Any]] | None = None,
    dependencies: list[dict[str, Any]] | None = None,
    label: str = "Synthetic Library item",
) -> dict[str, Any]:
    activation = {
        "type": "bootstrap_bundle",
        "bundle": {"files": files or [{"path": "SKILL.md", "content": "synthetic"}]},
    }
    required_files = [str(item["path"]) for item in activation["bundle"]["files"]]
    return {
        "schema_version": 1,
        "stable_id": stable_id,
        "version": version,
        "content_hash": library_content_hash(activation),
        "provenance": {
            "source": "curated://glasshive",
            "publisher": "GlassHive test registry",
            "revision": f"synthetic-{stable_id}-{version}",
        },
        "supported_profiles": profiles or ["codex-cli"],
        "requested_scopes": scopes or [],
        "configuration_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "dependencies": dependencies or [],
        "health_probe": {
            "type": "bootstrap_contract",
            "required_files": required_files,
        },
        "lifecycle": {
            "upgrade": "replace_same_stable_id",
            "remove": "restore_prior_bundle",
        },
        "activation": activation,
        "label": label,
    }


def register_manifest(store, manifest: dict[str, Any]):
    return store.register_library_item(
        stable_id=manifest["stable_id"],
        version=manifest["version"],
        content_hash=manifest["content_hash"],
        provenance=manifest["provenance"]["source"],
        supported_profiles=manifest["supported_profiles"],
        scopes=manifest["requested_scopes"],
        manifest=manifest,
    )
