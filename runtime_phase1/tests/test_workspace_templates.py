from __future__ import annotations

import json
import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.control_plane import ControlPlaneStore
from library_test_support import library_manifest, register_manifest


def _workspace(client: TestClient, *, headers: dict[str, str] | None = None) -> dict:
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "owner_id": "ignored" if headers else "demo-owner",
            "title": "Synthetic source project",
            "goal": "Never copy /private/source/path from this source project.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    response = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored" if headers else "demo-owner",
            "name": "Reusable research desk",
            "role": "operator",
            "profile": "codex-cli",
            "execution_mode": "docker",
            "workspace_kind": "named",
            "start_synchronously": False,
            "tags": ["Research", "Synthetic"],
            "bootstrap_bundle": {
                "project_definition": "Prepare the reusable synthetic briefing.",
                "provider_account": {"account_id": "acct_must_not_copy"},
                "provider_home": "/private/provider/home",
                "files": [{"path": "private.txt", "content": "must-not-copy"}],
                "env": {"SYNTHETIC_SECRET": "must-not-copy"},
                "mcp": {"secret_locator": "secret-store://must-not-copy"},
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _library_with_dependency(database, client: TestClient, worker_id: str) -> tuple[dict, dict]:
    store = ControlPlaneStore(str(database))
    dependency = register_manifest(
        store,
        library_manifest(
            stable_id="skill.synthetic.dependency",
            scopes=["documents:read"],
            files=[{"path": "DEPENDENCY.md", "content": "synthetic"}],
            label="Synthetic dependency",
        ),
    )
    library = register_manifest(
        store,
        library_manifest(
            stable_id="skill.synthetic.template",
            version="2.1.0",
            scopes=["documents:read"],
            dependencies=[
                {
                    "stable_id": dependency["stable_id"],
                    "version": dependency["version"],
                    "content_hash": dependency["content_hash"],
                    "scopes": ["documents:read"],
                }
            ],
            label="Synthetic template skill",
        ),
    )
    prepared = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "library_enable",
            "target_id": worker_id,
            "payload": {"library_id": library["library_id"], "scopes": ["documents:read"]},
        },
    ).json()
    confirmed = client.post(
        f"/v1/pending-changes/{prepared['change_id']}/confirm",
        json={"confirmation_token": prepared["confirmation_token"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return library, dependency


def test_template_snapshot_is_immutable_sanitized_and_source_independent(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    source = _workspace(client)
    library, dependency = _library_with_dependency(database, client, source["worker_id"])

    saved = client.post(
        f"/v1/workspaces/{source['worker_id']}/templates",
        json={"name": "Synthetic briefing", "description": "Reusable public-safe intent."},
    )
    assert saved.status_code == 201, saved.text
    template = saved.json()
    assert template["version"] == 1
    assert template["library_refs"] == [
        {
            "library_id": library["library_id"],
            "stable_id": "skill.synthetic.template",
            "version": "2.1.0",
            "content_hash": library["content_hash"],
            "scopes": ["documents:read"],
        }
    ]
    second = client.post(
        f"/v1/workspaces/{source['worker_id']}/templates",
        json={
            "name": "Synthetic briefing v2",
            "description": "Second immutable version.",
            "lineage_id": template["lineage_id"],
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2
    assert second.json()["parent_template_id"] == template["template_id"]

    with sqlite3.connect(database) as conn:
        content_text = conn.execute(
            "SELECT content_json FROM workspace_templates WHERE template_id = ?",
            (template["template_id"],),
        ).fetchone()[0]
        content = json.loads(content_text)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM workers WHERE worker_id = ?", (source["worker_id"],))
    assert content["worker"]["bootstrap_bundle"] == {
        "project_definition": "Prepare the reusable synthetic briefing."
    }
    for forbidden in (
        "/private/source/path",
        "/private/provider/home",
        "acct_must_not_copy",
        "must-not-copy",
        "private.txt",
        "grant_",
        "schedule",
        "audit",
    ):
        assert forbidden not in content_text

    instantiated = client.post(
        f"/v1/workspace-templates/{template['template_id']}/instantiate",
        json={"idempotency_key": "template-public-safe-1", "name": "Fresh briefing desk"},
    )
    repeated = client.post(
        f"/v1/workspace-templates/{template['template_id']}/instantiate",
        json={"idempotency_key": "template-public-safe-1", "name": "Fresh briefing desk"},
    )
    assert instantiated.status_code == 201, instantiated.text
    assert repeated.status_code == 201, repeated.text
    assert instantiated.json()["workspace"]["state"] == "paused"
    assert instantiated.json()["workspace"]["worker_id"] == repeated.json()["workspace"]["worker_id"]
    assert repeated.json()["idempotent_replay"] is True
    assert {item["stable_id"] for item in instantiated.json()["approvals_required"]} == {
        library["stable_id"],
        dependency["stable_id"],
    }
    worker_id = instantiated.json()["workspace"]["worker_id"]
    report = instantiated.json()["workspace"]["duplication_report"]
    assert report["source_state"] == "template"
    reloaded = client.get(f"/v1/workers/{worker_id}")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["duplication_report"]["source_state"] == "template"
    live = client.get(f"/v1/workers/{worker_id}/live")
    assert live.status_code == 200, live.text
    assert live.json()["worker"]["duplication_report"]["source_state"] == "template"
    assert report["reapproval_items"] == [{
        "action_id": "rea_" + hashlib.sha256(
            f"library_grant\0{library['library_id']}".encode()
        ).hexdigest()[:24],
        "kind": "library",
        "resolution": "library_grant",
        "reference": library["library_id"],
        "label": library["stable_id"],
        "route": "library",
        "scopes": ["documents:read"],
    }]
    assert report["outstanding_reapproval_items"] == report["reapproval_items"]
    assert client.get(
        f"/v1/workspaces/{worker_id}/capability-grants"
    ).json()["items"] == []
    assert client.post(
        f"/v1/workers/{worker_id}/message",
        json={"message": "Do not run before template capability review"},
    ).status_code == 409
    prepared = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "library_enable",
            "target_id": worker_id,
            "payload": {"library_id": library["library_id"], "scopes": ["documents:read"]},
        },
    ).json()
    assert client.post(
        f"/v1/pending-changes/{prepared['change_id']}/confirm",
        json={"confirmation_token": prepared["confirmation_token"]},
    ).status_code == 200
    after_review = client.post(
        f"/v1/workspace-templates/{template['template_id']}/instantiate",
        json={"idempotency_key": "template-public-safe-1", "name": "Fresh briefing desk"},
    )
    assert after_review.status_code == 201
    assert after_review.json()["idempotent_replay"] is True
    assert after_review.json()["workspace"]["duplication_report"]["outstanding_reapproval_items"] == []
    assert client.post(
        f"/v1/workers/{worker_id}/message",
        json={"message": "Run after template capability review"},
    ).status_code == 202
    conflict = client.post(
        f"/v1/workspace-templates/{template['template_id']}/instantiate",
        json={"idempotency_key": "template-public-safe-1", "name": "Different request"},
    )
    assert conflict.status_code == 409


def test_template_instantiation_failure_after_worker_creation_rolls_back_and_same_key_retries(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "runtime.db"
    app = create_app(db_path=str(database), runtime_backend="stub")
    service = app.state.service
    attempts = 0
    with TestClient(app) as client:
        source = _workspace(client)
        template = client.post(
            f"/v1/workspaces/{source['worker_id']}/templates",
            json={"name": "Crash-safe template"},
        ).json()
        original_create_worker = service.create_worker

        def create_then_fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            worker = original_create_worker(*args, **kwargs)
            if attempts == 1:
                raise RuntimeError("synthetic interruption after template worker creation")
            return worker

        monkeypatch.setattr(service, "create_worker", create_then_fail_once)
        request = {
            "idempotency_key": "template-worker-failure-retry-1",
            "name": "Recovered template workspace",
        }

        with pytest.raises(RuntimeError, match="synthetic interruption"):
            client.post(
                f"/v1/workspace-templates/{template['template_id']}/instantiate",
                json=request,
            )

        projects_after_failure = client.get("/v1/projects").json()["items"]
        workspaces_after_failure = client.get(
            "/v1/workspaces?kind=named,ephemeral,legacy"
        ).json()["items"]
        retried = client.post(
            f"/v1/workspace-templates/{template['template_id']}/instantiate",
            json=request,
        )
        projects_after_retry = client.get("/v1/projects").json()["items"]
        workspaces_after_retry = client.get(
            "/v1/workspaces?kind=named,ephemeral,legacy"
        ).json()["items"]

    assert [item["project_id"] for item in projects_after_failure] == [source["project_id"]]
    assert [item["worker_id"] for item in workspaces_after_failure] == [source["worker_id"]]
    assert retried.status_code == 201, retried.text
    assert retried.json()["idempotent_replay"] is False
    assert len(projects_after_retry) == 2
    assert len(workspaces_after_retry) == 2
    assert attempts == 2
    with sqlite3.connect(database) as conn:
        reservation = conn.execute(
            """
            SELECT status, project_id, worker_id
            FROM workspace_template_instantiations
            WHERE tenant_id = 'local' AND owner_id = 'demo-owner' AND idempotency_key = ?
            """,
            (request["idempotency_key"],),
        ).fetchone()
    assert reservation == (
        "completed",
        retried.json()["project"]["project_id"],
        retried.json()["workspace"]["worker_id"],
    )


def test_template_tamper_and_missing_library_fail_before_worker_creation(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    source = _workspace(client)
    library, dependency = _library_with_dependency(database, client, source["worker_id"])
    first = client.post(
        f"/v1/workspaces/{source['worker_id']}/templates",
        json={"name": "Tamper target"},
    ).json()
    second = client.post(
        f"/v1/workspaces/{source['worker_id']}/templates",
        json={"name": "Changed hash target"},
    ).json()
    third = client.post(
        f"/v1/workspaces/{source['worker_id']}/templates",
        json={"name": "Missing dependency target"},
    ).json()
    before = len(client.get("/v1/workspaces?kind=named").json()["items"])

    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE workspace_templates SET content_json = ? WHERE template_id = ?",
            ('{"schema_version":1,"tampered":true}', first["template_id"]),
        )
    tampered = client.post(
        f"/v1/workspace-templates/{first['template_id']}/instantiate",
        json={"idempotency_key": "template-tamper-1"},
    )
    assert tampered.status_code == 409
    assert "integrity" in tampered.json()["detail"].lower()

    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE control_plane_library SET content_hash = ? WHERE library_id = ?",
            ("sha256:" + ("c" * 64), library["library_id"]),
        )
    changed_hash = client.post(
        f"/v1/workspace-templates/{second['template_id']}/instantiate",
        json={"idempotency_key": "template-changed-hash-1"},
    )
    assert changed_hash.status_code == 409
    assert "hash" in changed_hash.json()["detail"].lower()

    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE control_plane_library SET content_hash = ? WHERE library_id = ?",
            (library["content_hash"], library["library_id"]),
        )
        conn.execute(
            "UPDATE control_plane_library SET status = 'unavailable' WHERE library_id = ?",
            (dependency["library_id"],),
        )
    missing = client.post(
        f"/v1/workspace-templates/{third['template_id']}/instantiate",
        json={"idempotency_key": "template-missing-dependency-1"},
    )
    assert missing.status_code == 400
    assert "library version" in missing.json()["detail"].lower()
    assert len(client.get("/v1/workspaces?kind=named").json()["items"]) == before


def test_templates_are_tenant_and_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-public-safe")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "different-signed-secret")
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))
    base = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-public-safe",
    }
    owner = {**base, "X-Viventium-User-Id": "member-a"}
    other = {**base, "X-Viventium-User-Id": "member-b"}
    source = _workspace(client, headers=owner)
    saved = client.post(
        f"/v1/workspaces/{source['worker_id']}/templates",
        headers=owner,
        json={"name": "Owner-only template"},
    )
    assert saved.status_code == 201, saved.text
    template_id = saved.json()["template_id"]

    assert [item["template_id"] for item in client.get("/v1/workspace-templates", headers=owner).json()["items"]] == [template_id]
    assert client.get("/v1/workspace-templates", headers=other).json()["items"] == []
    denied_save = client.post(
        f"/v1/workspaces/{source['worker_id']}/templates",
        headers=other,
        json={"name": "Must not cross owner boundary"},
    )
    assert denied_save.status_code == 404
    denied = client.post(
        f"/v1/workspace-templates/{template_id}/instantiate",
        headers=other,
        json={"idempotency_key": "template-other-user-1"},
    )
    assert denied.status_code == 404


