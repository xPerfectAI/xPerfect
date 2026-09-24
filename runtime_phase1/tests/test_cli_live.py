from __future__ import annotations

import json
import os
import subprocess
import time
import tomllib

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app


pytestmark = pytest.mark.skipif(
    os.environ.get("WPR_RUN_CLI_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="Set WPR_RUN_CLI_LIVE_TESTS=1 to run live Codex/Claude worker tests",
)


def wait_for_run(client: TestClient, run_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(1.0)
    raise AssertionError(f"Run {run_id} did not settle within {timeout}s")


def wait_for_run_state(
    client: TestClient,
    run_id: str,
    expected_states: set[str],
    timeout: float = 60.0,
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["state"] in expected_states:
            return run
        if run["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            raise AssertionError(
                f"Run settled as {run['state']} before reaching {sorted(expected_states)}"
            )
        time.sleep(0.1)
    raise AssertionError(
        f"Run did not reach {sorted(expected_states)} within {timeout}s"
    )


def wait_for_worker_event(
    client: TestClient,
    worker_id: str,
    event_type: str,
    timeout: float = 60.0,
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/workers/{worker_id}/events")
        assert response.status_code == 200
        for event in response.json()["items"]:
            if event["event_type"] == event_type:
                return event
        time.sleep(0.1)
    raise AssertionError(f"Worker did not emit {event_type} within {timeout}s")


def _create_project(client: TestClient, default_worker_profile: str) -> dict:
    return client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "CLI Workers Live",
            "goal": "Validate sandboxed CLI workers in the standalone runtime.",
            "default_worker_profile": default_worker_profile,
        },
    ).json()


def _claude_available() -> bool:
    env = dict(os.environ)
    if os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        env.pop("ANTHROPIC_API_KEY", None)
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--output-format",
                "json",
                "Reply with exactly CLAUDE_PREFLIGHT_OK and no other text.",
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=90,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    text = f"{result.stdout}\n{result.stderr}"
    blockers = ("Not logged in", "Invalid API key", "Credit balance is too low")
    return not any(marker in text for marker in blockers)


def test_live_codex_worker_can_run_and_resume(tmp_path):
    db_path = tmp_path / "runtime-cli-live.db"
    client = TestClient(create_app(str(db_path), runtime_backend="openclaw"))

    project = _create_project(client, "codex-cli")

    codex_worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Codex Worker",
            "role": "coder",
            "profile": "codex-cli",
            "backend": "openclaw",
            "execution_mode": "host",
            "bootstrap_profile": "codex-host",
        },
    ).json()
    assert codex_worker["runtime"] == "codex-cli"

    codex_run = client.post(
        f"/v1/workers/{codex_worker['worker_id']}/assign",
        json={
            "instruction": (
                "Reply with a final section exactly named FINAL REPORT: followed by "
                "CODEX_WORKER_OK."
            )
        },
    ).json()
    codex_done = wait_for_run(client, codex_run["run_id"])
    assert codex_done["state"] == "completed", codex_done
    assert "CODEX_WORKER_OK" in codex_done["output_text"], codex_done["output_text"]

    codex_worker_after = client.get(f"/v1/workers/{codex_worker['worker_id']}").json()
    assert codex_worker_after["session_key"]
    assert not codex_worker_after["session_key"].startswith("codex-worker:"), codex_worker_after["session_key"]

    codex_resume_run = client.post(
        f"/v1/workers/{codex_worker['worker_id']}/message",
        json={
            "message": (
                "Reply with a final section exactly named FINAL REPORT: followed by "
                "CODEX_RESUME_OK."
            )
        },
    ).json()
    codex_resume_done = wait_for_run(client, codex_resume_run["run_id"])
    assert codex_resume_done["state"] == "completed", codex_resume_done
    assert "CODEX_RESUME_OK" in codex_resume_done["output_text"], codex_resume_done["output_text"]


