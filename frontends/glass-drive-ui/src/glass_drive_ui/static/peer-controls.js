import { mountPeerPermissions } from './peer-permissions.js?v=20260922peers2';

// Optional, owner-controlled peer collaboration. Mount once per selected member.
export function mountPeerControls(container, worker, { requestHeaders = headers => headers } = {}) {
  const memberId = String(worker?.worker_id || '');
  const workspaceId = String(worker?.workspace_id || '');
  if (!memberId || !workspaceId) return () => {};
  const root = document.createElement('details');
  root.className = 'peer-controls';
  root.innerHTML = `<summary>Work with other workers</summary>
    <p>Only workers in your account. These settings apply to every worker in this workspace. Finding workers does not allow messages.</p>
    <form data-policy>
      <label><input type="checkbox" name="discoverEnabled"> Find my other workers</label>
      <label data-discovery-options hidden>Find workers in <select name="discovery"><option value="account">My whole account</option><option value="workspace">This workspace</option><option value="selected">Selected workspaces</option></select></label>
      <label data-selected hidden>Selected workspaces <select name="selected" multiple aria-label="Selected workspaces"></select></label>
      <label><input type="checkbox" name="access"> Allow approved worker messages</label>
      <button type="submit">Save discovery and access</button>
    </form>
    <p role="status" aria-live="polite" data-status></p>
    <p data-visible-peers hidden></p>
    <div data-permissions></div>
    <form data-message>
      <label>Worker <select name="target" required><option value="">Choose a worker</option></select></label>
      <label>Message <textarea name="message" required maxlength="65536"></textarea></label>
      <button type="submit">Queue message</button>
    </form>
    <p data-permission-note>Revoke stops new messages in that direction. It cannot erase messages already delivered.</p>
    <div data-grants></div>
    <button type="button" data-refresh>Refresh messages</button>
    <ol data-messages></ol>`;
  container.append(root);
  const status = root.querySelector('[data-status]');
  const policyForm = root.querySelector('[data-policy]');
  const messageForm = root.querySelector('[data-message]');
  let policy = null;
  let peers = [];
  let grants = [];
  let disposed = false;
  let pendingMessage = null;
  const routes = `/api/workers/${encodeURIComponent(memberId)}`;
  const policyRoute = `/api/workspaces/${encodeURIComponent(workspaceId)}/peer-policy`;
  const say = (text) => { if (!disposed) status.textContent = text; };
  async function request(url, method = 'GET', data) {
    const response = await fetch(url, { method, credentials: 'same-origin', headers: requestHeaders({ 'Content-Type': 'application/json' }), ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result?.detail?.code || 'peer_request_failed');
    return result;
  }
  function failure(error) {
    const words = { peer_policy_stale: 'Settings changed. Refresh and try again.', peer_snapshot_stale: 'The worker list changed. Reload workers before allowing messages.', peer_access_denied: 'Access is off, revoked, or expired. Allow the exact workers again.', peer_owner_session_required: 'Open your signed-in workspace to change peer settings.', peer_message_contains_credential: 'This message contains a credential. Remove it before sharing.', peer_native_endpoint_unavailable: 'The native peer connection is not configured.', peer_runtime_unavailable: 'Worker access is temporarily unavailable. Refresh to try again.', peer_selection_too_large: 'There are too many workers to show. Ask an admin to reduce the current roster.' };
    say(words[error.message] || 'Peer collaboration is unavailable. Refresh or check the runtime status.');
  }
  async function refresh() {
    const results = await Promise.all([request(policyRoute), request(`${routes}/peers`), request(`${routes}/peer-grants`), request('/api/peer-workspaces')]);
    [policy, { items: peers }, { items: grants }] = results;
    if (disposed) return;
    policyForm.elements.discoverEnabled.checked = policy.discovery !== 'off';
    policyForm.elements.discovery.value = policy.discovery === 'off' ? 'account' : policy.discovery;
    root.querySelector('[data-discovery-options]').hidden = policy.discovery === 'off';
    policyForm.elements.access.checked = policy.access_enabled;
    policyForm.elements.selected.replaceChildren();
    results[3].items.forEach(item => { const option = new Option(item.name, item.workspace_id); option.selected = policy.selected_workspaces.includes(item.workspace_id); policyForm.elements.selected.add(option); });
    root.querySelector('[data-selected]').hidden = policy.discovery !== 'selected';
    const target = messageForm.elements.target;
    const prior = target.value;
    target.replaceChildren(new Option('Choose a worker', ''));
    const recipients = new Map(grants.filter(g => g.source_worker_id === memberId && g.status === 'active' && g.scopes.includes('message')).map(g => [g.target_worker_id,g.target_name || 'Selected worker']));
    recipients.forEach((name,id) => target.add(new Option(name,id)));
    target.value = prior;
    const list = root.querySelector('[data-grants]');
    list.replaceChildren();
    for (const grant of [...grants].sort((left, right) => Number(right.status === 'active') - Number(left.status === 'active'))) {
      const line = document.createElement('p');
      const outgoing = grant.source_worker_id === memberId;
      const otherName = outgoing ? grant.target_name || 'selected worker' : grant.source_name || 'selected worker';
      line.textContent = `${outgoing ? 'Can send to' : 'Can receive from'} ${otherName}: ${grant.scopes.map(scope => ({ message: 'messages', wake: 'start an idle worker', context_read: 'shared result' })[scope] || 'scoped access').join(', ')} · ${({active:'allowed',policy_changed:'settings changed',expired:'expired',revoked:'revoked'})[grant.status] || 'unavailable'}${grant.status !== 'active' ? '' : grant.expires_at === null ? ' · until revoked' : ' · until ' + new Date(grant.expires_at).toLocaleString()} `;
      if (grant.status === 'active') {
        const button = document.createElement('button');
        button.type = 'button'; button.textContent = 'Revoke this direction';
        button.setAttribute('aria-label', `Revoke messages ${outgoing ? 'to' : 'from'} ${otherName}`);
        button.addEventListener('click', async () => {
          button.disabled = true; say(`Revoking messages ${outgoing ? 'to' : 'from'} ${otherName}…`);
          try {
            const result = await request(`/api/peer-grants/${encodeURIComponent(grant.grant_id)}`, 'DELETE');
            await refresh(); status.tabIndex=-1; status.focus();
            say(result.already_invoked_messages ? 'This direction is revoked. Some content was already delivered and cannot be erased.' : 'This direction is revoked. New messages using it are blocked.');
          } catch (error) { button.disabled = false; failure(error); }
        });
        line.append(button);
      }
      list.append(line);
    }
    const messages = await request(`${routes}/peer-messages`);
    const history = root.querySelector('[data-messages]');
    root.querySelector('[data-permission-note]').hidden = (!peers.length || !policy.access_enabled) && !grants.length;
    root.querySelector('[data-refresh]').hidden = !messages.items.length && !grants.length;
    history.replaceChildren();
    for (const item of messages.items) {
      const line = document.createElement('li');
      const direction = item.source_worker_id === memberId ? 'Sent' : 'Received';
      line.textContent = `${direction} · ${item.delivery_state}${item.run_state && item.run_state !== item.delivery_state ? ` · ${item.run_state}` : ''}: ${item.message || 'Content access is no longer available.'}`;
      history.append(line);
    }
    root.querySelector('[data-visible-peers]').hidden = !peers.length;
    root.querySelector('[data-visible-peers]').textContent = peers.length ? `Visible workers: ${peers.map(peer => peer.name).join(', ')}` : '';
    root.querySelector('[data-message]').hidden = !recipients.size;
    if (!peers.length && policy.discovery !== 'off') say('No other workers are visible yet. They must also allow discovery.');
  }
  policyForm.elements.discoverEnabled.addEventListener('change', () => { root.querySelector('[data-discovery-options]').hidden = !policyForm.elements.discoverEnabled.checked; root.querySelector('[data-selected]').hidden = !policyForm.elements.discoverEnabled.checked || policyForm.elements.discovery.value !== 'selected'; });
  policyForm.elements.discovery.addEventListener('change', () => { root.querySelector('[data-selected]').hidden = policyForm.elements.discovery.value !== 'selected'; });
  policyForm.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await request(policyRoute, 'PUT', { expected_revision: policy.revision, discovery: policyForm.elements.discoverEnabled.checked ? policyForm.elements.discovery.value : 'off', access_enabled: policyForm.elements.access.checked, selected_workspaces: policyForm.elements.discoverEnabled.checked && policyForm.elements.discovery.value === 'selected' ? Array.from(policyForm.elements.selected.selectedOptions, option => option.value) : [] });
      await refresh(); say('Settings saved. Changed policies need new permission; unchanged settings keep it.');
    } catch (error) { failure(error); }
  });
  const cleanupPermissions = mountPeerPermissions(root.querySelector('[data-permissions]'), memberId, { request, onSaved:refresh, report:say });
  messageForm.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const target = messageForm.elements.target.value;
      const grant = grants.find(g => g.source_worker_id === memberId && g.target_worker_id === target && g.status === 'active' && g.scopes.includes('message'));
      if (!grant) return say('Choose a worker with an active message grant.');
      const intent = { target_worker_id: target, grant_id: grant.grant_id, message: messageForm.elements.message.value };
      if (!pendingMessage || pendingMessage.signature !== JSON.stringify(intent)) pendingMessage = { signature: JSON.stringify(intent), payload: { ...intent, idempotency_key: crypto.randomUUID() } };
      const result = await request(`${routes}/peer-messages`, 'POST', pendingMessage.payload);
      if (result.delivery_state !== 'unavailable') { messageForm.reset(); pendingMessage = null; }
      await refresh(); say(result.delivery_state === 'invoked' ? 'Worker dispatch started. A model read receipt is not available.' : result.delivery_state === 'queued' ? 'Message accepted. Check the delivery state below.' : 'Saved, but the recipient cannot run yet.');
    } catch (error) {
      if (error.message === 'peer_access_denied') say('Message blocked. If the worker is idle, allow “May start a receiving worker when idle”; otherwise check the current permission.');
      else failure(error);
    }
  });
  root.querySelector('[data-refresh]').addEventListener('click', () => refresh().catch(failure));
  root.addEventListener('toggle', () => {
    if (!root.open) return;
    refresh().catch(failure);
    root.querySelector('.peer-permissions').open = true;
    cleanupPermissions.reload();
  });
  return () => { disposed = true; cleanupPermissions(); root.remove(); };
}
