from __future__ import annotations

import json
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from workers_projects_runtime.api import create_app
from workers_projects_runtime.bootstrap import bootstrap_env_for
from workers_projects_runtime.openclaw_runtime import RuntimeInfo, StubRuntime
from workers_projects_runtime.profile_runtime import (
    ClaudeCodeRuntime,
    CodexCliRuntime,
    HostClaudeCodeRuntime,
    HostCodexCliRuntime,
)

PROVIDER_SESSION_MODE_ENV = "GLASSHIVE_PROVIDER_SESSION_MODE"
AUTH = {
    "Authorization": "Bearer provider-session-mode-test-token",
    "X-Viventium-User-Id": "owner-a",
}


def _payload(workspace: Path, *, message_id: str, idempotency_key: str) -> dict:
    return {
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [
            {"role": "system", "content": "Use the supplied context."},
            {"role": "user", "content": f"Synthetic turn {message_id}."},
        ],
        "metadata": {
            "owner_id": "owner-a",
            "conversation_id": "conversation-a",
            "agent_id": "agent-a",
            "message_id": message_id,
            "idempotency_key": idempotency_key,
            # The request body is not the trusted owner of this transport mode.
            "provider_session_mode": "stateless",
            "glasshive_options": {
                "workspace": {"mode": "custom", "path": str(workspace)},
                "access": "workspace",
            },
        },
    }


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("WPR_API_TOKEN", "provider-session-mode-admin-test-token")
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_API_KEY", "provider-session-mode-test-token"
    )
    monkeypatch.setenv("GLASSHIVE_PROVIDER_PRINCIPAL_ID", "owner-a")
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS", str(tmp_path))
    return TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=StubRuntime(),
        )
    )


def _bundle(worker: dict) -> dict:
    return json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))


def _session_mode_worker(
    *,
    worker_id: str,
    profile: str,
    execution_mode: str,
    workspace: Path,
    stateless: bool,
) -> dict:
    env = {PROVIDER_SESSION_MODE_ENV: "stateless"} if stateless else {}
    return {
        "worker_id": worker_id,
        "name": "Synthetic conversation worker",
        "profile": profile,
        "runtime": profile,
        "model": "gpt-5.6-sol" if profile == "codex-cli" else "opus",
        "execution_mode": execution_mode,
        "trusted_run_lane": "conversation",
        "workspace_root": str(workspace),
        # A worker-row value is also native continuity and must be ignored in
        # stateless mode, even when session.json is unavailable.
        "session_key": "worker-row-native-session",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol" if profile == "codex-cli" else "opus",
                "access_mode": "full",
                "env": env,
            }
        ),
    }


def test_trusted_header_pins_stateless_mode_without_rotating_provider_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    try:
        default_response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_payload(
                workspace,
                message_id="message-default",
                idempotency_key="idempotency-default",
            ),
        )
        assert default_response.status_code == 200, default_response.text
        store = client.app.state.store
        initial_session = store.list_provider_sessions(owner_id="owner-a")[0]
        initial_identity = (
            initial_session["session_id"],
            initial_session["project_id"],
            initial_session["worker_id"],
        )
        initial_worker = store.get_worker(initial_session["worker_id"])
        assert initial_worker is not None
        assert PROVIDER_SESSION_MODE_ENV not in _bundle(initial_worker).get("env", {})

        stateless_response = client.post(
            "/v1/chat/completions",
            headers={**AUTH, "X-GlassHive-Provider-Session-Mode": "stateless"},
            json=_payload(
                workspace,
                message_id="message-stateless",
                idempotency_key="idempotency-stateless",
            ),
        )
        assert stateless_response.status_code == 200, stateless_response.text
        current_session = store.list_provider_sessions(owner_id="owner-a")[0]
        assert (
            current_session["session_id"],
            current_session["project_id"],
            current_session["worker_id"],
        ) == initial_identity
        current_worker = store.get_worker(current_session["worker_id"])
        assert current_worker is not None
        assert _bundle(current_worker)["env"][PROVIDER_SESSION_MODE_ENV] == "stateless"
        stateless_request = store.get_provider_request(stateless_response.json()["id"])
        assert stateless_request is not None
        stateless_decision = json.loads(stateless_request["replay_decision_json"])
        assert stateless_decision["provider_session_mode"] == "stateless"

        store.update_worker(
            current_session["worker_id"], session_key="prior-worker-native-session"
        )
        client.app.state.service._apply_runtime_info(
            current_session["worker_id"],
            RuntimeInfo(
                runtime="codex-cli",
                model="gpt-5.6-sol",
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=None,
                state_dir="",
                workspace_dir=str(workspace),
                pid=None,
            ),
            state="ready",
            last_error="",
        )
        assert (
            store.get_worker(current_session["worker_id"])["session_key"]
            == "prior-worker-native-session"
        )

        # Simulate a later admission changing the mutable worker row. The exact
        # run must recover its non-secret mode from its durable request record.
        stateless_run = store.get_run(stateless_request["run_id"])
        assert stateless_run is not None
        store.update_worker(
            current_session["worker_id"],
            bootstrap_bundle_json=json.dumps(
                {**_bundle(current_worker), "env": {}}
            ),
        )
        recovered_run_worker = client.app.state.service._run_local_worker(
            store.get_worker(current_session["worker_id"]),
            stateless_run,
        )
        assert (
            _bundle(recovered_run_worker)["env"][PROVIDER_SESSION_MODE_ENV]
            == "stateless"
        )

        persistent_response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_payload(
                workspace,
                message_id="message-persistent",
                idempotency_key="idempotency-persistent",
            ),
        )
        assert persistent_response.status_code == 200, persistent_response.text
        persistent_session = store.list_provider_sessions(owner_id="owner-a")[0]
        assert (
            persistent_session["session_id"],
            persistent_session["project_id"],
            persistent_session["worker_id"],
        ) == initial_identity
        persistent_worker = store.get_worker(persistent_session["worker_id"])
        assert persistent_worker is not None
        assert PROVIDER_SESSION_MODE_ENV not in _bundle(persistent_worker).get("env", {})
        persistent_request = store.get_provider_request(persistent_response.json()["id"])
        assert persistent_request is not None
        assert json.loads(persistent_request["replay_decision_json"])[
            "provider_session_mode"
        ] == "persistent"
    finally:
        client.app.state.service.shutdown()
        client.close()


