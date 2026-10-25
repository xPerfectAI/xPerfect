import { createLibraryBrowser, libraryRequest, libraryStatus, libraryRemovalOutcome, reviewLibraryUse, askNativeLibrarySetup } from './personal-library.js?v=20261024polish1';
import { readAuthCookie } from './auth-cookies.js?v=20261024cookies1';
import { watchOutputModel, watchTimingModel } from './delivery-presenter.js?v=20261024polish1';
import { workspaceLifecycleControl } from './launch-policy.js?v=20261024polish1';
import { createFileDraft, createWorkspaceFiles } from './files.js?v=20261024polish1';
import { attachNativeControls, canGuideNativeTurn } from './native-controls.js?v=20261024polish1';
import { createResultMarkdown } from './result-markdown.js?v=20261025filelinks1';
import { usageSummaryText, failureSupportText } from './usage-presenter.js?v=20261025parity1';
import { renderAdmissionHolds } from './spend-controls.js?v=20261025parity1';

// The same safe rendering as conversation replies: raw HTML is escaped and unsafe link
// schemes are refused. Links open beside the workspace instead of replacing it.
const resultMarkdown = createResultMarkdown();

const params = new URLSearchParams(window.location.search);
const workerId = window.location.pathname.split('/').filter(Boolean).at(-1);
const projectId = params.get('project_id');
const signedToken = params.get('gh_token') || '';
const requestedSurface = params.get('surface') === 'desktop' ? 'desktop' : 'terminal';
const uiBase = `${window.location.protocol}//${window.location.host}`;
const runtimeBase = uiBase;
const isApplePlatform = /Mac|iPhone|iPad|iPod/i.test(
  navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || ''
);
const queueShortcutLabel = isApplePlatform ? '⌘+Enter' : 'Ctrl+Enter';
const LONG_PRESS_MS = 550;
const ACTIVE_REFRESH_MS = 2000;
const IDLE_REFRESH_MS = 10000;
const TERMINAL_ATTENTION_STATES = new Set(['failed', 'cancelled', 'interrupted']);
const STEERABLE_RUN_STATES = new Set(['queued', 'running', 'settling']);
const GLASSHIVE_UI_REV = '20260811m';
const workspaceApiBase = `/api/workspace/${workerId}`;

