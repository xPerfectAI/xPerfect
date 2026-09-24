from __future__ import annotations

import json

from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.allowed_ai_policy import AllowedAiAdmissionError, AllowedAiPolicyService
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.store import (
    AllowedAiPolicyRevisionConflict,
    AllowedAiStartFenceConflict,
    Store,
)


def _project(client: TestClient, owner: str = "demo-owner") -> dict:
    response = client.post(
        "/v1/projects",
        json={"owner_id": owner, "title": "Allowed AI", "goal": "Policy test", "default_worker_profile": "codex-cli"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_store_allowed_ai_policy_is_independent_and_cas(tmp_path):
    store = Store(str(tmp_path / "policy.db"))
    policy = {"version": 1, "mode": "selected", "harnesses": []}
    assert store.get_allowed_ai_policy("project", "prj", tenant_id="local", owner_id="owner") is None
    assert store.put_allowed_ai_policy(
        "project", "prj", tenant_id="local", owner_id="owner", expected_revision=0,
        policy_json=json.dumps(policy),
    ) == 1
    assert store.get_allowed_ai_policy("project", "prj", tenant_id="local", owner_id="owner")["revision"] == 1
    try:
        store.put_allowed_ai_policy(
            "project", "prj", tenant_id="local", owner_id="owner", expected_revision=0,
            policy_json=json.dumps(policy),
        )
    except AllowedAiPolicyRevisionConflict as exc:
        assert exc.current_revision == 1
    else:
        raise AssertionError("stale Allowed AI update did not conflict")
    try:
        store.put_allowed_ai_policy(
            "project", "prj", tenant_id="local", owner_id="other", expected_revision=0,
            policy_json=json.dumps(policy),
        )
    except ValueError as exc:
        assert str(exc) == "Allowed AI policy scope is unavailable"
    else:
        raise AssertionError("cross-owner Allowed AI write did not fail closed")
    with store._connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_workspaces'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='allowed_ai_policies'"
        ).fetchone()


def test_allowed_ai_project_defaults_update_and_stale_revision(tmp_path):
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = _project(client)
        path = f"/v1/projects/{project['project_id']}/execution-policy"
        default = client.get(path)
        assert default.status_code == 200
        assert default.json()["revision"] == 0
        assert default.json()["policy"] == {"version": 1, "mode": "all_authorized", "harnesses": []}

        selected_empty = {"version": 1, "mode": "selected", "harnesses": []}
        saved = client.put(path, json={"expected_revision": 0, "policy": selected_empty})
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1
        stale = client.put(path, json={"expected_revision": 0, "policy": selected_empty})
        assert stale.status_code == 409
        assert stale.json() == {"detail": {"code": "policy_changed"}}


def test_allowed_ai_workspace_inherits_and_cannot_widen_project(tmp_path):
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = _project(client)
        project_id = project["project_id"]
        workspace_response = client.post(
            f"/v1/projects/{project_id}/execution-workspaces",
            json={"mode": "shared", "execution_mode": "docker"},
        )
        assert workspace_response.status_code == 201, workspace_response.text
        workspace_id = workspace_response.json()["workspace_id"]
        project_path = f"/v1/projects/{project_id}/execution-policy"
        workspace_path = f"/v1/workspaces/{workspace_id}/execution-policy"
        narrowed = {
            "version": 1,
            "mode": "selected",
            "harnesses": [
                {
                    "profile": "codex-cli",
                    "models": {"mode": "selected", "ids": ["codex-cli:gpt-6-astra"]},
                    "connections": {"mode": "all", "ids": []},
                }
            ],
        }
        assert client.put(project_path, json={"expected_revision": 0, "policy": narrowed}).status_code == 200
        inherited = client.get(workspace_path)
        assert inherited.status_code == 200
        assert inherited.json()["policy"]["mode"] == "inherit"
        assert inherited.json()["effective"]["mode"] == "selected"

        widened = {"version": 1, "mode": "all_authorized", "harnesses": []}
        saved = client.put(workspace_path, json={"expected_revision": 0, "policy": widened})
        assert saved.status_code == 200
        assert saved.json()["effective"] == narrowed


def test_allowed_ai_rejects_project_inherit_duplicate_and_unknown_scope(tmp_path):
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = _project(client)
        path = f"/v1/projects/{project['project_id']}/execution-policy"
        inherit = client.put(path, json={"expected_revision": 0, "policy": {"version": 1, "mode": "inherit", "harnesses": []}})
        assert inherit.status_code == 422
        duplicate = {
            "version": 1,
            "mode": "selected",
            "harnesses": [
                {"profile": "codex-cli", "models": {"mode": "selected", "ids": ["a", "a"]}, "connections": {"mode": "all", "ids": []}}
            ],
        }
        assert client.put(path, json={"expected_revision": 0, "policy": duplicate}).status_code == 422
        foreign_selection = {
            "version": 1,
            "mode": "selected",
            "harnesses": [
                {
                    "profile": "codex-cli",
                    "models": {"mode": "selected", "ids": ["claude-code:opus"]},
                    "connections": {"mode": "selected", "ids": ["acct_foreign"]},
                }
            ],
        }
        rejected = client.put(path, json={"expected_revision": 0, "policy": foreign_selection})
        assert rejected.status_code == 409
        assert rejected.json() == {"detail": {"code": "selection_unavailable"}}
        unknown_harness = {
            "version": 1,
            "mode": "selected",
            "harnesses": [
                {
                    "profile": "unregistered-harness",
                    "models": {"mode": "all", "ids": []},
                    "connections": {"mode": "all", "ids": []},
                }
            ],
        }
        rejected = client.put(path, json={"expected_revision": 0, "policy": unknown_harness})
        assert rejected.status_code == 409
        missing = client.get("/v1/projects/project-does-not-exist/execution-policy")
        assert missing.status_code == 404
        assert missing.json() == {"detail": {"code": "scope_missing"}}


def test_allowed_ai_options_do_not_expose_secret_locators(tmp_path):
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = _project(client)
        app.state.control_plane.create_provider_account(
            tenant_id="local",
            owner_id="demo-owner",
            provider="codex",
            label="Team subscription",
            auth_method="subscription",
            platform_support="supported",
            status="ready",
            secret_locator="keychain://synthetic/account",
        )
        response = client.get(f"/v1/projects/{project['project_id']}/execution-options")
        assert response.status_code == 200
        payload = response.json()
        assert "keychain://" not in json.dumps(payload)
        connections = payload["harnesses"][0]["connections"]
        assert connections[0]["label"] == "Team subscription"
        assert connections[0]["kind"] == "subscription"


def test_allowed_ai_offers_only_the_configured_exact_acp_model(tmp_path, monkeypatch):
    import workers_projects_runtime.allowed_ai_policy as policy_module

    class Accounts:
        def list_provider_accounts(self, **_):
            return [{"provider": "grok", "account_id": "acct_grok", "label": "Grok subscription",
                     "auth_method": "subscription", "status": "ready"}]

        def list_connections(self, **_):
            return []

    class Runtime:
        grok = object()

    monkeypatch.setattr(policy_module, "allowed_worker_profiles", lambda: ["grok-build"])
    monkeypatch.setenv("WPR_MODEL_GROK_BUILD", "grok-4.6")
    service = AllowedAiPolicyService(Store(str(tmp_path / "policy.db")), Accounts(), Runtime())
    options = service.options(scope_id="project", tenant_id="local", owner_id="owner")
    assert len(options["harnesses"]) == 1
    grok = options["harnesses"][0]
    assert grok["profile"] == "grok-build"
    assert grok["models"] == [{"id": "grok-build:grok-4.6", "label": "Grok Build / grok-4.6",
                               "status": "available"}]
    assert grok["connections"][0]["kind"] == "subscription"
    assert service._model_id("grok-build", "grok-4.6") == "grok-build:grok-4.6"
    assert service._model_id("grok-build", "grok-build:grok-4.6") == "grok-build:grok-4.6"
    assert service._model_id("grok-build", "grok-unconfigured") == ""
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD")
    assert service._model_id("grok-build", "grok-4.6") == ""
    assert service.options(scope_id="project", tenant_id="local", owner_id="owner")["harnesses"] == []


def test_allowed_ai_admission_blocks_new_starts_and_resume(tmp_path):
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = _project(client)
        project_id = project["project_id"]
        policy_path = f"/v1/projects/{project_id}/execution-policy"
        selected_empty = {"version": 1, "mode": "selected", "harnesses": []}
        assert client.put(
            policy_path,
            json={"expected_revision": 0, "policy": selected_empty},
        ).status_code == 200

        denied = client.post(
            f"/v1/projects/{project_id}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "denied",
                "role": "main",
                "profile": "codex-cli",
                "execution_mode": "docker",
            },
        )
        assert denied.status_code == 409
        assert denied.json()["detail"]["code"] == "allowed_ai_denied"

        paused = client.post(
            f"/v1/projects/{project_id}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "paused",
                "role": "main",
                "profile": "codex-cli",
                "execution_mode": "docker",
                "start_synchronously": False,
            },
        )
        assert paused.status_code == 201, paused.text
        assigned = client.post(
            f"/v1/workers/{paused.json()['worker_id']}/assign",
            json={"instruction": "should be denied"},
        )
        assert assigned.status_code == 409
        assert assigned.json()["detail"]["code"] == "allowed_ai_denied"


