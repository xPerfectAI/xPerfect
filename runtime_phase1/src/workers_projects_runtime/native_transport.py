"""Native worker access to the runtime's MCP endpoints in packages.

Packaged workspace boxes share one Docker bridge that refuses traffic between
its containers, so no box can reach another owner's box, or the runtime, over
the network. The runtime instead serves each workspace box one Unix socket in
that box's own mounted native directory, which only that box and the runtime
can see, and relays it to its local API. A small standard-library stdio bridge
carries every harness's MCP messages over that socket; the runtime still
authorizes each request by its exact-attempt bearer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import stat
import sys
import threading
from urllib.parse import quote, unquote, urlparse

logger = logging.getLogger(__name__)

SOCKET_NAME = ".xperfect-runtime.sock"
# WorkspaceBox mounts its service-owned native directory here in every box.
BOX_SOCKET = "/workspace/data/" + SOCKET_NAME
PACKAGED_PROFILES = frozenset({"local-linux", "hosted-xfs"})
# The same local-only plain-HTTP hosts the peer and coordinator endpoints accept.
LOCAL_HTTP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})
UPSTREAM = ("127.0.0.1", 8766)
# Python 3.13 removes a server's socket file when it closes unless told not to.
_KEEP_SOCKET_FILE = {"cleanup_socket": False} if sys.version_info >= (3, 13) else {}
_hub: "NativeSocketHub | None" = None


def packaged_profile() -> bool:
    """True for the packaged Docker profiles whose workers run in workspace boxes."""
    return (
        os.environ.get("XPERFECT_EXECUTION_PROFILE") in PACKAGED_PROFILES
        and os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "").strip().rstrip("/")
        == "http://runtime:8766"
        and bool(os.environ.get("XPERFECT_CONTROL_ROOT"))
    )


def native_base() -> str | None:
    """The in-box runtime origin for native workers, or None when unavailable."""
    if packaged_profile() and _hub is not None and _hub.running:
        return "http+unix://" + quote(BOX_SOCKET, safe="")
    return None


def mcp_listen_origin() -> tuple[str, int]:
    """The host and port a runtime MCP endpoint accepts in its Host header.

    Requests relayed by the stdio bridge name the runtime `runtime:8766`.
    """
    configured = os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766").rstrip("/")
    if socket_route(configured):
        from .native_mcp_bridge import HOST, PORT

        return HOST, PORT
    endpoint = urlparse(configured)
    return (endpoint.hostname or "127.0.0.1",
            endpoint.port or (443 if endpoint.scheme == "https" else 80))


def socket_route(value: str) -> str | None:
    """`value` when it is an `http+unix://` origin for an absolute socket path."""
    parsed = urlparse(value)
    path = unquote(parsed.netloc)
    if (parsed.scheme != "http+unix" or not path.startswith("/") or "\0" in path
            or parsed.path or parsed.params or parsed.query or parsed.fragment):
        return None
    return value


def worker_route() -> str | None:
    """The runtime origin a worker can reach for native context, or None when this
    installation gives workers no route.

    Packaged and hosted boxes use only their own socket. Elsewhere the operator's
    configured runtime address is used: a local socket the runtime serves, which
    host workers reach through the stdio bridge, or HTTP(S), where plain HTTP is
    accepted only on this host.
    """
    socket_base = native_base()
    if socket_base:
        return socket_base
    if packaged_profile() or os.environ.get("XPERFECT_EXECUTION_PROFILE") == "hosted-xfs":
        return None
    base = os.environ.get("GLASSHIVE_PEER_RUNTIME_BASE_URL", "").rstrip("/")
    if base.startswith("http+unix:"):
        return socket_route(base)
    endpoint = urlparse(base)
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        return None
    if endpoint.scheme == "http" and endpoint.hostname not in LOCAL_HTTP_HOSTS:
        return None
    return base


def _service_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise PermissionError("The native socket directory is not a private service directory")


def _remove_stale(descriptor: int) -> None:
    try:
        info = os.stat(SOCKET_NAME, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise PermissionError("Only a stale runtime socket may be replaced")
    os.unlink(SOCKET_NAME, dir_fd=descriptor)


def _bind_path(descriptor: int, path: Path) -> str:
    # Owner directories are longer than a socket address allows. Binding through
    # the held directory is short, and names exactly the directory checked above.
    if Path("/proc/self/fd").is_dir():
        return f"/proc/self/fd/{descriptor}/{SOCKET_NAME}"
    return str(path)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


class NativeSocketHub:
    """One Unix socket per workspace box, each relayed to the local runtime API."""

    def __init__(self, upstream: tuple[str, int] = UPSTREAM):
        self.upstream = upstream
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: dict[str, tuple[asyncio.AbstractServer, int]] = {}
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._loop is not None

    def start(self) -> None:
        loop, ready = asyncio.new_event_loop(), threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        thread = threading.Thread(target=run, name="xperfect-native-sockets", daemon=True)
        thread.start()
        if not ready.wait(10):
            raise RuntimeError("The native socket loop did not start")
        self._loop, self._thread = loop, thread

    def _call(self, coroutine, timeout: float = 10):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout)

    @staticmethod
    async def _close(server: asyncio.AbstractServer) -> None:
        # Stop accepting; a request already relayed finishes on its own.
        server.close()

    def ensure(self, directory: Path) -> Path:
        """Serve ``directory``'s socket, rebinding it when its file was replaced or lost."""
        path = Path(directory) / SOCKET_NAME
        with self._lock:
            if self._loop is None:
                raise RuntimeError("The native socket hub is not running")
            current = self._servers.get(str(path))
            if current is not None:
                try:
                    info = path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and info.st_ino == current[1]:
                        return path
                except FileNotFoundError:
                    pass
                del self._servers[str(path)]
                self._call(self._close(current[0]))
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _service_directory(descriptor)
                _remove_stale(descriptor)
                # The file stays when the runtime stops, so a restart can serve it again.
                server = self._call(asyncio.start_unix_server(
                    self._relay, path=_bind_path(descriptor, path), **_KEEP_SOCKET_FILE))
                try:
                    # Anything in this box may connect; each request still needs its bearer.
                    os.chmod(SOCKET_NAME, 0o666, dir_fd=descriptor)
                    inode = os.stat(SOCKET_NAME, dir_fd=descriptor, follow_symlinks=False).st_ino
                except OSError:
                    self._call(self._close(server))
                    raise
            finally:
                os.close(descriptor)
            self._servers[str(path)] = (server, inode)
        return path

    def release(self, directory: Path) -> None:
        """Stop serving a removed box and remove its socket file."""
        path = Path(directory) / SOCKET_NAME
        with self._lock:
            current = self._servers.pop(str(path), None)
            if current is None or self._loop is None:
                return
            self._call(self._close(current[0]))
            try:
                if path.lstat().st_ino == current[1]:
                    path.unlink()
            except FileNotFoundError:
                pass

    def recover(self, volume_root: Path) -> int:
        """Serve every box socket an earlier runtime left, so running boxes reconnect."""
        root, served = Path(volume_root), 0
        for pattern in ("execution_workspaces/*/native/" + SOCKET_NAME,
                        "owners/*/execution_workspaces/*/native/" + SOCKET_NAME):
            for path in root.glob(pattern):
                try:
                    self.ensure(path.parent)
                    served += 1
                except (OSError, RuntimeError) as exc:
                    logger.warning("A workspace socket could not be served again: %s", type(exc).__name__)
        return served

    def stop(self) -> None:
        with self._lock:
            servers, self._servers = [server for server, _ in self._servers.values()], {}
            loop, thread, self._loop, self._thread = self._loop, self._thread, None, None
        if loop is None:
            return
        for server in servers:
            try:
                asyncio.run_coroutine_threadsafe(self._close(server), loop).result(10)
            except Exception:  # pragma: no cover - a relay that will not close
                pass
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(10)
        loop.close()

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(*self.upstream), timeout=10
            )
        except (OSError, asyncio.TimeoutError):
            writer.close()
            return
        try:
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        finally:
            writer.close()
            upstream_writer.close()


