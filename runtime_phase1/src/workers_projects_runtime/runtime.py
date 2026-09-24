from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx

from .models import utc_now
from .openclaw_release import reviewed_openclaw_env, verify_reviewed_openclaw_binary
from .store import Store

DEFAULT_TOOL_TIMEOUT = int(os.environ.get("WPR_OPENCLAW_TOOL_TIMEOUT", "300"))
DEFAULT_PORT_START = int(os.environ.get("WPR_OPENCLAW_PORT_START", "19600"))
DEFAULT_PORT_END = int(os.environ.get("WPR_OPENCLAW_PORT_END", "19850"))
DEFAULT_SANDBOX_IMAGE = os.environ.get("WPR_OPENCLAW_SANDBOX_IMAGE", "openclaw-sandbox:bookworm-slim")
DEFAULT_SANDBOX_MODE = os.environ.get("WPR_OPENCLAW_SANDBOX_MODE", "all")
DEFAULT_SANDBOX_SCOPE = os.environ.get("WPR_OPENCLAW_SANDBOX_SCOPE", "agent")
DEFAULT_WORKSPACE_ACCESS = os.environ.get("WPR_OPENCLAW_WORKSPACE_ACCESS", "rw")
DEFAULT_DOCKER_NETWORK = os.environ.get("WPR_OPENCLAW_DOCKER_NETWORK", "bridge")
AUTO_BUILD_SANDBOX_IMAGE = os.environ.get("WPR_AUTO_BUILD_SANDBOX_IMAGE", "true").strip().lower() in {"1", "true", "yes", "on"}
READINESS_TIMEOUT = int(os.environ.get("WPR_OPENCLAW_READINESS_TIMEOUT", "60"))
OPENCLAW_BIN = os.environ.get("WPR_OPENCLAW_BIN", os.environ.get("OPENCLAW_BIN", "openclaw"))
PROVIDER_ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
]


class StubRuntimeManager:
    def __init__(self, store: Store, base_dir: Path):
        self.store = store
        self.base_dir = base_dir

    def profile_model(self, profile: str) -> str:
        if profile == "openclaw-codex":
            return "openai/codex-mini-latest"
        if profile == "openclaw-claude":
            return "anthropic/claude-sonnet-4-20250514"
        return "anthropic/claude-sonnet-4-20250514"

    async def create_worker_runtime(self, worker: dict[str, Any]) -> dict[str, Any]:
        worker_root = self.base_dir / "runtime" / worker["worker_id"]
        workspace = worker_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return self.store.update_worker(
            worker["worker_id"],
            state="ready",
            control_url=f"/ui/workers/{worker['worker_id']}",
            takeover_url=f"/ui/workers/{worker['worker_id']}",
            state_dir=str(worker_root),
            workspace_dir=str(workspace),
            session_key=f"worker:{worker['worker_id']}",
            gateway_port=19701,
            gateway_token="stub-token",
            pid=99999,
            last_error=None,
        )

    async def ensure_worker_running(self, worker: dict[str, Any]) -> dict[str, Any]:
        updated = self.store.get_worker(worker["worker_id"])
        if updated and updated["state"] == "paused":
            updated = self.store.update_worker(worker["worker_id"], state="ready")
        return updated or worker

    async def start_run(self, worker: dict[str, Any], run: dict[str, Any]) -> None:
        self.store.update_worker(worker["worker_id"], state="running", last_error=None)
        self.store.update_run(run["run_id"], state="running", started_at=utc_now())
        self.store.add_event(worker["project_id"], worker["worker_id"], run["run_id"], "run.started", run["instruction"])

        await asyncio.sleep(0.05)
        result = "OPENCLAW_STUB_OK"
        self.store.append_run_output(run["run_id"], result)
        self.store.update_run(run["run_id"], state="completed", ended_at=utc_now())
        self.store.update_worker(worker["worker_id"], state="ready")
        self.store.update_project(worker["project_id"], summary=result)
        self.store.add_event(worker["project_id"], worker["worker_id"], run["run_id"], "run.completed", result)

    async def interrupt_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.interrupted", "Worker interrupted")
        return self.store.update_worker(worker["worker_id"], state="paused")

    async def pause_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.paused", "Worker paused")
        return self.store.update_worker(worker["worker_id"], state="paused")

    async def resume_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.resumed", "Worker resumed")
        return self.store.update_worker(worker["worker_id"], state="ready")

    async def terminate_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.terminated", "Worker terminated")
        return self.store.update_worker(worker["worker_id"], state="terminated")