def test_allowed_ai_admission_accepts_exact_selected_route_and_rejects_connection_change(tmp_path):
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = _project(client)
        project_id = project["project_id"]
        account = app.state.control_plane.create_provider_account(
            tenant_id="local",
            owner_id="demo-owner",
            provider="codex",
            label="Personal Codex",
            auth_method="subscription",
            platform_support="supported",
            status="ready",
            secret_locator="keychain://synthetic/account",
        )
        account_id = str(account["account_id"])
        policy = {
            "version": 1,
            "mode": "selected",
            "harnesses": [
                {
                    "profile": "codex-cli",
                    "models": {"mode": "selected", "ids": ["codex-cli:gpt-6-astra"]},
                    "connections": {"mode": "selected", "ids": [account_id]},
                }
            ],
        }
        saved = client.put(
            f"/v1/projects/{project_id}/execution-policy",
            json={"expected_revision": 0, "policy": policy},
        )
        assert saved.status_code == 200, saved.text
        paused = client.post(
            f"/v1/projects/{project_id}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "selected",
                "role": "main",
                "profile": "codex-cli",
                "execution_mode": "docker",
                "start_synchronously": False,
                "bootstrap_bundle": {
                    "provider_model": "gpt-6-astra",
                    "provider_account": {
                        "policy": "personal_required",
                        "account_id": account_id,
                    },
                },
            },
        )
        assert paused.status_code == 201, paused.text
        worker = app.state.store.get_worker(paused.json()["worker_id"])
        snapshot = app.state.service._ensure_allowed_ai(worker)
        assert snapshot["model_id"] == "codex-cli:gpt-6-astra"
        assert snapshot["connection_id"] == account_id

        narrowed = {
            **policy,
            "harnesses": [
                {
                    **policy["harnesses"][0],
                    "connections": {"mode": "selected", "ids": []},
                }
            ],
        }
        # An empty nested selection is a denial, not a reset to all.
        assert client.put(
            f"/v1/projects/{project_id}/execution-policy",
            json={"expected_revision": 1, "policy": narrowed},
        ).status_code == 200
        try:
            app.state.service._ensure_allowed_ai(worker)
        except AllowedAiAdmissionError as exc:
            assert exc.code == "allowed_ai_connection_denied"
        else:
            raise AssertionError("Allowed AI connection narrowing did not block admission")


