"""Installed personal login keeps one credential owner and scoped worker authority."""
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from workers_projects_runtime import auth, conversation_provider, profile_runtime


OWNER = "a" * 24


@pytest.fixture
def installed(tmp_path, monkeypatch):
    state = tmp_path / "native-first-admin.json"
    state.write_text(json.dumps({"schema_version": 1, "status": "closed", "admin_user_id": OWNER}))
    state.chmod(0o600)
    home = tmp_path / "login-home"
    home.mkdir(mode=0o700)
    codex = home / ".codex"
    codex.mkdir(mode=0o700)
    (codex / "auth.json").write_text('{"OPENAI_API_KEY":"synthetic-local-only"}')
    (codex / "auth.json").chmod(0o600)
    monkeypatch.setenv("VIVENTIUM_NATIVE_FIRST_ADMIN_STATE", str(state))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    return state, home, codex


def test_installed_owner_is_opt_in_and_reuses_closed_authority(installed, monkeypatch):
    assert auth.native_installed_owner_id() == OWNER
    auth.require_native_installed_owner(OWNER)
    with pytest.raises(auth.GlassHiveAuthError, match="authenticated installed owner"):
        auth.require_native_installed_owner("b" * 24)
    monkeypatch.delenv("VIVENTIUM_NATIVE_FIRST_ADMIN_STATE")
    assert auth.native_installed_owner_id() is None
    auth.require_native_installed_owner("standalone-owner")


@pytest.mark.parametrize("invalid", ["pending", "token", "missing_owner", "mode", "link", "missing", "multi_user"])
def test_installed_owner_authority_fails_closed(installed, monkeypatch, invalid):
    state, _, _ = installed
    value = json.loads(state.read_text())
    if invalid == "mode":
        state.chmod(0o644)
    elif invalid == "link":
        moved = state.with_suffix(".original")
        state.rename(moved)
        state.symlink_to(moved)
    elif invalid == "missing":
        state.unlink()
    elif invalid == "multi_user":
        monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    else:
        if invalid == "pending":
            value["status"] = "pending"
        elif invalid == "token":
            value["token"] = "synthetic-stale-setup"
        else:
            value.pop("admin_user_id")
        state.write_text(json.dumps(value))
    with pytest.raises(auth.NativeOwnerUnavailableError, match="owner authority"):
        auth.require_native_installed_owner(OWNER)


def test_native_codex_links_cli_file_without_copying_worker_authority(installed, tmp_path, monkeypatch):
    _, _, source = installed
    target = tmp_path / "worker-codex"
    target.mkdir(mode=0o700)
    (target / "config.toml").write_text('developer_instructions="worker-specific"\n')
    monkeypatch.setattr(profile_runtime.shutil, "copy2", lambda *_: pytest.fail("must not copy credentials"))
    runtime = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path / "workers"))
    runtime._copy_host_codex_auth(target)
    runtime._copy_host_codex_auth(target)
    assert (target / "auth.json").is_symlink()
    assert (target / "auth.json").readlink() == source / "auth.json"
    assert "worker-specific" in (target / "config.toml").read_text()
    # The CLI's file backend writes through the link; no separate cached copy exists.
    with (target / "auth.json").open("w") as handle:
        handle.write('{"OPENAI_API_KEY":"synthetic-rotated-local-only"}')
    assert "rotated" in (source / "auth.json").read_text()
    (source / "auth.json").unlink()
    assert not (target / "auth.json").exists()
    with pytest.raises(profile_runtime.RuntimeErrorBase, match="reconnect"):
        runtime._copy_host_codex_auth(target)


@pytest.mark.parametrize("unsafe", ["file", "wrong_link", "source_mode", "source_link"])
def test_native_codex_never_replaces_unrelated_auth(installed, tmp_path, unsafe):
    _, _, source = installed
    target = tmp_path / "worker-codex"
    target.mkdir(mode=0o700)
    if unsafe == "file":
        (target / "auth.json").write_text("preserve-existing-copy")
    elif unsafe == "wrong_link":
        (target / "auth.json").symlink_to(tmp_path / "unrelated")
    elif unsafe == "source_mode":
        (source / "auth.json").chmod(0o644)
    else:
        (source / "auth.json").rename(source / "original.json")
        (source / "auth.json").symlink_to(source / "original.json")
    runtime = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path / "workers"))
    with pytest.raises(profile_runtime.RuntimeErrorBase, match="unsafe"):
        runtime._copy_host_codex_auth(target)
    if unsafe == "file":
        assert (target / "auth.json").read_text() == "preserve-existing-copy"


