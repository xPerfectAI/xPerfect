import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from urllib.parse import quote

import pytest

from workers_projects_runtime import native_transport
from workers_projects_runtime.native_mcp_bridge import main as bridge_main


@pytest.fixture
def short_root():
    # A socket address is short (104 bytes on macOS); keep the test paths below it.
    root = Path(tempfile.mkdtemp(prefix="xpns", dir="/tmp" if os.path.isdir("/tmp") else None))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def hub():
    hubs = []

    def start(upstream=("127.0.0.1", 9)):
        started = native_transport.NativeSocketHub(upstream)
        started.start()
        hubs.append(started)
        return started

    yield start
    for started in hubs:
        started.stop()


def _box(root: Path, *parts: str) -> Path:
    directory = root.joinpath(*parts)
    directory.mkdir(parents=True)
    directory.chmod(0o711)
    return directory


def _url(socket_path: Path, path: str) -> str:
    return "http+unix://" + quote(str(socket_path), safe="") + path


def test_packaged_base_requires_exact_profile_and_a_running_hub(monkeypatch, tmp_path):
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "hosted-xfs")
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://runtime:8766")
    monkeypatch.setenv("XPERFECT_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setattr(native_transport, "_hub", None)
    assert native_transport.native_base() is None
    running = native_transport.NativeSocketHub()
    running.start()
    try:
        monkeypatch.setattr(native_transport, "_hub", running)
        assert native_transport.native_base() == "http+unix://%2Fworkspace%2Fdata%2F.xperfect-runtime.sock"
        for name, value in (
            ("XPERFECT_EXECUTION_PROFILE", ""),
            ("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://other:8766"),
            ("XPERFECT_CONTROL_ROOT", ""),
        ):
            with monkeypatch.context() as scoped:
                scoped.setenv(name, value)
                assert native_transport.native_base() is None
    finally:
        running.stop()
    assert native_transport.native_base() is None


@pytest.mark.parametrize(
    ("base", "routed"),
    [
        ("http://127.0.0.1:8766", True),
        ("http://host.docker.internal:8766", True),
        ("https://runtime.example.test", True),
        ("http://192.0.2.10:8766", False),
        ("http://user:secret@127.0.0.1:8766", False),
        ("", False),
    ],
)
def test_worker_route_takes_plain_http_only_on_this_host(monkeypatch, base, routed):
    for name in ("XPERFECT_EXECUTION_PROFILE", "XPERFECT_CONTROL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", base)
    assert (native_transport.worker_route() == base) is routed


def test_packaged_and_hosted_workers_route_only_through_their_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://runtime:8766")
    monkeypatch.setenv("XPERFECT_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setattr(native_transport, "_hub", None)
    for profile in ("local-linux", "hosted-xfs"):
        monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", profile)
        assert native_transport.worker_route() is None
    # A hosted profile never falls back to a configured address, even a local one.
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://127.0.0.1:8766")
    assert native_transport.worker_route() is None
    running = native_transport.NativeSocketHub()
    running.start()
    try:
        monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", "http://runtime:8766")
        monkeypatch.setattr(native_transport, "_hub", running)
        assert native_transport.worker_route() == native_transport.native_base()
    finally:
        running.stop()


def _serve_probe_mcp():
    import uvicorn
    from mcp.server.fastmcp import Context, FastMCP
    from workers_projects_runtime.mcp_server import _mcp_transport_security_settings

    probe = FastMCP(
        "probe", stateless_http=True, json_response=True, streamable_http_path="/",
        # The product's allowlist for in-package callers of the runtime.
        transport_security=_mcp_transport_security_settings("runtime", 8766),
    )

    @probe.tool(name="echo")
    def echo(text: str, ctx: Context):
        request = ctx.request_context.request
        return {"text": text, "authorization": request.headers.get("authorization") if request else ""}

    server = uvicorn.Server(uvicorn.Config(
        probe.streamable_http_app(), host="127.0.0.1", port=0, log_level="error", lifespan="on",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "probe MCP server did not start"
        time.sleep(0.05)
    return server, thread, server.servers[0].sockets[0].getsockname()[1]


def test_bridge_carries_real_mcp_json_rpc_through_a_private_box_socket(short_root, hub):
    server, thread, upstream_port = _serve_probe_mcp()
    try:
        native = _box(short_root, "native")
        socket_path = hub(("127.0.0.1", upstream_port)).ensure(native)
        info = socket_path.lstat()
        assert stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid()
        assert stat.S_IMODE(info.st_mode) == 0o666
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "probe-client", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "echo", "arguments": {"text": "SOCKET-PROBE-7731"}}},
        ]
        environment = {"PATH": os.environ.get("PATH", ""), "PROBE_TOKEN": "synthetic-attempt-token-0123456789"}
        completed = subprocess.run(
            [sys.executable, "-c", native_transport.bridge_source(), _url(socket_path, "/"), "PROBE_TOKEN"],
            input="\n".join(json.dumps(message) for message in messages) + "\n",
            env=environment, capture_output=True, text=True, timeout=60,
        )
        replies = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        assert [reply["id"] for reply in replies] == [1, 2, 3], completed.stderr
        assert replies[0]["result"]["serverInfo"]["name"] == "probe"
        assert [tool["name"] for tool in replies[1]["result"]["tools"]] == ["echo"]
        called = json.dumps(replies[2]["result"])
        assert "SOCKET-PROBE-7731" in called
        assert "Bearer synthetic-attempt-token-0123456789" in called
    finally:
        server.should_exit = True
        thread.join(20)


def test_configured_socket_route_accepts_only_an_absolute_socket_origin(monkeypatch, short_root):
    for name in ("XPERFECT_EXECUTION_PROFILE", "XPERFECT_CONTROL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    origin = _url(short_root / "runtime.sock", "")
    for value, routed in (
        (origin, origin),
        (origin + "/", origin),
        ("http+unix://relative.sock", None),
        (origin + "/v1", None),
        (origin + "?q=1", None),
    ):
        monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", value)
        assert native_transport.worker_route() == routed
    monkeypatch.setenv("GLASSHIVE_PEER_RUNTIME_BASE_URL", origin)
    assert native_transport.mcp_listen_origin() == ("runtime", 8766)
    # A hosted runtime never takes a configured route, socket or not.
    monkeypatch.setenv("XPERFECT_EXECUTION_PROFILE", "hosted-xfs")
    assert native_transport.worker_route() is None


@pytest.mark.parametrize("socket_configured", [True, False])
def test_host_worker_reads_context_through_the_runtimes_own_socket(
    short_root, monkeypatch, socket_configured
):
    """A runtime served on a local socket (not packaged) gives its host workers that socket."""
    import uvicorn
    from workers_projects_runtime.worker_context_mcp import native_context_server

    for name in ("XPERFECT_EXECUTION_PROFILE", "XPERFECT_CONTROL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    socket_path = short_root / "runtime.sock"
    monkeypatch.setenv(
        "GLASSHIVE_PEER_RUNTIME_BASE_URL",
        _url(socket_path, "") if socket_configured else "http://127.0.0.1:8766",
    )
    listed = []

    class Configuration:
        def native_list(self, token):
            listed.append(token)
            return {"sources": [{"id": "bootstrap:project_definition", "note": "SOCKET-ROUTE-5521"}]}

    server = uvicorn.Server(uvicorn.Config(
        native_context_server(Configuration()).streamable_http_app(),
        uds=str(socket_path), log_level="error", lifespan="on",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            assert time.monotonic() < deadline, "context MCP server did not start"
            time.sleep(0.05)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "host-worker", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "list_context", "arguments": {}}},
        ]
        environment = {"PATH": os.environ.get("PATH", ""),
                       "CONTEXT_TOKEN": "synthetic-context-token-0123456789"}
        completed = subprocess.run(
            [sys.executable, "-c", native_transport.bridge_source(), _url(socket_path, "/"), "CONTEXT_TOKEN"],
            input="\n".join(json.dumps(message) for message in messages) + "\n",
            env=environment, capture_output=True, text=True, timeout=60,
        )
        replies = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        assert [reply["id"] for reply in replies] == [1, 2], completed.stderr
        if socket_configured:
            assert "SOCKET-ROUTE-5521" in json.dumps(replies[1]["result"])
            assert listed == ["synthetic-context-token-0123456789"]
        else:
            # The bridge names the runtime `runtime:8766`, which an HTTP-configured
            # runtime does not accept: nothing reaches the tool.
            assert all("error" in reply for reply in replies) and listed == []
    finally:
        server.should_exit = True
        thread.join(20)


def test_a_box_socket_is_served_only_from_a_private_service_directory(short_root, hub):
    running = hub()
    shared = _box(short_root, "shared")
    shared.chmod(0o777)
    with pytest.raises(PermissionError, match="private service directory"):
        running.ensure(shared)
    assert not (shared / native_transport.SOCKET_NAME).exists()
    # A file that is not a stale socket is never replaced or removed.
    native = _box(short_root, "native")
    planted = native / native_transport.SOCKET_NAME
    planted.write_text("not a socket")
    with pytest.raises(PermissionError, match="stale runtime socket"):
        running.ensure(native)
    assert planted.read_text() == "not a socket"
    target = _box(short_root, "target")
    link = _box(short_root, "links") / "native"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        running.ensure(link)
    assert not (target / native_transport.SOCKET_NAME).exists()


def _connects(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def test_a_restarted_runtime_serves_the_sockets_its_boxes_still_use(short_root, hub):
    boxes = [_box(short_root, "execution_workspaces", "w", "native"),
             _box(short_root, "owners", "d", "execution_workspaces", "w", "native")]
    first = hub()
    for native in boxes:
        first.ensure(native)
    first.stop()
    # The files remain so a restart can serve them; nothing listens meanwhile.
    assert all((native / native_transport.SOCKET_NAME).exists() for native in boxes)
    assert not any(_connects(native / native_transport.SOCKET_NAME) for native in boxes)
    second = hub()
    assert second.recover(short_root) == 2
    assert all(_connects(native / native_transport.SOCKET_NAME) for native in boxes)
    # A lost file is served again on the next box verification.
    (boxes[0] / native_transport.SOCKET_NAME).unlink()
    second.ensure(boxes[0])
    assert _connects(boxes[0] / native_transport.SOCKET_NAME)
    # A removed box stops being served and its file goes.
    second.release(boxes[1])
    assert not (boxes[1] / native_transport.SOCKET_NAME).exists()


def test_bridge_without_its_bearer_answers_unavailable_without_connecting():
    def unreachable(*args, **kwargs):
        raise AssertionError("the bridge must not connect without its bearer")

    output = io.StringIO()
    code = bridge_main(
        ["-c", "http+unix://%2Fworkspace%2Fdata%2F.xperfect-runtime.sock/v1/native/context/", "TOKEN"],
        io.StringIO('{"jsonrpc":"2.0","id":9,"method":"tools/list"}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n'),
        output, {}, post=unreachable,
    )
    assert code == 0
    # The request gets an answer; the notification gets none.
    assert output.getvalue().splitlines() == [json.dumps(
        {"jsonrpc": "2.0", "id": 9, "error": {"code": -32000, "message": "xPerfect native access is unavailable"}})]


@pytest.mark.parametrize("endpoint", ["https://runtime:8768/v1/native/context/", "http://runtime:8766/v1/native/context/",
                                      "http+unix://relative.sock/v1/", "http+unix://%2Fx.sock/v1/?q=1"])
def test_bridge_accepts_only_an_absolute_box_socket_endpoint(endpoint, capsys):
    assert bridge_main(["-c", endpoint, "TOKEN"], io.StringIO(""), io.StringIO(), {"TOKEN": "t"}) == 2


def test_bridge_maps_http_status_and_unreachable_sockets_to_json_rpc_errors():
    seen = []

    def forbidden(socket_path, path, body, headers):
        seen.append((socket_path, path, headers["Authorization"]))
        return 403, "application/json", b""

    def missing(socket_path, path, body, headers):
        raise FileNotFoundError(socket_path)

    for post, message in ((forbidden, "xPerfect native endpoint returned HTTP 403"),
                          (missing, "xPerfect native endpoint is unavailable")):
        output = io.StringIO()
        bridge_main(
            ["-c", "http+unix://%2Fworkspace%2Fdata%2F.xperfect-runtime.sock/v1/native/context/", "TOKEN"],
            io.StringIO('{"jsonrpc":"2.0","id":"a","method":"tools/list"}\n'),
            output, {"TOKEN": "synthetic-token"}, post=post,
        )
        assert json.loads(output.getvalue()) == {"jsonrpc": "2.0", "id": "a",
                                                 "error": {"code": -32000, "message": message}}
    assert seen == [("/workspace/data/.xperfect-runtime.sock", "/v1/native/context/", "Bearer synthetic-token")]


def test_harness_entries_keep_the_bearer_in_the_environment():
    url = "http+unix://%2Fworkspace%2Fdata%2F.xperfect-runtime.sock/v1/native/context/"
    entry = native_transport.stdio_server(url, "GLASSHIVE_CONTEXT_TOKEN")
    assert entry["type"] == "stdio" and entry["command"] == "python3"
    assert entry["args"][0] == "-c" and entry["args"][2:] == [url, "GLASSHIVE_CONTEXT_TOKEN"]
    assert entry["env"] == {"GLASSHIVE_CONTEXT_TOKEN": "${GLASSHIVE_CONTEXT_TOKEN}"}
    codex = tomllib.loads(native_transport.codex_stdio_block("xperfect-context", url, "GLASSHIVE_CONTEXT_TOKEN"))
    server = codex["mcp_servers"]["xperfect-context"]
    assert server["command"] == "python3"
    assert server["args"] == ["-c", native_transport.bridge_source(), url, "GLASSHIVE_CONTEXT_TOKEN"]
    assert server["env_vars"] == ["GLASSHIVE_CONTEXT_TOKEN"]
    assert native_transport.is_projected_stdio(entry, url)
    assert not native_transport.is_projected_stdio(entry, url.replace("context", "peers"))
    from workers_projects_runtime.grok_projection import mcp_servers_for_bundle

    grok = mcp_servers_for_bundle(
        {"claude_project_mcp": {"mcpServers": {"xperfect-context": entry}}},
        {"GLASSHIVE_CONTEXT_TOKEN": "synthetic-token"},
    )
    assert grok == [{
        "name": "xperfect-context", "command": "python3",
        "args": ["-c", native_transport.bridge_source(), url, "GLASSHIVE_CONTEXT_TOKEN"],
        "env": [{"name": "GLASSHIVE_CONTEXT_TOKEN", "value": "synthetic-token"}],
    }]


def test_coordinator_projection_uses_the_box_socket_bridge_when_bound_that_way():
    from workers_projects_runtime.coordinator_mcp import project_coordinator_bootstrap

    url = "http+unix://%2Fworkspace%2Fdata%2F.xperfect-runtime.sock/v1/native/coordinator/"
    worker = {"worker_id": "wrk_c", "_active_run_id": "run_c", "_coordinator_native_projection": {
        "worker_id": "wrk_c", "run_id": "run_c", "token": "synthetic-token", "url": url, "transport": "stdio"}}
    projected = project_coordinator_bootstrap(worker, {})
    server = projected["claude_project_mcp"]["mcpServers"]["xperfect-coordinator"]
    assert native_transport.is_projected_stdio(server, url)
    assert projected["env"] == {"GLASSHIVE_PEER_TOKEN": "synthetic-token"}
    codex = tomllib.loads(projected["codex_config_append"])["mcp_servers"]["xperfect-coordinator"]
    assert codex["args"][2] == url and "url" not in codex
    assert "synthetic-token" not in projected["codex_config_append"]
