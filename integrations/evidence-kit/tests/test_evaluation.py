from __future__ import annotations

from copy import deepcopy

import pytest

from xperfect_evidence.client import UncertainRequest
from xperfect_evidence.evaluation import isolation_receipt, lineage, run_cases, scorecard
from xperfect_evidence.manifest import EvidenceError, canonical, digest, sha
from xperfect_evidence.probe import read_probe


def setup_case(sample):
    (sample["source"].path / "task.txt").write_bytes(b"Copy the admitted input exactly.\n")
    pins = {"profile": "synthetic-cli", "model": "declared-model", "effort": "high", "service_sha256": "1" * 64,
            "image_sha256": "2" * 64, "harness": "native-version", "transport": "native", "bootstrap_sha256": "3" * 64, "skill_sha256": "4" * 64}
    case = {"case_id": "case-1", "worker_id": "worker-1", "task_path": "task.txt", "task_sha256": sha(b"Copy the admitted input exactly.\n"),
            "inputs": [sample["item"]], "pins": pins, "context_mode": "independent", "allowed_roots": ["/workspace/input"], "max_reruns": 1}
    checks = []
    for kind, denied in [("allowed_input", False), ("evaluator_root", True), ("control_root", True)]:
        path = sample["evaluator"].path / (kind + ".canary")
        path.write_bytes(b"Synthetic canary.")
        if denied:
            path.chmod(0)
        probe_result = read_probe(str(path), case_id="case-1", probe_run_id="probe-1")
        sample["evaluator"].put_json(kind + ".json", probe_result)
        checks.append({"kind": kind, **{k: probe_result[k] for k in ("path", "operation", "exit_code", "result")},
                       "evidence_path": kind + ".json", "evidence_sha256": sha(canonical(probe_result)), "canary_sha256": sha(b"Synthetic canary.")})
        path.chmod(0o600)
    probe = {"schema_version": 1, "case_id": "case-1", "context_mode": "independent", "pins_sha256": digest(pins),
             "allowed_roots": case["allowed_roots"], "probe_run_id": "probe-1", "checks": checks}
    return {"schema_version": 1, "batch_id": "batch-1", "max_submissions": 2, "cases": [case]}, probe


class Runtime:
    def __init__(self):
        self.assignments, self.calls = {}, 0
    def worker(self, _worker):
        return {"profile": "synthetic-cli", "model": "declared-model"}
    def assign(self, _worker, task, effort, key):
        if key not in self.assignments:
            self.calls += 1
            self.assignments[key] = {"run_id": "run-" + str(self.calls), "state": "queued", "instruction": task, "effort": effort}
        return self.assignments[key]
    def run(self, run_id):
        return next(i for i in self.assignments.values() if i["run_id"] == run_id)


def execute(sample, manifest, probe, runtime, **kwargs):
    return run_cases(manifest, inputs=sample["source"], evidence=sample["evaluator"], state=sample["state"], client=runtime,
                     observed_pins={"case-1": manifest["cases"][0]["pins"]}, probes={"case-1": probe}, **kwargs)


def test_pin_mismatch_and_missing_probe_fail_before_dispatch(sample):
    manifest, probe = setup_case(sample)
    runtime = Runtime()
    changed = deepcopy(probe)
    changed["checks"][1]["result"] = "read"
    with pytest.raises(EvidenceError):
        execute(sample, manifest, changed, runtime)
    assert runtime.calls == 0
    assert sample["state"].inventory("rejected")
    with pytest.raises(EvidenceError):
        run_cases(manifest, inputs=sample["source"], evidence=sample["evaluator"], state=sample["state"], client=runtime,
                  observed_pins={}, probes={"case-1": probe})
    assert runtime.calls == 0


def test_bounded_resume_uses_same_run_and_explicit_rerun_is_retained(sample):
    manifest, probe = setup_case(sample)
    runtime = Runtime()
    first = execute(sample, manifest, probe, runtime)
    assert execute(sample, manifest, probe, runtime) == first
    assert runtime.calls == 1
    rerun = execute(sample, manifest, probe, runtime, rerun=1)
    assert rerun[0]["run_id"] != first[0]["run_id"] and runtime.calls == 2
    assert len(sample["state"].inventory("batches/observations")) == 2
    with pytest.raises(EvidenceError):
        execute(sample, manifest, probe, runtime, rerun=2)
    reordered = {**manifest, "batch_id": "changed"}
    with pytest.raises(EvidenceError):
        execute(sample, reordered, probe, runtime)


def test_lost_reply_is_recovered_by_same_key_without_new_execution(sample):
    manifest, probe = setup_case(sample)
    class Unclear(Runtime):
        uncertain = True
        def assign(self, *args):
            result = super().assign(*args)
            if self.uncertain:
                self.uncertain = False
                raise UncertainRequest("reply lost")
            return result
    runtime = Unclear()
    with pytest.raises(UncertainRequest):
        execute(sample, manifest, probe, runtime)
    assert execute(sample, manifest, probe, runtime)[0]["run_id"] == "run-1"
    assert runtime.calls == 1


