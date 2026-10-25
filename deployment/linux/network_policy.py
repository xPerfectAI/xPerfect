"""Explicit operator control for one package bridge. Never run implicitly."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


def _contract():
    source = Path(__file__).resolve().parents[2] / "runtime_phase1/src/workers_projects_runtime/worker_egress.py"
    spec = importlib.util.spec_from_file_location("xperfect_worker_egress", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = _contract()


def run(arguments, *, check=True):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError("Worker bridge enforcement could not be verified; check the Docker iptables backend and operator permissions")
    return result


def require_operator():
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        raise ValueError("Apply this policy on the rootful Linux Docker host as its administrator")


def rules_for(record, policy):
    policy = contract.normalize_policy(policy)
    identifier, subnet, bridge = contract.network_identity(record)
    chain = "XPF_" + identifier[:16]
    rules = []
    for target in policy["denied"]:
        for exception in target["exceptions"]:
            protocol = exception["protocol"]
            # The exception skips our address reject, never Docker's later
            # forwarding/ICC policy or another operator's rules.
            rules.append(["-d", target["host"] + "/32", "-p", protocol, "-m", protocol,
                          "--dport", str(exception["port"]), "-j", "RETURN"])
        rules.append(["-d", target["host"] + "/32", "-j", "REJECT", "--reject-with", "icmp-admin-prohibited"])
    rules.append(["-j", "RETURN"])
    return chain, ["-i", bridge, "-s", subnet, "-j", chain], rules


def verify_rules(record, policy, *, execute=run):
    chain, jump, rules = rules_for(record, policy)
    execute(["iptables", "-w", "5", "-C", "DOCKER-USER", *jump])
    dispatch = [shlex.split(line) for line in execute(["iptables", "-w", "5", "-S", "DOCKER-USER"]).stdout.splitlines() if line.strip()]
    dispatch = [line for line in dispatch if line[:2] == ["-A", "DOCKER-USER"]]
    expected_jump = ["-A", "DOCKER-USER", *jump]
    if expected_jump not in dispatch:
        raise RuntimeError("Worker bridge dispatch is missing")
    bridge = jump[1]
    for preceding in dispatch[:dispatch.index(expected_jump)]:
        # Another package may have its own restrictive jump. A positive literal
        # input-interface match for a different bridge cannot bypass this one.
        interface = preceding[preceding.index("-i") + 1] if "-i" in preceding else ""
        if ("!" in preceding or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface)
                or interface == bridge):
            raise RuntimeError("Worker bridge rule can be bypassed; no foreign firewall rule was modified")
    output = execute(["iptables", "-w", "5", "-S", chain]).stdout
    lines = [shlex.split(line) for line in output.splitlines() if line.strip()]
    expected = [["-N", chain], *[["-A", chain, *rule] for rule in rules]]
    if lines != expected:
        raise RuntimeError("Worker bridge rules changed; no unrecognized rule was modified")
    for rule in rules:
        execute(["iptables", "-w", "5", "-C", chain, *rule])
    return hashlib.sha256(output.encode()).hexdigest()


def apply_rules(record, policy, *, execute=run):
    """Create only a network-ID-scoped chain and one restrictive jump."""
    chain, jump, rules = rules_for(record, policy)
    existing = execute(["iptables", "-w", "5", "-S", chain], check=False)
    if existing.returncode == 0:
        return verify_rules(record, policy, execute=execute)
    execute(["iptables", "-w", "5", "-S", "DOCKER-USER"])
    created, linked = [], False
    execute(["iptables", "-w", "5", "-N", chain])
    try:
        for rule in rules:
            execute(["iptables", "-w", "5", "-A", chain, *rule])
            created.append(rule)
        execute(["iptables", "-w", "5", "-I", "DOCKER-USER", "1", *jump])
        linked = True
        return verify_rules(record, policy, execute=execute)
    except BaseException:
        if linked:
            execute(["iptables", "-w", "5", "-D", "DOCKER-USER", *jump], check=False)
        for rule in reversed(created):
            execute(["iptables", "-w", "5", "-D", chain, *rule], check=False)
        # Refuses removal if someone added a rule; no global flush or restore.
        execute(["iptables", "-w", "5", "-X", chain], check=False)
        raise


def remove_rules(record, policy, *, execute=run):
    chain, jump, rules = rules_for(record, policy)
    if execute(["iptables", "-w", "5", "-S", chain], check=False).returncode:
        execute(["iptables", "-w", "5", "-S", "DOCKER-USER"])
        if execute(["iptables", "-w", "5", "-C", "DOCKER-USER", *jump], check=False).returncode == 0:
            raise RuntimeError("Worker bridge dispatch remains; no unrecognized rule was modified")
        return  # A prior exact removal succeeded but proof publication may need retry.
    verify_rules(record, policy, execute=execute)
    execute(["iptables", "-w", "5", "-D", "DOCKER-USER", *jump])
    for rule in reversed(rules):
        execute(["iptables", "-w", "5", "-D", chain, *rule])
    execute(["iptables", "-w", "5", "-X", chain])


def attest(*, docker, network, image, policy, apply=False, execute=run):
    require_operator()
    policy = contract.normalize_policy(policy)
    record = json.loads(docker(["network", "inspect", network]))[0]
    labels = record.get("Labels") or {}
    if not re.fullmatch(r"xperfect-[a-z0-9][a-z0-9-]{0,40}", str(labels.get("xperfect.package", ""))):
        raise ValueError("Select an xPerfect-managed worker bridge")
    _, subnet, bridge = contract.network_identity(record)
    interfaces = json.loads(execute(["ip", "-j", "-4", "address", "show", "dev", bridge]).stdout)
    import ipaddress
    if not any(ipaddress.IPv4Address(address["local"]) in ipaddress.IPv4Network(subnet)
               for interface in interfaces for address in interface.get("addr_info", [])
               if address.get("family") == "inet"):
        raise ValueError("Run enforcement on the Docker host that owns this exact bridge")
    chain, _, _ = rules_for(record, policy)
    existed = execute(["iptables", "-w", "5", "-S", chain], check=False).returncode == 0
    rules_hash = (apply_rules if apply else verify_rules)(record, policy, execute=execute)
    try:
        behavior = contract.probe_network(docker, network=network, image=image, policy=policy)
        final_network = json.loads(docker(["network", "inspect", network]))[0]
        if behavior["network_id"] != record["Id"] or contract.network_identity(final_network) != contract.network_identity(record):
            raise RuntimeError("Worker bridge generation changed during its probe")
        # Read the same exact rules after the probe; concurrent changes fail closed.
        if verify_rules(record, policy, execute=execute) != rules_hash:
            raise RuntimeError("Worker bridge enforcement changed during its probe")
    except BaseException:
        if apply and not existed:
            try:
                remove_rules(record, policy, execute=execute)
            except RuntimeError:
                pass  # Changed rules are retained; never flush an unknown rule.
        raise
    return {"version": 1, "enforcement": "iptables_readback", **behavior,
            "rules_sha256": rules_hash, "observed_at": time.time()}


def _write_receipt(path: Path, receipt):
    path = path.absolute()
    if path.parent.resolve(strict=True) != path.parent or path.is_symlink():
        raise ValueError("Use a real private operator directory for the receipt")
    temporary = path.with_name(path.name + ".pending-" + os.urandom(8).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Apply, verify or remove policy for one managed worker bridge")
    parser.add_argument("action", choices=("apply", "verify", "remove"))
    parser.add_argument("--network", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--image", required=True, help="Exact native image ID")
    parser.add_argument("--receipt", type=Path, required=True, help="Private operator proof file")
    parser.add_argument("--docker-host", default="unix:///var/run/docker.sock")
    parser.add_argument("--runtime-container", help="Exact package runtime, required for apply/verify; optional for removing an empty bridge")
    args = parser.parse_args()
    require_operator()
    if not args.docker_host.startswith("unix:///"):
        raise ValueError("Use the local Docker host's Unix socket")
    policy = contract.normalize_policy(json.loads(args.policy.read_text()))
    def docker(arguments):
        return run(["docker", "--host", args.docker_host, *arguments]).stdout.strip()
    record = json.loads(docker(["network", "inspect", args.network]))[0]
    if not re.fullmatch(r"xperfect-[a-z0-9][a-z0-9-]{0,40}", str((record.get("Labels") or {}).get("xperfect.package", ""))):
        raise ValueError("Select an xPerfect-managed worker bridge")
    contract.network_identity(record)
    if args.action == "remove":
        runtime = None
        if args.runtime_container:
            runtime = json.loads(docker(["inspect", args.runtime_container]))[0]
            labels = (runtime.get("Config") or {}).get("Labels") or {}
            if (labels.get("xperfect.package") != (record.get("Labels") or {}).get("xperfect.package")
                    or labels.get("xperfect.role") != "runtime" or runtime.get("State", {}).get("Running")):
                raise ValueError("Stop this exact package runtime before removing its network policy")
        if record.get("Containers") or {}:
            raise ValueError("Stop this package and its worker containers before removing its network policy")
        invalid = {"version": 1, "enforcement": "removing", "observed_at": 0}
        _write_receipt(args.receipt, invalid)
        if runtime is not None:
            docker(["cp", str(args.receipt), runtime["Id"] + ":/control/" + contract.RECEIPT_NAME])
        remove_rules(record, policy)
        invalid["enforcement"] = "removed"
        _write_receipt(args.receipt, invalid)
        if runtime is not None:
            docker(["cp", str(args.receipt), runtime["Id"] + ":/control/" + contract.RECEIPT_NAME])
        print("Invalidated the proof and removed only the stopped worker bridge policy.")
        return
    if not args.runtime_container:
        raise ValueError("Name the existing package runtime to apply or verify its worker network policy")
    target = json.loads(docker(["inspect", args.runtime_container]))[0]
    labels = (target.get("Config") or {}).get("Labels") or {}
    connected = (target.get("NetworkSettings") or {}).get("Networks") or {}
    expected_package = (record.get("Labels") or {}).get("xperfect.package")
    if (labels.get("xperfect.package") != expected_package or labels.get("xperfect.role") != "runtime"
            or connected.get(args.network, {}).get("NetworkID") != record["Id"]):
        raise ValueError("The runtime does not belong to this exact package bridge")
    chain, _, _ = rules_for(record, policy)
    if (args.action == "apply"
            and run(["iptables", "-w", "5", "-S", chain], check=False).returncode):
        # Revoke a prior removal acknowledgment before installing any new rule.
        # Verifying an already installed chain keeps its current proof available.
        _write_receipt(args.receipt, {"version": 1, "enforcement": "applying", "observed_at": 0})
        docker(["cp", str(args.receipt), target["Id"] + ":/control/" + contract.RECEIPT_NAME])
    receipt = attest(docker=docker, network=args.network, image=args.image, policy=policy, apply=args.action == "apply")
    _write_receipt(args.receipt, receipt)
    docker(["cp", str(args.receipt), target["Id"] + ":/control/" + contract.RECEIPT_NAME])
    print("Worker bridge rules and live positive/denied checks verified.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        print("xPerfect network policy: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
