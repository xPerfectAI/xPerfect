from __future__ import annotations

import hashlib
import json
import os

import pytest
import workers_projects_runtime.bootstrap as bootstrap_module

from workers_projects_runtime.bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    apply_bootstrap,
    bootstrap_env_for,
    refresh_project_runtime_files_for_worker,
    refresh_runtime_env_for_worker,
    sign_bootstrap_source_path,
)
from workers_projects_runtime.codex_plugins import (
    OPENAI_PLUGIN_MARKETPLACE_COMMIT,
    OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH,
)

import hashlib

from pathlib import Path

import workers_projects_runtime.bootstrap as bootstrap_module

from workers_projects_runtime.upload_projection import project_upload_files


def _clear_ambient_provider_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
        "PORTKEY_PROVIDER",
        "PORTKEY_VIRTUAL_KEY",
        "PORTKEY_CONFIG",
    ):
            monkeypatch.delenv(key, raising=False)


def test_bootstrap_materializes_canonical_worker_operating_contract(tmp_path):
    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker={"bootstrap_bundle_json": json.dumps({})},
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    agents_text = (tmp_path / "workspace" / "AGENTS.md").read_text()
    assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in agents_text
    assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in agents_text
    assert "FINAL REPORT:" in agents_text
    assert "polished ordinary end-user artifact" in agents_text
    assert "source/date/auth/scope constraints" in agents_text
    assert "do not use that item to support facts, scoring, or deliverables" in agents_text
    assert "source publication/evidence dates distinct from retrieval/access timestamps" in agents_text
    assert "access date must not widen or replace a user-limited source window" in agents_text
    assert "rejected or out-of-scope evidence" in agents_text
    assert "read it before planning, delegation, source collection, and final delivery" in agents_text
    assert "carry the user's constraints forward literally and exactly" in agents_text
    assert "correct that file before continuing" in agents_text
    assert "prioritize a usable core result before optional expansion" in agents_text
    assert "Do not force a download" in agents_text
    assert "Native capability discovery" in agents_text
    assert "Inspect what is actually available" in agents_text
    assert "use available research, browser, spreadsheet, PDF, document, deck, notebook, rendering, or verification tools" in agents_text
    assert "verify the package/tool is available" in agents_text
    assert "Do not overfit to examples" in agents_text


def test_docker_codex_bootstrap_exposes_the_official_native_plugin_catalog(tmp_path):
    home_dir = tmp_path / "home"

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker={
            "execution_mode": "docker",
            "_glasshive_provider_account_bound": True,
            "bootstrap_bundle_json": json.dumps({}),
        },
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    catalog = home_dir / ".codex" / ".tmp" / "plugins"
    revision = home_dir / ".codex" / ".tmp" / "plugins.sha"
    assert catalog.is_symlink()
    assert str(catalog.readlink()) == OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH
    assert revision.read_text() == f"{OPENAI_PLUGIN_MARKETPLACE_COMMIT}\n"


def test_codex_bootstrap_never_replaces_existing_native_plugin_catalog_state(tmp_path):
    home_dir = tmp_path / "home"
    catalog = home_dir / ".codex" / ".tmp" / "plugins"
    catalog.mkdir(parents=True)
    catalog.parent.chmod(0o750)
    marker = catalog / "must-stay"
    marker.write_text("user state")

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker={
            "execution_mode": "docker",
            "_glasshive_provider_account_bound": True,
            "bootstrap_bundle_json": json.dumps({}),
        },
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    assert marker.read_text() == "user state"
    assert (catalog.parent.stat().st_mode & 0o777) == 0o750


def test_enterprise_codex_bootstrap_seeds_catalog_only_for_a_personal_account(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    unbound_home = tmp_path / "unbound-home"
    bound_home = tmp_path / "bound-home"

    for home_dir, bound in ((unbound_home, False), (bound_home, True)):
        apply_bootstrap(
            home_dir=home_dir,
            workspace_dir=tmp_path / f"workspace-{bound}",
            runtime_name="codex-cli",
            worker={
                "execution_mode": "docker",
                "_glasshive_provider_account_bound": bound,
                "bootstrap_bundle_json": json.dumps({}),
            },
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )

    assert not os.path.lexists(unbound_home / ".codex" / ".tmp" / "plugins")
    assert (bound_home / ".codex" / ".tmp" / "plugins").is_symlink()


def test_bootstrap_materializes_one_proportional_verification_rule(tmp_path):
    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker={"bootstrap_bundle_json": json.dumps({})},
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    rule = bootstrap_module.GLASSHIVE_PROPORTIONAL_VERIFICATION_RULE
    assembled = bootstrap_module.GLASSHIVE_WORKER_PROJECT_CONTRACT
    agents_text = (tmp_path / "workspace" / "AGENTS.md").read_text()
    agents_mirror = (tmp_path / "workspace" / "agents.md").read_text()

    assert assembled.count(rule) == 1
    assert agents_text == agents_mirror
    assert agents_text.count(rule) == 1
    assert "HTML" not in rule
    for repeated_instruction in (
        "SELF-TEST AND VERIFY",
        "Debate with yourself",
        "Do not stop early",
        "LOOP UNTIL SUCCESS",
        "keep working or remediate",
        "Fix the layout",
        "Plan, execute, inspect, identify gaps/issues/misalignments",
    ):
        assert repeated_instruction not in agents_text

def test_enterprise_bootstrap_filters_worker_env_and_projects_provider_env(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_PROJECT_PROVIDER_ENV", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "process-openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example.com")

    worker = {
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "bundle-openai",
                    "ANTHROPIC_BASE_URL": "https://anthropic.enterprise.example.com",
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-broker-grant",
                    "PRIVATE_INTERNAL_TOKEN": "must-not-project",
                }
            }
        )
    }

    env = bootstrap_env_for(worker)

    assert env["OPENAI_API_KEY"] == "bundle-openai"
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == "public-safe-broker-grant"
    assert env["OPENAI_BASE_URL"] == "https://openai.example.com"
    assert "PRIVATE_INTERNAL_TOKEN" not in env


@pytest.mark.parametrize(
    ("profile", "present", "absent"),
    [
        ("codex-cli", {"OPENAI_API_KEY"}, {"ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID"}),
        ("claude-code", {"ANTHROPIC_API_KEY"}, {"OPENAI_API_KEY", "PORTKEY_API_KEY"}),
        ("openclaw-general", {"OPENAI_API_KEY"}, {"ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID"}),
    ],
)
def test_enterprise_deployment_provider_projection_is_profile_scoped(
    monkeypatch, profile, present, absent
):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_PROJECT_PROVIDER_ENV", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai")
    monkeypatch.setenv("PORTKEY_API_KEY", "synthetic-portkey")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-aws")
    env = bootstrap_env_for({"profile": profile})
    assert present.issubset(env)
    assert absent.isdisjoint(env)


