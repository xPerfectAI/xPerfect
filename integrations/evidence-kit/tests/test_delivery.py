from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from xperfect_evidence.delivery import (accept_callback, publish, reconcile_cursor, redeliver,
                                       retain, stage_source, upload_source)
from xperfect_evidence.manifest import EvidenceError, PathPolicy, canonical, digest, event_key
from xperfect_evidence.validators import validate_package


def kept(sample):
    return retain(sample["package"], output=sample["output"], retained=sample["retained"], source_manifest=sample["event"],
                  policy=sample["policy"], expected=sample["expected"], authority=sample["authority"])["package_sha256"]


def delivery(sample, package_digest, **kwargs):
    path_policy = kwargs.pop("policy", PathPolicy("NFC", False))
    return publish(package_digest, retained=sample["retained"], destination=sample["destination"], transaction_id="transaction-1",
                   generation=1, expected_previous=0, authority=sample["authority"], policy=path_policy, **kwargs)


def test_intake_replay_changed_revision_and_distinct_identical_instances(sample):
    first = stage_source(sample["event"], source=sample["source"], state=sample["state"])
    assert first == stage_source(sample["event"], source=sample["source"], state=sample["state"])
    changed = {**sample["event"], "inputs": [{**sample["item"], "display_name": "changed"}]}
    with pytest.raises(EvidenceError):
        stage_source(changed, source=sample["source"], state=sample["state"])
    (sample["source"].path / "original-copy.txt").write_bytes(sample["data"])
    second_event = {**sample["event"], "event_id": "event-2", "inputs": [sample["item"], {**sample["item"], "logical_id": "input-2", "source_id": "physical-2", "relative_path": "original-copy.txt", "display_name": "original.txt", "package_path": "original_2.txt"}]}
    second = stage_source(second_event, source=sample["source"], state=sample["state"])
    assert second["event_key"] != first["event_key"]
    root = "sources/" + second["event_key"] + "/inputs"
    assert sample["state"].inventory(root) == {digest("input-1"), digest("input-2")}


def test_intake_interruption_recovers_without_committing_early(sample):
    def crash(stage):
        if stage == "before_source_manifest":
            raise RuntimeError("injected crash")
    with pytest.raises(RuntimeError):
        stage_source(sample["event"], source=sample["source"], state=sample["state"], checkpoint=crash)
    assert not sample["state"].exists("sources/" + event_key(sample["event"]) + "/SOURCE.json")
    assert stage_source(sample["event"], source=sample["source"], state=sample["state"])["state"] == "staged"


def test_collision_is_rejected_before_any_write(sample):
    items = [sample["item"], {**sample["item"], "logical_id": "input-2", "package_path": "ORIGINAL.txt"}]
    with pytest.raises(EvidenceError):
        stage_source({**sample["event"], "inputs": items}, source=sample["source"], state=sample["state"], policy=PathPolicy("NFC", False))
    assert list(sample["state"].path.iterdir()) == []


def test_upload_recovers_runtime_file_ids_without_global_hash_dedupe(sample):
    stage_source(sample["event"], source=sample["source"], state=sample["state"])
    class Files:
        def upload(self, **kwargs):
            kwargs["root"].snapshot(kwargs["path"], expected=kwargs["item"])
            return {"upload_id": "file-1", "revision": "version-1", "sha256": kwargs["item"]["sha256"], "size_bytes": kwargs["item"]["size_bytes"]}
        def bind(self, worker, uploads, key):
            return {"worker_id": worker, "uploads": uploads}
    first = upload_source(sample["event"], state=sample["state"], client=Files(), worker_id="worker-1")
    assert upload_source(sample["event"], state=sample["state"], client=Files(), worker_id="worker-1") == first
    assert first["uploads"][0]["revision"] == "version-1"


def test_expired_cursor_uses_scoped_bounded_overlap():
    prior = {"scope": "a", "cursor": "cursor", "last_seen": 200000, "expired": True}
    assert reconcile_cursor(scope="a", prior=prior, initial_since=100000)["since"] == 113600
    assert reconcile_cursor(scope="b", prior=prior, initial_since=100000)["since"] == 100000
    assert reconcile_cursor(scope="a", prior={**prior, "expired": False}, initial_since=100000)["cursor"] == "cursor"


