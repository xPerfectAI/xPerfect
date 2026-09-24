import json
from datetime import datetime
from pathlib import Path

import pytest
import workers_projects_runtime.profile_runtime as module
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime, RuntimeErrorBase


def setup_runtime(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / 'runtime'))
    worker = {'worker_id': 'wrk_midnight', 'name': 'Guide', 'profile': 'codex-cli',
              'execution_mode': 'host', 'workspace_root': str(tmp_path / 'work')}
    return runtime, worker


def admit(runtime, worker, workspace, *, legacy=False):
    run_id = 'run_midnight'
    runtime._write_active_session(worker['worker_id'], {
        'session_name': 'host-midnight', 'run_id': run_id,
        'workspace_dir': '' if legacy else str(workspace),
        'stdout_path': str(runtime._run_root(worker['worker_id'], run_id) / 'stdout.log'),
        'heartbeat_path': str(module._active_run_status_path(workspace, run_id)),
    })
    runtime._write_action_audit(worker, {'kind': 'run.started', 'run_id': run_id, 'cwd': str(workspace)})
    return run_id


@pytest.mark.parametrize('legacy', [False, True])
def test_exact_admitted_workspace_wins_after_midnight(tmp_path, legacy):
    runtime, worker = setup_runtime(tmp_path)
    original = tmp_path / 'work' / '2026-08-01-guide'
    run_id = admit(runtime, worker, original, legacy=legacy)
    stale = {**worker, 'last_run_id': run_id, 'workspace_dir': str(tmp_path / 'work' / '2026-08-02-guide')}
    assert runtime._host_workspace_dir(stale) == original
    assert runtime._host_runtime_info(stale).workspace_dir == str(original)


@pytest.mark.parametrize('mismatch', ['worker', 'run', 'runtime', 'heartbeat', 'stdout'])
def test_legacy_recovery_requires_exact_admission(tmp_path, mismatch):
    runtime, worker = setup_runtime(tmp_path)
    original = tmp_path / 'admitted'
    run_id = admit(runtime, worker, original, legacy=True)
    if mismatch in ('worker', 'run', 'runtime'):
        path = runtime._action_audit_path(worker['worker_id'])
        payload = json.loads(path.read_text())
        payload[{'worker': 'worker_id', 'run': 'run_id', 'runtime': 'runtime'}[mismatch]] = 'other'
        path.write_text(json.dumps(payload)+'\n')
    else:
        session = runtime._read_active_session(worker['worker_id'])
        session[mismatch+'_path'] = str(tmp_path / 'other')
        runtime._write_active_session(worker['worker_id'], session)
    assert runtime._admitted_host_workspace({**worker, 'last_run_id': run_id}) is None
    with pytest.raises(RuntimeErrorBase, match='admitted workspace is unavailable'):
        runtime._host_workspace_dir({**worker, 'last_run_id': run_id})


def test_new_run_does_not_take_an_old_active_generation(tmp_path):
    runtime, worker = setup_runtime(tmp_path)
    admit(runtime, worker, tmp_path/'old')
    selected = tmp_path/'selected'
    assert runtime._host_workspace_dir({**worker, '_active_run_id': 'run_new', 'workspace_dir': str(selected)}) == selected


def test_ready_publishes_exact_workspace_before_clock_changes(tmp_path, monkeypatch):
    runtime, worker = setup_runtime(tmp_path)
    class Clock(datetime):
        day = 1
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, cls.day, 23, 59, 59)
    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(runtime, 'preflight_worker_profile', lambda *args: None)
    monkeypatch.setattr(runtime, '_active_pid', lambda *args: None)
    def materialize(_worker, workspace):
        workspace.mkdir(parents=True)
        Clock.day = 2
    monkeypatch.setattr(runtime, '_materialize_workspace', materialize)
    published = []
    worker['_runtime_info_callback'] = published.append
    info = runtime.ensure_worker_ready(worker)
    assert published == [info]
    assert Path(info.workspace_dir).name.startswith('2026-08-01-')
    assert runtime._host_workspace_dir({**worker, 'last_run_id': 'run_finished', 'workspace_dir': info.workspace_dir}) == Path(info.workspace_dir)


def test_runtime_identity_rejection_stops_admission(tmp_path, monkeypatch):
    runtime, worker = setup_runtime(tmp_path)
    monkeypatch.setattr(runtime, 'preflight_worker_profile', lambda *args: None)
    monkeypatch.setattr(runtime, '_active_pid', lambda *args: None)
    monkeypatch.setattr(runtime, '_materialize_workspace', lambda *args: None)
    def rejected(info):
        raise RuntimeErrorBase('Exact run generation changed')
    worker['_runtime_info_callback'] = rejected
    with pytest.raises(RuntimeErrorBase, match='Exact run generation changed'):
        runtime.ensure_worker_ready(worker)