def test_enterprise_compatible_provider_projection_follows_each_runtime_selector(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_PROJECT_PROVIDER_ENV", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai")
    monkeypatch.setenv("OPENAI_API_BASE", "https://openai.example.test/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "synthetic-portkey")
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://portkey.example.test/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://selected.example.test/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ENV_KEY", "PORTKEY_API_KEY")

    codex = bootstrap_env_for({"profile": "codex-cli"})
    openclaw = bootstrap_env_for({"profile": "openclaw-general"})

    assert codex["PORTKEY_API_KEY"] == "synthetic-portkey"
    assert "OPENAI_API_KEY" not in codex
    assert openclaw["OPENAI_API_KEY"] == "synthetic-openai"
    assert "PORTKEY_API_KEY" not in openclaw

    monkeypatch.setenv("WPR_OPENCLAW_BASE_URL", "https://selected.example.test/v1")
    monkeypatch.setenv("WPR_OPENCLAW_ENV_KEY", "PORTKEY_API_KEY")
    selected_openclaw = bootstrap_env_for({"profile": "openclaw-general"})
    assert selected_openclaw["PORTKEY_API_KEY"] == "synthetic-portkey"
    assert "OPENAI_API_KEY" not in selected_openclaw


def test_enterprise_worker_env_allowlist_rejects_user_provider_tokens(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_WORKER_ENV_ALLOWLIST", "GOOGLE_REFRESH_TOKEN")

    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "GOOGLE_REFRESH_TOKEN": "provider-token-must-not-project",
                }
            }
        )
    }

    with pytest.raises(RuntimeError, match="must not include user provider"):
        bootstrap_env_for(worker)


def test_local_bootstrap_env_filters_user_provider_tokens_without_blocking_provider_keys(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("GLASSHIVE_PROJECT_PROVIDER_ENV", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "process-openai")

    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "bundle-openai",
                    "PRIVATE_INTERNAL_TOKEN": "local-mode-keeps-existing-behavior",
                    "PATH": "/tmp/untrusted-path",
                    "GOOGLE_REFRESH_TOKEN": "must-not-project",
                    "GOOGLE_OAUTH_CLIENT_SECRET": "must-not-project",
                    "MS365_ACCESS_TOKEN": "must-not-project",
                }
            }
        )
    }

    env = bootstrap_env_for(worker)

    assert env["OPENAI_API_KEY"] == "bundle-openai"
    assert env["PRIVATE_INTERNAL_TOKEN"] == "local-mode-keeps-existing-behavior"
    assert "PATH" not in env
    assert "GOOGLE_REFRESH_TOKEN" not in env
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in env
    assert "MS365_ACCESS_TOKEN" not in env


def test_enterprise_bootstrap_does_not_copy_host_auth_or_identity_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    copied_files: list[tuple[str, str]] = []
    copied_trees: list[tuple[str, str]] = []

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="claude-code",
        worker={"bootstrap_bundle_json": json.dumps({})},
        copy_file=lambda source, target: copied_files.append((str(source), str(target))),
        copy_tree=lambda source, target: copied_trees.append((str(source), str(target))),
    )

    assert copied_files == []
    assert copied_trees == []


def test_clean_room_bootstrap_purges_reused_host_credentials_and_ambient_provider_env(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_MODE", raising=False)
    monkeypatch.setenv("GLASSHIVE_PROJECT_PROVIDER_ENV", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-provider-key")
    home_dir = tmp_path / "reused-home"
    workspace_dir = tmp_path / "workspace"
    trusted_state_dir = tmp_path / "trusted-state"
    stale_credentials = [
        home_dir / ".codex" / "auth.json",
        home_dir / ".claude.json",
        home_dir / ".claude" / ".credentials.json",
        home_dir / ".gitconfig",
        home_dir / ".git-credentials",
        home_dir / ".config" / "git" / "credentials",
        home_dir / ".config" / "gh" / "hosts.yml",
        home_dir / ".config" / "glab-cli" / "config.yml",
    ]
    for path in stale_credentials:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-stale-host-credential")
    stale_codex_config = home_dir / ".codex" / "config.toml"
    stale_codex_config.write_text(
        '[mcp_servers.host-private]\ncommand = "synthetic-host-command"\n'
    )
    stale_host_tree_paths = [
        home_dir / ".claude" / "settings.json",
        home_dir / ".claude" / "plugins" / "host-hook.json",
        home_dir / ".claude" / "projects" / "host-session.jsonl",
        home_dir / ".codex" / "sessions" / "host-session.jsonl",
    ]
    for path in stale_host_tree_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-stale-host-authority")
    stale_manifest = home_dir / ".glasshive" / "bootstrap-manifest.json"
    stale_manifest.parent.mkdir(parents=True, exist_ok=True)
    stale_manifest.write_text(
        json.dumps({"execution_policy": "parallel-clean-room-v1"})
    )
    broker_url = "http://host.docker.internal:3080/api/viventium/glasshive/capabilities/mcp"
    worker = {
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": "parallel-clean-room-v1",
                "env": {
                    "OPENAI_API_KEY": "synthetic-caller-provider-key",
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant",
                },
                "codex_config_append": (
                    "[mcp_servers.glasshive-user-capabilities]\n"
                    f'url = "{broker_url}"\n'
                    'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
                ),
            }
        ),
    }
    copied_files = []
    copied_trees = []

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: copied_files.append((source, target)),
        copy_tree=lambda source, target: copied_trees.append((source, target)),
        trusted_state_dir=trusted_state_dir,
    )

    assert copied_files == []
    assert copied_trees == []
    assert all(not path.exists() for path in stale_credentials)
    assert all(not path.exists() for path in stale_host_tree_paths)
    codex_config = stale_codex_config.read_text()
    assert "host-private" not in codex_config
    assert "glasshive-user-capabilities" in codex_config
    assert 'url = "http://host.docker.internal:8080/mcp"' in codex_config
    assert broker_url not in codex_config
    runtime_files = "\n".join(
        path.read_text()
        for path in (
            home_dir / ".glasshive" / "runtime.env",
            home_dir / ".glasshive" / "secret-runtime.env",
        )
        if path.exists()
    )
    assert "synthetic-ambient-provider-key" not in runtime_files
    assert "synthetic-caller-provider-key" not in runtime_files
    assert "GLASSHIVE_CAPABILITY_BROKER_TOKEN" not in runtime_files
    runtime_env = home_dir / ".glasshive" / "runtime.env"
    secret_env = home_dir / ".glasshive" / "secret-runtime.env"
    secret_keys = home_dir / ".glasshive" / "secret-runtime.keys"
    assert not runtime_env.exists()
    assert not secret_env.exists()
    assert not secret_keys.exists()
    assert (trusted_state_dir / ".parallel-clean-room-v1").read_text() == (
        "parallel-clean-room-v1\n"
    )

    worker_session = home_dir / ".claude" / "projects" / "worker-session.jsonl"
    worker_session.parent.mkdir(parents=True, exist_ok=True)
    worker_session.write_text("synthetic-worker-session")
    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="claude-code",
        worker=worker,
        copy_file=lambda source, target: copied_files.append((source, target)),
        copy_tree=lambda source, target: copied_trees.append((source, target)),
        trusted_state_dir=trusted_state_dir,
    )
    assert worker_session.read_text() == "synthetic-worker-session"