def test_trusted_validator_separates_source_unknown_partial_and_contradicted_baseline(sample):
    report = validate_package(sample["package"], output=sample["output"], source_manifest=sample["event"], policy=sample["policy"], expected=sample["expected"])
    assert report["accepted"] and report["source_review"] == "correct" and report["native_completion"] == "not_asserted"
    unknown = validate_package(sample["package"], output=sample["output"], source_manifest=sample["event"], policy={**sample["policy"], "require_source_review": False})
    assert unknown["source_review"] == "unknown" and unknown["checked"] == 0
    wrong = {**sample["expected"], "outputs": {"output-1": "0" * 64}}
    assert not validate_package(sample["package"], output=sample["output"], source_manifest=sample["event"], policy=sample["policy"], expected=wrong)["accepted"]
    with pytest.raises(EvidenceError):
        validate_package(sample["package"], output=sample["output"], source_manifest=sample["event"], policy={**sample["policy"], "validator_sha256": "0" * 64}, expected=sample["expected"])
    with pytest.raises(EvidenceError):
        validate_package({**sample["package"], "outputs": [{**sample["package"]["outputs"][0], "source_id": "invented"}]}, output=sample["output"], source_manifest=sample["event"], policy=sample["policy"])


@pytest.mark.parametrize("changed_module", ["validators.py", "manifest.py", "files.py"])
def test_validator_pin_covers_each_trusted_validation_module(tmp_path, monkeypatch, changed_module):
    from xperfect_evidence import validators
    source = Path(validators.__file__).parent
    original = validators.validator_identity()
    for name in validators.TRUSTED_VALIDATION_MODULES:
        shutil.copyfile(source / name, tmp_path / name)
    monkeypatch.setattr(validators, "__file__", str(tmp_path / "validators.py"))
    assert validators.validator_identity() == original
    policy = {**original, "require_source_review": True}
    assert validators.selected_policy(policy) == original
    path = tmp_path / changed_module
    path.write_bytes(path.read_bytes() + b"\n# Synthetic trusted-code change.\n")
    assert validators.validator_identity()["validator_sha256"] != original["validator_sha256"]
    with pytest.raises(EvidenceError, match="code pin differs"):
        validators.selected_policy(policy)


def test_earlier_single_file_validator_pin_is_refused():
    from xperfect_evidence import validators
    from xperfect_evidence.files import Root
    identity = validators.validator_identity()
    single_file_pin = Root(Path(validators.__file__).parent).snapshot("validators.py")["sha256"]
    with pytest.raises(EvidenceError, match="code pin differs"):
        validators.selected_policy({**identity, "validator_sha256": single_file_pin, "require_source_review": True})


def test_partial_source_review_and_fake_native_success_cannot_be_full_acceptance(sample):
    second = {**sample["package"]["outputs"][0], "logical_id": "output-2", "package_path": "second.txt"}
    package = {**sample["package"], "outputs": [sample["package"]["outputs"][0], second]}
    review = validate_package(package, output=sample["output"], source_manifest=sample["event"],
                              policy={**sample["policy"], "require_source_review": False}, expected=sample["expected"])
    assert review["source_review"] == "partial" and review["checked"] == 1 and review["total"] == 2
    sample["run"]["state"] = "failed"
    with pytest.raises(EvidenceError):
        kept(sample)
    assert not list(sample["retained"].path.iterdir())


@pytest.mark.parametrize("stage", ["reserved", "file_copied", "before_marker", "after_marker"])
def test_publication_crash_recovery_uses_retained_bytes_only(sample, stage):
    package_digest = kept(sample)
    def crash(current):
        if stage == current:
            raise RuntimeError("injected crash")
    with pytest.raises(RuntimeError):
        delivery(sample, package_digest, checkpoint=crash)
    # Removing the original worker output cannot force another model call.
    (sample["output"].path / "result.txt").unlink()
    result = redeliver(package_digest, retained=sample["retained"], destination=sample["destination"], transaction_id="transaction-1",
                       generation=1, expected_previous=0, authority=sample["authority"], policy=PathPolicy("NFC", False))
    assert result["delivery"] == "verified"
    assert result["recovered"] == (stage == "after_marker")


