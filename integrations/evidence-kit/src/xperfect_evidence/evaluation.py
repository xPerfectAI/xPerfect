"""Pinned cases and source-led scorecards over existing runtime identities."""

from __future__ import annotations

from .files import Root, disjoint
from .client import UncertainRequest
from .manifest import (EvidenceError, Limits, canonical, digest, fields, file_plan,
                       finite_seconds, hash_value, integer, sha, text)


PIN_FIELDS = {"profile", "model", "effort", "service_sha256", "image_sha256", "harness", "transport", "bootstrap_sha256", "skill_sha256"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "terminated", "expired"}


def case_manifest(value, limits=Limits()):
    fields(value, {"schema_version", "batch_id", "max_submissions", "cases"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise EvidenceError("Unsupported case manifest version")
    text(value["batch_id"])
    integer(value["max_submissions"], maximum=limits.files)
    cases = value["cases"]
    if not isinstance(cases, list) or not cases or len(cases) > limits.files:
        raise EvidenceError("Invalid case count")
    seen = set()
    for case in cases:
        fields(case, {"case_id", "worker_id", "task_path", "task_sha256", "inputs", "pins", "context_mode", "allowed_roots", "max_reruns"}, {"excluded_reason"})
        if "excluded_reason" in case:
            text(case["excluded_reason"], maximum=1024)
        for key in ("case_id", "worker_id", "task_path"):
            text(case[key])
        if case["case_id"] in seen:
            raise EvidenceError("Duplicate case identity")
        seen.add(case["case_id"])
        hash_value(case["task_sha256"])
        if not isinstance(case["inputs"], list):
            raise EvidenceError("Case inputs must be a list")
        if case["inputs"]:
            file_plan(case["inputs"], limits=limits)
        fields(case["pins"], PIN_FIELDS)
        for key, item in case["pins"].items():
            hash_value(item) if key.endswith("sha256") else text(item)
        if case["context_mode"] not in {"independent", "delegated"}:
            raise EvidenceError("Unknown context mode")
        if not isinstance(case["allowed_roots"], list) or not case["allowed_roots"] or len(set(case["allowed_roots"])) != len(case["allowed_roots"]):
            raise EvidenceError("Allowed roots must be explicit and unique")
        for root in case["allowed_roots"]:
            text(root, maximum=1024)
        integer(case["max_reruns"], maximum=100)
    return value


def isolation_receipt(receipt, case, evidence: Root):
    """Consume a trusted operator probe, never a worker's assertion of isolation."""
    fields(receipt, {"schema_version", "case_id", "context_mode", "pins_sha256", "allowed_roots", "probe_run_id", "checks"})
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1 or receipt["case_id"] != case["case_id"] or receipt["context_mode"] != case["context_mode"] or receipt["pins_sha256"] != digest(case["pins"]) or receipt["allowed_roots"] != case["allowed_roots"]:
        raise EvidenceError("Isolation receipt is not bound to this case and runtime")
    text(receipt["probe_run_id"])
    checks = receipt["checks"]
    if not isinstance(checks, list):
        raise EvidenceError("Missing actual read probes")
    required = {"allowed_input", "evaluator_root", "control_root"} if case["context_mode"] == "independent" else {"allowed_input", "delegated_context", "evaluator_root", "control_root"}
    observed = set()
    for check in checks:
        fields(check, {"kind", "path", "operation", "exit_code", "result", "evidence_path", "evidence_sha256", "canary_sha256"})
        if check["kind"] in observed or check["operation"] != "read":
            raise EvidenceError("Invalid or repeated read probe")
        observed.add(check["kind"])
        text(check["path"], maximum=1024)
        hash_value(check["evidence_sha256"])
        hash_value(check["canary_sha256"])
        raw = evidence.read(check["evidence_path"])
        if sha(raw) != check["evidence_sha256"]:
            raise EvidenceError("Actual read probe evidence changed")
        from .manifest import loads
        observed_probe = loads(raw, evidence.limits)
        fields(observed_probe, {"case_id", "probe_run_id", "operation", "path", "result", "exit_code", "content_sha256", "errno"})
        if (observed_probe["case_id"] != case["case_id"] or observed_probe["probe_run_id"] != receipt["probe_run_id"]
                or any(observed_probe[k] != check[k] for k in ("path", "operation", "result", "exit_code"))):
            raise EvidenceError("Read probe does not match its exact case and attempt")
        integer(check["exit_code"], maximum=255)
        denied = check["kind"] in {"evaluator_root", "control_root"}
        if denied:
            valid = (check["result"] in {"denied", "not_visible"} and check["exit_code"] != 0
                     and observed_probe["content_sha256"] is None
                     and observed_probe["errno"] in ({2} if check["result"] == "not_visible" else {1, 13}))
        else:
            valid = check["result"] == "read" and check["exit_code"] == 0 and observed_probe["content_sha256"] == check["canary_sha256"]
        if not valid:
            raise EvidenceError("Required isolation or allowed-context read was not proved")
    if observed != required:
        raise EvidenceError("Isolation probe set is incomplete")
    return receipt


def run_cases(manifest, *, inputs: Root, evidence: Root, state: Root, client,
              observed_pins, probes, rerun=0, maximum=None, attempt_reader=None):
    """One bounded pass. The runtime owns pending work and retry scheduling."""
    case_manifest(manifest, state.limits)
    disjoint(inputs.path, evidence.path, state.path)
    if not isinstance(observed_pins, dict) or not isinstance(probes, dict):
        raise EvidenceError("Observed pins and probes must be case-ID maps")
    maximum = len(manifest["cases"]) if maximum is None else integer(maximum, maximum=len(manifest["cases"]))
    batch = digest(manifest)
    # All inputs, pins and probe receipts are checked before any assignment.
    tasks = {}
    for case in manifest["cases"]:
        if "excluded_reason" in case:
            continue
        try:
            if observed_pins.get(case["case_id"]) != case["pins"]:
                raise EvidenceError("Configured runtime pins are missing or differ")
            if rerun > case["max_reruns"] or rerun < 0:
                raise EvidenceError("Rerun is outside the declared bound")
            isolation_receipt(probes.get(case["case_id"]), case, evidence)
            raw = inputs.read(case["task_path"])
            if sha(raw) != case["task_sha256"]:
                raise EvidenceError("Original task pin differs")
            tasks[case["case_id"]] = raw.decode("utf-8")
            for item in case["inputs"]:
                inputs.snapshot(item["relative_path"], expected=item)
            worker = client.worker(case["worker_id"])
            # The operator receipt pins substrate data not exposed in ordinary HTTP.
            if worker.get("profile") != case["pins"]["profile"] or worker.get("model") != case["pins"]["model"]:
                raise EvidenceError("Current worker selection differs from the case")
        except EvidenceError as exc:
            receipt = {"case_id": case["case_id"], "kind": "setup_failure", "stage": "preflight", "failure_type": type(exc).__name__, "batch_sha256": batch}
            state.put_json(f"rejected/{batch}/{digest(receipt)}.json", receipt)
            raise
    results = []

    def observe(run, key):
        if attempt_reader is None:
            return {**run, "evidence_kit": {"attempt_history": "unavailable"}}
        rows = attempt_reader(run["run_id"])
        if not isinstance(rows, list) or len(rows) > state.limits.files:
            raise EvidenceError("Attempt collector returned an unbounded result")
        seen = set()
        for row in rows:
            if row.get("run_id") != run["run_id"] or not row.get("attempt_id") or row["attempt_id"] in seen:
                raise EvidenceError("Attempt history does not match the runtime run")
            seen.add(row["attempt_id"])
        snapshot = {"run_id": run["run_id"], "attempts": rows}
        state.put_json(f"batches/attempts/{key}/{digest(snapshot)}.json", snapshot)
        return {**run, "evidence_kit": {"attempt_history": "observed", "attempts": rows}}

    with state.lock("batches/lock"):
        state.put_json("batches/manifest.json", manifest)
        count = state.json("batches/submissions.json") if state.exists("batches/submissions.json") else {"keys": []}
        for case in manifest["cases"][:maximum]:
            if "excluded_reason" in case:
                excluded = {"case_id": case["case_id"], "kind": "excluded", "reason": case["excluded_reason"]}
                state.put_json(f"batches/exclusions/{digest(case['case_id'])}.json", excluded)
                results.append(excluded)
                continue
            key = digest({"batch": batch, "case": case["case_id"], "rerun": rerun})
            if key not in count["keys"]:
                if len(count["keys"]) >= manifest["max_submissions"]:
                    raise EvidenceError("Submission bound reached; no new work was started")
                # Retain intent before sending. A setup fault consumes its declared slot.
                count["keys"].append(key)
                state.put_json("batches/submissions.json", count, replace=True)
            receipt_path = f"batches/observations/{key}.json"
            if state.exists(receipt_path):
                prior = state.json(receipt_path)
                if prior.get("run_id"):
                    results.append(observe(client.run(prior["run_id"]), key))
                    continue
                if prior.get("kind") != "uncertain_request":
                    raise EvidenceError("Prior setup failure retained; declare an explicit rerun")
            try:
                run = client.assign(case["worker_id"], tasks[case["case_id"]], case["pins"]["effort"], key)
            except EvidenceError as exc:
                kind = "uncertain_request" if isinstance(exc, UncertainRequest) else "setup_failure"
                state.put_json(receipt_path, {"case_id": case["case_id"], "rerun": rerun, "kind": kind, "failure_type": type(exc).__name__}, replace=True)
                raise
            state.put_json(receipt_path, {"case_id": case["case_id"], "rerun": rerun, "run_id": run["run_id"], "assignment_key": key}, replace=state.exists(receipt_path))
            results.append(observe(run, key))
    return results


def lineage(raw: bytes, rendered: bytes, native_input: bytes, *, run_id, attempt_id):
    text(run_id)
    text(attempt_id)
    return {"run_id": run_id, "attempt_id": attempt_id,
            "raw_task_sha256": sha(raw), "rendered_sha256": sha(rendered), "native_input_sha256": sha(native_input)}


def scorecard(observations, *, batch_elapsed_seconds, dispositions=()):
    """Explicit denominators; baseline agreement is never source truth."""
    finite_seconds(batch_elapsed_seconds)
    seen, all_rows, duration, reviewed, correct, unknown = set(), [], 0, 0, 0, 0
    for row in observations:
        fields(row, {"case_id", "rerun", "run_id", "attempt_id", "native_state", "structure", "source_review", "delivery", "elapsed_seconds", "usage"})
        key = (text(row["run_id"]), text(row["attempt_id"]))
        if key in seen:
            raise EvidenceError("Duplicate exact attempt observation")
        seen.add(key)
        integer(row["rerun"], maximum=100)
        duration += finite_seconds(row["elapsed_seconds"])
        review = row["source_review"]
        fields(review, {"checked", "correct", "total", "expected_sha256", "output_sha256"})
        for name in ("checked", "correct", "total"):
            integer(review[name])
        if not review["correct"] <= review["checked"] <= review["total"]:
            raise EvidenceError("Review denominator is inconsistent")
        hash_value(review["output_sha256"])
        if review["expected_sha256"] is not None:
            hash_value(review["expected_sha256"])
        elif review["checked"]:
            raise EvidenceError("Source correctness requires independent expected evidence")
        reviewed += review["checked"]
        correct += review["correct"]
        unknown += review["total"] - review["checked"]
        all_rows.append(row)
        text(row["case_id"])
    separate = []
    for row in dispositions:
        fields(row, {"case_id", "kind", "reason"})
        text(row["case_id"])
        text(row["reason"], maximum=1024)
        if row["kind"] not in {"excluded", "setup_failure", "unjudged"}:
            raise EvidenceError("Unknown case disposition")
        separate.append(row)
    return {"schema_version": 1, "attempts": all_rows, "dispositions": separate, "first_pass": [r for r in all_rows if r["rerun"] == 0],
            "later_submissions": [r for r in all_rows if r["rerun"] > 0],
            "checked": reviewed, "correct": correct, "unknown": unknown,
            "batch_elapsed_seconds": batch_elapsed_seconds, "summed_attempt_seconds": duration}