def _install_clean_room_ancestor_swap_race(
    monkeypatch,
    *,
    ancestor,
    displaced_ancestor,
    outside_dir,
):
    """Swap an authority ancestor after either the legacy check or its safe fd open."""

    original_exists = bootstrap_module.Path.exists
    original_open = bootstrap_module.os.open
    race = {"swapped": False}

    def swap_ancestor():
        if race["swapped"]:
            return
        ancestor.rename(displaced_ancestor)
        ancestor.symlink_to(outside_dir, target_is_directory=True)
        race["swapped"] = True

    def racing_exists(path):
        exists = original_exists(path)
        if path == ancestor and exists and not race["swapped"]:
            swap_ancestor()
        return exists

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if str(path) == ancestor.name and dir_fd is not None and not race["swapped"]:
            swap_ancestor()
        return descriptor

    monkeypatch.setattr(bootstrap_module.Path, "exists", racing_exists)
    monkeypatch.setattr(bootstrap_module.os, "open", racing_open)
    return race

def test_clean_room_authority_file_removal_does_not_follow_swapped_ancestor(
    tmp_path, monkeypatch
):
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
    trusted_state_dir = tmp_path / "trusted-state"
    claude_dir = home_dir / ".claude"
    displaced_claude_dir = home_dir / ".claude-displaced"
    outside_dir = tmp_path / "outside"
    worker = {
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": "parallel-clean-room-v1"}
        ),
    }
    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="claude-code",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
        trusted_state_dir=trusted_state_dir,
    )
    assert (trusted_state_dir / ".parallel-clean-room-v1").exists()
    claude_dir.mkdir(parents=True)
    outside_dir.mkdir()
    (claude_dir / ".credentials.json").write_text("synthetic-stale-worker-copy")
    outside_credential = outside_dir / ".credentials.json"
    outside_credential.write_text("synthetic-outside-sentinel")
    race = _install_clean_room_ancestor_swap_race(
        monkeypatch,
        ancestor=claude_dir,
        displaced_ancestor=displaced_claude_dir,
        outside_dir=outside_dir,
    )

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="claude-code",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
        trusted_state_dir=trusted_state_dir,
    )

    assert race["swapped"] is True
    assert outside_credential.read_text() == "synthetic-outside-sentinel"
    assert not (displaced_claude_dir / ".credentials.json").exists()
    assert not claude_dir.exists()
    assert not claude_dir.is_symlink()

def test_clean_room_authority_tree_removal_does_not_follow_swapped_ancestor(
    tmp_path, monkeypatch
):
    home_dir = tmp_path / "home"
    claude_dir = home_dir / ".claude"
    displaced_claude_dir = home_dir / ".claude-displaced"
    outside_dir = tmp_path / "outside"
    stale_tree_file = claude_dir / "plugins" / "synthetic-host-hook.json"
    stale_tree_file.parent.mkdir(parents=True)
    stale_tree_file.write_text("synthetic-stale-worker-copy")
    outside_tree_file = outside_dir / "plugins" / "sentinel.json"
    outside_tree_file.parent.mkdir(parents=True)
    outside_tree_file.write_text("synthetic-outside-sentinel")
    race = _install_clean_room_ancestor_swap_race(
        monkeypatch,
        ancestor=claude_dir,
        displaced_ancestor=displaced_claude_dir,
        outside_dir=outside_dir,
    )

    bootstrap_module._remove_sandbox_authority_path(home_dir, ".claude/plugins")

    assert race["swapped"] is True
    assert outside_tree_file.read_text() == "synthetic-outside-sentinel"
    assert not (displaced_claude_dir / "plugins").exists()
    assert not claude_dir.exists()
    assert not claude_dir.is_symlink()

def test_clean_room_glasshive_cleanup_does_not_follow_swapped_home(
    tmp_path, monkeypatch
):
    home_dir = tmp_path / "home"
    displaced_home_dir = tmp_path / "home-displaced"
    workspace_dir = tmp_path / "workspace"
    trusted_state_dir = tmp_path / "trusted-state"
    outside_dir = tmp_path / "outside"
    outside_glasshive_target = tmp_path / "outside-glasshive-target"
    home_dir.mkdir()
    workspace_dir.mkdir()
    trusted_state_dir.mkdir()
    outside_dir.mkdir()
    outside_glasshive_target.mkdir()
    (trusted_state_dir / ".parallel-clean-room-v1").write_text(
        "parallel-clean-room-v1\n"
    )
    outside_glasshive = outside_dir / ".glasshive"
    outside_glasshive.symlink_to(outside_glasshive_target, target_is_directory=True)
    original_marker_check = bootstrap_module._has_trusted_clean_room_policy_marker
    race = {"swapped": False}

    def marker_check_then_swap(state_dir):
        result = original_marker_check(state_dir)
        if result and not race["swapped"]:
            home_dir.rename(displaced_home_dir)
            home_dir.symlink_to(outside_dir, target_is_directory=True)
            race["swapped"] = True
        return result

    monkeypatch.setattr(
        bootstrap_module,
        "_has_trusted_clean_room_policy_marker",
        marker_check_then_swap,
    )

    with pytest.raises(PermissionError, match="real directory"):
        bootstrap_module._purge_clean_room_authority(
            home_dir,
            workspace_dir,
            trusted_state_dir,
        )

    assert race["swapped"] is True
    assert outside_glasshive.is_symlink()
    assert outside_glasshive.resolve() == outside_glasshive_target

def test_parallel_clean_room_workspace_files_do_not_follow_worker_symlinks(tmp_path):
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
    outside_dir = tmp_path / "outside"
    workspace_dir.mkdir()
    outside_dir.mkdir()
    (workspace_dir / "uploads").symlink_to(outside_dir, target_is_directory=True)
    worker = {
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": "parallel-clean-room-v1",
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/escape.txt",
                        "content": "must-stay-in-workspace",
                    }
                ],
            }
        ),
    }

    with pytest.raises(PermissionError, match="symbolic links"):
        apply_bootstrap(
            home_dir=home_dir,
            workspace_dir=workspace_dir,
            runtime_name="codex-cli",
            worker=worker,
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
            trusted_state_dir=tmp_path / "state",
        )

    assert not (outside_dir / "escape.txt").exists()

