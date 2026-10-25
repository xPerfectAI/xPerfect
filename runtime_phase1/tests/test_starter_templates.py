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


@pytest.mark.parametrize('removed_state',['archived','deleted'])
def test_distinct_starter_jobs_reuse_active_expert_but_not_removed_catalog_choice(environment,removed_state):
    _,client,service=environment
    pack=catalog()['items'][0]
    route=f"/v1/workspace-starters/{pack['id']}/import"
    payload={'content_hash':pack['content_hash'],'effort':'high'}
    first=client.post(route,json={**payload,'idempotency_key':'first-explicit-starter-job'}).json()['template']
    with service.store._connect() as conn:
        library_count=conn.execute('SELECT COUNT(*) FROM personal_library_items').fetchone()[0]
    second=client.post(route,json={**payload,'idempotency_key':'second-explicit-starter-job'})
    assert second.status_code==201,second.text
    assert second.json()['template']['template_id']==first['template_id']
    workers=[]
    for n,template in enumerate((first,second.json()['template'])):
        response=client.post(f"/v1/workspace-templates/{template['template_id']}/instantiate",json={'idempotency_key':f'explicit-starter-worker-{n}'})
        assert response.status_code==201,response.text
        workers.append(response.json()['workspace']['worker_id'])
    assert len(set(workers))==2
    with service.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM personal_library_items').fetchone()[0]==library_count
        assert conn.execute('SELECT COUNT(*) FROM workspace_templates').fetchone()[0]==1
    removed = service.control_plane_store.set_workspace_template_state(template_id=first['template_id'],tenant_id='local',owner_id='demo-owner',state=removed_state,expected_catalog_revision=1)
    fresh=client.post(route,json={**payload,'idempotency_key':'new-job-after-expert-removal'})
    assert fresh.status_code==201,fresh.text
    assert fresh.json()['template']['template_id']!=first['template_id']
    assert fresh.json()['template']['catalog_state']=='active'
    replay=client.post(route,json={**payload,'idempotency_key':'first-explicit-starter-job'})
    assert replay.json()['template']['template_id']==first['template_id']
    restored = client.post(f"/v1/workspace-templates/{first['template_id']}/state", json={
        'state':'active', 'expected_catalog_revision':removed['catalog_revision']})
    assert restored.status_code==200,restored.text
    after_restore = client.post(route,json={**payload,'idempotency_key':'new-job-after-expert-restore'})
    assert after_restore.status_code==201,after_restore.text
    assert after_restore.json()['template']['template_id']==fresh.json()['template']['template_id']
    assert client.post(route,json={**payload,'idempotency_key':'first-explicit-starter-job'}).json()['template']['template_id']==first['template_id']
    with service.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM personal_library_items').fetchone()[0]==library_count*2


def test_concurrent_starter_imports_share_one_staged_library_set(environment,monkeypatch):
    import threading
    from workers_projects_runtime.workspace_files import FileAdmissionError
    _,_,service=environment
    pack=catalog()['items'][0]
    barrier=threading.Barrier(2)
    local=threading.local()
    original=service.personal_library.create_item
    def synchronized(*args,**kwargs):
        if not getattr(local,'entered',False):
            local.entered=True;barrier.wait(timeout=5)
        return original(*args,**kwargs)
    monkeypatch.setattr(service.personal_library,'create_item',synchronized)
    def create(n):
        request=StarterImport(content_hash=pack['content_hash'],idempotency_key=f'concurrent-starter-job-{n}')
        try:
            return import_starter(service,pack['id'],'local','demo-owner',request)
        except FileAdmissionError as exc:
            assert exc.status_code==409
            return request
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(create,[1,2]))
    # A Files identity refusal is fail-closed, not a second import or a 500.
    # The normal same-key retry must recover through the same durable receipt.
    results=[import_starter(service,pack['id'],'local','demo-owner',item)
             if isinstance(item,StarterImport) else item for item in results]
    assert results[0]['template']['template_id']==results[1]['template']['template_id']
    with service.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM personal_library_items').fetchone()[0]==len(pack['abilities'])
        assert conn.execute('SELECT COUNT(*) FROM workspace_templates').fetchone()[0]==1


