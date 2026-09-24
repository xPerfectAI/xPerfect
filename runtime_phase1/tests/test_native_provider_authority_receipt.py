import hashlib
import json
import stat
import subprocess
import tomllib

import pytest

from workers_projects_runtime.bootstrap import (
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    apply_bootstrap,
    refresh_project_runtime_files_for_worker,
)
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
from workers_projects_runtime.profile_runtime import (
    ClaudeCodeRuntime,
    CodexCliRuntime,
    HostClaudeCodeRuntime,
    HostCodexCliRuntime,
)


def _conversation_worker(tmp_path, developer_instructions: str) -> tuple[HostClaudeCodeRuntime, dict]:
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_native_authority_receipt",
        "trusted_run_lane": "conversation",
        "name": "Viventium Main",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "full",
                "developer_instructions": developer_instructions,
            }
        ),
    }
    return runtime, worker


def _direct_worker(
    tmp_path,
    runtime_type,
    capsule: str,
    *,
    enabled: bool = True,
    scope: str = "all_agents",
    clean_room: bool = False,
):
    runtime = runtime_type(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": f"wrk_direct_{runtime.runtime_name.replace('-', '_')}",
        "trusted_run_lane": "mission",
        "profile": runtime.runtime_name,
        "runtime": runtime.runtime_name,
        "execution_mode": "docker",
        "model": (
            "claude-fable-5"
            if runtime.runtime_name == "claude-code"
            else "gpt-5.6-sol"
        ),
    }
    bundle = {
        "agents_md": f"Stable shared worker authority.\n\n{capsule}",
        "system_instructions": "Additional structural worker authority.",
        "claude_md": "Claude-specific worker policy.",
        "codex_md": "Codex-specific worker policy.",
        "claude_project_mcp": {
            "mcpServers": {
                "glasshive-user-capabilities": {
                    "type": "http",
                    "url": "http://host.docker.internal:8080/mcp",
                }
            }
        },
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://host.docker.internal:8080/mcp"'
        ),
        "viventium_feelings_projection": {
            "version": 1,
            "enabled": enabled,
            "scope": scope,
            "canonical_instruction_field": "agents_md",
            "snapshot_sha256": hashlib.sha256(capsule.encode()).hexdigest(),
            "expected_capsule_count": int(enabled),
        },
    }
    if clean_room:
        worker["bootstrap_profile"] = "clean-room"
        bundle["execution_policy"] = PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    runtime._ensure_dirs(worker["worker_id"])
    apply_bootstrap(
        home_dir=runtime._home_dir(worker["worker_id"]),
        workspace_dir=runtime._workspace_dir(worker["worker_id"]),
        runtime_name=runtime.runtime_name,
        worker=worker,
        copy_file=lambda _source, _target: None,
        copy_tree=lambda _source, _target: None,
    )
    return runtime, worker


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_direct_worker_native_boundary_receives_exactly_one_pinned_capsule(
    tmp_path, runtime_type
):
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic identical request-pinned state — café 🧭.\n"
        "</viventium_feeling_state>"
    )
    runtime, worker = _direct_worker(tmp_path, runtime_type, capsule)
    workspace = runtime._workspace_dir(worker["worker_id"])
    source_authority = (workspace / "AGENTS.md").read_text()
    assert source_authority.rstrip().endswith(capsule)

    command, env = runtime._build_command(
        worker,
        "Return only the requested synthetic artifact.",
        runtime._runtime_info(worker),
    )
    run_id = f"run-direct-{runtime.runtime_name}"
    receipt = runtime._native_provider_authority_receipt(
        worker,
        command=command,
        run_id=run_id,
        model=worker["model"],
    )

    if runtime.runtime_name == "claude-code":
        assert command.count("--append-system-prompt-file") == 1
        assert "--fallback-model" not in command
        assert command[command.index("--append-system-prompt-file") + 1] == (
            f"{runtime.sandbox.home_mount}/.glasshive/developer-instructions.txt"
        )
        authority_path = (
            runtime._home_dir(worker["worker_id"])
            / ".glasshive"
            / "developer-instructions.txt"
        )
        native_authority = authority_path.read_text()
        assert receipt["placement"] == "append_system_prompt_file"
        assert "@AGENTS.md" not in (workspace / "CLAUDE.md").read_text()
        assert "Claude-specific worker policy." in (
            workspace / "CLAUDE.md"
        ).read_text()
        assert set(
            json.loads((workspace / ".mcp.json").read_text())["mcpServers"]
        ) == {"glasshive-user-capabilities"}
    else:
        authority_path = (
            runtime._home_dir(worker["worker_id"]) / ".codex" / "config.toml"
        )
        native_authority = tomllib.loads(authority_path.read_text())[
            "developer_instructions"
        ]
        assert receipt["placement"] == "codex_developer_instructions"
        assert "project_doc_max_bytes=0" in command
        assert set(tomllib.loads(authority_path.read_text())["mcp_servers"]) == {
            "glasshive-user-capabilities"
        }

    assert native_authority == source_authority
    assert native_authority.count(capsule) == 1
    assert native_authority.rstrip().endswith(capsule)
    assert stat.S_IMODE(authority_path.stat().st_mode) == 0o600
    assert receipt["runtime"] == runtime.runtime_name
    assert receipt["model"] == worker["model"]
    assert receipt["run_id"] == run_id
    assert receipt["authority_sha256"] == hashlib.sha256(
        source_authority.encode()
    ).hexdigest()
    assert receipt["feeling_capsule_count"] == 1
    assert receipt["materialized"] is True
    for filename in ("CLAUDE.md", "CODEX.md"):
        assert capsule not in (workspace / filename).read_text()
    assert (workspace / "AGENTS.md").read_text() == source_authority
    assert capsule not in json.dumps(command)
    assert capsule not in json.dumps(env)
    assert capsule not in json.dumps(receipt)
    assert "--plugin-dir" not in command
    assert runtime.native_provider_authority_receipt(worker, run_id) is None


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_resumed_direct_worker_verifies_resume_and_fresh_native_authority_commands(
    tmp_path, monkeypatch, runtime_type
):
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic resumed request-pinned state.\n"
        "</viventium_feeling_state>"
    )
    runtime, worker = _direct_worker(tmp_path, runtime_type, capsule)
    worker["_active_run_id"] = "run-native-authority-resume"
    if runtime.runtime_name == "claude-code":
        monkeypatch.setattr(
            runtime, "_read_provider_session_key", lambda _worker: "session-claude"
        )
    else:
        monkeypatch.setattr(
            runtime, "_resumable_codex_session_key", lambda _worker: "session-codex"
        )

    command, _ = runtime._build_command(
        worker,
        "Resume one synthetic direct-worker task.",
        runtime._runtime_info(worker),
    )
    assert command[:2] == ["bash", "-c"]
    assert len(worker["_glasshive_native_authority_commands"]) == 2
    receipt = runtime._native_provider_authority_receipt(
        worker,
        command=command,
        run_id=worker["_active_run_id"],
        model=worker["model"],
    )
    assert receipt is not None
    assert receipt["model"] == worker["model"]

    tampered = [*command]
    tampered[-1] += "\n# synthetic tamper"
    with pytest.raises(
        RuntimeErrorBase,
        match="resume wrapper differs from its verified commands",
    ):
        runtime._native_provider_authority_receipt(
            worker,
            command=tampered,
            run_id=worker["_active_run_id"],
            model=worker["model"],
        )


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
@pytest.mark.parametrize("scope", ["all_agents", "conscious_agent", "unknown"])
def test_direct_worker_native_boundary_never_embodies_disabled_scopes(
    tmp_path, runtime_type, scope
):
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic state forbidden under this direct-worker scope.\n"
        "</viventium_feeling_state>"
    )
    runtime, worker = _direct_worker(
        tmp_path, runtime_type, capsule, enabled=False, scope=scope
    )
    command, _ = runtime._build_command(
        worker, "Return one synthetic result.", runtime._runtime_info(worker)
    )
    receipt = runtime._native_provider_authority_receipt(
        worker,
        command=command,
        run_id="run-scope-disabled",
        model=worker["model"],
    )

    assert receipt["feeling_capsule_count"] == 0
    for filename in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
        assert capsule not in (
            runtime._workspace_dir(worker["worker_id"]) / filename
        ).read_text()
    if runtime.runtime_name == "claude-code":
        authority = (
            runtime._home_dir(worker["worker_id"])
            / ".glasshive"
            / "developer-instructions.txt"
        ).read_text()
    else:
        authority = tomllib.loads(
            (
                runtime._home_dir(worker["worker_id"])
                / ".codex"
                / "config.toml"
            ).read_text()
        )["developer_instructions"]
    assert capsule not in authority


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_direct_worker_rejects_silent_native_runtime_substitution(
    tmp_path, runtime_type
):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, runtime_type, capsule)
    worker["runtime"] = (
        "codex-cli" if runtime.runtime_name == "claude-code" else "claude-code"
    )

    with pytest.raises(RuntimeErrorBase, match="configured native runtime"):
        runtime._build_command(
            worker, "Do the synthetic task.", runtime._runtime_info(worker)
        )


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_direct_worker_rejects_silent_native_model_substitution(
    tmp_path, runtime_type
):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, runtime_type, capsule)
    command, _ = runtime._build_command(
        worker, "Do the synthetic task.", runtime._runtime_info(worker)
    )
    model_flag = "--model" if runtime.runtime_name == "claude-code" else "-m"
    command[command.index(model_flag) + 1] = "unapproved-fallback-model"

    with pytest.raises(RuntimeErrorBase, match="configured native model"):
        runtime._native_provider_authority_receipt(
            worker,
            command=command,
            run_id="run-unapproved-model",
            model=worker["model"],
        )