def test_allowed_ai_start_fence_is_scope_bound_and_replay_safe(tmp_path):
    store = Store(str(tmp_path / "fence.db"))
    project = store.create_project("owner", "Fence", "Exact start", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="worker",
        role="main",
        profile="codex-cli",
        backend="stub",
        runtime="stub",
        model="gpt-6-astra",
        execution_mode="docker",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "run it")
    admission = {
        "scope_id": project["project_id"],
        "project_id": project["project_id"],
        "workspace_id": worker["workspace_id"],
        "tenant_id": "local",
        "owner_id": "owner",
        "profile": "codex-cli",
        "model_id": "codex-cli:gpt-6-astra",
        "connection_id": "acct_example",
        "project_revision": 0,
        "workspace_revision": 0,
        "policy": {"version": 1, "mode": "all_authorized", "harnesses": []},
    }
    receipt = {
        "protocol": "glasshive.native_connection_receipt.v1",
        "worker_id": worker["worker_id"],
        "run_id": run["run_id"],
        "profile": "codex-cli",
        "runtime": "stub",
        "route_kind": "legacy",
        "connection_id": "acct_example",
        "native_model": "gpt-6-astra",
    }
    first = store.record_allowed_ai_start_fence(
        run_id=run["run_id"],
        attempt_id="attempt-1",
        worker_id=worker["worker_id"],
        project_id=project["project_id"],
        workspace_id=worker["workspace_id"],
        tenant_id="local",
        owner_id="owner",
        admission=admission,
        receipt=receipt,
    )
    replay = store.record_allowed_ai_start_fence(
        run_id=run["run_id"],
        attempt_id="attempt-1",
        worker_id=worker["worker_id"],
        project_id=project["project_id"],
        workspace_id=worker["workspace_id"],
        tenant_id="local",
        owner_id="owner",
        admission=admission,
        receipt=receipt,
    )
    assert first["run_id"] == replay["run_id"] == run["run_id"]
    changed_receipt = {**receipt, "connection_id": "acct_other"}
    try:
        store.record_allowed_ai_start_fence(
            run_id=run["run_id"],
            attempt_id="attempt-1",
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            workspace_id=worker["workspace_id"],
            tenant_id="local",
            owner_id="owner",
            admission=admission,
            receipt=changed_receipt,
        )
    except AllowedAiStartFenceConflict as exc:
        assert "changed" in str(exc)
    else:
        raise AssertionError("native start receipt replay was not fenced")
    retry = store.record_allowed_ai_start_fence(
        run_id=run["run_id"],
        attempt_id="attempt-2",
        worker_id=worker["worker_id"],
        project_id=project["project_id"],
        workspace_id=worker["workspace_id"],
        tenant_id="local",
        owner_id="owner",
        admission=admission,
        receipt=receipt,
    )
    assert retry["attempt_id"] == "attempt-2"
    try:
        store.record_allowed_ai_start_fence(
            run_id=run["run_id"],
            attempt_id="attempt-3",
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            workspace_id=worker["workspace_id"],
            tenant_id="local",
            owner_id="owner",
            admission=admission,
            receipt=changed_receipt,
        )
    except AllowedAiStartFenceConflict as exc:
        assert "changed" in str(exc)
    else:
        raise AssertionError("a retry retargeted the immutable execution binding")
    try:
        store.record_allowed_ai_start_fence(
            run_id=run["run_id"],
            attempt_id="attempt-2",
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            workspace_id=worker["workspace_id"],
            tenant_id="local",
            owner_id="other-owner",
            admission=admission,
            receipt=receipt,
        )
    except AllowedAiStartFenceConflict as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("cross-owner native start scope was accepted")
    stored_run = store.get_run(run["run_id"])
    assert stored_run["allowed_ai_connection_receipt"]["connection_id"] == "acct_example"
    store.put_allowed_ai_policy(
        "project",
        project["project_id"],
        tenant_id="local",
        owner_id="owner",
        expected_revision=0,
        policy_json=json.dumps({"version": 1, "mode": "all_authorized", "harnesses": []}),
    )
    try:
        store.record_allowed_ai_start_fence(
            run_id=run["run_id"],
            attempt_id="attempt-2",
            worker_id=worker["worker_id"],
            project_id=project["project_id"],
            workspace_id=worker["workspace_id"],
            tenant_id="local",
            owner_id="owner",
            admission=admission,
            receipt=receipt,
        )
    except AllowedAiStartFenceConflict:
        pass
    else:
        raise AssertionError("native start crossed a committed policy revision")


