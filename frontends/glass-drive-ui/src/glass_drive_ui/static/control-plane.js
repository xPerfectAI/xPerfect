import { attachActionMenu } from './action-menu.js?v=20261024polish1';
import { parseRequestData, withRequestMessage, requestDataRows, requestDataFromRows, parseScheduleJson, hasPreciseNumbers, effectiveRunOutcome, plainRunOutcome } from './schedule-data.js?v=20261024polish1';
import { attachExistingClaudeConnection } from './native-controls.js?v=20261024polish1';
import {
  recurrenceSubmissionPolicy,
  scheduleDstPolicyState,
  scheduleEditUpdates,
  scheduleEditorType,
  tomorrowDateTimeLocal,
  weekdayAtInstant,
  weeklyStartDateTimeLocal,
  zonedDateTimeLocalValue,
} from './schedule-policy.js?v=20261024polish1';
import { equivalentReapprovalScopes } from './capability-review.js?v=20261024polish1';

const SUPPORT_COPY = {
  supported: 'Ready to connect',
  proof_required: 'Codex subscription sign-in is not enabled here. Ask the host owner to enable it, or connect an API key.',
  provider_permission_required: 'Claude subscription sign-in is not available on this host. Connect an API key instead.',
  setup_cli_required: 'Install this AI’s official app or command-line tool on the host, then try again.',
  unsupported_macos_host: 'This Mac cannot create a separate Claude subscription sign-in. Use the existing Claude sign-in above or connect an API key.',
  isolated_substrate_required: 'This host is not ready to keep each person’s AI sign-in separate. Ask the host owner to finish setup.',
  secret_store_required: 'The host needs secure key storage before accounts can connect.',
  managed_connection_required: 'Use this host’s account connection page.',
};

const PROVIDER_LABELS = {
  codex: 'Codex',
  claude: 'Claude Code',
  grok: 'Grok Build',
  xai: 'Grok Build',
};
const PROVIDER_METHOD_LABELS = {
  subscription: 'My subscription',
  api_key: 'Connected API key',
  enterprise_route: 'Enterprise route',
};
const CAPABILITY_REVIEW_KEY = 'glasshive.capability-review';

export function providerAccountLabelTransition({ provider, value, autoLabel }) {
  const normalizedProvider = String(provider || '').trim().toLowerCase();
  const providerName = normalizedProvider === 'claude'
    ? 'Claude'
    : (PROVIDER_LABELS[normalizedProvider] || 'AI');
  const nextAutoLabel = `Personal ${providerName}`;
  const currentValue = String(value || '');
  const previousAutoLabel = String(autoLabel || '');
  return {
    value: !currentValue.trim() || currentValue === previousAutoLabel
      ? nextAutoLabel
      : currentValue,
    autoLabel: nextAutoLabel,
  };
}

let api = null;
let controlPlane = null;
let grokModels = null;
let connectAi = null;
let connectAiLoadError = '';
let workspaceCatalog = { items: [] };
let oneOffWorkspaceCatalog = { items: [] };
let recurringSchedules = { items: [] };
let webhookDestinations = [];
let pendingScheduleCreate = null;
let pendingDestinationCreate = null;
let savedRequestDataKeys = new Set();
const settlingScheduleRefreshes = new Map();
let scheduleLoadError = '';
const scheduleOccurrences = new Map();
const scheduleHistoryGeneration = new Map();
const scheduleActionMessages = new Map();
const uncertainScheduleRunKeys = new Map();
const UNCERTAIN_SCHEDULE_RUN_STORAGE_KEY = 'xperfect.schedule-run-now.pending.v1';
let uncertainScheduleRunScope = '';
let editingScheduleId = '';
let scheduleEditorVisible = false;
let editingScheduleSnapshot = null;
let activeSetupAccount = '';
let setupPollTimer = 0;
let controlPlaneGeneration = 0;
let controlPlaneSectionsReady = Promise.resolve();
let scheduleCatalogReady = false;
let accountConnectionIntent = null;
let libraryRequestPending = false;
let pendingCapabilityReview = null;
const copyResetTimers = new WeakMap();
let connectionMenuAbort = null;

function profileForProvider(provider) {
  return { codex: 'codex-cli', claude: 'claude-code', grok: 'grok-build', xai: 'grok-build' }[provider] || '';
}

function providerForProfile(profile) {
  return { 'codex-cli': 'codex', 'direct-openai': 'codex', 'claude-code': 'claude',
    'direct-anthropic': 'claude', 'grok-build': 'grok', 'direct-xai': 'grok' }[profile] || '';
}

function announceConnectedAccount(connectedAccount) {
  if (!connectedAccount?.account_id) return;
  const intent = accountConnectionIntent;
  const sameProvider = !connectedAccount.profile || providerForProfile(intent?.profile) === providerForProfile(connectedAccount.profile);
  const account = controlPlane?.provider_accounts?.find(item => item.account_id === connectedAccount.account_id);
  if (intent?.authMethods?.length && !intent.authMethods.includes(account?.auth_method)) {
    setProviderStatus('This account is connected. The selected worker needs an API key; connect that key to continue its job.');
    return;
  }
  const profile = (sameProvider ? intent?.profile : '') || connectedAccount.profile || '';
  window.dispatchEvent(new CustomEvent('xperfect:account-connected', {
    detail: { ...connectedAccount, profile, returnView: intent?.returnView || '' },
  }));
  if (intent?.returnView) api.setView(intent.returnView);
  accountConnectionIntent = null;
}

function applyAccountConnectionIntent() {
  if (!controlPlane || !accountConnectionIntent?.pending || activeSetupAccount) return;
  const select = document.getElementById('provider-account-provider');
  const disclosure = document.getElementById('add-provider-account');
  const requestedProvider = accountConnectionIntent?.provider || '';
  if (select && requestedProvider && (controlPlane?.provider_options || []).some((item) => item.provider === requestedProvider)) {
    select.value = requestedProvider;
  }
  if (disclosure && !activeSetupAccount) disclosure.open = true;
  renderProviderOptionControls();
  const methodSelect = document.getElementById('provider-account-method');
  const permitted = accountConnectionIntent.authMethods || [];
  if (permitted.length && methodSelect) {
    const method = Array.from(methodSelect.options).find(item => permitted.includes(item.value));
    methodSelect.value = method?.value || '';
    renderProviderChoices(providerOptions(), select, methodSelect);
    renderSupportHint();
  }
  const existingClaude = document.querySelector('#existing-claude-connection button');
  const chosenProvider = document.querySelector('.provider-choice[aria-pressed="true"]');
  if (requestedProvider === 'claude' && existingClaude && (!permitted.length || permitted.includes('subscription'))) existingClaude.focus();
  else (chosenProvider || select)?.focus();
  accountConnectionIntent.pending = false;
}

export async function openAccountConnection({ profile = '', provider = '', authMethods = [], returnView = '' } = {}) {
  accountConnectionIntent = { profile, provider: provider || providerForProfile(profile), authMethods, returnView, pending: true };
  // The existing view owner starts its refresh. Do not issue a duplicate catalog request.
  api.setView('connections');
  applyAccountConnectionIntent();
}

