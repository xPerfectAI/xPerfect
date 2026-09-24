import { attachExistingClaudeConnection } from './native-controls.js?v=20260922o03c';
import {
  recurrenceSubmissionPolicy,
  scheduleEditUpdates,
  scheduleEditorType,
  tomorrowDateTimeLocal,
  weekdayAtInstant,
  weeklyStartDateTimeLocal,
  zonedDateTimeLocalValue,
} from './schedule-policy.js?v=20260922schedule10';
import { equivalentReapprovalScopes } from './capability-review.js?v=20260811m';

const SUPPORT_COPY = {
  supported: 'Ready to connect',
  proof_required: 'Available when this deployment enables isolated Codex subscription homes',
  provider_permission_required: 'Requires an approved hosted Claude authentication agreement',
  setup_cli_required: 'Account setup is not installed on this xPerfect deployment',
  unsupported_macos_host: 'Claude subscription isolation is not available for multi-user macOS host workers',
  isolated_substrate_required: 'This multi-user deployment has not enabled a dedicated worker isolation substrate. Use the managed connected-accounts route below.',
  secret_store_required: 'Requires this deployment\'s secure secret store',
  managed_connection_required: 'Connect this account through the deployment\'s managed connected-accounts page.',
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
let pendingCapabilityReview = null;
const copyResetTimers = new WeakMap();

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
  return `${label} ${new Date(timestamp * 1000).toLocaleString()}`;
}

