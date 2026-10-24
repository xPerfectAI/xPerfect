"""Reviewed text skills use native session selectors without ambient settings."""
import json
import os
from pathlib import Path

import pytest

from workers_projects_runtime.openclaw_runtime import RuntimeDependencyMissingError, RuntimeErrorBase
from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime


@pytest.fixture
def selected_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "default")
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "inherit")
    monkeypatch.delenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", raising=False)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime, "_help_text", lambda: "--plugin-dir --tools --permission-mode dontAsk --no-chrome --strict-mcp-config --setting-sources --json-schema --safe-mode --disable-slash-commands --no-session-persistence")
    monkeypatch.setattr(runtime, "_inject_private_subscription_auth", lambda _env: "owner_managed")
    monkeypatch.setattr(runtime, "_host_env", lambda _worker: {
        "HOME": str(tmp_path / "owner"), "CLAUDE_CONFIG_DIR": str(tmp_path / "isolated/.claude"),
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "synthetic-unselected",
    })
    worker = {"worker_id": "wrk_synthetic_skill", "profile": "claude-code", "execution_mode": "host", "model": "opus"}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    files = [{"scope": "workspace", "path": ".claude/skills/synthetic-report/SKILL.md", "content": "---\nname: synthetic-report\ndescription: Report synthetic results\n---\nRead references/example.md.\n"},
             {"scope": "workspace", "path": ".claude/skills/synthetic-report/references/example.md", "content": "Exact supporting text.\r\n"}]
    bundle = {"files": files, "claude_settings_local": {"disableAllHooks": True, "permissions": {"allow": ["Read"]}}}
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    runtime._materialize_workspace_files(worker, workspace, bundle)
    info = runtime._host_runtime_info(worker)
    info.workspace_dir = str(workspace)
    return runtime, worker, bundle, info, workspace


