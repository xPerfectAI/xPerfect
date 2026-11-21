import hashlib
import json

from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.worker_context_mcp import context_tool_manifest


def test_host_mission_progress_matches_only_exact_producer_and_attempt_scope(tmp_path, monkeypatch):
    runtime = object.__new__(HostCodexCliRuntime)
    worker = {"worker_id": "worker-a", "owner_id": "owner-a", "profile": "codex-cli",
              "_provider_activity_attempt_id": "attempt-a", "bootstrap_bundle_json": "{}"}
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    monkeypatch.setattr(runtime, "_run_root", lambda *args: tmp_path)
    monkeypatch.setattr(runtime, "_home_dir", lambda *args: home)
    monkeypatch.setattr(runtime, "_host_workspace_dir", lambda *args: workspace)
    instruction = "Keep both original files unchanged."
    contextual = json.dumps({"instruction": instruction, "context": [], "context_manifest": {},
        "context_contract": context_tool_manifest()["inherited_context_contract"]})
    scope = {"run_id": "run-a", "worker_id": "worker-a", "owner_id": "owner-a",
        "attempt_id": "attempt-a", "workspace_dir": str(workspace),
        "file_output_transport": "artifact_sha256"}
    scope_path = tmp_path / "native-image-scope.json"
    scope_path.write_text(json.dumps(scope))
    stdin = runtime._command_stdin_text(worker, runtime._host_mission_instruction(worker, contextual, scope), None)
    digest = hashlib.sha256(instruction.encode()).hexdigest()
    assert runtime._provider_rollout_input_matches(worker, "run-a", stdin, digest)
    assert not runtime._provider_rollout_input_matches(worker, "run-a", stdin + "Unexpected extra layer.", digest)
    scope_path.write_text(json.dumps({**scope, "attempt_id": "foreign-attempt"}))
    assert not runtime._provider_rollout_input_matches(worker, "run-a", stdin, digest)
