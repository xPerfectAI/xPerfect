"""Bounded idle native processes; no inference, sessions or agent scheduling.

Only a native adapter with a resolved *static* account environment may supply a
factory. Per-attempt broker credentials must never enter an idle process. Leases
are single use: the admitted attempt must prove its leased process stopped before
it releases its ordinary run authority. This module does not replace that fence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import shutil
import sys
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class NativeWarmConfig:
    workers: int = 0
    idle_seconds: float = 120.0

    @classmethod
    def from_performance(cls, value: dict | None) -> "NativeWarmConfig":
        value = value or {}
        count = value.get("native_warm_servers", 0)
        expiry = value.get("native_warm_idle_seconds", 120.0)
        if type(count) is not int or not 0 <= count <= 8:
            raise ValueError("native_warm_servers must be an integer from 0 to 8")
        if (type(expiry) not in (int, float) or not math.isfinite(expiry)
                or not 10 <= expiry <= 3600):
            raise ValueError("native_warm_idle_seconds must be from 10 to 3600")
        return cls(count, float(expiry))


@dataclass(frozen=True)
class WarmScope:
    """Identifiers/configuration revisions only. Never put tokens in this key."""
    tenant_id: str
    owner_id: str
    worker_id: str
    profile: str
    model: str
    account_id: str
    configuration_revision: str
    environment_digest: str = ""
    lane: str = "conversation"
    execution_mode: str = "host"


_STATIC_ENVIRONMENT_KEYS = frozenset({
    "HOME", "PATH", "SHELL", "TERM", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME",
    "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
    "GROK_HOME", "GROK_AUTH_PATH", "GROK_SUBAGENTS", "GROK_WORKFLOWS", "GROK_DISABLE_AUTOUPDATER",
    "GROK_SESSION_SUMMARY_MODEL", "GROK_TELEMETRY_ENABLED", "GROK_TELEMETRY_TRACE_UPLOAD",
    "GROK_EXTERNAL_OTEL", "GROK_CURSOR_MCPS_ENABLED", "GROK_CLAUDE_MCPS_ENABLED",
    "GLASSHIVE_WORKER_ID", "GLASSHIVE_WORKER_RUNTIME", "GLASSHIVE_EXECUTION_MODE",
    "GLASSHIVE_WORKSPACE_DIR", "GLASSHIVE_LOG_DIR", "GLASSHIVE_RUN_MODE", "NODE_PATH", "PYTHONPATH",
})


def native_environment_digest(environment: dict[str, str], *, lane: str, profile: str = "grok-build") -> str:
    """Hash declared non-secret mode/path settings, never credentials or tokens.

    Adapters must additionally compare their complete static environment in memory
    before a claim; unknown environment values must not be treated as unchanged.
    """
    if lane not in {"conversation", "mission"}:
        raise ValueError("Unsupported native warm lane")
    if profile == "grok-build" and lane == "conversation" and any(environment.get(key) != "0" for key in ("GROK_SUBAGENTS", "GROK_WORKFLOWS")):
        raise ValueError("Conversation native warm mode must disable ephemeral native background work")
    values = {key: value for key, value in environment.items() if key in _STATIC_ENVIRONMENT_KEYS}
    return hashlib.sha256(json.dumps([lane, values], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.lstat()
    if path.parent.is_symlink() or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise PermissionError("Native warm state directory must be private")
    temporary = path.with_name(path.name + ".tmp." + secrets.token_hex(8))
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_private_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536):
            raise PermissionError("Native warm record is not private")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Invalid native warm private object")
    return value


def read_native_warm_record(path: Path) -> dict:
    value = _read_private_json(path)
    if value.get("kind") != "xperfect.native.warm.v1":
        raise ValueError("Invalid native warm record")
    for field_name in ("scope", "owner", "child", "escaped_descendants"):
        if value.get(field_name) is not None and not isinstance(value[field_name], dict):
            raise ValueError("Invalid native warm record structure")
    return value


def read_native_warm_lease(path: Path, *, worker_id: str, run_id: str,
                           attempt_id: str, model: str | None = None,
                           profile: str = "grok-build") -> dict:
    value = _read_private_json(path)
    scope = value.get("scope")
    child = value.get("server")
    if (value.get("version") != 1 or not isinstance(scope, dict) or not isinstance(child, dict)
            or not value.get("lease_id") or value.get("run_id") != run_id
            or value.get("attempt_id") != attempt_id or scope.get("worker_id") != worker_id
            or profile not in {"grok-build", "codex-cli", "direct-openai", "direct-anthropic", "direct-xai"}
            or scope.get("profile") != profile or scope.get("execution_mode") != "host"
            or (model is not None and scope.get("model") != model)
            or type(child.get("pid")) is not int or child["pid"] <= 0
            or child.get("process_group") != child["pid"]
            or not str(child.get("process_start_identity") or "").startswith("ps-lstart:")):
        raise ValueError("Native warm lease does not match the admitted attempt")
    return value


def _write_stopped_attempt_receipt(record: dict, identity: dict) -> None:
    if record.get("phase") != "leased":
        return
    path = Path(record.get("attempt_descriptor") or "")
    scope = record.get("scope") or {}
    value = read_native_warm_lease(path, worker_id=scope.get("worker_id"),
        run_id=record.get("run_id"), attempt_id=record.get("attempt_id"), model=scope.get("model"),
        profile=scope.get("profile"))
    if (value.get("lease_id") != record.get("lease_id") or value.get("scope") != scope
            or value.get("server") != identity):
        raise ValueError("Native warm stop receipt changed authority")
    _write_private(path, {key: item for key, item in {
        **value, "stop_confirmed": True,
        "escaped_descendants": record.get("escaped_descendants", {}),
    }.items()
                          if key not in {"secret", "url"}})


def _native_stop_ledger(record_path: Path):
    record = read_native_warm_record(record_path)
    known = {int(pid): identity for pid, identity in record.get("escaped_descendants", {}).items()}
    def remember(found):
        known.update(found)
        record["escaped_descendants"] = {str(pid): identity for pid, identity in known.items()}
        _write_private(record_path, record)
    return known, remember


def signal_existing_native_stop_owner(runtime, record_path: Path, child: dict, sig) -> bool:
    """The existing exact native stopper's signal phase; no process-exit wait."""
    _, remember = _native_stop_ledger(record_path)
    return runtime._signal_recorded_native_child(child, sig, remember=remember)


