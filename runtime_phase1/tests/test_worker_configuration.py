import json
import tomllib
from types import SimpleNamespace

import pytest
from workers_projects_runtime.store import Store
from workers_projects_runtime.worker_configuration import (
    WorkerConfiguration,
    ConfigurationUpdate,
    ConfigurationError,
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
