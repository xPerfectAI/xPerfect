"""Opt-in, generation-bound proof for an operator-managed worker bridge.

The deployment owns firewall rules. Workers cannot write the control-volume
receipt. Ordinary installations perform no extra network operation.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import time


POLICY_ENV = "XPERFECT_WORKER_EGRESS_POLICY"
RECEIPT_NAME = "worker-egress.json"


class WorkerEgressUnavailable(RuntimeError):
    """A configured bridge has no current, matching enforcement proof."""


def normalize_policy(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"denied", "positive", "max_age_seconds", "timeout_seconds"}:
        raise ValueError("Worker egress policy must contain denied and positive TCP targets")
    normalized = {}
    for kind in ("denied", "positive"):
        entries = value.get(kind)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
            raise ValueError("Give between one and sixteen targets for each worker egress check")
        clean = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"host", "port"}:
                raise ValueError("Each worker egress target needs only host and port")
            host, port = entry["host"], entry["port"]
            if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Worker egress target host or TCP port is invalid")
            try:
                address = ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                if kind == "denied" or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
                    raise ValueError("Denied targets need IPv4 addresses; positive controls may use DNS names") from None
                host = host.lower()
            else:
                if address.is_unspecified or address.is_multicast or address.is_loopback:
                    raise ValueError("Worker egress targets must be routable from the worker bridge")
                host = str(address)
            if {"host": host, "port": port} in clean:
                raise ValueError("Worker egress targets must be distinct")
            clean.append({"host": host, "port": port})
        normalized[kind] = sorted(clean, key=lambda item: (item["host"], item["port"]))
    if any(target in normalized["positive"] for target in normalized["denied"]):
        raise ValueError("A denied target cannot also be a positive control")
    age = value.get("max_age_seconds", 300)
    timeout = value.get("timeout_seconds", 1)
    if type(age) is not int or not 5 <= age <= 3600:
        raise ValueError("Worker egress proof lifetime must be between 5 and 3600 seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 5:
        raise ValueError("Worker egress probe timeout must be between 0.1 and 5 seconds")
    if timeout * sum(len(normalized[kind]) for kind in ("positive", "denied")) > 20:
        raise ValueError("Worker egress target timeouts must fit within a twenty-second probe")
    normalized.update(max_age_seconds=age, timeout_seconds=float(timeout))
    return normalized


def policy_digest(policy: dict) -> str:
    return hashlib.sha256(json.dumps(normalize_policy(policy), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def configured_policy() -> dict | None:
    raw = os.environ.get(POLICY_ENV, "").strip()
    if not raw:
        return None
    try:
        return normalize_policy(json.loads(raw))
    except (ValueError, TypeError):
        raise WorkerEgressUnavailable("The configured worker egress policy is invalid") from None


def network_identity(record: dict) -> tuple[str, str, str]:
    """Accept only the package's IPv4 bridge; never infer IPv6 coverage."""
    identifier = record.get("Id", "")
    options = record.get("Options") or {}
    if (not re.fullmatch(r"[a-f0-9]{64}", identifier) or record.get("Driver") != "bridge"
            or record.get("EnableIPv6") is not False
            or options.get("com.docker.network.bridge.enable_icc") != "false"):
        raise WorkerEgressUnavailable("Worker egress proof needs the exact isolated IPv4 bridge")
    configs = (record.get("IPAM") or {}).get("Config") or []
    try:
        subnets = [ipaddress.IPv4Network(item["Subnet"]) for item in configs]
    except (KeyError, ValueError, TypeError):
        raise WorkerEgressUnavailable("Worker bridge subnet could not be verified") from None
    if len(subnets) != 1 or not subnets[0].is_private:
        raise WorkerEgressUnavailable("Worker egress proof needs one private IPv4 bridge subnet")
    bridge = options.get("com.docker.network.bridge.name") or "br-" + identifier[:12]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", bridge):
        raise WorkerEgressUnavailable("Worker bridge interface is invalid")
    return identifier, str(subnets[0]), bridge


PROBE_PROGRAM = r'''import errno,json,socket,sys,signal
signal.alarm(25)
p=json.loads(sys.argv[1]); results=[]
for kind in ('positive','denied'):
 for target in p[kind]:
  status='unavailable'; code=None
  try:
   addresses=socket.getaddrinfo(target['host'],target['port'],socket.AF_INET,socket.SOCK_STREAM)
   if not addresses: raise OSError('No IPv4 address')
   failures=[]
   for family,stype,proto,_,address in addresses:
    try:
     with socket.socket(family,stype,proto) as s:
      s.settimeout(p['timeout_seconds']);s.connect(address)
     status='reachable';break
    except OSError as exc: failures.append(exc.errno)
   else:
    status='refused';code=failures[-1] if failures else None
  except OSError as exc: code=exc.errno
  results.append({'kind':kind,'host':target['host'],'port':target['port'],'status':status,'errno':code})
print(json.dumps({'results':results},separators=(',',':')))
'''


def probe_network(docker, *, network: str, image: str, policy: dict) -> dict:
    """Check behavior as an unprivileged disposable member, without mounting data."""
    policy = normalize_policy(policy)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", network):
        raise ValueError("A declared worker network is required")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("An exact native image ID is required")
    record = json.loads(docker(["network", "inspect", network]))[0]
    identifier, _, _ = network_identity(record)
    output = docker(["run", "--rm", "--network", network, "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--user", "65534:65534",
                     "--memory", "64m", "--pids-limit", "32", "--entrypoint", "/usr/bin/python3",
                     image, "-I", "-c", PROBE_PROGRAM, json.dumps(policy, separators=(",", ":"))])
    try:
        results = json.loads(output)["results"]
        expected = [(kind, item["host"], item["port"]) for kind in ("positive", "denied") for item in policy[kind]]
        if not isinstance(results, list) or [(r["kind"], r["host"], r["port"]) for r in results] != expected:
            raise ValueError
        if any(r["status"] != ("reachable" if r["kind"] == "positive" else "refused") for r in results):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise WorkerEgressUnavailable("Worker egress checks failed: verify enforcement, DNS and positive controls") from None
    return {"network_id": identifier, "policy_sha256": policy_digest(policy), "results": results,
            "observed_at": time.time()}


def require_current_receipt(*, network: str, docker, now: float | None = None) -> None:
    policy = configured_policy()
    if policy is None:
        return
    try:
        root = Path(os.environ["XPERFECT_CONTROL_ROOT"])
        fd = os.open(root / RECEIPT_NAME, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 32768):
                raise ValueError
            receipt = json.load(stream)
        record = json.loads(docker(["network", "inspect", network]))[0]
        identifier, _, _ = network_identity(record)
        observed = receipt["observed_at"]
        current = time.time() if now is None else now
        if (receipt.get("version") != 1 or receipt.get("enforcement") != "iptables_readback"
                or receipt.get("network_id") != identifier
                or receipt.get("policy_sha256") != policy_digest(policy)
                or not re.fullmatch(r"[a-f0-9]{64}", receipt.get("rules_sha256", ""))
                or type(observed) not in (int, float) or not 0 <= current - observed <= policy["max_age_seconds"]):
            raise ValueError
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise WorkerEgressUnavailable("Worker egress proof is missing, stale or changed; refresh the package network proof") from None