def test_invalid_provider_session_mode_header_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/v1/chat/completions",
            headers={**AUTH, "X-GlassHive-Provider-Session-Mode": "shared"},
            json=_payload(
                workspace,
                message_id="message-invalid-mode",
                idempotency_key="idempotency-invalid-mode",
            ),
        )
        assert response.status_code == 422
    finally:
        client.app.state.service.shutdown()
        client.close()


def test_stateless_mode_is_a_non_secret_enterprise_bootstrap_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "1")
    worker = _session_mode_worker(
        worker_id="worker-enterprise-mode",
        profile="codex-cli",
        execution_mode="docker",
        workspace=tmp_path,
        stateless=True,
    )

    assert bootstrap_env_for(worker)[PROVIDER_SESSION_MODE_ENV] == "stateless"


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, ClaudeCodeRuntime])
def test_docker_native_runtime_ignores_prior_session_and_does_not_overwrite_it(
    runtime_type, tmp_path: Path
) -> None:
    profile = "codex-cli" if runtime_type is CodexCliRuntime else "claude-code"
    runtime = runtime_type(base_dir=str(tmp_path / profile))
    worker = _session_mode_worker(
        worker_id=f"worker-docker-{profile}",
        profile=profile,
        execution_mode="docker",
        workspace=tmp_path,
        stateless=True,
    )
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "prior-persistent-session")

    info = runtime._runtime_info(worker)
    command, _ = runtime._build_command(worker, "Answer this turn.", info)

    assert info.session_key is None
    assert "resume" not in command
    assert "--resume" not in command
    runtime._remember_native_session_key(worker, "new-stateless-session")
    assert runtime._read_session_key(worker["worker_id"]) == "prior-persistent-session"

    persistent_worker = {
        **worker,
        "bootstrap_bundle_json": json.dumps(
            {**_bundle(worker), "env": {}}
        ),
    }
    persistent_info = runtime._runtime_info(persistent_worker)
    persistent_command, _ = runtime._build_command(
        persistent_worker, "Resume normally.", persistent_info
    )
    assert persistent_info.session_key == "prior-persistent-session"
    assert ("resume" in persistent_command) or ("--resume" in persistent_command)


@pytest.mark.parametrize("runtime_type", [HostCodexCliRuntime, HostClaudeCodeRuntime])
def test_host_native_runtime_ignores_prior_session_and_does_not_overwrite_it(
    runtime_type, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    profile = "codex-cli" if runtime_type is HostCodexCliRuntime else "claude-code"
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = runtime_type(base_dir=str(tmp_path / profile))
    worker = _session_mode_worker(
        worker_id=f"worker-host-{profile}",
        profile=profile,
        execution_mode="host",
        workspace=workspace,
        stateless=True,
    )
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "prior-persistent-session")

    info = runtime._host_runtime_info(worker)
    command, _ = runtime._build_command(worker, "Answer this turn.", info)

    assert info.session_key is None
    assert "resume" not in command
    assert "--resume" not in command
    runtime._remember_native_session_key(worker, "new-stateless-session")
    assert runtime._read_session_key(worker["worker_id"]) == "prior-persistent-session"

    persistent_worker = {
        **worker,
        "bootstrap_bundle_json": json.dumps(
            {**_bundle(worker), "env": {}}
        ),
    }
    persistent_info = runtime._host_runtime_info(persistent_worker)
    persistent_command, _ = runtime._build_command(
        persistent_worker, "Resume normally.", persistent_info
    )
    assert persistent_info.session_key == "prior-persistent-session"
    assert ("resume" in persistent_command) or ("--resume" in persistent_command)