function node(tag, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function emptyList(message) {
  const item = node('div', 'empty-state-compact');
  item.append(node('strong', '', message), node('span', '', 'Nothing is shared globally or across users.'));
  return item;
}

function statusChip(status) {
  const value = String(status || 'not connected').replaceAll('_', ' ');
  const chip = node('span', 'status-chip', value);
  chip.dataset.status = String(status || 'unknown');
  return chip;
}

function providerTimestamp(label, value) {
  const timestamp = Number(value || 0);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return '';
  return `${label} ${new Date(timestamp * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}`;
}

function observedDuration(value) {
  const seconds = Math.max(0, Number(value || 0));
  if (!Number.isFinite(seconds)) return 'duration unavailable';
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h worker time`;
  if (seconds >= 60) return `${(seconds / 60).toFixed(1)}m worker time`;
  return `${seconds.toFixed(seconds >= 10 ? 0 : 1)}s worker time`;
}

function providerAccountDetails(account) {
  const providerId = String(account.provider || '');
  const provider = ['grok', 'xai'].includes(providerId)
    ? 'Grok · SpaceXSI'
    : (PROVIDER_LABELS[providerId] || String(providerId || 'AI').replaceAll('_', ' '));
  const method = { subscription: 'Subscription', api_key: 'API key', enterprise_route: 'Managed connection' }[account.auth_method]
    || String(account.auth_method || 'account').replaceAll('_', ' ');
  return `${provider} · ${method}`;
}

function retainProviderAccountFocus(account, button) {
  let restore = document.activeElement === button;
  const row = button.closest('.connection-row');
  const trigger = button.closest('.connection-more')?.querySelector('summary') || button;
  const action = trigger.dataset.accountAction;
  if (restore && trigger !== button) trigger.focus();
  const followFocus = (event) => {
    if (event.target !== button && event.target !== trigger) restore = false;
  };
  document.addEventListener('focusin', followFocus);
  return () => {
    document.removeEventListener('focusin', followFocus);
    if (!restore) return;
    const list = document.getElementById('provider-account-list');
    const current = Array.from(list?.querySelectorAll('.connection-row') || [])
      .find((item) => item.dataset.accountId === String(account.account_id || ''));
    const target = Array.from(current?.querySelectorAll('[data-account-action]') || [])
      .find((item) => item.dataset.accountAction === action && !item.disabled)
      || current?.querySelector('summary');
    if (target?.isConnected) target.focus();
    else if (row?.isConnected && !button.disabled) button.focus();
  };
}

function providerAccountUsage(account, technical = false) {
  const details = technical ? [] : [
    providerTimestamp('Verified', account.last_verified_at),
    providerTimestamp('Last used', account.last_used_at),
  ];
  const observedRuns = Number(account.observed_runs || 0);
  if (observedRuns > 0) {
    const observedFailures = Math.max(0, Number(account.observed_failures || 0));
    details.push(technical
      ? observedDuration(account.observed_duration_seconds)
      : `Observed here: ${observedRuns} run${observedRuns === 1 ? '' : 's'} · ${observedFailures} failed`);
  }
  const hasReportedTokens = account.observed_input_tokens != null || account.observed_output_tokens != null;
  if (hasReportedTokens && technical) {
    const inputTokens = Math.max(0, Number(account.observed_input_tokens || 0));
    const outputTokens = Math.max(0, Number(account.observed_output_tokens || 0));
    details.push(`Tokens reported by worker: ${inputTokens.toLocaleString()} input · ${outputTokens.toLocaleString()} output. Cached input may be counted separately.`);
  }
  return details.filter(Boolean).join(' · ');
}

function subscriptionRouteAvailable(account) {
  const provider = String(account?.provider || '').toLowerCase();
  const aliases = provider === 'openai' ? ['openai', 'codex']
    : provider === 'anthropic' ? ['anthropic', 'claude']
      : [provider];
  const option = (controlPlane?.provider_options || []).find((item) => aliases.includes(String(item.provider || '').toLowerCase()));
  return String(account?.platform_support || '') === 'supported'
    && String(option?.subscription_support || '') === 'supported';
}

async function copyText(value, button, fallbackNode = null) {
  const text = String(value || '');
  if (!text) return false;
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.className = 'clipboard-fallback';
    document.body.append(textarea);
    textarea.select();
    try {
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    }
    textarea.remove();
  }
  if (!copied && fallbackNode) {
    const range = document.createRange();
    range.selectNodeContents(fallbackNode);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
  }
  if (button) {
    const previous = button.dataset.copyLabel || button.textContent;
    button.dataset.copyLabel = previous;
    const existingTimer = copyResetTimers.get(button);
    if (existingTimer) window.clearTimeout(existingTimer);
    button.textContent = copied ? 'Copied' : 'Selected';
    copyResetTimers.set(button, window.setTimeout(() => {
      button.textContent = previous;
      copyResetTimers.delete(button);
    }, 1800));
  }
  return copied;
}

function setProviderStatus(message = '') {
  const status = document.getElementById('provider-account-status');
  if (!status) return;
  status.textContent = String(message || '');
}

async function reconnectProviderAccount(account, button) {
  if (account.auth_method === 'api_key' && account.credential_route === 'native') {
    const form = document.createElement('form');
    const input = document.createElement('input');
    input.type = 'password'; input.autocomplete = 'off'; input.required = true;
    input.maxLength = 8192; input.placeholder = 'Replacement API key';
    input.setAttribute('aria-label', 'Replacement API key');
    const submit = node('button', 'quiet-button', 'Connect key'); submit.type = 'submit';
    const cancel = node('button', 'quiet-button', 'Cancel'); cancel.type = 'button';
    cancel.addEventListener('click', () => { input.value = ''; form.remove(); button.hidden = false; });
    form.append(input, submit, cancel); button.after(form); button.hidden = true; input.focus();
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); submit.disabled = true;
      const value = input.value; input.value = '';
      try {
        const result = await api.postJson(`/api/provider-accounts/${encodeURIComponent(account.account_id)}/credentials`, { value });
        await loadControlPlane(); setProviderStatus(result.message);
      } catch (error) { setProviderStatus(error.message); submit.disabled = false; }
    });
    return;
  }
  if (account.auth_source === 'current_os_claude_subscription') return verifyProviderAccount(account, button);
  const accountId = encodeURIComponent(String(account.account_id || ''));
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = 'Checking…';
  try {
    if (String(account.auth_method || '') === 'subscription') {
      const setup = await api.postJson(`/api/provider-accounts/${accountId}/setup`);
      activeSetupAccount = String(account.account_id || '');
      showSetup(setup);
      stopSetupPolling();
      setupPollTimer = window.setTimeout(pollSetup, 900);
    } else {
      const result = await api.postJson(`/api/provider-accounts/${accountId}/verify`);
      showSetup(result);
      await loadControlPlane();
      showSetup(result);
    }
  } catch (error) {
    setProviderStatus(error.message);
    button.disabled = false;
    button.textContent = idleLabel;
  }
}

async function verifyProviderAccount(account, button) {
  const restoreFocus = retainProviderAccountFocus(account, button);
  const idleLabel = button.textContent || 'Test connection';
  button.disabled = true;
  button.textContent = 'Testing…';
  try {
    const result = await api.postJson(`/api/provider-accounts/${encodeURIComponent(String(account.account_id || ''))}/verify`);
    if (account.auth_source === 'current_os_claude_subscription') {
      await loadControlPlane(); setProviderStatus(result.message || 'Existing Claude sign-in checked.'); return;
    }
    showSetup(result);
    await loadControlPlane();
    showSetup(result);
  } catch (error) {
    setProviderStatus(error.message);
    button.disabled = false;
    button.textContent = idleLabel;
  } finally {
    restoreFocus();
  }
}

export async function removeProviderAccountRequest(requestApi, accountId) {
  const result = await requestApi.postJson(`/api/provider-accounts/${accountId}/disconnect`);
  await requestApi.deleteJson(`/api/provider-accounts/${accountId}`);
  return result;
}

async function removeProviderAccount(account, button) {
  const accountId = encodeURIComponent(String(account.account_id || ''));
  const label = String(account.label || account.provider || 'this account');
  const brokerBacked = account.credential_route === 'broker' || (account.auth_method === 'enterprise_route');
  const question = account.auth_source === 'current_os_claude_subscription'
    ? `Remove ${label} from xPerfect? Your native Claude sign-in stays connected.`
    : brokerBacked
    ? `Remove ${label} from xPerfect? This does not delete the key or route in connected accounts.`
    : account.auth_method === 'api_key'
      ? `Remove ${label} from xPerfect and delete its local key? This does not revoke the key at its provider.`
      : `Remove ${label} from xPerfect and delete its isolated credentials? xPerfect will also try to sign out at the provider.`;
  if (!window.confirm(question)) return;
  button.disabled = true;
  button.textContent = 'Removing…';
  try {
    const result = await removeProviderAccountRequest(api, accountId);
    await loadControlPlane();
    setProviderStatus(String(result?.message || `${label} removed.`));
  } catch (error) {
    try {
      await loadControlPlane();
    } catch (_reloadError) {
      // Keep the original removal error visible.
    }
    setProviderStatus(error.message);
    button.disabled = false;
    button.textContent = 'Remove';
  }
}

function renderProviderAccounts() {
  connectionMenuAbort?.abort();
  connectionMenuAbort = null;
  const note = document.getElementById("native-provider-note");
  if (note) note.textContent = controlPlane?.native_provider_note || "";
  const list = document.getElementById('provider-account-list');
  if (!list || !controlPlane) return;
  const accounts = controlPlane.provider_accounts || [];
  let nativeConnection = document.getElementById('existing-claude-connection');
  if (!nativeConnection) {
    nativeConnection = node('div', 'connection-item');
    nativeConnection.id = 'existing-claude-connection';
    list.before(nativeConnection);
  }
  attachExistingClaudeConnection(nativeConnection, {
    capability: { available: controlPlane.current_native_claude?.available === true
      && !accounts.some((account) => account.auth_source === 'current_os_claude_subscription') },
    getJson: async (path) => {
      const response = await fetch(api.withAuth(path));
      if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not check Claude'));
      return response.json();
    },
    postJson: api.postJson,
    onConnected: (result) => loadControlPlane({ connectedAccount: { account_id: result.account_id, profile: 'claude-code' } }),
  });
  const reviewAccountIds = new Set(
    (capabilityReviewRequest()?.items || [])
      .filter((item) => item.kind === 'provider_account')
      .map((item) => item.reference),
  );
  const addAccount = document.querySelector('#add-provider-account > summary');
  if (addAccount) addAccount.textContent = accounts.length ? 'Connect another account' : 'Connect an AI account';
  if (!accounts.length) {
    list.replaceChildren(node('p', 'connections-empty', controlPlane.sections?.provider_accounts === 'unavailable'
      ? 'Account status is unavailable. Refresh to try again.' : 'No AI account connected yet.'));
    return;
  }
  connectionMenuAbort = new AbortController();
  const rows = accounts.map((account) => {
    const row = node('article', 'connection-row');
    row.dataset.accountId = String(account.account_id || '');
    row.classList.toggle('needs-review', reviewAccountIds.has(row.dataset.accountId));
    const identity = node('div', 'connection-identity');
    const markProvider = { codex: 'codex', claude: 'claude', grok: 'grok', xai: 'grok' }[account.provider];
    if (markProvider) {
      const mark = node('img', 'provider-mark connection-provider-mark');
      mark.src = `/static/provider-${markProvider}.svg?v=20261024vector1`;
      mark.dataset.provider = markProvider;
      mark.alt = ''; mark.width = 32; mark.height = 32;
      identity.append(mark);
    }
    const copy = node('div', 'connection-copy');
    copy.append(
      node('strong', '', String(account.label || account.provider || 'Worker account')),
      node('span', '', providerAccountDetails(account)),
    );
    if (account.reconnect_reason && !['ready', 'connecting'].includes(String(account.status || ''))) {
      copy.append(node('span', 'connection-recovery', String(account.reconnect_reason)));
    }
    if (account.provider_notice?.message) {
      // The provider's own reason its last run stopped; a completed run clears it.
      copy.append(node('span', 'connection-recovery', `Last run: ${String(account.provider_notice.message)}`));
    }
    const actions = node('div', 'connection-actions connection-actions-primary');
    const brokerBacked = account.credential_route === 'broker' || (account.auth_method === 'enterprise_route');
    actions.append(statusChip(
      brokerBacked && account.status === 'ready'
        ? 'linked · verifies on run'
        : (account.status || account.platform_support),
    ));
    if (account.auth_source === 'current_os_claude_subscription') {
      const verify = node('button', 'quiet-button', 'Verify'); verify.type = 'button';
      verify.dataset.accountAction = 'verify';
      verify.addEventListener('click', () => verifyProviderAccount(account, verify));
      actions.append(verify);
    } else if (account.status === 'connecting' && String(account.auth_method || '') === 'subscription') {
      const continueSetup = node('button', 'quiet-button', 'Continue setup');
      continueSetup.type = 'button';
      continueSetup.addEventListener('click', () => {
        activeSetupAccount = String(account.account_id || '');
        continueSetup.disabled = true;
        pollSetup();
      });
      actions.append(continueSetup);
    } else if (
      String(account.auth_method || '') === 'subscription'
      && account.auth_source !== 'current_os_claude_subscription'
      && subscriptionRouteAvailable(account)
      && String(account.status || '') === 'action_required'
      && String(account.recovery_code || '') === 'credential_cleanup_failed'
    ) {
      const check = node('button', 'quiet-button', 'Check connection');
      check.type = 'button'; check.dataset.accountAction = 'verify';
      check.addEventListener('click', () => verifyProviderAccount(account, check));
      actions.append(check);
    } else if (
      ['disconnected', 'action_required', 'error', 'unavailable'].includes(String(account.status || ''))
      && (String(account.auth_method || '') !== 'subscription' || subscriptionRouteAvailable(account))
    ) {
      const reconnectLabel = String(account.auth_method || '') === 'subscription' ? 'Reconnect' : 'Reconnect & test';
      const reconnect = node('button', 'quiet-button', reconnectLabel);
      reconnect.type = 'button';
      reconnect.addEventListener('click', () => reconnectProviderAccount(account, reconnect));
      actions.append(reconnect);
    }
    if (brokerBacked && controlPlane?.manage_connections_url) {
      const manage = node('a', 'quiet-button', 'Manage account');
      manage.href = String(controlPlane.manage_connections_url);
      actions.append(manage);
    }
    const more = node('details', 'connection-more');
    const moreSummary = node('summary', 'connection-more-trigger', 'More');
    moreSummary.dataset.accountAction = 'more';
    moreSummary.setAttribute('aria-label', `More actions for ${String(account.label || account.provider || 'account')}`);
    const moreActions = node('div', 'connection-more-actions');
    const usage = providerAccountUsage(account);
    if (usage) moreActions.append(node('span', 'connection-more-note', usage));
    if (account.observed_input_tokens != null || account.observed_output_tokens != null || account.observed_duration_seconds != null) {
      const detailToggle = node('button', 'text-button', 'Usage details');
      detailToggle.type = 'button'; detailToggle.tabIndex = -1;
      detailToggle.setAttribute('role', 'menuitem'); detailToggle.setAttribute('aria-expanded', 'false');
      detailToggle.dataset.menuKeepOpen = 'true';
      const technical = node('span', 'connection-more-note', providerAccountUsage(account, true));
      technical.id = `account-usage-${String(account.account_id)}`; technical.hidden = true;
      detailToggle.setAttribute('aria-controls', technical.id);
      detailToggle.addEventListener('click', () => {
        technical.hidden = !technical.hidden;
        detailToggle.setAttribute('aria-expanded', String(!technical.hidden));
      });
      moreActions.append(detailToggle, technical);
    }
    if (account.status === 'ready' && account.auth_source !== 'current_os_claude_subscription') {
      const test = node('button', 'text-button', 'Test connection');
      test.type = 'button';
      test.dataset.icon = 'check';
      test.setAttribute('role', 'menuitem'); test.setAttribute('aria-label', 'Test connection'); test.tabIndex = -1;
      test.addEventListener('click', () => verifyProviderAccount(account, test));
      moreActions.append(test);
    }
    if (
      String(account.auth_method || '') === 'subscription'
      && account.auth_source !== 'current_os_claude_subscription'
      && subscriptionRouteAvailable(account)
      && (String(account.status || '') === 'ready'
        || (String(account.status || '') === 'action_required'
          && String(account.recovery_code || '') === 'credential_cleanup_failed'))
    ) {
      const signInAgain = node('button', 'text-button', 'Sign in again');
      signInAgain.type = 'button';
      signInAgain.dataset.icon = 'reconnect';
      signInAgain.setAttribute('role', 'menuitem'); signInAgain.setAttribute('aria-label', 'Sign in again'); signInAgain.tabIndex = -1;
      signInAgain.addEventListener('click', () => reconnectProviderAccount(account, signInAgain));
      moreActions.append(signInAgain);
    }
    const remove = node('button', 'text-button danger-text-button', 'Remove');
    remove.type = 'button';
    remove.dataset.icon = 'remove';
    remove.setAttribute('role', 'menuitem'); remove.setAttribute('aria-label', 'Remove'); remove.tabIndex = -1;
    remove.addEventListener('click', () => removeProviderAccount(account, remove));
    moreActions.append(remove);
    if (moreActions.childElementCount) {
      more.append(moreSummary, moreActions);
      attachActionMenu(more, moreSummary, moreActions, { signal: connectionMenuAbort.signal });
      actions.append(more);
    }
    identity.append(copy);
    row.append(identity, actions);
    return row;
  });
  list.replaceChildren(...rows);
}

function renderGrokModelChoice() {
  const card = document.getElementById('grok-model-choice');
  const select = document.getElementById('grok-native-model');
  const save = document.getElementById('save-grok-native-model');
  const description = document.getElementById('grok-model-description');
  const status = document.getElementById('grok-model-status');
  if (!card || !select || !save || !description || !status) return;
  const hasGrok = (controlPlane?.provider_accounts || []).some(account => ['grok', 'xai'].includes(account.provider))
    || document.getElementById('provider-account-provider')?.value === 'grok';
  card.hidden = !hasGrok;
  if (!hasGrok) return;
  const options = [new Option('Choose a model', '')];
  for (const model of grokModels?.models || []) options.push(new Option(model, model));
  select.replaceChildren(...options);
  const effective = String(grokModels?.effective_model || '');
  select.value = (grokModels?.models || []).includes(effective) ? effective : '';
  const deployed = grokModels?.source === 'deployment';
  select.disabled = deployed || !grokModels?.models?.length;
  save.hidden = deployed;
  save.disabled = select.disabled || !select.value;
  description.textContent = deployed
    ? `${effective} · Set by this deployment.`
    : 'Choose an exact model offered by the installed Grok harness.';
  status.textContent = deployed && grokModels?.status === 'ready'
    ? '' : String(grokModels?.message || (effective ? `${effective} · Your choice.` : ''));
}

function renderConnections() {
  const list = document.getElementById('data-connection-list');
  const manage = document.getElementById('manage-connections-link');
  const card = document.getElementById('connected-services-card');
  if (!list || !controlPlane) return;
  const connections = controlPlane.connections || [];
  if (controlPlane.sections?.connections === 'not_requested') return;
  if (controlPlane.sections?.connections === 'unavailable') {
    list.replaceChildren(node('p', 'connections-empty', 'Connected tools are unavailable. Refresh to try again.'));
    if (card) card.hidden = false;
    return;
  }
  const reviewConnectionIds = new Set(
    (capabilityReviewRequest()?.items || [])
      .filter((item) => item.kind === 'connection')
      .map((item) => item.reference),
  );
  if (!connections.length) {
    list.replaceChildren();
  } else {
    list.replaceChildren(...connections.map((connection) => {
      const row = node('article', 'connection-row');
      row.classList.toggle(
        'needs-review',
        reviewConnectionIds.has(String(connection.connection_id || '')),
      );
      const copy = node('div', 'connection-copy');
      copy.append(
        node('strong', '', String(connection.label || connection.kind || 'Connected service')),
        node('span', '', String(connection.kind || connection.adapter || 'user-scoped connection').replaceAll('_', ' ')),
      );
      row.append(copy, statusChip(connection.status));
      return row;
    }));
  }
  const manageUrl = String(controlPlane?.manage_connections_url || '');
  if (card) card.hidden = !connections.length && !manageUrl;
  if (manage && manageUrl) {
    manage.href = manageUrl;
    manage.hidden = false;
  } else if (manage) {
    manage.hidden = true;
  }
}

function commandRow(label, command) {
  const row = node('article', 'command-row');
  const copy = node('div', 'command-copy');
  copy.append(node('strong', '', label), node('code', '', command));
  const button = node('button', 'quiet-button', 'Copy');
  button.type = 'button';
  button.addEventListener('click', () => copyText(command, button, copy.querySelector('code')));
  row.append(copy, button);
  return row;
}

function referenceRow(label, value) {
  const row = node('article', 'command-row');
  const copy = node('div', 'command-copy');
  copy.append(node('strong', '', label), node('code', '', value));
  row.append(copy, node('span', 'card-note', 'Registration reference'));
  return row;
}

function connectAiClientCard(title, steps) {
  const card = node('article', 'connect-ai-client-card');
  card.append(node('strong', '', title), node('p', 'card-note', steps));
  return card;
}

function setConnectAiMode(mode) {
  const manual = mode === 'manual';
  const autoTab = document.getElementById('connect-ai-auto-tab');
  const manualTab = document.getElementById('connect-ai-manual-tab');
  const autoPanel = document.getElementById('connect-ai-auto-panel');
  const manualPanel = document.getElementById('connect-ai-manual-panel');
  if (autoTab) autoTab.setAttribute('aria-selected', String(!manual));
  if (manualTab) manualTab.setAttribute('aria-selected', String(manual));
  if (autoTab) autoTab.tabIndex = manual ? -1 : 0;
  if (manualTab) manualTab.tabIndex = manual ? 0 : -1;
  if (autoPanel) autoPanel.hidden = manual;
  if (manualPanel) manualPanel.hidden = !manual;
}

function renderConnectAi() {
  const list = document.getElementById('connect-ai-commands');
  const callbacks = document.getElementById('connect-ai-callbacks');
  const clientsList = document.getElementById('connect-ai-clients');
  const serverUrl = document.getElementById('connect-ai-server-url');
  const copyUrl = document.getElementById('copy-connect-ai-url');
  const prompt = document.getElementById('connect-ai-auto-prompt');
  const copyPrompt = document.getElementById('copy-connect-ai-prompt');
  const source = document.getElementById('connect-ai-source');
  const supportedSummary = document.getElementById('connect-ai-supported-summary');
  const automaticCopy = document.getElementById('connect-ai-auto-copy');
  if (!list || !connectAi) return;
  if (connectAiLoadError) {
    list.replaceChildren(node('p', 'connect-login-note', connectAiLoadError));
    callbacks?.replaceChildren();
    clientsList?.replaceChildren(node('p', 'connect-login-note', connectAiLoadError));
    if (serverUrl) serverUrl.textContent = '';
    if (prompt) prompt.textContent = connectAiLoadError;
    if (copyUrl) copyUrl.disabled = true;
    if (copyPrompt) copyPrompt.disabled = true;
    if (source) source.replaceChildren();
    return;
  }
  const clients = connectAi.clients || {};
  const canSetup = String(connectAi.configuration_status || '') === 'ready';
  const supportedNames = [
    ...(clients.codex ? ['Codex'] : []),
    ...(clients.claude ? ['Claude Code'] : []),
  ];
  const supportedText = supportedNames.length === 2
    ? `${supportedNames[0]} or ${supportedNames[1]}`
    : supportedNames[0] || 'a supported AI app';
  if (supportedSummary) supportedSummary.textContent = `Control your workspaces from ${supportedText}.`;
  if (automaticCopy) automaticCopy.textContent = `Paste this once into ${supportedText}. It will follow only its own setup.`;
  const localStdio = supportedNames.length > 0 && Object.values(clients).every(client => client.transport === 'stdio');
  const manualIntro = document.getElementById('connect-ai-manual-intro');
  const serverAddress = document.getElementById('connect-ai-server-address');
  const registration = document.getElementById('connect-ai-registration-details');
  if (manualIntro) manualIntro.textContent = localStdio
    ? 'Use the command for your AI app in Terminal setup. No sign-in is needed on this computer.'
    : 'Paste this address into your AI app’s Streamable HTTP or MCP server setting.';
  if (serverAddress) serverAddress.hidden = localStdio;
  if (registration) registration.hidden = localStdio;
  const mcpUrl = String(connectAi.mcp_url || '');
  const guidedPrompt = String(connectAi.guided_prompt || '');
  if (serverUrl) serverUrl.textContent = mcpUrl;
  if (prompt) prompt.textContent = canSetup ? guidedPrompt : String(connectAi.configuration_note || 'Setup is not available.');
  if (copyUrl) {
    copyUrl.disabled = !canSetup || !mcpUrl;
    copyUrl.onclick = () => copyText(mcpUrl, copyUrl, serverUrl);
  }
  if (copyPrompt) {
    copyPrompt.disabled = !canSetup || !guidedPrompt;
    copyPrompt.onclick = () => copyText(guidedPrompt, copyPrompt, prompt);
  }

  const clientCards = [];
  if (canSetup && clients.codex) {
    clientCards.push(connectAiClientCard(
      'Codex',
      String(clients.codex.card_note || 'Open Settings → MCP servers → Add server. Paste the xPerfect address, restart if prompted, then Authenticate.'),
    ));
  }
  if (canSetup && clients.claude) {
    clientCards.push(connectAiClientCard(
      'Claude Code',
      String(clients.claude.card_note || 'Use Terminal setup below, then run /mcp in Claude Code to finish sign-in.'),
    ));
  }
  clientsList?.replaceChildren(
    ...(clientCards.length
      ? clientCards
      : [node('p', 'connect-login-note', String(connectAi.configuration_note || 'Setup is not available.'))]),
  );

  const rows = canSetup ? [
    ...(clients.codex?.transport === 'stdio' ? [
      commandRow('Codex · Add server', String(clients.codex.add_command || '')),
    ] : clients.codex ? [
      commandRow('Codex · 1. Configure server', String(clients.codex.config_toml || '')),
      commandRow('Codex · 2. Sign in', String(clients.codex.login_command || '')),
    ] : []),
    ...(clients.claude ? [
      commandRow('Claude Code · Add server', String(clients.claude.add_command || '')),
    ] : []),
  ].filter((row) => row.querySelector('code')?.textContent) : [];
  if (clients.claude?.login_note) {
    rows.push(node('p', 'connect-login-note', String(clients.claude.login_note)));
  }
  rows.push(node('p', 'connect-login-note', `Saved as ${String(connectAi.server_name || 'this xPerfect server')}.`));
  const callbackRows = canSetup ? [
    ...(clients.codex ? [referenceRow(
      'Codex callback · Do not open this address',
      String(clients.codex.callback_uri || ''),
    )] : []),
    ...(clients.claude ? [referenceRow(
      'Claude Code callback · Do not open this address',
      String(clients.claude.callback_uri || ''),
    )] : []),
  ].filter((row) => row.querySelector('code')?.textContent) : [];
  if (connectAi.documentation_url) {
    const docs = node('a', 'connect-login-note', 'Client registration instructions');
    docs.href = String(connectAi.documentation_url);
    docs.target = '_blank';
    docs.rel = 'noreferrer';
    rows.push(docs);
  }
  list.replaceChildren(...rows);
  callbacks?.replaceChildren(...callbackRows);
  if (source) {
    const sourceLink = node('a', '', `${connectAi.source?.label || 'Source available'} · ${connectAi.source?.license || ''}`);
    sourceLink.href = String(connectAi.source?.repository_url || '#');
    sourceLink.target = '_blank';
    sourceLink.rel = 'noreferrer';
    source.replaceChildren(sourceLink);
  }
}

function compareLibraryVersions(left, right) {
  const parse = (value) => {
    const [main, prerelease = ''] = String(value || '0.0.0').split('-', 2);
    return { numbers: main.split('.').slice(0, 3).map((part) => Number(part) || 0), prerelease };
  };
  const a = parse(left);
  const b = parse(right);
  for (let index = 0; index < 3; index += 1) {
    if (a.numbers[index] !== b.numbers[index]) return a.numbers[index] > b.numbers[index] ? 1 : -1;
  }
  if (a.prerelease === b.prerelease) return 0;
  if (!a.prerelease) return 1;
  if (!b.prerelease) return -1;
  return a.prerelease.localeCompare(b.prerelease);
}

function capabilityReviewRequest() {
  if (pendingCapabilityReview) return pendingCapabilityReview;
  try {
    const raw = sessionStorage.getItem(CAPABILITY_REVIEW_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    const workerId = String(parsed.worker_id || '');
    const count = Math.max(0, Number(parsed.count || 0));
    if (!workerId || !count) return null;
    const items = Array.isArray(parsed.items)
      ? parsed.items.filter((item) => item && typeof item === 'object').map((item) => ({
        action_id: String(item.action_id || ''),
        kind: String(item.kind || ''),
        reference: String(item.reference || ''),
        resolution: String(item.resolution || ''),
        label: String(item.label || 'Capability'),
        route: item.route === 'connections' ? 'connections' : 'library',
        scopes: Array.isArray(item.scopes) ? item.scopes.map(String) : [],
        policy: String(item.policy || ''),
      })).filter((item) => item.action_id && item.kind && item.reference && item.resolution)
      : [];
    pendingCapabilityReview = { workerId, count: Math.max(count, items.length), items };
  } catch (_error) {
    try { sessionStorage.removeItem(CAPABILITY_REVIEW_KEY); } catch { /* Optional browser hint. */ }
  }
  return pendingCapabilityReview;
}

export function persistCapabilityReview(review) {
  pendingCapabilityReview = review;
  if (!review?.workerId || !(review?.items?.length || review?.count)) {
    try { sessionStorage.removeItem(CAPABILITY_REVIEW_KEY); } catch { /* Optional browser hint. */ }
    pendingCapabilityReview = null;
    return;
  }
  try {
    sessionStorage.setItem(CAPABILITY_REVIEW_KEY, JSON.stringify({
      worker_id: review.workerId,
      count: Math.max(Number(review.count || 0), review.items?.length || 0),
      items: review.items || [],
    }));
  } catch { /* Keep the exact in-memory receipt when browser storage is unavailable. */ }
}

function restoreCapabilityReviewFromCatalog() {
  if (capabilityReviewRequest()?.items?.length) return;
  const copiedWorkspace = (workspaceCatalog.items || []).find((workspace) => {
    const report = workspace?.duplication_report || {};
    return (report.outstanding_reapproval_items || []).some((item) => item?.action_id);
  });
  if (!copiedWorkspace) return;
  const report = copiedWorkspace.duplication_report || {};
  const items = report.outstanding_reapproval_items || [];
  persistCapabilityReview({
    workerId: String(copiedWorkspace.worker_id || ''),
    count: items.length,
    items,
  });
}

async function reconcileCapabilityReview(generation = controlPlaneGeneration) {
  const scope = scheduleRunScope();
  const review = capabilityReviewRequest();
  if (!review?.items?.length) return;
  let workspace = (workspaceCatalog.items || []).find(
    (item) => String(item.worker_id || '') === review.workerId,
  );
  if (!workspace) {
    try {
      const response = await fetch(api.withAuth(`/api/workspaces/${encodeURIComponent(review.workerId)}`));
      if (generation !== controlPlaneGeneration || scope !== scheduleRunScope()) return;
      if (response.status === 404) {
        persistCapabilityReview(null);
        return;
      }
      if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not restore capability review'));
      workspace = await response.json();
    } catch (_error) {
      // A transient runtime failure is not proof that this owner-scoped review disappeared.
      return;
    }
  }
  if (generation !== controlPlaneGeneration || scope !== scheduleRunScope()) return;
  const remaining = workspace?.duplication_report?.outstanding_reapproval_items || [];
  persistCapabilityReview({ ...review, count: remaining.length, items: remaining });
}

async function prepareProviderAccountReapproval(review, item, button) {
  button.disabled = true;
  button.textContent = 'Preparing review…';
  try {
    const pending = await api.postJson('/api/pending-changes', {
      change_type: 'workspace_provider_account',
      target_id: review.workerId,
      payload: { policy: item.policy, account_id: item.reference },
    });
    const changeId = encodeURIComponent(String(pending.change_id || ''));
    const token = encodeURIComponent(String(pending.confirmation_token || ''));
    if (!changeId || !token) throw new Error('xPerfect could not prepare the account review.');
    window.location.assign(`/confirm-change#change_id=${changeId}&token=${token}`);
  } catch (error) {
    button.textContent = error.message;
    button.disabled = false;
  }
}

function openCapabilitySetup(item, review) {
  if (item.resolution === 'personal_library' && review?.workerId) {
    window.location.assign(`/watch/${encodeURIComponent(review.workerId)}?surface=terminal&library=added`);
    return;
  }
  const route = item.route === 'connections' ? 'connections' : 'library';
  window.location.hash = route;
  document.getElementById(`${route}-view`)?.scrollIntoView({ block: 'start' });
}