const frame = document.getElementById('desktop-frame');
const overlay = document.getElementById('stage-overlay');
const stage = document.querySelector('.watch-stage');
const watchApp = document.querySelector('.watch-app');
const terminalLog = document.getElementById('terminal-log');
const terminalLogSummary = document.getElementById('terminal-log-summary');
const overlayLabel = document.querySelector('#stage-overlay .overlay-label');
const overlayTitle = document.getElementById('overlay-title');
const overlayDetail = document.getElementById('overlay-detail');
const signInLink = document.getElementById('watch-sign-in');
const stageResultText = document.getElementById('stage-result-text');
let pauseReadAt = 0;
async function pauseRequest(url, method = 'GET', body) {
  const response = await fetch(withAuth(url), { method, credentials: 'same-origin',
    headers: method === 'GET' ? { 'Content-Type': 'application/json' } : csrfHeaders({ 'Content-Type': 'application/json' }),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) throw new Error('Settings changed or are unavailable. Reload before resuming.');
  return response.json();
}
async function refreshWorkPauses(data, force = false) {
  const container = document.getElementById('watch-work-pauses');
  if (!container || data.can_manage_workspace !== true || signedToken) return;
  if (!force && Date.now() - pauseReadAt < 10000) return;
  pauseReadAt = Date.now();
  try {
    const result = await pauseRequest('/api/admission-holds');
    renderAdmissionHolds(container, (result.items || []).filter(item => item.scope_type === 'owner'
      || (item.scope_type === 'project' && item.scope_id === data.worker?.project_id)), pauseRequest, () => refreshWorkPauses(data, true));
  } catch { container.textContent = 'Work pause status is unavailable. Open workspace settings to reload.'; }
}

// A shown result is read from the top of the stage, not centred under empty space.
function showResult(text) {
  stageResultText.innerHTML = text ? resultMarkdown.render(text, { resultArtifacts: currentResultArtifacts }) : '';
  stageResultText.hidden = !text;
  overlay.dataset.result = String(Boolean(text));
}
const title = document.getElementById('watch-title');
const subtitle = document.getElementById('watch-subtitle');
const addWorkerButton = document.getElementById('watch-add-worker');
const askChatButton = document.getElementById('watch-ask-chat');
function syncAskChat(data) {
  if (!askChatButton) return;
  const worker = data.worker || {};
  askChatButton.hidden = data.can_manage_workspace !== true || Boolean(signedToken)
    || worker.workspace_kind === 'ephemeral' || Boolean(data.work_ref)
    || ['terminating', 'termination_failed', 'terminated'].includes(worker.state) || !worker.project_id;
  askChatButton.dataset.projectId = askChatButton.hidden ? '' : worker.project_id;
}
askChatButton?.addEventListener('click', () => {
  const targetProject = askChatButton.dataset.projectId;
  if (targetProject) window.location.assign(`/conversation?project_id=${encodeURIComponent(targetProject)}`);
});
addWorkerButton?.addEventListener('click', () => {
  const target = new URL('/', window.location.href);
  target.searchParams.set('add_worker', workerId);
  target.hash = 'workspaces';
  window.location.assign(target.href);
});
const latestOutputInline = document.getElementById('latest-output-inline');
const latestOutputFull = document.getElementById('latest-output-full');
const latestOutputHuman = document.getElementById('latest-output-human');
const latestOutputTechnical = document.getElementById('latest-output-technical');
const resultTechnical = document.getElementById('result-technical');
let actionFailure = null;
const resultActions = document.getElementById('result-actions');
const artifactList = document.getElementById('artifact-list');
const workspaceArtifacts = document.getElementById('workspace-artifacts');
const artifactCount = document.getElementById('artifact-count');
const runTime = document.getElementById('watch-run-time');
const runTimeLabel = document.getElementById('watch-time-label');
const runTimeValue = document.getElementById('watch-time-value');
const statusLabel = document.getElementById('status-label');
const statePill = document.getElementById('watch-state');
const menu = document.getElementById('more-menu');
const menuToggle = document.getElementById('menu-toggle');
const resultToggle = document.getElementById('result-toggle');
const resultToggleAction = document.getElementById('result-toggle-action');
const resultPanel = document.getElementById('result-panel');
const nativeControlsMount = document.getElementById('native-controls');
const resultPanelTitle = document.getElementById('result-panel-title');
const resultClose = document.getElementById('result-close');
const surfaceTerminalButton = document.getElementById('surface-terminal');
const surfaceDesktopButton = document.getElementById('surface-desktop');
const openExternal = document.getElementById('open-external');
const openTerminalLink = document.getElementById('open-terminal-link');
const openProjectWorkspace = document.getElementById('open-project-workspace');
const closeWorkspaceDialog = document.getElementById('watch-close-dialog');
const closeWorkspaceCancel = document.getElementById('watch-close-cancel');
const closeWorkspaceConfirm = document.getElementById('watch-close-confirm');
const steerForm = document.getElementById('steer-form');
const steerInput = document.getElementById('steer-input');
const sendButton = document.getElementById('send-button');
const runToggleButton = document.getElementById('run-toggle');
const guidancePrimary = document.getElementById('steer-guidance-primary');
const guidanceQueue = document.getElementById('steer-guidance-queue');
const filesToggle = document.getElementById('watch-files-toggle');
const filesPanel = document.getElementById('watch-files-panel');
const filesClose = document.getElementById('watch-files-close');
const filesInput = document.getElementById('watch-file-input');
const filesFolderInput = document.getElementById('watch-folder-input');
const filesDrop = document.getElementById('watch-file-drop');
const filesStatus = document.getElementById('watch-files-status');
const filesList = document.getElementById('watch-files-list');
const filesUploads = document.getElementById('watch-uploads-list');
const filesBudget = document.getElementById('watch-file-budget');
const filesHelp = document.getElementById('watch-upload-help');
const filesDirectory = document.getElementById('watch-files-directory');
const filesMore = document.getElementById('watch-files-more');
const filesFolder = document.getElementById('watch-new-folder');
const filesMove = document.getElementById('watch-files-move');
const filesExport = document.getElementById('watch-files-export');
const filesSelectionStatus = document.getElementById('watch-files-selection-status');
const filesSelectVisible = document.getElementById('watch-files-select-visible');
const filesClearSelection = document.getElementById('watch-files-clear-selection');
const filesTrashList = document.getElementById('watch-file-trash-list');
const filesTrashStatus = document.getElementById('watch-file-trash-status');
const filesAttach = document.getElementById('watch-attach-files');
const filesUploadArea = document.getElementById('watch-file-upload');
let filesCanWrite = null;
let libraryManager = false;
let libraryOwnerScope = '';
function currentLibraryOwnerScope() { return libraryManager ? libraryOwnerScope || watchDraft?.ownerScope() || '' : ''; }
let librarySnapshot = null;
function libraryTargetClosed() {
  return ['terminating', 'termination_failed', 'terminated'].includes(String(librarySnapshot?.worker?.close_state || librarySnapshot?.worker?.state || ''));
}
let libraryAddedEpoch = 0;
let requestedLibraryEntry = new URLSearchParams(window.location.search).get('library') === 'added';
let requestedLibraryPrepared = false;
let pendingLibraryTab = '';
let watchDraftScopeLoad = 0;
let watchDraftScopeController = null;
const fileError = (message) => { filesStatus.textContent = message; filesStatus.hidden = false; };
document.getElementById('watch-add-files').addEventListener('click', () => filesInput.click());
document.getElementById('watch-add-folder').addEventListener('click', () => {
  document.getElementById('watch-file-options').open = false;
  filesFolderInput.click();
});

let activeSurface = requestedSurface;
let currentDesktopUrl = withUiRev(withAuth(`${uiBase}/desktop/${workerId}`));
let currentTerminalUrl = withAuth(terminalViewUrl(runtimeBase, workerId, ''));
let lastAttachedUrl = '';
let attachStartedAt = 0;
let retryTimers = [];
let frameReady = false;
let currentRunState = '';
let currentDisplayState = 'starting';
let currentWorkerState = '';
let currentSummary = 'No run output yet.';
let currentFullOutput = 'No run output yet.';
let currentResultText = '';
let currentResultArtifacts = [];
let currentProjectTitle = projectId || 'Project';
let currentDeliverable = null;
let currentDesktopAvailable = false;
let currentDesktopCanStart = false;
let currentNoRunActivity = false;
let lastPromotedDeliverableKey = '';
let lastAttachedFilePreviewKey = '';
let currentFilePreviewKey = '';
let currentFilePreviewUrl = '';
let currentFileDownloadUrl = '';
let deliverablePromotionPending = false;
let queueModifierActive = false;
let longPressTimer = 0;
let longPressArmed = false;
let suppressNextClick = false;
let refreshTimer = 0;
let refreshInFlight = false;
let nativeControls = null;
let latestFileActivityKey = '';
let listedFileActivityKey = '';
let settledTerminalRun = '';
let terminalResultPreferred = false;

// Keyed on the latest run, its attempt and its phase: the terminal shows exactly that run,
// attaching its current attempt's session once it starts (again after a retry) and its saved
// output once it ends.
function terminalRunPhase(runState) {
  const state = String(runState || '').trim().toLowerCase();
  if (['completed', 'failed', 'cancelled', 'interrupted'].includes(state)) return 'ended';
  if (state === 'settling') return 'settling';
  if (['running', 'paused', 'needs_input'].includes(state)) return 'live';
  return 'waiting';
}

function terminalViewUrl(base, id, runId, runState = '', attemptId = '') {
  const url = `${base}/ui/workers/${id}/terminal`;
  if (!runId) return url;
  const attempt = attemptId ? `&attempt=${encodeURIComponent(attemptId)}` : '';
  return `${url}?run=${encodeURIComponent(runId)}${attempt}&phase=${terminalRunPhase(runState)}`;
}

// A failed, cancelled or interrupted run keeps its saved terminal output on screen below the
// attention notice, so the reason it stopped stays visible.
function showsSavedRunOutput(state) {
  return activeSurface === 'terminal' && TERMINAL_ATTENTION_STATES.has(state)
    && /[?&]run=/.test(currentTerminalUrl);
}

function withAuth(url) {
  if (!signedToken) return url;
  return `${url}${url.includes('?') ? '&' : '?'}gh_token=${encodeURIComponent(signedToken)}`;
}

function withUiRev(url) {
  const value = String(url || '');
  if (!value || /(?:^|[?&])gh_ui_rev=/.test(value)) return value;
  return `${value}${value.includes('?') ? '&' : '?'}gh_ui_rev=${encodeURIComponent(GLASSHIVE_UI_REV)}`;
}

function currentCsrfToken() {
  return readAuthCookie('glasshive_csrf');
}

function csrfHeaders(headers = {}) {
  const token = currentCsrfToken();
  return token ? { ...headers, 'X-GlassHive-CSRF': token } : headers;
}

function syncDocumentTitle(workerName, projectTitle, responseNeeded = false) {
  const safeProjectTitle = String(projectTitle || 'Workspace').trim() || 'Workspace';
  const safeWorkerName = String(workerName || 'Workspace').trim() || 'Workspace';
  // A background tab still shows that a native request is waiting on the owner.
  const prefix = responseNeeded ? 'Response needed · ' : '';
  document.title = `${prefix}xPerfect | ${safeProjectTitle} - ${safeWorkerName}`;
}

function ownerResponseNeeded(data) {
  const control = data?.native_control;
  return Boolean(control && control.read_only !== true
    && Array.isArray(control.pending_requests) && control.pending_requests.length);
}

// A terminal-only deployment leads with its existing human result when work ends.
// Native input stays visible during work or while the owner must answer a request.
function syncTerminalResultPresentation(data) {
  if (!terminalLog || !terminalLogSummary || !watchApp) return;
  const run = data?.latest_run || {};
  const runKey = `${String(run.run_id || '')}:${String(run.active_attempt_id || '')}`;
  const settled = Boolean(run.run_id) && terminalRunPhase(run.state) === 'ended'
    && activeSurface === 'terminal' && !currentDesktopAvailable && !ownerResponseNeeded(data)
    && Boolean(currentFullOutput.trim());
  if (!settled) {
    const wasPreferred = terminalResultPreferred;
    terminalResultPreferred = false;
    settledTerminalRun = '';
    watchApp.dataset.resultFirst = 'false';
    terminalLogSummary.hidden = true;
    if (!terminalLog.open) terminalLog.open = true;
    resultClose.hidden = false;
    if (wasPreferred && !actionFailure && !ownerResponseNeeded(data)) closeResultPanel();
    return;
  }
  if (settledTerminalRun !== runKey) {
    settledTerminalRun = runKey;
    terminalResultPreferred = true;
    if (terminalLog.open) terminalLog.open = false;
    openResultPanel();
  }
  watchApp.dataset.resultFirst = String(terminalResultPreferred);
  terminalLogSummary.hidden = !terminalResultPreferred;
  resultClose.hidden = terminalResultPreferred;
}

function displayStateForLive(data) {
  const workerState = String(data?.worker?.close_state || data?.worker?.state || '').trim().toLowerCase();
  const runState = String(data?.latest_run?.state || '').trim().toLowerCase();
  if (['terminating', 'termination_failed', 'terminated'].includes(workerState)) return workerState;
  if (runState === 'completed') return 'completed';
  if (['queued', 'running'].includes(runState)) return runState;
  if (['failed', 'cancelled', 'interrupted'].includes(runState)) return runState;
  if (['paused', 'idle', 'idle_terminated', 'stopped', 'ready'].includes(workerState)) {
    return workerState;
  }
  return workerState || 'starting';
}

function displayStateLabel(state) {
  const normalized = String(state || '').trim().toLowerCase();
  const activeLabels = { created: 'Starting', starting: 'Starting', resuming: 'Resuming', queued: 'Queued', running: 'Working', settling: 'Finishing', needs_input: 'Response needed', ready: 'Ready', paused: 'Paused', idle: 'Idle', stopped: 'Stopped' };
  if (activeLabels[normalized]) return activeLabels[normalized];
  if (normalized === 'completed') return 'Completed';
  if (normalized === 'failed') return 'Failed';
  if (normalized === 'interrupted') return 'Interrupted';
  if (normalized === 'cancelled') return 'Cancelled';
  if (normalized === 'idle_terminated') return 'Idle stopped';
  if (normalized === 'terminated') return 'Closed';
  if (normalized === 'terminating') return 'Closing';
  if (normalized === 'termination_failed') return 'Close needs attention';
  return normalized || 'starting';
}

function noRunActivityFor(data) {
  const runtime = data?.runtime_details || {};
  const state = String(data?.worker?.close_state || data?.worker?.state || '').trim().toLowerCase();
  return ['ready', 'paused', 'idle', 'idle_terminated', 'stopped'].includes(state)
    && data?.latest_run === null && data?.active_run === null
    && runtime.mode === 'host-computer' && !runtime.pid
    && !runtime.view_available && !runtime.view_url;
}

function clearRetryTimers() {
  for (const timer of retryTimers) window.clearTimeout(timer);
  retryTimers = [];
}

function closeMenu({ restoreFocus = false } = {}) {
  menu.hidden = true;
  menuToggle.setAttribute('aria-expanded', 'false');
  if (restoreFocus) menuToggle.focus();
}

function syncWatchLayout() {
  const ribbon = document.querySelector('.watch-ribbon');
  if (ribbon) watchApp.style.setProperty('--watch-header-height', `${ribbon.getBoundingClientRect().height}px`);
  const footer = document.querySelector('.watch-footer');
  if (footer) watchApp.style.setProperty('--watch-footer-height', `${footer.getBoundingClientRect().height}px`);
}

function watchMenuItems() {
  return Array.from(menu.querySelectorAll('button[role="menuitem"]'))
    .filter((button) => !button.hidden && !button.disabled && button.getClientRects().length);
}

function openMenu(last = false) {
  syncWatchLayout();
  menu.hidden = false;
  menuToggle.setAttribute('aria-expanded', 'true');
  const items = watchMenuItems();
  (last ? items.at(-1) : items[0])?.focus();
}


function closeResultPanel({ restoreFocus = false } = {}) {
  actionFailure = null;
  if (terminalResultPreferred) {
    terminalResultPreferred = false;
    watchApp.dataset.resultFirst = 'false';
    terminalLogSummary.hidden = true;
    if (!terminalLog.open) terminalLog.open = true;
    resultClose.hidden = false;
  }
  resultPanel.hidden = true;
  resultToggle.setAttribute('aria-expanded', 'false');
  const kind = latestOutputFull.hidden ? 'status' : 'result';
  resultToggle.setAttribute('aria-label', `Open latest workspace ${kind}`);
  if (resultToggleAction) resultToggleAction.textContent = `Open ${kind}`;
  if (restoreFocus) resultToggle.focus();
}

function openResultPanel() {
  if (!currentFullOutput.trim()) return;
  resultPanel.hidden = false;
  resultToggle.setAttribute('aria-expanded', 'true');
  const kind = latestOutputFull.hidden ? 'status' : 'result';
  resultToggle.setAttribute('aria-label', `Close latest workspace ${kind}`);
  if (resultToggleAction) resultToggleAction.textContent = `Close ${kind}`;
}

function forceReloadFrame(url) {
  frameReady = false;
  // Assigning src navigates even when the address is unchanged. Avoid a delayed blank
  // navigation that can race a newer surface or report a false successful load.
  frame.src = url;
}

function scheduleReconnects(url) {
  clearRetryTimers();
  for (const delay of activeSurface === 'terminal' ? [2500, 7000] : [3500, 9000]) {
    retryTimers.push(window.setTimeout(() => {
      if (lastAttachedUrl === url && !frameReady) {
        forceReloadFrame(url);
      }
    }, delay));
  }
}

function attachView(url, { force = false } = {}) {
  if (!url) return;
  // Chrome can defer an iframe's initial navigation inside a closed details element.
  // Start its load and recovery clock only after the native disclosure is visible.
  if (terminalLog && !terminalLog.open) return;
  if (!force && lastAttachedUrl === url && frame.src === url) return;
  lastAttachedUrl = url;
  lastAttachedFilePreviewKey = isFilePreviewUrl(url) ? currentFilePreviewKey : '';
  attachStartedAt = Date.now();
  frameReady = false;
  frame.src = url;
  if (!isFilePreviewUrl(url)) {
    scheduleReconnects(url);
  }
}

function filePreviewUrl() {
  return currentRunState === 'completed' && currentDeliverable?.kind === 'file'
    ? String(currentFilePreviewUrl || currentDeliverable.open_url || currentDeliverable.browser_url || '')
    : '';
}

function fileDeliverableKey(deliverable, runId) {
  if (!deliverable || deliverable.kind !== 'file') return '';
  const stablePath = String(deliverable.workspace_path || deliverable.label || '').trim();
  if (!stablePath) return '';
  return `${String(runId || '').trim()}:${stablePath}`;
}

function isFilePreviewUrl(url) {
  const previewUrl = filePreviewUrl();
  return Boolean(previewUrl) && String(url || '') === previewUrl;
}

function currentSurfaceUrl() {
  if (activeSurface === 'desktop') {
    return currentDesktopUrl || currentTerminalUrl;
  }
  return currentTerminalUrl || currentDesktopUrl;
}

function clearAttachedView() {
  clearRetryTimers();
  lastAttachedUrl = '';
  lastAttachedFilePreviewKey = '';
  attachStartedAt = 0;
  frameReady = false;
  if (frame.src !== 'about:blank') {
    frame.src = 'about:blank';
  }
}

function projectWorkspaceUrl() {
  return '/#workspaces';
}

function syncProjectWorkspaceLinks() {
  const available = Boolean(projectWorkspaceUrl());
  openProjectWorkspace.hidden = !available;
}

function syncSendAffordance() {
  if (steerInput.disabled) return;
  if (!STEERABLE_RUN_STATES.has(currentRunState)) {
    sendButton.dataset.mode = 'followup';
    sendButton.textContent = 'Send';
    sendButton.title = 'Send a follow-up to this workspace.';
    guidancePrimary.textContent = 'Send a follow-up';
    guidancePrimary.dataset.active = 'true';
    guidanceQueue.textContent = '';
    guidanceQueue.dataset.active = 'false';
    return;
  }
  const queueMode = queueModifierActive || longPressArmed;
  sendButton.dataset.mode = queueMode ? 'queue' : 'steer';
  sendButton.textContent = queueMode ? 'Queue' : 'Send';
  guidancePrimary.dataset.active = String(!queueMode);
  guidanceQueue.dataset.active = String(queueMode);
  guidanceQueue.dataset.mode = queueMode ? 'queue' : 'steer';
  if (longPressArmed) {
    guidanceQueue.textContent = 'Release to queue this follow-up without interrupting current work';
  } else if (queueModifierActive) {
    guidanceQueue.textContent = `Click Send or press ${queueShortcutLabel} to queue without interrupting current work`;
  } else {
    guidanceQueue.textContent = `Hold Send or ${queueShortcutLabel} to queue instead`;
  }
  sendButton.title = `Send redirects now. Hold Send or use ${queueShortcutLabel} to queue a follow-up without interrupting current work.`;
}

function syncRunToggle(state, runState = state) {
  if (!runToggleButton) return;
  const normalized = String(state || '').trim().toLowerCase();
  const control = workspaceLifecycleControl(normalized, runState);
  runToggleButton.hidden = control.hidden;
  runToggleButton.dataset.action = control.action;
  runToggleButton.textContent = control.label;
  runToggleButton.setAttribute('aria-label', `${control.label} workspace`);
  runToggleButton.setAttribute('aria-pressed', String(control.action === 'pause' && !control.disabled));
  runToggleButton.title = normalized === 'completed'
    ? 'Continue this completed workspace'
    : control.action === 'resume'
    ? 'Resume this workspace'
    : 'Pause this workspace';
  runToggleButton.disabled = control.disabled;
}

function syncSteerAvailability(state) {
  const closed = ['terminating', 'termination_failed', 'terminated'].includes(String(state || '').trim().toLowerCase());
  steerInput.disabled = closed;
  sendButton.disabled = closed;
  steerInput.placeholder = closed ? 'This workspace is closed' : 'Message this worker…';
  if (closed) {
    guidancePrimary.textContent = 'This workspace is closed. Return to Workspaces to create new work.';
    guidanceQueue.textContent = '';
    return;
  }
  // Restore the hint after a transient unavailable state, such as a service restart.
  guidancePrimary.textContent = 'Send redirects now';
  syncSendAffordance();
}

function autoResizeSteerInput() {
  if (!steerInput) return;
  steerInput.style.height = 'auto';
  const maximum = Math.min(360, Math.max(156, (window.innerHeight || 850) * .42));
  steerInput.style.height = `${Math.min(Math.max(steerInput.scrollHeight, 52), maximum)}px`;
  syncWatchLayout();
}

function clearLongPress() {
  if (longPressTimer) {
    window.clearTimeout(longPressTimer);
    longPressTimer = 0;
  }
  if (!longPressArmed) return;
  longPressArmed = false;
  syncSendAffordance();
}

function setQueueModifierActive(active) {
  if (queueModifierActive === active) return;
  queueModifierActive = active;
  syncSendAffordance();
}

function refreshDelayForState() {
  if (document.hidden) return IDLE_REFRESH_MS;
  const state = String(currentRunState || '').trim().toLowerCase();
  return ['created', 'starting', 'queued', 'running', 'resuming'].includes(state)
    ? ACTIVE_REFRESH_MS
    : IDLE_REFRESH_MS;
}

function scheduleRefresh(delayMs = refreshDelayForState()) {
  if (refreshTimer) window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => {
    refresh().catch(() => {});
  }, delayMs);
}

