"""Enterprise authentication never borrows an unselected operating-system login."""
from pathlib import Path
import os
import shutil
from types import SimpleNamespace

import pytest

from workers_projects_runtime.auth import (
    EnterpriseAuthSettings, enterprise_authentication_enabled, multi_user_security_enabled,
)
from workers_projects_runtime.bootstrap import apply_bootstrap
from workers_projects_runtime.codex_native_warm import CodexNativeWarmMixin
from workers_projects_runtime.current_native_account import enabled as current_native_enabled
from workers_projects_runtime.grok_native_warm import GrokNativeWarmMixin
from workers_projects_runtime.mission_provider_accounts import (
    deployment_provider_readiness, native_current_account_allowed,
)
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime, HostNativeCliMixin


@pytest.fixture(params=[
    (mode, flag, value)
    for mode in ("", "local", "legacy_compatibility", "multi_user")
    for flag, value in (
        (None, ""), ("GLASSHIVE_ENTERPRISE_MODE", "1"),
        ("WPR_ENTERPRISE_MODE", "1"), ("GLASSHIVE_ENTERPRISE_MODE", "enabled"),
        ("WPR_ENTERPRISE_MODE", "enabled"),
        (("GLASSHIVE_ENTERPRISE_MODE", "WPR_ENTERPRISE_MODE"), "1"),
    )
])
def security(request, monkeypatch, tmp_path):
    mode, flag, value = request.param
    # All credentials and homes in these checks are synthetic and local to pytest.
    for key in tuple(os.environ):
        if key.startswith(("OPENAI_", "ANTHROPIC_", "CLAUDE_", "PORTKEY_", "XAI_", "WPR_CODEX_")):
            monkeypatch.delenv(key, raising=False)
    for key in ("GLASSHIVE_SECURITY_MODE", "GLASSHIVE_ENTERPRISE_MODE", "WPR_ENTERPRISE_MODE",
                "XPERFECT_STORAGE_ROOT", "VIVENTIUM_NATIVE_FIRST_ADMIN_STATE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "owner/.codex"))
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    if mode:
        monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", mode)
    if flag:
        for key in ((flag,) if isinstance(flag, str) else flag):
            monkeypatch.setenv(key, value)
    return mode, bool(mode == "multi_user" or flag)


def test_authentication_boundary_controls_ambient_login_not_isolation_mode(security):
    mode, enterprise = security
    assert EnterpriseAuthSettings().enterprise is enterprise
    assert enterprise_authentication_enabled() is enterprise
    if mode:
        assert multi_user_security_enabled() is (mode == "multi_user")
    assert current_native_enabled() is not enterprise
    for profile in ("codex-cli", "claude-code", "grok-build"):
        assert native_current_account_allowed({"profile": profile}) is not enterprise
    assert deployment_provider_readiness("codex-cli")[0] == (
        "action_required" if enterprise else "deployment_managed"
    )


def test_bootstrap_and_host_writers_never_copy_enterprise_os_credentials(security, tmp_path, monkeypatch):
    _mode, enterprise = security
    source_codex = Path.home() / ".codex/auth.json"
    source_claude = Path.home() / ".claude.json"
    for source in (source_codex, source_claude):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"synthetic_credential":"must-stay-with-owner"}')
    attempted = []

    def copy_file(source, target):
        attempted.append(source)
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    for profile in ("codex-cli", "claude-code"):
        apply_bootstrap(
            home_dir=tmp_path / profile / "home", workspace_dir=tmp_path / profile / "workspace",
            runtime_name=profile, worker={}, copy_file=copy_file, copy_tree=lambda *_: None,
        )
    assert (source_codex in attempted) is not enterprise
    assert (source_claude in attempted) is not enterprise

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime, "_host_codex_worker_config", lambda *_a, **_kw: "")
    monkeypatch.setattr(runtime, "_project_host_codex_capability_roots", lambda *_: None)
    monkeypatch.setattr(runtime, "_host_plugin_denylist", lambda: ())
    monkeypatch.setattr(HostNativeCliMixin, "ensure_worker_ready", lambda *_: SimpleNamespace(workspace_dir=""))
    for lane in ("project", "conversation", "copy", "ready"):
        worker = {"worker_id": "wrk_" + lane, "profile": "codex-cli", "execution_mode": "host"}
        workspace = tmp_path / lane
        workspace.mkdir()
        if lane == "project":
            runtime._write_host_project_mcp_files(worker, workspace, {})
        elif lane == "conversation":
            runtime._write_conversation_runtime_files(worker, {})
        elif lane == "ready":
            monkeypatch.setattr(runtime, "_conversation_mode_from_worker", lambda _: True)
            runtime.ensure_worker_ready(worker)
        else:
            runtime._copy_host_codex_auth(runtime._host_codex_home(worker))
        target = runtime._host_codex_home(worker) / "auth.json"
        assert target.exists() is not enterprise
        if target.exists():
            assert target.read_bytes() == source_codex.read_bytes()


def test_enterprise_warm_preparation_stops_before_account_access(security):
    _mode, enterprise = security
    if not enterprise:
        pytest.skip("Ordinary local warm behavior is covered by its native warm tests")
    for mixin, profile in ((CodexNativeWarmMixin, "codex-cli"), (GrokNativeWarmMixin, "grok-build")):
        runtime = mixin.__new__(mixin)
        with pytest.raises(ValueError, match="preparation"):
            runtime._warm_context({"profile": profile, "execution_mode": "host"})


@pytest.mark.parametrize("binding", ["_glasshive_provider_account_bound", "_glasshive_inference_broker_bound"])
def test_local_bound_authority_does_not_copy_ambient_bootstrap_login(tmp_path, monkeypatch, binding):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    for key in ("GLASSHIVE_ENTERPRISE_MODE", "WPR_ENTERPRISE_MODE"):
        monkeypatch.delenv(key, raising=False)
    copied = []
    apply_bootstrap(
        home_dir=tmp_path / "home", workspace_dir=tmp_path / "workspace", runtime_name="codex-cli",
        worker={binding: True}, copy_file=lambda *paths: copied.append(paths), copy_tree=lambda *_: None,
    )
    assert copied == []
