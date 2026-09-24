from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import pytest

from workers_projects_runtime.control_plane import ControlPlaneError, ControlPlaneStore
from workers_projects_runtime.api import _build_runtime
from workers_projects_runtime.failure_classification import classify_cli_failure
from workers_projects_runtime.mission_provider_accounts import (
    MissionProviderAccountBinder,
    deployment_provider_readiness,
)
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager
from workers_projects_runtime.profile_runtime import (
    HostClaudeCodeRuntime,
    HostCodexCliRuntime,
    ProfiledWorkerRuntime,
)


class RecordingRuntime:
    def __init__(self, callback=None) -> None:
        self.callback = callback
        self.worker: dict | None = None
        self.released_workers: list[str] = []
        self.reconciled_homes: list[Path] = []
        self.release_callback = None
        self.desktop_exit_callback = None
        self.ensure_calls = 0
        self.ensured_workers: list[dict] = []
        self.completed_run: dict[str, object] | None = None

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.ensure_calls += 1
        self.ensured_workers.append(dict(worker))
        return RuntimeInfo(
            runtime=str(worker.get("profile") or "synthetic"),
            model="synthetic",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir="",
            workspace_dir="",
            pid=1,
        )

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        self.worker = dict(worker)
        if self.callback is not None:
            self.callback(worker, instruction, timeout_sec, run_id)
        return "synthetic result"

    def release_provider_account_binding(self, worker: dict) -> None:
        self.released_workers.append(str(worker.get("worker_id") or ""))
        if self.release_callback is not None:
            self.release_callback(worker)

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
        on_exit=None,
        max_runtime_seconds: float | None = None,
    ) -> dict[str, object]:
        self.worker = dict(worker)
        self.desktop_exit_callback = on_exit
        return {
            "status": "launched",
            "action": action,
            "url": url,
            "run_id": run_id,
        }

    def reconcile_provider_account_binding(self, account_home: Path) -> None:
        self.reconciled_homes.append(Path(account_home))

    def collect_completed_run(
        self,
        _worker: dict,
        run_id: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, object] | None:
        del run_id, instruction
        return dict(self.completed_run) if self.completed_run is not None else None


def _account(
    store: ControlPlaneStore,
    *,
    provider: str = "codex",
    owner_id: str = "user-a",
    auth_method: str = "subscription",
) -> dict:
    return store.create_provider_account(
        tenant_id="tenant-a",
        owner_id=owner_id,
        provider=provider,
        label=f"{provider.title()} account",
        auth_method=auth_method,
        platform_support="supported",
        secret_locator=f"native-home://{provider}-{owner_id}",
        status="ready",
    )


def _worker(account_id: str | None, *, profile: str = "codex-cli", policy: str = "personal_required") -> dict:
    selection: dict[str, str] = {"policy": policy}
    if account_id is not None:
        selection["account_id"] = account_id
    return {
        "worker_id": "wrk_personal",
        "tenant_id": "tenant-a",
        "owner_id": "user-a",
        "profile": profile,
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "mission", "provider_account": selection}
        ),
    }


def test_multi_user_docker_mission_projects_only_the_selected_account_home(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    observed: dict[str, object] = {}

    def inspect_binding(worker, _instruction, _timeout_sec, _run_id):
        observed["worker"] = dict(worker)

    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.codex = RecordingRuntime(inspect_binding)  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container"
    )
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"

    runtime.run_task(worker, "isolated mission", run_id="run_container_isolated")

    bound = observed["worker"]
    assert bound["_glasshive_provider_account_env"] == {  # type: ignore[index]
        "CODEX_HOME": "/workspace/.wpr-home/.codex"
    }
    mount_home = Path(  # type: ignore[index]
        bound["_glasshive_provider_account_mount_host"]
    )
    assert mount_home.is_dir()
    assert bound["_glasshive_provider_account_mount_target"] == (  # type: ignore[index]
        "/workspace/.provider-account"
    )
    assert store.active_provider_lease(
        account["account_id"], "codex-cli:mission"
    ) is None
    assert runtime.codex.released_workers == ["wrk_personal"]  # type: ignore[attr-defined]


def test_expired_personal_claude_session_requires_reconnect_and_releases_lease(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="claude")

    def fail_with_expired_session(_worker, _instruction, _timeout_sec, _run_id):
        failure = classify_cli_failure(
            stdout="\n".join(
                (
                    json.dumps(
                        {
                            "type": "assistant",
                            "error": "authentication_failed",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "is_error": True,
                            "api_error_status": None,
                        }
                    ),
                )
            ),
            stderr="",
            runtime_name="claude-code",
            exit_code=1,
        )
        error = RuntimeErrorBase("claude-code exited with code 1")
        error.failure_classification = failure  # type: ignore[attr-defined]
        raise error

    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    recorder = RecordingRuntime(fail_with_expired_session)
    runtime.claude = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    worker = _worker(account["account_id"], profile="claude-code")
    worker["execution_mode"] = "docker"

    with pytest.raises(RuntimeErrorBase) as failed:
        runtime.run_task(worker, "reuse the saved workspace", run_id="run_expired_claude")

    assert getattr(failed.value, "failure_classification", None) is not None, str(
        failed.value
    )
    updated = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )
    assert updated is not None
    assert updated["status"] == "action_required"
    assert updated["recovery_code"] == ""
    assert "Reconnect" in updated["reconnect_reason"]
    assert (
        failed.value.failure_classification.recommended_recovery  # type: ignore[attr-defined]
        == "Open Connections, reconnect the selected account, then continue the same workspace."
    )
    assert (
        failed.value.failure_classification.diagnostic_summary  # type: ignore[attr-defined]
        == "class=provider_auth_missing; runtime=claude-code"
    )
    assert store.active_provider_lease(
        account["account_id"], "claude-code:mission"
    ) is None
    assert recorder.released_workers == ["wrk_personal"]


def test_completed_personal_claude_auth_failure_keeps_connections_recovery(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="claude")
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    recorder = RecordingRuntime()
    recorder.completed_run = {
        "state": "failed",
        "output_text": "",
        "error_text": "claude-code exited with code 1",
        "failure_class": "provider_auth_missing",
        "failure_retryable": 0,
        "failure_user_message": "The worker could not use provider credentials.",
        "failure_recommended_recovery": "Fix the CLI login.",
        "failure_diagnostic_summary": "class=provider_auth_missing; exit_code=1",
        "_glasshive_personal_account_reconnect_required": True,
    }
    runtime.claude = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    worker = _worker(account["account_id"], profile="claude-code")
    worker["execution_mode"] = "docker"

    recovered = runtime.collect_completed_run(
        worker,
        run_id="run_expired_claude",
        instruction="reuse the saved workspace",
    )

    assert recovered is not None
    assert recovered["failure_user_message"] == (
        "The selected connected AI account must be reconnected."
    )
    assert recovered["failure_recommended_recovery"] == (
        "Open Connections, reconnect the selected account, then continue the same workspace."
    )
    assert recovered["failure_diagnostic_summary"] == (
        "class=provider_auth_missing; runtime=claude-code"
    )
    assert recovered["error_text"] == (
        "The selected connected AI account must be reconnected."
    )
    updated = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )
    assert updated is not None
    assert updated["status"] == "action_required"
    assert "Reconnect" in updated["reconnect_reason"]


