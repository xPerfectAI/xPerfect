const RECOVERY_KEY = 'glasshive.pending-confirmation';
const status = document.querySelector('#confirm-status');
const button = document.querySelector('#confirm-button');
let pending = null;
let confirmationToken = '';
let csrfToken = '';

function parameters() {
  const current = new URLSearchParams(window.location.hash.slice(1));
  const changeId = String(current.get('change_id') || '').trim();
  const token = String(current.get('token') || '').trim();
  if (changeId && token) {
    sessionStorage.setItem(RECOVERY_KEY, JSON.stringify({ changeId, token }));
    history.replaceState(null, '', '/confirm-change');
    return { changeId, token };
  }
  try {
    const stored = JSON.parse(sessionStorage.getItem(RECOVERY_KEY) || '{}');
    return { changeId: String(stored.changeId || ''), token: String(stored.token || '') };
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
  if (!response.ok) throw new Error(String(payload.detail || 'xPerfect could not complete this request.'));
  return payload;
}

async function initialize() {
  const { changeId, token } = parameters();
  if (!changeId || token.length < 16) throw new Error('This confirmation link is incomplete or no longer available.');
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
  if (pending.status !== 'pending') throw new Error(`This change is already ${String(pending.status || 'resolved')}.`);
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
  detail('#confirm-target', String(pending.target_label || pending.target_id || 'Unknown workspace'));
  detail('#confirm-capability', String(pending.capability_label || capabilityLabel(payload)));
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
      : 'Only this workspace will receive the capability below.');
  button.textContent = isReapprovalWaiver ? 'Continue without this capability' : 'Approve for this workspace';
  button.disabled = false;
}

button.addEventListener('click', async () => {
  if (!pending || !confirmationToken) return;
  button.disabled = true;
  status.textContent = 'Applying the approved change…';
  try {
    const response = await fetch(`/api/pending-changes/${encodeURIComponent(pending.change_id)}/confirm`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(csrfToken ? { 'X-GlassHive-CSRF': csrfToken } : {}),
      },
      body: JSON.stringify({ confirmation_token: confirmationToken }),
    });
    const result = await json(response);
    sessionStorage.removeItem(RECOVERY_KEY);
    confirmationToken = '';
    status.textContent = `Approved. This change is now ${String(result.status || 'confirmed')}.`;
    button.textContent = 'Approved';
    button.hidden = true;
    document.querySelector('#confirm-cancel').hidden = true;
    const back = document.querySelector('#confirm-back');
    back.href = `/watch/${encodeURIComponent(String(pending.target_id || ''))}?surface=desktop`;
    back.hidden = false;
  } catch (error) {
    status.textContent = error.message || 'xPerfect could not apply this change.';
    button.disabled = false;
  }
});

initialize().catch((error) => {
  status.textContent = error.message || 'xPerfect could not load this change.';
});
