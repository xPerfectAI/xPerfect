import MarkdownIt from './vendor/markdown-it-15.0.2.mjs';
import { attachNativeControls } from './native-controls.js?v=20260923conversation1';

const $ = (id) => document.getElementById(id);
const markdown = new MarkdownIt({html:false,linkify:false,typographer:false});
const base = '/v1/coordinator/conversations';
let conversationId = new URLSearchParams(location.search).get('id') || '';
let latest = null;
let timer;
let closed = false;
let submitting = false;
let draftKey = '';
let draftText = '';
const nativeWidgets = new Map();
const requestedProjectId = new URLSearchParams(location.search).get('project_id') || '';
const states = {accepted:'Accepted',queued:'Waiting to start',running:'Working',paused:'Paused',needs_input:'Needs your input',completed:'Complete',failed:'Could not finish',cancelled:'Stopped',interrupted:'Interrupted',blocked:'Waiting to start'};
const blockerMessages = Object.freeze({
  provider_account_busy:'The connected assistant is busy.',
  host_capacity:'The workspace is busy.',
  provider_unavailable:'The connected assistant is unavailable.',
  provider_upstream_unavailable:'The connected assistant is unavailable.',
  provider_auth_missing:'Reconnect the connected assistant before retrying.',
  provider_auth_projection_unavailable:'Reconnect the connected assistant before retrying.',
  provider_connected_account_reconnect_required:'Reconnect the connected assistant before retrying.',
  provider_unauthorized:'Reconnect the connected assistant before retrying.',
  shared_capacity_busy:'The workspace is busy.',
  shared_resource_authority_unavailable:'The workspace is preparing.',
  ParallelExecutionIsolationError:'The worker runtime is not ready. Check setup, then Retry.',
  HostCapacityError:'The workspace capacity check is unavailable. Retry after it recovers.',
});
function blockerMessage(code) {
  return blockerMessages[code] || 'The assistant is unavailable right now.';
}
// A turn whose provider attempt already ran has a final outcome; Retry would replay it.
function endedMessage(code) {
  return code === 'cancelled' ? 'You stopped this reply.'
    : code === 'native_input_expired' ? 'The assistant stopped because a request for your response expired.'
    : 'The assistant could not finish this reply.';
}

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}
function assistantMessage(text) {
  const message=node('div',undefined,'message assistant');
  // markdown-it escapes raw HTML and rejects unsafe link schemes by default.
  message.innerHTML=markdown.render(text);
  return message;
}
function csrf() {
  const cookie = document.cookie.split(';').map((x) => x.trim()).find((x) => x.startsWith('glasshive_csrf='));
  return cookie ? decodeURIComponent(cookie.slice('glasshive_csrf='.length)) : '';
}
async function api(path, method='GET', body) {
  const response = await fetch(path, {method, credentials:'same-origin', headers:{'Content-Type':'application/json','X-GlassHive-CSRF':csrf()}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) throw new Error('Sign in to continue. Your message is still here.');
    if (['coordinator_not_configured', 'model_configuration_required'].includes(result.detail?.code)) throw new Error(result.detail.message);
    if (response.status === 403) throw new Error('This conversation is not available to this account.');
    if ([400, 409, 503].includes(response.status) && typeof result.detail === 'string' && result.detail.length <= 300)
      throw new Error(result.detail);
    throw new Error('Could not complete that action. Try again.');
  }
  return result;
}
let statusErrorSource = '';
function showError(error, source='action') {
  statusErrorSource=source;
  $('status').textContent=error.message;
  $('status').classList.add('error');
}
function clearError(source='') {
  if (!statusErrorSource || (source && statusErrorSource!==source)) return;
  statusErrorSource='';
  $('status').textContent='';
  $('status').classList.remove('error');
}
function action(label, fn) {
  const button = node('button', label); button.type='button';
  button.addEventListener('click', async () => {button.disabled=true; try {await fn(); await refresh();clearError();} catch (error) {showError(error);} finally {button.disabled=false;}});
  return button;
}
function render(snapshot) {
  latest=snapshot;
  const firstPresentation=$('messages').dataset.version===undefined;
  const nearLatest=firstPresentation || (typeof window!=='undefined' && window.scrollY+window.innerHeight>=document.documentElement.scrollHeight-96);
  $('conversation-project-field').hidden=true;
  $('conversation-assistant-field').hidden=true;
  $('conversation-connect').hidden=true;
  $('empty').hidden=snapshot.turns.length>0;
  const messages=[];
  for (const turn of snapshot.turns) {
    if (turn.origin === 'interactive') messages.push(node('div',turn.message,'message user'));
    if (turn.response_json) {
      const response=JSON.parse(turn.response_json);
      for (const choice of response.choices || []) if (choice.message?.content) messages.push(assistantMessage(choice.message.content));
    }
    if (turn.origin === 'interactive' && turn.blocker && !turn.response_json && turn.request_id) {
      const ended=node('div',`${endedMessage(turn.blocker)} Your message is saved.`,'message');
      const resendKey=`resend:${turn.turn_id}`;
      if (resendKey.length<=160 && !snapshot.turns.some((item) => item.turn_id===resendKey)) {
        ended.append(action('Send again',() => api(`${base}/${encodeURIComponent(conversationId)}/turns`,'POST',{
          idempotency_key:resendKey,message:turn.message,
        })));
      }
      ended.append(action('Edit',async () => {$('message').value=turn.message;$('message').focus();}));
      messages.push(ended);
    } else if (turn.origin === 'interactive' && turn.blocker && !turn.response_json && turn.retry_after_at) {
      messages.push(node('div',`${blockerMessage(turn.blocker)} Your message is saved and will start automatically.`,'message'));
    } else if (turn.origin === 'interactive' && turn.blocker && !turn.response_json) {
      const blocked=node('div',`${blockerMessage(turn.blocker)} Your message is saved.`,'message');
      blocked.append(action('Retry',() => api(`${base}/${encodeURIComponent(conversationId)}/turns`,'POST',{idempotency_key:turn.turn_id,message:turn.message})));
      messages.push(blocked);
    } else if (turn.origin === 'worker_results' && turn.blocker && !turn.response_json && turn.request_id) {
      const later=snapshot.turns.slice(snapshot.turns.indexOf(turn)+1);
      const laterReply=later.some((item)=>Boolean(item.response_json));
      const laterAttempt=later.some((item)=>item.origin==='worker_results' && item.message===turn.message);
      if (!laterReply && !laterAttempt) {
        const failed=node('div','I could not combine the completed work. The results are saved.','message');
        failed.append(action('Try combining again',() => api(`${base}/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turn.turn_id)}/retry-result`,'POST')));
        messages.push(failed);
      }
    }
  }
  // Preserve active forms and text selection while status is unchanged.
  const digest=JSON.stringify(snapshot.turns);
  if ($('messages').dataset.version !== digest) {
    $('messages').replaceChildren(...messages);
    $('messages').dataset.version=digest;
    if (messages.length && nearLatest && typeof window!=='undefined') window.requestAnimationFrame?.(() => window.scrollTo(0,document.documentElement.scrollHeight));
  }
  $('work').hidden=snapshot.goals.length===0 && !(snapshot.foreground_native_runs || []).length;
  const goalDigest=JSON.stringify(snapshot.goals);
  if ($('goals').dataset.version !== goalDigest && !$('goals').contains(document.activeElement)) {
    const cards=snapshot.goals.map((goal) => {
      const card=node('section',undefined,'goal'); card.append(node('p',goal.text),node('span',states[goal.state] || goal.state,'goal-state'));
      if (!goal.run_id && ['accepted','blocked'].includes(goal.state)) card.append(action('Stop',() => api(`${base}/${encodeURIComponent(conversationId)}/goals/${encodeURIComponent(goal.goal_id)}/control`,'POST',{action:'stop',run_id:'',idempotency_key:crypto.randomUUID()})));
      if (goal.run_id) {
        const controls=node('div',undefined,'goal-actions');
        const control=(name,message='',key=crypto.randomUUID()) => api(`${base}/${encodeURIComponent(conversationId)}/goals/${encodeURIComponent(goal.goal_id)}/control`,'POST',{action:name,run_id:goal.run_id,idempotency_key:key,message});
        const sharedReply=snapshot.goals.filter((item)=>item.run_id===goal.run_id).length>1;
        if (!sharedReply && goal.state==='failed' && goal.retryable) {
          const retryKey=crypto.randomUUID();
          controls.append(action('Retry',() => control('retry','',retryKey)));
        }
        if (!sharedReply && ['running','queued','needs_input'].includes(goal.state)) controls.append(action('Pause',() => control('pause')));
        if (!sharedReply && goal.state==='paused') controls.append(action('Resume',() => control('resume')));
        if (!sharedReply && ['running','queued','needs_input','paused'].includes(goal.state)) {
          controls.append(action('Stop',() => control('stop')));
          const guide=node('details'); guide.append(node('summary','Give guidance'));
          const form=node('form',undefined,'steer-form'); const input=node('textarea'); input.setAttribute('aria-label','Guidance for this work'); input.required=true;
          const send=node('button','Send guidance'); send.type='submit'; form.append(input,send);
          form.addEventListener('submit',async(event)=>{event.preventDefault();send.disabled=true;try{await control('steer',input.value);guide.open=false;await refresh();clearError();}catch(error){showError(error);}finally{send.disabled=false;}});
          guide.append(form); card.append(guide);
        }
        const link=node('a','Open work'); link.href=`/watch/${encodeURIComponent(goal.worker_id)}`; controls.append(link); card.append(controls);
      }
      if (goal.has_result) {
        const result=node('details');const content=node('div',undefined,'result');result.append(node('summary','Result'),content);
        let offset=0;let loading=false;const more=action('Read more',load);
        async function load(){if(loading)return;loading=true;try{const page=await api(`${base}/${encodeURIComponent(conversationId)}/goals/${encodeURIComponent(goal.goal_id)}/result?offset=${offset}`);content.append(document.createTextNode(page.output_text));offset=page.next_offset;more.hidden=offset===null;}finally{loading=false;}}
        result.addEventListener('toggle',()=>{if(result.open && !content.textContent)load().catch(showError);});result.append(more);card.append(result);
      }
      return card;
    });
    $('goals').replaceChildren(...cards); $('goals').dataset.version=goalDigest;
  }
  const active=snapshot.turns.find((turn) => turn.request_id && !turn.response_json && !turn.blocker);
  $('stop-response').hidden=!active;
  $('stop-response').dataset.turn=active?.turn_id || '';
  // A saved turn can recover on Retry. Keep the status aligned with its live state.
  if (['Your message is saved. The assistant is not available yet.', 'Working on your request.'].includes($('status').textContent))
    $('status').textContent=active ? 'Working on your request.' : '';
}
async function refresh() {
  if (!conversationId || closed) return;
  const snapshot=await api(`${base}/${encodeURIComponent(conversationId)}`);
  render(snapshot);
  await refreshNativeControls(snapshot);
}
async function refreshNativeControls(snapshot) {
  const active=new Map();
  for (const goal of snapshot.goals || []) {
    if (goal.worker_id && goal.run_id && ['running','needs_input'].includes(goal.state)) {
      active.set(goal.worker_id,goal.run_id);
    }
  }
  for (const run of snapshot.foreground_native_runs || []) {
    if (run.worker_id && run.run_id) active.set(run.worker_id,run.run_id);
  }
  for (const [workerId,entry] of nativeWidgets) {
    if (!active.has(workerId)) {entry.mount.remove();nativeWidgets.delete(workerId);}
  }
  for (const [workerId,runId] of active) {
    try {
      const state=await api(`/api/worker/${encodeURIComponent(workerId)}/native-control`);
      if (state.available===false || state.run_id!==runId) continue;
      let entry=nativeWidgets.get(workerId);
      if (!entry) {
        const mount=node('div');$('native-controls').append(mount);
        const widget=attachNativeControls(mount,workerId,{
          getJson:(path)=>api(path),
          postJson:(path,body)=>api(path,'POST',body),
          onResponseNeeded:({waiting})=>{document.title=waiting?'Response needed · xPerfect':'Conversation · xPerfect';},
        });
        entry={mount,widget};nativeWidgets.set(workerId,entry);
      }
      entry.widget.updateLive(state);
    } catch (_) { /* A workspace without native controls still has its Watch link. */ }
  }
}
async function poll() {
  try {await refresh();clearError('poll');} catch (error) {showError(error,'poll');}
  if (!closed) timer=setTimeout(poll,1500);
}
async function loadProjects() {
  if (conversationId) { $('conversation-project-field').hidden=true; return; }
  try {
    const projects=await api('/api/conversation-projects');
    const select=$('conversation-project');
    for (const project of projects.items || []) {
      const option=node('option',project.title || project.project_id);
      option.value=project.project_id;
      select.append(option);
    }
    if (requestedProjectId && [...select.options].some((item)=>item.value===requestedProjectId)) select.value=requestedProjectId;
  } catch (error) { showError(error); }
}
async function loadAssistants() {
  if (conversationId) return;
  try {
    const bootstrap=await api('/api/bootstrap');
    const select=$('conversation-assistant');
    const names={codex:'Codex',openai:'Codex',claude:'Claude Code',anthropic:'Claude Code',grok:'Grok Build',xai:'Grok Build'};
    const accounts=(bootstrap.provider_accounts || []).filter((account) =>
      account.status === 'ready' && account.account_id && names[String(account.provider || '').toLowerCase()]);
    const options=accounts.map((account) => {
      const option=node('option',`${names[String(account.provider).toLowerCase()]} · ${account.label || 'Connected account'}`);
      option.value=account.account_id; return option;
    });
    if ((bootstrap.workspace_type_options || []).some((item) => item.value === 'host' && !item.disabled)) {
      const option=node('option','Assistant on this computer'); option.value=''; options.push(option);
    }
    select.replaceChildren(...options);
    const preferred=String(bootstrap.user_preferences?.default_worker_profile || '');
    const matched=accounts.find((account) => ({codex:'codex-cli',openai:'codex-cli',claude:'claude-code',anthropic:'claude-code',grok:'grok-build',xai:'grok-build'})[String(account.provider).toLowerCase()] === preferred);
    select.value=matched?.account_id || accounts[0]?.account_id || '';
    const available=options.length > 0;
    $('send').disabled=!available;
    $('conversation-connect').hidden=available;
    if (!available) $('status').textContent='Connect an assistant to start.';
  } catch (error) {showError(error); $('send').disabled=true;}
}
$('composer').addEventListener('submit',async(event)=>{
  event.preventDefault();if(submitting)return;
  const message=$('message').value;if(!message.trim())return;
  submitting=true;$('send').disabled=true;clearError();$('status').textContent='';
  try {
    if (!conversationId) {
      const projectId=$('conversation-project').value;
      const accountId=$('conversation-assistant').value;
      const created=await api(base,'POST',{
        ...(projectId ? {scope:{project_id:projectId}} : {}),
        ...(accountId ? {account_id:accountId} : {}),
      });
      conversationId=created.conversation_id;
      history.replaceState(null,'',`?id=${encodeURIComponent(conversationId)}`);
      $('conversation-project-field').hidden=true;
      $('conversation-assistant-field').hidden=true;
      $('conversation-connect').hidden=true;
    }
    if(draftText!==message){draftKey=crypto.randomUUID();draftText=message;}
    const submittedKey=draftKey;
    try {
      const receipt=await api(`${base}/${encodeURIComponent(conversationId)}/turns`,'POST',{idempotency_key:submittedKey,message});
      $('message').value='';draftText='';draftKey='';
      if(receipt.state==='blocked')$('status').textContent='Your message is saved. The assistant is not available yet.';
      await refresh();
    } catch (error) {
      // The response can fail after the runtime has saved the turn. Read the
      // durable turn before showing an error or allowing another Send.
      let snapshot;
      try {
        snapshot=await api(`${base}/${encodeURIComponent(conversationId)}`);
        render(snapshot);
      } catch (_) { /* Keep the draft for an idempotent retry. */ }
      const saved=snapshot?.turns?.find((turn) => turn.origin==='interactive' && turn.turn_id===submittedKey && turn.message===message);
      if (!saved) throw error;
      $('message').value='';draftText='';draftKey='';
      clearError();
      $('status').textContent=saved.response_json ? ''
        : saved.request_id && !saved.blocker ? 'Working on your request.'
        : 'Your message is saved. The assistant is not available yet.';
    }
  }catch(error){showError(error);}finally{submitting=false;$('send').disabled=false;$('message').focus();}
});
$('stop-response').addEventListener('click',async()=>{try{await api(`${base}/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent($('stop-response').dataset.turn)}/stop`,'POST');await refresh();clearError();}catch(error){showError(error);}});
$('new-conversation').addEventListener('click',()=>{location.href=location.pathname;});
window.addEventListener('pagehide',()=>{closed=true;clearTimeout(timer);});
loadProjects();
loadAssistants();
poll();