def test_native_event_observer_does_not_persist_a_stateless_session_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "codex"))
    worker = _session_mode_worker(
        worker_id="worker-stateless-observer",
        profile="codex-cli",
        execution_mode="docker",
        workspace=tmp_path,
        stateless=True,
    )
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "prior-persistent-session")
    output = tmp_path / "provider.jsonl"
    output.write_text(
        json.dumps({"type": "thread.started", "thread_id": "new-stateless-session"})
        + "\n"
    )
    monkeypatch.setattr(
        runtime,
        "_read_active_session",
        lambda _worker_id: {"run_id": "run-stateless", "model": "gpt-5.6-sol"},
    )
    stop = Event()
    stop.set()

    runtime._observe_native_session_events(
        worker["worker_id"],
        output,
        stop,
        run_id="run-stateless",
        worker=worker,
    )

    assert runtime._read_session_key(worker["worker_id"]) == "prior-persistent-session"


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, ClaudeCodeRuntime])
def test_crash_recovery_does_not_persist_a_stateless_native_session(
    runtime_type, tmp_path: Path
) -> None:
    profile = "codex-cli" if runtime_type is CodexCliRuntime else "claude-code"
    runtime = runtime_type(base_dir=str(tmp_path / profile))
    worker = _session_mode_worker(
        worker_id=f"worker-recovery-{profile}",
        profile=profile,
        execution_mode="docker",
        workspace=tmp_path,
        stateless=True,
    )
    runtime._ensure_dirs(worker["worker_id"])
    run_id = f"run-recovery-{profile}"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True)
    if profile == "codex-cli":
        stdout = "\n".join(
            [
                json.dumps(
                    {"type": "thread.started", "thread_id": "recovered-stateless-session"}
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "Recovered answer.",
                        },
                    }
                ),
            ]
        )
    else:
        stdout = json.dumps(
            {
                "type": "result",
                "session_id": "recovered-stateless-session",
                "result": "Recovered answer.",
            }
        )
    (run_root / "stdout.log").write_text(stdout + "\n")
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    runtime.reconcile_worker = lambda current: runtime._runtime_info(  # type: ignore[method-assign]
        current, pid=1234
    )

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"].startswith("Recovered answer.")
    assert "internal constraint diagnostic was unavailable" in recovered["output_text"]
    assert not runtime._session_meta_path(worker["worker_id"]).exists()


def test_service_recovery_uses_exact_stateless_run_mode_after_later_persistent_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    try:
        stateless_response = client.post(
            "/v1/chat/completions",
            headers={**AUTH, "X-GlassHive-Provider-Session-Mode": "stateless"},
            json=_payload(
                workspace,
                message_id="message-stateless-recovery",
                idempotency_key="idempotency-stateless-recovery",
            ),
        )
        assert stateless_response.status_code == 200, stateless_response.text
        store = client.app.state.store
        stateless_request = store.get_provider_request(stateless_response.json()["id"])
        assert stateless_request is not None
        stateless_run = store.get_run(stateless_request["run_id"])
        assert stateless_run is not None

        persistent_response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_payload(
                workspace,
                message_id="message-persistent-after-stateless",
                idempotency_key="idempotency-persistent-after-stateless",
            ),
        )
        assert persistent_response.status_code == 200, persistent_response.text
        session = store.list_provider_sessions(owner_id="owner-a")[0]
        current_worker = store.get_worker(session["worker_id"])
        assert current_worker is not None
        assert PROVIDER_SESSION_MODE_ENV not in _bundle(current_worker).get("env", {})

        runtime = CodexCliRuntime(base_dir=str(tmp_path / "recovery-runtime"))
        runtime._ensure_dirs(current_worker["worker_id"])
        run_root = runtime._run_root(
            current_worker["worker_id"], stateless_run["run_id"]
        )
        run_root.mkdir(parents=True)
        (run_root / "stdout.log").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "thread.started",
                            "thread_id": "must-not-persist-after-recovery",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "Recovered stateless answer.",
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        runtime.reconcile_worker = lambda current: RuntimeInfo(  # type: ignore[method-assign]
            runtime="codex-cli",
            model="gpt-5.6-sol",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=str(tmp_path / "recovery-runtime"),
            workspace_dir=str(workspace),
            pid=1234,
        )
        client.app.state.service.runtime = runtime

        recovered = client.app.state.service._collect_completed_run(
            current_worker,
            {**stateless_run, "active_attempt_id": ""},
        )

        assert recovered is not None
        assert recovered["state"] == "completed"
        assert recovered["output_text"] == "Recovered stateless answer."
        assert not runtime._session_meta_path(current_worker["worker_id"]).exists()
    finally:
        client.app.state.service.shutdown()
        client.close()