def test_codex_direct_worker_rejects_bootstrap_model_shadowing(tmp_path):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, CodexCliRuntime, capsule)
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["env"] = {"WPR_MODEL_CODEX_CLI": "unapproved-fallback-model"}
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    command, _ = runtime._build_command(
        worker, "Do the synthetic task.", runtime._runtime_info(worker)
    )

    with pytest.raises(RuntimeErrorBase, match="configured native model"):
        runtime._native_provider_authority_receipt(
            worker,
            command=command,
            run_id="run-unapproved-bootstrap-model",
            model=worker["model"],
        )


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_claude_direct_worker_honors_exact_declared_effort(
    tmp_path, effort
):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, ClaudeCodeRuntime, capsule)
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["env"] = {"WPR_CLAUDE_CODE_EFFORT": effort}
    worker["bootstrap_bundle_json"] = json.dumps(bundle)

    command, _ = runtime._build_command(
        worker, "Do the synthetic task.", runtime._runtime_info(worker)
    )

    assert command[command.index("--effort") + 1] == effort


def test_claude_direct_worker_rejects_unsupported_declared_effort(tmp_path):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, ClaudeCodeRuntime, capsule)
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["env"] = {"WPR_CLAUDE_CODE_EFFORT": "unapproved"}
    worker["bootstrap_bundle_json"] = json.dumps(bundle)

    with pytest.raises(RuntimeErrorBase, match="configured Claude effort"):
        runtime._build_command(
            worker, "Do the synthetic task.", runtime._runtime_info(worker)
        )