function syncWatchMenuAvailability(data) {
  const runtime = data?.runtime_details || {};
  const desktop = Boolean(runtime.view_available || runtime.view_url);
  const closed = ['terminating', 'termination_failed', 'terminated'].includes(String(data?.worker?.close_state || data?.worker?.state || ''));
  const manage = data?.can_manage_workspace === true && !closed;
  const pausedWithoutCompute = String(data?.worker?.state || '') === 'paused'
    && ['absent', 'missing', 'stopped', 'compute_unavailable'].includes(String(runtime.sandbox_state || ''));
  const desktopCanStart = runtime.desktop_supported === true && manage && !pausedWithoutCompute;
  const appActions = document.getElementById('watch-app-actions');
  if (appActions) appActions.hidden = !(desktop || desktopCanStart) || !manage;
  const nativeAction = { 'codex-cli': 'codex', 'claude-code': 'claude', 'openclaw-general': 'openclaw' }[data?.worker?.profile];
  for (const action of ['codex', 'claude', 'openclaw']) {
    const button = menu.querySelector(`[data-action="${action}"]`);
    const supported = !Array.isArray(runtime.desktop_actions) || runtime.desktop_actions.includes(action);
    if (button) button.hidden = !desktop || !manage || action !== nativeAction || !supported;
  }
  surfaceDesktopButton.hidden = !manage || !(desktop || desktopCanStart);
  // Keep the selected view discoverable alongside alternate supported views.
  surfaceTerminalButton.hidden = false;
  surfaceTerminalButton.disabled = currentNoRunActivity;
  openExternal.disabled = !data || (activeSurface === 'terminal' && currentNoRunActivity);
  openTerminalLink.hidden = activeSurface === 'terminal';
  const interrupt = menu.querySelector('[data-action="interrupt"]');
  if (interrupt) {
    const allowed = manage && STEERABLE_RUN_STATES.has(String(data?.latest_run?.state || ''));
    interrupt.hidden = !allowed;
    interrupt.disabled = !allowed;
  }
  const close = menu.querySelector('[data-action="terminate"]');
  if (close) close.hidden = data?.can_manage_workspace !== true || ['terminating', 'terminated'].includes(String(data?.worker?.close_state || data?.worker?.state || ''));
  const workspaceGroup = close?.closest('[role="group"]');
  if (workspaceGroup) workspaceGroup.hidden = openTerminalLink.hidden && (!interrupt || interrupt.hidden) && (!close || close.hidden);
}

function syncMenuLabels() {
  surfaceTerminalButton.dataset.active = String(activeSurface === 'terminal');
  surfaceDesktopButton.dataset.active = String(activeSurface === 'desktop');
  surfaceTerminalButton.setAttribute('aria-current', String(activeSurface === 'terminal'));
  surfaceDesktopButton.setAttribute('aria-current', String(activeSurface === 'desktop'));
  (openExternal.querySelector?.('span') || openExternal).textContent = activeSurface === 'desktop' && currentDesktopAvailable
      ? 'Open current desktop in new tab'
      : 'Open live activity in new tab';
}

function setSurface(surface, { force = false } = {}) {
  activeSurface = surface === 'desktop' ? 'desktop' : 'terminal';
  syncMenuLabels();
  showResult('');
  const state = currentDisplayState;
  if (currentWorkerState === 'terminated') {
    clearAttachedView();
    setOverlay('terminated');
    return;
  }
  if (activeSurface === 'terminal' && currentNoRunActivity && state === 'ready') {
    clearAttachedView();
    setOverlay(state);
    return;
  }
  if ((['created', 'starting', 'paused', 'idle', 'idle_terminated', 'stopped', 'terminating', 'termination_failed', 'terminated'].includes(state)
       && !(activeSurface === 'desktop' && currentDesktopAvailable
            && !['terminating', 'termination_failed', 'terminated'].includes(state)))
      || (TERMINAL_ATTENTION_STATES.has(state) && !showsSavedRunOutput(state)
          && !(activeSurface === 'desktop' && currentDesktopAvailable))) {
    clearAttachedView();
    setOverlay(state);
    return;
  }
  if (activeSurface === 'desktop' && !currentDesktopAvailable) {
    clearAttachedView();
    overlay.hidden = false;
    if (stage) {
      stage.dataset.overlayActive = 'true';
    }
    if (overlayLabel) {
      overlayLabel.textContent = state === 'ready' || state === 'idle' || state === 'completed' ? 'Workspace complete' : 'Workspace status';
    }
    overlayTitle.textContent = state === 'idle' ? 'Worker idle' : state === 'completed' ? 'Work complete' : state === 'ready' ? 'Workspace ready' : 'Work in progress';
    overlayDetail.textContent = state === 'ready' || state === 'idle' || state === 'completed'
      ? 'Send a follow-up below when you are ready.'
      : currentSummary || 'Follow progress in the workspace status above.';
    if (state === 'completed' && currentResultText) {
      showResult(currentResultText);
    }
    return;
  }
  const url = currentSurfaceUrl();
  const filePreviewKey = '';
  const sameFilePreviewAttached = Boolean(
    filePreviewKey
      && lastAttachedFilePreviewKey === filePreviewKey
      && lastAttachedUrl
      && !force
  );
  const stalledFilePreviewAttach = sameFilePreviewAttached
    && !frameReady
    && attachStartedAt
    && Date.now() - attachStartedAt > 12000;
  if (sameFilePreviewAttached && !stalledFilePreviewAttach && !force) {
    // Keep the completed file preview stable while signed URLs rotate in live payloads.
  } else if (force || lastAttachedUrl !== url || frame.src !== url) {
    attachView(url, { force });
  }
  setOverlay(state || 'starting');
}

