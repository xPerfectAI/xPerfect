from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

from workers_projects_runtime.docker_sandbox import DockerSandboxManager, SandboxInfo
from workers_projects_runtime.read_only_workspace import (
    POLICY_LABEL, declared_paths, policy_fingerprint, prepare_paths,
)


@pytest.fixture
def inputs(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager.runtime_root.chmod(0o750)
    paths = manager.paths("wrk_synthetic")
    manager._ensure_host_dirs(paths)
    source = paths["workspace_dir"] / "input"
    source.mkdir(mode=0o700)
    (source / "sample.txt").write_bytes(b"synthetic immutable input")
    (source / "sample.txt").chmod(0o600)
    return manager, paths, source


@pytest.mark.parametrize("value", [None, "input", {}, ["."], [".."], ["../input"], ["/input"],
                                     ["input/"], ["input//nested"], ["input/./nested"], ["a,b"],
                                     ["a:b"], ["a\\b"], ["a\nb"], [3], [".agents/skills/parser"], ["input", "input"],
                                     ["input", "input/nested"], ["a"] * 33])
def test_invalid_or_overlapping_declarations_are_refused(value):
    with pytest.raises(ValueError):
        declared_paths({"read_only_paths": value})


def test_declaration_is_optional_and_order_independent():
    assert declared_paths({}) == ()
    assert policy_fingerprint(()) == ""
    assert declared_paths({"read_only_paths": ["input", ".agents"]}) == (".agents", "input")


def test_managed_copies_become_readable_without_changing_bytes_or_private_root(inputs):
    manager, paths, source = inputs
    nested = source / "nested"
    nested.mkdir(mode=0o700)
    (nested / "data.bin").write_bytes(b"\x00\xff")
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    prepare_paths(manager.runtime_root, paths["workspace_dir"], ("input",))
    assert stat.S_IMODE(source.stat().st_mode) == 0o755
    assert stat.S_IMODE((source / "sample.txt").stat().st_mode) == 0o644
    assert stat.S_IMODE(manager.runtime_root.stat().st_mode) == 0o750
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory_symlink", "fifo"])
def test_untrusted_links_and_special_files_are_rejected(inputs, tmp_path, kind):
    manager, paths, source = inputs
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic outside scope")
    outside.chmod(0o600)
    if kind == "symlink":
        (source / "escape").symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, source / "escape")
    elif kind == "directory_symlink":
        (source / "escape").symlink_to(tmp_path, target_is_directory=True)
    else:
        os.mkfifo(source / "escape")
    with pytest.raises(RuntimeError, match="without links"):
        prepare_paths(manager.runtime_root, paths["workspace_dir"], ("input",))
    assert outside.read_text() == "synthetic outside scope"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600


def test_public_runtime_root_and_outside_workspace_are_refused(inputs, tmp_path):
    manager, paths, _ = inputs
    manager.runtime_root.chmod(0o755)
    with pytest.raises(RuntimeError, match="private runtime root"):
        prepare_paths(manager.runtime_root, paths["workspace_dir"], ("input",))
    with pytest.raises(RuntimeError, match="managed worker workspace"):
        prepare_paths(manager.runtime_root, tmp_path, ("input",))


def test_an_intermediate_symlink_is_never_followed(inputs, tmp_path):
    manager, paths, source = inputs
    (paths["workspace_dir"] / "alias").symlink_to(source, target_is_directory=True)
    with pytest.raises(RuntimeError, match="non-directory"):
        prepare_paths(manager.runtime_root, paths["workspace_dir"], ("alias",))


def test_file_and_nested_declarations_are_refused_before_mounting(inputs):
    manager, paths, source = inputs
    with pytest.raises(ValueError, match="top-level"):
        prepare_paths(manager.runtime_root, paths["workspace_dir"], ("input/sample.txt",))
    assert stat.S_IMODE((source / "sample.txt").stat().st_mode) == 0o600


def test_read_only_container_is_created_inert_with_nested_bind_mounts(inputs, monkeypatch):
    manager, paths, source = inputs
    commands = []
    monkeypatch.setattr(manager, "_ensure_isolated_network", lambda _: "synthetic-network")
    monkeypatch.setattr(manager, "_docker", lambda args, **_: (commands.append(args) or subprocess.CompletedProcess(args, 0)))
    worker = {"worker_id": "wrk_synthetic", "bootstrap_bundle_json": {"read_only_paths": ["input"]}}
    manager._create_container("wpr-wrk-synthetic", paths, worker=worker)
    command = commands[-1]
    assert command[0] == "create"
    assert command[command.index("--label") + 1] == f"{POLICY_LABEL}={policy_fingerprint(('input',))}"
    assert f"type=bind,src={source},dst={manager.workspace_mount}/input,readonly,bind-propagation=rprivate" in command