def test_claude_and_codex_direct_workers_share_identical_native_authority_hash(
    tmp_path,
):
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic shared Main-and-workers request-pinned state.\n"
        "</viventium_feeling_state>"
    )
    receipts = []
    for runtime_type in (ClaudeCodeRuntime, CodexCliRuntime):
        runtime, worker = _direct_worker(
            tmp_path / runtime_type.__name__, runtime_type, capsule
        )
        command, _ = runtime._build_command(
            worker, "Create one synthetic delivery.", runtime._runtime_info(worker)
        )
        receipts.append(
            runtime._native_provider_authority_receipt(
                worker,
                command=command,
                run_id=f"run-shared-{runtime.runtime_name}",
                model=worker["model"],
            )
        )

    assert {receipt["runtime"] for receipt in receipts} == {
        "claude-code",
        "codex-cli",
    }
    assert len({receipt["authority_sha256"] for receipt in receipts}) == 1
    assert {receipt["feeling_capsule_count"] for receipt in receipts} == {1}


def test_main_and_two_provider_specific_workers_share_one_pinned_capsule(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic exact Main and sibling request-pinned state.\n"
        "</viventium_feeling_state>"
    )
    direct_receipts = []
    exact_authority = ""
    for runtime_type in (ClaudeCodeRuntime, CodexCliRuntime):
        runtime, worker = _direct_worker(
            tmp_path / runtime_type.__name__, runtime_type, capsule
        )
        command, _ = runtime._build_command(
            worker, "Create one synthetic delivery.", runtime._runtime_info(worker)
        )
        direct_receipts.append(
            runtime._native_provider_authority_receipt(
                worker,
                command=command,
                run_id=f"run-sibling-{runtime.runtime_name}",
                model=worker["model"],
            )
        )
        exact_authority = (
            runtime._workspace_dir(worker["worker_id"]) / "AGENTS.md"
        ).read_text()

    main_root = tmp_path / "main"
    main_root.mkdir()
    main_runtime, main_worker = _conversation_worker(main_root, exact_authority)
    main_worker["model"] = "claude-fable-5"
    main_bundle = json.loads(main_worker["bootstrap_bundle_json"])
    main_bundle["provider_model"] = main_worker["model"]
    main_worker["bootstrap_bundle_json"] = json.dumps(main_bundle)
    workspace = main_runtime._host_workspace_dir(main_worker)
    main_runtime._materialize_workspace(main_worker, workspace)
    main_command, _ = main_runtime._build_command(
        main_worker,
        "Stay responsive while the synthetic Workers run.",
        main_runtime._host_runtime_info(main_worker),
    )
    main_receipt = main_runtime._native_provider_authority_receipt(
        main_worker,
        command=main_command,
        run_id="run-pinned-main",
        model=main_worker["model"],
    )

    receipts = [main_receipt, *direct_receipts]
    assert len({receipt["authority_sha256"] for receipt in receipts}) == 1
    assert {receipt["feeling_capsule_count"] for receipt in receipts} == {1}
    assert exact_authority.count(capsule) == 1
    assert main_receipt["model"] == "claude-fable-5"
    assert {receipt["runtime"] for receipt in direct_receipts} == {
        "claude-code",
        "codex-cli",
    }


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_direct_worker_reuse_replaces_then_clears_provider_native_feeling(
    tmp_path, runtime_type
):
    first = "<viventium_feeling_state>first synthetic state</viventium_feeling_state>"
    second = "<viventium_feeling_state>second synthetic state</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, runtime_type, first)
    home = runtime._home_dir(worker["worker_id"])
    workspace = runtime._workspace_dir(worker["worker_id"])

    def current_authority() -> str:
        if runtime.runtime_name == "claude-code":
            return (home / ".glasshive/developer-instructions.txt").read_text()
        return tomllib.loads((home / ".codex/config.toml").read_text())[
            "developer_instructions"
        ]

    runtime._build_command(
        worker, "First synthetic task.", runtime._runtime_info(worker)
    )
    assert current_authority().count(first) == 1

    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["agents_md"] = f"Stable shared worker authority.\n\n{second}"
    bundle["viventium_feelings_projection"].update(
        {
            "snapshot_sha256": hashlib.sha256(second.encode()).hexdigest(),
            "enabled": True,
            "scope": "all_agents",
            "expected_capsule_count": 1,
        }
    )
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    refresh_project_runtime_files_for_worker(home, workspace, worker)
    runtime._build_command(
        worker, "Second synthetic task.", runtime._runtime_info(worker)
    )
    assert first not in current_authority()
    assert current_authority().count(second) == 1

    bundle["viventium_feelings_projection"].update(
        {
            "enabled": False,
            "scope": "conscious_agent",
            "expected_capsule_count": 0,
        }
    )
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    refresh_project_runtime_files_for_worker(home, workspace, worker)
    runtime._build_command(
        worker, "Scope-off synthetic task.", runtime._runtime_info(worker)
    )
    assert first not in current_authority()
    assert second not in current_authority()


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_clean_room_direct_worker_preserves_only_scoped_broker_and_native_authority(
    tmp_path, runtime_type, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    capsule = (
        "<viventium_feeling_state>clean-room synthetic state"
        "</viventium_feeling_state>"
    )
    runtime, worker = _direct_worker(
        tmp_path, runtime_type, capsule, clean_room=True
    )

    command, env = runtime._build_command(
        worker, "Create a synthetic HTML artifact.", runtime._runtime_info(worker)
    )
    receipt = runtime._native_provider_authority_receipt(
        worker,
        command=command,
        run_id="run-clean-room-native",
        model=worker["model"],
    )

    assert receipt["feeling_capsule_count"] == 1
    assert capsule not in json.dumps(command)
    assert capsule not in json.dumps(env)
    assert "--plugin-dir" not in command
    assert not (runtime._home_dir(worker["worker_id"]) / ".claude/plugins").exists()
    servers = json.loads(
        (runtime._workspace_dir(worker["worker_id"]) / ".mcp.json").read_text()
    )["mcpServers"]
    assert set(servers) == {"glasshive-user-capabilities"}
    assert "feelings" not in json.dumps(servers).lower()


def test_codex_direct_worker_rejects_ignored_native_developer_config(
    tmp_path, monkeypatch
):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, CodexCliRuntime, capsule)
    monkeypatch.setenv("WPR_CODEX_CLI_IGNORE_USER_CONFIG", "1")

    with pytest.raises(RuntimeErrorBase, match="ignores its native developer"):
        runtime._build_command(
            worker, "Do the synthetic task.", runtime._runtime_info(worker)
        )


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_direct_worker_rejects_changed_provider_authority_before_invocation(
    tmp_path, runtime_type
):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, runtime_type, capsule)
    command, _ = runtime._build_command(
        worker, "Do the synthetic task.", runtime._runtime_info(worker)
    )
    home = runtime._home_dir(worker["worker_id"])
    if runtime.runtime_name == "claude-code":
        (home / ".glasshive/developer-instructions.txt").write_text(
            "Changed synthetic authority."
        )
    else:
        (home / ".codex/config.toml").write_text(
            'developer_instructions = "Changed synthetic authority."\n'
        )

    with pytest.raises(RuntimeErrorBase, match="differs from its pinned snapshot"):
        runtime._native_provider_authority_receipt(
            worker,
            command=command,
            run_id="run-changed-authority",
            model=worker["model"],
        )


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
def test_direct_worker_rejects_receipts_forged_inside_worker_writable_mount(
    tmp_path, runtime_type
):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    runtime, worker = _direct_worker(tmp_path, runtime_type, capsule)
    run_id = f"run-forged-worker-{runtime.runtime_name}"
    worker_writable_receipt = (
        runtime._run_root(worker["worker_id"], run_id)
        / "native-provider-authority-receipt.json"
    )
    worker_writable_receipt.parent.mkdir(parents=True)
    worker_writable_receipt.write_text(
        json.dumps(
            {
                "protocol": "glasshive.native_provider_authority_receipt.v1",
                "run_id": run_id,
                "runtime": runtime.runtime_name,
                "model": worker["model"],
                "authority_sha256": "f" * 64,
                "authority_chars": 256,
                "feeling_capsule_count": 0,
                "placement": (
                    "append_system_prompt_file"
                    if runtime.runtime_name == "claude-code"
                    else "codex_developer_instructions"
                ),
                "materialized": True,
            }
        )
    )
    worker_writable_receipt.chmod(0o600)

    assert runtime.native_provider_authority_receipt(worker, run_id) is None


