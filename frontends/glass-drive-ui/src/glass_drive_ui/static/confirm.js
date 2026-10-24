const RECOVERY_KEY = 'glasshive.pending-confirmation';
const status = document.querySelector('#confirm-status');
const button = document.querySelector('#confirm-button');
let pending = null;
let confirmationToken = '';
let csrfToken = '';
let cleanup = null;
let inFlight = false;

function parameters() {
  const current = new URLSearchParams(window.location.hash.slice(1));
  const changeId = String(current.get('change_id') || '').trim();
  const token = String(current.get('token') || '').trim();
  if (changeId && token) {
    try { sessionStorage.setItem(RECOVERY_KEY, JSON.stringify({ changeId, token })); } catch { /* Current-page confirmation still works. */ }
    history.replaceState(null, '', '/confirm-change');
    return { changeId, token };
  }
  try {
    const stored = JSON.parse(sessionStorage.getItem(RECOVERY_KEY) || '{}');
    return { changeId: String(stored.changeId || ''), token: String(stored.token || ''), cleanup: stored.cleanup };
  } catch {
    return { changeId: '', token: '' };
  }
}

function detail(id, value) {
  document.querySelector(id).textContent = value;
}

function optionalDetail(rowId, valueId, value) {
  const row = document.querySelector(rowId);
  if (!row) return false;
  const text = String(value || '').trim();
  row.hidden = !text;
  if (text) detail(valueId, text);
  return Boolean(text);
}

function capabilityLabel(payload) {
  return String(payload.library_id || payload.connection_id || payload.account_id || 'Unspecified capability');
}

async function json(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    throw new Error(Array.isArray(detail) ? detail.map(item => item.msg || 'Check the requested change.').join(' ')
      : typeof detail === 'object' && detail ? String(detail.message || 'xPerfect could not complete this request.')
        : String(detail || 'xPerfect could not complete this request.'));
  }
  return payload;
}

async function initialize() {
  const { changeId, token, cleanup: savedCleanup } = parameters();
  if (!changeId || (!savedCleanup && token.length < 16)) throw new Error('This confirmation link is incomplete or no longer available.');
  confirmationToken = token;

  const sessionResponse = await fetch('/auth/session');
  const session = sessionResponse.ok ? await sessionResponse.json() : {};
  csrfToken = String(session.csrf_token || '');

  const response = await fetch(`/api/pending-changes/${encodeURIComponent(changeId)}`);
  if (response.status === 401) {
    const config = await json(await fetch('/auth/config'));
    if (config.oidc) {
      window.location.replace('/auth/oidc/start?return_to=%2Fconfirm-change');
      return;
    }
  }
  pending = await json(response);
  if (savedCleanup) {
    if (pending.status !== 'confirmed' || pending.change_type !== 'library_upgrade'
        || String(pending.target_id || '') !== savedCleanup.workerId
        || String(pending.payload?.replaces_grant_id || '') !== savedCleanup.grantId) {
      throw new Error('These cleanup details no longer match this approved workspace change. Open the workspace to check it.');
    }
    cleanup = { workerId: savedCleanup.workerId, grantId: savedCleanup.grantId }; confirmationToken = '';
  } else if (pending.status !== 'pending') throw new Error(`This change is already ${String(pending.status || 'resolved')}.`);
  const payload = pending.payload || {};
  const isAccountChange = pending.change_type === 'workspace_provider_account';
  const isReapprovalWaiver = pending.change_type === 'workspace_duplication_reapproval_waiver';
  const librarySnapshot = payload.library_snapshot || {};
  const plan = Array.isArray(payload.library_plan_snapshot) ? payload.library_plan_snapshot : [];
  const dependencies = plan.slice(0, Math.max(0, plan.length - 1)).map((entry) => {
    const snapshot = entry.library_snapshot || {};
    const scopes = Array.isArray(entry.scopes) && entry.scopes.length
      ? ` · ${entry.scopes.join(', ')}`
      : '';
    return `${String(snapshot.stable_id || 'Capability')} ${String(snapshot.version || '')}${scopes}`.trim();
  });
  const provenance = librarySnapshot.provenance || {};
  const isLibraryUpgrade = pending.change_type === 'library_upgrade';
  detail('#confirm-target', String(pending.target_label || pending.target_id || 'Unknown workspace'));
  const name = String(pending.capability_label || capabilityLabel(payload));
  detail('#confirm-capability', librarySnapshot.version ? `${name} · Version ${librarySnapshot.version}` : name);
  detail('#confirm-scopes', isReapprovalWaiver
    ? 'Future runs continue without this copied capability'
    : (isAccountChange
      ? 'Future runs only; queued or running work is never changed'
      : (pending.effective_scopes || []).join(', ') || 'No additional permissions'));
  detail('#confirm-expiry', new Date(Number(pending.expires_at) * 1000).toLocaleString());
  let hasTechnicalDetails = optionalDetail('#confirm-version-row', '#confirm-version', librarySnapshot.version || '');
  hasTechnicalDetails = optionalDetail(
    '#confirm-provenance-row',
    '#confirm-provenance',
    [provenance.publisher, provenance.source, provenance.revision].filter(Boolean).join(' · '),
  ) || hasTechnicalDetails;
  hasTechnicalDetails = optionalDetail('#confirm-hash-row', '#confirm-hash', librarySnapshot.content_hash || '') || hasTechnicalDetails;
  hasTechnicalDetails = optionalDetail('#confirm-dependencies-row', '#confirm-dependencies', dependencies.join('; ')) || hasTechnicalDetails;
  document.querySelector('#confirm-details').hidden = false;
  document.querySelector('#confirm-technical').hidden = !hasTechnicalDetails;
  document.querySelector('#confirm-cancel').href = isReapprovalWaiver
    ? '/#workspaces'
    : (isAccountChange ? '/#connections' : '/#library');
  document.querySelector('#confirm-intro').textContent = isReapprovalWaiver
    ? 'Confirm that this copied workspace may run without the capability below.'
    : (isAccountChange
      ? 'Review which private worker account this workspace may use on future runs.'
      : isLibraryUpgrade ? 'Update only this workspace to the version below. The next native start uses the updated setup.'
        : 'Only this workspace will receive the capability below.');
  button.textContent = isReapprovalWaiver ? 'Continue without this capability' : isLibraryUpgrade ? 'Approve upgrade' : 'Approve for this workspace';
  button.disabled = false;
  if (cleanup) showCleanupPending();
}

