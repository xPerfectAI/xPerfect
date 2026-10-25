from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from xperfect_evidence.files import Root, disjoint
from xperfect_evidence.manifest import EvidenceError, Limits, PathPolicy, file_plan, loads, relative


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", "X:/path", "a\x00b", "a\nb", ""])
def test_unsafe_paths_are_rejected(path):
    with pytest.raises(EvidenceError):
        relative(path)


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{'])
def test_json_rejects_ambiguous_or_invalid_values(data):
    with pytest.raises(EvidenceError):
        loads(data)


def test_full_plan_checks_unicode_case_prefix_and_reserved_collisions(sample):
    item = sample["item"]
    for first, second in [("A.txt", "a.txt"), ("caf\u00e9.txt", "cafe\u0301.txt"), ("a", "a/b")]:
        plan = [{**item, "package_path": first}, {**item, "logical_id": "second", "package_path": second}]
        with pytest.raises(EvidenceError):
            file_plan(plan, PathPolicy("NFC", False))
    with pytest.raises(EvidenceError):
        file_plan([{**item, "package_path": "COMMIT.json"}])
    # A destination that explicitly distinguishes case retains that capability.
    file_plan([{**item, "package_path": "A"}, {**item, "logical_id": "second", "package_path": "a"}], PathPolicy("none", True))


def test_bounds_and_membership(sample):
    with pytest.raises(EvidenceError):
        file_plan([sample["item"]], limits=Limits(file_bytes=1))
    with pytest.raises(EvidenceError):
        file_plan([sample["item"], sample["item"]])
    with pytest.raises(EvidenceError):
        loads(b'"long"', Limits(manifest_bytes=2))
    with pytest.raises(EvidenceError):
        disjoint(sample["source"].path, sample["source"].path / "child")


def test_copy_verifies_source_and_destination_and_refuses_links(sample):
    source, target = sample["source"], sample["destination"]
    target.copy(source, "original.txt", "renamed.txt", sample["item"])
    assert target.read("renamed.txt") == sample["data"]
    assert target.snapshot("renamed.txt")["sha256"] == sample["item"]["sha256"]
    (source.path / "link").symlink_to(source.path / "original.txt")
    with pytest.raises(EvidenceError):
        source.snapshot("link")
    os.link(source.path / "original.txt", source.path / "hardlink")
    with pytest.raises(EvidenceError):
        source.snapshot("hardlink")


def test_directory_link_and_changed_bytes_fail(sample):
    (sample["source"].path / "outside").symlink_to(sample["output"].path, target_is_directory=True)
    with pytest.raises(EvidenceError):
        sample["source"].read("outside/result.txt")
    (sample["source"].path / "original.txt").write_bytes(b"changed")
    with pytest.raises(EvidenceError):
        sample["destination"].copy(sample["source"], "original.txt", "result", sample["item"])
    assert not sample["destination"].exists("result")


def test_immutable_receipts_and_unsafe_roots(sample, tmp_path):
    state = sample["state"]
    state.put_json("receipts/one.json", {"id": 1})
    state.put_json("receipts/one.json", {"id": 1})
    with pytest.raises(EvidenceError):
        state.put_json("receipts/one.json", {"id": 2})
    alias = tmp_path / "alias"
    alias.symlink_to(state.path, target_is_directory=True)
    with pytest.raises(EvidenceError):
        Root(alias)


def test_source_mutation_during_stream_is_detected(sample):
    class MutatingSink:
        def write(self, _chunk):
            (sample["source"].path / "original.txt").write_bytes(b"changed in flight")
    with pytest.raises(EvidenceError):
        sample["source"].snapshot("original.txt", sink=MutatingSink())


def test_simultaneous_new_lock_creation_uses_one_stable_inode(sample):
    def acquire(number):
        with sample["state"].lock(f"pairs/{number // 2}/lock"):
            return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(acquire, range(100)))
