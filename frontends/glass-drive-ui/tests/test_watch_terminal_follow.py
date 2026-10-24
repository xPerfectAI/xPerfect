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
    # Reattach at the transition from live root work to saved output while children finish.
    assert result["claimed"] == result["queued"]
    assert result["settling"] != result["running"] and "phase=settling" in result["settling"]
    assert result["running"] != result["queued"] and "phase=live" in result["running"]
    assert result["failed"] == result["completed"] != result["running"] and "phase=ended" in result["completed"]
    assert result["next"] != result["queued"] and "run=run_b" in result["next"]


def test_watch_attaches_the_terminal_for_the_latest_run_and_its_state():
    source = WATCH_JS.read_text()
    assert ("currentTerminalUrl = withAuth(terminalViewUrl(runtimeBase, workerId, String(data.latest_run?.run_id || ''),\n"
            "      String(data.latest_run?.state || ''), String(data.latest_run?.active_attempt_id || '')));") in source


def test_watch_and_terminal_pages_load_the_exact_run_script_versions():
    # Browsers keep a cached script for the same ?v= token, so a changed script needs a new token.
    watch_html = (STATIC / "watch.html").read_text()
    assert 'src="/static/watch.js?v=20261024polish1"' in watch_html
    # Shared page CSS uses the current application asset version, not a Watch-only copy.
    main_html = (STATIC / "index.html").read_text()
    for asset in ("theme.css", "styles.css"):
        pattern = rf'href="(/static/{re.escape(asset)}\?v=[A-Za-z0-9]+)"'
        watch_asset = re.search(pattern, watch_html)
        main_asset = re.search(pattern, main_html)
        assert watch_asset and main_asset, asset
        assert watch_asset.group(1) == main_asset.group(1)
    assert 'src="/static/terminal.js?v=20261024polish1"' in (STATIC / "terminal.html").read_text()


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
@pytest.mark.parametrize("phase", ["ended", "live", "waiting", "settling"])
def test_saved_terminal_hides_and_refuses_worker_actions_but_reconnect_stays_read_only(phase):
    page = (STATIC / "terminal.js").read_text()
    script = r'''
const assert=require('node:assert/strict'),calls=[],sockets=[];
const makeButton=action=>({dataset:{action},hidden:false,disabled:false,handlers:{},addEventListener(type,fn){this.handlers[type]=fn;}});
const actions=['resume','pause','interrupt'].map(makeButton),reconnect=makeButton('reconnect'),status={textContent:''};
global.document={cookie:'glasshive_csrf=synthetic-token',
 getElementById:id=>id==='terminal-status'?status:id==='terminal'?{clientWidth:800,clientHeight:400}:reconnect,
 querySelector:()=>({content:'nonce'}),querySelectorAll:()=>actions,createElement:()=>({})};
global.location={pathname:'/ui/workers/worker-exact/terminal',search:'?run=run-exact&phase='+PHASE,protocol:'http:',host:'testserver'};
global.WebSocket=class{constructor(url){this.url=url;sockets.push(this);}send(){}close(){}};WebSocket.OPEN=1;
global.ResizeObserver=class{observe(){}};global.Terminal=class{open(){}resize(){}focus(){}write(){}onData(){}};
global.window={setTimeout:()=>0,clearTimeout(){}};
global.fetch=async(url,options)=>{calls.push({url,options});return {ok:true};};
PAGE
(async()=>{
 const readOnly=PHASE==='ended'||PHASE==='settling';
 sockets[0].onopen();
 for(const button of actions){
  assert.equal(button.hidden,readOnly);
  // Invoke the actual handler even when hidden: the guard must refuse a stale/programmatic click.
  await button.handlers.click();assert.equal(button.disabled,false);
 }
 if(readOnly)assert.equal(calls.length,0);
 else{
  assert.equal(calls.length,3);
  for(let i=0;i<3;i++){
   assert.equal(calls[i].url,'/api/worker/worker-exact/action/'+actions[i].dataset.action);
   assert.equal(calls[i].options.method,'POST');assert.equal(calls[i].options.headers['X-GlassHive-CSRF'],'synthetic-token');
  }
 }
 const before=sockets.length,mutations=calls.length;reconnect.handlers.click();
 assert.equal(sockets.length,before+1);assert.equal(calls.length,mutations);
 assert.equal(sockets.at(-1).url,'ws://testserver/ws/workers/worker-exact/terminal?run=run-exact');
 // A stale tab may have opened before this exact run ended. Only the current
 // socket's saved-output close can make its worker controls permanently read-only.
 const current=sockets.at(-1),previous=sockets.at(-2);
 previous.onclose({code:4410,reason:'Run output shown'});
 assert.ok(actions.every(button=>button.hidden===readOnly));
 current.onclose({code:4410,reason:'Run output shown'});
 assert.ok(actions.every(button=>button.hidden));
 const beforeSavedClick=calls.length;
 for(const button of actions)await button.handlers.click();
 assert.equal(calls.length,beforeSavedClick);
 reconnect.handlers.click();
 assert.equal(sockets.at(-1).url,'ws://testserver/ws/workers/worker-exact/terminal?run=run-exact');
 assert.ok(actions.every(button=>button.hidden));assert.equal(calls.length,beforeSavedClick);
 console.log(JSON.stringify({passed:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''.replace("PHASE", json.dumps(phase)).replace("PAGE", page)
    assert _node(script) == {"passed": True}


def test_terminal_worker_actions_start_hidden_before_any_script_or_connection():
    from html.parser import HTMLParser

    class Buttons(HTMLParser):
        def __init__(self):
            super().__init__()
            self.actions = {}

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "button" and "data-action" in values:
                self.actions[values["data-action"]] = "hidden" in values

    buttons = Buttons()
    buttons.feed((STATIC / "terminal.html").read_text())
    assert buttons.actions == {"resume": True, "pause": True, "interrupt": True}


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
        "showResult", "setSurface", "setOverlay"))
    result = _node(r"""
const TERMINAL_ATTENTION_STATES = new Set(['failed', 'cancelled', 'interrupted']);
const element = () => ({hidden: true, textContent: '', dataset: {}, setAttribute(name,value){this[name]=String(value);}});
const terminalLog = {open: true}; const frame = {src: ''}; const overlay = element(); const stage = element();
const overlayLabel = element(); const overlayTitle = element(); const overlayDetail = element();
const stageResultText = element(); const surfaceTerminalButton = element(); const surfaceDesktopButton = element();
const openExternal = element(); const watchSignIn = element();
const resultMarkdown = {render: (text) => text};
global.window = {setTimeout: () => 0, clearTimeout: () => {}};
let activeSurface = 'terminal', currentDisplayState = '', currentWorkerState = 'ready', currentDesktopAvailable = false, currentNoRunActivity = false;
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
    assert result["completed"]["src"].endswith("#completed") and result["completed"]["overlayHidden"] is False
    assert result["desktop"]["covered"] == "true" and result["desktop"]["mode"] == ""


@node
def test_no_run_activity_requires_a_confirmed_idle_host_without_a_live_surface():
    source = WATCH_JS.read_text()
    result = _node(_function(source, "noRunActivityFor") + r"""
const idle = {worker:{state:'ready'},latest_run:null,active_run:null,
  runtime_details:{mode:'host-computer',runtime:'host-native',execution_mode:'host'}};
const positive = ['ready','paused','idle','idle_terminated','stopped'].map(state =>
  noRunActivityFor({...idle,worker:{state}}));
const negative = [
  null, {}, {...idle,latest_run:undefined}, {...idle,active_run:undefined},
  {...idle,latest_run:{run_id:'old-run',state:'completed'}},
  {...idle,latest_run:{state:'completed'}},
  {...idle,active_run:{run_id:'active-run',state:'running'}},
  ...['created','starting','resuming','queued','running','failed','terminated'].map(state =>
    ({...idle,worker:{state}})),
  {...idle,worker:{state:'ready',close_state:'termination_failed'}},
  {...idle,runtime_details:{}},
  ...['unknown','unavailable','workstation-desktop'].map(mode =>
    ({...idle,runtime_details:{mode}})),
  ...[{pid:42},{view_available:true},{view_url:'/existing-desktop'}].map(surface =>
    ({...idle,runtime_details:{...idle.runtime_details,...surface}})),
].map(noRunActivityFor);
console.log(JSON.stringify({positive,negative}));
""")
    assert all(result["positive"])
    assert not any(result["negative"])


@node
def test_ready_workspace_without_a_run_never_attaches_a_fake_live_session():
    source = WATCH_JS.read_text()
    functions = "\n".join(_function(source, name) for name in (
        "clearRetryTimers", "forceReloadFrame", "scheduleReconnects", "attachView", "filePreviewUrl",
        "isFilePreviewUrl", "currentSurfaceUrl", "clearAttachedView", "syncMenuLabels", "showsSavedRunOutput",
        "showResult", "setSurface", "setOverlay", "noRunActivityFor"))
    result = _node(r"""
const TERMINAL_ATTENTION_STATES = new Set(['failed', 'cancelled', 'interrupted']);
const element = () => ({hidden:true,textContent:'',dataset:{},setAttribute(name,value){this[name]=String(value);}});
const terminalLog={open:true};
const frame={src:''},overlay=element(),stage=element(),overlayLabel=element(),overlayTitle=element(),
  overlayDetail=element(),stageResultText=element(),surfaceTerminalButton=element(),surfaceDesktopButton=element(),openExternal=element();
const resultMarkdown={render:text=>text};
let timers=0;global.window={setTimeout:()=>{timers+=1;return timers;},clearTimeout:()=>{}};
let activeSurface='terminal',currentDisplayState='',currentWorkerState='',currentDesktopAvailable=false,currentNoRunActivity=false;
let currentTerminalUrl='/ui/workers/synthetic/terminal',currentDesktopUrl='',currentSummary='',currentResultText='',currentRunState='';
let lastAttachedUrl='',lastAttachedFilePreviewKey='',currentFilePreviewKey='',currentFilePreviewUrl='';
let currentDeliverable=null,attachStartedAt=0,frameReady=false,retryTimers=[];
""" + functions + r"""
const idle={worker:{state:'ready'},latest_run:null,active_run:null,runtime_details:{mode:'host-computer'}};
const view=()=>({src:frame.src,title:overlayTitle.textContent,label:overlayLabel.textContent,
  detail:overlayDetail.textContent,hidden:overlay.hidden,covered:stage.dataset.overlayActive,timers});
currentDisplayState=currentWorkerState='ready';currentNoRunActivity=noRunActivityFor(idle);
setSurface('terminal',{force:true});const ready=view();
Date.now=()=>120000;setSurface('terminal');setOverlay('ready');const polled=view();
currentDisplayState=currentWorkerState='paused';currentNoRunActivity=noRunActivityFor({...idle,worker:{state:'paused'}});
setSurface('terminal');const paused=view();
currentDisplayState=currentWorkerState='idle';currentNoRunActivity=noRunActivityFor({...idle,worker:{state:'idle'}});
setSurface('terminal');const stopped=view();
currentDisplayState=currentRunState='queued';currentWorkerState='ready';
currentNoRunActivity=noRunActivityFor({...idle,latest_run:{run_id:'actual-run',state:'queued'}});
currentTerminalUrl='/ui/workers/synthetic/terminal?run=actual-run&phase=waiting';
setSurface('terminal');const queued=view();
currentDisplayState=currentRunState='completed';currentTerminalUrl='/ui/workers/synthetic/terminal?run=actual-run&phase=ended';
setSurface('terminal');const completed=view();
currentDisplayState=currentRunState='failed';setSurface('terminal');const failed=view();
console.log(JSON.stringify({ready,polled,paused,stopped,queued,completed,failed}));
""")
    for state in ("ready", "polled", "paused", "stopped"):
        shown = result[state]
        assert shown["src"] == "about:blank" and shown["timers"] == 0
        assert not shown["hidden"] and shown["covered"] == "true"
        assert "Connecting" not in shown["label"] and "Opening live" not in shown["title"]
    assert result["ready"]["label"] == "Workspace ready"
    assert result["ready"]["title"] == "What should I work on?"
    assert "Send a message below" in result["ready"]["detail"]
    assert "Library" in result["ready"]["detail"]
    assert result["paused"]["title"] == "Workspace paused" and "Resume" in result["paused"]["detail"]
    assert "No work is running" in result["stopped"]["detail"]
    assert "last run is complete" not in result["stopped"]["detail"]
    assert "run=actual-run&phase=waiting" in result["queued"]["src"] and result["queued"]["timers"] == 2
    assert "run=actual-run&phase=ended" in result["completed"]["src"] and not result["completed"]["hidden"]
    assert "run=actual-run&phase=ended" in result["failed"]["src"] and result["failed"]["covered"] == "false"


@node
def test_terminal_page_status_for_an_ended_run_claims_only_what_the_runtime_reports():
    """Opening an ended run's terminal claims nothing about its output: the pager line or the
    no-output notice says what was kept, and the close reason tells the two ends apart."""
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
sockets[0].onclose({code: 4410, reason: 'No saved output'});
const none = status.textContent;
sockets[0].onclose({code: 4410, reason: 'Run output shown'});
console.log(JSON.stringify({url: sockets[0].url, opened, none, shown: status.textContent}));
""")
    assert result["url"] == "ws://testserver/ws/workers/wrk_1/terminal?run=run_a"
    assert result["opened"] == "This run has ended."
    assert result["none"] == "This run has ended. No terminal output was kept for it."
    assert result["shown"] == "Saved output closed. Select Reconnect to view it again."


@node
def test_a_completed_result_renders_its_markdown_safely_and_reads_from_the_top():
    """The final answer's markdown reads as formatted text, raw HTML stays text, links open
    beside the workspace, and a shown result marks the stage to read from the top."""
    source = WATCH_JS.read_text()
    start = source.index("const resultMarkdown = createResultMarkdown()")
    setup = source[start:source.index(";", start) + 1]
    markdown = (STATIC / "result-markdown.js").as_uri()
    result = _node(
        "(async () => {"
        f"const {{createResultMarkdown}} = await import({json.dumps(markdown)});"
        + setup
        + "const overlay = {dataset: {}}; const stageResultText = {hidden: true, innerHTML: ''};\n"
        + _function(source, "showResult")
        + r"""
showResult('- **Quantity:** 6 books\n\n<img src=x onerror=alert(1)> [terms](https://example.test/terms)');
const shown = {html: stageResultText.innerHTML, hidden: stageResultText.hidden, result: overlay.dataset.result};
showResult('');
console.log(JSON.stringify({shown, cleared: {html: stageResultText.innerHTML, hidden: stageResultText.hidden, result: overlay.dataset.result}}));
})()""")
    html = result["shown"]["html"]
    assert "<li><strong>Quantity:</strong> 6 books</li>" in html and "**" not in html
    assert "<img" not in html and "&lt;img" in html
    assert '<a href="https://example.test/terms" target="_blank" rel="noopener noreferrer">terms</a>' in html
    assert result["shown"]["hidden"] is False and result["shown"]["result"] == "true"
    assert result["cleared"] == {"html": "", "hidden": True, "result": "false"}


@node
def test_settling_terminal_reports_saved_output_without_claiming_the_job_ended():
    page = (STATIC / "terminal.js").read_text()
    script = r'''
const status={textContent:''},sockets=[];
global.document={cookie:'',getElementById:id=>id==='terminal-status'?status:id==='terminal'?{clientWidth:800,clientHeight:400}:{addEventListener(){}},querySelector:()=>({content:'nonce'}),querySelectorAll:()=>[],createElement:()=>({})};
global.location={pathname:'/ui/workers/worker/terminal',search:'?run=run_current&phase=settling',protocol:'http:',host:'testserver'};
global.WebSocket=class{constructor(url){this.url=url;sockets.push(this);}send(){}close(){}};WebSocket.OPEN=1;
global.ResizeObserver=class{observe(){}};global.Terminal=class{open(){}resize(){}focus(){}write(){}onData(){}};
global.window={setTimeout:()=>0,clearTimeout(){}};
PAGE
sockets[0].onopen();const opened=status.textContent;
sockets[0].onclose({code:4410,reason:'No saved output'});
console.log(JSON.stringify({opened,closed:status.textContent}));
'''.replace('PAGE',page)
    result = _node(script)
    assert result['opened'] == 'Work is finishing. Showing the latest output.'
    assert result['closed'] == 'Work is finishing. No terminal output is available yet.'
