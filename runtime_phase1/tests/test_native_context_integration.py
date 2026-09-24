"""Requires the O07 shared hooks; executes native command construction without a provider."""

import json
import pytest
from workers_projects_runtime.bootstrap import refresh_project_runtime_files_for_worker
from workers_projects_runtime.profile_runtime import ClaudeCodeRuntime, CodexCliRuntime
from test_native_provider_authority_receipt import _direct_worker


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_shared_cwd_keeps_user_policy_and_private_native_authority(
    tmp_path, monkeypatch, runtime_type
):
    capsule = "<viventium_feeling_state>\nSynthetic exact pinned state.\n</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, runtime_type, capsule)
    private_project = runtime._workspace_dir(worker["worker_id"])
    home = runtime._home_dir(worker["worker_id"])
    common = tmp_path / "common"
    common.mkdir()
    (common / "AGENTS.md").write_text("Common owner policy")
    (common / "CLAUDE.md").write_text("Common Claude policy")
    (common / ".mcp.json").write_text("Common tool config")
    before = {p.name: p.read_bytes() for p in common.iterdir()}
    worker["_native_context_placement"] = {
        "mode": "shared",
        "file_placement": "common",
        "home_dir": str(home),
        "native_home": runtime.sandbox.home_mount,
        "workspace_dir": str(common),
        "native_workspace": "/workspace/common",
        "private_project_dir": str(private_project),
    }
    monkeypatch.setattr(runtime, "_workspace_dir", lambda _: common)
    refresh_project_runtime_files_for_worker(home, common, worker)
    command, env = runtime._build_command(
        worker, "Exact task", runtime._runtime_info(worker)
    )
    assert {p.name: p.read_bytes() for p in common.iterdir()} == before
    if runtime_type is ClaudeCodeRuntime:
        assert "--strict-mcp-config" in command
        assert command[command.index("--mcp-config") + 1].startswith(
            runtime.sandbox.home_mount
        )
        assert str(common) not in command[command.index("--mcp-config") + 1]
        assert "autoMemoryDirectory" in json.loads(
            command[command.index("--settings") + 1]
        )
    else:
        assert "project_doc_max_bytes=0" not in command
        assert (home / ".codex/config.toml").read_text().count(
            "Synthetic exact pinned state."
        ) == 1


def test_full_api_mounts_context_with_native_authority_and_owner_configuration(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app

    monkeypatch.setenv("WPR_STATE_DIR", str(tmp_path / "state"))
    app = create_app(
        db_path=str(tmp_path / "state.db"),
        runtime_backend="stub",
        reconcile_on_startup=False,
    )
    store = app.state.service.store
    project = store.create_project("owner", "Example", "Goal", "claude-code")
    worker = store.create_worker(
        project["project_id"],
        "owner",
        "Member",
        "assistant",
        "claude-code",
        "local",
        "claude",
        "exact",
    )
    with TestClient(app) as client:
        # Local principal differs from explicit fixture owner; no accidental owner fall-through.
        response = client.get(f"/v1/workers/{worker['worker_id']}/configuration")
        assert response.status_code in {403, 404}
        assert client.post("/v1/native/context/", json={}).status_code == 403


def test_same_owner_dispatch_shares_exact_main_background_only(tmp_path):
    from types import SimpleNamespace
    from workers_projects_runtime.store import Store
    from workers_projects_runtime.control_plane import ControlPlaneStore
    from workers_projects_runtime.coordinator import (
        CoordinatorService,
        CoordinatorConfig,
        Goal,
    )
    from workers_projects_runtime.worker_context_sources import authorized_sources

    store = Store(tmp_path / "data.db")
    ControlPlaneStore(tmp_path / "data.db")
    provider = SimpleNamespace(
        _model=lambda _: SimpleNamespace(effort_choices=("high",))
    )
    coordinator = CoordinatorService(store, SimpleNamespace(), provider)
    conversation = coordinator.create(
        "local", "owner", CoordinatorConfig(model="exact", effort="high")
    )["conversation_id"]
    coordinator.accept_turn(
        "local",
        "owner",
        conversation,
        "turn",
        "Exact Main background\nwith whitespace  é",
        [Goal(id="goal", text="Goal")],
    )
    project = store.create_project("owner", "Example", "Goal", "claude-code")
    worker = store.create_worker(
        project["project_id"],
        "owner",
        "Member",
        "assistant",
        "claude-code",
        "local",
        "claude",
        "exact",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Task")
    with store._connect() as conn:
        conn.execute(
            "UPDATE coordinator_goals SET run_id=? WHERE conversation_id=?",
            (run["run_id"], conversation),
        )
        conn.execute(
            "UPDATE coordinator_turns SET response_json=? WHERE conversation_id=?",
            (
                json.dumps(
                    {
                        "choices": [{"message": {"content": "Visible Main reply"}}],
                        "internal_secret": "must not appear",
                    }
                ),
                conversation,
            ),
        )
    sources = authorized_sources(store, None, worker)
    assert {s["text"] for s in sources} == {
        "Exact Main background\nwith whitespace  é",
        "Visible Main reply",
    }
    assert authorized_sources(store, None, worker | {"owner_id": "other"}) == []
    assert "must not appear" not in json.dumps(sources)
    store.close()


def test_owner_mcp_configuration_reuses_existing_write_scope_and_revision():
    from mcp.server.fastmcp import FastMCP
    from workers_projects_runtime.worker_context_mcp import (
        register_owner_configuration_tools,
        context_tool_manifest,
    )
    from workers_projects_runtime.worker_configuration import ConfigurationUpdate
    from types import SimpleNamespace

    calls = []
    client = SimpleNamespace(
        _request=lambda *args, **kwargs: calls.append((args, kwargs)) or {"revision": 2}
    )
    server = FastMCP("configuration-fixture")
    register_owner_configuration_tools(server, client)
    getter = server._tool_manager.get_tool("worker_configuration_get")
    setter = server._tool_manager.get_tool("worker_configuration_update")
    getter.fn("worker")
    setter.fn("worker", ConfigurationUpdate(expected_revision=1))
    assert calls[0][0] == ("GET", "/v1/workers/worker/configuration")
    assert calls[1][0] == ("PUT", "/v1/workers/worker/configuration")
    assert calls[1][1]["require_write_scope"] is True
    assert calls[1][1]["json_body"]["expected_revision"] == 1
    assert (
        getter.description
        == context_tool_manifest()["tools"]["worker_configuration_get"]
    )
