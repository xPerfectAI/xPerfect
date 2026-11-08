"""Selection-only Native overlays retain valid transport without granting new servers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime


@pytest.fixture
def selection(tmp_path, monkeypatch):
    owner = tmp_path / "owner" / ".codex"
    owner.mkdir(parents=True)
    original = ('model_reasoning_effort="high"\n'
                '[mcp_servers.browser]\ncommand="/synthetic/browser"\nargs=["serve"]\n'
                '[mcp_servers.browser.tools.inspect]\nenabled=false\n'
                '[mcp_servers.other]\ncommand="/synthetic/other"\n')
    (owner / "config.toml").write_text(original)
    monkeypatch.setenv("CODEX_HOME", str(owner))
    monkeypatch.setenv("GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST", "browser,other")
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", "")
    monkeypatch.setenv("WPR_HOST_PLUGIN_DENYLIST", "")
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "inherit")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    return runtime, owner, original


def test_disabled_selection_preserves_existing_transport_and_all_server_policy(selection):
    runtime, owner, original = selection
    output = tomllib.loads(runtime._host_codex_worker_config(
        '[mcp_servers.browser]\nenabled=false\n'))
    server = output["mcp_servers"]["browser"]
    assert server["command"] == "/synthetic/browser" and server["args"] == ["serve"]
    assert server["enabled"] is False and server["tools"]["inspect"]["enabled"] is False
    assert output["mcp_servers"]["other"]["command"] == "/synthetic/other"
    assert output["model_reasoning_effort"] == "high"
    assert (owner / "config.toml").read_text() == original


def test_absent_disabled_selection_does_not_invent_transport_or_inherit_unselected_server(selection):
    runtime, owner, original = selection
    output = tomllib.loads(runtime._host_codex_worker_config(
        '[mcp_servers.absent]\nenabled=false\n'))
    assert "absent" not in output["mcp_servers"]
    assert set(output["mcp_servers"]) == {"browser", "other"}
    assert (owner / "config.toml").read_text() == original


def test_explicit_new_transport_and_unrelated_append_policy_remain_unchanged(selection):
    runtime, _, _ = selection
    output = tomllib.loads(runtime._host_codex_worker_config(
        'approval_policy="on-request"\n[mcp_servers.new-server]\nurl="http://localhost:3180/mcp"\n'
        '[mcp_servers.browser]\nenabled=false\n'))
    assert output["approval_policy"] == "on-request"
    assert output["mcp_servers"]["new-server"] == {"url": "http://localhost:3180/mcp"}
    assert output["mcp_servers"]["browser"]["enabled"] is False


@pytest.mark.parametrize("mode", ["known", "unknown", "restricted"])
def test_installed_codex_accepts_composed_selection_without_starting_tools(selection, tmp_path, mode):
    cli = shutil.which("codex")
    if cli is None:
        pytest.skip("Installed Codex CLI config reader is required")
    runtime, owner, original = selection
    selected = "browser" if mode != "unknown" else "absent"
    append = f'[mcp_servers.{selected}]\nenabled=false\n'
    bundle = {"provider_capabilities": {"native_tools": False},
              "glasshive_capability_broker": {"version": 1,"name": "scoped-broker",
                                             "url": "http://localhost:3180/mcp"}} if mode == "restricted" else {}
    text = runtime._host_codex_worker_config(append, capability_bundle=bundle)
    parser = tmp_path / "parser"
    parser.mkdir()
    (parser / "config.toml").write_text(text)
    env = {**os.environ, "CODEX_HOME": str(parser)}
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run([cli, "mcp", "list", "--json"], env=env, cwd=parser,
                            check=False, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    servers = {item["name"]: item for item in json.loads(result.stdout)}
    if mode == "known":
        assert servers["browser"]["enabled"] is False
    elif mode == "unknown":
        assert "absent" not in servers
    else:
        assert set(servers) == {"scoped-broker"}
    assert (owner / "config.toml").read_text() == original