def test_broker_auth_failure_does_not_claim_personal_subscription_expired(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="codex", auth_method="api_key")

    class FakeInferenceBroker:
        @contextmanager
        def bind_run(self, **_kwargs):
            yield {"adapter": "synthetic-broker"}

    def fail_broker_route(_worker, _instruction, _timeout_sec, _run_id):
        failure = classify_cli_failure(
            stdout="",
            stderr="HTTP status 401 Unauthorized",
            runtime_name="codex-cli",
            exit_code=1,
        )
        error = RuntimeErrorBase("codex-cli exited with code 1")
        error.failure_classification = failure  # type: ignore[attr-defined]
        raise error

    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = RecordingRuntime(fail_broker_route)  # type: ignore[assignment]
    runtime.inference_broker = FakeInferenceBroker()  # type: ignore[assignment]
    worker = {**_worker(account["account_id"]), "model": "gpt-synthetic"}

    with pytest.raises(RuntimeErrorBase) as failed:
        runtime.run_task(worker, "broker mission", run_id="run_broker_auth")

    updated = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )
    assert updated is not None
    assert updated["status"] == "ready"
    assert (
        failed.value.failure_classification.recommended_recovery  # type: ignore[attr-defined]
        != "Open Connections, reconnect the selected account, then continue the same workspace."
    )


def test_worker_tool_401_does_not_disable_selected_personal_subscription(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="claude")

    def fail_with_incidental_401(_worker, _instruction, _timeout_sec, _run_id):
        failure = classify_cli_failure(
            stdout=json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "error": "tool_failed",
                    "result": "A worker tool returned HTTP status 401 Unauthorized.",
                }
            ),
            stderr="",
            runtime_name="claude-code",
            exit_code=1,
        )
        error = RuntimeErrorBase("claude-code exited with code 1")
        error.failure_classification = failure  # type: ignore[attr-defined]
        raise error

    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    recorder = RecordingRuntime(fail_with_incidental_401)
    runtime.claude = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    worker = _worker(account["account_id"], profile="claude-code")
    worker["execution_mode"] = "docker"

    with pytest.raises(RuntimeErrorBase):
        runtime.run_task(worker, "debug a worker tool", run_id="run_tool_401")

    updated = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )
    assert updated is not None
    assert updated["status"] == "ready"
    assert store.active_provider_lease(
        account["account_id"], "claude-code:mission"
    ) is None
    assert recorder.released_workers == ["wrk_personal"]


def test_startup_recovery_removes_orphaned_interactive_provider_binding(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    recorder = RecordingRuntime()
    lease_during_cleanup: list[bool] = []
    recorder.release_callback = lambda _worker: lease_during_cleanup.append(
        any(
            row["account_id"] == account["account_id"]
            for row in store.unreleased_interactive_provider_leases()
        )
    )
    runtime.codex = recorder  # type: ignore[assignment]
    lease = store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="codex-cli:interactive",
        worker_id="wrk_interrupted_setup",
        run_id="interactive_interrupted_setup",
        ttl_seconds=180,
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE provider_account_leases SET expires_at = ? WHERE lease_id = ?",
            (time.time() - 1, lease["lease_id"]),
        )

    runtime.recover_interactive_provider_sessions()

    assert recorder.released_workers == ["wrk_interrupted_setup"]
    assert lease_during_cleanup == [True]
    assert store.active_provider_account_lease(account["account_id"]) is None
    with store._connect() as conn:
        recovered = conn.execute(
            "SELECT released_at FROM provider_account_leases WHERE lease_id = ?",
            (lease["lease_id"],),
        ).fetchone()
    assert recovered is not None and recovered["released_at"] is not None


def test_provider_bound_preflight_can_recreate_a_stale_task_sandbox(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    recorder = RecordingRuntime()
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = {
        **_worker(account["account_id"]),
        "execution_mode": "docker",
        "state": "running",
    }

    runtime.run_task(worker, "resume the saved workspace", run_id="run_recreate")

    assert recorder.ensured_workers
    assert recorder.ensured_workers[0]["_glasshive_task_run"] is True
    assert recorder.ensured_workers[0]["_active_run_id"] == "run_recreate"
    assert recorder.worker is not None
    assert "_glasshive_task_run" not in recorder.worker
    assert "_active_run_id" not in recorder.worker
    assert "_glasshive_task_run" not in worker
    assert "_active_run_id" not in worker


def test_docker_provider_mount_is_removed_before_lease_release(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    recorder = RecordingRuntime()
    observed: dict[str, object] = {}
    cleanup_order: list[str] = []

    def observe_release(_worker: dict) -> None:
        cleanup_order.append("container_removed")
        observed["lease_during_release"] = store.active_provider_lease(
            account["account_id"], "codex-cli:mission"
        )

    def observe_permission_tightening(self, *, account_home: Path) -> None:
        assert account_home.is_dir()
        cleanup_order.append("permissions_tightened")

    recorder.release_callback = observe_release
    monkeypatch.setattr(
        ProviderAccountHomeManager,
        "tighten_permissions",
        observe_permission_tightening,
    )
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"

    runtime.run_task(worker, "release the mount", run_id="run_release_mount")

    assert recorder.released_workers == ["wrk_personal"]
    assert cleanup_order == ["container_removed", "permissions_tightened"]
    assert observed["lease_during_release"] is not None
    assert store.active_provider_lease(account["account_id"], "codex-cli:mission") is None


def test_foreground_conversation_binds_exact_owner_account_and_start_receipt(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    selected = _account(store, owner_id="user-a")
    foreign = _account(store, owner_id="user-b")
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(database)
    )
    recorder = RecordingRuntime()
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    admitted: list[dict] = []
    runtime.provider_account_binder.configure_allowed_ai_start(
        lambda worker, receipt: admitted.append(dict(receipt))
    )
    worker = {**_worker(selected["account_id"]), "execution_mode": "docker"}
    worker["bootstrap_bundle_json"] = json.dumps({
        "run_mode": "conversation",
        "provider_account": {
            "policy": "personal_required", "account_id": selected["account_id"]
        },
        "connection_id": selected["account_id"],
    })
    worker["_allowed_ai_admission"] = {"connection_id": selected["account_id"]}
    assert runtime._run_task_with_provider_account(
        worker, "Synthetic foreground work", timeout_sec=30, run_id="foreground-a"
    ) == "synthetic result"
    assert recorder.worker is not None
    assert recorder.worker["_glasshive_provider_account_bound"] is True
    assert recorder.worker["_glasshive_provider_account_mount_host"] == str(
        runtime.provider_account_binder.homes.account_home_path(
            tenant_id="tenant-a", owner_id="user-a", account_id=selected["account_id"]
        )
    )
    assert admitted[0]["connection_id"] == selected["account_id"]
    assert admitted[0]["route_kind"] == "native"
    assert store.active_provider_account_lease(selected["account_id"]) is None

    wrong_owner = json.loads(worker["bootstrap_bundle_json"])
    wrong_owner["provider_account"]["account_id"] = foreign["account_id"]
    wrong_owner["connection_id"] = foreign["account_id"]
    with pytest.raises(RuntimeErrorBase, match="not available for this user"):
        runtime._run_task_with_provider_account(
            {**worker, "bootstrap_bundle_json": json.dumps(wrong_owner)},
            "Must not run", timeout_sec=30, run_id="foreground-foreign",
        )
    store.update_provider_account_status(
        account_id=selected["account_id"], tenant_id="tenant-a", owner_id="user-a",
        status="action_required", reconnect_reason="Reconnect this account",
    )
    with pytest.raises(RuntimeErrorBase, match="not ready"):
        runtime._run_task_with_provider_account(
            worker, "Must not run", timeout_sec=30, run_id="foreground-revoked"
        )
    assert len(admitted) == 1
    store.update_provider_account_status(
        account_id=selected["account_id"], tenant_id="tenant-a", owner_id="user-a",
        status="ready", reconnect_reason="", verified=True,
    )
    restarted = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(database)
    )
    restarted_recorder = RecordingRuntime()
    restarted.codex = restarted_recorder  # type: ignore[assignment]
    restarted.provider_account_binder.configure_allowed_ai_start(
        lambda _worker, receipt: admitted.append(dict(receipt))
    )
    assert restarted._run_task_with_provider_account(
        worker, "Continue after reconnect", timeout_sec=30, run_id="foreground-restarted"
    ) == "synthetic result"
    assert restarted_recorder.worker is not None
    assert restarted_recorder.worker["_glasshive_provider_account_mount_host"] == str(
        runtime.provider_account_binder.homes.account_home_path(
            tenant_id="tenant-a", owner_id="user-a", account_id=selected["account_id"]
        )
    )
    assert [receipt["connection_id"] for receipt in admitted] == [
        selected["account_id"], selected["account_id"]
    ]


def test_foreground_selected_account_requires_durable_start_admission(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(database)
    )
    runtime.codex = RecordingRuntime()  # type: ignore[assignment]
    worker = _worker(account["account_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["run_mode"] = "conversation"
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    with pytest.raises(RuntimeErrorBase, match="missing its native start admission"):
        runtime._run_task_with_provider_account(
            worker, "Must not run", timeout_sec=30, run_id="foreground-unadmitted"
        )


def test_legacy_foreground_connection_receipt_without_binding_fails_closed(tmp_path):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path))
    recorder = RecordingRuntime()
    runtime.codex = recorder  # type: ignore[assignment]
    worker = {
        "worker_id": "worker-legacy", "tenant_id": "local", "owner_id": "owner-a",
        "profile": "codex-cli", "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({
            "run_mode": "conversation", "connection_id": "account-without-binding"
        }),
    }
    with pytest.raises(RuntimeErrorBase, match="no validated native account binding"):
        runtime._run_task_with_provider_account(
            worker, "Must not run", timeout_sec=30, run_id="legacy-route"
        )
    assert recorder.worker is None


