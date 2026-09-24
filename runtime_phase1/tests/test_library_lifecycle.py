from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from library_test_support import library_manifest, register_manifest
from workers_projects_runtime.api import create_app
from workers_projects_runtime.control_plane import (
    CONTROL_PLANE_SCHEMA_VERSION,
    ControlPlaneConflict,
    ControlPlaneError,
    ControlPlaneStore,
)
from workers_projects_runtime.schema_version import record_schema_version, require_compatible_schema


def _workspace_record(database, *, bootstrap: dict | None = None) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                bootstrap_bundle_json TEXT,
                duplication_report_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO workers
                (worker_id, tenant_id, owner_id, profile, bootstrap_bundle_json, updated_at)
            VALUES ('wrk_public_safe', 'tenant-a', 'user-a', 'codex-cli', ?, 1)
            """,
            (json.dumps(bootstrap or {}),),
        )


def _enable(store: ControlPlaneStore, library_id: str, *, change_type: str = "library_enable", **extra):
    pending = store.create_pending_change(
        tenant_id="tenant-a",
        owner_id="user-a",
        change_type=change_type,
        target_id="wrk_public_safe",
        payload={"library_id": library_id, **extra},
        ttl_seconds=300,
    )
    return store.confirm_pending_change(
        change_id=pending["change_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        confirmation_token=pending["confirmation_token"],
    )


def test_library_manifest_rejects_missing_hash_secret_schema_and_unsafe_activation(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    missing = library_manifest(stable_id="skill.synthetic.missing")
    missing.pop("health_probe")
    with pytest.raises(ControlPlaneError, match="missing required fields"):
        store.publish_library_manifest(manifest=missing, published_by="operator")

    tampered = library_manifest(stable_id="skill.synthetic.tampered")
    tampered["activation"]["bundle"]["files"][0]["content"] = "changed after hashing"
    with pytest.raises(ControlPlaneError, match="content_hash"):
        store.publish_library_manifest(manifest=tampered, published_by="operator")

    secret_schema = library_manifest(stable_id="skill.synthetic.secret-schema")
    secret_schema["configuration_schema"]["properties"] = {"api_key": {"type": "string"}}
    with pytest.raises(ControlPlaneError, match="credentials or secrets"):
        store.publish_library_manifest(manifest=secret_schema, published_by="operator")

    unsafe = library_manifest(stable_id="skill.synthetic.unsafe")
    unsafe["activation"]["bundle"] = {"env": {"COMMAND": "run arbitrary shell"}}
    from workers_projects_runtime.library_registry import library_content_hash

    unsafe["content_hash"] = library_content_hash(unsafe["activation"])
    with pytest.raises(ControlPlaneError, match="unsupported codex-cli bootstrap fields"):
        store.publish_library_manifest(manifest=unsafe, published_by="operator")

    shell_adapter = library_manifest(stable_id="skill.synthetic.shell-adapter")
    shell_adapter["activation"]["bundle"] = {
        "codex_config_append": '[mcp_servers.unsafe]\ncommand = "sh"\nargs = ["-c", "do something"]',
    }
    shell_adapter["health_probe"] = {
        "type": "bootstrap_contract",
        "required_bundle_keys": ["codex_config_append"],
    }
    shell_adapter["content_hash"] = library_content_hash(shell_adapter["activation"])
    with pytest.raises(ControlPlaneError, match="reviewed remote fields"):
        store.publish_library_manifest(manifest=shell_adapter, published_by="operator")

    authority_replacement = library_manifest(
        stable_id="skill.synthetic.authority-replacement",
        files=[{"path": "AGENTS.md", "content": "override authority"}],
    )
    with pytest.raises(ControlPlaneError, match="worker authority"):
        store.publish_library_manifest(manifest=authority_replacement, published_by="operator")

    nested_authority = library_manifest(
        stable_id="skill.synthetic.nested-authority",
        files=[{"path": "nested/AGENTS.md", "content": "override nested authority"}],
    )
    with pytest.raises(ControlPlaneError, match="worker authority"):
        store.publish_library_manifest(manifest=nested_authority, published_by="operator")

    embedded_credential = library_manifest(
        stable_id="skill.synthetic.embedded-credential",
        files=[{"path": "SKILL.md", "content": "api_key=sk-syntheticcredentialvalue12345"}],
    )
    with pytest.raises(ControlPlaneError, match="credential-shaped"):
        store.publish_library_manifest(manifest=embedded_credential, published_by="operator")

    duplicate_file = library_manifest(
        stable_id="skill.synthetic.duplicate-file",
        files=[
            {"path": "skill.md", "content": "one"},
            {"path": "SKILL.md", "content": "two"},
        ],
    )
    with pytest.raises(ControlPlaneError, match="duplicate bootstrap file"):
        store.publish_library_manifest(manifest=duplicate_file, published_by="operator")

    remote_connector = library_manifest(
        stable_id="connector.synthetic.remote",
        profiles=["codex-cli", "claude-code"],
        files=[{"path": "CONNECTOR.md", "content": "Authenticate through the native MCP client."}],
    )
    remote_connector["activation"] = {
        "type": "bootstrap_bundle",
        "profiles": {
            "codex-cli": {
                "codex_config_append": (
                    '[mcp_servers.synthetic]\nurl = "https://connector.example.invalid/mcp"'
                )
            },
            "claude-code": {
                "claude_project_mcp": {
                    "mcpServers": {
                        "synthetic": {
                            "type": "http",
                            "url": "https://connector.example.invalid/mcp",
                        }
                    }
                }
            },
        },
    }
    remote_connector["health_probe"] = {
        "type": "bootstrap_contract",
        "required_mcp_servers": ["synthetic"],
    }
    remote_connector["content_hash"] = library_content_hash(remote_connector["activation"])
    assert store.publish_library_manifest(
        manifest=remote_connector,
        published_by="operator",
    )["stable_id"] == "connector.synthetic.remote"

    local_connector = json.loads(json.dumps(remote_connector))
    local_connector["stable_id"] = "connector.synthetic.local-target"
    local_connector["activation"]["profiles"]["codex-cli"]["codex_config_append"] = (
        '[mcp_servers.synthetic]\nurl = "https://127.0.0.1/mcp"'
    )
    local_connector["activation"]["profiles"]["claude-code"]["claude_project_mcp"]["mcpServers"]["synthetic"]["url"] = (
        "https://127.0.0.1/mcp"
    )
    local_connector["content_hash"] = library_content_hash(local_connector["activation"])
    with pytest.raises(ControlPlaneError, match="local or link-private"):
        store.publish_library_manifest(manifest=local_connector, published_by="operator")


def test_control_plane_v1_database_migrates_library_lifecycle_audit_columns(tmp_path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as conn:
        require_compatible_schema(conn, component="control_plane", target_version=1)
        conn.execute(
            """
            CREATE TABLE control_plane_library (
                library_id TEXT PRIMARY KEY,
                stable_id TEXT NOT NULL,
                version TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                provenance TEXT NOT NULL,
                supported_profiles_json TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(stable_id, version, content_hash)
            )
            """
        )
        record_schema_version(conn, component="control_plane", version=1)
    ControlPlaneStore(str(database))
    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(control_plane_library)")}
        assert {"status_reason", "published_by", "status_updated_at"}.issubset(columns)
        assert (
            require_compatible_schema(
                conn,
                component="control_plane",
                target_version=CONTROL_PLANE_SCHEMA_VERSION,
            )
            == CONTROL_PLANE_SCHEMA_VERSION
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'control_plane_library_events'"
        ).fetchone() == (1,)


def test_dependency_adapter_probe_and_atomic_failure_rollback(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    _workspace_record(database, bootstrap={"project_definition": "Synthetic baseline"})
    dependency = register_manifest(
        store,
        library_manifest(
            stable_id="skill.synthetic.dependency-probe",
            files=[{"path": "DEPENDENCY.md", "content": "dependency"}],
        ),
    )
    root_manifest = library_manifest(
        stable_id="skill.synthetic.root-probe",
        files=[{"path": "SKILL.md", "content": "root"}],
        dependencies=[
            {
                "stable_id": dependency["stable_id"],
                "version": dependency["version"],
                "content_hash": dependency["content_hash"],
                "scopes": [],
            }
        ],
    )
    root = register_manifest(store, root_manifest)

    confirmed = _enable(store, root["library_id"])
    grant = confirmed["applied"]
    assert [item["stable_id"] for item in grant["installation_plan"]] == [
        "skill.synthetic.dependency-probe",
        "skill.synthetic.root-probe",
    ]
    assert grant["health_probe"]["status"] == "healthy"
    with sqlite3.connect(database) as conn:
        installed = json.loads(
            conn.execute(
                "SELECT bootstrap_bundle_json FROM workers WHERE worker_id = 'wrk_public_safe'"
            ).fetchone()[0]
        )
    assert {item["path"] for item in installed["files"]} == {"DEPENDENCY.md", "SKILL.md"}

    store.revoke_workspace_grant(
        grant_id=grant["grant_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        worker_id="wrk_public_safe",
    )
    store.update_library_status(
        library_id=root["library_id"],
        status="disabled",
        reason="Synthetic dependent lifecycle QA",
        actor_id="operator",
    )
    store.update_library_status(
        library_id=dependency["library_id"],
        status="disabled",
        reason="Synthetic dependency lifecycle QA",
        actor_id="operator",
    )
    with pytest.raises(ControlPlaneConflict, match="every pinned dependency"):
        store.update_library_status(
            library_id=root["library_id"],
            status="available",
            reason="",
            actor_id="operator",
        )
    store.update_library_status(
        library_id=dependency["library_id"],
        status="available",
        reason="",
        actor_id="operator",
    )
    store.update_library_status(
        library_id=root["library_id"],
        status="available",
        reason="",
        actor_id="operator",
    )
    failing_manifest = library_manifest(
        stable_id="skill.synthetic.probe-failure",
        files=[{"path": "CAPABILITY.md", "content": "candidate"}],
    )
    failing_manifest["health_probe"] = {
        "type": "bootstrap_contract",
        "required_files": ["MISSING-PREREQUISITE.md"],
    }
    failing = register_manifest(store, failing_manifest)
    pending = store.create_pending_change(
        tenant_id="tenant-a",
        owner_id="user-a",
        change_type="library_enable",
        target_id="wrk_public_safe",
        payload={"library_id": failing["library_id"]},
        ttl_seconds=300,
    )
    with pytest.raises(ControlPlaneError, match="health probe failed"):
        store.confirm_pending_change(
            change_id=pending["change_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            confirmation_token=pending["confirmation_token"],
        )
    assert store.get_pending_change(
        change_id=pending["change_id"], tenant_id="tenant-a", owner_id="user-a"
    )["status"] == "pending"
    assert store.list_workspace_grants(
        tenant_id="tenant-a", owner_id="user-a", worker_id="wrk_public_safe"
    ) == []
    with sqlite3.connect(database) as conn:
        rolled_back = json.loads(
            conn.execute(
                "SELECT bootstrap_bundle_json FROM workers WHERE worker_id = 'wrk_public_safe'"
            ).fetchone()[0]
        )
    assert rolled_back == {"project_definition": "Synthetic baseline"}


def test_library_upgrade_is_newer_same_identity_scope_narrow_and_removable(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    _workspace_record(database, bootstrap={"project_definition": "Synthetic baseline"})
    first = register_manifest(
        store,
        library_manifest(
            stable_id="skill.synthetic.upgrade",
            version="1.0.0",
            scopes=["documents:read"],
            files=[{"path": "SKILL.md", "content": "version one"}],
        ),
    )
    first_grant = _enable(store, first["library_id"])["applied"]
    assert store.workspace_capability_readiness(
        tenant_id="tenant-a", owner_id="user-a", worker_ids=["wrk_public_safe", "wrk_empty"]
    ) == {
        "wrk_public_safe": {"active_grants": 1, "unavailable_grants": 0, "readiness": "ready"},
        "wrk_empty": {"active_grants": 0, "unavailable_grants": 0, "readiness": "ready"},
    }
    with pytest.raises(ControlPlaneConflict, match="active workspace grant"):
        store.update_library_status(
            library_id=first["library_id"],
            status="disabled",
            reason="Synthetic active-grant check",
            actor_id="operator",
        )
    write_dependency = register_manifest(
        store,
        library_manifest(
            stable_id="connector.synthetic.write-dependency",
            scopes=["documents:write"],
            files=[{"path": "WRITE.md", "content": "synthetic dependency"}],
        ),
    )
    dependency_widening = register_manifest(
        store,
        library_manifest(
            stable_id="skill.synthetic.upgrade",
            version="1.5.0",
            scopes=["documents:read"],
            files=[{"path": "SKILL.md", "content": "dependency widening candidate"}],
            dependencies=[
                {
                    "stable_id": write_dependency["stable_id"],
                    "version": write_dependency["version"],
                    "content_hash": write_dependency["content_hash"],
                    "scopes": ["documents:write"],
                }
            ],
        ),
    )
    with pytest.raises(ControlPlaneConflict, match="dependency or workspace scopes"):
        store.create_pending_change(
            tenant_id="tenant-a",
            owner_id="user-a",
            change_type="library_upgrade",
            target_id="wrk_public_safe",
            payload={
                "library_id": dependency_widening["library_id"],
                "replaces_grant_id": first_grant["grant_id"],
                "scopes": ["documents:read"],
            },
            ttl_seconds=300,
        )
    wider = register_manifest(
        store,
        library_manifest(
            stable_id="skill.synthetic.upgrade",
            version="2.0.0",
            scopes=["documents:read", "documents:write"],
            files=[{"path": "SKILL.md", "content": "version two"}],
        ),
    )
    with pytest.raises(ControlPlaneConflict, match="cannot widen"):
        store.create_pending_change(
            tenant_id="tenant-a",
            owner_id="user-a",
            change_type="library_upgrade",
            target_id="wrk_public_safe",
            payload={
                "library_id": wider["library_id"],
                "replaces_grant_id": first_grant["grant_id"],
            },
            ttl_seconds=300,
        )
    upgraded = _enable(
        store,
        wider["library_id"],
        change_type="library_upgrade",
        replaces_grant_id=first_grant["grant_id"],
        scopes=["documents:read"],
    )["applied"]
    assert upgraded["upgrade"] is True
    assert upgraded["replaced_grant_id"] == first_grant["grant_id"]
    active = store.list_workspace_grants(
        tenant_id="tenant-a", owner_id="user-a", worker_id="wrk_public_safe"
    )
    assert [item["grant_id"] for item in active] == [upgraded["grant_id"]]
    with pytest.raises(ControlPlaneConflict, match="newer version"):
        store.create_pending_change(
            tenant_id="tenant-a",
            owner_id="user-a",
            change_type="library_upgrade",
            target_id="wrk_public_safe",
            payload={
                "library_id": first["library_id"],
                "replaces_grant_id": upgraded["grant_id"],
                "scopes": ["documents:read"],
            },
            ttl_seconds=300,
        )
    store.revoke_workspace_grant(
        grant_id=upgraded["grant_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        worker_id="wrk_public_safe",
    )
    with sqlite3.connect(database) as conn:
        restored = json.loads(
            conn.execute(
                "SELECT bootstrap_bundle_json FROM workers WHERE worker_id = 'wrk_public_safe'"
            ).fetchone()[0]
        )
    assert restored == {"project_definition": "Synthetic baseline"}


def test_admin_registry_publication_status_lifecycle_and_role_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-public-safe")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENABLE_ADMIN_API", "true")
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))
    base = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-public-safe",
        "X-Viventium-User-Id": "user-public-safe",
    }
    member = {**base, "X-Viventium-User-Role": "member"}
    admin = {**base, "X-Viventium-User-Role": "tenant_admin"}
    manifest = library_manifest(stable_id="skill.synthetic.admin-published")

    proposal = client.post("/v1/library/proposals", headers=member, json={"manifest": manifest})
    assert proposal.status_code == 201, proposal.text
    proposal_id = proposal.json()["proposal_id"]
    assert client.get("/v1/library/proposals", headers=member).json()["items"][0]["proposal_id"] == proposal_id
    denied = client.post(
        f"/v1/admin/library/proposals/{proposal_id}/review",
        headers=member,
        json={"action": "publish", "reason": ""},
    )
    assert denied.status_code == 403
    review_queue = client.get("/v1/admin/library/proposals", headers=admin)
    assert review_queue.json()["items"][0]["proposal_id"] == proposal_id
    published = client.post(
        f"/v1/admin/library/proposals/{proposal_id}/review",
        headers=admin,
        json={"action": "publish", "reason": "Reviewed synthetic proposal"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    library_id = published.json()["library"]["library_id"]
    disabled = client.patch(
        f"/v1/admin/library/{library_id}",
        headers=admin,
        json={"status": "disabled", "reason": "Synthetic lifecycle QA"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    restored = client.patch(
        f"/v1/admin/library/{library_id}",
        headers=admin,
        json={"status": "available", "reason": ""},
    )
    assert restored.status_code == 200
    removed = client.patch(
        f"/v1/admin/library/{library_id}",
        headers=admin,
        json={"status": "removed", "reason": "Synthetic retirement QA"},
    )
    assert removed.status_code == 200
    cannot_restore = client.patch(
        f"/v1/admin/library/{library_id}",
        headers=admin,
        json={"status": "available", "reason": ""},
    )
    assert cannot_restore.status_code == 409
    events = client.get(f"/v1/admin/library/{library_id}/events", headers=admin)
    assert [item["next_status"] for item in events.json()["items"]] == [
        "available",
        "disabled",
        "available",
        "removed",
    ]
