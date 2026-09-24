import { watchOutputModel } from './delivery-presenter.js?v=20260923closed1';
import { workspaceLifecycleControl } from './launch-policy.js?v=20260811m';
import { createFileDraft, createWorkspaceFiles } from './files.js?v=20260923readable1';
import { attachNativeControls } from './native-controls.js?v=20260922o03c';

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
const overlayLabel = document.querySelector('#stage-overlay .overlay-label');
const overlayTitle = document.getElementById('overlay-title');
const overlayDetail = document.getElementById('overlay-detail');
const signInLink = document.getElementById('watch-sign-in');
const stageResultText = document.getElementById('stage-result-text');
const title = document.getElementById('watch-title');
const subtitle = document.getElementById('watch-subtitle');
const latestOutputInline = document.getElementById('latest-output-inline');
const latestOutputFull = document.getElementById('latest-output-full');
const latestOutputHuman = document.getElementById('latest-output-human');
const latestOutputTechnical = document.getElementById('latest-output-technical');
const resultTechnical = document.getElementById('result-technical');
let actionFailure = null;
const resultActions = document.getElementById('result-actions');
const artifactList = document.getElementById('artifact-list');
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
const openProjectWorkspaceMenu = document.getElementById('open-project-workspace-menu');
const openclawActionButton = document.querySelector('[data-action="openclaw"]');
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
let currentTerminalUrl = withAuth(`${runtimeBase}/ui/workers/${workerId}/terminal`);
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
let currentProjectTitle = projectId || 'Project';
let currentDeliverable = null;
let currentDesktopAvailable = false;
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
  const prefix = 'glasshive_csrf=';
  const entry = document.cookie
    .split(';')
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix));
  if (!entry) return '';
  const value = entry.slice(prefix.length);
  try {
    return decodeURIComponent(value);
  } catch (_error) {
    return value;
  }
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
  if (normalized === 'completed') return 'Completed';
  if (normalized === 'idle_terminated') return 'Idle stopped';
  if (normalized === 'terminated') return 'Closed';
  if (normalized === 'terminating') return 'Closing';
  if (normalized === 'termination_failed') return 'Close needs attention';
  return normalized || 'starting';
}

function clearRetryTimers() {
  for (const timer of retryTimers) window.clearTimeout(timer);
  retryTimers = [];
}

function closeMenu() {
  menu.hidden = true;
  menuToggle.setAttribute('aria-expanded', 'false');
}

