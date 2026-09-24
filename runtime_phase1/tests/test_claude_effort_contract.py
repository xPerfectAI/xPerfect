import json
import pytest

from workers_projects_runtime.mcp_server import _apply_effort_to_bundle
from workers_projects_runtime.profile_runtime import ClaudeCodeRuntime, HostClaudeCodeRuntime
from workers_projects_runtime.runtime_requirements import CLAUDE_CODE_EFFORT_LEVELS
from workers_projects_runtime.service import ParallelExecutionIsolationError, _validate_parallel_clean_room_environment
from workers_projects_runtime.store import canonical_parallel_clean_room_bootstrap


@pytest.mark.parametrize('effort', ['low', 'medium', 'high', 'xhigh', 'max'])
@pytest.mark.parametrize('runtime_class', [ClaudeCodeRuntime, HostClaudeCodeRuntime])
def test_native_effort_survives_mcp_validation_storage_and_cli(tmp_path, monkeypatch, effort, runtime_class):
    bundle = _apply_effort_to_bundle({}, profile='claude-code', effort=effort)
    _validate_parallel_clean_room_environment(bundle)
    stored = canonical_parallel_clean_room_bootstrap(bundle)
    assert stored['env']['WPR_CLAUDE_CODE_EFFORT'] == effort
    runtime = runtime_class(base_dir=str(tmp_path / 'runtime'))
    if runtime_class is HostClaudeCodeRuntime:
        monkeypatch.setattr(runtime, '_inject_private_subscription_auth', lambda env: None)
        monkeypatch.setattr(runtime, '_effort_supported', lambda value: value in CLAUDE_CODE_EFFORT_LEVELS)
    monkeypatch.setenv('WPR_CLAUDE_CODE_ENABLE_CHROME', '0')
    worker = {'worker_id': 'wrk_effort', 'name': 'Effort contract', 'profile': 'claude-code',
              'execution_mode': 'host' if runtime_class is HostClaudeCodeRuntime else 'docker',
              'model': 'opus', 'bootstrap_bundle_json': json.dumps(stored)}
    command, _ = runtime._build_command(worker, 'Write the requested note.', runtime._runtime_info(worker))
    assert command[command.index('--model') + 1] == 'opus'
    assert command[command.index('--effort') + 1] == effort


def test_default_still_omits_override_and_unknown_effort_is_rejected():
    assert _apply_effort_to_bundle({}, profile='claude-code', effort='default') == {}
    with pytest.raises(ValueError, match='Claude effort'):
        _apply_effort_to_bundle({}, profile='claude-code', effort='ultra')
    with pytest.raises(ParallelExecutionIsolationError):
        _validate_parallel_clean_room_environment({'env': {'WPR_CLAUDE_CODE_EFFORT': 'ultra'}})
    assert canonical_parallel_clean_room_bootstrap({'env': {'WPR_CLAUDE_CODE_EFFORT': 'ultra'}})['env'] == {}