async function waiveCapabilityReapproval(review, item, button) {
  button.disabled = true;
  button.textContent = 'Preparing review…';
  try {
    const pending = await api.postJson('/api/pending-changes', {
      change_type: 'workspace_duplication_reapproval_waiver',
      target_id: review.workerId,
      payload: { action_id: item.action_id },
    });
    const changeId = encodeURIComponent(String(pending.change_id || ''));
    const token = encodeURIComponent(String(pending.confirmation_token || ''));
    if (!changeId || !token) throw new Error('xPerfect could not prepare the confirmation.');
    window.location.assign(`/confirm-change#change_id=${changeId}&token=${token}`);
  } catch (error) {
    button.textContent = error.message;
    button.disabled = false;
  }
}

function renderCapabilityReviewBanner() {
  const banner = document.getElementById('capability-review-banner');
  if (!banner) return;
  const review = capabilityReviewRequest();
  if (!review?.items?.length) {
    banner.hidden = true;
    banner.replaceChildren();
    return;
  }
  const title = node('strong', '', `Review ${review.items.length} copied workspace capabilit${review.items.length === 1 ? 'y' : 'ies'}`);
  const summary = node('span', '', 'This worker is paused. Review the items below before you start it.');
  const actions = node('div', 'capability-review-actions');
  for (const item of review.items) {
    const row = node('div', 'capability-review-action');
    const button = node('button', 'quiet-button', `Review ${item.label}`);
    button.type = 'button';
    if (item.resolution === 'provider_selection' && item.policy && !item.reference.startsWith('policy:')) {
      button.addEventListener('click', () => prepareProviderAccountReapproval(review, item, button));
    } else if (item.resolution === 'connection_grant' || item.resolution === 'provider_grant') {
      button.textContent = `${item.label} was not copied`;
      button.disabled = true;
    } else {
      button.textContent = `Review ${item.label}`;
      button.addEventListener('click', () => openCapabilitySetup(item, review));
    }
    row.append(button);
    if (item.resolution !== 'provider_selection') {
      const skip = node('button', 'text-button', 'Continue without');
      skip.type = 'button';
      skip.addEventListener('click', () => waiveCapabilityReapproval(review, item, skip));
      row.append(skip);
    }
    actions.append(row);
  }
  banner.replaceChildren(title, summary, actions);
  banner.hidden = false;
}

function renderLibrary() {
  const list = document.getElementById('library-list');
  const empty = document.getElementById('library-empty');
  if (!list || !controlPlane) return;
  if (controlPlane.sections?.library === 'not_requested') {
    list.replaceChildren();
    if (empty) { empty.hidden = false; empty.textContent = 'Loading Library…'; }
    return;
  }
  const items = controlPlane.library || [];
  if (controlPlane.sections?.library === 'unavailable') {
    list.replaceChildren();
    if (empty) {
      empty.hidden = false;
      empty.textContent = 'The Library is unavailable. Refresh to try again.';
    }
    return;
  }
  const libraryById = new Map(items.map((libraryItem) => [String(libraryItem.library_id || ''), libraryItem]));
  const review = capabilityReviewRequest();
  if (review && document.getElementById('approved-library-details')) document.getElementById('approved-library-details').open = true;
  const requiredLibraryIds = new Set(
    (review?.items || [])
      .filter((item) => item.kind === 'library')
      .map((item) => item.reference),
  );
  if (empty) {
    empty.hidden = items.length > 0;
    empty.textContent = 'No approved capabilities are available yet.';
  }
  const cards = items.map((item) => {
    const manifest = item.manifest || {};
    const activatable = manifest.activatable === true && item.activation_status === 'ready';
    const capabilityName = String(manifest.label || manifest.name || item.stable_id || 'Approved capability');
    const card = node('article', 'library-card');
    card.dataset.libraryId = String(item.library_id || '');
    card.classList.toggle('needs-review', requiredLibraryIds.has(String(item.library_id || '')));
    const head = node('div', 'library-card-head');
    head.append(node('span', 'library-kind', String(manifest.kind || 'Capability')), statusChip(item.status || 'available'));
    card.append(
      head,
      node('h2', '', capabilityName),
      node('p', '', String(manifest.description || 'An approved, versioned capability for compatible workspaces.')),
    );
    const meta = node('div', 'library-meta');
    meta.append(
      node('span', '', `Version ${String(item.version || '1')}`),
      node('span', '', (item.supported_profiles || []).join(', ') || 'Compatible workers'),
      node('span', '', `Permissions: ${(item.scopes || []).join(', ') || 'none requested'}`),
    );
    const more = node('details', 'library-card-details'); more.append(node('summary', '', 'Details & access'), meta); card.append(more);
    const grant = node('div', 'library-grant');
    const select = node('select');
    select.setAttribute('aria-label', `Workspace for ${capabilityName}`);
    const placeholder = node(
      'option',
      '',
      workspaceCatalog.unavailable ? 'Workspaces unavailable · refresh to retry'
        : workspaceCatalog.next_cursor ? 'Choose recent workspace · search Workspaces for more' : 'Choose workspace',
    );
    placeholder.value = '';
    select.appendChild(placeholder);
    for (const workspace of workspaceCatalog.items || []) {
      const option = node('option', '', String(workspace.name || workspace.title || workspace.worker_id || 'Workspace'));
      option.value = String(workspace.worker_id || '');
      select.appendChild(option);
    }
    const button = node('button', 'card-action', activatable ? 'Add to workspace' : 'Unavailable in this deployment');
    button.type = 'button';
    button.disabled = true;
    let activeGrant = null;
    let replacementGrant = null;
    let grantGeneration = 0;
    const syncGrant = async () => {
      const generation = ++grantGeneration;
      const selectedWorkspace = select.value;
      activeGrant = null;
      replacementGrant = null;
      if (!activatable || !selectedWorkspace) {
        button.disabled = true;
        button.textContent = activatable ? 'Add to workspace' : 'Unavailable in this deployment';
        return;
      }
      button.disabled = true;
      button.textContent = 'Checking workspace…';
      try {
        const response = await fetch(api.withAuth(`/api/workspaces/${encodeURIComponent(selectedWorkspace)}/capability-grants`));
        if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load workspace capabilities'));
        const payload = await response.json();
        if (generation !== grantGeneration || select.value !== selectedWorkspace) return;
        const grants = payload.items || [];
        activeGrant = grants.find((grantItem) => grantItem.library_id === item.library_id) || null;
        replacementGrant = grants.find((grantItem) => {
          const installed = libraryById.get(String(grantItem.library_id || ''));
          return installed && installed.stable_id === item.stable_id && installed.library_id !== item.library_id;
        }) || null;
        if (activeGrant) {
          button.textContent = 'Remove from workspace';
          button.disabled = false;
        } else if (replacementGrant) {
          const installed = libraryById.get(String(replacementGrant.library_id || ''));
          const newer = compareLibraryVersions(item.version, installed?.version) > 0;
          button.textContent = newer ? 'Upgrade workspace' : 'Newer version already active';
          button.disabled = !newer;
        } else {
          button.textContent = 'Add to workspace';
          button.disabled = false;
        }
      } catch (error) {
        if (generation !== grantGeneration || select.value !== selectedWorkspace) return;
        button.textContent = error.message;
      }
    };
    select.addEventListener('change', syncGrant);
    button.addEventListener('click', async () => {
      if (!select.value) {
        select.focus();
        return;
      }
      button.disabled = true;
      button.textContent = activeGrant ? 'Removing…' : 'Preparing review…';
      try {
        if (activeGrant) {
          if (!window.confirm(`Remove ${capabilityName} from ${workspaceName(select.value)}?`)) {
            button.disabled = false;
            button.textContent = 'Remove from workspace';
            return;
          }
          await api.deleteJson(
            `/api/workspaces/${encodeURIComponent(select.value)}/capability-grants/${encodeURIComponent(String(activeGrant.grant_id || ''))}`,
          );
          await syncGrant();
          return;
        }
        const exactReviewItem = review?.workerId === select.value
          ? (review.items || []).find(
            (reviewItem) => reviewItem.kind === 'library' && reviewItem.reference === item.library_id,
          )
          : null;
        const equivalentScopes = equivalentReapprovalScopes(item.scopes || [], exactReviewItem);
        const requestedScopes = equivalentScopes ?? (replacementGrant
          ? (item.scopes || []).filter((scope) => (replacementGrant.scopes || []).includes(scope))
          : (item.scopes || []));
        const pending = await api.postJson('/api/pending-changes', {
          change_type: replacementGrant ? 'library_upgrade' : 'library_enable',
          target_id: select.value,
          payload: {
            library_id: item.library_id,
            scopes: requestedScopes,
            ...(replacementGrant ? { replaces_grant_id: replacementGrant.grant_id } : {}),
          },
        });
        const changeId = encodeURIComponent(String(pending.change_id || ''));
        const token = encodeURIComponent(String(pending.confirmation_token || ''));
        if (!changeId || !token) throw new Error('xPerfect could not prepare the confirmation.');
        window.location.assign(`/confirm-change#change_id=${changeId}&token=${token}`);
      } catch (error) {
        button.textContent = error.message;
        window.setTimeout(() => {
          syncGrant();
        }, 2200);
      }
    });
    if (activatable) grant.append(select, button);
    else grant.append(node('p', 'card-note', 'Not available in this installation. Your saved material is unchanged.'));
    if (
      review
      && requiredLibraryIds.has(String(item.library_id || ''))
      && Array.from(select.options).some((option) => option.value === review.workerId)
    ) {
      select.value = review.workerId;
      void syncGrant();
    }
    card.append(grant);
    return card;
  });
  list.replaceChildren(...cards);
}

function renderLibraryRequestWorkspaceOptions() {
  const select = document.getElementById('library-request-workspace');
  if (!select) return;
  const review = capabilityReviewRequest();
  const selected = review?.workerId || select.value;
  const workspaces = workspaceCatalog.items || [];
  const placeholder = node('option', '', workspaceCatalog.unavailable
    ? 'Workspaces are unavailable. Refresh to try again.' : workspaces.length
    ? (workspaceCatalog.next_cursor ? 'Choose recent workspace · search Workspaces for more' : 'Choose a saved workspace')
    : 'Create a saved workspace first');
  placeholder.value = '';
  const options = workspaces.map((workspace) => {
    const option = node('option', '', String(workspace.name || workspace.title || workspace.worker_id || 'Saved workspace'));
    option.value = String(workspace.worker_id || '');
    return option;
  });
  select.replaceChildren(placeholder, ...options);
  if (options.some((option) => option.value === selected)) select.value = selected;
  select.disabled = !options.length;
  const submit = document.querySelector('#library-request-form button[type="submit"]');
  if (submit) submit.disabled = !options.length || libraryRequestPending;
  const startJob = document.getElementById('library-start-job');
  if (startJob) startJob.hidden = Boolean(options.length) || Boolean(workspaceCatalog.unavailable);
  const status = document.getElementById('library-request-status');
  if (status && review) {
    status.textContent = `Workspace copied. Review ${review.count} capabilit${review.count === 1 ? 'y' : 'ies'} before running it.`;
  }
}

async function submitLibraryRequest(event) {
  event.preventDefault();
  if (libraryRequestPending) return;
  const workspaceId = document.getElementById('library-request-workspace')?.value || '';
  const requestText = document.getElementById('library-request-text')?.value.trim() || '';
  const status = document.getElementById('library-request-status');
  if (!workspaceId) {
    if (status) status.textContent = 'Choose a saved workspace.';
    document.getElementById('library-request-workspace')?.focus();
    return;
  }
  if (!requestText) {
    if (status) status.textContent = 'Describe the capability or paste its source.';
    document.getElementById('library-request-text')?.focus();
    return;
  }
  if (status) status.textContent = 'Sending a workspace-only request…';
  libraryRequestPending = true;
  const submit = event.currentTarget?.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    await api.postJson(`/api/workspace/${encodeURIComponent(workspaceId)}/message`, {
      message: requestText,
    });
    if (status) status.textContent = 'Request sent. The connection review stays open until access is verified or you continue without it.';
    const workspace = (workspaceCatalog.items || []).find((item) => String(item.worker_id || '') === workspaceId);
    const surface = document.getElementById('launch-surface')?.value === 'terminal' ? 'terminal' : 'desktop';
    const watchUrl = String(workspace?.watch_url || api.withAuth(`/watch/${encodeURIComponent(workspaceId)}?surface=${surface}`));
    window.location.assign(watchUrl);
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    libraryRequestPending = false;
    if (submit) submit.disabled = false;
  }
}

function workspaceName(workerId) {
  const workspace = [...(workspaceCatalog.items || []), ...(oneOffWorkspaceCatalog.items || [])]
    .find((item) => String(item.worker_id || '') === String(workerId || ''));
  return String(workspace?.name || workspace?.title || workerId || 'Saved workspace');
}

function formatDateTime(value) {
  if (!value) return 'Not scheduled';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed);
}

