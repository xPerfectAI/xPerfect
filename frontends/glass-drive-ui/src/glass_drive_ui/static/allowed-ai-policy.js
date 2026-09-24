const POLICY_VERSION = 1;
const POLICY_MODES = {
  project: new Set(['all_authorized', 'selected']),
  workspace: new Set(['inherit', 'all_authorized', 'selected']),
};
const SELECTION_MODES = new Set(['all', 'selected']);
const OPTION_STATUSES = new Set(['available', 'busy', 'unavailable', 'denied', 'unknown']);
const CONNECTION_KINDS = new Set(['subscription', 'api_key', 'configured_route', 'other']);

function requiredString(value, name) {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`Invalid ${name}`);
  return value;
}

function cloneSelection(value, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`Invalid ${name}`);
  if (Object.keys(value).some(key => !['mode', 'ids'].includes(key))) throw new Error(`Invalid ${name}`);
  const mode = value.mode;
  if (!SELECTION_MODES.has(mode)) throw new Error(`Invalid ${name} mode`);
  const ids = value.ids;
  if (!Array.isArray(ids) || ids.some(id => typeof id !== 'string' || !id.trim())) throw new Error(`Invalid ${name} ids`);
  const unique = new Set(ids);
  if (unique.size !== ids.length) throw new Error(`Duplicate ${name} ids`);
  if (mode === 'all' && ids.length) throw new Error(`${name} all mode must not contain ids`);
  return { mode, ids: [...ids] };
}

export function defaultAllowedAiPolicy(scope = 'project') {
  if (!POLICY_MODES[scope]) throw new Error('Invalid policy scope');
  return { version: POLICY_VERSION, mode: scope === 'workspace' ? 'inherit' : 'all_authorized', harnesses: [] };
}

export function normalizeAllowedAiPolicy(value, scope = 'project') {
  if (!POLICY_MODES[scope]) throw new Error('Invalid policy scope');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid policy');
  if (Object.keys(value).some(key => !['version', 'mode', 'harnesses'].includes(key))) throw new Error('Invalid policy');
  if (value.version !== POLICY_VERSION || value.mode === undefined || !Array.isArray(value.harnesses)) throw new Error('Invalid policy');
  if (!POLICY_MODES[scope].has(value.mode)) throw new Error('Invalid policy mode');
  if (value.mode !== 'selected' && value.harnesses.length) throw new Error('Default policy must not contain harnesses');
  const profiles = new Set();
  const harnesses = value.harnesses.map((harness, index) => {
    if (!harness || typeof harness !== 'object' || Array.isArray(harness)) throw new Error(`Invalid harness ${index}`);
    if (Object.keys(harness).some(key => !['profile', 'models', 'connections'].includes(key))) throw new Error(`Invalid harness ${index}`);
    const profile = requiredString(harness.profile, `harness ${index} profile`);
    if (profiles.has(profile)) throw new Error(`Duplicate harness ${profile}`);
    profiles.add(profile);
    return {
      profile,
      models: cloneSelection(harness.models, `harness ${profile} models`),
      connections: cloneSelection(harness.connections, `harness ${profile} connections`),
    };
  });
  return { version: POLICY_VERSION, mode: value.mode, harnesses };
}

function normalizeOption(value, type) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`Invalid ${type} option`);
  const id = requiredString(value.id, `${type} option id`);
  const label = requiredString(value.label || id, `${type} option label`);
  const status = OPTION_STATUSES.has(value.status) ? value.status : 'unknown';
  const normalized = { id, label, status };
  if (typeof value.reason === 'string' && value.reason.trim()) normalized.reason = value.reason;
  if (type === 'connection') normalized.kind = CONNECTION_KINDS.has(value.kind) ? value.kind : 'other';
  return normalized;
}

