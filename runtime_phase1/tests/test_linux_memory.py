import pytest
from workers_projects_runtime.service import host_resource_usage as real_host_resource_usage
from workers_projects_runtime.linux_memory import available_memory_bytes


def fixture(tmp_path, *, legacy=False, membership='/team/member', root='/'):
    proc = tmp_path / 'proc'
    (proc / 'self').mkdir(parents=True)
    (proc / 'meminfo').write_text('MemAvailable: 10000 kB\n')
    (proc / 'self/cgroup').write_text(('5:memory:' if legacy else '0::') + membership + '\n')
    mount = tmp_path / 'cg'
    mount.mkdir()
    (proc / 'self/mountinfo').write_text(f'1 2 0:1 {root} {mount} ro - '+('cgroup cgroup rw,memory' if legacy else 'cgroup2 cgroup rw')+'\n')
    return proc, mount


def limit(path, maximum, current, legacy=False):
    path.mkdir(parents=True, exist_ok=True)
    (path / ('memory.limit_in_bytes' if legacy else 'memory.max')).write_text(str(maximum))
    (path / ('memory.usage_in_bytes' if legacy else 'memory.current')).write_text(str(current))


def test_nested_parent_limit_is_binding(tmp_path):
    proc, cg = fixture(tmp_path)
    limit(cg / 'team/member', 'max', 100)
    limit(cg / 'team', 900, 600)
    assert available_memory_bytes(proc) == 300


def test_container_namespace_mount_root_and_exhaustion(tmp_path):
    proc, cg = fixture(tmp_path, membership='/team/member', root='/team/member')
    limit(cg, 500, 600)
    assert available_memory_bytes(proc) == 0


def test_unlimited_is_bounded_by_measured_host_memory(tmp_path):
    proc, cg = fixture(tmp_path, membership='/')
    limit(cg, 'max', 600)
    assert available_memory_bytes(proc) == 10000 * 1024


def test_legacy_memory_controller(tmp_path):
    proc, cg = fixture(tmp_path, legacy=True)
    limit(cg / 'team/member', 1000, 200, True)
    limit(cg / 'team', 600, 400, True)
    limit(cg, 100000, 1000, True)
    assert available_memory_bytes(proc) == 200


@pytest.mark.parametrize('fault', ['missing_group', 'missing_usage', 'malformed', 'mount_missing', 'no_available'])
def test_probe_failures_do_not_invent_memory(tmp_path, fault):
    proc, cg = fixture(tmp_path, membership='/')
    limit(cg, 1000, 200)
    if fault == 'missing_group':
        (proc / 'self/cgroup').write_text('0::/gone\n')
    elif fault == 'missing_usage':
        (cg / 'memory.current').unlink()
    elif fault == 'malformed':
        (cg / 'memory.max').write_text('unknown')
    elif fault == 'mount_missing':
        (proc / 'self/mountinfo').write_text('')
    else:
        (proc / 'meminfo').write_text('MemFree: 1000 kB\n')
    with pytest.raises((OSError, ValueError, KeyError)):
        available_memory_bytes(proc)


def test_service_linux_selects_native_measurement(monkeypatch):
    from workers_projects_runtime import service, linux_memory
    monkeypatch.setattr(service.sys, 'platform', 'linux')
    monkeypatch.setattr(linux_memory, 'available_memory_bytes', lambda: 12345)
    def reject_subprocess(*args, **kwargs):
        raise AssertionError('No macOS memory subprocess on Linux')
    monkeypatch.setattr(service.subprocess, 'run', reject_subprocess)
    result = real_host_resource_usage([])
    assert result.memory_probe_ok and result.available_memory_bytes == 12345