def test_parallel_clean_room_marker_is_invalidated_across_legacy_profile_transition(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
    trusted_state_dir = tmp_path / "trusted-state"
    clean_worker = {
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": "parallel-clean-room-v1"}
        ),
    }

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="claude-code",
        worker=clean_worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
        trusted_state_dir=trusted_state_dir,
    )
    marker = trusted_state_dir / ".parallel-clean-room-v1"
    assert marker.exists()

    def copy_legacy_claude_tree(source, target):
        del source
        for relative in ("plugins/host-hook.json", "projects/host-session.jsonl"):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("synthetic-host-authority")

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="claude-code",
        worker={"bootstrap_profile": "claude-host", "bootstrap_bundle_json": "{}"},
        copy_file=lambda source, target: None,
        copy_tree=copy_legacy_claude_tree,
        trusted_state_dir=trusted_state_dir,
    )
    assert not marker.exists()
    assert (home_dir / ".claude" / "plugins" / "host-hook.json").exists()

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="claude-code",
        worker=clean_worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
        trusted_state_dir=trusted_state_dir,
    )
    assert marker.exists()
    assert not (home_dir / ".claude" / "plugins" / "host-hook.json").exists()
    assert not (home_dir / ".claude" / "projects" / "host-session.jsonl").exists()

def test_enterprise_bootstrap_keeps_provider_secrets_out_of_interactive_runtime_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "synthetic-openai-key-not-for-shell",
                    "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-claude-oauth-token-not-for-shell",
                    "PORTKEY_VIRTUAL_KEY": "pk-test-not-for-shell",
                    "OPENAI_BASE_URL": "https://provider.example.com/v1",
                }
            }
        )
    }

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    runtime_env = (tmp_path / "home" / ".glasshive" / "runtime.env").read_text()
    secret_env = (tmp_path / "home" / ".glasshive" / "secret-runtime.env").read_text()
    secret_keys = (tmp_path / "home" / ".glasshive" / "secret-runtime.keys").read_text().splitlines()

    assert "OPENAI_BASE_URL" in runtime_env
    assert "OPENAI_API_KEY" not in runtime_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in runtime_env
    assert "PORTKEY_VIRTUAL_KEY" not in runtime_env
    assert "OPENAI_API_KEY" in secret_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in secret_env
    assert "PORTKEY_VIRTUAL_KEY" not in secret_env
    assert set(secret_keys) == {"OPENAI_API_KEY"}
    assert oct((tmp_path / "home" / ".glasshive" / "secret-runtime.env").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "home" / ".glasshive" / "secret-runtime.keys").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    "binding_marker",
    ["_glasshive_provider_account_bound", "_glasshive_inference_broker_bound"],
)
def test_run_bound_credentials_cannot_be_clobbered_by_persisted_runtime_env(
    tmp_path,
    monkeypatch,
    binding_marker,
):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-deployment-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://deployment-gateway.example.test/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-deployment-anthropic-key")
    worker = {
        binding_marker: True,
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "synthetic-bundle-key",
                    "OPENAI_BASE_URL": "https://bundle-gateway.example.test/v1",
                    "ANTHROPIC_API_KEY": "synthetic-bundle-anthropic-key",
                }
            }
        ),
    }

    refresh_runtime_env_for_worker(tmp_path / "home", worker)

    glasshive_dir = tmp_path / "home" / ".glasshive"
    persisted = "\n".join(
        path.read_text()
        for path in (
            glasshive_dir / "runtime.env",
            glasshive_dir / "secret-runtime.env",
        )
        if path.exists()
    )
    assert "OPENAI_API_KEY" not in persisted
    assert "OPENAI_BASE_URL" not in persisted
    assert "ANTHROPIC_API_KEY" not in persisted


def test_enterprise_bootstrap_replaces_persisted_sandbox_owned_secret_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    home_dir = tmp_path / "home"
    glasshive_dir = home_dir / ".glasshive"
    glasshive_dir.mkdir(parents=True)
    stale_secret = glasshive_dir / "secret-runtime.env"
    stale_secret.write_text("stale")
    stale_secret.chmod(0o400)
    stale_keys = glasshive_dir / "secret-runtime.keys"
    stale_keys.write_text("STALE_KEY\n")
    stale_keys.chmod(0o400)
    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "replacement-key",
                    "OPENAI_BASE_URL": "https://provider.example.com/v1",
                }
            }
        )
    }

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    assert "replacement-key" in stale_secret.read_text()
    assert stale_keys.read_text().splitlines() == ["OPENAI_API_KEY"]
    assert oct(stale_secret.stat().st_mode & 0o777) == "0o600"
    assert oct(stale_keys.stat().st_mode & 0o777) == "0o600"


def test_enterprise_run_only_secrets_are_refreshed_for_each_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "synthetic-openai-key-not-for-shell",
                    "OPENAI_BASE_URL": "https://provider.example.com/v1",
                }
            }
        )
    }
    home_dir = tmp_path / "home"

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )
    secret_env = home_dir / ".glasshive" / "secret-runtime.env"
    runtime_env = home_dir / ".glasshive" / "runtime.env"
    assert "OPENAI_API_KEY" in secret_env.read_text()
    assert "OPENAI_API_KEY" not in runtime_env.read_text()

    secret_env.unlink()
    refresh_runtime_env_for_worker(home_dir, worker, "codex-cli")

    assert "OPENAI_API_KEY" in secret_env.read_text()
    assert "OPENAI_API_KEY" not in runtime_env.read_text()
    assert oct(secret_env.stat().st_mode & 0o777) == "0o600"


def test_refresh_project_runtime_files_rotates_broker_mcp_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"

    def worker_for(token: str, url: str) -> dict:
        return {
            "bootstrap_bundle_json": json.dumps(
                {
                    "claude_project_mcp": {
                        "glasshive-user-capabilities": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": f"Bearer {token}"},
                        }
                    },
                    "codex_config_append": (
                        "[mcp_servers.glasshive-user-capabilities]\n"
                        f'url = "{url}"\n'
                        'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
                    ),
                    "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": token},
                }
            )
        }

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="codex-cli",
        worker=worker_for("old-grant", "http://broker-old.example/mcp"),
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )
    refresh_project_runtime_files_for_worker(
        home_dir,
        workspace_dir,
        worker_for("new-grant", "http://broker-new.example/mcp"),
    )

    project_mcp = json.loads((workspace_dir / ".mcp.json").read_text())
    codex_config = (home_dir / ".codex" / "config.toml").read_text()

    assert project_mcp["mcpServers"]["glasshive-user-capabilities"]["headers"]["Authorization"] == (
        "Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}"
    )
    assert "new-grant" not in (workspace_dir / ".mcp.json").read_text()
    assert "old-grant" not in (workspace_dir / ".mcp.json").read_text()
    assert "http://broker-new.example/mcp" in codex_config
    assert "http://broker-old.example/mcp" not in codex_config
    assert codex_config.count("[mcp_servers.glasshive-user-capabilities]") == 1
    assert oct((workspace_dir / ".mcp.json").stat().st_mode & 0o777) == "0o600"
    assert oct((home_dir / ".codex" / "config.toml").stat().st_mode & 0o777) == "0o600"


