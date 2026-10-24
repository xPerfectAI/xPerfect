import json
import tomllib
from types import SimpleNamespace

import pytest
from workers_projects_runtime.store import Store
from workers_projects_runtime.worker_configuration import (
    WorkerConfiguration,
    ConfigurationUpdate,
    ConfigurationError,
    PerformanceConfig,
)
from workers_projects_runtime.native_context_projection import (
    materialize_native_context,
    apply_claude_context,
)


@pytest.fixture
def configured(tmp_path):
    store = Store(tmp_path / "state.db")
    project = store.create_project("owner", "Example", "Fixture", "claude-code")
    worker = store.create_worker(
        project["project_id"],
        "owner",
        "Member",
        "assistant",
        "claude-code",
        "local",
        "claude",
        "exact",
        bootstrap_bundle={
            "project_definition": "Exact source é" * 4000,
            "claude_project_mcp": {
                "allowed": {"type": "http", "url": "https://example.test/mcp"},
                "other": {"type": "http", "url": "https://example.test/other"},
            },
        },
    )
    peers = SimpleNamespace(
        native_principal=lambda token, **kwargs: {
            "worker_id": worker["worker_id"],
            "tenant_id": "local",
            "owner_id": "owner",
            "run_id": "run",
            "attempt_id": "attempt",
        }
    )
    config = WorkerConfiguration(store, peers)
    yield config, worker
    store.close()


def test_owner_revision_and_unavailable_selection(configured):
    core, worker = configured
    view = core.get("local", "owner", worker["worker_id"])
    assert view["revision"] == 1
    assert view["effective"]["context"]["retrievable_chars"] > 0
    assert "Exact source" not in json.dumps(view)
    update = ConfigurationUpdate(
        expected_revision=1,
        context={
            "mode": "selected",
            "source_ids": ["bootstrap:project_definition"],
            "inline_chars": 10,
        },
    )
    assert core.put("local", "owner", worker["worker_id"], update)["revision"] == 2
    with pytest.raises(ConfigurationError, match="configuration_changed"):
        core.put("local", "owner", worker["worker_id"], update)
    with pytest.raises(ConfigurationError, match="worker_unavailable"):
        core.get("local", "other", worker["worker_id"])
    with pytest.raises(ConfigurationError, match="selection_unavailable"):
        core.put(
            "local",
            "owner",
            worker["worker_id"],
            ConfigurationUpdate(
                expected_revision=2,
                tools={"mode": "selected", "mcp_server_ids": ["invented"]},
            ),
        )


def test_performance_settings_save_reload_project_and_preserve_older_clients(configured):
    core, worker = configured
    saved = core.put("local", "owner", worker["worker_id"], ConfigurationUpdate(
        expected_revision=1, performance={"native_warm_servers": 2, "native_warm_idle_seconds": 60}))
    assert saved["requested"]["performance"] == {"native_warm_servers": 2, "native_warm_idle_seconds": 60}
    reopened = WorkerConfiguration(core.store, core.peers)
    assert reopened.get("local", "owner", worker["worker_id"])["requested"]["performance"] == saved["requested"]["performance"]
    # Old clients saving the previous three sections do not turn warming off.
    previous_client = reopened.put("local", "owner", worker["worker_id"], ConfigurationUpdate(expected_revision=2))
    assert previous_client["requested"]["performance"]["native_warm_servers"] == 2
    projected = reopened.prepare_run(worker, {"run_id": "run", "active_attempt_id": "attempt"})
    assert json.loads(projected["bootstrap_bundle_json"])["runtime_performance"] == saved["requested"]["performance"]
    assert previous_client["effective"]["performance"]["native_warm_status"]["supported"] is False


def test_owner_mcp_update_preserves_omitted_performance_and_accepts_explicit_zero(configured):
    from mcp.server.fastmcp import FastMCP
    from workers_projects_runtime.worker_context_mcp import register_owner_configuration_tools

    core, worker = configured
    core.put("local", "owner", worker["worker_id"], ConfigurationUpdate(
        expected_revision=1,
        performance={"native_warm_servers": 2, "native_warm_idle_seconds": 60},
    ))
    bodies = []

    def request(method, route, *, json_body, require_write_scope):
        assert method == "PUT"
        assert route == f"/v1/workers/{worker['worker_id']}/configuration"
        assert require_write_scope is True
        bodies.append(json_body)
        return core.put("local", "owner", worker["worker_id"],
                        ConfigurationUpdate.model_validate(json_body))

    server = FastMCP("configuration-fixture")
    register_owner_configuration_tools(server, SimpleNamespace(_request=request))
    update = server._tool_manager.get_tool("worker_configuration_update")
    saved = update.fn(worker["worker_id"], ConfigurationUpdate(expected_revision=2))
    assert "performance" not in bodies[0]
    assert saved["requested"]["performance"] == {
        "native_warm_servers": 2, "native_warm_idle_seconds": 60,
    }

    stopped = update.fn(worker["worker_id"], ConfigurationUpdate(
        expected_revision=3, performance={"native_warm_servers": 0},
    ))
    assert bodies[1]["performance"]["native_warm_servers"] == 0
    assert stopped["requested"]["performance"]["native_warm_servers"] == 0


