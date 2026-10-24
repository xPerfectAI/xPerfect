"""Measured latest-run timing and keyboard access to the actual Watch menu."""
import base64
import json
from pathlib import Path
import re
import subprocess

import pytest


STATIC = Path(__file__).parents[1] / "src/glass_drive_ui/static"
NOW = "2026-10-24T12:00:00Z"


def node(code):
    result = subprocess.run(["node", "--input-type=module"], input=code,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr + result.stdout


def timing_checks(body):
    module = "data:text/javascript;base64," + base64.b64encode(
        (STATIC / "delivery-presenter.js").read_bytes()).decode()
    node("import assert from 'node:assert/strict';\n"
         f"const {{watchTimingModel}}=await import({json.dumps(module)});\n"
         f"const now=Date.parse({json.dumps(NOW)});\n" + body)


@pytest.mark.parametrize(("run", "expected"), [
    ({"state": "queued", "queued_at": "2026-10-24T11:59:48Z"},
     {"label": "Waiting", "value": "12s"}),
    ({"state": "running", "queued_at": "2026-10-01T01:00:00Z",
      "started_at": "2026-10-24T11:57:57Z"},
     {"label": "Running for", "value": "2m 3s"}),
    ({"state": "settling", "started_at": "2026-10-24T10:55:00Z"},
     {"label": "Running for", "value": "1h 5m"}),
    ({"state": "completed", "started_at": "2026-10-20T10:00:00Z",
      "ended_at": "2026-10-20T10:00:30Z"},
     {"label": "Run time", "value": "30s"}),
    ({"state": "failed", "started_at": "2026-10-24T11:58:00Z",
      "ended_at": "2026-10-24T11:59:03Z"},
     {"label": "Run time", "value": "1m 3s"}),
    ({"state": "interrupted", "started_at": "2026-10-24T11:59:00Z",
      "ended_at": "2026-10-24T11:59:00Z"},
     {"label": "Run time", "value": "0s"}),
    ({"state": "cancelled", "started_at": "2026-10-24T10:00:00Z",
      "ended_at": "2026-10-24T11:30:00Z"},
     {"label": "Run time", "value": "1h 30m"}),
])
def test_timing_uses_that_runs_recorded_lifecycle_only(run, expected):
    timing_checks(f"assert.deepEqual(watchTimingModel({json.dumps(run)},now),"
                  f"{json.dumps(expected)});")


def test_missing_invalid_reversed_or_future_times_hide_the_measurement():
    timing_checks("""
for(const run of [null,{},'not a run',{state:'paused'},
 {state:'queued',started_at:'2026-10-24T11:00:00Z'},
 {state:'queued',queued_at:'not a timestamp'},
 {state:'queued',queued_at:'2026-10-24T12:00:01Z'},
 {state:'running',queued_at:'2026-10-24T11:00:00Z'},
 {state:'running',started_at:'2026-10-24T12:00:01Z'},
 {state:'completed',started_at:'2026-10-24T11:00:00Z'},
 {state:'completed',started_at:'bad',ended_at:'2026-10-24T11:00:00Z'},
 {state:'completed',started_at:'2026-10-24T11:01:00Z',ended_at:'2026-10-24T11:00:00Z'}
])assert.equal(watchTimingModel(run,now),null,JSON.stringify(run));
assert.equal(watchTimingModel({state:'running',started_at:'2026-10-24T11:00:00Z'},NaN),null);
""")


def test_settled_duration_uses_server_interval_when_browser_clock_is_behind():
    timing_checks("""
for(const state of ['completed','failed','interrupted','cancelled']){
 assert.deepEqual(watchTimingModel({state,started_at:'2026-10-24T13:00:00Z',ended_at:'2026-10-24T13:00:03Z'},now),{label:'Run time',value:'3s'});
}
""")


def test_timing_has_no_previous_run_fallback_progress_or_estimate():
    timing_checks("""
const prior=watchTimingModel({run_id:'run-old',state:'completed',started_at:'2026-10-24T10:00:00Z',ended_at:'2026-10-24T10:00:07Z'},now);
assert.deepEqual(prior,{label:'Run time',value:'7s'});
assert.equal(watchTimingModel({run_id:'run-new',state:'running',queued_at:'2026-10-24T11:00:00Z'},now),null);
assert.equal(watchTimingModel({run_id:'run-new',state:'queued',started_at:'2026-10-24T10:00:00Z'},now),null);
const current=watchTimingModel({run_id:'run-new',state:'running',started_at:'2026-10-24T11:59:58.500Z'},now);
assert.deepEqual(current,{label:'Running for',value:'1s'});
assert.deepEqual(Object.keys(current).sort(),['label','value']);
""")


def actual_function(name, source):
    match = re.search(r"^function " + re.escape(name) + r"\(.*?^}", source, re.M | re.S)
    assert match, name
    return match.group(0)


def menu_checks(body):
    source = (STATIC / "watch.js").read_text()
    functions = "\n".join(actual_function(name, source) for name in
                          ("closeMenu", "syncWatchLayout", "watchMenuItems", "openMenu"))
    listeners = source[source.index("menuToggle.addEventListener('click'"):
                       source.index("window.addEventListener('resize', syncWatchLayout)")]
    document_listeners = source[source.index("document.addEventListener('click', (event)"):
                                source.index("document.addEventListener('keyup', (event)")]
    node("""
import assert from 'node:assert/strict';
class Node {
 constructor(){this.handlers={};this.hidden=false;this.disabled=false;this.visible=true;this.attributes={};}
 addEventListener(name,handler){this.handlers[name]=handler;}
 setAttribute(name,value){this.attributes[name]=value;}
 contains(other){for(let node=other;node;node=node.parent)if(node===this)return true;return false;}
 focus(){document.activeElement=this;}
 getClientRects(){return this.visible?[{}]:[];}
}
globalThis.Node=Node;
const menu=new Node(),menuToggle=new Node(),outside=new Node();menu.hidden=true;
const items=Array.from({length:5},()=>new Node());items.forEach(item=>item.parent=menu);
items[1].disabled=true;items[2].hidden=true;items[3].visible=false;
menu.querySelectorAll=selector=>{assert.equal(selector,'button[role="menuitem"]');return items;};
const document={activeElement:menuToggle,handlers:{},
 addEventListener(name,handler){this.handlers[name]=handler;},querySelector:()=>null};
const watchApp={style:{setProperty:()=>{}}};
let terminalResultPreferred=true;
const resultPanel=new Node(),resultToggle=new Node(),filesPanel=new Node();filesPanel.hidden=true;
let resultCloses=0,lastResultClose=null;const closeResultPanel=options=>{resultCloses+=1;lastResultClose=options;};
const setQueueModifierActive=()=>{};
function key(value){return {key:value,prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}};}
""" + functions + "\n" + listeners + "\n" + document_listeners + "\n" + body)


def test_menu_click_and_arrows_focus_only_visible_enabled_items():
    menu_checks("""
menuToggle.handlers.click();
assert.equal(menu.hidden,false);assert.equal(menuToggle.attributes['aria-expanded'],'true');
assert.equal(document.activeElement,items[0]);
for(const [pressed,expected] of [['ArrowDown',items[4]],['ArrowDown',items[0]],['ArrowUp',items[4]],['Home',items[0]],['End',items[4]]]){
 const event=key(pressed);menu.handlers.keydown(event);assert.equal(event.prevented,true);assert.equal(document.activeElement,expected);
}
const event=key('x');menu.handlers.keydown(event);assert.equal(event.prevented,false);assert.equal(document.activeElement,items[4]);
menuToggle.handlers.click();assert.equal(menu.hidden,true);assert.equal(menuToggle.attributes['aria-expanded'],'false');
""")


def test_menu_trigger_arrows_open_at_first_or_last_and_escape_restores_focus():
    menu_checks("""
for(const [pressed,expected] of [['ArrowDown',items[0]],['ArrowUp',items[4]]]){
 const event=key(pressed);menuToggle.handlers.keydown(event);
 assert.equal(event.prevented,true);assert.equal(menu.hidden,false);assert.equal(document.activeElement,expected);
 const escape=key('Escape');menu.handlers.keydown(escape);
 assert.equal(escape.prevented,true);assert.equal(escape.stopped,true);assert.equal(menu.hidden,true);
 assert.equal(document.activeElement,menuToggle);assert.equal(menuToggle.attributes['aria-expanded'],'false');
}
openMenu();document.handlers.keydown(key('Escape'));
assert.equal(menu.hidden,true);assert.equal(document.activeElement,menuToggle);
""")


def test_menu_tab_and_outside_click_close_without_trapping_or_stealing_focus():
    menu_checks("""
openMenu();const tab=key('Tab');menu.handlers.keydown(tab);
assert.equal(menu.hidden,true);assert.equal(tab.prevented,false);assert.equal(tab.stopped,false);
openMenu();const icon=new Node();icon.parent=menuToggle;
for(const target of [items[0],icon]){document.handlers.click({target});assert.equal(menu.hidden,false);}
document.handlers.click({target:{}});assert.equal(menu.hidden,false,'Non-node events do not dismiss controls');
outside.focus();document.handlers.click({target:outside});
assert.equal(menu.hidden,true);assert.equal(document.activeElement,outside);assert.equal(resultCloses,0);
""")


def test_menu_with_no_available_items_does_not_trap_keyboard_focus():
    menu_checks("""
items.forEach(item=>item.disabled=true);menuToggle.focus();openMenu();
assert.equal(menu.hidden,false);assert.equal(document.activeElement,menuToggle);
for(const pressed of ['ArrowDown','ArrowUp','Home','End']){
 const event=key(pressed);menu.handlers.keydown(event);assert.equal(event.prevented,false);
 assert.equal(document.activeElement,menuToggle);
}
menu.handlers.keydown(key('Escape'));assert.equal(menu.hidden,true);assert.equal(document.activeElement,menuToggle);
""")


def test_escape_closes_only_the_top_owned_surface_and_keeps_native_dialog_authority():
    menu_checks("""
resultPanel.hidden=false;terminalResultPreferred=true;
document.handlers.keydown(key('Escape'));
assert.equal(resultCloses,1,'A completed result can also be dismissed with Escape');
assert.deepEqual(lastResultClose,{restoreFocus:true});
openMenu();document.handlers.keydown(key('Escape'));assert.equal(resultCloses,1);
document.querySelector=()=>({open:true});document.handlers.keydown(key('Escape'));
assert.equal(resultCloses,1,'A native dialog owns its own Escape');
document.querySelector=()=>null;const handled=key('Escape');handled.defaultPrevented=true;
document.handlers.keydown(handled);assert.equal(resultCloses,1);
""")


def test_result_actions_show_only_the_declared_file_identity_as_safe_text():
    source = (STATIC / "watch.js").read_text()
    node("""
import assert from 'node:assert/strict';
const element=()=>({children:[],attributes:{},textContent:'',
 replaceChildren(){this.children=[];},appendChild(node){this.children.push(node);},
 setAttribute(name,value){this.attributes[name]=value;}});
const document={createElement:element},resultActions=element();
const libraryManager=false;
""" + actual_function("syncResultActions", source) + """
syncResultActions({kind:'file',label:'<report>.xlsx',open_url:'/open',download_url:'/download'});
assert.equal(resultActions.hidden,false);assert.equal(resultActions.children[0].textContent,'<report>.xlsx');
assert.equal(resultActions.children[0].className,'result-file-name');
assert.equal(resultActions.children[1].attributes['aria-label'],'Open file: <report>.xlsx');
assert.equal(resultActions.children[2].attributes['aria-label'],'Download file: <report>.xlsx');
assert.equal(resultActions.children[1].href,'/open');assert.equal(resultActions.children[2].href,'/download');
syncResultActions({kind:'file',workspace_path:'reports/answer.txt',open_url:'/open'});
assert.equal(resultActions.children[0].textContent,'reports/answer.txt');
syncResultActions({kind:'file',open_url:'/unlabelled'});
assert.equal(resultActions.children.length,1,'Never infer a file identity from its URL');
syncResultActions(null);assert.equal(resultActions.hidden,true);assert.equal(resultActions.children.length,0);
""")


def menu_availability_checks(body):
    source = (STATIC / "watch.js").read_text()
    node("""
import assert from 'node:assert/strict';
const names=['codex','claude','openclaw','interrupt','terminate'];
const buttons=Object.fromEntries(names.map(name=>[name,{hidden:false}]));
const workspaceGroup={hidden:false};buttons.terminate.closest=()=>workspaceGroup;
const appActions={hidden:false},surfaceDesktopButton={},surfaceTerminalButton={},openTerminalLink={},openExternal={};
let currentNoRunActivity=false;
let activeSurface='terminal';
const menu={querySelector:selector=>buttons[selector.match(/data-action="([^"]+)"/)[1]]};
const document={getElementById:id=>{assert.equal(id,'watch-app-actions');return appActions;}};
const STEERABLE_RUN_STATES=new Set(['queued','running','settling']);
const owner={can_manage_workspace:true,worker:{state:'ready',profile:'codex-cli'},latest_run:{state:'running'},runtime_details:{}};
""" + actual_function("syncWatchMenuAvailability", source) + "\n" + body)


def test_unknown_or_native_host_does_not_offer_fake_desktop_app_actions():
    menu_availability_checks("""
for(const data of [null,{},owner,{...owner,can_manage_workspace:'true'}]){
 syncWatchMenuAvailability(data);
 assert.equal(appActions.hidden,true);assert.equal(surfaceDesktopButton.hidden,true);
 for(const name of ['codex','claude','openclaw'])assert.equal(buttons[name].hidden,true);
 assert.equal(surfaceTerminalButton.hidden,false,'Keep the selected Live activity view discoverable');
 assert.equal(openTerminalLink.hidden,true);
}
syncWatchMenuAvailability(owner);assert.equal(buttons.interrupt.hidden,false);assert.equal(buttons.terminate.hidden,false);
syncWatchMenuAvailability(null);assert.equal(buttons.interrupt.hidden,true);assert.equal(buttons.terminate.hidden,true);
assert.equal(workspaceGroup.hidden,true);
""")


def test_interrupt_recovers_from_unavailable_only_for_authorized_active_work():
    menu_availability_checks("""
buttons.interrupt.disabled=true;syncWatchMenuAvailability(null);
assert.equal(buttons.interrupt.hidden,true);
syncWatchMenuAvailability(owner);
assert.equal(buttons.interrupt.hidden,false);assert.equal(buttons.interrupt.disabled,false);
for(const data of [{...owner,can_manage_workspace:false},{...owner,latest_run:{state:'failed'}},{...owner,worker:{state:'terminated'}}]){
 syncWatchMenuAvailability(data);assert.equal(buttons.interrupt.hidden,true);assert.equal(buttons.interrupt.disabled,true);
}
""")


def test_declared_desktop_and_exact_native_profile_own_app_visibility():
    menu_availability_checks("""
for(const [profile,selected] of [['codex-cli','codex'],['claude-code','claude'],['openclaw-general','openclaw'],['unknown-type',null]]){
 syncWatchMenuAvailability({...owner,worker:{...owner.worker,profile},runtime_details:{view_available:true}});
 assert.equal(appActions.hidden,false);assert.equal(surfaceDesktopButton.hidden,false);
 assert.equal(surfaceTerminalButton.hidden,false);
 for(const name of ['codex','claude','openclaw'])assert.equal(buttons[name].hidden,name!==selected);
}
syncWatchMenuAvailability({...owner,runtime_details:{view_url:'/synthetic-authorized-desktop'}});
assert.equal(appActions.hidden,false);assert.equal(buttons.codex.hidden,false);
activeSurface='desktop';syncWatchMenuAvailability({...owner,runtime_details:{view_available:true}});
assert.equal(surfaceTerminalButton.hidden,false);assert.equal(openTerminalLink.hidden,false);
""")


def test_read_only_or_closed_workspace_keeps_views_but_hides_mutation_actions():
    menu_availability_checks("""
for(const data of [
 {...owner,can_manage_workspace:false},
 ...['terminating','terminated'].map(close_state=>({...owner,worker:{...owner.worker,close_state}})),
 {...owner,worker:{...owner.worker,state:'terminated'}}
]){
 syncWatchMenuAvailability({...data,runtime_details:{view_available:true}});
 assert.equal(surfaceDesktopButton.hidden,false,'Declared view remains readable without mutation authority');
 assert.equal(appActions.hidden,true);assert.equal(buttons.interrupt.hidden,true);assert.equal(buttons.terminate.hidden,true);
 for(const name of ['codex','claude','openclaw'])assert.equal(buttons[name].hidden,true);
 assert.equal(workspaceGroup.hidden,true);
}
syncWatchMenuAvailability({...owner,worker:{...owner.worker,close_state:'termination_failed'},runtime_details:{view_available:true}});
assert.equal(appActions.hidden,true);assert.equal(buttons.interrupt.hidden,true);
for(const name of ['codex','claude','openclaw'])assert.equal(buttons[name].hidden,true);
assert.equal(buttons.terminate.hidden,false,'The owner can retry an unconfirmed workspace shutdown');
assert.equal(workspaceGroup.hidden,false);
syncWatchMenuAvailability({...owner,can_manage_workspace:false,worker:{...owner.worker,close_state:'termination_failed'}});
assert.equal(buttons.terminate.hidden,true,'A shutdown failure does not grant a read-only viewer close authority');
for(const state of ['completed','failed','interrupted',undefined]){
 syncWatchMenuAvailability({...owner,latest_run:{state}});
 assert.equal(buttons.interrupt.hidden,true);assert.equal(buttons.terminate.hidden,false);
}
""")


def test_ended_native_panel_is_hidden_only_after_end_reaches_existing_controller():
    source = (STATIC / "watch.js").read_text()
    node("""
import assert from 'node:assert/strict';
const updates=[];let nativeControls={updateLive:value=>updates.push(value)};
const nativeControlsMount={hidden:false};let mounts=0;
const canGuideNativeTurn=()=>false,attachNativeControls=()=>{mounts+=1};
const signedToken='',workerId='worker-synthetic',nativeGetJson=()=>{},nativePostJson=()=>{};
""" + actual_function("syncNativeControls", source) + """
syncNativeControls({native_control:null});assert.equal(nativeControlsMount.hidden,true);assert.equal(updates.at(-1),null);
for(const state of [{available:false},{run_id:'run',attempt_id:'attempt',available:false,pending_requests:[]}]){
 syncNativeControls({native_control:state});assert.equal(nativeControlsMount.hidden,false);assert.equal(updates.at(-1),state);
}
syncNativeControls({native_control:null});assert.equal(nativeControlsMount.hidden,true);assert.equal(updates.at(-1),null);
assert.equal(mounts,0,'Unavailable or ended state cannot create a mutation-capable controller');
""")


def close_dialog_checks(body):
    source = (STATIC / "watch.js").read_text()
    listeners = source[source.index("for (const button of document.querySelectorAll('[data-action]'))"):
                       source.index("filesToggle?.addEventListener('click'")]
    node("""
import assert from 'node:assert/strict';
class Element {
 constructor(){this.handlers={};this.disabled=false;this.open=false;}
 addEventListener(name,handler){this.handlers[name]=handler;}
 focus(){focused=this;}
 getAttribute(name){return name==='data-action'?'terminate':null;}
 showModal(){this.open=true;}
 close(){this.open=false;this.handlers.close?.();}
}
let focused=null,menuCloses=0,refreshes=0,actionFailure={previous:true};
const closeButton=new Element(),closeWorkspaceDialog=new Element(),closeWorkspaceCancel=new Element(),closeWorkspaceConfirm=new Element(),menuToggle=new Element();
const document={querySelectorAll:selector=>{assert.equal(selector,'[data-action]');return [closeButton];}};
const calls=[],failures=[];let respond=async()=>{};
const postAction=async path=>{calls.push(path);return respond();};
const refresh=async()=>{refreshes+=1};const showActionFailure=error=>failures.push(error);
const closeMenu=()=>{menuCloses+=1};
const event=()=>({prevented:false,preventDefault(){this.prevented=true;}});
function nativeEscape(){const e=event();closeWorkspaceDialog.handlers.cancel(e);if(!e.prevented)closeWorkspaceDialog.close();return e;}
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
""" + listeners + "\n" + body)


def test_close_workspace_opens_confirmation_with_safe_default_and_cancel_never_posts():
    close_dialog_checks("""
await closeButton.handlers.click();
assert.equal(closeWorkspaceDialog.open,true);assert.equal(focused,closeWorkspaceCancel);assert.equal(menuCloses,1);assert.deepEqual(calls,[]);
closeWorkspaceCancel.handlers.click();assert.equal(closeWorkspaceDialog.open,false);assert.equal(focused,menuToggle);assert.deepEqual(calls,[]);
await closeButton.handlers.click();const escape=nativeEscape();
assert.equal(escape.prevented,false);assert.equal(closeWorkspaceDialog.open,false);assert.equal(focused,menuToggle);assert.deepEqual(calls,[]);
""")


def test_explicit_close_confirmation_posts_once_and_busy_escape_cannot_dismiss():
    close_dialog_checks("""
let finish;respond=()=>new Promise(resolve=>finish=resolve);
await closeButton.handlers.click();const submitting=closeWorkspaceConfirm.handlers.click();
assert.deepEqual(calls,['action/terminate']);assert.equal(closeWorkspaceConfirm.disabled,true);assert.equal(closeWorkspaceCancel.disabled,true);
await closeWorkspaceConfirm.handlers.click();assert.equal(calls.length,1);
const escape=nativeEscape();assert.equal(escape.prevented,true);assert.equal(closeWorkspaceDialog.open,true);
finish();await submitting;
assert.equal(closeWorkspaceDialog.open,false);assert.equal(focused,menuToggle);assert.equal(refreshes,1);assert.equal(actionFailure,null);
assert.equal(closeWorkspaceConfirm.disabled,false);assert.equal(closeWorkspaceCancel.disabled,false);assert.deepEqual(failures,[]);
""")


def test_close_failure_preserves_the_original_error_and_restores_focus_and_controls():
    close_dialog_checks("""
const rejected=new Error('Synthetic exact close failure');respond=async()=>{throw rejected};
await closeButton.handlers.click();await closeWorkspaceConfirm.handlers.click();
assert.deepEqual(calls,['action/terminate']);assert.equal(failures.length,1);assert.equal(failures[0],rejected);
assert.equal(closeWorkspaceDialog.open,false);assert.equal(focused,menuToggle);assert.equal(refreshes,0);
assert.equal(closeWorkspaceConfirm.disabled,false);assert.equal(closeWorkspaceCancel.disabled,false);
""")


def test_never_run_workspace_keeps_view_choice_without_offering_an_empty_new_tab():
    menu_availability_checks("""
currentNoRunActivity=true;syncWatchMenuAvailability({...owner,latest_run:null});
assert.equal(surfaceTerminalButton.hidden,false);assert.equal(surfaceTerminalButton.disabled,true);
assert.equal(openExternal.disabled,true,'Never open a fake live activity page before first work');
currentNoRunActivity=false;syncWatchMenuAvailability(owner);
assert.equal(surfaceTerminalButton.disabled,false);assert.equal(openExternal.disabled,false);
activeSurface='desktop';syncWatchMenuAvailability({...owner,runtime_details:{view_available:true}});
assert.equal(openExternal.disabled,false);
""")