def existing_native_stop_owner(runtime, record_path: Path, child: dict) -> bool:
    """Use the existing native descendant stopper and retain its escaped ledger."""
    known, remember = _native_stop_ledger(record_path)
    generation_stopped = runtime._end_recorded_native_child(child, remember=remember)
    descendants_stopped = runtime._end_remembered_descendants(known)
    return generation_stopped and descendants_stopped


def listener_owned_by(pid: int, port: int) -> bool:
    """Confirm this exact native process owns the loopback listener before auth."""
    proc = Path(f"/proc/{pid}/fd")
    if proc.is_dir():
        owned = set()
        try:
            for descriptor in proc.iterdir():
                try:
                    owned.add(os.readlink(descriptor))
                except FileNotFoundError:
                    pass
            for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
                fields = line.split()
                if (len(fields) > 9 and fields[1] == f"0100007F:{port:04X}"
                        and fields[3] == "0A" and "socket:[" + fields[9] + "]" in owned):
                    return True
            return False
        except OSError:
            return False
    binary = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").is_file() else None)
    if not binary:
        return False
    try:
        result = subprocess.run([binary, "-nP", "-a", "-p", str(pid),
            f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN", "-Fn"],
            capture_output=True, text=True, timeout=2, check=False)
        return result.returncode == 0 and f"n127.0.0.1:{port}" in result.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return False


@contextmanager
def _native_state_lock(directory: Path, *, timeout: float | None = None):
    """Serialize native state mutation in one private directory across processes."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ".native-warm.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise PermissionError("Native warm state lock is not private")
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Native configuration is busy")
                    time.sleep(.01)
        yield
    finally:
        os.close(fd)


def recover_native_warm_records(directory: Path, runtime, *, active_lease=None) -> dict:
    """Evict only proven orphaned generations; never kill another live runtime."""
    counts = {"stopped": 0, "active": 0, "pending": 0}
    for path in directory.glob("*.native-warm.json"):
        try:
            with _native_state_lock(directory):
                record = read_native_warm_record(path)
                owner = record.get("owner") or {}
                owner_pid = int(owner.get("pid") or 0)
                owner_identity = owner.get("process_start_identity")
                if owner_pid > 0 and process_start_identity(owner_pid) == owner_identity:
                    counts["active"] += 1
                    continue
                if record.get("phase") == "leased" and active_lease and active_lease(record):
                    counts["active"] += 1
                    continue
                child = record.get("child")
                if child is None:
                    # The guard writes its own identity before exec. No recorded
                    # native child means it never passed that startup boundary.
                    path.unlink()
                elif existing_native_stop_owner(runtime, path, child):
                    _write_stopped_attempt_receipt(read_native_warm_record(path), child)
                    path.unlink()
                else:
                    counts["pending"] += 1
                    continue
                counts["stopped"] += 1
        except (OSError, ValueError, TypeError, RuntimeError):
            counts["pending"] += 1
    return counts


def process_start_identity(pid: int) -> str:
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=5, check=False)
    value = " ".join(result.stdout.split())
    return "ps-lstart:" + value if result.returncode == 0 and value else ""


@dataclass
class NativeWarmServer:
    process: subprocess.Popen = field(repr=False)
    start_identity: str
    url: str
    secret: str = field(repr=False)
    record_path: Path | None = field(default=None, repr=False)
    stop_owner: Callable[[dict], bool] | None = field(default=None, repr=False)
    created_at: float = field(default_factory=time.monotonic)
    _stop_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stopped: bool = field(default=False, repr=False)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def alive(self) -> bool:
        return self.process.poll() is None

    def identity(self) -> dict:
        return {"pid": self.process.pid, "process_group": self.process.pid,
                "process_start_identity": self.start_identity}

    def stop(self) -> None:
        """Delegate complete native/escaped-descendant proof to its existing owner."""
        with self._stop_lock:
            if self._stopped:
                return
            actual = process_start_identity(self.process.pid)
            if (not self.start_identity or (actual and actual != self.start_identity)
                    or (self.process.poll() is None and actual != self.start_identity)):
                raise RuntimeError("Native warm process identity cannot be confirmed")
            if not self.stop_owner or not self.stop_owner(self.identity()):
                raise RuntimeError("Native warm process stop cannot be confirmed")
            self.process.wait(timeout=2)
            if self.record_path:
                record = read_native_warm_record(self.record_path)
                _write_stopped_attempt_receipt(record, self.identity())
                self.record_path.unlink(missing_ok=True)
            self._stopped = True


class NativeWarmStartupUnconfirmed(RuntimeError):
    """A failed factory still owns a process; capacity must retain its identity."""
    def __init__(self, server: NativeWarmServer):
        super().__init__("Native warm failed startup stop is unconfirmed")
        self.server = server


@dataclass
class NativeWarmLease:
    scope: WarmScope
    run_id: str
    attempt_id: str
    server: NativeWarmServer = field(repr=False)
    lease_id: str = field(default_factory=lambda: secrets.token_hex(16))

    def write_descriptor(self, path: Path) -> None:
        """A private attempt receipt. URL token never enters argv or telemetry."""
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or parent.stat().st_mode & 0o077:
            raise PermissionError("Native warm lease directory must be private")
        body = {"version": 1, "lease_id": self.lease_id,
                "run_id": self.run_id, "attempt_id": self.attempt_id,
                "scope": self.scope.__dict__, "server": self.server.identity(),
                "url": self.server.url, "secret": self.server.secret,
                "state_record": str(self.server.record_path) if self.server.record_path else None}
        if self.server.record_path:
            record = read_native_warm_record(self.server.record_path)
            if record.get("phase") != "idle" or record.get("scope") != self.scope.__dict__:
                raise ValueError("Native warm process was already claimed or changed scope")
            _write_private(self.server.record_path, {**record, "phase": "leased",
                "run_id": self.run_id, "attempt_id": self.attempt_id,
                "lease_id": self.lease_id, "attempt_descriptor": str(path)})
        _write_private(path, body)


@dataclass
class _Target:
    config: NativeWarmConfig
    factory: Callable[[], NativeWarmServer] = field(repr=False)
    expires: float


class NativeWarmPool:
    """Single-use idle capacity shared by native adapters, bounded across owners.

    A worker's changed scope invalidates its old *idle* capacity. Its active lease
    stays owned by that exact admitted attempt. Expiry removes targets rather than
    continually waking processes for a user who has left.
    """
    def __init__(self, *, global_limit: int = 16, owner_limit: int = 8):
        if not 1 <= owner_limit <= global_limit <= 64:
            raise ValueError("Invalid native warm resource limits")
        self.global_limit, self.owner_limit = global_limit, owner_limit
        self._condition = threading.Condition()
        self._targets: dict[WarmScope, _Target] = {}
        self._idle: dict[WarmScope, list[NativeWarmServer]] = {}
        self._leased: dict[str, NativeWarmLease] = {}
        self._pending: set[WarmScope] = set()
        self._stopping: list[tuple[WarmScope, NativeWarmServer]] = []
        self._closed = False
        self._thread = threading.Thread(target=self._maintain, daemon=True,
                                        name="xperfect-native-idle")
        self._thread.start()

    def prepare(self, scope: WarmScope, config: NativeWarmConfig,
                factory: Callable[[], NativeWarmServer]) -> None:
        stale = []
        with self._condition:
            if self._closed:
                raise RuntimeError("Native warm pool is closed")
            for previous in list(self._targets):
                if previous != scope and (previous.tenant_id, previous.owner_id, previous.worker_id) == (
                        scope.tenant_id, scope.owner_id, scope.worker_id):
                    self._targets.pop(previous)
                    stale.extend((previous, server) for server in self._idle.pop(previous, []))
            if config.workers:
                current = self._targets.get(scope)
                if current and current.config == config:
                    current.factory = factory
                    current.expires = time.monotonic() + config.idle_seconds
                else:
                    self._targets[scope] = _Target(config, factory, time.monotonic() + config.idle_seconds)
                idle = self._idle.setdefault(scope, [])
                while len(idle) > config.workers:
                    stale.append((scope, idle.pop()))
            else:
                self._targets.pop(scope, None)
                stale.extend((scope, server) for server in self._idle.pop(scope, []))
            self._stopping.extend(stale)
            self._condition.notify_all()
        for stale_scope, server in stale:
            self._stop_retained(stale_scope, server)

    def _stop_retained(self, scope: WarmScope, server: NativeWarmServer) -> None:
        # An uncertain stop remains counted, rather than disappearing from caps.
        entry = scope, server
        with self._condition:
            if entry not in self._stopping:
                self._stopping.append(entry)
        server.stop()
        with self._condition:
            self._stopping.remove(entry)

    def claim(self, scope: WarmScope, *, run_id: str, attempt_id: str) -> NativeWarmLease | None:
        if not run_id or not attempt_id:
            raise ValueError("A native warm lease requires an admitted run and attempt")
        dead = []
        with self._condition:
            if self._closed:
                return None
            idle = self._idle.get(scope, [])
            lease = None
            while idle:
                server = idle.pop()
                target = self._targets.get(scope)
                if server.alive() and target and target.expires > time.monotonic():
                    lease = NativeWarmLease(scope, run_id, attempt_id, server)
                    self._leased[lease.lease_id] = lease
                    break
                dead.append(server)
                self._stopping.append((scope, server))
            self._condition.notify_all()
        for server in dead:
            self._stop_retained(scope, server)
        return lease

    def finish(self, lease: NativeWarmLease) -> None:
        # Stop proof precedes removing the lease from the resource count.
        with self._condition:
            if self._leased.get(lease.lease_id) is not lease:
                # Shutdown can stop and remove the exact lease before the run's
                # finally block reaches us. Unknown live leases still fail closed.
                if lease.server.stopped:
                    return
                raise ValueError("Native warm lease is not owned by this pool")
        lease.server.stop()
        with self._condition:
            self._leased.pop(lease.lease_id, None)
            self._condition.notify_all()

    def status(self, scope: WarmScope) -> dict:
        with self._condition:
            return {"configured": self._targets[scope].config.workers if scope in self._targets else 0,
                    "idle": sum(server.alive() for server in self._idle.get(scope, [])),
                    "starting": int(scope in self._pending),
                    "leased": sum(lease.scope == scope for lease in self._leased.values()),
                    "provider_context_warmed": False}

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._targets.clear()
            self._condition.notify_all()
        self._thread.join(timeout=12)
        startup_unconfirmed = self._thread.is_alive()
        with self._condition:
            servers = [(scope, server) for scope, group in self._idle.items() for server in group]
            servers.extend((lease.scope, lease.server) for lease in self._leased.values())
            servers.extend(self._stopping)
        failures = []
        for scope, server in servers:
            try:
                server.stop()
            except Exception:
                failures.append((scope, server))
        with self._condition:
            # A still-finishing factory can append an uncertain generation while
            # cleanup runs. Remove only the snapshot we actually handled.
            handled = {id(server) for _, server in servers}
            self._idle = {scope: remaining for scope, group in self._idle.items()
                          if (remaining := [server for server in group if id(server) not in handled])}
            self._leased = {key: lease for key, lease in self._leased.items()
                            if id(lease.server) not in handled}
            self._stopping = [(scope, server) for scope, server in self._stopping
                              if id(server) not in handled]
            self._stopping.extend(failures)
            unconfirmed_count = len(self._stopping) + len(self._leased) + sum(map(len, self._idle.values()))
        if unconfirmed_count:
            raise RuntimeError(f"Native warm cleanup could not confirm {unconfirmed_count} process stop(s)")
        if startup_unconfirmed:
            raise RuntimeError("Native warm startup did not stop; known processes were cleaned up")

    def _counts(self, owner: tuple[str, str]) -> tuple[int, int]:
        entries = [(scope, server) for scope, group in self._idle.items() for server in group]
        scopes = [scope for scope, _ in entries] + [lease.scope for lease in self._leased.values()]
        scopes += list(self._pending)
        scopes += [scope for scope, _ in self._stopping]
        return len(scopes), sum((scope.tenant_id, scope.owner_id) == owner for scope in scopes)

    def _maintain(self) -> None:
        while True:
            stale, selected = [], None
            with self._condition:
                if self._closed:
                    return
                for scope, target in list(self._targets.items()):
                    if target.expires <= time.monotonic():
                        self._targets.pop(scope)
                        stale.extend((scope, server) for server in self._idle.pop(scope, []))
                self._stopping.extend(stale)
                for scope, target in self._targets.items():
                    total, owned = self._counts((scope.tenant_id, scope.owner_id))
                    if (len(self._idle.get(scope, [])) < target.config.workers
                            and total < self.global_limit and owned < self.owner_limit):
                        selected = scope, target
                        self._pending.add(scope)
                        break
                if selected is None and not stale:
                    self._condition.wait(timeout=0.25)
                    continue
            for stale_scope, server in stale:
                try:
                    self._stop_retained(stale_scope, server)
                except Exception:
                    # The exact generation remains retained and counted. A stop
                    # failure cannot kill the maintenance loop or free capacity.
                    pass
            if selected is None:
                continue
            scope, target = selected
            try:
                server = target.factory()
            except Exception as exc:
                # A factory with an unconfirmed native stop retains real capacity.
                # Ordinary pre-spawn failure owns no process and may release it.
                with self._condition:
                    if self._targets.get(scope) is target:
                        self._targets.pop(scope)
                    if isinstance(exc, NativeWarmStartupUnconfirmed):
                        self._stopping.append((scope, exc.server))
                    self._pending.discard(scope)
                    self._condition.notify_all()
                continue
            with self._condition:
                self._pending.discard(scope)
                accepted = (not self._closed and self._targets.get(scope) is target
                            and target.expires > time.monotonic())
                if accepted:
                    self._idle.setdefault(scope, []).append(server)
                else:
                    self._stopping.append((scope, server))
                self._condition.notify_all()
            if not accepted:
                try:
                    self._stop_retained(scope, server)
                except Exception:
                    pass


def start_grok_server(*, binary: str, model: str, cwd: Path,
                      static_environment: dict[str, str], state_directory: Path,
                      scope: WarmScope, stop_runtime, timeout: float = 8.0) -> NativeWarmServer:
    """Initialize Grok's native server without opening a session or doing inference.

    The adapter, not this helper, must validate that this is a static account
    environment. Attempt tokens and instruction text are not accepted separately.
    """
    from websockets.sync.client import connect

    if (scope.profile != "grok-build" or scope.execution_mode != "host" or scope.model != model
            or scope.environment_digest != native_environment_digest(static_environment, lane=scope.lane)):
        raise ValueError("Native warm scope does not match this static host Grok environment")
    if any(static_environment.get(key) for key in (
            "GLASSHIVE_RUN_ID", "GLASSHIVE_PEER_TOKEN", "GLASSHIVE_CONTEXT_TOKEN",
            "GLASSHIVE_CAPABILITY_BROKER_TOKEN")):
        raise ValueError("An idle native process cannot inherit attempt authority")
    def command(port, record_path):
        return [binary, "agent", "--no-leader", "--model", model,
                "serve", "--bind", f"127.0.0.1:{port}"]

    def probe(server, deadline):
        with connect(server.url, additional_headers={"Authorization": "Bearer " + server.secret},
                     open_timeout=min(1.0, max(.05, deadline - time.monotonic())),
                     close_timeout=0, max_size=4 * 1024 * 1024, proxy=None) as connection:
            connection.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": 1, "clientInfo": {"name": "xperfect-native", "version": "1"},
                           "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}}))
            response = json.loads(connection.recv(timeout=max(.05, deadline - time.monotonic())))
            result = response.get("result") or {}
            if (response.get("id") != 1 or result.get("protocolVersion") != 1
                    or result.get("_meta", {}).get("grokShell") is not True):
                raise RuntimeError("Native warm server initialization mismatch")

    return start_native_warm_server(command=command, probe=probe, cwd=cwd,
        static_environment=static_environment, secret_environment="GROK_AGENT_SECRET",
        state_directory=state_directory, scope=scope, stop_runtime=stop_runtime, timeout=timeout)


def start_native_warm_server(*, command, probe, cwd, static_environment,
                             secret_environment, state_directory, scope, stop_runtime,
                             timeout=8.0):
    """Shared guarded launch/readiness/Stop owner; adapters validate their protocol."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    secret = secrets.token_urlsafe(32)
    environment = {**static_environment, secret_environment: secret}
    record_path = state_directory / (secrets.token_hex(16) + ".native-warm.json")
    owner = {"pid": os.getpid(), "process_start_identity": process_start_identity(os.getpid())}
    if not owner["process_start_identity"]:
        raise RuntimeError("Native warm owner identity is unavailable")
    _write_private(record_path, {"kind": "xperfect.native.warm.v1", "phase": "starting",
        "created_at": time.time(), "owner": owner, "scope": scope.__dict__, "port": port,
        "child": None, "secret": secret})
    argv = command(port, record_path)
    try:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                    "--guarded-exec", str(record_path), "--", *argv],
                                   cwd=str(cwd), env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except OSError:
        record_path.unlink(missing_ok=True)
        raise
    server = None
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None and time.monotonic() < deadline:
            record = read_native_warm_record(record_path)
            child = record.get("child") or {}
            if server is None and child.get("pid") == process.pid and child.get("process_start_identity"):
                server = NativeWarmServer(process, child["process_start_identity"],
                    f"ws://127.0.0.1:{port}/ws", secret, record_path,
                    lambda identity: existing_native_stop_owner(stop_runtime, record_path, identity))
            if server is None or not listener_owned_by(process.pid, port):
                time.sleep(.01)
                continue
            if process_start_identity(process.pid) != server.start_identity:
                raise RuntimeError("Native warm listener identity changed")
            try:
                probe(server, deadline)
                _write_private(record_path, {**read_native_warm_record(record_path), "phase": "idle"})
                return server
            except (OSError, TimeoutError):
                time.sleep(.01)
        raise RuntimeError("Native warm server did not become ready")
    except BaseException:
        try:
            if server is None:
                # Synchronize with the exec guard before capturing its incarnation.
                with _native_state_lock(state_directory):
                    record = read_native_warm_record(record_path)
                    child = record.get("child")
                    if child is None and process.poll() is not None:
                        record_path.unlink(missing_ok=True)
                        process.wait(timeout=2)
                    else:
                        if child is None:
                            child = {"pid": process.pid, "process_group": process.pid,
                                     "process_start_identity": process_start_identity(process.pid)}
                            _write_private(record_path, {**record, "phase": "failed", "child": child})
                        server = NativeWarmServer(process, child.get("process_start_identity", ""),
                            f"ws://127.0.0.1:{port}/ws", secret, record_path,
                            lambda identity: existing_native_stop_owner(stop_runtime, record_path, identity))
            if server is not None:
                server.stop()
        except BaseException as cleanup:
            if server is None:
                try:
                    identity = process_start_identity(process.pid)
                except (OSError, subprocess.SubprocessError):
                    identity = ""
                server = NativeWarmServer(process, identity, f"ws://127.0.0.1:{port}/ws",
                    secret, record_path,
                    lambda child: existing_native_stop_owner(stop_runtime, record_path, child))
            raise NativeWarmStartupUnconfirmed(server) from cleanup
        raise


def _guarded_exec(arguments: list[str]) -> None:
    if len(arguments) < 4 or arguments[1] != "--":
        raise SystemExit("Invalid native warm launch")
    path = Path(arguments[0])
    with _native_state_lock(path.parent):
        record = read_native_warm_record(path)
        owner = record.get("owner") or {}
        if (record.get("phase") != "starting" or record.get("child") is not None
                or os.getppid() != owner.get("pid")
                or process_start_identity(owner["pid"]) != owner.get("process_start_identity")):
            raise SystemExit("Native warm launcher is no longer owned")
        identity = process_start_identity(os.getpid())
        if not identity:
            raise SystemExit("Native warm generation identity is unavailable")
        _write_private(path, {**record, "phase": "booting", "child": {
            "pid": os.getpid(), "process_group": os.getpgrp(), "process_start_identity": identity}})
    os.execvpe(arguments[2], arguments[2:], os.environ)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--guarded-exec":
        _guarded_exec(sys.argv[2:])
    else:
        raise SystemExit("Native warm execution requires its private launch record")