export function normalizeAllowedAiOptions(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || !Array.isArray(value.harnesses)) throw new Error('Invalid execution options');
  const scopeId = requiredString(value.scope_id, 'options scope id');
  const profiles = new Set();
  const harnesses = value.harnesses.map((harness, index) => {
    if (!harness || typeof harness !== 'object' || Array.isArray(harness)) throw new Error(`Invalid options harness ${index}`);
    const profile = requiredString(harness.profile, `options harness ${index} profile`);
    if (profiles.has(profile)) throw new Error(`Duplicate options harness ${profile}`);
    profiles.add(profile);
    const label = requiredString(harness.label || profile, `options harness ${profile} label`);
    if (!Array.isArray(harness.models) || !Array.isArray(harness.connections)) throw new Error(`Invalid options for ${profile}`);
    const modelIds = new Set();
    const models = harness.models.map(item => {
      const option = normalizeOption(item, 'model');
      if (modelIds.has(option.id)) throw new Error(`Duplicate model ${option.id}`);
      modelIds.add(option.id);
      return option;
    });
    const connectionIds = new Set();
    const connections = harness.connections.map(item => {
      const option = normalizeOption(item, 'connection');
      if (connectionIds.has(option.id)) throw new Error(`Duplicate connection ${option.id}`);
      connectionIds.add(option.id);
      return option;
    });
    return { profile, label, models, connections };
  });
  return { scope_id: scopeId, harnesses };
}

export function allowedAiRoute(scope, scopeId, kind = 'policy') {
  if (!POLICY_MODES[scope]) throw new Error('Invalid policy scope');
  const id = encodeURIComponent(requiredString(scopeId, 'scope id'));
  if (kind === 'options') return `/api/${scope === 'project' ? 'projects' : 'workspaces'}/${id}/execution-options`;
  if (kind !== 'policy') throw new Error('Invalid policy route');
  return `/api/${scope === 'project' ? 'projects' : 'workspaces'}/${id}/execution-policy`;
}

export function allowedAiSavePayload(scopeId, revision, policy, scope = 'project') {
  if (!Number.isInteger(revision) || revision < 0) throw new Error('Invalid policy revision');
  return { expected_revision: revision, policy: normalizeAllowedAiPolicy(policy, scope) };
}

