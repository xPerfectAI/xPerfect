"""Idle capacity never grants turn authority or crosses a selected worker scope."""
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from workers_projects_runtime.native_warm_pool import (
    NativeWarmConfig, NativeWarmPool, WarmScope,
)
import workers_projects_runtime.native_warm_pool as warm_owner


def scope(**changes):
    return replace(WarmScope("tenant", "owner", "worker", "grok-build",
                             "grok-4.7", "account", "revision"), **changes)


class Server:
    def __init__(self):
        self.stopped = False
        self.stops = 0
        self.stop_error = False
        self.secret = "synthetic-private-secret"
        self.url = "ws://127.0.0.1:12345/ws"
        self.record_path = None

    def alive(self):
        return not self.stopped

    def stop(self):
        self.stops += 1
        if self.stop_error:
            raise RuntimeError("Cannot prove native stop")
        self.stopped = True

    def identity(self):
        return {"pid": 12345, "process_group": 12345,
                "process_start_identity": "synthetic-generation"}


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


@pytest.mark.parametrize("count", [True, -1, 9, 1.5, "2"])
def test_warm_count_is_explicit_bounded_configuration(count):
    with pytest.raises(ValueError, match="native_warm_servers"):
        NativeWarmConfig.from_performance({"native_warm_servers": count})


@pytest.mark.parametrize("expiry", [True, -1, 3601, float("inf"), float("nan"), "30"])
def test_expiry_is_finite_and_bounded(expiry):
    with pytest.raises(ValueError, match="native_warm_idle_seconds"):
        NativeWarmConfig.from_performance({"native_warm_idle_seconds": expiry})


def test_default_zero_never_starts_native_process():
    pool = NativeWarmPool()
    try:
        pool.prepare(scope(), NativeWarmConfig.from_performance({}),
                     lambda: pytest.fail("Zero must use the ordinary cold path"))
        assert pool.claim(scope(), run_id="run", attempt_id="attempt") is None
        assert pool.status(scope()) == {"configured": 0, "idle": 0, "starting": 0,
                                        "leased": 0, "provider_context_warmed": False}
    finally:
        pool.close()


def test_n_real_idle_objects_claim_once_and_refill_with_global_bound(tmp_path):
    made = []
    def factory():
        server = Server()
        made.append(server)
        return server
    pool = NativeWarmPool(global_limit=3, owner_limit=3)
    try:
        pool.prepare(scope(), NativeWarmConfig(2), factory)
        wait_for(lambda: pool.status(scope())["idle"] == 2)
        first = pool.claim(scope(), run_id="run-a", attempt_id="attempt-a")
        second = pool.claim(scope(), run_id="run-b", attempt_id="attempt-b")
        assert first.server is not second.server
        assert first.lease_id != second.lease_id
        wait_for(lambda: pool.status(scope())["idle"] == 1)
        assert len(made) == 3
        assert pool.status(scope())["leased"] == 2
        first.write_descriptor(tmp_path / "private" / "lease.json")
        descriptor = json.loads((tmp_path / "private" / "lease.json").read_text())
        assert descriptor["run_id"] == "run-a" and descriptor["attempt_id"] == "attempt-a"
        assert descriptor["secret"] == "synthetic-private-secret"
        assert "synthetic-private-secret" not in repr(first)
        assert "synthetic-private-secret" not in json.dumps(pool.status(scope()))
        assert (tmp_path / "private" / "lease.json").stat().st_mode & 0o777 == 0o600
        pool.finish(first)
        wait_for(lambda: pool.status(scope())["idle"] == 2)
        assert first.server.stopped
        pool.finish(first)  # Already stopped exact leases are idempotent.
        pool.finish(second)
    finally:
        pool.close()
    assert all(server.stopped for server in made)


@pytest.mark.parametrize("change", [
    {"owner_id": "other"}, {"tenant_id": "other"}, {"worker_id": "other"},
    {"model": "other"}, {"account_id": "other"}, {"configuration_revision": "other"},
])
def test_foreign_scope_cannot_claim_idle_native_process(change):
    pool = NativeWarmPool()
    try:
        pool.prepare(scope(), NativeWarmConfig(1), Server)
        wait_for(lambda: pool.status(scope())["idle"] == 1)
        assert pool.claim(scope(**change), run_id="foreign", attempt_id="attempt") is None
        assert pool.status(scope())["idle"] == 1
    finally:
        pool.close()


