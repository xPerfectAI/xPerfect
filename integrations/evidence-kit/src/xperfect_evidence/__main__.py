"""Finite local commands; runtime controls remain in the existing API and MCP."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import time

from .archive import ReadOnlyAttempts, export_terminal, reconcile_pending
from .client import Client
from .delivery import accept_callback, publish, reconcile_cursor, redeliver, retain, stage_source, upload_source
from .evaluation import case_manifest, run_cases, scorecard
from .files import Root
from .manifest import EvidenceError, PathPolicy, canonical, source_event
from .probe import read_probe
from .validators import validator_identity


def load(path):
    path = Path(path).absolute()
    return Root(path.parent).json(path.name)


def client(args):
    return Client(args.url, token=os.environ.get(args.token_env, ""))


def receiver(args):
    association, state, runtime = load(args.association), Root(args.state), client(args)
    secret = os.environ.get(args.secret_env, "")
    if not secret:
        raise EvidenceError("Receiver secret environment variable is missing")
    accepted = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.connection.settimeout(5)
            try:
                if self.path != "/callback" or self.headers.get("Transfer-Encoding"):
                    raise EvidenceError("Use a bounded callback POST")
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= state.limits.manifest_bytes:
                    raise EvidenceError("Invalid callback size")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise EvidenceError("Incomplete callback bytes")
                receipt = accept_callback(raw, signature=self.headers.get("X-GlassHive-Signature", ""),
                                          secret=secret, association=association, client=runtime, state=state)
                status = 409 if receipt.get("callback_status") in {"conflict", "superseded"} else 202
                body = canonical(receipt)
                if receipt.get("terminal") and receipt.get("accepted"):
                    accepted.append(receipt)
            except (EvidenceError, ValueError, OSError):
                body, status = b'{"accepted":false}\n', 409
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    if not 1 <= args.seconds <= state.limits.seconds:
        raise EvidenceError("Receiver duration is outside command bounds")
    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.timeout = 1
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and not accepted:
            server.handle_request()
    return {"accepted": bool(accepted), "receipts": accepted}


def parser():
    result = argparse.ArgumentParser(description="Optional xPerfect evidence examples")
    commands = result.add_subparsers(dest="command", required=True)

    def add(name, required, *, api=False):
        command = commands.add_parser(name)
        for option in required.split():
            command.add_argument("--" + option, required=True)
        if api:
            command.add_argument("--url", required=True)
            command.add_argument("--token-env", default="XPERFECT_API_TOKEN")
        return command

    add("validator-policy", "")
    add("probe-read", "path case-id probe-run-id")
    check = add("check", "manifest")
    check.add_argument("--kind", choices=["source", "cases"], required=True)
    add("stage", "manifest source state")
    add("upload", "manifest state worker", api=True)
    run = add("evaluate", "manifest inputs evidence state pins probes", api=True)
    run.add_argument("--rerun", type=int, default=0)
    run.add_argument("--maximum", type=int)
    run.add_argument("--database")
    run.add_argument("--tenant")
    run.add_argument("--owner")
    score = add("score", "observations")
    score.add_argument("--elapsed", type=float, required=True)
    score.add_argument("--dispositions")
    archive = add("archive", "manifest source destination database tenant owner")
    archive.add_argument("--case-policy", choices=["sensitive", "insensitive"], required=True)
    archive.add_argument("--normalization", choices=["none", "NFC"], default="NFC")
    add("reconcile", "expected rows hold-sha256")
    cursor = add("cursor", "scope prior")
    cursor.add_argument("--initial-since", type=int, required=True)
    keep = add("retain", "package source-manifest output retained policy", api=True)
    keep.add_argument("--expected")
    for name in ("publish", "redeliver"):
        publication = add(name, "package-sha256 retained destination transaction", api=True)
        publication.add_argument("--generation", type=int, required=True)
        publication.add_argument("--previous", type=int, required=True)
        publication.add_argument("--normalization", choices=["none", "NFC"], default="NFC")
        publication.add_argument("--case-policy", choices=["sensitive", "insensitive"], required=True)
    receive = add("receive", "association state", api=True)
    receive.add_argument("--secret-env", default="XPERFECT_CALLBACK_SECRET")
    receive.add_argument("--port", type=int, default=8791)
    receive.add_argument("--seconds", type=int, default=120)
    return result


def execute(args):
    if args.command == "validator-policy":
        return {**validator_identity(), "require_source_review": True}
    if args.command == "probe-read":
        return read_probe(args.path, case_id=args.case_id, probe_run_id=args.probe_run_id)
    if args.command == "check":
        manifest = load(args.manifest)
        (source_event if args.kind == "source" else case_manifest)(manifest)
        return {"valid": True}
    if args.command == "stage":
        return stage_source(load(args.manifest), source=Root(args.source), state=Root(args.state))
    if args.command == "upload":
        return upload_source(load(args.manifest), state=Root(args.state), client=client(args), worker_id=args.worker)
    if args.command == "evaluate":
        if any((args.database, args.tenant, args.owner)) and not all((args.database, args.tenant, args.owner)):
            raise EvidenceError("Local attempt collection needs database, tenant and owner")
        collector = ReadOnlyAttempts(args.database, tenant_id=args.tenant, owner_id=args.owner) if args.database else None
        return run_cases(load(args.manifest), inputs=Root(args.inputs), evidence=Root(args.evidence), state=Root(args.state),
                         client=client(args), observed_pins=load(args.pins), probes=load(args.probes), rerun=args.rerun, maximum=args.maximum,
                         attempt_reader=collector.attempts if collector else None)
    if args.command == "score":
        return scorecard(load(args.observations), batch_elapsed_seconds=args.elapsed,
                         dispositions=load(args.dispositions) if args.dispositions else ())
    if args.command == "archive":
        return export_terminal(load(args.manifest), source=Root(args.source), destination=Root(args.destination),
                               lookup=ReadOnlyAttempts(args.database, tenant_id=args.tenant, owner_id=args.owner),
                               policy=PathPolicy(args.normalization, args.case_policy == "sensitive"))
    if args.command == "reconcile":
        return reconcile_pending(load(args.expected), load(args.rows), hold_set_sha256=args.hold_sha256)
    if args.command == "cursor":
        return reconcile_cursor(scope=args.scope, prior=load(args.prior), initial_since=args.initial_since)
    if args.command == "retain":
        return retain(load(args.package), output=Root(args.output), retained=Root(args.retained),
                      source_manifest=load(args.source_manifest), policy=load(args.policy),
                      expected=load(args.expected) if args.expected else None, authority=client(args).run)
    if args.command in {"publish", "redeliver"}:
        operation = publish if args.command == "publish" else redeliver
        return operation(args.package_sha256, retained=Root(args.retained), destination=Root(args.destination),
                         transaction_id=args.transaction, generation=args.generation, expected_previous=args.previous,
                         authority=client(args).run, policy=PathPolicy(args.normalization, args.case_policy == "sensitive"))
    if args.command == "receive":
        return receiver(args)
    raise EvidenceError("Unknown command")


def main(argv=None):
    try:
        result = execute(parser().parse_args(argv))
        sys.stdout.buffer.write(canonical(result))
        return 0
    except (EvidenceError, OSError, KeyError, TypeError, UnicodeError) as exc:
        # No exception strings: an OS/HTTP error can contain private paths or URLs.
        sys.stderr.write(json.dumps({"state": "blocked", "failure_type": type(exc).__name__}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
