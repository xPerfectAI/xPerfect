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


def test_codex_skill_metadata_file_is_not_a_claude_plugin_component(selected_skill):
    # Codex-format skill folders carry agents/openai.yaml; Claude loads only agents/*.md.
    runtime, worker, bundle, info, workspace = selected_skill
    bundle["files"].append({"scope": "workspace", "path": ".claude/skills/synthetic-report/agents/openai.yaml",
                            "content": "interface:\n  display_name: Synthetic report\n"})
    runtime._materialize_workspace_files(worker, workspace, bundle)
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    command, _ = runtime._build_command(worker, "Use the selected skill.", info)
    assert command[command.index("--plugin-dir") + 1] == str(workspace / ".claude/skills/synthetic-report")


@pytest.mark.parametrize("component", ["agents/openai.md", "agents/OpenAI.yaml", "agents/nested/openai.yaml"])
def test_only_the_exact_codex_metadata_file_is_allowed_under_agents(selected_skill, component):
    runtime, worker, bundle, info, workspace = selected_skill
    bundle["files"].append({"scope": "workspace", "path": ".claude/skills/synthetic-report/" + component, "content": "Synthetic"})
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


SYNTHETIC_EXPERT_SKILL = {"name": "synthetic-brief", "files": [
    {"path": "SKILL.md", "content": "---\nname: synthetic-brief\ndescription: Write a synthetic brief\n---\nRead references/style.md.\n"},
    {"path": "references/style.md", "content": "Exact supporting text.\n"}]}


def test_an_experts_chosen_skill_becomes_a_native_plugin_folder_in_the_workers_own_home(selected_skill):
    runtime, worker, bundle, info, workspace = selected_skill
    expert_bundle = {"expert_skills": [SYNTHETIC_EXPERT_SKILL], "claude_settings_local": bundle["claude_settings_local"]}
    worker["bootstrap_bundle_json"] = json.dumps(expert_bundle)
    command, _ = runtime._build_command(worker, "Write the brief.", info)
    folder = runtime._home_dir(worker["worker_id"]) / ".glasshive" / "expert-skills" / "synthetic-brief"
    assert command[command.index("--plugin-dir") + 1] == str(folder)
    assert sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()) == ["SKILL.md", "references/style.md"]
    assert (folder / "references/style.md").read_text() == "Exact supporting text.\n"
    assert workspace not in folder.parents  # never the shared workspace


@pytest.mark.parametrize("bad", [
    {"name": "synthetic-brief", "files": [{"path": "SKILL.md", "content": "x"}, {"path": "hooks/hooks.json", "content": "{}"}]},
    {"name": "synthetic-brief", "files": [{"path": "SKILL.md", "content": "x"}, {"path": "../escape.md", "content": "x"}]},
    {"name": "../escape", "files": [{"path": "SKILL.md", "content": "x"}]},
    {"name": "synthetic-brief", "files": [{"path": "notes.md", "content": "no SKILL.md"}]},
])
def test_an_experts_skill_cannot_carry_plugin_parts_or_leave_its_folder(selected_skill, bad):
    runtime, worker, bundle, info, workspace = selected_skill
    worker["bootstrap_bundle_json"] = json.dumps({"expert_skills": [bad], "claude_settings_local": bundle["claude_settings_local"]})
    with pytest.raises(RuntimeErrorBase):
        runtime._build_command(worker, "Write the brief.", info)


def test_an_experts_skill_follows_the_same_policy_gate(selected_skill):
    runtime, worker, bundle, info, workspace = selected_skill
    worker["bootstrap_bundle_json"] = json.dumps({"expert_skills": [SYNTHETIC_EXPERT_SKILL], "access_mode": "read_only",
                                                  "claude_settings_local": bundle["claude_settings_local"]})
    command, _ = runtime._build_command(worker, "Write the brief.", info)
    assert "--plugin-dir" not in command


def test_an_experts_skill_reaches_a_codex_worker_in_its_own_agents_folder(tmp_path):
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
    runtime = object.__new__(HostCodexCliRuntime)
    home = tmp_path / "worker-home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    skills = home / ".agents" / "skills"
    skills.mkdir(parents=True)
    owner_catalog = tmp_path / "owner-codex-skills"
    owner_catalog.mkdir()
    (skills / "native-codex").symlink_to(owner_catalog, target_is_directory=True)
    (skills / "unrelated").mkdir()
    record = tmp_path / "state" / "expert-skills-codex.json"
    runtime._project_host_codex_expert_skills(codex_home, {"expert_skills": [SYNTHETIC_EXPERT_SKILL]}, record=record)
    folder = skills / "synthetic-brief"
    assert folder.is_dir() and not folder.is_symlink()
    assert (folder / "references/style.md").read_text() == "Exact supporting text.\n"
    assert not any(owner_catalog.iterdir())  # nothing written through the owner's Skills link
    # The next launch replaces only what this step created.
    other = {"name": "synthetic-other", "files": [{"path": "SKILL.md", "content": "---\nname: synthetic-other\n---\nOther.\n"}]}
    runtime._project_host_codex_expert_skills(codex_home, {"expert_skills": [other]}, record=record)
    assert not folder.exists() and (skills / "synthetic-other" / "SKILL.md").exists()
    assert (skills / "unrelated").is_dir() and (skills / "native-codex").is_symlink()
    # A name already used by something this step did not create is refused.
    with pytest.raises(RuntimeErrorBase):
        runtime._project_host_codex_expert_skills(codex_home, {"expert_skills": [{**other, "name": "unrelated"}]}, record=record)