function formatScheduleDateTime(value, timezoneName = 'UTC') {
  if (!value) return 'Not scheduled';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return `${String(value)} · ${String(timezoneName || 'UTC')}`;
  const zone = String(timezoneName || 'UTC');
  try {
    const formatted = new Intl.DateTimeFormat(undefined, {
      timeZone: zone,
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(parsed);
    return `${formatted} · ${timeZoneCity(zone)}`;
  } catch (_error) {
    return `${formatDateTime(value)} · ${timeZoneCity(zone)}`;
  }
}

function intervalLabel(seconds) {
  const value = Number(seconds || 0);
  if (value > 0 && value % 604800 === 0) {
    const weeks = value / 604800;
    return `Every ${weeks} week${weeks === 1 ? '' : 's'}`;
  }
  if (value > 0 && value % 86400 === 0) {
    const days = value / 86400;
    return `Every ${days} day${days === 1 ? '' : 's'}`;
  }
  if (value > 0 && value % 3600 === 0) {
    const hours = value / 3600;
    return `Every ${hours} hour${hours === 1 ? '' : 's'}`;
  }
  if (value >= 60 && value % 60 === 0) { const minutes = value / 60; return `Every ${minutes} minute${minutes === 1 ? '' : 's'}`; }
  return `Every ${value} seconds`;
}

function recurrenceLabel(schedule) {
  const kind = String(schedule.recurrence_type || 'interval');
  const timezone = timeZoneCity(schedule.timezone_name || 'UTC');
  if (kind === 'once') return 'One time';
  if (kind === 'weekly') return `Every week · ${timezone}`;
  if (kind === 'rfc5545' && String(schedule.rrule || '').trim().toUpperCase() === 'FREQ=WEEKLY') {
    return `Every week · ${timezone}`;
  }
  if (kind === 'daily') {
    return `Every day at ${schedule.local_time || '—'} · ${timezone}`;
  }
  if (kind === 'cron') return `Custom cron · ${timezone}`;
  if (kind === 'rfc5545') return `Custom calendar rule · ${timezone}`;
  return intervalLabel(schedule.interval_seconds);
}

function effectiveScheduleInstruction() {
  const webhook = document.getElementById('recurring-schedule-target')?.value === 'webhook';
  if (!webhook) return document.getElementById('recurring-schedule-instruction')?.value.trim() || '';
  const field = document.getElementById('recurring-schedule-message');
  return field?.dataset.binding === 'instruction' ? field.value.trim() : '';
}

function hasFiredOnce(schedule) {
  return !schedule.active && !schedule.retired_at && schedule.recurrence_type === 'once' && schedule.clock_finished === true;
}

function isFinishedOnce(schedule) {
  return hasFiredOnce(schedule)
    && ['accepted', 'completed', 'succeeded', 'done'].includes(schedule.last_outcome);
}

function showSchedulePageStatus(message, tone = 'info') {
  const status = document.getElementById('schedule-page-status');
  if (status) { status.textContent = message; status.dataset.tone = tone; }
}

function scrollToSchedule(definitionId) {
  const card = [...document.querySelectorAll('.schedule-card')].find((item) => item.dataset.scheduleId === definitionId);
  card?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
}

function scheduleDeliveryPending(schedule) {
  return schedule?.target?.kind === 'webhook'
    && ['pending', 'delivering'].includes(schedule.last_outcome);
}

function refreshScheduleUntilSettled(definitionId) {
  const prior = settlingScheduleRefreshes.get(definitionId);
  if (prior?.timer) window.clearTimeout(prior.timer);
  const watch = { timer: null };
  settlingScheduleRefreshes.set(definitionId, watch);
  const stop = () => { if (settlingScheduleRefreshes.get(definitionId) === watch) settlingScheduleRefreshes.delete(definitionId); };
  const scope = scheduleRunScope();
  const initial = (recurringSchedules.items || []).find((item) => item.definition_id === definitionId);
  const occurrenceId = initial?.last_occurrence_id || '';
  const savedExpiry = Date.parse(initial?.last_occurrence_expires_at || '');
  const deadline = Number.isFinite(savedExpiry) ? savedExpiry : Date.now() + 30000;
  const delays = [500, 1500, 3000, 5000, 10000, 20000, 30000];
  const stillVisible = () => settlingScheduleRefreshes.get(definitionId) === watch
    && scope === scheduleRunScope() && document.visibilityState !== 'hidden'
    && document.getElementById('schedules-view')?.hidden === false;
  const scheduleNext = (index) => {
    const remaining = deadline - Date.now();
    if (!stillVisible() || remaining <= 0) { stop(); return; }
    watch.timer = window.setTimeout(async () => {
      watch.timer = null;
      if (!stillVisible()) { stop(); return; }
      try {
        // Read this saved request only; do not reload connections or the control plane.
        const timeout = globalThis.AbortSignal?.timeout?.(Math.max(1, Math.min(10000, deadline - Date.now() + 1000)));
        const response = await fetch(api.withAuth(`/api/recurring-schedules/${encodeURIComponent(definitionId)}`),
          { cache: 'no-store', ...(timeout ? { signal: timeout } : {}) });
        if (!stillVisible()) { stop(); return; }
        if (!response.ok) { if (response.status === 404) stop(); else scheduleNext(index + 1); return; }
        const schedule = parseScheduleJson(await response.text());
        if (!stillVisible() || (occurrenceId && schedule.last_occurrence_id !== occurrenceId)) { stop(); return; }
        const position = (recurringSchedules.items || []).findIndex((item) => item.definition_id === definitionId);
        if (position < 0) { stop(); return; }
        if (recurringSchedules.items[position].last_outcome !== schedule.last_outcome) {
          scheduleOccurrences.delete(definitionId);
          scheduleHistoryGeneration.set(definitionId, (scheduleHistoryGeneration.get(definitionId) || 0) + 1);
        }
        recurringSchedules.items[position] = schedule;
        const outcome = schedule.last_outcome;
        if (['pending', 'delivering', 'queued', 'scheduled', 'running'].includes(outcome)) {
          renderSchedules(); scheduleNext(index + 1); return;
        }
        const message = plainRunOutcome(outcome, schedule.target?.kind === 'webhook');
        rememberScheduleAction(schedule, message, '', ['accepted', 'completed', 'succeeded', 'done'].includes(outcome) ? 'success' : ['unacknowledged', 'rejected', 'failed', 'expired', 'delivery_expired'].includes(outcome) ? 'error' : 'info');
        stop(); renderSchedules();
      } catch (_) {
        // A lost status read does not change the durable delivery or permit a duplicate.
        if (stillVisible()) scheduleNext(index + 1); else stop();
      }
    }, Math.min(delays[Math.min(index, delays.length - 1)], remaining));
  };
  scheduleNext(0);
}

function resumePendingScheduleRefreshes() {
  for (const schedule of recurringSchedules.items || []) {
    if (scheduleDeliveryPending(schedule) && !settlingScheduleRefreshes.has(schedule.definition_id)) refreshScheduleUntilSettled(schedule.definition_id);
  }
}

function showScheduleFieldError(error, status, savedOneOff = false) {
  const fields = error.validationFields || [];
  const known = [
    ['local_time', 'Choose a valid time.', 'recurring-schedule-local-time'],
    ['timezone_name', 'Choose a valid city or time zone.', 'schedule-timezone-search'],
    ['starts_at', 'Choose a valid future date and time.', 'recurring-schedule-starts-at'],
    ['first_run_at', 'Choose a valid future date and time.', 'recurring-schedule-starts-at'],
    ['interval_seconds', 'Repeat at least once a minute.', 'recurring-schedule-interval-value'],
    ['destination_id', 'Choose an app, or connect one first.', 'recurring-schedule-destination'],
    ['instruction', 'Tell the workspace what it should do.', 'recurring-schedule-instruction'],
    ['request_data', 'Check the extra app details.', 'recurring-schedule-request-data'],
    ['cron_expression', 'Use five parts, such as 0 9 * * 1 (Monday at 9 AM).', 'recurring-schedule-cron'],
  ];
  const match = known.find(([field]) => fields.includes(field) || String(error.message || '').startsWith(`${field} `));
  const message = match?.[1] || 'Couldn’t save this schedule. Check the details and try again.';
  if (status) { status.textContent = savedOneOff ? `Workspace saved. ${message}` : message; status.dataset.tone = 'error'; }
  if (match) {
    if (match[0] === 'timezone_name') { const picker = document.getElementById('schedule-timezone-picker'); if (picker) picker.hidden = false; renderTimeZoneChoices(); }
    if (match[0] === 'request_data') { document.getElementById('schedule-request-advanced').open = true; document.getElementById('schedule-data-json').open = true; }
    document.getElementById(match[2])?.focus();
  }
}

function scheduleTargetName(schedule) {
  return schedule.target?.kind === 'webhook'
    ? (webhookDestinations.find((item) => item.destination_id === schedule.target.destination_id)?.name || 'Connected app')
    : workspaceName(schedule.worker_id);
}

function scheduleNextLabel(schedule) {
  const next = schedule.next_occurrence_at || schedule.next_run_at;
  if (!schedule.active) {
    if (schedule.retired_at) return 'Removed · past runs saved';
    if (isFinishedOnce(schedule)) return `${schedule.last_outcome === 'accepted' ? 'Sent' : 'Done'} ${formatScheduleDateTime(schedule.last_occurrence_at, schedule.timezone_name || 'UTC')}`;
    if (hasFiredOnce(schedule)) return 'No future runs · check this request in history';
    return 'Paused · no future runs';
  }
  return `Next scheduled: ${formatScheduleDateTime(next, schedule.timezone_name || 'UTC')}`;
}

function renderScheduleWorkspaceOptions() {
  const select = document.getElementById('recurring-schedule-workspace');
  if (!select) return;
  const selected = select.value;
  const workspaces = [...(workspaceCatalog.items || []), ...(oneOffWorkspaceCatalog.items || [])];
  const placeholder = node('option', '', workspaces.length
    ? (workspaceCatalog.next_cursor || oneOffWorkspaceCatalog.next_cursor ? 'Choose recent workspace · search Workspaces for more' : 'Choose a workspace')
    : 'No workspaces yet');
  placeholder.value = '';
  const options = workspaces.map((workspace) => {
    const label = String(workspace.name || workspace.title || workspace.worker_id || 'Workspace');
    const option = node('option', '', workspace.workspace_kind === 'ephemeral' ? `${label} · save for schedule` : label);
    option.value = String(workspace.worker_id || '');
    return option;
  });
  select.replaceChildren(placeholder, ...options);
  if (options.some((option) => option.value === selected)) select.value = selected;
  select.disabled = !options.length;
  const emptyState = document.getElementById('recurring-schedule-workspace-empty');
  if (emptyState) emptyState.hidden = Boolean(options.length);
  const webhook = document.getElementById('recurring-schedule-target')?.value === 'webhook';
  const needsWorkspace = !webhook && !options.length && !editingScheduleId;
  for (const id of ['recurring-schedule-instruction-field', 'recurring-schedule-fields',
    'recurring-schedule-advanced', 'recurring-schedule-submit', 'schedule-when-heading', 'schedule-preview']) {
    const field = document.getElementById(id);
    if (field) field.hidden = needsWorkspace || (webhook && id === 'recurring-schedule-instruction-field');
  }
}

const WEBHOOK_TIMING_LABELS = {
  resolve_ms: 'Look up receiver address',
  connect_ms: 'Connect to receiver',
  tls_ms: 'Verify secure connection',
  sign_ms: 'Sign the request',
  write_ms: 'Send the request',
  read_ms: 'Receive the response',
  to_response_headers_ms: 'Request to response headers · includes connection',
  executor_to_send_ms: 'Prepare delivery',
  clock_lateness_ms: 'Scheduled time to clock detection',
  outbox_wait_ms: 'Clock detection to sender start · includes save and queue',
  executor_queue_ms: 'Sender queue wait',
  due_to_claim_ms: 'Scheduled time to clock claim',
  claim_to_commit_ms: 'Clock claim to saved delivery',
  outbox_to_executor_ms: 'Saved delivery to available sender',
  total_ms: 'HTTP attempt total · signing, connection, send and response',
};

function occurrenceDeliveryDetails(occurrence) {
  const values = Object.entries(occurrence.timings_ms || {}).filter(([, value]) => Number.isFinite(value));
  if (!values.length) return null;
  const details = node('details', 'schedule-history-error');
  details.append(node('summary', '', 'Delivery times'));
  const lines = values.map(([key, value]) => `${WEBHOOK_TIMING_LABELS[key] || key}: ${value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(2)} ms`}`);
  details.append(node('pre', '', lines.join('\n')));
  return details;
}

function renderOccurrenceHistory(container, items, timezoneName = 'UTC') {
  if (!items.length) {
    container.replaceChildren(emptyList('No runs yet'));
    return;
  }
  container.replaceChildren(...items.map((occurrence) => {
    const row = node('div', 'schedule-history-row');
    const outcome = effectiveRunOutcome(occurrence);
    row.append(
      node('span', '', formatScheduleDateTime(occurrence.scheduled_for, timezoneName)),
      (() => { const chip = statusChip(outcome); chip.textContent = plainRunOutcome(outcome, !occurrence.scheduled_run_id); return chip; })(),
    );
    if (outcome === 'accepted') row.append(node('span', 'schedule-history-detail', 'The app received this request. Its task can continue in the background.'));
    if (Number.isInteger(occurrence.http_status)) row.append(node('span', 'schedule-history-detail', `Receiver replied HTTP ${occurrence.http_status}`));
    const deliveryDetails = occurrenceDeliveryDetails(occurrence);
    if (deliveryDetails) row.append(deliveryDetails);
    const error = String(occurrence.last_error || '').trim();
    if (error) {
      const recovery = outcome === 'unacknowledged'
        ? 'Your app may already have started. Check there before sending a new request.'
        : !occurrence.scheduled_run_id ? 'Check the connection and Error details before retrying.' : 'Open the workspace to see what needs help.';
      row.append(node('span', 'schedule-history-detail', recovery));
      const details = node('details', 'schedule-history-error');
      details.append(node('summary', '', 'Error details'), node('pre', '', error));
      row.append(details);
    }
    return row;
  }));
}

async function toggleOccurrenceHistory(definitionId, container, button) {
  if (!container.hidden) {
    container.hidden = true;
    button.textContent = 'View history';
    return;
  }
  container.hidden = false;
  button.textContent = 'Hide history';
  if (scheduleOccurrences.has(definitionId)) {
    renderOccurrenceHistory(container, scheduleOccurrences.get(definitionId), container.dataset.timezone || 'UTC');
    return;
  }
  await loadOccurrenceHistory(definitionId, container);
}

async function loadOccurrenceHistory(definitionId, container) {
  const generation = scheduleHistoryGeneration.get(definitionId) || 0;
  container.replaceChildren(node('span', 'schedule-card-meta', 'Loading past runs…'));
  try {
    const response = await fetch(api.withAuth(`/api/recurring-schedules/${encodeURIComponent(definitionId)}/occurrences?limit=20`));
    if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load past runs'));
    const payload = await response.json();
    if (generation !== (scheduleHistoryGeneration.get(definitionId) || 0)) return;
    const items = payload.items || [];
    scheduleOccurrences.set(definitionId, items);
    renderOccurrenceHistory(container, items, container.dataset.timezone || 'UTC');
  } catch (error) {
    if (generation !== (scheduleHistoryGeneration.get(definitionId) || 0)) return;
    const message = node('span', 'schedule-history-detail', `Could not load history: ${error.message}`);
    const retry = node('button', 'quiet-button', 'Retry history');
    retry.type = 'button';
    retry.addEventListener('click', () => void loadOccurrenceHistory(definitionId, container));
    container.replaceChildren(message, retry);
  }
}

function scheduleActionStatus(fallback) {
  return fallback || document.getElementById('recurring-schedule-status');
}

function setScheduleEditorVisible(visible) {
  scheduleEditorVisible = Boolean(visible);
  const card = document.getElementById('schedule-create-card');
  if (card) card.hidden = !scheduleEditorVisible;
  const layout = document.querySelector('.schedule-layout');
  layout?.classList.toggle('schedule-editor-open', scheduleEditorVisible);
}

function rememberScheduleAction(schedule, message, destination = '', tone = 'info') {
  scheduleActionMessages.set(String(schedule.definition_id || ''), { message, destination, tone });
}

function scheduleRunScope() {
  const tenantId = String(controlPlane?.me?.tenant_id || '').trim();
  const userId = String(controlPlane?.me?.user_id || '').trim();
  return tenantId && userId ? `${tenantId}\u0000${userId}` : '';
}

function scheduleRunStorageKey(scope) {
  return `${UNCERTAIN_SCHEDULE_RUN_STORAGE_KEY}:${encodeURIComponent(scope)}`;
}

function restoreUncertainScheduleRunKeys() {
  const scope = scheduleRunScope();
  if (scope === uncertainScheduleRunScope) return;
  if (uncertainScheduleRunScope) {
    recurringSchedules = { items: [] };
    webhookDestinations = [];
    pendingDestinationCreate = null;
    closeDestinationDialog();
    document.getElementById('recurring-schedule-form')?.reset();
    setRequestData({});
    editingScheduleId = ''; editingScheduleSnapshot = null; pendingScheduleCreate = null;
    setScheduleEditorVisible(false);
    const secretOnce = document.getElementById('webhook-destination-secret-once');
    if (secretOnce) { secretOnce.textContent = ''; secretOnce.hidden = true; }
    scheduleActionMessages.clear();
    scheduleOccurrences.clear();
    scheduleHistoryGeneration.clear();
  }
  uncertainScheduleRunScope = scope;
  uncertainScheduleRunKeys.clear();
  if (!scope) return;
  try {
    const saved = JSON.parse(globalThis.sessionStorage?.getItem(scheduleRunStorageKey(scope)) || 'null');
    if (saved?.scope !== scope || !saved.keys || typeof saved.keys !== 'object') return;
    for (const [definitionId, key] of Object.entries(saved.keys)) {
      if (definitionId && typeof key === 'string' && key.length >= 8 && key.length <= 128) {
        uncertainScheduleRunKeys.set(definitionId, key);
      }
    }
  } catch (_error) {
    // Private browsing can disable session storage; the in-page key still protects list refreshes.
  }
}

function persistUncertainScheduleRunKeys() {
  if (!uncertainScheduleRunScope) return;
  try {
    if (uncertainScheduleRunKeys.size) {
      globalThis.sessionStorage?.setItem(scheduleRunStorageKey(uncertainScheduleRunScope), JSON.stringify({
        scope: uncertainScheduleRunScope,
        keys: Object.fromEntries(uncertainScheduleRunKeys),
      }));
    } else {
      globalThis.sessionStorage?.removeItem(scheduleRunStorageKey(uncertainScheduleRunScope));
    }
  } catch (_error) {
    // Keep the in-page key if storage is unavailable.
  }
}

function renderScheduleActionStatus(status, remembered) {
  status.replaceChildren();
  status.dataset.tone = remembered?.tone || 'info';
  if (!remembered?.message) return;
  status.append(document.createTextNode(remembered.message));
  if (remembered.destination) {
    const link = node('a', 'inline-link', 'Open Connections');
    link.href = remembered.destination;
    status.append(document.createTextNode(' '), link);
  }
}

async function deactivateSchedule(schedule, button, actionStatus) {
  const name = scheduleTargetName(schedule);
  if (!window.confirm(`Pause future runs for ${name}? You can resume it later and your past runs stay saved.`)) return;
  button.disabled = true;
  button.textContent = 'Pausing…';
  const status = scheduleActionStatus(actionStatus);
  try {
    await api.patchJson(`/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}`, { enabled: false });
    const message = 'Schedule paused. Your past runs stay saved.';
    rememberScheduleAction(schedule, message);
    if (status) status.textContent = message;
    await loadSchedules();
  } catch (error) {
    if (status) status.textContent = error.message;
    button.disabled = false;
    button.textContent = 'Pause';
  }
}

async function resumeSchedule(schedule, button, actionStatus) {
  button.disabled = true;
  button.textContent = 'Resuming…';
  const status = scheduleActionStatus(actionStatus);
  try {
    await api.patchJson(`/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}`, { enabled: true });
    const message = 'Schedule resumed. Its task and history are unchanged.';
    rememberScheduleAction(schedule, message);
    if (status) status.textContent = message;
    await loadSchedules();
  } catch (error) {
    if (status) status.textContent = error.message;
    button.disabled = false;
    button.textContent = 'Resume schedule';
  }
}

function resetScheduleEditor(message = '', { hide = true } = {}) {
  editingScheduleId = '';
  editingScheduleSnapshot = null;
  pendingScheduleCreate = null;
  const secretOnce = document.getElementById('webhook-destination-secret-once');
  if (secretOnce) { secretOnce.textContent = ''; secretOnce.hidden = true; }
  const form = document.getElementById('recurring-schedule-form');
  form?.reset();
  setRequestData({});
  for (const id of ['schedule-request-advanced', 'schedule-data-json', 'recurring-schedule-advanced']) { const details = document.getElementById(id); if (details) details.open = false; }
  closeDestinationDialog();
  const destinationName = document.getElementById('webhook-destination-name');
  if (destinationName) delete destinationName.dataset.destinationId;
  if (form) {
    delete form.dataset.scheduleWeekday;
    delete form.dataset.scheduleWeeklyTime;
    delete form.dataset.createIdempotencyKey;
  }
  const timezone = document.getElementById('recurring-schedule-timezone');
  const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (timezone && browserTimezone) timezone.value = browserTimezone;
  const workspace = document.getElementById('recurring-schedule-workspace');
  if (workspace) workspace.disabled = !(workspaceCatalog.items || []).length && !(oneOffWorkspaceCatalog.items || []).length;
  const submit = document.getElementById('recurring-schedule-submit');
  if (submit) submit.textContent = 'Create schedule';
  const cancel = document.getElementById('recurring-schedule-cancel-edit');
  if (cancel) {
    cancel.hidden = hide;
    cancel.textContent = 'Cancel';
  }
  const title = document.getElementById('recurring-schedule-form-title');
  if (title) title.textContent = 'New schedule';
  const status = document.getElementById('recurring-schedule-status');
  if (status) { status.textContent = message; status.dataset.tone = 'info'; }
  if (message && hide) showSchedulePageStatus(message, 'success');
  const weeklyTime = document.getElementById('recurring-schedule-weekly-time');
  if (weeklyTime) weeklyTime.value = '09:00';
  const weekday = document.getElementById('recurring-schedule-weekday');
  if (weekday) weekday.value = 'MO';
  const customType = document.getElementById('recurring-schedule-custom-type');
  if (customType) customType.value = 'interval';
  setScheduleEditorVisible(!hide);
  syncRecurringScheduleFields();
}

function openScheduleEditor() {
  void loadWebhookDestinations();
  resetScheduleEditor('', { hide: false });
  renderScheduleWorkspaceOptions();
  document.querySelector('[name="schedule-target-choice"]:checked')?.focus();
}

function editSchedule(schedule) {
  editingScheduleId = String(schedule.definition_id || '');
  setScheduleEditorVisible(true);
  const setValue = (id, value) => {
    const field = document.getElementById(id);
    if (field) field.value = value == null ? '' : String(value);
  };
  setValue('recurring-schedule-target', schedule.target?.kind || 'worker');
  setValue('recurring-schedule-destination', schedule.target?.destination_id || '');
  setRequestData(schedule.target?.request_data || {}, schedule.target?.kind === 'webhook' ? String(schedule.instruction || '') : '', true);
  for (const id of ['schedule-request-advanced', 'schedule-data-json']) { const details = document.getElementById(id); if (details) details.open = false; }
  syncWebhookDestinationActions();
  setValue('recurring-schedule-workspace', schedule.worker_id);
  setValue('recurring-schedule-instruction', schedule.instruction);
  const intervalSeconds = Number(schedule.interval_seconds || 3600);
  const editorRecurrenceType = scheduleEditorType(schedule);
  const simpleRecurrenceType = ['once', 'daily', 'weekly'].includes(editorRecurrenceType)
    ? editorRecurrenceType
    : 'custom';
  setValue('recurring-schedule-type', simpleRecurrenceType);
  setValue('recurring-schedule-custom-type', simpleRecurrenceType === 'custom' ? editorRecurrenceType : 'interval');
  setValue('recurring-schedule-local-time', schedule.local_time || '09:00');
  setValue('recurring-schedule-timezone', schedule.timezone_name || 'UTC');
  const zonePicker = document.getElementById('schedule-timezone-picker'); if (zonePicker) zonePicker.hidden = true;
  const zoneChange = document.getElementById('schedule-timezone-change'); if (zoneChange) zoneChange.setAttribute('aria-expanded', 'false');
  setValue('recurring-schedule-dst-policy', scheduleDstPolicyState(editorRecurrenceType, schedule.dst_policy).value);
  setValue(
    'recurring-schedule-starts-at',
    zonedDateTimeLocalValue(schedule.starts_at, schedule.timezone_name || 'UTC'),
  );
  const startLocal = zonedDateTimeLocalValue(schedule.starts_at, schedule.timezone_name || 'UTC');
  setValue(
    'recurring-schedule-weekday',
    editorRecurrenceType === 'weekly'
      ? weekdayAtInstant(schedule.starts_at, schedule.timezone_name || 'UTC')
      : 'MO',
  );
  setValue(
    'recurring-schedule-weekly-time',
    editorRecurrenceType === 'weekly' && startLocal
      ? startLocal.slice(11, 16)
      : '09:00',
  );
  const form = document.getElementById('recurring-schedule-form');
  if (form && editorRecurrenceType === 'weekly') {
    form.dataset.scheduleWeekday = document.getElementById('recurring-schedule-weekday')?.value || '';
    form.dataset.scheduleWeeklyTime = document.getElementById('recurring-schedule-weekly-time')?.value || '';
  }
  setValue(
    'recurring-schedule-ends-at',
    zonedDateTimeLocalValue(schedule.ends_at, schedule.timezone_name || 'UTC'),
  );
  setValue('recurring-schedule-cron', schedule.cron_expression || '');
  setValue('recurring-schedule-rrule', schedule.rrule || '');
  const intervalUnit = intervalSeconds > 0 && intervalSeconds % 604800 === 0
    ? 'weeks'
    : (intervalSeconds > 0 && intervalSeconds % 86400 === 0
      ? 'days'
      : (intervalSeconds > 0 && intervalSeconds % 3600 === 0 ? 'hours' : 'minutes'));
  setValue('recurring-schedule-interval-unit', intervalUnit);
  const intervalFactor = intervalUnit === 'weeks'
    ? 604800
    : (intervalUnit === 'days' ? 86400 : (intervalUnit === 'hours' ? 3600 : intervalUnit === 'minutes' ? 60 : 1));
  setValue('recurring-schedule-interval-value', intervalSeconds / intervalFactor);
  setValue('recurring-schedule-overlap-policy', schedule.overlap_policy || 'skip');
  setValue('recurring-schedule-misfire-grace', Number(schedule.misfire_grace_seconds || 0) / 60);
  setValue('recurring-schedule-catch-up-policy', schedule.catch_up_policy || 'skip');
  setValue('recurring-schedule-catch-up-limit', schedule.max_catch_up_occurrences || 1);
  setValue('recurring-schedule-jitter', schedule.jitter_seconds || 0);
  const enabled = document.getElementById('recurring-schedule-enabled');
  if (enabled) enabled.checked = Boolean(schedule.enabled);
  const workspace = document.getElementById('recurring-schedule-workspace');
  if (workspace) workspace.disabled = !(workspaceCatalog.items || []).length && !(oneOffWorkspaceCatalog.items || []).length;
  const submit = document.getElementById('recurring-schedule-submit');
  if (submit) submit.textContent = 'Save changes';
  const cancel = document.getElementById('recurring-schedule-cancel-edit');
  if (cancel) {
    cancel.hidden = false;
    cancel.textContent = 'Cancel';
  }
  const title = document.getElementById('recurring-schedule-form-title');
  if (title) title.textContent = 'Edit schedule';
  const status = document.getElementById('recurring-schedule-status');
  if (status) status.textContent = 'Your past runs stay saved.';
  syncRecurringScheduleFields();
  editingScheduleSnapshot = {
    schedule: { ...schedule },
    initial: readRecurringScheduleFormState(),
  };
  document.getElementById('schedule-create-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function runScheduleNow(schedule, button, actionStatus, historyContainer = null) {
  const current = (recurringSchedules.items || []).find((item) => item.definition_id === schedule.definition_id) || schedule;
  if (scheduleDeliveryPending(current)) {
    button.disabled = true;
    button.textContent = 'Sending…';
    refreshScheduleUntilSettled(schedule.definition_id);
    return;
  }
  restoreUncertainScheduleRunKeys();
  button.disabled = true;
  button.textContent = 'Requesting…';
  const status = scheduleActionStatus(actionStatus);
  const definitionId = String(schedule.definition_id || '');
  const idempotencyKey = uncertainScheduleRunKeys.get(definitionId)
    || globalThis.crypto?.randomUUID?.()
    || `run-now-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  uncertainScheduleRunKeys.set(definitionId, idempotencyKey);
  persistUncertainScheduleRunKeys();
  scheduleOccurrences.delete(definitionId);
  scheduleHistoryGeneration.set(definitionId, (scheduleHistoryGeneration.get(definitionId) || 0) + 1);
  try {
    const result = await api.postJson(
      `/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}/run-now`,
      { idempotency_key: idempotencyKey },
    );
    uncertainScheduleRunKeys.delete(definitionId);
    persistUncertainScheduleRunKeys();
    const message = result.status === 'owner_action_required'
      ? String(result.message || 'Reconnect the affected account, then try again.')
      : schedule.target?.kind === 'webhook'
        ? 'Delivery requested. History shows when your app receives it.'
        : 'Run requested. It will use the same private workspace and connected capabilities.';
    const recoveryDestination = String(result.action_url || '')
      || (result.recovery_route === 'connections' ? '/#connections' : '');
    rememberScheduleAction(schedule, message, recoveryDestination);
    if (status) status.textContent = message;
    await loadSchedules();
    refreshScheduleUntilSettled(definitionId);
  } catch (error) {
    const message = `${error.message} We could not confirm whether it started. Check history before trying again.`;
    rememberScheduleAction(schedule, message, '', 'error');
    if (status) { status.textContent = message; status.dataset.tone = 'error'; }
    button.disabled = false;
    button.textContent = 'Confirm prior run';
    if (historyContainer && !historyContainer.hidden) {
      await loadOccurrenceHistory(definitionId, historyContainer);
    }
  }
}

