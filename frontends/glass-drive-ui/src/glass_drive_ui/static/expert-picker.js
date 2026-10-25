/* A starting team uses the same worker templates, Library and launch owner. */
import { libraryRequest, libraryModal, reviewLibraryUse } from './personal-library.js?v=20261024polish1';
import { attachActionMenu } from './action-menu.js?v=20261024polish1';

const node = (tag, cls = '', text = '') => { const e = document.createElement(tag); e.className = cls; e.textContent = text; return e; };
// Presentation belongs to stable public catalog IDs, never a person's name or job text.
const expertArtwork = {
  'bioanalytical-scientist': 'teal',
  'clinical-biostatistician': 'blue',
  'finance-director': 'ochre',
  'growth-marketer': 'coral',
  'regulatory-affairs-specialist': 'violet',
};
export async function expertRequestKey(values) {
  const bytes = new TextEncoder().encode(JSON.stringify(values));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return 'expert-' + [...new Uint8Array(digest)].map(v => v.toString(16).padStart(2, '0')).join('');
}

export function createExpertPicker({ form, getData, onChoose, request = libraryRequest } = {}) {
  const section = node('section', 'expert-picker'); section.hidden = true; section.setAttribute('aria-label', 'Choose an expert');
  const heading = node('div', 'expert-heading');
  const words = node('div'); words.append(node('h2', '', 'Choose an expert'), node('p', '', 'Choose an AI expert, or create your own.'));
  const own = node('button', 'expert-create'); own.type = 'button';
  own.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M12 5v14M5 12h14"/></svg><span>Create</span>';
  heading.append(words);
  const tabs = node('div', 'expert-tabs'); tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', 'Expert collection');
  const ready = node('button', '', 'Experts'); ready.type = 'button'; ready.setAttribute('aria-pressed', 'true');
  const saved = node('button', '', 'My experts'); saved.type = 'button'; saved.setAttribute('aria-pressed', 'false');
  tabs.append(ready, saved);
  const filters = node('div', 'expert-tabs'); filters.hidden = true; filters.setAttribute('role', 'group'); filters.setAttribute('aria-label', 'My experts status');
  for (const [state, label] of [['active', 'Available'], ['archived', 'Archived'], ['deleted', 'Removed']]) {
    const button = node('button', '', label); button.type = 'button'; button.dataset.catalogState = state;
    button.onclick = () => { catalogState = state; void loadTemplates(); }; filters.append(button);
  }
  const grid = node('div', 'expert-grid');
  const message = node('p', 'expert-status'); message.setAttribute('role', 'status'); message.hidden = true;
  const retry = node('button', 'expert-own', 'Retry'); retry.type = 'button'; retry.hidden = true; retry.setAttribute('aria-label', 'Retry loading experts');
  section.append(heading, tabs, filters, grid, message, retry); form.prepend(section);
  const startNew = node('button', 'expert-own', 'Start a new worker'); startNew.type = 'button'; startNew.hidden = true; section.append(startNew);
  let owner = '', epoch = 0, packs = [], templates = [], selected = null, view = 'starters', loading = false;
  let lifetime = new AbortController();
  let catalogState = 'active', pendingCreation = null, catalogEpoch = 0;
  const tell = text => { message.textContent = text; message.hidden = !text; };
  const fresh = token => Boolean(owner) && token === epoch;
  async function loadTemplates() {
    const token = epoch, requestEpoch = ++catalogEpoch;
    try {
      const response = await request('/api/workspace-templates?state=' + catalogState, { signal: lifetime.signal });
      if (fresh(token) && requestEpoch === catalogEpoch) { templates = response.items || []; render(); }
    } catch (error) { if (fresh(token) && requestEpoch === catalogEpoch && error.name !== 'AbortError') tell(error.message); }
  }
  function accepted() {
    if (pendingCreation) { try { sessionStorage.removeItem(pendingCreation.slot); } catch { /* No authority in storage. */ } pendingCreation = null; }
    startNew.hidden = true;
  }
  startNew.onclick = () => { accepted(); tell('Ready to create a fresh worker. Your job and expert choices are kept; select Run Project.'); };
  function field(body, label, value, multiline = false) {
    const wrapper = node('label', '', label); const input = node(multiline ? 'textarea' : 'input');
    input.value = value || ''; if (!multiline) { input.required = true; input.maxLength = 160; } else input.maxLength = 1000;
    wrapper.append(input); body.append(wrapper); return input;
  }
  function manage(item, action) {
    const token = epoch, path = '/api/workspace-templates/' + encodeURIComponent(item.template_id);
    const body = { expected_catalog_revision: item.catalog_revision };
    const labels = { edit: 'Edit expert', duplicate: 'Copy expert', archive: 'Archive expert', remove: 'Remove expert', restore: 'Restore expert' };
    let name, description, sourceWorker; const key = crypto.randomUUID();
    const dialog = libraryModal(labels[action], { action: labels[action], signal: lifetime.signal,
      onSave: async ({ signal }) => {
        if (action === 'edit' && sourceWorker?.value) await request('/api/workspaces/' + encodeURIComponent(sourceWorker.value) + '/templates', {
          method: 'POST', signal, body: { ...body, name: name.value.trim(), description: description.value.trim(),
            lineage_id: item.lineage_id, expected_template_id: item.template_id } });
        else if (action === 'edit') await request(path, { method: 'PATCH', signal, body: { ...body, name: name.value.trim(),
          ...(description.value.trim() !== String(item.description || '') ? { description: description.value.trim() } : {}) } });
        else if (action === 'duplicate') await request(path + '/duplicate', { method: 'POST', signal, body: { name: name.value.trim(), idempotency_key: key } });
        else await request(path + (action === 'remove' ? '' : '/state'), { method: action === 'remove' ? 'DELETE' : 'POST', signal,
          body: { ...body, state: action === 'archive' ? 'archived' : 'active', confirm: action === 'remove' } });
        if (!fresh(token)) return;
        if (action !== 'duplicate' && selected?.kind === 'template'
            && (selected.id === item.template_id || (item.lineage_id && selected.lineage_id === item.lineage_id))) {
          selected = null; remember(); onChoose?.(null);
        }
        await loadTemplates(); tell(action === 'remove' ? 'Expert removed. Find it under Removed to restore it. Existing workers and saved versions are kept.' : 'Expert saved. Existing workers keep their original setup.');
      } });
    if (action === 'edit' || action === 'duplicate') name = field(dialog.body, 'Expert name', action === 'duplicate' ? item.name + ' copy' : item.name);
    if (action === 'edit') {
      description = field(dialog.body, 'What does this expert do?', item.description, true);
      const wrapper = node('label', '', 'Update its knowledge and setup from a worker (optional)'); sourceWorker = node('select');
      const unchanged = node('option', '', 'Keep the saved setup'); unchanged.value = ''; sourceWorker.append(unchanged);
      for (const worker of getData?.()?.existing_workspaces || []) {
        if (worker.catalog_state === 'deleted') continue;
        const option = node('option', '', [worker.name, worker.profile, worker.project_title].filter(Boolean).join(' · ')); option.value = worker.worker_id; sourceWorker.append(option);
      }
      wrapper.append(sourceWorker); dialog.body.append(wrapper, node('p', '', 'Save a new version. Workers already started keep their original setup.'));
    }
    if (action === 'remove') dialog.body.append(node('p', '', 'Remove this expert from your choices? Its versions, knowledge and existing workers are kept. You can restore it later.'));
    if (action === 'archive') dialog.body.append(node('p', '', 'Put this expert aside. Restore it from Archived when you need it again.'));
    if (action === 'restore') dialog.body.append(node('p', '', 'Make this expert available for new jobs again.'));
  }
  async function history(item) {
    const token = epoch;
    try {
      const data = await request('/api/workspace-templates/' + encodeURIComponent(item.template_id) + '/versions', { signal: lifetime.signal });
      if (!fresh(token)) return;
      const dialog = libraryModal(`${item.name} · Versions`, { signal: lifetime.signal });
      for (const version of data.items || []) {
        const row = node('article', 'expert-version'); row.append(node('strong', '', `${version.name} · v${version.version}`), node('p', '', version.description));
        if (catalogState === 'active') {
          const use = node('button', 'expert-choose', 'Use this version'); use.type = 'button';
          use.onclick = () => { choose({ ...version, id: version.template_id, kind: 'template' }); dialog.close(); }; row.append(use);
        }
        dialog.body.append(row);
      }
    } catch (error) { if (fresh(token)) tell(error.message); }
  }
  function remember() { try { sessionStorage.setItem('xperfect.expert-selection:' + owner, JSON.stringify(selected ? { id: selected.id, kind: selected.kind } : null)); } catch { /* Deterministic server keys also protect reload retries. */ } }
  function choose(item, options = {}) {
    const choices = getData?.()?.new_workspace_options;
    if (item && Array.isArray(choices)) {
      const option = choices.find(value => value.profile === item.profile);
      if (!option || option.disabled) { tell('This expert’s worker type is not available here. Your current job and worker choices are kept.'); return; }
    }
    selected = item; remember(); render();
    onChoose?.(item, options);
    tell(item ? `${item.kind === 'starter' && item.role ? item.role : item.name} selected. Add the job and background below, then run.` : 'Your own worker starts with the choices below.');
  }
  function render() {
    ready.setAttribute('aria-pressed', String(view === 'starters')); saved.setAttribute('aria-pressed', String(view === 'templates'));
    filters.hidden = view !== 'templates';
    for (const button of filters.children) button.setAttribute('aria-pressed', String(button.dataset.catalogState === catalogState));
    const savedOptions = templates.map(t => ({ ...t, id: t.template_id, kind: 'template' }));
    if (catalogState === 'active' && selected?.kind === 'template' && !savedOptions.some(item => item.id === selected.id)) savedOptions.push(selected);
    const options = view === 'starters' ? packs : savedOptions;
    grid.replaceChildren(...options.map(item => {
      const artwork = item.kind === 'starter' && Object.hasOwn(expertArtwork, item.id) ? expertArtwork[item.id] : null;
      const card = node('article', 'expert-card'); card.dataset.tone = artwork || 'neutral';
      const chosen = selected?.id === item.id && selected?.kind === item.kind;
      card.classList.toggle('is-selected', chosen);
      const top = node('div', 'expert-card-top');
      const shortName = String(item.name || 'Expert').split('·')[0].trim();
      const profession = item.kind === 'starter' && item.role ? String(item.role) : '';
      const title = profession || String(item.name || 'AI expert');
      const avatar = node('span', 'expert-avatar'); avatar.setAttribute('aria-hidden', 'true');
      const fallback = node('span', 'expert-avatar-fallback');
      fallback.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="12" cy="8" r="3.5"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg>';
      avatar.append(fallback);
      if (artwork) {
        const image = node('img'); image.src = `/static/experts/${item.id}.webp?v=20261025gouache2`;
        image.alt = ''; image.width = 88; image.height = 88; image.loading = 'lazy'; image.decoding = 'async';
        image.onerror = () => { image.hidden = true; };
        avatar.append(image);
      }
      const identity = node('div', 'expert-identity'); identity.append(node('h3', '', title));
      if (item.kind === 'template') identity.append(node('p', 'expert-name', `Version ${item.version || 1}`));
      if (profession && shortName !== title) identity.append(node('p', 'expert-name', shortName));
      top.append(avatar, identity);
      const outcome = node('p', 'expert-outcome', item.outcome || item.description || 'Use your saved knowledge and abilities for a new job.');
      const abilities = node('ul', 'expert-abilities'); abilities.setAttribute('aria-label', 'Included knowledge and abilities');
      const included = (item.abilities || []).filter(a => a.included);
      for (const ability of included) abilities.append(node('li', '', ability.name));
      if (!included.length) {
        const count = (item.personal_library_refs || []).length + (item.library_refs || []).length;
        abilities.append(node('li', '', count ? `${count} saved knowledge and ability selections` : 'Your saved professional setup'));
      }
      const more = node('details', 'expert-details'); more.append(node('summary', '', 'What’s included'), abilities);
      const workerType = (getData?.()?.new_workspace_options || []).find(value => value.profile === item.profile)?.label || item.profile;
      more.append(node('p', '', item.kind === 'template' ? 'Starts a new worker with this expert’s saved account, knowledge and setup. To continue previous work, choose Existing below.' : `Runs with ${workerType}. Choose your account below. Native skills are reviewed before use.`));
      if (item.kind === 'template' && item.role && item.role !== 'main') more.append(node('p', '', `Role and focus: ${item.role}`));
      more.append(node('p', '', 'Add your company material without publishing it. Tools and native skills keep their real setup and permissions.'));
      for (const ability of (item.abilities || []).filter(a => !a.included)) more.append(node('p', '', `Optional: ${ability.name} · set up when needed`));
      const pick = node('button', 'expert-choose', chosen ? '✓ Selected' : 'Choose expert'); pick.type = 'button';
      pick.setAttribute('aria-label', `${chosen ? 'Selected expert' : 'Choose expert'}: ${title}${profession && shortName !== title ? ` · ${shortName}` : ''}`);
      pick.setAttribute('aria-pressed', String(chosen)); pick.onclick = () => choose(item);
      pick.disabled = item.kind === 'template' && catalogState !== 'active';
      card.append(top, outcome, more, pick);
      if (item.kind === 'template') {
        const menu = node('details', 'expert-details'); const opener = node('summary', '', 'Manage'); const actions = node('div', 'expert-actions');
        menu.append(opener, actions); attachActionMenu(menu, opener, actions, { menu: false });
        const operations = catalogState === 'active' ? ['edit', 'duplicate', 'archive', 'remove'] : ['restore', ...(catalogState === 'archived' ? ['remove'] : [])];
        for (const action of operations) {
          const button = node('button', 'secondary-button', { edit: 'Edit', duplicate: 'Copy', archive: 'Archive', remove: 'Remove', restore: 'Restore' }[action]);
          button.type = 'button'; button.onclick = () => { menu.open = false; manage(item, action); }; actions.append(button);
        }
        const versions = node('button', 'secondary-button', 'Versions'); versions.type = 'button'; versions.onclick = () => { menu.open = false; void history(item); }; actions.append(versions); card.append(menu);
      }
      return card;
    }));
    if (view === 'starters' || catalogState === 'active') grid.append(own);
    if (!options.length) grid.append(node('p', 'expert-empty', loading ? 'Loading experts…' : view === 'templates' ? 'No saved experts yet. Save a worker as an expert from Workspaces.' : 'No experts to show. Use Create to set up your own worker.'));
  }
  own.onclick = () => choose(null, { custom: true });
  ready.onclick = () => { view = 'starters'; render(); };
  saved.onclick = () => { view = 'templates'; render(); void loadTemplates(); };
  async function update(data) {
    const next = String(data?.draft_owner_scope || '');
    if (next === owner) {
      if (catalogState === 'active') templates = data?.workspace_templates || [];
      render(); return;
    }
    lifetime.abort(); lifetime = new AbortController(); epoch += 1; catalogEpoch += 1; owner = next;
    packs = []; templates = owner ? (data?.workspace_templates || []) : []; selected = null;
    view = 'starters'; loading = false; pendingCreation = null; catalogState = 'active'; startNew.hidden = true; tell('');
    section.hidden = !owner; retry.hidden = true;
    if (!owner) { render(); return; }
    await loadStarters();
  }
  async function loadStarters() {
    if (!owner || loading) return;
    const token = epoch; loading = true; retry.hidden = true; tell(''); render();
    try {
      const payload = await request('/api/workspace-starters', { signal: lifetime.signal });
      if (!fresh(token)) return;
      packs = (payload.items || []).map(p => ({ ...p, kind: 'starter' }));
      let restorationNotice = '';
      try {
        const prior = JSON.parse(sessionStorage.getItem('xperfect.expert-selection:' + owner) || 'null');
        selected = [...packs, ...templates.map(t => ({ ...t, id: t.template_id, kind: 'template' }))].find(t => t.id === prior?.id && t.kind === prior?.kind) || null;
        if (!selected && prior?.kind === 'template' && prior.id) {
          const exact = await request('/api/workspace-templates/' + encodeURIComponent(prior.id), { signal: lifetime.signal });
          if (!fresh(token)) return;
          if (exact.catalog_state === 'active') selected = { ...exact, id: exact.template_id, kind: 'template' };
          else { remember(); restorationNotice = 'This saved expert is archived or removed. Restore it in My experts before using it.'; }
        }
      } catch (error) {
        if (!fresh(token)) return;
        if ([403, 404].includes(error.status)) remember();
        restorationNotice = 'Could not restore that exact expert version. Your job is kept; choose an available expert or retry.';
      }
      if (selected) {
        view = selected.kind === 'template' ? 'templates' : 'starters';
        onChoose?.(selected, { restoring: true });
      }
      tell(restorationNotice);
    } catch (error) { if (fresh(token) && error.name !== 'AbortError') { retry.hidden = false; tell(`Experts are unavailable: ${error.message}. Your own worker choices remain available.`); } }
    finally { if (fresh(token)) { loading = false; render(); } }
  }
  retry.onclick = () => loadStarters();
  async function prepare({ providerAccount, effort = '', name = '' } = {}) {
    if (!selected) return null;
    const token = epoch, item = selected, exactOwner = owner;
    const check = () => { if (!fresh(token) || item !== selected) throw new Error('Your account or expert choice changed. Your job is kept; check the selection before running.'); };
    const context = [exactOwner, item.id, item.content_hash, providerAccount || null, effort];
    const pendingSlot = 'xperfect.expert-pending:' + await expertRequestKey([...context, name.trim()]); check();
    let attempt;
    try { attempt = sessionStorage.getItem(pendingSlot); } catch { /* The in-memory request still survives network retry. */ }
    if (!attempt) attempt = pendingCreation?.slot === pendingSlot ? pendingCreation.attempt : crypto.randomUUID();
    pendingCreation = { slot: pendingSlot, attempt };
    try { sessionStorage.setItem(pendingSlot, attempt); } catch { /* Same-turn retry uses the retained in-memory attempt. */ }
    // The server reuses an active exact starter setup. Each explicit job owns
    // its own receipt so removing that setup cannot trap later new jobs.
    const key = await expertRequestKey([...context, attempt]); check();
    let template;
    if (item.kind === 'starter') {
      tell(`Preparing ${item.name} with the included expertise…`);
      const imported = await request(`/api/workspace-starters/${encodeURIComponent(item.id)}/import`, {
        method: 'POST', signal: lifetime.signal, body: { idempotency_key: key, content_hash: item.content_hash,
          ...(providerAccount ? { provider_account: providerAccount } : {}), ...(effort ? { effort } : {}) },
      }); check(); template = imported.template;
    } else template = item;
    const workerName = name.trim() || item.name;
    const instanceKey = await expertRequestKey([exactOwner, template.template_id, workerName, attempt]); check();
    const created = await request(`/api/workspace-templates/${encodeURIComponent(template.template_id)}/instantiate`, {
      method: 'POST', signal: lifetime.signal, body: { idempotency_key: instanceKey, name: workerName },
    }); check();
    const worker = created.workspace;
    if (worker.catalog_state === 'deleted' || ['terminated', 'terminating', 'termination_failed'].includes(worker.close_state || worker.state)) {
      startNew.hidden = false;
      const message = 'This earlier worker was closed. Start a new worker to keep this job separate; its saved results remain.'; tell(message); throw new Error(message);
    }
    const refs = template.personal_library_refs || [];
    if (refs.length) {
      const received = await request(`/api/workspace/${encodeURIComponent(worker.worker_id)}/library`, { signal: lifetime.signal }); check();
      const native = (received.items || []).filter(value => ['skill', 'native', 'approved'].includes(value.kind) && value.active !== false);
      if ((created.approvals_required || []).length && native.length) {
        const confirmed = await reviewLibraryUse(native, worker.name || workerName, { signal: lifetime.signal, profile: worker.profile }); check();
        if (!confirmed) { tell(`${worker.name || workerName} is saved and waiting for your review. Your job is still below.`); return null; }
        const applied = await request(`/api/workspace/${encodeURIComponent(worker.worker_id)}/library`, {
          method: 'POST', signal: lifetime.signal, body: { refs: native.map(value => ({ item_id: value.item_id, version_id: value.version_id })), request_key: 'review-' + instanceKey, confirm_native: true },
        }); check();
        if ((applied.pending || []).length) throw new Error('This worker still needs setup. Its exact requirements remain in Library → Added here; your job is kept.');
      }
    }
    const exact = await request(`/api/workspaces/${encodeURIComponent(worker.worker_id)}`, { signal: lifetime.signal }); check();
    if (exact.catalog_state === 'deleted' || ['terminated', 'terminating', 'termination_failed'].includes(exact.close_state || exact.state)) {
      startNew.hidden = false; tell('This worker was closed. Start a new worker; your job is kept.');
      throw new Error('This worker was closed. Start a new worker; your job is kept.');
    }
    tell(`${exact.name || workerName} is ready for this job.`);
    return exact;
  }
  render();
  return { update, prepare, accepted, selection: () => selected, targetLabel: () => selected?.name || '', clear: () => choose(null), owner: () => owner };
}