def test_live_host_codex_plugin_denylist_worker_can_run(tmp_path, monkeypatch):
    denied_plugin = "viventium-feelings@project-viventium"
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", denied_plugin)
    db_path = tmp_path / "runtime-host-plugin-live.db"
    client = TestClient(create_app(str(db_path), runtime_backend="openclaw"))
    project = _create_project(client, "codex-cli")

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Codex Plugin Isolation Worker",
            "role": "coder",
            "profile": "codex-cli",
            "backend": "openclaw",
            "execution_mode": "host",
        },
    ).json()
    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={
            "instruction": (
                "Reply with a final section exactly named FINAL REPORT: followed by "
                "CODEX_PLUGIN_DENYLIST_OK."
            )
        },
    ).json()
    done = wait_for_run(client, run["run_id"])

    assert done["state"] == "completed", done
    assert "CODEX_PLUGIN_DENYLIST_OK" in done["output_text"]
    worker_root = tmp_path / "host_codex_cli_runtime" / "workers" / worker["worker_id"]
    config = tomllib.loads((worker_root / "home" / ".codex" / "config.toml").read_text())
    assert config["plugins"][denied_plugin]["enabled"] is False
    instruction = next((worker_root / "home" / ".glasshive-runs").glob("*/instruction.stdin"))
    assert denied_plugin not in instruction.read_text()


def test_live_host_claude_plugin_denylist_worker_can_run(tmp_path, monkeypatch):
    if os.environ.get("WPR_RUN_CLAUDE_CODE_LIVE_TESTS", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        pytest.skip("Set WPR_RUN_CLAUDE_CODE_LIVE_TESTS=1 to run live Claude Code worker tests")
    denied_plugin = "viventium-feelings@project-viventium"
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", denied_plugin)
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    db_path = tmp_path / "runtime-host-claude-plugin-live.db"
    client = TestClient(create_app(str(db_path), runtime_backend="openclaw"))
    project = _create_project(client, "claude-code")

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Claude Plugin Isolation Worker",
            "role": "coder",
            "profile": "claude-code",
            "backend": "openclaw",
            "execution_mode": "host",
        },
    ).json()
    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={
            "instruction": (
                "Reply with a final section exactly named FINAL REPORT: followed by "
                "CLAUDE_PLUGIN_DENYLIST_OK."
            )
        },
    ).json()
    done = wait_for_run(client, run["run_id"])

    assert done["state"] == "completed", done
    assert "CLAUDE_PLUGIN_DENYLIST_OK" in done["output_text"]
    worker_root = tmp_path / "host_claude_code_runtime" / "workers" / worker["worker_id"]
    instruction = next((worker_root / "home" / ".glasshive-runs").glob("*/instruction.stdin"))
    assert denied_plugin not in instruction.read_text()


def test_live_claude_worker_can_run_and_resume(tmp_path):
    if os.environ.get("WPR_RUN_CLAUDE_CODE_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("Set WPR_RUN_CLAUDE_CODE_LIVE_TESTS=1 to run live Claude Code worker tests")
    if not _claude_available():
        pytest.skip("Claude Code auth or credits are not available for a live containerized run")

    db_path = tmp_path / "runtime-cli-live.db"
    client = TestClient(create_app(str(db_path), runtime_backend="openclaw"))
    project = _create_project(client, "claude-code")

    claude_worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Claude Worker",
            "role": "coder",
            "profile": "claude-code",
            "backend": "openclaw",
            "execution_mode": "host",
            "bootstrap_profile": "claude-host",
        },
    ).json()
    assert claude_worker["runtime"] == "claude-code"

    claude_run = client.post(
        f"/v1/workers/{claude_worker['worker_id']}/assign",
        json={"instruction": "Reply with exactly CLAUDE_WORKER_OK and no other text."},
    ).json()
    claude_done = wait_for_run(client, claude_run["run_id"])
    assert claude_done["state"] == "completed", claude_done
    assert "CLAUDE_WORKER_OK" in claude_done["output_text"], claude_done["output_text"]

    claude_resume_run = client.post(
        f"/v1/workers/{claude_worker['worker_id']}/message",
        json={"message": "Reply with exactly CLAUDE_RESUME_OK and no other text."},
    ).json()
    claude_resume_done = wait_for_run(client, claude_resume_run["run_id"])
    assert claude_resume_done["state"] == "completed", claude_resume_done
    assert "CLAUDE_RESUME_OK" in claude_resume_done["output_text"], claude_resume_done["output_text"]

    claude_worker_after = client.get(f"/v1/workers/{claude_worker['worker_id']}").json()
    assert claude_worker_after["session_key"]