def start_hub(volume_root: Path) -> NativeSocketHub:
    """Start the runtime's socket hub; boxes are served as they are verified."""
    global _hub
    hub = NativeSocketHub()
    hub.start()
    hub.recover(volume_root)
    _hub = hub
    return hub


def stop_hub() -> None:
    global _hub
    hub, _hub = _hub, None
    if hub is not None:
        hub.stop()


def ensure_box_socket(native_root: Path) -> None:
    """Serve one verified box; a packaged box without its socket fails closed."""
    if _hub is not None and packaged_profile():
        _hub.ensure(native_root)


def release_box_socket(native_root: Path) -> None:
    if _hub is not None:
        _hub.release(native_root)


def bridge_source() -> str:
    return (Path(__file__).with_name("native_mcp_bridge.py")).read_text(encoding="utf-8")


def bridge_interpreter(base: str) -> str:
    """A packaged box runs its image's python3; a host worker runs the runtime's own
    interpreter, since a host's bare python3 may be missing or a system shim."""
    return "python3" if base == native_base() else sys.executable


def stdio_server(url: str, token_env: str, interpreter: str = "python3") -> dict:
    """Claude/Grok-format stdio server entry for one native endpoint."""
    return {
        "type": "stdio",
        "command": interpreter,
        "args": ["-c", bridge_source(), url, token_env],
        "env": {token_env: "${" + token_env + "}"},
    }


def codex_stdio_block(name: str, url: str, token_env: str, interpreter: str = "python3") -> str:
    """Codex config block; the bearer stays in the harness environment."""
    arguments = ", ".join(json.dumps(item) for item in ("-c", bridge_source(), url, token_env))
    return (
        f"[mcp_servers.{name}]\ncommand = {json.dumps(interpreter)}\nargs = [{arguments}]\n"
        f"env_vars = [{json.dumps(token_env)}]\n"
    )


def is_projected_stdio(server: dict, url: str) -> bool:
    """Whether a native server entry is exactly this module's bridge for url."""
    args = server.get("args")
    return (
        server.get("command") in {"python3", sys.executable}
        and isinstance(args, list)
        and len(args) == 4
        and args[0] == "-c"
        and args[2] == url
    )
