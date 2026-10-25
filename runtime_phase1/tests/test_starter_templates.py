from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.starter_templates import (
    StarterImport, catalog, import_starter, install_starter_routes, register_starter_tools,
)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOW_HOST_WORKERS", "true")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    from dataclasses import replace
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    class PreparedRuntime(StubRuntime):
        def _runtime_info(self, worker, *, pid):
            workspace = tmp_path / worker["worker_id"]
            workspace.mkdir(exist_ok=True)
            return replace(super()._runtime_info(worker, pid=pid), workspace_dir=str(workspace))
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=PreparedRuntime())
    return app, TestClient(app), app.state.service


@pytest.mark.parametrize("starter_id", [
    "clinical-biostatistician", "bioanalytical-scientist", "regulatory-affairs-specialist",
    "growth-marketer", "finance-director",
])
def test_pack_import_is_an_ordinary_exact_template_not_a_running_worker(environment, starter_id):
    app, client, service = environment
    published = client.get("/v1/workspace-starters")
    assert published.status_code == 200, published.text
    pack = next(p for p in published.json()["items"] if p["id"] == starter_id)
    from workers_projects_runtime.starter_templates import PACK_ROOT
    declared = json.loads((PACK_ROOT / starter_id / "template.json").read_text())
    expected_count = len(declared["library_items"])
    selected_count = sum(entry.get("selected", True) for entry in declared["library_items"])
    assert len(pack["abilities"]) == expected_count
    assert any(entry["kind"] == "skill" and entry["included"] for entry in pack["abilities"])
    assert all(not entry["included"] for entry in pack["abilities"] if entry["kind"] == "native")
    payload = {"idempotency_key": "starter-positive-" + starter_id, "content_hash": pack["content_hash"], "effort": "high"}
    result = client.post(f"/v1/workspace-starters/{starter_id}/import", json=payload)
    assert result.status_code == 201, result.text
    template = result.json()["template"]
    assert template["profile"] == "claude-code"
    assert len(template["personal_library_refs"]) == selected_count
    private = service.control_plane_store.get_workspace_template(tenant_id="local", owner_id="demo-owner", template_id=template["template_id"])
    assert private["content"]["worker"]["bootstrap_bundle"]["env"] == {"WPR_CLAUDE_CODE_EFFORT": "high"}
    assert not private["content"]["worker"].get("provider_account_ref")
    with service.store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM workers").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM personal_library_items").fetchone()[0] == expected_count
    replay = client.post(f"/v1/workspace-starters/{starter_id}/import", json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["template"]["template_id"] == template["template_id"]
    created = client.post(f"/v1/workspace-templates/{template['template_id']}/instantiate", json={"idempotency_key": "starter-instance-" + starter_id})
    assert created.status_code == 201, created.text
    worker = created.json()["workspace"]
    assert worker["state"] == "paused" and worker["last_run_id"] is None
    # Native skills require this exact new worker's review, never an import-time grant.
    assert created.json()["approvals_required"]
    assert len(client.get(f"/v1/workspaces/{worker['worker_id']}/personal-library").json()["items"]) == selected_count
    assert client.post(f"/v1/workers/{worker['worker_id']}/message", json={"message": "must not start unreviewed"}).status_code == 409


def test_hash_and_request_changes_fail_before_new_material(environment):
    _, client, service = environment
    packs = catalog()["items"]
    starter_id = packs[0]["id"]
    expected_count = len(packs[0]["abilities"])
    route = f"/v1/workspace-starters/{starter_id}/import"
    assert client.post(route, json={"idempotency_key": "stale-hash-key", "content_hash": "sha256:stale"}).status_code == 409
    assert service.personal_library.list_items("local", "demo-owner")["items"] == []
    payload = {"idempotency_key": "same-request-key", "content_hash": packs[0]["content_hash"]}
    good = client.post(route, json=payload)
    assert good.status_code == 201, good.text
    other = packs[1]
    assert client.post(f"/v1/workspace-starters/{other['id']}/import", json={**payload, "content_hash": other["content_hash"]}).status_code == 409
    assert client.post(route, json={**payload, "owner_id": "another-owner"}).status_code == 422
    with service.store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM personal_library_items").fetchone()[0] == expected_count
        assert conn.execute("SELECT count(*) FROM workspace_templates").fetchone()[0] == 1


def test_completed_import_replays_its_exact_template_after_catalog_upgrade(environment, monkeypatch, tmp_path):
    """A retry resumes the selected version; it is not a silent package upgrade."""
    import workers_projects_runtime.starter_templates as module
    _, client, service = environment
    pack = catalog()["items"][0]
    route = f"/v1/workspace-starters/{pack['id']}/import"
    payload = {"idempotency_key": "keep-completed-version", "content_hash": pack["content_hash"]}
    original = client.post(route, json=payload)
    assert original.status_code == 201, original.text
    # The packaged revision is no longer present. Its completed receipt must
    # still replay the original private template without touching the catalog.
    monkeypatch.setattr(module, "PACK_ROOT", tmp_path / "upgraded-catalog")
    replay = client.post(route, json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["template"]["template_id"] == original.json()["template"]["template_id"]
    assert replay.json()["template"]["personal_library_refs"] == original.json()["template"]["personal_library_refs"]
    with service.store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM workspace_templates").fetchone()[0] == 1


def test_partial_import_retries_existing_items_and_concurrent_import_creates_one_template(environment, monkeypatch):
    _, _, service = environment
    pack = catalog()["items"][0]
    expected_count = len(pack["abilities"])
    payload = StarterImport(idempotency_key="partial-import-key", content_hash=pack["content_hash"])
    cp = service.control_plane_store
    original = cp.create_workspace_template
    failures = []
    def fail_once(**kwargs):
        if not failures:
            failures.append(True)
            raise OSError("synthetic interrupted persistence")
        return original(**kwargs)
    monkeypatch.setattr(cp, "create_workspace_template", fail_once)
    with pytest.raises(OSError):
        import_starter(service, pack["id"], "local", "demo-owner", payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: import_starter(service, pack["id"], "local", "demo-owner", payload), range(2)))
    assert results[0]["template"]["template_id"] == results[1]["template"]["template_id"]
    with service.store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM workspace_templates").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM personal_library_items").fetchone()[0] == expected_count
        assert conn.execute("SELECT count(*) FROM workspace_template_imports WHERE template_id IS NOT NULL").fetchone()[0] == 1


