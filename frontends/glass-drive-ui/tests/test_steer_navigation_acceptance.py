"""Keep an in-flight operator message alive through app-owned navigation."""
import json
from pathlib import Path
import re
import subprocess

import pytest

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'
SOURCE = (STATIC / 'app.js').read_text()


def function(name):
    match = re.search(r'^(?:async )?function ' + re.escape(name) + r'\(.*?^\}', SOURCE, re.M | re.S)
    assert match, name
    return match.group(0)


HELPERS = '\n'.join(function(name) for name in [
    'withAuth', 'bindWorkspaceInteractionOwner', 'currentWorkspaceTile', 'pendingWorkspaceSteer',
    'waitForWorkspaceSteers', 'waitForWorkspaceSteer', 'guardWorkspacePageNavigation',
    'submitWorkspaceSteer', 'syncTileSteerAvailability', 'autoResizeTextarea',
    'openWorkspaceSurface', 'openWorkspaceTools',
])

HARNESS = r'''
const assert=require('node:assert/strict');
let workspaceInteractionOwnerScope='',workspaceSteerInFlight=new Map(),signedToken='';
let tiles=new Map(),requests=[],navigation=[],refreshes=0;
let currentHref='http://app.example.invalid/#workspaces';
let postResult=async()=>({accepted:true});
const window={location:{
 get href(){return currentHref},set href(value){navigation.push(value);currentHref=value},
 get origin(){return new URL(currentHref).origin},get pathname(){return new URL(currentHref).pathname},
 get search(){return new URL(currentHref).search},assign(value){navigation.push(value);currentHref=value}
}};
const document={querySelectorAll(selector){assert.equal(selector,'.workspace-tile');return [...tiles.values()];}};
const workerApiUrl=(id,suffix)=>'/api/worker/'+id+suffix;
const workspaceStateLabel=workspace=>workspace.state||'paused';
const workspaceSetupAction=()=> 'claude';
const shouldResumeOnWorkspaceOpen=({renderedState,fallbackState})=>(renderedState||fallbackState)==='paused';
async function postJson(path,payload){requests.push({path,payload});return postResult(path,payload);}
function makeTile(id,text='Exact operator message',scope='owner-a'){
 const input={value:text,style:{},scrollHeight:44,focus(){this.focused=true}};
 const send={disabled:false},output={textContent:''};
 const tile={dataset:{workerId:id,ownerScope:scope,displayState:'paused'},input,send,output,
  querySelector(selector){return selector==='.workspace-steer textarea'?input
   :selector==='.workspace-steer button[type="submit"]'?send
   :selector==='[data-worker-output]'?output:null;}};
 tiles.set(id,tile);return tile;
}
function control(tile){return{disabled:false,textContent:'Open workspace',closest(){return tile;}};}
const workspace=id=>({worker_id:id,workspace_kind:'named',state:'paused',workspace_url:'/watch/'+id});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return{promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
const refresh=async()=>{refreshes++;};
function anchor(href,{target='',preview=false,download=false}={}){
 return{target,classList:{contains(name){return name==='workspace-glass-open'&&preview;}},
  getAttribute(name){return name==='href'?href:null},hasAttribute(name){return name==='download'&&download;}};
}
function clickEvent(link,extra={}){return{button:0,target:{closest(){return link}},preventDefault(){this.prevented=true;},...extra};}
HELPERS
bindWorkspaceInteractionOwner('owner-a');
(async()=>{
SCENARIO
})().catch(error=>{console.error(error);process.exit(1)});
'''