def test_template_preserves_only_owner_account_reference_and_revalidates_readiness(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    source = _workspace(client)
    store = ControlPlaneStore(str(database))
    account = store.create_provider_account(
        tenant_id="local",
        owner_id="demo-owner",
        provider="codex",
        label="Synthetic reusable account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )
    pending = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": source["worker_id"],
            "payload": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            },
        },
    ).json()
    switched = client.post(
        f"/v1/pending-changes/{pending['change_id']}/confirm",
        json={"confirmation_token": pending["confirmation_token"]},
    )
    assert switched.status_code == 200, switched.text

    template = client.post(
        f"/v1/workspaces/{source['worker_id']}/templates",
        json={"name": "Account-aware template"},
    )
    assert template.status_code == 201, template.text
    assert template.json()["provider_account_ref"] == {
        "policy": "personal_required",
        "account_id": account["account_id"],
        "provider": "codex",
    }
    with sqlite3.connect(database) as conn:
        content = json.loads(
            conn.execute(
                "SELECT content_json FROM workspace_templates WHERE template_id = ?",
                (template.json()["template_id"],),
            ).fetchone()[0]
        )
    assert content["worker"]["provider_account_ref"] == template.json()["provider_account_ref"]
    assert "native-home://" not in json.dumps(content)

    store.update_provider_account_status(
        account_id=str(account["account_id"]),
        tenant_id="local",
        owner_id="demo-owner",
        status="action_required",
        reconnect_reason="Synthetic reconnect",
    )
    blocked = client.post(
        f"/v1/workspace-templates/{template.json()['template_id']}/instantiate",
        json={"idempotency_key": "template-account-blocked-1"},
    )
    assert blocked.status_code == 400
    assert "reconnected" in blocked.json()["detail"].lower()

    store.update_provider_account_status(
        account_id=str(account["account_id"]),
        tenant_id="local",
        owner_id="demo-owner",
        status="ready",
        verified=True,
    )
    created = client.post(
        f"/v1/workspace-templates/{template.json()['template_id']}/instantiate",
        json={"idempotency_key": "template-account-ready-1"},
    )
    assert created.status_code == 201, created.text
    catalog = client.get("/v1/workspaces?kind=named").json()["items"]
    instantiated = next(
        item for item in catalog if item["worker_id"] == created.json()["workspace"]["worker_id"]
    )
    assert instantiated["provider_account"] == {
        "policy": "personal_required",
        "account_id": account["account_id"],
    }