function observedDuration(value) {
  const seconds = Math.max(0, Number(value || 0));
  if (!Number.isFinite(seconds)) return 'duration unavailable';
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h worker time`;
  if (seconds >= 60) return `${(seconds / 60).toFixed(1)}m worker time`;
  return `${seconds.toFixed(seconds >= 10 ? 0 : 1)}s worker time`;
}

function providerAccountDetails(account) {
  const provider = String(account.provider || 'AI').replaceAll('_', ' ');
  const method = String(account.auth_method || 'account').replaceAll('_', ' ');
  return `${provider} · ${method}`;
}

function providerAccountUsage(account) {
  const details = [
    providerTimestamp('Verified', account.last_verified_at),
    providerTimestamp('Last used', account.last_used_at),
  ];
  const observedRuns = Number(account.observed_runs || 0);
  if (observedRuns > 0) {
    const observedFailures = Math.max(0, Number(account.observed_failures || 0));
    details.push(
      `Observed by xPerfect: ${observedRuns} run${observedRuns === 1 ? '' : 's'} · `
      + `${observedFailures} failed · ${observedDuration(account.observed_duration_seconds)}`,
    );
  }
  const hasReportedTokens = account.observed_input_tokens != null || account.observed_output_tokens != null;
  if (hasReportedTokens) {
    const inputTokens = Math.max(0, Number(account.observed_input_tokens || 0));
    const outputTokens = Math.max(0, Number(account.observed_output_tokens || 0));
    details.push(`Tokens reported by worker: ${inputTokens.toLocaleString()} input · ${outputTokens.toLocaleString()} output`);
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
    button.textContent = String(account.auth_method || '') === 'subscription' ? 'Reconnect' : 'Reconnect & test';
  }
}

async function verifyProviderAccount(account, button) {
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
    list.replaceChildren(node('p', 'connections-empty', 'No AI account connected yet.'));
    return;
  }
  const rows = accounts.map((account) => {
    const row = node('article', 'connection-row');
    row.dataset.accountId = String(account.account_id || '');
    row.classList.toggle('needs-review', reviewAccountIds.has(row.dataset.accountId));
    const copy = node('div', 'connection-copy');
    copy.append(
      node('strong', '', String(account.label || account.provider || 'Worker account')),
      node('span', '', providerAccountDetails(account)),
    );
    if (account.reconnect_reason && !['ready', 'connecting'].includes(String(account.status || ''))) {
      copy.append(node('span', 'connection-recovery', String(account.reconnect_reason)));
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
      check.type = 'button';
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
    const moreSummary = node('summary', '', 'More');
    moreSummary.setAttribute('aria-label', `More actions for ${String(account.label || account.provider || 'account')}`);
    const moreActions = node('div', 'connection-more-actions');
    const usage = providerAccountUsage(account);
    if (usage) moreActions.append(node('span', 'connection-more-note', usage));
    if (account.status === 'ready' && account.auth_source !== 'current_os_claude_subscription') {
      const test = node('button', 'text-button', 'Test connection');
      test.type = 'button';
      test.addEventListener('click', () => verifyProviderAccount(account, test));
      moreActions.append(test);
    }
    if (
      String(account.auth_method || '') === 'subscription'
      && account.auth_source !== 'current_os_claude_subscription'
      && subscriptionRouteAvailable(account)
      && String(account.status || '') === 'action_required'
      && String(account.recovery_code || '') === 'credential_cleanup_failed'
    ) {
      const signInAgain = node('button', 'text-button', 'Sign in again');
      signInAgain.type = 'button';
      signInAgain.addEventListener('click', () => reconnectProviderAccount(account, signInAgain));
      moreActions.append(signInAgain);
    }
    const remove = node('button', 'text-button danger-text-button', 'Remove');
    remove.type = 'button';
    remove.addEventListener('click', () => removeProviderAccount(account, remove));
    moreActions.append(remove);
    if (moreActions.childElementCount) {
      more.append(moreSummary, moreActions);
      actions.append(more);
    }
    row.append(copy, actions);
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
  const hasGrok = (controlPlane?.provider_options || []).some((option) => option.provider === 'grok');
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
      'Open Settings → MCP servers → Add server. Paste the xPerfect address, restart if prompted, then Authenticate.',
    ));
  }
  if (canSetup && clients.claude) {
    clientCards.push(connectAiClientCard(
      'Claude Code',
      'Use Terminal setup below, then run /mcp in Claude Code to finish sign-in.',
    ));
  }
  clientsList?.replaceChildren(
    ...(clientCards.length
      ? clientCards
      : [node('p', 'connect-login-note', String(connectAi.configuration_note || 'Setup is not available.'))]),
  );

  const rows = canSetup ? [
    ...(clients.codex ? [
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
    sessionStorage.removeItem(CAPABILITY_REVIEW_KEY);
  }
  return pendingCapabilityReview;
}

function persistCapabilityReview(review) {
  pendingCapabilityReview = review;
  if (!review?.workerId || !review?.items?.length) {
    sessionStorage.removeItem(CAPABILITY_REVIEW_KEY);
    pendingCapabilityReview = null;
    return;
  }
  sessionStorage.setItem(CAPABILITY_REVIEW_KEY, JSON.stringify({
    worker_id: review.workerId,
    count: review.items.length,
    items: review.items,
  }));
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

async function reconcileCapabilityReview() {
  const review = capabilityReviewRequest();
  if (!review?.items?.length) return;
  let workspace = (workspaceCatalog.items || []).find(
    (item) => String(item.worker_id || '') === review.workerId,
  );
  if (!workspace) {
    try {
      const response = await fetch(api.withAuth(`/api/workspaces/${encodeURIComponent(review.workerId)}`));
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

function openCapabilitySetup(item) {
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
  const summary = node('span', '', 'Nothing private was copied automatically. Choose each item below before running this workspace.');
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
      button.addEventListener('click', () => openCapabilitySetup(item));
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
  const items = controlPlane.library || [];
  const libraryById = new Map(items.map((libraryItem) => [String(libraryItem.library_id || ''), libraryItem]));
  const review = capabilityReviewRequest();
  const requiredLibraryIds = new Set(
    (review?.items || [])
      .filter((item) => item.kind === 'library')
      .map((item) => item.reference),
  );
  if (empty) empty.hidden = items.length > 0;
  const cards = items.map((item) => {
    const manifest = item.manifest || {};
    const activatable = manifest.activatable === true && item.activation_status === 'ready';
    const capabilityName = String(manifest.label || manifest.name || item.stable_id || 'Approved capability');
    const card = node('article', 'library-card');
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
    card.append(meta);
    const grant = node('div', 'library-grant');
    const select = node('select');
    select.setAttribute('aria-label', `Workspace for ${capabilityName}`);
    const placeholder = node(
      'option',
      '',
      workspaceCatalog.next_cursor ? 'Choose recent workspace · search Workspaces for more' : 'Choose workspace',
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
    const syncGrant = async () => {
      activeGrant = null;
      replacementGrant = null;
      if (!activatable || !select.value) {
        button.disabled = true;
        button.textContent = activatable ? 'Add to workspace' : 'Unavailable in this deployment';
        return;
      }
      button.disabled = true;
      button.textContent = 'Checking workspace…';
      try {
        const response = await fetch(api.withAuth(`/api/workspaces/${encodeURIComponent(select.value)}/capability-grants`));
        if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load workspace capabilities'));
        const payload = await response.json();
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
    grant.append(select, button);
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
  const placeholder = node('option', '', workspaces.length
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
  const status = document.getElementById('library-request-status');
  if (status && review) {
    status.textContent = `Workspace copied. Review ${review.count} capabilit${review.count === 1 ? 'y' : 'ies'} before running it.`;
  }
}

async function submitLibraryRequest(event) {
  event.preventDefault();
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
  try {
    await api.postJson(`/api/workspace/${encodeURIComponent(workspaceId)}/message`, {
      message: requestText,
    });
    if (status) status.textContent = 'Request sent. The connection review stays open until access is verified or you continue without it.';
    const workspace = (workspaceCatalog.items || []).find((item) => String(item.worker_id || '') === workspaceId);
    const watchUrl = String(workspace?.watch_url || api.withAuth(`/watch/${encodeURIComponent(workspaceId)}?surface=desktop`));
    window.location.assign(watchUrl);
  } catch (error) {
    if (status) status.textContent = error.message;
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
    return `${formatted} · ${zone}`;
  } catch (_error) {
    return `${formatDateTime(value)} · ${zone}`;
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
  return `Every ${value} seconds`;
}

function recurrenceLabel(schedule) {
  const kind = String(schedule.recurrence_type || 'interval');
  const timezone = String(schedule.timezone_name || 'UTC');
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
  return `${intervalLabel(schedule.interval_seconds)} · ${timezone}`;
}

function scheduleNextLabel(schedule) {
  const next = schedule.next_occurrence_at || schedule.next_run_at;
  if (!schedule.active) {
    return schedule.retired_at ? 'Retired · history retained' : 'Paused · no future occurrences';
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
  const needsWorkspace = !options.length && !editingScheduleId;
  for (const id of ['recurring-schedule-instruction-field', 'recurring-schedule-fields',
    'recurring-schedule-advanced', 'recurring-schedule-submit']) {
    const field = document.getElementById(id);
    if (field) field.hidden = needsWorkspace;
  }
}

function renderOccurrenceHistory(container, items, timezoneName = 'UTC') {
  if (!items.length) {
    container.replaceChildren(emptyList('No occurrence has run yet'));
    return;
  }
  container.replaceChildren(...items.map((occurrence) => {
    const row = node('div', 'schedule-history-row');
    const outcome = String(occurrence.outcome && occurrence.outcome !== 'pending'
      ? occurrence.outcome
      : (occurrence.state || 'pending'));
    row.append(
      node('span', '', formatScheduleDateTime(occurrence.scheduled_for, timezoneName)),
      statusChip(outcome),
    );
    const error = String(occurrence.last_error || '').trim();
    if (error) {
      const recovery = ['action_required', 'failed'].includes(outcome)
        ? 'Check Error details, fix the cause, then use Run now or resume this schedule.'
        : 'Open the workspace status for details before retrying.';
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
  container.replaceChildren(node('span', 'schedule-card-meta', 'Loading occurrence history…'));
  try {
    const response = await fetch(api.withAuth(`/api/recurring-schedules/${encodeURIComponent(definitionId)}/occurrences?limit=20`));
    if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load occurrence history'));
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

function rememberScheduleAction(schedule, message, destination = '') {
  scheduleActionMessages.set(String(schedule.definition_id || ''), { message, destination });
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
  if (!remembered?.message) return;
  status.append(document.createTextNode(remembered.message));
  if (remembered.destination) {
    const link = node('a', 'inline-link', 'Open Connections');
    link.href = remembered.destination;
    status.append(document.createTextNode(' '), link);
  }
}

async function deactivateSchedule(schedule, button, actionStatus) {
  const name = workspaceName(schedule.worker_id);
  if (!window.confirm(`Pause future occurrences for ${name}? You can resume it later and its history will be kept.`)) return;
  button.disabled = true;
  button.textContent = 'Pausing…';
  const status = scheduleActionStatus(actionStatus);
  try {
    await api.patchJson(`/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}`, { enabled: false });
    const message = 'Schedule paused. Its previous occurrences are still available.';
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
    const message = 'Schedule resumed with the same private workspace and history.';
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
  const form = document.getElementById('recurring-schedule-form');
  form?.reset();
  if (form) {
    delete form.dataset.scheduleWeekday;
    delete form.dataset.scheduleWeeklyTime;
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
  if (status) status.textContent = message;
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
  resetScheduleEditor('', { hide: false });
  renderScheduleWorkspaceOptions();
  const firstAction = document.getElementById('recurring-schedule-workspace')?.disabled
    ? 'recurring-schedule-create-workspace' : 'recurring-schedule-instruction';
  document.getElementById(firstAction)?.focus();
}

function editSchedule(schedule) {
  editingScheduleId = String(schedule.definition_id || '');
  setScheduleEditorVisible(true);
  const setValue = (id, value) => {
    const field = document.getElementById(id);
    if (field) field.value = value == null ? '' : String(value);
  };
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
  setValue('recurring-schedule-dst-policy', schedule.dst_policy || 'next_valid_earliest');
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
      : (intervalSeconds > 0 && intervalSeconds % 3600 === 0 ? 'hours' : 'seconds'));
  setValue('recurring-schedule-interval-unit', intervalUnit);
  const intervalFactor = intervalUnit === 'weeks'
    ? 604800
    : (intervalUnit === 'days' ? 86400 : (intervalUnit === 'hours' ? 3600 : 1));
  setValue('recurring-schedule-interval-value', intervalSeconds / intervalFactor);
  setValue('recurring-schedule-overlap-policy', schedule.overlap_policy || 'skip');
  setValue('recurring-schedule-misfire-grace', Number(schedule.misfire_grace_seconds || 0) / 60);
  setValue('recurring-schedule-catch-up-policy', schedule.catch_up_policy || 'skip');
  setValue('recurring-schedule-catch-up-limit', schedule.max_catch_up_occurrences || 1);
  setValue('recurring-schedule-jitter', schedule.jitter_seconds || 0);
  const enabled = document.getElementById('recurring-schedule-enabled');
  if (enabled) enabled.checked = Boolean(schedule.enabled);
  const workspace = document.getElementById('recurring-schedule-workspace');
  if (workspace) workspace.disabled = true;
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
  if (status) status.textContent = 'Editing this definition will not change its occurrence history.';
  syncRecurringScheduleFields();
  editingScheduleSnapshot = {
    schedule: { ...schedule },
    initial: readRecurringScheduleFormState(),
  };
  document.getElementById('schedule-create-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function runScheduleNow(schedule, button, actionStatus, historyContainer = null) {
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
      : 'Run requested. It will use the same private workspace and connected capabilities.';
    const recoveryDestination = String(result.action_url || '')
      || (result.recovery_route === 'connections' ? '/#connections' : '');
    rememberScheduleAction(schedule, message, recoveryDestination);
    if (status) status.textContent = message;
    await loadSchedules();
  } catch (error) {
    const message = `${error.message} We could not confirm whether it started. Check history before trying again.`;
    rememberScheduleAction(schedule, message);
    if (status) status.textContent = message;
    button.disabled = false;
    button.textContent = 'Confirm prior run';
    if (historyContainer && !historyContainer.hidden) {
      await loadOccurrenceHistory(definitionId, historyContainer);
    }
  }
}

async function retireSchedule(schedule, button, actionStatus) {
  const name = workspaceName(schedule.worker_id);
  if (!window.confirm(`Remove this schedule for ${name}? It cannot be resumed, but its occurrence history will be kept.`)) return;
  button.disabled = true;
  button.textContent = 'Removing…';
  const status = scheduleActionStatus(actionStatus);
  try {
    await api.deleteJson(`/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}`);
    if (editingScheduleId === schedule.definition_id) resetScheduleEditor();
    await loadSchedules();
    const pageStatus = document.getElementById('recurring-schedule-status');
    if (pageStatus) pageStatus.textContent = 'Schedule removed. Its occurrence history is still available.';
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
    node('span', '', 'Choose New schedule to give a workspace repeatable work.'),
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
  const schedules = [...(recurringSchedules.items || [])].sort((left, right) => {
    if (Boolean(left.active) !== Boolean(right.active)) return left.active ? -1 : 1;
    return String(left.next_run_at || '').localeCompare(String(right.next_run_at || ''));
  });
  const scheduleIds = new Set(schedules.map((schedule) => String(schedule.definition_id || '')));
  for (const definitionId of scheduleActionMessages.keys()) {
    if (!scheduleIds.has(definitionId)) scheduleActionMessages.delete(definitionId);
  }
  if (!scheduleLoadError) {
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
    const head = node('div', 'schedule-card-head');
    const copy = node('div', 'schedule-card-copy');
    copy.append(
      node('strong', '', String(schedule.instruction || 'Recurring workspace task')),
      node('span', '', workspaceName(schedule.worker_id)),
    );
    const retired = Boolean(schedule.retired_at);
    head.append(copy, statusChip(retired ? 'retired' : (schedule.active ? 'active' : 'paused')));
    const meta = node('div', 'schedule-card-meta');
    meta.append(
      node('span', '', recurrenceLabel(schedule)),
      node('span', '', scheduleNextLabel(schedule)),
    );
    if (schedule.last_occurrence_at) {
      meta.append(node('span', '', `Last occurrence: ${formatScheduleDateTime(schedule.last_occurrence_at, schedule.timezone_name || 'UTC')}`));
    }
    if (schedule.last_outcome || schedule.last_error) {
      meta.append(node(
        'span',
        'schedule-history-detail',
        `Latest result: ${String(schedule.last_outcome || 'needs attention').replaceAll('_', ' ')}${schedule.last_error ? ' · Details in history' : ''}`,
      ));
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
      const runNowButton = node('button', 'quiet-button', unconfirmedRun ? 'Confirm prior run' : 'Run now');
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
    } else if (!retired) {
      const resumeButton = node('button', 'quiet-button', 'Resume schedule');
      resumeButton.type = 'button';
      resumeButton.addEventListener('click', () => resumeSchedule(schedule, resumeButton, actionStatus));
      actions.append(resumeButton);
    }
    const more = node('details', 'schedule-card-more');
    const moreSummary = node('summary', 'quiet-button', 'More');
    moreSummary.setAttribute('aria-label', `More actions for ${workspaceName(schedule.worker_id)}`);
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
    actions.append(more);
    card.append(head, meta, actions, actionStatus, history);
    return card;
  });
  list.replaceChildren(...(loadError ? [loadError] : []), ...cards);
}

async function loadSchedules() {
  try {
    const response = await fetch(api.withAuth('/api/recurring-schedules?include_inactive=true'));
    if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load recurring schedules'));
    recurringSchedules = await response.json();
    scheduleLoadError = '';
    scheduleOccurrences.clear();
    for (const definitionId of scheduleHistoryGeneration.keys()) {
      scheduleHistoryGeneration.set(definitionId, scheduleHistoryGeneration.get(definitionId) + 1);
    }
  } catch (error) {
    scheduleLoadError = error.message;
  }
  renderSchedules();
}

function syncRecurringScheduleFields() {
  const selectedKind = document.getElementById('recurring-schedule-type')?.value || 'daily';
  const customKind = document.getElementById('recurring-schedule-custom-type')?.value || 'interval';
  const kind = selectedKind === 'custom' ? customKind : selectedKind;
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
  const advanced = document.querySelector('.schedule-advanced');
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
  if (advanced) advanced.hidden = false;
}

function readRecurringScheduleFormState() {
  const value = (id) => document.getElementById(id)?.value || '';
  return {
    instruction: value('recurring-schedule-instruction'),
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
  const instruction = document.getElementById('recurring-schedule-instruction')?.value.trim() || '';
  const selectedType = document.getElementById('recurring-schedule-type')?.value || 'daily';
  const selectedCustomType = document.getElementById('recurring-schedule-custom-type')?.value || 'interval';
  const selectedRecurrenceType = selectedType === 'custom' ? selectedCustomType : selectedType;
  if (!workerId) {
    if (status) status.textContent = 'Choose a workspace first.';
    document.getElementById('recurring-schedule-workspace')?.focus();
    return;
  }
  if (!instruction) {
    if (status) status.textContent = 'Describe what the workspace should do.';
    document.getElementById('recurring-schedule-instruction')?.focus();
    return;
  }
  const intervalValue = Number(document.getElementById('recurring-schedule-interval-value')?.value || 1);
  const intervalUnit = document.getElementById('recurring-schedule-interval-unit')?.value || 'hours';
  const rawIntervalSeconds = Math.round(intervalValue * (
    intervalUnit === 'weeks' ? 604800 : (intervalUnit === 'days' ? 86400 : (intervalUnit === 'hours' ? 3600 : 1))
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
    if (status) status.textContent = 'Choose when this schedule should start.';
    document.getElementById('recurring-schedule-starts-at')?.focus();
    return;
  }
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
  if (status) status.textContent = editingScheduleId ? 'Saving changes…' : oneOff ? 'Saving workspace and schedule…' : 'Creating schedule…';
  let savedOneOff = false;
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
    if (editingScheduleId) {
      const updates = editingScheduleSnapshot
        ? scheduleEditUpdates({
          original: editingScheduleSnapshot.schedule,
          initial: editingScheduleSnapshot.initial,
          current: readRecurringScheduleFormState(),
          payload,
        })
        : payload;
      await api.patchJson(`/api/recurring-schedules/${encodeURIComponent(editingScheduleId)}`, updates);
    } else {
      if (oneOff) {
        await api.patchJson(`/api/workspace/${encodeURIComponent(workerId)}/metadata`, { workspace_kind: 'named' });
        oneOffWorkspaceCatalog.items = oneOffWorkspaceCatalog.items.filter((item) => String(item.worker_id || '') !== workerId);
        workspaceCatalog.items = [...(workspaceCatalog.items || []), { ...oneOff, workspace_kind: 'named' }];
        savedOneOff = true;
        renderScheduleWorkspaceOptions();
      }
      await api.postJson(`/api/workspace/${encodeURIComponent(workerId)}/recurring-schedules`, payload);
    }
    const completedEdit = Boolean(editingScheduleId);
    resetScheduleEditor(completedEdit ? 'Schedule changes saved.' : 'Schedule created.');
    scheduleOccurrences.clear();
    await loadSchedules();
  } catch (error) {
    if (status) status.textContent = savedOneOff
      ? `Workspace saved. Schedule was not created: ${error.message}` : error.message;
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
  const option = availableProviderOptions().find((item) => item.provider === provider);
  const supported = Boolean(option?.methods.includes(method));
  const keyField = document.getElementById('provider-api-key-field');
  const keyInput = document.getElementById('provider-api-key');
  const needsKey = method === 'api_key' && option?.native_api_key_support === 'supported';
  if (keyField) keyField.hidden = !needsKey;
  if (keyInput) { keyInput.required = needsKey; if (!needsKey) keyInput.value = ''; }
  setProviderStatus(supported ? '' : 'This option is no longer available. Refresh and try again.');
  if (submit) {
    submit.textContent = method === 'subscription' ? `Connect ${PROVIDER_LABELS[provider] || 'account'}` : 'Add account';
    submit.disabled = !supported;
  }
}

function availableProviderOptions() {
  return (controlPlane?.provider_options || [])
    .map((option) => ({
      ...option,
      provider: String(option.provider || '').trim().toLowerCase(),
      methods: Array.isArray(option.methods)
        ? option.methods.map((method) => String(method || '').trim()).filter(Boolean)
        : [],
    }))
    .filter((option) => option.provider && option.methods.length > 0);
}

function renderProviderOptionControls() {
  const addAccount = document.getElementById('add-provider-account');
  const providerSelect = document.getElementById('provider-account-provider');
  const methodSelect = document.getElementById('provider-account-method');
  const defaultToggle = document.getElementById('provider-account-default');
  if (!addAccount || !providerSelect || !methodSelect) return;

  const providers = availableProviderOptions();
  const accounts = controlPlane?.provider_accounts || [];
  const setupHelp = document.getElementById('provider-install-help');
  if (setupHelp && !providers.length) setupHelp.open = true;
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
    : providers[0].provider;
  const selectedProvider = providers.find((option) => option.provider === providerSelect.value) || providers[0];
  methodSelect.replaceChildren(...selectedProvider.methods.map((method) => {
    const element = document.createElement('option');
    element.value = method;
    element.textContent = PROVIDER_METHOD_LABELS[method] || method.replaceAll('_', ' ');
    return element;
  }));
  methodSelect.value = selectedProvider.methods.includes(currentMethod) ? currentMethod : selectedProvider.methods[0];
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
    defaultToggle.checked = accounts.length === 0;
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
  const code = String(payload.setup_code || '');
  const inputRequired = Boolean(payload.input_required) && !payload.complete;
  const inputSubmitted = Boolean(payload.input_submitted) && !payload.complete;
  const helpUrl = String(payload.help_url || '');
  const rawInstructions = String(payload.instructions || payload.message || '').trim();
  const waitingForGuidance = !payload.complete && !rawInstructions && !inputSubmitted;
  const needsFallback = !payload.complete
    && !waitingForGuidance
    && !inputSubmitted
    && (!setupUrl || (provider === 'codex' && !code));
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
    setupHelp.hidden = !helpUrl;
  }
  if (setupGuidance) setupGuidance.textContent = needsFallback
    ? 'Use the provider instructions below.'
    : waitingForGuidance
      ? 'Starting secure sign-in…'
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
      activeSetupAccount = '';
      stopSetupPolling();
      await loadControlPlane();
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
      activeSetupAccount = '';
      stopSetupPolling();
      await loadControlPlane();
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
  const [controlResponse, connectResponse, workspacePayload, oneOffPayload, scheduleResponse, modelResponse] = await Promise.all([
    fetch(api.withAuth('/api/control-plane')),
    fetch(api.withAuth('/api/connect-ai')).catch(() => null),
    loadWorkspaceChoices(),
    loadWorkspaceChoices('ephemeral').catch(() => ({ items: [] })),
    fetch(api.withAuth('/api/recurring-schedules?include_inactive=true')).catch((error) => ({ error })),
    fetch(api.withAuth('/api/native-models/grok-build')).catch(() => null),
  ]);
  if (!controlResponse.ok) throw new Error(await api.responseMessage(controlResponse, 'Could not load connections'));
  controlPlane = await controlResponse.json();
  grokModels = modelResponse?.ok ? await modelResponse.json() : {
    models: [], status: 'catalog_unavailable', message: 'Grok models are unavailable. Refresh Connections after checking the native harness.',
  };
  restoreUncertainScheduleRunKeys();
  if (connectResponse?.ok) {
    connectAi = await connectResponse.json();
    connectAiLoadError = '';
  } else {
    connectAi = { clients: {} };
    connectAiLoadError = 'External AI client setup is temporarily unavailable.';
  }
  workspaceCatalog = workspacePayload;
  oneOffWorkspaceCatalog = oneOffPayload;
  restoreCapabilityReviewFromCatalog();
  if (scheduleResponse.ok) {
    recurringSchedules = await scheduleResponse.json();
    scheduleLoadError = '';
    scheduleOccurrences.clear();
    for (const definitionId of scheduleHistoryGeneration.keys()) {
      scheduleHistoryGeneration.set(definitionId, scheduleHistoryGeneration.get(definitionId) + 1);
    }
  } else {
    scheduleLoadError = scheduleResponse.error
      ? String(scheduleResponse.error.message || 'Could not load recurring schedules')
      : await api.responseMessage(scheduleResponse, 'Could not load recurring schedules');
  }
  await reconcileCapabilityReview();
  renderProviderOptionControls();
  renderProviderAccounts();
  renderGrokModelChoice();
  renderConnections();
  renderConnectAi();
  renderLibrary();
  renderLibraryRequestWorkspaceOptions();
  renderSchedules();
  renderCapabilityReviewBanner();
  window.dispatchEvent(new CustomEvent('glasshive:control-plane-updated', { detail: { connectedAccount } }));
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
    await loadControlPlane();
    if (created.platform_support === 'supported' && created.auth_method === 'subscription') {
      const setup = await api.postJson(`/api/provider-accounts/${encodeURIComponent(created.account_id)}/setup`);
      activeSetupAccount = String(created.account_id || '');
      showSetup(setup);
      stopSetupPolling();
      setupPollTimer = window.setTimeout(pollSetup, 900);
    } else {
      setProviderStatus(keyResult?.message || (created.auth_method !== 'subscription'
        ? 'Connected-account reference added. xPerfect will verify it when this workspace runs.'
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
    const eventLabel = activityLabels[eventType] || eventType.replaceAll('.', ' ').replaceAll('_', ' ');
    const row = node('article', 'activity-row');
    const copy = node('div', 'connection-copy');
    copy.append(
      node('strong', '', String(event.workspace_name || event.name || event.title || 'Workspace')),
      node('span', '', `${eventLabel} · ${formatDateTime(event.created_at || event.updated_at || event.last_activity_at)}`),
    );
    row.append(copy, statusChip(eventType));
    return row;
  }));
}

export async function refreshControlPlane() {
  if (!api) return;
  await loadControlPlane();
}

export function initializeControlPlane(dependencies) {
  api = dependencies;
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
  document.getElementById('recurring-schedule-cancel-edit')?.addEventListener('click', () => resetScheduleEditor('Edit cancelled.'));
  document.getElementById('recurring-schedule-create-workspace')?.addEventListener('click', () => {
    dependencies.setView('project');
    document.getElementById('description')?.focus();
  });
  document.getElementById('recurring-schedule-type')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('recurring-schedule-custom-type')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('recurring-schedule-catch-up-policy')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('recurring-schedule-tomorrow')?.addEventListener('click', () => {
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
  });
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