@pytest.mark.parametrize("runtime_type", [ClaudeCodeRuntime, CodexCliRuntime])
@pytest.mark.parametrize("native_event_kind", ["valid", "missing", "wrong_provider"])
def test_direct_worker_persists_receipt_only_after_native_provider_event(
    tmp_path, runtime_type, native_event_kind, monkeypatch
):
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic actual-invocation-only private state.\n"
        "</viventium_feeling_state>"
    )
    runtime, worker = _direct_worker(tmp_path, runtime_type, capsule)
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = f"run-native-event-{runtime.runtime_name}"

    class FakeSandbox:
        container_name = "wpr-synthetic-native-receipt"
        container_id = "a" * 64
        pid = 8234
        state = "running"

    monkeypatch.setattr(
        runtime.sandbox, "ensure_ready", lambda *_args, **_kwargs: FakeSandbox()
    )
    monkeypatch.setattr(
        runtime.sandbox, "inspect", lambda *_args, **_kwargs: FakeSandbox()
    )
    monkeypatch.setattr(
        runtime.sandbox, "list_screen_sessions", lambda *_args, **_kwargs: []
    )
    prepared_paths: list[list[str]] = []
    monkeypatch.setattr(
        runtime.sandbox,
        "ensure_container_writable_paths",
        lambda *_args, **_kwargs: prepared_paths.append(list(_args[2])),
    )
    monkeypatch.setattr(
        runtime.sandbox, "screen_session_pid", lambda *_args, **_kwargs: 9245
    )

    def fake_provider_invocation(worker_id, *_args, **_kwargs):
        assert runtime.native_provider_authority_receipt(worker, run_id) is None
        run_root = runtime._run_root(worker_id, run_id)
        if native_event_kind == "missing":
            event = {"type": "turn.completed"}
        elif (runtime.runtime_name == "claude-code") != (
            native_event_kind == "wrong_provider"
        ):
            event = {
                "type": "result",
                "session_id": "synthetic-claude-native-session",
                "result": "FINAL REPORT:\nSynthetic native result.",
            }
        else:
            event = {
                "type": "thread.started",
                "thread_id": "synthetic-codex-native-session",
            }
        (run_root / "stdout.log").write_text(json.dumps(event) + "\n")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(
            ["screen"], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(
        runtime.sandbox, "start_screen_session", fake_provider_invocation
    )
    monkeypatch.setattr(
        runtime,
        "_parse_output",
        lambda *_args, **_kwargs: (None, "FINAL REPORT:\nSynthetic native result."),
    )

    if native_event_kind != "valid":
        with pytest.raises(RuntimeErrorBase, match="no exact provider-native"):
            runtime.run_task(
                worker,
                "Create the synthetic requested result.",
                run_id=run_id,
            )
        assert runtime.native_provider_authority_receipt(worker, run_id) is None
        assert runtime._read_session_key(worker["worker_id"]) not in {
            "synthetic-claude-native-session",
            "synthetic-codex-native-session",
        }
        return

    runtime.run_task(worker, "Create the synthetic requested result.", run_id=run_id)

    expected_authority_root = (
        f"{runtime.sandbox.home_mount}/.glasshive"
        if runtime.runtime_name == "claude-code"
        else f"{runtime.sandbox.home_mount}/.codex"
    )
    assert expected_authority_root in prepared_paths[0]
    receipt = runtime.native_provider_authority_receipt(worker, run_id)
    assert receipt is not None
    assert receipt["runtime"] == runtime.runtime_name
    assert receipt["run_id"] == run_id
    assert receipt["feeling_capsule_count"] == 1
    receipt_path = runtime._native_provider_authority_receipt_path(
        worker["worker_id"], run_id
    )
    assert not receipt_path.is_relative_to(runtime._home_dir(worker["worker_id"]))
    assert not receipt_path.is_relative_to(
        runtime._workspace_dir(worker["worker_id"])
    )
    assert stat.S_IMODE(receipt_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert capsule not in receipt_path.read_text()
    assert capsule not in (runtime._run_root(worker["worker_id"], run_id) / "run.sh").read_text()
    restarted = runtime_type(base_dir=str(tmp_path / "private-state"))
    assert restarted.native_provider_authority_receipt(worker, run_id) == receipt


def test_same_durable_run_retries_isolate_native_receipts_and_terminal_files_by_attempt(
    tmp_path, monkeypatch
):
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic retry-isolated private state.\n"
        "</viventium_feeling_state>"
    )
    runtime, worker = _direct_worker(tmp_path, ClaudeCodeRuntime, capsule)
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = "run-native-retry-same-durable-run"
    attempt_ids = ("att_synthetic_first", "att_synthetic_second")
    launches: list[str] = []

    class FakeSandbox:
        container_name = "wpr-synthetic-native-retry"
        container_id = "b" * 64
        pid = 8234
        state = "running"

    monkeypatch.setattr(
        runtime.sandbox, "ensure_ready", lambda *_args, **_kwargs: FakeSandbox()
    )
    monkeypatch.setattr(
        runtime.sandbox, "inspect", lambda *_args, **_kwargs: FakeSandbox()
    )
    monkeypatch.setattr(
        runtime.sandbox, "list_screen_sessions", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        runtime.sandbox, "ensure_container_writable_paths", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        runtime.sandbox, "screen_session_pid", lambda *_args, **_kwargs: 9245
    )
    monkeypatch.setattr(
        runtime.sandbox, "stop_screen_session", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        runtime.sandbox, "terminate_run_processes", lambda *_args, **_kwargs: None
    )

    def fake_provider_invocation(worker_id, *_args, worker=None, **_kwargs):
        attempt_id = str((worker or {}).get("_run_attempt_id") or "")
        run_root = runtime._attempt_run_root(worker_id, run_id, attempt_id)
        launches.append(attempt_id)
        event = {
            "type": "result",
            "session_id": f"synthetic-session-{attempt_id}",
            "result": f"FINAL REPORT:\nSynthetic result for {attempt_id}.",
        }
        (run_root / "stdout.log").write_text(json.dumps(event) + "\n")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(
            ["screen"], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(
        runtime.sandbox, "start_screen_session", fake_provider_invocation
    )
    monkeypatch.setattr(
        runtime,
        "_parse_output",
        lambda worker, *_args, **_kwargs: (
            None,
            f"FINAL REPORT:\nSynthetic result for {worker['_run_attempt_id']}.",
        ),
    )

    first_worker = {**worker, "_run_attempt_id": attempt_ids[0]}
    runtime.run_task(first_worker, "Create the first synthetic result.", run_id=run_id)
    first_root = runtime._attempt_run_root(
        worker["worker_id"], run_id, attempt_ids[0]
    )
    (first_root / "stderr.log").write_text("synthetic stale first-attempt error")
    (first_root / "exit_code").write_text("130")

    second_worker = {**worker, "_run_attempt_id": attempt_ids[1]}
    assert runtime.collect_completed_run(second_worker, run_id=run_id) is None
    result = runtime.run_task(
        second_worker, "Retry the same durable synthetic work.", run_id=run_id
    )

    assert launches == list(attempt_ids)
    assert "second" in result
    second_root = runtime._attempt_run_root(
        worker["worker_id"], run_id, attempt_ids[1]
    )
    assert first_root != second_root
    assert (first_root / "exit_code").read_text() == "130"
    assert (second_root / "exit_code").read_text() == "0"
    for attempt_id, run_worker in zip(attempt_ids, (first_worker, second_worker)):
        receipt = runtime.native_provider_authority_receipt(run_worker, run_id)
        assert receipt is not None
        assert receipt["attempt_id"] == attempt_id
        assert runtime._native_provider_authority_receipt_path(
            worker["worker_id"], run_id, attempt_id=attempt_id
        ).exists()


def test_claude_conversation_materializes_current_authority_as_native_system_prompt_file(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    capsule = (
        "Stable Viventium authority.\n\n"
        "<viventium_feeling_state>\n"
        "Synthetic bright and playful private causal state.\n"
        "</viventium_feeling_state>"
    )
    runtime, worker = _conversation_worker(tmp_path, capsule)
    workspace = runtime._host_workspace_dir(worker)

    runtime._materialize_workspace(worker, workspace)
    command, _ = runtime._build_command(
        worker,
        "Give one synthetic visible answer.",
        runtime._host_runtime_info(worker),
    )

    prompt_path = runtime._state_dir(worker["worker_id"]) / "developer-instructions.txt"
    assert prompt_path.read_text() == capsule
    assert command[command.index("--append-system-prompt-file") + 1] == str(prompt_path)
    assert capsule not in command


def test_codex_conversation_receipt_reads_exact_native_developer_authority(
    tmp_path, monkeypatch
):
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text("")
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic calm private causal state.\n"
        "</viventium_feeling_state>"
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-private-state"))
    life = tmp_path / "Codex-Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_native_authority_receipt",
        "trusted_run_lane": "conversation",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "developer_instructions": capsule,
            }
        ),
    }
    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)
    command, _ = runtime._build_command(
        worker,
        "Answer one synthetic prompt.",
        runtime._host_runtime_info(worker),
    )

    receipt = runtime._native_provider_authority_receipt(
        worker,
        command=command,
        run_id="run-synthetic-codex-receipt",
        model="gpt-5.6-sol",
    )

    assert receipt["placement"] == "codex_developer_instructions"
    assert receipt["authority_sha256"] == hashlib.sha256(capsule.encode()).hexdigest()
    assert receipt["feeling_capsule_count"] == 1
    assert receipt["materialized"] is True


