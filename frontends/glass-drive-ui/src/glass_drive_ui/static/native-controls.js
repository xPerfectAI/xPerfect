// Existing authenticated runtime proxy and CSRF-aware fetch helpers own access.
export function canGuideNativeTurn(state, signed = false) {
  return !signed && state?.available === true && state.read_only === false
    && ['interject', 'cancel', 'permission'].some((action) => state.capabilities?.[action] === true);
}

export function attachNativeControls(container, workerId, { getJson, postJson, onResponseNeeded }) {
  const panel = document.createElement('details');
  panel.className = 'native-turn-controls';
  const heading = document.createElement('summary');
  heading.textContent = 'Guide or respond';
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  const requests = document.createElement('div');
  const text = document.createElement('textarea');
  text.setAttribute('aria-label', 'Message to the active turn');
  text.placeholder = 'Add direction to the active turn';
  text.maxLength = 65536;
  const send = button('Send to active turn');
  const cancel = button('Cancel active turn');
  panel.append(heading, status, requests, text, send, cancel);
  container.append(panel);
  const parentPanel = container.closest('details');
  const parentHeading = parentPanel?.querySelector(':scope > summary');
  const parentHeadingText = parentHeading?.textContent || 'More';
  const url = `/api/worker/${encodeURIComponent(workerId)}/native-control`;
  let state = null;
  let pending = false;
  let requestKey = '';
  let announcedKey = '';
  let currentTarget = null;
  let unconfirmedInterject = null;
  let confirmedRequests = [];
  let statusReason = '';
  function showStatus(message, reason) {
    status.textContent = message;
    statusReason = reason;
  }
  function sameTarget(left, right) {
    return Boolean(left && right && left.run_id === right.run_id && left.attempt_id === right.attempt_id);
  }
  function updateActionAvailability() {
    const canControl = Boolean(state) && sameTarget(currentTarget, state)
      && state.available !== false && state.read_only !== true && !pending;
    send.disabled = !canControl || state.capabilities?.interject !== true
      || sameTarget(currentTarget, unconfirmedInterject);
    cancel.disabled = !canControl || state.capabilities?.cancel !== true;
    for (const section of requests.children) {
      section.disabled = !canControl || state.capabilities?.permission !== true;
    }
  }
  function button(label) {
    const node = document.createElement('button');
    node.type = 'button'; node.className = 'secondary-button compact-button native-control-action';
    node.textContent = label; return node;
  }
  // Native context can be one long line; wrap it so every offered option stays in view.
  function wrapText(node) {
    node.style.whiteSpace = 'pre-wrap'; node.style.overflowWrap = 'anywhere'; return node;
  }
  async function submit(payload) {
    if (!state || pending || !sameTarget(currentTarget, state)
        || state.available === false || state.read_only === true
        || state.capabilities?.[payload.action] !== true
        || (payload.action === 'interject' && sameTarget(currentTarget, unconfirmedInterject))) return;
    const submittedTarget = { ...currentTarget };
    pending = true;
    updateActionAvailability();
    try {
      const result = await postJson(url, { ...submittedTarget, ...payload });
      if (!sameTarget(currentTarget, submittedTarget)) return;
      if (result.status === 'pending' && payload.action === 'interject') {
        unconfirmedInterject = submittedTarget;
      }
      if (result.status === 'queued' && payload.action === 'interject' && text.value === payload.text) {
        text.value = '';
      }
      showStatus(result.status === 'queued' ? 'Message queued for the active turn.'
        : result.status === 'cancel_requested' ? 'Cancellation requested.'
        : result.status === 'permission_submitted' ? 'Response submitted.'
        : result.status === 'pending' ? 'Awaiting native receipt. Do not send again.'
        : result.error || result.status || 'Native control returned no status.', 'action');
    } catch (error) {
      if (sameTarget(currentTarget, submittedTarget)) showStatus(error.message, 'action');
    } finally { pending = false; updateActionAvailability(); }
  }
  send.addEventListener('click', () => { if (text.value.trim()) submit({ action: 'interject', text: text.value }); });
  cancel.addEventListener('click', () => submit({ action: 'cancel' }));
  function showRequest(item) {
    const section = document.createElement('fieldset');
    section.style.minInlineSize = '0';
    const title = document.createElement('legend');
    title.style.overflowWrap = 'anywhere';
    const params = item.request || {};
    const method = String(item.method || '').replace(/^_/, '');
    title.textContent = params.toolCall?.title || params.message || 'The worker needs your response';
    section.append(title);
    if (Number.isFinite(item.expires_at)) {
      const deadline = document.createElement('p');
      deadline.textContent = `Respond by ${new Date(item.expires_at * 1000).toLocaleTimeString()}, or this request expires.`;
      section.append(deadline);
    }
    const rawContext = params.toolCall?.rawInput ?? params.toolCall?.content
      ?? params.rawInput ?? params.raw_input ?? params.content ?? params.command;
    if (rawContext !== undefined && rawContext !== null && String(rawContext).trim()) {
      const context = wrapText(document.createElement('pre'));
      context.setAttribute('aria-label', 'Requested context');
      context.textContent = typeof rawContext === 'string'
        ? rawContext
        : JSON.stringify(rawContext, null, 2);
      section.append(context);
    }
    const respond = (value) => submit({ action: 'permission', request_id: item.request_id, ...value });
    if (method === 'session/request_permission') {
      const options = Array.isArray(params.options) ? params.options : [];
      const persistent = options.filter((option) => ['allow_always', 'reject_always'].includes(option.kind));
      const once = options.filter((option) => option.kind === 'allow_once');
      const remaining = options.filter((option) => option.kind !== 'allow_once'
        && !['allow_always', 'reject_always'].includes(option.kind));
      function addChoice(parent, option) {
        const choice = button(option.name || option.kind || 'Respond');
        choice.addEventListener('click', () => respond({ option_id: option.optionId }));
        parent.append(choice);
      }
      for (const option of [...once, ...remaining]) addChoice(section, option);
      if (persistent.length) {
        const more = document.createElement('details');
        const moreHeading = document.createElement('summary');
        moreHeading.textContent = 'More choices';
        more.append(moreHeading);
        for (const option of persistent) addChoice(more, option);
        section.append(more);
      }
    } else if (method === 'x.ai/ask_user_question') {
      const fields = [];
      for (const question of params.questions || []) {
        const group = document.createElement('fieldset');
        const label = document.createElement('legend'); label.textContent = question.question; group.append(label);
        const choices = [];
        for (const option of question.options || []) {
          const wrapper = document.createElement('label');
          const input = document.createElement('input');
          input.type = question.multiSelect || question.multi_select ? 'checkbox' : 'radio';
          input.name = `${item.request_id}-${fields.length}`;
          input.value = option.label;
          wrapper.append(input, document.createTextNode(`${option.label} — ${option.description || ''}`));
          group.append(wrapper); choices.push(input);
        }
        const other = document.createElement('input'); other.type = 'text'; other.placeholder = 'Your answer';
        other.setAttribute('aria-label', `Other answer: ${question.question}`); group.append(other);
        fields.push({ question: question.question, choices, other }); section.append(group);
      }
      const accept = button('Submit answers');
      accept.addEventListener('click', () => {
        const answers = {}; const annotations = {};
        for (const field of fields) {
          answers[field.question] = field.choices.filter((input) => input.checked).map((input) => input.value);
          if (field.other.value.trim()) { answers[field.question].push('Other'); annotations[field.question] = { notes: field.other.value }; }
        }
        respond({ response: { outcome: 'accepted', answers, annotations } });
      });
      section.append(accept);
    } else if (method === 'x.ai/exit_plan_mode') {
      const plan = wrapText(document.createElement('pre')); plan.textContent = params.planContent || params.plan_content || 'No plan content supplied.';
      section.append(plan);
      const approve = button('Approve plan'); approve.addEventListener('click', () => respond({ response: { outcome: 'approved' } })); section.append(approve);
    } else if (method === 'x.ai/mcp/elicit') {
      const fields = [];
      const schema = params.requestedSchema || {};
      let supported = params.mode !== 'url';
      if (params.mode === 'url') {
        try {
          const destination = new URL(params.url);
          if (!['https:', 'http:'].includes(destination.protocol) || destination.username || destination.password) throw new Error('Unsupported link');
          const link = document.createElement('a'); link.href = destination.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Open provider request'; section.append(link);
          const completed = button('I completed the provider request'); completed.addEventListener('click', () => respond({ response: { outcome: 'accept' } })); section.append(completed);
        } catch (_) { const note = document.createElement('p'); note.textContent = 'The provider link is invalid.'; section.append(note); }
      }
      for (const [name, property] of Object.entries(schema.properties || {})) {
        if (!['string', 'boolean', 'number', 'integer'].includes(property.type)) { supported = false; continue; }
        const label = document.createElement('label'); label.textContent = property.title || name;
        const input = document.createElement('input'); input.type = property.type === 'boolean' ? 'checkbox' : property.type === 'string' ? 'text' : 'number';
        input.required = (schema.required || []).includes(name); label.append(input); section.append(label); fields.push({name, property, input});
      }
      if (supported) {
        const accept = button('Submit'); accept.addEventListener('click', () => {
          if (fields.some(({input}) => !input.reportValidity())) return;
          const content = Object.fromEntries(fields.map(({name,property,input}) => [name, property.type === 'boolean' ? input.checked : property.type === 'string' ? input.value : Number(input.value)]));
          respond({ response: { outcome: 'accept', content } });
        }); section.append(accept);
      } else if (params.mode !== 'url') {
        const note = document.createElement('p'); note.textContent = 'This request requires a native client that supports this form.'; section.append(note);
      }
      const decline = button('Decline'); decline.addEventListener('click', () => respond({ response: { outcome: 'decline' } })); section.append(decline);
    }
    const dismiss = button('Cancel request'); dismiss.addEventListener('click', () => respond({})); section.append(dismiss);
    return section;
  }

  function renderState(next, { reveal = false } = {}) {
    state = next && typeof next === 'object' ? next : null;
    // The live API uses null only when there is no active native turn.
    if (next === null) {
      currentTarget = null;
      unconfirmedInterject = null;
      confirmedRequests = [];
      requestKey = '';
      announcedKey = '';
      requests.replaceChildren();
      showStatus('', '');
      heading.textContent = 'Guide or respond';
      if (parentHeading) parentHeading.textContent = parentHeadingText;
      updateActionAvailability();
      onResponseNeeded?.({ waiting: false, isNew: false });
      return;
    }
    let targetChanged = false;
    // Missing-ID/unavailable objects do not prove that a request or its answer ended.
    if (typeof state?.run_id === 'string' && state.run_id
        && typeof state?.attempt_id === 'string' && state.attempt_id) {
      if (!sameTarget(currentTarget, state)) {
        currentTarget = { run_id: state.run_id, attempt_id: state.attempt_id };
        unconfirmedInterject = null;
        targetChanged = true;
        showStatus('', '');
      }
    }
    const hasSnapshot = Boolean(state) && sameTarget(currentTarget, state) && state.available !== false;
    if (hasSnapshot) {
      confirmedRequests = Array.isArray(state.pending_requests)
        ? state.pending_requests.filter((item) => item && typeof item === 'object')
        : [];
    } else if (targetChanged) {
      confirmedRequests = [];
    }
    const nextKey = JSON.stringify(confirmedRequests.map((item) => String(item.request_id || '')));
    const requestsChanged = targetChanged || nextKey !== requestKey;
    if (requestsChanged) {
      requests.replaceChildren(...confirmedRequests.map(showRequest));
      requestKey = nextKey;
    }
    updateActionAvailability();
    const waiting = confirmedRequests.length > 0;
    heading.textContent = waiting ? 'Response needed' : 'Guide or respond';
    if (!hasSnapshot) {
      if (currentTarget && !['action', 'unconfirmed'].includes(statusReason)) {
        showStatus('Native controls are not ready for this turn.', 'availability');
      }
    } else if (waiting) {
      if (requestsChanged || statusReason !== 'action') {
        showStatus(state.read_only === true
          ? 'The worker is waiting for a workspace member to respond.'
          : 'The worker is waiting for your response.', 'request');
      }
      if (reveal) {
        panel.open = true;
        if (parentPanel) parentPanel.open = true;
      }
    } else if (sameTarget(currentTarget, unconfirmedInterject)) {
      showStatus('Awaiting native receipt. Do not send again.', 'unconfirmed');
    } else if (state.read_only === true && statusReason !== 'action') {
      showStatus('Native controls are available to a workspace member.', 'availability');
    } else if (['availability', 'read', 'request', 'unconfirmed'].includes(statusReason)) {
      showStatus('', '');
    }
    if (parentHeading) {
      parentHeading.textContent = waiting ? 'More · response needed' : parentHeadingText;
    }
    // A host surface may surface each new request set once; it keeps its own layout.
    const isNew = waiting && (targetChanged || nextKey !== announcedKey);
    announcedKey = waiting ? nextKey : '';
    onResponseNeeded?.({ waiting, isNew });
  }

  function updateLive(next) {
    renderState(next, { reveal: true });
  }

  async function refresh() {
    // Without a running turn there is no native state to read.
    if (!state || state.available === false || !panel.open || !panel.isConnected || pending) return;
    const requestedTarget = currentTarget;
    try {
      const next = await getJson(url);
      if (currentTarget === requestedTarget) renderState(next);
    } catch (error) {
      if (currentTarget !== requestedTarget) return;
      renderState({ available: false });
      if (!['action', 'unconfirmed'].includes(statusReason)) showStatus(error.message, 'read');
    }
  }
  panel.addEventListener('toggle', refresh);
  const timer = setInterval(() => { if (!panel.isConnected) clearInterval(timer); else refresh(); }, 2000);
  return { updateLive, refresh };
}

