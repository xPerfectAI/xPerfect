const workerId = window.location.pathname.split('/').filter(Boolean).at(-1);
const params = new URLSearchParams(window.location.search);
const signedToken = params.get('gh_token') || '';
const viewOnly = params.get('preview') === '1';

const stage = document.getElementById('desktop-stage');
const overlay = document.getElementById('desktop-overlay');
const statusEl = document.getElementById('desktop-status');
const detailEl = document.getElementById('desktop-detail');
const workspaceStatusLink = document.getElementById('workspace-status-link');
const clipboardStatus = document.getElementById('clipboard-status');

let rfb = null;
let currentWsUrl = '';
let currentPassword = '';
let clipboardInterval = null;
let lastLocalClipboard = '';
let lastRemoteClipboard = '';
let rfbImportAttempt = 0;
let desktopRefreshTimer = 0;
let desktopRefreshInFlight = false;
let desktopRefreshDelayMs = 5000;
let latestLivePayload = null;
let settledDesktopSuppressed = false;

const ACTIVE_RUN_STATES = new Set(['queued', 'running']);
const SETTLED_RUN_STATES = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
const ACTIVE_WORKER_STATES = new Set(['created', 'starting', 'ready', 'running', 'resuming', 'interrupting']);
const PARKED_WORKER_STATES = new Set(['paused', 'terminating', 'termination_failed', 'terminated', 'failed']);

function normalizedState(value) {
  return String(value || '').trim().toLowerCase();
}

function withAuth(url) {
  if (!signedToken || /(?:^|[?&])gh_token=/.test(String(url || ''))) return url;
  return `${url}${url.includes('?') ? '&' : '?'}gh_token=${encodeURIComponent(signedToken)}`;
}

function setStatus(title, detail, { hideOverlay = false, showWorkspaceLink = false } = {}) {
  statusEl.textContent = title;
  detailEl.textContent = detail;
  if (workspaceStatusLink) workspaceStatusLink.hidden = !showWorkspaceLink;
  overlay.hidden = hideOverlay;
}

function setClipboardStatus(text) {
  clipboardStatus.textContent = text;
}

function buildDesktopTitle(workerName, projectTitle) {
  const safeWorker = String(workerName || 'Worker').trim() || 'Worker';
  const safeProject = String(projectTitle || 'Project').trim() || 'Project';
  document.title = `xPerfect | ${safeWorker} - ${safeProject}`;
}

function refreshDelayForLiveState(data) {
  if (document.hidden) return 15000;
  const workerState = normalizedState(data?.worker?.close_state || data?.worker?.state);
  const runState = normalizedState(data?.latest_run?.state);
  return ACTIVE_RUN_STATES.has(runState) || ['created', 'starting', 'resuming', 'interrupting'].includes(workerState)
    ? 5000
    : 15000;
}

function viewHealthHealthy(data) {
  const healthy = data?.runtime_details?.view_health?.healthy;
  return typeof healthy === 'boolean' ? healthy : null;
}

function isSettledWorkspaceState(data) {
  const workerState = normalizedState(data?.worker?.close_state || data?.worker?.state);
  const runState = normalizedState(data?.latest_run?.state);
  if (ACTIVE_RUN_STATES.has(runState)) {
    return false;
  }
  if (SETTLED_RUN_STATES.has(runState)) {
    return true;
  }
  if (ACTIVE_WORKER_STATES.has(workerState)) {
    return false;
  }
  return PARKED_WORKER_STATES.has(workerState);
}

