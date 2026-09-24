from __future__ import annotations

import hashlib
import sqlite3

from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.store import Store


def _workspace(client: TestClient, *, profile: str = "codex-cli") -> dict:
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Account switch desk", "goal": "Synthetic QA"},
    ).json()
    return client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Account switch desk",
            "role": "main",
            "profile": profile,
            "workspace_kind": "named",
            "start_synchronously": False,
        },
    ).json()


def _ready_account(store: ControlPlaneStore, *, provider: str = "codex") -> dict:
    account = store.create_provider_account(
        tenant_id="local",
        owner_id="demo-owner",
        provider=provider,
        label="Synthetic private account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
    )
    return store.update_provider_account_status(
        account_id=str(account["account_id"]),
        tenant_id="local",
        owner_id="demo-owner",
        status="ready",
        verified=True,
    )


def test_workspace_account_switch_is_confirmed_owner_scoped_and_future_only(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    workspace = _workspace(client)
    store = ControlPlaneStore(str(database))
    account = _ready_account(store)

    prepared = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            },
        },
    )
    assert prepared.status_code == 201
    assert prepared.json()["payload"]["account_snapshot"]["label"] == "Synthetic private account"

    confirmed = client.post(
        f"/v1/pending-changes/{prepared.json()['change_id']}/confirm",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["applied"] == {
        "worker_id": workspace["worker_id"],
        "provider_account": {
            "policy": "personal_required",
            "account_id": account["account_id"],
        },
        "account": {
            "account_id": account["account_id"],
            "provider": "codex",
            "label": "Synthetic private account",
        },
        "applies_to": "future_runs",
    }
    catalog = client.get("/v1/workspaces?kind=named").json()["items"]
    selected = next(item for item in catalog if item["worker_id"] == workspace["worker_id"])
    assert selected["provider_account"] == {
        "policy": "personal_required",
        "account_id": account["account_id"],
    }
    assert "bootstrap_bundle_json" not in selected


def test_workspace_duplicate_reports_personal_account_reapproval_without_copying_selection(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    workspace = _workspace(client)
    store = ControlPlaneStore(str(database))
    account = _ready_account(store)
    pending = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            },
        },
    ).json()
    assert client.post(
        f"/v1/pending-changes/{pending['change_id']}/confirm",
        json={"confirmation_token": pending["confirmation_token"]},
    ).status_code == 200

    copied = client.post(
        f"/v1/workspaces/{workspace['worker_id']}/duplicate",
        json={"idempotency_key": "duplicate-personal-account-1"},
    )

    assert copied.status_code == 201
    report = copied.json()["workspace"]["duplication_report"]
    assert report["capabilities_requiring_reapproval"] == 1
    assert report["reapproval_items"] == [{
        "action_id": "rea_" + hashlib.sha256(
            f"provider_selection\0{account['account_id']}".encode()
        ).hexdigest()[:24],
        "kind": "provider_account",
        "resolution": "provider_selection",
        "reference": account["account_id"],
        "label": "Synthetic private account",
        "route": "connections",
        "policy": "personal_required",
        "scopes": [],
    }]
    assert "provider_account" not in copied.json()["workspace"]
    copied_worker_id = copied.json()["workspace"]["worker_id"]
    assert client.post(
        f"/v1/workers/{copied_worker_id}/message",
        json={"message": "Run before account review"},
    ).status_code == 409
    bypass = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_duplication_reapproval_waiver",
            "target_id": copied_worker_id,
            "payload": {"action_id": report["reapproval_items"][0]["action_id"]},
        },
    )
    assert bypass.status_code == 409
    assert "choose" in bypass.json()["detail"].lower()
    copied_pending = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": copied_worker_id,
            "payload": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            },
        },
    ).json()
    assert client.post(
        f"/v1/pending-changes/{copied_pending['change_id']}/confirm",
        json={"confirmation_token": copied_pending["confirmation_token"]},
    ).status_code == 200
    assert client.post(
        f"/v1/workers/{copied_worker_id}/message",
        json={"message": "Run after account review"},
    ).status_code == 202