def test_allowed_ai_execution_binding_is_immutable_and_records_lineage(tmp_path):
    store = Store(str(tmp_path / "binding.db"))
    project = store.create_project("owner", "Binding", "Lineage", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"], owner_id="owner", name="worker", role="main",
        profile="codex-cli", backend="stub", runtime="stub", model="gpt-6-astra",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "bind it")
    binding = {
        "version": 1,
        "origin": {"ref": "conversation-1", "surface": "coordinator"},
        "destination": {
            "project_id": project["project_id"],
            "workspace_id": worker["workspace_id"],
        },
        "route": {
            "profile": "codex-cli",
            "model_id": "codex-cli:gpt-6-astra",
            "native_model": "gpt-6-astra",
            "effort": "high",
            "connection_id": "acct_example",
        },
        "policy": {"project_revision": 2, "workspace_revision": 1},
    }
    stored = store.bind_execution_binding(
        run_id=run["run_id"], worker_id=worker["worker_id"],
        project_id=project["project_id"], tenant_id="local", owner_id="owner",
        binding=binding,
    )
    assert stored["execution_binding"]["origin"]["ref"] == "conversation-1"
    assert store.bind_execution_binding(
        run_id=run["run_id"], worker_id=worker["worker_id"],
        project_id=project["project_id"], tenant_id="local", owner_id="owner",
        binding=binding,
    )["execution_binding"]["route"]["connection_id"] == "acct_example"
    try:
        store.bind_execution_binding(
            run_id=run["run_id"], worker_id=worker["worker_id"],
            project_id=project["project_id"], tenant_id="local", owner_id="owner",
            binding={**binding, "route": {**binding["route"], "connection_id": "acct_other"}},
        )
    except AllowedAiStartFenceConflict as exc:
        assert "changed" in str(exc)
    else:
        raise AssertionError("execution binding was mutable")


