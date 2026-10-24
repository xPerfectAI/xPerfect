"""Terminal run recovery does not restart compute without corrected work."""
import base64
import json
from pathlib import Path
import re
import subprocess

import pytest


STATIC = Path(__file__).parents[1] / "src/glass_drive_ui/static"


def node(code):
    module = "data:text/javascript;base64," + base64.b64encode(
        (STATIC / "launch-policy.js").read_bytes()).decode()
    code = "import assert from 'node:assert/strict';\n" + (
        "const {workspaceLifecycleControl,shouldResumeOnWorkspaceOpen}=await import("
        + json.dumps(module) + ");\n") + code
    result = subprocess.run(["node", "--input-type=module"], input=code,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr + result.stdout


def function(name, source):
    match = re.search(r"^function " + re.escape(name) + r"\(.*?^}", source, re.M | re.S)
    assert match, name
    return match.group(0)


@pytest.mark.parametrize("run_state", ["failed", "cancelled", "interrupted"])
def test_terminal_run_on_paused_compute_never_offers_resume(run_state):
    node(f"""
const control=workspaceLifecycleControl('paused',{json.dumps(run_state)});
assert.equal(control.action,'pause');assert.equal(control.label,'Pause');assert.equal(control.hidden,true);
""")


@pytest.mark.parametrize("run_state", ["failed", "cancelled", "interrupted"])
def test_terminal_run_keeps_pause_only_while_compute_is_running(run_state):
    node(f"""
for(const state of ['queued','running','resuming']){{
 assert.deepEqual(workspaceLifecycleControl(state,{json.dumps(run_state)}),{{action:'pause',label:'Pause',hidden:false,disabled:false}});
}}
for(const state of ['ready','retained','paused','idle','idle_terminated','stopped','unknown']){{
 assert.equal(workspaceLifecycleControl(state,{json.dumps(run_state)}).hidden,true);
}}
""")


def test_completed_continue_paused_resume_and_closed_guards_are_preserved():
    node("""
assert.deepEqual(workspaceLifecycleControl('completed','completed'),{action:'resume',label:'Continue',hidden:false,disabled:false});
for(const state of ['ready','retained','paused','idle','idle_terminated','stopped']){
 assert.deepEqual(workspaceLifecycleControl(state),{action:'resume',label:'Resume',hidden:false,disabled:false});
 assert.equal(workspaceLifecycleControl(state,null).hidden,true,'Unknown last-run state cannot authorize a resume affordance');
}
assert.equal(workspaceLifecycleControl('running',null).hidden,false);
for(const state of ['terminating','termination_failed','terminated']){
 for(const runState of ['failed','cancelled','interrupted',undefined]){
  const control=workspaceLifecycleControl(state,runState);assert.equal(control.hidden,true);assert.equal(control.disabled,true);
 }
}
assert.equal(shouldResumeOnWorkspaceOpen({workspaceKind:'named',renderedState:'unknown',fallbackState:'paused'}),false);
assert.equal(shouldResumeOnWorkspaceOpen({workspaceKind:'named',renderedState:'paused'}),true);
""")


def test_watch_footer_uses_both_run_and_compute_states():
    source = (STATIC / "watch.js").read_text()
    node("""
const runToggleButton={dataset:{},setAttribute(){}};
""" + function("syncRunToggle", source) + """
for(const runState of ['failed','cancelled','interrupted']){
 syncRunToggle('paused',runState);assert.equal(runToggleButton.hidden,true);assert.equal(runToggleButton.dataset.action,'pause');
 syncRunToggle('running',runState);assert.equal(runToggleButton.hidden,false);assert.equal(runToggleButton.textContent,'Pause');
}
syncRunToggle('completed','completed');assert.equal(runToggleButton.hidden,false);assert.equal(runToggleButton.textContent,'Continue');
syncRunToggle('paused','paused');assert.equal(runToggleButton.hidden,false);assert.equal(runToggleButton.textContent,'Resume');
""")


def tile_checks(body):
    source = (STATIC / "app.js").read_text()
    render = function("renderWorkspaceTile", source)
    initial = render[render.index("  const workerId ="):render.index("  const currentSelection =")]
    preview = render[render.index("  const glassPane ="):render.index("  glass.appendChild(glassPane);")]
    toggle = render[render.index("  const toggle = createButton(lifecycleControl.label"):render.index("  const interrupt = createButton('Interrupt'")]
    more = render[render.index("  more.addEventListener('toggle'"):render.index("  tile.__nativeControlsMount")]
    steer = re.findall(r"  syncTileSteerAvailability\(tile,[^;]+;", render)
    assert len(steer) == 1
    helpers = "\n".join(function(name, source) for name in
                        ("rawWorkspaceState", "workspaceStateLabel", "isWorkspaceActive",
                         "isWorkspaceResumable", "createButton", "updateTileControlLabels", "setGlassPane"))
    node("""
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.handlers={};this.hidden=false;this.disabled=false;}
 append(...nodes){this.children.push(...nodes);}appendChild(node){this.children.push(node);return node;}
 replaceChildren(...nodes){this.children=nodes;}addEventListener(name,handler){this.handlers[name]=handler;}
 setAttribute(){} querySelector(selector){return this.lookup?.(selector)||null;}closest(){return this.tile||null;}
}
const document={createElement:tag=>new Element(tag),querySelectorAll:()=>[]};
const ACTIVE_STATES=new Set(['created','starting','queued','running','resuming']);
const RESUME_STATES=new Set(['ready','paused','idle','idle_terminated','stopped','completed','retained']);
const TERMINAL_ATTENTION_STATES=new Set(['failed','cancelled','interrupted']);
const INTERRUPTIBLE_STATES=new Set(['queued','running']),DISABLED_CONTROL_STATES=new Set(['created','starting','terminating','termination_failed','terminated']);
const MAX_VIEW_ONLY_PREVIEWS=4,previewWorkerIds=()=>[];let refreshes=0;
const refreshWorkspaceTile=async()=>{refreshes+=1},runWorkerAction=async()=>{};
const displayStateLabel=value=>value;
function syncTileSteerAvailability(tile,state){tile.dataset.displayState=state;}
""" + helpers + "\nfunction initial(workspace){\n" + initial + """
 const tile=new Element('article');tile.dataset.watchVisible='true';
 const actions=new Element('div'),more=new Element('details'),glass=new Element('div');glass.tile=tile;
 const refreshBootstrap=()=>{};
""" + preview + toggle + more + steer[0] + """
 tile.lookup=selector=>selector==='[data-worker-action-toggle]'?toggle:null;
 glass.lookup=selector=>selector==='[data-worker-glass]'?glassPane:null;
 return {tile,toggle,more,glass,preview:glassPane};
}
""" + body)


def test_initial_unknown_last_run_resolves_through_existing_live_and_more_paths():
    tile_checks("""
const workspace={worker_id:'worker-synthetic',state:'paused',last_run_id:'run-saved'};
for(const resolved of ['paused','failed','cancelled','interrupted']){
 const view=initial(workspace);assert.equal(view.toggle.hidden,true);assert.equal(view.tile.dataset.displayState,'unknown');
 assert.equal(view.preview.children.some(node=>node.tag==='button'),false);
 assert.match(view.preview.children[0].textContent,/Checking latest work/);
 assert.equal(shouldResumeOnWorkspaceOpen({workspaceKind:'named',renderedState:view.tile.dataset.displayState,fallbackState:'paused'}),false);
 view.more.open=true;view.more.handlers.toggle();assert.equal(refreshes>0,true);
 updateTileControlLabels(view.tile,resolved,'paused');setGlassPane(view.glass,workspace.worker_id,resolved,false,()=>{});
 assert.equal(view.tile.dataset.displayState,resolved);
 if(resolved==='paused'){
  assert.equal(view.toggle.hidden,false);assert.equal(view.toggle.textContent,'Resume');
  assert.equal(view.preview.children[0].textContent,'Resume workspace');
 }else{
  assert.equal(view.toggle.hidden,true);assert.equal(view.preview.children.some(node=>node.tag==='button'),false);
  assert.match(view.preview.children[0].textContent,/Send a corrected follow-up/);
 }
 const before=refreshes;view.more.handlers.toggle();assert.equal(refreshes,before,'Known live state does not add detail polls');
}
""")


def test_initial_normal_pause_and_completed_result_keep_their_existing_controls():
    tile_checks("""
const paused=initial({worker_id:'worker-new',state:'paused'});
assert.equal(paused.toggle.hidden,false);assert.equal(paused.toggle.textContent,'Resume');
assert.equal(paused.preview.children[0].textContent,'Resume workspace');
assert.equal(paused.tile.dataset.displayState,'paused');
const complete=initial({worker_id:'worker-complete',state:'paused',state_label:'completed',last_run_id:'run-complete'});
assert.equal(complete.toggle.hidden,false);assert.equal(complete.toggle.textContent,'Continue');
assert.equal(complete.preview.children.some(node=>node.tag==='button'),false);
assert.equal(complete.preview.children[0].textContent,'Delivery ready');
for(const runState of ['failed','cancelled','interrupted']){
 const view=initial({worker_id:'worker-failed',state:'paused',state_label:runState,last_run_id:'run-failed'});
 assert.equal(view.toggle.hidden,true);assert.equal(view.preview.children.some(node=>node.tag==='button'),false);
 updateTileControlLabels(view.tile,runState,'running');assert.equal(view.toggle.hidden,false);assert.equal(view.toggle.textContent,'Pause');
}
""")