def test_stale_provider_mount_reconciliation_fails_closed_before_second_binding(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    recorder = RecordingRuntime()

    def refuse_stale_mount(_account_home: Path) -> None:
        raise RuntimeError("synthetic stale provider mount")

    recorder.reconcile_provider_account_binding = refuse_stale_mount  # type: ignore[method-assign]
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"

    with pytest.raises(RuntimeErrorBase, match="stale provider credentials"):
        runtime.run_task(worker, "must not overlap", run_id="run-after-crash")

    quarantined = store.get_provider_account(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )
    assert quarantined is not None
    assert quarantined["status"] == "action_required"
    assert quarantined["recovery_code"] == "credential_cleanup_failed"
    assert store.active_provider_lease(account["account_id"], "codex-cli:mission") is None
    assert recorder.worker is None


def test_cleanup_failure_quarantines_account_but_releases_exclusive_lease(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(database)
    )
    recorder = RecordingRuntime()
    recorder.release_callback = lambda _worker: (_ for _ in ()).throw(
        PermissionError("synthetic mapped uid")
    )
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"

    with pytest.raises(RuntimeErrorBase, match="account was quarantined"):
        runtime.run_task(worker, "complete before cleanup", run_id="run_cleanup_failure")

    updated = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert updated["status"] == "action_required"
    assert updated["recovery_code"] == "credential_cleanup_failed"
    assert store.active_provider_lease(account["account_id"], "codex-cli:mission") is None


def test_transient_cleanup_failure_retries_before_quarantining_account(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(database)
    )
    recorder = RecordingRuntime()
    release_attempts = 0

    def transient_cleanup(_worker):
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise PermissionError("synthetic transient mount release")

    recorder.release_callback = transient_cleanup
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"

    assert runtime.run_task(
        worker,
        "complete before transient cleanup",
        run_id="run_transient_cleanup",
    ) == "synthetic result"

    updated = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert release_attempts == 2
    assert updated["status"] == "ready"
    assert updated["recovery_code"] == ""
    assert store.active_provider_lease(account["account_id"], "codex-cli:mission") is None


def test_transient_home_tightening_failure_retries_full_idempotent_cleanup(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(database)
    )
    recorder = RecordingRuntime()
    cleanup_events: list[str] = []
    tighten_attempts = 0

    recorder.release_callback = lambda _worker: cleanup_events.append("release")

    def transient_tighten(_manager, *, account_home):
        nonlocal tighten_attempts
        tighten_attempts += 1
        cleanup_events.append(f"tighten:{tighten_attempts}")
        if tighten_attempts == 1:
            raise ControlPlaneError("synthetic transient account-home debris")

    monkeypatch.setattr(
        ProviderAccountHomeManager,
        "tighten_permissions",
        transient_tighten,
    )
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"

    assert runtime.run_task(
        worker,
        "complete before transient account-home cleanup",
        run_id="run_transient_home_cleanup",
    ) == "synthetic result"

    updated = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert cleanup_events == ["release", "tighten:1", "release", "tighten:2"]
    assert updated["status"] == "ready"
    assert updated["recovery_code"] == ""
    assert store.active_provider_lease(account["account_id"], "codex-cli:mission") is None


def test_lease_heartbeat_loss_stops_docker_binding_and_fails_mission(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )

    def slow_run(_worker, _instruction, _timeout_sec, _run_id):
        time.sleep(0.14)

    recorder = RecordingRuntime(slow_run)
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_LEASE_HEARTBEAT_SECONDS", "0.05")

    def fail_heartbeat(self, **_kwargs):
        raise ControlPlaneError("synthetic lease store outage")

    monkeypatch.setattr(ControlPlaneStore, "heartbeat_provider_lease", fail_heartbeat)
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"

    with pytest.raises(RuntimeErrorBase, match="lease renewal failed"):
        runtime.run_task(worker, "must stop on lease loss", run_id="run_lease_loss")

    assert recorder.released_workers == ["wrk_personal"]
    updated = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert updated is not None
    assert updated["status"] == "action_required"


def test_sqlite_lease_heartbeat_loss_stops_docker_binding_and_fails_mission(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )

    def slow_run(_worker, _instruction, _timeout_sec, _run_id):
        time.sleep(0.14)

    recorder = RecordingRuntime(slow_run)
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_LEASE_HEARTBEAT_SECONDS", "0.05")

    def fail_heartbeat(self, **_kwargs):
        raise sqlite3.OperationalError("synthetic database outage")

    monkeypatch.setattr(ControlPlaneStore, "heartbeat_provider_lease", fail_heartbeat)
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"

    with pytest.raises(RuntimeErrorBase, match="lease renewal failed"):
        runtime.run_task(worker, "must stop on sqlite lease loss", run_id="run_sqlite_lease_loss")

    assert recorder.released_workers == ["wrk_personal"]


def test_profiled_runtime_binds_private_home_and_holds_exclusive_lease_for_entire_mission(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    observed: dict[str, object] = {}

    def inspect_during_run(worker, _instruction, _timeout_sec, _run_id):
        observed["env"] = dict(worker["_glasshive_provider_account_env"])
        observed["lease"] = store.active_provider_lease(
            account["account_id"], "codex-cli:mission"
        )

    recorder = RecordingRuntime(inspect_during_run)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = recorder  # type: ignore[assignment]

    assert runtime.run_task(
        _worker(account["account_id"]),
        "Run the synthetic mission",
        run_id="run_personal",
    ) == "synthetic result"

    assert observed["lease"] is not None
    assert observed["lease"]["worker_id"] == "wrk_personal"  # type: ignore[index]
    private_home = Path(observed["env"]["CODEX_HOME"])  # type: ignore[index]
    assert private_home.is_dir()
    assert private_home.parent.parent.parent.parent == tmp_path / "provider_accounts"
    assert private_home != Path.home() / ".codex"
    assert store.active_provider_lease(account["account_id"], "codex-cli:mission") is None


def test_desktop_action_projects_the_exact_active_mission_provider_binding(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"
    recorder = RecordingRuntime()
    runtime.codex = recorder  # type: ignore[assignment]
    with runtime.provider_account_binder.bind(
        worker,
        runtime_name="codex-cli",
        run_id="run_personal",
        timeout_sec=60,
        release_binding=lambda _worker: None,
        reconcile_binding=lambda _home: None,
    ) as bound:
        runtime.provider_account_binder.mark_active_route_ready(
            bound,
            runtime_name="codex-cli",
            run_id="run_personal",
        )
        account_home = Path(bound["_glasshive_provider_account_mount_host"])
        launched = runtime.desktop_action(
            worker,
            "browser",
            url="file:///workspace/project/index.html",
            run_id="run_personal",
        )
        assert launched["status"] == "launched"
        assert recorder.worker is not None
        assert recorder.worker["_glasshive_provider_account_bound"] is True
        assert recorder.worker["_glasshive_provider_account_env"] == {
            "CODEX_HOME": "/workspace/.wpr-home/.codex"
        }
        assert recorder.worker["_glasshive_provider_account_mount_host"] == str(
            account_home.resolve(strict=True)
        )
        assert recorder.worker["_glasshive_provider_account_mount_target"] == (
            "/workspace/.provider-account"
        )
    assert store.active_provider_lease(
        account["account_id"], "codex-cli:mission"
    ) is None


def test_claude_mission_keeps_workspace_tools_and_leases_only_secure_storage(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="claude")
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    worker = _worker(account["account_id"], profile="claude-code")
    worker["execution_mode"] = "docker"

    with runtime.provider_account_binder.bind(
        worker,
        runtime_name="claude-code",
        run_id="run_personal",
        timeout_sec=60,
        release_binding=lambda _worker: None,
        reconcile_binding=lambda _home: None,
    ) as bound:
        assert bound["_glasshive_provider_account_env"] == {
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/workspace/.provider-account/claude",
        }

    assert store.active_provider_lease(
        account["account_id"], "claude-code:mission"
    ) is None


@pytest.mark.parametrize(
    ("provider", "profile", "runtime_attr", "action"),
    (
        ("codex", "codex-cli", "codex", "codex"),
        ("claude", "claude-code", "claude", "claude"),
    ),
)
def test_idle_personal_workspace_native_setup_borrows_account_until_window_closes(
    tmp_path, monkeypatch, provider, profile, runtime_attr, action
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider=provider)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    if provider == "claude":
        monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    worker = {
        **_worker(account["account_id"], profile=profile),
        "execution_mode": "docker",
    }
    recorder = RecordingRuntime()
    setattr(runtime, runtime_attr, recorder)

    launched = runtime.desktop_action(worker, action)

    assert launched["status"] == "launched"
    assert recorder.worker is not None
    assert recorder.worker["_glasshive_provider_account_bound"] is True
    assert recorder.worker["_glasshive_provider_account_mount_host"]
    assert callable(recorder.desktop_exit_callback)
    lease = store.active_provider_lease(account["account_id"], f"{profile}:interactive")
    assert lease is not None
    assert lease["worker_id"] == worker["worker_id"]
    assert str(lease["run_id"]).startswith("interactive_")

    recorder.desktop_exit_callback()
    recorder.desktop_exit_callback()

    assert store.active_provider_lease(
        account["account_id"], f"{profile}:interactive"
    ) is None
    assert recorder.released_workers == [worker["worker_id"]]


def test_idle_non_harness_desktop_action_stays_unbound(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="claude")
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = {
        **_worker(
            account["account_id"],
            profile="claude-code",
            policy="personal_preferred",
        ),
        "execution_mode": "docker",
    }
    recorder = RecordingRuntime()
    runtime.claude = recorder  # type: ignore[assignment]

    launched = runtime.desktop_action(worker, "browser", url="about:blank")

    assert launched["status"] == "launched"
    assert recorder.worker is not None
    assert "_glasshive_provider_account_bound" not in recorder.worker
    assert recorder.desktop_exit_callback is None
    assert store.active_provider_account_lease(account["account_id"]) is None


def test_idle_api_key_workspace_setup_does_not_mount_a_subscription_home(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="codex", auth_method="api_key")
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = {
        **_worker(account["account_id"], profile="codex-cli"),
        "execution_mode": "docker",
    }
    recorder = RecordingRuntime()
    runtime.codex = recorder  # type: ignore[assignment]

    launched = runtime.desktop_action(worker, "codex")

    assert launched["status"] == "launched"
    assert recorder.worker is not None
    assert "_glasshive_provider_account_bound" not in recorder.worker
    assert store.active_provider_account_lease(account["account_id"]) is None


def test_idle_native_setup_busy_account_fails_before_launch(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="claude")
    store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="claude-code:mission",
        worker_id="wrk_other",
        run_id="run_other",
        ttl_seconds=180,
    )
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    worker = {
        **_worker(
            account["account_id"],
            profile="claude-code",
            policy="personal_preferred",
        ),
        "execution_mode": "docker",
    }
    recorder = RecordingRuntime()
    runtime.claude = recorder  # type: ignore[assignment]

    with pytest.raises(RuntimeErrorBase, match="already in use"):
        runtime.desktop_action(worker, "claude")

    assert recorder.worker is None


def test_idle_native_setup_launch_failure_releases_account(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, provider="claude")
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    worker = {
        **_worker(account["account_id"], profile="claude-code"),
        "execution_mode": "docker",
    }

    class FailingRuntime(RecordingRuntime):
        def desktop_action(self, worker, action, **kwargs):
            self.worker = dict(worker)
            raise RuntimeErrorBase("synthetic launch failed")

    recorder = FailingRuntime()
    runtime.claude = recorder  # type: ignore[assignment]

    with pytest.raises(RuntimeErrorBase, match="synthetic launch failed"):
        runtime.desktop_action(worker, "claude")

    assert recorder.released_workers == [worker["worker_id"]]
    assert store.active_provider_account_lease(account["account_id"]) is None


def test_mission_cleanup_waits_for_a_borrowed_desktop_action_projection(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"
    mission_bound = Event()
    finish_mission = Event()
    mission_finished = Event()
    action_borrowed = Event()
    release_action = Event()
    binding_released = Event()
    thread_errors: list[BaseException] = []

    def run_mission() -> None:
        try:
            with runtime.provider_account_binder.bind(
                worker,
                runtime_name="codex-cli",
                run_id="run_personal",
                timeout_sec=60,
                release_binding=lambda _worker: binding_released.set(),
                reconcile_binding=lambda _home: None,
            ) as bound:
                runtime.provider_account_binder.mark_active_route_ready(
                    bound,
                    runtime_name="codex-cli",
                    run_id="run_personal",
                )
                mission_bound.set()
                finish_mission.wait()
        except BaseException as exc:  # pragma: no cover - asserted in the parent thread
            thread_errors.append(exc)
        finally:
            mission_finished.set()

    def borrow_action() -> None:
        try:
            mission_bound.wait()
            with runtime.provider_account_binder.project_active_binding(
                worker,
                runtime_name="codex-cli",
                run_id="run_personal",
            ):
                action_borrowed.set()
                release_action.wait()
        except BaseException as exc:  # pragma: no cover - asserted in the parent thread
            thread_errors.append(exc)

    mission_thread = Thread(target=run_mission, daemon=True)
    action_thread = Thread(target=borrow_action, daemon=True)
    mission_thread.start()
    action_thread.start()
    assert action_borrowed.wait(10)

    finish_mission.set()
    with runtime.provider_account_binder._active_binding_condition:
        assert runtime.provider_account_binder._active_binding_condition.wait_for(
            lambda: bool(
                runtime.provider_account_binder._active_bindings.get("wrk_personal", {}).get(
                    "closing"
                )
            ),
            timeout=10,
        )
    assert not binding_released.is_set()
    assert not mission_finished.is_set()

    release_action.set()
    action_thread.join(timeout=10)
    mission_thread.join(timeout=10)
    assert not action_thread.is_alive()
    assert not mission_thread.is_alive()
    assert binding_released.is_set()
    assert mission_finished.is_set()
    assert thread_errors == []
    assert store.active_provider_lease(
        account["account_id"], "codex-cli:mission"
    ) is None


def test_desktop_action_rejects_stale_or_cross_worker_provider_binding(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    worker = _worker(account["account_id"])
    worker["execution_mode"] = "docker"
    ProviderAccountHomeManager(tmp_path / "provider_accounts").ensure_home(
        tenant_id="tenant-a",
        owner_id="user-a",
        account_id=account["account_id"],
        provider="codex",
    )

    with pytest.raises(RuntimeErrorBase, match="ready provider route"):
        with runtime.provider_account_binder.project_active_binding(
            worker,
            runtime_name="codex-cli",
            run_id="run_personal",
        ):
            pass

    lease = store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="codex-cli:mission",
        worker_id="wrk_other",
        run_id="run_personal",
        ttl_seconds=60,
    )
    try:
        with pytest.raises(RuntimeErrorBase, match="ready provider route"):
            with runtime.provider_account_binder.project_active_binding(
                worker,
                runtime_name="codex-cli",
                run_id="run_personal",
            ):
                pass
    finally:
        store.release_provider_lease(
            lease_id=lease["lease_id"], tenant_id="tenant-a", owner_id="user-a"
        )


def test_second_account_for_same_worker_is_rejected_before_first_container_cleanup(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    first_account = _account(store)
    second_account = _account(store)
    binder = MissionProviderAccountBinder(
        db_path=str(database),
        home_root=tmp_path / "provider_accounts",
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    first_active = Event()
    release_first = Event()
    first_finished = Event()
    first_releases: list[str] = []
    second_releases: list[str] = []
    second_reconciles: list[Path] = []
    errors: list[BaseException] = []

    def hold_first_account() -> None:
        try:
            with binder.bind(
                {**_worker(first_account["account_id"]), "execution_mode": "docker"},
                runtime_name="codex-cli",
                run_id="run_first",
                timeout_sec=60,
                release_binding=lambda worker: first_releases.append(worker["worker_id"]),
                reconcile_binding=lambda _home: None,
            ):
                first_active.set()
                assert release_first.wait(5)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            first_finished.set()

    first_thread = Thread(target=hold_first_account)
    first_thread.start()
    assert first_active.wait(5)

    with pytest.raises(RuntimeErrorBase, match="already has an active provider account route"):
        with binder.bind(
            {**_worker(second_account["account_id"]), "execution_mode": "docker"},
            runtime_name="codex-cli",
            run_id="run_second",
            timeout_sec=60,
            release_binding=lambda worker: second_releases.append(worker["worker_id"]),
            reconcile_binding=second_reconciles.append,
        ):
            pass

    assert not first_finished.is_set()
    assert first_releases == []
    assert second_releases == []
    assert second_reconciles == []
    assert store.active_provider_lease(
        first_account["account_id"], "codex-cli:mission"
    ) is not None
    assert store.active_provider_lease(
        second_account["account_id"], "codex-cli:mission"
    ) is None

    release_first.set()
    first_thread.join(timeout=5)
    assert not first_thread.is_alive()
    assert errors == []
    assert first_releases == ["wrk_personal"]


def test_worker_route_stays_reserved_until_native_container_cleanup_finishes(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    first_account = _account(store)
    second_account = _account(store)
    binder = MissionProviderAccountBinder(
        db_path=str(database),
        home_root=tmp_path / "provider_accounts",
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    mission_entered = Event()
    finish_mission = Event()
    cleanup_started = Event()
    finish_cleanup = Event()
    mission_finished = Event()
    errors: list[BaseException] = []

    def block_cleanup(_worker: dict) -> None:
        cleanup_started.set()
        assert finish_cleanup.wait(5)

    def run_first() -> None:
        try:
            with binder.bind(
                {**_worker(first_account["account_id"]), "execution_mode": "docker"},
                runtime_name="codex-cli",
                run_id="run_first",
                timeout_sec=60,
                release_binding=block_cleanup,
                reconcile_binding=lambda _home: None,
            ):
                mission_entered.set()
                assert finish_mission.wait(5)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            mission_finished.set()

    thread = Thread(target=run_first)
    thread.start()
    assert mission_entered.wait(5)
    finish_mission.set()
    assert cleanup_started.wait(5)
    assert not mission_finished.is_set()

    with pytest.raises(RuntimeErrorBase, match="already has an active provider account route"):
        with binder.bind(
            {**_worker(second_account["account_id"]), "execution_mode": "docker"},
            runtime_name="codex-cli",
            run_id="run_second",
            timeout_sec=60,
            release_binding=lambda _worker: None,
            reconcile_binding=lambda _home: None,
        ):
            pass
    with pytest.raises(RuntimeErrorBase, match="already has an active provider account route"):
        with binder.bind_unbound_route(
            _worker(second_account["account_id"]),
            runtime_name="codex-cli",
            run_id="run_fallback",
            account_id=second_account["account_id"],
        ):
            pass

    finish_cleanup.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    with binder.bind_unbound_route(
        _worker(second_account["account_id"]),
        runtime_name="codex-cli",
        run_id="run_after_cleanup",
        account_id=second_account["account_id"],
    ):
        pass


def test_broker_grant_is_not_issued_while_native_worker_cleanup_holds_the_route(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    native_account = _account(store)
    broker_account = _account(store, auth_method="api_key")
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.codex = RecordingRuntime()  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    mission_entered = Event()
    finish_mission = Event()
    cleanup_started = Event()
    finish_cleanup = Event()
    errors: list[BaseException] = []

    class CountingBroker:
        def __init__(self) -> None:
            self.binds = 0

        @contextmanager
        def bind_run(self, **_kwargs):
            self.binds += 1
            yield {"adapter": "synthetic-broker"}

    broker = CountingBroker()
    runtime.inference_broker = broker  # type: ignore[assignment]

    def release_native(_worker: dict) -> None:
        cleanup_started.set()
        assert finish_cleanup.wait(10)

    def run_native() -> None:
        try:
            with runtime.provider_account_binder.bind(
                {**_worker(native_account["account_id"]), "execution_mode": "docker"},
                runtime_name="codex-cli",
                run_id="run_native",
                timeout_sec=60,
                release_binding=release_native,
                reconcile_binding=lambda _home: None,
            ):
                mission_entered.set()
                assert finish_mission.wait(10)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = Thread(target=run_native)
    thread.start()
    assert mission_entered.wait(10)
    finish_mission.set()
    assert cleanup_started.wait(10)

    broker_worker = {
        **_worker(broker_account["account_id"]),
        "execution_mode": "docker",
        "model": "gpt-synthetic",
    }
    with pytest.raises(RuntimeErrorBase, match="already has an active provider account route"):
        runtime._run_task_with_provider_account(
            broker_worker,
            "must not issue",
            timeout_sec=60,
            run_id="run_broker",
        )
    assert broker.binds == 0

    finish_cleanup.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []


def test_desktop_action_rejects_reacquired_lease_with_identical_run_metadata(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = {**_worker(account["account_id"]), "execution_mode": "docker"}
    replacement: dict | None = None
    with pytest.raises(RuntimeErrorBase, match="release the provider account mission lease"):
        with runtime.provider_account_binder.bind(
            worker,
            runtime_name="codex-cli",
            run_id="run_personal",
            timeout_sec=60,
            release_binding=lambda _worker: None,
            reconcile_binding=lambda _home: None,
        ) as bound:
            runtime.provider_account_binder.mark_active_route_ready(
                bound,
                runtime_name="codex-cli",
                run_id="run_personal",
            )
            original = store.active_provider_lease(
                account["account_id"], "codex-cli:mission"
            )
            assert original is not None
            store.release_provider_lease(
                lease_id=original["lease_id"],
                tenant_id="tenant-a",
                owner_id="user-a",
            )
            replacement = store.acquire_provider_lease(
                account_id=account["account_id"],
                tenant_id="tenant-a",
                owner_id="user-a",
                lane="codex-cli:mission",
                worker_id="wrk_personal",
                run_id="run_personal",
                ttl_seconds=60,
            )
            with pytest.raises(RuntimeErrorBase, match="exact active provider account lease"):
                with runtime.provider_account_binder.project_active_route(
                    worker,
                    runtime_name="codex-cli",
                    run_id="run_personal",
                ):
                    pass
    assert replacement is not None
    store.release_provider_lease(
        lease_id=replacement["lease_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )


def test_personal_preferred_fallback_desktop_action_uses_its_active_unbound_route(
    tmp_path,
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    disconnected = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Disconnected",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://disconnected-desktop",
        status="disconnected",
    )
    mission_running = Event()
    finish_mission = Event()

    def hold_mission(_worker, _instruction, _timeout_sec, _run_id):
        mission_running.set()
        assert finish_mission.wait(5)

    recorder = RecordingRuntime(hold_mission)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = recorder  # type: ignore[assignment]
    worker = _worker(disconnected["account_id"], policy="personal_preferred")
    errors: list[BaseException] = []

    def run_mission() -> None:
        try:
            runtime.run_task(worker, "fallback mission", run_id="run_fallback")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = Thread(target=run_mission)
    thread.start()
    assert mission_running.wait(5)
    result = runtime.desktop_action(
        worker,
        "browser",
        url="file:///workspace/project/index.html",
        run_id="run_fallback",
    )
    assert result["status"] == "launched"
    assert recorder.worker is not None
    assert recorder.worker["_glasshive_provider_account_preferred_fallback"] is True
    assert "_glasshive_provider_account_bound" not in recorder.worker
    finish_mission.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []


def test_broker_mission_desktop_action_uses_its_active_unbound_route(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store, auth_method="api_key")
    mission_running = Event()
    finish_mission = Event()

    class FakeInferenceBroker:
        @contextmanager
        def bind_run(self, **_kwargs):
            yield {"adapter": "synthetic-broker"}

    def hold_mission(_worker, _instruction, _timeout_sec, _run_id):
        mission_running.set()
        assert finish_mission.wait(5)

    recorder = RecordingRuntime(hold_mission)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = recorder  # type: ignore[assignment]
    runtime.inference_broker = FakeInferenceBroker()  # type: ignore[assignment]
    worker = {**_worker(account["account_id"]), "model": "gpt-synthetic"}
    errors: list[BaseException] = []

    def run_mission() -> None:
        try:
            runtime.run_task(worker, "broker mission", run_id="run_broker")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = Thread(target=run_mission)
    thread.start()
    assert mission_running.wait(5)
    result = runtime.desktop_action(
        worker,
        "browser",
        url="file:///workspace/project/index.html",
        run_id="run_broker",
    )
    assert result["status"] == "launched"
    assert recorder.worker is not None
    assert recorder.worker["_glasshive_inference_broker_bound"] is True
    assert "_glasshive_provider_account_bound" not in recorder.worker
    finish_mission.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []


def test_desktop_action_waits_until_the_mission_container_is_initially_ready(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    first_ensure = Event()
    allow_ready = Event()
    mission_running = Event()
    finish_mission = Event()

    class ReadinessRuntime(RecordingRuntime):
        def __init__(self) -> None:
            super().__init__(self._hold_mission)
            self.ready = False
            self.create_count = 0

        def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
            self.ensure_calls += 1
            if not self.ready:
                self.create_count += 1
                first_ensure.set()
                assert allow_ready.wait(5)
                self.ready = True
            return super().ensure_worker_ready(worker)

        def _hold_mission(self, _worker, _instruction, _timeout_sec, _run_id):
            mission_running.set()
            assert finish_mission.wait(5)

        def desktop_action(self, worker: dict, action: str, **kwargs):
            self.ensure_worker_ready(worker)
            return super().desktop_action(worker, action, **kwargs)

    recorder = ReadinessRuntime()
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.codex = recorder  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    worker = {**_worker(account["account_id"]), "execution_mode": "docker"}
    errors: list[BaseException] = []

    def run_mission() -> None:
        try:
            runtime.run_task(worker, "ready mission", run_id="run_ready")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = Thread(target=run_mission)
    thread.start()
    assert first_ensure.wait(5)
    with pytest.raises(RuntimeErrorBase, match="ready provider route"):
        runtime.desktop_action(worker, "browser", run_id="run_ready")
    assert recorder.create_count == 1
    assert recorder.ensure_calls == 1

    allow_ready.set()
    assert mission_running.wait(5)
    result = runtime.desktop_action(worker, "browser", run_id="run_ready")
    assert result["status"] == "launched"
    assert recorder.create_count == 1
    finish_mission.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []


def test_runtime_factory_wires_the_exact_control_plane_database_into_mission_binding(tmp_path):
    database = tmp_path / "nondefault-name.sqlite3"
    ControlPlaneStore(str(database))

    runtime = _build_runtime("openclaw", str(database), None)

    assert isinstance(runtime, ProfiledWorkerRuntime)
    assert runtime.provider_account_binder.store is not None
    assert runtime.provider_account_binder.store.db_path == database.resolve()


def test_personal_required_fails_closed_for_missing_cross_user_busy_and_docker_accounts(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    other_account = _account(store, owner_id="user-b")
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = RecordingRuntime()  # type: ignore[assignment]

    with pytest.raises(RuntimeErrorBase, match="requires a provider account"):
        runtime.run_task(_worker(None), "missing", run_id="run_missing")

    with pytest.raises(RuntimeErrorBase, match="not available for this user"):
        runtime.run_task(_worker(other_account["account_id"]), "cross-user", run_id="run_cross")

    lease = store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="codex-cli:mission",
        worker_id="wrk_busy",
        run_id="run_busy",
        ttl_seconds=60,
    )
    try:
        with pytest.raises(RuntimeErrorBase, match="already in use"):
            runtime.run_task(_worker(account["account_id"]), "busy", run_id="run_second")
    finally:
        store.release_provider_lease(
            lease_id=lease["lease_id"], tenant_id="tenant-a", owner_id="user-a"
        )

    docker_worker = _worker(account["account_id"])
    docker_worker["execution_mode"] = "docker"
    runtime.codex = RecordingRuntime()  # type: ignore[assignment]
    with pytest.raises(RuntimeErrorBase, match="host-native"):
        runtime.run_task(docker_worker, "unsupported substrate", run_id="run_docker")


def test_personal_preferred_uses_the_legacy_route_when_personal_binding_is_unavailable(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    ready = _account(store)
    disconnected = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Disconnected",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://disconnected-preferred",
        status="disconnected",
    )
    recorder = RecordingRuntime()
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = recorder  # type: ignore[assignment]

    for account_id, run_id in (
        ("acct_missing", "run_preferred_missing"),
        (disconnected["account_id"], "run_preferred_disconnected"),
    ):
        runtime.run_task(
            _worker(account_id, policy="personal_preferred"),
            "preferred fallback",
            run_id=run_id,
        )
        assert recorder.worker is not None
        assert recorder.worker["_glasshive_provider_account_preferred_fallback"] is True
        assert "_glasshive_provider_account_env" not in recorder.worker

    lease = store.acquire_provider_lease(
        account_id=ready["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="codex-cli:mission",
        worker_id="wrk_busy",
        run_id="run_busy",
        ttl_seconds=60,
    )
    try:
        runtime.run_task(
            _worker(ready["account_id"], policy="personal_preferred"),
            "preferred busy fallback",
            run_id="run_preferred_busy",
        )
        assert recorder.worker is not None
        assert recorder.worker["_glasshive_provider_account_preferred_fallback"] is True
    finally:
        store.release_provider_lease(
            lease_id=lease["lease_id"], tenant_id="tenant-a", owner_id="user-a"
        )


@pytest.mark.parametrize("policy", ["personal_required", "personal_preferred"])
def test_personal_subscription_missions_fail_closed_without_multi_user_substrate_isolation(
    tmp_path, monkeypatch, policy
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = RecordingRuntime()  # type: ignore[assignment]
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")

    with pytest.raises(RuntimeErrorBase, match="dedicated OS or container boundary"):
        runtime.run_task(
            _worker(account["account_id"], policy=policy),
            "must not reach a shared service UID",
            run_id=f"run_{policy}",
        )


def test_provider_account_lease_is_released_when_the_mission_runtime_fails(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)

    def fail_during_run(_worker, _instruction, _timeout_sec, _run_id):
        assert store.active_provider_lease(
            account["account_id"], "codex-cli:mission"
        ) is not None
        raise RuntimeErrorBase("synthetic mission failure")

    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = RecordingRuntime(fail_during_run)  # type: ignore[assignment]

    with pytest.raises(RuntimeErrorBase, match="synthetic mission failure"):
        runtime.run_task(
            _worker(account["account_id"]),
            "fail safely",
            run_id="run_failure",
        )
    assert store.active_provider_lease(
        account["account_id"], "codex-cli:mission"
    ) is None


def test_provider_account_lease_heartbeats_while_mission_is_running(
    tmp_path, monkeypatch
):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    account = _account(store)
    calls: list[str] = []
    original = ControlPlaneStore.heartbeat_provider_lease

    def record_heartbeat(self, **kwargs):
        calls.append(str(kwargs["lease_id"]))
        return original(self, **kwargs)

    monkeypatch.setattr(
        ControlPlaneStore, "heartbeat_provider_lease", record_heartbeat
    )
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_ACCOUNT_LEASE_HEARTBEAT_SECONDS", "0.05"
    )

    def wait_during_run(_worker, _instruction, _timeout_sec, _run_id):
        time.sleep(0.14)

    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path), provider_account_db_path=str(database)
    )
    runtime.host_codex = RecordingRuntime(wait_during_run)  # type: ignore[assignment]

    runtime.run_task(
        _worker(account["account_id"]), "heartbeat", run_id="run_heartbeat"
    )

    assert calls


def test_provider_account_default_lease_is_a_short_heartbeated_crash_window(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_PROVIDER_ACCOUNT_LEASE_TTL_SECONDS", raising=False)

    assert MissionProviderAccountBinder._lease_ttl_seconds(None) == 180
    assert MissionProviderAccountBinder._lease_ttl_seconds(24 * 60 * 60) == 180

    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_LEASE_TTL_SECONDS", "999999")
    assert MissionProviderAccountBinder._lease_ttl_seconds(None) == 60 * 60


def test_legacy_and_unselected_optional_missions_preserve_existing_runtime_path(tmp_path):
    database = tmp_path / "runtime.db"
    ControlPlaneStore(str(database))
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    recorder = RecordingRuntime()
    runtime.host_codex = recorder  # type: ignore[assignment]
    legacy_worker = {
        "worker_id": "wrk_legacy",
        "tenant_id": "tenant-a",
        "owner_id": "user-a",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }

    runtime.run_task(legacy_worker, "legacy", run_id="run_legacy")
    assert recorder.worker is not None
    assert "_glasshive_provider_account_env" not in recorder.worker

    optional_worker = _worker(None, policy="personal_preferred")
    runtime.run_task(optional_worker, "preferred", run_id="run_preferred")
    assert recorder.worker is not None
    assert "_glasshive_provider_account_env" not in recorder.worker

    legacy_optional_worker = _worker(None, policy="personal_optional")
    runtime.run_task(legacy_optional_worker, "optional", run_id="run_optional")
    assert recorder.worker is not None
    assert "_glasshive_provider_account_env" not in recorder.worker


def test_selected_account_provider_must_match_worker_profile_and_be_ready(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    claude = _account(store, provider="claude", auth_method="enterprise_route")
    disconnected = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Disconnected",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://disconnected",
        status="disconnected",
    )
    runtime = ProfiledWorkerRuntime(
        base_dir=str(tmp_path),
        provider_account_db_path=str(database),
    )
    runtime.host_codex = RecordingRuntime()  # type: ignore[assignment]

    with pytest.raises(RuntimeErrorBase, match="does not match"):
        runtime.run_task(_worker(claude["account_id"]), "wrong provider", run_id="run_wrong")
    with pytest.raises(RuntimeErrorBase, match="not ready"):
        runtime.run_task(_worker(disconnected["account_id"]), "not ready", run_id="run_not_ready")


def test_host_command_builders_apply_only_the_bound_native_provider_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "global-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "global-claude"))
    codex_home = tmp_path / "personal-codex" / "codex"
    claude_home = tmp_path / "personal-claude" / "claude"
    codex_home.mkdir(parents=True)
    claude_home.mkdir(parents=True)
    info = RuntimeInfo(
        runtime="test",
        model="test-model",
        gateway_url="",
        gateway_port=None,
        gateway_token=None,
        session_key=None,
        state_dir=str(tmp_path),
        workspace_dir=str(tmp_path),
        pid=None,
    )

    codex = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-runtime"))
    codex._assert_host_codex_worker_policy = lambda worker: None  # type: ignore[method-assign]
    codex_worker = _worker("acct_synthetic")
    codex_bundle = json.loads(codex_worker["bootstrap_bundle_json"])
    codex_bundle["env"] = {
        "OPENAI_API_KEY": "synthetic-competing-key",
        "OPENAI_BASE_URL": "https://synthetic.invalid",
    }
    codex_worker["bootstrap_bundle_json"] = json.dumps(codex_bundle)
    codex_worker["_glasshive_provider_account_env"] = {"CODEX_HOME": str(codex_home)}
    codex_worker["_glasshive_provider_account_bound"] = True
    _, codex_env = codex._build_command(codex_worker, "mission", info)
    assert codex_env["CODEX_HOME"] == str(codex_home)
    assert codex_env["CODEX_HOME"] != str(tmp_path / "global-codex")
    assert "OPENAI_API_KEY" not in codex_env
    assert "OPENAI_BASE_URL" not in codex_env

    claude = HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-runtime"))
    claude_worker = _worker("acct_synthetic", profile="claude-code")
    claude_bundle = json.loads(claude_worker["bootstrap_bundle_json"])
    claude_bundle["env"] = {
        "ANTHROPIC_API_KEY": "synthetic-competing-key",
        "ANTHROPIC_BASE_URL": "https://synthetic.invalid",
    }
    claude_worker["bootstrap_bundle_json"] = json.dumps(claude_bundle)
    claude_worker["_glasshive_provider_account_env"] = {
        "CLAUDE_CONFIG_DIR": str(claude_home),
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(claude_home),
    }
    claude_worker["_glasshive_provider_account_bound"] = True
    _, claude_env = claude._build_command(claude_worker, "mission", info)
    assert claude_env["CLAUDE_CONFIG_DIR"] == str(claude_home)
    assert claude_env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(claude_home)
    assert claude_env["CLAUDE_CONFIG_DIR"] != str(tmp_path / "global-claude")
    assert "ANTHROPIC_API_KEY" not in claude_env
    assert "ANTHROPIC_BASE_URL" not in claude_env


def test_deployment_provider_readiness_is_profile_aware_and_route_complete(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    provider_names = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
        "WPR_CODEX_CLI_BASE_URL",
        "WPR_CODEX_CLI_ENV_KEY",
        "WPR_OPENCLAW_BASE_URL",
        "WPR_OPENCLAW_ENV_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "WPR_CLAUDE_CODE_USE_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "AWS_REGION",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    }
    for name in provider_names:
        monkeypatch.delenv(name, raising=False)

    for profile in ("codex-cli", "openclaw-general", "claude-code", "unknown"):
        assert deployment_provider_readiness(profile)[0] == "action_required"

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-standard-openai-key")
    assert deployment_provider_readiness("codex-cli")[0] == "deployment_managed"
    assert deployment_provider_readiness("openclaw-general")[0] == "deployment_managed"
    assert deployment_provider_readiness("claude-code")[0] == "action_required"
    monkeypatch.delenv("OPENAI_API_KEY")

    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://selected.example.test/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ENV_KEY", "PORTKEY_API_KEY")
    monkeypatch.setenv("PORTKEY_API_KEY", "synthetic-portkey")
    assert deployment_provider_readiness("codex-cli")[0] == "deployment_managed"
    assert deployment_provider_readiness("openclaw-general")[0] == "action_required"
    monkeypatch.setenv("WPR_OPENCLAW_BASE_URL", "https://selected.example.test/v1")
    monkeypatch.setenv("WPR_OPENCLAW_ENV_KEY", "PORTKEY_API_KEY")
    assert deployment_provider_readiness("openclaw-general")[0] == "deployment_managed"
    monkeypatch.setenv("WPR_OPENCLAW_ENV_KEY", "PATH")
    assert deployment_provider_readiness("openclaw-general")[0] == "action_required"
    monkeypatch.delenv("WPR_CODEX_CLI_BASE_URL")
    monkeypatch.delenv("WPR_CODEX_CLI_ENV_KEY")
    monkeypatch.delenv("WPR_OPENCLAW_BASE_URL")
    monkeypatch.delenv("WPR_OPENCLAW_ENV_KEY")
    monkeypatch.delenv("PORTKEY_API_KEY")

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-claude-oauth")
    assert deployment_provider_readiness("claude-code")[0] == "deployment_managed"
    assert deployment_provider_readiness("codex-cli")[0] == "action_required"
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")

    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "true")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "synthetic-bedrock-bearer")
    assert deployment_provider_readiness("claude-code")[0] == "action_required"
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert deployment_provider_readiness("claude-code")[0] == "deployment_managed"


def test_legacy_enterprise_flag_uses_the_same_fail_closed_provider_readiness(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_SECURITY_MODE", raising=False)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
        "WPR_CODEX_CLI_BASE_URL",
        "WPR_CODEX_CLI_ENV_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    assert deployment_provider_readiness("codex-cli")[0] == "action_required"

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-standard-openai-key")
    assert deployment_provider_readiness("codex-cli")[0] == "deployment_managed"


def test_bound_codex_mission_never_copies_process_global_auth_into_worker_state(tmp_path, monkeypatch):
    global_home = tmp_path / "global-codex"
    global_home.mkdir()
    (global_home / "auth.json").write_text('{"synthetic_global_credential":"must-not-copy"}')
    monkeypatch.setenv("CODEX_HOME", str(global_home))
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker = _worker("acct_synthetic")
    worker["_glasshive_provider_account_env"] = {
        "CODEX_HOME": str(tmp_path / "personal" / "codex")
    }
    worker["_glasshive_provider_account_bound"] = True

    runtime._write_host_project_mcp_files(
        worker,
        workspace,
        {"codex_config_append": '[mcp_servers.synthetic]\ncommand = "synthetic"'},
    )

    assert not (runtime._host_codex_home(worker) / "auth.json").exists()