function text(tag, value, className = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function optionStatus(option) {
  if (option.status === 'available') return '';
  if (option.status === 'busy') return ' · Busy; new work will queue';
  if (option.status === 'denied') return ` · Not authorized${option.reason ? `: ${option.reason}` : ''}`;
  if (option.status === 'unavailable') return ` · Unavailable${option.reason ? `: ${option.reason}` : ''}`;
  return ` · Status unavailable${option.reason ? `: ${option.reason}` : ''}`;
}

export function missingAllowedAiOption(id, type, remembered = null) {
  const saved = remembered?.get(id);
  return {
    id,
    label: saved?.label || `Saved ${type} · ${id}`,
    status: 'unavailable',
    ...(type === 'connection' ? { kind: saved?.kind || 'other' } : {}),
    reason: 'No longer in current choices',
  };
}

function policySummary(policy) {
  if (!policy) return 'Loading policy…';
  if (policy.mode === 'inherit') return 'Use project settings';
  if (policy.mode === 'all_authorized') return 'All authorized options';
  if (!policy.harnesses.length) return 'No new starts allowed';
  const count = policy.harnesses.length;
  return `${count} harness${count === 1 ? '' : 'es'} selected`;
}

export function allowedAiPolicyWarning(policy) {
  if (!policy || policy.mode !== 'selected') return '';
  if (!policy.harnesses.length) return 'No harnesses selected. New starts will be blocked until you choose an option.';
  const incomplete = policy.harnesses.filter(harness =>
    (harness.models.mode === 'selected' && !harness.models.ids.length)
    || (harness.connections.mode === 'selected' && !harness.connections.ids.length));
  if (incomplete.length) return 'A selected harness has no model or connection options. New starts using it will be blocked.';
  return '';
}

export function selectedAllowedAiPolicy(options, saved = null) {
  if (saved?.mode === 'selected') return normalizeAllowedAiPolicy(saved, 'project');
  const catalog = normalizeAllowedAiOptions(options);
  const blocked = new Set((options.harnesses || []).filter(harness => harness.blocked).map(harness => harness.profile));
  return normalizeAllowedAiPolicy({
    version: POLICY_VERSION,
    mode: 'selected',
    harnesses: catalog.harnesses.filter(harness => !blocked.has(harness.profile)).map(harness => ({
      profile: harness.profile,
      models: { mode: 'all', ids: [] },
      connections: { mode: 'all', ids: [] },
    })),
  }, 'project');
}

export function restrictAllowedAiOptions(options, projectPolicy, workspacePolicy = null) {
  const catalog = normalizeAllowedAiOptions(options);
  const ceiling = normalizeAllowedAiPolicy(projectPolicy, 'project');
  if (ceiling.mode !== 'selected') return catalog;
  const allowed = new Map(ceiling.harnesses.map(harness => [harness.profile, harness]));
  const saved = new Map(workspacePolicy?.mode === 'selected' ? workspacePolicy.harnesses.map(harness => [harness.profile, harness]) : []);
  const restrict = (item, selection) => selection.mode === 'all' || selection.ids.includes(item.id)
    ? item : { ...item, status: 'denied', reason: 'Not allowed by project' };
  return {
    ...catalog,
    harnesses: catalog.harnesses.filter(harness => allowed.has(harness.profile) || saved.has(harness.profile)).map(harness => {
      const parent = allowed.get(harness.profile);
      const retained = saved.get(harness.profile);
      const visible = (items, selection, remembered) => items
        .filter(item => selection?.mode === 'all' || selection?.ids.includes(item.id) || remembered?.ids.includes(item.id))
        .map(item => selection ? restrict(item, selection) : { ...item, status: 'denied', reason: 'Not allowed by project' });
      return {
        ...harness,
        blocked: !parent,
        models: visible(harness.models, parent?.models, retained?.models),
        connections: visible(harness.connections, parent?.connections, retained?.connections),
      };
    }),
  };
}

export function selectedOutsideProject(options, policy) {
  if (!options || policy?.mode !== 'selected') return false;
  const offered = new Map(options.harnesses.map(harness => [harness.profile, harness]));
  return policy.harnesses.some(selected => {
    const harness = offered.get(selected.profile);
    if (!harness) return false;
    if (harness.blocked) return true;
    return [['models', selected.models], ['connections', selected.connections]].some(([key, choice]) =>
      choice.mode === 'selected' && choice.ids.some(id => harness[key].some(item => item.id === id && item.status === 'denied')));
  });
}

function selectedIds(container, attribute) {
  return Array.from(container.querySelectorAll(`input[data-${attribute}]:checked`), input => input.value);
}

export function selectableExactIds(items) {
  return items.filter(item => item.status === 'available' || item.status === 'busy').map(item => item.id);
}

function renderOptionGroup(container, title, items, selection, attribute, kind, remembered) {
  container.replaceChildren();
  const legend = text('h5', title);
  container.append(legend);
  const mode = document.createElement('input'); mode.type = 'hidden';
  mode.dataset[`${attribute}Mode`] = 'true';
  mode.value = selection?.mode || 'all';
  container.append(mode);
  const toggle = text('button', '', 'allowed-ai-group-toggle'); toggle.type = 'button'; container.append(toggle);
  const choices = document.createElement('div');
  choices.className = 'allowed-ai-choice-list';
  choices.dataset[`${attribute}Choices`] = 'true';
  const selected = new Set(selection?.ids || []);
  const visible = [...items];
  for (const id of selected) {
    if (!visible.some(item => item.id === id)) visible.push(missingAllowedAiOption(id, kind, remembered));
  }
  for (const option of visible) {
    const label = document.createElement('label');
    label.className = `allowed-ai-option allowed-ai-option-${option.status}`;
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.value = option.id; checkbox.dataset[attribute] = 'true'; checkbox.checked = selected.has(option.id);
    checkbox.disabled = option.status !== 'available' && option.status !== 'busy' && !checkbox.checked;
    label.append(checkbox, text('span', `${option.label}${optionStatus(option)}`));
    choices.append(label);
  }
  if (!visible.length) choices.append(text('p', `No ${kind} options are available for this harness.`, 'member-note'));
  container.append(choices);
  const sync = () => {
    choices.hidden = mode.value !== 'selected';
    toggle.textContent = mode.value === 'all' ? 'Choose exact models' : 'Allow all models';
  };
  const onToggle = () => {
    const selecting = mode.value === 'all';
    mode.value = selecting ? 'selected' : 'all';
    const defaults = new Set(selecting ? selectableExactIds(visible) : []);
    for (const checkbox of choices.querySelectorAll('input')) checkbox.checked = defaults.has(checkbox.value);
    sync(); mode.dispatchEvent(new Event('change', { bubbles: true }));
  };
  toggle.addEventListener('click', onToggle);
  sync();
  return () => toggle.removeEventListener('click', onToggle);
}

function renderHarnesses(container, options, policy, onChange, remembered) {
  container.replaceChildren();
  const current = policy.mode === 'selected' ? new Map(policy.harnesses.map(item => [item.profile, item])) : new Map();
  const cleanups = [];
  const visible = [...options.harnesses];
  for (const profile of current.keys()) {
    if (!visible.some(harness => harness.profile === profile)) visible.push({ profile, label: `${remembered.get(profile)?.label || `Saved harness · ${profile}`} · unavailable`, models: [], connections: [], missing: true });
  }
  for (const harness of visible) {
    const selected = current.get(harness.profile);
    const fieldset = document.createElement('fieldset');
    fieldset.className = 'allowed-ai-harness';
    fieldset.dataset.profile = harness.profile;
    const legend = document.createElement('legend');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.dataset.harness = 'true'; checkbox.value = harness.profile; checkbox.checked = Boolean(selected);
    checkbox.disabled = Boolean(harness.blocked && !selected);
    const harnessLabel = document.createElement('label');
    harnessLabel.append(checkbox, text('span', `${harness.label}${harness.blocked ? ' · Not allowed by project' : ''}`));
    legend.append(harnessLabel);
    fieldset.append(legend);
    const modelGroup = document.createElement('div'); modelGroup.className = 'allowed-ai-option-group'; modelGroup.dataset.group = 'models';
    const connectionGroup = document.createElement('div'); connectionGroup.className = 'allowed-ai-option-group'; connectionGroup.dataset.group = 'connections';
    cleanups.push(renderOptionGroup(modelGroup, 'Models', harness.models, selected?.models, 'modelId', 'model', remembered.get(harness.profile)?.models));
    cleanups.push(renderConnectionGroups(connectionGroup, harness.connections, selected?.connections, remembered.get(harness.profile)?.connections));
    fieldset.append(modelGroup, connectionGroup);
    const refreshCaptions = () => {
      fieldset.classList.toggle('allowed-ai-harness-off', !checkbox.checked);
      const modelMode = modelGroup.querySelector('[data-model-id-mode]')?.value;
      const connectionMode = connectionGroup.querySelector('[data-connection-mode]')?.value;
      modelGroup.querySelector('h5').textContent = `Models · ${modelMode === 'all' ? 'All authorized' : `${selectedIds(modelGroup, 'model-id').length} chosen`}`;
      connectionGroup.querySelector('h5').textContent = `Accounts and routes · ${connectionMode === 'all' ? 'All authorized' : `${selectedIds(connectionGroup, 'connection-id').length} chosen`}`;
    };
    const sync = () => {
      refreshCaptions();
      onChange();
    };
    fieldset.addEventListener('change', sync);
    cleanups.push(() => fieldset.removeEventListener('change', sync));
    refreshCaptions();
    if (harness.missing) fieldset.append(text('p', 'This saved harness is no longer offered. Uncheck it to remove it.', 'member-note'));
    container.append(fieldset);
  }
  if (!options.harnesses.length) container.append(text('p', 'No authorized harness options are available yet.', 'member-note'));
  return () => cleanups.forEach(cleanup => cleanup());
}

function renderConnectionGroups(container, items, selection, remembered) {
  container.replaceChildren(text('h5', 'Accounts and routes'));
  const mode = document.createElement('input'); mode.type = 'hidden'; mode.dataset.connectionMode = 'true';
  mode.value = selection?.mode || 'all';
  container.append(mode);
  const toggle = text('button', '', 'allowed-ai-group-toggle'); toggle.type = 'button'; container.append(toggle);
  const choices = document.createElement('div'); choices.className = 'allowed-ai-choice-list'; choices.dataset.connectionChoices = 'true';
  const selected = new Set(selection?.ids || []);
  const visible = [...items];
  for (const id of selected) if (!visible.some(item => item.id === id)) visible.push(missingAllowedAiOption(id, 'connection', remembered));
  const groups = new Map();
  for (const option of visible) {
    const key = option.kind || 'other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(option);
  }
  const labels = { subscription: 'Subscriptions', api_key: 'API keys', configured_route: 'Configured routes', other: 'Other connections' };
  for (const [kind, group] of groups) {
    choices.append(text('h6', labels[kind] || labels.other));
    for (const option of group) {
      const label = document.createElement('label'); label.className = `allowed-ai-option allowed-ai-option-${option.status}`;
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = option.id; checkbox.dataset.connectionId = 'true'; checkbox.checked = selected.has(option.id);
      checkbox.disabled = option.status !== 'available' && option.status !== 'busy' && !checkbox.checked;
      label.append(checkbox, text('span', `${option.label}${optionStatus(option)}`)); choices.append(label);
    }
  }
  if (!visible.length) choices.append(text('p', 'No connection options are available for this harness.', 'member-note'));
  container.append(choices);
  const sync = () => {
    choices.hidden = mode.value !== 'selected';
    toggle.textContent = mode.value === 'all' ? 'Choose accounts or routes' : 'Allow all connections';
  };
  const onToggle = () => {
    const selecting = mode.value === 'all';
    mode.value = selecting ? 'selected' : 'all';
    const defaults = new Set(selecting ? selectableExactIds(visible) : []);
    for (const checkbox of choices.querySelectorAll('input')) checkbox.checked = defaults.has(checkbox.value);
    sync(); mode.dispatchEvent(new Event('change', { bubbles: true }));
  };
  toggle.addEventListener('click', onToggle);
  sync();
  return () => toggle.removeEventListener('click', onToggle);
}

function policyFromForm(form) {
  const mode = form.querySelector('input[name="policyMode"]:checked')?.value;
  if (mode !== 'selected') return { version: POLICY_VERSION, mode, harnesses: [] };
  const harnesses = [];
  for (const fieldset of form.querySelectorAll('[data-profile]')) {
    const harness = fieldset.querySelector('input[data-harness]');
    if (!harness?.checked) continue;
    const modelMode = fieldset.querySelector('[data-model-id-mode]')?.value || 'all';
    const connectionMode = fieldset.querySelector('[data-connection-mode]')?.value || 'all';
    harnesses.push({
      profile: harness.value,
      models: { mode: modelMode, ids: modelMode === 'selected' ? selectedIds(fieldset, 'model-id') : [] },
      connections: { mode: connectionMode, ids: connectionMode === 'selected' ? selectedIds(fieldset, 'connection-id') : [] },
    });
  }
  return normalizeAllowedAiPolicy({ version: POLICY_VERSION, mode, harnesses }, 'project');
}

function responsePolicy(value, scope, scopeId) {
  const policy = value?.policy || value?.requested_policy || (value?.requested?.mode ? value.requested : value?.mode ? value : null);
  const revision = value?.revision ?? value?.policy_revision;
  if (!policy || !Number.isInteger(revision) || revision < 0) throw new Error('Unsupported policy response');
  if (value.scope_id !== undefined && value.scope_id !== scopeId) throw new Error('Policy scope changed');
  return { revision, policy: normalizeAllowedAiPolicy(policy, scope), effective: value.effective || null };
}

function responseOptions(value, scopeId) {
  const options = value?.options || value;
  const normalized = normalizeAllowedAiOptions(options);
  if (normalized.scope_id !== scopeId) throw new Error('Options scope changed');
  return normalized;
}

function settingsError(error, action) {
  if (error?.status === 401 || error?.status === 403) return 'Sign in as the workspace owner to manage Allowed AI settings.';
  if (error?.status === 404) return 'Allowed AI settings are unavailable in this runtime. Refresh after the runtime update.';
  if (error?.code === 'selection_unavailable') return 'An option changed. Reload the current choices and select it again.';
  if (error?.status === 409) return 'Settings changed elsewhere. Reload before saving another change.';
  if (action === 'Authorized options') return 'AI choices could not be loaded. Retry choices.';
  return `${action} could not be confirmed. Reload settings to try again.`;
}

function mountScope(container, scope, scopeId, request, onConnectAccount, projectId = '', onSaved = () => {}) {
  const details = document.createElement('details'); details.className = 'allowed-ai-scope';
  details.open = scope === 'workspace';
  details.innerHTML = `<summary><span data-scope-summary>${scope === 'project' ? 'Project' : 'Workspace'} · Loading policy…</span></summary><div class="allowed-ai-scope-content"><p class="member-note" role="status" aria-live="polite" data-policy-status>Loading policy…</p><button type="button" data-policy-reload hidden>Reload settings</button><div data-policy-content></div></div>`;
  container.append(details);
  const state = { policy: null, revision: null, options: null, selectedDraft: null, busy: false, optionsLoading: false, optionsVersion: 0, ceilingOverride: null, remembered: new Map(), disposed: false, controlsCleanup: () => {}, optionsCleanup: () => {}, onOptionsLoaded: () => {} };
  const status = message => { if (!state.disposed) details.querySelector('[data-policy-status]').textContent = message; };
  const summary = message => { if (!state.disposed) details.querySelector('[data-scope-summary]').textContent = `${scope === 'project' ? 'Project' : 'Workspace'} · ${message}`; };
  const reload = details.querySelector('[data-policy-reload]');
  const content = details.querySelector('[data-policy-content]');
  const loadOptions = async () => {
    if (state.options || state.optionsLoading || state.busy || state.disposed) return;
    const version = state.optionsVersion;
    state.optionsLoading = true; status('Loading authorized AI choices…'); reload.disabled = true;
    try {
      const ceiling = scope === 'workspace' && projectId
        ? state.ceilingOverride || responsePolicy(await request(allowedAiRoute('project', projectId)), 'project', projectId).policy
        : defaultAllowedAiPolicy('project');
      const catalog = responseOptions(await request(allowedAiRoute(scope, scopeId, 'options')), scopeId);
      if (version !== state.optionsVersion || state.disposed) return;
      for (const harness of catalog.harnesses) {
        const prior = state.remembered.get(harness.profile);
        state.remembered.set(harness.profile, {
          label: harness.label,
          models: new Map([...(prior?.models || []), ...harness.models.map(item => [item.id, item])]),
          connections: new Map([...(prior?.connections || []), ...harness.connections.map(item => [item.id, item])]),
        });
      }
      state.options = restrictAllowedAiOptions(catalog, ceiling, state.selectedDraft || state.policy);
      status('');
    } catch (error) { if (version === state.optionsVersion) status(settingsError(error, 'Authorized options')); }
    finally { if (version === state.optionsVersion && !state.disposed) { state.optionsLoading = false; reload.disabled = false; state.onOptionsLoaded(); } }
  };
  const render = () => {
    if (!state.policy) return;
    state.controlsCleanup(); state.optionsCleanup(); content.replaceChildren();
    const form = document.createElement('form'); form.className = 'allowed-ai-form';
    const mode = document.createElement('fieldset'); mode.className = 'allowed-ai-mode';
    mode.append(text('legend', 'Allowed AI'));
    const modes = scope === 'workspace'
      ? [['inherit', 'Use project settings'], ['all_authorized', 'All project-allowed'], ['selected', 'Choose AI options']]
      : [['all_authorized', 'All authorized (default)'], ['selected', 'Choose AI options']];
    for (const [value, label] of modes) {
      const input = document.createElement('input'); input.type = 'radio'; input.name = 'policyMode'; input.value = value; input.checked = state.policy.mode === value;
      const choice = document.createElement('label'); choice.append(input, text('span', label)); mode.append(choice);
    }
    const modeValue = () => form.querySelector('input[name="policyMode"]:checked')?.value;
    form.append(mode);
    const description = text('p', 'Applies to new work only.', 'member-note');
    form.append(description);
    const choose = document.createElement('div'); choose.className = 'allowed-ai-choose';
    choose.innerHTML = '<p class="member-note" data-options-note></p><button type="button" data-options-retry hidden>Retry choices</button><div data-harnesses></div>';
    form.append(choose);
    const warning = text('p', '', 'allowed-ai-warning'); warning.setAttribute('role', 'status'); warning.setAttribute('aria-live', 'polite'); form.append(warning);
    const actions = document.createElement('div'); actions.className = 'allowed-ai-actions';
    const save = document.createElement('button'); save.type = 'submit'; save.textContent = 'Save Allowed AI';
    const refresh = document.createElement('button'); refresh.type = 'button'; refresh.textContent = 'Reload'; refresh.dataset.policyReload = 'true';
    actions.append(save, refresh);
    if (typeof onConnectAccount === 'function') {
      const connect = document.createElement('button'); connect.type = 'button'; connect.textContent = 'Connect account'; connect.className = 'allowed-ai-connect';
      connect.addEventListener('click', onConnectAccount); actions.append(connect);
    }
    form.append(actions); content.append(form);
    const updateWarning = () => {
      let candidate;
      try {
        candidate = policyFromForm(form);
        if (candidate.mode === 'selected' && state.options) state.selectedDraft = candidate;
        warning.textContent = candidate.mode === 'selected' && !state.options ? '' : allowedAiPolicyWarning(candidate);
        if (scope === 'workspace') {
          const limited = selectedOutsideProject(state.options, candidate);
          summary(`${policySummary(state.policy)}${limited ? ' · Outside project limit' : ''}`);
          if (candidate.mode === 'selected' && state.options) choose.querySelector('[data-options-note]').textContent = limited
            ? 'Some checked choices are outside Project settings and cannot start new work.'
            : 'Project limits apply. Checked choices outside them stay visible but cannot start.';
        }
      }
      catch { warning.textContent = ''; }
    };
    const renderOptions = () => {
      state.optionsCleanup(); state.optionsCleanup = () => {};
      const optionsContent = choose.querySelector('[data-harnesses]'); optionsContent.replaceChildren();
      const note = choose.querySelector('[data-options-note]');
      const retry = choose.querySelector('[data-options-retry]');
      choose.hidden = modeValue() !== 'selected';
      if (modeValue() !== 'selected') {
        save.disabled = state.busy || state.optionsLoading;
        return;
      }
      if (!state.options) {
        note.textContent = 'Could not load choices yet. Retry to choose exact AI options.';
        retry.hidden = state.optionsLoading;
        save.disabled = true;
        if (!state.optionsLoading && !retry.dataset.failed) { note.textContent = 'Loading authorized choices…'; loadOptions(); }
        return;
      }
      retry.hidden = true; retry.dataset.failed = '';
      if (!state.selectedDraft) state.selectedDraft = selectedAllowedAiPolicy(state.options, state.policy);
      note.textContent = scope === 'workspace'
        ? 'Project limits apply. Checked choices outside them stay visible but cannot start.'
        : 'Uncheck a harness to exclude it. Choose exact models or accounts only when needed. Busy choices can queue.';
      state.optionsCleanup = renderHarnesses(optionsContent, state.options, state.selectedDraft, updateWarning, state.remembered);
      save.disabled = state.busy;
      updateWarning();
    };
    const syncMode = () => { renderOptions(); updateWarning(); };
    mode.addEventListener('change', syncMode);
    choose.querySelector('[data-options-retry]').addEventListener('click', () => { choose.querySelector('[data-options-retry]').dataset.failed = ''; loadOptions(); });
    refresh.addEventListener('click', load);
    reload.hidden = true; reload.onclick = load;
    state.onOptionsLoaded = () => {
      if (!state.options) choose.querySelector('[data-options-retry]').dataset.failed = 'true';
      renderOptions();
    };
    renderOptions(); updateWarning(); summary(policySummary(state.policy)); status('');
    state.controlsCleanup = () => { mode.removeEventListener('change', syncMode); state.onOptionsLoaded = () => {}; state.optionsCleanup(); };
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (state.busy) return;
      if (modeValue() === 'selected' && !state.options) { status('Load the current choices before saving.'); return; }
      let policy;
      try { policy = policyFromForm(form); } catch (error) { status(error.message || 'Choose valid Allowed AI options.'); return; }
      state.busy = true; save.disabled = true; refresh.disabled = true; reload.disabled = true; status('Saving Allowed AI settings…');
      let saved = false;
      try {
        const result = await request(allowedAiRoute(scope, scopeId), 'PUT', allowedAiSavePayload(scopeId, state.revision, policy, scope));
        const next = responsePolicy(result, scope, scopeId);
        state.policy = next.policy; state.revision = next.revision; state.selectedDraft = next.policy.mode === 'selected' ? next.policy : null;
        saved = true;
      } catch (error) { status(settingsError(error, 'Allowed AI settings')); }
      finally { state.busy = false; if (!state.disposed) { save.disabled = false; refresh.disabled = false; reload.disabled = false; } }
      if (saved && !state.disposed) { render(); status('Saved.'); onSaved(state.policy); }
    });
  };
  async function load() {
    if (state.busy || state.disposed) return;
    if (scope === 'workspace') state.ceilingOverride = null;
    state.busy = true; reload.hidden = false; reload.disabled = true; status('Loading policy…');
    let next = null;
    try {
      next = responsePolicy(await request(allowedAiRoute(scope, scopeId)), scope, scopeId);
    } catch (error) { content.replaceChildren(); summary('Unavailable'); status(settingsError(error, 'Allowed AI settings')); }
    finally { state.busy = false; reload.disabled = false; }
    if (next && !state.disposed) {
      state.policy = next.policy; state.revision = next.revision; state.options = null; state.optionsVersion += 1; state.optionsLoading = false;
      state.selectedDraft = next.policy.mode === 'selected' ? next.policy : null;
      render();
    }
  }
  details.addEventListener('toggle', () => { if (details.open && !state.policy) load(); });
  load();
  return {
    details,
    projectChanged(policy) {
      if (scope !== 'workspace' || state.disposed) return;
      state.ceilingOverride = policy;
      state.options = null; state.optionsVersion += 1; state.optionsLoading = false;
      status('Project changed. Updating workspace choices…');
      state.onOptionsLoaded();
      if (!state.optionsLoading) loadOptions();
    },
    destroy() { state.disposed = true; state.optionsVersion += 1; state.controlsCleanup(); state.optionsCleanup(); details.remove(); },
  };
}