def test_allowed_ai_admission_persists_run_route_binding(tmp_path):
    app = create_app(str(tmp_path / "service-binding.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = _project(client)
        response = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "queued",
                "role": "main",
                "profile": "codex-cli",
                "execution_mode": "docker",
                "start_synchronously": False,
            },
        )
        assert response.status_code == 201, response.text
        worker_id = response.json()["worker_id"]
        run = app.state.service.assign_run(worker_id, "persist this", start_processor=False)
        stored = app.state.store.get_run(run["run_id"])
        binding = stored["execution_binding"]
        assert binding["version"] == 1
        assert binding["destination"]["project_id"] == project["project_id"]
        assert binding["destination"]["workspace_id"]
        assert binding["route"]["profile"] == "codex-cli"
        assert binding["route"]["native_model"] == "stub/codex-cli"
        assert binding["policy"]["project_revision"] == 0


def test_allowed_ai_runtime_invocation_fence_rejects_stale_admission(tmp_path):
    app = create_app(str(tmp_path / "invocation-fence.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        project = _project(client)
        created = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "fenced",
                "role": "main",
                "profile": "codex-cli",
                "execution_mode": "docker",
                "start_synchronously": False,
            },
        )
        assert created.status_code == 201, created.text
        worker = app.state.store.get_worker(created.json()["worker_id"])
        run = app.state.service.assign_run(
            worker["worker_id"], "fence this", start_processor=False
        )
        admission = run["allowed_ai_admission"]
        assert admission["project_revision"] == 0
        claimed = app.state.store.claim_next_queued_run(
            worker["worker_id"], executor_id=app.state.service._executor_id
        )
        assert claimed is not None
        lease = app.state.store.acquire_host_run_lease(
            runtime_family="stub",
            lane="mission",
            tenant_id="local",
            owner_id="demo-owner",
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            executor_id=app.state.service._executor_id,
            conversation_limit=2,
            mission_limit=64,
            account_mission_limit=64,
            tenant_mission_limit=64,
            lease_ttl_s=300,
        )
        assert app.state.store.admit_claimed_run(
            run["run_id"],
            lease_id=lease["lease_id"],
            executor_id=app.state.service._executor_id,
        )
        with app.state.store._connect() as conn:
            conn.execute(
                "UPDATE runs SET origin_scope_json=?, allowed_ai_admission_json='{}' "
                "WHERE run_id=?",
                (json.dumps({"version": 1, "project_id": project["project_id"]}), run["run_id"]),
            )
        assert app.state.store.mark_run_runtime_invoked(
            run["run_id"],
            lease_id=lease["lease_id"],
            executor_id=app.state.service._executor_id,
        ) is None
        with app.state.store._connect() as conn:
            conn.execute(
                "UPDATE runs SET origin_scope_json='{}', allowed_ai_admission_json=? "
                "WHERE run_id=?",
                (json.dumps(admission), run["run_id"]),
            )
        app.state.store.put_allowed_ai_policy(
            "project",
            project["project_id"],
            tenant_id="local",
            owner_id="demo-owner",
            expected_revision=0,
            policy_json=json.dumps({"version": 1, "mode": "all_authorized", "harnesses": []}),
        )
        assert app.state.store.mark_run_runtime_invoked(
            run["run_id"],
            lease_id=lease["lease_id"],
            executor_id=app.state.service._executor_id,
        ) is None
        assert app.state.store.get_run(run["run_id"])["state"] == "admitted"


def test_origin_policy_still_limits_default_all_child(tmp_path):
    app = create_app(str(tmp_path / "origin-ceiling.db"), runtime_backend="stub", runtime=StubRuntime())
    with TestClient(app) as client:
        origin = _project(client)
        child = _project(client)
        origin_path = f"/v1/projects/{origin['project_id']}/execution-policy"
        denied = {"version": 1, "mode": "selected", "harnesses": []}
        assert client.put(origin_path, json={"expected_revision": 0, "policy": denied}).status_code == 200
        worker = {
            "tenant_id": "local", "owner_id": "demo-owner",
            "project_id": child["project_id"], "workspace_id": "",
            "profile": "codex-cli", "model": "stub/codex-cli",
            "origin_scope": {
                "version": 1, "tenant_id": "local", "owner_id": "demo-owner",
                "project_id": origin["project_id"], "workspace_id": "",
                "execution_mode": "host", "source_revision": 0,
            },
        }
        try:
            app.state.service.allowed_ai_policy.admission_snapshot(worker)
        except AllowedAiAdmissionError as exc:
            assert exc.code == "allowed_ai_denied"
        else:
            raise AssertionError("Child project escaped the origin policy")
        worker["origin_scope"]["project_id"] = "missing-origin"
        try:
            app.state.service.allowed_ai_policy.admission_snapshot(worker)
        except AllowedAiAdmissionError as exc:
            assert exc.code == "scope_missing"
        else:
            raise AssertionError("Missing origin scope was ignored")
