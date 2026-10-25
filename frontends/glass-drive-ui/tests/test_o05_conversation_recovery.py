"""O05: a stopped or failed reply shows its real outcome and a working recovery."""
import json
import shutil
import subprocess
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "glass_drive_ui" / "static" / "conversation.js"

# These tests extract individual page functions. Supply the module-level Library
# collaborator they do not import; actual Library UI behavior has its own cases.
LIBRARY_STUBS = r"""
let libraryOwnerReady=false, librarySelectionDirty=false, draftLibrarySignature='';
const chatLibrary={refs(){return [];},setRefs(){},setScope(){},setOwnerScope(){}};
const nativeWidgets=new Map();
const window={innerHeight:900,scrollY:0};
globalThis.document ||= {activeElement:null};
const markdown={render(text){return text;}};
function readAuthCookie(){return '';}
function attachNativeControls(){return {refresh(){},destroy(){}};}
"""


def with_collaborators(code):
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("function autoResizeMessage()")
    resize = source[start:source.index("\n}", start) + 2]
    return LIBRARY_STUBS + resize + "\n" + code


def test_ended_turns_state_their_outcome_and_blocked_turns_keep_the_durable_retry():
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("const blockerMessages")
    body = source[start:source.index("async function refresh()", start)]
    node = shutil.which("node")
    assert node, "Node is required for the conversation recovery proof"
    code = r'''
const posts = [];
class El {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.hidden=false;this.value='';this.style={};this.scrollHeight=78;this.textContent='';this.listeners={};this.classList={add(){},remove(){}};}
  append(...c){this.children.push(...c);} replaceChildren(...c){this.children=c;}
  addEventListener(t,f){this.listeners[t]=f;} contains(){return false;} focus(){this.focused=true;}
}
const elements = {};
const $ = (id) => (elements[id] ||= new El('div'));
globalThis.document = {createElement:(t)=>new El(t), cookie:'', activeElement:null,documentElement:{scrollHeight:0}};
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
// A typed account/model prerequisite keeps the text but cannot blindly resend.
const setupFailures = [
  ['model_configuration_required', 'The selected model is unavailable for this account. Check the account and model in Connections.'],
  ['provider_auth_missing', 'Reconnect or verify the selected account in Connections before trying again.'],
  ['provider_connected_account_reconnect_required', 'Reconnect or verify the selected account in Connections before trying again.'],
  ['provider_unauthorized', 'Reconnect or verify the selected account in Connections before trying again.'],
];
render({turns:setupFailures.map(([code]) => turn(code, code, `request-${code}`)),goals:[]});
for (const [i, [code, expected]] of setupFailures.entries()) {
  const message = $('messages').children.filter((item) => item.children.length)[i];
  if (!message || message.textContent !== `${expected} Your message is saved.` || message.children.length !== 1 || message.children[0].textContent !== 'Edit') throw new Error(`${code}: missing recovery or unsafe resend`);
  await message.children[0].listeners.click();
  if ($('message').value !== `msg ${code}` || !$('message').focused) throw new Error(`${code}: saved message lost`);
}
// Result delivery uses the same typed setup recovery, retains results and does not rerun a helper.
for (const [code, expected] of setupFailures) {
  render({turns:[{...internal,turn_id:`setup-${code}`,blocker:code}],goals:[]});
  const row=$('messages').children[0];
  if (row.textContent !== `${expected} The results are saved.`) throw new Error(`${code}: generic result error hid setup`);
  if (row.children[0].textContent !== 'Open Connections' || row.children[0].href !== '/#connections') throw new Error(`${code}: missing setup link`);
  if (row.children[1].textContent !== 'Try combining again') throw new Error(`${code}: saved result recovery lost`);
  await row.children[1].listeners.click();
  if (!posts.at(-1)[0].endsWith(`/turns/setup-${code}/retry-result`)) throw new Error(`${code}: result retry rerouted work`);
}
if (!$('stop-response').hidden) throw new Error('ended setup failure kept Stop response');
globalThis.fetch = async () => ({ok:false,status:409,json:async()=>({detail:'Choose a connected assistant before starting a conversation'})});
try { await api('/v1/coordinator/conversations'); throw new Error('expected refusal'); }
catch (error) { if (error.message !== 'Choose a connected assistant before starting a conversation') throw error; }
'''
    result = subprocess.run([node, "--input-type=module"], input=with_collaborators(code), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_a_goal_the_runtime_refused_to_start_says_it_is_blocked_and_why():
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("const blockerMessages")
    body = source[start:source.index("async function refresh()", start)]
    node = shutil.which("node")
    assert node, "Node is required for the goal state proof"
    code = r"""
class El {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.hidden=false;this.value='';this.style={};this.scrollHeight=78;this.textContent='';this.listeners={};this.classList={add(){},remove(){}};}
  append(...c){this.children.push(...c);} replaceChildren(...c){this.children=c;}
  addEventListener(t,f){this.listeners[t]=f;} contains(){return false;} focus(){} setAttribute(){}
}
const elements = {};
const $ = (id) => (elements[id] ||= new El('div'));
globalThis.document = {createElement:(t)=>new El(t), cookie:'', activeElement:null,documentElement:{scrollHeight:0}};
const base = '/v1/coordinator/conversations'; const conversationId = 'c1'; let latest = null;
const states = {accepted:'Accepted',queued:'Waiting to start',blocked:'Waiting to start'};
async function refresh() {}
""" + body + r"""
const refused = {goal_id:'poem',text:'Write the poem',state:'blocked',blocker:'ParallelExecutionIsolationError',run_id:'',worker_id:''};
const queued = {goal_id:'csv',text:'Make the CSV',state:'queued',blocker:'',run_id:'run-1',worker_id:'w-1'};
render({turns:[],goals:[refused, queued]});
const labels = $('goals').children.map((card) => card.children.find((c) => c.className === 'goal-state').textContent);
if (labels[0] !== 'Blocked: The worker runtime is not ready. Check setup, then Retry.') throw new Error(labels[0]);
if (labels[1] !== 'Waiting to start') throw new Error(labels[1]);
if (!$('goals').children[0].children.some((c) => c.textContent === 'Stop')) throw new Error('blocked goal lost Stop');
// Existing-worker admission blockers must name the recovery, not blame the assistant.
const workerBlockers = [
  ['worker_account_reconnect_required', 'Reconnect this worker’s account in Connections.'],
  ['worker_review_required', 'This worker needs review in Watch before it can receive work.'],
  ['worker_unavailable', 'This worker is unavailable for new work. Check its status in Watch.'],
];
render({turns:[],goals:workerBlockers.map(([blocker], i) => ({...refused,goal_id:`worker-${i}`,worker_id:'existing-worker',blocker}))});
for (const [i, [blocker, expected]] of workerBlockers.entries()) {
  const card = $('goals').children[i];
  const label = card.children.find((c) => c.className === 'goal-state').textContent;
  if (label !== `Blocked: ${expected}`) throw new Error(`${blocker}: ${label}`);
  if (!card.children.some((c) => c.textContent === 'Stop')) throw new Error(`${blocker}: lost Stop`);
  if (card.children.some((c) => c.textContent === 'Retry')) throw new Error(`${blocker}: invented Retry`);
}
if (blockerMessage('future_unknown_blocker') !== 'The assistant is unavailable right now.') throw new Error('unknown blocker lost safe fallback');
if (goalStateLabel({...queued,blocker:'worker_unavailable'}) !== 'Waiting to start') throw new Error('stale blocker replaced current queue state');

"""
    result = subprocess.run([node, "--input-type=module"], input=with_collaborators(code), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_uncertain_send_reconciles_saved_turn_and_reuses_key_if_not_saved():
    source = SOURCE.read_text(encoding="utf-8")
    submit = source[source.index("$('composer').addEventListener('submit'"):source.index("$('stop-response').addEventListener")]
    node = shutil.which("node")
    assert node, "Node is required for the conversation recovery proof"
    code = r'''
class El {
  constructor(){this.value='';this.style={};this.scrollHeight=78;this.disabled=false;this.textContent='';this.classList={add(){},remove(){}};this.listeners={};}
  addEventListener(type, fn){this.listeners[type]=fn;} focus(){}
}
const elements={}; const $=(id)=>(elements[id] ||= new El());
const base='/v1/coordinator/conversations';let conversationId='c1';let submitting=false;
let draftKey='';let draftText='';let posted=[];let saved=true;let latest=null;
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
    result = subprocess.run([node, "--input-type=module"], input=with_collaborators(code), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_a_conversation_this_deployment_cannot_run_says_why_and_offers_no_retry():
    """On `./xperfect start` a conversation with a connected account needs the shared Linux
    runtime. The reply states that and where to work instead; a Retry could never succeed."""
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("const blockerMessages")
    body = source[start:source.index("async function refresh()", start)]
    node = shutil.which("node")
    assert node, "Node is required for the conversation recovery proof"
    code = r'''
class El {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.hidden=false;this.value='';this.style={};this.scrollHeight=78;this.textContent='';this.listeners={};this.classList={add(){},remove(){}};}
  append(...c){this.children.push(...c);} replaceChildren(...c){this.children=c;}
  addEventListener(t,f){this.listeners[t]=f;} contains(){return false;} focus(){}
}
const elements = {};
const $ = (id) => (elements[id] ||= new El('div'));
globalThis.document = {createElement:(t)=>new El(t), cookie:'', activeElement:null,documentElement:{scrollHeight:0}};
const base = '/v1/coordinator/conversations'; const conversationId = 'c1'; let latest = null;
const states = {};
async function refresh() {}
globalThis.fetch = async () => ({ok:true, status:200, json:async()=>({})});
''' + body + r'''
const turn = (turn_id, blocker) => ({turn_id, origin:'interactive', message:`msg ${turn_id}`, blocker, request_id:'', response_json:''});
render({turns:[turn('linux','shared_linux_runtime_required'), turn('busy','provider_account_busy')], goals:[]});
const rows = $('messages').children.filter((c) => c.textContent.includes('Your message is saved'));
if (rows[0].textContent !== 'Conversations with a connected account need xPerfect on a configured Linux host, such as the packaged install. On this computer, use Run project. Your message is saved.') throw new Error(rows[0].textContent);
if (rows[0].children.length !== 0) throw new Error('a blocker this deployment cannot clear offered Retry');
if (rows[1].children.length !== 1 || rows[1].children[0].textContent !== 'Retry') throw new Error('a retryable blocker lost Retry');
console.log('ok');
'''
    result = subprocess.run([node, "--input-type=module"], input=with_collaborators(code), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr or result.stdout


def test_a_conversation_this_computer_cannot_start_says_why_before_anything_is_sent():
    """On `./xperfect start` a connected account's conversation needs the shared Linux runtime and
    this computer lacks the default assistant's CLI. The page states that and offers Run project
    before anything is typed, instead of after Send. An older runtime keeps every option."""
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("const blockerMessages")
    head = source[start:source.index("function endedMessage", start)]
    load = source[source.index("async function loadAssistants()"):source.index("$('composer').addEventListener('submit'")]
    node = shutil.which("node")
    assert node, "Node is required for the conversation start proof"
    code = 'import { renderAccountIdentity } from ' + json.dumps(SOURCE.with_name('account-menu.js').as_uri()) + ';\n' + r'''
class El { constructor(){this.hidden=false;this.disabled=false;this.textContent='';this.value='';this.children=[];} replaceChildren(...c){this.children=c;} }
const elements = {}; const $ = (id) => (elements[id] ||= new El());
globalThis.document = {getElementById:$};
function node(tag, text){ const e = new El(); e.textContent = text; return e; }
let conversationId = ''; let responses = {}; let latest=null;
async function api(path){ if (!(path in responses)) throw new Error('unexpected ' + path); const r = responses[path]; if (r instanceof Error) throw r; return r; }
function showError(error){ throw error; }
''' + head + load + r'''
const bootstrap = {provider_accounts:[{account_id:'acct_1',provider:'claude',status:'ready',label:'Existing Claude sign-in'}],
  workspace_type_options:[{value:'host',disabled:false}], user_preferences:{}};
const run = async (readiness, boot = bootstrap) => {
  for (const id in elements) delete elements[id];
  responses = {'/api/bootstrap': boot, '/v1/coordinator/readiness': readiness};
  await loadAssistants();
  return {send: $('send').disabled, message: $('message').disabled, project: $('conversation-project-field').hidden,
    field: $('conversation-assistant-field').hidden, run: $('conversation-run-project').hidden,
    connect: $('conversation-connect').hidden, status: $('status').textContent,
    options: $('conversation-assistant').children.map((o) => o.textContent), value: $('conversation-assistant').value};
};
console.log(JSON.stringify({
  here: await run({connected_account:{available:false,code:'shared_linux_runtime_required'},default:{available:false,code:'native_cli_missing'}}),
  older: await run(new Error('Not found')),
  packaged: await run({connected_account:{available:true,code:'ready'},default:{available:false,code:'native_cli_missing'}}),
  typedDirect: await run({connected_account:{available:true,code:'ready',supported_account_ids:['acct_1'],supported_accounts:[{account_id:'acct_1',profile:'direct-anthropic',auth_method:'api_key'}]},default:{available:false,code:'native_cli_missing'}}),
  unsupported: await run({connected_account:{available:true,code:'ready',supported_account_ids:[]},default:{available:true,code:'ready'}}),
  // A Linux host whose shared workspace is not set up: a known failure, before any input.
  unconfigured: await run({connected_account:{available:false,code:'shared_configuration_required'},default:{available:true,code:'ready'}},
    {...bootstrap, workspace_type_options: []}),
}));
'''
    result = subprocess.run([node, "--input-type=module"], input=with_collaborators(code), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    shown = json.loads(result.stdout)
    assert shown["here"] == {
        "send": True, "message": True, "project": True, "field": True, "run": False, "connect": True,
        "options": [], "value": "",
        "status": "Conversations with a connected account need xPerfect on a configured Linux host, such as the packaged install. On this computer, use Run project.",
    }
    assert shown["older"]["send"] is False and shown["older"]["message"] is False and shown["older"]["run"] is True
    assert shown["older"]["project"] is False
    assert shown["older"]["options"] == ["Claude Code · Existing Claude sign-in", "Assistant on this computer"]
    assert shown["packaged"]["options"] == ["Claude Code · Existing Claude sign-in"] and shown["packaged"]["value"] == "acct_1"
    assert shown["typedDirect"]["options"] == ["Anthropic · Direct · Existing Claude sign-in · API key"]
    assert shown["unsupported"]["options"] == ["Assistant on this computer"]
    assert shown["unconfigured"]["options"] == [] and shown["unconfigured"]["send"] and shown["unconfigured"]["message"]
    assert shown["unconfigured"]["run"] is False and shown["unconfigured"]["connect"] is True
    assert shown["unconfigured"]["status"] == ("Conversations with a connected account need this deployment's shared workspace, "
                                           "which is not available. Use Run project for now.")


def test_native_work_guidance_uses_declared_actions_and_dismiss_keeps_history():
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("const blockerMessages")
    body = source[start:source.index("async function refresh()", start)]
    node = shutil.which("node")
    assert node, "Node is required for the native work control proof"
    code = r'''
class El {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.hidden=false;this.value='';this.style={};this.scrollHeight=78;this.textContent='';this.listeners={};this.classList={add(){},remove(){}};}
  append(...c){this.children.push(...c);} replaceChildren(...c){this.children=c;}
  addEventListener(t,f){this.listeners[t]=f;} contains(){return false;} focus(){} setAttribute(){}
}
const elements={}; const $=(id)=>(elements[id] ||= new El('div'));
globalThis.document={createElement:(tag)=>new El(tag),cookie:'',activeElement:null,documentElement:{scrollHeight:0}};
const base='/v1/coordinator/conversations'; const conversationId='c1'; let latest=null;
const states={completed:'Completed',running:'Running'};
const posts=[]; let fail=true;
async function refresh(){}
globalThis.fetch=async(path,init)=>{posts.push([path,JSON.parse(init.body||'{}')]);if(fail)throw new Error('Synthetic lost response');return {ok:true,status:200,json:async()=>({})};};
''' + body + r'''
const goal={goal_id:'child',worker_id:'worker',run_id:'run',text:'Complete the work',state:'completed',has_result:true,actions:['queue','message','dismiss']};
render({turns:[],goals:[goal]});
const card=$('goals').children[0];
const guide=card.children.find((el)=>el.tag==='details' && el.children[0].textContent==='Give guidance');
if(!guide)throw new Error('missing native guidance');
const form=guide.children[1]; const [mode,input,send]=form.children;
if(mode.children.map((el)=>el.value).join(',')!=='message,queue')throw new Error('offered unsupported Steer');
mode.value='message';input.value='Preserve the original goal';
await form.listeners.submit({preventDefault(){}});
const first=posts.at(-1)[1];
fail=false;await form.listeners.submit({preventDefault(){}});
if(posts.at(-1)[1].idempotency_key!==first.idempotency_key)throw new Error('retry duplicated the guidance effect');
if(first.action!=='message'||first.run_id!=='run'||first.message!==input.value)throw new Error('guidance target changed');
const controls=card.children.find((el)=>el.className==='goal-actions');
const dismiss=controls.children.find((el)=>el.textContent==='Dismiss');
if(!dismiss)throw new Error('terminal card cannot be dismissed');
await dismiss.listeners.click();
if(posts.at(-1)[1].action!=='dismiss')throw new Error('dismiss used a lifecycle action');
render({turns:[],goals:[{...goal,dismissed:true}]});
if($('goals').children.length||!$('work').hidden)throw new Error('dismissed card still shown');
render({turns:[],goals:[{...goal,actions:[]}]});
if($('goals').children[0].children.some((el)=>el.tag==='details'&&el.children[0].textContent==='Give guidance'))throw new Error('empty action mask offers guidance');
render({turns:[],goals:[{...goal,actions:undefined}]});
if($('goals').children[0].children.some((el)=>el.tag==='details'&&el.children[0].textContent==='Give guidance'))throw new Error('terminal foreground handle offers child controls');
render({turns:[],goals:[{...goal,run_id:'queued-result',state:'queued',control_run_id:'paused-source',control_state:'paused',actions:['resume','message','queue','stop']}]});
const pausedControls=$('goals').children[0].children.find((el)=>el.className==='goal-actions');
const resume=pausedControls.children.find((el)=>el.textContent==='Resume');
if(!resume)throw new Error('queued result hid native paused Resume');
await resume.listeners.click();
if(posts.at(-1)[1].action!=='resume'||posts.at(-1)[1].run_id!=='paused-source')throw new Error('Resume targeted the queued result instead of exact paused source');
'''
    result = subprocess.run([node, "--input-type=module"], input=with_collaborators(code), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