def test_claude_native_authority_receipt_is_bound_to_spawned_run_and_exact_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic guarded private causal state.\n"
        "</viventium_feeling_state>"
    )
    runtime, worker = _conversation_worker(tmp_path, capsule)
    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)
    command, _ = runtime._build_command(
        worker,
        "Answer the synthetic feeling question.",
        runtime._host_runtime_info(worker),
    )

    receipt = runtime._native_provider_authority_receipt(
        worker,
        command=command,
        run_id="run-synthetic-receipt",
        model="opus",
    )

    assert receipt == {
        "protocol": "glasshive.native_provider_authority_receipt.v1",
        "run_id": "run-synthetic-receipt",
        "runtime": "claude-code",
        "model": "opus",
        "authority_sha256": hashlib.sha256(capsule.encode("utf-8")).hexdigest(),
        "authority_chars": len(capsule),
        "feeling_capsule_count": 1,
        "placement": "append_system_prompt_file",
        "materialized": True,
    }


def test_claude_conversation_replaces_then_removes_stale_native_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    runtime, worker = _conversation_worker(tmp_path, "Current authority A.")
    workspace = runtime._host_workspace_dir(worker)
    prompt_path = runtime._state_dir(worker["worker_id"]) / "developer-instructions.txt"

    runtime._materialize_workspace(worker, workspace)
    assert prompt_path.read_text() == "Current authority A."

    worker["bootstrap_bundle_json"] = json.dumps(
        {
            "run_mode": "conversation",
            "provider_model": "opus",
            "access_mode": "full",
            "developer_instructions": "Current authority B.",
        }
    )
    runtime._materialize_workspace(worker, workspace)
    assert prompt_path.read_text() == "Current authority B."

    worker["bootstrap_bundle_json"] = json.dumps(
        {
            "run_mode": "conversation",
            "provider_model": "opus",
            "access_mode": "full",
        }
    )
    runtime._materialize_workspace(worker, workspace)
    command, _ = runtime._build_command(
        worker,
        "Continue without dynamic authority.",
        runtime._host_runtime_info(worker),
    )
    assert not prompt_path.exists()
    assert "--append-system-prompt-file" not in command