def test_enterprise_ambient_provider_keys_are_run_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-key-not-for-shell")

    home_dir = tmp_path / "home"
    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker={"bootstrap_bundle_json": json.dumps({"env": {}})},
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    glasshive_dir = home_dir / ".glasshive"
    runtime_env = glasshive_dir / "runtime.env"
    secret_env = glasshive_dir / "secret-runtime.env"
    secret_keys = glasshive_dir / "secret-runtime.keys"

    assert "OPENAI_API_KEY" in secret_env.read_text()
    assert "OPENAI_API_KEY" in secret_keys.read_text()
    assert not runtime_env.exists() or "OPENAI_API_KEY" not in runtime_env.read_text()


def test_local_bootstrap_keeps_legacy_interactive_runtime_env_behavior(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    worker = {"bootstrap_bundle_json": json.dumps({"env": {"OPENAI_API_KEY": "local-dev-key"}})}

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    runtime_env = (tmp_path / "home" / ".glasshive" / "runtime.env").read_text()
    assert "OPENAI_API_KEY" in runtime_env
    assert oct((tmp_path / "home" / ".glasshive" / "runtime.env").stat().st_mode & 0o777) == "0o600"
    assert not (tmp_path / "home" / ".glasshive" / "secret-runtime.env").exists()


@pytest.mark.parametrize("enterprise", [False, True])
def test_server_only_authority_is_never_projected_to_worker_env(monkeypatch, enterprise):
    if enterprise:
        monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
        monkeypatch.setenv(
            "GLASSHIVE_WORKER_ENV_ALLOWLIST",
            "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET,"
            "VIVENTIUM_GLASSHIVE_ADMISSION_URL,"
            "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET",
        )
    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET": "synthetic-do-not-project",
                    "VIVENTIUM_GLASSHIVE_ADMISSION_URL": "https://internal.example/admit",
                    "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET": "synthetic-admission-secret",
                    "SAFE_WORKER_VALUE": "visible",
                }
            }
        )
    }

    env = bootstrap_env_for(worker)

    assert "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET" not in env
    assert "VIVENTIUM_GLASSHIVE_ADMISSION_URL" not in env
    assert "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET" not in env
    assert "synthetic-do-not-project" not in json.dumps(env)

def test_enterprise_bootstrap_rejects_unsigned_source_path(tmp_path, monkeypatch):
    uploads_root = tmp_path / "uploads"
    other_user_file = uploads_root / "user-b" / "brief.txt"
    other_user_file.parent.mkdir(parents=True)
    other_user_file.write_text("other user's data")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))

    worker = {
        "tenant_id": "tenant-alpha",
        "owner_id": "user-a",
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/brief.txt",
                        "source_path": str(other_user_file),
                    }
                ]
            }
        ),
    }

    with pytest.raises(PermissionError, match="not authorized"):
        apply_bootstrap(
            home_dir=tmp_path / "home",
            workspace_dir=tmp_path / "workspace",
            runtime_name="codex-cli",
            worker=worker,
            copy_file=lambda source, target: target.write_text(source.read_text()),
            copy_tree=lambda source, target: None,
        )


def test_multi_user_security_mode_rejects_unsigned_source_path_without_legacy_flag(tmp_path, monkeypatch):
    uploads_root = tmp_path / "uploads"
    other_user_file = uploads_root / "user-b" / "brief.txt"
    other_user_file.parent.mkdir(parents=True)
    other_user_file.write_text("synthetic cross-user data")
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_MODE", raising=False)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-secret")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))

    worker = {
        "tenant_id": "tenant-alpha",
        "owner_id": "user-a",
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/brief.txt",
                        "source_path": str(other_user_file),
                    }
                ]
            }
        ),
    }

    with pytest.raises(PermissionError, match="not authorized"):
        apply_bootstrap(
            home_dir=tmp_path / "home",
            workspace_dir=tmp_path / "workspace",
            runtime_name="codex-cli",
            worker=worker,
            copy_file=lambda source, target: target.write_text(source.read_text()),
            copy_tree=lambda source, target: None,
        )


def test_enterprise_bootstrap_source_signing_never_reuses_service_or_callback_secrets(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "uploads" / "user-a" / "brief.txt"
    source.parent.mkdir(parents=True)
    source.write_text("synthetic source")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.delenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", raising=False)
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret-must-not-sign-files")
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET",
        "callback-secret-must-not-sign-files",
    )

    assert sign_bootstrap_source_path(
        source,
        tenant_id="tenant-alpha",
        owner_id="user-a",
    ) == ""

def test_enterprise_bootstrap_accepts_signed_source_path_for_same_user(tmp_path, monkeypatch):
    uploads_root = tmp_path / "uploads"
    user_file = uploads_root / "user-a" / "brief.txt"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("same user's data")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    token = sign_bootstrap_source_path(user_file, tenant_id="tenant-alpha", owner_id="user-a")

    worker = {
        "tenant_id": "tenant-alpha",
        "owner_id": "user-a",
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/brief.txt",
                        "source_path": str(user_file),
                        "source_path_token": token,
                    }
                ]
            }
        ),
    }
    workspace = tmp_path / "workspace"

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: target.write_text(source.read_text()),
        copy_tree=lambda source, target: None,
    )

    assert (workspace / "uploads" / "brief.txt").read_text() == "same user's data"


def test_bootstrap_materializes_base64_uploaded_file(tmp_path):
    workspace = tmp_path / "workspace"

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="codex-cli",
        worker={
            "bootstrap_bundle_json": json.dumps(
                {
                    "files": [
                        {
                            "scope": "workspace",
                            "path": "uploads/report.bin",
                            "encoding": "base64",
                            "content_base64": "AAECSGVsbG8=",
                        }
                    ]
                }
            )
        },
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    assert (workspace / "uploads" / "report.bin").read_bytes() == b"\x00\x01\x02Hello"


def test_request_upload_projection_preserves_same_name_order_and_original_bytes(
    tmp_path, monkeypatch
):
    uploads_root = tmp_path / "uploads-root"
    owner_root = uploads_root / "owner-a"
    owner_root.mkdir(parents=True)
    sources = []
    for index, content in enumerate((b"image-one", b"image-two", b"image-three"), 1):
        source = owner_root / f"id-{index}__photo.jpg"
        source.write_bytes(content)
        sources.append(source)
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    files = [
        {
            "file_id": f"image-{index}",
            "filename": "photo.jpg",
            "filepath": f"/uploads/owner-a/{source.name}",
            "type": "image/jpeg",
            "media_group_index": index - 1,
        }
        for index, source in enumerate(sources, 1)
    ]
    projected = project_upload_files(
        {"request_files": files, "request_attachments": files},
        owner_id="owner-a",
        storage_owner_id="owner-a",
    )
    workspace = tmp_path / "workspace"

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="codex-cli",
        worker={
            "bootstrap_profile": "none",
            "bootstrap_bundle_json": json.dumps({"files": projected}),
        },
        copy_file=lambda source, target: target.write_bytes(source.read_bytes()),
        copy_tree=lambda source, target: None,
    )

    assert [item["path"] for item in projected] == [
        "uploads/photo.jpg",
        "uploads/photo-2.jpg",
        "uploads/photo-3.jpg",
    ]
    assert [
        (workspace / item["path"]).read_bytes() for item in projected
    ] == [b"image-one", b"image-two", b"image-three"]