function redactLiveProgressText(value) {
  return String(value || '')
    .replace(/([?&](?:gh_token|gh_sig|token|signature|sig)=)[^\s&"'<>)]*/gi, '$1[redacted]')
    .replace(/(ghr_)[A-Za-z0-9_-]+/g, '$1[redacted]');
}

function liveProgressText(data) {
  const consolePayload = data?.console || {};
  const raw = String(consolePayload.stdout || consolePayload.stderr || '').trim();
  if (!raw) return '';
  const redacted = redactLiveProgressText(raw).trim();
  if (redacted.length <= 2400) return redacted;
  return `...\n${redacted.slice(-2400)}`;
}

function summarizeOutput(data) {
  currentRunState = String(data?.latest_run?.state || '').trim();
  const output = watchOutputModel(data, liveProgressText(data));
  const pending = Array.isArray(data?.native_control?.pending_requests)
    && data.native_control.pending_requests.length > 0;
  if (!pending) return output;
  return {
    ...output,
    label: 'Response needed',
    panelTitle: 'The worker needs your response',
    summary: data.native_control.read_only === true
      ? 'The worker is waiting for a workspace member to respond.'
      : 'The worker is waiting for your response.',
  };
}

function syncResultActions(deliverable) {
  if (!resultActions) return;
  resultActions.replaceChildren();
  const actions = [];
  if (deliverable?.kind === 'file') {
    const openUrl = String(deliverable.open_url || deliverable.browser_url || '');
    const downloadUrl = String(deliverable.download_url || '');
    if (openUrl) actions.push({ label: 'Open file', url: openUrl, primary: true });
    if (downloadUrl) actions.push({ label: 'Download file', url: downloadUrl, primary: false, download: true });
  }
  resultActions.hidden = actions.length === 0;
  const fileName = deliverable?.kind === 'file'
    ? String(deliverable.label || deliverable.workspace_path || '').trim() : '';
  if (actions.length && fileName) {
    const identity = document.createElement('span');
    identity.className = 'result-file-name';
    identity.textContent = fileName;
    resultActions.appendChild(identity);
  }
  for (const action of actions) {
    const link = document.createElement('a');
    link.className = `result-action${action.primary ? ' primary' : ''}`;
    link.href = action.url;
    link.textContent = action.label;
    if (fileName) link.setAttribute('aria-label', `${action.label}: ${fileName}`);
    if (action.download) {
      link.download = '';
    } else {
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    }
    resultActions.appendChild(link);
  }
  if (libraryManager && deliverable?.kind === 'file' && (deliverable.file_id && deliverable.revision || deliverable.source === 'workspace_file' && deliverable.workspace_path)) {
    const exactVersion = Boolean(deliverable.file_id && deliverable.revision);
    const keep = document.createElement('button'); keep.type = 'button'; keep.className = 'result-action'; keep.textContent = exactVersion ? 'Keep for reuse' : 'Save current file…';
    keep.title = exactVersion ? 'Save this exact delivered file version' : 'Open Files to choose its current version, which may differ from this result';
    keep.onclick = async () => { keep.disabled = true; try { if (exactVersion) await keepDeliveredFile(deliverable, keep); else await showCurrentDeliveredFile(deliverable); } catch (error) { fileError(error.message); filesPanel.hidden = false; filesToggle.setAttribute('aria-expanded', 'true'); selectLibraryTab('files'); } finally { keep.disabled = false; } }; resultActions.append(keep); resultActions.hidden = false;
  }
}
async function keepDeliveredFile(file, button) {
  if (!file.file_id || !file.revision) throw new Error('This result has no saved file version. Open Files to choose the current file.');
  return watchLibrary.capture(file, button);
}
async function showCurrentDeliveredFile(file) {
  const owner = currentLibraryOwnerScope();
  if (!owner) return;
  const path = String(file.workspace_path || ''); const directory = path.split('/').slice(0, -1).join('/');
  filesPanel.hidden = false; filesToggle.setAttribute('aria-expanded', 'true'); selectLibraryTab('files');
  await workspaceFiles.open(directory);
  if (owner === currentLibraryOwnerScope() && !filesStatus.textContent) fileError('Choose the current file’s menu, then Keep for reuse. Its current version may differ from this result.');
}

function formatArtifactSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '';
  if (value < 1024) return `${value} bytes`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function syncArtifactList(items) {
  if (!artifactList) return;
  artifactList.replaceChildren();
  const files = Array.isArray(items)
    ? items.filter((item) => item && !item.is_dir && (item.open_url || item.download_url || item.path))
    : [];
  artifactList.hidden = files.length === 0;
  if (workspaceArtifacts) workspaceArtifacts.hidden = files.length === 0;
  if (artifactCount) artifactCount.textContent = String(files.length);
  if (!files.length) return;

  const visibleFiles = files.slice(0, 20);
  for (const file of visibleFiles) {
    const row = document.createElement('div');
    row.className = 'artifact-row';

    const label = document.createElement('span');
    label.className = 'artifact-label';
    label.textContent = String(file.path || file.name || 'artifact');
    row.appendChild(label);

    const meta = document.createElement('span');
    meta.className = 'artifact-meta';
    const size = formatArtifactSize(file.size);
    meta.textContent = size || 'file';
    row.appendChild(meta);

    if (file.open_url) {
      const open = document.createElement('a');
      open.className = 'artifact-link';
      open.href = String(file.open_url);
      open.target = '_blank';
      open.rel = 'noopener noreferrer';
      open.textContent = 'Open';
      row.appendChild(open);
    }
    if (file.download_url) {
      const download = document.createElement('a');
      download.className = 'artifact-link';
      download.href = String(file.download_url);
      download.download = '';
      download.textContent = 'Download';
      row.appendChild(download);
    }

    artifactList.appendChild(row);
  }
  if (files.length > visibleFiles.length) {
    const more = document.createElement('div');
    more.className = 'artifact-list-more';
    more.textContent = `${files.length - visibleFiles.length} more files`;
    artifactList.appendChild(more);
  }
}

const workspaceFiles = createWorkspaceFiles({
  workerId, list: filesList, status: filesStatus, directoryLabel: filesDirectory,
  more: filesMore, folderButton: filesFolder, csrf: currentCsrfToken,
  moveButton: filesMove, exportLink: filesExport,
  selectionStatus: filesSelectionStatus, selectVisible: filesSelectVisible,
  clearSelection: filesClearSelection, batch: document.getElementById('watch-file-batch'),
  trashList: filesTrashList, trashStatus: filesTrashStatus,
  trashPanel: document.getElementById('watch-file-trash'),
  trashOpen: document.getElementById('watch-file-trash-open'),
  trashClose: document.getElementById('watch-file-trash-close'),
  optionsMenu: document.getElementById('watch-file-options'),
  notice: document.getElementById('watch-file-notice'),
  noticeText: document.getElementById('watch-file-notice-text'),
  noticeUndo: document.getElementById('watch-file-undo'),
  noticeClose: document.getElementById('watch-file-notice-close'),
  moveDialog: document.getElementById('watch-file-move-dialog'),
  moveLocation: document.getElementById('watch-file-move-location'),
  moveParent: document.getElementById('watch-file-move-parent'),
  moveFolders: document.getElementById('watch-file-move-folders'),
  moveMore: document.getElementById('watch-file-move-more'),
  moveError: document.getElementById('watch-file-move-error'),
  moveConfirm: document.getElementById('watch-file-move-confirm'),
  moveCancel: document.getElementById('watch-file-move-cancel'),
  moveCancelBottom: document.getElementById('watch-file-move-cancel-bottom'),
  audienceLabel: document.getElementById('watch-files-audience'),
  onKeepForReuse: signedToken ? null : (entry, returnFocus) => libraryManager ? watchLibrary.capture(entry, returnFocus) : Promise.reject(new Error('Sign in with a workspace manager account to save this file.')),
  onAccess: (canWrite) => {
    filesCanWrite = canWrite;
    watchDraftActivation = activateWatchDraft(canWrite);
  },
});
// One attach per upload from this page. The automatic attach and Add to workspace can both see a
// ready upload; attaching it twice would add a second copy to the workspace. A failed attach can retry.
const attachStarted = new Set();
// Reports which of this call's uploads were accepted and which failed; an upload another call is
// already attaching is left to that call. Only failed uploads become available to retry.
async function attachOnce(ids) {
  const fresh = ids.filter((id) => !attachStarted.has(id));
  fresh.forEach((id) => attachStarted.add(id));
  let result = { accepted: [], failed: [] };
  try {
    if (fresh.length) result = await workspaceFiles.attach(fresh);
  } catch (error) {
    fresh.forEach((id) => attachStarted.delete(id));
    throw error;
  }
  result.failed.forEach(({ uploadId }) => attachStarted.delete(uploadId));
  return result;
}
const unattachedIds = (readyIds) => readyIds.filter((id) => !attachStarted.has(id));
const syncAttachButton = () => { filesAttach.hidden = !unattachedIds(watchDraft.readyIds()).length; };
// An accepted attach belongs to the owner who made it, even if the Files refresh that follows fails
// and clears this page's owner: the draft records it for that owner, so it is not offered again.
async function attachForOwner(ids, ownerScope) {
  const attaching = attachOnce(ids);
  syncAttachButton();
  try {
    const { accepted, failed } = await attaching;
    // Uploads accepted in this attempt are recorded even when another one was refused.
    for (const id of accepted) watchDraft.dismissReady(id, ownerScope);
    if (failed.length === 1) throw failed[0].error;
    if (failed.length) throw new Error(`${failed.length} files were not added: ${failed[0].error.message}`);
  } finally { syncAttachButton(); }
}
async function attachReadyUpload(item, ownerScope) {
  if (ownerScope !== watchDraft.ownerScope()) return;
  try {
    await attachForOwner([item.upload_id], ownerScope);
  } catch (error) {
    if (ownerScope === watchDraft.ownerScope()) fileError(`${item.name} was not added: ${error.message}. Retry below.`);
  }
}
async function attachPendingUploads() {
  const pending = unattachedIds(watchDraft.readyIds());
  const ownerScope = watchDraft.ownerScope();
  if (!ownerScope || !pending.length) return;
  try {
    await attachForOwner(pending, ownerScope);
  } catch (error) {
    if (ownerScope === watchDraft.ownerScope()) fileError(error.message);
  }
}
const watchDraft = createFileDraft({
  input: filesInput, folderInput: filesFolderInput,
  drop: filesDrop, list: filesUploads, help: filesHelp,
  budget: filesBudget, csrf: currentCsrfToken, scope: `watch.${workerId}`,
  quietReady: true, dropOpensPicker: false,
  onChange: ({ readyIds }) => { filesAttach.hidden = !unattachedIds(readyIds).length; },
  onReady: attachReadyUpload,
});
const watchLibrary = createLibraryBrowser({ mount: document.getElementById('watch-library-browser'), workerId,
  getOwnerScope: currentLibraryOwnerScope, onPick: async (refs, resolved, { signal }) => {
    if (!libraryManager) throw new Error('This workspace is not editable from this view.');
    if (libraryTargetClosed()) throw new Error('This workspace is closed. You can still save and reuse its files.');
    const owner = currentLibraryOwnerScope();
    const nativeItems = (resolved.items || []).filter(item => ['skill', 'native', 'approved'].includes(item.kind));
    let confirmNative = false;
    if (nativeItems.length) confirmNative = await reviewLibraryUse(nativeItems, librarySnapshot?.worker?.name || 'this worker', { signal, profile: librarySnapshot?.worker?.profile });
    if (nativeItems.length && !confirmNative) return;
    if (!libraryManager || signal.aborted || owner !== currentLibraryOwnerScope()) throw new Error('Your account changed. Open Library again before continuing.');
    if (libraryTargetClosed()) throw new Error('This workspace is closed. You can still save and reuse its files.');
    await libraryRequest(`/api/workspace/${encodeURIComponent(workerId)}/library`, { method: 'POST', body: { refs, request_key: crypto.randomUUID(), confirm_native: confirmNative } });
    await workspaceFiles.refresh(); await refreshAddedLibrary(); selectLibraryTab('added');
  }, pickLabel: 'Add here' });
function syncWatchLibrarySnapshot(data) {
  libraryManager = data?.can_manage_workspace === true && !signedToken;
  if (!libraryManager) libraryOwnerScope = '';
  librarySnapshot = data; syncLibraryAccess();
}
function syncLibraryAccess() {
  const owner = currentLibraryOwnerScope();
  const allowed = Boolean(owner);
  for (const name of ['added', 'saved']) document.getElementById(`watch-library-tab-${name}`).hidden = !allowed;
  watchLibrary?.setAccess(allowed, owner);
  watchLibrary.setPickAvailable(!libraryTargetClosed(), 'This workspace is closed. You can still save and reuse its files.');
  workspaceFiles.setKeepForReuseAccess(allowed);
  if (requestedLibraryEntry && libraryManager) {
    filesPanel.hidden = false; filesToggle.setAttribute('aria-expanded', 'true');
    if (!allowed && !requestedLibraryPrepared) { requestedLibraryPrepared = true; void workspaceFiles.open(); }
  }
  if (allowed && requestedLibraryEntry) { pendingLibraryTab = ''; requestedLibraryEntry = false; filesPanel.hidden = false; filesToggle.setAttribute('aria-expanded', 'true'); selectLibraryTab('added'); }
  if (allowed && pendingLibraryTab && !filesPanel.hidden) {
    const preferredTab = pendingLibraryTab; pendingLibraryTab = ''; selectLibraryTab(preferredTab);
  }
  const busy = ['queued', 'running', 'settling'].includes(String(librarySnapshot?.latest_run?.state || ''));
  for (const button of document.querySelectorAll('[data-library-remove], [data-library-mutation]')) { button.disabled = libraryTargetClosed() || busy || button.dataset.libraryBusy === 'true'; button.title = libraryTargetClosed() ? 'This workspace is closed' : busy ? 'Available when the worker is idle' : ''; }
  if (!allowed) { libraryAddedEpoch += 1; document.getElementById('watch-library-added-list').replaceChildren(); if (document.querySelector('.watch-library-tabs [aria-selected="true"]')?.id !== 'watch-library-tab-files') selectLibraryTab('files'); }
}
function selectLibraryTab(name, focus = false, remember = false) {
  if (remember) pendingLibraryTab = '';
  if (name !== 'files' && !currentLibraryOwnerScope()) name = 'files';
  if (remember) { try { localStorage.setItem('xperfect.display.library-tab', name); } catch { /* Display preference only. */ } }
  for (const button of document.querySelectorAll('.watch-library-tabs [role="tab"]')) {
    const active = button.id === `watch-library-tab-${name}`; button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
    document.getElementById(button.getAttribute('aria-controls')).hidden = !active; if (active && focus) button.focus();
  }
  if (name === 'added') void refreshAddedLibrary();
  if (name === 'saved') void watchLibrary.refresh();
}
function libraryUndoButton(item, status) {
  const undo = document.createElement('button'); undo.type = 'button'; undo.className = 'quiet-button'; undo.textContent = item.kind === 'approved' ? 'Restore for review' : 'Undo';
  undo.dataset.libraryRemove = item.application_id;
  const busy = ['queued', 'running', 'settling'].includes(String(librarySnapshot?.latest_run?.state || ''));
  undo.disabled = busy || libraryTargetClosed(); if (undo.disabled) undo.title = libraryTargetClosed() ? 'This workspace is closed' : 'Available when the worker is idle';
  undo.onclick = async () => {
    const owner = currentLibraryOwnerScope(); const signal = watchLibrary.signal();
    if (!libraryManager || signal.aborted || libraryTargetClosed()) return;
    undo.disabled = true; undo.dataset.libraryBusy = 'true';
    try {
      await libraryRequest(`/api/workspace/${encodeURIComponent(workerId)}/library/${encodeURIComponent(item.application_id)}/undo`, { method: 'POST', body: {} });
      if (signal.aborted || owner !== currentLibraryOwnerScope()) return;
      await refreshAddedLibrary(); await workspaceFiles.refresh();
    } catch (error) { if (!signal.aborted) { status.textContent = error.message; undo.disabled = false; } }
    finally { undo.dataset.libraryBusy = 'false'; }
  };
  return undo;
}
async function refreshAddedLibrary() {
  if (!libraryManager) return; const epoch = ++libraryAddedEpoch; const mount = document.getElementById('watch-library-added-list'); const status = document.getElementById('watch-library-added-status'); status.textContent = 'Loading added items…';
  try {
    const data = await libraryRequest(`/api/workspace/${encodeURIComponent(workerId)}/library`);
    if (epoch !== libraryAddedEpoch || !libraryManager) return; mount.replaceChildren();
    for (const item of data.items || []) {
      const row = document.createElement('article'); row.className = 'personal-library-item'; const copy = document.createElement('div'); copy.className = 'personal-library-item-copy';
      const name = document.createElement('strong'); name.textContent = item.name; const state = document.createElement('small'); state.textContent = libraryStatus(item); copy.append(name, state); row.append(copy);
      if (item.state === 'removed') {
        if (item.undo_available) row.append(libraryUndoButton(item, status));
        mount.append(row); continue;
      }
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'quiet-button'; remove.textContent = item.state === 'cleanup_unconfirmed' ? 'Retry file cleanup' : 'Remove here';
      const active = ['queued', 'running', 'settling'].includes(String(librarySnapshot?.latest_run?.state || '')); remove.dataset.libraryRemove = item.application_id; remove.disabled = active || libraryTargetClosed(); if (remove.disabled) remove.title = libraryTargetClosed() ? 'This workspace is closed' : 'Available when the worker is idle';
      remove.onclick = async () => {
        if (libraryTargetClosed()) return;
        remove.disabled = true; remove.dataset.libraryBusy = 'true';
        try {
          const result = await libraryRequest(`/api/workspace/${encodeURIComponent(workerId)}/library/${encodeURIComponent(item.application_id)}`, { method: 'DELETE' });
          await refreshAddedLibrary(); await workspaceFiles.refresh();
          const outcome = libraryRemovalOutcome(result);
          status.replaceChildren(document.createTextNode(outcome.message));
          if (outcome.needsRetry) {
            const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'quiet-button'; retry.textContent = 'Retry file cleanup'; retry.dataset.libraryRemove = item.application_id;
            retry.onclick = async () => { retry.disabled = true; await remove.onclick(); }; status.append(retry);
          }
          if (result.can_undo || result.undo_available) status.append(libraryUndoButton(item, status));
        } catch (error) { status.textContent = error.message; remove.disabled = false; }
        finally { remove.dataset.libraryBusy = 'false'; }
      };
      const actions = document.createElement('div'); actions.className = 'personal-library-item-actions'; actions.append(remove);
      if (item.state === 'cleanup_unconfirmed' && item.undo_available) actions.append(libraryUndoButton(item, status));
      if (['needs_review', 'needs_setup'].includes(item.state)) {
        const setup = document.createElement('button'); setup.type = 'button'; setup.className = 'secondary-button'; setup.textContent = item.kind === 'approved' || item.state === 'needs_review' ? 'Review' : 'Set up';
        setup.dataset.libraryMutation = 'true'; setup.disabled = libraryTargetClosed(); if (setup.disabled) setup.title = 'This workspace is closed';
        setup.onclick = async () => { const owner = currentLibraryOwnerScope(); const signal = watchLibrary.signal(); setup.disabled = true; try {
          if (libraryTargetClosed()) return;
          const exact = await libraryRequest(`/api/personal-library/${encodeURIComponent(item.item_id)}?version_id=${encodeURIComponent(item.version_id)}`);
          if (!libraryManager || signal.aborted || owner !== currentLibraryOwnerScope()) return;
          if (item.kind === 'approved') {
            const resources = await libraryRequest('/api/control-plane?sections=resources'); const approved = (resources.library || []).find(value => value.library_id === exact.payload?.library_id);
            if (!approved) throw new Error('This approved capability is unavailable. Refresh Library to choose another.');
            if (!libraryManager || signal.aborted || owner !== currentLibraryOwnerScope()) throw new Error('Your account changed. Open Library again before continuing.');
            if (libraryTargetClosed()) throw new Error('This workspace is closed. You can still save and reuse its files.');
            const pending = await libraryRequest('/api/pending-changes', { method: 'POST', body: { change_type: 'library_enable', target_id: workerId, payload: { library_id: approved.library_id, scopes: approved.scopes || [] } } });
            if (!pending.change_id || !pending.confirmation_token) throw new Error('Could not prepare the existing capability review.');
            window.location.assign(`/confirm-change#change_id=${encodeURIComponent(pending.change_id)}&token=${encodeURIComponent(pending.confirmation_token)}`);
          } else if (item.state === 'needs_review') {
            if (await reviewLibraryUse([exact], librarySnapshot?.worker?.name || 'this worker', { signal, profile: librarySnapshot?.worker?.profile })) { if (libraryTargetClosed()) throw new Error('This workspace is closed. You can still save and reuse its files.'); await libraryRequest(`/api/workspace/${encodeURIComponent(workerId)}/library`, { method: 'POST', body: { refs: [{ item_id: item.item_id, version_id: item.version_id }], request_key: crypto.randomUUID(), confirm_native: true } }); await refreshAddedLibrary(); }
          } else askNativeLibrarySetup({ workerId, item: exact, signal, canStart: () => !libraryTargetClosed() });
        } catch (error) { status.textContent = error.message; } finally { setup.disabled = libraryTargetClosed(); } };
        actions.prepend(setup);
      }
      row.append(actions); mount.append(row);
    }
    if (!(data.items || []).length) { const empty = document.createElement('p'); empty.className = 'personal-library-empty'; empty.textContent = 'No knowledge or abilities added yet. You can still use Files.'; mount.append(empty); }
    status.textContent = '';
  } catch (error) { if (epoch === libraryAddedEpoch && libraryManager) status.textContent = `Could not load added items: ${error.message}`; }
}
for (const button of document.querySelectorAll('.watch-library-tabs [role="tab"]')) {
  button.addEventListener('click', () => selectLibraryTab(button.id.slice('watch-library-tab-'.length), false, true));
  button.addEventListener('keydown', event => { const tabs = [...document.querySelectorAll('.watch-library-tabs [role="tab"]')].filter(tab => !tab.hidden); const index = tabs.indexOf(button); let next; if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length]; if (event.key === 'ArrowLeft') next = tabs[(index + tabs.length - 1) % tabs.length]; if (event.key === 'Home') next = tabs[0]; if (event.key === 'End') next = tabs.at(-1); if (next) { event.preventDefault(); selectLibraryTab(next.id.slice('watch-library-tab-'.length), true, true); } });
}
const setWatchDraftBusy = (busy) => {
  watchDraft.setBusy(busy);
  filesAttach.disabled = busy;
};
// The latest owner-scope check; it always settles (errors are shown inside).
let watchDraftActivation = Promise.resolve();

async function settledWatchDraftScope() {
  let current;
  do {
    current = watchDraftActivation;
    await current;
  } while (current !== watchDraftActivation);
  return watchDraft.ownerScope();
}

// A DataTransfer is empty once its drop event returns, so read what was dropped
// synchronously and walk folders later.
function captureDroppedFiles(transfer) {
  const items = Array.from(transfer?.items || []).filter((item) => item.kind === 'file');
  const entries = items.map((item) => item.webkitGetAsEntry?.() || null);
  const files = items.map((item) => item.getAsFile?.() || null);
  const fallback = Array.from(transfer?.files || []);
  async function walk(entry, prefix) {
    const path = `${prefix}${entry.name}`;
    if (entry.isFile) return [{ file: await new Promise((resolve, reject) => entry.file(resolve, reject)), relativePath: path }];
    const reader = entry.createReader();
    const children = [];
    for (;;) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      children.push(...batch);
    }
    const result = [];
    for (const child of children) result.push(...await walk(child, `${path}/`));
    return result;
  }
  return async () => {
    if (!items.length) return fallback.map((file) => ({ file, relativePath: file.name }));
    const picked = [];
    for (let index = 0; index < items.length; index += 1) {
      if (entries[index]?.isDirectory) picked.push(...await walk(entries[index], ''));
      else if (files[index]) picked.push({ file: files[index], relativePath: files[index].name });
    }
    return picked;
  };
}