def test_changed_worker_configuration_evices_idle_but_keeps_exact_active_lease():
    made = []
    def factory():
        server = Server()
        made.append(server)
        return server
    pool = NativeWarmPool(global_limit=2, owner_limit=2)
    try:
        pool.prepare(scope(), NativeWarmConfig(2), factory)
        wait_for(lambda: pool.status(scope())["idle"] == 2)
        lease = pool.claim(scope(), run_id="run", attempt_id="attempt")
        pool.prepare(scope(model="other"), NativeWarmConfig(1), factory)
        wait_for(lambda: pool.status(scope(model="other"))["idle"] == 1)
        assert not lease.server.stopped
        assert pool.claim(scope(), run_id="new-run", attempt_id="new-attempt") is None
        assert any(server.stopped for server in made)
        pool.finish(lease)
    finally:
        pool.close()


def test_owner_and_global_caps_include_leased_and_starting_processes():
    pool = NativeWarmPool(global_limit=2, owner_limit=1)
    try:
        pool.prepare(scope(), NativeWarmConfig(2), Server)
        wait_for(lambda: pool.status(scope())["idle"] == 1)
        first = pool.claim(scope(), run_id="run", attempt_id="attempt")
        pool.prepare(scope(worker_id="second"), NativeWarmConfig(1), Server)
        pool.prepare(scope(owner_id="other", worker_id="third"), NativeWarmConfig(1), Server)
        wait_for(lambda: pool.status(scope(owner_id="other", worker_id="third"))["idle"] == 1)
        assert pool.status(scope())["idle"] == 0
        assert pool.status(scope(worker_id="second"))["idle"] == 0
        pool.finish(first)
    finally:
        pool.close()


def test_expiry_stops_idle_process_without_continuous_refill():
    made = []
    def factory():
        server = Server()
        made.append(server)
        return server
    pool = NativeWarmPool()
    try:
        # Short internal lifetime makes the same expiry event deterministic in QA.
        pool.prepare(scope(), NativeWarmConfig(1, .05), factory)
        wait_for(lambda: pool.status(scope())["idle"] == 1)
        wait_for(lambda: pool.status(scope())["configured"] == 0)
        assert len(made) == 1 and made[0].stopped
    finally:
        pool.close()


def test_unconfirmed_stop_retains_resource_lease():
    pool = NativeWarmPool()
    try:
        pool.prepare(scope(), NativeWarmConfig(1), Server)
        wait_for(lambda: pool.status(scope())["idle"] == 1)
        lease = pool.claim(scope(), run_id="run", attempt_id="attempt")
        lease.server.stop_error = True
        with pytest.raises(RuntimeError, match="Cannot prove"):
            pool.finish(lease)
        assert pool.status(scope())["leased"] == 1
        lease.server.stop_error = False
        pool.finish(lease)
    finally:
        pool.close()


def test_failed_start_is_not_retried_forever_or_reported_as_inference_failure():
    calls = []
    def factory():
        calls.append(True)
        raise OSError("Synthetic unavailable executable")
    pool = NativeWarmPool()
    try:
        pool.prepare(scope(), NativeWarmConfig(1), factory)
        wait_for(lambda: calls and pool.status(scope())["starting"] == 0)
        assert len(calls) == 1
        assert pool.claim(scope(), run_id="run", attempt_id="attempt") is None
        assert pool.status(scope())["configured"] == 0
    finally:
        pool.close()


def test_disable_while_starting_never_publishes_an_idle_process():
    entered, release = threading.Event(), threading.Event()
    server = Server()
    def factory():
        entered.set()
        assert release.wait(timeout=2)
        return server
    pool = NativeWarmPool()
    try:
        pool.prepare(scope(), NativeWarmConfig(1), factory)
        assert entered.wait(timeout=2)
        pool.prepare(scope(), NativeWarmConfig(0), factory)
        release.set()
        wait_for(lambda: server.stopped)
        assert pool.status(scope())["idle"] == 0
    finally:
        release.set()
        pool.close()


