/* A starting team uses the same worker templates, Library and launch owner. */
import { libraryRequest, reviewLibraryUse } from './personal-library.js?v=20261024polish1';

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
  const own = node('button', 'expert-own', 'Create my own'); own.type = 'button'; heading.append(words, own);
  const tabs = node('div', 'expert-tabs'); tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', 'Expert collection');
  const ready = node('button', '', 'Experts'); ready.type = 'button'; ready.setAttribute('aria-pressed', 'true');
  const saved = node('button', '', 'My experts'); saved.type = 'button'; saved.setAttribute('aria-pressed', 'false');
  tabs.append(ready, saved);
  const grid = node('div', 'expert-grid');
  const message = node('p', 'expert-status'); message.setAttribute('role', 'status'); message.hidden = true;
  const retry = node('button', 'expert-own', 'Retry'); retry.type = 'button'; retry.hidden = true; retry.setAttribute('aria-label', 'Retry loading experts');
  section.append(heading, tabs, grid, message, retry); form.prepend(section);
  let owner = '', epoch = 0, packs = [], templates = [], selected = null, view = 'starters', loading = false;
  let lifetime = new AbortController();
  const tell = text => { message.textContent = text; message.hidden = !text; };
  const fresh = token => Boolean(owner) && token === epoch;
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
    own.setAttribute('aria-pressed', String(!selected));
    const options = view === 'starters' ? packs : templates.map(t => ({ ...t, id: t.template_id, kind: 'template' }));
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
        const image = node('img'); image.src = `/static/experts/${item.id}.webp?v=20261024portraits2`;
        image.alt = ''; image.width = 88; image.height = 88; image.loading = 'lazy'; image.decoding = 'async';
        image.onerror = () => { image.hidden = true; };
        avatar.append(image);
      }
      const identity = node('div', 'expert-identity'); identity.append(node('h3', '', title));
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
      card.append(top, outcome, more, pick); return card;
    }));
    if (!options.length) grid.append(node('p', 'expert-empty', loading ? 'Loading experts…' : view === 'templates' ? 'No saved experts yet. Save a worker as an expert from Workspaces.' : 'No experts to show. You can create your own worker below.'));
  }
  own.onclick = () => choose(null, { custom: true });
  ready.onclick = () => { view = 'starters'; render(); };
  saved.onclick = () => { view = 'templates'; render(); };
  async function update(data) {
    templates = data?.workspace_templates || [];
    const next = String(data?.draft_owner_scope || '');
    if (next === owner) { render(); return; }
    lifetime.abort(); lifetime = new AbortController(); epoch += 1; owner = next; packs = []; selected = null; loading = false;
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
      try {
        const prior = JSON.parse(sessionStorage.getItem('xperfect.expert-selection:' + owner) || 'null');
        selected = [...packs, ...templates.map(t => ({ ...t, id: t.template_id, kind: 'template' }))].find(t => t.id === prior?.id && t.kind === prior?.kind) || null;
      } catch { /* A storage hint never owns worker identity. */ }
      if (selected) {
        view = selected.kind === 'template' ? 'templates' : 'starters';
        onChoose?.(selected, { restoring: true });
      }
      tell('');
    } catch (error) { if (fresh(token) && error.name !== 'AbortError') { retry.hidden = false; tell(`Experts are unavailable: ${error.message}. Your own worker choices remain available.`); } }
    finally { if (fresh(token)) { loading = false; render(); } }
  }
  retry.onclick = () => loadStarters();
  async function prepare({ providerAccount, effort = '', name = '' } = {}) {
    if (!selected) return null;
    const token = epoch, item = selected, exactOwner = owner;
    const check = () => { if (!fresh(token) || item !== selected) throw new Error('Your account or expert choice changed. Your job is kept; check the selection before running.'); };
    const context = [exactOwner, item.id, item.content_hash, providerAccount || null, effort];
    const key = await expertRequestKey(context); check();
    let template;
    if (item.kind === 'starter') {
      tell(`Preparing ${item.name} with the included expertise…`);
      const imported = await request(`/api/workspace-starters/${encodeURIComponent(item.id)}/import`, {
        method: 'POST', signal: lifetime.signal, body: { idempotency_key: key, content_hash: item.content_hash,
          ...(providerAccount ? { provider_account: providerAccount } : {}), ...(effort ? { effort } : {}) },
      }); check(); template = imported.template;
    } else template = item;
    const workerName = name.trim() || item.name;
    const instanceKey = await expertRequestKey([exactOwner, template.template_id, workerName]); check();
    const created = await request(`/api/workspace-templates/${encodeURIComponent(template.template_id)}/instantiate`, {
      method: 'POST', signal: lifetime.signal, body: { idempotency_key: instanceKey, name: workerName },
    }); check();
    const worker = created.workspace;
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
    tell(`${exact.name || workerName} is ready for this job.`);
    return exact;
  }
  render();
  return { update, prepare, selection: () => selected, targetLabel: () => selected?.name || '', clear: () => choose(null), owner: () => owner };
}
