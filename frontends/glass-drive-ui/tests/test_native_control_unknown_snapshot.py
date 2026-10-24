"""Unknown native reads must not erase a user's answer or a confirmed receipt."""
import base64
import json
from pathlib import Path
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def node(body):
    module = 'data:text/javascript;base64,' + base64.b64encode((STATIC / 'native-controls.js').read_bytes()).decode()
    code = '''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(MODULE);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.value='';}
 append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes}
 setAttribute(key,value){this[key]=value} addEventListener(key,handler){this.handlers[key]=handler}
 closest(){return this.parentDetails || null} querySelector(){return this.summary || null}
}
globalThis.document={createElement:tag=>new Element(tag)};globalThis.setInterval=()=>0;
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
const question={request_id:'question-A',method:'x.ai/mcp/elicit',request:{mode:'form',requestedSchema:{properties:{answer:{type:'string'}}}}};
const live={available:true,read_only:false,run_id:'run-A',attempt_id:'attempt-A',capabilities:{permission:true,interject:true,cancel:true},pending_requests:[question]};
const inputOf=section=>section.children.find(child=>child.tag==='label').children.find(child=>child.tag==='input');
function setup(helpers={}){
 const mount=new Element('div');
 const parent=new Element('details');parent.summary=new Element('summary');parent.summary.textContent='More';mount.parentDetails=parent;
 const controller=attachNativeControls(mount,'worker',{getJson:async()=>live,postJson:async()=>({status:'permission_submitted'}),...helpers});
 controller.updateLive(live);
 const [heading,status,requests,text,send,cancel]=mount.children[0].children;
 return {controller,panel:mount.children[0],parentHeading:parent.summary,heading,status,requests,text,send,cancel};
}
'''.replace('MODULE', json.dumps(module)) + body
    result = subprocess.run(['node', '--input-type=module'], input=code,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr + result.stdout


def test_busy_same_target_read_keeps_partly_typed_answer_and_disables_actions():
    node('''
const {controller,requests,send,cancel}=setup();
const section=requests.children[0],input=inputOf(section);input.value='Partly typed answer';
controller.updateLive({...live,available:false,pending_requests:[]});
assert.equal(requests.children[0],section,'Unavailable is not a confirmed empty request list');
assert.equal(input.value,'Partly typed answer');assert.equal(section.disabled,true);
assert.equal(send.disabled,true);assert.equal(cancel.disabled,true);
''')


def test_unknown_and_read_only_snapshots_retain_answers_until_confirmed_empty_or_new_target():
    node('''
const notices=[];
const {controller,requests}=setup({onResponseNeeded:value=>notices.push(value)});
let section=requests.children[0];inputOf(section).value='Keep my answer';
for(const unknown of [{}, {available:false}, {...live,available:false,pending_requests:[]}]){
 controller.updateLive(unknown);
 assert.equal(requests.children[0],section);assert.equal(inputOf(section).value,'Keep my answer');
 assert.equal(section.disabled,true);assert.equal(notices.at(-1).waiting,true);assert.equal(notices.at(-1).isNew,false);
 controller.updateLive(live);assert.equal(requests.children[0],section);assert.equal(section.disabled,false);
}
controller.updateLive({...live,read_only:true});assert.equal(section.disabled,true);
controller.updateLive(live);assert.equal(requests.children[0],section);assert.equal(section.disabled,false);
controller.updateLive({...live,pending_requests:[]});assert.equal(requests.children.length,0);assert.equal(notices.at(-1).waiting,false);
controller.updateLive(live);section=requests.children[0];inputOf(section).value='Old target answer';
controller.updateLive({...live,run_id:'run-B',attempt_id:'attempt-B'});
assert.notEqual(requests.children[0],section);assert.equal(inputOf(requests.children[0]).value,'');
assert.equal(notices.at(-1).isNew,true,'A valid new target owns a new request even when its local ID is reused');
''')


def test_read_and_busy_status_clear_on_recovery_without_erasing_question_draft():
    node('''
const {controller,panel,status,requests}=setup({getJson:async()=>{throw new Error('Synthetic read unavailable')}});
const section=requests.children[0];inputOf(section).value='Answer survives the read error';
panel.open=true;await controller.refresh();
assert.equal(requests.children[0],section);assert.equal(section.disabled,true);assert.equal(status.textContent,'Synthetic read unavailable');
controller.updateLive(live);
assert.equal(requests.children[0],section);assert.equal(inputOf(section).value,'Answer survives the read error');
assert.equal(status.textContent,'The worker is waiting for your response.');assert.equal(section.disabled,false);
controller.updateLive({...live,pending_requests:[]});
controller.updateLive({...live,available:false,pending_requests:[]});assert.equal(status.textContent,'Native controls are not ready for this turn.');
controller.updateLive({...live,pending_requests:[]});assert.equal(status.textContent,'');
panel.open=true;await controller.refresh();assert.equal(status.textContent,'Synthetic read unavailable');
controller.updateLive({...live,pending_requests:[]});assert.equal(status.textContent,'');
''')


def test_real_action_receipt_or_error_and_other_partial_answers_survive_read_recovery():
    node('''
for(const outcome of ['queued','rejected']){
 const {controller,panel,status,requests,text,send}=setup({getJson:async()=>{throw new Error('Read failed')},postJson:async()=>{
  if(outcome==='rejected')throw new Error('Native action rejected');return {status:'queued'};
 }});
 const section=requests.children[0];inputOf(section).value='Separate partial answer';
 text.value='Guidance beside the question';send.handlers.click();await settle();
 const notice=outcome==='queued'?'Message queued for the active turn.':'Native action rejected';assert.equal(status.textContent,notice);
 controller.updateLive({...live,available:false,pending_requests:[]});
 assert.equal(requests.children[0],section);assert.equal(inputOf(section).value,'Separate partial answer');assert.equal(status.textContent,notice);
 controller.updateLive(live);assert.equal(requests.children[0],section);assert.equal(status.textContent,notice);
 panel.open=true;await controller.refresh();assert.equal(requests.children[0],section);assert.equal(status.textContent,notice);
 controller.updateLive(live);assert.equal(status.textContent,notice);assert.equal(inputOf(section).value,'Separate partial answer');
}
''')


def test_end_of_turn_clears_question_and_response_needed_but_keeps_unsent_guidance():
    node('''
const notices=[];let reads=0;
const {controller,panel,parentHeading,heading,status,requests,text,send,cancel}=setup({
 onResponseNeeded:value=>notices.push(value),getJson:async()=>{reads+=1;return live}
});
inputOf(requests.children[0]).value='Old question answer';text.value='Unsent freeform guidance';
assert.equal(heading.textContent,'Response needed');assert.equal(parentHeading.textContent,'More · response needed');
controller.updateLive(null);
assert.equal(requests.children.length,0);assert.equal(heading.textContent,'Guide or respond');
assert.equal(parentHeading.textContent,'More');assert.equal(status.textContent,'');
assert.deepEqual(notices.at(-1),{waiting:false,isNew:false});
assert.equal(send.disabled,true);assert.equal(cancel.disabled,true);assert.equal(text.value,'Unsent freeform guidance');
panel.open=true;await controller.refresh();assert.equal(reads,0);
controller.updateLive({...live,run_id:'run-B',attempt_id:'attempt-B'});
assert.equal(inputOf(requests.children[0]).value,'');assert.equal(notices.at(-1).isNew,true);
assert.equal(text.value,'Unsent freeform guidance');
''')


def test_end_of_turn_clears_queued_unconfirmed_cancel_error_and_unavailable_notices():
    node('''
for(const outcome of ['queued','pending','cancel_requested','permission_submitted','rejected','unavailable']){
 const {controller,status,requests,text,send,cancel}=setup({postJson:async()=>{
  if(outcome==='rejected')throw new Error('Native action rejected');return {status:outcome};
 }});
 if(outcome==='unavailable')controller.updateLive({...live,available:false,pending_requests:[]});
 else{ text.value='Guidance';(outcome==='cancel_requested'?cancel:send).handlers.click();await settle(); }
 assert.notEqual(status.textContent,'');
 controller.updateLive(null);
 assert.equal(status.textContent,'',outcome+' notice must end with its native turn');
 assert.equal(requests.children.length,0);assert.equal(send.disabled,true);assert.equal(cancel.disabled,true);
}
''')


def test_end_of_turn_fences_late_action_and_read_replies():
    node('''
let finishPost,finishRead;
const {controller,panel,status,requests,text,send}=setup({
 postJson:()=>new Promise(resolve=>finishPost=resolve),getJson:()=>new Promise(resolve=>finishRead=resolve)
});
panel.open=true;const read=controller.refresh();
text.value='Sent guidance';send.handlers.click();await settle();
text.value='New unsent edit';controller.updateLive(null);
finishPost({status:'queued'});finishRead(live);await read;await settle();
assert.equal(status.textContent,'');assert.equal(requests.children.length,0);assert.equal(send.disabled,true);
assert.equal(text.value,'New unsent edit');
''')
