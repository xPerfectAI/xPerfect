import { mountPeerControls } from './peer-controls.js';
import { renderMemberActions } from './member-actions.js';
import { renderAddMember } from './member-create.js';
import { mountWorkerConfiguration } from './worker-configuration.js';
import { mountAllowedAiPolicy } from './allowed-ai-policy.js?v=20260923manager1';

let activePanel = null;
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function memberPanelError(error) {
  if (error.status === 401 || error.status === 403) return 'Sign in as the workspace owner to manage members and permissions.';
  if (error.status === 404) return 'Member settings are unavailable in this runtime. Refresh after the runtime update.';
  return error.message || 'The member runtime is unavailable. Try refreshing.';
}

export function openWorkspaceMembers(initialMember, { requestHeaders = headers => headers, providerAccounts = [], profileAccountProviders = {}, onChanged = () => {}, startAdd = false } = {}) {
  activePanel?.close();
  const previousFocus = document.activeElement;
  if (!document.querySelector('link[data-member-styles]')) {
    const stylesheet = document.createElement('link'); stylesheet.rel = 'stylesheet'; stylesheet.href = '/static/workspace-members.css'; stylesheet.dataset.memberStyles = 'true'; document.head.append(stylesheet);
  }
  if (!document.querySelector('link[data-allowed-ai-styles]')) {
    const stylesheet = document.createElement('link'); stylesheet.rel = 'stylesheet'; stylesheet.href = '/static/allowed-ai.css?v=20260922choices12'; stylesheet.dataset.allowedAiStyles = 'true'; document.head.append(stylesheet);
  }
  const dialog = element('dialog', 'workspace-members');
  dialog.setAttribute('aria-labelledby', 'member-panel-title');
  dialog.innerHTML = '<header class="member-panel-header"><div><p class="member-eyebrow">Workspace</p><h2 id="member-panel-title">Workspace settings</h2><p data-workspace-summary></p></div><div class="member-header-actions"><button type="button" data-refresh aria-label="Refresh settings">↻</button><button type="button" data-close aria-label="Close workspace settings">×</button></div></header><div class="member-panel-body"><nav class="member-sidebar" aria-label="Workspace members"><div class="member-list-heading"><h3>Workers</h3><button type="button" data-add>Add worker</button></div><div data-members role="list"></div></nav><main class="member-detail" data-detail></main></div><footer class="member-panel-footer"><p role="status" aria-live="polite" data-status>Loading members…</p></footer>';
  document.body.append(dialog); activePanel = dialog;
  let selectedId = String(initialMember.worker_id || '');
  let workspaceId = String(initialMember.workspace_id || '');
  let workspace = null;
  let peersCleanup = () => {};
  let configurationCleanup = () => {};
  let allowedAiCleanup = () => {};
  let disposed = false;
  let loading = false;
  let writing = false;
  let initialAdd = startAdd;
  const status = text => { if (!disposed) dialog.querySelector('.member-panel-footer [data-status]').textContent = text; };
  async function request(url, method = 'GET', body) {
    if (method !== 'GET') writing = true;
    try {
      const response = await fetch(url, { method, credentials:'same-origin', cache:'no-store', headers:requestHeaders({ 'Content-Type':'application/json' }), ...(body === undefined ? {} : { body:JSON.stringify(body) }) });
      let payload;
      try { payload = await response.json(); } catch { payload = {}; }
      if (!response.ok) {
        const detail = payload?.detail;
        const message = typeof detail === 'string'
          ? detail
          : detail?.message || detail?.recovery || 'The member runtime is unavailable. Try refreshing.';
        const error = new Error(message);
        error.status = response.status;
        error.code = String(detail?.code || '');
        throw error;
      }
      return payload;
    } finally { if (method !== 'GET') writing = false; }
  }
  function select(member) {
    selectedId = member.worker_id; peersCleanup(); configurationCleanup(); allowedAiCleanup();
    for (const button of dialog.querySelectorAll('[data-member-id]')) button.setAttribute('aria-current', String(button.dataset.memberId === selectedId));
    const detail = dialog.querySelector('[data-detail]'); detail.replaceChildren();
    detail.append(element('h3','member-title',member.name || 'Member'),element('p','member-subtitle',String(member.state || 'Unknown').replaceAll('_',' ')));
    renderMemberActions(detail, member, { request, refresh, status });
    const settings = element('section','member-settings'); detail.append(settings);
    const config = element('details','member-configuration');
    config.append(element('summary','','Advanced settings'));
    const nativeState = [member.profile,member.model].filter(Boolean).join(' · ');
    if (nativeState) config.append(element('p','member-note',nativeState));
    allowedAiCleanup = mountAllowedAiPolicy(settings, {
      projectId: member.project_id || workspace?.project_id || initialMember.project_id || '',
      workspaceId: member.workspace_id || workspaceId || initialMember.workspace_id || '',
      request,
      onConnectAccount: () => {
        dialog.close();
        document.getElementById('connections-tab')?.click();
        const addAccount = document.getElementById('add-provider-account');
        if (addAccount) { addAccount.open = true; addAccount.scrollIntoView({ block: 'start' }); addAccount.querySelector('summary')?.focus(); }
      },
    });
    peersCleanup = mountPeerControls(settings, member, { requestHeaders });
    settings.append(config);
    configurationCleanup = mountWorkerConfiguration(config, member, { requestHeaders });
  }
  async function refresh() {
    if (disposed || loading) return;
    loading = true; dialog.setAttribute('aria-busy','true'); status('Loading members…');
    try {
      if (!workspaceId) {
        const live = await request(`/api/worker/${encodeURIComponent(selectedId)}/live`);
        workspaceId = String(live.worker?.workspace_id || '');
      }
      if (!workspaceId) throw new Error('This runtime has not supplied a workspace identity yet.');
      workspace = await request(`/api/execution-workspaces/${encodeURIComponent(workspaceId)}/members`);
      if (!Array.isArray(workspace.members)) throw new Error('The runtime returned an unsupported member catalog.');
      if (disposed) return;
      const members = workspace.members;
      const readiness = workspace.runtime_readiness || {};
      const shared = workspace.mode === 'shared';
      const available = readiness.available === true;
      dialog.querySelector('[data-add]').hidden = !shared || !available;
      const readinessMessage = String(readiness.message || readiness.reason || readiness.code || 'runtime readiness is unavailable').trim();
      dialog.querySelector('[data-workspace-summary]').textContent = `${members.length} worker${members.length === 1 ? '' : 's'} · ${shared ? 'Shared workspace' : 'Separate workspace'}${shared && !available ? ` · Shared workspace unavailable: ${readinessMessage}` : ''}`;
      const list = dialog.querySelector('[data-members]'); list.replaceChildren();
      for (const member of members) {
        const item = element('div','member-list-item'); item.setAttribute('role','listitem');
        const button = element('button','member-select'); button.type='button'; button.dataset.memberId = member.worker_id;
        button.append(element('strong','',member.name || 'Member'),element('span','',String(member.state || 'Unknown').replaceAll('_',' ')));
        button.addEventListener('click',() => select(member)); item.append(button); list.append(item);
      }
      const selected = members.find(member => member.worker_id === selectedId) || members[0];
      if (selected) select(selected); else { peersCleanup(); configurationCleanup(); allowedAiCleanup(); dialog.querySelector('[data-detail]').replaceChildren(element('p','member-note','This workspace has no retained members.')); }
      status('Settings are current.'); onChanged();
      if (initialAdd) {
        initialAdd = false;
        if (shared && available) showAdd();
      }
    } catch(error) { status(memberPanelError(error)); }
    finally { loading=false; dialog.removeAttribute('aria-busy'); }
  }
  function showAdd() {
    if (writing || workspace?.mode !== 'shared' || workspace.runtime_readiness?.available !== true) return;
    peersCleanup(); configurationCleanup(); allowedAiCleanup();
    renderAddMember(dialog.querySelector('[data-detail]'), workspace, { request, providerAccounts, profileAccountProviders, status, onCreated: async member => { selectedId = member.worker_id; await refresh(); dialog.querySelector('[aria-current="true"]')?.focus(); }, onCancel: refresh, onUncertain: () => { workspace.runtime_readiness.available = false; dialog.querySelector('[data-add]').hidden = true; } });
  }
  dialog.querySelector('[data-add]').hidden = true;
  dialog.querySelector('[data-add]').addEventListener('click', showAdd);
  dialog.querySelector('[data-refresh]').addEventListener('click',refresh);
  dialog.querySelector('[data-close]').addEventListener('click',() => dialog.close());
  dialog.addEventListener('close',() => { disposed=true; peersCleanup(); configurationCleanup(); allowedAiCleanup(); dialog.remove(); if(activePanel===dialog) activePanel=null; previousFocus?.focus(); });
  dialog.showModal(); dialog.querySelector('[data-close]').focus(); refresh();
  return { close:() => dialog.close(), refresh };
}
