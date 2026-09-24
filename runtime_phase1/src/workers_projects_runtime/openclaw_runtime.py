from __future__ import annotations

import os
import json
import secrets
import shlex
import signal
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol

import httpx

from .openclaw_release import reviewed_openclaw_env, verify_reviewed_openclaw_binary
from .terminal_takeover import TerminalTarget

_PROVIDER_ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_URL",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
    "XAI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OLLAMA_API_KEY",
    "OLLAMA_HOST",
]


def _declared_long_mission(worker: dict) -> bool:
    try:
        bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(bundle, dict)
        and bundle.get("viventium_run_liveness")
        == {"version": 1, "long_mission": True}
    )

def _run_timeout_sec(
    timeout_sec: float | None = None,
    *,
    declared_long: bool = False,
) -> float | None:
    raw = os.environ.get("GLASSHIVE_RUN_TIMEOUT_SEC", "").strip()
    if not raw and not declared_long:
        raw = os.environ.get("GLASSHIVE_MAX_RUN_DURATION_S", "").strip()
    if not raw:
        raw = os.environ.get("WPR_RUN_TIMEOUT_SEC", "").strip()
    if not raw:
        return timeout_sec if timeout_sec and timeout_sec > 0 else None
    if raw.lower() in {"0", "none", "off", "false", "disabled"}:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return timeout_sec if timeout_sec and timeout_sec > 0 else None
    return parsed if parsed > 0 else None




@dataclass
class RuntimeInfo:
    runtime: str
    model: str
    gateway_url: str
    gateway_port: int | None
    gateway_token: str | None
    session_key: str | None
    state_dir: str | None
    workspace_dir: str | None
    pid: int | None


class RuntimeErrorBase(RuntimeError):
    pass


class RuntimeConfigurationError(RuntimeErrorBase):
    """A user-actionable runtime setting is missing or invalid."""


class ProviderRateLimitError(RuntimeErrorBase):
    """Structured provider throttling with an authoritative bounded retry delay."""

    failure_class = "provider_rate_limited"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float,
        provider_event_source: str = "http",
        provider_failure_attestation: dict[str, str | int] | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_s = max(0.1, min(float(retry_after_s), 86_400.0))
        self.provider_event_source = str(provider_event_source or "")
        self.provider_failure_attestation = dict(provider_failure_attestation or {})

class HostCapacityError(RuntimeErrorBase):
    """Structured, retryable host admission pressure; never a terminal run failure."""

    code = "host_capacity"
    failure_class = "host_capacity"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        capacity_class: str,
        retry_after_s: float | None = None,
        dimension: str = "",
        configured: dict[str, int] | None = None,
        used: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.capacity_class = str(capacity_class or "host")
        self.retry_after_s = retry_after_s
        self.dimension = str(dimension or "")
        self.configured = dict(configured or {})
        self.used = dict(used or {})

class RuntimeDependencyMissingError(RuntimeErrorBase):
    def __init__(
        self,
        message: str,
        *,
        binary: str = "",
        runtime_name: str = "",
        profile: str = "",
        execution_mode: str = "",
        required_version: str = "",
        actual_version: str = "",
        dependency_label: str = "",
        recovery_hint: str = "",
    ) -> None:
        super().__init__(message)
        self.binary = binary
        self.runtime_name = runtime_name
        self.profile = profile
        self.execution_mode = execution_mode
        self.required_version = required_version
        self.actual_version = actual_version
        self.dependency_label = dependency_label
        self.recovery_hint = recovery_hint


class ProviderAuthenticationMissingError(RuntimeDependencyMissingError):
    """The selected runtime cannot start without reconnecting its provider auth."""

    code = "provider_auth_missing"
    failure_class = "provider_auth_missing"
    retryable = False

class WorkerPausedError(RuntimeErrorBase):
    pass


class WorkerTerminatedError(RuntimeErrorBase):
    pass


class WorkerInterruptedError(RuntimeErrorBase):
    pass


class WorkerDetachedError(WorkerInterruptedError):
    """This process stopped waiting on a live generation that keeps running.

    Raised only by a managed restart: the exact native session, its container, and its
    transcript files stay untouched so the restarted service can adopt the same generation
    and collect its single terminal result instead of launching a duplicate.
    """