// Dropping onto Watch opens Files; the files join the draft only once this
// viewer's own account scope is verified, and only if it did not change meanwhile.
// Drops are read at drop time and then handled one at a time, in order.
let watchDropQueue = Promise.resolve();

function queueWatchDrop(transfer) {
  const readDrop = captureDroppedFiles(transfer);
  filesPanel.hidden = false; filesToggle.setAttribute('aria-expanded', 'true');
  watchDropQueue = watchDropQueue.then(() => addWatchDrop(readDrop), () => addWatchDrop(readDrop));
}

async function addWatchDrop(readDrop) {
  await workspaceFiles.open();
  const scope = await settledWatchDraftScope();
  const listingProblem = String(filesStatus?.textContent || '').trim();
  if (filesCanWrite !== true && listingProblem) {
    fileError(`These files were not added because Files could not load: ${listingProblem} Reopen Files and try again.`);
    return;
  }
  if (filesCanWrite === false) { fileError('This workspace view can download files. Editing requires a workspace member.'); return; }
  if (filesCanWrite !== true || !scope) {
    fileError('These files were not added because your account is still being checked. Drop them again in a moment.');
    return;
  }
  let picked;
  try { picked = await readDrop(); } catch (error) { fileError(`Could not read the dropped files: ${error.message}`); return; }
  if (scope !== watchDraft.ownerScope()) { fileError('Your account changed before the files were added. Drop them again.'); return; }
  watchDraft.addFiles(picked);
}

