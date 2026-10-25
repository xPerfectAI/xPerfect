import { attachActionMenu } from './action-menu.js?v=20261024polish1';
import { readAuthCookie } from './auth-cookies.js?v=20261024cookies1';
import { attachLibraryPanel } from './library-panel.js?v=20261024polish1';
import { createFileDraft, requestFileName, formatBytes } from './files.js?v=20261024polish1';

const uid = () => crypto.randomUUID();
const make = (tag, className = '', text = '') => { const element = document.createElement(tag); if (className) element.className = className; if (text) element.textContent = text; return element; };
const refOf = (item) => ({ item_id: String(item.item_id || ''), version_id: String(item.version_id || item.current_version_id || '') });
const kinds = { note: 'Knowledge', knowledge: 'Reference files', skill: 'Skill', native: 'Tool or plugin', bundle: 'Ability pack', approved: 'Approved tool' };
const libraryPurpose = { note: 'Background and working knowledge', knowledge: 'Source material for the work', skill: 'A specialized way of doing the work', native: 'Connect through the worker’s own setup', bundle: 'Knowledge and abilities, ready to use together', approved: 'Reviewed tools with their own access controls' };
const libraryPaths = { note: '<path d="M4 4h6a2 2 0 0 1 2 2v14a3 3 0 0 0-3-2H4zM20 4h-6a2 2 0 0 0-2 2m0 14a3 3 0 0 1 3-2h5V4"/>', knowledge: '<path d="M14 2H6v20h12V6zM14 2v5h5M9 12h6M9 16h6"/>', skill: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>', native: '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-6 6v4M12 17a6 6 0 0 1-6-6"/>', bundle: '<path d="m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5"/>', approved: '<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6zM8 12l3 3 5-6"/>' };
function libraryIcon(kind) { const icon = make('span', 'library-ability-icon'); icon.setAttribute('aria-hidden', 'true'); icon.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${libraryPaths[kind] || libraryPaths.skill}</svg>`; return icon; }
export function libraryItemMatches(item, scope = 'available', category = 'all') {
  const archived = item.state === 'archived';
  const group = ['native', 'approved'].includes(item.kind) ? 'tools' : 'knowledge';
  return (scope === 'all' || (scope === 'archived' ? archived : !archived)) && (category === 'all' || group === category);
}
export function uniqueLibraryRefs(refs = []) {
  const found = new Map();
  for (const ref of refs) {
    if (!ref || typeof ref.item_id !== 'string' || typeof ref.version_id !== 'string' || !ref.item_id || !ref.version_id) throw new Error('This Library selection has no saved version. Choose it again.');
    if (found.has(ref.item_id) && found.get(ref.item_id).version_id !== ref.version_id) throw new Error('Two versions of the same Library item are selected. Choose one version.');
    found.set(ref.item_id, { item_id: ref.item_id, version_id: ref.version_id });
  }
  return [...found.values()];
}
export function libraryWorkerTarget({ value = '', label = '', name = '' } = {}) {
  if (value.startsWith('open:') || value.startsWith('existing:')) return label || 'the selected saved worker';
  if (value.startsWith('duplicate:')) return `a new copy of ${label || 'the selected saved worker'}`;
  return name.trim() || (label ? `the new ${label}` : 'the new worker');
}
export function skillFolderFiles(files = []) {
  const kept = [], skipped = [];
  for (const file of files) {
    const name = String(file.name || file.webkitRelativePath?.split('/').at(-1) || '');
    if (['.DS_Store', 'Thumbs.db', 'desktop.ini'].includes(name) || name.startsWith('._')) skipped.push(file);
    else kept.push(file);
  }
  return { files: kept, skipped };
}
export function libraryRemovalOutcome(result = {}) {
  const needsRetry = result.native_cleanup?.state === 'unconfirmed';
  const retained = Boolean(result.retained?.length || result.native_cleanup?.retained?.length);
  const capabilityRetained = Boolean(result.retained_capabilities?.length);
  return { needsRetry, message: needsRetry ? 'Removed from future starts. Native files have not been removed yet. '
    : capabilityRetained ? `Removed this Library reference. The capability stays enabled for another selected Library item.${retained ? ' Edited or shared copies were kept.' : ''} `
      : retained ? 'Removed from future context. Edited or shared copies were kept. ' : 'Removed from this worker. ' };
}
export function libraryStatus(item) {
  const state = String(item.state || item.status || item.code || '');
  if (state === 'removed') return item.undo_available ? 'Removed · Undo available' : 'Removed';
  return ({ cleanup_unconfirmed: 'Removed from future starts · Native files still need cleanup', enabled: 'Enabled · Worker load not confirmed', in_files: 'Files ready for the next message', placed: 'Files added · Worker load not confirmed', ready_for_next_start: 'Ready for the next message', needs_review: 'Needs your review', needs_setup: 'Needs native setup', not_available: 'Not available for this worker type', unavailable: 'Not available for this worker type' })[state] || state.replaceAll('_', ' ') || 'Availability not confirmed';
}
function csrfToken() {
  return readAuthCookie('glasshive_csrf');
}
export async function libraryRequest(path, { method = 'GET', body, signal } = {}) {
  const csrf = csrfToken();
  const response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store', signal,
    headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(csrf ? { 'X-GlassHive-CSRF': csrf } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) {
    let detail; try { detail = (await response.json()).detail; } catch { /* Keep an actionable local error. */ }
    const error = new Error(Array.isArray(detail) ? detail.map(issue => issue.msg || 'Check the selected fields.').join(' ') : typeof detail === 'object' && detail ? [detail.message, detail.recovery].filter(Boolean).join(' ') || `Library request failed (${response.status}).` : detail || `Library request failed (${response.status}).`);
    error.status = response.status; error.code = detail?.code; throw error;
  }
  return response.status === 204 ? {} : response.json();
}
function modal(title, { action = '', cancelLabel = '', onSave, onClose, signal: ownerSignal } = {}) {
  const returnFocus = document.activeElement; const dialog = make('dialog', 'personal-library-dialog'); const form = make('form', 'personal-library-form');
  const head = make('div', 'personal-library-dialog-head'); const heading = make('h2', '', title); heading.id = `library-title-${uid()}`;
  dialog.setAttribute('aria-labelledby', heading.id);
  const close = make('button', 'personal-library-close', '×'); close.type = 'button'; close.setAttribute('aria-label', 'Close dialog');
  head.append(heading, close); const body = make('div', 'personal-library-form-body'); const error = make('p', 'personal-library-error'); error.setAttribute('role', 'alert'); error.hidden = true;
  const actions = make('div', 'personal-library-dialog-actions'); const cancel = make('button', 'secondary-button', cancelLabel || (action ? 'Cancel' : 'Close')); cancel.type = 'button';
  const save = make('button', 'primary-button', action); save.type = 'submit'; save.hidden = !action; actions.append(cancel, save); form.append(body, error, actions); dialog.append(head, form);
  let busy = false; let controller = new AbortController();
  const closeDialog = () => { if (!busy) dialog.close(); };
  const ownerChanged = () => dialog.close();
  ownerSignal?.addEventListener('abort', ownerChanged, { once: true });
  close.onclick = cancel.onclick = closeDialog;
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { ownerSignal?.removeEventListener('abort', ownerChanged); controller.abort(); onClose?.(); dialog.remove(); if (returnFocus?.isConnected && !returnFocus.disabled) returnFocus.focus(); }, { once: true });
  form.addEventListener('input', () => { error.hidden = true; error.textContent = ''; });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!action || !onSave || busy || !form.reportValidity()) return;
    busy = true; save.disabled = close.disabled = cancel.disabled = true;
    try { await onSave?.({ body, signal: controller.signal }); dialog.close(); }
    catch (issue) { if (!controller.signal.aborted) { error.textContent = issue.message || 'Could not save. Your changes are still here.'; error.hidden = false; } }
    finally { busy = false; save.disabled = close.disabled = cancel.disabled = false; }
  });
  document.body.append(dialog); dialog.showModal(); if (ownerSignal?.aborted) dialog.close();
  return { dialog, body, form, error, save, signal: controller.signal, showError(message) { error.textContent = message; error.hidden = false; }, close: closeDialog };
}
export { modal as libraryModal };
function field(label, { value = '', textarea = false, required = true, placeholder = '' } = {}) {
  const wrapper = make('label', 'personal-library-field'); const title = make('span', '', label); const input = make(textarea ? 'textarea' : 'input');
  if (!textarea) input.type = 'text'; else input.rows = 5;
  input.value = value; input.required = required; input.placeholder = placeholder; wrapper.append(title, input); return { wrapper, input };
}
const itemsOf = (data) => Array.isArray(data) ? data : data?.items || [];

/** One owner-scoped view. Native installation and file bytes remain with their existing owners. */
export function createLibraryBrowser({ mount, workerId = '', request = libraryRequest, getOwnerScope = () => '', onChange = () => {}, onPick = null, pickLabel = 'Use selected', initialRefs = [], approved = true } = {}) {
  const selected = new Map(uniqueLibraryRefs(initialRefs).map(ref => [ref.item_id, ref])); let items = []; let catalog = []; let epoch = 0; let enabled = false; let owner = ''; let busy = false; let pickAvailable = true; let loadSequence = 0; const captureKeys = new Map(); let ownerLifecycle = new AbortController();
  const pendingDialogs = new Set();
  mount.classList.add('personal-library');
  const toolbar = make('div', 'personal-library-toolbar'); const search = make('input'); search.type = 'search'; search.placeholder = 'Find knowledge, skills or tools…'; search.setAttribute('aria-label', 'Search My Library');
  const add = make('details', 'personal-library-add'); const summary = make('summary', '', '＋ Add'); summary.setAttribute('aria-label', 'Add to My Library'); const menu = make('div', 'personal-library-add-menu'); add.append(summary, menu);
  attachActionMenu(add, summary, menu);
  toolbar.append(search, add); const options = make('div', 'personal-library-options');
  let scope = 'available'; let category = 'all';
  try { const prefs = JSON.parse(localStorage.getItem('xperfect.display.library-filter') || '{}'); if (['available', 'archived', 'all'].includes(prefs.scope)) scope = prefs.scope; if (['all', 'knowledge', 'tools'].includes(prefs.category)) category = prefs.category; } catch { /* Optional display preference. */ }
  const filters = make('div', 'personal-library-filters');
  function filterGroup(label, entries, current, change) {
    const group = make('div', 'library-filter-group'); group.setAttribute('role', 'group'); group.setAttribute('aria-label', label);
    for (const [value, title] of entries) { const button = make('button', '', title); button.type = 'button'; button.dataset.value = value; button.setAttribute('aria-pressed', String(value === current)); button.onclick = () => { for (const peer of group.children) peer.setAttribute('aria-pressed', String(peer === button)); change(value); try { localStorage.setItem('xperfect.display.library-filter', JSON.stringify({ scope, category })); } catch { /* Optional preference. */ } }; group.append(button); }
    return group;
  }
  filters.append(filterGroup('Library availability', [['available', 'Active'], ['archived', 'Archived'], ['all', 'All']], scope, value => { scope = value; void reload(); }), filterGroup('Knowledge and tools', [['all', 'Everything'], ['knowledge', 'Knowledge & skills'], ['tools', 'Tools & plugins']], category, value => { category = value; render(); }));
  const refreshButton = make('button', 'quiet-button', 'Refresh'); refreshButton.type = 'button'; options.append(filters, refreshButton);
  options.title = 'Active means available in your Library. Added here shows what this worker has received.';
  const status = make('p', 'personal-library-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const list = make('div', 'personal-library-list'); list.setAttribute('aria-label', 'Saved Library items'); const footer = make('div', 'personal-library-footer');
  const count = make('span'); const choose = make('button', 'primary-button', pickLabel); choose.type = 'button'; footer.append(count, choose); footer.hidden = !onPick;
  mount.replaceChildren(toolbar, options, status, list, footer);
  const tell = (text = '') => { status.textContent = text; status.hidden = !text; };
  const live = token => enabled && token === epoch;
  const currentSelection = () => uniqueLibraryRefs([...selected.values()]);
  function track(view) { pendingDialogs.add(view); view.dialog.addEventListener('close', () => pendingDialogs.delete(view), { once: true }); return view; }
  const post = (path, body) => request(path, { method: 'POST', body });
  async function reload() {
    if (!enabled) return; const token = epoch; const sequence = ++loadSequence; const fresh = () => live(token) && sequence === loadSequence;
    tell('Loading your Library…');
    const catalogRequest = (approved ? request('/api/control-plane?sections=resources') : Promise.resolve({ library: [] })).then(data => ({ data }), error => ({ error }));
    try {
      const personal = await request(`/api/personal-library?include_archived=${scope !== 'available' ? 'true' : 'false'}`);
      if (!fresh()) return; items = itemsOf(personal); tell(''); render();
    } catch (error) { if (fresh()) { list.replaceChildren(); tell(`Could not load your Library: ${error.message}`); } }
    const result = await catalogRequest; if (!fresh()) return; if (result.error) tell(`Your saved items are available. Approved catalog could not load: ${result.error.message}`); else { catalog = (result.data.library || []).filter(item => item.status === 'available'); render(); }
  }
  async function save(payload, key = uid()) {
    const token = epoch; const result = await post('/api/personal-library', { ...payload, idempotency_key: key });
    if (!live(token)) throw new Error('Your account changed. Open Library again before continuing.');
    onChange(result); return result;
  }
  async function resolve(refs = currentSelection()) { return post('/api/personal-library/resolve', { refs }); }
  async function ensureApproved(item, requestKey) {
    const data = await save({ name: String(item.manifest?.label || item.manifest?.name || item.stable_id || 'Approved capability'), kind: 'approved', library_id: item.library_id }, requestKey);
    await reload(); return refOf(data.item || data);
  }
  function chooseVersion(item) {
    const token = epoch; const view = track(modal('Saved versions')); view.body.append(make('p', 'personal-library-help', 'Choose the exact version to use. Existing work keeps its accepted version.'));
    void request(`/api/personal-library/${encodeURIComponent(item.item_id)}`).then(detail => {
      if (!live(token) || !view.dialog.isConnected) return;
      for (const version of detail.versions || []) {
        const button = make('button', 'personal-library-version', `Version ${version.version} · ${new Date(version.created_at * 1000).toLocaleString()}`); button.type = 'button';
        button.onclick = async () => { button.disabled = true; try { const exact = await request(`/api/personal-library/${encodeURIComponent(item.item_id)}?version_id=${encodeURIComponent(version.version_id)}`); if (!live(token) || !view.dialog.isConnected) return; selected.set(item.item_id, refOf(exact)); if (onPick) { view.close(); render(); } else { view.close(); await showDetail(exact); } } catch (error) { if (live(token)) view.showError(error.message); } finally { button.disabled = false; } };
        view.body.append(button);
      }
    }).catch(error => view.showError(error.message));
  }
  function editor(kind, item = null) {
    add.open = false; const token = epoch; const createKey = uid(); const payload = item?.payload || {}; const label = kind === 'bundle' ? 'Create a bundle' : kind === 'note' ? 'Write a note' : kind === 'native' ? 'Save a native source' : kind === 'skill' ? 'Add a skill folder' : 'Save files in My Library';
    const name = field('Name', { value: item?.name || '', placeholder: 'Give this a useful name' }); let text, source, profile, draft, folder;
    const chosenRefs = item?.refs || currentSelection().flatMap(ref => { const saved = items.find(value => value.item_id === ref.item_id); return saved?.kind === 'bundle' ? saved.refs || [] : [ref]; });
    const refs = new Map(kind === 'bundle' ? chosenRefs.map(ref => [ref.item_id, ref]) : []);
    const view = track(modal(item ? 'Edit saved item' : label, { action: item ? 'Save new version' : 'Save', onClose: () => draft?.setOwnerScope(null), onSave: async () => {
      if (!live(token)) throw new Error('Your account changed. Open the editor again.');
      let next = { name: name.input.value.trim(), kind };
      if (kind === 'note') next.text = text.input.value;
      if (kind === 'native') { next.source_uri = source.input.value.trim(); next.profile = profile.value; if (payload.package_version) next.package_version = payload.package_version; }
      if (kind === 'bundle') { next.refs = uniqueLibraryRefs([...refs.values()]); if (!next.refs.length) throw new Error('Choose at least one item for this bundle.'); }
      if (kind === 'knowledge') { if (draft.blocked()) throw new Error('Wait for uploads, retry or remove failed files.'); const ready = draft.readyRefs(); if (!ready.length && !item) throw new Error('Add at least one file.'); next.upload_ids = ready.length ? ready.map(ref => ref.upload_id) : item.uploads.map(ref => ref.upload_id); next.upload_revisions = ready.length ? ready.map(ref => ref.revision) : item.uploads.map(ref => ref.revision); }
      if (kind === 'skill') {
        const picked = [...(folder.files || [])]; const { files } = skillFolderFiles(picked);
        if (!files.length && (!item || picked.length)) throw new Error('Choose a complete skill folder with text files. System files are skipped.');
        next.profiles = ['claude-code'];
        if (files.length) {
          if (files.length > 128) throw new Error('A skill can contain up to 128 text files.');
          if (files.reduce((total, file) => total + file.size, 0) > 4 * 1024 * 1024) throw new Error('This skill is too large for text projection. Use its native package setup.');
          const decoder = new TextDecoder('utf-8', { fatal: true }); next.files = [];
          for (const file of files) {
            if (file.size > 1024 * 1024) throw new Error(`${file.name} is too large for a text skill. Use its native package setup.`);
            const relative = String(file.webkitRelativePath || file.name); const parts = relative.split('/'); const packageName = parts.length > 1 ? parts[0] : 'saved-skill'; const within = parts.length > 1 ? parts.slice(1).join('/') : file.name;
            let content; try { content = decoder.decode(await file.arrayBuffer()); } catch { throw new Error(`${file.name} is not a text file. Use native package setup to keep its binary assets intact.`); }
            next.files.push({ path: `.claude/skills/${packageName}/${within}`, content });
          }
        } else next.files = payload.files;
      }
      if (kind === 'approved') next.library_id = payload.library_id;
      if (!live(token)) throw new Error('Your account changed. Open the editor again.');
      if (item) await request(`/api/personal-library/${encodeURIComponent(item.item_id)}`, { method: 'PATCH', body: { ...next, expected_version_id: item.current_version_id || item.version_id } });
      else await save(next, createKey);
      if (draft) draft.reset(); if (live(token)) { await reload(); onChange(); }
    } }));
    view.body.append(name.wrapper);
    if (kind === 'note') { text = field('What should the worker know?', { textarea: true, value: payload.text || '' }); view.body.append(text.wrapper); }
    if (kind === 'native') {
      source = field('Source', { value: payload.source_uri || '', placeholder: 'A supported native source or package URL' });
      const wrapper = make('label', 'personal-library-field'); wrapper.append(make('span', '', 'Worker type')); profile = make('select');
      for (const [value, title] of [['', 'Choose when using it'], ['claude-code', 'Claude Code'], ['codex-cli', 'Codex'], ['grok-build', 'Grok Build']]) { const option = make('option', '', title); option.value = value; profile.append(option); } profile.value = payload.profile || '';
      wrapper.append(profile); view.body.append(source.wrapper, wrapper, make('p', 'personal-library-help', 'Saving a source does not install it or copy an account. The worker’s native setup keeps its permissions.'));
    }
    if (kind === 'bundle') {
      view.body.append(make('p', 'personal-library-help', 'Choose the exact items to reuse together. A bundle grants no new tools.'));
      for (const child of items.filter(value => value.kind !== 'bundle' && value.state !== 'archived')) {
        const row = make('label', 'personal-library-check'); const check = make('input'); check.type = 'checkbox'; check.checked = refs.has(child.item_id);
        check.onchange = () => check.checked ? refs.set(child.item_id, refOf(child)) : refs.delete(child.item_id); row.append(check, make('span', '', `${child.name} · v${child.version}`)); view.body.append(row);
      }
      if (!items.some(value => value.kind !== 'bundle')) view.body.append(make('p', 'personal-library-help', 'Save some items first, then group them here.'));
    }
    if (kind === 'skill') {
      const wrapper = make('label', 'personal-library-field'); wrapper.append(make('span', '', 'Complete skill folder')); folder = make('input'); folder.type = 'file'; folder.multiple = true; folder.setAttribute('webkitdirectory', ''); folder.setAttribute('directory', ''); wrapper.append(folder);
      const skipped = make('p', 'personal-library-help'); skipped.setAttribute('role', 'status'); skipped.hidden = true;
      folder.onchange = () => { const result = skillFolderFiles([...(folder.files || [])]); skipped.hidden = !result.skipped.length; skipped.textContent = result.skipped.length ? `Skipped ${result.skipped.length} system file${result.skipped.length === 1 ? '' : 's'}.` : ''; };
      view.body.append(wrapper, skipped, make('p', 'personal-library-help', 'Keeps the skill instructions and supporting text files. Claude Code project skills are supported here. Binary packages and other native types use their own setup.'));
    }
    if (kind === 'knowledge') {
      const input = make('input'); input.type = 'file'; input.multiple = true; input.hidden = true; const addFiles = make('button', 'secondary-button', 'Add files'); addFiles.type = 'button';
      addFiles.onclick = () => { if (live(token)) input.click(); };
      const drop = make('div', 'file-drop', 'Drop files here'); drop.tabIndex = 0; drop.setAttribute('role', 'button'); drop.setAttribute('aria-label', 'Choose files or drop them here');
      const list = make('ul', 'project-file-list'); const help = make('p', 'personal-library-help'); const budget = make('p', 'file-budget');
      view.body.append(addFiles, input, drop, list, help, budget);
      draft = createFileDraft({ input, drop, list, help, budget, addButton: addFiles, scope: 'personal-library', csrf: csrfToken, onChange: ({ blocked }) => { view.save.disabled = blocked; } });
      draft.setOwnerScope(owner);
      if (item?.uploads?.length) view.body.append(make('p', 'personal-library-help', 'Add replacements to save a new file version, or save a new name using the retained files.'));
    }
    name.input.focus(); return view;
  }
  async function showDetail(item) {
    const token = epoch; const detail = await request(`/api/personal-library/${encodeURIComponent(item.item_id)}?version_id=${encodeURIComponent(item.version_id)}`); if (!live(token)) return;
    const view = track(modal(detail.name)); view.body.append(make('p', 'personal-library-help', `${kinds[detail.kind] || detail.kind} · Version ${detail.version}`));
    if (detail.payload?.description) view.body.append(make('p', 'library-detail-description', detail.payload.description));
    if (detail.kind === 'note') { const text = make('pre', 'personal-library-note', detail.payload?.text || ''); view.body.append(text); }
    for (const upload of detail.uploads || []) view.body.append(make('p', '', `${upload.name} · ${formatBytes(upload.size_bytes)}`));
    if (detail.payload?.source_uri) view.body.append(make('code', 'personal-library-source', detail.payload.source_uri));
    if (detail.payload?.files) { const technical = make('details', 'library-native-details'); technical.append(make('summary', '', 'Included files')); const names = make('ul'); for (const file of detail.payload.files) names.append(make('li', '', file.path)); technical.append(names); view.body.append(technical); }
    if (detail.kind === 'native') view.body.append(make('p', 'personal-library-help', 'Use the existing native setup with a worker. A saved reference is not a loaded tool.'));
    if (detail.kind === 'bundle') for (const ref of detail.refs || []) { const child = items.find(value => value.item_id === ref.item_id); view.body.append(make('p', '', `${child?.name || ref.item_id} · saved version`)); }
  }
  async function confirmDelete(item) {
    const token = epoch;
    const detail = await request(`/api/personal-library/${encodeURIComponent(item.item_id)}`);
    if (!live(token)) return;
    const view = track(modal('Delete from My Library?', { action: 'Delete saved item', onSave: async () => {
      if (!live(token)) throw new Error('Your account changed. Open Library again.');
      await request(`/api/personal-library/${encodeURIComponent(item.item_id)}?confirm=true`, { method: 'DELETE' });
      if (!live(token)) return; selected.delete(item.item_id); await reload();
    } }));
    view.body.append(make('p', '', `${detail.name} will be removed from your Library. This does not uninstall native tools or delete workspace copies.`));
    if (detail.holders?.length) {
      view.body.append(make('p', 'personal-library-help', 'These saved references will keep their exact versions:'));
      const holders = make('ul', 'personal-library-holders');
      const labels = { bundle: 'Bundle', template: 'Template', application: 'Worker', conversation: 'Chat', turn: 'Message', conversation_turn: 'Message', removal_undo: 'Recovery' };
      for (const holder of detail.holders) {
        const type = labels[holder.holder_kind] || String(holder.holder_kind || 'Saved reference').replaceAll('_', ' ');
        holders.append(make('li', '', `${holder.label || `${type}: ${holder.holder_id}`}${holder.version ? ` · v${holder.version}` : ''}`));
      }
      view.body.append(holders);
    } else view.body.append(make('p', 'personal-library-help', 'No saved work, bundles or templates currently keep this item.'));
  }
  function render() {
    if (!enabled) return;
    const focusedRow = document.activeElement?.closest('.personal-library-item');
    const focusedItem = focusedRow?.parentNode === list ? focusedRow.dataset.itemId : '';
    const query = search.value.trim().toLocaleLowerCase(); list.replaceChildren();
    for (const item of items.filter(value => libraryItemMatches(value, scope, category) && `${value.name} ${kinds[value.kind] || value.kind}`.toLocaleLowerCase().includes(query))) {
      const row = make('article', 'personal-library-item'); row.dataset.itemId = item.item_id; const body = make('div', 'personal-library-item-copy'); const title = make('button', 'personal-library-name', item.name); title.type = 'button'; title.onclick = () => void showDetail({ ...item, version_id: selected.get(item.item_id)?.version_id || item.version_id }).catch(error => tell(error.message));
      body.append(title, make('small', '', `${kinds[item.kind] || item.kind} · ${selected.has(item.item_id) && selected.get(item.item_id).version_id !== item.version_id ? 'Earlier saved version selected' : `v${item.version}`}${item.state === 'archived' ? ' · Archived' : ''}`));
      if (item.uploads?.length) body.append(make('small', '', `${item.uploads.length} file${item.uploads.length === 1 ? '' : 's'}`));
      if (onPick) { const check = make('input'); check.type = 'checkbox'; check.checked = selected.has(item.item_id); check.setAttribute('aria-label', `Select ${item.name}`); check.disabled = item.state === 'archived'; check.onchange = () => { if (check.checked) selected.set(item.item_id, refOf(item)); else selected.delete(item.item_id); title.setAttribute('aria-pressed', String(check.checked)); syncCount(); }; title.disabled = check.disabled; title.setAttribute('aria-pressed', String(check.checked)); title.onclick = () => check.click(); row.append(check); }
      row.append(libraryIcon(item.kind), body);
      body.append(make('p', 'library-ability-purpose', item.payload?.description || libraryPurpose[item.kind] || 'Reusable knowledge and abilities'));
      const menu = make('details', 'file-row-menu'); const summary = make('summary', '', '⋯'); summary.setAttribute('aria-label', `Options for ${item.name}`); const actions = make('div', 'file-row-menu-items');
      for (const [title, handler] of [
        ['Details', () => showDetail({ ...item, version_id: selected.get(item.item_id)?.version_id || item.version_id })],
        ['Edit', async () => { const token = epoch; const detail = await request(`/api/personal-library/${encodeURIComponent(item.item_id)}`); if (live(token)) editor(detail.kind, detail); }], ['Versions', () => chooseVersion(item)],
        [item.state === 'archived' ? 'Restore' : 'Archive', async () => { await post(`/api/personal-library/${encodeURIComponent(item.item_id)}/archive`, { archived: item.state !== 'archived', expected_version_id: item.current_version_id || item.version_id }); await reload(); }],
        ['Delete from My Library', () => confirmDelete(item)],
      ]) { const button = make('button', '', title); button.type = 'button'; button.setAttribute('aria-label', title); button.onclick = () => { menu.open = false; summary.focus(); Promise.resolve(handler()).catch(error => tell(error.message)); }; actions.append(button); }
      menu.append(summary, actions); attachActionMenu(menu, summary, actions); row.append(menu); list.append(row);
    }
    for (const item of catalog.filter(value => scope !== 'archived' && category !== 'knowledge' && !items.some(personal => personal.kind === 'approved' && personal.payload?.library_id === value.library_id) && `${value.manifest?.label || value.manifest?.name || value.stable_id || ''}`.toLocaleLowerCase().includes(query))) {
      const row = make('article', 'personal-library-item'); const body = make('div', 'personal-library-item-copy'); body.append(make('strong', '', item.manifest?.label || item.manifest?.name || item.stable_id || 'Approved capability'), make('small', '', `Approved · v${item.version}`)); const button = make('button', 'secondary-button', onPick ? 'Select' : 'Review'); button.type = 'button'; const requestKey = `approved.${item.library_id}.${uid()}`;
      button.onclick = async () => { button.disabled = true; try { if (onPick) { const ref = await ensureApproved(item, requestKey); selected.set(ref.item_id, ref); render(); } else { window.dispatchEvent(new CustomEvent('xperfect:approved-library', { detail: { libraryId: item.library_id } })); } } catch (error) { tell(error.message); } finally { button.disabled = false; } }; row.append(body, button); list.append(row);
    }
    if (!list.children.length) list.append(make('p', 'personal-library-empty', query ? 'No matching items. Try another name.' : 'No items in this view. Add an item or change the filters.'));
    if (focusedItem) {
      const retained = [...list.children].find(row => row.dataset.itemId === focusedItem);
      (retained?.querySelector('summary') || search).focus();
    }
    syncCount();
  }
  function syncCount() { count.textContent = `${selected.size} selected`; choose.disabled = !selected.size || busy || !pickAvailable; }
  choose.onclick = async () => { if (busy || !pickAvailable) return; const token = epoch; busy = true; syncCount(); tell('Checking saved versions…'); try { const refs = currentSelection(); const resolved = await resolve(refs); if (!live(token)) return; await onPick?.(refs, resolved, { signal: ownerLifecycle.signal }); if (live(token)) tell(''); } catch (error) { if (live(token)) tell(error.message); } finally { busy = false; syncCount(); } };
  for (const [title, kind] of [['Add files', 'knowledge'], ['Write a note', 'note'], ['Add a skill folder', 'skill'], ['Add from source', 'native'], ['Create a bundle', 'bundle']]) { const action = make('button', '', title); action.type = 'button'; action.setAttribute('aria-label', title); action.onclick = () => { summary.focus(); editor(kind); }; menu.append(action); }
  search.oninput = render; refreshButton.onclick = () => void reload();
  return { refresh: reload, resolve, selection: currentSelection, signal: () => ownerLifecycle.signal, setPickAvailable(value, reason = '') { pickAvailable = value === true; choose.title = pickAvailable ? '' : reason; syncCount(); }, setAccess(value, ownerScope = getOwnerScope()) {
    const next = Boolean(value) && Boolean(ownerScope); if (next === enabled && owner === ownerScope) return;
    const changedOwner = Boolean(owner) && owner !== ownerScope; ownerLifecycle.abort(); ownerLifecycle = new AbortController(); epoch += 1; owner = ownerScope || ''; enabled = next; mount.hidden = !enabled; items = []; catalog = []; if (changedOwner || !next) { selected.clear(); captureKeys.clear(); } for (const view of pendingDialogs) view.dialog.close(); list.replaceChildren();
    if (enabled) void reload();
  }, async capture(file, returnFocus) { if (!enabled || !workerId) return; const token = epoch; const name = await requestFileName({ title: 'Keep for reuse', value: file.display_name || file.name || file.path?.split('/').at(-1) || 'Saved file', returnFocus, signal: ownerLifecycle.signal }); if (!name?.trim() || !live(token)) return; const signature = JSON.stringify([owner, file.file_id, file.revision, name.trim()]); if (!captureKeys.has(signature)) captureKeys.set(signature, uid()); const saved = await post(`/api/workspace/${encodeURIComponent(workerId)}/library/capture`, { file_id: file.file_id, revision: file.revision, name: name.trim(), idempotency_key: captureKeys.get(signature) }); captureKeys.delete(signature); if (live(token)) { await reload(); tell(`${name.trim()} saved privately in My Library.`); return saved; } }, destroy() { ownerLifecycle.abort(); epoch += 1; enabled = false; for (const view of pendingDialogs) view.dialog.close(); mount.replaceChildren(); } };
}

export function openLibraryPicker({ title = 'What knowledge and abilities does this worker need?', refs = [], ownerScope, request = libraryRequest, onChoose, signal } = {}) {
  return new Promise(resolve => {
    let accepted = false; let browser;
    const abort = () => view.dialog.close();
    let disposeSizing;
    const view = modal(title, { cancelLabel: 'Cancel', onClose: () => { disposeSizing?.(); signal?.removeEventListener('abort', abort); browser?.destroy(); if (!accepted) resolve(null); } });
    view.dialog.classList.add('personal-library-picker-dialog');
    disposeSizing = attachLibraryPanel(view.dialog, { dialog: true });
    if (signal?.aborted) { view.close(); return; }
    signal?.addEventListener('abort', abort, { once: true });
    view.body.classList.add('personal-library-picker');
    browser = createLibraryBrowser({ mount: view.body, request, initialRefs: refs, onPick: async (chosen, resolved) => { await onChoose?.(chosen, resolved); accepted = true; view.close(); resolve({ refs: chosen, resolved }); } });
    const footer = view.body.querySelector('.personal-library-footer');
    const actions = view.form.querySelector('.personal-library-dialog-actions');
    actions.append(footer.querySelector('.primary-button'));
    footer.append(actions);
    view.form.append(footer);
    browser.setAccess(true, ownerScope);
  });
}

/** Only IDs/labels are retained; file bytes remain in the canonical upload store. */
export function createLibrarySelection({ button, list, scope, getOwnerScope = () => '', getTargetLabel = () => 'this worker', request = libraryRequest, onChange = () => {} } = {}) {
  let owner = ''; let refs = []; let labels = []; let generation = 0; let selectionScope = scope; let pickerController; let ownerLifecycle = new AbortController();
  const storage = () => `xperfect.library.selection.${selectionScope}.${owner}`;
  function persist() { try { if (owner) sessionStorage.setItem(storage(), JSON.stringify({ refs, labels })); } catch { /* Page-local selection still works. */ } }
  function render() {
    list.replaceChildren(); button.hidden = !owner;
    refs.forEach((ref, index) => { const row = make('li', 'personal-library-selected'); row.append(make('span', '', labels[index] || 'Saved Library item')); const remove = make('button', 'quiet-button', 'Remove'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove ${labels[index] || 'Library item'} from this selection`); remove.onclick = () => { refs.splice(index, 1); labels.splice(index, 1); persist(); render(); onChange(); }; row.append(remove); list.append(row); });
  }
  button.addEventListener('click', async () => {
    if (!owner) return; pickerController?.abort(); pickerController = new AbortController();
    const token = generation; const next = await openLibraryPicker({ title: `What specialized knowledge and abilities does ${getTargetLabel()} require for its work?`, refs, ownerScope: owner, request, signal: pickerController.signal }); if (!next || token !== generation) return;
    refs = next.refs; labels = refs.map(ref => { const item = next.resolved.items?.find(item => item.item_id === ref.item_id); return item ? `${item.name} · v${item.version}` : 'Saved Library item'; }); persist(); render(); onChange();
  });
  return { refs: () => refs.map(ref => ({ ...ref })), signal: () => ownerLifecycle.signal, setRefs(next, names = []) { refs = uniqueLibraryRefs(next); labels = names; persist(); render(); }, setScope(next) { const oldKey = storage(); selectionScope = next; persist(); try { sessionStorage.removeItem(oldKey); } catch { /* Hints are optional. */ } }, async resolve(chosen = refs) { return chosen.length ? request('/api/personal-library/resolve', { method: 'POST', body: { refs: chosen } }) : { items: [], upload_ids: [], upload_revisions: [], pending: [] }; }, setOwnerScope(value) { const next = String(value || ''); if (next === owner) return; pickerController?.abort(); ownerLifecycle.abort(); ownerLifecycle = new AbortController(); owner = next; generation += 1; refs = []; labels = []; if (owner) { try { const saved = JSON.parse(sessionStorage.getItem(storage()) || '{}'); refs = uniqueLibraryRefs(saved.refs || []); labels = Array.isArray(saved.labels) ? saved.labels.map(String) : []; } catch { /* Ignore damaged hints. */ } } render(); }, clear() { refs = []; labels = []; persist(); render(); }, destroy() { pickerController?.abort(); ownerLifecycle.abort(); generation += 1; owner = ''; refs = []; labels = []; render(); } };
}