def test_uncertain_idle_stop_stays_in_capacity_budget():
    pool = NativeWarmPool(global_limit=1, owner_limit=1)
    server = Server()
    try:
        pool.prepare(scope(), NativeWarmConfig(1), lambda: server)
        wait_for(lambda: pool.status(scope())["idle"] == 1)
        server.stop_error = True
        with pytest.raises(RuntimeError, match="Cannot prove"):
            pool.prepare(scope(), NativeWarmConfig(0), Server)
        pool.prepare(scope(worker_id="next"), NativeWarmConfig(1), Server)
        assert pool.status(scope(worker_id="next"))["idle"] == 0
        assert pool._counts(("tenant", "owner")) == (1, 1)
        server.stop_error = False
    finally:
        server.stop_error = False
        pool.close()


def test_refresh_during_native_start_keeps_that_same_pending_generation():
    entered, release = threading.Event(), threading.Event()
    server = Server()
    def factory():
        entered.set()
        assert release.wait(timeout=2)
        return server
    pool = NativeWarmPool()
    try:
        pool.prepare(scope(), NativeWarmConfig(1), factory)
        assert entered.wait(timeout=2)
        pool.prepare(scope(), NativeWarmConfig(1), factory)
        release.set()
        wait_for(lambda: pool.status(scope())["idle"] == 1)
        assert not server.stopped
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("observed", ["", "foreign-generation"])
def test_wrong_or_unknown_process_incarnation_is_never_signalled(monkeypatch, observed):
    process = SimpleNamespace(pid=12345, poll=lambda: None, wait=Mock())
    signal = Mock()
    monkeypatch.setattr(warm_owner, "process_start_identity", lambda _: observed)
    monkeypatch.setattr(warm_owner.os, "killpg", signal)
    server = warm_owner.NativeWarmServer(process, "own-generation", "ws://127.0.0.1:12345/ws", "private")
    with pytest.raises(RuntimeError, match="identity cannot be confirmed"):
        server.stop()
    signal.assert_not_called()
    process.wait.assert_not_called()


def test_close_attempts_all_servers_and_retains_only_unconfirmed_stops():
    made = []
    def factory():
        server = Server()
        made.append(server)
        return server
    pool = NativeWarmPool(global_limit=3, owner_limit=3)
    pool.prepare(scope(), NativeWarmConfig(3), factory)
    wait_for(lambda: pool.status(scope())["idle"] == 3)
    made[0].stop_error = True
    with pytest.raises(RuntimeError, match="1 process"):
        pool.close()
    assert made[1].stopped and made[2].stopped
    assert not made[0].stopped
    made[0].stop_error = False
    pool.close()
    assert made[0].stopped


def test_lane_environment_digest_covers_mode_paths_but_never_credentials():
    env = {"GROK_SUBAGENTS": "0", "GROK_WORKFLOWS": "0", "GROK_AUTH_PATH": "/private/native/auth.json",
           "XAI_API_KEY": "first-private-key", "GLASSHIVE_PEER_TOKEN": "private-attempt-token"}
    digest = warm_owner.native_environment_digest(env, lane="conversation")
    assert digest == warm_owner.native_environment_digest({**env, "XAI_API_KEY": "second-private-key"}, lane="conversation")
    assert digest != warm_owner.native_environment_digest({**env, "GROK_AUTH_PATH": "/private/other/auth.json"}, lane="conversation")
    assert digest != warm_owner.native_environment_digest(env, lane="mission")
    with pytest.raises(ValueError, match="disable ephemeral"):
        warm_owner.native_environment_digest({**env, "GROK_SUBAGENTS": "1"}, lane="conversation")


def test_existing_stop_owner_records_escaped_descendants_before_proof(tmp_path):
    path = tmp_path / "private" / "server.native-warm.json"
    warm_owner._write_private(path, {"kind": "xperfect.native.warm.v1"})
    calls = []
    def stop_child(child, remember):
        calls.append("native")
        remember({98765: "ps-lstart:synthetic-escaped"})
        assert warm_owner.read_native_warm_record(path)["escaped_descendants"] == {"98765": "ps-lstart:synthetic-escaped"}
        return True
    def stop_descendants(known):
        calls.append("escaped")
        assert known == {98765: "ps-lstart:synthetic-escaped"}
        return True
    runtime = SimpleNamespace(_end_recorded_native_child=stop_child, _end_remembered_descendants=stop_descendants)
    assert warm_owner.existing_native_stop_owner(runtime, path, {"pid": 12345})
    assert calls == ["native", "escaped"]