async function retireSchedule(schedule, button, actionStatus) {
  const name = scheduleTargetName(schedule);
  if (!window.confirm(`Remove this schedule for ${name}? It cannot be resumed, but your past runs stay saved.`)) return;
  button.disabled = true;
  button.textContent = 'Removing…';
  const status = scheduleActionStatus(actionStatus);
  try {
    await api.deleteJson(`/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}`);
    if (editingScheduleId === schedule.definition_id) resetScheduleEditor();
    await loadSchedules();
    showSchedulePageStatus('Schedule removed. Your past runs stay saved.', 'success');
  } catch (error) {
    if (status) status.textContent = error.message;
    button.disabled = false;
    button.textContent = 'Remove';
  }
}

function scheduleEmptyState(message = 'No schedules yet') {
  const item = node('div', 'empty-state-compact');
  item.append(
    node('strong', '', message),
    node('span', '', 'Set a time for a task or send something to an app.'),
  );
  return item;
}

function scheduleLoadErrorNotice(message, hasCachedSchedules) {
  const notice = node('div', 'schedule-load-error');
  notice.setAttribute('role', 'alert');
  notice.append(node('span', '', `Could not refresh schedules: ${message}${hasCachedSchedules ? ' Showing the last loaded list.' : ''}`));
  const retry = node('button', 'quiet-button', 'Retry');
  retry.type = 'button';
  retry.addEventListener('click', () => {
    retry.disabled = true;
    retry.textContent = 'Retrying…';
    void loadSchedules();
  });
  notice.append(retry);
  return notice;
}

function renderSchedules() {
  renderScheduleWorkspaceOptions();
  const list = document.getElementById('schedule-list');
  if (!list) return;
  setScheduleEditorVisible(scheduleEditorVisible);
  const loadError = scheduleLoadError
    ? scheduleLoadErrorNotice(scheduleLoadError, Boolean((recurringSchedules.items || []).length))
    : null;
  const count = document.getElementById('schedule-count');
  if (count) count.textContent = String((recurringSchedules.items || []).length);
  const schedules = [...(recurringSchedules.items || [])].sort((left, right) => {
    if (Boolean(left.active) !== Boolean(right.active)) return left.active ? -1 : 1;
    return String(left.next_run_at || '').localeCompare(String(right.next_run_at || ''));
  });
  const scheduleIds = new Set(schedules.map((schedule) => String(schedule.definition_id || '')));
  for (const definitionId of scheduleActionMessages.keys()) {
    if (!scheduleIds.has(definitionId)) scheduleActionMessages.delete(definitionId);
  }
  if (scheduleCatalogReady && !scheduleLoadError) {
    for (const definitionId of uncertainScheduleRunKeys.keys()) {
      if (!scheduleIds.has(definitionId)) uncertainScheduleRunKeys.delete(definitionId);
    }
    persistUncertainScheduleRunKeys();
  }
  if (!schedules.length) {
    list.replaceChildren(loadError || scheduleEmptyState());
    return;
  }
  const cards = schedules.map((schedule) => {
    const card = node('article', 'schedule-card');
    card.dataset.scheduleId = String(schedule.definition_id || '');
    const head = node('div', 'schedule-card-head');
    const copy = node('div', 'schedule-card-copy');
    copy.append(
      node('strong', '', String(schedule.instruction || (schedule.target?.kind === 'webhook' ? (typeof schedule.target.request_data?.message === 'string' && schedule.target.request_data.message || `Send to ${scheduleTargetName(schedule)}`) : 'Scheduled task'))),
      node('span', '', `${schedule.target?.kind === 'webhook' ? 'App' : 'Workspace'} · ${scheduleTargetName(schedule)}`),
    );
    const retired = Boolean(schedule.retired_at);
    const finished = isFinishedOnce(schedule);
    const stateChip = statusChip(retired ? 'retired' : (finished ? 'done' : hasFiredOnce(schedule) ? schedule.last_outcome || 'pending' : schedule.active ? 'active' : 'paused'));
    if (hasFiredOnce(schedule) && !finished) stateChip.textContent = plainRunOutcome(schedule.last_outcome || 'pending', schedule.target?.kind === 'webhook');
    head.append(copy, stateChip);
    const meta = node('div', 'schedule-card-meta');
    meta.append(
      node('span', '', recurrenceLabel(schedule)),
      node('span', '', scheduleNextLabel(schedule)),
    );
    if (schedule.last_occurrence_at) {
      meta.append(node('span', '', `Last run: ${formatScheduleDateTime(schedule.last_occurrence_at, schedule.timezone_name || 'UTC')}`));
    }
    if (schedule.last_outcome || schedule.last_error) {
      const latestResult = node(
        'span',
        'schedule-history-detail inline-status',
        `Latest result: ${plainRunOutcome(schedule.last_outcome, schedule.target?.kind === 'webhook')}${schedule.last_error ? ' · Details in history' : ''}`,
      );
      latestResult.dataset.tone = ['accepted', 'completed', 'succeeded', 'done'].includes(schedule.last_outcome) ? 'success'
        : ['unacknowledged', 'rejected', 'failed', 'expired', 'delivery_expired'].includes(schedule.last_outcome) ? 'error' : 'info';
      meta.append(latestResult);
    }
    const actions = node('div', 'schedule-card-actions');
    const actionStatus = node('p', 'inline-status');
    actionStatus.setAttribute('aria-live', 'polite');
    actionStatus.setAttribute('data-schedule-status', String(schedule.definition_id || ''));
    renderScheduleActionStatus(
      actionStatus,
      scheduleActionMessages.get(String(schedule.definition_id || '')),
    );
    const history = node('div', 'schedule-history');
    history.hidden = true;
    history.dataset.timezone = String(schedule.timezone_name || 'UTC');
    if (!retired) {
      const unconfirmedRun = uncertainScheduleRunKeys.has(String(schedule.definition_id || ''));
      if (unconfirmedRun && !scheduleActionMessages.has(String(schedule.definition_id || ''))) {
        rememberScheduleAction(schedule, 'A previous Run now request is unconfirmed. Check history, then confirm it before starting a new run.');
        renderScheduleActionStatus(actionStatus, scheduleActionMessages.get(String(schedule.definition_id || '')));
      }
      const pendingDelivery = scheduleDeliveryPending(schedule);
      const runNowButton = node('button', 'quiet-button', pendingDelivery ? 'Sending…' : unconfirmedRun ? 'Confirm prior run' : schedule.last_outcome === 'unacknowledged' ? 'Send again' : 'Run now');
      runNowButton.disabled = pendingDelivery;
      runNowButton.type = 'button';
      runNowButton.addEventListener('click', () => runScheduleNow(schedule, runNowButton, actionStatus, history));
      const editButton = node('button', 'quiet-button', 'Edit');
      editButton.type = 'button';
      editButton.addEventListener('click', () => editSchedule(schedule));
      actions.append(runNowButton, editButton);
    }
    if (schedule.active && !retired) {
      const stopButton = node('button', 'quiet-button', 'Pause');
      stopButton.type = 'button';
      stopButton.addEventListener('click', () => deactivateSchedule(schedule, stopButton, actionStatus));
      actions.append(stopButton);
    } else if (!retired && !hasFiredOnce(schedule)) {
      const resumeButton = node('button', 'quiet-button', 'Resume schedule');
      resumeButton.type = 'button';
      resumeButton.addEventListener('click', () => resumeSchedule(schedule, resumeButton, actionStatus));
      actions.append(resumeButton);
    }
    const more = node('details', 'schedule-card-more');
    const moreSummary = node('summary', 'quiet-button', 'More');
    moreSummary.setAttribute('aria-label', `More actions for ${scheduleTargetName(schedule)}`);
    const moreActions = node('div', 'schedule-card-more-actions');
    const historyButton = node('button', 'quiet-button', 'View history');
    historyButton.type = 'button';
    historyButton.addEventListener('click', () => toggleOccurrenceHistory(String(schedule.definition_id || ''), history, historyButton));
    if (schedule.last_error) actions.append(historyButton);
    else moreActions.append(historyButton);
    if (!retired) {
      const retireButton = node('button', 'quiet-button', 'Remove');
      retireButton.type = 'button';
      retireButton.addEventListener('click', () => retireSchedule(schedule, retireButton, actionStatus));
      moreActions.append(retireButton);
    }
    more.append(moreSummary, moreActions);
    attachActionMenu(more, moreSummary, moreActions);
    actions.append(more);
    card.append(head, meta, actions, actionStatus, history);
    return card;
  });
  list.replaceChildren(...(loadError ? [loadError] : []), ...cards);
}

function setDataStatus(message) {
  const status = document.getElementById('schedule-data-status');
  if (status) status.textContent = message;
}

function syncMessageField(value) {
  const field = document.getElementById('recurring-schedule-message');
  if (!field) return;
  const instruction = field.dataset.binding === 'instruction';
  if (!instruction) field.value = typeof value.message === 'string' ? value.message : '';
  field.dataset.originalValue = field.value;
  const custom = !instruction && Object.hasOwn(value, 'message') && typeof value.message !== 'string';
  const precise = !instruction && hasPreciseNumbers(value);
  field.disabled = custom || precise;
  const note = document.getElementById('schedule-message-note');
  if (note) { note.hidden = !custom && !precise; note.textContent = precise ? 'This app has very large numbers. Edit its JSON in Extra app details to keep them exact.' : 'This app’s message has a custom format. Edit it in Extra app details.'; }
}

function setRequestData(value, instruction = '', saved = false) {
  savedRequestDataKeys = saved ? new Set(Object.keys(value)) : new Set();
  const message = document.getElementById('recurring-schedule-message');
  if (message) { message.dataset.binding = instruction ? 'instruction' : 'message'; message.value = instruction; }
  const label = document.getElementById('schedule-message-label');
  if (label) label.textContent = instruction ? 'What should happen?' : 'What should it say? (optional)';
  const field = document.getElementById('recurring-schedule-request-data');
  if (field) field.value = JSON.stringify(value, null, 2);
  syncMessageField(value);
  renderDataRows(value);
  setDataStatus('');
}

function appendDataRow(item) {
  const container = document.getElementById('schedule-data-fields');
  if (!container) return;
  const row = node('div', 'schedule-data-row');
  const key = node('input'); key.type = 'text'; key.value = item.key; key.placeholder = 'Name'; key.setAttribute('aria-label', 'Detail name');
  const type = node('select'); type.setAttribute('aria-label', 'Detail type');
  for (const [value, label] of [['string', 'Text'], ['number', 'Number'], ['boolean', 'True / false'], ['null', 'Empty']]) {
    const option = node('option', '', label); option.value = value; type.append(option);
  }
  type.value = item.type;
  const value = node('input'); value.type = 'text'; value.value = item.value; value.placeholder = 'Value'; value.setAttribute('aria-label', 'Detail value');
  if (savedRequestDataKeys.has(item.key)) { key.readOnly = true; value.readOnly = true; type.disabled = true; row.dataset.saved = 'true'; }
  const remove = node('button', 'schedule-icon-button', '×'); remove.type = 'button'; remove.setAttribute('aria-label', 'Remove detail');
  if (savedRequestDataKeys.has(item.key)) remove.disabled = true;
  const sync = () => { try { commitDataRows(); setDataStatus(''); } catch (error) { setDataStatus(error.message); } };
  key.addEventListener('input', sync); type.addEventListener('change', sync); value.addEventListener('input', sync);
  remove.addEventListener('click', () => { row.remove(); sync(); });
  row.append(key, type, value, remove); container.append(row);
}

function renderDataRows(value) {
  const container = document.getElementById('schedule-data-fields');
  if (!container) return;
  const extras = Object.fromEntries(Object.entries(value).filter(([key, item]) => key !== 'message' || typeof item !== 'string'));
  const rows = requestDataRows(extras);
  container.replaceChildren(); container.dataset.mode = rows ? 'fields' : 'json';
  const note = document.getElementById('schedule-data-complex');
  const add = document.getElementById('schedule-data-add');
  if (note) note.hidden = Boolean(rows);
  if (add) add.hidden = !rows;
  if (rows) rows.forEach(appendDataRow);
}

function commitDataRows() {
  const container = document.getElementById('schedule-data-fields');
  const raw = document.getElementById('recurring-schedule-request-data');
  if (!container || !raw || container.dataset.mode !== 'fields') return;
  const previous = parseRequestData(raw.value);
  const rows = [...container.querySelectorAll('.schedule-data-row')].map((row) => {
    const [key, value] = row.querySelectorAll('input');
    return { key: key.value, value: value.value, type: row.querySelector('select').value };
  });
  const result = requestDataFromRows(rows);
  if (typeof previous.message === 'string') Object.defineProperty(result, 'message', { value: previous.message, enumerable: true, configurable: true, writable: true });
  raw.value = JSON.stringify(result, null, 2);
}

function syncRequestMessage() {
  const field = document.getElementById('recurring-schedule-message');
  const raw = document.getElementById('recurring-schedule-request-data');
  if (!field || !raw || field.dataset.binding === 'instruction' || field.disabled) return;
  try {
    raw.value = JSON.stringify(withRequestMessage(raw.value, field.value, field.dataset.originalValue || ''), null, 2);
    field.dataset.originalValue = field.value; setDataStatus('');
  } catch (_error) { setDataStatus('Check the JSON in Extra app details before changing this message.'); }
}

function collectRequestData() {
  // JSON edits update the fields as they are entered. Invalid row values must
  // fail visibly rather than silently sending the last valid value.
  commitDataRows();
  const field = document.getElementById('recurring-schedule-message');
  const raw = document.getElementById('recurring-schedule-request-data')?.value || '{}';
  if (field?.dataset.binding === 'instruction' || field?.disabled) return parseRequestData(raw);
  return withRequestMessage(raw, field?.value || '', field?.dataset.originalValue || '');
}

function clearDestinationSecrets() {
  for (const id of ['webhook-destination-secret', 'webhook-destination-headers', 'webhook-destination-url']) {
    const field = document.getElementById(id); if (field) field.value = '';
  }
  const once = document.getElementById('webhook-destination-secret-once');
  if (once) { once.textContent = ''; once.hidden = true; }
  const panel = document.getElementById('webhook-destination-secret-panel');
  if (panel) panel.hidden = true;
}

function closeDestinationDialog() {
  const dialog = document.getElementById('webhook-destination-setup');
  if (dialog?.open) dialog.close();
  clearDestinationSecrets();
}

function openDestinationDialog(manage = false) {
  const dialog = document.getElementById('webhook-destination-setup');
  const form = document.getElementById('webhook-destination-form');
  if (!dialog || !form) return;
  clearDestinationSecrets(); form.reset();
  const selectedId = document.getElementById('recurring-schedule-destination')?.value || '';
  const selected = manage && webhookDestinations.find((item) => item.destination_id === selectedId);
  form.dataset.mode = selected ? 'manage' : 'create';
  document.getElementById('webhook-destination-title').textContent = selected ? 'Manage connection' : 'Connect an app';
  document.getElementById('webhook-destination-name').value = selected?.name || '';
  document.getElementById('webhook-destination-url-field').hidden = Boolean(selected);
  document.getElementById('webhook-destination-existing-note').hidden = !selected;
  document.getElementById('webhook-destination-save').textContent = selected ? 'Save changes' : 'Connect app';
  document.getElementById('webhook-destination-done').textContent = 'Cancel';
  document.getElementById('webhook-destination-mode').disabled = Boolean(selected);
  document.getElementById('webhook-destination-mode').value = selected?.payload_mode || 'envelope';
  document.getElementById('webhook-destination-advanced').open = false;
  document.getElementById('webhook-destination-status').textContent = '';
  for (const id of ['webhook-destination-rotate', 'webhook-destination-toggle']) document.getElementById(id).hidden = !selected;
  syncWebhookDestinationActions();
  // Name belongs to the new connection, not the currently selected connection.
  if (!selected) document.getElementById('webhook-destination-name').value = '';
  dialog.showModal(); document.getElementById('webhook-destination-name').focus();
}

function timeZoneCity(zone) {
  return String(zone || 'UTC').split('/').pop().replaceAll('_', ' ');
}

function syncTimeZoneSummary() {
  const zone = document.getElementById('recurring-schedule-timezone')?.value || 'UTC';
  const city = timeZoneCity(zone);
  const summary = document.getElementById('schedule-timezone-city');
  const note = document.getElementById('recurring-schedule-timezone-note');
  if (summary) { summary.textContent = city; summary.title = zone; }
  if (note) note.textContent = `Times use ${city}’s local clock.`;
}

