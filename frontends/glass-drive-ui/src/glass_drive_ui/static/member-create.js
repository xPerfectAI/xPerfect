const PROFILE_EFFORTS = {
  'codex-cli': ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'],
  'claude-code': ['default', 'low', 'medium', 'high', 'xhigh', 'max'],
  'grok-build': [],
};

function labelFor(value) {
  return String(value || '').replaceAll('_', ' ').replace(/\b\w/g, character => character.toUpperCase());
}

function renderAccountOptions(select, policy, profile, providerAccounts, profileAccountProviders) {
  const providers = new Set(profileAccountProviders?.[profile] || []);
  const accounts = providerAccounts.filter(account => (
    providers.has(String(account.provider || '').toLowerCase())
    && String(account.status || '').toLowerCase() === 'ready'
  ));
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = accounts.length ? 'Choose a ready personal account' : 'No ready personal account';
  select.replaceChildren(placeholder);
  for (const account of accounts) {
    const option = document.createElement('option');
    option.value = String(account.account_id || '');
    option.textContent = String(account.label || account.provider || 'Personal account');
    if (account.is_default) option.textContent += ' (default)';
    select.append(option);
  }
  select.disabled = policy.value === 'legacy' || !profile || !accounts.length;
  if (policy.value === 'personal_required' && !accounts.length) {
    select.setCustomValidity('Connect a ready personal account before choosing Only my account.');
  } else {
    select.setCustomValidity('');
  }
}

function renderEffortOptions(select, profile) {
  const values = PROFILE_EFFORTS[profile] || [];
  select.replaceChildren(new Option('Deployment default', ''));
  for (const value of values) select.append(new Option(labelFor(value), value));
  if (profile === 'grok-build') {
    select.replaceChildren(new Option('Native default', ''));
    select.disabled = true;
  } else {
    select.disabled = !profile;
  }
}

export function renderAddMember(container, workspace, { request, providerAccounts = [], profileAccountProviders = {}, status, onCreated, onCancel, onUncertain }) {
  container.innerHTML = '<h3 class="member-title">Add worker</h3><p class="member-note">This worker will share this project workspace. Choose a native profile and account policy when needed.</p><form class="member-create"><label>Name (optional)<input name="name" maxlength="120" autocomplete="off"></label><label>Native profile<select name="profile"><option value="">Configured default</option></select></label><label>Account policy<select name="provider_account_policy"><option value="legacy">Use deployment-managed account</option><option value="personal_preferred">Prefer my account; allow fallback</option><option value="personal_required">Only my account</option></select></label><label>Personal account<select name="provider_account_id" disabled><option value="">Choose a profile first</option></select></label><label>Effort<select name="effort" disabled><option value="">Deployment default</option></select></label><p class="member-note" data-profiles></p><div class="member-actions"><button type="submit">Add worker</button><button type="button" data-cancel>Cancel</button></div></form>';
  const form = container.querySelector('form');
  const profile = form.elements.profile;
  const policy = form.elements.provider_account_policy;
  const account = form.elements.provider_account_id;
  const effort = form.elements.effort;
  let submitted = false;
  container.querySelector('[data-cancel]').addEventListener('click', onCancel);
  container.querySelector('input').focus();
  request('/api/member-profiles').then(catalog => {
    if (!container.contains(form)) return;
    for (const item of catalog.items || []) {
      if (!item.adapter_registered || item.shared_workspace !== true || !item.execution_modes?.includes(workspace.execution_mode)) continue;
      const option = document.createElement('option'); option.value = item.profile; option.textContent = item.label || item.profile; profile.append(option);
    }
    renderEffortOptions(effort, profile.value);
    renderAccountOptions(account, policy, profile.value, providerAccounts, profileAccountProviders);
  }).catch(() => { if (container.contains(form)) container.querySelector('[data-profiles]').textContent = 'Provider choices are unavailable. The configured default is still selected.'; });
  profile.addEventListener('change', () => {
    renderEffortOptions(effort, profile.value);
    renderAccountOptions(account, policy, profile.value, providerAccounts, profileAccountProviders);
  });
  policy.addEventListener('change', () => renderAccountOptions(account, policy, profile.value, providerAccounts, profileAccountProviders));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitted) return;
    submitted = true;
    for (const control of form.querySelectorAll('button,input,select')) control.disabled = true;
    status('Adding worker…');
    const payload = {};
    if (form.elements.name.value.trim()) payload.name = form.elements.name.value.trim();
    if (profile.value) payload.profile = profile.value;
    if (effort.value) payload.effort = effort.value;
    if (policy.value) payload.provider_account_policy = policy.value;
    if (account.value && policy.value !== 'legacy') payload.provider_account_id = account.value;
    try {
      const member = await request(`/api/execution-workspaces/${encodeURIComponent(workspace.workspace_id)}/members`, 'POST', payload);
      if (!member.worker_id) throw new Error('The runtime did not return a worker identity.');
      await onCreated(member);
    } catch (error) {
      // The runtime has no create idempotency contract. Never retry an uncertain write.
      if (!error.status || error.status >= 500) {
        onUncertain();
        status('The result is uncertain. Refresh the worker list before adding another worker.');
        container.querySelector('[data-cancel]').disabled = false;
        container.querySelector('[data-cancel]').textContent = 'Refresh worker list';
      } else {
        status(error.status === 401 || error.status === 403 ? 'Sign in as the workspace owner to add a worker.' : error.message || 'The runtime could not add this worker. Refresh settings to check availability.');
        submitted = false;
        for (const control of form.querySelectorAll('button,input,select')) control.disabled = false;
      }
    }
  });
}
