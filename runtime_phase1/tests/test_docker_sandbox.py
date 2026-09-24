from __future__ import annotations

import hashlib
import json
import os
import signal
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import pytest

from workers_projects_runtime.docker_sandbox import (
    AI_WORKER_APT_SNAPSHOT,
    AI_WORKER_CLAUDE_CODE_NPM_SPEC,
    AI_WORKER_CODEX_NPM_SPEC,
    AI_WORKER_PYTHON_LOCK_PATH,
    DockerSandboxManager,
    FreshSandboxInspection,
    PARALLEL_CLEAN_ROOM_TMPFS,
    SandboxInfo,
    VNC_PASSWORD_ALPHABET,
    _CLAUDE_WORKSPACE_ONBOARDING_SCRIPT,
    _CODEX_PROVIDER_AUTH_LINK_SCRIPT,
    _ai_worker_browser_extension_check_script,
    _ai_worker_browser_native_host_bootstrap_script,
    _provider_account_seal_script,
    _safe_docker_exec_env,
)
from workers_projects_runtime.bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_NATIVE_CAPABILITY_INVENTORY,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
)
from workers_projects_runtime.openclaw_release import (
    OPENCLAW_RUNTIME_FAST_URI_VERSION,
    OPENCLAW_RUNTIME_LOCK_SHA256,
    OPENCLAW_RUNTIME_VERSION,
)
from workers_projects_runtime.codex_plugins import (
    OPENAI_PLUGIN_MARKETPLACE_COMMIT,
    OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH,
    OPENAI_PLUGIN_MARKETPLACE_ORIGIN,
)

import re

from workers_projects_runtime.docker_sandbox import (
    DockerSandboxManager,
    FreshSandboxInspection,
    PARALLEL_CLEAN_ROOM_TMPFS,
    SandboxInfo,
    _ai_worker_browser_extension_check_script,
    _ai_worker_browser_native_host_bootstrap_script,
    _safe_docker_exec_env,
)

from workers_projects_runtime.bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_NATIVE_CAPABILITY_INVENTORY,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
)

from workers_projects_runtime.openclaw_runtime import HostCapacityError


def test_worker_codex_uses_the_current_reviewed_release():
    assert AI_WORKER_CODEX_NPM_SPEC == "@openai/codex@0.155.0-alpha.9.2"


def test_worker_claude_uses_the_current_reviewed_release():
    assert AI_WORKER_CLAUDE_CODE_NPM_SPEC == "@anthropic-ai/claude-code@2.1.280"


def test_bootstrap_copy_never_follows_an_existing_destination_symlink(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path / "workers"))
    source = tmp_path / "source-auth.json"
    destination = tmp_path / "home" / ".codex" / "auth.json"
    source.write_text("host credential")
    destination.parent.mkdir(parents=True)
    destination.symlink_to("/workspace/.provider-account/codex/auth.json")

    manager._copy_file(source, destination)  # type: ignore[attr-defined]

    assert destination.is_symlink()
    assert destination.readlink() == Path("/workspace/.provider-account/codex/auth.json")


def test_running_task_sandbox_recreate_requires_transient_task_marker():
    assert DockerSandboxManager._worker_state_allows_substrate_recreate(
        {"state": "running", "_glasshive_task_run": True}
    )
    assert not DockerSandboxManager._worker_state_allows_substrate_recreate(
        {"state": "running"}
    )


def _stub_parallel_clean_room_mission_network(
    manager: DockerSandboxManager,
) -> None:
    manager._ensure_parallel_clean_room_mission_network = (  # type: ignore[method-assign]
        lambda container_name: manager._parallel_clean_room_mission_network_name(
            container_name
        )
    )

def test_safe_docker_exec_env_preserves_claude_headless_oauth_only():
    env = _safe_docker_exec_env(
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
            "ANTHROPIC_API_KEY": "api-key",
            "UNRELATED_SECRET": "must-not-pass",
        }
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert env["ANTHROPIC_API_KEY"] == "api-key"
    assert "UNRELATED_SECRET" not in env


def test_safe_docker_exec_env_preserves_bound_provider_home_selectors():
    env = _safe_docker_exec_env(
        {
            "CODEX_HOME": "/workspace/.wpr-home/.codex",
            "CLAUDE_CONFIG_DIR": "/workspace/.wpr-home/.claude",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/workspace/.provider-account/claude",
            "UNRELATED_SECRET": "must-not-pass",
        }
    )

    assert env["CODEX_HOME"] == "/workspace/.wpr-home/.codex"
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == "/workspace/.provider-account/claude"
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "UNRELATED_SECRET" not in env


def test_claude_workspace_onboarding_marker_preserves_native_state(tmp_path):
    workspace_home = tmp_path / "home"
    workspace_home.mkdir(mode=0o700)
    state_path = workspace_home / ".claude.json"
    state_path.write_text('{"theme":"dark"}\n', encoding="utf-8")
    state_path.chmod(0o600)

    completed = subprocess.run(
        [sys.executable, "-c", _CLAUDE_WORKSPACE_ONBOARDING_SCRIPT, str(workspace_home)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "hasCompletedOnboarding": True,
        "theme": "dark",
    }
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_claude_workspace_onboarding_accepts_container_root_owned_bind_home(tmp_path):
    workspace_home = tmp_path / "root-owned-bind-home"
    workspace_home.mkdir(mode=0o700)
    wrapper = f"""
import pathlib
import types
import sys

workspace = pathlib.Path(sys.argv[1])
real_lstat = pathlib.Path.lstat

def docker_bind_lstat(path):
    metadata = real_lstat(path)
    if path == workspace:
        return types.SimpleNamespace(st_mode=metadata.st_mode, st_uid=0)
    return metadata

pathlib.Path.lstat = docker_bind_lstat
exec({_CLAUDE_WORKSPACE_ONBOARDING_SCRIPT!r})
"""

    completed = subprocess.run(
        [sys.executable, "-c", wrapper, str(workspace_home)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((workspace_home / ".claude.json").read_text(encoding="utf-8")) == {
        "hasCompletedOnboarding": True,
    }


def test_claude_workspace_onboarding_tightens_acl_mask_mode(tmp_path):
    workspace_home = tmp_path / "acl-prepared-home"
    workspace_home.mkdir(mode=0o700)
    state_path = workspace_home / ".claude.json"
    state_path.write_text('{"nativeState":"preserved"}\n', encoding="utf-8")
    state_path.chmod(0o670)

    completed = subprocess.run(
        [sys.executable, "-c", _CLAUDE_WORKSPACE_ONBOARDING_SCRIPT, str(workspace_home)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "hasCompletedOnboarding": True,
        "nativeState": "preserved",
    }
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_safe_docker_exec_env_preserves_bedrock_run_credentials_only():
    env = _safe_docker_exec_env(
        {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_ACCESS_KEY_ID": "AKIAEXAMPLEONLY0000",
            "AWS_SECRET_ACCESS_KEY": "synthetic-secret-not-real",
            "AWS_REGION": "us-east-1",
            "AWS_EC2_METADATA_DISABLED": "true",
            "API_TIMEOUT_MS": "240000",
            "UNRELATED_SECRET": "must-not-pass",
        }
    )

    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLEONLY0000"
    assert env["AWS_SECRET_ACCESS_KEY"] == "synthetic-secret-not-real"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["API_TIMEOUT_MS"] == "240000"
    assert "UNRELATED_SECRET" not in env


def test_docker_disk_probe_falls_back_when_running_containers_lack_gnu_df(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    probes: list[list[str]] = []

    def fake_docker(args: list[str], **_kwargs):
        probes.append(args)
        if args[:2] == ["exec", "busybox-service"]:
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                "df: unrecognized option: output=size,used,avail",
            )
        if args[:3] == ["run", "--rm", "--network"]:
            return subprocess.CompletedProcess(
                args,
                0,
                "1B-blocks Used Available\n68719476736 4000000000 64719476736\n",
                "",
            )
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]

    assert manager._docker_vm_available_disk_bytes({"busybox-service"}) == 64_719_476_736
    assert any(args[:3] == ["run", "--rm", "--network"] for args in probes)

def test_seed_bootstrap_writes_default_worker_contract_without_bundle(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
    home_dir.mkdir()
    workspace_dir.mkdir()

    manager._seed_bootstrap(home_dir, workspace_dir, "codex-cli", {"worker_id": "wrk_contract"})

    agents_text = (workspace_dir / "AGENTS.md").read_text()
    assert "GlassHive Worker Contract" in agents_text
    assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in agents_text
    assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in agents_text
    assert GLASSHIVE_NATIVE_CAPABILITY_INVENTORY in agents_text
    assert "WebDriver/Selenium endpoint" in agents_text
    assert "Use the visible workstation surface" in agents_text
    assert "Less is more" in agents_text
    assert "Do not force a download" in agents_text
    assert "@AGENTS.md" in (workspace_dir / "CLAUDE.md").read_text()


def test_create_container_adds_host_gateway_alias_for_broker_reachability(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    captured: list[list[str]] = []

    def fake_docker(args: list[str], **kwargs):
        captured.append(args)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="cid", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    command = captured[-1]
    assert "--add-host" in command
    assert command[command.index("--network") + 1] == manager._network_name_for_container("wpr-test")
    assert "host.docker.internal:host-gateway" in command
    assert "--security-opt" in command
    assert "seccomp=unconfined" in command
    assert f"TMPDIR={manager.service_tmp_dir}" in command
    assert f"TMPDIR={manager._browser_tmp_dir()}" not in command
    assert f"XDG_CACHE_HOME={manager._browser_cache_dir()}" in command
    assert f"XDG_CONFIG_HOME={manager._browser_config_dir()}" in command

    monkeypatch.setenv("WPR_SANDBOX_ADD_HOST_GATEWAY", "0")
    manager_without_alias = DockerSandboxManager(base_dir=str(tmp_path / "disabled"))
    captured.clear()
    manager_without_alias._docker = fake_docker  # type: ignore[method-assign]
    manager_without_alias._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    assert "--add-host" not in captured[-1]
    assert "--security-opt" in captured[-1]
    assert "seccomp=unconfined" in captured[-1]

    monkeypatch.setenv("WPR_SANDBOX_ALLOW_CHROMIUM_USERNS", "0")
    manager_without_chromium_userns = DockerSandboxManager(base_dir=str(tmp_path / "no-userns"))
    captured.clear()
    manager_without_chromium_userns._docker = fake_docker  # type: ignore[method-assign]
    manager_without_chromium_userns._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    assert "--security-opt" not in captured[-1]
    assert "seccomp=unconfined" not in captured[-1]


def test_create_container_mounts_only_a_trusted_bound_provider_account_home(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    account_home.mkdir(parents=True)
    captured: list[list[str]] = []

    def fake_docker(args: list[str], **kwargs):
        captured.append(args)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], 1, "", "not found")
        return subprocess.CompletedProcess(["docker", *args], 0, "cid", "")

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
        worker={
            "_glasshive_provider_account_bound": True,
            "_glasshive_provider_account_mount_host": str(account_home),
            "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
        },
    )

    command = captured[-1]
    assert (
        f"{account_home.resolve()}:/workspace/.provider-account"
        in command
    )


def test_bound_provider_account_mount_grants_and_verifies_only_worker_user_access(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    (account_home / "codex").mkdir(parents=True)
    (account_home / "codex" / "auth.json").write_text("synthetic auth")
    calls: list[tuple[str | None, list[str]]] = []

    def fake_docker_exec(
        container_name,
        command,
        *,
        env=None,
        cwd=None,
        detach=False,
        fire_and_forget=False,
        user=None,
    ):
        calls.append((user, command))
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]
    worker = {
        "_glasshive_provider_account_bound": True,
        "_glasshive_provider_account_mount_host": str(account_home),
        "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
        "_glasshive_provider_account_env": {
            "CODEX_HOME": "/workspace/.wpr-home/.codex"
        },
    }

    manager._grant_provider_account_access("wpr-test", worker)  # type: ignore[attr-defined]

    assert len(calls) == 3
    assert calls[0][0] == "root"
    assert calls[0][1][:2] == ["python3", "-c"]
    assert calls[0][1][2] == _CODEX_PROVIDER_AUTH_LINK_SCRIPT
    assert calls[0][1][3:] == [
        "/workspace/.provider-account",
        "codex",
        "/workspace/.wpr-home",
        "seluser",
    ]
    assert "os.O_NOFOLLOW" in calls[0][1][2]
    assert "metadata.st_nlink != 1" in calls[0][1][2]
    assert "os.symlink" in calls[0][1][2]
    assert calls[1][0] == "root"
    grant_script = calls[1][1][-1]
    assert "command -v setfacl" in grant_script
    assert "setfacl -m u:seluser:x /workspace/.provider-account" in grant_script
    assert "setfacl -m u:seluser:rw /workspace/.provider-account/codex/auth.json" in grant_script
    assert "setfacl -R" not in grant_script
    assert calls[2][0] == "seluser"
    verify_script = calls[2][1][-1]
    assert "test -r /workspace/.wpr-home/.codex" in verify_script
    assert "test -w /workspace/.wpr-home/.codex" in verify_script
    assert "readlink /workspace/.wpr-home/.codex/auth.json" in verify_script
    assert "/workspace/.provider-account/codex/auth.json" in verify_script


def _run_codex_provider_auth_link(
    provider_root: Path,
    workspace_home: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CODEX_PROVIDER_AUTH_LINK_SCRIPT,
            str(provider_root),
            "codex",
            str(workspace_home),
            f"{os.getuid()}:{os.getgid()}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_codex_provider_auth_link_keeps_workspace_config_and_uses_live_account_auth(tmp_path):
    provider_root = tmp_path / "provider"
    provider_home = provider_root / "codex"
    provider_home.mkdir(parents=True)
    provider_auth = provider_home / "auth.json"
    provider_auth.write_text("synthetic live credential")
    provider_auth.chmod(0o600)
    workspace_home = tmp_path / "workspace-home"
    workspace_codex = workspace_home / ".codex"
    workspace_codex.mkdir(parents=True)
    workspace_config = workspace_codex / "config.toml"
    workspace_config.write_text("[features]\nplugins = true\n")
    plugin_marker = workspace_codex / "plugins" / "installed.marker"
    plugin_marker.parent.mkdir()
    plugin_marker.write_text("persistent")
    workspace_codex.chmod(0o750)
    original_owner = (workspace_codex.stat().st_uid, workspace_codex.stat().st_gid)

    first = _run_codex_provider_auth_link(provider_root, workspace_home)
    auth_link = workspace_codex / "auth.json"
    # Codex's file backend refreshes auth with an in-place truncate/write. The
    # live link must update the selected account file, never copy it into the
    # reusable workspace.
    auth_link.write_text("synthetic refreshed credential")
    second = _run_codex_provider_auth_link(provider_root, workspace_home)

    assert first.returncode == second.returncode == 0
    assert auth_link.is_symlink()
    assert auth_link.readlink() == provider_auth
    assert auth_link.read_text() == "synthetic refreshed credential"
    assert provider_auth.read_text() == "synthetic refreshed credential"
    assert workspace_config.read_text() == "[features]\nplugins = true\n"
    assert plugin_marker.read_text() == "persistent"
    assert (workspace_codex.stat().st_uid, workspace_codex.stat().st_gid) == original_owner
    assert stat.S_IMODE(workspace_codex.stat().st_mode) == 0o750


def test_codex_provider_auth_overlay_restores_workspace_login_for_idle_reuse(tmp_path):
    provider_root = tmp_path / "provider"
    provider_home = provider_root / "codex"
    provider_home.mkdir(parents=True)
    provider_auth = provider_home / "auth.json"
    provider_auth.write_text("synthetic mission credential")
    provider_auth.chmod(0o600)
    workspace_home = tmp_path / "workspace-home"
    workspace_codex = workspace_home / ".codex"
    workspace_codex.mkdir(parents=True)
    workspace_auth = workspace_codex / "auth.json"
    workspace_auth.write_text("synthetic workspace credential")
    workspace_auth.chmod(0o600)
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    private_dir = tmp_path / "worker-private"

    manager._prepare_workspace_codex_auth_overlay(  # type: ignore[attr-defined]
        workspace_home,
        private_dir,
    )

    mission = _run_codex_provider_auth_link(provider_root, workspace_home)

    assert mission.returncode == 0
    assert workspace_auth.is_symlink()
    assert workspace_auth.readlink() == provider_auth
    private_backup = private_dir / "codex-workspace-auth.json"
    assert private_backup.read_text() == "synthetic workspace credential"
    assert private_backup.parent != workspace_home
    assert not os.path.lexists(workspace_codex / ".glasshive-workspace-auth.json")

    manager._restore_workspace_codex_auth(  # type: ignore[attr-defined]
        workspace_home,
        private_dir,
        expected_target=str(provider_auth),
    )

    assert workspace_auth.is_file()
    assert not workspace_auth.is_symlink()
    assert workspace_auth.read_text() == "synthetic workspace credential"
    assert not private_backup.exists()


def test_codex_provider_auth_overlay_leaves_idle_workspace_ready_for_manual_login(tmp_path):
    provider_root = tmp_path / "provider"
    provider_home = provider_root / "codex"
    provider_home.mkdir(parents=True)
    provider_auth = provider_home / "auth.json"
    provider_auth.write_text("synthetic mission credential")
    provider_auth.chmod(0o600)
    workspace_home = tmp_path / "workspace-home"
    workspace_home.mkdir()
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    private_dir = tmp_path / "worker-private"
    manager._prepare_workspace_codex_auth_overlay(  # type: ignore[attr-defined]
        workspace_home,
        private_dir,
    )

    mission = _run_codex_provider_auth_link(provider_root, workspace_home)
    assert mission.returncode == 0

    manager._restore_workspace_codex_auth(  # type: ignore[attr-defined]
        workspace_home,
        private_dir,
        expected_target=str(provider_auth),
    )

    assert not (workspace_home / ".codex" / "auth.json").exists()
    assert not (private_dir / "codex-workspace-auth.json").exists()


def test_codex_provider_auth_link_preserves_host_managed_workspace_home_contract():
    assert "os.fchown(codex_home_fd" not in _CODEX_PROVIDER_AUTH_LINK_SCRIPT
    assert "os.fchmod(codex_home_fd" not in _CODEX_PROVIDER_AUTH_LINK_SCRIPT


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_codex_provider_auth_link_rejects_unsafe_account_auth(tmp_path, unsafe_kind):
    provider_root = tmp_path / "provider"
    provider_home = provider_root / "codex"
    provider_home.mkdir(parents=True)
    provider_auth = provider_home / "auth.json"
    external = tmp_path / "external"
    external.write_text("unchanged")
    if unsafe_kind == "symlink":
        provider_auth.symlink_to(external)
    elif unsafe_kind == "hardlink":
        os.link(external, provider_auth)
    else:
        os.mkfifo(provider_auth)
    workspace_home = tmp_path / "workspace-home"
    workspace_home.mkdir()

    result = _run_codex_provider_auth_link(provider_root, workspace_home)

    assert result.returncode != 0
    assert external.read_text() == "unchanged"
    assert not (workspace_home / ".codex" / "auth.json").exists()


@pytest.mark.parametrize("unsafe_kind", ["wrong_link", "fifo"])
def test_codex_provider_auth_link_never_replaces_workspace_auth_state(
    tmp_path,
    unsafe_kind,
):
    provider_root = tmp_path / "provider"
    provider_home = provider_root / "codex"
    provider_home.mkdir(parents=True)
    provider_auth = provider_home / "auth.json"
    provider_auth.write_text("synthetic live credential")
    provider_auth.chmod(0o600)
    workspace_home = tmp_path / "workspace-home"
    workspace_codex = workspace_home / ".codex"
    workspace_codex.mkdir(parents=True)
    workspace_auth = workspace_codex / "auth.json"
    if unsafe_kind == "wrong_link":
        workspace_auth.symlink_to(tmp_path / "wrong-account" / "auth.json")
    else:
        os.mkfifo(workspace_auth)

    result = _run_codex_provider_auth_link(provider_root, workspace_home)

    assert result.returncode != 0
    if unsafe_kind == "wrong_link":
        assert workspace_auth.is_symlink()
        assert workspace_auth.readlink() == tmp_path / "wrong-account" / "auth.json"
    else:
        assert stat.S_ISFIFO(workspace_auth.stat().st_mode)


def test_bound_claude_account_does_not_apply_codex_runtime_metadata_contract(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    (account_home / "claude").mkdir(parents=True)
    calls: list[tuple[str | None, list[str]]] = []

    def fake_docker_exec(
        container_name,
        command,
        *,
        env=None,
        cwd=None,
        detach=False,
        fire_and_forget=False,
        user=None,
    ):
        calls.append((user, command))
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]
    manager._grant_provider_account_access(  # type: ignore[attr-defined]
        "wpr-test",
        {
            "_glasshive_provider_account_bound": True,
            "_glasshive_provider_account_mount_host": str(account_home),
            "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
            "_glasshive_provider_account_env": {
                "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/workspace/.provider-account/claude",
            },
        },
    )

    assert calls[0][0] == manager.user
    assert calls[0][1][:2] == ["python3", "-c"]
    grant_script = calls[1][1][-1]
    assert "installation_id" not in grant_script
    assert "/tmp/arg0" not in grant_script
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" in grant_script
    assert len(calls) == 3


def test_bound_codex_account_requires_the_exact_persistent_workspace_home(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    (account_home / "other").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="exact persistent workspace home"):
        manager._grant_provider_account_access(  # type: ignore[attr-defined]
            "wpr-test",
            {
                "_glasshive_provider_account_bound": True,
                "_glasshive_provider_account_mount_host": str(account_home),
                "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
                "_glasshive_provider_account_env": {
                    "CODEX_HOME": "/workspace/.provider-account/other"
                },
            },
        )


def test_bound_codex_account_fails_closed_when_auth_link_cannot_be_prepared(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    (account_home / "codex").mkdir(parents=True)
    calls: list[tuple[str | None, list[str]]] = []

    def fake_docker_exec(
        container_name,
        command,
        *,
        env=None,
        cwd=None,
        detach=False,
        fire_and_forget=False,
        user=None,
    ):
        calls.append((user, command))
        return subprocess.CompletedProcess(
            ["docker"], 1 if command[:2] == ["python3", "-c"] else 0, "", ""
        )

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="unsafe authentication state"):
        manager._grant_provider_account_access(  # type: ignore[attr-defined]
            "wpr-test",
            {
                "_glasshive_provider_account_bound": True,
                "_glasshive_provider_account_mount_host": str(account_home),
                "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
                "_glasshive_provider_account_env": {
                    "CODEX_HOME": "/workspace/.wpr-home/.codex"
                },
            },
        )

    assert len(calls) == 1
    assert calls[0][0] == "root"


def test_provider_account_mount_requires_private_binder_marker(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-untrusted"
    account_home.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="not validated by xPerfect"):
        manager._provider_account_mount(  # type: ignore[attr-defined]
            {
                "_glasshive_provider_account_mount_host": str(account_home),
                "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
            }
        )


def test_sandbox_network_isolation_detects_default_bridge_and_accepts_shared_icc_disabled_network(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    common = {
        "container_name": "wpr-wrk-test",
        "container_id": "cid",
        "state": "ready",
        "workspace_dir": str(tmp_path / "workspace"),
        "home_dir": str(tmp_path / "home"),
        "pid": 123,
        "image": "img",
    }

    assert manager._sandbox_needs_network_recreate(  # type: ignore[attr-defined]
        "wrk_test", SandboxInfo(**common, networks=("bridge",))
    ) is True
    assert manager._sandbox_needs_network_recreate(  # type: ignore[attr-defined]
        "wrk_test",
        SandboxInfo(
            **common,
            networks=(manager._network_name_for_container("wpr-wrk-test"),),
        ),
    ) is False
    assert manager._network_name_for_container("wpr-one") == manager._network_name_for_container("wpr-two")


def test_stale_provider_account_mount_is_removed_before_reuse(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    account_home.mkdir(parents=True)
    commands: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        commands.append(list(args))
        if args[:2] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(["docker", *args], 0, stdout="cid-stale\n", stderr="")
        if args and args[0] == "inspect":
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "cid-stale",
                            "Name": "/wpr-stale-worker",
                            "Mounts": [
                                {
                                    "Source": str(account_home.resolve()),
                                    "Destination": "/workspace/.provider-account",
                                }
                            ],
                        }
                    ]
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(["docker", *args], 0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    removed = manager.terminate_containers_mounting_provider_account(account_home)

    assert removed == ["wpr-stale-worker"]
    assert ["rm", "-f", "cid-stale"] in commands
    assert any(command[:2] == ["network", "rm"] for command in commands)


def test_absent_provider_account_home_never_invokes_docker(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._docker = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("Docker must not be called for an absent account home")
    )

    assert manager.terminate_containers_mounting_provider_account(
        tmp_path / "provider-accounts" / "absent"
    ) == []
    manager.repair_provider_account_access(tmp_path / "provider-accounts" / "absent")


def test_provider_account_repair_is_bounded_and_uses_rootless_namespace(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    account_home.mkdir(parents=True)
    commands: list[list[str]] = []
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._docker = lambda args, **_kwargs: (  # type: ignore[method-assign]
        commands.append(list(args))
        or subprocess.CompletedProcess(["docker", *args], 0, stdout="", stderr="")
    )

    manager.repair_provider_account_access(tmp_path / "provider-accounts" / "absent")
    manager.repair_provider_account_access(account_home)

    assert len(commands) == 1
    command = commands[0]
    assert command[:3] == ["run", "--rm", "--network"]
    assert [command[index + 1] for index, value in enumerate(command) if value == "--cap-add"] == [
        "CHOWN", "DAC_OVERRIDE", "FOWNER"
    ]
    assert "no-new-privileges" in command
    repair_mount = f"type=bind,src={account_home.resolve()},dst=/workspace/.provider-account"
    assert repair_mount in command
    assert not any(value.endswith(",rw") for value in command)
    script = command[-1]
    assert "-xdev -type l -delete" in script
    assert "-type f -links +1" in script
    assert "! -type d ! -type f" in script
    assert "chown 0:0" in script
    assert "chmod 0700" in script and "chmod 0600" in script
    assert "setfacl -b" in script and "setfacl -k" in script


def test_provider_account_seal_unlinks_entries_before_validating_unsafe_file_types():
    script = _provider_account_seal_script("/workspace/.provider-account")
    first_mutation = min(script.index("chown 0:0"), script.index("chmod 0700"))
    assert script.index("-type l -delete") < script.index("-links +1")
    assert script.index("-links +1") < first_mutation
    assert script.index("! -type d ! -type f") < first_mutation


def test_provider_account_seal_unlinks_symlink_without_following_target(tmp_path):
    account_home = tmp_path / "account"
    account_home.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("preserve", encoding="utf-8")
    wrapper = account_home / "wrapper"
    wrapper.symlink_to(external)
    command_bin = tmp_path / "bin"
    command_bin.mkdir()
    for command in ("setfacl", "chown", "chmod"):
        shim = command_bin / command
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", _provider_account_seal_script(str(account_home))],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{command_bin}:{os.environ.get('PATH', '')}"},
    )

    assert result.returncode == 0, result.stderr
    assert not wrapper.exists() and not wrapper.is_symlink()
    assert external.read_text(encoding="utf-8") == "preserve"


def test_parallel_clean_room_container_command_is_hardened_and_internal_network_scoped(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER",
        "glasshive-provider-egress",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER",
        "glasshive-capability-broker-proxy",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK",
        "provider-egress",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    captured: list[list[str]] = []

    def fake_docker(args: list[str], **kwargs):
        captured.append(args)
        return subprocess.CompletedProcess(
            ["docker", *args], returncode=0, stdout="cid", stderr=""
        )

    manager._docker = fake_docker  # type: ignore[method-assign]
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"

    manager._create_container(
        "wpr-clean-room",
        {"workspace_dir": workspace_dir, "home_dir": home_dir},
        execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    )

    command = captured[-1]
    assert command[0] == "create"
    assert "-d" not in command
    mission_network = manager._parallel_clean_room_mission_network_name(
        "wpr-clean-room"
    )
    assert command[command.index("--network") + 1] == mission_network
    assert mission_network.startswith("glasshive-parallel-clean-room-m-")
    assert command[command.index("--user") + 1] == manager.user
    assert "--pid" not in command
    assert not any(item.startswith("--pid=") for item in command)
    assert "--ipc=private" in command
    assert "--uts" not in command
    assert not any(item.startswith("--uts=") for item in command)
    assert "--cgroupns=private" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in command
    assert "no-new-privileges:true" in command
    assert "--add-host" not in command
    assert not any("host-gateway" in item for item in command)
    assert not any("seccomp=unconfined" in item for item in command)
    # Docker does not materialize published host ports for an --internal
    # network. Clean-room workstations stay on their isolated mission network;
    # the owner-scoped Glass Drive work view is the supported operator surface.
    assert "-p" not in command
    bind_mounts = [
        command[index + 1]
        for index, item in enumerate(command)
        if item == "--mount"
    ]
    assert bind_mounts == [
        (
            f"type=bind,src={workspace_dir},dst={manager.workspace_mount},"
            "bind-propagation=rprivate"
        ),
        (
            f"type=bind,src={home_dir},dst={manager.home_mount},"
            "bind-propagation=rprivate"
        ),
    ]
    assert f"HTTP_PROXY=http://provider-egress:8080" in command
    assert f"HTTPS_PROXY=http://provider-egress:8080" in command
    assert (
        "NO_PROXY=provider-egress,host.docker.internal,localhost,127.0.0.1"
        in command
    )
    assert (
        f"com.viventium.parallel-clean-room.policy={PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}"
        in command
    )
    tmpfs_targets = {
        command[index + 1].split(":", 1)[0]
        for index, item in enumerate(command)
        if item == "--tmpfs"
    }
    assert tmpfs_targets == {
        "/tmp",
        "/run",
        "/run/glasshive",
        "/run/screen",
        "/var/tmp",
        "/var/log/supervisor",
        "/opt/selenium/logs",
        "/opt/selenium/assets",
    }
    assert "-v" not in command

def test_parallel_clean_room_network_names_are_stable_and_mission_unique(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    first = manager._parallel_clean_room_mission_network_name("wpr-worker-a")
    replay = manager._parallel_clean_room_mission_network_name("wpr-worker-a")
    second = manager._parallel_clean_room_mission_network_name("wpr-worker-b")

    assert first == replay
    assert first != second
    assert re.fullmatch(r"glasshive-parallel-clean-room-m-[0-9a-f]{16}", first)
    assert len(first) <= 128

def test_parallel_clean_room_creates_exact_proxy_only_mission_network_before_worker(
    tmp_path, monkeypatch
):
    base_network = "glasshive-parallel-clean-room"
    provider = "glasshive-provider-egress"
    broker = "glasshive-capability-broker-proxy"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", base_network)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", provider)
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", broker)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    container_name = "wpr-worker-isolated"
    mission_network = manager._parallel_clean_room_mission_network_name(container_name)
    calls: list[list[str]] = []
    network_created = False
    connected: set[str] = set()

    def fake_docker(args, **_kwargs):
        nonlocal network_created
        calls.append(args)
        if args == ["network", "inspect", mission_network]:
            if not network_created:
                return subprocess.CompletedProcess(args, 1, "", "not found")
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Name": mission_network,
                            "Driver": "bridge",
                            "Internal": True,
                            "Labels": {
                                "com.viventium.parallel-clean-room.policy": (
                                    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                                ),
                                "com.viventium.parallel-clean-room.role": "mission-network",
                                "com.viventium.parallel-clean-room.worker-container": container_name,
                            },
                            "Containers": {
                                ("a" * 64 if name == provider else "b" * 64): {
                                    "Name": name
                                }
                                for name in sorted(connected)
                            },
                        }
                    ]
                ),
                "",
            )
        if args[0:2] == ["network", "create"]:
            network_created = True
            return subprocess.CompletedProcess(args, 0, mission_network, "")
        if args[0:2] == ["network", "connect"]:
            connected.add(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ["inspect", provider]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Id": "a" * 64,
                            "NetworkSettings": {
                                "Networks": {
                                    mission_network: {
                                        "Aliases": [provider, "provider-egress"]
                                    }
                                }
                            },
                        }
                    ]
                ),
                "",
            )
        if args == ["inspect", broker]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Id": "b" * 64,
                            "NetworkSettings": {
                                "Networks": {
                                    mission_network: {
                                        "Aliases": [
                                            broker,
                                            "host.docker.internal",
                                        ]
                                    }
                                }
                            },
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]

    assert manager._ensure_parallel_clean_room_mission_network(container_name) == (
        mission_network
    )
    assert [
        "network",
        "connect",
        "--alias",
        "provider-egress",
        mission_network,
        provider,
    ] in calls
    assert [
        "network",
        "connect",
        "--alias",
        "host.docker.internal",
        mission_network,
        broker,
    ] in calls

