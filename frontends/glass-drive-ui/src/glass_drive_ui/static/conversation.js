import { createLibrarySelection } from './personal-library.js?v=20261024polish1';
import { readAuthCookie } from './auth-cookies.js?v=20261024cookies1';
import { renderAccountIdentity } from './account-menu.js?v=20261024polish1';
import MarkdownIt from './vendor/markdown-it-15.0.3.mjs';
import { attachNativeControls } from './native-controls.js?v=20261024polish1';

const $ = (id) => document.getElementById(id);
function autoResizeMessage() {
  const input = $('message');
  input.style.height = 'auto';
  const viewport = window.visualViewport?.height || window.innerHeight || 850;
  const maximum = Math.min(360, Math.max(78, viewport * .42));
  input.style.height = `${Math.min(Math.max(input.scrollHeight, 78), maximum)}px`;
  if (document.activeElement === input) $('composer').scrollIntoView({block:'nearest'});
}
const markdown = new MarkdownIt({html:false,linkify:false,typographer:false});
const base = '/v1/coordinator/conversations';
let conversationId = new URLSearchParams(location.search).get('id') || '';
let latest = null;
let timer;
let closed = false;
let submitting = false;
let draftKey = '';
let draftText = '';
let draftLibrarySignature = '';
let librarySelectionDirty = false;
let libraryOwnerReady = false;
const chatLibrary = createLibrarySelection({ button: $('chat-choose-library'), list: $('chat-library-selection'), scope: `chat.${conversationId || 'new'}`, onChange: () => { librarySelectionDirty = true; } });
const nativeWidgets = new Map();
const requestedProjectId = new URLSearchParams(location.search).get('project_id') || '';
const states = {accepted:'Accepted',queued:'Waiting to start',running:'Working',paused:'Paused',needs_input:'Needs your input',completed:'Complete',failed:'Could not finish',cancelled:'Stopped',interrupted:'Interrupted',blocked:'Blocked'};
const blockerMessages = Object.freeze({
  worker_account_reconnect_required:'Reconnect this worker’s account in Connections.',
  worker_review_required:'This worker needs review in Watch before it can receive work.',
  worker_unavailable:'This worker is unavailable for new work. Check its status in Watch.',
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
  shared_linux_runtime_required:'Conversations with a connected account need xPerfect on a configured Linux host, such as the packaged install. On this computer, use Run project.',
  shared_runtime_unavailable:'Conversations with a connected account need xPerfect on a configured Linux host, such as the packaged install. On this computer, use Run project.',
  shared_configuration_required:'Conversations with a connected account need this deployment\'s shared workspace, which is not available. Use Run project for now.',
  shared_configuration_invalid:'Conversations with a connected account need this deployment\'s shared workspace, which is not available. Use Run project for now.',
  shared_storage_authority_unavailable:'Conversations with a connected account need this deployment\'s shared workspace, which is not available. Use Run project for now.',
  shared_account_projection_unavailable:'Conversations with a connected account need this deployment\'s shared workspace, which is not available. Use Run project for now.',
  shared_owner_storage_unavailable:'Conversations with a connected account need this deployment\'s shared workspace, which is not available. Use Run project for now.',
  shared_account_container_unavailable:'Conversations with a connected account need this deployment\'s shared workspace, which is not available. Use Run project for now.',
  shared_account_recovery_pending:'A connected account is being recovered. Try again shortly.',
});
// A blocker no retry can clear on this deployment offers no Retry.
const unavailableHere = new Set(['shared_linux_runtime_required', 'shared_runtime_unavailable']);
function blockerMessage(code) {
  return blockerMessages[code] || 'The assistant is unavailable right now.';
}
// A goal the runtime refused to start says so, and why, instead of seeming to wait.
function goalStateLabel(goal) {
  const state=goal.control_state || goal.state;
  return goal.state === 'blocked' && goal.blocker ? `Blocked: ${blockerMessage(goal.blocker)}` : states[state] || state;
}
// Why a connected account's conversation cannot start here, before anything is typed.
function connectedUnavailableMessage(code) {
  return blockerMessages[code]
    || 'Conversations with a connected account are not available on this deployment right now. Use Run project for now.';
}
// Account/model failures need a setup change before another provider request.
const endedSetupRequired = new Set(['model_configuration_required', 'provider_auth_missing', 'provider_connected_account_reconnect_required', 'provider_unauthorized']);
// A turn whose provider attempt already ran has a final outcome; Retry would replay it.
function endedMessage(code) {
  if (endedSetupRequired.has(code)) return code === 'model_configuration_required'
    ? 'The selected model is unavailable for this account. Check the account and model in Connections.'
    : 'Reconnect or verify the selected account in Connections before trying again.';
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
  return readAuthCookie('glasshive_csrf');
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
  if (libraryOwnerReady && !librarySelectionDirty && Array.isArray(snapshot.library?.refs)) {
    chatLibrary.setRefs(snapshot.library?.refs, snapshot.library.refs.map(ref => { const item = (snapshot.library.items || []).find(item => item.item_id === ref.item_id); return item ? `${item.name} · v${item.version}` : 'Saved Library item'; }));
  }
  const notLoaded = (snapshot.library?.context || []).filter(source => source.loaded === false && ['binary', 'over_limit'].includes(source.reason));
  $('chat-library-status').hidden = !notLoaded.length;
  $('chat-library-status').textContent = notLoaded.length ? 'Some selected files are not loaded for this Queen. Their full references stay available to its workers.' : '';
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
      if (!endedSetupRequired.has(turn.blocker) && resendKey.length<=160 && !snapshot.turns.some((item) => item.turn_id===resendKey)) {
        ended.append(action('Send again',() => api(`${base}/${encodeURIComponent(conversationId)}/turns`,'POST',{
          idempotency_key:resendKey,message:turn.message,
        })));
      }
      ended.append(action('Edit',async () => {$('message').value=turn.message;$('message').focus();autoResizeMessage();}));
      messages.push(ended);
    } else if (turn.origin === 'interactive' && turn.blocker && !turn.response_json && turn.retry_after_at) {
      messages.push(node('div',`${blockerMessage(turn.blocker)} Your message is saved and will start automatically.`,'message'));
    } else if (turn.origin === 'interactive' && turn.blocker && !turn.response_json) {
      const blocked=node('div',`${blockerMessage(turn.blocker)} Your message is saved.`,'message');
      if (!unavailableHere.has(turn.blocker)) {
        blocked.append(action('Retry',() => api(`${base}/${encodeURIComponent(conversationId)}/turns`,'POST',{idempotency_key:turn.turn_id,message:turn.message})));
      }
      messages.push(blocked);
    } else if (turn.origin === 'worker_results' && turn.blocker && !turn.response_json && turn.request_id) {
      const later=snapshot.turns.slice(snapshot.turns.indexOf(turn)+1);
      const laterReply=later.some((item)=>Boolean(item.response_json));
      const laterAttempt=later.some((item)=>item.origin==='worker_results' && item.message===turn.message);
      if (!laterReply && !laterAttempt) {
        const setupRequired=endedSetupRequired.has(turn.blocker);
        const failed=node('div',setupRequired
          ? `${endedMessage(turn.blocker)} The results are saved.`
          : 'I could not combine the completed work. The results are saved.','message');
        if (setupRequired) {
          const connection=node('a','Open Connections');
          connection.href='/#connections';
          failed.append(connection);
        }
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
  $('work').hidden=!snapshot.goals.some((goal)=>!goal.dismissed) && !(snapshot.foreground_native_runs || []).length;
  const goalDigest=JSON.stringify(snapshot.goals);
  if ($('goals').dataset.version !== goalDigest && !$('goals').contains(document.activeElement)) {
    const cards=snapshot.goals.filter((goal)=>!goal.dismissed).map((goal) => {
      const card=node('section',undefined,'goal'); card.append(node('p',goal.text),node('span',goalStateLabel(goal),'goal-state'));
      if (!goal.run_id && ['accepted','blocked'].includes(goal.state)) card.append(action('Stop',() => api(`${base}/${encodeURIComponent(conversationId)}/goals/${encodeURIComponent(goal.goal_id)}/control`,'POST',{action:'stop',run_id:'',idempotency_key:crypto.randomUUID()})));
      if (goal.run_id) {
        const controls=node('div',undefined,'goal-actions');
        const controlState=goal.control_state || goal.state;
        const control=(name,message='',key=crypto.randomUUID()) => api(`${base}/${encodeURIComponent(conversationId)}/goals/${encodeURIComponent(goal.goal_id)}/control`,'POST',{action:name,run_id:goal.control_run_id || goal.run_id,idempotency_key:key,message});
        const sharedReply=snapshot.goals.filter((item)=>item.run_id===goal.run_id).length>1;
        const can=(name)=>!Array.isArray(goal.actions)||goal.actions.includes(name);
        if (!sharedReply && can('retry') && goal.state==='failed' && goal.retryable) {
          const retryKey=crypto.randomUUID();
          controls.append(action('Retry',() => control('retry','',retryKey)));
        }
        if (!sharedReply && can('pause') && ['running','queued','needs_input'].includes(controlState)) controls.append(action('Pause',() => control('pause')));
        if (!sharedReply && can('resume') && controlState==='paused') controls.append(action('Resume',() => control('resume')));
        if (!sharedReply && ['running','queued','needs_input','paused'].includes(controlState) && can('stop')) controls.append(action('Stop',() => control('stop')));
        const guidanceActions=Array.isArray(goal.actions) ? ['message','queue','steer'].filter((name)=>can(name)) : (['running','queued','needs_input','paused'].includes(goal.state) ? ['steer'] : []);
        if (!sharedReply && guidanceActions.length) {
          const guide=node('details'); guide.append(node('summary','Give guidance'));
          const form=node('form',undefined,'steer-form'); const input=node('textarea'); input.setAttribute('aria-label','Guidance for this work'); input.required=true;
          const mode=node('select'); mode.setAttribute('aria-label','Guidance action');
          for(const name of guidanceActions){const option=node('option',name[0].toUpperCase()+name.slice(1));option.value=name;mode.append(option);}
          const send=node('button','Send guidance'); send.type='submit'; form.append(mode,input,send);
          let pendingGuidance=null;
          form.addEventListener('submit',async(event)=>{event.preventDefault();send.disabled=true;try{
            if(!pendingGuidance || pendingGuidance.action!==mode.value || pendingGuidance.message!==input.value) pendingGuidance={action:mode.value,message:input.value,key:crypto.randomUUID()};
            await control(pendingGuidance.action,pendingGuidance.message,pendingGuidance.key);pendingGuidance=null;guide.open=false;await refresh();clearError();
          }catch(error){showError(error);}finally{send.disabled=false;}});
          guide.append(form); card.append(guide);
        }
        if(!sharedReply && Array.isArray(goal.actions) && can('dismiss')) controls.append(action('Dismiss',async()=>{await control('dismiss');await refresh();}));
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
    if (goal.worker_id && goal.run_id && ['running','needs_input'].includes(goal.control_state || goal.state)) {
      active.set(goal.worker_id,goal.control_run_id || goal.run_id);
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
          onResponseNeeded:({waiting})=>{document.title=waiting?'Response needed · xPerfect':'Chat · xPerfect';},
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
  if (conversationId) {
    try {
      const bootstrap = await api('/api/bootstrap');
      renderAccountIdentity(bootstrap.identity || {}, Boolean(bootstrap.csrf_token));
      chatLibrary.setOwnerScope(bootstrap.csrf_token ? bootstrap.draft_owner_scope : null); libraryOwnerReady = Boolean(bootstrap.csrf_token);
      if (latest && !librarySelectionDirty && Array.isArray(latest.library?.refs)) chatLibrary.setRefs(latest.library?.refs, latest.library.refs.map(ref => { const item = (latest.library.items || []).find(item => item.item_id === ref.item_id); return item ? `${item.name} · v${item.version}` : 'Saved Library item'; }));
    } catch (error) { showError(error); }
    return;
  }
  try {
    const [bootstrap,readiness]=await Promise.all([
      api('/api/bootstrap'),
      // Without readiness (an older runtime), every way to start stays offered, as before.
      api('/v1/coordinator/readiness').catch(() => null),
    ]);
    renderAccountIdentity(bootstrap.identity || {}, Boolean(bootstrap.csrf_token));
      chatLibrary.setOwnerScope(bootstrap.csrf_token ? bootstrap.draft_owner_scope : null); libraryOwnerReady = Boolean(bootstrap.csrf_token);
      if (latest && !librarySelectionDirty && Array.isArray(latest.library?.refs)) chatLibrary.setRefs(latest.library?.refs, latest.library.refs.map(ref => { const item = (latest.library.items || []).find(item => item.item_id === ref.item_id); return item ? `${item.name} · v${item.version}` : 'Saved Library item'; }));
    // The runtime's typed readiness decides; only a known failure withholds a way to start.
    const connectedCode=String(readiness?.connected_account?.code || '');
    const connectedHere=readiness?.connected_account?.available !== false;
    const defaultHere=readiness?.default?.available !== false;
    const select=$('conversation-assistant');
    const names={codex:'Codex',openai:'Codex',claude:'Claude Code',anthropic:'Claude Code',grok:'Grok Build',xai:'Grok Build'};
    const supportedIds=readiness?.connected_account?.supported_account_ids;
    const supportedAccounts=readiness?.connected_account?.supported_accounts || [];
    const profiles={'codex-cli':'Codex','claude-code':'Claude Code','grok-build':'Grok Build','direct-openai':'OpenAI · Direct','direct-anthropic':'Anthropic · Direct','direct-xai':'Grok · Direct'};
    const accounts=(connectedHere ? bootstrap.provider_accounts || [] : []).filter((account) =>
      account.status === 'ready' && account.account_id && names[String(account.provider || '').toLowerCase()]
      && (!Array.isArray(supportedIds) || supportedIds.includes(account.account_id)));
    const options=accounts.flatMap((account) => {
      const supported=supportedAccounts.find(item=>item.account_id===account.account_id);
      const assistant=profiles[supported?.profile] || names[String(account.provider).toLowerCase()];
      const auth=supported?.auth_method === 'api_key' ? ' · API key' : supported?.auth_method === 'bedrock' ? ' · Bedrock' : '';
      if (supported?.auth_method === 'bedrock' && supported.models?.length) return supported.models.map(model => {
        const option=node('option',`${assistant} · ${account.label || 'Connected account'} · Bedrock · ${model.native_model}`);
        option.value=model.id; option._accountId=account.account_id; option._modelId=model.id; return option;
      });
      const option=node('option',`${assistant} · ${account.label || 'Connected account'}${auth}`);
      option.value=account.account_id; return [option];
    });
    if (defaultHere && (bootstrap.workspace_type_options || []).some((item) => item.value === 'host' && !item.disabled)) {
      const option=node('option','Assistant on this computer'); option.value=''; options.push(option);
    }
    select.replaceChildren(...options);
    const preferred=String(bootstrap.user_preferences?.default_worker_profile || '');
    const matched=accounts.find((account) => ({codex:'codex-cli',openai:'codex-cli',claude:'claude-code',anthropic:'claude-code',grok:'grok-build',xai:'grok-build'})[String(account.provider).toLowerCase()] === preferred);
    select.value=options.find(option => (option._accountId || option.value) === (matched?.account_id || accounts[0]?.account_id))?.value || '';
    const available=options.length > 0;
    // Nothing here can start a conversation: ask for no input it could not use.
    $('send').disabled=!available;
    $('message').disabled=!available;
    $('conversation-assistant-field').hidden=!available;
    if (!available) $('conversation-project-field').hidden=true;
    // Connecting an assistant cannot help where a connected account's conversation cannot run.
    $('conversation-connect').hidden=available || !connectedHere;
    $('conversation-run-project').hidden=available || connectedHere;
    if (!available) $('status').textContent=connectedHere ? 'Connect an assistant to start.' : connectedUnavailableMessage(connectedCode);
  } catch (error) {showError(error); $('send').disabled=true;}
}
$('composer').addEventListener('submit',async(event)=>{
  event.preventDefault();if(submitting)return;
  const message=$('message').value;if(!message.trim())return;
  submitting=true;$('send').disabled=true;clearError();$('status').textContent='';
  try {
    if (!conversationId) {
      const projectId=$('conversation-project').value;
      const assistant=$('conversation-assistant').selectedOptions?.[0];
      const accountId=assistant?._accountId || $('conversation-assistant').value;
      const modelId=assistant?._modelId || '';
      const created=await api(base,'POST',{
        ...(projectId ? {scope:{project_id:projectId}} : {}),
        ...(accountId ? {account_id:accountId} : {}),
        ...(modelId ? {model_id:modelId} : {}),
      });
      conversationId=created.conversation_id;
      chatLibrary.setScope(`chat.${conversationId}`);
      history.replaceState(null,'',`?id=${encodeURIComponent(conversationId)}`);
      $('conversation-project-field').hidden=true;
      $('conversation-assistant-field').hidden=true;
      $('conversation-connect').hidden=true;
    }
    const selectedLibrary = chatLibrary.refs(); const librarySignature = JSON.stringify(selectedLibrary);
    if(draftText!==message || draftLibrarySignature!==librarySignature){draftKey=crypto.randomUUID();draftText=message;draftLibrarySignature=librarySignature;}
    const submittedKey=draftKey;
    try {
      const receipt=await api(`${base}/${encodeURIComponent(conversationId)}/turns`,'POST',{idempotency_key:submittedKey,message,...(librarySelectionDirty || !latest ? {library_refs:selectedLibrary} : {})});
      $('message').value='';autoResizeMessage();draftText='';draftKey='';draftLibrarySignature='';librarySelectionDirty=false;
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
      $('message').value='';autoResizeMessage();draftText='';draftKey='';draftLibrarySignature='';librarySelectionDirty=false;
      clearError();
      $('status').textContent=saved.response_json ? ''
        : saved.request_id && !saved.blocker ? 'Working on your request.'
        : 'Your message is saved. The assistant is not available yet.';
    }
  }catch(error){showError(error);}finally{submitting=false;$('send').disabled=false;$('message').focus();}
});
$('stop-response').addEventListener('click',async()=>{try{await api(`${base}/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent($('stop-response').dataset.turn)}/stop`,'POST');await refresh();clearError();}catch(error){showError(error);}});
$('new-conversation').addEventListener('click',()=>{location.href=location.pathname;});
$('message').addEventListener('input', autoResizeMessage);
window.addEventListener('resize', autoResizeMessage);
window.visualViewport?.addEventListener('resize', autoResizeMessage);
autoResizeMessage();
window.addEventListener('pagehide',()=>{closed=true;clearTimeout(timer);});
loadProjects();
loadAssistants();
poll();

async function leaveAccount(scope, button) {
  button.disabled = true; chatLibrary.setOwnerScope(null); libraryOwnerReady = false;
  try {
    const result = await api('/auth/logout', 'POST', {scope});
    closed = true;
    clearTimeout(timer);
    location.assign(result.redirect_url || '/login');
  } catch (error) { showError(error); button.disabled = false; void loadAssistants(); }
}
$('switch-account')?.addEventListener('click', (event) => leaveAccount('provider', event.currentTarget));
$('local-sign-out')?.addEventListener('click', (event) => leaveAccount('local', event.currentTarget));
