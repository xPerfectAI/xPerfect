"""Create a private synthetic example; never starts a worker or contacts a service."""

from __future__ import annotations

import argparse
from pathlib import Path

from xperfect_evidence.files import Root
from xperfect_evidence.manifest import digest, sha
from xperfect_evidence.validators import validator_identity


def prepare(directory, *, run_id="run-not-assigned", attempt_id="attempt-not-started", event_id="event-1"):
    root_path = Path(directory).absolute()
    root_path.mkdir(mode=0o700)
    root = Root(root_path)
    for name in ("source", "state", "output", "retained", "destination", "evaluator"):
        (root.path / name).mkdir(mode=0o700)
    data = b"Synthetic source: three blue blocks.\n"
    Root(root.path / "source").put("original.txt", data)
    item = {"logical_id": "input-1", "source_id": "physical-1", "relative_path": "original.txt", "package_path": "original.txt",
            "size_bytes": len(data), "sha256": sha(data)}
    event = {"schema_version": 1, "owner_scope": "example-owner", "source_scope": "example-source", "event_id": event_id, "revision": "revision-1", "inputs": [item]}
    output = {**item, "logical_id": "output-1", "source_id": "input-1", "relative_path": "result.txt", "package_path": "Readable Result.txt"}
    root.put_json("source-event.json", event)
    root.put_json("package.json", {"schema_version": 1, "source_manifest_sha256": digest(event), "run_id": run_id,
                                  "attempt_id": attempt_id, "outputs": [output]})
    evaluator = Root(root.path / "evaluator")
    evaluator.put_json("expected.json", {"schema_version": 1, "source_manifest_sha256": digest(event), "outputs": {"output-1": sha(data)}})
    evaluator.put_json("validator-policy.json", {**validator_identity(), "require_source_review": True})
    return {"source_manifest_sha256": digest(event), "run_id": run_id, "attempt_id": attempt_id}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--run-id", default="run-not-assigned")
    parser.add_argument("--attempt-id", default="attempt-not-started")
    parser.add_argument("--event-id", default="event-1")
    args = parser.parse_args()
    print(prepare(args.directory, run_id=args.run_id, attempt_id=args.attempt_id, event_id=args.event_id))