function renderTimeZoneChoices() {
  const choice = document.getElementById('schedule-timezone-choice');
  if (!choice) return;
  const current = document.getElementById('recurring-schedule-timezone')?.value || 'UTC';
  const query = document.getElementById('schedule-timezone-search')?.value.trim().toLocaleLowerCase() || '';
  let zones = [];
  try { zones = Intl.supportedValuesOf('timeZone'); } catch (_error) { /* Saved and local zones remain available on older browsers. */ }
  const available = [...new Set([current, 'UTC', ...zones])]
    .filter((zone) => !query || `${timeZoneCity(zone)} ${zone}`.toLocaleLowerCase().includes(query))
    .sort((left, right) => timeZoneCity(left).localeCompare(timeZoneCity(right)));
  const placeholder = node('option', '', available.length ? 'Choose a city' : 'No cities found'); placeholder.value = '';
  choice.replaceChildren(placeholder, ...available.map((zone) => {
    const region = zone.includes('/') ? zone.slice(0, zone.lastIndexOf('/')).replaceAll('_', ' ') : 'Universal time';
    const option = node('option', '', `${timeZoneCity(zone)} · ${region}`); option.value = zone; return option;
  }));
  choice.value = available.includes(current) ? current : '';
}

function useTomorrowScheduleTime() {
  const timezone = document.getElementById('recurring-schedule-timezone')?.value.trim() || 'UTC';
  const startsAt = document.getElementById('recurring-schedule-starts-at');
  const status = document.getElementById('recurring-schedule-status');
  const value = tomorrowDateTimeLocal(timezone);
  if (!value) {
    if (status) status.textContent = 'Enter a valid time zone before using the shortcut.';
    document.getElementById('recurring-schedule-timezone')?.focus();
    return;
  }
  if (startsAt) startsAt.value = value;
  if (status) status.textContent = '';
  updateSchedulePreview();
}

function updateSchedulePreview() {
  const preview = document.getElementById('schedule-preview');
  if (!preview) return;
  const webhook = document.getElementById('recurring-schedule-target')?.value === 'webhook';
  const where = webhook ? document.getElementById('recurring-schedule-destination') : document.getElementById('recurring-schedule-workspace');
  const name = where?.selectedOptions?.[0]?.textContent;
  const type = document.getElementById('recurring-schedule-type')?.value;
  const time = document.getElementById('recurring-schedule-local-time')?.value || '09:00';
  let when = type === 'daily' ? `Every day at ${time}` : type === 'weekly' ? 'Every week' : type === 'custom' ? 'On your custom schedule' : 'One time';
  if (type === 'weekly') {
    const day = document.getElementById('recurring-schedule-weekday')?.selectedOptions?.[0]?.textContent || 'week';
    const at = document.getElementById('recurring-schedule-weekly-time')?.value || '09:00';
    when = `Every ${day} at ${at}`;
  } else if (type === 'once') {
    const date = document.getElementById('recurring-schedule-starts-at')?.value;
    if (date) { const parsed = new Date(date); when = Number.isFinite(parsed.getTime()) ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(parsed) : date; }
  }
  const zone = document.getElementById('recurring-schedule-timezone')?.value || 'UTC';
  if (type !== 'custom') when += ` · ${timeZoneCity(zone)}`;
  preview.textContent = where?.value ? `${when} · ${name}` : 'Choose where and when. We’ll take it from there.';
}

function syncWebhookDestinationActions() {
  const selectedId = document.getElementById('recurring-schedule-destination')?.value || '';
  const selected = webhookDestinations.find((item) => item.destination_id === selectedId);
  const toggle = document.getElementById('webhook-destination-toggle');
  const rotate = document.getElementById('webhook-destination-rotate');
  if (toggle) { toggle.disabled = !selected; toggle.textContent = selected?.enabled === false ? 'Enable connection' : 'Disable connection'; }
  if (rotate) rotate.disabled = !selected;
  const manage = document.getElementById('webhook-destination-manage');
  if (manage) manage.hidden = !selected;
  const name = document.getElementById('webhook-destination-name');
  if (name && name.dataset.destinationId !== selectedId) { name.value = selected?.name || ''; name.dataset.destinationId = selectedId; }
}

function renderWebhookDestinations() {
  const select = document.getElementById('recurring-schedule-destination');
  if (!select) return;
  const selected = select.value;
  const placeholder = node('option', '', 'Choose an app');
  placeholder.value = '';
  select.replaceChildren(placeholder, ...webhookDestinations.map((item) => {
    const option = node('option', '', `${item.name}${item.enabled === false ? ' · disabled' : ''}`);
    option.value = item.destination_id;
    return option;
  }));
  if (webhookDestinations.some((item) => item.destination_id === selected)) select.value = selected;
  syncWebhookDestinationActions();
}

async function loadWebhookDestinations() {
  const scope = scheduleRunScope();
  try {
    const response = await fetch(api.withAuth('/api/webhook-destinations'));
    if (!response.ok) throw new Error('Destinations are unavailable. Retry refresh.');
    const payload = await response.json();
    if (scope !== scheduleRunScope()) return;
    webhookDestinations = payload.items || [];
    renderWebhookDestinations();
    const status = document.getElementById('schedule-app-status'); if (status) status.textContent = '';
    const retry = document.getElementById('schedule-app-retry'); if (retry) retry.hidden = true;
  } catch (error) {
    if (scope !== scheduleRunScope()) return;
    for (const id of ['webhook-destination-status', 'schedule-app-status']) { const status = document.getElementById(id); if (status) status.textContent = error.message; }
    const retry = document.getElementById('schedule-app-retry'); if (retry) retry.hidden = false;
  }
}

async function manageWebhookDestination(action) {
  const scope = scheduleRunScope();
  const status = document.getElementById('webhook-destination-status');
  const secretOnce = document.getElementById('webhook-destination-secret-once');
  const saveButton = document.getElementById('webhook-destination-save');
  if (saveButton) saveButton.disabled = true;
  for (const id of ['webhook-destination-rotate', 'webhook-destination-toggle']) { const button = document.getElementById(id); if (button) button.disabled = true; }
  if (['create', 'rotate'].includes(action)) {
    if (secretOnce) { secretOnce.textContent = ''; secretOnce.hidden = true; }
    const panel = document.getElementById('webhook-destination-secret-panel'); if (panel) panel.hidden = true;
  }
  const selectedId = document.getElementById('recurring-schedule-destination')?.value || '';
  const selected = webhookDestinations.find((item) => item.destination_id === selectedId);
  try {
    let result;
    if (action === 'create') {
      const name = document.getElementById('webhook-destination-name')?.value.trim() || '';
      const url = document.getElementById('webhook-destination-url')?.value.trim() || '';
      const secret = document.getElementById('webhook-destination-secret')?.value || null;
      const headers = JSON.parse(document.getElementById('webhook-destination-headers')?.value || '{}');
      if (!name) { document.getElementById('webhook-destination-name')?.focus(); throw new Error('Enter an app name.'); }
      if (!url) { document.getElementById('webhook-destination-url')?.focus(); throw new Error('Enter the app’s receiving address.'); }
      if (!headers || typeof headers !== 'object' || Array.isArray(headers)) throw new Error('Headers must be a JSON object.');
      const payload = { name, url, secret, headers, payload_mode: document.getElementById('webhook-destination-mode')?.value || 'envelope' };
      const exactRequest = JSON.stringify(payload);
      if (pendingDestinationCreate?.request !== exactRequest) pendingDestinationCreate = { request: exactRequest, key: globalThis.crypto?.randomUUID?.() || `destination-${Date.now()}-${Math.random().toString(16).slice(2)}` };
      result = await api.postJson('/api/webhook-destinations', { ...payload, idempotency_key: pendingDestinationCreate.key });
      pendingDestinationCreate = null;
    } else if (action === 'rename') {
      if (!selected) throw new Error('Choose an app first.');
      const name = document.getElementById('webhook-destination-name')?.value.trim() || '';
      if (!name) throw new Error('Enter an app name.');
      const payload = { name };
      const headers = document.getElementById('webhook-destination-headers')?.value.trim() || '';
      if (headers) payload.headers = parseRequestData(headers);
      result = await api.patchJson(`/api/webhook-destinations/${encodeURIComponent(selectedId)}`, payload);
    } else {
      if (!selected) throw new Error('Choose an app first.');
      const secret = document.getElementById('webhook-destination-secret')?.value || '';
      if (action === 'rotate' && !secret) throw new Error('Enter a new signing key in Advanced connection settings first.');
      result = await api.patchJson(`/api/webhook-destinations/${encodeURIComponent(selectedId)}`, action === 'rotate' ? { secret } : { enabled: selected.enabled === false });
    }
    if (scope !== scheduleRunScope()) return;
    for (const id of ['webhook-destination-secret', 'webhook-destination-headers', 'webhook-destination-url']) {
      const field = document.getElementById(id); if (field) field.value = '';
    }
    if (result.secret_once && secretOnce) { secretOnce.textContent = result.secret_once; secretOnce.hidden = false; document.getElementById('webhook-destination-secret-panel').hidden = false; }
    await loadWebhookDestinations();
    const destination = document.getElementById('recurring-schedule-destination');
    if (destination && result.destination_id) destination.value = result.destination_id;
    syncWebhookDestinationActions();
    if (status) status.textContent = action === 'create' ? 'Connected. You can now choose this app in a schedule.' : 'Connection updated.';
    const done = document.getElementById('webhook-destination-done'); if (done) done.textContent = 'Done';
    if (action === 'create') {
      const form = document.getElementById('webhook-destination-form'); if (form) form.dataset.mode = 'manage';
      if (saveButton) saveButton.textContent = 'Save changes';
      document.getElementById('webhook-destination-title').textContent = 'App connected';
      document.getElementById('webhook-destination-url-field').hidden = true;
      document.getElementById('webhook-destination-existing-note').hidden = false;
      document.getElementById('webhook-destination-mode').disabled = true;
      for (const id of ['webhook-destination-rotate', 'webhook-destination-toggle']) document.getElementById(id).hidden = false;
    }
    updateSchedulePreview();
  } catch (error) {
    if (status) status.textContent = error instanceof SyntaxError ? 'Check the authentication JSON in Advanced connection settings.' : error.message;
  } finally { if (saveButton) saveButton.disabled = false; syncWebhookDestinationActions(); }
}

async function loadSchedules() {
  scheduleCatalogReady = false;
  try {
    const response = await fetch(api.withAuth('/api/recurring-schedules?include_inactive=true'));
    if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load recurring schedules'));
    recurringSchedules = parseScheduleJson(await response.text());
    scheduleCatalogReady = true;
    scheduleLoadError = '';
    scheduleOccurrences.clear();
    for (const definitionId of scheduleHistoryGeneration.keys()) {
      scheduleHistoryGeneration.set(definitionId, scheduleHistoryGeneration.get(definitionId) + 1);
    }
  } catch (error) {
    scheduleLoadError = error.message;
  }
  await loadWebhookDestinations();
  renderSchedules();
  resumePendingScheduleRefreshes();
}

function syncRecurringScheduleFields() {
  const webhook = document.getElementById('recurring-schedule-target')?.value === 'webhook';
  for (const option of document.querySelectorAll('[name="schedule-target-choice"]')) option.checked = option.value === (webhook ? 'webhook' : 'worker');
  const webhookFields = document.getElementById('recurring-schedule-webhook-fields');
  const workspaceField = document.getElementById('recurring-schedule-workspace-field');
  const instructionField = document.getElementById('recurring-schedule-instruction-field');
  if (webhookFields) webhookFields.hidden = !webhook;
  if (workspaceField) workspaceField.hidden = webhook;
  if (instructionField) instructionField.hidden = webhook;
  renderScheduleWorkspaceOptions();
  syncWebhookDestinationActions();
  const selectedKind = document.getElementById('recurring-schedule-type')?.value || 'daily';
  const customKind = document.getElementById('recurring-schedule-custom-type')?.value || 'interval';
  const kind = selectedKind === 'custom' ? customKind : selectedKind;
  const dstPolicy = document.getElementById('recurring-schedule-dst-policy');
  const dstPolicyField = document.getElementById('recurring-schedule-dst-policy-field');
  const dstState = scheduleDstPolicyState(kind, dstPolicy?.value);
  if (dstPolicyField) dstPolicyField.hidden = !dstState.applicable;
  if (dstPolicy) { dstPolicy.disabled = !dstState.applicable; dstPolicy.value = dstState.value; }
  const intervalField = document.getElementById('recurring-schedule-interval-field');
  const dailyField = document.getElementById('recurring-schedule-daily-field');
  const onceField = document.getElementById('recurring-schedule-once-field');
  const weeklyField = document.getElementById('recurring-schedule-weekly-field');
  const timezoneField = document.getElementById('recurring-schedule-timezone-field');
  const customTypeField = document.getElementById('recurring-schedule-custom-type-field');
  const cronField = document.getElementById('recurring-schedule-cron-field');
  const rruleField = document.getElementById('recurring-schedule-rrule-field');
  const startsAt = document.getElementById('recurring-schedule-starts-at');
  const startsAtLabel = document.getElementById('recurring-schedule-start-label');
  const catchUpPolicy = document.getElementById('recurring-schedule-catch-up-policy')?.value || 'skip';
  const catchUpLimitField = document.getElementById('recurring-schedule-catch-up-limit-field');
  const advanced = document.getElementById('recurring-schedule-advanced');
  if (intervalField) intervalField.hidden = kind !== 'interval';
  if (dailyField) dailyField.hidden = kind !== 'daily';
  if (onceField) onceField.hidden = kind !== 'once';
  if (weeklyField) weeklyField.hidden = kind !== 'weekly';
  if (timezoneField) timezoneField.hidden = kind === 'interval';
  if (customTypeField) customTypeField.hidden = selectedKind !== 'custom';
  if (cronField) cronField.hidden = kind !== 'cron';
  if (rruleField) rruleField.hidden = kind !== 'rfc5545';
  if (startsAt) startsAt.required = false;
  if (startsAtLabel) startsAtLabel.textContent = 'Choose date and time';
  if (catchUpLimitField) catchUpLimitField.hidden = catchUpPolicy !== 'bounded';
  // The workspace guard owns whether this whole section is available.
  if (advanced) advanced.hidden = !webhook && !(workspaceCatalog.items || []).length && !(oneOffWorkspaceCatalog.items || []).length && !editingScheduleId;
  syncTimeZoneSummary();
  updateSchedulePreview();
}

function readRecurringScheduleFormState() {
  const value = (id) => document.getElementById(id)?.value || '';
  return {
    targetKind: value('recurring-schedule-target'),
    destination: value('recurring-schedule-destination'),
    requestData: value('recurring-schedule-request-data'),
    workerId: value('recurring-schedule-workspace'),
    instruction: effectiveScheduleInstruction(),
    type: value('recurring-schedule-type'),
    customType: value('recurring-schedule-custom-type'),
    intervalValue: value('recurring-schedule-interval-value'),
    intervalUnit: value('recurring-schedule-interval-unit'),
    localTime: value('recurring-schedule-local-time'),
    timezone: value('recurring-schedule-timezone'),
    startsAt: value('recurring-schedule-starts-at'),
    weeklyDay: value('recurring-schedule-weekday'),
    weeklyTime: value('recurring-schedule-weekly-time'),
    endsAt: value('recurring-schedule-ends-at'),
    cron: value('recurring-schedule-cron'),
    rrule: value('recurring-schedule-rrule'),
    dstPolicy: value('recurring-schedule-dst-policy'),
    enabled: Boolean(document.getElementById('recurring-schedule-enabled')?.checked),
    overlapPolicy: value('recurring-schedule-overlap-policy'),
    misfireGrace: value('recurring-schedule-misfire-grace'),
    catchUpPolicy: value('recurring-schedule-catch-up-policy'),
    catchUpLimit: value('recurring-schedule-catch-up-limit'),
    jitter: value('recurring-schedule-jitter'),
  };
}