async function activateWatchDraft(canWrite) {
  const load = ++watchDraftScopeLoad;
  const hadOwnerScope = Boolean(watchDraft.ownerScope());
  watchDraftScopeController?.abort();
  watchDraftScopeController = null;
  if (!canWrite) {
    watchDraft.setOwnerScope(null); filesUploadArea.hidden = true;
    if (!libraryManager) {
      libraryOwnerScope = ''; setWatchDraftBusy(false); syncLibraryAccess(); return;
    }
    // Read-only Files do not remove this exact workspace manager's private Library.
    // Reuse this owner check; keep its upload draft disabled.
  }
  const controller = new AbortController();
  watchDraftScopeController = controller;
  setWatchDraftBusy(true);
  if (!hadOwnerScope) filesUploadArea.hidden = true;
  try {
    const response = await fetch('/api/bootstrap', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`Account check failed (${response.status}).`);
    const data = await response.json();
    if (load !== watchDraftScopeLoad || (!filesCanWrite && !libraryManager)) return;
    watchDraft.setOwnerScope(filesCanWrite ? data.draft_owner_scope : null);
    libraryOwnerScope = libraryManager && /^[a-f0-9]{64}$/.test(String(data.draft_owner_scope || '')) ? data.draft_owner_scope : '';
    syncLibraryAccess();
    filesUploadArea.hidden = !filesCanWrite || !watchDraft.ownerScope();
    if (filesCanWrite && !watchDraft.ownerScope()) fileError('Your account could not be verified. Refresh to add files.');
  } catch (error) {
    if (load !== watchDraftScopeLoad) return;
    watchDraft.setOwnerScope(null); libraryOwnerScope = '';
    syncLibraryAccess();
    filesUploadArea.hidden = true;
    fileError(`Could not check your account: ${error.message} Refresh to add files.`);
  } finally {
    if (load === watchDraftScopeLoad) {
      watchDraftScopeController = null;
      setWatchDraftBusy(false);
    }
  }
}

async function maybePromoteDeliverable(data) {
  const deliverable = data.deliverable || null;
  const runState = String(data.latest_run?.state || '').trim();
  const runId = String(data.latest_run?.run_id || '').trim();
  if (deliverable?.kind === 'file' && (deliverable.open_url || deliverable.browser_url) && runState === 'completed') {
    const fileUrl = String(deliverable.open_url || deliverable.browser_url || '').trim();
    const promotionKey = fileDeliverableKey(deliverable, runId) || `${runId}:${fileUrl}`;
    currentFilePreviewKey = promotionKey;
    currentFilePreviewUrl = fileUrl;
    currentFileDownloadUrl = String(deliverable.download_url || '');
    currentDeliverable = {
      ...deliverable,
      open_url: currentFilePreviewUrl,
      download_url: currentFileDownloadUrl,
    };
    syncResultActions(currentDeliverable);
    if (promotionKey && promotionKey !== lastPromotedDeliverableKey) {
      lastPromotedDeliverableKey = promotionKey;
    }
    return;
  }
  if (!deliverable || !deliverable.browser_url || deliverable.preferred_surface !== 'desktop') return;
  if (!['running', 'completed'].includes(runState)) return;
  const promotionKey = `${runId}:${deliverable.browser_url}`;
  if (!promotionKey || promotionKey === lastPromotedDeliverableKey || deliverablePromotionPending) return;

  deliverablePromotionPending = true;
  try {
    await postAction('action:browser', { url: deliverable.browser_url });
    lastPromotedDeliverableKey = promotionKey;
    activeSurface = 'desktop';
    setSurface('desktop', { force: true });
    window.setTimeout(() => {
      postAction('action:focus_browser').catch(() => {});
    }, 300);
  } catch (error) {
    console.debug('deliverable promotion failed', error);
  } finally {
    deliverablePromotionPending = false;
  }
}

function renderOutputContent(output) {
  statusLabel.textContent = output.label;
  resultPanelTitle.textContent = output.panelTitle;
  latestOutputInline.textContent = output.summary;
  latestOutputHuman.textContent = output.summary;
  latestOutputHuman.hidden = Boolean(output.result);
  // Only a completed run's answer is a result; other output stays in Technical details.
  latestOutputFull.innerHTML = output.result ? resultMarkdown.render(output.result, { resultArtifacts: currentResultArtifacts }) : '';
  latestOutputFull.hidden = !output.result;
  const kind = output.result ? 'result' : 'status';
  const verb = resultPanel.hidden ? 'Open' : 'Close';
  resultToggle.setAttribute('aria-label', `${verb} latest workspace ${kind}`);
  if (resultToggleAction) resultToggleAction.textContent = `${verb} ${kind}`;
  latestOutputTechnical.textContent = output.technical || '';
  resultTechnical.hidden = !output.technical;
  if (!output.technical) resultTechnical.open = false;
  currentSummary = output.summary;
  currentFullOutput = [output.summary, output.result, output.technical].filter(Boolean).join('\n\n');
  resultToggle.hidden = !currentFullOutput.trim();
}

function showActionFailure(error) {
  const technical = String(error?.message || error || 'Unknown error');
  let summary = 'The action could not be confirmed. Check the workspace status before trying again.';
  try {
    const detail = JSON.parse(technical)?.detail;
    if (typeof detail === 'string' && detail.trim()) summary = detail;
    else if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      summary = [detail.message, detail.recovery].filter(value => typeof value === 'string' && value.trim()).join(' ') || summary;
    }
  } catch { /* Keep the existing fallback for unstructured failures. */ }
  actionFailure = { label: 'Action not confirmed', panelTitle: 'Action needs attention',
    summary, result: '', technical };
  renderOutputContent(actionFailure);
  resultTechnical.open = false;
  openResultPanel();
}

function renderOutput(data) {
  currentResultArtifacts = data.artifacts?.items || [];
  const output = summarizeOutput(data);
  renderOutputContent(actionFailure || output);
  syncResultActions(data.deliverable || null);
  syncArtifactList(data.artifacts?.items || []);
  currentResultText = currentRunState === 'completed' ? String(output.result || '').trim() : '';
  if (Array.isArray(data?.native_control?.pending_requests) && data.native_control.pending_requests.length) {
    openResultPanel();
  }
  if (!currentFullOutput.trim()) closeResultPanel();
}

async function nativeHttpError(response) {
  const body = await response.json().catch(() => null);
  return new Error(typeof body?.detail === 'string' ? body.detail : `Native controls unavailable (${response.status}).`);
}

async function nativeGetJson(url) {
  const response = await fetch(withAuth(url), { cache: 'no-store' });
  if (!response.ok) throw await nativeHttpError(response);
  return response.json();
}