@pytest.mark.parametrize("profile", ["codex-cli", "claude-code"])
def test_native_direct_worker_cannot_consume_another_owners_login(installed, tmp_path, profile):
    runtime_type = profile_runtime.HostCodexCliRuntime if profile == "codex-cli" else profile_runtime.HostClaudeCodeRuntime
    runtime = runtime_type(base_dir=str(tmp_path / "workers"))
    worker = {"worker_id": "wrk_foreign", "owner_id": "b" * 24, "profile": profile}
    with pytest.raises(auth.GlassHiveAuthError, match="authenticated installed owner"):
        runtime._host_env(worker)
    with pytest.raises(auth.GlassHiveAuthError, match="authenticated installed owner"):
        runtime._write_conversation_runtime_files(worker, {})


def test_native_claude_uses_cli_auth_before_any_raw_token_projection(installed, tmp_path, monkeypatch):
    _, home, _ = installed
    runtime = profile_runtime.HostClaudeCodeRuntime(base_dir=str(tmp_path / "workers"))
    monkeypatch.setattr(profile_runtime, "_read_claude_keychain_oauth", lambda **_: pytest.fail("must not read Keychain tokens"))
    monkeypatch.setattr(profile_runtime, "_usable_explicit_claude_oauth_token", lambda: pytest.fail("must not project tokens"))
    seen = []
    def status(binary, *, child_env, **kwargs):
        seen.append(dict(child_env))
        assert child_env["HOME"] == str(home)
        assert not any(name in child_env for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "ANTHROPIC_API_KEY"))
        return True
    monkeypatch.setattr(profile_runtime, "_claude_cli_managed_auth_available", status)
    child = {"HOME": str(tmp_path / "worker-home"), "CLAUDE_CONFIG_DIR": "unselected", "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-unselected", "ANTHROPIC_API_KEY": "synthetic-unselected", "PATH": "/usr/bin:/bin"}
    assert runtime._inject_private_subscription_auth(child) == "owner_managed"
    assert profile_runtime._claude_host_auth_available(runtime.binary, child_env=child)
    assert len(seen) == 2
    assert child["PATH"] == "/usr/bin:/bin"


@pytest.mark.parametrize("owner", [OWNER, "b" * 24])
def test_provider_native_owner_gate_preserves_cortex_scope(installed, owner):
    payload = conversation_provider.ChatCompletionRequest(model="codex-cli:gpt-5.6-sol", messages=[{"role": "user", "content": "Synthetic request"}], metadata={"owner_id": owner, "actor_kind": "system", "origin": "system"})
    request = Request({"type": "http", "headers": []})
    context = conversation_provider.ProviderAuthContext("local", "courier", True, True, "full")
    if owner != OWNER:
        with pytest.raises(HTTPException) as error:
            conversation_provider._hydrate_metadata(payload, request, context)
        assert error.value.status_code == 403
    else:
        result = conversation_provider._hydrate_metadata(payload, request, context)
        assert result.metadata.owner_id == OWNER
        assert result.metadata.actor_kind == "system"
        assert result.metadata.origin == "system"


def test_native_provider_status_does_not_accept_or_expose_projected_tokens(installed, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-wrong-authority")
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-service-authority")
    monkeypatch.setattr(conversation_provider, "_configured_binary", lambda _: "/owned/codex")
    seen = []
    def run(command, **kwargs):
        seen.append((command, kwargs))
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "WPR_API_TOKEN" not in kwargs["env"]
        return type("Result", (), {"returncode": 1})()
    monkeypatch.setattr(conversation_provider.subprocess, "run", run)
    assert not conversation_provider._harness_auth_configured("codex-cli")
    assert seen[0][0][-2:] == ["-c", 'cli_auth_credentials_store="file"']


def test_native_codex_command_forces_file_backend_after_worker_config(installed, tmp_path, monkeypatch):
    runtime = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path / "workers"))
    worker = {"worker_id": "wrk_owner", "owner_id": OWNER, "profile": "codex-cli", "execution_mode": "host", "workspace_root": str(tmp_path / "workspace"), "trusted_run_lane": "conversation", "bootstrap_bundle_json": json.dumps({"run_mode": "conversation", "developer_instructions": "Synthetic authority.", "codex_config_append": 'cli_auth_credentials_store="keyring"'})}
    runtime._write_conversation_runtime_files(worker, json.loads(worker["bootstrap_bundle_json"]))
    command, env = runtime._build_command(worker, "Synthetic request.", runtime._host_runtime_info(worker))
    assert command[-3:] == ["-c", 'cli_auth_credentials_store="file"', "-"]
    assert env["CODEX_HOME"] == str(runtime._host_codex_home(worker))


def test_native_unavailable_authority_is_not_owner_permission_denial(installed):
    state, _, _ = installed
    state.unlink()
    payload = conversation_provider.ChatCompletionRequest(model="codex-cli:gpt-5.6-sol", messages=[{"role": "user", "content": "Synthetic request"}], metadata={"owner_id": OWNER})
    context = conversation_provider.ProviderAuthContext("local", "courier", True, True, "full")
    with pytest.raises(HTTPException) as error:
        conversation_provider._hydrate_metadata(payload, Request({"type": "http", "headers": []}), context)
    assert error.value.status_code == 503
