const ATTENTION = new Map([
  ['action_required', 0],
  ['blocked', 0],
  ['termination_failed', 0],
  ['failed', 1],
  ['cancelled', 1],
  ['interrupted', 1],
]);

function normalizedState(workspace) {
  return String(workspace?.display_state || workspace?.state_label || workspace?.state || '').trim().toLowerCase();
}

export function compareWorkspacePriority(left, right) {
  const leftAttention = ATTENTION.get(normalizedState(left));
  const rightAttention = ATTENTION.get(normalizedState(right));
  const leftBucket = leftAttention ?? (left?.favorite ? 2 : 3);
  const rightBucket = rightAttention ?? (right?.favorite ? 2 : 3);
  if (leftBucket !== rightBucket) return leftBucket - rightBucket;
  return String(left?.workspace_label || left?.name || left?.worker_id || '').localeCompare(
    String(right?.workspace_label || right?.name || right?.worker_id || ''),
  );
}

export function previewWorkerIds(items, limit = 3) {
  return (Array.isArray(items) ? items : [])
    .filter((item) => item?.visible && item?.active)
    .slice(0, Math.max(0, Number(limit) || 0))
    .map((item) => String(item.worker_id || ''))
    .filter(Boolean);
}

export function shouldHydrateWorkspaceDelivery({
  runState = '',
  runId = '',
  hydratedRunId = '',
  legacyLoaded = false,
} = {}) {
  if (String(runState).trim().toLowerCase() !== 'completed') return false;
  const current = String(runId || '').trim();
  if (current) return current !== String(hydratedRunId || '').trim();
  return !legacyLoaded;
}