async function nativePostJson(url, payload) {
  const response = await fetch(withAuth(url), {
    method: 'POST',
    headers: csrfHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await nativeHttpError(response);
  return response.json();
}

async function postAction(action, payload) {
  const response = await fetch(withAuth(`${workspaceApiBase}/${action.startsWith('action:') ? 'action/' + action.split(':', 2)[1] : action}`), {
    method: 'POST',
    headers: csrfHeaders(payload ? { 'Content-Type': 'application/json' } : {}),
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function submitFooterInstruction(mode) {
  const message = steerInput.value.trim();
  if (!message) return;
  const path = mode === 'queue' || !STEERABLE_RUN_STATES.has(currentRunState)
    ? 'message' : 'steer';
  try {
    const response = await fetch(withAuth(`${workspaceApiBase}/${path}`), {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ message }),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    steerInput.value = '';
    autoResizeSteerInput();
    closeResultPanel();
    actionFailure = null;
    renderOutputContent({ label: path === 'message' ? 'Follow-up accepted' : 'Guidance accepted',
      panelTitle: path === 'message' ? 'Follow-up accepted' : 'Guidance accepted',
      summary: 'Your instruction was accepted. The workspace status shows what happens next.',
      result: message, technical: '' });
  } catch (error) {
    showActionFailure(error);
  }
}

function setOverlay(state, detail) {
  const needsAttention = TERMINAL_ATTENTION_STATES.has(state);
  const connecting = Boolean(attachStartedAt && !frameReady);
  if (activeSurface === 'desktop' && currentDesktopAvailable && !connecting
      && !['terminating', 'termination_failed', 'terminated'].includes(state)) {
    overlay.hidden = true;
    if (stage) { stage.dataset.overlayActive = 'false'; stage.dataset.overlayMode = ''; }
    return;
  }
  const filePreviewActive = activeSurface === 'desktop' && Boolean(filePreviewUrl());
  const readyForFirstRun = currentNoRunActivity && state === 'ready';
  const waiting = readyForFirstRun || state === 'starting' || state === 'paused' || state === 'idle' || state === 'idle_terminated' || state === 'stopped' || state === 'terminating' || state === 'termination_failed' || state === 'terminated' || needsAttention || connecting;
  // The attention notice sits above that run's saved output instead of covering it.
  const banner = needsAttention && showsSavedRunOutput(state);
  overlay.hidden = !waiting;
  if (stage) {
    stage.dataset.overlayActive = String(waiting && !banner);
    stage.dataset.overlayMode = banner ? 'banner' : '';
  }
  if (!waiting) return;

  if (readyForFirstRun) {
    if (overlayLabel) overlayLabel.textContent = 'Workspace ready';
    overlayTitle.textContent = 'What should I work on?';
    overlayDetail.textContent = 'Send a message below to start work. Your files are available in Library.';
    return;
  }

  if (needsAttention) {
    if (overlayLabel) {
      overlayLabel.textContent = 'Workspace needs attention';
    }
    overlayTitle.textContent = 'Workspace needs attention';
    overlayDetail.textContent = banner
      ? 'The latest run did not complete. Its saved terminal output is below; review it, then send a corrected follow-up.'
      : 'The latest run did not complete. Review the status details, then send a corrected follow-up.';
    return;
  }

  if (['terminating', 'termination_failed', 'terminated'].includes(state)) {
    const closing = state === 'terminating';
    const closeFailed = state === 'termination_failed';
    const savedResult = state === 'terminated' && Boolean(currentResultText);
    if (overlayLabel) {
      overlayLabel.textContent = closing ? 'Workspace closing' : closeFailed ? 'Close needs attention' : savedResult ? 'Saved work' : 'Workspace closed';
    }
    overlayTitle.textContent = closing ? 'Workspace closing' : closeFailed ? 'Close needs attention' : savedResult ? 'Your result' : 'Workspace closed';
    overlayDetail.textContent = closing
      ? 'xPerfect is safely stopping this workspace.'
      : closeFailed
      ? 'xPerfect could not confirm compute shutdown. Retry Close workspace; new work remains blocked.'
      : savedResult
      ? 'This workspace is closed. Your saved result remains available.'
      : 'This workspace was shut down. Return to Workspaces to create new work.';
    showResult(savedResult ? currentResultText : '');
    return;
  }

  if (state === 'paused') {
    if (overlayLabel) {
      overlayLabel.textContent = 'Workspace paused';
    }
    overlayTitle.textContent = 'Workspace paused';
    overlayDetail.textContent = 'Compute is stopped for this workspace. Use Resume to continue from the same state.';
    return;
  }

  if (state === 'idle' || state === 'idle_terminated' || state === 'stopped') {
    if (overlayLabel) {
      overlayLabel.textContent = 'Workspace idle';
    }
    overlayTitle.textContent = 'Worker idle';
    overlayDetail.textContent = currentNoRunActivity
      ? 'No work is running. Send a message below to start work. Your files are available in Library.'
      : 'The last run is complete and compute is stopped to save resources. Send follow-up work and xPerfect will resume this workspace automatically.';
    return;
  }

  if (connecting && Date.now() - attachStartedAt >= 12000) {
    if (overlayLabel) overlayLabel.textContent = 'View unavailable';
    overlayTitle.textContent = activeSurface === 'desktop' ? 'The desktop has not connected' : 'Live activity has not connected';
    overlayDetail.textContent = 'Open More and select this view to reconnect, or open it in a new tab. Your work can continue while the view reconnects.';
    return;
  }

  if (connecting) {
    if (overlayLabel) {
      overlayLabel.textContent = filePreviewActive ? 'Delivered file' : 'Connecting';
    }
    overlayTitle.textContent = filePreviewActive
      ? 'Opening delivered file…'
      : activeSurface === 'desktop'
        ? 'Attaching live workspace…'
        : 'Opening live activity…';
    overlayDetail.textContent = filePreviewActive
      ? 'The completed file preview is loading. Use Open delivered file in new tab if this takes more than a few seconds.'
      : activeSurface === 'desktop'
        ? 'The desktop is waking up. If it takes more than a few seconds, open the current desktop in a new tab.'
      : 'We are opening this worker’s live activity. If it takes more than a few seconds, open the current session in a new tab.';
    return;
  }

  if (overlayLabel) {
    overlayLabel.textContent = 'Workspace warming up';
  }
  overlayTitle.textContent = filePreviewActive
    ? 'Opening delivered file…'
    : activeSurface === 'desktop'
      ? 'Preparing live workspace…'
      : 'Opening live activity…';
  overlayDetail.textContent = detail || (filePreviewActive
    ? 'The completed file preview will appear here automatically.'
    : activeSurface === 'desktop'
      ? 'The desktop will attach automatically when the workspace is ready.'
    : 'Your worker’s activity will appear here when it is ready.');
}

function showWorkspaceUnavailable(status) {
  const code = Number(status);
  const heading = code === 401 ? 'Sign in to continue' : 'Workspace unavailable';
  const message = code === 401
    ? 'Your sign-in ended. Sign in again to continue.'
    : [403, 404].includes(code)
      ? 'This workspace is unavailable to this account.'
      : 'Workspace status is unavailable right now.';
  if (signInLink) {
    signInLink.hidden = code !== 401;
    if (code === 401) {
      signInLink.href = `/login?return_to=${encodeURIComponent(window.location.pathname + window.location.search + window.location.hash)}`;
    }
  }
  currentDisplayState = 'unavailable';
  currentRunState = 'unavailable';
  currentDesktopAvailable = false;
  currentDeliverable = null;
  currentSummary = message;
  currentFullOutput = message;
  currentResultText = '';
  showResult('');
  clearAttachedView();
  title.textContent = heading;
  subtitle.textContent = message;
  statePill.textContent = code === 401 ? 'Sign-in needed' : 'Unavailable';
  statePill.dataset.state = 'unavailable';
  actionFailure = null;
  renderOutputContent({
    label: heading, panelTitle: heading,
    summary: message, result: '', technical: '',
  });
  syncResultActions(null);
  syncArtifactList([]);
  if (runTime) runTime.hidden = true;
  filesToggle.disabled = true; menuToggle.disabled = true;
  filesPanel.hidden = true; filesToggle.setAttribute('aria-expanded', 'false');
  syncWatchMenuAvailability(null);
  syncWatchLibrarySnapshot(null);
  const interrupt = menu.querySelector('[data-action="interrupt"]');
  if (interrupt) interrupt.disabled = true;
  if (runToggleButton) {
    runToggleButton.hidden = true;
    runToggleButton.disabled = true;
  }
  steerInput.disabled = true;
  sendButton.disabled = true;
  steerInput.placeholder = code === 401 ? 'Sign in to use this workspace' : 'Workspace unavailable';
  guidancePrimary.textContent = message;
  guidanceQueue.textContent = '';
  overlay.hidden = false;
  if (stage) stage.dataset.overlayActive = 'true';
  if (overlayLabel) overlayLabel.textContent = heading;
  overlayTitle.textContent = heading;
  overlayDetail.textContent = message;
}
function syncNativeControls(data) {
  if (nativeControlsMount) nativeControlsMount.hidden = data?.native_control === null;
  if (canGuideNativeTurn(data?.native_control, signedToken) && !nativeControls && nativeControlsMount) {
    nativeControls = attachNativeControls(nativeControlsMount, workerId, {
      getJson: nativeGetJson,
      postJson: nativePostJson,
    });
  }
  nativeControls?.updateLive(data?.native_control || null);
}

async function refresh() {
  if (refreshInFlight) {
    scheduleRefresh(ACTIVE_REFRESH_MS);
    return;
  }
  refreshInFlight = true;
  try {
    const response = await fetch(withAuth(`${workspaceApiBase}/live`));
    if (!response.ok) {
      showWorkspaceUnavailable(response.status);
      return;
    }
    const data = await response.json();
    if (signInLink) signInLink.hidden = true;
    const worker = data.worker;
    const usage = document.getElementById('watch-usage');
    if (usage) { usage.hidden = !data.latest_run; usage.textContent = usageSummaryText(data.latest_run?.usage); }
    const support = document.getElementById('watch-provider-support');
    if (support) { support.textContent = failureSupportText(data.latest_run); support.hidden = !support.textContent || data.can_manage_workspace !== true; }
    refreshWorkPauses(data);
    syncAskChat(data);
    filesToggle.disabled = false; menuToggle.disabled = false;
    currentWorkerState = String(worker.state || '').trim().toLowerCase();
    const runtime = data.runtime_details || {};
    const displayState = displayStateForLive(data);
    currentDisplayState = displayState;
    currentProjectTitle = String(data.project_title || worker.project_id || projectId || 'Project');
    currentDeliverable = data.deliverable || null;

    const profileLabel = { 'codex-cli': 'Codex', 'claude-code': 'Claude Code',
      'grok-build': 'Grok Build', 'openclaw-general': 'OpenClaw',
      'direct-openai': 'OpenAI Direct', 'direct-anthropic': 'Anthropic Direct',
      'direct-xai': 'Grok Direct' }[worker.profile] || worker.profile || 'AI worker';
    const shared = data.execution_workspace_mode === 'shared';
    title.textContent = String(worker.name || currentProjectTitle || 'Worker');
    subtitle.textContent = `${profileLabel} · ${shared ? 'Shared workspace' : 'Separate workspace'}`;
    if (addWorkerButton) addWorkerButton.hidden = !shared || data.can_manage_workspace !== true;
    syncNativeControls(data);
    syncDocumentTitle(currentProjectTitle, worker.name, ownerResponseNeeded(data));
    statePill.textContent = displayStateLabel(displayState);
    statePill.dataset.state = displayState;
    const timing = watchTimingModel(data.latest_run);
    if (runTime) runTime.hidden = !timing;
    if (timing) { runTimeLabel.textContent = timing.label; runTimeValue.textContent = timing.value; }
    syncRunToggle(displayState === 'completed' ? displayState : currentWorkerState, data.latest_run?.state);

    currentDesktopAvailable = Boolean(runtime.view_available || runtime.view_url);
    currentDesktopCanStart = runtime.desktop_supported === true && data.can_manage_workspace === true
      && !['terminating', 'termination_failed', 'terminated'].includes(String(worker.close_state || worker.state || ''))
      && !(worker.state === 'paused' && ['absent', 'missing', 'stopped', 'compute_unavailable'].includes(String(runtime.sandbox_state || '')));
    currentNoRunActivity = noRunActivityFor(data);
  currentDesktopUrl = currentDesktopAvailable ? withUiRev(withAuth(`${uiBase}/desktop/${workerId}`)) : '';
    currentTerminalUrl = withAuth(terminalViewUrl(runtimeBase, workerId, String(data.latest_run?.run_id || ''),
      String(data.latest_run?.state || ''), String(data.latest_run?.active_attempt_id || '')));

    renderOutput(data);
    syncSteerAvailability(displayState);
    latestFileActivityKey = JSON.stringify([
      displayState,
      ...(data.artifacts?.items || []).map((item) => [item.path, item.size, item.revision]),
    ]);
    if (!filesPanel.hidden && latestFileActivityKey !== listedFileActivityKey) {
      listedFileActivityKey = latestFileActivityKey;
      void workspaceFiles.refresh();
    }
    await maybePromoteDeliverable(data);
    syncMenuLabels();
    syncWatchMenuAvailability(data);
    syncWatchLibrarySnapshot(data);
    // Set the enclosing details state before attaching its iframe, and keep that
    // state stable across refreshes so its browsing context is not reinitialized.
    syncTerminalResultPresentation(data);
    setSurface(activeSurface, { force: false });
    if (!(activeSurface === 'desktop' && !currentDesktopAvailable)) {
      setOverlay(
        currentWorkerState === 'terminated' ? 'terminated' : displayState,
        displayState === 'paused'
          ? 'Use Resume to continue the same workspace.'
          : activeSurface === 'desktop'
            ? 'The desktop will attach automatically when the workspace is ready.'
            : 'Live activity will appear here when the worker is ready.'
      );
    }
    syncWatchLayout();
  } finally {
    refreshInFlight = false;
    scheduleRefresh();
  }
}

for (const button of document.querySelectorAll('[data-action]')) {
  button.addEventListener('click', async () => {
    const action = button.getAttribute('data-action');
    if (action === 'terminate') {
      closeMenu(); closeWorkspaceDialog.showModal(); closeWorkspaceCancel.focus(); return;
    }
    if (action === 'files') {
      closeMenu(); filesPanel.hidden = false;
      filesToggle.setAttribute('aria-expanded', 'true');
      listedFileActivityKey = latestFileActivityKey;
      void workspaceFiles.open(); filesClose.focus();
      return;
    }
    try {
      if (['pause', 'resume', 'interrupt', 'terminate'].includes(action)) {
        await postAction(`action/${action}`.replace('action/action/', 'action/'));
      } else {
        await postAction(`action:${action}`);
      }
      actionFailure = null;
      closeMenu();
      await refresh();
    } catch (error) {
      showActionFailure(error);
    }
  });
}

closeWorkspaceDialog?.addEventListener('cancel', (event) => {
  if (closeWorkspaceConfirm.disabled) event.preventDefault();
});
closeWorkspaceCancel?.addEventListener('click', () => closeWorkspaceDialog.close());
closeWorkspaceDialog?.addEventListener('close', () => menuToggle.focus());
closeWorkspaceConfirm?.addEventListener('click', async () => {
  if (closeWorkspaceConfirm.disabled) return;
  closeWorkspaceConfirm.disabled = true;
  closeWorkspaceCancel.disabled = true;
  try {
    await postAction('action/terminate');
    closeWorkspaceDialog.close(); actionFailure = null; await refresh();
  } catch (error) {
    closeWorkspaceDialog.close(); showActionFailure(error);
  } finally {
    closeWorkspaceConfirm.disabled = false; closeWorkspaceCancel.disabled = false;
  }
});

filesToggle?.addEventListener('click', () => {
  filesPanel.hidden = !filesPanel.hidden;
  pendingLibraryTab = '';
  filesToggle.setAttribute('aria-expanded', String(!filesPanel.hidden));
  if (!filesPanel.hidden) {
    listedFileActivityKey = latestFileActivityKey;
    let preferredTab = 'files';
    try { const saved = localStorage.getItem('xperfect.display.library-tab'); if (['files', 'added', 'saved'].includes(saved)) preferredTab = saved; } catch { /* Files is the first-use default. */ }
    pendingLibraryTab = preferredTab !== 'files' && !currentLibraryOwnerScope() ? preferredTab : '';
    selectLibraryTab(preferredTab);
    void workspaceFiles.open(); filesClose?.focus();
  }
});
filesClose?.addEventListener('click', () => {
  pendingLibraryTab = '';
  filesPanel.hidden = true;
  filesToggle?.setAttribute('aria-expanded', 'false');
  filesToggle?.focus();
});
filesAttach?.addEventListener('click', attachPendingUploads);
// A parent overlay receives file drags before the desktop iframe can intercept them.
const desktopFileOverlay = document.getElementById('watch-desktop-file-drop');
let dragDepth = 0;
document.addEventListener('dragenter', (event) => {
  if (filesCanWrite === false) return;
  if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
  dragDepth += 1; desktopFileOverlay.hidden = false;
});
document.addEventListener('dragleave', (event) => {
  if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) desktopFileOverlay.hidden = true;
});
document.addEventListener('dragover', (event) => {
  if (Array.from(event.dataTransfer?.types || []).includes('Files')) event.preventDefault();
});
desktopFileOverlay.addEventListener('drop', (event) => {
  event.preventDefault(); dragDepth = 0; desktopFileOverlay.hidden = true;
  if (filesCanWrite === false) {
    filesPanel.hidden = false; filesToggle.setAttribute('aria-expanded', 'true');
    void workspaceFiles.open();
    fileError('This workspace view can download files. Editing requires a workspace member.');
  } else queueWatchDrop(event.dataTransfer);
});
document.addEventListener('drop', (event) => {
  if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
  event.preventDefault(); dragDepth = 0; desktopFileOverlay.hidden = true;
  if (event.target !== desktopFileOverlay && !event.target.closest('#watch-file-drop')) {
    if (filesCanWrite === false) {
      filesPanel.hidden = false; filesToggle.setAttribute('aria-expanded', 'true');
      void workspaceFiles.open();
      fileError('This workspace view can download files. Editing requires a workspace member.');
    } else queueWatchDrop(event.dataTransfer);
  }
});

surfaceTerminalButton.addEventListener('click', () => {
  closeMenu();
  if (terminalResultPreferred) closeResultPanel();
  setSurface('terminal', { force: true });
});

surfaceDesktopButton.addEventListener('click', async () => {
  closeMenu();
  if (!currentDesktopAvailable && currentDesktopCanStart) {
    surfaceDesktopButton.disabled = true;
    try {
      await postAction('action:browser', { url: 'about:blank' });
      await refresh();
    } catch (error) {
      showActionFailure(error);
      return;
    } finally {
      surfaceDesktopButton.disabled = false;
    }
  }
  if (terminalResultPreferred) closeResultPanel();
  setSurface('desktop', { force: true });
});

terminalLog?.addEventListener('toggle', () => {
  if (!terminalLog.open) {
    clearRetryTimers();
    if (!frameReady) attachStartedAt = 0;
    return;
  }
  setSurface(activeSurface, { force: !frameReady && !attachStartedAt });
});

frame.addEventListener('load', () => {
  // src is the requested address, not the document that emitted this event. A late
  // about:blank or prior-surface load must not cancel recovery for the current view.
  if (!lastAttachedUrl || frame.src !== lastAttachedUrl) return;
  try {
    const loaded = frame.contentDocument;
    if (!loaded || loaded.readyState !== 'complete' || loaded.location.href !== lastAttachedUrl) return;
  } catch { return; }
  frameReady = true;
  attachStartedAt = 0;
  clearRetryTimers();
  setOverlay(currentDisplayState);
});

menuToggle.addEventListener('click', () => {
  if (menu.hidden) openMenu(); else closeMenu();
});
menuToggle.addEventListener('keydown', (event) => {
  if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
  event.preventDefault();
  openMenu(event.key === 'ArrowUp');
});
menu.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    event.preventDefault(); event.stopPropagation(); closeMenu({ restoreFocus: true }); return;
  }
  if (event.key === 'Tab') { closeMenu(); return; }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  const items = watchMenuItems();
  if (!items.length) return;
  event.preventDefault();
  const index = items.indexOf(document.activeElement);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
    : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
  items[next].focus();
});
window.addEventListener('resize', syncWatchLayout);

resultToggle.addEventListener('click', () => {
  if (resultPanel.hidden) {
    openResultPanel();
  } else {
    closeResultPanel();
  }
});

resultClose.addEventListener('click', () => {
  closeResultPanel({ restoreFocus: true });
});

document.addEventListener('click', (event) => {
  const target = event.target;
  if (!(target instanceof Node)) return;
  if (!menu.hidden && !menu.contains(target) && !menuToggle.contains(target)) {
    closeMenu();
  }
  if (!terminalResultPreferred && !resultPanel.hidden && !resultPanel.contains(target) && !resultToggle.contains(target)) {
    closeResultPanel();
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !event.defaultPrevented && !document.querySelector('dialog[open]')) {
    if (!menu.hidden) closeMenu({ restoreFocus: true });
    else if (!filesPanel.hidden) filesClose?.click();
    else if (!resultPanel.hidden) closeResultPanel({ restoreFocus: true });
  }
  setQueueModifierActive(Boolean(event.metaKey || event.ctrlKey));
});

document.addEventListener('keyup', (event) => {
  setQueueModifierActive(Boolean(event.metaKey || event.ctrlKey));
});

window.addEventListener('blur', () => {
  setQueueModifierActive(false);
  clearLongPress();
});

openExternal.addEventListener('click', () => {
  closeMenu();
  window.open(currentSurfaceUrl(), '_blank', 'noopener,noreferrer');
});

for (const button of [openProjectWorkspace]) {
  button.addEventListener('click', () => {
    closeMenu();
    window.location.assign('/#workspaces');
  });
}

openTerminalLink.addEventListener('click', () => {
  closeMenu();
  window.open(currentTerminalUrl, '_blank', 'noopener,noreferrer');
});

steerInput.addEventListener('input', autoResizeSteerInput);

steerInput.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey) return;
  if (event.metaKey || event.ctrlKey) {
    event.preventDefault();
    submitFooterInstruction('queue');
    return;
  }
  event.preventDefault();
  submitFooterInstruction('steer');
});