def test_parallel_clean_room_network_creation_pressure_is_retryable_capacity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER",
        "glasshive-provider-egress",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER",
        "glasshive-capability-broker-proxy",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    container_name = "wpr-worker-network-capacity"
    mission_network = manager._parallel_clean_room_mission_network_name(container_name)

    def fake_docker(args, **_kwargs):
        if args == ["network", "inspect", mission_network]:
            return subprocess.CompletedProcess(args, 1, "", "not found")
        if args[0:2] == ["network", "create"]:
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                "all predefined address pools have been fully subnetted",
            )
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(HostCapacityError) as captured:
        manager._ensure_parallel_clean_room_mission_network(container_name)
    assert captured.value.failure_class == "host_capacity"
    assert captured.value.capacity_class == "docker_network"

def test_parallel_clean_room_network_capacity_reclaims_orphans_then_retries(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER",
        "glasshive-provider-egress",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER",
        "glasshive-capability-broker-proxy",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    container_name = "wpr-worker-network-recovery"
    mission_network = manager._parallel_clean_room_mission_network_name(container_name)
    orphan_worker = "wpr-worker-network-orphan"
    orphan_network = manager._parallel_clean_room_mission_network_name(orphan_worker)
    provider = "glasshive-provider-egress"
    broker = "glasshive-capability-broker-proxy"
    ids = {provider: "a" * 64, broker: "b" * 64}
    create_attempts = 0
    networks = {
        orphan_network: {"exists": True, "worker": orphan_worker, "connected": set()},
        mission_network: {"exists": False, "worker": container_name, "connected": set()},
    }
    calls: list[list[str]] = []

    def network_payload(network_name):
        state = networks[network_name]
        return json.dumps(
            [
                {
                    "Name": network_name,
                    "Driver": "bridge",
                    "Internal": True,
                    "Labels": {
                        "com.viventium.parallel-clean-room.policy": (
                            PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                        ),
                        "com.viventium.parallel-clean-room.role": "mission-network",
                        "com.viventium.parallel-clean-room.worker-container": state[
                            "worker"
                        ],
                    },
                    "Containers": {
                        ids[name]: {"Name": name}
                        for name in sorted(state["connected"])
                    },
                }
            ]
        )

    def fake_docker(args, **_kwargs):
        nonlocal create_attempts
        calls.append(args)
        if args[0:2] == ["network", "ls"]:
            listed = [name for name, state in networks.items() if state["exists"]]
            return subprocess.CompletedProcess(args, 0, "\n".join(listed), "")
        if args[0:2] == ["network", "inspect"] and args[-1] in networks:
            state = networks[args[-1]]
            if not state["exists"]:
                return subprocess.CompletedProcess(args, 1, "", "not found")
            return subprocess.CompletedProcess(args, 0, network_payload(args[-1]), "")
        if args[0:2] == ["network", "create"]:
            assert args[-1] == mission_network
            create_attempts += 1
            if networks[orphan_network]["exists"]:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    "all predefined address pools have been fully subnetted",
                )
            networks[mission_network]["exists"] = True
            return subprocess.CompletedProcess(args, 0, mission_network, "")
        if args == ["inspect", orphan_worker]:
            return subprocess.CompletedProcess(args, 1, "", "No such object")
        if args[0:2] == ["network", "connect"]:
            networks[args[-2]]["connected"].add(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0:3] == ["network", "disconnect", "-f"]:
            networks[args[-2]]["connected"].remove(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0:2] == ["network", "rm"]:
            assert not networks[args[-1]]["connected"]
            networks[args[-1]]["exists"] = False
            return subprocess.CompletedProcess(args, 0, args[-1], "")
        if args in (["inspect", provider], ["inspect", broker]):
            proxy = args[-1]
            role = "provider-proxy" if proxy == provider else "broker-proxy"
            alias = (
                "provider-egress"
                if proxy == provider
                else "host.docker.internal"
            )
            attachments = {
                name: {"Aliases": [proxy, alias]}
                for name, state in networks.items()
                if state["exists"] and proxy in state["connected"]
            }
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Id": ids[proxy],
                            "Config": {
                                "Labels": {
                                    "com.viventium.parallel-clean-room.policy": (
                                        PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                                    ),
                                    "com.viventium.parallel-clean-room.role": role,
                                }
                            },
                            "NetworkSettings": {"Networks": attachments},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]

    assert manager._ensure_parallel_clean_room_mission_network(container_name) == (
        mission_network
    )
    assert create_attempts == 2
    assert networks[orphan_network]["exists"] is False
    assert networks[mission_network]["connected"] == {provider, broker}
    assert ["network", "rm", orphan_network] in calls

def test_parallel_clean_room_repairs_missing_proxy_attachments_after_proxy_restart(
    tmp_path, monkeypatch
):
    base_network = "glasshive-parallel-clean-room"
    provider = "glasshive-provider-egress"
    broker = "glasshive-capability-broker-proxy"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", base_network)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", provider)
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", broker)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    container_name = "wpr-worker-restarted-proxies"
    mission_network = manager._parallel_clean_room_mission_network_name(container_name)
    calls: list[list[str]] = []
    connected: set[str] = set()
    ids = {provider: "a" * 64, broker: "b" * 64}

    def fake_docker(args, **_kwargs):
        calls.append(args)
        if args == ["network", "inspect", mission_network]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Name": mission_network,
                            "Driver": "bridge",
                            "Internal": True,
                            "Labels": {
                                "com.viventium.parallel-clean-room.policy": (
                                    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                                ),
                                "com.viventium.parallel-clean-room.role": "mission-network",
                                "com.viventium.parallel-clean-room.worker-container": container_name,
                            },
                            "Containers": {
                                ids[name]: {"Name": name} for name in sorted(connected)
                            },
                        }
                    ]
                ),
                "",
            )
        if args[0:2] == ["network", "connect"]:
            connected.add(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ["inspect", provider]:
            aliases = [provider, "provider-egress"]
        elif args == ["inspect", broker]:
            aliases = [broker, "host.docker.internal"]
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                [
                    {
                        "Id": ids[args[-1]],
                        "NetworkSettings": {
                            "Networks": {mission_network: {"Aliases": aliases}}
                        },
                    }
                ]
            ),
            "",
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    assert manager._ensure_parallel_clean_room_mission_network(container_name) == (
        mission_network
    )
    assert connected == {provider, broker}
    assert not any(args[0:2] == ["network", "create"] for args in calls)

def test_parallel_clean_room_repairs_all_attested_mission_networks_after_proxy_recreate(
    tmp_path, monkeypatch
):
    base_network = "glasshive-parallel-clean-room"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", base_network)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER",
        "glasshive-provider-egress",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER",
        "glasshive-capability-broker-proxy",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK",
        "provider-egress",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    workers = ("wpr-worker-restart-a", "wpr-worker-restart-b")
    networks = tuple(
        manager._parallel_clean_room_mission_network_name(worker)
        for worker in workers
    )
    repaired: list[str] = []

    def fake_docker(args, **_kwargs):
        if args[0:2] == ["network", "ls"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(networks), "")
        if args[0:2] == ["network", "inspect"] and args[-1] in networks:
            worker = workers[networks.index(args[-1])]
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Name": args[-1],
                            "Driver": "bridge",
                            "Internal": True,
                            "Labels": {
                                "com.viventium.parallel-clean-room.policy": (
                                    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                                ),
                                "com.viventium.parallel-clean-room.role": "mission-network",
                                "com.viventium.parallel-clean-room.worker-container": worker,
                            },
                            "Containers": {},
                        }
                    ]
                ),
                "",
            )
        if args[0:1] == ["inspect"] and args[-1] in workers:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps([{"Id": "c" * 64}]),
                "",
            )
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._ensure_parallel_clean_room_mission_network = (  # type: ignore[method-assign]
        lambda worker: repaired.append(worker) or networks[workers.index(worker)]
    )

    assert manager.repair_parallel_clean_room_mission_networks() == tuple(
        sorted(networks)
    )
    assert repaired == [
        workers[networks.index(network)] for network in sorted(networks)
    ]

