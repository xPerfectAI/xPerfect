"""Optional immutable intake and local destination transactions; no execution API."""

from __future__ import annotations

import hashlib
import hmac
import time

from .evaluation import TERMINAL
from .files import Root, disjoint
from .manifest import (EvidenceError, PathPolicy, canonical, digest, event_key,
                       fields, file_plan, hash_value, integer, loads, source_event, text)
from .validators import selected_policy, validate_package


def stage_source(event, *, source: Root, state: Root, policy=PathPolicy(), checkpoint=lambda _stage: None):
    source_event(event, policy, state.limits)
    disjoint(source.path, state.path)
    for item in event["inputs"]:
        source.snapshot(item["relative_path"], expected=item)
    key, event_digest = event_key(event), digest(event)
    base = "sources/" + key
    with state.lock(base + "/lock"):
        if state.exists(base + "/SOURCE.json"):
            if digest(state.json(base + "/SOURCE.json")) != event_digest:
                raise EvidenceError("Existing source-event revision changed")
        for item in event["inputs"]:
            state.copy(source, item["relative_path"], base + "/inputs/" + digest(item["logical_id"]), item)
            checkpoint("input_staged")
        if state.inventory(base + "/inputs") != {digest(i["logical_id"]) for i in event["inputs"]}:
            raise EvidenceError("Staged source inventory differs")
        checkpoint("before_source_manifest")
        state.put_json(base + "/SOURCE.json", event)
    return {"event_key": key, "source_manifest_sha256": event_digest, "state": "staged"}


def upload_source(event, *, state: Root, client, worker_id):
    """Reuse the existing file owner. This operation never assigns work."""
    source_event(event, limits=state.limits)
    key, base = event_key(event), "sources/" + event_key(event)
    if state.json(base + "/SOURCE.json") != event:
        raise EvidenceError("Staged source differs")
    with state.lock(base + "/lock"):
        uploads = []
        for item in event["inputs"]:
            upload = client.upload(root=state, path=base + "/inputs/" + digest(item["logical_id"]), item=item,
                                   key=digest({"event": key, "input": item["logical_id"]}), draft_id=key)
            uploads.append({"logical_id": item["logical_id"], **upload})
        # A ready file may have been renamed or revised since an earlier receipt.
        binding = client.bind(worker_id, uploads, digest({"event": key, "worker": worker_id, "kind": "files"}))
        receipt = {"source_manifest_sha256": digest(event), "worker_id": worker_id, "uploads": uploads}
        state.put_json(base + "/UPLOADS.json", receipt)
        return {**receipt, "binding": binding}


def reconcile_cursor(*, scope, prior, initial_since, overlap_seconds=86400):
    """Neutral expired-cursor handoff. The source adapter supplies event revisions."""
    text(scope)
    integer(initial_since)
    integer(overlap_seconds, maximum=31 * 86400)
    if prior is None or prior.get("scope") != scope:
        return {"scope": scope, "cursor": None, "since": initial_since, "reconcile": True}
    fields(prior, {"scope", "cursor", "last_seen", "expired"})
    integer(prior["last_seen"])
    if type(prior["expired"]) is not bool:
        raise EvidenceError("Invalid source cursor state")
    if prior["expired"]:
        return {"scope": scope, "cursor": None, "since": max(initial_since, prior["last_seen"] - overlap_seconds), "reconcile": True}
    text(prior["cursor"], maximum=4096)
    return {"scope": scope, "cursor": prior["cursor"], "since": None, "reconcile": False}


