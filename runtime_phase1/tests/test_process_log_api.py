import pytest
from fastapi.testclient import TestClient
from workers_projects_runtime import api
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.profile_runtime import (
    CodexCliRuntime,
    ProfiledWorkerRuntime,
)
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.signed_links import sign_link_params
from test_process_log import event


@pytest.fixture(autouse=True)
def no_background_dispatch_in_diagnostic_fixtures(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED", "false")


class ReaderRuntime(StubRuntime):
    def __init__(self, root):
        self.codex = CodexCliRuntime(str(root))
        self.calls = []

    def read_run_process_log(self, worker, run_id, **options):
        self.calls.append((worker["worker_id"], run_id))
        return self.codex.read_run_process_log(worker, run_id, **options)

    def ensure_worker_ready(self, worker):
        raise AssertionError("Diagnostics must not prepare workers")

    def run(self, *args, **kwargs):
        raise AssertionError("Diagnostics must not execute workers")


def stored_fixture(store, runtime, owner="alice", tenant="local"):
    project = store.create_project(
        owner, "Fixture", "Read-only diagnostic fixture", "codex-cli", tenant_id=tenant
    )
    worker = store.create_worker(
        project["project_id"],
        owner,
        "Fixture",
        "fixture",
        "codex-cli",
        "docker",
        "codex-cli",
        "fixture",
        tenant_id=tenant,
    )
    old = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "PRIVATE ORIGINAL INSTRUCTIONS",
        state="failed",
    )
    new = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "PRIVATE NEW INSTRUCTIONS",
        state="running",
    )
    for run, text in ((old, "OLD public evidence"), (new, "NEW public evidence")):
        root = (
            runtime.codex._home_dir(worker["worker_id"])
            / ".glasshive-runs"
            / run["run_id"]
        )
        root.mkdir(parents=True)
        (root / "stdout.log").write_text(event(text))
        (root / "stderr.log").write_text("")
    return worker, old, new


def prohibit_healing(*args, **kwargs):
    raise AssertionError("Read-only endpoint called worker healing")


def test_native_exact_run_no_healing_no_db_writes_and_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "0")
    monkeypatch.setenv("WPR_ENTERPRISE_MODE", "0")
    monkeypatch.setattr(api, "load_viventium_runtime_env", lambda: None)
    monkeypatch.setattr(WorkersProjectsService, "heal_worker", prohibit_healing)
    runtime = ReaderRuntime(tmp_path / "runtime")
    with TestClient(api.create_app(str(tmp_path / "db.sqlite"), runtime=runtime)) as c:
        store = c.app.state.store
        worker, old, new = stored_fixture(store, runtime)
        before = [store.get_run(r["run_id"]) for r in (old, new)]
        url = f"/v1/runs/{old['run_id']}/process-log"
        assert c.get(url).status_code == 401
        headers = {"Authorization": "Bearer synthetic-service-token"}
        result = c.get(url, headers=headers)
        assert result.status_code == 200
        assert result.json()["worker_id"] == worker["worker_id"]
        assert "OLD public evidence" in result.json()["text"]
        assert (
            "NEW public evidence" not in result.text
            and "PRIVATE ORIGINAL" not in result.text
        )
        new_result = c.get(f"/v1/runs/{new['run_id']}/process-log", headers=headers)
        assert "NEW public evidence" in new_result.text
        assert (
            c.get(
                url, params={"stream": "../../auth.json"}, headers=headers
            ).status_code
            == 400
        )
        assert before == [store.get_run(r["run_id"]) for r in (old, new)]
        assert len(runtime.calls) == 3
        foreign = store.create_project("alice", "Foreign project", "Synthetic ownership mismatch", "codex-cli")
        store.update_worker(worker["worker_id"], project_id=foreign["project_id"])
        assert c.get(url, headers=headers).status_code == 404
        assert len(runtime.calls) == 3


def test_unconfigured_auth_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("WPR_API_TOKEN", raising=False)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "0")
    monkeypatch.setenv("WPR_ENTERPRISE_MODE", "0")
    monkeypatch.setattr(api, "load_viventium_runtime_env", lambda: None)
    with TestClient(
        api.create_app(str(tmp_path / "db.sqlite"), runtime=StubRuntime())
    ) as c:
        assert c.get("/v1/runs/unknown/process-log").status_code == 403