export function reviewLibraryUse(items, target = 'this worker', { signal, profile = '' } = {}) {
  return new Promise(resolve => {
    let accepted = false;
    const view = modal(`Use these items with ${target}?`, { signal, action: 'Use supported items', onSave: async () => { accepted = true; }, onClose: () => resolve(accepted) });
    view.body.append(make('p', 'personal-library-help', 'Knowledge stays in Files. Native skills or tools apply only where supported; anything that needs setup or a restart stays visible. Existing tool approvals are still required.'));
    const list = make('ul');
    for (const item of items) {
      const profiles = item.payload?.profiles || [];
      const unavailable = item.kind === 'skill' && profile && profiles.length && !profiles.includes(profile);
      const effect = unavailable ? ' · Not available for this worker type' : item.kind === 'native' ? ' · Needs native setup' : '';
      const version = Number.isInteger(item.version) && item.version > 0 ? ` · v${item.version}` : '';
      list.append(make('li', '', `${item.name || item.item_id}${version} · ${kinds[item.kind] || item.kind}${effect}`));
    }
    view.body.append(list);
  });
}

export function askNativeLibrarySetup({ workerId, item, request = libraryRequest, signal, canStart = () => true } = {}) {
  const message = field('What should this worker add?', { textarea: true, placeholder: 'Describe what you want to set up, using the saved source below.' });
  const source = String(item?.payload?.source_uri || '');
  const view = modal('Use the worker’s native setup', { signal, action: 'Ask this worker', onSave: async () => {
    if (!canStart()) throw new Error('This workspace is closed. You can still save and reuse its files.');
    await request(`/api/workspace/${encodeURIComponent(workerId)}/message`, { method: 'POST', body: { message: message.input.value + (source ? `\n\nSource: ${source}` : '') } });
  } });
  view.body.append(message.wrapper);
  if (item?.payload?.source_uri) view.body.append(make('code', 'personal-library-source', item.payload.source_uri));
  view.body.append(make('p', 'personal-library-help', 'Your request and the exact source shown above are sent together to this worker. Native sign-in, new access and package installation stay visible in the live workspace. A reply does not by itself prove that a plugin loaded.'));
  message.input.focus(); return view;
}