@pytest.mark.parametrize("count", [True, -1, 9, 1.5, "1"])
def test_performance_native_server_count_is_typed_and_bounded(count):
    with pytest.raises(ValueError):
        PerformanceConfig(native_warm_servers=count)


def test_exact_overflow_durable_and_revocation(configured):
    core, worker = configured
    projected = core.prepare_run(
        worker, {"run_id": "run", "active_attempt_id": "attempt"}
    )
    assert (
        projected["_context_projection"]["manifest"]["context"]["retrievable_chars"] > 0
    )
    content = ""
    offset = 0
    while True:
        page = core.native_read(
            "synthetic", "bootstrap:project_definition", offset, 10000
        )
        content += page["text"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert content == json.loads(worker["bootstrap_bundle_json"])["project_definition"]
    assert WorkerConfiguration(core.store, core.peers).native_list("synthetic")[0][
        "chars"
    ] == len(content)
    core.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(
            expected_revision=1, context={"mode": "selected", "source_ids": []}
        ),
    )
    with pytest.raises(ConfigurationError, match="context_snapshot_unavailable"):
        core.native_read("synthetic", "bootstrap:project_definition")


def test_exact_tool_subset_retains_runtime_tools(configured):
    core, worker = configured
    core.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(
            expected_revision=1,
            tools={"mode": "selected", "mcp_server_ids": ["allowed"]},
        ),
    )
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["claude_project_mcp"]["runtime-control"] = {
        "type": "http",
        "url": "https://example.test/control",
    }
    bundle["codex_config_append"] = (
        '[mcp_servers.other]\nurl="https://example.test/other"\n[mcp_servers.allowed]\nurl="https://example.test/mcp"'
    )
    projected = core.prepare_run(
        worker | {"bootstrap_bundle_json": bundle},
        {"run_id": "run", "active_attempt_id": "attempt"},
    )
    projected_bundle = json.loads(projected["bootstrap_bundle_json"])
    assert set(projected_bundle["claude_project_mcp"]["mcpServers"]) == {
        "allowed",
        "runtime-control",
    }
    assert (
        "mcp_servers.other" not in projected_bundle["codex_config_append"]
    )


def test_effective_tool_catalog_includes_declared_grok_servers(configured):
    core, worker = configured
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["grok_mcp_servers"] = [
        {"name": "grok-only", "type": "http", "url": "https://example.test/grok"}
    ]
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET profile=?,bootstrap_bundle_json=? WHERE worker_id=?",
            ("grok-build", json.dumps(bundle), worker["worker_id"]),
        )
    view = core.get("local", "owner", worker["worker_id"])
    assert "grok-only" in {item["id"] for item in view["available_tools"]}


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("claude-code", {"claude-only"}),
        ("codex-cli", {"codex-only"}),
        ("grok-build", {"claude-only", "grok-only"}),
    ],
)
def test_effective_tool_catalog_matches_native_harness_projection(configured, profile, expected):
    core, worker = configured
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["claude_project_mcp"] = {
        "claude-only": {"type": "http", "url": "https://example.test/claude"}
    }
    bundle["codex_config_append"] = (
        '[mcp_servers.codex-only]\nurl = "https://example.test/codex"\n'
    )
    bundle["grok_mcp_servers"] = [
        {"name": "grok-only", "type": "http", "url": "https://example.test/grok"}
    ]
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET profile=?,bootstrap_bundle_json=? WHERE worker_id=?",
            (profile, json.dumps(bundle), worker["worker_id"]),
        )
    view = core.get("local", "owner", worker["worker_id"])
    assert {item["id"] for item in view["available_tools"]} == expected
    assert set(view["effective"]["tools"]["mcp_server_ids"]) == expected
    absent = next(iter({"claude-only", "codex-only", "grok-only"} - expected))
    with pytest.raises(ConfigurationError, match="selection_unavailable"):
        core.put(
            "local", "owner", worker["worker_id"],
            ConfigurationUpdate(expected_revision=1, tools={
                "mode": "selected", "mcp_server_ids": [absent],
            }),
        )
    core.put(
        "local", "owner", worker["worker_id"],
        ConfigurationUpdate(expected_revision=1, tools={
            "mode": "selected", "mcp_server_ids": sorted(expected),
        }),
    )
    fresh = core.store.get_worker(worker["worker_id"])
    projected = core.prepare_run(fresh, {"run_id": "run", "active_attempt_id": "attempt"})
    selected_bundle = json.loads(projected["bootstrap_bundle_json"])
    if profile == "claude-code":
        projected_ids = set(selected_bundle["claude_project_mcp"]["mcpServers"])
    elif profile == "codex-cli":
        projected_ids = set(tomllib.loads(selected_bundle["codex_config_append"])["mcp_servers"])
    else:
        from workers_projects_runtime.grok_projection import mcp_servers_for_bundle
        projected_ids = {s["name"] for s in mcp_servers_for_bundle(selected_bundle, {})}
    assert projected_ids == expected