function closeResultPanel() {
  actionFailure = null;
  resultPanel.hidden = true;
  resultToggle.setAttribute('aria-expanded', 'false');
  const kind = latestOutputFull.hidden ? 'status' : 'result';
  resultToggle.setAttribute('aria-label', `Open latest workspace ${kind}`);
  if (resultToggleAction) resultToggleAction.textContent = `Open ${kind}`;
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
  frame.src = 'about:blank';
  window.setTimeout(() => {
    frame.src = url;
  }, 180);
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

function attachView(url) {
  if (!url) return;
  if (lastAttachedUrl === url && frame.src === url) return;
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
  openProjectWorkspaceMenu.hidden = !available;
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

function syncRunToggle(state) {
  if (!runToggleButton) return;
  const normalized = String(state || '').trim().toLowerCase();
  const control = workspaceLifecycleControl(normalized);
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
  steerInput.placeholder = closed ? 'This workspace is closed' : 'Steer this workspace';
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
  steerInput.style.height = `${Math.min(Math.max(steerInput.scrollHeight, 52), 156)}px`;
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

function syncMenuLabels() {
  surfaceTerminalButton.dataset.active = String(activeSurface === 'terminal');
  surfaceDesktopButton.dataset.active = String(activeSurface === 'desktop');
  openExternal.textContent = activeSurface === 'desktop'
      ? 'Open current desktop in new tab'
      : 'Open current session in new tab';
}

function setSurface(surface, { force = false } = {}) {
  activeSurface = surface === 'desktop' ? 'desktop' : 'terminal';
  syncMenuLabels();
  stageResultText.hidden = true;
  const state = currentDisplayState;
  if (currentWorkerState === 'terminated') {
    clearAttachedView();
    setOverlay('terminated');
    return;
  }
  if (['created', 'starting', 'paused', 'idle', 'idle_terminated', 'stopped', 'terminating', 'termination_failed', 'terminated'].includes(state) || TERMINAL_ATTENTION_STATES.has(state)) {
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
      stageResultText.textContent = currentResultText;
      stageResultText.hidden = false;
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
    attachView(url);
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
    panelTitle: 'Grok needs your response',
    summary: data.native_control.read_only === true
      ? 'Grok is waiting for a workspace member to respond.'
      : 'Grok is waiting for your response.',
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
  for (const action of actions) {
    const link = document.createElement('a');
    link.className = `result-action${action.primary ? ' primary' : ''}`;
    link.href = action.url;
    link.textContent = action.label;
    if (action.download) {
      link.download = '';
    } else {
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    }
    resultActions.appendChild(link);
  }
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
  if (!files.length) return;

  const heading = document.createElement('div');
  heading.className = 'artifact-list-heading';
  heading.textContent = files.length === 1 ? 'Workspace file' : `Workspace files (${files.length})`;
  artifactList.appendChild(heading);

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
  onAccess: (canWrite) => {
    filesCanWrite = canWrite;
    watchDraftActivation = activateWatchDraft(canWrite);
  },
});
const watchDraft = createFileDraft({
  input: filesInput, folderInput: filesFolderInput,
  drop: filesDrop, list: filesUploads, help: filesHelp,
  budget: filesBudget, csrf: currentCsrfToken, scope: `watch.${workerId}`,
  quietReady: true, dropOpensPicker: false,
  onChange: ({ readyIds }) => { filesAttach.hidden = !readyIds.length; },
  onReady: async (item, ownerScope) => {
    if (ownerScope !== watchDraft.ownerScope()) return;
    try {
      await workspaceFiles.attach([item.upload_id]);
      if (ownerScope !== watchDraft.ownerScope()) return;
      watchDraft.dismissReady(item.upload_id);
      filesAttach.hidden = !watchDraft.readyIds().length;
    } catch (error) { if (ownerScope === watchDraft.ownerScope()) fileError(`${item.name} was not added: ${error.message}. Retry below.`); }
  },
});
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
    setWatchDraftBusy(false);
    watchDraft.setOwnerScope(null);
    filesUploadArea.hidden = true;
    return;
  }
  const controller = new AbortController();
  watchDraftScopeController = controller;
  setWatchDraftBusy(true);
  if (!hadOwnerScope) filesUploadArea.hidden = true;
  try {
    const response = await fetch('/api/bootstrap', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`Account check failed (${response.status}).`);
    const data = await response.json();
    if (load !== watchDraftScopeLoad || !filesCanWrite) return;
    watchDraft.setOwnerScope(data.draft_owner_scope);
    filesUploadArea.hidden = !watchDraft.ownerScope();
    if (!watchDraft.ownerScope()) fileError('Your account could not be verified. Refresh to add files.');
  } catch (error) {
    if (load !== watchDraftScopeLoad) return;
    watchDraft.setOwnerScope(null);
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
  latestOutputFull.textContent = output.result || '';
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
  actionFailure = { label: 'Action not confirmed', panelTitle: 'Action needs attention',
    summary: 'The action could not be confirmed. Check the workspace status before trying again.',
    result: '', technical: String(error?.message || error || 'Unknown error') };
  renderOutputContent(actionFailure);
  resultTechnical.open = false;
  openResultPanel();
}

function renderOutput(data) {
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
  const completed = state === 'completed';
  const needsAttention = TERMINAL_ATTENTION_STATES.has(state);
  const connecting = !completed && attachStartedAt && !frameReady && Date.now() - attachStartedAt < 12000;
  const filePreviewActive = activeSurface === 'desktop' && Boolean(filePreviewUrl());
  const waiting = state === 'starting' || state === 'paused' || state === 'idle' || state === 'idle_terminated' || state === 'stopped' || state === 'terminating' || state === 'termination_failed' || state === 'terminated' || needsAttention || connecting;
  overlay.hidden = !waiting;
  if (stage) {
    stage.dataset.overlayActive = String(waiting);
  }
  if (!waiting) return;

  if (needsAttention) {
    if (overlayLabel) {
      overlayLabel.textContent = 'Workspace needs attention';
    }
    overlayTitle.textContent = 'Workspace needs attention';
    overlayDetail.textContent = 'The latest run did not complete. Review the status details, then send a corrected follow-up.';
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
    stageResultText.hidden = !savedResult;
    if (savedResult) stageResultText.textContent = currentResultText;
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
    overlayDetail.textContent = 'The last run is complete and compute is stopped to save resources. Send follow-up work and xPerfect will resume this workspace automatically.';
    return;
  }

  if (connecting) {
    if (overlayLabel) {
      overlayLabel.textContent = filePreviewActive ? 'Delivered file' : 'Workspace attaching';
    }
    overlayTitle.textContent = filePreviewActive
      ? 'Opening delivered file…'
      : activeSurface === 'desktop'
        ? 'Attaching live workspace…'
        : 'Attaching exact live session…';
    overlayDetail.textContent = filePreviewActive
      ? 'The completed file preview is loading. Use Open delivered file in new tab if this takes more than a few seconds.'
      : activeSurface === 'desktop'
        ? 'The desktop is waking up. If it takes more than a few seconds, open the current desktop in a new tab.'
      : 'We are connecting to the exact running session. If it takes more than a few seconds, open the current session in a new tab.';
    return;
  }

  if (overlayLabel) {
    overlayLabel.textContent = 'Workspace warming up';
  }
  overlayTitle.textContent = filePreviewActive
    ? 'Opening delivered file…'
    : activeSurface === 'desktop'
      ? 'Preparing live workspace…'
      : 'Preparing exact live session…';
  overlayDetail.textContent = detail || (filePreviewActive
    ? 'The completed file preview will appear here automatically.'
    : activeSurface === 'desktop'
      ? 'The desktop will attach automatically when the workspace is ready.'
    : 'The exact workspace session will appear here as soon as the workspace is ready.');
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
  stageResultText.hidden = true;
  clearAttachedView();
  title.textContent = heading;
  subtitle.textContent = message;
  statePill.textContent = code === 401 ? 'Sign-in needed' : 'Unavailable';
  actionFailure = null;
  renderOutputContent({
    label: heading, panelTitle: heading,
    summary: message, result: message, technical: '',
  });
  syncResultActions(null);
  syncArtifactList([]);
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
    currentWorkerState = String(worker.state || '').trim().toLowerCase();
    const runtime = data.runtime_details || {};
    const displayState = displayStateForLive(data);
    currentDisplayState = displayState;
    currentProjectTitle = String(data.project_title || worker.project_id || projectId || 'Project');
    currentDeliverable = data.deliverable || null;

    title.textContent = currentProjectTitle || 'Workspace live view';
    subtitle.textContent = `${worker.profile || 'worker'} workspace · ${displayStateLabel(displayState)}`;
    if (openclawActionButton) {
      openclawActionButton.hidden = !String(worker.profile || '').startsWith('openclaw');
    }
    if (String(worker.profile || '') === 'grok-build' && !signedToken && !nativeControls && nativeControlsMount) {
      nativeControls = attachNativeControls(nativeControlsMount, workerId, {
        getJson: nativeGetJson,
        postJson: nativePostJson,
      });
    }
    nativeControls?.updateLive(data?.native_control || null);
    syncDocumentTitle(currentProjectTitle, worker.name, ownerResponseNeeded(data));
    statePill.textContent = displayStateLabel(displayState);
    syncRunToggle(displayState === 'completed' ? displayState : currentWorkerState);

    currentDesktopAvailable = Boolean(runtime.view_available || runtime.view_url);
  currentDesktopUrl = currentDesktopAvailable ? withUiRev(withAuth(`${uiBase}/desktop/${workerId}`)) : '';
    currentTerminalUrl = withAuth(`${runtimeBase}/ui/workers/${workerId}/terminal`);

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
    setSurface(activeSurface, { force: false });
    if (!(activeSurface === 'desktop' && !currentDesktopAvailable)) {
      setOverlay(
        currentWorkerState === 'terminated' ? 'terminated' : displayState,
        displayState === 'paused'
          ? 'Use Resume to continue the same workspace.'
          : activeSurface === 'desktop'
            ? 'The desktop will attach automatically when the workspace is ready.'
            : 'The exact workspace session will attach automatically when the workspace is ready.'
      );
    }
  } finally {
    refreshInFlight = false;
    scheduleRefresh();
  }
}

for (const button of document.querySelectorAll('[data-action]')) {
  button.addEventListener('click', async () => {
    const action = button.getAttribute('data-action');
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

filesToggle?.addEventListener('click', () => {
  filesPanel.hidden = !filesPanel.hidden;
  filesToggle.setAttribute('aria-expanded', String(!filesPanel.hidden));
  if (!filesPanel.hidden) {
    listedFileActivityKey = latestFileActivityKey;
    void workspaceFiles.open(); filesClose?.focus();
  }
});
filesClose?.addEventListener('click', () => {
  filesPanel.hidden = true;
  filesToggle?.setAttribute('aria-expanded', 'false');
  filesToggle?.focus();
});
filesAttach?.addEventListener('click', async () => {
  const pending = watchDraft.readyIds();
  const ownerScope = watchDraft.ownerScope();
  if (!ownerScope || !pending.length) return;
  try {
    await workspaceFiles.attach(pending);
    if (ownerScope !== watchDraft.ownerScope()) return;
    pending.forEach((id) => watchDraft.dismissReady(id));
    filesAttach.hidden = true;
  } catch (error) { if (ownerScope === watchDraft.ownerScope()) fileError(error.message); }
});
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
  setSurface('terminal', { force: true });
});

surfaceDesktopButton.addEventListener('click', () => {
  closeMenu();
  setSurface('desktop', { force: true });
});

frame.addEventListener('load', () => {
  if (!frame.src || frame.src === 'about:blank') return;
  frameReady = true;
  attachStartedAt = 0;
  if (!['paused', 'idle', 'idle_terminated', 'stopped', 'terminating', 'termination_failed', 'terminated'].includes(currentDisplayState)) {
    overlay.hidden = true;
    if (stage) {
      stage.dataset.overlayActive = 'false';
    }
  }
});

menuToggle.addEventListener('click', () => {
  const open = menu.hidden;
  menu.hidden = !open;
  menuToggle.setAttribute('aria-expanded', String(open));
  if (open) {
    const firstMenuItem = menu.querySelector('button:not([hidden])');
    if (firstMenuItem instanceof HTMLElement) {
      firstMenuItem.focus();
    }
  }
});

resultToggle.addEventListener('click', () => {
  if (resultPanel.hidden) {
    openResultPanel();
  } else {
    closeResultPanel();
  }
});

resultClose.addEventListener('click', () => {
  closeResultPanel();
});

document.addEventListener('click', (event) => {
  const target = event.target;
  if (!(target instanceof Node)) return;
  if (!menu.hidden && !menu.contains(target) && !menuToggle.contains(target)) {
    closeMenu();
  }
  if (!resultPanel.hidden && !resultPanel.contains(target) && !resultToggle.contains(target)) {
    closeResultPanel();
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeMenu();
    closeResultPanel();
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

for (const button of [openProjectWorkspace, openProjectWorkspaceMenu]) {
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

syncMenuLabels();
syncProjectWorkspaceLinks();
syncSendAffordance();
syncRunToggle('starting');
syncSteerAvailability('starting');
autoResizeSteerInput();
refresh().catch(() => {});
