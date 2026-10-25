"""Requires the O07 shared hooks; executes native command construction without a provider."""

import json
from pathlib import Path
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


def test_unrouted_run_stops_before_start_and_names_the_cause(tmp_path, monkeypatch):
    """A deployment that gives workers no runtime route (no packaged socket, no
    configured address) cannot offer background beyond the inline limit."""
    import time
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app

    for name in (
        "GLASSHIVE_PEER_RUNTIME_BASE_URL",
        "XPERFECT_EXECUTION_PROFILE",
        "XPERFECT_CONTROL_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WPR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WPR_DEFAULT_OWNER_ID", "owner")
    app = create_app(
        db_path=str(tmp_path / "state.db"),
        runtime_backend="stub",
        reconcile_on_startup=False,
    )
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={
            "owner_id": "owner", "title": "Example", "goal": "Goal",
            "default_worker_profile": "claude-code",
        }).json()
        worker = client.post(f"/v1/projects/{project['project_id']}/workers", json={
            "owner_id": "owner", "name": "Member", "role": "main",
            "profile": "claude-code", "start_synchronously": False,
            "bootstrap_bundle": {"project_definition": "Synthetic line.\n" * 2000},
        }).json()
        configuration = client.get(f"/v1/workers/{worker['worker_id']}/configuration")
        effective = configuration.json()["effective"]
        assert effective["context"]["retrievable_chars"] == 32000 - 24000
        assert [issue["code"] for issue in effective["issues"]] == [
            "context_retrieval_unavailable"
        ]
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Use the whole background."},
        ).json()
        deadline = time.time() + 10
        while time.time() < deadline:
            state = client.get(f"/v1/runs/{run['run_id']}").json()
            if state["state"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
    assert state["state"] == "failed"
    assert state["failure_class"] == "context_endpoint_unavailable"
    assert "inline limit" in state["failure_user_message"]
    assert "Workspace settings" in state["failure_recommended_recovery"]


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
        _model=lambda _, **_scope: SimpleNamespace(effort_choices=("high",))
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
    unrelated = coordinator.create(
        "local", "owner", CoordinatorConfig(model="exact", effort="high")
    )["conversation_id"]
    coordinator.accept_turn(
        "local", "owner", unrelated, "other-turn", "Unrelated same-owner content",
        [Goal(id="other-goal", text="Other goal")],
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
    worker = {**worker, "_context_run_id": run["run_id"]}
    sources = authorized_sources(store, None, worker)
    assert {s["text"] for s in sources} == {
        "Exact Main background\nwith whitespace  é",
        "Visible Main reply",
    }
    assert authorized_sources(store, None, worker | {"owner_id": "other"}) == []
    assert "must not appear" not in json.dumps(sources)
    assert "Unrelated same-owner content" not in json.dumps(sources)
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


@pytest.mark.parametrize("profile", ["codex-cli", "claude-code", "grok-build"])
@pytest.mark.parametrize("native_tools", [True, False])
def test_host_mission_configuration_stays_private_with_shared_project(tmp_path, monkeypatch, profile, native_tools):
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime, HostClaudeCodeRuntime
    from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
    runtime_type = {"codex-cli": HostCodexCliRuntime, "claude-code": HostClaudeCodeRuntime,
                    "grok-build": HostGrokBuildRuntime}[profile]
    owner_home = tmp_path / "owner"
    (owner_home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("CODEX_HOME", str(owner_home / ".codex"))
    runtime = runtime_type(str(tmp_path / "state"))
    monkeypatch.setattr(runtime, "_host_plugin_denylist", lambda: [])
    monkeypatch.setattr(runtime, "_host_codex_personality", lambda: "inherit")
    monkeypatch.setattr(runtime, "_project_host_codex_capability_roots", lambda _: None)
    monkeypatch.setattr(runtime, "_project_host_claude_capability_roots", lambda _: None)
    monkeypatch.setattr(runtime, "_copy_host_codex_auth", lambda _: None)
    workspace = tmp_path / "shared"
    workspace.mkdir()
    owned = {"AGENTS.md": b"Owner project rules", "CLAUDE.md": b"Owner Claude rules",
             ".mcp.json": b'{"mcpServers":{"owner-tool":{"url":"https://example.test/owner"}}}'}
    for name, data in owned.items():
        (workspace / name).write_bytes(data)
    bundle = {"agents_md": "Exact synthetic worker instructions.",
              "provider_capabilities": {"native_tools": native_tools},
              "claude_project_mcp": {"scoped-tools": {"type": "http", "url": "https://example.test/scoped"}}}
    if profile == "codex-cli":
        bundle["codex_config_append"] = '[mcp_servers.scoped-tools]\nurl="https://example.test/scoped"\n'
    worker = {"worker_id": "worker-" + profile, "owner_id": "local", "profile": profile,
              "model": "synthetic-model", "trusted_run_lane": "mission", "execution_mode": "host",
              "workspace_dir": str(workspace), "workspace_root": str(workspace),
              "bootstrap_bundle_json": json.dumps(bundle)}
    actual_workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, actual_workspace)
    assert all((workspace / name).read_bytes() == data for name, data in owned.items())
    assert Path(runtime._host_native_mission_placement(worker)["workspace_dir"]) == actual_workspace
    selectors = worker["_native_context_selectors"]
    config = Path(selectors["host_root"]) / "mcp.json"
    assert config.is_file() and config.stat().st_mode & 0o777 == 0o600
    assert json.loads(config.read_text())["mcpServers"] == (bundle["claude_project_mcp"] if native_tools else {})
    assert "Exact synthetic worker instructions." in Path(selectors["codex_instructions"]).read_text()
    if profile == "codex-cli":
        private_codex = runtime._home_dir(worker["worker_id"]) / ".codex/config.toml"
        assert ("[mcp_servers.scoped-tools]" in private_codex.read_text()) is native_tools
    assert (actual_workspace / "glasshive-host-tools/capture-front-window.sh").is_file()
    assert json.loads(worker["bootstrap_bundle_json"]) == bundle
    # An existing worker may still have earlier generated log mandates. A later
    # run refreshes its private authority without changing owner project files.
    stale = "Keep `work-log.md` current with concise progress, blockers, and completion notes."
    for path in [Path(selectors["codex_instructions"]),
                 Path(selectors["claude_instructions"]),
                 runtime._home_dir(worker["worker_id"]) / ".grok/AGENTS.md"]:
        path.write_text(stale)
    refresh_project_runtime_files_for_worker(runtime._home_dir(worker["worker_id"]), actual_workspace, worker)
    assert all((workspace / name).read_bytes() == data for name, data in owned.items())
    # Reproduce the original-worker command path after ensure_worker_ready copied it.
    from workers_projects_runtime.native_context_projection import prepare_private_command
    from workers_projects_runtime.profile_runtime import HOST_DEFAULT_AGENTS_MD, HOST_DEFAULT_CLAUDE_MD
    worker.pop("_native_context_selectors", None)
    final = prepare_private_command(runtime, worker, bundle)
    selected_authority = Path(final["codex_instructions"]).read_text()
    assert HOST_DEFAULT_AGENTS_MD in selected_authority
    assert "Exact synthetic worker instructions." in selected_authority
    assert HOST_DEFAULT_CLAUDE_MD in Path(final["claude_instructions"]).read_text()
    for path in [Path(final["codex_instructions"]), Path(final["claude_instructions"]),
                 runtime._home_dir(worker["worker_id"]) / ".grok/AGENTS.md"]:
        assert stale not in path.read_text()
        assert "keep `work-log.md` updated" not in path.read_text()
    assert all((workspace / name).read_bytes() == data for name, data in owned.items())
    if profile == "claude-code":
        runtime._assert_host_claude_mcp_config(worker, Path(final["mcp_config"]))
    if profile == "codex-cli":
        runtime._assert_host_codex_worker_policy(worker)


def test_mixed_host_missions_never_replace_shared_native_config(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime, HostClaudeCodeRuntime
    from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
    owner = tmp_path / "owner"
    (owner / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(owner))
    monkeypatch.setenv("CODEX_HOME", str(owner / ".codex"))
    shared = tmp_path / "shared"
    shared.mkdir()
    user_config = b'{"mcpServers":{"owner-tool":{"url":"https://example.test/owner"}}}'
    (shared / ".mcp.json").write_bytes(user_config)
    pairs = []
    for profile, runtime_type in [("codex-cli", HostCodexCliRuntime), ("claude-code", HostClaudeCodeRuntime), ("grok-build", HostGrokBuildRuntime)]:
        runtime = runtime_type(str(tmp_path / "state"))
        for method in ("_project_host_codex_capability_roots", "_project_host_claude_capability_roots", "_copy_host_codex_auth"):
            monkeypatch.setattr(runtime, method, lambda _: None)
        monkeypatch.setattr(runtime, "_host_plugin_denylist", lambda: [])
        monkeypatch.setattr(runtime, "_host_codex_personality", lambda: "inherit")
        bundle = {"agents_md": "Shared synthetic task authority.", "claude_project_mcp": {"scoped-tools": {"type": "http", "url": "https://example.test/scoped"}}}
        worker = {"worker_id": "mixed-" + profile, "profile": profile, "owner_id": "local", "execution_mode": "host", "trusted_run_lane": "mission", "workspace_dir": str(shared), "bootstrap_bundle_json": json.dumps(bundle)}
        pairs.append((runtime, worker))
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda pair: pair[0]._materialize_workspace(pair[1], shared), pairs))
    assert (shared / ".mcp.json").read_bytes() == user_config
    assert len({worker["_native_context_selectors"]["mcp_config"] for _, worker in pairs}) == 3
    for runtime, worker in pairs:
        config = Path(worker["_native_context_selectors"]["mcp_config"])
        assert json.loads(config.read_text())["mcpServers"]["scoped-tools"]["url"] == "https://example.test/scoped"
        assert runtime._host_native_mission_placement({**worker, "profile": "opencode"}) is None


@pytest.mark.parametrize('lane,bundle_mode', [('conversation', None), ('conversation', 'conversation'), ('mission', 'conversation')])
def test_creation_preserves_trusted_lane_before_host_native_materialization(tmp_path, monkeypatch, lane, bundle_mode):
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
    from workers_projects_runtime.service import WorkersProjectsService
    from workers_projects_runtime.store import Store
    import workers_projects_runtime.profile_runtime as profiles
    monkeypatch.setattr(profiles, 'require_native_installed_owner', lambda _: None)
    native = HostGrokBuildRuntime(str(tmp_path / 'native'))
    store = Store(str(tmp_path / 'state.db'))
    runtime = StubRuntime()
    observed = []
    def prepare(worker):
        # This is the real service create -> Store -> native preparation boundary,
        # before provider-session upsert can repair a wrongly defaulted lane.
        observed.append((worker['trusted_run_lane'], store.get_worker(worker['worker_id'])['trusted_run_lane']))
        return native.prepare_worker_workspace(worker)
    runtime.prepare_worker_workspace = prepare
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service.executor.submit(lambda: None).result(timeout=2)
    service.executor.submit = lambda *args, **kwargs: None
    monkeypatch.setattr(service, '_reserved_runtime_preflight', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, '_ensure_execution_allowed', lambda *args, **kwargs: None)
    project = store.create_project('owner', 'Native context', 'Keep purpose at creation', 'grok-build')
    bundle = {'provider_model': 'grok-selected', 'developer_instructions': 'Exact conversation authority.'}
    if bundle_mode: bundle['run_mode'] = bundle_mode
    try:
        worker = service.create_worker(project['project_id'], 'owner', 'Synthetic agent', 'assistant',
            'grok-build', '', execution_mode='host', workspace_root=str(tmp_path / 'workspace'),
            bootstrap_bundle=bundle, start_synchronously=False, _trusted_run_lane=lane)
        assert observed == [(lane, lane)]
        home = native._home_dir(worker['worker_id'])
        global_rules = home / '.grok/AGENTS.md'
        assert global_rules.exists() is (lane == 'mission')
        if lane == 'conversation':
            # Native global user instructions are outside runtime-owned authority.
            # A final refresh must neither replace nor remove such a file.
            global_rules.parent.mkdir(parents=True, exist_ok=True)
            global_rules.write_text('User-owned global policy.')
            final = {**store.get_worker(worker['worker_id']),
                     'bootstrap_bundle_json': json.dumps({**bundle, 'run_mode': 'conversation'})}
            assert native._host_native_mission_placement(final) is None
            native.prepare_worker_workspace(final)
            assert global_rules.read_text() == 'User-owned global policy.'
            # No native mission defaults can be appended after the exact override.
            command = native._grok_command(final, native._host_runtime_info(final, pid=None), host=True)
            selected = Path(command[command.index('--system-prompt-file') + 1]).read_text()
            assert selected == 'Exact conversation authority.'
        else:
            assert 'Exact conversation authority.' in global_rules.read_text()
            assert native._host_native_mission_placement(worker) is not None
    finally:
        service.shutdown()