SCENARIOS = {
    'shared_navigation_retains_existing_signed_scope': r'''
signedToken='synthetic-signed-scope';const tile=makeTile('A');
const target={...workspace('A'),state:'completed',workspace_url:'/watch/A?project_id=synthetic#details'};
tile.dataset.displayState='completed';
await openWorkspaceSurface(target,control(tile));
await openWorkspaceTools(target,control(tile));
assert.deepEqual(navigation,['/watch/A?project_id=synthetic&gh_token=synthetic-signed-scope#details',
 '/watch/A?project_id=synthetic&gh_token=synthetic-signed-scope#details']);
assert.equal(withAuth('/watch/A?gh_token=already-scoped'),'/watch/A?gh_token=already-scoped');
''',
    'pending_same_worker_open_waits_only_for_acceptance': r'''
const tile=makeTile('A'),ack=deferred(),refreshDone=deferred();
postResult=path=>path.endsWith('/steer')?ack.promise:{};
const sending=submitWorkspaceSteer(workspace('A'),tile,tile.input,()=>refreshDone.promise);
const opening=openWorkspaceSurface(workspace('A'),control(tile));
await flush();assert.equal(requests.length,1);assert.equal(navigation.length,0);assert.equal(tile.send.disabled,true);
ack.resolve({run_id:'accepted-A',state:'queued'});await opening;
assert.deepEqual(navigation,['/watch/A']);assert.equal(requests.length,1,'Accepted steer owns startup; no redundant Resume');
assert.equal(tile.input.value,'');refreshDone.resolve();await sending;
''',
    'rerender_does_not_reenable_duplicate_send_or_erase_new_draft': r'''
const old=makeTile('A'),ack=deferred();postResult=()=>ack.promise;
const sending=submitWorkspaceSteer(workspace('A'),old,old.input,refresh);
const current=makeTile('A');syncTileSteerAvailability(current,'paused');assert.equal(current.send.disabled,true);
await submitWorkspaceSteer(workspace('A'),current,current.input,refresh);assert.equal(requests.length,1);
current.input.value='A different draft entered while the first message is sent';
ack.resolve({run_id:'accepted-A'});await sending;
assert.equal(current.input.value,'A different draft entered while the first message is sent');assert.equal(current.send.disabled,false);
''',
    'failure_keeps_draft_cause_and_current_page': r'''
const tile=makeTile('A'),ack=deferred();postResult=()=>ack.promise;
const sending=submitWorkspaceSteer(workspace('A'),tile,tile.input,refresh);
const opening=openWorkspaceSurface(workspace('A'),control(tile));
await flush();ack.reject(new Error('The request was rejected.'));
await Promise.all([sending,opening]);assert.equal(navigation.length,0);assert.equal(requests.length,1);
assert.equal(tile.input.value,'Exact operator message');assert.equal(tile.output.textContent,'The request was rejected.');
assert.equal(tile.send.disabled,false);assert.equal(refreshes,0);
''',
    'tools_open_waits_before_native_setup': r'''
const tile=makeTile('A'),ack=deferred();postResult=path=>path.endsWith('/steer')?ack.promise:{};
const sending=submitWorkspaceSteer(workspace('A'),tile,tile.input,refresh);
const opening=openWorkspaceTools(workspace('A'),control(tile));await flush();assert.equal(requests.length,1);
ack.resolve({run_id:'accepted-A'});await Promise.all([sending,opening]);
assert.deepEqual(requests.map(x=>x.path),['/api/worker/A/steer','/api/worker/A/action/claude']);assert.deepEqual(navigation,['/watch/A']);
''',
    'other_worker_execution_is_independent_but_page_exit_preserves_both': r'''
const a=makeTile('A'),b=makeTile('B'),ackA=deferred(),ackB=deferred();
postResult=path=>path==='/api/worker/A/steer'?ackA.promise:path==='/api/worker/B/steer'?ackB.promise:{};
const sendA=submitWorkspaceSteer(workspace('A'),a,a.input,refresh),sendB=submitWorkspaceSteer(workspace('B'),b,b.input,refresh);
assert.equal(requests.length,2,'Both workers accept input independently');
const opening=openWorkspaceSurface(workspace('B'),control(b));await flush();ackB.resolve({run_id:'accepted-B'});await sendB;
assert.equal(navigation.length,0,'Viewing B must not cancel the still-sent message to A');
ackA.resolve({run_id:'accepted-A'});await Promise.all([sendA,opening]);assert.deepEqual(navigation,['/watch/B']);assert.equal(requests.length,2);
''',
    'owner_change_fences_old_reply_and_navigation': r'''
const old=makeTile('A'),ack=deferred();postResult=()=>ack.promise;
const sending=submitWorkspaceSteer(workspace('A'),old,old.input,refresh),opening=openWorkspaceSurface(workspace('A'),control(old));
bindWorkspaceInteractionOwner('owner-b');const current=makeTile('A','New owner draft','owner-b');
ack.resolve({run_id:'old-owner-accepted'});await Promise.all([sending,opening]);
assert.equal(navigation.length,0);assert.equal(current.input.value,'New owner draft');assert.equal(current.output.textContent,'');assert.equal(refreshes,0);
''',
    'chat_and_home_page_links_wait_for_receipts': r'''
for(const href of ['/conversation','/']){
 const tile=makeTile('A'),ack=deferred();postResult=()=>ack.promise;navigation=[];currentHref='http://app.example.invalid/#workspaces';
 const sending=submitWorkspaceSteer(workspace('A'),tile,tile.input,refresh);
 const event=clickEvent(anchor(href));const leaving=guardWorkspacePageNavigation(event);await flush();
 assert.equal(event.prevented,true);assert.equal(navigation.length,0);
 ack.resolve({run_id:'accepted-A'});await Promise.all([sending,leaving]);assert.deepEqual(navigation,[new URL(href,'http://app.example.invalid/').href]);
}
''',
    'hash_modifier_newtab_download_and_external_links_remain_native': r'''
const tile=makeTile('A'),ack=deferred();postResult=()=>ack.promise;
const sending=submitWorkspaceSteer(workspace('A'),tile,tile.input,refresh);
for(const [link,extra] of [[anchor('/#library'),{}],[anchor('#workspaces'),{}],[anchor('/conversation'),{ctrlKey:true}],
 [anchor('/conversation',{target:'_blank'}),{}],[anchor('/file',{download:true}),{}],[anchor('https://docs.example.invalid/'),{}]]){
 const event=clickEvent(link,extra);await guardWorkspacePageNavigation(event);assert.equal(event.prevented,undefined);
}
assert.equal(navigation.length,0);ack.resolve({run_id:'accepted-A'});await sending;
''',
    'page_navigation_failure_is_not_followed_by_automatic_exit': r'''
const tile=makeTile('A'),ack=deferred();postResult=()=>ack.promise;
const sending=submitWorkspaceSteer(workspace('A'),tile,tile.input,refresh);
const event=clickEvent(anchor('/conversation'));const leaving=guardWorkspacePageNavigation(event);
ack.reject(new Error('No acceptance receipt.'));await Promise.all([sending,leaving]);
assert.equal(event.prevented,true);assert.equal(navigation.length,0);assert.equal(tile.input.value,'Exact operator message');
assert.equal(tile.output.textContent,'No acceptance receipt.');
''',
}


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_real_steer_and_navigation_functions_preserve_acceptance(scenario):
    script = HARNESS.replace('HELPERS', HELPERS).replace('SCENARIO', SCENARIOS[scenario])
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_existing_form_preview_identity_and_full_page_consumers_use_the_shared_gate():
    assert "await submitWorkspaceSteer(workspace, tile, steerInput, refreshBootstrap);" in SOURCE
    assert "previewLink.addEventListener('click'" in SOURCE
    preview = re.search(r"previewLink.addEventListener\('click', async \(event\) => \{.*?^    \}\);", SOURCE, re.M | re.S)
    assert preview and 'await openWorkspaceSurface(workspace, previewLink);' in preview.group(0)
    assert "document.addEventListener('click', guardWorkspacePageNavigation, true);" in SOURCE
    assert 'bindWorkspaceInteractionOwner(bootstrap.draft_owner_scope);' in SOURCE
    assert 'await waitForWorkspaceSteers(launchOwnerScope);' in SOURCE
    logout = function('signOut')
    assert logout.index('await waitForWorkspaceSteers()') < logout.index("postJson('/auth/logout'")
    assert "await waitForWorkspaceSteers(String(tile.dataset.ownerScope || ''));" in SOURCE
    assert 'pendingWorkspaceSteer(workerId, data?.draft_owner_scope || workspaceInteractionOwnerScope)?.message' in SOURCE