def test_non_native_profile_does_not_claim_worker_mcp_selection(configured):
    core, worker = configured
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET profile=? WHERE worker_id=?",
            ("openclaw-general", worker["worker_id"]),
        )
    view = core.get("local", "owner", worker["worker_id"])
    assert view["available_tools"] == []
    assert view["effective"]["tools"]["mcp_server_ids"] == []
    assert view["effective"]["tools"]["selection_supported"] is False
    with pytest.raises(ConfigurationError, match="selection_unavailable"):
        core.put(
            "local", "owner", worker["worker_id"],
            ConfigurationUpdate(expected_revision=1, tools={
                "mode": "selected", "mcp_server_ids": [],
            }),
        )


@pytest.mark.parametrize("profile", ["claude-code", "codex-cli", "grok-build"])
def test_disabled_declaration_is_not_reported_as_effective_connection(configured, profile):
    core, worker = configured
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["claude_project_mcp"] = {
        "paused": {"type": "http", "url": "https://example.test/paused", "disabled": True}
    }
    bundle["codex_config_append"] = (
        '[mcp_servers.paused]\nurl = "https://example.test/paused"\nenabled = false\n'
    )
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET profile=?,bootstrap_bundle_json=? WHERE worker_id=?",
            (profile, json.dumps(bundle), worker["worker_id"]),
        )
    view = core.get("local", "owner", worker["worker_id"])
    assert view["available_tools"] == []
    assert view["effective"]["tools"]["mcp_server_ids"] == []
    with pytest.raises(ConfigurationError, match="selection_unavailable"):
        core.put(
            "local", "owner", worker["worker_id"],
            ConfigurationUpdate(expected_revision=1, tools={
                "mode": "selected", "mcp_server_ids": ["paused"],
            }),
        )


def test_snapshot_changed_content_and_page_bounds(configured):
    core, worker = configured
    core.prepare_run(worker, {"run_id": "run", "active_attempt_id": "attempt"})
    with pytest.raises(ConfigurationError, match="context_page_invalid"):
        core.native_read("synthetic", "bootstrap:project_definition", -1)
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?",
            (json.dumps({"project_definition": "Changed"}), worker["worker_id"]),
        )
    with pytest.raises(ConfigurationError, match="context_authority_changed"):
        core.native_read("synthetic", "bootstrap:project_definition")


def test_private_native_projection_preserves_common_and_permissions(tmp_path):
    common = tmp_path / "common"
    common.mkdir()
    (common / "AGENTS.md").write_text("Shared user policy")
    (common / ".mcp.json").write_text("Shared MCP")
    home = tmp_path / "member"
    home.mkdir()
    placement = {
        "home_dir": str(home),
        "native_home": "/workspace/data/members/100/home",
        "workspace_dir": str(common),
    }
    selectors = materialize_native_context(
        placement,
        {
            "agents_md": "Member rule",
            "developer_instructions": "Exact content",
            "claude_project_mcp": {},
        },
        projection={"revision": 1},
    )
    assert (common / "AGENTS.md").read_text() == "Shared user policy"
    assert (common / ".mcp.json").read_text() == "Shared MCP"
    assert (
        "Exact content"
        in (home / ".glasshive/native/claude-instructions.md").read_text()
    )
    command = apply_claude_context(
        [
            "claude",
            "-p",
            "--model",
            "exact-model",
            "--settings",
            '{"sandbox":{"enabled":true}}',
            "--mcp-config",
            "old",
            "--strict-mcp-config",
        ],
        selectors,
    )
    assert command.count("--mcp-config") == 1
    assert (
        json.loads(command[command.index("--settings") + 1])["sandbox"]["enabled"]
        is True
    )
    assert command[command.index("--model") + 1] == "exact-model"
    assert (home / ".glasshive/native/mcp.json").stat().st_mode & 0o777 == 0o600
    assert "Member rule" in (home / ".grok/AGENTS.md").read_text()


def test_private_native_projection_rejects_symlink(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".glasshive").symlink_to(outside)
    with pytest.raises(PermissionError):
        materialize_native_context(
            {"home_dir": str(home), "native_home": "/private"}, {}
        )
    assert not list(outside.iterdir())