def accept_callback(raw, *, signature, secret, association, client, state: Root,
                    now=None, horizon_seconds=7 * 86400):
    """Durably accept authenticated transport, then let the application process it."""
    if not secret:
        raise EvidenceError("Receiver secret is required")
    fields(association, {"worker_id", "run_id"}, {"origin_ref", "work_ref"})
    if ("origin_ref" in association) != ("work_ref" in association):
        raise EvidenceError("Delegated callbacks need both origin and work references")
    payload = loads(raw, state.limits)
    if not isinstance(payload, dict):
        raise EvidenceError("Callback is not an object")
    if payload.get("worker_id") != association["worker_id"] or payload.get("run_id") != association["run_id"]:
        raise EvidenceError("Callback identity differs from the trusted association")
    binding = f"{association['worker_id']}:{association['run_id']}".encode()
    derived = hmac.new(secret.encode(), binding, hashlib.sha256).hexdigest().encode()
    wanted = "sha256=" + hmac.new(derived, raw, hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(wanted, signature):
        raise EvidenceError("Callback signature differs")
    callback_id = text(payload.get("callback_id"))
    integer(payload.get("callback_ts"))
    integer(horizon_seconds, maximum=31 * 86400)
    if abs((time.time() if now is None else now) - payload["callback_ts"]) > horizon_seconds:
        raise EvidenceError("Callback is outside its configured recovery horizon")
    if "origin_ref" in association:
        verified = client.verify_callback(association)
        expected_attempt = verified.get("attemptId")
    else:
        worker = client.worker(association["worker_id"])
        if worker.get("worker_id") != association["worker_id"] or worker.get("last_run_id") != association["run_id"]:
            raise EvidenceError("Callback is not the worker's current run")
        expected_attempt = None
    run = client.run(association["run_id"])
    if run.get("worker_id") != association["worker_id"] or run.get("run_id") != association["run_id"]:
        raise EvidenceError("Callback has no matching current run")
    event = text(payload.get("event"))
    terminal_events = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
    control_events = {"run.queued", "run.waiting_on_capacity", "run.started", "run.paused"}
    if event not in terminal_events | control_events:
        raise EvidenceError("Unsupported callback event")
    delivery = {"callback_id": callback_id, "run_id": run["run_id"],
                "payload_sha256": hashlib.sha256(raw).hexdigest(), "callback_ts": payload["callback_ts"]}
    if event in control_events:
        receipt = {"callback_id": callback_id, "run_id": run["run_id"], "association_sha256": digest(association),
                   "event": event, "accepted": True, "terminal": False}
        with state.lock("callbacks/lock"):
            state.put_json("callbacks/control/" + digest(callback_id) + ".json", receipt)
            state.put_json("callbacks/deliveries/" + delivery["payload_sha256"] + ".json", delivery)
        return receipt
    if run.get("state") not in TERMINAL or not run.get("active_attempt_id"):
        raise EvidenceError("Terminal callback has no matching terminal run")
    revision = payload.get("result_revision", 0)
    integer(revision)
    if revision:
        result_digest = payload.get("result_digest")
        if not isinstance(result_digest, str) or not result_digest.startswith("sha256:"):
            raise EvidenceError("Terminal result digest is missing")
        hash_value(result_digest[7:])
    else:
        if "result_digest" in payload:
            raise EvidenceError("Result digest needs its positive revision")
        result_digest = "sha256:" + digest({"run_id": run["run_id"], "attempt_id": run["active_attempt_id"],
                                            "state": run["state"], "output_text": run.get("output_text", ""),
                                            "error_text": run.get("error_text", "")})
    receipt = {"callback_id": callback_id, "association_sha256": digest(association), "run_id": run["run_id"],
               "attempt_id": run["active_attempt_id"], "native_state": run["state"], "event": event,
               "result_revision": revision, "result_digest": result_digest}
    with state.lock("callbacks/lock"):
        current_path = "callbacks/current/" + digest(run["run_id"]) + ".json"
        current = state.json(current_path) if state.exists(current_path) else None
        if current and current["association_sha256"] != receipt["association_sha256"]:
            raise EvidenceError("Callback owner association changed")
        if current and current["result_revision"] > revision:
            decision = "superseded"
        elif ((expected_attempt is not None and run["active_attempt_id"] != expected_attempt)
              or (payload.get("attempt_id") is not None and payload["attempt_id"] != run["active_attempt_id"])):
            raise EvidenceError("Callback attempt is no longer current")
        elif current and current["result_revision"] == revision and current != receipt:
            decision = "conflict"
        else:
            decision = "idempotent" if current == receipt else "accepted"
            state.put_json("callbacks/accepted/" + digest(callback_id) + ".json", receipt)
            state.put_json(current_path, receipt, replace=True)
            current = receipt
        state.put_json("callbacks/deliveries/" + delivery["payload_sha256"] + ".json", delivery)
    # These are the existing sender's exact monotonic result-CAS ACK fields.
    return {**receipt, "accepted": decision in {"accepted", "idempotent"}, "terminal": True,
            "callback_status": decision, "current_callback_id": current["callback_id"],
            "current_result_revision": current["result_revision"], "current_result_digest": current["result_digest"]}


def _authority(reader, package):
    run = reader(package["run_id"])
    if (run.get("run_id") != package["run_id"] or run.get("active_attempt_id") != package["attempt_id"]
            or run.get("state") != "completed"):
        raise EvidenceError("Current run/attempt authority does not permit publication")
    return run


def retain(package, *, output: Root, retained: Root, source_manifest, policy, authority, expected=None):
    source_event(source_manifest, limits=retained.limits)
    disjoint(output.path, retained.path)
    text(package.get("run_id"))
    text(package.get("attempt_id"))
    _authority(authority, package)
    validation = validate_package(package, output=output, source_manifest=source_manifest, policy=policy, expected=expected)
    if not validation["accepted"]:
        raise EvidenceError("Independent validation did not accept this output")
    package_digest = digest(package)
    base = "retained/" + package_digest
    with retained.lock(base + "/lock"):
        for item in package["outputs"]:
            retained.copy(output, item["relative_path"], base + "/files/" + digest(item["logical_id"]), item)
        if retained.inventory(base + "/files") != {digest(i["logical_id"]) for i in package["outputs"]}:
            raise EvidenceError("Retained file inventory differs")
        retained.put_json(base + "/SOURCE.json", source_manifest)
        retained.put_json(base + "/POLICY.json", policy)
        retained.put_json(base + "/EXPECTED.json", expected)
        retained.put_json(base + "/PACKAGE.json", package)
        retained.put_json(base + "/VALIDATION.json", validation)
        _authority(authority, package)
        retained.put_json(base + "/RECEIPT.json", {"package_sha256": package_digest, "validation_sha256": digest(validation)})
    return {"package_sha256": package_digest, "validation": validation}


def _retained(retained, package_digest):
    from .manifest import hash_value
    hash_value(package_digest)
    base = "retained/" + package_digest
    package = retained.json(base + "/PACKAGE.json")
    validation = retained.json(base + "/VALIDATION.json")
    receipt = retained.json(base + "/RECEIPT.json")
    if digest(package) != package_digest or receipt != {"package_sha256": package_digest, "validation_sha256": digest(validation)}:
        raise EvidenceError("Retained package or validation receipt changed")
    # Evaluate only trusted installed code, never a validator from retained worker files.
    policy = retained.json(base + "/POLICY.json")
    selected_policy(policy, worker_roots=(retained.path,))
    # Use the retained root directly: Root does not follow any symlinked descendant.
    mapped = {**package, "outputs": [{**i, "relative_path": base + "/files/" + digest(i["logical_id"])} for i in package["outputs"]]}
    if retained.inventory(base + "/files") != {digest(i["logical_id"]) for i in package["outputs"]}:
        raise EvidenceError("Retained file inventory differs")
    source = retained.json(base + "/SOURCE.json")
    recomputed = validate_package(mapped, output=retained, source_manifest=source, policy=policy,
                                  expected=retained.json(base + "/EXPECTED.json"))
    # Physical retention paths change only the package hash; all semantic evidence must match.
    recomputed["package_sha256"] = package_digest
    if recomputed != validation or not recomputed["accepted"]:
        raise EvidenceError("Retained validation cannot be reproduced")
    return base, package, validation


def publish(package_digest, *, retained: Root, destination: Root, transaction_id,
            generation, expected_previous, authority, policy: PathPolicy, checkpoint=lambda _stage: None):
    """Read retained bytes only. There is no model execution or fallback path."""
    text(transaction_id)
    integer(generation)
    integer(expected_previous)
    if generation != expected_previous + 1:
        raise EvidenceError("Publication generation must follow its explicit predecessor")
    disjoint(retained.path, destination.path)
    base, package, validation = _retained(retained, package_digest)
    file_plan(package["outputs"], policy, destination.limits)
    transaction = "transactions/" + digest(transaction_id)
    target = transaction + f"/generations/{generation}"
    reservation = {"schema_version": 1, "transaction_sha256": digest(transaction_id), "generation": generation,
                   "expected_previous": expected_previous, "package_sha256": package_digest,
                   "validation_sha256": digest(validation), "path_policy": {"normalization": policy.normalization, "case_sensitive": policy.case_sensitive}}

    def readback():
        if destination.json(target + "/RESERVATION.json") != reservation:
            raise EvidenceError("Destination generation reservation conflicts")
        for item in package["outputs"]:
            destination.snapshot(target + "/files/" + item["package_path"], expected=item)
        if destination.inventory(target + "/files") != {i["package_path"] for i in package["outputs"]}:
            raise EvidenceError("Destination file inventory differs")
        if destination.json(target + "/COMMIT.json") != reservation:
            raise EvidenceError("Destination commit marker conflicts")

    with destination.lock(transaction + "/lock"):
        head = destination.json(transaction + "/HEAD.json") if destination.exists(transaction + "/HEAD.json") else {"generation": 0}
        if head["generation"] not in {expected_previous, generation}:
            raise EvidenceError("Stale publication generation")
        if head["generation"] == generation and head != reservation:
            raise EvidenceError("Current destination generation conflicts")
        if destination.exists(target + "/COMMIT.json"):
            readback()
            # Lost reply after marker but before pointer settlement adopts the exact commit.
            destination.put_json(transaction + "/HEAD.json", reservation, replace=True)
            return {**reservation, "delivery": "verified", "recovered": True}
        if head["generation"] == generation:
            raise EvidenceError("Destination head has no verified commit marker")
        _authority(authority, package)
        destination.put_json(target + "/RESERVATION.json", reservation)
        checkpoint("reserved")
        for item in package["outputs"]:
            _authority(authority, package)
            destination.copy(retained, base + "/files/" + digest(item["logical_id"]), target + "/files/" + item["package_path"], item)
            checkpoint("file_copied")
        for item in package["outputs"]:
            destination.snapshot(target + "/files/" + item["package_path"], expected=item)
        if destination.inventory(target + "/files") != {i["package_path"] for i in package["outputs"]}:
            raise EvidenceError("Destination file inventory differs")
        _authority(authority, package)
        checkpoint("before_marker")
        _authority(authority, package)
        destination.put_json(target + "/COMMIT.json", reservation)
        checkpoint("after_marker")
        readback()
        destination.put_json(transaction + "/HEAD.json", reservation, replace=True)
    return {**reservation, "delivery": "verified", "recovered": False}


def redeliver(*args, **kwargs):
    """The same retained-byte transaction, with zero access to an assign operation."""
    return publish(*args, **kwargs)
