"""Exercise the real Watch navigation and deferred Library display state."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'
SOURCE = (STATIC / 'watch.js').read_text()


def function(name):
    match = re.search(r'^function ' + name + r'\(.*?^}', SOURCE, re.M | re.S)
    assert match, name
    return match.group(0)


def node(script):
    if not shutil.which('node'):
        pytest.skip('Node required')
    result = subprocess.run(['node', '--input-type=module'], input="import assert from 'node:assert/strict';\n" + script,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


FRAME_HARNESS = r'''
const element=()=>({hidden:false,dataset:{},textContent:'',setAttribute(){}});
const overlay=element(),stage=element(),overlayLabel=element(),overlayTitle=element(),overlayDetail=element(),stageResultText=element();
const surfaceTerminalButton=element(),surfaceDesktopButton=element(),openExternal=element(),resultMarkdown={render:t=>t};
let now=1000; Date.now=()=>now;
let activeSurface='terminal',currentDisplayState='running',currentWorkerState='ready',currentDesktopAvailable=true,currentNoRunActivity=false;
let currentTerminalUrl='https://example.invalid/terminal?run=one',currentDesktopUrl='https://example.invalid/desktop',currentSummary='',currentResultText='',currentRunState='running';
let lastAttachedUrl='',lastAttachedFilePreviewKey='',currentFilePreviewKey='',currentFilePreviewUrl='',currentDeliverable=null;
let frameReady=false,attachStartedAt=0,retryTimers=[];
const TERMINAL_ATTENTION_STATES=new Set(['failed','cancelled','interrupted']);
const writes=[],timers=new Map();let nextTimer=0;
const window={setTimeout(fn,delay){timers.set(++nextTimer,{fn,delay});return nextTimer;},clearTimeout:id=>timers.delete(id)};
const frame={_src:'about:blank',set src(value){this._src=value;writes.push(value);},get src(){return this._src;},
 contentDocument:{readyState:'complete',location:{href:'about:blank'}},addEventListener(name,fn){this[name]=fn;}};
const terminalLog={open:true,addEventListener(name,fn){this[name]=fn;}};
'''


def frame_checks(body):
    functions = '\n'.join(function(name) for name in ('clearRetryTimers', 'forceReloadFrame', 'scheduleReconnects',
        'attachView', 'filePreviewUrl', 'isFilePreviewUrl', 'currentSurfaceUrl', 'clearAttachedView',
        'syncMenuLabels', 'showsSavedRunOutput', 'showResult', 'setSurface', 'setOverlay'))
    load = SOURCE[SOURCE.index("terminalLog?.addEventListener('toggle'"):SOURCE.index("menuToggle.addEventListener('click'")]
    node(FRAME_HARNESS + functions + load + body)


def test_force_reconnect_navigates_same_url_but_polling_keeps_current_session():
    frame_checks(r'''
setSurface('terminal');assert.equal(writes.length,1);assert.equal(timers.size,2);
setSurface('terminal');assert.equal(writes.length,1);
frame.contentDocument.location.href=frame.src;frame.load();assert.equal(frameReady,true);assert.equal(timers.size,0);
setSurface('terminal',{force:true});assert.equal(writes.length,2);assert.equal(frameReady,false);
assert.equal(overlay.hidden,false);assert.equal(timers.size,2);
const retry=[...timers.values()][0];retry.fn();assert.equal(writes.length,3);
assert.ok(writes.every(url=>url===currentTerminalUrl),'Recovery never queues a blank navigation');
const oldRetry=[...timers.values()][1];setSurface('desktop');const count=writes.length;
oldRetry.fn();assert.equal(writes.length,count,'Old retry cannot replace a newer surface');
clearAttachedView();oldRetry.fn();assert.equal(frame.src,'about:blank');assert.equal(timers.size,0);
''')


def test_blank_prior_loading_inaccessible_or_redirected_document_cannot_mark_current_surface_ready():
    frame_checks(r'''
setSurface('terminal');
for(const href of ['about:blank','https://example.invalid/terminal?run=old','https://example.invalid/login']){
 frame.contentDocument.location.href=href;frame.load();assert.equal(frameReady,false);assert.equal(overlay.hidden,false);assert.equal(timers.size,2);
}
frame.contentDocument=null;frame.load();assert.equal(frameReady,false);
frame.contentDocument={readyState:'loading',location:{href:frame.src}};frame.load();assert.equal(frameReady,false);
frame.contentDocument.readyState='complete';frame.load();assert.equal(frameReady,true);assert.equal(overlay.hidden,true);assert.equal(timers.size,0);
clearAttachedView();frame.contentDocument.location.href=currentTerminalUrl;frame.load();assert.equal(frameReady,false);
''')


def test_unloaded_completed_surface_and_timeout_remain_truthful_and_recover():
    frame_checks(r'''
currentDisplayState=currentRunState='completed';setSurface('terminal');assert.equal(overlay.hidden,false);
now=14000;setSurface('terminal');assert.equal(overlay.hidden,false);assert.equal(overlayLabel.textContent,'View unavailable');
assert.match(overlayDetail.textContent,/reconnect/);
setSurface('terminal',{force:true});assert.equal(overlayLabel.textContent,'Connecting');
frame.contentDocument.location.href=frame.src;frame.load();assert.equal(overlay.hidden,true);
currentDisplayState=currentRunState='failed';currentTerminalUrl='https://example.invalid/terminal?run=one&phase=ended';
setSurface('terminal');frame.contentDocument.location.href=frame.src;frame.load();
assert.equal(overlay.hidden,false);assert.equal(stage.dataset.overlayMode,'banner');
assert.equal(stage.dataset.overlayActive,'false','Loaded saved output keeps its failure notice');
''')


def test_hidden_disclosure_defers_navigation_then_loads_when_open_without_reloading_a_healthy_view():
    frame_checks(r'''
terminalLog.open=false;currentDisplayState=currentRunState='completed';setSurface('terminal');
assert.deepEqual(writes,[]);assert.equal(timers.size,0);assert.equal(attachStartedAt,0);
terminalLog.open=true;terminalLog.toggle();assert.deepEqual(writes,[currentTerminalUrl]);
assert.equal(overlay.hidden,false);assert.equal(timers.size,2);
frame.contentDocument.location.href=frame.src;frame.load();assert.equal(frameReady,true);
terminalLog.open=false;terminalLog.toggle();terminalLog.open=true;terminalLog.toggle();
assert.equal(writes.length,1,'Reopening a healthy view must preserve its native session');
assert.equal(overlay.hidden,true);
// Closing during a pending navigation cancels hidden retries. Opening recovers that exact view.
currentTerminalUrl+='&attempt=next';setSurface('terminal');terminalLog.open=false;terminalLog.toggle();
assert.equal(timers.size,0);assert.equal(attachStartedAt,0);const before=writes.length;
terminalLog.open=true;terminalLog.toggle();assert.equal(writes.length,before+1);
frame.contentDocument.location.href='https://example.invalid/login';frame.load();
assert.equal(frameReady,false,'Deferred recovery must still reject an auth redirect');
''')


LIBRARY_HARNESS = r'''
let libraryManager=false,owner='',pendingLibraryTab='',requestedLibraryEntry=false,requestedLibraryPrepared=false,libraryAddedEpoch=0;
let librarySnapshot=null,listedFileActivityKey='',latestFileActivityKey='current',stored='saved';
const writes=[],reads=[];const currentLibraryOwnerScope=()=>libraryManager?owner:'';
const localStorage={getItem:()=>stored,setItem:(k,v)=>writes.push(v)};
const panels=Object.fromEntries(['files','added','saved'].map(name=>[`watch-library-${name}`,{hidden:true}]));
const tabs=['files','added','saved'].map(name=>({id:`watch-library-tab-${name}`,hidden:false,attrs:{},
 setAttribute(k,v){this.attrs[k]=v;},getAttribute:()=>`watch-library-${name}`,focus(){}}));
const addedList={replaceChildren(){reads.push('clear');}};
const document={querySelectorAll:s=>s.includes('watch-library-tabs')?tabs:[],
 querySelector:()=>tabs.find(t=>t.attrs['aria-selected']==='true'),
 getElementById:id=>id==='watch-library-added-list'?addedList:tabs.find(t=>t.id===id)||panels[id]};
const watchLibrary={setAccess(allowed,scope){reads.push(['access',allowed,scope]);},setPickAvailable(){},refresh(){reads.push(['saved',currentLibraryOwnerScope()]);}};
const refreshAddedLibrary=()=>reads.push(['added',currentLibraryOwnerScope()]);
const workspaceFiles={setKeepForReuseAccess(){},open(){reads.push('files');}};
const control=()=>({addEventListener(name,fn){this[name]=fn;},setAttribute(){},focus(){}});
const filesPanel={hidden:true},filesToggle=control(),filesClose=control();
const current=()=>tabs.find(t=>t.attrs['aria-selected']==='true')?.id;
'''


def library_checks(body):
    start=SOURCE.index("filesToggle?.addEventListener('click'")
    handlers=SOURCE[start:SOURCE.index("filesAttach?.addEventListener",start)]
    node(LIBRARY_HARNESS + '\n'.join(function(name) for name in ('libraryTargetClosed','syncLibraryAccess','selectLibraryTab')) + handlers + body)


def test_saved_display_intent_waits_for_verified_owner_then_restores_without_reopening():
    library_checks(r'''
filesToggle.click();assert.equal(current(),'watch-library-tab-files');assert.equal(pendingLibraryTab,'saved');
libraryManager=true;syncLibraryAccess();assert.equal(current(),'watch-library-tab-files');
assert.ok(!reads.some(r=>r[0]==='saved'));
owner='owner-a';syncLibraryAccess();assert.equal(current(),'watch-library-tab-saved');
assert.deepEqual(reads.filter(r=>r[0]==='saved'),[['saved','owner-a']]);assert.equal(pendingLibraryTab,'');
syncLibraryAccess();assert.equal(reads.filter(r=>r[0]==='saved').length,1);assert.deepEqual(writes,[]);
libraryManager=false;owner='';syncLibraryAccess();assert.equal(current(),'watch-library-tab-files');assert.equal(tabs[2].hidden,true);
''')


def test_explicit_choice_close_signed_view_and_deep_link_do_not_leak_or_override_intent():
    library_checks(r'''
filesToggle.click();selectLibraryTab('files',false,true);libraryManager=true;owner='owner-a';syncLibraryAccess();
assert.equal(current(),'watch-library-tab-files');assert.deepEqual(writes,['files']);
filesClose.click();libraryManager=false;owner='';filesToggle.click();assert.equal(pendingLibraryTab,'saved');
filesClose.click();libraryManager=true;owner='owner-b';syncLibraryAccess();assert.equal(filesPanel.hidden,true);assert.equal(current(),'watch-library-tab-files');
// Shared/signed views never gain a manager scope, even if a draft scope exists.
libraryManager=false;owner='unusable-owner';filesToggle.click();syncLibraryAccess();
assert.equal(current(),'watch-library-tab-files');assert.ok(!reads.some(r=>r[0]==='saved'));
// An explicit Added-here deep link takes precedence over remembered display only.
requestedLibraryEntry=true;libraryManager=true;owner='owner-c';syncLibraryAccess();
assert.equal(current(),'watch-library-tab-added');assert.equal(pendingLibraryTab,'');
assert.deepEqual(reads.filter(r=>r[0]==='added'),[['added','owner-c']]);
''')


def test_live_sizing_does_not_depend_on_details_anonymous_content_height():
    css=(STATIC/'styles.css').read_text()
    assert '.watch-terminal-log { position: relative; min-height: 0; }' in css
    assert '.watch-app:not([data-result-first="true"]) > .watch-terminal-log > .watch-stage { position: absolute; inset: 0; }' in css
    assert '.watch-terminal-log > .watch-stage { height: 100%; }' not in css
    assert '.watch-app[data-result-first="true"] > .watch-terminal-log[open] > .watch-stage { height: 36vh; min-height: 160px; }' in css
