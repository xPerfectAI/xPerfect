"""Real shared native renderer state and its three stylesheet consumers."""
import base64
import json
from pathlib import Path
import re
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def function(name, source):
    match = re.search(r'^function ' + re.escape(name) + r'\(.*?^}', source, re.M | re.S)
    assert match, name
    return match.group(0)


def node(code):
    for key, name in [('NATIVE', 'native-controls.js'), ('ONBOARDING', 'worker-onboarding.js')]:
        module = 'data:text/javascript;base64,' + base64.b64encode((STATIC / name).read_bytes()).decode()
        code = code.replace(key, json.dumps(module))
    result = subprocess.run(['node', '--input-type=module'], input=code,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr + result.stdout


def test_chat_draft_grows_with_content_and_keeps_room_for_actions_on_short_viewports():
    source = (STATIC / 'conversation.js').read_text()
    node("import assert from 'node:assert/strict';\n" + function('autoResizeMessage', source) + '''
const input={value:'A retained draft',style:{},scrollHeight:78};
let revealed=0;const composer={scrollIntoView(options){assert.equal(options.block,'nearest');revealed++}};
const $=id=>id==='composer'?composer:input;
globalThis.document={activeElement:null};
globalThis.window={innerHeight:900,visualViewport:{height:900}};
autoResizeMessage();assert.equal(input.style.height,'78px');
input.scrollHeight=600;autoResizeMessage();assert.equal(input.style.height,'360px');
window.visualViewport.height=400;autoResizeMessage();assert.equal(input.style.height,'168px');
assert.equal(input.value,'A retained draft','Sizing must not edit or send the draft');
input.scrollHeight=24;autoResizeMessage();assert.equal(input.style.height,'78px');
window.visualViewport=undefined;window.innerHeight=600;input.scrollHeight=600;
autoResizeMessage();assert.equal(input.style.height,'252px');
assert.equal(revealed,0,'Do not move the page while the user is reading');
document.activeElement=input;autoResizeMessage();assert.equal(revealed,1);
''')


def test_queued_native_guidance_clears_only_unchanged_acknowledged_text():
    node('''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(NATIVE);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.value='';}
 append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes}
 setAttribute(key,value){this[key]=value} addEventListener(key,handler){this.handlers[key]=handler}
 closest(){return null}
}
globalThis.document={createElement:tag=>new Element(tag)};globalThis.setInterval=()=>0;
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
for(const outcome of ['queued','edited','pending','failed','rejected']){
 const mount=new Element('div'),calls=[];let resolve,reject;
 const receipt=new Promise((yes,no)=>{resolve=yes;reject=no});
 const controller=attachNativeControls(mount,'worker',{getJson:async()=>({}),postJson:async(path,body)=>{calls.push({path,body});return receipt}});
 controller.updateLive({available:true,read_only:false,run_id:'run',attempt_id:'attempt',capabilities:{interject:true}});
 const panel=mount.children[0],[heading,status,requests,text,send]=panel.children;
 assert.equal(panel.className,'native-turn-controls');
 assert.match(send.className,/secondary-button/);assert.match(send.className,/compact-button/);
 text.value='Exact direction';send.handlers.click();send.handlers.click();assert.equal(calls.length,1);
 assert.equal(text.value,'Exact direction','The unsent draft stays while waiting');
 if(outcome==='edited')text.value='A newer draft';
 if(outcome==='rejected')reject(new Error('Native send rejected'));
 else resolve({status:outcome==='edited'?'queued':outcome});
 await settle();
 assert.equal(text.value,outcome==='queued'?'':outcome==='edited'?'A newer draft':'Exact direction');
 if(outcome==='queued'){send.handlers.click();assert.equal(calls.length,1,'Cleared text cannot repeat the acknowledged send')}
 if(outcome==='rejected')assert.equal(status.textContent,'Native send rejected');
 if(outcome==='pending')assert.equal(status.textContent,'Awaiting native receipt. Do not send again.');
}
''')


def test_native_button_styles_reach_app_watch_and_chat_with_existing_tokens():
    theme = (STATIC / 'theme.css').read_text()
    assert '.native-turn-controls .native-control-action {' in theme
    for token in ['--control-bg', '--control-hover', '--line', '--text']:
        assert token in theme
    styles = (STATIC / 'styles.css').read_text()
    # The anchored menu retains its nested controls without expanding the workspace tile.
    assert re.search(r'\.workspace-tile-more-actions > details\s*\{[^}]*padding:', styles)
    assert not re.search(r'\.workspace-tile-more\[open\][^{]*\{[^}]*flex:\s*1 1 100%', styles)
    assert '.action-menu-panel {' in theme
    for name in ['index.html', 'watch.html', 'conversation.html']:
        assert '/static/theme.css?v=20261024polish1' in (STATIC / name).read_text()
    for name in ['app.js', 'watch.js', 'conversation.js']:
        assert "./native-controls.js?v=20261024polish1" in (STATIC / name).read_text()


def test_watch_terminal_status_labels_and_colors_use_typed_state():
    source = (STATIC / 'watch.js').read_text()
    node("import assert from 'node:assert/strict';\n" + function('displayStateLabel', source) + '''
for(const [state,label] of [['failed','Failed'],['interrupted','Interrupted'],['cancelled','Cancelled'],['completed','Completed']]){
 assert.equal(displayStateLabel(state),label);
}
''')
    assert 'statePill.dataset.state = displayState;' in source
    assert "statePill.dataset.state = 'unavailable';" in source
    styles = (STATIC / 'styles.css').read_text()
    assert re.search(r'\.state-pill:is\(\[data-state="failed"\].*?color:\s*var\(--danger\)', styles, re.S)
    assert re.search(r'\.state-pill:is\(\[data-state="interrupted"\].*?color:\s*var\(--warning\)', styles, re.S)


def test_default_worker_reuses_host_availability_without_remapping_selection():
    app = (STATIC / 'app.js').read_text()
    code = '''
import assert from 'node:assert/strict';
const {workerTypeOption}=await import(ONBOARDING);
class Select {
 constructor(){this.options=[];this.value=''} replaceChildren(...options){this.options=options;this.value=options.find(x=>!x.disabled)?.value||''}
}
let workspaceType='host';
const document={createElement:()=>({}),getElementById:()=>({value:workspaceType})};
FUNCTION
const data={default_workspace_type:'host',user_preferences:{default_worker_profile:'native-missing'},new_workspace_options:[
 {value:'new:native-ready',profile:'native-ready',label:'Ready worker',host_cli_installed:true},
 {value:'new:native-missing',profile:'native-missing',label:'Missing worker',host_cli_installed:false},
 {value:'new:blocked',profile:'blocked',label:'Disabled worker',disabled:true},
 {value:'new:unprobed',profile:'unprobed',label:'Unprobed worker'},
 {value:'new:legacy'}]};
const select=new Select();renderDefaultWorkerOptions(select,data);
assert.equal(select.value,'native-missing','Saved default remains visible, even when unavailable');
assert.equal(select.options.find(x=>x.value==='native-missing').disabled,true);
assert.match(select.options.find(x=>x.value==='native-missing').textContent,/Not installed on this computer/);
assert.equal(select.options.find(x=>x.value==='native-ready').disabled,false);
assert.equal(select.options.find(x=>x.value==='unprobed').disabled,false);
assert.equal(select.options.find(x=>x.value==='legacy').textContent,'legacy');
workspaceType='sandboxed';renderDefaultWorkerOptions(select,data);
assert.equal(select.value,'native-missing');
assert.equal(select.options.find(x=>x.value==='native-missing').disabled,false,'Missing host CLI does not disable a sandbox worker');
assert.equal(select.options.find(x=>x.value==='blocked').disabled,true,'Declared global disabled state stays disabled');
assert.equal(workerTypeOption(data.new_workspace_options[1],true).label,'Missing worker · Not installed on this computer');
'''.replace('FUNCTION', function('renderDefaultWorkerOptions', app))
    node(code)


def test_unconfirmed_interject_blocks_repeat_across_unavailable_reads_but_not_cancel_or_new_target():
    node('''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(NATIVE);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.value='';}
 append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes}
 setAttribute(key,value){this[key]=value} addEventListener(key,handler){this.handlers[key]=handler}
 closest(){return null}
}
globalThis.document={createElement:tag=>new Element(tag)};globalThis.setInterval=()=>0;
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
const mount=new Element('div'),calls=[];let accept;
const receipt=new Promise(resolve=>accept=resolve);
const controller=attachNativeControls(mount,'worker',{getJson:async()=>({available:false}),postJson:async(path,body)=>{
 calls.push(body);return calls.length===1?receipt:{status:body.action==='cancel'?'cancel_requested':'queued'};
}});
const state={available:true,read_only:false,run_id:'run',attempt_id:'attempt-A',capabilities:{interject:true,cancel:true}};
controller.updateLive(state);
const panel=mount.children[0],[heading,status,requests,text,send,cancel]=panel.children;
text.value='One direction';send.handlers.click();assert.equal(calls.length,1);
controller.updateLive({available:false});accept({status:'pending'});await settle();
for(const transient of [{available:false},{available:false,pending_requests:[]}]){
 controller.updateLive(transient);controller.updateLive(state);
 assert.equal(text.value,'One direction');assert.equal(send.disabled,true);
 send.handlers.click();await settle();assert.equal(calls.length,1);
}
assert.equal(status.textContent,'Awaiting native receipt. Do not send again.');
text.value='Edited draft stays';send.handlers.click();await settle();assert.equal(calls.length,1);
assert.equal(cancel.disabled,false);cancel.handlers.click();await settle();
assert.equal(calls.length,2);assert.equal(calls[1].action,'cancel');assert.equal(send.disabled,true);
panel.open=true;await controller.refresh();controller.updateLive(state);assert.equal(send.disabled,true);
controller.updateLive({...state,attempt_id:'attempt-B'});
assert.equal(send.disabled,false);send.handlers.click();await settle();
assert.equal(calls.length,3);assert.equal(calls[2].attempt_id,'attempt-B');assert.equal(calls[2].text,'Edited draft stays');
assert.equal(text.value,'');
''')


def test_old_target_ack_or_error_cannot_clear_new_target_draft_or_status():
    node('''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(NATIVE);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.value='';}
 append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes}
 setAttribute(key,value){this[key]=value} addEventListener(key,handler){this.handlers[key]=handler}
 closest(){return null}
}
globalThis.document={createElement:tag=>new Element(tag)};globalThis.setInterval=()=>0;
for(const result of ['queued','error','pending']){
 const mount=new Element('div');let accept,reject;
 const receipt=new Promise((yes,no)=>{accept=yes;reject=no});
 const controller=attachNativeControls(mount,'worker',{getJson:async()=>({}),postJson:async()=>receipt});
 const state={available:true,read_only:false,run_id:'run-A',attempt_id:'attempt-A',capabilities:{interject:true}};
 controller.updateLive(state);
 const [heading,status,requests,text,send]=mount.children[0].children;
 text.value='Same text in new target';send.handlers.click();
 controller.updateLive({...state,run_id:'run-B',attempt_id:'attempt-B',available:false});
 const priorStatus=status.textContent;
 if(result==='error')reject(new Error('Old target error'));else accept({status:result});
 for(let i=0;i<8;i++)await Promise.resolve();
 assert.equal(text.value,'Same text in new target');assert.equal(status.textContent,priorStatus);
 controller.updateLive({...state,run_id:'run-B',attempt_id:'attempt-B'});assert.equal(send.disabled,false);
}
''')


def test_late_native_poll_cannot_overwrite_new_target_or_its_unconfirmed_guard():
    node('''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(NATIVE);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.value='';}
 append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes}
 setAttribute(key,value){this[key]=value} addEventListener(key,handler){this.handlers[key]=handler}
 closest(){return null}
}
globalThis.document={createElement:tag=>new Element(tag)};globalThis.setInterval=()=>0;
for(const outcome of ['old-state','unavailable','error']){
 const mount=new Element('div'),calls=[];let resolve,reject;
 const oldPoll=new Promise((yes,no)=>{resolve=yes;reject=no});
 const controller=attachNativeControls(mount,'worker',{getJson:async()=>oldPoll,postJson:async(path,body)=>{calls.push(body);return {status:'pending'}}});
 const state={available:true,read_only:false,run_id:'run-A',attempt_id:'attempt-A',capabilities:{interject:true,cancel:true}};
 controller.updateLive(state);
 const panel=mount.children[0],[heading,status,requests,text,send,cancel]=panel.children;
 panel.open=true;const polling=controller.refresh();
 const next={...state,run_id:'run-B',attempt_id:'attempt-B'};controller.updateLive(next);
 text.value='New target direction';send.handlers.click();for(let i=0;i<8;i++)await Promise.resolve();
 assert.equal(calls.length,1);assert.equal(send.disabled,true);
 if(outcome==='error')reject(new Error('Old poll error'));
 else resolve(outcome==='unavailable'?{available:false}:state);
 await polling;
 assert.equal(cancel.disabled,false,'A stale poll must not disable the newer target');
 assert.equal(status.textContent,'Awaiting native receipt. Do not send again.');
 controller.updateLive(next);send.handlers.click();for(let i=0;i<8;i++)await Promise.resolve();
 assert.equal(send.disabled,true);assert.equal(calls.length,1,'The new target keeps its unconfirmed action guard');
 assert.equal(text.value,'New target direction');
}
''')


def test_native_refresh_skips_explicit_unavailable_but_keeps_read_only_observation():
    node('''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(NATIVE);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.value='';}
 append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes}
 setAttribute(key,value){this[key]=value} addEventListener(key,handler){this.handlers[key]=handler}
 closest(){return null}
}
globalThis.document={createElement:tag=>new Element(tag)};globalThis.setInterval=()=>0;
const mount=new Element('div');let reads=0;
const state={available:true,read_only:true,run_id:'run',attempt_id:'attempt',capabilities:{interject:true}};
const controller=attachNativeControls(mount,'worker',{getJson:async()=>{reads+=1;return state},postJson:async()=>{throw new Error('No write expected')}});
const panel=mount.children[0];panel.open=true;
controller.updateLive({...state,available:false});await controller.refresh();assert.equal(reads,0);
controller.updateLive(state);await controller.refresh();assert.equal(reads,1,'Read-only available state can still be observed');
assert.equal(panel.children[4].disabled,true);assert.equal(panel.children[5].disabled,true);
controller.updateLive(null);await controller.refresh();assert.equal(reads,1);
''')