def test_restart_cleanup_keeps_live_owner_and_active_attempt_but_stops_orphan(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    for key, phase, pid in [("live", "idle", 1), ("orphan", "idle", 2), ("leased", "leased", 2)]:
        warm_owner._write_private(directory / (key + ".native-warm.json"), {
            "kind": "xperfect.native.warm.v1", "phase": phase,
            "owner": {"pid": pid, "process_start_identity": "owner-live"}, "child": {"pid": 12345}})
    monkeypatch.setattr(warm_owner, "process_start_identity", lambda pid: "owner-live" if pid == 1 else "")
    stopped = []
    monkeypatch.setattr(warm_owner, "existing_native_stop_owner", lambda runtime, path, child: stopped.append(path.name) or True)
    counts = warm_owner.recover_native_warm_records(directory, object(), active_lease=lambda record: record["phase"] == "leased")
    assert counts == {"stopped": 1, "active": 2, "pending": 0}
    assert stopped == ["orphan.native-warm.json"]
    assert (directory / "live.native-warm.json").is_file()
    assert (directory / "leased.native-warm.json").is_file()


def test_orphan_recovery_cannot_delete_guard_between_owner_check_and_identity_write(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    path = directory / "guard.native-warm.json"
    owner_pid = warm_owner.os.getppid()
    warm_owner._write_private(path, {
        "kind": "xperfect.native.warm.v1", "phase": "starting", "child": None,
        "owner": {"pid": owner_pid, "process_start_identity": "owner-generation"},
    })
    owner_alive = [True]
    monkeypatch.setattr(warm_owner, "process_start_identity", lambda pid:
        "owner-generation" if pid == owner_pid and owner_alive[0] else
        "child-generation" if pid == warm_owner.os.getpid() else "")
    entered, release = threading.Event(), threading.Event()
    original_write = warm_owner._write_private
    def paused_write(target, record):
        entered.set()
        assert release.wait(2)
        original_write(target, record)
    monkeypatch.setattr(warm_owner, "_write_private", paused_write)
    executed = []
    monkeypatch.setattr(warm_owner.os, "execvpe", lambda *args: executed.append(True))
    stopped = []
    monkeypatch.setattr(warm_owner, "existing_native_stop_owner", lambda runtime, target, child:
        stopped.append(child) or True)
    guard = threading.Thread(target=warm_owner._guarded_exec,
        args=([str(path), "--", "grok", "agent"],))
    guard.start()
    assert entered.wait(2)
    owner_alive[0] = False
    results = []
    recovery = threading.Thread(target=lambda: results.append(
        warm_owner.recover_native_warm_records(directory, object())))
    recovery.start()
    time.sleep(.02)
    assert recovery.is_alive() and path.exists()
    release.set()
    guard.join(2)
    recovery.join(2)
    assert executed and stopped[0]["process_start_identity"] == "child-generation"
    assert results == [{"stopped": 1, "active": 0, "pending": 0}]
    assert not path.exists()


def test_failed_factory_with_unconfirmed_process_remains_counted_and_owned():
    server = Server()
    server.stop_error = True
    pool = NativeWarmPool(global_limit=1, owner_limit=1)
    second = Mock(side_effect=AssertionError("Unproven failed startup must retain capacity"))
    def failed_factory():
        raise warm_owner.NativeWarmStartupUnconfirmed(server)
    try:
        pool.prepare(scope(), NativeWarmConfig(1), failed_factory)
        wait_for(lambda: bool(pool._stopping))
        assert pool._counts(("tenant", "owner")) == (1, 1)
        pool.prepare(scope(worker_id="second"), NativeWarmConfig(1), second)
        time.sleep(.03)
        second.assert_not_called()
        with pytest.raises(RuntimeError, match="cleanup"):
            pool.close()
        assert server.stops == 1 and pool._counts(("tenant", "owner")) == (1, 1)
    finally:
        server.stop_error = False
        pool.close()
    assert server.stopped


def test_close_stops_all_known_processes_when_startup_thread_exit_is_unconfirmed(monkeypatch):
    server = Server()
    pool = NativeWarmPool()
    pool.prepare(scope(), NativeWarmConfig(1), lambda: server)
    wait_for(lambda: pool.status(scope())["idle"] == 1)
    actual_thread = pool._thread
    class UnconfirmedStartup:
        def join(self, **kwargs):
            pass
        def is_alive(self):
            return True
    pool._thread = UnconfirmedStartup()
    with pytest.raises(RuntimeError, match="startup did not stop"):
        pool.close()
    assert server.stopped and not pool._idle
    actual_thread.join(timeout=2)


def test_close_preserves_uncertain_generation_added_after_cleanup_snapshot():
    first, late = Server(), Server()
    pool = NativeWarmPool()
    pool.prepare(scope(), NativeWarmConfig(1), lambda: first)
    wait_for(lambda: pool.status(scope())["idle"] == 1)
    original_stop = first.stop
    def stop_and_publish_late_failure():
        with pool._condition:
            pool._stopping.append((scope(worker_id="late"), late))
        original_stop()
    first.stop = stop_and_publish_late_failure
    with pytest.raises(RuntimeError, match="cleanup"):
        pool.close()
    assert first.stopped and not late.stopped
    assert pool._counts(("tenant", "owner")) == (1, 1)
    assert pool._stopping == [(scope(worker_id="late"), late)]
    pool.close()
    assert late.stopped and pool._counts(("tenant", "owner")) == (0, 0)


def test_stop_receipt_is_durable_and_secret_free_before_last_native_record_is_removed(tmp_path, monkeypatch):
    path = tmp_path / "state" / "server.native-warm.json"
    descriptor = tmp_path / "attempt" / "lease.json"
    process = Mock(pid=12345)
    process.poll.return_value = 0
    identity = {"pid": 12345, "process_group": 12345,
                "process_start_identity": "ps-lstart:synthetic-generation"}
    monkeypatch.setattr(warm_owner, "process_start_identity", lambda pid: identity["process_start_identity"])
    owner = Mock(return_value=True)
    server = warm_owner.NativeWarmServer(process, identity["process_start_identity"],
        "ws://127.0.0.1:12345/ws", "synthetic-private-secret", path, owner)
    warm_owner._write_private(path, {"kind": "xperfect.native.warm.v1", "phase": "idle",
        "scope": scope().__dict__, "child": identity,
        "escaped_descendants": {"23456": "ps-lstart:escaped-generation"}})
    lease = warm_owner.NativeWarmLease(scope(), "run", "attempt", server)
    lease.write_descriptor(descriptor)
    original_unlink = Path.unlink
    def checked_unlink(target, *args, **kwargs):
        if target == path:
            receipt = warm_owner.read_native_warm_lease(descriptor,
                worker_id="worker", run_id="run", attempt_id="attempt", model="grok-4.7")
            assert receipt["stop_confirmed"] is True
            assert receipt["escaped_descendants"] == {"23456": "ps-lstart:escaped-generation"}
            assert not {"secret", "url"} & receipt.keys()
        return original_unlink(target, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", checked_unlink)
    server.stop()
    server.stop()  # A concurrent finalizer must not create a second cleanup failure.
    owner.assert_called_once_with(identity)
    assert not path.exists()


def test_wrong_attempt_cannot_consume_native_warm_receipt(tmp_path):
    path = tmp_path / "attempt" / "lease.json"
    warm_owner._write_private(path, {"version": 1, "lease_id": "lease", "run_id": "run",
        "attempt_id": "attempt", "scope": scope().__dict__,
        "server": {"pid": 12345, "process_group": 12345,
                   "process_start_identity": "ps-lstart:synthetic-generation"}})
    with pytest.raises(ValueError, match="admitted attempt"):
        warm_owner.read_native_warm_lease(path, worker_id="worker", run_id="other",
                                          attempt_id="attempt", model="grok-4.7")


def test_finish_after_shutdown_is_idempotent_only_for_proven_stopped_lease():
    pool = NativeWarmPool()
    scope = WarmScope('tenant', 'owner', 'worker', 'grok-build', 'model', 'account', 'revision')
    class Server:
        record_path = None
        stopped = False
        def alive(self): return not self.stopped
        def stop(self): self.stopped = True
    server = Server()
    pool.prepare(scope, NativeWarmConfig(1), lambda: server)
    deadline = time.monotonic() + 2
    while pool.status(scope)['idle'] != 1 and time.monotonic() < deadline:
        time.sleep(.005)
    lease = pool.claim(scope, run_id='run', attempt_id='attempt')
    assert lease is not None
    pool.close()
    assert server.stopped
    pool.finish(lease)
    assert pool.status(scope)['leased'] == 0
