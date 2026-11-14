"""Evidence publication must not follow paths planted by a worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from workers_projects_runtime import run_evidence


@pytest.fixture(params=["ledger", "evidence"])
def writer(request):
    if request.param == "ledger":
        return run_evidence.write_constraint_ledger, "constraint-ledger.json"
    return run_evidence.write_run_evidence, "evidence.json"


@pytest.mark.parametrize("linked_component", ["glasshive-run", "runs", "run_example"])
@pytest.mark.parametrize("destination", ["outside", "input"])
def test_worker_parent_link_cannot_redirect_evidence(tmp_path, writer, linked_component, destination):
    write, name = writer
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / "outside" if destination == "outside" else workspace / "input"
    protected.mkdir()
    canary = protected / name
    canary.write_bytes(b"retained source\n")
    path = workspace
    for part in ("glasshive-run", "runs", "run_example"):
        path /= part
        if part == linked_component:
            path.symlink_to(protected, target_is_directory=True)
            break
        path.mkdir()

    with pytest.raises(OSError):
        write(workspace, {"run_id": "run_example"}, "run_example")

    assert canary.read_bytes() == b"retained source\n"
    assert sorted(p.name for p in protected.iterdir()) == [name]


def test_linked_workspace_is_refused(tmp_path, writer):
    write, _ = writer
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        write(workspace, {"run_id": "run_example"}, "run_example")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("link_type", ["symbolic", "hard"])
def test_leaf_link_is_replaced_without_changing_its_target(tmp_path, writer, link_type):
    write, name = writer
    target = tmp_path / "source.txt"
    target.write_bytes(b"original\n")
    parent = tmp_path / "glasshive-run" / "runs" / "run_example"
    parent.mkdir(parents=True)
    leaf = parent / name
    if link_type == "symbolic":
        leaf.symlink_to(target)
    else:
        os.link(target, leaf)
    path = write(tmp_path, {"run_id": "run_example"}, "run_example")
    assert path == leaf
    assert target.read_bytes() == b"original\n"
    assert not leaf.is_symlink()
    assert json.loads(leaf.read_text()) == {"run_id": "run_example"}


def test_parent_swap_does_not_redirect_pinned_write(tmp_path, writer, monkeypatch):
    write, name = writer
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / name
    canary.write_bytes(b"original\n")
    parent = tmp_path / "glasshive-run" / "runs" / "run_example"
    parent.mkdir(parents=True)
    retained = parent.with_name("retained")
    original_replace = os.replace
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)
    swapped = False

    def replace(source, destination, **kwargs):
        nonlocal swapped
        target_fd = kwargs.get("dst_dir_fd")
        pinned_parent = (os.fstat(target_fd).st_dev, os.fstat(target_fd).st_ino) if target_fd is not None else None
        if not swapped and Path(destination).name == name and (
            Path(destination).parent == parent or pinned_parent == parent_identity
        ):
            parent.rename(retained)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(run_evidence.os, "replace", replace)
    write(tmp_path, {"run_id": "run_example"}, "run_example")
    assert swapped
    assert canary.read_bytes() == b"original\n"
    assert json.loads((retained / name).read_text()) == {"run_id": "run_example"}


def test_replace_failure_retains_existing_file_and_removes_temporary(tmp_path, writer, monkeypatch):
    write, name = writer
    write(tmp_path, {"run_id": "run_example", "value": "before"}, "run_example")
    parent = tmp_path / "glasshive-run" / "runs" / "run_example"
    before = (parent / name).read_bytes()

    def fail(*args, **kwargs):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(run_evidence.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic replace failure"):
        write(tmp_path, {"run_id": "run_example", "value": "after"}, "run_example")
    assert (parent / name).read_bytes() == before
    assert list(parent.iterdir()) == [parent / name]
    assert not list((tmp_path / "glasshive-run").glob(".*"))


def test_evidence_keeps_bytes_mode_and_exact_run_identity(tmp_path, writer):
    write, name = writer
    payload = {"run_id": "run_example", "value": ["example", 1]}
    path = write(tmp_path, payload, "run_example")
    expected = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    latest = tmp_path / "glasshive-run" / name
    assert path == latest.parent / "runs" / "run_example" / name
    assert path.read_bytes() == latest.read_bytes() == expected
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(latest.stat().st_mode) == 0o644
    write(tmp_path, {"run_id": "run_next"}, "run_next")
    assert path.read_bytes() == expected


def test_ledger_can_write_run_without_publishing_latest(tmp_path):
    latest = tmp_path / "glasshive-run" / "constraint-ledger.json"
    latest.parent.mkdir()
    latest.write_bytes(b"retained latest\n")
    path = run_evidence.write_constraint_ledger(
        tmp_path, {"run_id": "run_example"}, "run_example", publish_latest=False,
    )
    assert latest.read_bytes() == b"retained latest\n"
    assert path == latest.parent / "runs" / "run_example" / latest.name


def test_runtime_caller_refuses_parent_link_without_publishing_a_receipt(tmp_path, monkeypatch):
    from workers_projects_runtime.profile_runtime import _write_constraint_ledger_for_run
    import socket

    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "constraint-ledger.json"
    canary.write_bytes(b"retained source\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "glasshive-run").symlink_to(outside, target_is_directory=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("No provider connection is permitted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    _, path = _write_constraint_ledger_for_run(
        worker={"worker_id": "wrk_example", "profile": "codex-cli", "execution_mode": "docker"},
        instruction="Write a synthetic report.", workspace=workspace, run_id="run_example",
    )
    assert path == ""
    assert canary.read_bytes() == b"retained source\n"
    assert list(outside.iterdir()) == [canary]