def test_concurrent_generation_adopts_exact_commit_and_rejects_stale(sample):
    package_digest = kept(sample)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: delivery(sample, package_digest), range(2)))
    assert sorted(r["recovered"] for r in results) == [False, True]
    publish(package_digest, retained=sample["retained"], destination=sample["destination"], transaction_id="transaction-1",
            generation=2, expected_previous=1, authority=sample["authority"], policy=PathPolicy("NFC", False))
    with pytest.raises(EvidenceError):
        delivery(sample, package_digest)


def test_cancelled_or_replaced_attempt_never_commits(sample):
    package_digest = kept(sample)
    sample["run"]["state"] = "cancelled"
    with pytest.raises(EvidenceError):
        delivery(sample, package_digest)
    assert not list(sample["destination"].path.rglob("COMMIT.json"))
    sample["run"]["state"] = "completed"
    sample["run"]["active_attempt_id"] = "replacement"
    with pytest.raises(EvidenceError):
        delivery(sample, package_digest)


def test_authority_loss_at_final_commit_boundary_never_writes_marker(sample):
    package_digest = kept(sample)
    def lose_authority(stage):
        if stage == "before_marker":
            sample["run"]["state"] = "cancelled"
    with pytest.raises(EvidenceError):
        delivery(sample, package_digest, checkpoint=lose_authority)
    assert not list(sample["destination"].path.rglob("COMMIT.json"))


def test_destination_case_collision_is_refused_before_any_destination_write(sample):
    first = sample["package"]["outputs"][0]
    second = {**first, "logical_id": "output-2", "package_path": "readable result.txt"}
    package = {**sample["package"], "outputs": [first, second]}
    expected = {**sample["expected"], "outputs": {"output-1": first["sha256"], "output-2": first["sha256"]}}
    retained = retain(package, output=sample["output"], retained=sample["retained"], source_manifest=sample["event"],
                      policy=sample["policy"], expected=expected, authority=sample["authority"])
    with pytest.raises(EvidenceError):
        delivery(sample, retained["package_sha256"], policy=PathPolicy("NFC", False))
    assert not list(sample["destination"].path.iterdir())


def test_lost_commit_reply_is_truthful_even_after_cancellation(sample):
    package_digest = kept(sample)
    def crash(stage):
        if stage == "after_marker":
            sample["run"]["state"] = "cancelled"
            raise RuntimeError("lost reply")
    with pytest.raises(RuntimeError):
        delivery(sample, package_digest, checkpoint=crash)
    assert delivery(sample, package_digest)["recovered"] is True


def test_retained_tamper_and_destination_extra_bytes_refuse_recovery(sample):
    package_digest = kept(sample)
    delivery(sample, package_digest)
    files = next(sample["destination"].path.rglob("COMMIT.json")).parent / "files"
    (files / "extra.txt").write_text("unexpected")
    with pytest.raises(EvidenceError):
        delivery(sample, package_digest)
    (files / "extra.txt").unlink()
    retained_file = sample["retained"].path / "retained" / package_digest / "files" / digest("output-1")
    retained_file.write_text("tampered")
    with pytest.raises(EvidenceError):
        delivery(sample, package_digest)


def test_callback_authentication_current_attempt_and_durable_duplicate(sample):
    association = {"origin_ref": "origin-ref", "work_ref": "work-ref", "worker_id": "worker-1", "run_id": "run-1"}
    raw = canonical({"callback_id": "callback-1", "callback_ts": 100, "worker_id": "worker-1", "run_id": "run-1", "event": "run.completed"})
    secret = "synthetic-test-secret"
    derived = hmac.new(secret.encode(), b"worker-1:run-1", hashlib.sha256).hexdigest().encode()
    signature = "sha256=" + hmac.new(derived, raw, hashlib.sha256).hexdigest()
    class Reader:
        def verify_callback(self, _association):
            return {"attemptId": "attempt-1"}
        def run(self, _run):
            return dict(sample["run"])
        def assign(self, *_args):
            pytest.fail("Callback acceptance must not execute work")
    def accept(sig=signature):
        return accept_callback(raw, signature=sig, secret=secret, association=association, client=Reader(), state=sample["state"], now=100)
    first, replay = accept(), accept()
    assert first["callback_status"] == "accepted" and replay["callback_status"] == "idempotent"
    assert first["callback_id"] == replay["callback_id"] and first["result_digest"] == replay["result_digest"]
    with pytest.raises(EvidenceError):
        accept("sha256=invalid")
    sample["run"]["active_attempt_id"] = "replacement"
    with pytest.raises(EvidenceError):
        accept()