async function submitRecurringSchedule(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById('recurring-schedule-status');
  const workerId = document.getElementById('recurring-schedule-workspace')?.value || '';
  const oneOff = (oneOffWorkspaceCatalog.items || []).find((item) => String(item.worker_id || '') === workerId);
  const instruction = effectiveScheduleInstruction();
  const selectedType = document.getElementById('recurring-schedule-type')?.value || 'daily';
  const selectedCustomType = document.getElementById('recurring-schedule-custom-type')?.value || 'interval';
  const selectedRecurrenceType = selectedType === 'custom' ? selectedCustomType : selectedType;
  const webhook = document.getElementById('recurring-schedule-target')?.value === 'webhook';
  const destinationId = document.getElementById('recurring-schedule-destination')?.value || '';
  let requestData = {};
  if (webhook) {
    try { requestData = collectRequestData(); }
    catch (error) { if (status) { status.textContent = error instanceof SyntaxError ? 'Check the JSON in Extra app details.' : error.message; status.dataset.tone = 'error'; } document.getElementById('schedule-request-advanced').open = true; document.getElementById('schedule-data-json').open = true; document.getElementById('recurring-schedule-request-data')?.focus(); return; }
    if (!requestData || typeof requestData !== 'object' || Array.isArray(requestData)) { if (status) status.textContent = 'Request data must be a JSON object.'; return; }
    if (!destinationId) { showScheduleFieldError(new Error('destination_id missing'), status); return; }
  }
  if (!webhook && !workerId) {
    if (status) { status.textContent = 'Choose a workspace first.'; status.dataset.tone = 'error'; }
    document.getElementById('recurring-schedule-workspace')?.focus();
    return;
  }
  if (!webhook && !instruction) {
    if (status) { status.textContent = 'Describe what the workspace should do.'; status.dataset.tone = 'error'; }
    document.getElementById('recurring-schedule-instruction')?.focus();
    return;
  }
  const intervalValue = Number(document.getElementById('recurring-schedule-interval-value')?.value || 1);
  const intervalUnit = document.getElementById('recurring-schedule-interval-unit')?.value || 'hours';
  const rawIntervalSeconds = Math.round(intervalValue * (
    intervalUnit === 'weeks' ? 604800 : (intervalUnit === 'days' ? 86400 : (intervalUnit === 'hours' ? 3600 : intervalUnit === 'minutes' ? 60 : 1))
  ));
  const localTime = document.getElementById('recurring-schedule-local-time')?.value || '09:00';
  const weeklyDay = document.getElementById('recurring-schedule-weekday')?.value || 'MO';
  const weeklyTime = document.getElementById('recurring-schedule-weekly-time')?.value || '09:00';
  const selectedTimezone = document.getElementById('recurring-schedule-timezone')?.value.trim() || 'UTC';
  let startsAt = document.getElementById('recurring-schedule-starts-at')?.value || '';
  const endsAt = document.getElementById('recurring-schedule-ends-at')?.value || '';
  const cronExpression = document.getElementById('recurring-schedule-cron')?.value.trim() || '';
  const selectedRrule = document.getElementById('recurring-schedule-rrule')?.value.trim() || '';
  if (selectedRecurrenceType === 'weekly') {
    const unchangedWeeklyAnchor = editingScheduleId
      && editingScheduleSnapshot?.initial.type === 'weekly'
      && editingScheduleSnapshot.initial.timezone === selectedTimezone
      && form.dataset.scheduleWeekday === weeklyDay
      && form.dataset.scheduleWeeklyTime === weeklyTime;
    if (!unchangedWeeklyAnchor) {
      startsAt = weeklyStartDateTimeLocal(weeklyDay, weeklyTime, selectedTimezone);
    }
    if (!startsAt) {
      if (status) status.textContent = 'Choose a weekday, time, and valid timezone for this weekly schedule.';
      document.getElementById('recurring-schedule-weekday')?.focus();
      return;
    }
  }
  if (selectedRecurrenceType === 'once' && !startsAt) {
    if (status) { status.textContent = 'Choose when this schedule should start.'; status.dataset.tone = 'error'; }
    document.getElementById('recurring-schedule-starts-at')?.focus();
    return;
  }
  if (selectedRecurrenceType === 'interval' && rawIntervalSeconds < 60) { showScheduleFieldError(new Error('interval_seconds too short'), status); return; }
  const {
    recurrenceType,
    intervalSeconds,
    timezoneName,
    rrule,
  } = recurrenceSubmissionPolicy(selectedRecurrenceType, {
    intervalSeconds: rawIntervalSeconds,
    timezoneName: selectedTimezone,
    rrule: selectedRrule,
  });
  if (recurrenceType === 'cron' && !cronExpression) {
    if (status) status.textContent = 'Enter a structured cron expression.';
    document.getElementById('recurring-schedule-cron')?.focus();
    return;
  }
  if (recurrenceType === 'rfc5545' && !rrule) {
    if (status) status.textContent = 'Enter an RFC 5545 recurrence rule.';
    document.getElementById('recurring-schedule-rrule')?.focus();
    return;
  }
  const scheduleText = recurrenceLabel({
    recurrence_type: selectedRecurrenceType,
    interval_seconds: intervalSeconds,
    local_time: localTime,
    timezone_name: timezoneName,
    cron_expression: cronExpression,
    rrule,
    next_run_at: startsAt,
  });
  button.disabled = true;
  if (status) status.dataset.tone = 'info';
  if (status) status.textContent = editingScheduleId ? 'Saving changes…' : oneOff ? 'Saving workspace and schedule…' : 'Creating schedule…';
  let savedOneOff = false;
  let savedDefinition = null;
  try {
    const payload = {
      instruction,
      recurrence_type: recurrenceType,
      interval_seconds: intervalSeconds,
      local_time: recurrenceType === 'daily' ? localTime : '',
      timezone_name: timezoneName,
      dst_policy: document.getElementById('recurring-schedule-dst-policy')?.value || 'next_valid_earliest',
      cron_expression: recurrenceType === 'cron' ? cronExpression : '',
      rrule: recurrenceType === 'rfc5545' ? rrule : '',
      starts_at: startsAt || (editingScheduleId ? '' : null),
      ends_at: endsAt || (editingScheduleId ? '' : null),
      enabled: Boolean(document.getElementById('recurring-schedule-enabled')?.checked),
      overlap_policy: document.getElementById('recurring-schedule-overlap-policy')?.value || 'skip',
      misfire_grace_seconds: Math.round(Number(document.getElementById('recurring-schedule-misfire-grace')?.value || 0) * 60),
      catch_up_policy: document.getElementById('recurring-schedule-catch-up-policy')?.value || 'skip',
      max_catch_up_occurrences: Number(document.getElementById('recurring-schedule-catch-up-limit')?.value || 1),
      jitter_seconds: Number(document.getElementById('recurring-schedule-jitter')?.value || 0),
      schedule_text: scheduleText,
    };
    const target = webhook ? { kind: 'webhook', destination_id: destinationId, request_data: requestData } : { kind: 'worker', worker_id: workerId };
    if (editingScheduleId) {
      const updates = editingScheduleSnapshot
        ? scheduleEditUpdates({
          original: editingScheduleSnapshot.schedule,
          initial: editingScheduleSnapshot.initial,
          current: readRecurringScheduleFormState(),
          payload,
        })
        : payload;
      const oldTarget = editingScheduleSnapshot?.schedule.target || { kind: 'worker', worker_id: editingScheduleSnapshot?.schedule.worker_id };
      if (JSON.stringify(oldTarget) !== JSON.stringify(target)) updates.target = target;
      if (oldTarget.kind !== target.kind) updates.instruction = instruction;
      savedDefinition = await api.patchJson(`/api/recurring-schedules/${encodeURIComponent(editingScheduleId)}`, updates);
    } else {
      if (!webhook && oneOff) {
        await api.patchJson(`/api/workspace/${encodeURIComponent(workerId)}/metadata`, { workspace_kind: 'named' });
        oneOffWorkspaceCatalog.items = oneOffWorkspaceCatalog.items.filter((item) => String(item.worker_id || '') !== workerId);
        workspaceCatalog.items = [...(workspaceCatalog.items || []), { ...oneOff, workspace_kind: 'named' }];
        savedOneOff = true;
        renderScheduleWorkspaceOptions();
      }
      const exactRequest = JSON.stringify({ ...payload, target });
      if (pendingScheduleCreate?.request !== exactRequest) pendingScheduleCreate = { request: exactRequest, key: globalThis.crypto?.randomUUID?.() || `schedule-${Date.now()}-${Math.random().toString(16).slice(2)}` };
      savedDefinition = await api.postJson('/api/recurring-schedules', { ...payload, target, idempotency_key: pendingScheduleCreate.key });
    }
    const completedEdit = Boolean(editingScheduleId);
    const savedId = savedDefinition?.definition_id || editingScheduleId;
    resetScheduleEditor(completedEdit ? 'Schedule changes saved.' : 'Schedule created.');
    scheduleOccurrences.clear();
    await loadSchedules();
    const savedSchedule = (recurringSchedules.items || []).find((item) => item.definition_id === savedId);
    if (savedSchedule) { rememberScheduleAction(savedSchedule, completedEdit ? 'Changes saved.' : 'Schedule created.', '', 'success'); renderSchedules(); scrollToSchedule(savedSchedule.definition_id); refreshScheduleUntilSettled(savedSchedule.definition_id); }
  } catch (error) {
    showScheduleFieldError(error, status, savedOneOff);
  } finally {
    button.disabled = false;
  }
}

function renderSupportHint() {
  const addAccount = document.getElementById('add-provider-account');
  if (!addAccount?.open) {
    setProviderStatus('');
    return;
  }
  const provider = document.getElementById('provider-account-provider')?.value || 'codex';
  const method = document.getElementById('provider-account-method')?.value || 'subscription';
  const submit = document.querySelector('#provider-account-form button[type="submit"]');
  const option = providerOptions().find((item) => item.provider === provider);
  const supported = Boolean(option?.methods.includes(method));
  const keyField = document.getElementById('provider-api-key-field');
  const keyInput = document.getElementById('provider-api-key');
  const needsKey = method === 'api_key' && option?.native_api_key_support === 'supported';
  if (keyField) keyField.hidden = !needsKey;
  if (keyInput) { keyInput.required = needsKey; if (!needsKey) keyInput.value = ''; }
  const support = document.getElementById('provider-support-note');
  if (support) {
    support.textContent = supported
      ? (method === 'subscription' ? 'Sign in securely with your existing subscription.'
        : method === 'api_key' ? 'Paste a provider API key. It stays in this account’s private storage.'
          : 'Use your organization’s configured connection.')
      : providerUnavailableReason(option);
    support.dataset.state = supported ? 'available' : 'unavailable';
  }
  setProviderStatus(supported ? '' : providerUnavailableReason(option));
  if (submit) {
    submit.textContent = method === 'subscription' ? `Connect ${PROVIDER_LABELS[provider] || 'account'}` : 'Add account';
    submit.disabled = !supported;
  }
}

function providerOptions() {
  return (controlPlane?.provider_options || [])
    .map((option) => ({
      ...option,
      provider: String(option.provider || '').trim().toLowerCase(),
      methods: Array.isArray(option.methods)
        ? option.methods.map((method) => String(method || '').trim()).filter(Boolean)
          .filter(method => !accountConnectionIntent?.authMethods?.length || accountConnectionIntent.authMethods.includes(method))
        : [],
    }))
    .filter((option) => option.provider);
}

function availableProviderOptions() {
  return providerOptions().filter((option) => option.methods.length > 0);
}

function providerUnavailableReason(option) {
  if (controlPlane?.sections?.runtime_health === 'unavailable') {
    return 'AI setup status is unavailable. Refresh to try again.';
  }
  if (option?.provider === 'claude' && controlPlane?.current_native_claude?.available) {
    return 'Choose Use existing Claude sign-in to connect the account already on this computer.';
  }
  if (option?.subscription_support === 'setup_cli_required') {
    return `Install ${PROVIDER_LABELS[option.provider] || 'the native AI app'} on the computer running xPerfect, then refresh.`;
  }
  if (option?.subscription_support === 'unsupported_macos_host') {
    return 'Separate Claude subscription accounts are not available on this Mac. Use an existing Claude sign-in or an available API key connection.';
  }
  if (option?.subscription_support === 'proof_required') {
    return 'Codex subscription sign-in is not enabled on this server.';
  }
  return SUPPORT_COPY[option?.subscription_support]
    || 'This provider has no connection method available on this server.';
}

function renderProviderChoices(providers, providerSelect, methodSelect) {
  const choices = document.getElementById('provider-choice-list');
  if (choices) {
    providerSelect.closest('label')?.setAttribute('hidden', '');
    choices.replaceChildren(...providers.map((option) => {
      const choice = node('button', 'provider-choice');
      choice.type = 'button';
      choice.dataset.provider = option.provider;
      choice.setAttribute('aria-pressed', String(providerSelect.value === option.provider));
      const mark = node('img', 'provider-mark');
      const markProvider = { codex: 'codex', claude: 'claude', grok: 'grok', xai: 'grok' }[option.provider];
      if (markProvider) mark.src = `/static/provider-${markProvider}.svg?v=20261024vector1`;
      mark.dataset.provider = markProvider || '';
      mark.hidden = !markProvider;
      mark.alt = '';
      mark.width = 28;
      mark.height = 28;
      mark.addEventListener('error', () => { mark.hidden = true; }, { once: true });
      const copy = node('span', 'provider-choice-copy');
      const nativeClaude = option.provider === 'claude' && controlPlane?.current_native_claude?.available;
      copy.append(node('strong', '', option.label || PROVIDER_LABELS[option.provider] || option.provider),
        node('small', 'provider-choice-state', controlPlane?.sections?.runtime_health === 'unavailable' ? 'Status unavailable' : option.methods.length ? 'Connect account'
          : nativeClaude ? 'Use existing sign-in' : 'Setup needed'));
      choice.append(mark, copy);
      choice.addEventListener('click', () => {
        providerSelect.value = option.provider;
        renderProviderOptionControls();
        if (nativeClaude && !option.methods.length) document.querySelector('#existing-claude-connection button')?.focus();
        else Array.from(choices.children).find((item) => item.dataset.provider === option.provider)?.focus();
      });
      return choice;
    }));
  }
  const methods = document.getElementById('provider-method-choice');
  if (methods) {
    methodSelect.closest('label')?.setAttribute('hidden', '');
    const selected = providers.find((option) => option.provider === providerSelect.value);
    methods.hidden = !selected || selected.methods.length < 2;
    methods.replaceChildren(...(selected?.methods || []).map((method) => {
      const label = node('label', 'provider-method-option');
      const input = node('input');
      input.type = 'radio'; input.name = 'provider-connection-method'; input.value = method;
      input.checked = methodSelect.value === method;
      input.addEventListener('change', () => {
        if (!input.checked) return;
        methodSelect.value = method;
        renderSupportHint();
      });
      label.append(input, node('span', '', PROVIDER_METHOD_LABELS[method] || method.replaceAll('_', ' ')));
      return label;
    }));
  }
}

function renderProviderOptionControls() {
  const addAccount = document.getElementById('add-provider-account');
  const providerSelect = document.getElementById('provider-account-provider');
  const methodSelect = document.getElementById('provider-account-method');
  const defaultToggle = document.getElementById('provider-account-default');
  if (!addAccount || !providerSelect || !methodSelect) return;

  const providers = providerOptions();
  const accounts = controlPlane?.provider_accounts || [];
  const setupHelp = document.getElementById('provider-install-help');
  if (setupHelp && !providers.some((option) => option.methods.length)) setupHelp.open = true;
  const summary = addAccount.querySelector('summary');
  if (summary) summary.textContent = accounts.length ? 'Connect another account' : 'Connect account';
  const currentProvider = providerSelect.value;
  const currentMethod = methodSelect.value;
  providerSelect.replaceChildren(...providers.map((option) => {
    const element = document.createElement('option');
    element.value = option.provider;
    element.textContent = PROVIDER_LABELS[option.provider] || option.provider;
    return element;
  }));
  if (!providers.length) {
    methodSelect.replaceChildren();
    addAccount.hidden = true;
    setProviderStatus('');
    return;
  }

  providerSelect.value = providers.some((option) => option.provider === currentProvider)
    ? currentProvider
    : providers.find((option) => option.methods.length)?.provider || providers[0].provider;
  const selectedProvider = providers.find((option) => option.provider === providerSelect.value) || providers[0];
  methodSelect.replaceChildren(...selectedProvider.methods.map((method) => {
    const element = document.createElement('option');
    element.value = method;
    element.textContent = PROVIDER_METHOD_LABELS[method] || method.replaceAll('_', ' ');
    return element;
  }));
  methodSelect.value = selectedProvider.methods.includes(currentMethod) ? currentMethod : selectedProvider.methods[0] || '';
  renderProviderChoices(providers, providerSelect, methodSelect);
  const labelInput = document.getElementById('provider-account-label');
  if (labelInput) {
    const label = providerAccountLabelTransition({
      provider: providerSelect.value,
      value: labelInput.value,
      autoLabel: labelInput.dataset.autoLabel,
    });
    labelInput.value = label.value;
    labelInput.dataset.autoLabel = label.autoLabel;
  }
  addAccount.hidden = Boolean(activeSetupAccount);
  if (!accounts.length && !activeSetupAccount) addAccount.open = true;
  if (defaultToggle) {
    defaultToggle.checked = accounts.length === 0 && controlPlane?.sections?.provider_accounts !== 'unavailable';
  }
  renderSupportHint();
}

function showSetup(payload) {
  const panel = document.getElementById('provider-setup-panel');
  const instructions = document.getElementById('provider-setup-instructions');
  const setupGuidance = document.getElementById('provider-setup-guidance');
  const setupLink = document.getElementById('provider-setup-link');
  const setupCode = document.getElementById('provider-setup-code');
  const setupCodeRow = document.getElementById('provider-setup-code-row');
  const setupInputForm = document.getElementById('provider-setup-input-form');
  const setupInput = document.getElementById('provider-setup-input');
  const setupHelp = document.getElementById('provider-setup-help');
  const technical = document.getElementById('provider-setup-technical');
  const restart = document.getElementById('restart-provider-setup');
  const provider = String(payload.provider || 'codex');
  const setupUrl = String(payload.setup_url || '');
  const browserFlow = payload.setup_flow === 'browser';
  const code = String(payload.setup_code || '');
  const inputRequired = Boolean(payload.input_required) && !payload.complete;
  const inputSubmitted = Boolean(payload.input_submitted) && !payload.complete;
  const helpUrl = String(payload.help_url || '');
  const rawInstructions = String(payload.instructions || payload.message || '').trim();
  const waitingForGuidance = !payload.complete && !rawInstructions && !setupUrl && !inputSubmitted;
  const needsFallback = !payload.complete
    && !waitingForGuidance
    && !inputSubmitted
    && (!setupUrl || (provider === 'codex' && !code && !browserFlow));
  const accountId = String(payload.account_id || activeSetupAccount || '');
  const accountRow = [...document.querySelectorAll('.connection-row')]
    .find((candidate) => candidate.dataset.accountId === accountId);
  const accountChip = accountRow?.querySelector('.status-chip');
  const accountAction = accountRow?.querySelector('.connection-actions-primary > button');
  const accountMore = accountRow?.querySelector('.connection-more');
  const accountRecovery = accountRow?.querySelector('.connection-recovery');
  const addAccount = document.getElementById('add-provider-account');
  const externalClients = document.getElementById('connect-ai-advanced');
  if (instructions) instructions.textContent = rawInstructions
    || (inputSubmitted ? 'Finishing sign-in…' : 'Preparing sign-in…');
  if (panel) panel.hidden = Boolean(payload.complete);
  if (setupLink) {
    setupLink.href = setupUrl || '#';
    const providerName = provider === 'claude' ? 'Claude' : provider === 'codex' ? 'Codex' : 'provider';
    setupLink.textContent = `Open ${providerName} sign-in`;
    setupLink.hidden = !setupUrl;
  }
  if (setupCode) setupCode.textContent = code;
  if (setupCodeRow) setupCodeRow.hidden = !code;
  if (setupInputForm) setupInputForm.hidden = !inputRequired;
  if (payload.complete && setupInput) setupInput.value = '';
  if (setupHelp) {
    setupHelp.href = helpUrl || '#';
    setupHelp.textContent = 'Open ChatGPT security settings';
    setupHelp.hidden = browserFlow || !helpUrl;
  }
  if (setupGuidance) setupGuidance.textContent = needsFallback
    ? 'Use the provider instructions below.'
    : waitingForGuidance
      ? 'Starting secure sign-in…'
      : browserFlow
        ? 'Continue sign-in in your browser. Return here when you are done.'
        : (provider === 'codex'
        ? 'Open sign-in, then enter the code.'
        : inputRequired
          ? 'Open sign-in, then paste the code here.'
          : inputSubmitted
            ? 'Finishing secure sign-in…'
            : 'Open sign-in to continue.');
  if (technical && needsFallback) {
    technical.open = true;
    technical.dataset.autoOpened = 'true';
  } else if (technical?.dataset.autoOpened === 'true') {
    technical.open = false;
    delete technical.dataset.autoOpened;
  }
  if (!payload.complete && accountChip) {
    accountChip.textContent = 'connecting';
    accountChip.dataset.status = 'connecting';
  }
  if (!payload.complete && accountAction) accountAction.hidden = true;
  if (accountMore) accountMore.hidden = !payload.complete;
  if (accountRecovery) accountRecovery.hidden = !payload.complete;
  if (addAccount) addAccount.hidden = !payload.complete || availableProviderOptions().length === 0;
  if (externalClients) externalClients.hidden = !payload.complete;
  if (restart) restart.hidden = !activeSetupAccount || Boolean(payload.complete);
  setProviderStatus(payload.status === 'ready'
    ? 'Connected.'
    : payload.complete
      ? String(payload.message || 'Sign-in was not completed. Try again.')
      : (needsFallback ? 'Sign-in details changed. Follow the technical details below.' : ''));
}

async function submitProviderSetupInput(event) {
  event.preventDefault();
  if (!activeSetupAccount) return;
  const setupInput = document.getElementById('provider-setup-input');
  const button = document.getElementById('submit-provider-setup-input');
  const value = String(setupInput?.value || '').trim();
  if (!value) {
    setupInput?.focus();
    return;
  }
  button.disabled = true;
  button.textContent = 'Finishing…';
  setupInput.value = '';
  try {
    const payload = await api.postJson(
      `/api/provider-accounts/${encodeURIComponent(activeSetupAccount)}/setup/input`,
      { value },
    );
    showSetup(payload);
    if (payload.complete) {
      const accountId = activeSetupAccount;
      activeSetupAccount = '';
      stopSetupPolling();
      await loadControlPlane({ connectedAccount: payload.status === 'ready'
        ? { account_id: payload.account_id || accountId, profile: profileForProvider(payload.provider || controlPlane?.provider_accounts?.find(account => account.account_id === accountId)?.provider) } : null });
      showSetup(payload);
    } else if (!setupPollTimer) {
      setupPollTimer = window.setTimeout(pollSetup, 500);
    }
  } catch (error) {
    setProviderStatus(error.message);
    setupInput?.focus();
  } finally {
    button.disabled = false;
    button.textContent = 'Finish connecting';
  }
}

async function cancelActiveSetup({ reload = true } = {}) {
  if (!activeSetupAccount) return null;
  const accountId = activeSetupAccount;
  const payload = await api.postJson(`/api/provider-accounts/${encodeURIComponent(accountId)}/setup/cancel`);
  activeSetupAccount = '';
  stopSetupPolling();
  showSetup(payload);
  if (reload) await loadControlPlane();
  return { accountId, payload };
}