export function currentClaudeConnectionView(capability, status) {
  if (capability?.available !== true) return { visible: false };
  const state = status?.state || '';
  const messages = {
    ready_to_try: 'Your existing Claude sign-in is ready to connect.',
    cli_missing: 'Claude Code needs to be installed first.',
    sign_in_required: 'Sign in to Claude Code, then check again.',
    different_auth_route: 'Claude is using another sign-in method.',
    identity_unavailable: 'Update Claude Code so xPerfect can verify your account.',
    unavailable: 'Claude could not be checked. Try again.',
  };
  return { visible: true, action: !state || state === 'ready_to_try' ? 'connect' : 'check',
    label: !state || state === 'ready_to_try' ? 'Use existing Claude sign-in' : 'Check again',
    message: messages[state] || '', help: Boolean(state && state !== 'ready_to_try') };
}

export function attachExistingClaudeConnection(container, { capability, getJson, postJson, onConnected }) {
  container.replaceChildren();
  let status = null;
  let pending = false;
  let connectNext = true;
  const action = document.createElement('button'); action.type = 'button'; action.className = 'quiet-button';
  const message = document.createElement('p'); message.setAttribute('role', 'status');
  const help = document.createElement('details');
  const summary = document.createElement('summary'); summary.textContent = 'Sign-in help';
  const explanation = document.createElement('p'); explanation.textContent = 'Install Claude Code if needed, open it and sign in with your Claude subscription. Then check again.';
  const install = document.createElement('a'); install.href = 'https://code.claude.com/docs/en/setup';
  install.target = '_blank'; install.rel = 'noopener noreferrer'; install.textContent = 'Official Claude Code setup';
  const command = document.createElement('code'); command.textContent = 'claude auth login';
  help.append(summary, explanation, install, command);
  container.append(action, message, help);
  function render() {
    const view = currentClaudeConnectionView(capability, status);
    container.hidden = !view.visible;
    connectNext = view.action === 'connect';
    action.textContent = view.label || '';
    action.disabled = pending;
    message.textContent = view.message || '';
    help.hidden = !view.help;
  }
  action.addEventListener('click', async () => {
    if (pending || container.hidden) return;
    pending = true; action.disabled = true;
    try {
      const result = connectNext
        ? await postJson('/api/provider-accounts/current-native/claude')
        : await getJson('/api/provider-accounts/current-native/claude');
      status = result;
      if (connectNext && result.complete === true && result.status === 'ready' && result.account_id) {
        message.textContent = 'Existing Claude sign-in connected.';
        await onConnected(result);
        return;
      }
      render();
    } catch (error) {
      message.textContent = error.message || 'Could not connect Claude. Try again.';
      help.hidden = false;
    } finally { pending = false; action.disabled = false; }
  });
  render();
}


export function selectConnectedClaudeAccount({ connected, workspaceValue, accountSelect, policySelect }) {
  if (connected?.profile !== 'claude-code' || workspaceValue !== 'new:claude-code'
      || !accountSelect || !policySelect
      || ![...accountSelect.options].some((option) => option.value === connected.account_id)) return false;
  policySelect.value = 'personal_required';
  accountSelect.value = connected.account_id;
  accountSelect.dispatchEvent(new Event('change'));
  return true;
}
