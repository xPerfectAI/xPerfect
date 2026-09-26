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
            "      String(data.latest_run?.state || ''), String(data.latest_run?.active_attempt_id || '')));") in source


def test_watch_and_terminal_pages_load_the_exact_run_script_versions():
    # Browsers keep a cached script for the same ?v= token, so a changed script needs a new token.
    watch_html = (STATIC / "watch.html").read_text()
    assert 'src="/static/watch.js?v=20260926run2"' in watch_html
    assert 'href="/static/styles.css?v=20260926run2"' in watch_html
    assert 'src="/static/terminal.js?v=20260926run2"' in (STATIC / "terminal.html").read_text()


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
        "Saved output closed. Select Reconnect to view it again.",
        "Terminal disconnected. Unlock again if your session expired, then reconnect.",
    ]
    # Waiting and an ended live session reconnect by themselves; shown output does not.
    assert result["delays"] == [1500, 2000]
    assert [count for _, count in result["seen"]] == [1, 2, 2, 2]


@node
def test_terminal_url_follows_a_retry_of_the_same_run():
    source = WATCH_JS.read_text()
    result = _node("\n".join([
        _function(source, "terminalRunPhase"),
        _function(source, "terminalViewUrl"),
        "const url = (attempt) => terminalViewUrl('', 'wrk_1', 'run_a', 'running', attempt);",
        "console.log(JSON.stringify({first: url('att_1'), again: url('att_1'), retry: url('att_2'), none: url('')}));",
    ]))
    assert result["first"] == "/ui/workers/wrk_1/terminal?run=run_a&attempt=att_1&phase=live"
    assert result["again"] == result["first"]
    assert result["retry"] != result["first"] and "attempt=att_2" in result["retry"]
    assert result["none"] == "/ui/workers/wrk_1/terminal?run=run_a&phase=live"


@node
def test_failed_cancelled_and_interrupted_runs_keep_their_saved_output_under_the_notice():
    """Runs the real setSurface/setOverlay flow: a run that did not complete keeps its saved
    terminal output attached below a compact notice instead of an opaque cover."""
    source = WATCH_JS.read_text()
    functions = "\n".join(_function(source, name) for name in (
        "clearRetryTimers", "forceReloadFrame", "scheduleReconnects", "attachView", "filePreviewUrl",
        "isFilePreviewUrl", "currentSurfaceUrl", "clearAttachedView", "syncMenuLabels", "showsSavedRunOutput",
        "setSurface", "setOverlay"))
    result = _node(r"""
const TERMINAL_ATTENTION_STATES = new Set(['failed', 'cancelled', 'interrupted']);
const element = () => ({hidden: true, textContent: '', dataset: {}});
const frame = {src: ''}; const overlay = element(); const stage = element();
const overlayLabel = element(); const overlayTitle = element(); const overlayDetail = element();
const stageResultText = element(); const surfaceTerminalButton = element(); const surfaceDesktopButton = element();
const openExternal = element(); const watchSignIn = element();
global.window = {setTimeout: () => 0, clearTimeout: () => {}};
let activeSurface = 'terminal', currentDisplayState = '', currentWorkerState = 'ready', currentDesktopAvailable = false;
let currentTerminalUrl = '', currentDesktopUrl = '', currentSummary = '', currentResultText = '', currentRunState = '';
let lastAttachedUrl = '', lastAttachedFilePreviewKey = '', currentFilePreviewKey = '', currentFilePreviewUrl = '';
let currentDeliverable = null, attachStartedAt = 0, frameReady = true, retryTimers = [];
""" + functions + r"""
const view = (state, url, surface = 'terminal') => {
  currentDisplayState = state; currentRunState = state; currentTerminalUrl = url; frame.src = '';
  lastAttachedUrl = ''; frameReady = true; attachStartedAt = 0;
  setSurface(surface, {force: true});
  return {src: frame.src, overlayHidden: overlay.hidden, mode: stage.dataset.overlayMode || '',
          covered: stage.dataset.overlayActive, title: overlayTitle.textContent, detail: overlayDetail.textContent};
};
const ended = (state) => `/ui/workers/wrk_1/terminal?run=run_a&attempt=att_1&phase=ended#${state}`;
console.log(JSON.stringify({
  failed: view('failed', ended('failed')), cancelled: view('cancelled', ended('cancelled')),
  interrupted: view('interrupted', ended('interrupted')), noRun: view('failed', '/ui/workers/wrk_1/terminal'),
  completed: view('completed', ended('completed')), desktop: view('failed', ended('failed'), 'desktop'),
}));
""")
    for state in ("failed", "cancelled", "interrupted"):
        shown = result[state]
        assert shown["src"].endswith("#" + state), shown
        assert shown["overlayHidden"] is False and shown["mode"] == "banner" and shown["covered"] == "false"
        assert shown["title"] == "Workspace needs attention" and "saved terminal output is below" in shown["detail"]
    assert result["noRun"]["src"] in ("", "about:blank") and result["noRun"]["covered"] == "true"
    assert result["noRun"]["mode"] == ""
    assert result["completed"]["src"].endswith("#completed") and result["completed"]["overlayHidden"] is True
    assert result["desktop"]["covered"] == "true" and result["desktop"]["mode"] == ""


@node
def test_terminal_page_says_the_complete_saved_output_is_shown_for_an_ended_run():
    page = (STATIC / "terminal.js").read_text()
    result = _node(r"""
const status = {textContent: ''};
global.document = {
  getElementById: id => id === 'terminal-status' ? status
    : id === 'terminal' ? {clientWidth: 800, clientHeight: 400} : {addEventListener: () => {}},
  querySelector: () => ({content: 'nonce'}), querySelectorAll: () => [], createElement: () => ({}), cookie: '',
};
global.location = {pathname: '/ui/workers/wrk_1/terminal', search: '?run=run_a&attempt=att_1&phase=ended',
                   protocol: 'http:', host: 'testserver'};
const sockets = [];
global.WebSocket = class { constructor(url) { this.url = url; sockets.push(this); } send() {} close() {} };
global.WebSocket.OPEN = 1;
global.ResizeObserver = class { observe() {} };
global.Terminal = class { open() {} resize() {} focus() {} write() {} onData() {} };
global.window = {setTimeout: () => 0, clearTimeout: () => {}};
eval(""" + json.dumps(page) + r""");
sockets[0].onopen();
const opened = status.textContent;
sockets[0].onclose({code: 4410});
console.log(JSON.stringify({url: sockets[0].url, opened, closed: status.textContent}));
""")
    assert result["url"] == "ws://testserver/ws/workers/wrk_1/terminal?run=run_a"
    assert result["opened"] == "This run has ended. Its complete saved output is shown; scroll up for earlier output."
    assert result["closed"] == "Saved output closed. Select Reconnect to view it again."