async function restartActiveSetup(button) {
  if (!activeSetupAccount) return;
  button.disabled = true;
  button.textContent = 'Restarting…';
  try {
    const cancelled = await cancelActiveSetup({ reload: false });
    if (!cancelled?.accountId) return;
    const payload = await api.postJson(`/api/provider-accounts/${encodeURIComponent(cancelled.accountId)}/setup`);
    activeSetupAccount = cancelled.accountId;
    showSetup(payload);
    setupPollTimer = window.setTimeout(pollSetup, 900);
  } catch (error) {
    await loadControlPlane().catch(() => {});
    setProviderStatus(error.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Restart sign-in';
  }
}

function stopSetupPolling() {
  if (setupPollTimer) window.clearTimeout(setupPollTimer);
  setupPollTimer = 0;
}

async function pollSetup() {
  if (!activeSetupAccount) return;
  try {
    const response = await fetch(api.withAuth(`/api/provider-accounts/${encodeURIComponent(activeSetupAccount)}/setup`));
    if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not check provider sign-in'));
    const payload = await response.json();
    showSetup(payload);
    if (payload.complete) {
      const accountId = activeSetupAccount;
      activeSetupAccount = '';
      stopSetupPolling();
      await loadControlPlane({ connectedAccount: payload.status === 'ready'
        ? { account_id: payload.account_id || accountId, profile: profileForProvider(payload.provider || '') } : null });
      showSetup(payload);
      return;
    }
  } catch (error) {
    setProviderStatus(error.message);
  }
  setupPollTimer = window.setTimeout(pollSetup, 1500);
}

async function loadWorkspaceChoices(kind = 'named,legacy') {
  const query = new URLSearchParams({ kind, limit: '100' });
  const response = await fetch(api.withAuth(`/api/workspaces?${query.toString()}`));
  if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load personal workspaces'));
  return response.json();
}

async function loadControlPlane({ connectedAccount = null } = {}) {
  const generation = ++controlPlaneGeneration;
  scheduleCatalogReady = false;
  // Start independent reads together. Their failure or speed cannot block account sign-in.
  const settled = (promise) => promise.then(value => ({ value }), error => ({ error }));
  const requests = {
    workspaces: settled(loadWorkspaceChoices()),
    oneOff: settled(loadWorkspaceChoices('ephemeral')),
    connectionHelp: settled(fetch(api.withAuth('/api/connect-ai'))),
    schedules: settled(fetch(api.withAuth('/api/recurring-schedules?include_inactive=true'))),
    models: settled(fetch(api.withAuth('/api/native-models/grok-build'))),
    resources: settled(fetch(api.withAuth('/api/control-plane?sections=resources'))),
  };
  const controlResponse = await fetch(api.withAuth('/api/control-plane?sections=accounts'));
  if (!controlResponse.ok) throw new Error(await api.responseMessage(controlResponse, 'Could not load connections'));
  const payload = await controlResponse.json();
  if (generation !== controlPlaneGeneration) return;
  const previousScope = scheduleRunScope();
  controlPlane = payload;
  if (previousScope && previousScope !== scheduleRunScope()) {
    workspaceCatalog = { items: [] };
    oneOffWorkspaceCatalog = { items: [] };
    recurringSchedules = { items: [] };
    webhookDestinations = [];
    grokModels = null;
    connectAi = null;
    pendingCapabilityReview = null;
    accountConnectionIntent = null;
    activeSetupAccount = '';
    stopSetupPolling();
    renderLibrary();
    renderLibraryRequestWorkspaceOptions();
    renderSchedules();
  }
  restoreUncertainScheduleRunKeys();
  renderProviderOptionControls();
  renderProviderAccounts();
  renderConnections();
  applyAccountConnectionIntent();
  window.dispatchEvent(new CustomEvent('glasshive:control-plane-updated', { detail: { connectedAccount } }));
  if (connectedAccount) announceConnectedAccount(connectedAccount);

  // Each section commits its own readback. Keep the account route usable even if another
  // section is slow or unavailable. A superseded refresh may not restore another owner's UI.
  const apply = (request, update, failure) => request.then(async (result) => {
    if (generation !== controlPlaneGeneration) return;
    await update(result);
  }).catch((error) => {
    if (generation === controlPlaneGeneration) failure?.(error);
  });
  controlPlaneSectionsReady = Promise.all([
    apply(requests.resources, async ({ value: response }) => {
      if (!response?.ok) throw new Error('Tools and Library are unavailable. Refresh to try again.');
      const resources = await response.json();
      if (generation !== controlPlaneGeneration) return;
      if (resources.me?.user_id !== controlPlane.me?.user_id || resources.me?.tenant_id !== controlPlane.me?.tenant_id) return;
      controlPlane.connections = resources.connections || [];
      controlPlane.library = resources.library || [];
      controlPlane.sections = { ...controlPlane.sections,
        connections: resources.sections?.connections || 'unavailable',
        library: resources.sections?.library || 'unavailable' };
      renderConnections();
      renderLibrary();
    }, () => {
      controlPlane.sections = { ...controlPlane.sections, connections: 'unavailable', library: 'unavailable' };
      renderConnections();
      renderLibrary();
    }),
    apply(requests.workspaces, async ({ value, error }) => {
      workspaceCatalog = error ? { items: [], unavailable: true } : value;
      restoreCapabilityReviewFromCatalog();
      renderLibrary();
      renderLibraryRequestWorkspaceOptions();
      renderSchedules();
      if (!error) await reconcileCapabilityReview(generation);
      if (generation === controlPlaneGeneration) renderCapabilityReviewBanner();
    }, () => {
      workspaceCatalog = { items: [], unavailable: true };
      renderLibrary();
      renderLibraryRequestWorkspaceOptions();
    }),
    apply(requests.oneOff, ({ value, error }) => {
      oneOffWorkspaceCatalog = error ? { items: [], unavailable: true } : value;
      renderSchedules();
    }, () => { oneOffWorkspaceCatalog = { items: [], unavailable: true }; renderSchedules(); }),
    apply(requests.connectionHelp, async ({ value: response }) => {
      if (response?.ok) {
        const value = await response.json();
        if (generation !== controlPlaneGeneration) return;
        connectAi = value;
        connectAiLoadError = '';
      } else {
        connectAi = { clients: {} };
        connectAiLoadError = 'External AI client setup is temporarily unavailable.';
      }
      renderConnectAi();
    }, () => {
      connectAi = { clients: {} };
      connectAiLoadError = 'External AI client setup is temporarily unavailable.';
      renderConnectAi();
    }),
    apply(requests.schedules, async ({ value: response, error }) => {
      if (response?.ok) {
        const value = parseScheduleJson(await response.text());
        if (generation !== controlPlaneGeneration) return;
        recurringSchedules = value;
        scheduleCatalogReady = true;
        scheduleLoadError = '';
        scheduleOccurrences.clear();
        for (const definitionId of scheduleHistoryGeneration.keys()) {
          scheduleHistoryGeneration.set(definitionId, scheduleHistoryGeneration.get(definitionId) + 1);
        }
      } else {
        const message = error ? String(error.message || 'Could not load schedules')
          : await api.responseMessage(response, 'Could not load schedules');
        if (generation !== controlPlaneGeneration) return;
        scheduleLoadError = message;
      }
      renderSchedules();
      resumePendingScheduleRefreshes();
    }, () => {
      scheduleLoadError = 'Schedules are unavailable. Refresh to try again.';
      renderSchedules();
    }),
    apply(requests.models, async ({ value: response }) => {
      const value = response?.ok ? await response.json() : {
        models: [], status: 'catalog_unavailable',
        message: 'Grok models are unavailable. Refresh Connections to try again.',
      };
      if (generation !== controlPlaneGeneration) return;
      grokModels = value;
      renderGrokModelChoice();
    }, () => {
      grokModels = { models: [], status: 'catalog_unavailable', message: 'Grok models are unavailable. Refresh Connections to try again.' };
      renderGrokModelChoice();
    }),
    loadWebhookDestinations(),
  ]).catch(() => {
    // Owning sections report their own request failures; account setup remains available.
  });
}

async function submitProviderAccount(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const provider = document.getElementById('provider-account-provider').value;
  const method = document.getElementById('provider-account-method').value;
  const option = (controlPlane?.provider_options || []).find((item) => item.provider === provider);
  if (
    !option?.methods?.includes(method)
  ) {
    setProviderStatus(SUPPORT_COPY[option?.subscription_support] || 'This account route is not available.');
    return;
  }
  button.disabled = true;
  setProviderStatus('Creating setup…');
  try {
    const payload = {
      provider: document.getElementById('provider-account-provider').value,
      auth_method: document.getElementById('provider-account-method').value,
      label: document.getElementById('provider-account-label').value.trim(),
      make_default: document.getElementById('provider-account-default').checked,
    };
    const created = await api.postJson('/api/provider-accounts', payload);
    let keyResult = null;
    if (method === 'api_key' && option?.native_api_key_support === 'supported') {
      const keyInput = document.getElementById('provider-api-key');
      const value = keyInput.value;
      keyInput.value = '';
      keyResult = await api.postJson(`/api/provider-accounts/${encodeURIComponent(created.account_id)}/credentials`, { value });
    }
    form.reset();
    const disclosure = document.getElementById('add-provider-account');
    if (disclosure) disclosure.open = false;
    if (created.platform_support === 'supported' && created.auth_method === 'subscription') {
      const setup = await api.postJson(`/api/provider-accounts/${encodeURIComponent(created.account_id)}/setup`);
      activeSetupAccount = String(created.account_id || '');
      showSetup(setup);
      stopSetupPolling();
      if (setup.complete) {
        activeSetupAccount = '';
        await loadControlPlane({ connectedAccount: setup.status === 'ready'
          ? { account_id: created.account_id, profile: profileForProvider(provider) } : null });
        showSetup(setup);
      } else {
        setupPollTimer = window.setTimeout(pollSetup, 900);
        await loadControlPlane();
      }
    } else {
      await loadControlPlane({ connectedAccount: (keyResult || created).status === 'ready'
        ? { account_id: created.account_id, profile: profileForProvider(provider) } : null });
      setProviderStatus(keyResult?.message || (created.auth_method !== 'subscription'
        ? 'Account connected. xPerfect will check model access when the job starts.'
        : (SUPPORT_COPY[created.platform_support]
          || (created.status === 'ready' ? 'Account ready.' : 'Complete sign-in before using this account.'))));
    }
  } catch (error) {
    setProviderStatus(error.message);
  } finally {
    button.disabled = false;
  }
}

export function renderActivity(events = [], availability = 'ready') {
  const list = document.getElementById('activity-list');
  if (!list) return;
  if (availability !== 'ready') {
    list.replaceChildren(emptyList('Activity is temporarily unavailable—refresh to retry'));
    return;
  }
  const ordered = [...events].sort((left, right) => String(right.created_at || right.last_activity_at || right.updated_at || '').localeCompare(String(left.created_at || left.last_activity_at || left.updated_at || '')));
  if (!ordered.length) {
    list.replaceChildren(emptyList('No workspace activity yet'));
    return;
  }
  list.replaceChildren(...ordered.slice(0, 40).map((event) => {
    const eventType = String(event.event_type || event.state || 'workspace.activity');
    const activityLabels = {
      'worker.created': 'Workspace created',
      'worker.ready': 'Workspace ready',
      'worker.metadata_updated': 'Workspace details updated',
      'worker.metadata.updated': 'Workspace details updated',
      'worker.duplicated': 'Workspace duplicated',
      'run.queued': 'Work queued',
      'run.started': 'Work started',
      'run.completed': 'Work completed',
      'run.failed': 'Work needs attention',
    };
    const fallbackLabel = eventType.replaceAll('.', ' ').replaceAll('_', ' ');
    const eventLabel = activityLabels[eventType] || fallbackLabel.charAt(0).toUpperCase() + fallbackLabel.slice(1);
    const conversationId = typeof event.conversation_id === 'string' ? event.conversation_id : '';
    const row = node('article', 'activity-row');
    const copy = node('div', 'connection-copy');
    copy.append(
      node('strong', '', conversationId ? 'Chat' : String(event.workspace_name || event.name || event.title || 'Workspace')),
      node('span', '', `${eventLabel} · ${formatDateTime(event.created_at || event.updated_at || event.last_activity_at)}`),
    );
    row.append(copy);
    if (conversationId || event.worker_id) {
      const open = node('a', 'quiet-button', conversationId ? 'Open chat' : 'Open workspace');
      open.href = conversationId ? `/conversation?id=${encodeURIComponent(conversationId)}`
        : `/watch/${encodeURIComponent(event.worker_id)}`;
      row.append(open);
    }
    return row;
  }));
}

export async function refreshControlPlane() {
  if (!api) return;
  await loadControlPlane();
  await controlPlaneSectionsReady;
}

export function initializeControlPlane(dependencies) {
window.addEventListener('xperfect:approved-library', event => {
  const details = document.getElementById('approved-library-details'); if (details) details.open = true;
  const id = String(event.detail?.libraryId || '');
  const card = [...document.querySelectorAll('[data-library-id]')].find(value => value.dataset.libraryId === id);
  card?.scrollIntoView({ block: 'center', behavior: 'smooth' }); card?.querySelector('select')?.focus();
});

  api = dependencies;
  window.addEventListener('xperfect:connect-account', (event) => {
    openAccountConnection(event.detail || {}).catch((error) => setProviderStatus(error.message));
  });
  const connectAiTabs = [
    document.getElementById('connect-ai-auto-tab'),
    document.getElementById('connect-ai-manual-tab'),
  ].filter(Boolean);
  connectAiTabs[0]?.addEventListener('click', () => setConnectAiMode('auto'));
  connectAiTabs[1]?.addEventListener('click', () => setConnectAiMode('manual'));
  connectAiTabs.forEach((tab, index) => tab.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const nextIndex = event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? connectAiTabs.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + connectAiTabs.length) % connectAiTabs.length;
    setConnectAiMode(nextIndex === 0 ? 'auto' : 'manual');
    connectAiTabs[nextIndex].focus();
  }));
  setConnectAiMode('auto');
  document.getElementById('library-request-form')?.addEventListener('submit', submitLibraryRequest);
  document.getElementById('library-start-job')?.addEventListener('click', () => {
    dependencies.setView('project');
    document.getElementById('description')?.focus();
  });
  document.getElementById('provider-account-form')?.addEventListener('submit', submitProviderAccount);
  document.getElementById('grok-native-model')?.addEventListener('change', () => {
    const save = document.getElementById('save-grok-native-model');
    if (save) save.disabled = !document.getElementById('grok-native-model')?.value;
  });
  document.getElementById('save-grok-native-model')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const selected = document.getElementById('grok-native-model')?.value || '';
    const status = document.getElementById('grok-model-status');
    if (!selected || !status) return;
    button.disabled = true;
    status.textContent = 'Saving model…';
    try {
      await api.patchJson('/api/preferences', { grok_model: selected });
      await loadControlPlane();
      status.textContent = `${selected} · Your choice. Ready for new work.`;
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
  document.getElementById('provider-account-provider')?.addEventListener('change', renderProviderOptionControls);
  document.getElementById('provider-account-method')?.addEventListener('change', renderSupportHint);
  document.getElementById('add-provider-account')?.addEventListener('toggle', (event) => {
    if (event.currentTarget.open) renderSupportHint();
    else if (document.getElementById('provider-setup-panel')?.hidden) setProviderStatus('');
  });
  document.getElementById('refresh-connections')?.addEventListener('click', () => loadControlPlane().catch((error) => {
    setProviderStatus(error.message);
  }));
  document.getElementById('new-schedule-action')?.addEventListener('click', () => {
    dependencies.setView('schedules');
    openScheduleEditor();
  });
  document.getElementById('recurring-schedule-form')?.addEventListener('submit', submitRecurringSchedule);
  document.getElementById('recurring-schedule-target')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('recurring-schedule-destination')?.addEventListener('change', syncWebhookDestinationActions);
  for (const option of document.querySelectorAll('[name="schedule-target-choice"]')) option.addEventListener('change', () => {
    document.getElementById('recurring-schedule-target').value = option.value;
    syncRecurringScheduleFields();
  });
  document.getElementById('schedule-app-retry')?.addEventListener('click', loadWebhookDestinations);
  document.getElementById('webhook-destination-copy-key')?.addEventListener('click', (event) => copyText(document.getElementById('webhook-destination-secret-once')?.textContent || '', event.currentTarget));
  document.getElementById('webhook-destination-connect')?.addEventListener('click', () => openDestinationDialog(false));
  document.getElementById('webhook-destination-manage')?.addEventListener('click', () => openDestinationDialog(true));
  for (const id of ['webhook-destination-close', 'webhook-destination-done']) document.getElementById(id)?.addEventListener('click', closeDestinationDialog);
  document.getElementById('webhook-destination-setup')?.addEventListener('close', clearDestinationSecrets);
  document.getElementById('webhook-destination-form')?.addEventListener('submit', (event) => { event.preventDefault(); void manageWebhookDestination(event.currentTarget.dataset.mode === 'manage' ? 'rename' : 'create'); });
  document.getElementById('recurring-schedule-message')?.addEventListener('input', syncRequestMessage);
  document.getElementById('schedule-data-add')?.addEventListener('click', () => {
    appendDataRow({ key: '', type: 'string', value: '' });
    document.querySelector('#schedule-data-fields .schedule-data-row:last-child input')?.focus();
  });
  document.getElementById('recurring-schedule-request-data')?.addEventListener('input', () => {
    try { const value = parseRequestData(document.getElementById('recurring-schedule-request-data').value); syncMessageField(value); renderDataRows(value); setDataStatus(''); }
    catch (_error) { setDataStatus('Check the JSON before saving.'); }
  });
  document.getElementById('webhook-destination-rotate')?.addEventListener('click', () => manageWebhookDestination('rotate'));
  document.getElementById('webhook-destination-toggle')?.addEventListener('click', () => manageWebhookDestination('toggle'));
  document.getElementById('recurring-schedule-cancel-edit')?.addEventListener('click', () => resetScheduleEditor('Edit cancelled.'));
  document.getElementById('recurring-schedule-create-workspace')?.addEventListener('click', () => {
    dependencies.setView('project');
    document.getElementById('description')?.focus();
  });
  document.getElementById('recurring-schedule-type')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('recurring-schedule-custom-type')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('recurring-schedule-catch-up-policy')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('recurring-schedule-tomorrow')?.addEventListener('click', useTomorrowScheduleTime);
  document.getElementById('schedule-timezone-change')?.addEventListener('click', () => {
    const picker = document.getElementById('schedule-timezone-picker'); picker.hidden = !picker.hidden;
    document.getElementById('schedule-timezone-change').setAttribute('aria-expanded', String(!picker.hidden));
    if (!picker.hidden) { renderTimeZoneChoices(); document.getElementById('schedule-timezone-search').focus(); }
  });
  document.getElementById('schedule-timezone-search')?.addEventListener('input', renderTimeZoneChoices);
  document.getElementById('schedule-timezone-choice')?.addEventListener('change', (event) => {
    if (!event.currentTarget.value) return;
    document.getElementById('recurring-schedule-timezone').value = event.currentTarget.value; syncTimeZoneSummary(); updateSchedulePreview();
  });
  document.getElementById('recurring-schedule-timezone')?.addEventListener('input', () => { syncTimeZoneSummary(); updateSchedulePreview(); });
  document.getElementById('recurring-schedule-form')?.addEventListener('input', updateSchedulePreview);
  document.getElementById('recurring-schedule-form')?.addEventListener('change', updateSchedulePreview);
  document.getElementById('refresh-schedules')?.addEventListener('click', () => loadSchedules());
  document.getElementById('copy-provider-setup-code')?.addEventListener('click', (event) => {
    const codeNode = document.getElementById('provider-setup-code');
    copyText(codeNode?.textContent || '', event.currentTarget, codeNode);
  });
  document.getElementById('provider-setup-input-form')?.addEventListener(
    'submit',
    submitProviderSetupInput,
  );
  document.getElementById('restart-provider-setup')?.addEventListener('click', (event) => {
    restartActiveSetup(event.currentTarget);
  });
  const timezone = document.getElementById('recurring-schedule-timezone');
  const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (timezone && browserTimezone) timezone.value = browserTimezone;
  setScheduleEditorVisible(false);
  syncRecurringScheduleFields();
  document.getElementById('cancel-provider-setup')?.addEventListener('click', async () => {
    if (!activeSetupAccount) return;
    try {
      await cancelActiveSetup();
    } catch (error) {
      setProviderStatus(error.message);
    }
  });
}