function approvedNavigation() {
  document.querySelector('#confirm-cancel').hidden = true;
  const notice = document.querySelector('.confirm-notice'); if (notice) notice.hidden = true;
  const back = document.querySelector('#confirm-back');
  back.href = `/watch/${encodeURIComponent(String(pending.target_id || ''))}?surface=desktop`;
  back.hidden = false;
}

function rememberCleanup() {
  try { sessionStorage.setItem(RECOVERY_KEY, JSON.stringify({ changeId: pending.change_id, cleanup })); } catch { /* Keep this page's exact cleanup target. */ }
}

function showCleanupPending() {
  approvedNavigation();
  document.querySelector('#confirm-intro').textContent = 'The upgrade is approved for the next native start. Old native files still need cleanup.';
  status.textContent = 'Upgrade approved. Old native files have not been removed yet. Retry file cleanup when the worker is idle.';
  button.textContent = 'Retry file cleanup'; button.hidden = false; button.disabled = false;
}

function finishApproved({ nativeEffect = '', retained = false, retried = false } = {}) {
  try { sessionStorage.removeItem(RECOVERY_KEY); } catch { /* The completed receipt needs no browser storage. */ }
  cleanup = null; confirmationToken = ''; approvedNavigation();
  document.querySelector('#confirm-intro').textContent = 'This workspace change is approved.';
  status.textContent = retried ? 'Old file cleanup completed.' : 'Approved. This change is confirmed.';
  if (retained) status.textContent += ' Edited or shared files were kept.';
  if (nativeEffect === 'next_native_start' || retried) status.textContent += ' The update takes effect at the next native start.';
  button.textContent = 'Approved'; button.hidden = true;
}

async function retryCleanup() {
  status.textContent = 'Retrying old file cleanup…';
  const result = await json(await fetch(`/api/workspaces/${encodeURIComponent(cleanup.workerId)}/capability-grants/${encodeURIComponent(cleanup.grantId)}`, {
    method: 'DELETE', headers: { ...(csrfToken ? { 'X-GlassHive-CSRF': csrfToken } : {}) },
  }));
  if (result.native_cleanup?.state === 'unconfirmed') { rememberCleanup(); showCleanupPending(); }
  else finishApproved({ nativeEffect: result.native_effect, retained: Boolean(result.native_cleanup?.retained?.length), retried: true });
}

button.addEventListener('click', async () => {
  if (inFlight || !pending || (!confirmationToken && !cleanup)) return;
  inFlight = true; button.disabled = true;
  status.textContent = 'Applying the approved change…';
  try {
    if (cleanup) { await retryCleanup(); return; }
    const response = await fetch(`/api/pending-changes/${encodeURIComponent(pending.change_id)}/confirm`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(csrfToken ? { 'X-GlassHive-CSRF': csrfToken } : {}),
      },
      body: JSON.stringify({ confirmation_token: confirmationToken }),
    });
    const result = await json(response);
    const applied = result.applied || {};
    confirmationToken = '';
    if (applied.native_cleanup?.state === 'unconfirmed') {
      const workerId = String(pending.target_id || '');
      const grantId = String(applied.replaced_grant_id || '');
      if (pending.change_type !== 'library_upgrade' || !workerId || !grantId
          || grantId !== String(pending.payload?.replaces_grant_id || '')) {
        try { sessionStorage.removeItem(RECOVERY_KEY); } catch { /* No unsafe recovery target is retained. */ }
        approvedNavigation(); status.textContent = 'Upgrade approved, but old file cleanup was not confirmed. Open the workspace to check it.';
        button.hidden = true; return;
      }
      cleanup = { workerId, grantId }; rememberCleanup(); showCleanupPending();
    } else finishApproved({ nativeEffect: applied.native_effect, retained: Boolean(applied.native_cleanup?.retained?.length) });
  } catch (error) {
    status.textContent = error.message || 'xPerfect could not apply this change.';
    button.disabled = false;
  } finally { inFlight = false; }
});

initialize().catch((error) => {
  status.textContent = error.message || 'xPerfect could not load this change.';
});
