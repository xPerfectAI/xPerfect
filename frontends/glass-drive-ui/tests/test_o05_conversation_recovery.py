"""O05: a stopped or failed reply shows its real outcome and a working recovery."""
import shutil
import subprocess
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "glass_drive_ui" / "static" / "conversation.js"


def test_ended_turns_state_their_outcome_and_blocked_turns_keep_the_durable_retry():
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("const blockerMessages")
    body = source[start:source.index("async function refresh()", start)]
    node = shutil.which("node")
    assert node, "Node is required for the conversation recovery proof"
    code = r'''
const posts = [];
class El {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.hidden=false;this.value='';this.textContent='';this.listeners={};this.classList={add(){},remove(){}};}
  append(...c){this.children.push(...c);} replaceChildren(...c){this.children=c;}
  addEventListener(t,f){this.listeners[t]=f;} contains(){return false;} focus(){this.focused=true;}
}
const elements = {};
const $ = (id) => (elements[id] ||= new El('div'));
globalThis.document = {createElement:(t)=>new El(t), cookie:'', activeElement:null};
const base = '/v1/coordinator/conversations'; const conversationId = 'c1'; let latest = null;
const states = {};
async function refresh() {}
globalThis.fetch = async (path, init) => { posts.push([path, JSON.parse(init.body || '{}')]); return {ok:true, status:200, json:async()=>({})}; };
''' + body + r'''
const turn = (turn_id, blocker, request_id) => ({turn_id, origin:'interactive', message:`msg ${turn_id}`, blocker, request_id, response_json:''});
render({turns:[turn('stopped','cancelled','req-1'), turn('failed','failed','req-2'), turn('expired','native_input_expired','req-3'), turn('blocked','provider_unavailable','')], goals:[]});
const rows = $('messages').children.filter((c) => c.children.length);
const text = rows.map((r) => r.textContent);
const labels = rows.map((r) => r.children[0].textContent);
if (text[0] !== 'You stopped this reply. Your message is saved.') throw new Error(text[0]);
if (text[1] !== 'The assistant could not finish this reply. Your message is saved.') throw new Error(text[1]);
if (text[2] !== 'The assistant stopped because a request for your response expired. Your message is saved.') throw new Error(text[2]);
if (text[3] !== 'The connected assistant is unavailable. Your message is saved.') throw new Error(text[3]);
if (JSON.stringify(labels) !== JSON.stringify(['Send again','Send again','Send again','Retry'])) throw new Error(JSON.stringify(labels));
await rows[1].children[0].listeners.click();
if (posts.length !== 1 || posts[0][1].idempotency_key !== 'resend:failed' || posts[0][1].message !== 'msg failed') throw new Error('send again must submit the saved turn once');
await rows[1].children[0].listeners.click();
if (posts[1][1].idempotency_key !== posts[0][1].idempotency_key) throw new Error('resend key changed on retry');
await rows[1].children[1].listeners.click();
if ($('message').value !== 'msg failed' || !$('message').focused) throw new Error('edit did not restore the draft');
showError(new Error('A prior control failed.'));
await rows[3].children[0].listeners.click();
if ($('status').textContent) throw new Error('successful later action kept a stale error');
if (posts.length !== 3 || posts[2][1].idempotency_key !== 'blocked') throw new Error(JSON.stringify(posts));
render({turns:[turn('failed','failed','req-2'), turn('resend:failed','','req-new')], goals:[]});
const endedAfterResend = $('messages').children.find((item) => item.textContent === 'The assistant could not finish this reply. Your message is saved.');
if (endedAfterResend.children.length !== 1 || endedAfterResend.children[0].textContent !== 'Edit') throw new Error('completed resend still offers duplicate action');
// A stopped reply no longer shows the running reply's Stop control.
if ($('stop-response').hidden) throw new Error('active retry lost Stop control');
// Internal result turns carry machine JSON. Show a human recovery action without exposing it.
const internal = {turn_id:'internal-result',origin:'worker_results',message:'{"goal_results":[]}',blocker:'failed',request_id:'req-internal',response_json:''};
const failedGoal = {goal_id:'goal-1',worker_id:'worker-1',run_id:'run-failed',state:'failed',retryable:true,text:'Prepare the document',has_result:false};
render({turns:[internal],goals:[failedGoal]});
if ($('messages').children.length !== 1 || $('messages').children[0].textContent.includes('goal_results')) throw new Error('internal result payload leaked');
if ($('messages').children[0].children[0].textContent !== 'Try combining again') throw new Error('missing result recovery');
await $('messages').children[0].children[0].listeners.click();
if (!posts.at(-1)[0].endsWith('/turns/internal-result/retry-result')) throw new Error('wrong result retry endpoint');
const card = $('goals').children[0];
const controls = card.children.find((child) => child.className === 'goal-actions');
const retry = controls.children.find((child) => child.textContent === 'Retry');
if (!retry) throw new Error('failed retryable child has no Retry');
await retry.listeners.click();
const firstRetry = posts.at(-1);
if (!firstRetry[0].endsWith('/goals/goal-1/control') || firstRetry[1].action !== 'retry' || firstRetry[1].run_id !== 'run-failed') throw new Error(JSON.stringify(firstRetry));
await retry.listeners.click();
if (posts.at(-1)[1].idempotency_key !== firstRetry[1].idempotency_key) throw new Error('the same Retry click changed its key');
render({turns:[internal],goals:[{...failedGoal,retryable:false}]});
const closedControls = $('goals').children[0].children.find((child) => child.className === 'goal-actions');
if (closedControls.children.some((child) => child.textContent === 'Retry')) throw new Error('nonretryable child still offers Retry');
render({turns:[],goals:[],foreground_native_runs:[{worker_id:'foreground',run_id:'active'}]});
if ($('work').hidden) throw new Error('foreground native input is hidden before goals exist');
globalThis.fetch = async () => ({ok:false,status:409,json:async()=>({detail:'Choose a connected assistant before starting a conversation'})});
try { await api('/v1/coordinator/conversations'); throw new Error('expected refusal'); }
catch (error) { if (error.message !== 'Choose a connected assistant before starting a conversation') throw error; }
'''
    result = subprocess.run([node, "--input-type=module"], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_uncertain_send_reconciles_saved_turn_and_reuses_key_if_not_saved():
    source = SOURCE.read_text(encoding="utf-8")
    submit = source[source.index("$('composer').addEventListener('submit'"):source.index("$('stop-response').addEventListener")]
    node = shutil.which("node")
    assert node, "Node is required for the conversation recovery proof"
    code = r'''
class El {
  constructor(){this.value='';this.disabled=false;this.textContent='';this.classList={add(){},remove(){}};this.listeners={};}
  addEventListener(type, fn){this.listeners[type]=fn;} focus(){}
}
const elements={}; const $=(id)=>(elements[id] ||= new El());
const base='/v1/coordinator/conversations';let conversationId='c1';let submitting=false;
let draftKey='';let draftText='';let posted=[];let saved=true;
const history={replaceState(){}};
function showError(error){$('status').textContent=error.message;$('status').classList.add('error');}
function clearError(){$('status').textContent='';$('status').classList.remove('error');}
async function api(path,method,body){
  if (method==='POST'){posted.push(body);throw new Error('The request timed out.');}
  return {turns:saved?[{origin:'interactive',turn_id:posted.at(-1).idempotency_key,message:$('message').value,request_id:'req-1',blocker:'',response_json:''}]:[]};
}
function render(){} async function refresh(){}
''' + submit + r'''
const send=$('composer').listeners.submit;
$('message').value='One task';
await send({preventDefault(){}});
if ($('message').value || $('status').textContent!=='Working on your request.') throw new Error('accepted turn shown as a failed send');
const firstKey=posted[0].idempotency_key;
saved=false;$('message').value='Another task';
await send({preventDefault(){}});
if ($('message').value!=='Another task' || $('status').textContent!=='The request timed out.') throw new Error('unsaved draft was lost');
await send({preventDefault(){}});
if (posted[1].idempotency_key!==posted[2].idempotency_key || posted[1].idempotency_key===firstKey) throw new Error('retry duplicated a turn');
'''
    result = subprocess.run([node, "--input-type=module"], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
