"""Cold preparation skips only disabled quotas and adapters without CLI probes."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from workers_projects_runtime.direct_runtime import DirectModelRuntime
from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
from workers_projects_runtime.service import GlassHiveQuotaExceededError, WorkersProjectsService
from workers_projects_runtime.openclaw_runtime import RuntimeDependencyMissingError
from workers_projects_runtime.profile_registry import UnsupportedWorkerProfileError


QUOTAS = [
    'GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER',
    'GLASSHIVE_MAX_ACTIVE_WORKERS_PER_TENANT',
    'GLASSHIVE_MAX_WORKSPACES_PER_USER',
    'GLASSHIVE_MAX_WORKSPACES_PER_TENANT',
]
ACTIVE = {'created', 'starting', 'ready', 'running', 'resuming', 'interrupting'}
COUNTS = [
    {'tenant_id': 'tenant-a', 'owner_id': 'owner-a', 'states': ACTIVE},
    {'tenant_id': 'tenant-a', 'states': ACTIVE},
    {'tenant_id': 'tenant-a', 'owner_id': 'owner-a', 'exclude_states': {'terminated'}},
    {'tenant_id': 'tenant-a', 'exclude_states': {'terminated'}},
]


def quota_service(monkeypatch):
    for key in QUOTAS:
        monkeypatch.delenv(key, raising=False)
    service = WorkersProjectsService.__new__(WorkersProjectsService)
    service.store = SimpleNamespace(count_workers=Mock(return_value=0))
    service._quota_workspace_options = Mock(return_value=[{'worker_id': 'synthetic-option'}])
    return service


def test_unlimited_worker_quotas_do_not_count_rows(monkeypatch):
    service = quota_service(monkeypatch)
    service._enforce_worker_limits(tenant_id='tenant-a', owner_id='owner-a')
    service.store.count_workers.assert_not_called()
    service._quota_workspace_options.assert_not_called()


@pytest.mark.parametrize('index', range(4))
@pytest.mark.parametrize('at_limit', [False, True])
def test_enabled_quota_keeps_exact_scope_and_denial(monkeypatch, index, at_limit):
    service = quota_service(monkeypatch)
    monkeypatch.setenv(QUOTAS[index], '3')
    service.store.count_workers.return_value = 3 if at_limit else 2
    if at_limit:
        with pytest.raises(GlassHiveQuotaExceededError) as caught:
            service._enforce_worker_limits(tenant_id='tenant-a', owner_id='owner-a')
        assert caught.value.env_name == QUOTAS[index]
        assert caught.value.limit == 3
        assert caught.value.current_count == 3
        assert caught.value.available_workspace_options == [{'worker_id': 'synthetic-option'}]
        service._quota_workspace_options.assert_called_once_with(
            tenant_id='tenant-a', owner_id='owner-a',
            states=ACTIVE if index < 2 else None,
            exclude_states=None if index < 2 else {'terminated'},
        )
    else:
        service._enforce_worker_limits(tenant_id='tenant-a', owner_id='owner-a')
        service._quota_workspace_options.assert_not_called()
    service.store.count_workers.assert_called_once_with(**COUNTS[index])


def test_mixed_quotas_keep_order_and_stop_at_first_denial(monkeypatch):
    service = quota_service(monkeypatch)
    for index in [0, 2, 3]:
        monkeypatch.setenv(QUOTAS[index], '3')
    service.store.count_workers.side_effect = [1, 3]
    with pytest.raises(GlassHiveQuotaExceededError) as caught:
        service._enforce_worker_limits(tenant_id='tenant-a', owner_id='owner-a')
    assert caught.value.env_name == QUOTAS[2]
    assert [call.kwargs for call in service.store.count_workers.call_args_list] == [COUNTS[0], COUNTS[2]]


@pytest.mark.parametrize('profile', ['direct-openai', 'direct-anthropic', 'direct-xai'])
def test_actual_direct_preflight_has_no_cli_and_keeps_dependency_error(monkeypatch, profile):
    import workers_projects_runtime.direct_runtime as module
    runtime = DirectModelRuntime.__new__(DirectModelRuntime)
    runtime.runtime_name = profile
    assert runtime.preflight_uses_cli_subprocess is False
    monkeypatch.setattr(module.importlib.util, 'find_spec', lambda name: object() if name == 'pydantic_ai' else None)
    runtime.preflight_worker_profile(profile, 'host')
    monkeypatch.setattr(module.importlib.util, 'find_spec', lambda _name: None)
    with pytest.raises(RuntimeDependencyMissingError):
        runtime.preflight_worker_profile(profile, 'host')


def test_profiled_runtime_projects_selected_trait_and_preserves_native_unknown():
    runtime = ProfiledWorkerRuntime.__new__(ProfiledWorkerRuntime)
    runtime.host_direct_xai = SimpleNamespace(preflight_uses_cli_subprocess=False)
    runtime.host_codex = SimpleNamespace(preflight_uses_cli_subprocess=True)
    runtime.host_claude = SimpleNamespace()
    assert runtime.preflight_uses_cli_subprocess_for('direct-xai', 'host') is False
    assert runtime.preflight_uses_cli_subprocess_for('codex-cli', 'host') is True
    assert runtime.preflight_uses_cli_subprocess_for('claude-code', 'host') is True
    with pytest.raises(UnsupportedWorkerProfileError):
        runtime.preflight_uses_cli_subprocess_for('synthetic-missing', 'host')


@pytest.mark.parametrize('selected_trait,legacy_trait,forced,expected_guard', [
    (False, True, False, False), (True, False, False, True),
    (False, True, True, True), (None, False, False, False),
    (None, True, False, True),
])
def test_preflight_reservation_uses_selected_trait_and_forced_snapshot(
    selected_trait, legacy_trait, forced, expected_guard,
):
    service = WorkersProjectsService.__new__(WorkersProjectsService)
    runtime = SimpleNamespace(preflight_worker_profile=Mock(), preflight_uses_cli_subprocess=legacy_trait)
    if selected_trait is not None:
        runtime.preflight_uses_cli_subprocess_for = Mock(return_value=selected_trait)
    service.runtime = runtime
    service._ensure_runtime_available = Mock()
    guarded = []
    @contextmanager
    def guard(*args, **kwargs):
        guarded.append((args, kwargs))
        yield {'synthetic-capacity': True}, object()
    service._durable_preflight_capacity = guard
    result = service._reserved_runtime_preflight('direct-xai', 'host', tenant_id='tenant-a', owner_id='owner-a',
        lane='conversation', require_capacity_snapshot=forced)
    service._ensure_runtime_available.assert_called_once_with('direct-xai', 'host')
    assert bool(guarded) is expected_guard
    assert result == ({'synthetic-capacity': True} if expected_guard else {})


def test_missing_legacy_trait_remains_conservative():
    service = WorkersProjectsService.__new__(WorkersProjectsService)
    service.runtime = SimpleNamespace(preflight_worker_profile=Mock())
    service._ensure_runtime_available = Mock()
    @contextmanager
    def rejected(*_args, **_kwargs):
        raise RuntimeError('Synthetic reservation denied')
        yield
    service._durable_preflight_capacity = rejected
    with pytest.raises(RuntimeError, match='Synthetic reservation denied'):
        service._reserved_runtime_preflight('codex-cli', 'host', tenant_id='tenant-a', owner_id='owner-a')
    service._ensure_runtime_available.assert_not_called()


def test_runtime_without_preflight_keeps_noop_without_trait_projection():
    service = WorkersProjectsService.__new__(WorkersProjectsService)
    service.runtime = SimpleNamespace(preflight_uses_cli_subprocess_for=Mock(side_effect=AssertionError('No preflight owner')))
    assert service._reserved_runtime_preflight('synthetic', 'host', tenant_id='tenant-a', owner_id='owner-a') == {}
    service.runtime.preflight_uses_cli_subprocess_for.assert_not_called()