class OpenClawRuntimeManager:
    def __init__(self, store: Store, base_dir: Path):
        self.store = store
        self.base_dir = base_dir
        self.runtime_root = self.base_dir / "runtime"
        self.log_root = self.base_dir / "logs"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)
        self.active_tasks: dict[str, asyncio.Task[Any]] = {}
        self.worker_locks: dict[str, asyncio.Lock] = {}
        self.viventium_core_dir = Path(os.environ.get("WPR_VIVENTIUM_CORE_DIR", Path(__file__).resolve().parents[6] / "viventium_core"))
        self.openclaw_source_dir = Path(os.environ.get("WPR_OPENCLAW_SOURCE_DIR", self.viventium_core_dir / "viventium_v0_4" / "openclaw"))

    def profile_model(self, profile: str) -> str:
        if profile == "openclaw-codex":
            return os.environ.get("WPR_MODEL_OPENCLAW_CODEX", "openai/codex-mini-latest")
        if profile == "openclaw-claude":
            return os.environ.get("WPR_MODEL_OPENCLAW_CLAUDE", "anthropic/claude-sonnet-4-20250514")
        if profile == "openclaw-desktop":
            return os.environ.get("WPR_MODEL_OPENCLAW_DESKTOP", "anthropic/claude-sonnet-4-20250514")
        return os.environ.get("WPR_MODEL_OPENCLAW_GENERAL", "anthropic/claude-sonnet-4-20250514")

    def _worker_lock(self, worker_id: str) -> asyncio.Lock:
        if worker_id not in self.worker_locks:
            self.worker_locks[worker_id] = asyncio.Lock()
        return self.worker_locks[worker_id]

    def _worker_root(self, worker_id: str) -> Path:
        root = self.runtime_root / worker_id
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        return root

    def _find_free_port(self) -> int:
        for port in range(DEFAULT_PORT_START, DEFAULT_PORT_END + 1):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
        raise RuntimeError("No free ports available for OpenClaw workers")

    def _gateway_headers(self, worker: dict[str, Any], *, stream: bool = False) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {worker['gateway_token']}",
            "x-openclaw-agent-id": "main",
            "x-openclaw-session-key": worker["session_key"],
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        return headers

    def _gateway_base_url(self, worker: dict[str, Any]) -> str:
        return f"http://127.0.0.1:{worker['gateway_port']}"

    def _tools_invoke_url(self, worker: dict[str, Any]) -> str:
        return f"{self._gateway_base_url(worker)}/tools/invoke"

    def _responses_url(self, worker: dict[str, Any]) -> str:
        return f"{self._gateway_base_url(worker)}/v1/responses"

    def _openclaw_env(self, state_dir: Path, config_path: Path, token: str) -> dict[str, str]:
        env = reviewed_openclaw_env(os.environ)
        env["OPENCLAW_STATE_DIR"] = str(state_dir)
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
        env["OPENCLAW_GATEWAY_TOKEN"] = token
        for key in PROVIDER_ENV_KEYS:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _write_config(self, worker: dict[str, Any], worker_root: Path, port: int, token: str) -> Path:
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
                    "model": {"primary": worker["model"]},
                    "sandbox": {
                        "mode": DEFAULT_SANDBOX_MODE,
                        "scope": DEFAULT_SANDBOX_SCOPE,
                        "workspaceAccess": DEFAULT_WORKSPACE_ACCESS,
                        "docker": {
                            "image": DEFAULT_SANDBOX_IMAGE,
                            "network": DEFAULT_DOCKER_NETWORK,
                        },
                    },
                },
            },
            "tools": {
                "fs": {"workspaceOnly": True},
                "exec": {
                    "host": "sandbox",
                    "applyPatch": {"workspaceOnly": True},
                },
                "elevated": {"enabled": False},
            },
            "session": {"dmScope": "per-channel-peer"},
            "plugins": {"enabled": True},
        }
        config_path = worker_root / "openclaw.json"
        config_path.write_text(json.dumps(config, indent=2))
        return config_path

    def _ensure_sandbox_image(self) -> None:
        inspect = subprocess.run(["docker", "image", "inspect", DEFAULT_SANDBOX_IMAGE], capture_output=True, text=True)
        if inspect.returncode == 0:
            return
        if not AUTO_BUILD_SANDBOX_IMAGE:
            raise RuntimeError(
                f"OpenClaw sandbox image '{DEFAULT_SANDBOX_IMAGE}' is missing. Build it with: cd {self.openclaw_source_dir} && ./scripts/sandbox-setup.sh"
            )
        build = subprocess.run(["bash", "scripts/sandbox-setup.sh"], cwd=str(self.openclaw_source_dir), capture_output=True, text=True)
        if build.returncode != 0:
            raise RuntimeError(f"Failed to build OpenClaw sandbox image: {build.stderr[-4000:]}")

    async def _wait_for_ready(self, worker: dict[str, Any]) -> None:
        deadline = asyncio.get_running_loop().time() + READINESS_TIMEOUT
        payload = {"tool": "sessions_list", "args": {}, "sessionKey": worker["session_key"]}
        headers = self._gateway_headers(worker)
        async with httpx.AsyncClient(timeout=5) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.post(self._tools_invoke_url(worker), json=payload, headers=headers)
                    if response.status_code in {200, 401, 404}:
                        return
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
                    pass
                await asyncio.sleep(1)
        raise RuntimeError(f"OpenClaw worker {worker['worker_id']} was not ready within {READINESS_TIMEOUT}s")

    async def _stop_process(self, worker: dict[str, Any]) -> None:
        pid = worker.get("pid")
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGTERM)
            await asyncio.sleep(1.5)
            try:
                os.kill(pid, 0)
            except OSError:
                return
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return

    def _looks_alive(self, worker: dict[str, Any]) -> bool:
        pid = worker.get("pid")
        port = worker.get("gateway_port")
        if not pid or not port:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    async def create_worker_runtime(self, worker: dict[str, Any]) -> dict[str, Any]:
        lock = self._worker_lock(worker["worker_id"])
        async with lock:
            worker = self.store.update_worker(worker["worker_id"], state="starting", last_error=None) or worker
            verify_reviewed_openclaw_binary(OPENCLAW_BIN)
            self._ensure_sandbox_image()
            worker_root = self._worker_root(worker["worker_id"])
            port = worker.get("gateway_port") or self._find_free_port()
            token = worker.get("gateway_token") or secrets.token_urlsafe(24)
            session_key = worker.get("session_key") or f"worker:{worker['worker_id']}"
            config_path = self._write_config(worker, worker_root, port, token)

            log_stdout = open(self.log_root / f"{worker['worker_id']}.stdout.log", "a")
            log_stderr = open(self.log_root / f"{worker['worker_id']}.stderr.log", "a")
            env = self._openclaw_env(worker_root, config_path, token)
            cmd = [*shlex.split(OPENCLAW_BIN), "gateway", "--port", str(port), "--bind", "loopback", "--token", token, "--allow-unconfigured", "--force"]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_stdout,
                stderr=log_stderr,
                env=env,
                cwd=str(worker_root / "workspace"),
            )

            updated = self.store.update_worker(
                worker["worker_id"],
                state="starting",
                control_url=f"/ui/workers/{worker['worker_id']}",
                takeover_url=f"/ui/workers/{worker['worker_id']}",
                gateway_port=port,
                gateway_token=token,
                session_key=session_key,
                state_dir=str(worker_root),
                workspace_dir=str(worker_root / "workspace"),
                pid=process.pid,
                last_error=None,
            )
            assert updated is not None
            await self._wait_for_ready(updated)
            ready = self.store.update_worker(worker["worker_id"], state="ready")
            self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.ready", "OpenClaw gateway ready")
            return ready or updated

    async def ensure_worker_running(self, worker: dict[str, Any]) -> dict[str, Any]:
        lock = self._worker_lock(worker["worker_id"])
        async with lock:
            latest = self.store.get_worker(worker["worker_id"]) or worker
            if self._looks_alive(latest):
                if latest["state"] in {"paused", "resuming", "starting"}:
                    latest = self.store.update_worker(worker["worker_id"], state="ready") or latest
                return latest
            if latest["state"] in {"terminating", "termination_failed", "terminated"}:
                raise RuntimeError("Worker is terminated")
            return await self.create_worker_runtime(latest)

    async def _stream_run(self, worker_id: str, run_id: str, instruction: str) -> None:
        worker = self.store.get_worker(worker_id)
        if not worker:
            return
        try:
            worker = await self.ensure_worker_running(worker)
            self.store.update_worker(worker_id, state="running", last_error=None)
            self.store.update_run(run_id, state="running", started_at=utc_now())
            self.store.add_event(worker["project_id"], worker_id, run_id, "run.started", instruction)

            payload = {"model": "openclaw", "input": instruction, "stream": True, "user": worker["session_key"]}
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    self._responses_url(worker),
                    json=payload,
                    headers=self._gateway_headers(worker, stream=True),
                ) as response:
                    response.raise_for_status()
                    event_type: Optional[str] = None
                    async for raw_line in response.aiter_lines():
                        if raw_line is None:
                            continue
                        line = raw_line.strip()
                        if not line:
                            continue
                        if line.startswith("event: "):
                            event_type = line[7:].strip()
                            continue
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            payload_data = json.loads(data_str)
                        except json.JSONDecodeError:
                            payload_data = {"raw": data_str}

                        if event_type == "response.output_text.delta":
                            delta = str(payload_data.get("delta", ""))
                            if delta:
                                self.store.append_run_output(run_id, delta)
                                self.store.add_event(worker["project_id"], worker_id, run_id, event_type, delta)
                        elif event_type == "response.output_item.added":
                            item = payload_data.get("item") or payload_data
                            if isinstance(item, dict) and item.get("type") == "function_call":
                                name = item.get("name", "tool")
                                self.store.add_event(worker["project_id"], worker_id, run_id, "tool.call", str(name))
                        elif event_type == "response.completed":
                            self.store.add_event(worker["project_id"], worker_id, run_id, event_type, "Run completed")
                        elif event_type == "response.failed":
                            message = json.dumps(payload_data, default=str)
                            self.store.add_event(worker["project_id"], worker_id, run_id, event_type, message)
                        elif event_type:
                            summary = payload_data.get("message") if isinstance(payload_data, dict) else None
                            if summary:
                                self.store.add_event(worker["project_id"], worker_id, run_id, event_type, str(summary))

            final_run = self.store.update_run(run_id, state="completed", ended_at=utc_now())
            final_worker = self.store.update_worker(worker_id, state="ready", last_error=None)
            final_output = (final_run or {}).get("output_text", "")
            if final_worker:
                self.store.update_project(final_worker["project_id"], summary=final_output[:4000])
                self.store.add_event(final_worker["project_id"], worker_id, run_id, "run.completed", final_output[:2000] or "Run completed")
        except asyncio.CancelledError:
            run = self.store.get_run(run_id)
            worker = self.store.get_worker(worker_id)
            if run:
                self.store.update_run(run_id, state="interrupted", ended_at=utc_now(), error_text="Interrupted by operator")
            if worker:
                self.store.update_worker(worker_id, state="paused", last_error="Interrupted by operator")
                self.store.add_event(worker["project_id"], worker_id, run_id, "run.interrupted", "Interrupted by operator")
            raise
        except Exception as exc:
            run = self.store.get_run(run_id)
            worker = self.store.get_worker(worker_id)
            message = f"{type(exc).__name__}: {exc}"
            if run:
                self.store.update_run(run_id, state="failed", ended_at=utc_now(), error_text=message)
            if worker:
                self.store.update_worker(worker_id, state="failed", last_error=message)
                self.store.add_event(worker["project_id"], worker_id, run_id, "run.failed", message)
        finally:
            self.active_tasks.pop(worker_id, None)

    async def start_run(self, worker: dict[str, Any], run: dict[str, Any]) -> None:
        if worker["worker_id"] in self.active_tasks:
            raise RuntimeError("Worker already has an active run")
        task = asyncio.create_task(self._stream_run(worker["worker_id"], run["run_id"], run["instruction"]))
        self.active_tasks[worker["worker_id"]] = task

    async def interrupt_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        task = self.active_tasks.get(worker["worker_id"])
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        latest = self.store.get_worker(worker["worker_id"]) or worker
        await self._stop_process(latest)
        interrupted = self.store.update_worker(worker["worker_id"], pid=None, state="paused", last_error="Interrupted by operator")
        self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.interrupted", "Worker interrupted and paused")
        return interrupted or latest

    async def pause_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        return await self.interrupt_worker(worker)

    async def resume_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        resumed = await self.ensure_worker_running(worker)
        resumed = self.store.update_worker(worker["worker_id"], state="ready", last_error=None) or resumed
        self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.resumed", "Worker resumed")
        return resumed

    async def terminate_worker(self, worker: dict[str, Any]) -> dict[str, Any]:
        task = self.active_tasks.get(worker["worker_id"])
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        latest = self.store.get_worker(worker["worker_id"]) or worker
        await self._stop_process(latest)
        state_dir = latest.get("state_dir")
        if state_dir:
            shutil.rmtree(state_dir, ignore_errors=True)
        terminated = self.store.update_worker(worker["worker_id"], pid=None, state="terminated", last_error=None)
        self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.terminated", "Worker terminated and runtime removed")
        return terminated or latest
