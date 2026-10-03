from __future__ import annotations

import pytest
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from fastapi.testclient import TestClient
import workers_projects_runtime.native_model_selection as model_selection

from workers_projects_runtime.api import create_app
from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.native_model_selection import ModelConfigurationRequired, selected_grok_model
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.store import Store


@pytest.fixture
def scoped_catalog(tmp_path, monkeypatch):
    binary = _native_cli(tmp_path)
    home = tmp_path / 'private-native-home'
    home.mkdir()
    monkeypatch.setenv('WPR_GROK_BIN', str(binary))
    monkeypatch.setenv('GROK_HOME', str(home))
    cached = getattr(model_selection, '_native_grok_catalog', None)
    if cached:
        cached.cache_clear()
    calls = []

    def sample(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=0, stdout='Available models:\n  - grok-4.7\n  - grok-4.7-build-fast\n')

    monkeypatch.setattr(model_selection.subprocess, 'run', sample)
    yield binary, home, calls
    cached = getattr(model_selection, '_native_grok_catalog', None)
    if cached:
        cached.cache_clear()


def test_native_catalog_repeated_lookup_reuses_positive_result_without_shared_mutation(scoped_catalog):
    _, _, calls = scoped_catalog
    first = model_selection.native_grok_models()
    first.append('synthetic-other')
    assert model_selection.native_grok_models() == ['grok-4.7', 'grok-4.7-build-fast']
    assert len(calls) == 1


@pytest.mark.parametrize('changed', ['binary', 'settings', 'auth', 'environment', 'home'])
def test_native_catalog_scope_change_refreshes_immediately(scoped_catalog, monkeypatch, changed):
    binary, home, calls = scoped_catalog
    model_selection.native_grok_models()
    if changed == 'binary':
        binary.write_text(binary.read_text() + '\n# synthetic replacement\n')
    elif changed == 'settings':
        (home / 'settings.json').write_text('{"synthetic":true}')
    elif changed == 'auth':
        (home / 'auth.json').write_text('{"synthetic":"changed"}')
    elif changed == 'home':
        replacement = home / 'another-home'
        replacement.mkdir()
        monkeypatch.setenv('GROK_HOME', str(replacement))
    else:
        monkeypatch.setenv('XAI_API_KEY', 'synthetic-changed-credential')
    model_selection.native_grok_models()
    assert len(calls) == 2


def test_native_catalog_refreshes_after_thirty_seconds(scoped_catalog, monkeypatch):
    _, _, calls = scoped_catalog
    now = [0.0]
    monkeypatch.setattr(model_selection.time, 'monotonic', lambda: now[0])
    model_selection.native_grok_models()
    now[0] = 29.9
    model_selection.native_grok_models()
    assert len(calls) == 1
    now[0] = 30.0
    model_selection.native_grok_models()
    assert len(calls) == 2


def test_catalog_observation_reports_actual_hit_scope_and_ttl_without_private_values(scoped_catalog, monkeypatch, caplog):
    import json
    import logging
    _, home, calls = scoped_catalog
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    now = [0.0]
    monkeypatch.setattr(model_selection.time, 'monotonic', lambda: now[0])
    caplog.set_level(logging.INFO, logger=model_selection.__name__)
    model_selection.native_grok_models()
    now[0] = 10.0
    model_selection.native_grok_models()
    (home / 'settings.json').write_text('{"synthetic-private-setting":true}')
    model_selection.native_grok_models()
    now[0] = 40.0
    model_selection.native_grok_models()
    rows = [json.loads(record.message.removeprefix('[NativeP0] ')) for record in caplog.records]
    lookups = [row for row in rows if row['stage'] == 'catalog_lookup']
    assert [row['decision'] for row in lookups] == ['miss_scope_or_cold', 'hit', 'miss_scope_or_cold', 'miss_ttl']
    assert [row['subprocessCount'] for row in lookups] == [1, 0, 1, 1]
    assert len(calls) == 3
    assert lookups[0]['scopeHash'] == lookups[1]['scopeHash']
    assert lookups[1]['componentHashes']['managed_files'] != lookups[2]['componentHashes']['managed_files']
    assert lookups[1]['componentHashes']['environment'] == lookups[2]['componentHashes']['environment']
    assert str(home) not in caplog.text
    assert 'synthetic-private-setting' not in caplog.text
    assert 'grok-4.7' not in caplog.text


def test_catalog_observation_disabled_and_failed_sink_do_not_change_lookup(scoped_catalog, monkeypatch, caplog):
    monkeypatch.delenv('VIVENTIUM_VOICE_LOG_LATENCY', raising=False)
    assert model_selection.native_grok_models() == ['grok-4.7', 'grok-4.7-build-fast']
    assert not caplog.records
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    monkeypatch.setattr(model_selection._logger, 'info', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('synthetic recorder failure')))
    assert model_selection.native_grok_models() == ['grok-4.7', 'grok-4.7-build-fast']


def test_catalog_observation_identifies_join_without_a_second_subprocess(scoped_catalog, monkeypatch, caplog):
    import json
    import logging
    from concurrent.futures import Future
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY','1')
    caplog.set_level(logging.INFO, logger=model_selection.__name__)
    started, joined, release = Event(), Event(), Event()
    class ObservedFuture(Future):
        def result(self, *args, **kwargs):
            joined.set()
            return super().result(*args, **kwargs)
    monkeypatch.setattr(model_selection,'Future',ObservedFuture)
    calls=[]
    def sample(*args, **kwargs):
        calls.append(1); started.set()
        assert release.wait(timeout=2)
        return SimpleNamespace(returncode=0,stdout='Available models:\n  - grok-4.7\n')
    monkeypatch.setattr(model_selection.subprocess,'run',sample)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader=pool.submit(model_selection.native_grok_models)
        assert started.wait(timeout=2)
        follower=pool.submit(model_selection.native_grok_models)
        assert joined.wait(timeout=2)
        release.set()
        assert leader.result(timeout=2)==follower.result(timeout=2)==['grok-4.7']
    rows=[json.loads(record.message.removeprefix('[NativeP0] ')) for record in caplog.records]
    lookups=[row for row in rows if row['stage']=='catalog_lookup']
    assert {row['decision'] for row in lookups}=={'join','miss_scope_or_cold'}
    assert len(calls)==1
    assert next(row for row in lookups if row['decision']=='join')['subprocessCount']==0