@contextmanager
def runtime_start_boundary(worker: dict):
    """Enter the service-owned Close/start fence at the real external start boundary."""

    guard_factory = worker.get("_runtime_start_guard")
    if callable(guard_factory):
        with guard_factory():
            yield
        return
    yield


def notify_runtime_started(worker: dict) -> None:
    callback = worker.get("_runtime_started_callback")
    if callable(callback):
        callback()


def notify_runtime_info(worker: dict, info: RuntimeInfo) -> None:
    callback = worker.get("_runtime_info_callback")
    if callable(callback):
        callback(info)


def _response_output_parts(data: dict) -> list[str]:
    output_parts: list[str] = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text", "")
                    if text:
                        output_parts.append(text)
        elif item.get("type") == "function_call":
            output_parts.append(f"[Tool call: {item.get('name', 'function')}]")
    return output_parts


def _streamed_response_text(response: httpx.Response) -> str:
    """Read an OpenAI Responses SSE stream after its request was accepted."""

    output_parts: list[str] = []
    completed_text = ""
    completed_response: dict = {}
    event_type = ""
    saw_completed = False
    for raw_line in response.iter_lines():
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.startswith("event:"):
            event_type = line.partition(":")[2].strip()
            continue
        if not line.startswith("data:"):
            continue
        raw_data = line.partition(":")[2].strip()
        if raw_data == "[DONE]":
            break
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload_type = str(payload.get("type") or event_type)
        if payload_type == "response.output_text.delta":
            delta = str(payload.get("delta") or "")
            if delta:
                output_parts.append(delta)
        elif payload_type == "response.output_text.done":
            completed_text = str(payload.get("text") or "")
        elif payload_type == "response.output_item.added":
            item = payload.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "function_call":
                output_parts.append(f"[Tool call: {item.get('name', 'function')}]")
        elif payload_type == "response.completed":
            saw_completed = True
            candidate = payload.get("response") or {}
            if isinstance(candidate, dict):
                completed_response = candidate
        elif payload_type == "response.failed":
            failure = payload.get("response") or payload.get("error") or payload
            raise RuntimeErrorBase(
                f"OpenClaw response failed: {json.dumps(failure, default=str)[:500]}"
            )
    if not saw_completed:
        raise RuntimeErrorBase("OpenClaw stream ended before response.completed")
    if output_parts:
        return "".join(output_parts).strip()
    if completed_text:
        return completed_text.strip()
    completed_parts = _response_output_parts(completed_response)
    if completed_parts:
        return "\n".join(completed_parts).strip()
    return str(completed_response or {"status": "completed"})


class RunStartupRejectedError(RuntimeErrorBase):
    """A spawned exact run could not cross the durable startup fence."""

    def __init__(self, message: str, *, termination_confirmed: bool) -> None:
        super().__init__(message)
        self.termination_confirmed = bool(termination_confirmed)

class WorkerRuntime(Protocol):
    def resolve_model(self, profile: str) -> str: ...

    def preflight_worker_profile(self, profile: str, execution_mode: str = "docker") -> None: ...

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo: ...

    def prepare_worker_workspace(self, worker: dict) -> RuntimeInfo: ...

    def pause_worker(self, worker: dict) -> RuntimeInfo: ...

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo: ...

    def terminate_worker(self, worker: dict) -> RuntimeInfo: ...

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str: ...

    def reconcile_worker(self, worker: dict) -> RuntimeInfo: ...


