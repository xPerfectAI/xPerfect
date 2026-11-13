import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from workers_projects_runtime import runtime_permissions as permissions
from workers_projects_runtime.docker_sandbox import DockerSandboxManager


def test_worker_exec_pins_canonical_numeric_identity(tmp_path):
    manager = DockerSandboxManager(str(tmp_path / "control"))
    requests = []
    manager._docker = lambda args, **kwargs: (requests.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    manager._docker_exec("synthetic", ["/usr/bin/id", "-u"])
    assert requests[0][requests[0].index("-u") + 1] == "1200:1201"


def test_permission_operation_passes_only_control_plane_numeric_identity(tmp_path):
    manager = DockerSandboxManager(str(tmp_path / "control"))
    payloads = []
    def execute(container, command, **kwargs):
        payloads.append(json.loads(command[-1]))
        return subprocess.CompletedProcess(command, 0, "", "")
    manager._docker_exec = execute
    manager._runtime_permission_operation("synthetic", "screen", path="/run/screen/S-seluser")
    assert payloads[0]["uid"] == 1200 and payloads[0]["gid"] == 1201
    assert "user" not in payloads[0]


def test_permission_helper_does_not_resolve_worker_editable_accounts(monkeypatch):
    import pwd
    from types import SimpleNamespace
    monkeypatch.setattr(pwd, "getpwnam", lambda *_: SimpleNamespace(pw_uid=0, pw_gid=0))
    captured = []
    monkeypatch.setattr(permissions, "writable", lambda arguments, uid: captured.append((uid, arguments["gid"])))
    permissions.main({"operation": "writable", "user": "seluser", "uid": 1200, "gid": 1201})
    assert captured == [(1200, 1201)]


def test_workspace_repair_accepts_worker_hardlinks_and_ignores_fifo(tmp_path):
    root = tmp_path / "workspace"; root.mkdir()
    result = root / "result.txt"; result.write_text("completed synthetic result")
    os.link(result, root / "result-copy.txt")
    os.mkfifo(root / "pipe")
    permissions.writable({"paths": [str(root)], "require_acl": False,
        "preserve_owner": True, "host_uid": os.getuid(), "local_world_fallback": False}, os.getuid())
    assert result.read_text() == (root / "result-copy.txt").read_text()
    assert (root / "pipe").is_fifo()


def test_workspace_walk_does_not_depend_on_python_recursion_limit(tmp_path):
    root = tmp_path / "workspace"; root.mkdir()
    leaf = root
    for _ in range(250):
        leaf = leaf / "d"; leaf.mkdir()
    (leaf / "result.txt").write_text("completed synthetic result")
    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(200)
        with permissions.directory(str(root)) as fd:
            seen = sum(1 for _ in permissions.opened_entries(fd, str(root), set()))
    finally:
        sys.setrecursionlimit(limit)
    assert seen == 252


@pytest.mark.parametrize("change", ["deleted", "replaced", "symlink"])
def test_workspace_repair_skips_entries_changed_after_listing(tmp_path, monkeypatch, change):
    root = tmp_path / "workspace"; root.mkdir()
    victim = root / "temporary"; victim.write_text("synthetic temporary file")
    outside = tmp_path / "outside"; outside.write_text("synthetic trusted file"); outside.chmod(0o640)
    before = (outside.read_bytes(), outside.stat().st_mode)
    original = os.open
    changed = False
    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal changed
        if path == "temporary" and not changed:
            changed = True
            victim.unlink()
            if change == "replaced": victim.write_text("replacement")
            elif change == "symlink": victim.symlink_to(outside)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", replace_before_open)
    permissions.writable({"paths": [str(root)], "require_acl": False,
        "preserve_owner": True, "host_uid": os.getuid(), "local_world_fallback": False}, os.getuid())
    assert changed and (outside.read_bytes(), outside.stat().st_mode) == before


@pytest.mark.parametrize("kind", ["leaf_symlink", "parent_symlink", "hardlink"])
def test_secret_maintenance_never_changes_a_link_target(tmp_path, kind):
    home = tmp_path / "home"; home.mkdir()
    target = tmp_path / "root-tool"; target.write_text("synthetic trusted tool"); target.chmod(0o755)
    before = (target.stat().st_uid, target.stat().st_mode, target.read_bytes())
    if kind == "parent_symlink":
        outside = tmp_path / "outside"; outside.mkdir()
        (outside / "secret-runtime.env").symlink_to(target)
        (home / ".glasshive").symlink_to(outside, target_is_directory=True)
    else:
        (home / ".glasshive").mkdir()
        secret = home / ".glasshive/secret-runtime.env"
        if kind == "hardlink": os.link(target, secret)
        else: secret.symlink_to(target)
    with pytest.raises(OSError):
        permissions.secrets({"path": str(home / ".glasshive")}, os.getuid())
    assert (target.stat().st_uid, target.stat().st_mode, target.read_bytes()) == before


def test_secret_leaf_replacement_after_open_cannot_redirect_descriptor(tmp_path, monkeypatch):
    root = tmp_path / "secrets"; root.mkdir()
    secret = root / "secret-runtime.env"; secret.write_text("synthetic secret")
    target = tmp_path / "root-tool"; target.write_text("synthetic trusted tool"); target.chmod(0o755)
    before = (target.stat().st_uid, target.stat().st_mode, target.read_bytes())
    real_open = os.open
    swapped = False
    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        fd = real_open(path, flags, *args, **kwargs)
        if path == "secret-runtime.env" and not swapped:
            swapped = True
            secret.rename(root / "retained-open-secret")
            secret.symlink_to(target)
        return fd
    monkeypatch.setattr(os, "open", racing_open)
    permissions.secrets({"path": str(root)}, os.getuid())
    assert swapped
    assert (target.stat().st_uid, target.stat().st_mode, target.read_bytes()) == before


def test_root_exec_ignores_worker_path_profiles_and_interpreter_environment(tmp_path):
    manager = DockerSandboxManager(str(tmp_path / "control"))
    requests = []
    def docker(args, **kwargs):
        env_file = Path(args[args.index("--env-file") + 1])
        values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        requests.append((args, values))
        return subprocess.CompletedProcess(args, 0, "synthetic", "")
    manager._docker = docker
    manager._docker_exec("synthetic", ["bash", "-lc", "printf synthetic"], user="root",
        env={"PATH": "/worker-controlled", "BASH_ENV": "/worker-hook", "LD_PRELOAD": "/worker-library", "PYTHONPATH": "/worker-modules"})
    args, values = requests[0]
    assert args[-5:] == ["/bin/bash", "--noprofile", "--norc", "-c", "printf synthetic"]
    assert values["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"
    assert all(values[name] == "" for name in ("BASH_ENV", "ENV", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "PYTHONPATH", "PYTHONHOME"))
    assert values["PYTHONSAFEPATH"] == "1" and values["PYTHONNOUSERSITE"] == "1"
    manager._docker_exec("synthetic", ["python3", "-c", "print('synthetic')"], user="0")
    assert requests[1][0][-4:] == ["/usr/bin/python3", "-I", "-c", "print('synthetic')"]

