function boundedSummary(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  return text.length <= 600 ? text : `${text.slice(0, 597)}...`;
}

function actionModel(value, fallbackLabel = 'Delivery') {
  if (!value || typeof value !== 'object') return null;
  const openUrl = String(value.open_url || '').trim();
  const downloadUrl = String(value.download_url || '').trim();
  if (!openUrl && !downloadUrl) return null;
  return {
    label: String(value.path || value.label || value.workspace_path || fallbackLabel).trim() || fallbackLabel,
    contentType: String(value.content_type || '').trim().toLowerCase(),
    openUrl,
    downloadUrl,
  };
}

function referenceBasename(value) {
  if (!value || typeof value !== 'object') return '';
  for (const candidate of [value.path, value.label, value.workspace_path, value.browser_url]) {
    const text = String(candidate || '').trim().split(/[?#]/, 1)[0].replaceAll('\\', '/');
    const basename = text.split('/').filter(Boolean).pop() || '';
    if (basename) return basename;
  }
  return '';
}

function referencePaths(value) {
  if (!value || typeof value !== 'object') return [];
  const paths = [];
  for (const [candidate, structuredPath] of [
    [value.workspace_path, true],
    [value.path, true],
    [value.label, false],
  ]) {
    const normalized = String(candidate || '')
      .trim()
      .split(/[?#]/, 1)[0]
      .replaceAll('\\', '/')
      .replace(/^\.\//, '')
      .replace(/^\/+/, '');
    if (normalized && (structuredPath || normalized.includes('/'))) paths.push(normalized);
  }
  return [...new Set(paths)];
}

function structuredFailureSummary(data) {
  return String(data?.latest_run?.state || '').trim().toLowerCase() === 'failed'
    && Number(data?.latest_run?.failure_structured) === 1
    ? boundedSummary(data?.latest_run?.failure_user_message) : '';
}

export function workspaceDeliveryModel(data) {
  const state = String(data?.latest_run?.state || '').trim().toLowerCase();
  const summary = boundedSummary(data?.latest_output);
  if (state !== 'completed') {
    const progress = workspaceProgressModel({ runState: state,
      workerState: data?.worker?.close_state || data?.worker?.state });
    return { available: false, state, summary: structuredFailureSummary(data) || progress.summary,
      primary: null, artifacts: [] };
  }

  const declaredPrimary = actionModel(data?.deliverable, 'Delivered result');
  const seen = new Set();
  const artifacts = [];
  const artifactReferences = new Map();
  for (const item of Array.isArray(data?.artifacts?.items) ? data.artifacts.items : []) {
    const action = actionModel(item, 'Delivered file');
    if (!action) continue;
    const key = `${action.label}\u0000${action.openUrl}\u0000${action.downloadUrl}`;
    if (seen.has(key)) continue;
    seen.add(key);
    artifacts.push(action);
    artifactReferences.set(action, referencePaths(item));
  }
  const intendedPaths = new Set(referencePaths(data?.deliverable));
  const intendedBasename = referenceBasename(data?.deliverable);
  const primary = declaredPrimary
    || artifacts.find((artifact) => artifactReferences.get(artifact).some((path) => intendedPaths.has(path)))
    || artifacts.find((artifact) => referenceBasename(artifact) === intendedBasename)
    // A current runtime's explicit null means no file belongs to this reply.
    // Keep historical Files browsable; only older payloads without this field
    // retain their inventory-first preview fallback.
    || (!Object.prototype.hasOwnProperty.call(data || {}, 'deliverable') ? artifacts[0] : null)
    || null;
  return {
    available: Boolean(summary || primary || artifacts.length),
    state,
    summary: summary || (primary ? `${primary.label} is ready.` : 'Workspace completed.'),
    primary,
    artifacts,
  };
}

// Status comes from runtime state, never from instruction text or console output.
export function workspaceProgressModel({ runState, workerState, hasDeliverable = false }) {
  const run = String(runState || '').trim().toLowerCase();
  const worker = String(workerState || '').trim().toLowerCase();
  const closing = {
    terminating: ['Closing', 'Closing workspace', 'The workspace is stopping.'],
    termination_failed: ['Needs attention', 'Close needs attention', 'Shutdown could not be confirmed. Retry Close workspace; new work remains blocked.'],
    terminated: ['Closed', 'Workspace closed', 'This workspace is closed. Its saved output remains available.'],
  }[worker];
  const states = {
    created: ['Starting', 'Starting workspace', 'The worker is preparing to start.'],
    starting: ['Starting', 'Starting workspace', 'The worker is preparing to start.'],
    resuming: ['Starting', 'Resuming workspace', 'The worker is preparing to continue.'],
    queued: ['Queued', 'Queued work', 'This work is queued. Its status updates automatically.'],
    running: hasDeliverable
      ? ['Live preview', 'Live preview', 'A preview is available while work continues.']
      : ['Working', 'Work in progress', 'The worker is working on your project.'],
    completed: ['Complete', hasDeliverable ? 'Delivered result' : 'Work complete', 'Work complete. You can send a follow-up.'],
    failed: ['Needs attention', 'Run needs attention', 'The run did not complete. Review the details before trying again.'],
    cancelled: ['Cancelled', 'Run cancelled', 'This run was cancelled. You can send a new instruction.'],
    interrupted: ['Interrupted', 'Run interrupted', 'This run was interrupted. You can send a follow-up.'],
    paused: ['Paused', 'Workspace paused', 'Resume the workspace when you are ready to continue.'],
    ready: ['Ready', 'Workspace ready', 'The workspace is ready for an instruction.'],
    idle: ['Idle', 'Workspace idle', 'The workspace is waiting for an instruction.'],
    idle_terminated: ['Idle', 'Workspace idle', 'Compute is stopped. Send an instruction to continue.'],
    stopped: ['Stopped', 'Workspace stopped', 'Compute is stopped. Resume to continue.'],
  };
  const typed = Array.isArray(closing) ? closing : states[run || worker];
  const [label, panelTitle, summary] = (Array.isArray(typed) ? typed : null)
    || ['Status unavailable', 'Workspace status', 'The current status is unavailable. It updates automatically.'];
  return { label, panelTitle, summary };
}

export function watchOutputModel(data, technical = '') {
  const state = String(data?.latest_run?.state || '').trim().toLowerCase();
  const workerState = String(data?.worker?.close_state || data?.worker?.state || '').trim().toLowerCase();
  const raw = String(data?.latest_output || '').trim();
  const deliverable = data?.deliverable;
  const progress = workspaceProgressModel({ runState: state,
    workerState,
    hasDeliverable: Boolean(deliverable) });
  if (state === 'completed') {
    const label = String(deliverable?.label || deliverable?.workspace_path || '');
    return { ...progress, summary: label ? `${progress.summary} Result: ${label}` : progress.summary,
      result: raw, technical };
  }
  // A structured runtime classification is typed state; its user message says why.
  const failure = structuredFailureSummary(data);
  const closedFailure = workerState === 'terminated' && state === 'failed';
  return { ...progress, ...(closedFailure
    ? { summary: 'This work could not finish. The workspace is closed. Start new work from Workspaces.' }
    : failure ? { summary: failure } : {}), result: '',
    technical: [...new Set([...(closedFailure ? [failure] : []), raw, technical].filter(Boolean))].join('\n\n') };
}

// Measured wall-clock time for the latest job, not an inferred progress or ETA.
export function watchTimingModel(run, now = Date.now()) {
  if (!run || typeof run !== 'object') return null;
  const state = String(run.state || '').trim().toLowerCase();
  const waiting = state === 'queued';
  const settled = ['completed', 'failed', 'cancelled', 'interrupted'].includes(state);
  if (!waiting && !settled && !['running', 'settling'].includes(state)) return null;
  const start = Date.parse(waiting ? run.queued_at : run.started_at);
  const end = settled ? Date.parse(run.ended_at) : Number(now);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  const seconds = Math.floor((end - start) / 1000);
  const value = seconds < 60 ? `${seconds}s`
    : seconds < 3600 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
    : `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return { label: waiting ? 'Waiting' : settled ? 'Run time' : 'Running for', value };
}