def test_parallel_clean_room_repair_removes_attested_orphan_mission_network(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER",
        "glasshive-provider-egress",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER",
        "glasshive-capability-broker-proxy",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    worker = "wpr-worker-stale"
    network = manager._parallel_clean_room_mission_network_name(worker)
    provider = "glasshive-provider-egress"
    broker = "glasshive-capability-broker-proxy"
    ids = {provider: "a" * 64, broker: "b" * 64}
    connected: set[str] = set()
    network_exists = True
    calls: list[list[str]] = []

    def network_payload():
        return json.dumps(
            [
                {
                    "Name": network,
                    "Driver": "bridge",
                    "Internal": True,
                    "Labels": {
                        "com.viventium.parallel-clean-room.policy": (
                            PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                        ),
                        "com.viventium.parallel-clean-room.role": "mission-network",
                        "com.viventium.parallel-clean-room.worker-container": worker,
                    },
                    "Containers": {
                        ids[name]: {"Name": name} for name in sorted(connected)
                    },
                }
            ]
        )

    def fake_docker(args, **_kwargs):
        nonlocal network_exists
        calls.append(args)
        if args[0:2] == ["network", "ls"]:
            listed = f"{network}\n" if network_exists else ""
            return subprocess.CompletedProcess(args, 0, listed, "")
        if args == ["network", "inspect", network]:
            if not network_exists:
                return subprocess.CompletedProcess(args, 1, "", "No such network")
            return subprocess.CompletedProcess(args, 0, network_payload(), "")
        if args == ["inspect", worker]:
            return subprocess.CompletedProcess(args, 1, "", "No such object")
        if args[0:2] == ["network", "connect"]:
            connected.add(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0:3] == ["network", "disconnect", "-f"]:
            connected.remove(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ["network", "rm", network]:
            assert connected == set()
            network_exists = False
            return subprocess.CompletedProcess(args, 0, network, "")
        if args in (["inspect", provider], ["inspect", broker]):
            proxy = args[-1]
            role = "provider-proxy" if proxy == provider else "broker-proxy"
            alias = (
                "provider-egress"
                if proxy == provider
                else "host.docker.internal"
            )
            attachments = (
                {network: {"Aliases": [proxy, alias]}}
                if proxy in connected
                else {}
            )
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Id": ids[proxy],
                            "Config": {
                                "Labels": {
                                    "com.viventium.parallel-clean-room.policy": (
                                        PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                                    ),
                                    "com.viventium.parallel-clean-room.role": role,
                                }
                            },
                            "NetworkSettings": {"Networks": attachments},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]

    assert manager.repair_parallel_clean_room_mission_networks() == ()
    assert network_exists is False
    assert connected == set()
    assert ["network", "rm", network] in calls

def test_parallel_clean_room_repair_ignores_stale_foreign_namespace_network(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER",
        "glasshive-provider-egress",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER",
        "glasshive-capability-broker-proxy",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    worker = "wpr-worker-current"
    current_network = manager._parallel_clean_room_mission_network_name(worker)
    stale_worker = "wpr-worker-from-another-runtime"
    stale_network = "viventium-parallel-old-runtime-m-deadbeefdeadbeef"
    repaired: list[str] = []

    def fake_docker(args, **_kwargs):
        if args[0:2] == ["network", "ls"]:
            return subprocess.CompletedProcess(
                args, 0, f"{stale_network}\n{current_network}\n", ""
            )
        if args[0:2] == ["network", "inspect"]:
            network_name = args[-1]
            network_worker = (
                worker if network_name == current_network else stale_worker
            )
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Name": network_name,
                            "Driver": "bridge",
                            "Internal": True,
                            "Labels": {
                                "com.viventium.parallel-clean-room.policy": (
                                    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                                ),
                                "com.viventium.parallel-clean-room.role": (
                                    "mission-network"
                                ),
                                "com.viventium.parallel-clean-room.worker-container": (
                                    network_worker
                                ),
                            },
                            "Containers": {},
                        }
                    ]
                ),
                "",
            )
        if args == ["inspect", worker]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps([{"Id": "c" * 64}]),
                "",
            )
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._ensure_parallel_clean_room_mission_network = (  # type: ignore[method-assign]
        lambda worker_name: repaired.append(worker_name) or current_network
    )

    assert manager.repair_parallel_clean_room_mission_networks() == (
        current_network,
    )
    assert repaired == [worker]

def test_parallel_clean_room_rejects_foreign_peer_on_mission_network(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER",
        "glasshive-provider-egress",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER",
        "glasshive-capability-broker-proxy",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    container_name = "wpr-worker-peer-fence"
    network_name = manager._parallel_clean_room_mission_network_name(container_name)
    payload = [
        {
            "Name": network_name,
            "Driver": "bridge",
            "Internal": True,
            "Labels": {
                "com.viventium.parallel-clean-room.policy": (
                    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                ),
                "com.viventium.parallel-clean-room.role": "mission-network",
                "com.viventium.parallel-clean-room.worker-container": container_name,
            },
            "Containers": {
                "a" * 64: {"Name": "glasshive-provider-egress"},
                "b" * 64: {"Name": "glasshive-capability-broker-proxy"},
                "c" * 64: {"Name": "foreign-worker"},
            },
        }
    ]
    manager._docker = lambda args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args, 0, json.dumps(payload), ""
    )

    with pytest.raises(RuntimeError, match="foreign endpoint"):
        manager._ensure_parallel_clean_room_mission_network(container_name)

def test_parallel_clean_room_rejects_missing_exact_proxy_alias_on_mission_network(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER",
        "glasshive-provider-egress",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER",
        "glasshive-capability-broker-proxy",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    container_name = "wpr-worker-alias-fence"
    network_name = manager._parallel_clean_room_mission_network_name(container_name)
    network_payload = [
        {
            "Name": network_name,
            "Driver": "bridge",
            "Internal": True,
            "Labels": {
                "com.viventium.parallel-clean-room.policy": (
                    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                ),
                "com.viventium.parallel-clean-room.role": "mission-network",
                "com.viventium.parallel-clean-room.worker-container": container_name,
            },
            "Containers": {
                "a" * 64: {"Name": "glasshive-provider-egress"},
                "b" * 64: {"Name": "glasshive-capability-broker-proxy"},
            },
        }
    ]

    def fake_docker(args, **_kwargs):
        if args[:2] == ["network", "inspect"]:
            payload = network_payload
        elif args == ["inspect", "glasshive-provider-egress"]:
            payload = [
                {
                    "Id": "a" * 64,
                    "NetworkSettings": {
                        "Networks": {network_name: {"Aliases": ["wrong-alias"]}}
                    },
                }
            ]
        elif args == ["inspect", "glasshive-capability-broker-proxy"]:
            payload = [
                {
                    "Id": "b" * 64,
                    "NetworkSettings": {
                        "Networks": {
                            network_name: {
                                "Aliases": ["host.docker.internal"]
                            }
                        }
                    },
                }
            ]
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="proxy alias could not be attested"):
        manager._ensure_parallel_clean_room_mission_network(container_name)