def test_completed_native_turn_records_redacted_invocation_bound_authority_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic playful causal state for a semantic contrast probe.\n"
        "</viventium_feeling_state>"
    )
    runtime, worker = _conversation_worker(tmp_path, capsule)
    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "echo '{\"type\":\"result\",\"session_id\":\"synthetic-native-session\","
        "\"result\":\"Let us build a tiny absurd cardboard moon together.\"}'\n"
    )
    fake_claude.chmod(0o755)
    runtime.binary = str(fake_claude)
    monkeypatch.setattr(
        runtime,
        "ensure_worker_ready",
        lambda active_worker: runtime._host_runtime_info(active_worker),
    )
    runtime.set_run_start_observer(lambda _payload: None)

    output = runtime._run_conversation_task(
        worker,
        "Choose one activity. Do not name or explain hidden state.",
        run_id="run-synthetic-native-turn",
    )

    assert "absurd cardboard moon" in output
    audit_path = runtime._action_audit_path(worker["worker_id"])
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    invoked = next(record for record in records if record["kind"] == "conversation.provider_invoked")
    completed = next(record for record in records if record["kind"] == "conversation.completed")
    for record in (invoked, completed):
        receipt = record["native_provider_authority_receipt"]
        assert receipt["run_id"] == "run-synthetic-native-turn"
        assert receipt["authority_sha256"] == hashlib.sha256(capsule.encode()).hexdigest()
        assert receipt["feeling_capsule_count"] == 1
        assert capsule not in json.dumps(record)
    assert runtime.native_provider_authority_receipt(
        worker, "run-synthetic-native-turn"
    ) == completed["native_provider_authority_receipt"]