def test_attempt_selection_is_owned_and_cannot_fall_back_to_legacy_output(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-token")
    runtime = ReaderRuntime(tmp_path / "runtime")
    with TestClient(api.create_app(str(tmp_path / "db.sqlite"), runtime=runtime)) as client:
        store = client.app.state.store
        worker, old, new = stored_fixture(store, runtime)
        for attempt, run, number in [("attempt-one", old, 1), ("attempt-two", old, 2), ("attempt-foreign", new, 1)]:
            with store._connect() as connection:
                connection.execute(
                    "INSERT INTO run_attempts (attempt_id, run_id, attempt_number, state, claimed_at) VALUES (?, ?, ?, 'completed', '2026-01-01T00:00:00+00:00')",
                    (attempt, run["run_id"], number),
                )
            root = runtime.codex._attempt_run_root(worker["worker_id"], run["run_id"], attempt)
            root.mkdir(parents=True)
            (root / "stdout.log").write_text(event(attempt + " exact evidence"))
        store.update_run(old["run_id"], active_attempt_id="attempt-two")
        url = f"/v1/runs/{old['run_id']}/process-log"
        headers = {"Authorization": "Bearer synthetic-service-token"}
        current = client.get(url, headers=headers).json()
        assert current["attempt_id"] == "attempt-two" and "attempt-two exact evidence" in current["text"]
        earlier = client.get(url, headers=headers, params={"attempt_id": "attempt-one"}).json()
        assert earlier["attempt_id"] == "attempt-one" and "attempt-one exact evidence" in earlier["text"]
        assert "OLD public evidence" not in earlier["text"]
        assert client.get(url, headers=headers, params={"attempt_id": "attempt-foreign"}).status_code == 404
        assert client.get(url, headers=headers, params={"attempt_id": "missing"}).status_code == 404
        runtime.codex._attempt_run_root(worker["worker_id"], old["run_id"], "attempt-two").joinpath("stdout.log").unlink()
        assert client.get(url, headers=headers).status_code == 404


def test_enterprise_operator_owner_tenant_and_member_checks(tmp_path, monkeypatch):
    for key, val in {
        "WPR_API_TOKEN": "synthetic-service-token",
        "GLASSHIVE_ENTERPRISE_MODE": "1",
        "GLASSHIVE_AUTH_MODE": "first_party_assertion",
        "GLASSHIVE_ENTERPRISE_TENANT_ID": "fixture-tenant",
        "GLASSHIVE_SIGNED_LINK_SECRET": "synthetic-distinct-signing-secret",
    }.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(api, "load_viventium_runtime_env", lambda: None)
    monkeypatch.setattr(WorkersProjectsService, "heal_worker", prohibit_healing)
    runtime = ReaderRuntime(tmp_path / "runtime")
    with TestClient(api.create_app(str(tmp_path / "db.sqlite"), runtime=runtime)) as c:
        worker, old, new = stored_fixture(
            c.app.state.store, runtime, tenant="fixture-tenant"
        )
        url = f"/v1/runs/{old['run_id']}/process-log"
        h = {
            "Authorization": "Bearer synthetic-service-token",
            "x-viventium-user-id": "alice",
            "x-viventium-user-role": "operator",
        }
        assert c.get(url, headers=h).status_code == 200
        assert (
            c.get(url, headers={**h, "x-viventium-user-id": "bob"}).status_code == 404
        )
        assert (
            c.get(url, headers={**h, "x-viventium-user-role": "member"}).status_code
            == 403
        )
        assert (
            c.get(url, headers={**h, "x-viventium-tenant-id": "different"}).status_code
            == 401
        )
        params = sign_link_params(
            kind="worker_view",
            worker_id=worker["worker_id"],
            tenant_id="fixture-tenant",
            owner_id="alice",
        )
        assert c.get(url, params=params).status_code == 401
        assert len(runtime.calls) == 1


def test_profile_delegation_does_not_create_or_heal_worker(tmp_path):
    runtime = ProfiledWorkerRuntime(str(tmp_path))
    worker = {
        "worker_id": "fixture-worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }
    before = set(tmp_path.rglob("*"))
    with pytest.raises(FileNotFoundError):
        runtime.read_run_process_log(worker, "missing-run")
    assert set(tmp_path.rglob("*")) == before
