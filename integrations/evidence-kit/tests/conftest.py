from __future__ import annotations

import pytest

from xperfect_evidence.files import Root
from xperfect_evidence.manifest import digest, sha
from xperfect_evidence.validators import validator_identity


@pytest.fixture
def sample(tmp_path):
    roots = {}
    for name in ("source", "state", "output", "retained", "destination", "evaluator"):
        path = tmp_path / name
        path.mkdir()
        roots[name] = Root(path)
    data = b"Independent synthetic source.\n"
    (roots["source"].path / "original.txt").write_bytes(data)
    (roots["output"].path / "result.txt").write_bytes(data)
    item = {"logical_id": "input-1", "source_id": "physical-1", "relative_path": "original.txt",
            "package_path": "original.txt", "size_bytes": len(data), "sha256": sha(data)}
    event = {"schema_version": 1, "owner_scope": "owner-a", "source_scope": "source-a", "event_id": "event-1",
             "revision": "revision-1", "inputs": [item]}
    output = {**item, "logical_id": "output-1", "source_id": "input-1", "relative_path": "result.txt", "package_path": "Readable Result.txt"}
    package = {"schema_version": 1, "source_manifest_sha256": digest(event), "run_id": "run-1", "attempt_id": "attempt-1", "outputs": [output]}
    policy = {**validator_identity(), "require_source_review": True}
    expected = {"schema_version": 1, "source_manifest_sha256": digest(event), "outputs": {"output-1": sha(data)}}
    run = {"run_id": "run-1", "worker_id": "worker-1", "active_attempt_id": "attempt-1", "state": "completed"}
    return {**roots, "event": event, "item": item, "package": package, "policy": policy,
            "expected": expected, "run": run, "authority": lambda _run: dict(run), "data": data}