def test_starter_pending_import_recovers_same_key_after_typed_file_refusal(environment,monkeypatch):
    from workers_projects_runtime.workspace_files import FileAdmissionError
    _,client,service=environment
    pack=catalog()['items'][0]
    route=f"/v1/workspace-starters/{pack['id']}/import"
    payload={'content_hash':pack['content_hash'],'idempotency_key':'retry-owned-file-refusal'}
    original=service.personal_library.create_item
    def refused(*args,**kwargs):
        raise FileAdmissionError('Synthetic file identity changed; retry safely',409)
    monkeypatch.setattr(service.personal_library,'create_item',refused)
    refused_response=client.post(route,json=payload)
    assert refused_response.status_code==409,refused_response.text
    monkeypatch.setattr(service.personal_library,'create_item',original)
    recovered=client.post(route,json=payload)
    assert recovered.status_code==201,recovered.text
    replay=client.post(route,json=payload)
    assert replay.json()['template']['template_id']==recovered.json()['template']['template_id']
    with service.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM personal_library_items').fetchone()[0]==len(pack['abilities'])
        assert conn.execute('SELECT COUNT(*) FROM workspace_templates').fetchone()[0]==1


@pytest.mark.parametrize("custom_library_refs", [False, True])
def test_fresh_starter_import_keeps_original_setup_after_saved_expert_customization(environment, custom_library_refs):
    """Ready-made selection must not silently adopt the user's My experts revision."""
    from copy import deepcopy

    _, client, service = environment
    pack = catalog()["items"][0]
    route = f"/v1/workspace-starters/{pack['id']}/import"
    choices = {"content_hash": pack["content_hash"], "effort": "high"}
    original = client.post(route, json={**choices, "idempotency_key": "original-ready-made-setup"})
    assert original.status_code == 201, original.text
    first = original.json()["template"]
    # A distinct untouched job reuses one exact expert and one Library set.
    untouched = client.post(route, json={**choices, "idempotency_key": "second-ready-made-setup"})
    assert untouched.status_code == 201, untouched.text
    assert untouched.json()["template"]["template_id"] == first["template_id"]
    with service.store._connect() as conn:
        library_count = conn.execute("SELECT count(*) FROM personal_library_items").fetchone()[0]

    changed = client.patch("/v1/workspace-templates/" + first["template_id"], json={
        "name": "Company-specific expert", "description": "Only the company's customized onboarding goal",
        "expected_catalog_revision": first["catalog_revision"],
    })
    assert changed.status_code == 200, changed.text
    customized = changed.json()
    assert customized["version"] == 2
    cp = service.control_plane_store
    private_customized = cp.get_workspace_template(
        tenant_id="local", owner_id="demo-owner", template_id=customized["template_id"])
    assert private_customized["content"]["project"]["goal"] == customized["description"]
    if custom_library_refs:
        # The existing immutable-version owner can retain an independently
        # customized Library selection without changing its native/account setup.
        content = deepcopy(private_customized["content"])
        assert len(content["personal_library_refs"]) > 1
        content["personal_library_refs"] = content["personal_library_refs"][1:]
        customized = cp.create_workspace_template(
            tenant_id="local", owner_id="demo-owner", name=customized["name"],
            description=customized["description"], content=content,
            lineage_id=customized["lineage_id"], expected_template_id=customized["template_id"],
            expected_catalog_revision=customized["catalog_revision"])
        service.personal_library.retain_refs("local", "demo-owner", content["personal_library_refs"],
                                             "workspace_template", customized["template_id"])
    custom_hash = customized["content_hash"]

    fresh = client.post(route, json={**choices, "idempotency_key": "fresh-ready-made-after-customization"})
    assert fresh.status_code == 201, fresh.text
    selected = fresh.json()["template"]
    assert selected["template_id"] == first["template_id"]
    assert selected["name"] == first["name"] and selected["description"] == first["description"]
    assert selected["content_hash"] == first["content_hash"]
    assert selected["personal_library_refs"] == first["personal_library_refs"]
    replay = client.post(route, json={**choices, "idempotency_key": "fresh-ready-made-after-customization"})
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["template"]["template_id"] == first["template_id"]

    # Exact My experts selection keeps the user's newer version, not the starter.
    still_custom = client.get("/v1/workspace-templates/" + customized["template_id"])
    assert still_custom.status_code == 200
    assert still_custom.json()["content_hash"] == custom_hash
    assert still_custom.json()["name"] == "Company-specific expert"
    assert client.get("/v1/workspace-templates").json()["items"][0]["template_id"] == customized["template_id"]
    selected_custom = client.post(f"/v1/workspace-templates/{customized['template_id']}/instantiate",
                                 json={"idempotency_key": "explicit-my-experts-customized-version"})
    assert selected_custom.status_code == 201, selected_custom.text
    assert selected_custom.json()["template"]["template_id"] == customized["template_id"]
    assert selected_custom.json()["project"]["goal"] == "Only the company's customized onboarding goal"
    assert selected_custom.json()["workspace"]["state"] == "paused"
    with service.store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM personal_library_items").fetchone()[0] == library_count
        assert conn.execute("SELECT count(*) FROM workspace_templates").fetchone()[0] == (3 if custom_library_refs else 2)