function settledWorkspaceStatus(data) {
  const workerState = normalizedState(data?.worker?.close_state || data?.worker?.state);
  const runState = normalizedState(data?.latest_run?.state);
  const hasFiles = Boolean(data?.artifacts?.items?.length || data?.deliverable);

  if (['terminating', 'termination_failed', 'terminated'].includes(workerState)) {
    const closing = workerState === 'terminating';
    const closeFailed = workerState === 'termination_failed';
    return {
      title: closing ? 'Workspace closing' : closeFailed ? 'Close needs attention' : 'Workspace closed',
      detail: closing
        ? 'xPerfect is safely stopping this workspace.'
        : closeFailed
        ? 'xPerfect could not confirm compute shutdown. Retry Close workspace; new work remains blocked.'
        : 'This workspace was closed. Return to Workspaces to create new work.',
    };
  }
  if (runState === 'completed') {
    return {
      title: 'Workspace complete',
      detail: hasFiles
        ? 'The latest output and workspace files are available from the status panel. Continue this workspace when you want fresh compute.'
        : 'The latest result is available from the status panel. Continue this workspace when you want fresh compute.',
    };
  }
  if (workerState === 'paused') {
    return {
      title: 'Workspace paused',
      detail: 'Compute is stopped for this workspace. Resume or send follow-up work to reattach a fresh desktop session.',
    };
  }
  if (['failed', 'cancelled', 'interrupted'].includes(runState) || workerState === 'failed') {
    return {
      title: 'Workspace needs attention',
      detail: 'The live desktop is not running. Open the status panel for the last result and blocker details.',
    };
  }
  return {
    title: 'Desktop unavailable',
    detail: 'The live desktop is not running for this workspace. Open the status panel for the latest result.',
  };
}

function showSettledWorkspaceStatus(data) {
  const status = settledWorkspaceStatus(data);
  stopClipboardSync();
  setClipboardStatus('Clipboard sync: inactive until workspace resumes');
  setStatus(status.title, status.detail, { showWorkspaceLink: true });
}

if (workspaceStatusLink) {
  workspaceStatusLink.href = withAuth(`/watch/${encodeURIComponent(workerId)}?surface=desktop`);
}

function scheduleDesktopRefresh(delayMs = desktopRefreshDelayMs) {
  if (desktopRefreshTimer) window.clearTimeout(desktopRefreshTimer);
  desktopRefreshTimer = window.setTimeout(() => {
    void connectDesktop();
  }, delayMs);
}

function stopClipboardSync() {
  if (clipboardInterval) {
    window.clearInterval(clipboardInterval);
    clipboardInterval = null;
  }
}

async function pushLocalClipboardToRemote(text) {
  if (!rfb || !text || text === lastRemoteClipboard) return;
  try {
    rfb.clipboardPasteFrom(text);
    lastLocalClipboard = text;
    setClipboardStatus('Clipboard sync: local → sandbox');
  } catch (error) {
    console.debug('clipboard paste failed', error);
  }
}

function installClipboardSync() {
  stopClipboardSync();

  window.addEventListener('paste', (event) => {
    const text = event.clipboardData?.getData('text/plain') || '';
    if (!text) return;
    void pushLocalClipboardToRemote(text);
  });

  const tryPollClipboard = async () => {
    if (!navigator.clipboard?.readText) return;
    try {
      const text = await navigator.clipboard.readText();
      if (!text || text === lastLocalClipboard) return;
      lastLocalClipboard = text;
      await pushLocalClipboardToRemote(text);
      setClipboardStatus('Clipboard sync: bi-directional');
    } catch (error) {
      setClipboardStatus('Clipboard sync: click desktop once to enable');
    }
  };

  const armClipboardOnGesture = async () => {
    try {
      await tryPollClipboard();
    } finally {
      window.removeEventListener('pointerdown', armClipboardOnGesture);
      window.removeEventListener('keydown', armClipboardOnGesture);
      clipboardInterval = window.setInterval(() => {
        void tryPollClipboard();
      }, 1500);
    }
  };

  window.addEventListener('pointerdown', armClipboardOnGesture, { once: true });
  window.addEventListener('keydown', armClipboardOnGesture, { once: true });
  void tryPollClipboard();
}