def test_api_owner_scope_pagination_and_conflict(configured):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from workers_projects_runtime.worker_configuration_api import (
        install_worker_configuration_routes,
    )

    core, worker = configured
    app = FastAPI()

    def principal(request):
        if request.headers.get("x-test-viewer"):
            raise HTTPException(403, "Owner required")
        return "local", "owner"

    install_worker_configuration_routes(app, core, principal)
    with TestClient(app) as client:
        path = f"/v1/workers/{worker['worker_id']}/configuration"
        assert client.get(path).status_code == 200
        assert client.get(path, headers={"x-test-viewer": "true"}).status_code == 403
        page = client.get(
            path + "/context/bootstrap:project_definition?max_chars=13"
        ).json()
        assert len(page["text"]) == 13 and page["next_offset"] == 13
        assert (
            client.get(
                path + "/context/bootstrap:project_definition?max_chars=0"
            ).status_code
            == 422
        )
        payload = {
            "expected_revision": 1,
            "background": {"enabled": False, "max_parallel_runs": 1},
        }
        assert client.put(path, json=payload).status_code == 200
        assert client.put(path, json=payload).status_code == 409
        assert (
            client.put(
                path, json={"expected_revision": 2, "invented": True}
            ).status_code
            == 422
        )


def test_background_guard_obeys_live_invocation_and_foreground(configured):
    from workers_projects_runtime.worker_configuration import guard_worker_configuration

    core, worker = configured
    run = core.store.create_run(worker["worker_id"], worker["project_id"], "Task")
    core.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(
            expected_revision=1, background={"enabled": False, "max_parallel_runs": 1}
        ),
    )
    with core.store._connect() as conn:
        with pytest.raises(ConfigurationError, match="background_disabled"):
            guard_worker_configuration(conn, run["run_id"])
        conn.execute(
            "UPDATE workers SET trusted_run_lane='conversation' WHERE worker_id=?",
            (worker["worker_id"],),
        )
        guard_worker_configuration(conn, run["run_id"])


def test_late_broker_projection_cannot_reenable_unselected_tools(configured):
    from workers_projects_runtime.worker_configuration import (
        enforce_context_tools,
        contextual_instruction,
    )

    core, worker = configured
    core.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(
            expected_revision=1, tools={"mode": "selected", "mcp_server_ids": []}
        ),
    )
    projected = core.prepare_run(
        worker, {"run_id": "run", "active_attempt_id": "attempt"}
    )
    projected_bundle = json.loads(projected["bootstrap_bundle_json"])
    projected_bundle["claude_project_mcp"]["mcpServers"]["late-broker"] = {
        "type": "http",
        "url": "https://example.test/late",
    }
    projected["bootstrap_bundle_json"] = json.dumps(projected_bundle)
    filtered_bundle = json.loads(enforce_context_tools(projected)["bootstrap_bundle_json"])
    assert filtered_bundle["claude_project_mcp"]["mcpServers"] == {}
    raw = "Exact user request\n  with spacing é"
    envelope = json.loads(contextual_instruction(raw, projected))
    assert envelope["instruction"] == raw
    assert envelope["context"][0]["text"]
    assert "context_contract" not in envelope
    assert contextual_instruction(raw, {}) == raw


@pytest.mark.parametrize("mode", ["inherit", "selected"])
@pytest.mark.parametrize("disabled", [False, True])
def test_runtime_context_endpoint_supersedes_owner_name_collision(
    configured, monkeypatch, mode, disabled
):
    from workers_projects_runtime.grok_projection import mcp_servers_for_bundle
    from workers_projects_runtime.worker_configuration import enforce_context_tools
    from workers_projects_runtime.worker_context_mcp import bind_context_projection

    core, worker = configured
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["claude_project_mcp"]["xperfect-context"] = {
        "type": "http", "url": "https://example.test/owner"
    }
    bundle["codex_config_append"] = (
        '[mcp_servers.xperfect-context]\nurl = "https://example.test/owner"\n'
    )
    bundle["grok_mcp_servers"] = [{
        "name": "xperfect-context", "type": "http",
        "url": "https://example.test/owner", "disabled": disabled,
    }]
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET profile=?, bootstrap_bundle_json=? WHERE worker_id=?",
            ("grok-build", json.dumps(bundle), worker["worker_id"]),
        )
    core.put(
        "local", "owner", worker["worker_id"],
        ConfigurationUpdate(expected_revision=1, tools={"mode": mode, "mcp_server_ids": []}),
    )
    core.peers.mint_native_session = lambda *args, **kwargs: "synthetic-context-token"
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8876")
    projected = enforce_context_tools(bind_context_projection(
        core, core.store.get_worker(worker["worker_id"]),
        {"run_id": "run", "active_attempt_id": "attempt"},
    ))
    effective = json.loads(projected["bootstrap_bundle_json"])
    assert projected["_context_projection"]["manifest"]["context"]["retrievable_chars"] > 0
    assert "xperfect-context" not in effective.get("_configured_removed_mcp_servers", [])
    assert all(item["name"] != "xperfect-context" for item in effective["grok_mcp_servers"])
    assert tomllib.loads(effective["codex_config_append"])["mcp_servers"]["xperfect-context"]["url"] == (
        "http://127.0.0.1:8876/v1/native/context/"
    )
    servers = {item["name"]: item for item in mcp_servers_for_bundle(effective, effective["env"])}
    assert servers["xperfect-context"]["url"] == "http://127.0.0.1:8876/v1/native/context/"
    assert "https://example.test/owner" not in json.dumps(servers)
    assert core.store.get_worker(worker["worker_id"])["bootstrap_bundle_json"] == json.dumps(bundle)