def test_duplicate_handles_a_forgotten_selected_account_without_an_impossible_review(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    store = ControlPlaneStore(str(database))

    def selected_workspace(policy: str, *, forget: bool) -> dict:
        workspace = _workspace(client)
        account = _ready_account(store)
        pending = client.post(
            "/v1/pending-changes",
            json={
                "change_type": "workspace_provider_account",
                "target_id": workspace["worker_id"],
                "payload": {"policy": policy, "account_id": account["account_id"]},
            },
        ).json()
        assert client.post(
            f"/v1/pending-changes/{pending['change_id']}/confirm",
            json={"confirmation_token": pending["confirmation_token"]},
        ).status_code == 200
        store.update_provider_account_status(
            account_id=account["account_id"],
            tenant_id="local",
            owner_id="demo-owner",
            status="disconnected",
        )
        if forget:
            store.forget_provider_account(
                account_id=account["account_id"], tenant_id="local", owner_id="demo-owner"
            )
        return workspace

    disconnected_preferred = selected_workspace("personal_preferred", forget=False)
    disconnected_preferred_copy = client.post(
        f"/v1/workspaces/{disconnected_preferred['worker_id']}/duplicate",
        json={"idempotency_key": "disconnected-preferred-account"},
    )
    assert disconnected_preferred_copy.status_code == 201
    assert disconnected_preferred_copy.json()["workspace"]["duplication_report"][
        "capabilities_requiring_reapproval"
    ] == 0

    disconnected_required = selected_workspace("personal_required", forget=False)
    disconnected_required_copy = client.post(
        f"/v1/workspaces/{disconnected_required['worker_id']}/duplicate",
        json={"idempotency_key": "disconnected-required-account"},
    )
    assert disconnected_required_copy.status_code == 409

    preferred = selected_workspace("personal_preferred", forget=True)
    preferred_copy = client.post(
        f"/v1/workspaces/{preferred['worker_id']}/duplicate",
        json={"idempotency_key": "forgotten-preferred-account"},
    )
    assert preferred_copy.status_code == 201
    copied = preferred_copy.json()["workspace"]
    assert copied["duplication_report"]["capabilities_requiring_reapproval"] == 0
    assert client.post(
        f"/v1/workers/{copied['worker_id']}/message",
        json={"message": "Use the valid deployment fallback"},
    ).status_code == 202

    required = selected_workspace("personal_required", forget=True)
    required_copy = client.post(
        f"/v1/workspaces/{required['worker_id']}/duplicate",
        json={"idempotency_key": "forgotten-required-account"},
    )
    assert required_copy.status_code == 409
    assert required_copy.json()["detail"] == (
        "This workspace requires a personal AI account that is not ready. "
        "Reconnect it or choose a current account before duplicating it."
    )


def test_duplicate_keeps_provider_grant_and_provider_selection_as_distinct_review_actions(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    workspace = _workspace(client)
    store = ControlPlaneStore(str(database))
    account = _ready_account(store)
    selection = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            },
        },
    ).json()
    assert client.post(
        f"/v1/pending-changes/{selection['change_id']}/confirm",
        json={"confirmation_token": selection["confirmation_token"]},
    ).status_code == 200
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            INSERT INTO workspace_capability_grants
                (grant_id, tenant_id, owner_id, worker_id, library_id, connection_id,
                 account_id, scopes_json, prior_bootstrap_bundle_json,
                 applied_bootstrap_bundle_json, installation_plan_json, probe_json,
                 created_at, revoked_at)
            VALUES ('grant_provider_reference', 'local', 'demo-owner', ?, NULL, NULL, ?,
                    '[]', '{}', '{}', '[]', '{}', 1, NULL)
            """,
            (workspace["worker_id"], account["account_id"]),
        )

    copied = client.post(
        f"/v1/workspaces/{workspace['worker_id']}/duplicate",
        json={"idempotency_key": "duplicate-provider-actions-1"},
    )

    assert copied.status_code == 201
    copied_worker_id = copied.json()["workspace"]["worker_id"]
    items = copied.json()["workspace"]["duplication_report"]["reapproval_items"]
    assert {item["resolution"] for item in items} == {"provider_grant", "provider_selection"}
    assert len({item["action_id"] for item in items}) == 2
    assert {item["reference"] for item in items} == {account["account_id"]}
    copied_selection = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": copied_worker_id,
            "payload": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            },
        },
    ).json()
    assert client.post(
        f"/v1/pending-changes/{copied_selection['change_id']}/confirm",
        json={"confirmation_token": copied_selection["confirmation_token"]},
    ).status_code == 200
    assert client.post(
        f"/v1/workers/{copied_worker_id}/message",
        json={"message": "The distinct provider grant still needs a decision"},
    ).status_code == 409
    provider_grant = next(item for item in items if item["resolution"] == "provider_grant")
    skip = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_duplication_reapproval_waiver",
            "target_id": copied_worker_id,
            "payload": {"action_id": provider_grant["action_id"]},
        },
    ).json()
    assert client.post(
        f"/v1/pending-changes/{skip['change_id']}/confirm",
        json={"confirmation_token": skip["confirmation_token"]},
    ).status_code == 200
    assert client.post(
        f"/v1/workers/{copied_worker_id}/message",
        json={"message": "Both exact review actions were resolved"},
    ).status_code == 202


def test_workspace_account_switch_revalidates_profile_status_and_active_lease(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    workspace = _workspace(client)
    store = ControlPlaneStore(str(database))
    wrong_provider = _ready_account(store, provider="claude")
    mismatch = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {
                "policy": "personal_required",
                "account_id": wrong_provider["account_id"],
            },
        },
    )
    assert mismatch.status_code == 400
    assert "profile" in mismatch.json()["detail"].lower()

    account = _ready_account(store)
    prepared = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {"policy": "personal_required", "account_id": account["account_id"]},
        },
    ).json()
    lease = store.acquire_provider_lease(
        account_id=str(account["account_id"]),
        tenant_id="local",
        owner_id="demo-owner",
        lane="mission",
        worker_id=str(workspace["worker_id"]),
        run_id="run_synthetic_active",
        ttl_seconds=60,
    )
    blocked = client.post(
        f"/v1/pending-changes/{prepared['change_id']}/confirm",
        json={"confirmation_token": prepared["confirmation_token"]},
    )
    assert blocked.status_code == 409
    assert "finish" in blocked.json()["detail"].lower()
    store.release_provider_lease(
        lease_id=str(lease["lease_id"]), tenant_id="local", owner_id="demo-owner"
    )

    store.update_provider_account_status(
        account_id=str(account["account_id"]),
        tenant_id="local",
        owner_id="demo-owner",
        status="action_required",
        reconnect_reason="Synthetic reconnect",
    )
    stale = client.post(
        f"/v1/pending-changes/{prepared['change_id']}/confirm",
        json={"confirmation_token": prepared["confirmation_token"]},
    )
    assert stale.status_code == 409
    assert "reconnected" in stale.json()["detail"].lower()


def test_workspace_account_switch_rejects_closed_workspace_before_and_after_review(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    workspace = _workspace(client)
    store = ControlPlaneStore(str(database))
    account = _ready_account(store)
    request = {
        "change_type": "workspace_provider_account",
        "target_id": workspace["worker_id"],
        "payload": {"policy": "personal_required", "account_id": account["account_id"]},
    }

    prepared = client.post("/v1/pending-changes", json=request)
    assert prepared.status_code == 201
    Store(str(database)).update_worker_state(workspace["worker_id"], "termination_failed")

    confirm = client.post(
        f"/v1/pending-changes/{prepared.json()['change_id']}/confirm",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
    )
    assert confirm.status_code == 409
    assert confirm.json()["detail"] == "Workspace is closed; create a new workspace for new work"

    rejected = client.post("/v1/pending-changes", json=request)
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "Workspace is closed; create a new workspace for new work"

    racing_workspace = _workspace(client)
    racing_request = {**request, "target_id": racing_workspace["worker_id"]}
    control_plane = client.app.state.control_plane
    original_create = control_plane.create_pending_change

    def close_before_reservation(**kwargs):
        Store(str(database)).update_worker_state(
            racing_workspace["worker_id"], "termination_failed"
        )
        return original_create(**kwargs)

    monkeypatch.setattr(control_plane, "create_pending_change", close_before_reservation)
    raced = client.post("/v1/pending-changes", json=racing_request)
    assert raced.status_code == 409
    assert raced.json()["detail"] == "Workspace is closed; create a new workspace for new work"
    with sqlite3.connect(database) as conn:
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM control_plane_pending_changes WHERE target_id = ?",
            (racing_workspace["worker_id"],),
        ).fetchone()[0]
    assert pending_count == 0


def test_workspace_account_switch_does_not_leak_cross_owner_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-public-safe")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-link-secret")
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    headers_a = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-public-safe",
        "X-Viventium-User-Id": "user-a",
    }
    headers_b = {**headers_a, "X-Viventium-User-Id": "user-b"}
    project = client.post(
        "/v1/projects",
        headers=headers_a,
        json={"owner_id": "ignored", "title": "Private desk", "goal": "Synthetic QA"},
    ).json()
    workspace = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers_a,
        json={"owner_id": "ignored", "name": "Private desk", "role": "main", "profile": "codex-cli"},
    ).json()
    store = ControlPlaneStore(str(database))
    account = store.create_provider_account(
        tenant_id="tenant-public-safe",
        owner_id="user-a",
        provider="codex",
        label="Private A",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )
    hidden = client.post(
        "/v1/pending-changes",
        headers=headers_b,
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {"policy": "personal_required", "account_id": account["account_id"]},
        },
    )
    assert hidden.status_code == 404
    assert "private a" not in hidden.text.lower()
    assert str(account["account_id"]) not in hidden.text
