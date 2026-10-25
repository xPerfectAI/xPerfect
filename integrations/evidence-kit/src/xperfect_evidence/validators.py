"""Trusted application-selected validators. Worker code is never loaded."""

from __future__ import annotations

from pathlib import Path

from .files import Root, disjoint
from .manifest import EvidenceError, digest, fields, file_plan, hash_value, source_event, text


VALIDATOR_ID = "byte-map"
VALIDATOR_VERSION = 1
TRUSTED_VALIDATION_MODULES = ("validators.py", "manifest.py", "files.py")


def validator_identity():
    path = Path(__file__).absolute()
    root = Root(path.parent)
    return {"validator_id": VALIDATOR_ID, "validator_version": VALIDATOR_VERSION,
            "validator_sha256": digest({name: root.snapshot(name)["sha256"]
                                        for name in TRUSTED_VALIDATION_MODULES})}


def selected_policy(policy, *, worker_roots=()):
    fields(policy, {"validator_id", "validator_version", "validator_sha256", "require_source_review"})
    identity = validator_identity()
    if any(policy[k] != identity[k] for k in identity) or type(policy["require_source_review"]) is not bool:
        raise EvidenceError("Trusted validator selection or code pin differs")
    for worker_root in worker_roots:
        disjoint(Path(__file__).parent, worker_root)
    return identity


def validate_package(package, *, output: Root, source_manifest, policy, expected=None):
    source_event(source_manifest, limits=output.limits)
    fields(package, {"schema_version", "source_manifest_sha256", "run_id", "attempt_id", "outputs"})
    if type(package["schema_version"]) is not int or package["schema_version"] != 1 or package["source_manifest_sha256"] != digest(source_manifest):
        raise EvidenceError("Output source ownership differs")
    text(package["run_id"])
    text(package["attempt_id"])
    identity = selected_policy(policy, worker_roots=(output.path,))
    file_plan(package["outputs"], limits=output.limits)
    source_ids = {i["logical_id"] for i in source_manifest["inputs"]}
    output_sources = {i["source_id"] for i in package["outputs"]}
    if output_sources != source_ids:
        raise EvidenceError("Output omits or invents a physical source identity")
    for item in package["outputs"]:
        output.snapshot(item["relative_path"], expected=item)
    checked, correct = 0, 0
    if expected is not None:
        fields(expected, {"schema_version", "source_manifest_sha256", "outputs"})
        if expected["schema_version"] != 1 or expected["source_manifest_sha256"] != digest(source_manifest):
            raise EvidenceError("Expected evidence belongs to another source")
        if not isinstance(expected["outputs"], dict):
            raise EvidenceError("Expected output evidence must be a logical-ID map")
        output_ids = {i["logical_id"] for i in package["outputs"]}
        if expected["outputs"].keys() - output_ids:
            raise EvidenceError("Expected evidence names an absent output")
        for item in package["outputs"]:
            wanted = expected["outputs"].get(item["logical_id"])
            if wanted is not None:
                hash_value(wanted)
                checked += 1
                correct += wanted == item["sha256"]
    total = len(package["outputs"])
    source_state = "correct" if checked == correct == total else "incorrect" if correct < checked else "partial" if checked else "unknown"
    accepted = source_state == "correct" if policy["require_source_review"] else source_state != "incorrect"
    return {"schema_version": 1, **identity, "policy_sha256": digest(policy),
            "source_manifest_sha256": digest(source_manifest), "package_sha256": digest(package),
            "expected_sha256": digest(expected) if expected is not None else None,
            "run_id": package["run_id"], "attempt_id": package["attempt_id"],
            "native_completion": "not_asserted", "structure": "valid", "source_review": source_state,
            "checked": checked, "correct": correct, "total": total, "accepted": accepted}