class StubRuntime:
    preflight_uses_cli_subprocess = False

    # The deterministic test runtime has no external process/session identity.
    # The service may therefore confirm its in-process generation before entry.
    requires_run_start_identity = False

    def resolve_model(self, profile: str) -> str:
        return {
            "openclaw-codex": "stub/openai-codex",
            "openclaw-claude": "stub/claude",
            "codex-cli": "stub/codex-cli",
            "claude-code": "stub/claude-code",
        }.get(profile, "stub/general")

    def preflight_worker_profile(self, profile: str, execution_mode: str = "docker") -> None:
        return None

    def _runtime_info(self, worker: dict, *, pid: int | None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        workspace_dir = f"/tmp/{worker_id}/workspace"
        raw_bundle = worker.get("bootstrap_bundle_json")
        if isinstance(raw_bundle, str) and raw_bundle.strip():
            try:
                bundle = json.loads(raw_bundle)
            except json.JSONDecodeError:
                bundle = {}
            if isinstance(bundle, dict) and bundle.get("run_mode") == "conversation":
                workspace_dir = str(Path(str(worker.get("workspace_root") or workspace_dir)).expanduser().resolve())
        return RuntimeInfo(
            runtime="openclaw-stub",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general")),
            gateway_url=f"http://127.0.0.1/stub/{worker_id}",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"agent:main:wpr:worker:{worker_id}",
            state_dir=f"/tmp/{worker_id}/state",
            workspace_dir=workspace_dir,
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._runtime_info(worker, pid=99999)

    def prepare_worker_workspace(self, worker: dict) -> RuntimeInfo:
        return self._runtime_info(worker, pid=None)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        # The stub has no external process, so a synthetic exact-run interrupt
        # is synchronously confirmed and must not report a fake live PID.
        return self._runtime_info(worker, pid=None)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_info(worker, pid=None)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        with runtime_start_boundary(worker):
            notify_runtime_started(worker)
        return f"STUB_OK: {instruction}"

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        if worker.get("state") in {"paused", "terminating", "termination_failed", "terminated"}:
            return self._runtime_info(worker, pid=None)
        return self.ensure_worker_ready(worker)


    preflight_uses_cli_subprocess = False

    requires_run_start_identity = False

    def isolated_resource_usage(self) -> dict[str, object]:
        # Deterministic synthetic substrate used only by unit/API test setups.
        return {
            "child_processes": 0,
            "threads": 0,
            "available_memory_bytes": 16 * 1024**3,
            "available_disk_bytes": 64 * 1024**3,
            "running_worker_containers": 0,
            "process_probe_ok": True,
            "memory_probe_ok": True,
            "disk_probe_ok": True,
        }


class OpenClawRuntime:
    _sandbox_build_lock = Lock()

    # This adapter publishes start synchronously from its guarded HTTP dispatch
    # boundary. It does not launch a detached per-run process that needs the
    # external startup-identity observer used by the CLI runtimes.
    requires_run_start_identity = False

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parents[2] / "data"
        self.runtime_root = self.base_dir / "openclaw_runtime"
        self.logs_dir = self.runtime_root / "logs"
        self.workers_dir = self.runtime_root / "workers"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.workers_dir.mkdir(parents=True, exist_ok=True)

        self.openclaw_bin = os.environ.get("WPR_OPENCLAW_BIN", "openclaw")
        self.port_start = int(os.environ.get("WPR_OPENCLAW_PORT_START", "19600"))
        self.port_end = int(os.environ.get("WPR_OPENCLAW_PORT_END", "19899"))
        self.readiness_timeout = int(os.environ.get("WPR_OPENCLAW_READINESS_TIMEOUT", "45"))
        self.sandbox_image = os.environ.get("WPR_OPENCLAW_SANDBOX_IMAGE", "openclaw-sandbox:bookworm-slim")
        self.sandbox_network = os.environ.get("WPR_OPENCLAW_SANDBOX_NETWORK", "bridge")
        self.auto_build_sandbox = os.environ.get("WPR_OPENCLAW_BUILD_SANDBOX", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.openclaw_repo = self._detect_openclaw_repo()
        self.local_ollama_model = self._detect_local_ollama_model()

    def resolve_model(self, profile: str) -> str:
        general_default = os.environ.get("WPR_MODEL_OPENCLAW_GENERAL", "").strip() or self._preferred_general_model()
        desktop_default = os.environ.get("WPR_MODEL_OPENCLAW_DESKTOP", "").strip() or general_default
        defaults = {
            "openclaw-general": general_default,
            "openclaw-codex": os.environ.get("WPR_MODEL_OPENCLAW_CODEX", "openai-codex/gpt-5.3-codex"),
            "openclaw-claude": os.environ.get("WPR_MODEL_OPENCLAW_CLAUDE", "anthropic/claude-sonnet-4-6"),
            "openclaw-desktop": desktop_default,
        }
        return defaults.get(profile, defaults["openclaw-general"])

    def _prepare_worker_runtime(self) -> None:
        """Complete shared, non-worker provisioning outside the Close/start fence."""

        verify_reviewed_openclaw_binary(self.openclaw_bin)
        self._ensure_sandbox_image()

    def _start_worker_runtime(self, worker: dict) -> RuntimeInfo:
        """Return a live process identity without waiting for gateway readiness."""

        meta = self._metadata_for_worker(worker)
        pid = self._safe_int(worker.get("pid"))
        gateway_port = self._safe_int(worker.get("gateway_port"))
        gateway_token = worker.get("gateway_token") or meta["gateway_token"]

        if pid and gateway_port and gateway_token and self._process_alive(pid):
            return RuntimeInfo(
                runtime="openclaw",
                model=worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general")),
                gateway_url=f"http://127.0.0.1:{gateway_port}",
                gateway_port=gateway_port,
                gateway_token=gateway_token,
                session_key=worker.get("session_key") or meta["session_key"],
                state_dir=meta["state_dir"],
                workspace_dir=meta["workspace_dir"],
                pid=pid,
            )

        port = gateway_port or self._find_free_port()
        token = gateway_token
        self._write_config(
            state_dir=Path(meta["state_dir"]),
            port=port,
            token=token,
            model=worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general")),
        )
        process = self._start_gateway(
            state_dir=Path(meta["state_dir"]),
            workspace_dir=Path(meta["workspace_dir"]),
            worker_id=worker["worker_id"],
            port=port,
            token=token,
        )
        return RuntimeInfo(
            runtime="openclaw",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general")),
            gateway_url=f"http://127.0.0.1:{port}",
            gateway_port=port,
            gateway_token=token,
            session_key=worker.get("session_key") or meta["session_key"],
            state_dir=meta["state_dir"],
            workspace_dir=meta["workspace_dir"],
            pid=process.pid,
        )

    @staticmethod
    def _runtime_worker_with_info(worker: dict, info: RuntimeInfo) -> dict:
        return {
            **worker,
            "runtime": info.runtime,
            "model": info.model,
            "gateway_url": info.gateway_url,
            "gateway_port": info.gateway_port,
            "gateway_token": info.gateway_token,
            "session_key": info.session_key,
            "state_dir": info.state_dir,
            "workspace_dir": info.workspace_dir,
            "pid": info.pid,
        }

    def _start_and_publish_worker_runtime(self, worker: dict) -> RuntimeInfo:
        with runtime_start_boundary(worker):
            info = self._start_worker_runtime(worker)
            try:
                notify_runtime_info(worker, info)
            except Exception as persistence_error:
                try:
                    self.terminate_worker(self._runtime_worker_with_info(worker, info))
                except Exception as cleanup_error:
                    raise RuntimeErrorBase(
                        "OpenClaw runtime metadata could not be persisted and the new gateway could not be cleaned up"
                    ) from cleanup_error
                raise persistence_error
        return info

    def _wait_for_published_worker_ready(
        self,
        worker: dict,
        info: RuntimeInfo,
    ) -> RuntimeInfo:
        if not info.gateway_port or not info.gateway_token:
            raise RuntimeErrorBase("OpenClaw gateway runtime metadata is incomplete")
        try:
            self._wait_for_ready(info.gateway_port, info.gateway_token)
        except Exception as readiness_error:
            try:
                cleaned = self.terminate_worker(
                    self._runtime_worker_with_info(worker, info)
                )
            except Exception as cleanup_error:
                raise RuntimeErrorBase(
                    "OpenClaw gateway readiness failed and the new gateway could not be cleaned up"
                ) from cleanup_error
            notify_runtime_info(worker, cleaned)
            raise readiness_error
        return info

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self._prepare_worker_runtime()
        info = self._start_and_publish_worker_runtime(worker)
        return self._wait_for_published_worker_ready(worker, info)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        pid = self._safe_int(worker.get("pid"))
        if pid:
            self._terminate_pid(pid)
        meta = self._metadata_for_worker(worker)
        return RuntimeInfo(
            runtime="openclaw",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general")),
            gateway_url=worker.get("gateway_url") or "",
            gateway_port=self._safe_int(worker.get("gateway_port")),
            gateway_token=worker.get("gateway_token") or meta["gateway_token"],
            session_key=worker.get("session_key") or meta["session_key"],
            state_dir=meta["state_dir"],
            workspace_dir=meta["workspace_dir"],
            pid=None,
        )

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self.pause_worker(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self.pause_worker(worker)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        response: httpx.Response | None = None
        try:
            with httpx.Client(timeout=_run_timeout_sec(timeout_sec)) as client:
                self._prepare_worker_runtime()
                info = self._start_and_publish_worker_runtime(worker)
                info = self._wait_for_published_worker_ready(worker, info)
                with runtime_start_boundary(worker):
                    headers = {
                        "Authorization": f"Bearer {info.gateway_token}",
                        "Content-Type": "application/json",
                        "x-openclaw-agent-id": "main",
                        "x-openclaw-session-key": info.session_key
                        or f"agent:main:wpr:worker:{worker['worker_id']}",
                    }
                    request = client.build_request(
                        "POST",
                        f"{info.gateway_url}/v1/responses",
                        json={"model": "openclaw", "input": instruction, "stream": True},
                        headers={**headers, "Accept": "text/event-stream"},
                    )
                    response = client.send(request, stream=True)
                    if response.is_error:
                        response.read()
                    response.raise_for_status()
                    notify_runtime_started(worker)
                if "text/event-stream" in str(response.headers.get("content-type") or "").lower():
                    return _streamed_response_text(response)
                response.read()
                response.raise_for_status()
                data = response.json()
        except httpx.ConnectError as exc:
            state = str(worker.get("state") or "")
            if state == "paused":
                raise WorkerPausedError("Worker was paused while a run was active") from exc
            if state in {"terminating", "termination_failed", "terminated"}:
                raise WorkerTerminatedError("Worker was terminated while a run was active") from exc
            raise RuntimeErrorBase(f"Could not connect to OpenClaw gateway for worker {worker['worker_id']}: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeErrorBase(f"OpenClaw returned HTTP {exc.response.status_code}: {exc.response.text[:500]}") from exc
        except httpx.TimeoutException as exc:
            effective_timeout = _run_timeout_sec(
                timeout_sec,
                declared_long=_declared_long_mission(worker),
            )
            raise RuntimeErrorBase(f"OpenClaw timed out after {effective_timeout}s") from exc
        except httpx.TransportError as exc:
            raise RuntimeErrorBase("OpenClaw response stream ended unexpectedly") from exc
        finally:
            if response is not None:
                response.close()

        output_parts = _response_output_parts(data)

        if output_parts:
            return "\n".join(output_parts).strip()
        return str(data)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        meta = self._metadata_for_worker(worker)
        pid = self._safe_int(worker.get("pid"))
        port = self._safe_int(worker.get("gateway_port"))
        token = worker.get("gateway_token") or meta["gateway_token"]
        alive = bool(pid and port and token and self._process_alive(pid) and self._probe_gateway(port, token))
        return RuntimeInfo(
            runtime="openclaw",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general")),
            gateway_url=f"http://127.0.0.1:{port}" if alive and port else "",
            gateway_port=port,
            gateway_token=token,
            session_key=worker.get("session_key") or meta["session_key"],
            state_dir=meta["state_dir"],
            workspace_dir=meta["workspace_dir"],
            pid=pid if alive else None,
        )

    def terminal_target(self, worker: dict) -> TerminalTarget:
        meta = self._metadata_for_worker(worker)
        return TerminalTarget(
            command=["screen", "-xRR", f"wpr-{worker['worker_id']}"],
            cwd=meta["workspace_dir"],
            env={"TERM": "xterm-256color"},
            title=f"{worker['name']} terminal",
            subtitle="OpenClaw workspace terminal",
        )

    def describe_worker(self, worker: dict) -> dict[str, object]:
        meta = self._metadata_for_worker(worker)
        return {
            "mode": "openclaw-gateway",
            "runtime": "openclaw",
            "workspace_dir": meta["workspace_dir"],
            "state_dir": meta["state_dir"],
            "gateway_url": worker.get("gateway_url") or "",
            "gateway_port": self._safe_int(worker.get("gateway_port")),
            "pid": self._safe_int(worker.get("pid")),
            "sandbox_image": self.sandbox_image,
        }

    def _metadata_for_worker(self, worker: dict) -> dict[str, str]:
        worker_root = self.workers_dir / worker["worker_id"]
        state_dir = worker_root / "state"
        workspace_dir = state_dir / "workspace"
        state_dir.mkdir(parents=True, exist_ok=True)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        session_key = worker.get("session_key") or f"agent:main:wpr:worker:{worker['worker_id']}"
        gateway_token = worker.get("gateway_token") or secrets.token_urlsafe(24)
        return {
            "state_dir": str(state_dir),
            "workspace_dir": str(workspace_dir),
            "session_key": session_key,
            "gateway_token": gateway_token,
        }

    def _write_config(self, state_dir: Path, port: int, token: str, model: str) -> None:
        config = {
            "gateway": {
                "port": port,
                "mode": "local",
                "bind": "loopback",
                "auth": {"mode": "token", "token": token},
                "http": {"endpoints": {"responses": {"enabled": True}}},
            },
            "agents": {
                "defaults": {
                    "model": {"primary": model},
                    "sandbox": {
                        "mode": "all",
                        "scope": "session",
                        "workspaceAccess": "rw",
                        "docker": {
                            "image": self.sandbox_image,
                            "network": self.sandbox_network,
                        },
                    },
                }
            },
            "session": {
                "dmScope": "per-channel-peer",
                "maintenance": {
                    "mode": "enforce",
                    "pruneAfter": "14d",
                    "maxEntries": 500,
                    "rotateBytes": "20mb",
                },
            },
            "tools": {
                "fs": {"workspaceOnly": True},
            },
            "plugins": {"enabled": True},
        }
        (state_dir / "openclaw.json").write_text(__import__("json").dumps(config, indent=2))

    def _start_gateway(self, state_dir: Path, workspace_dir: Path, worker_id: str, port: int, token: str) -> subprocess.Popen:
        stdout_path = self.logs_dir / f"{worker_id}.stdout.log"
        stderr_path = self.logs_dir / f"{worker_id}.stderr.log"
        stdout_handle = stdout_path.open("a")
        stderr_handle = stderr_path.open("a")
        env = reviewed_openclaw_env(os.environ)
        env["OPENCLAW_STATE_DIR"] = str(state_dir)
        env["OPENCLAW_CONFIG_PATH"] = str(state_dir / "openclaw.json")
        env["OPENCLAW_GATEWAY_TOKEN"] = token
        model = self._resolve_config_model(state_dir)
        for key in _PROVIDER_ENV_KEYS:
            value = os.environ.get(key, "")
            if value:
                env[key] = value
        if model.startswith("ollama/"):
            env.setdefault("OLLAMA_HOST", "http://127.0.0.1:11434")
            env.setdefault("OLLAMA_API_KEY", "ollama-local")

        cmd = [
            *shlex.split(self.openclaw_bin),
            "gateway",
            "--port",
            str(port),
            "--bind",
            "loopback",
            "--token",
            token,
            "--allow-unconfigured",
            "--force",
        ]
        try:
            process = subprocess.Popen(
                cmd,
                cwd=workspace_dir,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
        except FileNotFoundError as exc:
            stdout_handle.close()
            stderr_handle.close()
            raise RuntimeErrorBase(
                f"OpenClaw binary not found at '{self.openclaw_bin}'. Install it before running the standalone service."
            ) from exc
        stdout_handle.close()
        stderr_handle.close()
        return process

    def _resolve_config_model(self, state_dir: Path) -> str:
        config_path = state_dir / "openclaw.json"
        if not config_path.exists():
            return ""
        try:
            import json

            data = json.loads(config_path.read_text())
        except Exception:
            return ""
        return (
            data.get("agents", {})
            .get("defaults", {})
            .get("model", {})
            .get("primary", "")
        )

    def _wait_for_ready(self, port: int, token: str) -> None:
        deadline = time.time() + self.readiness_timeout
        while time.time() < deadline:
            if self._probe_gateway(port, token):
                return
            time.sleep(1)
        raise RuntimeErrorBase(f"OpenClaw gateway on port {port} was not ready within {self.readiness_timeout}s")

    def _probe_gateway(self, port: int, token: str) -> bool:
        try:
            with httpx.Client(timeout=3) as client:
                response = client.post(
                    f"http://127.0.0.1:{port}/tools/invoke",
                    json={"tool": "__wpr_probe__", "args": {}},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
            return response.status_code in {200, 404}
        except httpx.HTTPError:
            return False

    def _find_free_port(self) -> int:
        for port in range(self.port_start, self.port_end + 1):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
        raise RuntimeErrorBase("No free local ports available for OpenClaw workers")

    def _process_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _terminate_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        for _ in range(20):
            if not self._process_alive(pid):
                return
            time.sleep(0.25)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return

    def _safe_int(self, value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _ensure_sandbox_image(self) -> None:
        if self._docker_image_exists(self.sandbox_image):
            return
        if not self.auto_build_sandbox:
            raise RuntimeErrorBase(
                f"Required sandbox image '{self.sandbox_image}' is missing and auto-build is disabled"
            )
        repo = self.openclaw_repo
        if repo is None:
            raise RuntimeErrorBase(
                f"Required sandbox image '{self.sandbox_image}' is missing and the local OpenClaw repo could not be found"
            )
        with self._sandbox_build_lock:
            if self._docker_image_exists(self.sandbox_image):
                return
            subprocess.run(["bash", "scripts/sandbox-setup.sh"], cwd=repo, check=True)

    def _docker_image_exists(self, image_name: str) -> bool:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _detect_openclaw_repo(self) -> Path | None:
        explicit = os.environ.get("WPR_OPENCLAW_REPO", "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            return path if path.exists() else None

        current = Path(__file__).resolve()
        candidates: list[Path] = []
        for parent in current.parents:
            candidates.append(parent / "viventium_core" / "viventium_v0_4" / "openclaw")
            candidates.append(parent / "app" / "viventium_core" / "viventium_v0_4" / "openclaw")
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _detect_local_ollama_model(self) -> str | None:
        preferred = os.environ.get("WPR_LOCAL_OLLAMA_MODEL", "").strip()
        if preferred:
            return f"ollama/{preferred}" if not preferred.startswith("ollama/") else preferred

        prefer_local = os.environ.get("WPR_PREFER_LOCAL_OLLAMA", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not prefer_local:
            return None

        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return None

        if result.returncode != 0:
            return None

        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        available = {line.split()[0] for line in lines[1:] if line and not line.startswith("NAME")}
        for candidate in ("qwen2.5:1.5b", "smollm2:135m"):
            if candidate in available:
                return f"ollama/{candidate}"
        return None

    def _preferred_general_model(self) -> str:
        if os.environ.get("WPR_PREFER_OPENCLAW_CLAUDE_CLI", "1").strip().lower() in {"1", "true", "yes", "on"}:
            if (Path.home() / ".claude").exists() or (Path.home() / ".claude.json").exists():
                return "claude-cli/opus-4.6"
        if (Path.home() / ".codex" / "auth.json").exists():
            return "codex-cli/gpt-5.4"
        prefer_anthropic = os.environ.get("WPR_PREFER_ANTHROPIC", "0").strip().lower() in {"1", "true", "yes", "on"}
        if prefer_anthropic and os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return "anthropic/claude-sonnet-4-6"
        if os.environ.get("OPENAI_API_KEY", "").strip():
            return "openai/gpt-5.2"
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return "anthropic/claude-sonnet-4-6"
        if self.local_ollama_model:
            return self.local_ollama_model
        return "openai/gpt-5.2"