def test_configuration_survives_reopen_without_token_or_source_text(configured):
    core, worker = configured
    core.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(
            expected_revision=1,
            context={"mode": "selected", "source_ids": []},
            tools={"mode": "selected", "mcp_server_ids": []},
        ),
    )
    reopened = WorkerConfiguration(core.store, core.peers)
    view = reopened.get("local", "owner", worker["worker_id"])
    assert view["revision"] == 2
    assert view["effective"]["context"]["sources"] == []
    assert view["effective"]["tools"]["mcp_server_ids"] == []


def test_changed_attempt_cannot_retrieve_old_snapshot(configured):
    core, worker = configured
    core.prepare_run(worker, {"run_id": "run", "active_attempt_id": "attempt"})
    prior = core.peers.native_principal("synthetic")
    core.peers.native_principal = lambda token, **kwargs: prior | {"attempt_id": "different"}
    with pytest.raises(ConfigurationError, match="context_snapshot_unavailable"):
        core.native_read("synthetic", "bootstrap:project_definition")


def test_authorized_text_file_pages_exact_bytes_and_binary_is_not_fake_text(
    configured, tmp_path, monkeypatch
):
    import base64

    core, worker = configured
    source = tmp_path / "source.txt"
    source.write_text("Exact uploaded background é")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    bundle = {
        "files": [
            {"scope": "workspace", "path": "source.txt", "source_path": str(source)},
            {
                "scope": "workspace",
                "path": "binary.dat",
                "content_base64": base64.b64encode(b"\xff\xfe").decode(),
            },
        ]
    }
    worker = worker | {"bootstrap_bundle_json": bundle}
    sources = core.sources(worker)
    assert len(sources) == 1
    assert sources[0]["text"] == "Exact uploaded background é"


def test_missing_file_is_unavailable_not_empty_success(
    configured, tmp_path, monkeypatch
):
    core, worker = configured
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?",
            (
                json.dumps(
                    {
                        "files": [
                            {
                                "scope": "workspace",
                                "path": "missing.txt",
                                "source_path": str(tmp_path / "missing.txt"),
                            }
                        ]
                    }
                ),
                worker["worker_id"],
            ),
        )
    view = core.get("local", "owner", worker["worker_id"])
    assert view["available_sources"][0]["status"] == "unavailable"
    assert view["effective"]["context"]["sources"][0]["delivery"] == "unavailable"
    assert view["effective"]["issues"][0]["code"] == "context_unavailable"
    with pytest.raises(ConfigurationError, match="context_unavailable"):
        core.read("local", "owner", worker["worker_id"], "bootstrap:file:0")