def test_request_upload_projection_resolves_same_name_files_by_exact_file_id(
    tmp_path, monkeypatch
):
    uploads_root = tmp_path / "uploads-root"
    owner_root = uploads_root / "owner-a"
    owner_root.mkdir(parents=True)
    sources = []
    for index, content in enumerate((b"image-one", b"image-two", b"image-three"), 1):
        source = owner_root / f"image-{index}__photo.jpg"
        source.write_bytes(content)
        sources.append(source)
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))

    projected = project_upload_files(
        {
            "request_files": [
                {
                    "file_id": f"image-{index}",
                    "filename": "photo.jpg",
                    "type": "image/jpeg",
                    "media_group_index": index - 1,
                }
                for index in range(1, 4)
            ]
        },
        owner_id="owner-a",
        storage_owner_id="owner-a",
    )

    assert [item["path"] for item in projected] == [
        "uploads/photo.jpg",
        "uploads/photo-2.jpg",
        "uploads/photo-3.jpg",
    ]
    assert [Path(item["source_path"]).read_bytes() for item in projected] == [
        b"image-one",
        b"image-two",
        b"image-three",
    ]

def test_request_upload_projection_keeps_ordered_idless_album_entries(tmp_path, monkeypatch):
    uploads_root = tmp_path / "uploads-root"
    owner_root = uploads_root / "owner-a"
    owner_root.mkdir(parents=True)
    (owner_root / "first__photo.jpg").write_bytes(b"image-one")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))

    projected = project_upload_files(
        {
            "request_files": [
                {"filename": "photo.jpg", "type": "image/jpeg", "media_group_index": 0},
                {"filename": "photo.jpg", "type": "image/jpeg", "media_group_index": 1},
            ]
        },
        owner_id="owner-a",
        storage_owner_id="owner-a",
    )

    assert [item["path"] for item in projected] == [
        "uploads/photo.jpg.metadata.json",
        "uploads/photo-2.jpg.metadata.json",
    ]
    assert all("source_path" not in item for item in projected)

def test_request_upload_projection_rejects_a_foreign_owner_path(tmp_path, monkeypatch):
    uploads_root = tmp_path / "uploads-root"
    foreign = uploads_root / "owner-b" / "id__photo.jpg"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"foreign-image")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))

    projected = project_upload_files(
        {
            "request_files": [
                {
                    "file_id": "foreign-image",
                    "filename": "photo.jpg",
                    "filepath": "/uploads/owner-b/id__photo.jpg",
                    "type": "image/jpeg",
                }
            ]
        },
        owner_id="owner-a",
        storage_owner_id="owner-a",
    )

    assert len(projected) == 1
    assert projected[0]["path"] == "uploads/photo.jpg.metadata.json"
    assert "source_path" not in projected[0]
    assert projected[0]["upload_blocker"]

def test_request_upload_projection_emits_one_public_safe_text_entry(monkeypatch):
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    monkeypatch.delenv("WPR_BOOTSTRAP_SOURCE_ROOTS", raising=False)

    projected = project_upload_files(
        {
            "request_files": [
                {
                    "file_id": "public-safe-text",
                    "filename": "note.txt",
                    "text": "Synthetic public-safe note.",
                    "context": "private request context must not project",
                    "type": "text/plain",
                }
            ]
        },
        tenant_id="tenant-a",
        owner_id="owner-a",
        storage_owner_id="owner-a",
    )

    assert projected == [
        {
            "scope": "workspace",
            "path": "uploads/note.txt",
            "content": "Synthetic public-safe note.",
            "file_id": "public-safe-text",
            "filename": "note.txt",
            "type": "text/plain",
        }
    ]

def test_request_upload_projection_drops_untrusted_context_and_source_reference(
    monkeypatch,
):
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    monkeypatch.delenv("WPR_BOOTSTRAP_SOURCE_ROOTS", raising=False)

    projected = project_upload_files(
        {
            "request_files": [
                {
                    "file_id": "missing-original",
                    "filename": "document.pdf",
                    "filepath": "/private/synthetic/document.pdf",
                    "context": "private request context must not project",
                    "type": "application/pdf",
                }
            ]
        },
        tenant_id="tenant-a",
        owner_id="owner-a",
        storage_owner_id="owner-a",
    )

    serialized = json.dumps(projected)
    assert len(projected) == 1
    assert "private request context" not in serialized
    assert "/private/synthetic" not in serialized
    assert "source_ref" not in serialized

def test_request_upload_projection_recursively_drops_unsafe_source_metadata(
    monkeypatch,
):
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    monkeypatch.delenv("WPR_BOOTSTRAP_SOURCE_ROOTS", raising=False)
    projected = project_upload_files(
        {
            "outer": {
                "nested": [
                    {
                        "file_id": "/private/host/file-id",
                        "filename": "/private/host/name.txt",
                        "filepath": "https://private.example.invalid/raw/name.txt",
                        "source": "/private/host/name",
                        "type": "https://private.example.invalid/type",
                        "text": "safe text body",
                    },
                    {
                        "file_id": "file-safe_002",
                        "filename": "safe.txt",
                        "source": "librechat_model_context",
                        "type": "text/plain",
                        "text": "second safe text body",
                    },
                ]
            }
        },
        tenant_id="tenant-a",
        owner_id="owner-a",
        storage_owner_id="storage-a",
    )

    serialized = json.dumps(projected, sort_keys=True)
    assert len(projected) == 2
    assert "/private/host" not in serialized
    assert "https://private.example.invalid" not in serialized
    assert projected[0]["filename"] == "name.txt"
    assert "file_id" not in projected[0]
    assert "source" not in projected[0]
    assert "type" not in projected[0]
    assert projected[1]["file_id"] == "file-safe_002"
    assert projected[1]["source"] == "librechat_model_context"
    assert projected[1]["type"] == "text/plain"

def test_bootstrap_rejects_empty_uploaded_file_unless_explicitly_allowed(tmp_path):
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match="empty"):
        apply_bootstrap(
            home_dir=tmp_path / "home",
            workspace_dir=workspace,
            runtime_name="codex-cli",
            worker={
                "bootstrap_bundle_json": json.dumps(
                    {
                        "files": [
                            {
                                "scope": "workspace",
                                "path": "uploads/empty.bin",
                                "encoding": "base64",
                                "content_base64": "",
                            }
                        ]
                    }
                )
            },
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="codex-cli",
        worker={
            "bootstrap_bundle_json": json.dumps(
                {
                    "files": [
                        {
                            "scope": "workspace",
                            "path": "uploads/empty-allowed.bin",
                            "encoding": "base64",
                            "content_base64": "",
                            "allow_empty": True,
                        }
                    ]
                }
            )
        },
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    assert (workspace / "uploads" / "empty-allowed.bin").read_bytes() == b""


def test_bootstrap_rejects_file_entry_without_content_or_source(tmp_path):
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match="missing content or source_path"):
        apply_bootstrap(
            home_dir=tmp_path / "home",
            workspace_dir=workspace,
            runtime_name="codex-cli",
            worker={
                "bootstrap_bundle_json": json.dumps(
                    {
                        "files": [
                            {
                                "scope": "workspace",
                                "path": "uploads/missing.txt",
                            }
                        ]
                    }
                )
            },
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )

    assert not (workspace / "uploads" / "missing.txt").exists()
