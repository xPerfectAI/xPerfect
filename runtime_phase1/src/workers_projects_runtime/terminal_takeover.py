from __future__ import annotations

import asyncio
import json
import os
import pty
import struct
import subprocess
import termios
from dataclasses import dataclass
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect


@dataclass
class TerminalTarget:
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    title: str = "Worker Terminal"
    subtitle: str = ""


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    os.write(fd, b"")
    size = struct.pack("HHHH", rows, cols, 0, 0)
    termios.tcsetwinsize(fd, (rows, cols))
    try:
        import fcntl

        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        return


async def bridge_terminal(
    websocket: WebSocket,
    target: TerminalTarget,
    *,
    should_close: Callable[[], bool] | None = None,
) -> None:
    await websocket.accept()
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        target.command,
        cwd=target.cwd,
        env=target.env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        text=False,
    )
    os.close(slave_fd)
    _set_winsize(master_fd, 32, 120)

    async def reader() -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            await websocket.send_text(data.decode("utf-8", errors="replace"))

    async def writer() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"type": "input", "data": raw}
            kind = str(payload.get("type") or "")
            if kind == "resize":
                rows = max(int(payload.get("rows") or 24), 12)
                cols = max(int(payload.get("cols") or 80), 40)
                _set_winsize(master_fd, rows, cols)
                continue
            if kind == "input":
                data = str(payload.get("data") or "")
                if data:
                    os.write(master_fd, data.encode())

    async def close_monitor() -> None:
        while True:
            await asyncio.sleep(0.2)
            if should_close and should_close():
                try:
                    await websocket.close(code=1008, reason="Workspace closed")
                except RuntimeError:
                    pass
                return

    reader_task = asyncio.create_task(reader())
    writer_task = asyncio.create_task(writer())
    monitor_task = asyncio.create_task(close_monitor()) if should_close else None
    try:
        tasks = {reader_task, writer_task}
        if monitor_task:
            tasks.add(monitor_task)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    finally:
        for task in (reader_task, writer_task, monitor_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
        try:
            process.terminate()
        except OSError:
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
