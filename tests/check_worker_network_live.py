"""Opt-in operator acceptance on a disposable Linux Docker host.

No provider credentials or application data are used. This checks the selected
bridge, not a cloud-wide metadata policy. Run as root on the actual Docker host.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("network_policy", ROOT / "deployment/linux/network_policy.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-image", required=True)
    parser.add_argument("--docker-host", default="unix:///var/run/docker.sock")
    args = parser.parse_args()
    guard.require_operator()

    def docker(arguments):
        return guard.run(["docker", "--host", args.docker_host, *arguments]).stdout.strip()

    # Both targets must be reachable first. A failed public endpoint is a failed
    # prerequisite, never evidence that a firewall blocked it.
    denied = sorted({item[4][0] for item in socket.getaddrinfo("example.com", 443, socket.AF_INET, socket.SOCK_STREAM)})[0]
    policy = guard.contract.normalize_policy({
        "denied": [{"host": denied, "port": 443}],
        "positive": [{"host": "api.openai.com", "port": 443}],
        "timeout_seconds": 2, "max_age_seconds": 60,
    })
    network = "xperfect-check-" + os.urandom(6).hex()
    unrelated = network + "-control"
    identities = []
    applied = False
    original_environment = {key: os.environ.get(key) for key in
                            (guard.contract.POLICY_ENV, "XPERFECT_CONTROL_ROOT")}

    def inspect(name):
        return json.loads(docker(["network", "inspect", name]))[0]

    def observed(name):
        output = docker(["run", "--rm", "--network", name, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "65534:65534",
            "--memory", "64m", "--pids-limit", "32", "--entrypoint", "/usr/bin/python3",
            args.native_image, "-I", "-c", guard.contract.PROBE_PROGRAM, json.dumps(policy)])
        return json.loads(output)["results"]

    try:
        for name in (network, unrelated):
            identities.append(docker(["network", "create", "--driver", "bridge",
                "--label", "xperfect.package=" + network,
                "--opt", "com.docker.network.bridge.enable_icc=false", name]))
        before = observed(network)
        assert all(row["status"] == "reachable" for row in before), "Targets were not reachable before enforcement"
        assert all(row["status"] == "reachable" for row in observed(unrelated)), "Control bridge is unavailable"
        record = inspect(network)
        receipt = guard.attest(docker=docker, network=network, image=args.native_image, policy=policy, apply=True)
        applied = True
        assert receipt["enforcement"] == "iptables_readback"
        assert [row["status"] for row in receipt["results"]] == ["reachable", "refused"]
        assert all(row["status"] == "reachable" for row in observed(unrelated)), "An unrelated bridge changed"
        assert guard.attest(docker=docker, network=network, image=args.native_image, policy=policy)["rules_sha256"] == receipt["rules_sha256"]
        with tempfile.TemporaryDirectory(prefix="xperfect-network-proof-") as folder:
            control = Path(folder)
            guard._write_receipt(control / guard.contract.RECEIPT_NAME, receipt)
            os.environ[guard.contract.POLICY_ENV] = json.dumps(policy)
            os.environ["XPERFECT_CONTROL_ROOT"] = str(control)
            guard.contract.require_current_receipt(network=network, docker=docker)
            for name, now in ((network, receipt["observed_at"] + 61), (unrelated, None)):
                try:
                    guard.contract.require_current_receipt(network=name, docker=docker, now=now)
                except guard.contract.WorkerEgressUnavailable:
                    pass
                else:
                    raise AssertionError("Stale or foreign-generation proof was accepted")
        guard.remove_rules(record, policy)
        applied = False
        assert all(row["status"] == "reachable" for row in observed(network)), "Removing the owned policy did not restore traffic"
        print(json.dumps({"result": "PASS", "enforcement": receipt["enforcement"],
            "native_image": args.native_image,
            "network_generation_sha256": hashlib.sha256(receipt["network_id"].encode()).hexdigest(),
            "checks": ["reachable-before", "exact-rules", "denied-after", "positive-after",
                       "unrelated-bridge", "repeat-readback", "stale-refusal", "generation-refusal", "remove-readback"]}))
    finally:
        for key, value in original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if applied:
            guard.remove_rules(inspect(network), policy)
        for identity in reversed(identities):
            docker(["network", "rm", identity])


if __name__ == "__main__":
    main()
