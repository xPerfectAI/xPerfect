"""Opt-in operator acceptance on a disposable Linux Docker host.

No provider credentials or application data are used. This checks the selected
bridge, not a cloud-wide metadata policy. Run as root on the actual Docker host.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("network_policy", ROOT / "deployment/linux/network_policy.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def check_package(receipt_path, policy_path, docker):
    """Use a disposable package created by the normal unprivileged launcher."""
    receipt = json.loads(receipt_path.read_text())
    runtime = receipt["containers"]["runtime"]
    network = receipt["networks"]["workers"]
    image = receipt["native_image"]
    package = network.removesuffix("-workers")
    if not package.startswith("xperfect-qa-"):
        raise ValueError("Package acceptance requires an explicitly disposable xperfect-qa-* package")
    for identity in receipt["containers"].values():
        record = json.loads(docker(["inspect", identity]))[0]
        if (record.get("Config", {}).get("Labels") or {}).get("xperfect.package") != package:
            raise ValueError("Disposable package identities do not match")
    policy = guard.contract.normalize_policy(json.loads(policy_path.read_text()))
    program = '''import importlib.util,os,subprocess,sys
from pathlib import Path
s=importlib.util.spec_from_file_location("service","/opt/xperfect/deployment/linux/service.py")
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
os.environ.update(m.load_environment(Path("/control/config.json"),"runtime"))
from workers_projects_runtime.worker_egress import WorkerEgressUnavailable
from workers_projects_runtime.workspace_box import WorkspaceBox,WorkspaceMemberBinding
box=WorkspaceBox(volume_root=Path(os.environ["XPERFECT_SHARED_VOLUME_ROOT"]),
 volume_name=os.environ["XPERFECT_SHARED_VOLUME_NAME"],image=os.environ["XPERFECT_SHARED_IMAGE"],
 binding=WorkspaceMemberBinding("wsp_egress_acceptance","wrk_egress_acceptance","local","acceptance",20001),
 memory_bytes=268435456,pids_limit=64,
 control_root=Path(os.environ["XPERFECT_CONTROL_ROOT"])/"workspaces",
 network=os.environ["XPERFECT_SHARED_NETWORK"])
try:
 command=box.command(["/bin/true"])
 result=subprocess.run(command,capture_output=True,text=True,timeout=45)
 if result.returncode:raise RuntimeError("Product workspace command failed")
 # The normal product owner, not a reconstructed docker-run approximation,
 # created and verified this exact native member container and command.
 box.release_if_sole_member(box._inspect()["Id"])
except WorkerEgressUnavailable:sys.exit(42)
'''

    def native(expected):
        result = guard.run(["docker", "--host", args_endpoint,
            "exec", runtime, "/opt/xperfect/venvs/runtime/bin/python", "-I", "-c", program], check=False)
        assert result.returncode == expected, "Installed native admission did not match the expected proof state"

    def healthy():
        deadline = time.monotonic() + 45
        while True:
            try:
                with urllib.request.urlopen(receipt["ui_url"] + "/health", timeout=3) as response:
                    assert response.status == 200
                docker(["exec", runtime, "/opt/xperfect/venvs/runtime/bin/python", "-I", "-c",
                    "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8766/health',timeout=3).status==200"])
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)

    # The CLI's selected Unix endpoint is passed explicitly; no ambient context
    # or provider credentials enter the native probe.
    args_endpoint = docker.endpoint
    healthy()
    native(42)
    with tempfile.TemporaryDirectory(prefix="xperfect-package-policy-") as folder:
        proof = Path(folder) / "proof.json"
        command = [sys.executable, str(ROOT / "deployment/linux/network_policy.py"), "apply",
            "--network", network, "--image", image, "--policy", str(policy_path),
            "--receipt", str(proof), "--docker-host", args_endpoint, "--runtime-container", runtime]
        guard.run(command)
        native(0)
        stale = json.loads(proof.read_text())
        stale["observed_at"] -= policy["max_age_seconds"] + 1
        guard._write_receipt(proof, stale)
        docker(["cp", str(proof), runtime + ":/control/" + guard.contract.RECEIPT_NAME])
        native(42)
        docker(["restart", runtime])
        healthy()
        native(42)
        # Model a host/firewall restart without affecting another package:
        # remove only this exact chain while its proof is already invalid.
        guard.remove_rules(json.loads(docker(["network", "inspect", network]))[0], policy)
        guard.run(command)
        native(0)
        for role in ("mcp", "ui", "runtime"):
            docker(["stop", receipt["containers"][role]])
        docker(["network", "disconnect", network, runtime])
        command[2] = "remove"
        guard.run(command)
    print(json.dumps({"result": "PASS", "package_checks": ["healthy-without-proof", "native-refused",
        "operator-apply", "product-workspace-command", "stale-refused", "healthy-restart-with-stale-proof",
        "rules-reinstalled", "native-recovered", "stopped-policy-removal"]}))


def check_peer_exception_isolation(docker, *, network, image, positive):
    """A scoped exception must still traverse Docker's ICC=false peer denial."""
    identity = docker(["network", "create", "--driver", "bridge", "--label", "xperfect.package=" + network,
                       "--opt", "com.docker.network.bridge.enable_icc=false", network])
    peer = None
    applied = False
    removed = True
    listener_port = 18080
    try:
        # The target is disposable and unprivileged. A loopback control proves
        # it really listens even though another bridge member cannot reach it.
        listener = '''import signal,socket
signal.alarm(90)
with socket.socket() as s:
 s.bind(('0.0.0.0',18080));s.listen()
 while True:
  c,_=s.accept();c.close()
'''
        peer = docker(["run", "-d", "--network", network, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "65534:65534", "--memory", "64m",
            "--pids-limit", "32", "--entrypoint", "/usr/bin/python3", image, "-I", "-c", listener])
        ready = '''import socket,time
deadline=time.monotonic()+5
while True:
 try:
  with socket.create_connection(('127.0.0.1',18080),timeout=.2):pass
  break
 except OSError:
  if time.monotonic()>=deadline:raise
  time.sleep(.05)
'''
        docker(["exec", "--user", "65534:65534", peer, "/usr/bin/python3", "-I", "-c", ready])
        peer_record = json.loads(docker(["inspect", peer]))[0]
        address = peer_record["NetworkSettings"]["Networks"][network]["IPAddress"]
        policy = guard.contract.normalize_policy({
            "denied": [{"host": address, "probe_port": listener_port + 1,
                        "exceptions": [{"protocol": "tcp", "port": listener_port}]}],
            "positive": positive, "timeout_seconds": 1, "max_age_seconds": 60})
        checked = guard.contract.normalize_policy({**policy,
            "positive": [*positive, {"host": address, "port": listener_port}]})
        def peer_is_blocked():
            output = docker(["run", "--rm", "--network", network, "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "65534:65534", "--memory", "64m",
                "--pids-limit", "32", "--entrypoint", "/usr/bin/python3", image, "-I", "-c",
                guard.contract.PROBE_PROGRAM, json.dumps(checked)])
            results = json.loads(output)["results"]
            target = next(row for row in results if row["kind"] == "positive" and row["host"] == address)
            assert target["status"] != "reachable", "An exact exception bypassed Docker worker isolation"
            assert all(row["status"] == "reachable" for row in results
                       if row["kind"] == "positive" and row["host"] != address), "External positive control failed"
        peer_is_blocked()
        receipt = guard.attest(docker=docker, network=network, image=image, policy=policy, apply=True)
        applied = True
        peer_is_blocked()
        # Exclude a dead listener as the cause of peer refusal after enforcement.
        docker(["exec", "--user", "65534:65534", peer, "/usr/bin/python3", "-I", "-c", ready])
        assert receipt["results"][-1]["errno"] == guard.contract.LINUX_REJECT_ERRNO
    finally:
        try:
            if applied:
                removed = False
                guard.remove_rules(json.loads(docker(["network", "inspect", network]))[0], policy)
                removed = True
        finally:
            if peer:
                docker(["rm", "-f", peer])
            if removed:
                docker(["network", "rm", identity])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-image", required=True)
    parser.add_argument("--docker-host", default="unix:///var/run/docker.sock")
    parser.add_argument("--package-receipt", type=Path)
    parser.add_argument("--package-policy", type=Path)
    args = parser.parse_args()
    guard.require_operator()

    def docker(arguments):
        return guard.run(["docker", "--host", args.docker_host, *arguments]).stdout.strip()
    docker.endpoint = args.docker_host
    if bool(args.package_receipt) != bool(args.package_policy):
        raise ValueError("Package acceptance needs both its receipt and policy")

    # Both targets must be reachable first. A failed public endpoint is a failed
    # prerequisite, never evidence that a firewall blocked it.
    denied = sorted({item[4][0] for item in socket.getaddrinfo("example.com", 443, socket.AF_INET, socket.SOCK_STREAM)})[0]
    policy = guard.contract.normalize_policy({
        "denied": [{"host": denied, "probe_port": 80}],
        "positive": [{"host": "example.org", "port": 443}],
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

    def observed(name, checked_policy=None):
        output = docker(["run", "--rm", "--network", name, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "65534:65534",
            "--memory", "64m", "--pids-limit", "32", "--entrypoint", "/usr/bin/python3",
            args.native_image, "-I", "-c", guard.contract.PROBE_PROGRAM, json.dumps(checked_policy or policy)])
        return json.loads(output)["results"]

    try:
        for name in (network, unrelated):
            identities.append(docker(["network", "create", "--driver", "bridge",
                "--label", "xperfect.package=" + network,
                "--opt", "com.docker.network.bridge.enable_icc=false", name]))
        before = observed(network)
        assert all(row["status"] == "reachable" for row in before), "Targets were not reachable before enforcement"
        second_port = guard.contract.normalize_policy({**policy, "denied": [{"host": denied, "probe_port": 443}]})
        assert all(row["status"] == "reachable" for row in observed(network, second_port)), "Second TCP port was not reachable before enforcement"
        assert all(row["status"] == "reachable" for row in observed(unrelated)), "Control bridge is unavailable"
        record = inspect(network)
        receipt = guard.attest(docker=docker, network=network, image=args.native_image, policy=policy, apply=True)
        applied = True
        assert receipt["enforcement"] == "iptables_readback"
        assert [row["status"] for row in receipt["results"]] == ["reachable", "refused"]
        assert receipt["results"][-1]["errno"] == errno.EHOSTUNREACH, "Kernel REJECT signature was not qualified"
        assert observed(network, second_port)[-1]["errno"] == errno.EHOSTUNREACH, "A second port bypassed the address denial"
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
        policy = guard.contract.normalize_policy({**policy, "denied": [{"host": denied, "probe_port": 80,
            "exceptions": [{"protocol": "tcp", "port": 443}]}],
            "positive": [*policy["positive"], {"host": denied, "port": 443}]})
        narrowed = guard.attest(docker=docker, network=network, image=args.native_image, policy=policy, apply=True)
        applied = True
        assert narrowed["results"][-1]["errno"] == errno.EHOSTUNREACH
        assert all(row["status"] == "reachable" for row in narrowed["results"][:-1]), "An exact TCP exception was not reachable"
        guard.remove_rules(record, policy)
        applied = False
        check_peer_exception_isolation(docker, network=network + "-peer", image=args.native_image,
                                       positive=[{"host": "example.org", "port": 443}])
        print(json.dumps({"result": "PASS", "enforcement": receipt["enforcement"],
            "native_image": args.native_image,
            "network_generation_sha256": hashlib.sha256(receipt["network_id"].encode()).hexdigest(),
            "reject_errno": errno.EHOSTUNREACH,
            "checks": ["reachable-before", "exact-rules", "address-denial-two-tcp-ports", "narrow-exception",
                       "exception-keeps-worker-isolation", "peer-listener-positive-control", "positive-after",
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
    if args.package_receipt:
        check_package(args.package_receipt, args.package_policy, docker)


if __name__ == "__main__":
    main()
