export function preferredProviderAccountId(readyAccounts, currentAccount) {
  const currentId = String(currentAccount || '').trim();
  if (currentId && readyAccounts.some((account) => String(account.account_id || '') === currentId)) {
    return currentId;
  }
  const defaultAccount = readyAccounts.find((account) => Boolean(account.is_default));
  if (defaultAccount) return String(defaultAccount.account_id || '');
  return readyAccounts.length === 1 ? String(readyAccounts[0].account_id || '') : '';
}

export function credentialPolicyTransition({
  currentPolicy,
  savedPersonalPolicy,
  forcedLegacy,
  supportsPersonalAccounts,
}) {
  const personalPolicy = forcedLegacy
    ? String(savedPersonalPolicy || 'personal_required')
    : String(currentPolicy || 'personal_required');
  if (!supportsPersonalAccounts) {
    return { value: 'legacy', savedPersonalPolicy: personalPolicy, forcedLegacy: true };
  }
  return { value: personalPolicy, savedPersonalPolicy: '', forcedLegacy: false };
}

const WORKSPACE_OPEN_RESUME_STATES = new Set(['paused', 'idle', 'idle_terminated', 'stopped']);
const WORKSPACE_LIFECYCLE_RESUME_STATES = new Set([
  'ready',
  'paused',
  'idle',
  'idle_terminated',
  'stopped',
  'completed',
  'retained',
]);
const WORKSPACE_LIFECYCLE_DISABLED_STATES = new Set([
  'created',
  'starting',
  'terminating',
  'termination_failed',
  'terminated',
]);
const WORKSPACE_LIFECYCLE_HIDDEN_STATES = new Set([
  'terminating',
  'termination_failed',
  'terminated',
]);

export function shouldResumeOnWorkspaceOpen({ workspaceKind, renderedState, fallbackState }) {
  const displayedState = String(renderedState || fallbackState || '').trim().toLowerCase();
  return String(workspaceKind || '') === 'named'
    && WORKSPACE_OPEN_RESUME_STATES.has(displayedState);
}

export function workspaceSetupAction(profile) {
  const normalized = String(profile || '').trim().toLowerCase();
  if (normalized === 'codex-cli') return 'codex';
  if (normalized === 'claude-code') return 'claude';
  if (normalized.startsWith('openclaw')) return 'openclaw';
  return 'terminal';
}

export function workspaceLifecycleControl(state) {
  const normalized = String(state || '').trim().toLowerCase();
  const action = WORKSPACE_LIFECYCLE_RESUME_STATES.has(normalized) ? 'resume' : 'pause';
  return {
    action,
    label: normalized === 'completed' ? 'Continue' : action === 'resume' ? 'Resume' : 'Pause',
    hidden: WORKSPACE_LIFECYCLE_HIDDEN_STATES.has(normalized),
    disabled: WORKSPACE_LIFECYCLE_DISABLED_STATES.has(normalized),
  };
}

export function workerAccountSummary({ workspaceValue, accountId, policy, data }) {
  const value = String(workspaceValue || '');
  const saved = value.startsWith('open:') || value.startsWith('duplicate:');
  const workspace = saved ? (data?.existing_workspaces || []).find(
    (item) => String(item.worker_id || '') === value.split(':', 2)[1]) : null;
  const profile = saved ? workspace?.profile : value.split(':', 2)[1];
  const option = (data?.new_workspace_options || []).find((item) => (
    String(item.profile || String(item.value || '').split(':', 2)[1]) === profile));
  const name = String(option?.label || workspace?.profile_label || profile || 'Worker');
  let route;
  if (saved) {
    const readiness = workspace?.provider_readiness || {};
    if (!workspace || !readiness.readiness || readiness.readiness === 'unavailable') route = 'Account status unavailable';
    else if (readiness.readiness === 'action_required') route = `${readiness.label || 'Account'} · Needs attention`;
    else if (readiness.fallback || readiness.policy === 'legacy') route = 'Deployment-managed account';
    else route = String(readiness.label || 'Saved workspace account');
    if (value.startsWith('duplicate:') && readiness.account_id) route += ' · Reapproval required after copy';
  } else if (policy === 'legacy') route = 'Deployment-managed account selected';
  else if (data?.bootstrap_sections && data.bootstrap_sections.provider_accounts !== 'ready') route = 'Account status unavailable';
  else {
    const account = (data?.provider_accounts || []).find((item) => String(item.account_id || '') === String(accountId || ''));
    if (!account) route = policy === 'personal_preferred'
      ? 'No personal account selected · Deployment fallback allowed' : 'Personal account required';
    else {
      route = String(account.label || account.provider || 'Personal account');
      if (account.status !== 'ready') route += ' · Needs attention';
      if (policy === 'personal_preferred') route += ' · Deployment fallback allowed';
    }
  }
  return `${name} · ${route}`;
}
