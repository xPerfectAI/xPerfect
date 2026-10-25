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
# The probe runs on Linux, including when the client/tests run on another OS.
LINUX_REJECT_ERRNO = 113


class WorkerEgressUnavailable(RuntimeError):
    """A configured bridge has no current, matching enforcement proof."""


def normalize_policy(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"denied", "positive", "max_age_seconds", "timeout_seconds"}:
        raise ValueError("Worker egress policy must contain denied addresses and positive TCP controls")
    normalized = {}
    for kind in ("denied", "positive"):
        entries = value.get(kind)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
            raise ValueError("Give between one and sixteen targets for each worker egress check")
        clean = []
        for entry in entries:
            fields = {"host", "port"} if kind == "positive" else {"host", "probe_port", "port", "exceptions"}
            if not isinstance(entry, dict) or set(entry) - fields or "host" not in entry:
                raise ValueError("Worker egress target fields are invalid")
            # Retain the earlier opt-in spelling, but never treat it as a rule port.
            port_name = "port" if kind == "positive" or "probe_port" not in entry else "probe_port"
            if port_name not in entry or (kind == "denied" and "probe_port" in entry and "port" in entry):
                raise ValueError("A denied address needs one probe_port; a positive control needs port")
            host, port = entry["host"], entry[port_name]
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
            if any(item["host"] == host and (kind == "denied" or item["port"] == port) for item in clean):
                raise ValueError("Worker egress targets must be distinct")
            if kind == "positive":
                clean.append({"host": host, "port": port})
                continue
            exceptions = entry.get("exceptions", [])
            if not isinstance(exceptions, list) or len(exceptions) > 16:
                raise ValueError("A denied address permits at most sixteen explicit TCP or UDP exceptions")
            allowed = []
            for exception in exceptions:
                if (not isinstance(exception, dict) or set(exception) != {"protocol", "port"}
                        or exception["protocol"] not in ("tcp", "udp")
                        or type(exception["port"]) is not int or not 1 <= exception["port"] <= 65535
                        or exception in allowed):
                    raise ValueError("Worker egress exceptions need a distinct TCP or UDP port")
                if exception == {"protocol": "tcp", "port": port}:
                    raise ValueError("The denied probe_port cannot also be a TCP exception")
                allowed.append(dict(exception))
            clean.append({"host": host, "probe_port": port,
                          "exceptions": sorted(allowed, key=lambda item: (item["protocol"], item["port"]))})
        normalized[kind] = sorted(clean, key=lambda item: (item["host"], item.get("port", item.get("probe_port"))))
    for target in normalized["positive"]:
        for denied in normalized["denied"]:
            if (target["host"] == denied["host"]
                    and {"protocol": "tcp", "port": target["port"]} not in denied["exceptions"]):
                raise ValueError("A positive control on a denied address needs an explicit TCP exception")
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
  port=target['port'] if kind=='positive' else target['probe_port']
  status='unavailable'; code=None
  try:
   addresses=socket.getaddrinfo(target['host'],port,socket.AF_INET,socket.SOCK_STREAM)
   if not addresses: raise OSError('No IPv4 address')
   failures=[]
   for family,stype,proto,_,address in addresses:
    try:
     with socket.socket(family,stype,proto) as s:
      s.settimeout(p['timeout_seconds']);s.connect(address)
     status='reachable';break
    except OSError as exc: failures.append(exc.errno)
   else:
    code=failures[-1] if failures else None
    status='refused' if failures and all(n==errno.EHOSTUNREACH for n in failures) else 'unavailable'
  except OSError as exc: code=exc.errno
  results.append({'kind':kind,'host':target['host'],'port':port,'status':status,'errno':code})
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
        expected = [(kind, item["host"], item["port"] if kind == "positive" else item["probe_port"])
                    for kind in ("positive", "denied") for item in policy[kind]]
        if not isinstance(results, list) or [(r["kind"], r["host"], r["port"]) for r in results] != expected:
            raise ValueError
        if any(r["status"] != ("reachable" if r["kind"] == "positive" else "refused") for r in results):
            raise ValueError
        # Linux maps our icmp-admin-prohibited REJECT to EHOSTUNREACH. Other
        # errors, including timeouts, do not qualify as enforcement evidence.
        if any(type(r.get("errno")) is not int or r["errno"] != LINUX_REJECT_ERRNO
               for r in results if r["kind"] == "denied"):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise WorkerEgressUnavailable("Worker egress checks failed: verify enforcement, DNS and positive controls") from None
    return {"network_id": identifier, "policy_sha256": policy_digest(policy), "results": results,
            "observed_at": time.time()}


def _read_receipt(path: Path) -> dict:
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 32768):
                    raise ValueError
                return json.load(stream)
        except (FileNotFoundError, json.JSONDecodeError):
            if attempt:
                raise
            # docker cp may briefly replace the proof. One bounded read retry
            # tolerates publication without accepting a malformed or stale file.
            time.sleep(0.05)


def require_current_receipt(*, network: str, docker, now: float | None = None) -> None:
    policy = configured_policy()
    if policy is None:
        return
    try:
        receipt = _read_receipt(Path(os.environ["XPERFECT_CONTROL_ROOT"]) / RECEIPT_NAME)
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
        raise WorkerEgressUnavailable("Worker egress proof is missing, stale or changed; ask the host administrator to apply the package network policy") from None