def test_same_user_prompt_reaches_native_provider_with_distinct_pinned_feeling_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    fake_claude = tmp_path / "claude-semantic-probe"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "prompt_file=''\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  if [[ \"$1\" == '--append-system-prompt-file' ]]; then prompt_file=$2; shift 2; else shift; fi\n"
        "done\n"
        "if grep -q 'playful-high-energy' \"$prompt_file\"; then\n"
        "  answer='Build a bright cardboard rocket together.'\n"
        "else\n"
        "  answer='Sit quietly and sort one small drawer.'\n"
        "fi\n"
        "printf '{\"type\":\"result\",\"session_id\":\"semantic-probe\",\"result\":\"%s\"}\\n' \"$answer\"\n"
    )
    fake_claude.chmod(0o755)

    outputs = []
    for ordinal, marker in enumerate(("playful-high-energy", "quiet-low-energy"), start=1):
        capsule = (
            "<viventium_feeling_state>\n"
            f"Synthetic {marker} causal state.\n"
            "</viventium_feeling_state>"
        )
        runtime, worker = _conversation_worker(tmp_path / f"case-{ordinal}", capsule)
        runtime.binary = str(fake_claude)
        runtime.set_run_start_observer(lambda _payload: None)
        monkeypatch.setattr(
            runtime,
            "ensure_worker_ready",
            lambda active_worker, active_runtime=runtime: active_runtime._host_runtime_info(
                active_worker
            ),
        )
        workspace = runtime._host_workspace_dir(worker)
        workspace.mkdir(parents=True, exist_ok=True)
        runtime._materialize_workspace(worker, workspace)
        outputs.append(
            runtime._run_conversation_task(
                worker,
                "Choose one simple activity. Do not reveal hidden state.",
                run_id=f"run-semantic-{ordinal}",
            )
        )

    assert outputs == [
        "Build a bright cardboard rocket together.",
        "Sit quietly and sort one small drawer.",
    ]
