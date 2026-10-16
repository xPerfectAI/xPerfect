"""The native worker lane owns its skill policy without changing other config."""
from __future__ import annotations

import difflib
import fcntl
import json
import os
from pathlib import Path
import sys

import pytest

from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
from workers_projects_runtime.profile_runtime import tomllib


SKILL = 'long-running-background-tasks'


def record(tmp_path, lane='conversation'):
    return {'worker_id': 'synthetic-worker', 'owner_id': 'synthetic-owner',
            'tenant_id': 'synthetic-tenant', 'profile': 'grok-build',
            'model': 'grok-selected', 'execution_mode': 'host',
            'trusted_run_lane': lane, 'workspace_dir': str(tmp_path / 'workspace'),
            'bootstrap_bundle_json': json.dumps({'run_mode': 'conversation'})}


@pytest.mark.parametrize('source', [
    '', '# retained comment\n[plugins]\nauto_update = false\n',
    '[skills]\npaths = ["./team"]\n', '[skills]',
    'skills.paths = ["./team"]\n', 'skills = {}\n',
    'skills = { paths = ["./team"] }\n',
    '["skills"]\n"disabled" = [] # retained\n',
    '[skills]\ndisabled = ["other-skill"]\n',
    '[skills]\ndisabled = [\n  "other-skill", # retain this\n]\n',
    'skills.disabled = ["other-skill",]\n',
    'skills = { disabled = ["other-skill"], paths = ["./team"] }\n',
    '# retain CRLF\r\n[skills]\r\ndisabled = []\r\n',
    '[other]\ntext = """[skills]\ndisabled = [\"not-real\"]\n"""\n'
    '[skills]\ndisabled = ["other-skill"]\n',
])
def test_named_policy_changes_only_one_list_and_preserves_every_existing_byte(source):
    from workers_projects_runtime.grok_runtime import _grok_conversation_skill_config
    before = tomllib.loads(source)
    actual = _grok_conversation_skill_config(source)
    expected_skills = {**before.get('skills', {})}
    expected_skills['disabled'] = [*expected_skills.get('disabled', []), SKILL]
    assert tomllib.loads(actual) == {**before, 'skills': expected_skills}
    changes = difflib.SequenceMatcher(a=source, b=actual, autojunk=False).get_opcodes()
    assert all(op in ('equal', 'insert') for op, *_ in changes)
    assert _grok_conversation_skill_config(actual) == actual


@pytest.mark.parametrize('source', [
    '[skills\n', 'skills = "invalid"\n', '[skills]\ndisabled = "invalid"\n',
    '[skills]\ndisabled = [42]\n',
])
def test_invalid_native_config_is_not_replaced(tmp_path, source):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    worker = record(tmp_path)
    home = runtime._grok_home(worker)
    home.mkdir(parents=True)
    path = home / 'config.toml'
    path.write_text(source)
    with pytest.raises(RuntimeErrorBase):
        runtime._prepare_conversation_skills(worker, home)
    assert path.read_text() == source


@pytest.mark.parametrize('lane', ['conversation', 'mission', None])
@pytest.mark.parametrize('host', [True, False])
def test_persisted_lane_controls_skill_policy_before_any_run(tmp_path, monkeypatch, lane, host):
    import workers_projects_runtime.grok_runtime as owner
    from types import SimpleNamespace
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    runtime.sandbox = SimpleNamespace(home_mount='/workspace/home', box=None,
                                     paths=runtime.sandbox.paths)
    worker = record(tmp_path, lane)
    monkeypatch.setattr(runtime, '_host_env', lambda _: {'XAI_API_KEY': 'synthetic'})
    monkeypatch.setattr(runtime, '_container_env_for_worker', lambda _: {'XAI_API_KEY': 'synthetic'})
    monkeypatch.setattr(owner, 'apply_bound_provider_account_environment', lambda *a, **k: None)
    home = runtime._grok_home(worker)
    home.mkdir(parents=True)
    config = home / 'config.toml'
    before = '# Keep this exactly\n[skills]\ndisabled = ["another-choice"]\n'
    config.write_text(before)
    history = home / 'retained-history.jsonl'
    history.write_text('{"synthetic":"same native history"}\n')
    environment = runtime._native_environment(worker, host=host)
    assert environment['XAI_API_KEY'] == 'synthetic'
    disabled = tomllib.loads(config.read_text())['skills']['disabled']
    expected = ['another-choice', SKILL] if lane == 'conversation' else ['another-choice']
    assert disabled == expected
    if lane != 'conversation':
        assert config.read_text() == before
    first_stat = config.stat()
    runtime._native_environment({**worker, '_active_run_id': 'next-run', '_run_attempt_id': 'next-attempt'}, host=host)
    assert config.stat().st_mtime_ns == first_stat.st_mtime_ns
    assert history.read_text() == '{"synthetic":"same native history"}\n'


def test_unsafe_config_and_lock_links_do_not_change_the_target(tmp_path):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    worker = record(tmp_path)
    home = runtime._grok_home(worker)
    home.mkdir(parents=True)
    target = tmp_path / 'unrelated.toml'
    target.write_text('unchanged = true\n')
    config = home / 'config.toml'
    config.symlink_to(target)
    with pytest.raises(RuntimeErrorBase):
        runtime._prepare_conversation_skills(worker, home)
    config.unlink()
    (home / '.config-init.lock').symlink_to(target)
    with pytest.raises(RuntimeErrorBase):
        runtime._prepare_conversation_skills(worker, home)
    assert target.read_text() == 'unchanged = true\n'


def test_native_writer_lock_is_respected_without_overwriting(tmp_path, monkeypatch):
    import workers_projects_runtime.grok_runtime as owner
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    worker = record(tmp_path)
    home = runtime._grok_home(worker)
    home.mkdir(parents=True)
    config = home / 'config.toml'
    config.write_text('# existing\n')
    with (home / '.config-init.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        clock = iter([0, 2])
        monkeypatch.setattr(owner.time, 'monotonic', lambda: next(clock))
        with pytest.raises(RuntimeErrorBase, match='configuration is busy'):
            runtime._prepare_conversation_skills(worker, home)
    assert config.read_text() == '# existing\n'


def test_warm_revision_tracks_worker_config_but_unchanged_reuse_is_stable(tmp_path, monkeypatch):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    runtime.binary = sys.executable
    worker = record(tmp_path)
    workspace = runtime._host_workspace_dir(worker)
    workspace.mkdir(parents=True)
    home = runtime._grok_home(worker)
    home.mkdir(parents=True)
    (home / 'config.toml').write_text('[skills]\ndisabled = ["' + SKILL + '"]\n')
    monkeypatch.setattr(runtime, '_native_environment', lambda *a, **k: {
        'GROK_HOME': str(home), 'PATH': os.environ.get('PATH', '')})
    first, _, _ = runtime._warm_context(worker)
    same, _, _ = runtime._warm_context(worker)
    assert first == same
    config = home / 'config.toml'
    config.write_text(config.read_text() + '\n# Native config changed\n')
    changed, _, _ = runtime._warm_context(worker)
    assert changed.configuration_revision != first.configuration_revision
    assert changed.lane == first.lane == 'conversation'