def test_deliberate_delegation_needs_allowed_context_read(sample):
    manifest, probe = setup_case(sample)
    case = manifest["cases"][0]
    case["context_mode"], probe["context_mode"] = "delegated", "delegated"
    with pytest.raises(EvidenceError):
        isolation_receipt(probe, case, sample["evaluator"])
    path = sample["evaluator"].path / "delegated.canary"
    path.write_bytes(b"Authorized context.")
    raw = read_probe(str(path), case_id="case-1", probe_run_id="probe-1")
    sample["evaluator"].put_json("delegated.json", raw)
    probe["checks"].append({"kind": "delegated_context", **{k: raw[k] for k in ("path", "operation", "exit_code", "result")},
                            "evidence_path": "delegated.json", "evidence_sha256": sha(canonical(raw)), "canary_sha256": sha(b"Authorized context.")})
    assert isolation_receipt(probe, case, sample["evaluator"]) is probe


def test_scorecard_keeps_outcome_layers_denominators_and_parallel_time_separate():
    review = {"checked": 1, "correct": 1, "total": 3, "expected_sha256": "1" * 64, "output_sha256": "2" * 64}
    row = {"case_id": "one", "rerun": 0, "run_id": "run-1", "attempt_id": "attempt-1", "native_state": "completed",
           "structure": "valid", "source_review": review, "delivery": "unjudged", "elapsed_seconds": 10,
           "usage": {"state": "unknown"}}
    failed = {**row, "rerun": 1, "run_id": "run-2", "attempt_id": "attempt-2", "native_state": "failed", "structure": "transport_invalid",
              "source_review": {**review, "checked": 0, "correct": 0, "expected_sha256": None}, "elapsed_seconds": 8}
    score = scorecard([row, failed], batch_elapsed_seconds=11)
    assert score["checked"] == 1 and score["unknown"] == 5
    assert score["batch_elapsed_seconds"] == 11 and score["summed_attempt_seconds"] == 18
    assert score["first_pass"] == [row]
    assert score["attempts"][1]["structure"] == "transport_invalid"
    with pytest.raises(EvidenceError):
        scorecard([{**row, "source_review": {**review, "expected_sha256": None}}], batch_elapsed_seconds=1)
    with pytest.raises(EvidenceError):
        scorecard([row, row], batch_elapsed_seconds=1)


def test_lineage_records_distinct_exact_bytes_and_attempts():
    a = lineage(b"Repeat. Repeat.", b"Rules\nRepeat. Repeat.", b"Rules\nRepeat. Repeat.\nTail", run_id="run", attempt_id="attempt-a")
    b = lineage(b"Repeat. Repeat.", b"Rules\nRepeat. Repeat.", b"Rules\nRepeat. Repeat.\nTail", run_id="run", attempt_id="attempt-b")
    assert a["raw_task_sha256"] != a["native_input_sha256"]
    assert a["attempt_id"] != b["attempt_id"] and a["native_input_sha256"] == b["native_input_sha256"]


def test_explicit_exclusion_has_no_fake_run_or_execution(sample):
    manifest, _probe = setup_case(sample)
    manifest["cases"][0]["excluded_reason"] = "Required independent ground truth is unavailable."
    runtime = Runtime()
    result = run_cases(manifest, inputs=sample["source"], evidence=sample["evaluator"], state=sample["state"], client=runtime,
                       observed_pins={}, probes={})
    assert runtime.calls == 0 and result[0]["kind"] == "excluded" and "run_id" not in result[0]
    report = scorecard([], batch_elapsed_seconds=0, dispositions=result)
    assert not report["attempts"] and report["dispositions"] == result


def test_local_attempt_observations_retain_every_runtime_attempt_and_changed_snapshot(sample):
    manifest, probe = setup_case(sample)
    runtime = Runtime()
    rows = [{"run_id": "run-1", "attempt_id": "attempt-1", "state": "failed"},
            {"run_id": "run-1", "attempt_id": "attempt-2", "state": "running"}]
    def run_with_collector():
        return run_cases(manifest, inputs=sample["source"], evidence=sample["evaluator"], state=sample["state"], client=runtime,
                         observed_pins={"case-1": manifest["cases"][0]["pins"]}, probes={"case-1": probe}, attempt_reader=lambda _run: rows)
    assert len(run_with_collector()[0]["evidence_kit"]["attempts"]) == 2
    rows[1] = {**rows[1], "state": "completed"}
    assert run_with_collector()[0]["evidence_kit"]["attempts"][1]["state"] == "completed"
    assert len(sample["state"].inventory("batches/attempts")) == 2 and runtime.calls == 1