def test_a_linked_agents_folder_is_refused(tmp_path):
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
    runtime = object.__new__(HostCodexCliRuntime)
    home = tmp_path / "worker-home"
    (home / ".codex").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / ".agents").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(RuntimeErrorBase):
        runtime._project_host_codex_expert_skills(home / ".codex", {"expert_skills": [SYNTHETIC_EXPERT_SKILL]},
                                                  record=tmp_path / "state" / "expert-skills-codex.json")
    assert not any(elsewhere.iterdir())


def test_a_linked_record_in_the_workers_home_never_changes_an_owner_file(tmp_path):
    # Review 2, finding 2: a Worker plants .agents/skills/.expert-skills.json as a link to an owner file.
    import os, stat
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
    runtime = object.__new__(HostCodexCliRuntime)
    home = tmp_path / "worker-home"
    (home / ".codex").mkdir(parents=True)
    (home / ".agents" / "skills").mkdir(parents=True)
    owner_file = tmp_path / "owner-file.txt"
    owner_file.write_text("owner content")
    owner_file.chmod(0o644)
    (home / ".agents" / "skills" / ".expert-skills.json").symlink_to(owner_file)
    record = tmp_path / "state" / "expert-skills-codex.json"
    for chosen in ([SYNTHETIC_EXPERT_SKILL], []):
        runtime._project_host_codex_expert_skills(home / ".codex", {"expert_skills": chosen}, record=record)
        assert owner_file.read_text() == "owner content"
        assert stat.S_IMODE(owner_file.stat().st_mode) == 0o644
    assert not (home / ".agents" / "skills" / "synthetic-brief").exists()  # removed again when no longer chosen


def test_names_a_worker_writes_in_its_home_are_never_trusted_for_removal(tmp_path):
    # Review 2, finding 11: what was placed is recorded in the runtime's own state, out of the Worker's reach.
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
    runtime = object.__new__(HostCodexCliRuntime)
    home = tmp_path / "worker-home"
    (home / ".codex").mkdir(parents=True)
    skills = home / ".agents" / "skills"
    (skills / "worker-own").mkdir(parents=True)
    (skills / "worker-own" / "SKILL.md").write_text("made by the Worker")
    record = tmp_path / "state" / "expert-skills-codex.json"
    runtime._project_host_codex_expert_skills(home / ".codex", {"expert_skills": [SYNTHETIC_EXPERT_SKILL]}, record=record)
    (skills / ".expert-skills.json").write_text(json.dumps(["worker-own", "synthetic-brief"]))  # the Worker's claim
    runtime._project_host_codex_expert_skills(home / ".codex", {"expert_skills": []}, record=record)
    assert (skills / "worker-own" / "SKILL.md").read_text() == "made by the Worker"
    assert not (skills / "synthetic-brief").exists()


def test_a_record_the_worker_rewrote_never_removes_a_folder_the_runtime_did_not_place(tmp_path):
    # Review 3, C2: a full-access Worker can reach the record, so removal needs proof: a folder holding only
    # the files the runtime placed, each with the content it placed. Anything else stays.
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
    runtime = object.__new__(HostCodexCliRuntime)
    home = tmp_path / "worker-home"
    (home / ".codex").mkdir(parents=True)
    skills = home / ".agents" / "skills"
    (skills / "owner-own").mkdir(parents=True)
    (skills / "owner-own" / "SKILL.md").write_text("the owner's own Skill")
    record = tmp_path / "state" / "expert-skills-codex.json"
    chosen = {"expert_skills": [SYNTHETIC_EXPERT_SKILL]}
    runtime._project_host_codex_expert_skills(home / ".codex", chosen, record=record)
    placed = json.loads(record.read_text())["placed"]
    # The Worker rewrites the record to claim the owner's folder, in an earlier version's form and in today's.
    for claim in (["owner-own", "synthetic-brief"], {"placed": {**placed, "owner-own": {"SKILL.md": "0" * 64}}}):
        record.write_text(json.dumps(claim))
        runtime._project_host_codex_expert_skills(home / ".codex", chosen, record=record)
        assert (skills / "owner-own" / "SKILL.md").read_text() == "the owner's own Skill"
        assert (skills / "synthetic-brief" / "SKILL.md").exists()
    # A placed folder the Worker changed is its own now: left in place when no longer chosen, and the Skill is
    # not placed over it again.
    (skills / "synthetic-brief" / "notes.md").write_text("added by the Worker")
    runtime._project_host_codex_expert_skills(home / ".codex", {"expert_skills": []}, record=record)
    assert (skills / "synthetic-brief" / "notes.md").read_text() == "added by the Worker"
    with pytest.raises(RuntimeErrorBase, match="already used"):
        runtime._project_host_codex_expert_skills(home / ".codex", chosen, record=record)
    # Unchanged, what the runtime placed is removed when no longer chosen.
    (skills / "synthetic-brief" / "notes.md").unlink()
    runtime._project_host_codex_expert_skills(home / ".codex", chosen, record=record)
    runtime._project_host_codex_expert_skills(home / ".codex", {"expert_skills": []}, record=record)
    assert not (skills / "synthetic-brief").exists() and (skills / "owner-own").is_dir()