def test_required_clean_room_broker_is_not_falsely_disabled(configured):
    from workers_projects_runtime.bootstrap import PARALLEL_CLEAN_ROOM_EXECUTION_POLICY

    core, worker = configured
    with core.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?",
            (
                json.dumps({"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}),
                worker["worker_id"],
            ),
        )
    view = core.get("local", "owner", worker["worker_id"])
    assert view["effective"]["tools"]["selection_supported"] is False
    with pytest.raises(ConfigurationError, match="selection_unavailable"):
        core.put(
            "local",
            "owner",
            worker["worker_id"],
            ConfigurationUpdate(
                expected_revision=1, tools={"mode": "selected", "mcp_server_ids": []}
            ),
        )


@pytest.mark.parametrize("run_id", ["run_current", None])
def test_native_context_manifest_uses_current_runtime_run_without_environment(monkeypatch, run_id):
    from workers_projects_runtime.worker_configuration import contextual_instruction

    monkeypatch.delenv("GLASSHIVE_RUN_ID", raising=False)
    context = {"inline_chars": 17, "retrievable_chars": 23, "sources": ["source"], "run_id": "run_stale"}
    projection = {"inline": [{"id": "source", "text": "Exact context é\n"}], "manifest": {"context": context}}
    worker = {"_context_projection": projection, "_active_run_id": run_id}
    instruction = "Exact current request é\n  no normalization"
    envelope = json.loads(contextual_instruction(instruction, worker))
    assert set(envelope) == {"instruction", "context", "context_manifest"}
    assert envelope["instruction"] == instruction
    assert envelope["context"] == projection["inline"]
    expected = {key: value for key, value in context.items() if key != "run_id"}
    if run_id:
        expected["run_id"] = run_id
    assert envelope["context_manifest"] == expected
    assert context["run_id"] == "run_stale"  # Shared saved projection stays unchanged.


def test_inherited_conversation_uses_existing_exact_snapshot_reader(configured):
    from workers_projects_runtime.worker_context_sources import authorized_sources
    core, worker = configured
    from workers_projects_runtime.control_plane import ControlPlaneStore
    ControlPlaneStore(core.store.db_path)
    source_text = 'Previous accepted conversation — exact words.\n' * 1800
    inherited = {'version':1,'conversation_id':'conversation-1','sources':[
        {'id':'message:old','role':'user','text':source_text},
        {'id':'message:reply','role':'assistant','text':'The prior task is done.'},
        {'id':'message:current','role':'user','text':'Start the new task.'},
    ]}
    bundle=json.loads(worker['bootstrap_bundle_json']);bundle['inherited_conversation']=inherited
    with core.store._connect() as conn:
        conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',(json.dumps(bundle),worker['worker_id']))
    fresh=core.store.get_worker(worker['worker_id'])
    core.source_resolver=lambda item: authorized_sources(core.store,None,item)
    sources=core.sources(fresh)
    selected=next(item for item in sources if item['id'].endswith(':message:old'))
    assert json.loads(selected['text'])['text']==source_text
    projected=core.prepare_run(fresh,{'run_id':'run','active_attempt_id':'attempt'})
    assert projected['_context_projection']['manifest']['context']['retrievable_chars']>0
    text='';offset=0
    while True:
        page=core.native_read('synthetic',selected['id'],offset,10000)
        text+=page['text'];offset=page['next_offset']
        if offset is None: break
    assert text==selected['text']
    with pytest.raises(ConfigurationError,match='worker_unavailable'):
        core.read('local','another-owner',worker['worker_id'],selected['id'])


def test_current_and_recent_context_precede_large_history_and_bootstrap(configured):
    from workers_projects_runtime.worker_context_sources import authorized_sources
    from workers_projects_runtime.control_plane import ControlPlaneStore
    from workers_projects_runtime.store import canonical_parallel_clean_room_bootstrap
    from workers_projects_runtime.worker_configuration import contextual_instruction
    core, worker = configured
    ControlPlaneStore(core.store.db_path)
    inherited = {'version':1,'conversation_id':'conversation-1','assigned_source_ordinals':[2], 'sources':[
        {'id':'old','role':'user','text':'Old history. '*4000,'current_input':False,'source_ordinals':[]},
        {'id':'first','role':'user','text':'The checkword is Amber.', 'current_input':False,'source_ordinals':[]},
        {'id':'current','role':'user','text':'Write the checkword and 143.', 'current_input':True,'assigned_input':True,'source_ordinals':[2]},
        {'id':'context:preparedContinuity','role':'context','text':'Prepared memory and Feelings.'},
    ]}
    bundle=json.loads(worker['bootstrap_bundle_json'])
    bundle['inherited_conversation']=inherited
    # Same clean-room canonical owner used by native account admission and storage.
    bundle=canonical_parallel_clean_room_bootstrap(bundle)
    assert bundle['inherited_conversation']==inherited
    with core.store._connect() as conn:
        conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',(json.dumps(bundle),worker['worker_id']))
    core.source_resolver=lambda item: authorized_sources(core.store,None,item)
    projected=core.prepare_run(core.store.get_worker(worker['worker_id']),{'run_id':'run','active_attempt_id':'attempt'})
    inline=projected['_context_projection']['inline']
    assert inline[0]['id'].endswith(':current')
    assert inline[1]['id'].endswith(':first')
    assert any('Amber' in row['text'] for row in inline)
    envelope=json.loads(contextual_instruction('Write the checkword and 143.',projected))
    assert envelope['instruction']=='Write the checkword and 143.'
    assert 'not additional tasks' in envelope['context_contract']
    assert json.loads(inline[0]['text'])['assigned_input'] is True


def test_recent_conversation_precedes_large_archive_without_losing_paged_sources(configured):
    from workers_projects_runtime.worker_context_sources import authorized_sources
    from workers_projects_runtime.control_plane import ControlPlaneStore
    from workers_projects_runtime.worker_configuration import contextual_instruction
    from workers_projects_runtime.worker_context_mcp import context_tool_manifest

    core, worker = configured
    ControlPlaneStore(core.store.db_path)
    archive = 'Exact continuity archive.\n' * 4600
    inherited = {'version': 1, 'conversation_id': 'conversation-1', 'sources': [
        {'id': 'old', 'role': 'user', 'text': 'Older local history.\n' * 2000},
        {'id': 'recent', 'role': 'user', 'text': 'The checkword is Azure.'},
        {'id': 'assigned', 'role': 'user', 'text': 'Write the checkword.',
         'current_input': True, 'assigned_input': True, 'source_ordinals': [1]},
        {'id': 'other-current', 'role': 'user', 'text': 'Keep the formatting.',
         'current_input': True, 'assigned_input': False, 'source_ordinals': [2]},
        {'id': 'archive', 'role': 'context', 'text': archive},
    ]}
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['inherited_conversation'] = inherited
    with core.store._connect() as conn:
        conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',
                     (json.dumps(bundle), worker['worker_id']))
    fresh = core.store.get_worker(worker['worker_id'])
    core.source_resolver = lambda item: authorized_sources(core.store, None, item)
    projected = core.prepare_run(fresh, {'run_id': 'run', 'active_attempt_id': 'attempt'})
    inline = projected['_context_projection']['inline']
    assert [row['id'].rsplit(':', 1)[-1] for row in inline[:4]] == [
        'assigned', 'other-current', 'recent', 'old']
    assert any('Azure' in row['text'] for row in inline)
    assert sum(len(row['text']) for row in inline) == 24000
    archive_source = next(row for row in core.sources(fresh) if row['id'].endswith(':archive'))
    assert next(row['text'] for row in inline if row['id'] == archive_source['id']) == ''
    text, offset = '', 0
    while True:
        page = core.native_read('synthetic', archive_source['id'], offset, 10000)
        text += page['text']
        offset = page['next_offset']
        if offset is None:
            break
    assert text == archive_source['text']
    assert json.loads(text)['text'] == archive
    envelope = json.loads(contextual_instruction('Write the checkword.', projected))
    assert envelope['context_contract'] == context_tool_manifest()['inherited_context_contract']