@pytest.mark.parametrize("lane", ["mission", "conversation"])
@pytest.mark.parametrize("auth", ["owner", "api", "bound"])
def test_full_worker_selects_exact_skill_and_preserves_authority(selected_skill, monkeypatch, lane, auth):
    runtime, worker, bundle, info, workspace = selected_skill
    if lane == "conversation":
        worker["trusted_run_lane"] = "conversation"
        bundle.update(run_mode="conversation", access_mode="full")
    if auth == "api":
        worker["_glasshive_provider_api_key_file"] = "/synthetic/key-reference"
    if auth == "bound":
        bundle["provider_account"] = {"policy": "personal_required", "account_id": "acct_synthetic"}
        worker.update(_glasshive_provider_account_bound=True,
                      _glasshive_provider_account_env={"CLAUDE_CONFIG_DIR": "/synthetic/bound-account", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/synthetic/bound-account"})
        monkeypatch.setattr(runtime, "_inject_private_subscription_auth", lambda _env: pytest.fail("must retain bound account"))
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    command, environment = runtime._build_command(worker, "Use the selected reporting skill.", info)
    assert command[command.index("--plugin-dir") + 1] == str(workspace / ".claude/skills/synthetic-report")
    assert command.count("--plugin-dir") == 1
    assert all(command[index + 1] == "" for index, value in enumerate(command) if value == "--setting-sources")
    assert "--strict-mcp-config" in command
    assert command[command.index("--model") + 1] == "opus"
    assert json.loads(command[command.index("--settings") + 1]) == bundle["claude_settings_local"]
    assert environment["CLAUDE_CONFIG_DIR"] == ("/synthetic/bound-account" if auth == "bound" else str(workspace.parent / "isolated/.claude"))
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in environment


@pytest.mark.parametrize("policy", ["restricted", "read_only", "workspace", "web_disabled"])
def test_restricted_paths_never_select_skill_directories(selected_skill, monkeypatch, policy):
    runtime, worker, bundle, info, workspace = selected_skill
    worker["trusted_run_lane"] = "conversation"
    bundle["run_mode"] = "conversation"
    if policy == "restricted":
        bundle["provider_capabilities"] = {"native_tools": False}
    elif policy == "web_disabled":
        monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    else:
        bundle["access_mode"] = policy
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    monkeypatch.setattr(runtime, "_declared_skill_plugin_directories", lambda *_: pytest.fail("restricted path inspected skill files"))
    command, _ = runtime._build_command(worker, "Use only permitted capabilities.", info)
    assert "--plugin-dir" not in command
    assert "--no-chrome" in command
    assert "--strict-mcp-config" in command


def test_unselected_and_source_only_skills_do_not_add_selectors(selected_skill):
    runtime, worker, bundle, info, workspace = selected_skill
    for files in [[], [{"path": ".claude/skills/synthetic-report/SKILL.md", "source_path": "/synthetic/source"}]]:
        worker["bootstrap_bundle_json"] = json.dumps({"files": files, "library_selection": {"items": [{"kind": "skill", "state": "ready_for_next_start"}]}})
        command, _ = runtime._build_command(worker, "Read selected material.", info)
        assert "--plugin-dir" not in command


def test_missing_native_selector_fails_clearly(selected_skill, monkeypatch):
    runtime, worker, _, info, _ = selected_skill
    monkeypatch.setattr(runtime, "_help_text", lambda: "--setting-sources")
    with pytest.raises(RuntimeDependencyMissingError, match="native skill directory selector"):
        runtime._build_command(worker, "Use the selected skill.", info)


@pytest.mark.parametrize("component", [".claude-plugin/plugin.json", "hooks/hooks.json", ".mcp.json", ".lsp.json", "agents/reviewer.md", "commands/extra.md", "skills/other/SKILL.md", "settings.json", "bin/tool", "workflows/task.js", "output-styles/style.md", "themes/theme.json", "monitors/monitors.json", "Hooks/hooks.json", ".MCP.JSON", ".Claude-Plugin/plugin.json"])
def test_text_skill_cannot_add_native_plugin_components(selected_skill, component):
    runtime, worker, bundle, info, workspace = selected_skill
    bundle["files"].append({"scope": "workspace", "path": ".claude/skills/synthetic-report/" + component, "content": "Synthetic component"})
    runtime._materialize_workspace_files(worker, workspace, bundle)
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    with pytest.raises(RuntimeErrorBase, match="native plugin components"):
        runtime._build_command(worker, "Use the selected skill.", info)


@pytest.mark.parametrize("change", ["changed", "missing", "extra_file", "extra_directory", "file_link", "directory_link", "hard_link", "root_link"])
def test_skill_selection_rejects_changed_or_unowned_package(selected_skill, change):
    runtime, worker, _, info, workspace = selected_skill
    root = workspace / ".claude/skills/synthetic-report"
    reference = root / "references/example.md"
    if change == "changed":
        reference.write_text("Changed text.")
    elif change == "missing":
        reference.unlink()
    elif change == "extra_file":
        (root / "unexpected.md").write_text("Not reviewed.")
    elif change == "extra_directory":
        (root / "unexpected").mkdir()
    elif change == "file_link":
        target = workspace / "linked-reference.md"
        reference.rename(target)
        reference.symlink_to(target)
    elif change == "directory_link":
        target = workspace / "linked-references"
        reference.parent.rename(target)
        reference.parent.symlink_to(target, target_is_directory=True)
    elif change == "hard_link":
        os.link(reference, workspace / "linked-reference.md")
    else:
        target = workspace / "linked-skill"
        root.rename(target)
        root.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeErrorBase, match="unavailable or changed"):
        runtime._build_command(worker, "Use the selected skill.", info)


def test_nested_reference_names_do_not_become_plugin_components(selected_skill):
    runtime, worker, bundle, info, workspace = selected_skill
    bundle["files"].append({"scope": "workspace", "path": ".claude/skills/synthetic-report/references/hooks/schema.md", "content": "A harmless reference."})
    runtime._materialize_workspace_files(worker, workspace, bundle)
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    command, _ = runtime._build_command(worker, "Use the selected skill.", info)
    assert "--plugin-dir" in command


def test_unselected_worker_keeps_existing_setting_sources(selected_skill, monkeypatch):
    runtime, worker, _, info, _ = selected_skill
    worker["bootstrap_bundle_json"] = "{}"
    monkeypatch.setattr(runtime, "_inject_private_subscription_auth", lambda _env: "projected_access_token")
    command, _ = runtime._build_command(worker, "Use existing capabilities.", info)
    assert "--plugin-dir" not in command
    assert "--setting-sources" not in command