def test_plain_assignment_callback_uses_current_run_without_inventing_delegation(sample):
    association = {"worker_id": "worker-1", "run_id": "run-1"}
    raw = canonical({"callback_id": "callback-2", "callback_ts": 100, "event": "run.completed", **association})
    secret = "synthetic-test-secret"
    derived = hmac.new(secret.encode(), b"worker-1:run-1", hashlib.sha256).hexdigest().encode()
    signature = "sha256=" + hmac.new(derived, raw, hashlib.sha256).hexdigest()
    class Reader:
        current = "run-1"
        def worker(self, _worker):
            return {"worker_id": "worker-1", "last_run_id": self.current}
        def run(self, _run):
            return dict(sample["run"])
        def verify_callback(self, _association):
            pytest.fail("A plain assignment has no delegation association")
    reader = Reader()
    assert accept_callback(raw, signature=signature, secret=secret, association=association, client=reader, state=sample["state"], now=100)["accepted"]
    reader.current = "run-newer"
    with pytest.raises(EvidenceError):
        accept_callback(raw, signature=signature, secret=secret, association=association, client=reader, state=sample["state"], now=100)


def test_terminal_callback_retry_uses_result_identity_and_exact_cas_ack(sample):
    association = {"worker_id": "worker-1", "run_id": "run-1"}
    secret = "synthetic-test-secret"
    derived = hmac.new(secret.encode(), b"worker-1:run-1", hashlib.sha256).hexdigest().encode()
    class Reader:
        def worker(self, _worker):
            return {"worker_id": "worker-1", "last_run_id": "run-1"}
        def run(self, _run):
            return dict(sample["run"])
    def accept(payload):
        raw = canonical(payload)
        signature = "sha256=" + hmac.new(derived, raw, hashlib.sha256).hexdigest()
        return accept_callback(raw, signature=signature, secret=secret, association=association, client=Reader(), state=sample["state"], now=200)
    payload = {**association, "event": "run.completed", "attempt_id": "attempt-1", "callback_id": "cb_terminal_" + "1" * 64,
               "callback_ts": 100, "result_revision": 1, "result_digest": "sha256:" + "2" * 64}
    first = accept(payload)
    replay = accept({**payload, "callback_ts": 200, "actionCapabilities": [{"fresh": "transport capability"}]})
    assert first["callback_status"] == "accepted" and replay["callback_status"] == "idempotent"
    for key in ("callback_id", "run_id", "result_revision", "result_digest"):
        assert replay[key] == payload[key]
    assert replay["current_callback_id"] == payload["callback_id"]
    assert replay["current_result_revision"] == 1 and replay["current_result_digest"] == payload["result_digest"]
    assert len(sample["state"].inventory("callbacks/accepted")) == 1
    assert len(sample["state"].inventory("callbacks/deliveries")) == 2
    conflict = accept({**payload, "callback_id": "cb_terminal_" + "3" * 64, "result_digest": "sha256:" + "4" * 64})
    assert conflict["callback_status"] == "conflict" and not conflict["accepted"]
    newer = {**payload, "callback_id": "cb_terminal_" + "5" * 64, "result_revision": 2, "result_digest": "sha256:" + "6" * 64}
    assert accept(newer)["callback_status"] == "accepted"
    superseded = accept(payload)
    assert superseded["callback_status"] == "superseded" and superseded["current_result_revision"] == 2


def test_authenticated_control_callback_is_durable_without_terminal_completion(sample):
    association = {"worker_id": "worker-1", "run_id": "run-1"}
    secret = "synthetic-test-secret"
    payload = {**association, "event": "run.started", "callback_id": "cb_started", "callback_ts": 100}
    raw = canonical(payload)
    derived = hmac.new(secret.encode(), b"worker-1:run-1", hashlib.sha256).hexdigest().encode()
    signature = "sha256=" + hmac.new(derived, raw, hashlib.sha256).hexdigest()
    class Reader:
        def worker(self, _worker):
            return {"worker_id": "worker-1", "last_run_id": "run-1"}
        def run(self, _run):
            return {**sample["run"], "state": "running"}
    result = accept_callback(raw, signature=signature, secret=secret, association=association, client=Reader(), state=sample["state"], now=100)
    assert result["accepted"] and result["terminal"] is False
    assert len(sample["state"].inventory("callbacks/control")) == 1