def test_inherited_contract_is_absent_when_only_ordinary_sources_selected(configured):
    from workers_projects_runtime.worker_context_sources import authorized_sources
    from workers_projects_runtime.control_plane import ControlPlaneStore
    from workers_projects_runtime.worker_configuration import contextual_instruction

    core, worker = configured
    ControlPlaneStore(core.store.db_path)
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['inherited_conversation'] = {'version': 1, 'conversation_id': 'conversation-1',
        'sources': [{'id': 'old', 'role': 'user', 'text': 'Earlier request.'}]}
    with core.store._connect() as conn:
        conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',
                     (json.dumps(bundle), worker['worker_id']))
    core.source_resolver = lambda item: authorized_sources(core.store, None, item)
    core.put('local', 'owner', worker['worker_id'], ConfigurationUpdate(expected_revision=1,
        context={'mode': 'selected', 'source_ids': ['bootstrap:project_definition']}))
    projected = core.prepare_run(core.store.get_worker(worker['worker_id']),
                                 {'run_id': 'run', 'active_attempt_id': 'attempt'})
    envelope = json.loads(contextual_instruction('Ordinary native work.', projected))
    assert 'context_contract' not in envelope


@pytest.mark.parametrize('continuation', [False, True])
def test_assigned_clause_keeps_shared_conditions_in_start_and_followup_context(configured, continuation):
    from workers_projects_runtime.worker_context_sources import authorized_sources
    from workers_projects_runtime.control_plane import ControlPlaneStore
    from workers_projects_runtime.worker_configuration import contextual_instruction
    from workers_projects_runtime.worker_context_mcp import context_tool_manifest

    core, worker = configured
    ControlPlaneStore(core.store.db_path)
    instruction = 'Add a sample row to the data file.'
    original = (instruction + ' Separately, open the public reference page. '
                'For both tasks, use only synthetic data and do not publish anything.')
    inherited = {'version': 1, 'conversation_id': 'conversation-1', 'assigned_source_ordinals': [1],
                 'sources': [{'id': 'current', 'role': 'user', 'text': original,
                              'current_input': True, 'assigned_input': True, 'source_ordinals': [1]}]}
    if continuation:
        run = core.store.create_run(worker['worker_id'], worker['project_id'], instruction,
                                    continuation_context={'inherited_conversation': inherited})
    else:
        bundle = json.loads(worker['bootstrap_bundle_json'])
        bundle['inherited_conversation'] = inherited
        with core.store._connect() as conn:
            conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',
                         (json.dumps(bundle), worker['worker_id']))
        worker = core.store.get_worker(worker['worker_id'])
        run = {'run_id': 'synthetic-start'}
    core.source_resolver = lambda item: authorized_sources(core.store, None, item)
    projected = core.prepare_run(worker, {**run, 'active_attempt_id': 'attempt'})
    envelope = json.loads(contextual_instruction(instruction, projected))
    current = next(row for row in envelope['context'] if row['id'].endswith(':current'))
    assert envelope['instruction'] == instruction
    assert json.loads(current['text'])['text'] == original
    manifest = context_tool_manifest()
    assert manifest['version'] == 3
    assert envelope['context_contract'] == manifest['inherited_context_contract']
    assert 'Apply conditions, limits and output requirements from those sources that govern the assigned task.' in envelope['context_contract']
    assert 'Do not execute unassigned or historical requests.' in envelope['context_contract']