def test_claude_and_codex_receive_one_canonical_feeling_capsule_via_agents_md(tmp_path):
    workspace = tmp_path / "workspace"
    capsule = (
        "<viventium_feeling_state>\n"
        "synthetic request-pinned state\n"
        "</viventium_feeling_state>"
    )

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="claude-code",
        worker={
            "bootstrap_bundle_json": json.dumps(
                {
                    "agents_md": f"Stable worker policy.\n\n{capsule}",
                    "claude_md": "Claude-specific policy.",
                    "codex_md": "Codex-specific policy.",
                    "viventium_feelings_projection": {
                        "version": 1,
                        "enabled": True,
                        "scope": "all_agents",
                        "canonical_instruction_field": "agents_md",
                        "snapshot_sha256": hashlib.sha256(
                            capsule.encode("utf-8")
                        ).hexdigest(),
                        "expected_capsule_count": 1,
                    },
                }
            )
        },
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    generated = {
        name: (workspace / name).read_text()
        for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md")
    }
    assert generated["AGENTS.md"].count(capsule) == 1
    assert generated["CLAUDE.md"].count(capsule) == 0
    assert generated["CODEX.md"].count(capsule) == 0
    assert generated["CLAUDE.md"].startswith("@AGENTS.md")
    assert "Claude-specific policy." in generated["CLAUDE.md"]
    assert "Codex-specific policy." in generated["CODEX.md"]

def test_two_direct_workers_receive_the_identical_request_pinned_capsule(tmp_path):
    capsule = (
        "<viventium_feeling_state>\n"
        "one identical synthetic request-pinned state\n"
        "</viventium_feeling_state>"
    )
    pinned_hash = hashlib.sha256(capsule.encode("utf-8")).hexdigest()
    bundle = {
        "agents_md": f"Stable worker policy.\n\n{capsule}",
        "claude_md": "Claude-specific policy.",
        "codex_md": "Codex-specific policy.",
        "viventium_feelings_projection": {
            "version": 1,
            "enabled": True,
            "scope": "all_agents",
            "canonical_instruction_field": "agents_md",
            "snapshot_sha256": pinned_hash,
            "expected_capsule_count": 1,
        },
    }

    for worker_number, runtime_name in enumerate(("claude-code", "codex-cli")):
        workspace = tmp_path / f"workspace-{worker_number}"
        apply_bootstrap(
            home_dir=tmp_path / f"home-{worker_number}",
            workspace_dir=workspace,
            runtime_name=runtime_name,
            worker={"bootstrap_bundle_json": json.dumps(bundle)},
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )

        instructions = (workspace / "AGENTS.md").read_text()
        assert instructions.count(capsule) == 1
        assert instructions.rstrip().endswith(capsule)
        assert hashlib.sha256(capsule.encode("utf-8")).hexdigest() == pinned_hash
        for provider_file in ("CLAUDE.md", "CODEX.md"):
            assert capsule not in (workspace / provider_file).read_text()

def test_direct_worker_moves_feeling_after_later_structural_system_instructions(
    tmp_path,
):
    capsule = (
        "<viventium_feeling_state>\n"
        "synthetic final request-pinned state\n"
        "</viventium_feeling_state>"
    )
    workspace = tmp_path / "workspace"

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="claude-code",
        worker={
            "bootstrap_bundle_json": json.dumps(
                {
                    "agents_md": f"Stable worker policy.\n\n{capsule}",
                    "system_instructions": "Later structural worker policy.",
                    "viventium_feelings_projection": {
                        "version": 1,
                        "enabled": True,
                        "scope": "all_agents",
                        "canonical_instruction_field": "agents_md",
                        "snapshot_sha256": hashlib.sha256(
                            capsule.encode("utf-8")
                        ).hexdigest(),
                        "expected_capsule_count": 1,
                    },
                }
            )
        },
        copy_file=lambda _source, _target: None,
        copy_tree=lambda _source, _target: None,
    )

    authority = (workspace / "AGENTS.md").read_text()
    assert authority.count(capsule) == 1
    assert authority.index("Later structural worker policy.") < authority.index(
        capsule
    )
    assert authority.rstrip().endswith(capsule)

def test_reused_direct_worker_replaces_or_removes_only_the_current_feeling_capsule(
    tmp_path,
):
    first_capsule = (
        "<viventium_feeling_state>first synthetic state</viventium_feeling_state>"
    )
    second_capsule = (
        "<viventium_feeling_state>second synthetic state</viventium_feeling_state>"
    )
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"

    def worker_for(capsule: str, *, enabled: bool, scope: str) -> dict[str, str]:
        return {
            "bootstrap_bundle_json": json.dumps(
                {
                    "agents_md": f"Stable worker policy.\n\n{capsule}",
                    "claude_md": "Claude-specific policy.",
                    "codex_md": "Codex-specific policy.",
                    "viventium_feelings_projection": {
                        "version": 1,
                        "enabled": enabled,
                        "scope": scope,
                        "canonical_instruction_field": "agents_md",
                        "snapshot_sha256": hashlib.sha256(
                            capsule.encode("utf-8")
                        ).hexdigest(),
                        "expected_capsule_count": 1 if enabled else 0,
                    },
                }
            )
        }

    apply_bootstrap(
        home_dir=home,
        workspace_dir=workspace,
        runtime_name="claude-code",
        worker=worker_for(first_capsule, enabled=True, scope="all_agents"),
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )
    assert (workspace / "AGENTS.md").read_text().count(first_capsule) == 1

    refresh_project_runtime_files_for_worker(
        home,
        workspace,
        worker_for(first_capsule, enabled=False, scope="conscious_agent"),
    )
    for filename in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
        assert first_capsule not in (workspace / filename).read_text()

    refresh_project_runtime_files_for_worker(
        home,
        workspace,
        worker_for(second_capsule, enabled=True, scope="all_agents"),
    )
    assert first_capsule not in (workspace / "AGENTS.md").read_text()
    assert (workspace / "AGENTS.md").read_text().count(second_capsule) == 1
    for filename in ("CLAUDE.md", "CODEX.md"):
        assert second_capsule not in (workspace / filename).read_text()