@pytest.mark.parametrize("defect", ["missing", "writable", "foreign_source", "changed_policy", "nested_bind", "nested_volume", "nested_tmpfs"])
def test_reuse_refuses_changed_mount_authority(inputs, defect):
    manager, paths, source = inputs
    target = manager.workspace_mount + "/input"
    sandbox = SandboxInfo(container_name="synthetic", container_id="synthetic-generation", state="created", workspace_dir=str(paths["workspace_dir"]),
                          home_dir=str(paths["home_dir"]), pid=None, image=manager.image,
                          privileged=False, runtime_user="seluser", cap_add=(),
                          read_only_workspace_policy=policy_fingerprint(("input",)),
                          bind_mount_options=((str(source), target, False, "ro", "rprivate"),),
                          bind_mount_targets=(target,), mount_records=(("bind", str(source), target),))
    worker = {"worker_id": "wrk_synthetic", "bootstrap_bundle_json": {"read_only_paths": ["input"]}}
    manager._require_read_only_workspace_mounts(worker, sandbox)
    if defect == "missing": sandbox.bind_mount_options = ()
    elif defect == "writable": sandbox.bind_mount_options = ((str(source), target, True, "rw", "rprivate"),)
    elif defect == "foreign_source": sandbox.bind_mount_options = (("/foreign", target, False, "ro", "rprivate"),)
    elif defect == "changed_policy": worker["bootstrap_bundle_json"]["read_only_paths"] = []
    elif defect == "nested_tmpfs": sandbox.tmpfs_targets += (target + "/nested",)
    else: sandbox.mount_records += ((defect.removeprefix("nested_"), "/synthetic", target + "/nested"),)
    with pytest.raises(RuntimeError):
        manager._require_read_only_workspace_mounts(worker, sandbox)


@pytest.mark.parametrize("authority", ["root", "privileged", "SYS_ADMIN"])
def test_reuse_refuses_mount_administration_authority(inputs, authority):
    manager, paths, source = inputs
    worker = {"worker_id": "wrk_synthetic", "bootstrap_bundle_json": {"read_only_paths": ["input"]}}
    target = manager.workspace_mount + "/input"
    sandbox = SandboxInfo(container_name="synthetic", container_id="synthetic", state="created",
                          workspace_dir=str(paths["workspace_dir"]), home_dir=str(paths["home_dir"]),
                          pid=None, image=manager.image, privileged=authority == "privileged",
                          runtime_user="root" if authority == "root" else "seluser",
                          cap_add=("SYS_ADMIN",) if authority == "SYS_ADMIN" else (),
                          read_only_workspace_policy=policy_fingerprint(("input",)),
                          bind_mount_options=((str(source), target, False, "ro", "rprivate"),))
    with pytest.raises(RuntimeError, match="unprivileged non-root"):
        manager._require_read_only_workspace_mounts(worker, sandbox)


def test_specific_read_only_path_cannot_be_requested_as_writable(inputs):
    manager, _, _ = inputs
    with pytest.raises(RuntimeError, match="cannot be made writable"):
        manager._ensure_container_writable_paths("synthetic", [manager.workspace_mount + "/input/subdir"], read_only_paths=("input",))


@pytest.mark.parametrize("serialized", [False, True])
def test_host_launch_cannot_silently_ignore_an_immutable_input_policy(tmp_path, serialized):
    import json
    from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
    from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase

    runtime = HostCodexCliRuntime(str(tmp_path / "runtime"))
    workspace = tmp_path / "workspace"
    bundle = {"read_only_paths": ["input"]}
    worker = {"worker_id": "wrk_synthetic", "execution_mode": "host",
              "bootstrap_bundle_json": json.dumps(bundle) if serialized else bundle}
    with pytest.raises(RuntimeErrorBase, match="require Docker"):
        runtime._materialize_workspace(worker, workspace)
    assert not workspace.exists()


@pytest.mark.parametrize("variant", ["reported_without_source", "reported_empty_source", "configured_only"])
def test_inspect_keeps_tmpfs_mount_authority_when_source_is_empty(inputs, variant):
    import json
    manager, paths, source = inputs
    target = manager.workspace_mount + "/input"
    extra = {"Type": "tmpfs", "Destination": target + "/nested"}
    if variant == "reported_empty_source": extra["Source"] = ""
    entry = {"Id": "a" * 64, "Image": "sha256:" + "a" * 64,
        "Config": {"Image": manager.image, "User": "seluser", "Labels": {POLICY_LABEL: policy_fingerprint(("input",))}},
        "HostConfig": {"Privileged": False, "CapAdd": []}, "State": {"Status": "created"},
        "Mounts": [{"Type": "bind", "Source": str(source), "Destination": target,
                    "RW": False, "Mode": "ro", "Propagation": "rprivate"}]}
    if variant == "configured_only":
        entry["HostConfig"]["Mounts"] = [{"Type": "tmpfs", "Target": target + "/nested"}]
    else:
        entry["Mounts"].append(extra)
    sandbox = manager._sandbox_from_inspect_output("wrk_synthetic", json.dumps([entry]))
    assert sandbox is not None
    assert ("tmpfs", "", target + "/nested") in sandbox.mount_records
    worker = {"worker_id": "wrk_synthetic", "bootstrap_bundle_json": {"read_only_paths": ["input"]}}
    with pytest.raises(RuntimeError):
        manager._require_read_only_workspace_mounts(worker, sandbox)