def test_parallel_clean_room_accepts_null_aliases_for_exact_worker_endpoint(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    provider = "glasshive-provider-egress"
    broker = "glasshive-capability-broker-proxy"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", provider)
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", broker)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    container_name = "wpr-worker-null-aliases"
    network_name = manager._parallel_clean_room_mission_network_name(container_name)
    ids = {
        provider: "a" * 64,
        broker: "b" * 64,
        container_name: "c" * 64,
    }

    def fake_docker(args, **_kwargs):
        if args == ["network", "inspect", network_name]:
            payload = [
                {
                    "Name": network_name,
                    "Driver": "bridge",
                    "Internal": True,
                    "Labels": {
                        "com.viventium.parallel-clean-room.policy": (
                            PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                        ),
                        "com.viventium.parallel-clean-room.role": "mission-network",
                        "com.viventium.parallel-clean-room.worker-container": container_name,
                    },
                    "Containers": {
                        container_id: {"Name": member_name}
                        for member_name, container_id in ids.items()
                    },
                }
            ]
        elif args == ["inspect", provider]:
            payload = [
                {
                    "Id": ids[provider],
                    "NetworkSettings": {
                        "Networks": {
                            network_name: {"Aliases": [provider, "provider-egress"]}
                        }
                    },
                }
            ]
        elif args == ["inspect", broker]:
            payload = [
                {
                    "Id": ids[broker],
                    "NetworkSettings": {
                        "Networks": {
                            network_name: {
                                "Aliases": [broker, "host.docker.internal"]
                            }
                        }
                    },
                }
            ]
        elif args == ["inspect", container_name]:
            payload = [
                {
                    "Id": ids[container_name],
                    "NetworkSettings": {
                        "Networks": {network_name: {"Aliases": None}}
                    },
                }
            ]
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    manager._docker = fake_docker  # type: ignore[method-assign]

    assert manager._ensure_parallel_clean_room_mission_network(container_name) == (
        network_name
    )

def test_legacy_container_command_does_not_force_clean_room_identity_or_namespaces(
    tmp_path
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    captured: list[list[str]] = []
    manager._docker = lambda args, **_kwargs: (  # type: ignore[method-assign]
        captured.append(args)
        or subprocess.CompletedProcess(args, returncode=0, stdout="cid", stderr="")
    )

    manager._create_container(
        "wpr-legacy",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    command = captured[-1]
    assert "--user" not in command
    assert "--pid=private" not in command
    assert "--ipc=private" not in command

@pytest.mark.parametrize("runtime_user", ["root", "0", "worker"])
def test_parallel_clean_room_creation_rejects_noncanonical_runtime_user(
    tmp_path, monkeypatch, runtime_user
):
    monkeypatch.setenv("WPR_SANDBOX_USER", runtime_user)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._docker = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("unsafe clean-room identity must fail before Docker")
    )

    with pytest.raises(RuntimeError, match="non-root seluser"):
        manager._create_container(
            "wpr-clean-room",
            {
                "workspace_dir": tmp_path / "workspace",
                "home_dir": tmp_path / "home",
            },
            execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        )

def test_parallel_clean_room_container_creation_fails_closed_without_network_policy(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", raising=False)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL", "http://provider-egress:8080"
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._docker = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("Docker must not run without the dedicated internal network")
    )

    with pytest.raises(RuntimeError, match="dedicated internal network"):
        manager._create_container(
            "wpr-clean-room",
            {
                "workspace_dir": tmp_path / "workspace",
                "home_dir": tmp_path / "home",
            },
            execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        )

def test_describe_self_heals_novnc_when_service_port_resets(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, object]] = []

    sandbox = SandboxInfo(
        container_name="wpr-wrk-test",
        container_id="cid",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=1234,
        image="img",
        novnc_port=57900,
        selenium_port=57901,
        openclaw_port=57902,
    )
    manager.inspect = lambda worker_id: sandbox  # type: ignore[method-assign]

    readiness = iter([False, True])
    manager._novnc_http_ready = lambda port: next(readiness)  # type: ignore[method-assign]

    def fake_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(("exec", command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_exec  # type: ignore[method-assign]

    details = manager.describe("wrk_test")

    assert details["view_available"] is True
    view_url = str(details["view_url"])
    assert view_url.startswith("http://127.0.0.1:57900/?")
    assert "autoconnect=1" in view_url
    assert "password=" in view_url
    assert details["view_health"] == {"healthy": True, "repaired": True, "reason": "ok"}
    assert calls
    repair_script = str(calls[0][1])
    assert "TMPDIR=/tmp" in repair_script
    assert manager._browser_tmp_dir() not in repair_script


def test_container_uses_unique_required_desktop_and_grid_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    commands: list[list[str]] = []
    secret_environments: list[dict[str, str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        commands.append(list(args))
        if args and args[0] == "run":
            env_path = Path(args[args.index("--env-file") + 1])
            assert env_path.stat().st_mode & 0o777 == 0o600
            secret_environments.append(
                dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines())
            )
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="container-id", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._ensure_isolated_network = lambda _name: "wpr-wrk-one-net"  # type: ignore[method-assign]
    paths_one = manager.paths("wrk_one")
    paths_two = manager.paths("wrk_two")
    manager._ensure_host_dirs(paths_one)  # type: ignore[attr-defined]
    manager._ensure_host_dirs(paths_two)  # type: ignore[attr-defined]

    for paths in (paths_one, paths_two):
        for key in ("worker_root", "state_dir", "workspace_dir", "home_dir"):
            assert paths[key].stat().st_mode & 0o777 == 0o700

    manager._create_container("wpr-wrk-one", paths_one, worker={"worker_id": "wrk_one"})  # type: ignore[attr-defined]
    manager._create_container("wpr-wrk-two", paths_two, worker={"worker_id": "wrk_two"})  # type: ignore[attr-defined]

    run_commands = [command for command in commands if command and command[0] == "run"]
    assert len(run_commands) == 2

    def env_values(command: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for index, value in enumerate(command[:-1]):
            if value == "-e" and "=" in command[index + 1]:
                key, raw = command[index + 1].split("=", 1)
                result[key] = raw
        return result

    first = env_values(run_commands[0])
    assert first["SE_VNC_NO_PASSWORD"] == "0"
    assert first["SE_ROUTER_USERNAME"]
    assert first["SE_MASK_SECRETS"] == "true"
    assert "SE_VNC_PASSWORD" not in first
    assert "SE_ROUTER_PASSWORD" not in first
    assert all("SE_VNC_PASSWORD=" not in value for value in run_commands[0])
    assert all("SE_ROUTER_PASSWORD=" not in value for value in run_commands[0])
    assert secret_environments[0]["SE_VNC_PASSWORD"] != secret_environments[1]["SE_VNC_PASSWORD"]
    assert len(secret_environments[0]["SE_VNC_PASSWORD"]) == 8
    assert set(secret_environments[0]["SE_VNC_PASSWORD"]).issubset(set(VNC_PASSWORD_ALPHABET))
    assert len(set(VNC_PASSWORD_ALPHABET)) >= 64
    assert secret_environments[0]["SE_ROUTER_PASSWORD"] != secret_environments[1]["SE_ROUTER_PASSWORD"]
    assert not Path(run_commands[0][run_commands[0].index("--env-file") + 1]).exists()
    assert (paths_one["state_dir"] / "desktop-credentials.json").stat().st_mode & 0o777 == 0o600
    assert (paths_one["home_dir"] / ".vnc").is_dir()
    assert (paths_one["home_dir"] / ".vnc").stat().st_mode & 0o777 == 0o700


def test_local_running_sandbox_readiness_keeps_container_acl_mask(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    paths = manager.paths("wrk_local")
    paths["workspace_dir"].mkdir(parents=True)
    paths["home_dir"].mkdir(parents=True)
    for key in ("worker_root", "state_dir", "workspace_dir", "home_dir"):
        paths[key].chmod(0o770)

    sandbox = SandboxInfo(
        container_name="wpr-wrk-local",
        container_id="cid",
        state="running",
        workspace_dir=str(paths["workspace_dir"]),
        home_dir=str(paths["home_dir"]),
        pid=1234,
        image="img",
    )
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox  # type: ignore[method-assign]
    manager._sandbox_needs_chromium_userns_recreate = lambda resolved: False  # type: ignore[method-assign]
    manager._sandbox_needs_network_recreate = lambda worker_id, resolved: False  # type: ignore[method-assign]
    manager._sandbox_needs_provider_mount_recreate = lambda worker, resolved: False  # type: ignore[method-assign]
    manager._sandbox_needs_desktop_auth_recreate = lambda worker_id, resolved: False  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args: pytest.fail("local running sandbox was unexpectedly repaired")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: None  # type: ignore[method-assign]

    resolved = manager.ensure_ready({"worker_id": "wrk_local"}, "codex-cli")

    assert resolved is sandbox
    for key in ("worker_root", "state_dir", "workspace_dir", "home_dir"):
        assert paths[key].stat().st_mode & 0o777 == 0o770
    assert (paths["home_dir"] / ".vnc").stat().st_mode & 0o777 == 0o700


def test_passwordless_desktop_requires_explicit_insecure_local_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_SANDBOX_VNC_NO_PASSWORD", "true")
    with pytest.raises(RuntimeError, match="passwordless desktop"):
        DockerSandboxManager(base_dir=str(tmp_path))

    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "single_user")
    monkeypatch.setenv("WPR_ALLOW_INSECURE_LOCAL_DESKTOP", "true")
    manager = DockerSandboxManager(base_dir=str(tmp_path / "allowed"))
    assert manager.vnc_no_password is True


def test_inspect_reports_paused_when_docker_state_is_paused(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    payload = [
        {
            "Id": "abc123",
            "State": {"Status": "running", "Paused": True, "Pid": 4242},
            "NetworkSettings": {
                "Ports": {
                    "7900/tcp": [{"HostIp": "127.0.0.1", "HostPort": "58100"}],
                    "4444/tcp": [{"HostIp": "127.0.0.1", "HostPort": "58101"}],
                }
            },
        }
    ]

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        return subprocess.CompletedProcess(
            ["docker", *args],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    sandbox = manager.inspect("wrk_test")
    assert sandbox is not None
    assert sandbox.state == "paused"
    assert sandbox.pid is None
    assert sandbox.novnc_port == 58100
    assert sandbox.selenium_port == 58101


def test_fast_sandbox_does_not_treat_projected_paths_as_container_evidence(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def missing_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="No such container")

    manager._docker = missing_docker  # type: ignore[method-assign]

    worker = {
        "worker_id": "wrk_projected",
        "state": "ready",
        "state_dir": str(tmp_path / "state"),
        "workspace_dir": str(tmp_path / "workspace"),
    }

    assert manager.fast_sandbox_from_worker(worker) is None


def test_fast_sandbox_rejects_stale_provider_mount_for_idle_workspace(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    sandbox = SandboxInfo(
        container_name="wpr-wrk-idle",
        container_id="container-idle",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=4242,
        image=manager.image,
        networks=(manager._network_name_for_container("wpr-wrk-idle"),),
        mounts=((str(tmp_path / "provider-account"), "/workspace/.provider-account"),),
    )
    manager.inspect = lambda worker_id: sandbox  # type: ignore[method-assign]

    assert manager.fast_sandbox_from_worker(
        {
            "worker_id": "wrk_idle",
            "container_id": "container-idle",
            "state": "ready",
        }
    ) is None


def test_ensure_ready_creates_container_when_only_projected_paths_exist(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    created: list[str] = []

    def fake_inspect(worker_id: str):
        if created:
            return SandboxInfo(
                container_name="wpr-wrk-projected",
                container_id="container123",
                state="running",
                workspace_dir=str(tmp_path / "docker_sandboxes" / "workers" / worker_id / "state" / "workspace"),
                home_dir=str(tmp_path / "docker_sandboxes" / "workers" / worker_id / "state" / "home"),
                pid=4242,
                image=manager.image,
            )
        return None

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager.inspect = fake_inspect  # type: ignore[method-assign]
    manager._create_container = lambda container_name, paths, worker=None: created.append(container_name)  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda container_name, paths: None  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: None  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: None  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: None  # type: ignore[method-assign]

    worker = {
        "worker_id": "wrk_projected",
        "state": "ready",
        "state_dir": str(tmp_path / "state"),
        "workspace_dir": str(tmp_path / "workspace"),
    }

    sandbox = manager.ensure_ready(worker, runtime_name="codex-cli")

    assert created == ["wpr-wrk-projected"]
    assert sandbox.container_id == "container123"


def test_ensure_ready_recreates_ready_container_missing_chromium_userns(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []
    removed = False
    created = False

    def fake_inspect(worker_id: str):
        if created:
            return SandboxInfo(
                container_name="wpr-wrk-test",
                container_id="container-new",
                state="running",
                workspace_dir=str(tmp_path / "workspace"),
                home_dir=str(tmp_path / "home"),
                pid=4242,
                image=manager.image,
                security_options=("seccomp=unconfined",),
            )
        if removed:
            return None
        return SandboxInfo(
            container_name="wpr-wrk-test",
            container_id="container-old",
            state="running",
            workspace_dir=str(tmp_path / "workspace"),
            home_dir=str(tmp_path / "home"),
            pid=4242,
            image=manager.image,
            security_options=(),
        )

    def fake_docker(args: list[str], **kwargs):
        nonlocal removed
        if args[:2] == ["rm", "-f"]:
            calls.append("rm")
            removed = True
            return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")
        if args[:2] == ["network", "rm"]:
            calls.append("network-rm")
            return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call: {args}")

    def fake_create_container(container_name, paths, worker=None):
        nonlocal created
        calls.append(f"create:{container_name}")
        created = True

    manager._require_docker = lambda: calls.append("require")  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append("host_dirs")  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append("seed")  # type: ignore[method-assign]
    manager.inspect = fake_inspect  # type: ignore[method-assign]
    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._ensure_image = lambda: calls.append("image")  # type: ignore[method-assign]
    manager._create_container = fake_create_container  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test", "state": "ready", "container_id": "container-old"}, "codex-cli")

    assert sandbox.container_id == "container-new"
    assert sandbox.security_options == ("seccomp=unconfined",)
    assert calls == [
        "require",
        "host_dirs",
            "seed",
            "rm",
            "network-rm",
            "image",
        "create:wpr-wrk-test",
        "writable",
        "provider_tmp",
        "harden",
        "background",
        "prime",
    ]


@pytest.mark.parametrize(
    ("worker_state", "pre_run_recreate_allowed"),
    [("ready", False), ("starting", True)],
)
def test_parallel_clean_room_removes_mismatched_generation_before_seeding_grant(
    tmp_path, monkeypatch, worker_state, pre_run_recreate_allowed
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    events: list[str] = []
    legacy_present = True
    new_container_created = False

    legacy = SandboxInfo(
        container_name="wpr-wrk-clean-transition",
        container_id="legacy-wide-network-generation",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=7331,
        image=manager.image,
        security_options=("seccomp=unconfined",),
    )
    replacement = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_transition",
        container_id="c" * 64,
    )

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_worker_permissions_migrated = lambda _root: None  # type: ignore[method-assign]

    def fake_inspect(_worker_id):
        events.append("inspect")
        if legacy_present:
            return legacy
        return replacement if new_container_created else None

    def fake_fresh_inspect(_worker_id):
        events.append("inspect_fresh")
        if legacy_present:
            return FreshSandboxInspection(status="present", sandbox=legacy)
        if new_container_created:
            return FreshSandboxInspection(status="present", sandbox=replacement)
        return FreshSandboxInspection(status="confirmed_absent")

    def fake_terminate(_worker_id, *, expected_container_id=None):
        nonlocal legacy_present
        events.append("terminate")
        assert expected_container_id == "legacy-wide-network-generation"
        legacy_present = False
        return SandboxInfo(
            container_name=str(expected_container_id),
            container_id=None,
            state="terminated",
            workspace_dir=str(tmp_path / "workspace"),
            home_dir=str(tmp_path / "home"),
            pid=None,
            image=manager.image,
        )

    def fake_seed(*_args, **_kwargs):
        events.append("seed")
        assert legacy_present is False, (
            "the old wide-network container could read the fresh broker grant"
        )

    def fake_create(*_args, **_kwargs):
        nonlocal new_container_created
        events.append("create")
        new_container_created = True

    manager.inspect = fake_inspect  # type: ignore[method-assign]
    manager.inspect_fresh = fake_fresh_inspect  # type: ignore[method-assign]
    manager.terminate = fake_terminate  # type: ignore[method-assign]
    manager._seed_bootstrap = fake_seed  # type: ignore[method-assign]
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._ensure_image = lambda: events.append("image")  # type: ignore[method-assign]
    manager._create_container = fake_create  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    manager._set_plain_background = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    result = manager.ensure_ready(
        {
            "worker_id": "wrk_clean_transition",
            "state": worker_state,
            "_pre_run_substrate_recreate_allowed": pre_run_recreate_allowed,
            "bootstrap_profile": "clean-room",
            "bootstrap_bundle_json": json.dumps(
                {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
            ),
        },
        "codex-cli",
    )

    assert result.container_id == "c" * 64
    assert events[:8] == [
        "image",
        "inspect_fresh",
        "terminate",
        "inspect_fresh",
        "create",
        "inspect_fresh",
        "inspect_fresh",
        "seed",
    ]
    assert events.index("create") < events.index("seed")

def test_parallel_clean_room_attests_existing_generation_before_safe_reuse_seed(
    tmp_path, monkeypatch
):
    network_name = "glasshive-parallel-clean-room"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", network_name)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    sandbox = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_reuse",
        container_id="attested-clean-room-generation",
    )
    events: list[str] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_worker_permissions_migrated = lambda _root: None  # type: ignore[method-assign]
    manager.inspect_fresh = lambda _worker_id: (  # type: ignore[method-assign]
        events.append("inspect_fresh")
        or FreshSandboxInspection(status="present", sandbox=sandbox)
    )
    manager._seed_bootstrap = lambda *_args, **_kwargs: events.append("seed")  # type: ignore[method-assign]
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager.terminate = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("an attested clean-room generation must be reused")
    )
    manager._ensure_image = lambda: events.append("image")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    result = manager.ensure_ready(
        {
            "worker_id": "wrk_clean_reuse",
            "state": "running",
            "bootstrap_profile": "clean-room",
            "bootstrap_bundle_json": json.dumps(
                {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
            ),
        },
        "codex-cli",
    )

    assert result.container_id == "attested-clean-room-generation"
    assert events == [
        "image",
        "inspect_fresh",
        "inspect_fresh",
        "seed",
        "inspect_fresh",
    ]

def test_parallel_clean_room_rechecks_proxy_boundary_immediately_before_seed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    sandbox = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_boundary_recheck",
        container_id="attested-clean-room-generation",
    )
    seeded: list[bool] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager.inspect_fresh = lambda _worker_id, **_kwargs: FreshSandboxInspection(  # type: ignore[method-assign]
        status="present", sandbox=sandbox
    )
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": False,
        "reason": "parallel_clean_room_provider_proxy_unhealthy",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = lambda *_args, **_kwargs: seeded.append(True)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="provider_proxy_unhealthy"):
        manager.ensure_ready(
            {
                "worker_id": "wrk_clean_boundary_recheck",
                "state": "running",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert seeded == []

def test_parallel_clean_room_rechecks_same_worker_generation_after_boundary(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    original = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_generation_swap",
        container_id="a" * 64,
    )
    replacement = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_generation_swap",
        container_id="b" * 64,
    )
    inspections = iter(
        (
            FreshSandboxInspection(status="present", sandbox=original),
            FreshSandboxInspection(status="present", sandbox=replacement),
        )
    )
    seeded: list[bool] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager.inspect_fresh = lambda _worker_id: next(inspections)  # type: ignore[method-assign]
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = lambda *_args, **_kwargs: seeded.append(True)  # type: ignore[method-assign]
    manager._ensure_worker_permissions_migrated = lambda _root: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="generation changed during boundary attestation"):
        manager.ensure_ready(
            {
                "worker_id": "wrk_clean_generation_swap",
                "state": "running",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert seeded == []

def test_parallel_clean_room_reserves_absent_generation_before_seeding_grant(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    reserved = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_absent_reservation",
        container_id="c" * 64,
    )
    events: list[str] = []
    container_reserved = False

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: events.append("image")  # type: ignore[method-assign]

    def inspect_fresh(_worker_id):
        events.append("inspect_fresh")
        if container_reserved:
            return FreshSandboxInspection(status="present", sandbox=reserved)
        return FreshSandboxInspection(status="confirmed_absent")

    def create_container(*_args, **_kwargs):
        nonlocal container_reserved
        events.append("reserve_container")
        container_reserved = True

    def seed(*_args, **_kwargs):
        events.append("seed")
        assert container_reserved, "fresh authority requires a reserved exact generation"

    manager.inspect_fresh = inspect_fresh  # type: ignore[method-assign]
    manager._create_container = create_container  # type: ignore[method-assign]
    manager.parallel_clean_room_readiness = lambda: (  # type: ignore[method-assign]
        events.append("boundary") or {"ready": True, "reason": ""}
    )
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = seed  # type: ignore[method-assign]
    manager._ensure_worker_permissions_migrated = lambda _root: None  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    manager._set_plain_background = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    result = manager.ensure_ready(
        {
            "worker_id": "wrk_clean_absent_reservation",
            "state": "ready",
            "bootstrap_bundle_json": json.dumps(
                {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
            ),
        },
        "codex-cli",
    )

    assert result.container_id == "c" * 64
    assert events == [
        "image",
        "boundary",
        "inspect_fresh",
        "reserve_container",
        "inspect_fresh",
        "boundary",
        "inspect_fresh",
        "seed",
        "inspect_fresh",
    ]

def test_parallel_clean_room_never_starts_replacement_after_reserved_seed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    reserved = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_post_seed_swap",
        container_id="a" * 64,
    )
    reserved.state = "created"
    reserved.port_bindings = tuple(
        (port, host, 0) for port, host, _published in reserved.port_bindings
    )
    replacement = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_post_seed_swap",
        container_id="b" * 64,
    )
    replacement.state = "created"
    replacement.port_bindings = tuple(
        (port, host, 0) for port, host, _published in replacement.port_bindings
    )
    container_reserved = False
    replacement_active = False
    docker_calls: list[list[str]] = []

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]

    def inspect_fresh(_worker_id):
        if replacement_active:
            return FreshSandboxInspection(status="present", sandbox=replacement)
        if container_reserved:
            return FreshSandboxInspection(status="present", sandbox=reserved)
        return FreshSandboxInspection(status="confirmed_absent")

    def create_container(*_args, **_kwargs):
        nonlocal container_reserved
        container_reserved = True

    def seed(*_args, **_kwargs):
        nonlocal replacement_active
        replacement_active = True

    manager.inspect_fresh = inspect_fresh  # type: ignore[method-assign]
    manager._create_container = create_container  # type: ignore[method-assign]
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = seed  # type: ignore[method-assign]
    manager._ensure_worker_permissions_migrated = lambda _root: None  # type: ignore[method-assign]
    manager._docker = lambda args, **_kwargs: (  # type: ignore[method-assign]
        docker_calls.append(args)
        or subprocess.CompletedProcess(args, 0, "", "")
    )
    manager.inspect = lambda _worker_id: replacement  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="generation changed after authority projection"):
        manager.ensure_ready(
            {
                "worker_id": "wrk_clean_post_seed_swap",
                "state": "ready",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert not any(call and call[0] == "start" for call in docker_calls)

def test_parallel_clean_room_rejects_every_extra_host_before_seed(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    sandbox = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_extra_host",
        container_id="attested-clean-room-generation",
    )
    sandbox.extra_hosts = ("provider-egress:203.0.113.99",)

    assert manager._sandbox_matches_parallel_clean_room_policy(sandbox) is False

@pytest.mark.parametrize("worker_state", ["running", "starting"])
def test_parallel_clean_room_never_seeds_unremovable_mismatched_generation(
    tmp_path, monkeypatch, worker_state
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    mismatched = SandboxInfo(
        container_name="wpr-wrk-clean-running",
        container_id="legacy-active-generation",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=9551,
        image=manager.image,
        security_options=("seccomp=unconfined",),
    )
    seeded: list[bool] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager.inspect_fresh = lambda _worker_id: FreshSandboxInspection(  # type: ignore[method-assign]
        status="present", sandbox=mismatched
    )
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = lambda *_args, **_kwargs: seeded.append(True)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="does not attest"):
        manager.ensure_ready(
            {
                "worker_id": "wrk_clean_running",
                "state": worker_state,
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert seeded == []

def test_parallel_clean_room_rechecks_name_after_exact_generation_removal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    original = SandboxInfo(
        container_name="wpr-wrk-clean-name-race",
        container_id="original-wide-network-generation",
        state="exited",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=None,
        image=manager.image,
        security_options=("seccomp=unconfined",),
    )
    replacement = SandboxInfo(
        container_name="wpr-wrk-clean-name-race",
        container_id="replacement-wide-network-generation",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=9552,
        image=manager.image,
        security_options=("seccomp=unconfined",),
    )
    inspections = iter(
        (
            FreshSandboxInspection(status="present", sandbox=original),
            FreshSandboxInspection(status="present", sandbox=replacement),
        )
    )
    events: list[str] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager.inspect_fresh = lambda _worker_id: (  # type: ignore[method-assign]
        events.append("inspect_fresh") or next(inspections)
    )

    def fake_terminate(_worker_id, *, expected_container_id=None):
        events.append(f"terminate:{expected_container_id}")
        return original

    manager.terminate = fake_terminate  # type: ignore[method-assign]
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = lambda *_args, **_kwargs: events.append("seed")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="generation changed during replacement"):
        manager.ensure_ready(
            {
                "worker_id": "wrk_clean_name_race",
                "state": "ready",
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert events == [
        "inspect_fresh",
        "terminate:original-wide-network-generation",
        "inspect_fresh",
    ]

def _attested_clean_room_sandbox(
    manager: DockerSandboxManager, worker_id: str, *, container_id: str
) -> SandboxInfo:
    paths = manager.paths(worker_id)
    container_name = manager._container_name(worker_id)
    mission_network = manager._parallel_clean_room_mission_network_name(container_name)
    sandbox = SandboxInfo(
        container_name=container_name,
        container_id=container_id,
        state="running",
        workspace_dir=str(paths["workspace_dir"]),
        home_dir=str(paths["home_dir"]),
        pid=9661,
        image=manager.image,
        security_options=("no-new-privileges:true",),
        execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        image_id="sha256:" + ("a" * 64),
        image_reference=manager.image,
        runtime_user=manager.user,
        entrypoint=None,
        command=("/opt/bin/entry_point.sh",),
        expected_image_id="sha256:" + ("a" * 64),
        expected_runtime_user=manager.user,
        expected_entrypoint=None,
        expected_command=("/opt/bin/entry_point.sh",),
        network_mode=mission_network,
        pid_mode="private",
        ipc_mode="private",
        uts_mode="private",
        userns_mode="",
        cgroupns_mode="private",
        read_only_rootfs=True,
        cap_drop=("ALL",),
        bind_mount_targets=(manager.workspace_mount, manager.home_mount),
        tmpfs_targets=tuple(
            value.split(":", 1)[0] for value in PARALLEL_CLEAN_ROOM_TMPFS
        ),
        tmpfs_options=tuple(
            sorted(
                (
                    value.split(":", 1)[0],
                    tuple(sorted(value.split(":", 1)[1].split(","))),
                )
                for value in PARALLEL_CLEAN_ROOM_TMPFS
            )
        ),
        port_bindings=(),
        expected_environment=(("HOME", "/home/seluser"), ("PATH", "/usr/bin")),
        environment=tuple(
            sorted(
                {
                    "PATH": "/usr/bin",
                    "HOME": manager.home_mount,
                    "TERM": manager.term_value,
                    "TMPDIR": manager.service_tmp_dir,
                    "XDG_CACHE_HOME": manager._browser_cache_dir(),
                    "XDG_CONFIG_HOME": manager._browser_config_dir(),
                    "SE_VNC_NO_PASSWORD": "1" if manager.vnc_no_password else "0",
                    "HTTP_PROXY": "http://provider-egress:8080",
                    "HTTPS_PROXY": "http://provider-egress:8080",
                    "NO_PROXY": (
                        "provider-egress,host.docker.internal,localhost,127.0.0.1"
                    ),
                }.items()
            )
        ),
    )
    sandbox.attached_networks = (mission_network,)
    sandbox.privileged = False
    sandbox.cap_add = ()
    sandbox.bind_mount_pairs = (
        (str(paths["workspace_dir"]), manager.workspace_mount),
        (str(paths["home_dir"]), manager.home_mount),
    )
    sandbox.mount_records = (
        ("bind", str(paths["workspace_dir"]), manager.workspace_mount),
        ("bind", str(paths["home_dir"]), manager.home_mount),
    )
    sandbox.bind_mount_options = (
        (
            str(paths["workspace_dir"]),
            manager.workspace_mount,
            True,
            "",
            "rprivate",
        ),
        (
            str(paths["home_dir"]),
            manager.home_mount,
            True,
            "",
            "rprivate",
        ),
    )
    return sandbox

def _clean_room_inspect_payload(
    manager: DockerSandboxManager, worker_id: str, *, container_id: object
) -> list[dict[str, object]]:
    paths = manager.paths(worker_id)
    container_name = manager._container_name(worker_id)
    mission_network = manager._parallel_clean_room_mission_network_name(container_name)
    return [
        {
            "Id": container_id,
            "Image": "sha256:" + ("a" * 64),
            "State": {"Status": "running", "Paused": False, "Pid": 9771},
            "HostConfig": {
                "NetworkMode": mission_network,
                "PidMode": "private",
                "IpcMode": "private",
                "UTSMode": "private",
                "UsernsMode": "",
                "CgroupnsMode": "private",
                "SecurityOpt": ["no-new-privileges:true"],
                "ReadonlyRootfs": True,
                "Privileged": False,
                "CapAdd": None,
                "CapDrop": ["ALL"],
                "ExtraHosts": [],
                "Tmpfs": {
                    value.split(":", 1)[0]: value.split(":", 1)[1]
                    for value in PARALLEL_CLEAN_ROOM_TMPFS
                },
            },
            "Config": {
                "Image": manager.image,
                "User": manager.user,
                "Entrypoint": None,
                "Cmd": ["/opt/bin/entry_point.sh"],
                "Env": [
                    "PATH=/usr/bin",
                    f"HOME={manager.home_mount}",
                    f"TERM={manager.term_value}",
                    f"TMPDIR={manager.service_tmp_dir}",
                    f"XDG_CACHE_HOME={manager._browser_cache_dir()}",
                    f"XDG_CONFIG_HOME={manager._browser_config_dir()}",
                    f"SE_VNC_NO_PASSWORD={'1' if manager.vnc_no_password else '0'}",
                    "HTTP_PROXY=http://provider-egress:8080",
                    "HTTPS_PROXY=http://provider-egress:8080",
                    (
                        "NO_PROXY=provider-egress,host.docker.internal,"
                        "localhost,127.0.0.1"
                    ),
                ],
                "Labels": {
                    "com.viventium.parallel-clean-room.policy": (
                        PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                    )
                }
            },
            "NetworkSettings": {
                "Ports": {},
                "Networks": {mission_network: {}},
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(paths["workspace_dir"]),
                    "Destination": manager.workspace_mount,
                    "Mode": "",
                    "RW": True,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": str(paths["home_dir"]),
                    "Destination": manager.home_mount,
                    "Mode": "",
                    "RW": True,
                    "Propagation": "rprivate",
                },
            ],
        }
    ]

def _configured_image_inspect_payload(
    manager: DockerSandboxManager,
    *,
    image_id: str = "sha256:" + ("a" * 64),
) -> list[dict[str, object]]:
    return [
        {
            "Id": image_id,
            "Config": {
                "User": manager.user,
                "Entrypoint": None,
                "Cmd": ["/opt/bin/entry_point.sh"],
                "Env": ["PATH=/usr/bin", "HOME=/home/seluser"],
            },
        }
    ]

def test_fresh_image_inspection_accepts_docker_omitted_null_entrypoint(
    tmp_path, monkeypatch
):
    """Docker Engine omits Config.Entrypoint when the image default is null."""

    worker_id = "wrk_clean_omitted_entrypoint"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK",
        "glasshive-parallel-clean-room",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    container_payload = _clean_room_inspect_payload(
        manager,
        worker_id,
        container_id="a" * 64,
    )
    image_payload = _configured_image_inspect_payload(manager)
    del image_payload[0]["Config"]["Entrypoint"]

    def fake_docker(args, **_kwargs):
        payload = image_payload if args[:2] == ["image", "inspect"] else container_payload
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    manager._docker = fake_docker  # type: ignore[method-assign]

    inspection = manager.inspect_fresh(worker_id)

    assert inspection.status == "present"
    assert inspection.sandbox is not None
    assert inspection.sandbox.expected_entrypoint is None

def test_fresh_inspection_parses_exact_clean_room_policy_evidence(
    tmp_path, monkeypatch
):
    worker_id = "wrk_clean_exact_inspect"
    network_name = "glasshive-parallel-clean-room"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", network_name)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = _clean_room_inspect_payload(
        manager,
        worker_id,
        container_id="a" * 64,
    )
    def fake_docker(args, **_kwargs):
        if args[:2] == ["image", "inspect"]:
            stdout = json.dumps(_configured_image_inspect_payload(manager))
        else:
            stdout = json.dumps(payload)
        return subprocess.CompletedProcess(args, 0, stdout, "")

    manager._docker = fake_docker  # type: ignore[method-assign]

    inspection = manager.inspect_fresh(worker_id)

    assert inspection.status == "present"
    assert inspection.sandbox is not None
    assert inspection.sandbox.container_id == "a" * 64
    assert inspection.sandbox.attached_networks == (
        manager._parallel_clean_room_mission_network_name(
            manager._container_name(worker_id)
        ),
    )
    assert inspection.sandbox.privileged is False
    assert inspection.sandbox.cap_add == ()
    assert set(inspection.sandbox.bind_mount_pairs) == {
        (inspection.sandbox.workspace_dir, manager.workspace_mount),
        (inspection.sandbox.home_dir, manager.home_mount),
    }
    assert manager._sandbox_matches_parallel_clean_room_policy(
        inspection.sandbox
    ) is True

def test_fresh_inspection_attests_reserved_created_container_before_dynamic_ports(
    tmp_path, monkeypatch
):
    worker_id = "wrk_clean_created_inspect"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = _clean_room_inspect_payload(
        manager,
        worker_id,
        container_id="a" * 64,
    )
    entry = payload[0]
    entry["State"] = {"Status": "created", "Paused": False, "Pid": 0}
    host_config = entry["HostConfig"]
    assert isinstance(host_config, dict)
    host_config["PidMode"] = ""
    host_config["UTSMode"] = ""
    host_config["PortBindings"] = {}
    network_settings = entry["NetworkSettings"]
    assert isinstance(network_settings, dict)
    network_settings["Ports"] = {}

    def fake_docker(args, **_kwargs):
        response = (
            _configured_image_inspect_payload(manager)
            if args[:2] == ["image", "inspect"]
            else payload
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(response), "")

    manager._docker = fake_docker  # type: ignore[method-assign]
    inspection = manager.inspect_fresh(worker_id)

    assert inspection.status == "present"
    assert inspection.sandbox is not None
    assert inspection.sandbox.state == "created"
    assert manager._sandbox_matches_parallel_clean_room_policy(
        inspection.sandbox
    ) is True

def test_fresh_inspection_classifies_internal_network_unassigned_legacy_ports_as_drift(
    tmp_path, monkeypatch
):
    """A real Docker Desktop internal network leaves requested HostPorts unassigned."""

    worker_id = "wrk_clean_unassigned_legacy_ports"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = _clean_room_inspect_payload(
        manager,
        worker_id,
        container_id="a" * 64,
    )
    entry = payload[0]
    host_config = entry["HostConfig"]
    network_settings = entry["NetworkSettings"]
    assert isinstance(host_config, dict)
    assert isinstance(network_settings, dict)
    host_config["PortBindings"] = {
        f"{manager.novnc_container_port}/tcp": [
            {"HostIp": "127.0.0.1", "HostPort": ""}
        ]
    }
    network_settings["Ports"] = {
        f"{manager.novnc_container_port}/tcp": []
    }

    def fake_docker(args, **_kwargs):
        response = (
            _configured_image_inspect_payload(manager)
            if args[:2] == ["image", "inspect"]
            else payload
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(response), "")

    manager._docker = fake_docker  # type: ignore[method-assign]
    inspection = manager.inspect_fresh(worker_id)

    assert inspection.status == "present"
    assert inspection.sandbox is not None
    assert inspection.sandbox.port_bindings == (
        (manager.novnc_container_port, "127.0.0.1", 0),
    )
    assert manager._sandbox_matches_parallel_clean_room_policy(
        inspection.sandbox
    ) is False

@pytest.mark.parametrize(
    ("surface", "mutate"),
    [
        (
            "uts_host",
            lambda entry: entry["HostConfig"].__setitem__("UTSMode", "host"),
        ),
        (
            "userns_host",
            lambda entry: entry["HostConfig"].__setitem__("UsernsMode", "host"),
        ),
        (
            "cgroupns_host",
            lambda entry: entry["HostConfig"].__setitem__("CgroupnsMode", "host"),
        ),
        (
            "public_port",
            lambda entry: entry["NetworkSettings"]["Ports"].__setitem__(
                "7900/tcp", [{"HostIp": "0.0.0.0", "HostPort": "40997"}]
            ),
        ),
        (
            "extra_port",
            lambda entry: entry["NetworkSettings"]["Ports"].__setitem__(
                "9999/tcp", [{"HostIp": "127.0.0.1", "HostPort": "40999"}]
            ),
        ),
        (
            "multiple_port_bindings",
            lambda entry: entry["NetworkSettings"]["Ports"].__setitem__(
                "7900/tcp",
                [
                    {"HostIp": "127.0.0.1", "HostPort": "40997"},
                    {"HostIp": "127.0.0.1", "HostPort": "40998"},
                ],
            ),
        ),
        (
            "read_only_bind",
            lambda entry: entry["Mounts"][0].__setitem__("RW", False),
        ),
        (
            "shared_bind",
            lambda entry: entry["Mounts"][0].update(
                {"Mode": "rw,rshared", "Propagation": "rshared"}
            ),
        ),
        (
            "weak_tmpfs",
            lambda entry: entry["HostConfig"]["Tmpfs"].__setitem__(
                "/tmp", "rw,size=9999999999"
            ),
        ),
        (
            "ambient_provider_secret",
            lambda entry: entry["Config"]["Env"].append(
                "OPENAI_API_KEY=synthetic-ambient-secret"
            ),
        ),
        (
            "ambient_cloud_secret",
            lambda entry: entry["Config"]["Env"].append(
                "AWS_ACCESS_KEY_ID=synthetic-ambient-id"
            ),
        ),
    ],
)
def test_fresh_clean_room_attestation_rejects_namespace_port_mount_and_env_drift(
    tmp_path, monkeypatch, surface, mutate
):
    del surface
    worker_id = "wrk_clean_complete_attestation"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = _clean_room_inspect_payload(
        manager, worker_id, container_id="f" * 64
    )
    mutate(payload[0])

    def fake_docker(args, **_kwargs):
        response = (
            _configured_image_inspect_payload(manager)
            if args[:2] == ["image", "inspect"]
            else payload
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(response), "")

    manager._docker = fake_docker  # type: ignore[method-assign]
    inspection = manager.inspect_fresh(worker_id)

    assert inspection.status == "present"
    assert inspection.sandbox is not None
    assert manager._sandbox_matches_parallel_clean_room_policy(
        inspection.sandbox
    ) is False

def _configure_clean_room_existing_container_test(
    manager: DockerSandboxManager,
    container_payload: list[dict[str, object]],
    *,
    image_result: subprocess.CompletedProcess[str] | None = None,
) -> tuple[list[list[str]], list[bool]]:
    docker_calls: list[list[str]] = []
    seeded: list[bool] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_worker_permissions_migrated = lambda _root: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *_args, **_kwargs: seeded.append(True)  # type: ignore[method-assign]
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)

    def fake_docker(args, **_kwargs):
        docker_calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return image_result or subprocess.CompletedProcess(
                args,
                0,
                json.dumps(_configured_image_inspect_payload(manager)),
                "",
            )
        return subprocess.CompletedProcess(args, 0, json.dumps(container_payload), "")

    manager._docker = fake_docker  # type: ignore[method-assign]
    return docker_calls, seeded

def test_parallel_clean_room_fresh_attestation_ignores_warm_image_cache(
    tmp_path, monkeypatch
):
    worker_id = "wrk_clean_fresh_image"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._image_checked_at = time.monotonic()
    docker_calls, seeded = _configure_clean_room_existing_container_test(
        manager,
        _clean_room_inspect_payload(
            manager,
            worker_id,
            container_id="b" * 64,
        ),
    )

    result = manager.ensure_ready(
        {
            "worker_id": worker_id,
            "state": "running",
            "bootstrap_profile": "clean-room",
            "bootstrap_bundle_json": json.dumps(
                {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
            ),
        },
        "codex-cli",
    )

    assert result.container_id == "b" * 64
    assert docker_calls == [
        ["inspect", manager._container_name(worker_id)],
        ["image", "inspect", manager.image],
        ["inspect", manager._container_name(worker_id)],
        ["image", "inspect", manager.image],
        ["inspect", manager._container_name(worker_id)],
        ["image", "inspect", manager.image],
    ]
    assert seeded == [True]

@pytest.mark.parametrize(
    "substrate_drift",
    [
        "wrong_image_id",
        "wrong_image_reference",
        "root_user",
        "entrypoint_override",
        "command_override",
        "host_pid_namespace",
        "host_ipc_namespace",
        "extra_volume_mount",
    ],
)
def test_parallel_clean_room_never_seeds_exact_substrate_drift(
    tmp_path, monkeypatch, substrate_drift
):
    worker_id = "wrk_clean_substrate_drift"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = _clean_room_inspect_payload(
        manager,
        worker_id,
        container_id="c" * 64,
    )
    entry = payload[0]
    config = entry["Config"]
    host_config = entry["HostConfig"]
    mounts = entry["Mounts"]
    if substrate_drift == "wrong_image_id":
        entry["Image"] = "sha256:" + ("f" * 64)
    elif substrate_drift == "wrong_image_reference":
        config["Image"] = "synthetic-wrong-image:latest"
    elif substrate_drift == "root_user":
        config["User"] = "root"
    elif substrate_drift == "entrypoint_override":
        config["Entrypoint"] = ["/bin/sh"]
    elif substrate_drift == "command_override":
        config["Cmd"] = ["-c", "sleep infinity"]
    elif substrate_drift == "host_pid_namespace":
        host_config["PidMode"] = "host"
    elif substrate_drift == "host_ipc_namespace":
        host_config["IpcMode"] = "host"
    elif substrate_drift == "extra_volume_mount":
        mounts.append(
            {
                "Type": "volume",
                "Source": "synthetic-protected-state",
                "Destination": "/protected-state",
            }
        )
    _docker_calls, seeded = _configure_clean_room_existing_container_test(
        manager, payload
    )

    with pytest.raises(RuntimeError, match="does not attest"):
        manager.ensure_ready(
            {
                "worker_id": worker_id,
                "state": "running",
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert seeded == []

@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (124, "", "Docker image inspect timed out"),
        (0, "{malformed-json", ""),
        (0, json.dumps([{"Id": "not-an-image-id", "Config": {}}]), ""),
    ],
    ids=["timeout", "malformed_json", "malformed_schema"],
)
def test_parallel_clean_room_image_probe_uncertainty_blocks_before_seed_or_removal(
    tmp_path, monkeypatch, returncode, stdout, stderr
):
    worker_id = "wrk_clean_image_uncertain"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    image_result = subprocess.CompletedProcess(
        ["image", "inspect", manager.image], returncode, stdout, stderr
    )
    docker_calls, seeded = _configure_clean_room_existing_container_test(
        manager,
        _clean_room_inspect_payload(
            manager,
            worker_id,
            container_id="d" * 64,
        ),
        image_result=image_result,
    )
    manager.terminate = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("image-probe uncertainty must block before removal")
    )

    with pytest.raises(RuntimeError, match="image inspection is unavailable"):
        manager.ensure_ready(
            {
                "worker_id": worker_id,
                "state": "ready",
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert docker_calls[:2] == [
        ["inspect", manager._container_name(worker_id)],
        ["image", "inspect", manager.image],
    ]
    assert seeded == []

def test_parallel_clean_room_rejects_docker_socket_bind_before_seed(
    tmp_path, monkeypatch
):
    worker_id = "wrk_clean_docker_socket"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = _clean_room_inspect_payload(
        manager,
        worker_id,
        container_id="e" * 64,
    )
    payload[0]["Mounts"].append(
        {
            "Type": "bind",
            "Source": "/var/run/docker.sock",
            "Destination": "/var/run/docker.sock",
            "Mode": "rw",
            "RW": True,
            "Propagation": "rprivate",
        }
    )
    _docker_calls, seeded = _configure_clean_room_existing_container_test(
        manager, payload
    )

    with pytest.raises(RuntimeError, match="does not attest"):
        manager.ensure_ready(
            {
                "worker_id": worker_id,
                "state": "running",
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert seeded == []

@pytest.mark.parametrize(
    ("id_case", "raw_container_id"),
    [
        ("missing", None),
        ("empty", ""),
        ("whitespace", "   "),
        ("wrong_type", 17),
        ("malformed", "not-a-full-docker-container-id"),
    ],
    ids=lambda value: str(value),
)
def test_parallel_clean_room_never_seeds_when_fresh_container_id_is_invalid(
    tmp_path, monkeypatch, id_case, raw_container_id
):
    worker_id = "wrk_clean_invalid_id"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = _clean_room_inspect_payload(
        manager, worker_id, container_id=raw_container_id
    )
    if id_case == "missing":
        payload[0].pop("Id")
    seeded: list[bool] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._docker = lambda args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args, 0, json.dumps(payload), ""
    )
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = lambda *_args, **_kwargs: seeded.append(True)  # type: ignore[method-assign]

    with pytest.raises(
        RuntimeError,
        match="inspection is unavailable: docker_inspect_malformed",
    ):
        manager.ensure_ready(
            {
                "worker_id": worker_id,
                "state": "running",
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert seeded == []

@pytest.mark.parametrize(
    "policy_drift",
    [
        "extra_attached_network",
        "wrong_bind_source",
        "privileged",
        "cap_add",
        "extra_cap_drop",
        "extra_security_option",
    ],
)
def test_parallel_clean_room_matcher_rejects_exact_policy_drift(
    tmp_path, monkeypatch, policy_drift
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    sandbox = _attested_clean_room_sandbox(
        manager,
        "wrk_clean_exact_policy",
        container_id="attested-clean-room-generation",
    )
    assert manager._sandbox_matches_parallel_clean_room_policy(sandbox) is True

    if policy_drift == "extra_attached_network":
        sandbox.attached_networks = (
            "glasshive-parallel-clean-room",
            "host-egress-network",
        )
    elif policy_drift == "wrong_bind_source":
        sandbox.bind_mount_pairs = (
            (str(tmp_path / "forged-host-root"), manager.workspace_mount),
            (sandbox.home_dir, manager.home_mount),
        )
    elif policy_drift == "privileged":
        sandbox.privileged = True
    elif policy_drift == "cap_add":
        sandbox.cap_add = ("SYS_ADMIN",)
    elif policy_drift == "extra_cap_drop":
        sandbox.cap_drop = ("ALL", "NET_RAW")
    elif policy_drift == "extra_security_option":
        sandbox.security_options = (
            "no-new-privileges:true",
            "apparmor=unconfined",
        )

    assert manager._sandbox_matches_parallel_clean_room_policy(sandbox) is False
    seeded: list[bool] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager.inspect_fresh = lambda _worker_id: FreshSandboxInspection(  # type: ignore[method-assign]
        status="present", sandbox=sandbox
    )
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = lambda *_args, **_kwargs: seeded.append(True)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="does not attest"):
        manager.ensure_ready(
            {
                "worker_id": "wrk_clean_exact_policy",
                "state": "running",
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert seeded == []

def test_parallel_clean_room_ignores_cached_attestation_when_current_generation_mismatches(
    tmp_path, monkeypatch
):
    worker_id = "wrk_clean_stale_cache"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._inspect_cache[worker_id] = (
        time.monotonic(),
        _attested_clean_room_sandbox(
            manager, worker_id, container_id="cached-attested-generation"
        ),
    )
    current_payload = _clean_room_inspect_payload(
        manager,
        worker_id,
        container_id="c" * 64,
    )
    current_payload[0]["HostConfig"]["NetworkMode"] = "bridge"
    current_payload[0]["HostConfig"]["SecurityOpt"] = ["seccomp=unconfined"]
    current_payload[0]["Config"]["Labels"] = {}
    current_payload[0]["NetworkSettings"]["Networks"] = {"bridge": {}}
    docker_calls: list[list[str]] = []
    seeded: list[bool] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]

    def fake_docker(args, **_kwargs):
        docker_calls.append(args)
        payload = (
            _configured_image_inspect_payload(manager)
            if args[:2] == ["image", "inspect"]
            else current_payload
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = lambda *_args, **_kwargs: seeded.append(True)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="does not attest"):
        manager.ensure_ready(
            {
                "worker_id": worker_id,
                "state": "running",
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert docker_calls == [
        ["inspect", manager._container_name(worker_id)],
        ["image", "inspect", manager.image],
    ]
    assert seeded == []

@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (124, "", "Docker command timed out after 5s"),
        (0, "{malformed-json", ""),
    ],
    ids=["timeout", "malformed"],
)
def test_parallel_clean_room_never_seeds_when_fresh_inspection_is_unavailable(
    tmp_path, monkeypatch, returncode, stdout, stderr
):
    worker_id = "wrk_clean_ambiguous_inspect"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._inspect_cache[worker_id] = (
        time.monotonic(),
        _attested_clean_room_sandbox(
            manager, worker_id, container_id="stale-attested-generation"
        ),
    )
    seeded: list[bool] = []
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda _paths: None  # type: ignore[method-assign]
    manager._docker = lambda args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args, returncode, stdout, stderr
    )
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    _stub_parallel_clean_room_mission_network(manager)
    manager._seed_bootstrap = lambda *_args, **_kwargs: seeded.append(True)  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="inspection is unavailable"):
        manager.ensure_ready(
            {
                "worker_id": worker_id,
                "state": "ready",
                "bootstrap_profile": "clean-room",
                "bootstrap_bundle_json": json.dumps(
                    {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
                ),
            },
            "codex-cli",
        )

    assert seeded == []

def test_fresh_inspection_confirms_absence_without_using_cached_attestation(
    tmp_path, monkeypatch
):
    worker_id = "wrk_clean_confirmed_absent"
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager._inspect_cache[worker_id] = (
        time.monotonic(),
        _attested_clean_room_sandbox(
            manager, worker_id, container_id="cached-attested-generation"
        ),
    )
    def fake_docker(args, **_kwargs):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(_configured_image_inspect_payload(manager)),
                "",
            )
        return subprocess.CompletedProcess(
            args,
            1,
            "",
            f"Error: No such object: {manager._container_name(worker_id)}",
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    inspection = manager.inspect_fresh(worker_id)

    assert inspection.status == "confirmed_absent"
    assert inspection.sandbox is None
    assert inspection.reason == "docker_confirmed_container_absent"
    assert worker_id not in manager._inspect_cache

def test_inspect_uses_short_cache_and_stale_fallback(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager.inspect_cache_ttl_sec = 60
    calls = 0
    payload = [
        {
            "Id": "abc123",
            "State": {"Status": "running", "Paused": False, "Pid": 4242},
            "NetworkSettings": {"Ports": {"7900/tcp": [{"HostPort": "58100"}]}},
        }
    ]

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout=json.dumps(payload), stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    first = manager.inspect("wrk_test")
    second = manager.inspect("wrk_test")

    assert first is not None
    assert second is first
    assert calls == 1

    manager.inspect_cache_ttl_sec = -1

    def timeout_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        return subprocess.CompletedProcess(["docker", *args], returncode=124, stdout="", stderr="timed out")

    manager._docker = timeout_docker  # type: ignore[method-assign]

    assert manager.inspect("wrk_test") is first


def test_terminate_invalidates_inspect_cache_before_idle_resume(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager.inspect_cache_ttl_sec = 60
    exists = True
    calls: list[str] = []

    def running_payload() -> str:
        return json.dumps(
            [
                {
                    "Id": "abc123",
                    "State": {"Status": "running", "Paused": False, "Pid": 4242},
                    "NetworkSettings": {"Ports": {}},
                }
            ]
        )

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        nonlocal exists
        if args[:1] == ["inspect"]:
            calls.append("inspect")
            return subprocess.CompletedProcess(
                ["docker", *args],
                returncode=0 if exists else 1,
                stdout=running_payload() if exists else "",
                stderr="" if exists else "Error: No such object: wpr-wrk-test",
            )
        if args[:2] == ["rm", "-f"]:
            calls.append("rm")
            exists = False
            return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")
        if args[:2] == ["network", "rm"]:
            calls.append("network-rm")
            return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call: {args}")

    def fake_create_container(container_name, paths, worker=None):
        nonlocal exists
        calls.append("create")
        exists = True

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._require_docker = lambda: calls.append("require")  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append("host_dirs")  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append("seed")  # type: ignore[method-assign]
    manager._ensure_image = lambda: calls.append("image")  # type: ignore[method-assign]
    manager._create_container = fake_create_container  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("repair")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]

    cached = manager.inspect("wrk_test")
    assert cached is not None
    manager.terminate("wrk_test")

    resumed = manager.ensure_ready({"worker_id": "wrk_test", "state": "starting"}, "codex-cli")

    assert resumed.container_name == "wpr-wrk-test"
    assert "create" in calls
    assert calls.count("inspect") >= 3


def test_terminate_fails_if_docker_leaves_the_container_running(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    running = json.dumps(
        [
            {
                "Id": "abc123",
                "State": {"Status": "running", "Paused": False, "Pid": 4242},
                "HostConfig": {},
                "NetworkSettings": {"Ports": {}},
            }
        ]
    )

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        _ = check, capture_output, kwargs
        if args[:1] == ["inspect"]:
            return subprocess.CompletedProcess(["docker", *args], 0, running, "")
        if args[:2] == ["rm", "-f"]:
            return subprocess.CompletedProcess(["docker", *args], 1, "", "removal failed")
        raise AssertionError(f"unexpected docker call: {args}")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="still running after termination"):
        manager.terminate("wrk_test")


def test_clean_room_termination_ignores_stale_cached_generation_after_confirmed_absence(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    worker_id = "wrk_stale_release_cache"
    stale = SandboxInfo(
        container_name=manager._container_name(worker_id),
        container_id="a" * 64,
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=42,
        image=manager.image,
        network_mode="legacy-wide-network",
        execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    )
    docker_calls: list[list[str]] = []
    removed_networks: list[tuple[str, str | None]] = []
    manager.inspect = lambda _worker_id: stale  # type: ignore[method-assign]
    manager.inspect_fresh = lambda _worker_id, **_kwargs: FreshSandboxInspection(  # type: ignore[method-assign]
        status="confirmed_absent",
        reason="docker_confirmed_container_absent",
    )
    manager._docker = lambda args, **_kwargs: (  # type: ignore[method-assign]
        docker_calls.append(args)
        or subprocess.CompletedProcess(
            args,
            1 if args[:1] == ["inspect"] else 0,
            "",
            "Error: No such object",
        )
    )
    manager._remove_parallel_clean_room_mission_network = (  # type: ignore[method-assign]
        lambda container_name, network_name=None: removed_networks.append(
            (container_name, network_name)
        )
    )

    result = manager.terminate(
        worker_id,
        expected_absent=True,
        execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    )

    assert result.state == "terminated"
    assert not any(args[:2] == ["rm", "-f"] for args in docker_calls)
    assert removed_networks == [
        (
            manager._container_name(worker_id),
            manager._parallel_clean_room_mission_network_name(
                manager._container_name(worker_id)
            ),
        )
    ]

def test_clean_room_expected_absence_rejects_replacement_generation_before_removal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    worker_id = "wrk_absent_replaced"
    replacement = SandboxInfo(
        container_name=manager._container_name(worker_id),
        container_id="b" * 64,
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=42,
        image=manager.image,
        execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    )
    manager.inspect_fresh = lambda _worker_id, **_kwargs: FreshSandboxInspection(  # type: ignore[method-assign]
        status="present",
        sandbox=replacement,
    )
    docker_calls: list[list[str]] = []
    manager._docker = lambda args, **_kwargs: (  # type: ignore[method-assign]
        docker_calls.append(args)
        or subprocess.CompletedProcess(args, 0, "", "")
    )

    with pytest.raises(RuntimeError, match="generation changed"):
        manager.terminate(
            worker_id,
            expected_absent=True,
            execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        )

    assert not any(args[:2] == ["rm", "-f"] for args in docker_calls)

def test_terminate_fails_when_docker_rm_is_rejected(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = json.dumps(
        [
            {
                "Id": "abc123",
                "State": {"Status": "running", "Paused": False, "Pid": 4242},
                "NetworkSettings": {"Ports": {}},
            }
        ]
    )

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        if args[:1] == ["inspect"]:
            return subprocess.CompletedProcess(
                ["docker", *args], returncode=0, stdout=payload, stderr=""
            )
        if args[:2] == ["rm", "-f"]:
            return subprocess.CompletedProcess(
                ["docker", *args], returncode=1, stdout="", stderr="removal rejected"
            )
        raise AssertionError(f"unexpected docker call: {args}")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="could not be confirmed"):
        manager.terminate("wrk_test")

def test_terminate_fails_when_container_remains_after_successful_rm(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = json.dumps(
        [
            {
                "Id": "abc123",
                "State": {"Status": "running", "Paused": False, "Pid": 4242},
                "NetworkSettings": {"Ports": {}},
            }
        ]
    )
    calls: list[str] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        if args[:1] == ["inspect"]:
            calls.append("inspect")
            return subprocess.CompletedProcess(
                ["docker", *args], returncode=0, stdout=payload, stderr=""
            )
        if args[:2] == ["rm", "-f"]:
            calls.append("rm")
            return subprocess.CompletedProcess(
                ["docker", *args], returncode=0, stdout="", stderr=""
            )
        raise AssertionError(f"unexpected docker call: {args}")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="could not be confirmed"):
        manager.terminate("wrk_test")

    assert calls == ["inspect", "rm", "inspect"]

def test_terminate_exact_container_refuses_new_worker_generation(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    payload = json.dumps(
        [
            {
                "Id": "container-b",
                "State": {"Status": "running", "Paused": False, "Pid": 4242},
                "NetworkSettings": {"Ports": {}},
            }
        ]
    )
    removals: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        if args[:1] == ["inspect"]:
            return subprocess.CompletedProcess(
                ["docker", *args], returncode=0, stdout=payload, stderr=""
            )
        if args[:2] == ["rm", "-f"]:
            removals.append(args)
        raise AssertionError(f"unexpected docker call: {args}")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="generation changed"):
        manager.terminate("wrk_test", expected_container_id="container-a")

    assert removals == []

def test_terminate_exact_container_accepts_already_absent_generation(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            ["docker", *args],
            returncode=1,
            stdout="",
            stderr=f"Error: No such object: {args[-1]}",
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    result = manager.terminate("wrk_test", expected_container_id="container-a")

    assert result.state == "terminated"
    assert calls == [["inspect", "wpr-wrk-test"], ["inspect", "container-a"]]

def test_parallel_clean_room_termination_retires_exact_mission_network(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    retired: list[tuple[str, str]] = []

    manager.inspect = lambda _worker_id: None  # type: ignore[method-assign]
    manager._docker = lambda args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args,
        1,
        "",
        f"Error: No such object: {args[-1]}",
    )
    manager._remove_parallel_clean_room_mission_network = (  # type: ignore[method-assign]
        lambda container_name, network_name=None: retired.append(
            (container_name, str(network_name or ""))
        )
    )

    manager.terminate(
        "wrk_test",
        expected_container_id="a" * 64,
        execution_policy=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    )

    assert retired == [
        (
            "wpr-wrk-test",
            manager._parallel_clean_room_mission_network_name("wpr-wrk-test"),
        )
    ]

def test_remove_parallel_clean_room_mission_network_disconnects_only_attested_proxies(
    tmp_path, monkeypatch
):
    base_network = "glasshive-parallel-clean-room"
    provider = "glasshive-provider-egress"
    broker = "glasshive-capability-broker-proxy"
    container_name = "wpr-worker-retired"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", base_network)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    mission_network = manager._parallel_clean_room_mission_network_name(container_name)
    provider_id = "a" * 64
    broker_id = "b" * 64
    connected = {provider, broker}
    calls: list[list[str]] = []

    def network_payload():
        members = {}
        if provider in connected:
            members[provider_id] = {"Name": provider}
        if broker in connected:
            members[broker_id] = {"Name": broker}
        return json.dumps(
            [
                {
                    "Name": mission_network,
                    "Driver": "bridge",
                    "Internal": True,
                    "Labels": {
                        "com.viventium.parallel-clean-room.policy": (
                            PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                        ),
                        "com.viventium.parallel-clean-room.role": "mission-network",
                        "com.viventium.parallel-clean-room.worker-container": container_name,
                    },
                    "Containers": members,
                }
            ]
        )

    def fake_docker(args, **_kwargs):
        calls.append(args)
        if args == ["network", "inspect", mission_network]:
            return subprocess.CompletedProcess(args, 0, network_payload(), "")
        if args == ["inspect", provider]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Id": provider_id,
                            "Config": {
                                "Labels": {
                                    "com.viventium.parallel-clean-room.policy": (
                                        PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                                    ),
                                    "com.viventium.parallel-clean-room.role": "provider-proxy",
                                }
                            },
                        }
                    ]
                ),
                "",
            )
        if args == ["inspect", broker]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Id": broker_id,
                            "Config": {
                                "Labels": {
                                    "com.viventium.parallel-clean-room.policy": (
                                        PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                                    ),
                                    "com.viventium.parallel-clean-room.role": "broker-proxy",
                                }
                            },
                        }
                    ]
                ),
                "",
            )
        if args[:3] == ["network", "disconnect", "-f"]:
            connected.remove(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ["network", "rm", mission_network]:
            assert connected == set()
            return subprocess.CompletedProcess(args, 0, mission_network, "")
        raise AssertionError(f"unexpected docker call: {args}")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._remove_parallel_clean_room_mission_network(container_name)

    assert [
        ["network", "disconnect", "-f", mission_network, provider],
        ["network", "disconnect", "-f", mission_network, broker],
        ["network", "rm", mission_network],
    ] == [call for call in calls if call[0:2] != ["network", "inspect"] and call[0] != "inspect"]

def test_parallel_clean_room_run_grant_is_projected_only_to_exact_generation_tmpfs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    worker_id = "wrk_tmpfs_grant"
    container_id = "c" * 64
    run_id = "run_tmpfs_grant"
    grant = "synthetic-run-grant-generation-bound"
    sandbox = _attested_clean_room_sandbox(
        manager,
        worker_id,
        container_id=container_id,
    )
    manager.inspect_fresh = lambda _worker_id: FreshSandboxInspection(  # type: ignore[method-assign]
        status="present", sandbox=sandbox
    )
    manager.parallel_clean_room_readiness = lambda: {  # type: ignore[method-assign]
        "ready": True,
        "reason": "",
    }
    manager._ensure_parallel_clean_room_mission_network = (  # type: ignore[method-assign]
        lambda _container_name: sandbox.network_mode
    )
    calls: list[list[str]] = []
    stdin_payloads: list[str] = []

    def fake_docker(args, **kwargs):
        calls.append(args)
        if kwargs.get("input_text") is not None:
            stdin_payloads.append(kwargs["input_text"])
        return subprocess.CompletedProcess(args, 0, "", "")

    manager._docker = fake_docker  # type: ignore[method-assign]

    projected = manager.project_parallel_clean_room_run_secrets(
        worker_id,
        expected_container_id=container_id,
        run_id=run_id,
        env={"GLASSHIVE_CAPABILITY_BROKER_TOKEN": grant},
    )

    secret_root = f"/run/glasshive/{run_id}"
    assert projected == {
        "env_file": f"{secret_root}/secret-runtime.env",
        "keys_file": f"{secret_root}/secret-runtime.keys",
    }
    assert stdin_payloads == [
        "export GLASSHIVE_CAPABILITY_BROKER_TOKEN="
        "synthetic-run-grant-generation-bound\n",
        "GLASSHIVE_CAPABILITY_BROKER_TOKEN\n",
    ]
    assert grant not in json.dumps(calls)
    assert all(any(container_id in part for part in call) for call in calls)
    assert any(
        call[:5] == ["exec", "-u", manager.user, container_id, "bash"]
        and "mkdir -p" in " ".join(call)
        for call in calls
    )
    assert sum(call[:2] == ["exec", "-i"] for call in calls) == 2
    assert not any("chown" in " ".join(call) for call in calls)
    assert not any(call[0] == "cp" for call in calls)
    assert (
        "/run/glasshive:rw,nosuid,nodev,noexec,size=16m,"
        "mode=700,uid=1200,gid=1201"
    ) in PARALLEL_CLEAN_ROOM_TMPFS

    manager.clear_parallel_clean_room_run_secrets(
        worker_id,
        expected_container_id=container_id,
        run_id=run_id,
    )

    assert any(
        call[:4] == ["exec", "-u", manager.user, container_id]
        and f"/run/glasshive/{run_id}" in " ".join(call)
        for call in calls
    )

def test_docker_exec_timeout_returns_failed_result(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_run(*args, **kwargs):
        _ = args, kwargs
        raise subprocess.TimeoutExpired(["docker", "exec"], timeout=2, output="", stderr="")

    monkeypatch.setenv("WPR_DOCKER_EXEC_TIMEOUT_SEC", "2")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = manager._docker_exec("wpr-test", ["bash", "-c", "sleep 99"])

    assert result.returncode == 124
    assert "timed out after 2s" in result.stderr


def test_docker_exec_keeps_secret_environment_values_out_of_argv(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    observed: dict[str, object] = {}

    def fake_docker(args: list[str], **kwargs):
        observed["args"] = list(args)
        env_path = Path(args[args.index("--env-file") + 1])
        observed["env_path"] = env_path
        observed["env_text"] = env_path.read_text(encoding="utf-8")
        assert env_path.stat().st_mode & 0o777 == 0o600
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]
    result = manager._docker_exec(
        "wpr-test",
        ["bash", "-lc", "true"],
        env={"OPENAI_API_KEY": "synthetic-secret-value", "HOME": "/workspace/.wpr-home"},
    )

    assert result.returncode == 0
    assert "synthetic-secret-value" in str(observed["env_text"])
    assert all("synthetic-secret-value" not in value for value in observed["args"])
    assert not Path(observed["env_path"]).exists()


def test_docker_exec_detach_uses_popen_without_waiting(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, command, *, stdout=None, stderr=None, **kwargs):
            calls.append(command)
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    result = manager._docker_exec(
        "wpr-test",
        ["bash", "-lc", "sleep 60"],
        env={"HOME": "/workspace/.wpr-home"},
        cwd="/workspace/project",
        detach=True,
        fire_and_forget=True,
    )

    assert result.returncode == 0
    deadline = time.time() + 1
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    assert calls[0][:2] == ["sh", "-lc"]
    assert "sleep 0.1; docker exec -d -u seluser" in calls[0][2]
    assert "--env-file" in calls[0][2]
    assert "HOME=/workspace/.wpr-home" not in calls[0][2]
    assert "wpr-test bash -lc 'sleep 60'" in calls[0][2]


def test_docker_exec_monitors_visible_provider_session_until_window_closes(
    tmp_path, monkeypatch
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []
    allow_exit = Event()
    callback_called = Event()

    class FakePopen:
        def __init__(self, command, *, stdout=None, stderr=None, **kwargs):
            calls.append(command)

        def wait(self, timeout=None):
            assert timeout == 30
            assert allow_exit.wait(2)
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    result = manager._docker_exec(
        "wpr-test",
        ["xterm", "-e", "claude"],
        env={"HOME": "/workspace/.wpr-home"},
        cwd="/workspace/project",
        fire_and_forget=True,
        on_exit=callback_called.set,
        max_runtime_seconds=30,
    )

    assert result.returncode == 0
    assert len(calls) == 1
    assert "docker exec -u seluser" in calls[0][2]
    assert "docker exec -d" not in calls[0][2]
    assert not callback_called.is_set()

    allow_exit.set()
    assert callback_called.wait(2)


def test_docker_exec_timeout_releases_provider_before_reaping_detached_client(
    tmp_path, monkeypatch
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    callback_called = Event()
    events: list[str] = []

    class FakePopen:
        pid = 4242

        def __init__(self, _command, *, stdout=None, stderr=None, **kwargs):
            self.wait_count = 0

        def wait(self, timeout=None):
            self.wait_count += 1
            events.append(f"wait:{timeout}")
            if self.wait_count == 1:
                raise subprocess.TimeoutExpired("synthetic interactive session", timeout)
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, sent_signal: events.append(
            f"signal:{pid}:{signal.Signals(sent_signal).name}"
        ),
    )

    def close_session():
        events.append("on_exit")
        callback_called.set()

    result = manager._docker_exec(
        "wpr-test",
        ["xterm", "-e", "claude"],
        env={"HOME": "/workspace/.wpr-home"},
        fire_and_forget=True,
        on_exit=close_session,
        max_runtime_seconds=30,
    )

    assert result.returncode == 0
    assert callback_called.wait(2)
    assert events == ["wait:30", "on_exit", "wait:15"]


def test_docker_exec_timeout_escalates_stubborn_detached_client_after_cleanup(
    tmp_path, monkeypatch
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    callback_called = Event()
    events: list[str] = []

    class FakePopen:
        pid = 4242

        def __init__(self, _command, *, stdout=None, stderr=None, **kwargs):
            self.wait_count = 0

        def wait(self, timeout=None):
            self.wait_count += 1
            events.append(f"wait:{timeout}")
            if self.wait_count < 4:
                raise subprocess.TimeoutExpired("synthetic interactive session", timeout)
            return 0

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, sent_signal: events.append(
            f"signal:{pid}:{signal.Signals(sent_signal).name}"
        ),
    )

    def close_session():
        events.append("on_exit")
        callback_called.set()

    result = manager._docker_exec(
        "wpr-test",
        ["xterm", "-e", "claude"],
        env={"HOME": "/workspace/.wpr-home"},
        fire_and_forget=True,
        on_exit=close_session,
        max_runtime_seconds=30,
    )

    assert result.returncode == 0
    assert callback_called.wait(2)
    assert events == [
        "wait:30",
        "on_exit",
        "wait:15",
        "signal:4242:SIGTERM",
        "wait:5",
        "signal:4242:SIGKILL",
        "wait:5",
    ]
    assert list((manager.runtime_root / ".docker-exec-env").glob("*.env")) == []


def test_docker_exec_timeout_falls_back_when_process_group_signals_fail(
    tmp_path, monkeypatch
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    callback_called = Event()
    events: list[str] = []

    class FakePopen:
        pid = 4242

        def __init__(self, _command, *, stdout=None, stderr=None, **kwargs):
            self.wait_count = 0

        def wait(self, timeout=None):
            self.wait_count += 1
            events.append(f"wait:{timeout}")
            if self.wait_count < 4:
                raise subprocess.TimeoutExpired("synthetic interactive session", timeout)
            return 0

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    def fail_group_signal(_pid, sent_signal):
        events.append(f"signal_failed:{signal.Signals(sent_signal).name}")
        raise OSError("synthetic host process-group failure")

    monkeypatch.setattr(os, "killpg", fail_group_signal)

    def close_session():
        events.append("on_exit")
        callback_called.set()

    result = manager._docker_exec(
        "wpr-test",
        ["xterm", "-e", "claude"],
        env={"HOME": "/workspace/.wpr-home"},
        fire_and_forget=True,
        on_exit=close_session,
        max_runtime_seconds=30,
    )

    assert result.returncode == 0
    assert callback_called.wait(2)
    deadline = time.monotonic() + 2
    while len(events) < 9 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events == [
        "wait:30",
        "on_exit",
        "wait:15",
        "signal_failed:SIGTERM",
        "terminate",
        "wait:5",
        "signal_failed:SIGKILL",
        "kill",
        "wait:5",
    ]
    assert list((manager.runtime_root / ".docker-exec-env").glob("*.env")) == []


def test_docker_exec_spawn_failure_is_reported_and_cleans_private_env(
    tmp_path, monkeypatch
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fail_to_spawn(*_args, **_kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(subprocess, "Popen", fail_to_spawn)

    result = manager._docker_exec(
        "wpr-test",
        ["xterm", "-e", "claude"],
        env={"HOME": "/workspace/.wpr-home"},
        fire_and_forget=True,
        on_exit=lambda: None,
        max_runtime_seconds=30,
    )

    assert result.returncode == 1
    assert result.stderr == "Docker exec could not start"
    assert list((manager.runtime_root / ".docker-exec-env").glob("*.env")) == []


def test_docker_exec_detach_confirms_docker_accepts_command(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_docker(args: list[str], **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    result = manager._docker_exec(
        "wpr-test",
        ["screen", "-DmS", "job-run_123", "bash", "run.sh"],
        env={"HOME": "/workspace/.wpr-home"},
        cwd="/workspace/project",
        detach=True,
    )

    assert result.returncode == 0
    assert calls[0][0][:2] == ["exec", "-d"]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["check"] is False
    assert "job-run_123" in calls[0][0]


def test_plain_background_retries_while_desktop_starts(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    captured: dict[str, object] = {}

    def fake_docker_exec(container_name, command, **kwargs):
        captured["container_name"] = container_name
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(["docker", "exec"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._set_plain_background("wpr-test")

    assert captured["container_name"] == "wpr-test"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["bash", "-c"]
    assert "seq 1 60" in command[2]
    assert "xsetroot -solid black" in command[2]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["detach"] is True
    assert kwargs["fire_and_forget"] is True
    assert kwargs["env"]["DISPLAY"] == manager.display_value


def test_seed_bootstrap_writes_project_scope_files(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
    upload_source = tmp_path / "uploaded.txt"
    upload_source.write_text("Uploaded content")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    home_dir.mkdir(parents=True)
    workspace_dir.mkdir(parents=True)

    worker = {
        "worker_id": "wrk_test",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {"TEST_FLAG": "1"},
                "system_instructions": "Use operator checkpoints before risky actions.",
                "claude_project_mcp": {
                    "glass-hive": {
                        "type": "http",
                        "transport": "http",
                        "url": "http://127.0.0.1:8767/mcp",
                    }
                },
                "codex_config_append": "[mcp_servers.glass-hive]\nurl = \"http://127.0.0.1:8767/mcp\"",
                "claude_settings_local": {
                    "permissions": {
                        "allow": ["Bash(ls *)"],
                    }
                },
                "files": [
                    {
                        "scope": "workspace",
                        "path": "notes/bootstrap.txt",
                        "content": "Bootstrapped",
                    },
                    {
                        "scope": "workspace",
                        "path": "uploads/uploaded.txt",
                        "source_path": str(upload_source),
                    }
                ],
            }
        ),
    }

    manager._seed_bootstrap(home_dir, workspace_dir, "claude-code", worker)

    agents_text = (workspace_dir / "AGENTS.md").read_text()
    claude_text = (workspace_dir / "CLAUDE.md").read_text()
    assert "GlassHive Worker Contract" in agents_text
    assert "Use operator checkpoints before risky actions." in agents_text
    assert "@AGENTS.md" in claude_text
    assert "Use operator checkpoints before risky actions." in agents_text
    assert json.loads((workspace_dir / ".mcp.json").read_text())["mcpServers"]["glass-hive"]["url"] == "http://127.0.0.1:8767/mcp"
    assert json.loads((workspace_dir / ".claude" / "settings.local.json").read_text())["permissions"]["allow"] == ["Bash(ls *)"]
    assert "glass-hive" in (home_dir / ".codex" / "config.toml").read_text()
    assert stat.S_IMODE((workspace_dir / ".mcp.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((workspace_dir / ".claude" / "settings.local.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((home_dir / ".codex" / "config.toml").stat().st_mode) == 0o600
    assert (workspace_dir / "notes" / "bootstrap.txt").read_text() == "Bootstrapped"
    assert (workspace_dir / "uploads" / "uploaded.txt").read_text() == "Uploaded content"
    assert "TEST_FLAG" in (home_dir / ".glasshive" / "runtime.env").read_text()
    manifest = json.loads((home_dir / ".glasshive" / "bootstrap-manifest.json").read_text())
    assert manifest["bootstrap_profile"] == "clean-room"
    assert "claude_project_mcp" in manifest["bundle_keys"]


def test_seed_parallel_clean_room_bootstrap_records_policy_outside_mounted_roots(
    tmp_path
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
    trusted_state_dir = tmp_path / "trusted-state"
    home_dir.mkdir()
    workspace_dir.mkdir()
    worker = {
        "worker_id": "wrk_clean_room_marker",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
        ),
    }

    manager._seed_bootstrap(
        home_dir,
        workspace_dir,
        "codex-cli",
        worker,
        trusted_state_dir=trusted_state_dir,
    )

    assert (trusted_state_dir / ".parallel-clean-room-v1").read_text() == (
        "parallel-clean-room-v1\n"
    )
    assert not (home_dir / ".parallel-clean-room-v1").exists()
    assert not (workspace_dir / ".parallel-clean-room-v1").exists()

def test_terminal_desktop_action_waits_for_live_session(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    command = manager._desktop_action_command("terminal", session_name="job-run_123456")
    assert command is not None
    assert command[0] == "xterm"
    assert "WPR Live Run" in command
    assert "screen -xRR" in command[-1]
    assert "job-run_123456" in command[-1]


def test_browser_desktop_action_uses_clean_chromium_profile_and_no_no_sandbox(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    command = manager._desktop_action_command("browser", url="https://example.test/report")
    assert command is not None
    assert command[:2] == ["bash", "-lc"]
    launch_script = command[-1]
    syntax = subprocess.run(["bash", "-n"], input=launch_script, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    assert "--no-sandbox" not in launch_script
    assert "--disable-dev-shm-usage" in launch_script
    assert "--no-first-run" in launch_script
    assert "--no-default-browser-check" in launch_script
    assert "/usr/bin/chromium-base" in launch_script
    assert "glasshive-browser-native-host-bootstrap" in launch_script
    assert "--start-maximized" in launch_script
    assert "--new-tab" in launch_script
    assert "bookmark_bar" in launch_script
    assert "show_on_all_tabs" in launch_script
    assert "https://example.test/report" in launch_script


def test_prime_idle_desktop_uses_clean_chromium_profile_and_no_no_sandbox(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, list[str]]] = []

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((container_name, command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._prime_idle_desktop("wpr-test")

    assert calls
    script = calls[-1][1][-1]
    assert calls[-1][1][:2] == ["bash", "-lc"]
    syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    assert "--no-sandbox" not in script
    assert "--disable-dev-shm-usage" in script
    assert "--new-window" in script
    assert "nohup /usr/bin/chromium-base" in script
    assert "glasshive-browser-native-host-bootstrap" in script
    assert "bookmark_bar" in script
    assert "show_on_all_tabs" in script
    assert "wmctrl -xa chromium.Chromium" in script


def test_desktop_action_skips_heavy_path_repair_for_running_container(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []
    envs: list[dict] = []

    class FakeSandbox:
        container_name = "wpr-test"
        state = "running"
        container_id = "cid"
        pid = 1234
        image = "img"
        novnc_port = 57900
        selenium_port = 57901
        openclaw_port = 57902

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(f"exec:{detach}:{fire_and_forget}:{command[0]}")
        envs.append(dict(env or {}))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    launched = manager.desktop_action("wrk_test", "codex-cli", "focus_browser")

    assert launched["status"] == "launched"
    assert "writable" not in calls
    assert calls == ["provider_tmp", "harden", "exec:True:True:bash"]
    assert envs[-1]["TMPDIR"] == manager._browser_tmp_dir()
    assert envs[-1]["XDG_CACHE_HOME"] == manager._browser_cache_dir()
    assert envs[-1]["XDG_CONFIG_HOME"] == manager._browser_config_dir()


def test_desktop_action_revalidates_projected_worker_without_container_evidence(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    sandbox = SandboxInfo(
        container_name="wpr-wrk-test",
        container_id="container123",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=1234,
        image="img",
        novnc_port=None,
    )
    manager.ensure_ready = lambda *args, **kwargs: calls.append("ensure") or sandbox  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(f"{container_name}:{detach}:{fire_and_forget}:{command[0]}")
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    launched = manager.desktop_action(
        "wrk_test",
        "codex-cli",
        "focus_browser",
        worker={"worker_id": "wrk_test", "state": "ready", "state_dir": str(tmp_path / "state")},
    )

    assert launched["status"] == "launched"
    assert launched["view_url"] is None
    assert calls == ["ensure", "wpr-wrk-test:True:True:bash"]


@pytest.mark.parametrize(
    ("runtime_name", "action", "provider_environment", "absent_selectors"),
    (
        (
            "claude-code",
            "claude",
            {
                "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/workspace/.provider-account/claude",
            },
            {"CODEX_HOME", "CLAUDE_CONFIG_DIR"},
        ),
        (
            "codex-cli",
            "codex",
            {"CODEX_HOME": "/workspace/.wpr-home/.codex"},
            {"CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"},
        ),
    ),
)
def test_desktop_action_projects_only_the_validated_personal_provider_home(
    tmp_path,
    runtime_name,
    action,
    provider_environment,
    absent_selectors,
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    captured_env: dict[str, str] = {}
    sandbox = SandboxInfo(
        container_name="wpr-wrk-test",
        container_id="container123",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=1234,
        image="img",
        novnc_port=None,
    )
    manager.ensure_ready = lambda *args, **kwargs: sandbox  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        captured_env.update(dict(env or {}))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_test",
        "state": "running",
        "execution_mode": "docker",
        "bootstrap_bundle": {
            "provider_account": {
                "policy": "personal_required",
                "account_id": "acct_claude",
            }
        },
        "_glasshive_provider_account_bound": True,
        "_glasshive_provider_account_env": provider_environment,
    }

    launched = manager.desktop_action(
        "wrk_test",
        runtime_name,
        action,
        worker=worker,
    )

    assert launched["status"] == "launched"
    for selector, selector_value in provider_environment.items():
        assert captured_env[selector] == selector_value
    assert absent_selectors.isdisjoint(captured_env)


def test_ensure_ready_skips_image_probe_for_existing_container(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    class FakeSandbox:
        container_name = "wpr-test"
        state = "running"
        container_id = "cid"
        workspace_dir = str(tmp_path / "workspace")
        home_dir = str(tmp_path / "home")
        pid = 1234
        image = "img"
        novnc_port = 57900
        selenium_port = 57901
        openclaw_port = 57902

    manager._require_docker = lambda: calls.append("require")  # type: ignore[method-assign]
    manager._ensure_image = lambda: calls.append("image")  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append("host_dirs")  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append("seed")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    assert sandbox.container_name == "wpr-test"
    assert calls == ["require", "host_dirs", "seed", "provider_tmp", "harden"]


def test_ensure_ready_builds_container_when_projected_worker_inspect_misses(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    inspect_calls = 0

    def fake_inspect(worker_id: str):
        nonlocal inspect_calls
        inspect_calls += 1
        if inspect_calls == 1:
            return None
        return SandboxInfo(
            container_name="wpr-wrk-test",
            container_id="container123",
            state="running",
            workspace_dir=str(tmp_path / "workspace"),
            home_dir=str(tmp_path / "home"),
            pid=1234,
            image="img",
        )

    manager._require_docker = lambda: calls.append("require")  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append("host_dirs")  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append("seed")  # type: ignore[method-assign]
    manager.inspect = fake_inspect  # type: ignore[method-assign]
    manager._ensure_image = lambda: calls.append("image")  # type: ignore[method-assign]
    manager._create_container = lambda container_name, paths, worker=None: calls.append(f"create:{container_name}")  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")}, "openclaw")

    assert sandbox.container_name == "wpr-wrk-test"
    assert sandbox.state == "running"
    assert calls == [
        "require",
        "host_dirs",
        "seed",
        "image",
        "create:wpr-wrk-test",
        "writable",
        "provider_tmp",
        "harden",
        "background",
        "prime",
    ]


def test_ensure_image_uses_short_probe_and_caches_success(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[list[str], float | None]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        calls.append((args, timeout_sec))
        payload = [{"Config": {"Labels": manager._expected_image_provenance()}}]
        return subprocess.CompletedProcess(
            ["docker", *args], returncode=0, stdout=json.dumps(payload), stderr=""
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()
    manager._ensure_image()

    assert calls == [(["image", "inspect", manager.image], manager.image_inspect_timeout_sec)]


def test_ensure_image_uses_dedicated_long_build_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_DOCKER_IMAGE_BUILD_TIMEOUT_SEC", "777")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[list[str], float | None]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        calls.append((args, timeout_sec))
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    assert calls[-1][0][:2] == ["build", "-t"]
    assert calls[-1][1] == 777


def test_ensure_image_includes_document_delivery_toolchain(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    requirements_lock = (manager.build_root / "workstation-requirements.lock").read_text()
    assert "libreoffice-writer" in dockerfile
    assert "libreoffice-impress" in dockerfile
    assert "pandoc" in dockerfile
    assert "poppler-utils" in dockerfile
    assert "acl" in dockerfile
    assert "python-docx==" in requirements_lock
    assert "python-pptx==" in requirements_lock
    assert "reportlab==" in requirements_lock
    assert "requests==" in requirements_lock
    assert "pymupdf==" in requirements_lock
    assert "--require-hashes --no-deps" in dockerfile
    assert "/usr/bin/locale-check" in dockerfile


def test_ensure_image_retry_replaces_read_only_reviewed_requirements_lock(tmp_path):
    def fake_docker(
        args: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        timeout_sec=None,
    ):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    first = DockerSandboxManager(base_dir=str(tmp_path))
    first._docker = fake_docker  # type: ignore[method-assign]
    first._ensure_image()

    requirements_lock = first.build_root / "workstation-requirements.lock"
    requirements_lock.chmod(0o444)

    retried = DockerSandboxManager(base_dir=str(tmp_path))
    retried._docker = fake_docker  # type: ignore[method-assign]
    retried._ensure_image()

    assert requirements_lock.read_bytes() == AI_WORKER_PYTHON_LOCK_PATH.read_bytes()


def test_ensure_image_defaults_to_no_forced_ai_worker_browser_extensions(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    assert manager.image.endswith(":phase1-node22-docs8-openclaw2026.7.1-6")
    assert "FROM selenium/standalone-chromium:4.46.0-20260707@sha256:" in dockerfile
    assert "com.glasshive.workstation.provenance=reviewed-v1" in dockerfile
    assert "com.glasshive.workstation.provider-account-acl=required-v1" in dockerfile
    assert AI_WORKER_APT_SNAPSHOT == "20260801T000000Z"
    assert f"com.glasshive.workstation.apt-snapshot={AI_WORKER_APT_SNAPSHOT}" in dockerfile
    assert f"com.glasshive.workstation.openai-plugins-commit={OPENAI_PLUGIN_MARKETPLACE_COMMIT}" in dockerfile
    assert OPENAI_PLUGIN_MARKETPLACE_ORIGIN in dockerfile
    assert OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH in dockerfile
    assert f"rev-parse HEAD)\" = {OPENAI_PLUGIN_MARKETPLACE_COMMIT}" in dockerfile
    assert f"snapshot.ubuntu.com/ubuntu/{AI_WORKER_APT_SNAPSHOT}" in dockerfile
    assert "nodejs_22.23.2-1nodesource1_${arch}.deb" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "@openai/codex@0.155.0-alpha.9.2" in dockerfile
    assert "@anthropic-ai/claude-code@2.1.280" in dockerfile
    assert "--cache /tmp/glasshive-npm-cache" in dockerfile
    assert "rm -rf /tmp/glasshive-npm-cache /root/.npm /home/seluser/.npm" in dockerfile
    assert "/etc/chromium/policies/managed/glasshive-ai-worker-extensions.json" in dockerfile
    assert "/etc/opt/chrome/policies/managed/glasshive-ai-worker-extensions.json" in dockerfile
    assert "ExtensionInstallForcelist" in dockerfile
    assert "ExtensionInstallForcelist\":[]" in dockerfile
    assert "fcoeoabgfenejglbffodgkkbkcdhcgfn;https://clients2.google.com/service/update2/crx" not in dockerfile
    assert "hehggadaopoacecdllhhajmbjkdcmajg;https://clients2.google.com/service/update2/crx" not in dockerfile
    assert "glasshive-browser-extension-check" in dockerfile
    assert "glasshive-browser-native-host-bootstrap" in dockerfile


def test_custom_worker_image_without_reviewed_provenance_fails_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_SANDBOX_IMAGE", "example.invalid/custom-worker:test")
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], **kwargs):
        payload = [{"Config": {"Labels": {}}}]
        return subprocess.CompletedProcess(
            ["docker", *args], returncode=0, stdout=json.dumps(payload), stderr=""
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="provenance labels"):
        manager._ensure_image()


def test_ensure_image_consumes_reviewed_openclaw_lock_and_disables_bonjour(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    staged_lock = manager.build_root / "openclaw-runtime-lock" / "package-lock.json"
    assert staged_lock.is_file()
    assert hashlib.sha256(staged_lock.read_bytes()).hexdigest() == OPENCLAW_RUNTIME_LOCK_SHA256
    assert "COPY openclaw-runtime-lock/ /opt/glasshive-openclaw-runtime/" in dockerfile
    assert "npm ci --omit=dev" in dockerfile
    assert "node_modules/openclaw/package.json" in dockerfile
    assert "node_modules/fast-uri/package.json" in dockerfile
    assert OPENCLAW_RUNTIME_VERSION in dockerfile
    assert OPENCLAW_RUNTIME_FAST_URI_VERSION in dockerfile
    assert f"grep -Fq 'OpenClaw {OPENCLAW_RUNTIME_VERSION} ('" in dockerfile
    assert "/usr/local/bin/openclaw" in dockerfile
    assert "ENV OPENCLAW_DISABLE_BONJOUR=1" in dockerfile
    assert "openclaw@latest" not in dockerfile


def test_ensure_image_fails_closed_when_reviewed_openclaw_lock_drifts(tmp_path, monkeypatch):
    invalid_lock_root = tmp_path / "invalid-openclaw-lock"
    invalid_lock_root.mkdir()
    (invalid_lock_root / "package.json").write_text("{}\n")
    (invalid_lock_root / "package-lock.json").write_text("{}\n")
    monkeypatch.setattr(
        "workers_projects_runtime.openclaw_release.OPENCLAW_RUNTIME_LOCK_DIR",
        invalid_lock_root,
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path / "data"))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        pytest.fail("Docker build must not start with an unreviewed OpenClaw lock")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="OpenClaw runtime lock"):
        manager._ensure_image()


def test_ensure_image_can_opt_in_to_ai_worker_browser_extension_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("WPR_AI_WORKER_BROWSER_EXTENSIONS", "all")
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    assert "fcoeoabgfenejglbffodgkkbkcdhcgfn;https://clients2.google.com/service/update2/crx" in dockerfile
    assert "hehggadaopoacecdllhhajmbjkdcmajg;https://clients2.google.com/service/update2/crx" in dockerfile
    assert "glasshive-browser-extension-check" in dockerfile
    assert "glasshive-browser-native-host-bootstrap" in dockerfile
    assert "com.anthropic.claude_code_browser_extension" in dockerfile
    assert "com.openai.codexextension" in dockerfile


def test_ai_worker_browser_native_host_scripts_default_to_disabled(tmp_path):
    bootstrap_script = _ai_worker_browser_native_host_bootstrap_script()
    check_script = _ai_worker_browser_extension_check_script()
    for script in (bootstrap_script, check_script):
        syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert syntax.returncode == 0, syntax.stderr

    result = subprocess.run(
        ["bash", "-c", bootstrap_script],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "claude-code native-host disabled" in result.stdout
    assert "codex native-host disabled" in result.stdout
    assert not (tmp_path / "home" / ".config" / "chromium" / "NativeMessagingHosts").exists()


def test_ai_worker_browser_native_host_scripts_remove_disabled_managed_extension_state(tmp_path):
    bootstrap_script = _ai_worker_browser_native_host_bootstrap_script()
    home = tmp_path / "home"
    for extension_id in ("fcoeoabgfenejglbffodgkkbkcdhcgfn", "hehggadaopoacecdllhhajmbjkdcmajg"):
        stale = home / ".config" / "chromium" / "Default" / "Extensions" / extension_id / "1.0_0"
        stale.mkdir(parents=True)
        (stale / "manifest.json").write_text("{}\n")
    native_dir = home / ".config" / "chromium" / "NativeMessagingHosts"
    native_dir.mkdir(parents=True)
    for host in ("com.anthropic.claude_code_browser_extension", "com.openai.codexextension"):
        (native_dir / f"{host}.json").write_text("{}\n")

    result = subprocess.run(
        ["bash", "-c", bootstrap_script],
        env={
            **os.environ,
            "HOME": str(home),
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (home / ".config" / "chromium" / "Default" / "Extensions" / "fcoeoabgfenejglbffodgkkbkcdhcgfn").exists()
    assert not (home / ".config" / "chromium" / "Default" / "Extensions" / "hehggadaopoacecdllhhajmbjkdcmajg").exists()
    assert not (native_dir / "com.anthropic.claude_code_browser_extension.json").exists()
    assert not (native_dir / "com.openai.codexextension.json").exists()


def test_ai_worker_browser_native_host_scripts_are_valid_and_install_claude_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("WPR_AI_WORKER_BROWSER_EXTENSIONS", "claude,codex")
    bootstrap_script = _ai_worker_browser_native_host_bootstrap_script()
    check_script = _ai_worker_browser_extension_check_script()
    for script in (bootstrap_script, check_script):
        syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert syntax.returncode == 0, syntax.stderr
        script_path = tmp_path / "roundtrip-script"
        dockerfile_lines = " ".join(shlex.quote(line) for line in script.splitlines())
        roundtrip = subprocess.run(
            ["bash", "-lc", f"printf '%s\\n' {dockerfile_lines} > {shlex.quote(str(script_path))}; bash -n {shlex.quote(str(script_path))}"],
            text=True,
            capture_output=True,
        )
        assert roundtrip.returncode == 0, roundtrip.stderr

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/usr/bin/env sh\nexit 0\n")
    fake_claude.chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", bootstrap_script],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "claude-code native-host installed" in result.stdout

    wrapper = tmp_path / "home" / ".claude" / "chrome" / "chrome-native-host"
    assert wrapper.exists()
    assert os.access(wrapper, os.X_OK)
    assert str(fake_claude) in wrapper.read_text()
    assert "--chrome-native-host" in wrapper.read_text()

    for browser in ("chromium", "google-chrome"):
        manifest = (
            tmp_path
            / "home"
            / ".config"
            / browser
            / "NativeMessagingHosts"
            / "com.anthropic.claude_code_browser_extension.json"
        )
        data = json.loads(manifest.read_text())
        assert data["name"] == "com.anthropic.claude_code_browser_extension"
        assert data["path"] == str(wrapper)
        assert data["type"] == "stdio"
        assert data["allowed_origins"] == ["chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/"]

    codex_manifest = (
        tmp_path
        / "home"
        / ".config"
        / "chromium"
        / "NativeMessagingHosts"
        / "com.openai.codexextension.json"
    )
    assert not codex_manifest.exists()
    assert "codex native-host pending: extension-host bundle not found" in result.stdout


def test_desktop_env_forwards_codex_native_host_provisioning(monkeypatch, tmp_path):
    monkeypatch.setenv("WPR_CODEX_CHROME_PLUGIN_ROOT", "/opt/codex-chrome")
    monkeypatch.setenv("CODEX_CHROME_PLUGIN_ROOT", "/workspace/.wpr-home/.codex/chrome")
    monkeypatch.setenv("WPR_CODEX_NODE_REPL_PATH", "/opt/codex-node-repl")
    monkeypatch.setenv("CODEX_NODE_REPL_PATH", "/workspace/.wpr-home/.codex/node_repl")

    env = DockerSandboxManager(base_dir=str(tmp_path))._desktop_env()

    assert env["WPR_CODEX_CHROME_PLUGIN_ROOT"] == "/opt/codex-chrome"
    assert env["CODEX_CHROME_PLUGIN_ROOT"] == "/workspace/.wpr-home/.codex/chrome"
    assert env["WPR_CODEX_NODE_REPL_PATH"] == "/opt/codex-node-repl"
    assert env["CODEX_NODE_REPL_PATH"] == "/workspace/.wpr-home/.codex/node_repl"


def test_ai_worker_browser_native_host_bootstrap_installs_codex_manifest_when_bundle_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("WPR_AI_WORKER_BROWSER_EXTENSIONS", "claude,codex")
    bootstrap_script = _ai_worker_browser_native_host_bootstrap_script()
    syntax = subprocess.run(["bash", "-n"], input=bootstrap_script, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("claude", "codex", "node"):
        fake = fake_bin / name
        fake.write_text("#!/usr/bin/env sh\nexit 0\n")
        fake.chmod(0o755)
    fake_node_repl = tmp_path / "node_repl"
    fake_node_repl.write_text("#!/usr/bin/env sh\nexit 0\n")
    fake_node_repl.chmod(0o755)

    plugin_root = tmp_path / "home" / ".codex" / "plugins" / "cache" / "openai-bundled" / "chrome" / "26.616.71553"
    for arch in ("arm64", "x64"):
        extension_host = plugin_root / "extension-host" / "linux" / arch / "extension-host"
        extension_host.parent.mkdir(parents=True, exist_ok=True)
        extension_host.write_text("#!/usr/bin/env sh\nexit 0\n")
        extension_host.chmod(0o755)
    (plugin_root / "scripts").mkdir(parents=True, exist_ok=True)
    (plugin_root / "scripts" / "browser-client.mjs").write_text("export {};\n")

    result = subprocess.run(
        ["bash", "-c", bootstrap_script],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "WPR_CODEX_NODE_REPL_PATH": str(fake_node_repl),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "codex native-host installed" in result.stdout

    manifest = (
        tmp_path
        / "home"
        / ".config"
        / "chromium"
        / "NativeMessagingHosts"
        / "com.openai.codexextension.json"
    )
    data = json.loads(manifest.read_text())
    assert data["name"] == "com.openai.codexextension"
    assert data["type"] == "stdio"
    assert data["allowed_origins"] == ["chrome-extension://hehggadaopoacecdllhhajmbjkdcmajg/"]
    assert data["path"].startswith(str(plugin_root / "extension-host" / "linux"))

    config = json.loads((Path(data["path"]).with_name("extension-host-config.json")).read_text())
    assert config["schemaVersion"] == 1
    assert config["browserClientPath"] == str(plugin_root / "scripts" / "browser-client.mjs")
    assert config["codexCliPath"] == str(fake_bin / "codex")
    assert config["nodePath"] == str(fake_bin / "node")
    assert config["nodeReplPath"] == str(fake_node_repl)
    assert config["extensionId"] == "hehggadaopoacecdllhhajmbjkdcmajg"


def test_start_screen_session_prepares_runtime_dir_and_detaches(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str | None, list[str], bool, dict | None]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.ensure_ready = lambda *args, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    manager.stop_screen_session = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((user, command, detach, env))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.start_screen_session(
        "wrk_test",
        "codex-cli",
        "job-run_123456",
        ["echo", "ok"],
        env={
            "OPENAI_API_KEY": "secret",
            "API_TIMEOUT_MS": "240000",
            "PATH": "/usr/bin:/bin",
        },
    )

    assert calls[0][0] == "root"
    assert "mkdir -p /run/screen" in calls[0][1][-1]
    assert calls[1][1][:2] == ["screen", "-DmS"]
    assert calls[1][2] is True
    assert calls[1][3]["PATH"] == "/usr/bin:/bin"
    assert calls[1][3]["OPENAI_API_KEY"] == "secret"
    assert calls[1][3]["API_TIMEOUT_MS"] == "240000"


def test_parallel_clean_room_screen_runtime_is_preowned_and_never_chowned(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str | None, list[str]]] = []

    def fake_docker_exec(
        container_name,
        command,
        *,
        env=None,
        cwd=None,
        detach=False,
        fire_and_forget=False,
        user=None,
        input_text=None,
    ):
        calls.append((user, command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._ensure_screen_runtime_dir("wpr-clean", clean_room=True)

    assert any(
        entry.startswith("/run/screen:")
        and "mode=1777" in entry
        and "uid=1200" in entry
        and "gid=1201" in entry
        for entry in PARALLEL_CLEAN_ROOM_TMPFS
    )
    assert calls == [(manager.user, calls[0][1])]
    assert "chown" not in calls[0][1][-1]
    assert "chmod 1777 /run/screen" in calls[0][1][-1]
    assert "chmod 700 /run/screen/S-seluser" in calls[0][1][-1]

def test_screen_session_pid_reads_matching_screen_socket(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        if command[:2] == ["bash", "-lc"]:
            assert command[-1] == "job-run_123456"
            return subprocess.CompletedProcess(["docker"], returncode=0, stdout="12345\n", stderr="")
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    assert manager.screen_session_pid("wrk_test", "codex-cli", "job-run_123456") == 12345
    assert any("mkdir -p /run/screen" in command[-1] for command in calls)


def test_screen_session_pid_survives_refused_runtime_dir_preparation(tmp_path):
    """A live clean-room session must stay identifiable after start even when the container
    user can no longer chmod /run/screen: the pid lookup is read-only and ``screen -ls`` is
    the authority."""
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    class FakeSandbox:
        container_name = "wpr-clean"
        execution_policy = PARALLEL_CLEAN_ROOM_EXECUTION_POLICY

    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]

    def refused_prepare(container_name, *, clean_room=False):
        raise RuntimeError(
            f"Failed to prepare screen runtime directory in {container_name}: "
            "chmod: changing permissions of '/run/screen': Operation not permitted"
        )

    manager._ensure_screen_runtime_dir = refused_prepare  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        if command[:2] == ["bash", "-lc"]:
            assert command[-1] == "job-run_654321"
            return subprocess.CompletedProcess(["docker"], returncode=0, stdout="543\n", stderr="")
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    assert manager.screen_session_pid("wrk_clean", "claude-code", "job-run_654321") == 543
    assert calls and calls[-1][:2] == ["bash", "-lc"]


def test_exact_release_session_probe_never_ensures_or_repairs_container(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager.ensure_ready = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("release teardown must not ensure or repair a sandbox")
    )
    manager._docker_exec = lambda *_args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args=["docker", "exec"],
        returncode=0,
        stdout="\t123.job-run_exact\t(Detached)\n",
        stderr="",
    )

    sessions = manager.list_screen_sessions(
        "wrk_exact_probe",
        "codex-cli",
        worker={
            "worker_id": "wrk_exact_probe",
            "_compute_release_container_id": "a" * 64,
        },
    )

    assert sessions == ["job-run_exact"]

def test_start_screen_session_revalidates_projected_worker_without_container_evidence(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, list[str], bool, bool]] = []

    sandbox = SandboxInfo(
        container_name="wpr-wrk-test",
        container_id="container123",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=1234,
        image="img",
    )
    manager.ensure_ready = lambda *args, **kwargs: calls.append(("ensure", [], False, False)) or sandbox  # type: ignore[method-assign]
    manager.stop_screen_session = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((container_name, command, detach, fire_and_forget))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.start_screen_session(
        "wrk_test",
        "codex-cli",
        "job-run_fast",
        ["echo", "ok"],
        worker={"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")},
    )

    assert calls[0] == ("ensure", [], False, False)
    assert calls[1][0] == "wpr-wrk-test"
    assert "mkdir -p /run/screen" in calls[1][1][-1]
    assert calls[2] == ("wpr-wrk-test", ["screen", "-DmS", "job-run_fast", "echo", "ok"], True, False)


def test_stop_screen_session_targets_all_exact_duplicate_sockets(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.ensure_ready = lambda *args, **kwargs: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.stop_screen_session(
        "wrk_test",
        "openclaw",
        "openclaw-gateway",
        worker={"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")},
    )

    script = calls[0][2]
    assert calls[0][-1] == "openclaw-gateway"
    assert "matching_sockets()" in script
    assert "if (name == target) print socket" in script
    assert 'screen -S "$socket" -X quit' in script
    assert "sockets=$(matching_sockets)" in script
    assert "Screen session remains after quit" in script
    assert "exit 43" in script


def test_stop_screen_session_targets_captured_container_generation(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, list[str]]] = []

    def reject_mutable_lookup(*_args, **_kwargs):
        raise AssertionError("exact cleanup must not resolve the mutable worker container")

    manager.inspect = reject_mutable_lookup  # type: ignore[method-assign]
    manager._docker_exec = (  # type: ignore[method-assign]
        lambda container_name, command, **_kwargs: (
            calls.append((container_name, command))
            or subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")
        )
    )

    manager.stop_screen_session(
        "wrk_test",
        "codex-cli",
        "job-run_exact",
        worker={"worker_id": "wrk_test", "state": "running"},
        expected_container_id="container-generation-a",
        missing_ok=True,
    )

    assert len(calls) == 1
    assert calls[0][0] == "container-generation-a"
    assert calls[0][1][-1] == "job-run_exact"

def test_terminate_run_processes_targets_run_env_and_descendants(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.ensure_ready = lambda *args, **kwargs: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((command, env))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.terminate_run_processes(
        "wrk_test",
        "openclaw",
        "run_123",
        worker={"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")},
    )

    command, env = calls[0]
    script = command[-1]
    assert "GLASSHIVE_ACTIVE_RUN_ID=$run_id" in script
    assert "descendants()" in script
    assert "/workspace/.wpr-home/.glasshive-runs/run_123" not in script
    assert env is not None
    assert env["GLASSHIVE_STOP_RUN_ROOT"] == "/workspace/.wpr-home/.glasshive-runs/run_123"
    assert env["GLASSHIVE_STOP_RUN_ID"] == "run_123"
    assert "self_pid=$$" in script
    assert '$1 != self_pid' in script
    assert "process_spec()" in script
    assert "capture_specs()" in script
    assert "capture_pgids()" in script
    assert "alive_pids()" in script
    assert "alive_pgids()" in script
    assert "signal_groups()" in script
    assert "self_pgid=" in script
    assert "expected_start=${spec#*:}" in script
    assert "specs=$(merge_specs" in script
    assert "pgids=$(merge_specs" in script
    assert script.count("discovered=$(matching_pids)") == 2
    assert script.count('kill -KILL "$pid"') == 2
    assert "signal_groups KILL $surviving_groups" in script
    assert "survivors=" in script
    assert "exit 43" in script


@pytest.mark.parametrize(
    ("returncode", "detail"),
    [
        (43, "Run processes remain after TERM/KILL: 4242"),
        (125, "docker exec failed"),
    ],
)
def test_terminate_run_processes_propagates_nonzero_and_survivor_results(tmp_path, returncode, detail):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        return subprocess.CompletedProcess(["docker"], returncode=returncode, stdout="", stderr=detail)

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match=detail):
        manager.terminate_run_processes(
            "wrk_test",
            "claude-code",
            "run_123",
            worker={"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")},
        )


def test_terminate_run_processes_does_not_match_its_own_cleanup_shell(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def run_cleanup_locally(_container_name, command, **_kwargs):
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )

    manager._docker_exec = run_cleanup_locally  # type: ignore[method-assign]

    manager.terminate_run_processes(
        "wrk_cleanup_self",
        "codex-cli",
        "run_cleanup_self",
        worker={"worker_id": "wrk_cleanup_self", "state": "running"},
    )

def test_terminate_run_processes_targets_captured_container_generation(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, list[str]]] = []

    def reject_mutable_lookup(*_args, **_kwargs):
        raise AssertionError("exact cleanup must not resolve the mutable worker container")

    manager.inspect = reject_mutable_lookup  # type: ignore[method-assign]
    manager._docker_exec = (  # type: ignore[method-assign]
        lambda container_name, command, **_kwargs: (
            calls.append((container_name, command))
            or subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")
        )
    )

    manager.terminate_run_processes(
        "wrk_test",
        "codex-cli",
        "run_exact",
        worker={"worker_id": "wrk_test", "state": "running"},
        expected_container_id="container-generation-a",
        missing_ok=True,
    )

    assert len(calls) == 1
    assert calls[0][0] == "container-generation-a"
    assert "/workspace/.wpr-home/.glasshive-runs/run_exact" in calls[0][1][-1]

@pytest.mark.parametrize("operation", ["screen", "run"])
def test_exact_generation_cleanup_accepts_confirmed_missing_container(tmp_path, operation):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager.inspect = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("exact cleanup must not inspect a replacement generation")
    )
    manager._docker_exec = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["docker"],
            returncode=1,
            stdout="",
            stderr="Error response from daemon: No such container: container-generation-a",
        )
    )

    if operation == "screen":
        manager.stop_screen_session(
            "wrk_test",
            "codex-cli",
            "job-run_exact",
            expected_container_id="container-generation-a",
            missing_ok=True,
        )
    else:
        manager.terminate_run_processes(
            "wrk_test",
            "codex-cli",
            "run_exact",
            expected_container_id="container-generation-a",
            missing_ok=True,
        )

def test_terminate_run_processes_surfaces_docker_exec_failure(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    class FakeSandbox:
        container_name = "wpr-test"

    manager.ensure_ready = lambda *args, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    manager._docker_exec = (  # type: ignore[method-assign]
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["docker"], returncode=125, stdout="", stderr="docker exec unavailable"
        )
    )

    with pytest.raises(RuntimeError, match="Failed to terminate exact run processes"):
        manager.terminate_run_processes(
            "wrk_test",
            "codex-cli",
            "run_failure",
            worker={
                "worker_id": "wrk_test",
                "state": "running",
                "state_dir": str(tmp_path / "state"),
            },
        )

def test_ensure_ready_repairs_bind_mount_ownership_before_prime(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    class FakeSandbox:
        def __init__(self, state: str):
            self.container_name = "wpr-test"
            self.state = state
            self.container_id = "cid"
            self.workspace_dir = str(tmp_path / "workspace")
            self.home_dir = str(tmp_path / "home")
            self.pid = 1234
            self.image = "img"
            self.novnc_port = 57900
            self.selenium_port = 57901
            self.openclaw_port = 57902

    sandbox_states = [None, FakeSandbox("running")]

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager._create_container = lambda *args, **kwargs: calls.append("create")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(f"{user}:{command[-1]}")
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    assert calls[0] == "create"
    assert calls[1].startswith("root:")
    assert "chown -R" not in calls[1]
    assert "chmod -R" not in calls[1]
    assert (
        "find /workspace/project /workspace/.wpr-home /workspace/.wpr-home/tmp "
        "/workspace/.wpr-home/.cache /workspace/.wpr-home/.config -type d -exec setfacl"
    ) in calls[1]
    assert calls[2].startswith("root:set -e; runtime_uid=$(id -u seluser)")
    assert calls[3].startswith("root:set -e; for file in /workspace/.wpr-home/.glasshive/secret-runtime.env")
    assert calls[4:] == ["background", "prime"]


@pytest.mark.parametrize("projected", [False, True])
def test_multi_user_ensure_ready_always_repairs_private_roots(tmp_path, monkeypatch, projected):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, object]] = []

    class FakeSandbox:
        container_name = "wpr-test"
        container_id = "cid"
        state = "running"
        workspace_dir = str(tmp_path / "workspace")
        home_dir = str(tmp_path / "home")
        pid = 1234
        image = "img"
        novnc_port = 57900
        selenium_port = 57901
        openclaw_port = 57902

    sandbox = FakeSandbox()
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append(("host", paths))  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append(("seed", args))  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: None if projected else sandbox  # type: ignore[method-assign]
    manager.fast_sandbox_from_worker = lambda worker: sandbox if projected else None  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda container_name, paths: calls.append(("repair", paths))  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: None  # type: ignore[method-assign]

    resolved = manager.ensure_ready(
        {"worker_id": "wrk_test", "container_id": "cid" if projected else ""},
        "codex-cli",
    )

    assert resolved is sandbox
    assert ("repair", manager._default_writable_container_paths()) in calls


def test_ensure_container_writable_paths_repairs_specific_run_dir(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str | None, list[str]]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((user, command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.ensure_container_writable_paths(
        "wrk_test",
        "codex-cli",
        ["/workspace/.wpr-home/.glasshive-runs/run_123"],
    )

    assert calls == [
        (
            "root",
            [
                    "bash",
                    "-c",
                    "set -e; mkdir -p /workspace/.wpr-home/.glasshive-runs/run_123; "
                    "find /workspace/.wpr-home/.glasshive-runs/run_123 -xdev ! -type s ! -type l "
                    "! -user seluser -exec chown -h seluser {} +; "
                    "find /workspace/.wpr-home/.glasshive-runs/run_123 -xdev ! -type s "
                    "! -type l -exec chmod u+rwX,go-rwx {} +",
            ],
        )
    ]


@pytest.mark.parametrize(
    ("environment_name", "environment_value"),
    [
        ("GLASSHIVE_ENTERPRISE_MODE", "true"),
        ("GLASSHIVE_ENTERPRISE_MODE", "enabled"),
        ("WPR_ENTERPRISE_MODE", "1"),
        ("WPR_ENTERPRISE_MODE", "enabled"),
        ("GLASSHIVE_SECURITY_MODE", "multi_user"),
    ],
)
def test_enterprise_writable_path_repair_fails_closed_without_posix_acl(
    tmp_path,
    monkeypatch,
    environment_name,
    environment_value,
):
    monkeypatch.setenv(environment_name, environment_value)
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        return subprocess.CompletedProcess(["docker"], returncode=1, stdout="", stderr="setfacl unavailable")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="POSIX ACL"):
        manager._ensure_container_writable_paths(
            "wpr-test",
            ["/workspace/.wpr-home/.glasshive-runs/run_123"],
        )

    assert len(calls) == 1
    repair_script = calls[0][-1]
    assert "command -v setfacl" in repair_script
    assert "u:root:rwX" in repair_script
    assert "g::---,o::---,m::rwX" in repair_script
    assert "d:u:root:rwX" in repair_script
    assert "d:g::---,d:o::---,d:m::rwX" in repair_script
    assert "chmod -R a+rwX" not in repair_script


def test_explicit_single_user_security_mode_keeps_local_acl_compatibility(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "enabled")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._ensure_container_writable_paths(
        "wpr-test",
        ["/workspace/.wpr-home/.glasshive-runs/run_123"],
    )

    assert "chmod -R a+rwX" in calls[0][-1]


def test_repair_provider_temp_ownership_restores_claude_runtime_directory(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str | None, list[str]]] = []

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((user, command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._repair_provider_temp_ownership("wpr-test")

    assert len(calls) == 1
    assert calls[0][0] == "root"
    script = calls[0][1][-1]
    assert "provider_tmp=/workspace/.wpr-home/tmp/claude-${runtime_uid}" in script
    assert 'chown -R "${runtime_uid}:${runtime_gid}" "$provider_tmp"' in script
    assert 'chmod 700 "$provider_tmp"' in script


def test_ensure_container_writable_paths_skips_pulse_runtime_symlink_on_fakeowner_mount(
    tmp_path,
):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    pulse_runtime = (
        "/workspace/.wpr-home/.config/pulse/"
        "synthetic-machine-runtime"
    )

    def fake_docker_exec(
        container_name,
        command,
        *,
        env=None,
        cwd=None,
        detach=False,
        fire_and_forget=False,
        user=None,
    ):
        script = command[-1]
        chown_repair = script.split(";", 3)[2]
        if "! -user seluser" not in chown_repair or "! -type l" not in chown_repair:
            return subprocess.CompletedProcess(
                ["docker"],
                returncode=1,
                stdout="",
                stderr=(
                    "chown: changing ownership of "
                    f"'{pulse_runtime}': Permission denied"
                ),
            )
        return subprocess.CompletedProcess(
            ["docker"], returncode=0, stdout="", stderr=""
        )

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._ensure_container_writable_paths(
        "wpr-test",
        ["/workspace/.wpr-home/.config"],
    )

def test_create_container_applies_default_resource_caps(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    commands: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        commands.append(args)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="cid", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    command = next(item for item in commands if item and item[0] == "run")
    assert command[command.index("--shm-size") + 1] == "1g"
    assert command[command.index("--memory") + 1] == "3g"
    assert command[command.index("--memory-swap") + 1] == "3g"
    assert command[command.index("--cpus") + 1] == "2"
    assert command[command.index("--pids-limit") + 1] == "512"
    assert command[-1] == manager.image


def test_ensure_ready_primes_idle_desktop_when_container_is_new(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    class FakeSandbox:
        def __init__(self, state: str):
            self.container_name = "wpr-test"
            self.state = state
            self.container_id = "cid"
            self.workspace_dir = str(tmp_path / "workspace")
            self.home_dir = str(tmp_path / "home")
            self.pid = 1234
            self.image = "img"
            self.novnc_port = 57900
            self.selenium_port = 57901
            self.openclaw_port = 57902

    sandbox_states = [None, FakeSandbox("running")]

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager._create_container = lambda *args, **kwargs: calls.append("create")  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")
    assert calls == ["create", "writable", "provider_tmp", "harden", "background", "prime"]
    marker = json.loads((manager._paths("wrk_test")["state_dir"] / "desktop-prime.json").read_text())
    assert marker["schema"] == "glasshive.desktop_prime.v1"
    assert marker["status"] == "launched"
    assert marker["container_name"] == "wpr-test"
    assert marker["default_browser_url"].startswith("data:text/html")


def test_ensure_ready_records_idle_desktop_prime_failure(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    class FakeSandbox:
        def __init__(self, state: str):
            self.container_name = "wpr-test"
            self.state = state
            self.container_id = "cid"
            self.workspace_dir = str(tmp_path / "workspace")
            self.home_dir = str(tmp_path / "home")
            self.pid = 1234
            self.image = "img"
            self.novnc_port = 57900
            self.selenium_port = 57901
            self.openclaw_port = 57902

    sandbox_states = [None, FakeSandbox("running")]

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager._create_container = lambda *args, **kwargs: calls.append("create")  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]

    def fail_prime(container_name):
        calls.append("prime")
        raise RuntimeError("prime failed")

    manager._prime_idle_desktop = fail_prime  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="prime failed"):
        manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    marker = json.loads((manager._paths("wrk_test")["state_dir"] / "desktop-prime.json").read_text())
    assert calls == ["create", "writable", "provider_tmp", "harden", "background", "prime"]
    assert marker["status"] == "failed"
    assert "prime failed" in marker["detail"]


def test_ensure_ready_records_idle_desktop_prime_nonzero_return_as_failure(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    class FakeSandbox:
        def __init__(self, state: str):
            self.container_name = "wpr-test"
            self.state = state
            self.container_id = "cid"
            self.workspace_dir = str(tmp_path / "workspace")
            self.home_dir = str(tmp_path / "home")
            self.pid = 1234
            self.image = "img"
            self.novnc_port = 57900
            self.selenium_port = 57901
            self.openclaw_port = 57902

    sandbox_states = [None, FakeSandbox("running")]

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager._create_container = lambda *args, **kwargs: calls.append("create")  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append("prime")
        return subprocess.CompletedProcess(["docker"], returncode=42, stdout="", stderr="wmctrl failed")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Idle desktop prime failed"):
        manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    marker = json.loads((manager._paths("wrk_test")["state_dir"] / "desktop-prime.json").read_text())
    assert calls == ["create", "writable", "provider_tmp", "harden", "background", "prime"]
    assert marker["status"] == "failed"
    assert "wmctrl failed" in marker["detail"]
