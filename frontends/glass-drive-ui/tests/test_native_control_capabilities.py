"""Native controls follow declared actions, not the worker's provider label."""
import base64
import json
from pathlib import Path
import re
import subprocess

STATIC=Path(__file__).parents[1]/'src/glass_drive_ui/static'


def function(source,name):
    match=re.search(r'^function '+re.escape(name)+r'\(.*?^}',source,re.M|re.S)
    assert match,name
    return match.group(0)


def node(code):
    module='data:text/javascript;base64,'+base64.b64encode((STATIC/'native-controls.js').read_bytes()).decode()
    result=subprocess.run(['node','--input-type=module','--eval',code.replace('MODULE',json.dumps(module))],
                          text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr+result.stdout


def test_watch_and_workspaces_mount_existing_panel_from_typed_live_actions_only():
    watch=(STATIC/'watch.js').read_text();app=(STATIC/'app.js').read_text()
    code='''
import assert from 'node:assert/strict';
const {canGuideNativeTurn}=await import(MODULE);
let signedToken='',nativeControls=null,nativeControlsMount={},workerId='worker-A';
const nativeGetJson=()=>{},nativePostJson=()=>{},getJson=()=>{},postJson=()=>{};
const mounts=[],updates=[];
const attachNativeControls=(container,id,helpers)=>{mounts.push({container,id});return {updateLive:state=>updates.push(state)}};
const live={available:true,read_only:false,capabilities:{interject:true,cancel:false,permission:false}};
FUNCTIONS
for(const state of [null,{},true,{available:'true',capabilities:{interject:true}},
 {available:true,read_only:true,capabilities:{interject:true}},
 {available:true,capabilities:{interject:true}}, {available:true,read_only:'false',capabilities:{interject:true}},
 {available:true,capabilities:{interject:'true'}},{available:true,capabilities:{}}]){
 assert.equal(canGuideNativeTurn(state),false);syncNativeControls({native_control:state});
 syncWorkspaceNativeControls({__nativeControlsMount:{}},'worker-negative',{native_control:state});
}
assert.equal(mounts.length,0);
signedToken='synthetic-read-only-link';syncNativeControls({native_control:live});assert.equal(mounts.length,0);signedToken='';
for(const profile of ['codex-cli','claude-code','grok-build','another-supported-native']){
 nativeControls=null;syncNativeControls({worker:{profile},native_control:live});syncNativeControls({worker:{profile},native_control:live});
 const tile={__nativeControlsMount:{}};syncWorkspaceNativeControls(tile,'worker-'+profile,{worker:{profile},native_control:live});
 syncWorkspaceNativeControls(tile,'worker-'+profile,{native_control:live});
}
assert.equal(mounts.length,8);assert.equal(updates.length,16);
syncNativeControls({native_control:null});assert.equal(updates.at(-1),null);
'''.replace('FUNCTIONS',function(watch,'syncNativeControls')+'\n'+function(app,'syncWorkspaceNativeControls'))
    node(code)
    assert 'syncNativeControls(data);' in watch
    assert 'syncWorkspaceNativeControls(tile, workerId, data);' in app
    assert 'tile.__nativeControlsMount = moreActions;' in app
    assert "if (workspace.profile === 'grok-build')" not in app
    assert "String(worker.profile || '') === 'grok-build' &&" not in watch


def test_shared_native_panel_enables_only_declared_actions_and_keeps_exact_route_scope():
    node('''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(MODULE);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.disabled=false;this.value='';}
 append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes}
 setAttribute(key,value){this[key]=value} addEventListener(key,handler){this.handlers[key]=handler}
 closest(){return null}
}
globalThis.document={createElement:tag=>new Element(tag)};globalThis.setInterval=()=>0;globalThis.clearInterval=()=>{};
const mount=new Element('div'),calls=[];
const controller=attachNativeControls(mount,'worker-synthetic',{getJson:async()=>({}),postJson:async(path,body)=>{calls.push({path,body});return {status:'queued'}}});
const state={run_id:'run-current',attempt_id:'attempt-current',available:true,read_only:false,
 capabilities:{interject:true,cancel:false,permission:false},pending_requests:[]};
controller.updateLive(state);
const panel=mount.children[0],[heading,status,requests,text,send,cancel]=panel.children;
assert.equal(text['aria-label'],'Message to the active turn');assert.equal(send.disabled,false);assert.equal(cancel.disabled,true);
text.value='Exact direction';send.handlers.click();for(let i=0;i<8;i++)await Promise.resolve();
assert.deepEqual(calls,[{path:'/api/worker/worker-synthetic/native-control',body:{run_id:'run-current',attempt_id:'attempt-current',action:'interject',text:'Exact direction'}}]);
assert.equal(status.textContent,'Message queued for the active turn.');
cancel.handlers.click();for(let i=0;i<8;i++)await Promise.resolve();assert.equal(calls.length,1);
const pending=[{request_id:'request-current',method:'session/request_permission',request:{options:[{kind:'allow_once',optionId:'once',name:'Allow once'}]}}];
controller.updateLive({...state,pending_requests:pending});assert.equal(requests.children[0].disabled,true);
controller.updateLive({...state,read_only:true,capabilities:{interject:true,cancel:true,permission:true},pending_requests:pending});
assert.equal(send.disabled,true);assert.equal(cancel.disabled,true);assert.equal(requests.children[0].disabled,true);
send.handlers.click();cancel.handlers.click();for(let i=0;i<8;i++)await Promise.resolve();assert.equal(calls.length,1);
controller.updateLive({...state,capabilities:{interject:false,cancel:false,permission:true},pending_requests:pending});
assert.equal(send.disabled,true);assert.equal(cancel.disabled,true);assert.equal(requests.children[0].disabled,false);
controller.updateLive(null);assert.equal(send.disabled,true);assert.equal(cancel.disabled,true);
''')