export function mountAllowedAiPolicy(container, { projectId = '', workspaceId = '', request, onConnectAccount } = {}) {
  const root = document.createElement('section'); root.className = 'allowed-ai-policy';
  root.innerHTML = '<h4>Allowed AI</h4><p class="member-note">All authorized AI stays available by default. Change this only to limit new work.</p><div data-allowed-ai-scopes></div>';
  container.append(root);
  const mounted = [];
  const scopes = root.querySelector('[data-allowed-ai-scopes]');
  let workspaceScope = null;
  if (projectId && typeof request === 'function') mounted.push(mountScope(scopes, 'project', String(projectId), request, onConnectAccount, '', policy => workspaceScope?.projectChanged(policy)));
  if (workspaceId && typeof request === 'function') {
    workspaceScope = mountScope(scopes, 'workspace', String(workspaceId), request, onConnectAccount, String(projectId));
    mounted.push(workspaceScope);
  }
  if (!workspaceScope && mounted.length) mounted[0].details.open = true;
  if (!mounted.length) scopes.append(text('p', 'Allowed AI settings are unavailable until this workspace has a project and execution identity.', 'member-note'));
  for (const current of mounted) current.details.addEventListener('toggle', () => {
    if (current.details.open) for (const other of mounted) if (other !== current) other.details.open = false;
  });
  return () => { mounted.forEach(item => item.destroy()); root.remove(); };
}
