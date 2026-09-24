"""O05: current Codex CLIs removed --full-auto; keep the same sandbox without it."""
import json

from workers_projects_runtime.profile_runtime import CodexCliRuntime, HostCodexCliRuntime


def _sandboxed_without_approvals(command):
    assert "--full-auto" not in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    pairs = [command[i:i + 2] for i in range(len(command) - 1)]
    assert ["-c", 'sandbox_mode="workspace-write"'] in pairs
    assert ["-c", 'approval_policy="never"'] in pairs


def test_host_codex_first_turn_uses_version_stable_sandbox_overrides(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    life = tmp_path / "life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_flags", "profile": "codex-cli", "execution_mode": "host",
        "trusted_run_lane": "conversation", "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation", "provider_model": "gpt-5.6-sol",
                                             "access_mode": "workspace"}),
    }
    command, _ = runtime._build_command(worker, "Talk naturally.", runtime._host_runtime_info(worker))
    _sandboxed_without_approvals(command)


def test_workspace_codex_without_bypass_uses_the_same_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CODEX_DANGEROUS", "0")
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_workspace_flags", "name": "Workspace", "profile": "codex-cli",
              "execution_mode": "docker"}
    command, _ = runtime._build_command(worker, "do the work", runtime._runtime_info(worker))
    _sandboxed_without_approvals(command)