@pytest.mark.parametrize("mismatch", [None, "worker", "run", "stdout"])
def test_post_idle_recovery_uses_durable_admission(tmp_path, mismatch):
    runtime, worker = setup_runtime(tmp_path)
    original = tmp_path / "admitted"
    run_id = admit(runtime, worker, original, legacy=True)
    heartbeat = module._active_run_status_path(original, run_id)
    heartbeat.parent.mkdir(parents=True)
    payload = {"schema": "glasshive.active_run.v1", "run_id": run_id,
               "runtime": runtime.runtime_name, "worker": {"worker_id": worker["worker_id"]},
               "transcript_paths": {"stdout": str(runtime._run_root(worker["worker_id"], run_id) / "stdout.log")}}
    if mismatch == "worker": payload["worker"]["worker_id"] = "foreign"
    if mismatch == "run": payload["run_id"] = "foreign"
    if mismatch == "stdout": payload["transcript_paths"]["stdout"] = str(tmp_path / "foreign")
    heartbeat.write_text(json.dumps(payload))
    runtime._clear_active_session(worker["worker_id"])
    result = runtime._admitted_host_workspace({**worker, "last_run_id": run_id})
    assert result == (None if mismatch else original)


def test_paused_reconciliation_refreshes_identity_without_resuming():
    from types import SimpleNamespace
    from workers_projects_runtime.service import WorkersProjectsService
    service = WorkersProjectsService.__new__(WorkersProjectsService)
    service.store = SimpleNamespace(
        get_active_run=lambda _: None,
        has_queued_runs=lambda _: False,
        has_queued_capacity_retry=lambda _: False,
        has_queued_running_invariant_retry=lambda _: False,
    )
    calls = []
    service._refresh_runtime_info = lambda worker_id, **kwargs: calls.append((worker_id, kwargs))
    service._reconcile_worker_row({"worker_id": "wrk_paused", "state": "paused", "last_error": "", "execution_mode": "host", "last_run_id": "run_finished"})
    assert calls == [("wrk_paused", {"state": "paused", "last_error": ""})]


@pytest.mark.parametrize("profile", ["codex-cli", "claude-code"])
def test_accepted_first_mission_can_initialize_workspace(tmp_path, monkeypatch, profile):
    from workers_projects_runtime.profile_runtime import HostClaudeCodeRuntime
    runtime_type = HostCodexCliRuntime if profile == "codex-cli" else HostClaudeCodeRuntime
    runtime = runtime_type(base_dir=str(tmp_path / "runtime"))
    worker = {"worker_id": "wrk_fresh", "profile": profile, "execution_mode": "host",
              "last_run_id": "run_accepted", "workspace_root": str(tmp_path / "work")}
    monkeypatch.setattr(runtime, "preflight_worker_profile", lambda *args: None)
    monkeypatch.setattr(runtime, "_active_pid", lambda *args: None)
    published = []
    worker["_runtime_info_callback"] = published.append
    def materialize(current, workspace):
        # Persist the selected identity before filesystem setup can fail or restart.
        assert len(published) == 1
        assert published[0].workspace_dir == str(workspace)
        workspace.mkdir(parents=True)
    monkeypatch.setattr(runtime, "_materialize_workspace", materialize)
    info = runtime.ensure_worker_ready(worker)
    assert Path(info.workspace_dir).is_dir()


@pytest.mark.parametrize("identity", ["state_dir", "session_key", "active_session", "audit"])
def test_missing_initialized_workspace_cannot_be_reallocated(tmp_path, identity):
    runtime, worker = setup_runtime(tmp_path)
    worker["last_run_id"] = "run_existing"
    if identity in {"state_dir", "session_key"}:
        worker[identity] = "previous-runtime-identity"
    else:
        path = (runtime._active_session_meta_path(worker["worker_id"])
                if identity == "active_session" else runtime._action_audit_path(worker["worker_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("corrupt evidence")
    with pytest.raises(RuntimeErrorBase, match="admitted workspace is unavailable"):
        runtime._host_workspace_dir(worker)


def test_interrupted_materialization_reuses_persisted_workspace(tmp_path, monkeypatch):
    runtime, worker = setup_runtime(tmp_path)
    worker.update(last_run_id="run_first", created_at="2026-08-01T23:59:59+00:00")
    monkeypatch.setattr(runtime, "preflight_worker_profile", lambda *args: None)
    monkeypatch.setattr(runtime, "_active_pid", lambda *args: None)
    persisted = {}
    def publish(info):
        persisted.update(workspace_dir=info.workspace_dir, state_dir=info.state_dir)
    worker["_runtime_info_callback"] = publish
    def interrupted(*args):
        raise OSError("setup interrupted")
    monkeypatch.setattr(runtime, "_materialize_workspace", interrupted)
    with pytest.raises(OSError, match="setup interrupted"):
        runtime.ensure_worker_ready(worker)
    selected = Path(persisted["workspace_dir"])
    assert selected.name.startswith("2026-08-01-")
    # A retry before publication also derives the path from durable creation time.
    assert runtime._host_workspace_dir(worker) == selected
    monkeypatch.setattr(runtime, "_materialize_workspace", lambda current, path: path.mkdir(parents=True))
    assert runtime.ensure_worker_ready({**worker, **persisted}).workspace_dir == str(selected)
