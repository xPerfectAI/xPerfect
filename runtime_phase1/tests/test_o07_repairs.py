"""Causal regressions for independently reproduced O07 failures."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from workers_projects_runtime import bootstrap
from workers_projects_runtime.store import Store
from workers_projects_runtime.peer_collaboration import PeerCollaboration, PeerError
from workers_projects_runtime.worker_configuration import (
    WorkerConfiguration,
    ConfigurationUpdate,
    ConfigurationError,
    enforce_context_tools,
)
from workers_projects_runtime.worker_context_mcp import bind_context_projection
from workers_projects_runtime.profile_runtime import (
    CodexCliRuntime,
    HostNativeCliMixin,
    ProfiledWorkerRuntime,
    _declared_long_mission,
)
from workers_projects_runtime.native_context_projection import (
    materialize_native_context,
)


def live_authority(store, worker, run, now):
    """Synthetic runtime-owned attempt/lease, not merely a running status row."""
    with store._connect() as conn:
        conn.execute(
            "UPDATE runs SET state='running',active_attempt_id='attempt' WHERE run_id=?",
            (run["run_id"],),
        )
        conn.execute(
            "INSERT INTO run_attempts(attempt_id,run_id,attempt_number,state,claimed_at,admitted_at,runtime_invoked_at,lease_id) VALUES('attempt',?,1,'running',?,?,?,'lease')",
            (run["run_id"], *[now.isoformat()] * 3),
        )
        conn.execute(
            """INSERT INTO host_run_leases(lease_id,runtime_family,lane,tenant_id,owner_id,worker_id,run_id,executor_id,attempt_id,status,acquired_at,heartbeat_at,expires_at)
        VALUES('lease','host','mission','local','owner',? ,?,'executor','attempt','active',?,?,?)""",
            (
                worker["worker_id"],
                run["run_id"],
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(seconds=30)).isoformat(),
            ),
        )


@pytest.fixture
def live(tmp_path, monkeypatch):
    store = Store(tmp_path / "db")
    p = store.create_project("owner", "Fixture", "Task", "codex-cli")
    worker = store.create_worker(
        p["project_id"],
        "owner",
        "Member",
        "worker",
        "codex-cli",
        "local",
        "codex",
        "exact",
        bootstrap_bundle={
            "project_definition": "Exact source é",
            "codex_config_append": '[mcp_servers.codex_only]\nurl="https://example.test/mcp"\n',
        },
    )
    peers = PeerCollaboration(store, SimpleNamespace())
    config = WorkerConfiguration(store, peers)
    run = store.create_run(worker["worker_id"], p["project_id"], "Task")
    now = datetime.now(timezone.utc)
    live_authority(store, worker, run, now)
    config.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(expected_revision=1, context={"inline_chars": 0}),
    )
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    projected = bind_context_projection(config, worker, store.get_run(run["run_id"]))
    token = json.loads(projected["bootstrap_bundle_json"])["env"][
        "GLASSHIVE_CONTEXT_TOKEN"
    ]
    yield config, peers, worker, run, token, now
    store.close()


def clock_at(monkeypatch, now):
    class At(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("workers_projects_runtime.peer_collaboration.datetime", At)


def test_hosted_context_retrieval_refuses_plaintext_before_minting(live, monkeypatch):
    config, peers, worker, run, _, _ = live
    minted = []
    peers.mint_native_session = lambda *args, **kwargs: minted.append((args, kwargs))
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "hosted-xfs")
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://runtime:8766")
    monkeypatch.setenv("GLASSHIVE_PUBLIC_BASE_URL", "http://example.test:8443")
    with pytest.raises(ConfigurationError, match="context_endpoint_unavailable"):
        bind_context_projection(config, worker, config.store.get_run(run["run_id"]))
    assert minted == []


def test_hosted_context_refuses_unimplemented_https_bridge_before_minting(live, monkeypatch):
    config, peers, worker, run, _, _ = live
    minted = []
    peers.mint_native_session = lambda *args, **kwargs: minted.append((args, kwargs))
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "hosted-xfs")
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://runtime:8766")
    monkeypatch.setenv("GLASSHIVE_PUBLIC_BASE_URL", "https://example.test:8443")
    with pytest.raises(ConfigurationError, match="context_endpoint_unavailable"):
        bind_context_projection(config, worker, config.store.get_run(run["run_id"]))
    assert minted == []


def test_projected_json_reaches_profile_and_native_consumers(live):
    config, _, worker, run, _, _ = live
    projected = config.prepare_run(worker, config.store.get_run(run["run_id"]))
    assert isinstance(projected["bootstrap_bundle_json"], str)
    bundle = json.loads(projected["bootstrap_bundle_json"])
    bundle.update(
        {
            "run_mode": "conversation",
            "viventium_run_liveness": {"version": 1, "long_mission": True},
            "system_instructions": "Synthetic context",
            "messaging_delivery_control": {"version": 1, "required": True},
            "env": {"WPR_CODEX_CLI_REASONING_EFFORT": "high"},
        }
    )
    projected["bootstrap_bundle_json"] = json.dumps(bundle)
    profile = object.__new__(ProfiledWorkerRuntime)
    codex = object.__new__(CodexCliRuntime)
    native = object.__new__(HostNativeCliMixin)

    assert profile._run_mode_from_worker(projected) == "conversation"
    assert _declared_long_mission(projected) is True
    native_bundle = native._bootstrap_bundle_for_worker(projected)
    assert native_bundle["system_instructions"] == "Synthetic context"
    assert native_bundle["messaging_delivery_control"] == {
        "version": 1,
        "required": True,
    }
    assert codex._bootstrap_env_value(
        projected, "WPR_CODEX_CLI_REASONING_EFFORT"
    ) == "high"


def test_context_renewal_requires_current_lease_and_keeps_peer_expiry(
    live, monkeypatch
):
    config, peers, worker, run, token, now = live
    peer = peers.mint_native_session(worker["worker_id"], run["run_id"])
    with pytest.raises(PeerError):
        peers.native_principal(token)
    with pytest.raises(PeerError):
        config.native_read(peer, "bootstrap:project_definition")
    future = now + timedelta(seconds=3602)
    # Actual runtime heartbeat is the source of renewed liveness.
    with config.store._connect() as conn:
        conn.execute(
            "UPDATE host_run_leases SET heartbeat_at=?,expires_at=? WHERE lease_id='lease'",
            (future.isoformat(), (future + timedelta(seconds=30)).isoformat()),
        )
    clock_at(monkeypatch, future)
    assert (
        config.native_read(token, "bootstrap:project_definition")["text"]
        == "Exact source é"
    )
    with pytest.raises(PeerError):
        peers.native_principal(peer)
    with pytest.raises(PeerError):
        peers.native_principal(token)
    with config.store._connect() as conn:
        expiry = conn.execute(
            "SELECT expires_at FROM peer_native_sessions WHERE purpose='context'"
        ).fetchone()[0]
    assert datetime.fromisoformat(expiry) == future + timedelta(hours=1)


@pytest.mark.parametrize(
    "failure",
    [
        "crash",
        "replaced",
        "ended",
        "released",
        "member_closed",
        "attempt_ended",
        "stale_running",
    ],
)
def test_lost_authority_denies_and_commits_cleanup(live, monkeypatch, failure):
    config, peers, worker, run, token, now = live
    with config.store._connect() as conn:
        if failure == "replaced":
            conn.execute("UPDATE runs SET active_attempt_id='other'")
        elif failure == "ended":
            conn.execute("UPDATE runs SET state='completed'")
        elif failure == "released":
            conn.execute(
                "UPDATE host_run_leases SET status='released',released_at=?",
                (now.isoformat(),),
            )
        elif failure == "member_closed":
            conn.execute("UPDATE workers SET state='terminating'")
        elif failure == "attempt_ended":
            conn.execute("UPDATE run_attempts SET ended_at=?", (now.isoformat(),))
        elif failure == "stale_running":
            conn.execute("DELETE FROM run_attempts")
    if failure == "crash":
        clock_at(monkeypatch, now + timedelta(seconds=31))
    with pytest.raises(PeerError):
        config.native_read(token, "bootstrap:project_definition")
    with config.store._connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM peer_native_sessions WHERE purpose='context'"
            ).fetchone()[0]
            == 0
        )


def test_noncontext_saves_preserve_snapshot_but_removed_context_revokes(live):
    config, _, worker, _, token, _ = live
    config.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(
            expected_revision=2,
            context={"inline_chars": 7},
            background={"enabled": False},
            tools={"mode": "selected", "mcp_server_ids": []},
        ),
    )
    assert (
        config.native_read(token, "bootstrap:project_definition")["text"]
        == "Exact source é"
    )
    config.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(
            expected_revision=3, context={"mode": "selected", "source_ids": []}
        ),
    )
    with pytest.raises(ConfigurationError, match="context_snapshot_unavailable"):
        config.native_read(token, "bootstrap:project_definition")
    config.put(
        "local", "owner", worker["worker_id"], ConfigurationUpdate(expected_revision=4)
    )
    with pytest.raises(ConfigurationError, match="context_snapshot_unavailable"):
        config.native_read(token, "bootstrap:project_definition")


@pytest.mark.parametrize(
    "target",
    [
        "AGENTS.md",
        "agents.md",
        "CLAUDE.md",
        "claude.md",
        "CODEX.md",
        "codex.md",
        ".mcp.json",
        ".claude/settings.local.json",
        ".claude",
    ],
)
def test_private_project_links_never_overwrite_common(tmp_path, target):
    common = tmp_path / "common"
    common.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    output = common / "policy"
    output.write_text("Exact common instructions")
    link = private / target
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(
        common if target == ".claude" else output,
        target_is_directory=target == ".claude",
    )
    preserved = private / "private-sentinel"
    preserved.write_text("Private user data")
    home = tmp_path / "home"
    worker = {
        "_native_context_placement": {
            "home_dir": str(home),
            "native_home": str(home),
            "private_project_dir": str(private),
        },
        "bootstrap_bundle_json": {
            "claude_settings_local": {},
            "claude_project_mcp": {},
        },
    }
    with pytest.raises(PermissionError):
        bootstrap.refresh_project_runtime_files_for_worker(home, common, worker)
    assert output.read_text() == "Exact common instructions"


def test_private_placement_still_refreshes_managed_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    common = tmp_path / "common"
    common.mkdir()
    exact = b"\x00binary\xff"
    calls = []

    def copy(root, entry, worker):
        calls.append(root)
        (root / entry["path"]).write_bytes(exact)

    monkeypatch.setattr(
        "workers_projects_runtime.workspace_files.materialize_managed_file", copy
    )
    worker = {
        "_native_context_placement": {
            "home_dir": str(home),
            "native_home": str(home),
            "private_project_dir": str(tmp_path / "private"),
        },
        "bootstrap_bundle_json": {
            "files": [{"managed_upload_id": "upload", "path": "later.bin"}]
        },
    }
    bootstrap.refresh_project_runtime_files_for_worker(home, common, worker)
    assert calls == [common] and (common / "later.bin").read_bytes() == exact


def test_codex_catalog_selected_empty_and_prior_managed_removal(live, tmp_path):
    config, _, worker, run, _, _ = live
    assert "codex_only" in {
        item["id"]
        for item in config.get("local", "owner", worker["worker_id"])["available_tools"]
    }
    home = tmp_path / "home"
    placement = {"home_dir": str(home), "native_home": str(home)}
    inherited = config.prepare_run(worker, config.store.get_run(run["run_id"]))
    materialize_native_context(
        placement, json.loads(inherited["bootstrap_bundle_json"])
    )
    assert "mcp_servers.codex_only" in (home / ".codex/config.toml").read_text()
    config.put(
        "local",
        "owner",
        worker["worker_id"],
        ConfigurationUpdate(
            expected_revision=2, tools={"mode": "selected", "mcp_server_ids": []}
        ),
    )
    selected = enforce_context_tools(
        config.prepare_run(worker, config.store.get_run(run["run_id"]))
    )
    materialize_native_context(placement, json.loads(selected["bootstrap_bundle_json"]))
    assert "mcp_servers.codex_only" not in (home / ".codex/config.toml").read_text()
    # Removal from authoritative upstream also retires only previously managed config.
    materialize_native_context(
        placement, json.loads(inherited["bootstrap_bundle_json"])
    )
    materialize_native_context(placement, {"claude_project_mcp": {}})
    assert "mcp_servers.codex_only" not in (home / ".codex/config.toml").read_text()


def test_quoted_codex_id_fails_selection_before_acceptance(live):
    config, _, worker, _, _, _ = live
    bundle = {
        "codex_config_append": '[mcp_servers."quoted.id"]\nurl="https://example.test/mcp"'
    }
    with config.store._connect() as conn:
        conn.execute(
            "UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?",
            (json.dumps(bundle), worker["worker_id"]),
        )
    with pytest.raises(ConfigurationError, match="selection_unavailable"):
        config.put(
            "local",
            "owner",
            worker["worker_id"],
            ConfigurationUpdate(
                expected_revision=2, tools={"mode": "selected", "mcp_server_ids": []}
            ),
        )


def test_context_generation_migrates_without_invalidating_current_snapshot(live):
    config, peers, worker, _, token, _ = live
    with config.store._connect() as conn:
        conn.execute("UPDATE worker_context_snapshots SET revision=2")
        conn.execute("ALTER TABLE worker_configurations DROP COLUMN context_revision")
        conn.execute(
            "UPDATE glasshive_schema_versions SET version=1 WHERE component='worker_configuration'"
        )
    migrated = WorkerConfiguration(config.store, peers)
    assert (
        migrated.native_read(token, "bootstrap:project_definition")["text"]
        == "Exact source é"
    )
    with config.store._connect() as conn:
        assert (
            conn.execute(
                "SELECT context_revision FROM worker_configurations"
            ).fetchone()[0]
            == 2
        )


def test_private_refresh_copies_new_authorized_upload_after_initial_setup(
    tmp_path, monkeypatch
):
    import asyncio
    from workers_projects_runtime.workspace_files import WorkspaceFiles

    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    store = Store(tmp_path / "files.db")
    files = WorkspaceFiles(store)
    p = store.create_project("owner", "Files", "Task", "codex-cli")
    w = store.create_worker(
        p["project_id"],
        "owner",
        "Member",
        "worker",
        "codex-cli",
        "local",
        "codex",
        "exact",
    )
    common = tmp_path / "common"
    common.mkdir()
    home = tmp_path / "home"
    placement = {
        "home_dir": str(home),
        "native_home": str(home),
        "private_project_dir": str(tmp_path / "private"),
    }
    w = store.update_worker(w["worker_id"], workspace_dir=str(common))
    bootstrap.refresh_project_runtime_files_for_worker(
        home, common, w | {"_native_context_placement": placement}
    )
    content = b"\x00exact\xff"
    upload = files.create_upload(
        "local",
        "owner",
        draft_id="draft",
        name="later.bin",
        size_bytes=len(content),
        idempotency_key="upload",
    )

    async def chunks():
        yield content

    asyncio.run(files.receive("local", "owner", upload["upload_id"], chunks()))
    bound = files.bind(w["worker_id"], "local", "owner", [upload["upload_id"]], "bind")
    current = store.get_worker(w["worker_id"]) | {
        "_native_context_placement": placement
    }
    bootstrap.refresh_project_runtime_files_for_worker(home, common, current)
    target = common / bound["items"][0]["path"]
    assert target.read_bytes() == content
    target.write_bytes(b"user edit")
    bootstrap.refresh_project_runtime_files_for_worker(home, common, current)
    assert target.read_bytes() == b"user edit"
    store.close()