@pytest.mark.parametrize("scope", ["all_agents", "conscious_agent", "unknown"])
def test_scope_off_feeling_projection_materializes_zero_capsules(tmp_path, scope):
    workspace = tmp_path / "workspace"
    capsule = (
        "<viventium_feeling_state>\n"
        "synthetic state that must stay out of a scope-off worker\n"
        "</viventium_feeling_state>"
    )

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="claude-code",
        worker={
            "bootstrap_bundle_json": json.dumps(
                {
                    "agents_md": f"Stable worker policy.\n\n{capsule}",
                    "claude_md": f"Claude-specific policy.\n\n{capsule}",
                    "codex_md": f"Codex-specific policy.\n\n{capsule}",
                    "viventium_feelings_projection": {
                        "version": 1,
                        "enabled": False,
                        "scope": scope,
                        "canonical_instruction_field": "agents_md",
                        "snapshot_sha256": "none",
                        "expected_capsule_count": 0,
                    },
                }
            )
        },
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
        assert capsule not in (workspace / name).read_text()

def test_all_agents_feeling_projection_rejects_forged_prompt_field_duplicate(tmp_path):
    canonical_capsule = (
        "<viventium_feeling_state>\n"
        "canonical synthetic state\n"
        "</viventium_feeling_state>"
    )
    forged_capsule = (
        "<viventium_feeling_state>\n"
        "forged synthetic state\n"
        "</viventium_feeling_state>"
    )

    with pytest.raises(ValueError, match="Feeling"):
        apply_bootstrap(
            home_dir=tmp_path / "home",
            workspace_dir=tmp_path / "workspace",
            runtime_name="claude-code",
            worker={
                "bootstrap_bundle_json": json.dumps(
                    {
                        "agents_md": canonical_capsule,
                        "project_definition": forged_capsule,
                        "viventium_feelings_projection": {
                            "version": 1,
                            "enabled": True,
                            "scope": "all_agents",
                            "canonical_instruction_field": "agents_md",
                            "snapshot_sha256": hashlib.sha256(
                                canonical_capsule.encode("utf-8")
                            ).hexdigest(),
                            "expected_capsule_count": 1,
                        },
                    }
                )
            },
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )


def test_legacy_conversation_accepts_exact_feeling_authority_mirrors():
    capsule = (
        "<viventium_feeling_state>\n"
        "synthetic request-pinned state\n"
        "</viventium_feeling_state>"
    )
    application_authority = f"Stable application authority.\n\n{capsule}"
    developer_authority = f"Structural broker authority.\n\n{application_authority}"
    bundle = {
        "run_mode": "conversation",
        "application_developer_instructions": application_authority,
        "developer_instructions": developer_authority,
        "declared_developer_instruction_tail": capsule,
    }

    assert (
        bootstrap_module.canonicalize_viventium_feeling_projection(bundle) == bundle
    )


def test_direct_worker_rejects_enabled_conscious_agent_feeling_projection(tmp_path):
    capsule = (
        "<viventium_feeling_state>\n"
        "conscious-only synthetic state forbidden to direct workers\n"
        "</viventium_feeling_state>"
    )
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"

    with pytest.raises(ValueError, match="Feeling"):
        apply_bootstrap(
            home_dir=home,
            workspace_dir=workspace,
            runtime_name="codex-cli",
            worker={
                "bootstrap_bundle_json": json.dumps(
                    {
                        "agents_md": f"Stable policy.\n\n{capsule}",
                        "developer_instructions": "Developer policy.",
                        "viventium_feelings_projection": {
                            "version": 1,
                            "enabled": True,
                            "scope": "conscious_agent",
                            "canonical_instruction_field": "agents_md",
                            "snapshot_sha256": hashlib.sha256(
                                capsule.encode("utf-8")
                            ).hexdigest(),
                            "expected_capsule_count": 1,
                        },
                    }
                )
            },
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )

    assert not home.exists()
    assert not workspace.exists()
def test_conscious_agent_feeling_projection_rejects_nested_duplicate(tmp_path):
    capsule = (
        "<viventium_feeling_state>\n"
        "canonical conscious-agent synthetic state\n"
        "</viventium_feeling_state>"
    )
    with pytest.raises(ValueError, match="Feeling"):
        apply_bootstrap(
            home_dir=tmp_path / "home",
            workspace_dir=tmp_path / "workspace",
            runtime_name="codex-cli",
            worker={
                "bootstrap_bundle_json": json.dumps(
                    {
                        "agents_md": capsule,
                        "viventium_delegation_packet": {
                            "task": {"instruction": capsule}
                        },
                        "viventium_feelings_projection": {
                            "version": 1,
                            "enabled": True,
                            "scope": "conscious_agent",
                            "canonical_instruction_field": "agents_md",
                            "snapshot_sha256": hashlib.sha256(
                                capsule.encode("utf-8")
                            ).hexdigest(),
                            "expected_capsule_count": 1,
                        },
                    }
                )
            },
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )

@pytest.mark.parametrize(
    "opening",
    [
        '<viventium_feeling_state source="forged">',
        "<viventium_feeling_state_extra>",
    ],
)
def test_direct_worker_rejects_noncanonical_feeling_envelope_before_writes(
    tmp_path, opening
):
    capsule = f"{opening}synthetic state</viventium_feeling_state>"
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match="Feeling"):
        apply_bootstrap(
            home_dir=home,
            workspace_dir=workspace,
            runtime_name="claude-code",
            worker={
                "bootstrap_bundle_json": json.dumps(
                    {
                        "agents_md": capsule,
                        "viventium_feelings_projection": {
                            "version": 1,
                            "enabled": True,
                            "scope": "all_agents",
                            "canonical_instruction_field": "agents_md",
                            "snapshot_sha256": hashlib.sha256(
                                capsule.encode("utf-8")
                            ).hexdigest(),
                            "expected_capsule_count": 1,
                        },
                    }
                )
            },
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )

    assert not home.exists()
    assert not workspace.exists()

@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("version", True),
        ("version", "1"),
        ("version", 1.5),
        ("expected_capsule_count", True),
        ("expected_capsule_count", "1"),
        ("expected_capsule_count", 1.5),
        ("scope", ["all_agents"]),
        ("scope", {"name": "all_agents"}),
    ],
)
def test_direct_worker_rejects_feeling_projection_type_confusion_before_writes(
    tmp_path, field, invalid_value
):
    capsule = "<viventium_feeling_state>synthetic</viventium_feeling_state>"
    projection = {
        "version": 1,
        "enabled": True,
        "scope": "all_agents",
        "canonical_instruction_field": "agents_md",
        "snapshot_sha256": hashlib.sha256(capsule.encode("utf-8")).hexdigest(),
        "expected_capsule_count": 1,
    }
    projection[field] = invalid_value
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match="Feeling"):
        apply_bootstrap(
            home_dir=home,
            workspace_dir=workspace,
            runtime_name="codex-cli",
            worker={
                "bootstrap_bundle_json": json.dumps(
                    {
                        "agents_md": capsule,
                        "viventium_feelings_projection": projection,
                    }
                )
            },
            copy_file=lambda source, target: None,
            copy_tree=lambda source, target: None,
        )

    assert not home.exists()
    assert not workspace.exists()