@pytest.mark.parametrize('failed', ['empty', 'error'])
def test_native_catalog_does_not_cache_unavailable_results(scoped_catalog, monkeypatch, failed):
    _, _, calls = scoped_catalog

    def sample(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1 if failed == 'error' else 0, stdout='')
        return SimpleNamespace(returncode=0, stdout='Available models:\n  - grok-4.7\n')

    monkeypatch.setattr(model_selection.subprocess, 'run', sample)
    assert model_selection.native_grok_models() == []
    assert model_selection.native_grok_models() == ['grok-4.7']
    assert model_selection.native_grok_models() == ['grok-4.7']
    assert len(calls) == 2


@pytest.mark.parametrize('failed', ['empty', 'error', 'timeout'])
def test_native_catalog_concurrent_failed_lookup_shares_one_probe_then_recovers(
    scoped_catalog, monkeypatch, failed
):
    _, _, calls = scoped_catalog
    ready = Barrier(4)
    all_entered = Event()
    release_probe = Event()
    original_lock = model_selection._catalog_lock
    counter_lock = Lock()
    arrivals = []

    class ObservedLock:
        def __enter__(self):
            with counter_lock:
                arrivals.append(1)
                if len(arrivals) == 4:
                    all_entered.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    monkeypatch.setattr(model_selection, '_catalog_lock', ObservedLock())

    def sample(*args, **kwargs):
        calls.append(1)
        if not release_probe.is_set():
            assert release_probe.wait(timeout=2)
        if failed == 'timeout':
            raise model_selection.subprocess.TimeoutExpired('synthetic', 12)
        return SimpleNamespace(returncode=1 if failed == 'error' else 0, stdout='')

    monkeypatch.setattr(model_selection.subprocess, 'run', sample)

    def lookup():
        ready.wait(timeout=2)
        return model_selection.native_grok_models()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(lookup) for _ in range(4)]
        # Release only after all callers reached the same real lookup admission seam.
        assert all_entered.wait(timeout=2)
        release_probe.set()
        assert [future.result(timeout=3) for future in futures] == [[], [], [], []]
    assert len(calls) == 1
    monkeypatch.setattr(model_selection.subprocess, 'run', lambda *a, **k: (
        calls.append(1) or SimpleNamespace(returncode=0, stdout='Available models:\n  - grok-4.7\n')))
    assert model_selection.native_grok_models() == ['grok-4.7']
    assert model_selection.native_grok_models() == ['grok-4.7']
    assert len(calls) == 2


def _native_cli(tmp_path):
    binary = tmp_path / "grok-synthetic"
    binary.write_text("#!/bin/sh\n[ \"$1\" = models ] || exit 2\nprintf 'Default model: grok-native-b\\nAvailable models:\\n  * grok-native-b (default)\\n  - grok-native-a\\n'\n")
    binary.chmod(0o700)
    return binary


def test_owner_model_choice_is_exact_durable_and_isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD", raising=False)
    monkeypatch.setenv("WPR_GROK_BIN", str(_native_cli(tmp_path)))
    path = str(tmp_path / "runtime.db")
    with TestClient(create_app(path, runtime_backend="stub", runtime=StubRuntime())) as client:
        missing = client.get("/v1/native-models/grok-build")
        assert missing.status_code == 200
        assert missing.json()["status"] == "model_configuration_required"
        assert missing.json()["models"] == ["grok-native-b", "grok-native-a"]
        assert client.patch("/v1/preferences", json={"grok_model": "grok-not-offered"}).status_code == 400
        assert client.patch("/v1/preferences", json={"grok_model": " grok-native-a"}).status_code == 400
        saved = client.patch("/v1/preferences", json={"grok_model": "grok-native-a"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["grok_model"] == "grok-native-a"
        selected = client.get("/v1/native-models/grok-build").json()
        assert (selected["effective_model"], selected["source"], selected["status"]) == (
            "grok-native-a", "owner", "ready",
        )
    store = Store(path)
    assert selected_grok_model(store, "local", "demo-owner") == ("grok-native-a", "owner")
    with pytest.raises(ModelConfigurationRequired):
        selected_grok_model(store, "local", "another-owner")
    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = store
    assert provider._model("grok-build:grok-native-a", owner_id="demo-owner").native_model == "grok-native-a"
    with pytest.raises(ModelConfigurationRequired):
        provider._model("grok-build:grok-native-a", owner_id="another-owner")
    store.close()


def test_deployment_model_wins_without_rewriting_owner_choice(tmp_path, monkeypatch):
    store = Store(tmp_path / "runtime.db")
    store.upsert_user_preferences(tenant_id="local", owner_id="owner", grok_model="grok-native-a")
    monkeypatch.setenv("WPR_MODEL_GROK_BUILD", "grok-native-b")
    assert selected_grok_model(store, "local", "owner") == ("grok-native-b", "deployment")
    monkeypatch.delenv("WPR_MODEL_GROK_BUILD")
    assert selected_grok_model(store, "local", "owner") == ("grok-native-a", "owner")
    assert store.get_user_preferences("local", "owner")["grok_model"] == "grok-native-a"
    store.close()