async function connectDesktop() {
  if (desktopRefreshInFlight) {
    scheduleDesktopRefresh(5000);
    return;
  }
  desktopRefreshInFlight = true;
  try {
    const response = await fetch(withAuth(`/api/worker/${workerId}/live`));
    if (!response.ok) {
      setStatus('Desktop unavailable', 'xPerfect could not load the worker runtime details for this sandbox.');
      return;
    }
    const data = await response.json();
    latestLivePayload = data;
    desktopRefreshDelayMs = refreshDelayForLiveState(data);
    const runtime = data.runtime_details || {};
    const viewAvailable = Boolean(runtime.view_available || runtime.view_url);
    const settledWorkspace = isSettledWorkspaceState(data);
    if (!settledWorkspace) settledDesktopSuppressed = false;
    buildDesktopTitle(data.worker?.name, data.project_title);

    if (!viewAvailable) {
      if (settledWorkspace) {
        showSettledWorkspaceStatus(data);
      } else {
        setStatus('Desktop unavailable', 'This worker does not currently expose a live desktop surface.');
      }
      return;
    }
    if (settledWorkspace && (settledDesktopSuppressed || viewHealthHealthy(data) === false)) {
      showSettledWorkspaceStatus(data);
      return;
    }

    const wsScheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsScheme}//${window.location.host}${withAuth(`/novnc/${workerId}/websockify`)}`;
    const credentialResponse = await fetch(
      withAuth(`/api/workspace/${workerId}/desktop-credentials`),
      {
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      },
    );
    if (!credentialResponse.ok) {
      setStatus('Desktop authentication unavailable', 'xPerfect could not securely attach this live desktop. Retry after the worker finishes starting.');
      return;
    }
    const credentialPayload = await credentialResponse.json();
    const password = String(credentialPayload.password || '');

    if (rfb && wsUrl === currentWsUrl && password === currentPassword) {
      return;
    }

    currentWsUrl = wsUrl;
    currentPassword = password;

    setStatus(
      viewOnly ? 'Attaching view-only preview…' : 'Attaching live sandbox…',
      viewOnly
        ? 'xPerfect is showing this workspace without keyboard, mouse, or clipboard control.'
        : 'xPerfect is connecting directly to the worker desktop and enabling clipboard sync.',
    );

    const modulePath = withAuth(`/novnc/${workerId}/core/rfb.js?attempt=${rfbImportAttempt}`);
    let RFB;
    try {
      ({ default: RFB } = await import(modulePath));
    } catch (error) {
      rfbImportAttempt += 1;
      if (settledWorkspace) {
        settledDesktopSuppressed = true;
        showSettledWorkspaceStatus(data);
        console.debug('rfb import skipped for settled workspace', error);
        return;
      }
      setStatus(
        'Desktop reconnecting…',
        'xPerfect is refreshing the live desktop client and will attach again automatically.',
      );
      console.debug('rfb import failed', error);
      return;
    }
    settledDesktopSuppressed = false;

    if (rfb) {
      try {
        rfb.disconnect();
      } catch (error) {
        console.debug('rfb disconnect failed', error);
      }
      stage.replaceChildren();
    }

    rfb = new RFB(stage, wsUrl, {
      credentials: { password },
    });
    rfb.scaleViewport = true;
    rfb.background = '#000';
    rfb.viewOnly = viewOnly;
    rfb.focusOnClick = !viewOnly;
    rfb.showDotCursor = !viewOnly;

    rfb.addEventListener('connect', () => {
      setStatus(
        viewOnly ? 'Live preview' : 'Sandbox connected',
        viewOnly
          ? 'Open the workspace to steer this worker.'
          : 'Click anywhere inside the desktop to steer directly. Clipboard sync is active when the browser allows it.',
        { hideOverlay: true },
      );
      setClipboardStatus(viewOnly ? 'View only' : 'Clipboard sync: bi-directional');
      if (viewOnly) return;
      try {
        rfb.focus();
      } catch (error) {
        console.debug('rfb focus failed', error);
      }
    });

    rfb.addEventListener('disconnect', (event) => {
      rfb = null;
      currentWsUrl = '';
      currentPassword = '';
      stopClipboardSync();
      if (isSettledWorkspaceState(latestLivePayload)) {
        settledDesktopSuppressed = true;
        showSettledWorkspaceStatus(latestLivePayload);
        return;
      }
      setStatus(
        event.detail.clean ? 'Desktop disconnected' : 'Desktop reconnecting…',
        event.detail.clean
          ? 'The sandbox desktop session ended. Reload the page or reopen the worker if needed.'
          : 'The desktop connection dropped. xPerfect will retry automatically.',
        { hideOverlay: false },
      );
    });

    rfb.addEventListener('clipboard', async (event) => {
      if (viewOnly) return;
      const text = String(event.detail?.text || '');
      if (!text) return;
      lastRemoteClipboard = text;
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
        }
        setClipboardStatus('Clipboard sync: bi-directional');
      } catch (error) {
        setClipboardStatus('Clipboard sync: sandbox → local blocked by browser');
      }
    });

    if (!viewOnly) installClipboardSync();
  } finally {
    desktopRefreshInFlight = false;
    scheduleDesktopRefresh();
  }
}

document.addEventListener('visibilitychange', () => {
  scheduleDesktopRefresh(0);
});

void connectDesktop();