def test_followup_run_reads_its_durable_current_context(configured):
    from workers_projects_runtime.worker_context_sources import authorized_sources
    from workers_projects_runtime.control_plane import ControlPlaneStore
    core,worker=configured
    ControlPlaneStore(core.store.db_path)
    core.source_resolver=lambda item: authorized_sources(core.store,None,item)
    run=core.store.create_run(worker['worker_id'],worker['project_id'],'Use the new checkword.',continuation_context={
        'inherited_conversation':{'version':1,'conversation_id':'conversation-1','assigned_source_ordinals':[1],'sources':[
            {'id':'current','role':'user','text':'The new checkword is Amber.', 'current_input':True,'assigned_input':True,'source_ordinals':[1]}
        ]}
    })
    core.peers.native_principal=lambda token,**kwargs:{'worker_id':worker['worker_id'],'tenant_id':'local','owner_id':'owner','run_id':run['run_id'],'attempt_id':'attempt'}
    projected=core.prepare_run(worker,{**run,'active_attempt_id':'attempt'})
    source=next(row for row in projected['_context_projection']['inline'] if row['id'].endswith(':current'))
    read=core.native_read('synthetic',source['id'],0,10000)
    assert json.loads(read['text'])['text']=='The new checkword is Amber.'
    assert 'inherited_conversation' not in json.loads(core.store.get_worker(worker['worker_id'])['bootstrap_bundle_json'])


def test_real_clean_room_admission_persists_inherited_history(account_client, monkeypatch):
    from test_account_api import account_headers, conversation_orchestrator_payload
    service,store=account_client.app.state.service,account_client.app.state.store
    service.executor.submit=lambda *args,**kwargs: None
    monkeypatch.setenv('VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY','1')
    payload=conversation_orchestrator_payload(title='Inherited context contract')
    context={'version':1,'conversation_id':'conversation-1','assigned_source_ordinals':[2],'sources':[
        {'id':'earlier','role':'user','text':'The checkword is Amber.','current_input':False,'source_ordinals':[]},
        {'id':'current','role':'user','text':'Write that checkword.','current_input':True,'source_ordinals':[2],'assigned_input':True}]}
    payload['bootstrapBundle']['inherited_conversation']=context
    response=account_client.post('/v1/delegations',headers=account_headers(idempotency_key='context-clean-room'),json=payload)
    assert response.status_code==202,response.text
    accepted=store.get_delegation(response.json()['workRef'],tenant_id='tenant-a',owner_id='owner-a')
    worker=store.get_worker(accepted['worker_id'])
    assert json.loads(worker['bootstrap_bundle_json'])['inherited_conversation']==context
    run=store.get_run(accepted['current_run_id'])
    projected=service.worker_configuration.prepare_run(worker,{**run,'active_attempt_id':'synthetic-attempt'})
    assert any('Amber' in row['text'] for row in projected['_context_projection']['inline'])

from test_account_api import account_client


def test_inherited_source_lone_surrogate_preserves_decoded_fields_through_native_reader(configured):
    from workers_projects_runtime.worker_context_sources import authorized_sources
    from workers_projects_runtime.control_plane import ControlPlaneStore
    core,worker=configured
    ControlPlaneStore(core.store.db_path)
    original={'id':'current','role':'user','text':'Exact é and malformed \ud800 unit.',
        'current_input':True,'assigned_input':True,'source_ordinals':[1],
        'tool_calls':[{'name':'prior_tool','args':{'value':0.00005,'lone':'\udfff','telegramUserId':'historical-data'}}]}
    bundle=json.loads(worker['bootstrap_bundle_json'])
    bundle['inherited_conversation']={'version':1,'conversation_id':'conversation','sources':[original]}
    with core.store._connect() as conn:
        conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',(json.dumps(bundle),worker['worker_id']))
    core.source_resolver=lambda item:authorized_sources(core.store,None,item)
    projected=core.prepare_run(core.store.get_worker(worker['worker_id']),{'run_id':'run','active_attempt_id':'attempt'})
    source=next(row for row in projected['_context_projection']['inline'] if row['id'].endswith(':current'))
    assert 'é' in source['text']
    assert json.loads(source['text'])==original
    read=core.native_read('synthetic',source['id'],0,10000)
    assert json.loads(read['text'])==original
