import asyncio
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from glass_drive_ui.server import create_app
import glass_drive_ui.server as server_module
from test_server import FakeRuntimeClient


class DesktopRuntime(FakeRuntimeClient):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def worker_live(self, worker_id, *, compact=False):
        value = super().worker_live(worker_id, compact=compact)
        query = urlencode({'password': 'synthetic', 'path': 'websockify', 'token': self.token})
        value['runtime_details']['view_url'] = 'http://runtime:7900/?' + query
        return value


class Upstream:
    def __init__(self):
        self.first = True
        self.closed = False

    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    def __aiter__(self): return self
    async def __anext__(self):
        if self.first:
            self.first = False
            return b'RFB 003.008\n'
        await asyncio.sleep(30)
        raise StopAsyncIteration
    async def send(self, message): pass
    async def close(self): self.closed = True


def test_attested_human_desktop_reconnects_after_ai_settles_but_close_wins():
    from pathlib import Path
    import subprocess
    root = Path(server_module.__file__).parent / 'static'
    source = (root / 'desktop.js').read_text()
    helpers = source[source.index('const ACTIVE_RUN_STATES ='):source.index('\nfunction settledWorkspaceStatus(')]
    script = helpers + r'''
const healthy={view_available:true,view_health:{healthy:true}};
for(const state of ['paused','ready']){
 const data={worker:{state},latest_run:{state:'completed'},runtime_details:healthy};
 if(isSettledWorkspaceState(data))throw Error('operator desktop cannot reconnect');
}
for(const state of ['terminating','termination_failed','terminated']){
 if(!isSettledWorkspaceState({worker:{state},runtime_details:healthy}))throw Error('Close must revoke desktop');
}
if(!isSettledWorkspaceState({worker:{state:'paused'},runtime_details:{view_available:false}}))throw Error('unavailable desktop is settled');
if(!isSettledWorkspaceState({worker:{state:'ready'},latest_run:{state:'completed'},runtime_details:{view_available:true,view_health:{healthy:false}}}))throw Error('unhealthy stale display cannot be live');
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    page = (root / 'desktop.html').read_text()
    assert 'This desktop is shared with this worker, including anything you open or sign in to.' in page
    assert 'id="desktop-screen"' in page


def test_existing_owner_proxy_forwards_only_standard_opaque_route_token(monkeypatch):
    captured = []
    def connect(url, **kwargs):
        captured.append(url)
        return Upstream()
    monkeypatch.setattr(server_module.websockets, 'connect', connect)
    token = 'a' * 64
    client = TestClient(create_app(runtime_client=DesktopRuntime(token)))
    with client.websocket_connect('/novnc/wrk_1/websockify') as websocket:
        assert websocket.receive_bytes() == b'RFB 003.008\n'
    assert captured == ['ws://runtime:7900/websockify?token=' + token]
    assert 'synthetic' not in captured[0]


def test_denied_desktop_clipboard_reports_unavailable_without_promising_a_click_fix():
    from pathlib import Path
    import subprocess
    source = Path(server_module.__file__).parent.joinpath('static/desktop.js').read_text()
    clipboard = source[source.index('function stopClipboardSync() {'):source.index('\nasync function connectDesktop()')]
    script = r'''
const assert = require('node:assert/strict');
const listeners = new Map(); let status = ''; let clipboardInterval = null;
let lastLocalClipboard = ''; let lastRemoteClipboard = ''; const pasted = [];
const rfb = {clipboardPasteFrom:text=>pasted.push(text)};
const window = {addEventListener:(name,fn)=>listeners.set(name,fn),
 removeEventListener:name=>listeners.delete(name),clearInterval:()=>{},setInterval:()=>1};