def test_import_is_owner_scoped_and_rejects_another_owners_account(environment):
    _, _, service = environment
    pack = catalog()["items"][0]
    request = StarterImport(idempotency_key="same-key-private-owners", content_hash=pack["content_hash"])
    a = import_starter(service, pack["id"], "tenant-a", "owner-a", request)
    b = import_starter(service, pack["id"], "tenant-b", "owner-b", request)
    assert a["template"]["template_id"] != b["template"]["template_id"]
    assert a["template"]["personal_library_refs"] != b["template"]["personal_library_refs"]
    with pytest.raises(Exception, match="Reconnect"):
        import_starter(service, pack["id"], "tenant-b", "owner-b", StarterImport(
            idempotency_key="forbidden-account", content_hash=pack["content_hash"],
            provider_account={"policy": "personal_required", "account_id": "acct_not_owned"}))


@pytest.mark.parametrize("kind,role", [("signed_link", "owner"), ("session", "viewer")])
def test_restricted_identity_cannot_import_or_browse_private_management(environment, kind, role):
    _, _, service = environment
    app = FastAPI()
    install_starter_routes(app, service, lambda r: (SimpleNamespace(auth_mode=kind, role=role), "local", "demo-owner"))
    client = TestClient(app)
    pack = catalog()["items"][0]
    assert client.get("/v1/workspace-starters").status_code == 403
    assert client.post(f"/v1/workspace-starters/{pack['id']}/import", json={"idempotency_key": "restricted-import", "content_hash": pack["content_hash"]}).status_code == 403


def test_native_mcp_uses_the_same_owner_routes_and_exact_selection():
    class Server:
        tools = {}
        def tool(self, **options):
            def bind(fn):
                self.tools[options["name"]] = fn
                return fn
            return bind
    class Client:
        calls = []
        def _request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"items": []}
    server, client = Server(), Client()
    register_starter_tools(server, client)
    server.tools["workspace_starter_list"]()
    server.tools["workspace_starter_import"]("synthetic-role", "sha256:exact", "native-request-key", "acct_synthetic", "personal_required", "high")
    assert client.calls == [
        ("GET", "/v1/workspace-starters", {"require_write_scope": True}),
        ("POST", "/v1/workspace-starters/synthetic-role/import", {"require_write_scope": True, "json_body": {
            "content_hash": "sha256:exact", "idempotency_key": "native-request-key", "effort": "high",
            "provider_account": {"policy": "personal_required", "account_id": "acct_synthetic"}}}),
    ]


def test_starter_catalog_mcp_is_read_only_but_import_is_not():
    from workers_projects_runtime.starter_templates import register_starter_tools
    class Server:
        def __init__(self):
            self.metadata = {}
        def tool(self, **metadata):
            self.metadata[metadata["name"]] = metadata
            return lambda function: function
    server = Server()
    register_starter_tools(server, object())
    read = server.metadata["workspace_starter_list"]["annotations"]
    assert read.readOnlyHint is True and read.destructiveHint is False
    assert read.idempotentHint is True and read.openWorldHint is False
    assert not server.metadata["workspace_starter_import"].get("annotations")
