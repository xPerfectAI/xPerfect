import json
import subprocess
from workers_projects_runtime.docker_sandbox import DockerSandboxManager


def test_fresh_runtime_measures_vm_without_legacy_worker_directory(tmp_path, monkeypatch):
    sandbox = DockerSandboxManager(str(tmp_path))
    assert not (sandbox.runtime_root / 'workers').exists()
    commands = []
    def docker(args, **kwargs):
        commands.append(args[0])
        output = {
            'info': json.dumps({'MemTotal': 8 * 1024**3}),
            'ps': 'external-service\n',
            'stats': json.dumps({'MemUsage': '1GiB / 8GiB'}),
            'exec': 'Size Used Avail\n100000000000 1000000000 99000000000\n',
        }[args[0]]
        return subprocess.CompletedProcess(args, 0, output, '')
    monkeypatch.setattr(sandbox, '_docker', docker)
    usage = sandbox.resource_usage()
    assert usage.process_probe_ok and usage.memory_probe_ok and usage.disk_probe_ok
    assert usage.available_memory_bytes == 7 * 1024**3
    assert usage.running_worker_ids == ()
    assert usage.available_disk_bytes > 0
    assert commands == ['info', 'ps', 'stats', 'exec']
    assert not (sandbox.runtime_root / 'workers').exists()