sendButton.addEventListener('pointerdown', (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey) return;
  clearLongPress();
  longPressTimer = window.setTimeout(() => {
    longPressTimer = 0;
    longPressArmed = true;
    syncSendAffordance();
  }, LONG_PRESS_MS);
});

for (const eventName of ['pointercancel', 'pointerleave']) {
  sendButton.addEventListener(eventName, () => {
    clearLongPress();
  });
}

document.addEventListener('visibilitychange', () => {
  scheduleRefresh(0);
});

sendButton.addEventListener('pointerup', (event) => {
  if (!longPressArmed) {
    clearLongPress();
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  suppressNextClick = true;
  clearLongPress();
  submitFooterInstruction('queue');
});

sendButton.addEventListener('click', (event) => {
  if (suppressNextClick) {
    suppressNextClick = false;
    event.preventDefault();
    return;
  }
  if (event.metaKey || event.ctrlKey) {
    event.preventDefault();
    submitFooterInstruction('queue');
  }
});

steerForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  await submitFooterInstruction('steer');
});

syncWatchMenuAvailability(null);
  syncWatchLibrarySnapshot(null);
syncMenuLabels();
syncProjectWorkspaceLinks();
syncSendAffordance();
syncRunToggle('starting');
syncSteerAvailability('starting');
autoResizeSteerInput();
refresh().catch(() => {});