const navigator = {clipboard:{readText:async()=>{throw new DOMException('Denied','NotAllowedError')}}};
function setClipboardStatus(value){status=value;}
''' + clipboard + r'''
(async()=>{
 installClipboardSync(); await new Promise(resolve=>setImmediate(resolve));
 assert.equal(status,'Clipboard access unavailable. Check browser permissions.');
 await listeners.get('pointerdown')();
 assert.equal(status,'Clipboard access unavailable. Check browser permissions.');
 assert(!listeners.has('keydown'));
 listeners.get('paste')({clipboardData:{getData:()=> 'ordinary paste'}});
 assert.deepEqual(pasted,['ordinary paste']);
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('token', ['short', '../foreign', 'https://other.invalid/route', 'a' * 63])
def test_invalid_upstream_token_is_refused_before_connection(monkeypatch, token):
    monkeypatch.setattr(server_module.websockets, 'connect', lambda *a, **k: pytest.fail('must not forward'))
    client = TestClient(create_app(runtime_client=DesktopRuntime(token)))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect('/novnc/wrk_1/websockify'): pass
    assert exc.value.code == 1008


def test_watch_explicit_desktop_click_starts_once_without_resuming_paused_ai():
    import json
    from pathlib import Path
    import subprocess
    source = Path(server_module.__file__).parent.joinpath('static/watch.js').read_text()
    menu = source[source.index('function syncWatchMenuAvailability(data) {'):source.index('\nfunction syncMenuLabels()')]
    handler = source[source.index("surfaceDesktopButton.addEventListener('click', async () => {"):source.index("\nterminalLog?.addEventListener('toggle'")]
    overlay = source[source.index('function setOverlay(state, detail) {'):source.index('\nfunction ', source.index('function setOverlay(state, detail) {') + 1)]
    script = r'''
let clicked; let currentDesktopAvailable = false; let currentDesktopCanStart = true;
let currentNoRunActivity = false; let activeSurface = 'terminal'; let terminalResultPreferred = false;
let attachStartedAt = 0; let frameReady = true;
const calls = []; const selected = []; const app = {hidden:true};
const group = {hidden:false};
const buttons = Object.fromEntries(['codex','claude','openclaw','interrupt','terminate'].map(name=>[name,{hidden:false,disabled:false,closest:()=>group}]));
const surfaceDesktopButton = {hidden:true,disabled:false,addEventListener:(_,fn)=>clicked=fn};
const surfaceTerminalButton = {}; const openExternal = {}; const openTerminalLink = {};
const menu = {querySelector:selector=>buttons[selector.match(/data-action="([a-z]+)"/)[1]]};
const document = {getElementById:()=>app};
const STEERABLE_RUN_STATES = new Set(['running']); const TERMINAL_ATTENTION_STATES = new Set(['failed']);
const overlay = {hidden:false}; const stage = {dataset:{}};
function closeMenu(){}; function closeResultPanel(){};
function showActionFailure(error){throw error};
async function postAction(action,payload){calls.push({action,payload})};
async function refresh(){currentDesktopAvailable=true};
function setSurface(surface){selected.push(surface)};
''' + menu + handler + overlay + r'''
(async()=>{
 syncWatchMenuAvailability({worker:{state:'paused',profile:'codex-cli'},can_manage_workspace:true,
  runtime_details:{desktop_supported:true,sandbox_state:'paused',view_available:false,desktop_actions:['browser','focus_browser']}});
 if(surfaceDesktopButton.hidden || buttons.codex.hidden!==true)throw Error('wrong declared capabilities');
 await clicked(); await clicked();
 if(calls.length!==1 || calls[0].action!=='action:browser' || calls[0].payload.url!=='about:blank')throw Error('wrong startup');
 if(selected.length!==2 || selected.some(x=>x!=='desktop'))throw Error('not selected');
 activeSurface='desktop';setOverlay('paused');
 if(!overlay.hidden || stage.dataset.overlayActive!=='false')throw Error('paused AI obscures human browser');
 syncWatchMenuAvailability({worker:{state:'paused'},can_manage_workspace:false,
  runtime_details:{desktop_supported:true,view_available:true}});
 if(!surfaceDesktopButton.hidden)throw Error('read-only desktop offered');
 console.log(JSON.stringify({startup:calls.length,pausedAIResumes:calls.filter(x=>x.action.includes('resume')).length}));
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'startup': 1, 'pausedAIResumes': 0}
