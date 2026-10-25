"""One opt-in native API smoke example on an existing disposable worker.

This submits one model job. It never creates workers, chooses models or repairs output.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import time
from urllib.parse import urlencode
import uuid

from prepare import prepare
from xperfect_evidence.client import Client, segment
from xperfect_evidence.delivery import publish, redeliver, retain, stage_source, upload_source
from xperfect_evidence.evaluation import TERMINAL
from xperfect_evidence.files import Root
from xperfect_evidence.manifest import EvidenceError, Limits, PathPolicy, canonical, digest, sha


def exercise(args):
    if not 1 <= args.seconds <= 3600:
        raise EvidenceError("Choose a finite 1–3600 second deadline")
    limits = Limits(seconds=args.seconds)
    path = Path(args.directory).absolute().resolve(strict=False)
    prepare(path, event_id="synthetic-" + uuid.uuid4().hex)
    root = Root(path, limits)
    runtime = Client(args.url, token=os.environ.get(args.token_env, ""), limits=limits)
    worker = runtime.worker(args.worker)
    if worker.get("state") != "ready":
        raise EvidenceError("Use a ready disposable worker with its account and exact model configured")
    event = root.json("source-event.json")
    source, state = Root(path / "source", limits), Root(path / "state", limits)
    stage_source(event, source=source, state=state)
    admitted = upload_source(event, state=state, client=runtime, worker_id=args.worker)
    bound_path = admitted["binding"]["items"][0]["path"]
    task = (f"Read the admitted file at {bound_path}. Copy its exact bytes to result.txt. "
            "Return a Markdown link to result.txt. Do not change the input or contact external destinations.")
    run = runtime.assign(args.worker, task, args.effort, digest({"source": digest(event), "kind": "copy"}))
    deadline = time.monotonic() + args.seconds
    while run["state"] not in TERMINAL:
        if time.monotonic() >= deadline:
            raise EvidenceError("Native work remains pending; inspect the same saved run")
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        run = runtime.run(run["run_id"])
    if run["state"] != "completed" or not run.get("active_attempt_id"):
        raise EvidenceError("Native work did not complete; preserve its exact failure")
    content = runtime.request("GET", f"/v1/workers/{segment(args.worker)}/artifacts/download?" + urlencode({"path": "result.txt"}), json_result=False)
    if sha(content) != event["inputs"][0]["sha256"]:
        raise EvidenceError("Native result contradicts the independently pinned source")
    output = Root(path / "output", limits)
    output.put("result.txt", content)
    package = root.json("package.json")
    package.update(run_id=run["run_id"], attempt_id=run["active_attempt_id"])
    root.put_json("package.json", package, replace=True)
    evaluator = Root(path / "evaluator", limits)
    retained, destination = Root(path / "retained", limits), Root(path / "destination", limits)
    kept = retain(package, output=output, retained=retained, source_manifest=event, policy=evaluator.json("validator-policy.json"),
                  expected=evaluator.json("expected.json"), authority=runtime.run)
    def lose_reply(stage):
        if stage == "after_marker":
            raise RuntimeError("synthetic lost commit reply")
    before = runtime.request("GET", f"/v1/workers/{segment(args.worker)}/runs")
    path_policy = PathPolicy("NFC", args.case_policy == "sensitive")
    try:
        publish(kept["package_sha256"], retained=retained, destination=destination, transaction_id=event["event_id"],
                generation=1, expected_previous=0, authority=runtime.run, policy=path_policy, checkpoint=lose_reply)
    except RuntimeError:
        pass
    delivered = redeliver(kept["package_sha256"], retained=retained, destination=destination, transaction_id=event["event_id"],
                          generation=1, expected_previous=0, authority=runtime.run, policy=path_policy)
    after = runtime.request("GET", f"/v1/workers/{segment(args.worker)}/runs")
    identities = lambda value: sorted((i["run_id"], i.get("active_attempt_id")) for i in value["items"])
    if identities(before) != identities(after):
        raise EvidenceError("Runtime identities changed during delivery-only recovery")
    result = {"run_id": run["run_id"], "attempt_id": run["active_attempt_id"], "native_state": run["state"],
              "source_manifest_sha256": digest(event), "package_sha256": kept["package_sha256"],
              "source_review": kept["validation"]["source_review"], "delivery": delivered["delivery"],
              "recovered_commit": delivered["recovered"], "same_run_attempt_identities": True,
              "configured_profile": worker["profile"], "configured_model": worker["model"]}
    root.put_json("LIVE-RECEIPT.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--token-env", default="XPERFECT_API_TOKEN")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--case-policy", choices=["sensitive", "insensitive"], required=True)
    args = parser.parse_args()
    try:
        print(canonical(exercise(args)).decode(), end="")
    except (EvidenceError, OSError, KeyError, TypeError) as exc:
        print(canonical({"state": "blocked", "failure_type": type(exc).__name__}).decode(), end="")
        raise SystemExit(2)
