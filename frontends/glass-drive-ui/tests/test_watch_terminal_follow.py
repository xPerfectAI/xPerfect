"""Watch's terminal shows exactly the latest run: it waits while that run is queued, attaches
its own session once it starts and shows its saved output once it ends, never another run's
session. Runs the real watch.js functions and terminal.js page script under Node."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src/glass_drive_ui/static"
WATCH_JS = STATIC / "watch.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to run the page scripts")


def _function(source: str, name: str) -> str:
    match = re.search(rf"^function {name}\(.*?^}}\n", source, flags=re.S | re.M)
    assert match, name
    return match.group(0)


def _node(script: str) -> dict:
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


@node
def test_terminal_url_names_the_run_and_changes_only_with_its_run_or_phase():
    source = WATCH_JS.read_text()
    result = _node("\n".join([
        "let signedToken = 'link token';",
        _function(source, "terminalRunPhase"),
        _function(source, "terminalViewUrl"),
        _function(source, "withAuth"),
        "const url = (run, state) => withAuth(terminalViewUrl('http://127.0.0.1:8780', 'wrk_1', run, state));",
        "console.log(JSON.stringify({before: url('', ''), queued: url('run_a', 'queued'),",
        "  claimed: url('run_a', 'claimed'), running: url('run_a', 'running'), settling: url('run_a', 'settling'),",
        "  completed: url('run_a', 'completed'), failed: url('run_a', 'failed'), next: url('run_b', 'queued'),",
        "  plain: terminalViewUrl('', 'wrk_1', '')}));",
    ]))
    assert result["plain"] == "/ui/workers/wrk_1/terminal"
    assert result["before"] == "http://127.0.0.1:8780/ui/workers/wrk_1/terminal?gh_token=link%20token"
    assert result["queued"] == ("http://127.0.0.1:8780/ui/workers/wrk_1/terminal"
                                "?run=run_a&phase=waiting&gh_token=link%20token")
    # One reattachment per phase: waiting, live, ended.
    assert result["claimed"] == result["queued"]
    assert result["settling"] == result["running"] != result["queued"] and "phase=live" in result["running"]
    assert result["failed"] == result["completed"] != result["running"] and "phase=ended" in result["completed"]
    assert result["next"] != result["queued"] and "run=run_b" in result["next"]


def test_watch_attaches_the_terminal_for_the_latest_run_and_its_state():
    source = WATCH_JS.read_text()
    assert ("currentTerminalUrl = withAuth(terminalViewUrl(runtimeBase, workerId, String(data.latest_run?.run_id || ''),\n"
            "      String(data.latest_run?.state || '')));") in source


def test_watch_and_terminal_pages_load_the_exact_run_script_versions():
    # Browsers keep a cached script for the same ?v= token, so a changed script needs a new token.
    assert 'src="/static/watch.js?v=20260926run1"' in (STATIC / "watch.html").read_text()
    assert 'src="/static/terminal.js?v=20260926run1"' in (STATIC / "terminal.html").read_text()


@node
def test_terminal_page_carries_the_run_and_explains_each_close():
    page = (STATIC / "terminal.js").read_text()
    result = _node(r"""
const status = {textContent: ''};
const buttons = {};
global.document = {
  getElementById: id => id === 'terminal-status' ? status
    : id === 'terminal' ? {clientWidth: 800, clientHeight: 400}
    : {addEventListener: (_type, handler) => { buttons[id] = handler; }},
  querySelector: () => ({content: 'nonce'}),
  querySelectorAll: () => [],
  createElement: () => ({}),
  cookie: '',
};
global.location = {pathname: '/ui/workers/wrk_1/terminal', search: '?run=run_a&phase=waiting',
                   protocol: 'http:', host: 'testserver'};
const sockets = [];
global.WebSocket = class { constructor(url) { this.url = url; sockets.push(this); } send() {} close() {} };
global.WebSocket.OPEN = 1;
global.ResizeObserver = class { observe() {} };
global.Terminal = class { open() {} resize() {} focus() {} write() {} onData() {} };
const timers = [];
global.window = {setTimeout: (callback, ms) => timers.push({callback, ms}), clearTimeout: () => {}};
eval(""" + json.dumps(page) + r""");
const seen = [];
const close = code => { sockets[sockets.length - 1].onclose({code}); seen.push([status.textContent, timers.length]); };
close(4408);
timers[0].callback();
close(4409);
timers[1].callback();
close(4410);
close(1000);
console.log(JSON.stringify({urls: sockets.map(socket => socket.url), seen,
                            delays: timers.map(timer => timer.ms)}));
""")
    assert result["urls"][0] == "ws://testserver/ws/workers/wrk_1/terminal?run=run_a"
    assert all(url == result["urls"][0] for url in result["urls"])
    assert [text for text, _ in result["seen"]] == [
        "Waiting for this run to start…",
        "This run’s live session ended.",
        "This run has ended. Its saved output is shown.",
        "Terminal disconnected. Unlock again if your session expired, then reconnect.",
    ]
    # Waiting and an ended live session reconnect by themselves; shown output does not.
    assert result["delays"] == [1500, 2000]
    assert [count for _, count in result["seen"]] == [1, 2, 2, 2]
