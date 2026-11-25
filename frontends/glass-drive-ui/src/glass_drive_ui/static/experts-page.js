/* The Experts page: an Expert's whole setup on one page, in Run project's style. Saving makes a new version. */
import { libraryRequest, openLibraryPicker } from './personal-library.js?v=20261024polish1';
import { AIS, ASK, DEFAULT_EXPERT, aiMark, expertSaved, openChannels, openExpertChat, squarePng } from './experts.js?v=20261125page1';

const node = (tag, cls = '', text = '') => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
const ICONS = {
  experts: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 5.2a3 3 0 0 1 0 5.6M18 19a5.5 5.5 0 0 0-2.6-4.7"/>',
  does: '<rect x="3" y="7" width="18" height="13" rx="2.5"/><path d="M8.5 7V5.5A1.5 1.5 0 0 1 10 4h4a1.5 1.5 0 0 1 1.5 1.5V7M3 12.5h18"/>',
  identity: '<path d="M5 4h10l4 4v12H5z"/><path d="M14.5 4v4.5H19M8.5 12.5h7M8.5 16h5"/>',
  knowledge: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5zM4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5"/>',
  team: '<circle cx="12" cy="7.5" r="3"/><circle cx="5.5" cy="15.5" r="2.5"/><circle cx="18.5" cy="15.5" r="2.5"/><path d="M10 10l-3 3.5M14 10l3 3.5"/>',
  ask: '<path d="M4 5h16v11H9l-5 4z"/><path d="M9.8 9a2.3 2.3 0 1 1 3 2.2c-.6.2-.8.6-.8 1.2M12 14.3h.01"/>',
  where: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
};
const icon = name => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'onboarding-icon'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '1.7'); svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
  svg.innerHTML = ICONS[name]; return svg;
};
let uid = 0; const nextId = prefix => `${prefix}-${++uid}`;
const AI_NAME = Object.fromEntries(AIS);
const TALKERS = new Set(['claude-code', 'codex-cli']);
const PLACES = [['host', 'This computer'], ['sandboxed', 'Sandboxed workspace']];
const LEVELS = { host: 'Full access', sandboxed: 'Its workspace only' };
const LEVEL_VALUES = { host: 'full', sandboxed: 'workspace' };
const PLACE_HINTS = {
  host: 'It can read and change what you can on this computer. Your instructions and Ask me first are the guard, not a wall. Narrower permissions on this computer come later.',
  sandboxed: 'It works in its own sandboxed workspace and its Workers reach only that workspace. Claude talks and coordinates there with its own tools off, so all the work happens inside the sandbox. This needs containers on the computer or server running xPerfect.',
};

/** A section in Run project's shape: its icon chip and title, then its content. */
function section(name, title, ...content) {
  const box = node('section', 'knowledge-card experts-section'); const heading = node('div', 'section-heading');
  const h2 = node('h2', 'onboarding-title'); h2.id = nextId('experts-title'); h2.append(icon(name), node('span', '', title));
  heading.append(h2); box.setAttribute('aria-labelledby', h2.id); box.append(heading, ...content); return { box, heading };
}
/** Run project's two-part switch (New | Existing), for any short set of choices. */
function toggle(label, options, value, onChange) {
  const set = node('fieldset', 'choice-toggle'); const legend = node('legend', 'sr-only', label); set.append(legend);
  const name = nextId('experts-choice');
  for (const [v, text] of options) {
    const l = node('label'); const input = node('input'); input.type = 'radio'; input.name = name; input.value = v; input.checked = v === value;
    input.onchange = () => onChange(v); l.append(input, node('span', '', text)); set.append(l);
  }
  return set;
}
const help = text => node('small', 'field-help', text);
function field(label, control, hint = '') {
  const l = node('label', 'project-field'); l.append(node('span', '', label), control); if (hint) l.append(help(hint)); return l;
}
/** A model's own name under its AI ("Codex / GPT-6.2 Sol" → "GPT-6.2 Sol"). */
export function modelName(option) {
  const label = String(option?.label || option?.id || '');
  const parts = label.split(' / ');
  return parts.length > 1 ? parts.slice(1).join(' / ') : label.replace(/^[^:]*:/, '');
}

/** The rows of the AI team: per AI its usual model, then each exact model the install offers, then any model this Expert
 *  saved exactly that the list no longer names (kept as saved). */
export function teamRows(options, spec) {
  const byProfile = new Map((options || []).map(item => [item.profile, item]));
  const saved = [spec.team.talk.model, spec.team.coordinate.model, ...spec.team.work.filter(entry => entry.includes(':'))].filter(Boolean);
  return AIS.map(([profile, name]) => {
    const offered = byProfile.get(profile);
    const models = (offered?.models || []).map(m => ({ id: m.id, name: modelName(m), status: m.status || 'available', reason: m.reason || '' }));
    for (const id of saved) {
      if (id.startsWith(profile + ':') && !models.some(m => m.id === id)) models.push({ id, name: id.slice(profile.length + 1), status: 'saved', reason: 'Saved with this Expert; not in this install’s list now.' });
    }
    return { profile, name, usual: offered?.usual || '', models };
  });
}

/** An AI whose tool is not on this computer and that has no connected account: the page says so, with the way to
 *  connect it. Unknown (no data yet) is never shown as missing. */
export function notSetUp(profile, data) {
  const option = (data?.new_workspace_options || []).find(o => o.profile === profile);
  const providers = { 'claude-code': ['claude', 'anthropic'], 'codex-cli': ['codex', 'openai'], 'grok-build': ['grok', 'xai'] }[profile] || [];
  const connected = (data?.provider_accounts || []).some(a => a.status === 'ready' && providers.includes(String(a.provider || '').toLowerCase()));
  return Boolean(data) && option?.host_cli_installed === false && !connected;
}

/** The Expert's settings as the runtime saves them, from what the page shows. */
export function readTeam(state) {
  const work = state.work.filter(Boolean);
  if (!work.length) throw new Error('Choose at least one AI that may work.');
  const backup = role => { const v = state.backup[role]; if (!v) return null; return { profile: v, account_id: '' }; };
  const team = {
    talk: { profile: state.talk.profile, account_id: state.account.talk, backup: backup('talk'), effort: state.replies, model: state.talk.model },
    coordinate: { profile: state.coordinate.profile, account_id: state.account.coordinate, backup: backup('coordinate'), effort: '', model: state.coordinate.model },
    work,
  };
  if (state.preferred && work.includes(state.preferred)) team.preferred = state.preferred;
  return team;
}

export function mountExpertsPage({ root, getData = () => null, request = libraryRequest } = {}) {
  root.replaceChildren();
  const copy = node('div', 'composer-copy');
  const h1 = node('h1', 'onboarding-title'); h1.append(icon('experts'), node('span', '', 'Experts'));
  copy.append(h1, node('p', '', 'Your experts, and how each one works. Choose one to see and change its setup.'));
  const form = node('form', 'composer-form experts-form'); form.noValidate = true;
  const strip = node('div', 'experts-strip'); strip.setAttribute('role', 'group'); strip.setAttribute('aria-label', 'Your experts');
  const status = node('p', 'experts-status'); status.setAttribute('role', 'status');
  const unsaved = node('div', 'experts-unsaved'); unsaved.setAttribute('role', 'alert'); unsaved.hidden = true;
  const body = node('div', 'experts-body');
  form.append(strip, unsaved, status, body); root.append(copy, form);

  let experts = []; let current = null; let spec = null; let options = null; let speeds = {}; let library = [];
  let state = null; let dirty = false; let token = 0; let pictureChange = null; let pictureUrl = ''; let accountsOpen = false; const openAis = new Set(); let lastGridControl = '';
  const say = text => { status.textContent = text || ''; };

  async function loadList() {
    const response = await request('/api/workspace-templates?state=active');
    experts = response.items || [];
  }
  async function loadOptions() {
    if (!options) options = request('/api/expert-ai-options').then(r => r.items || []).catch(() => null);
    if (!Object.keys(speeds).length) {
      request('/api/experts-talk-speeds').then(r => { speeds = Object.fromEntries((r.items || []).map(item => [item.model, item])); if (state) renderTeam(); }).catch(() => {});
    }
    return options;
  }
  function renderStrip() {
    strip.replaceChildren();
    for (const item of experts) {
      const b = node('button', 'experts-pick'); b.type = 'button'; b.setAttribute('aria-pressed', String(current?.lineage_id === item.lineage_id));
      const face = node('span', 'experts-pick-face'); face.setAttribute('aria-hidden', 'true');
      face.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><path d="m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5"/></svg>';
      const known = pictures.get(item.lineage_id);
      if (known) { const img = node('img'); img.src = known; img.alt = ''; face.append(img); }
      b.append(face, node('span', 'experts-pick-name', item.name || 'Expert'));
      b.onclick = () => go(item.template_id); strip.append(b);
    }
    const add = node('button', 'experts-pick experts-new'); add.type = 'button'; add.setAttribute('aria-pressed', String(!current && Boolean(state)));
    add.innerHTML = '<span class="experts-pick-face" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></span>';
    add.append(node('span', 'experts-pick-name', 'New expert')); add.onclick = () => go('new'); strip.append(add);
  }
  const pictures = new Map();
  function loadPictures() {
    for (const item of experts) {
      if (pictures.has(item.lineage_id)) continue; pictures.set(item.lineage_id, '');
      request('/api/experts/' + encodeURIComponent(item.template_id) + '/picture').then(r => {
        const url = typeof r?.picture === 'string' && r.picture.startsWith('data:image/png;base64,') ? r.picture : '';
        if (url) { pictures.set(item.lineage_id, url); renderStrip(); }
      }).catch(() => {});
    }
  }
  const shownId = () => (current ? current.template_id : 'new');
  const pin = id => { const hash = '#experts/' + encodeURIComponent(id); if (location.hash !== hash) history.replaceState(null, '', location.pathname + location.search + hash); };
  function go(id) { void show(id); }
  // Unsaved changes are kept in this tab (a reload brings them back) until they are saved or discarded.
  // A saved expert's draft is keyed by its own id (only its owner can open it), so a reload finds it before the
  // account data has loaded; a new expert's draft needs the owner.
  const draftKey = id => {
    if (id && id !== 'new') return `xperfect.expert-draft:${id}`;
    const owner = getData()?.draft_owner_scope || ''; return owner ? `xperfect.expert-draft:${owner}:new` : '';
  };
  let draftTimer = 0;
  function keepDraft() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      const key = draftKey(shownId()); if (!key || !state || !dirty) return;
      const { offered, ...kept } = state;
      try { sessionStorage.setItem(key, JSON.stringify({ base: current?.template_id || '', state: kept })); } catch { /* The page itself still holds the changes. */ }
    }, 250);
  }
  function dropDraft(id = shownId()) { clearTimeout(draftTimer); const key = draftKey(id); try { if (key) sessionStorage.removeItem(key); } catch { /* Nothing kept. */ } }
  function readDraft(id) {
    const key = draftKey(id); if (!key) return null;
    try { const kept = JSON.parse(sessionStorage.getItem(key) || 'null'); return kept && kept.base === (id === 'new' ? '' : id) ? kept.state : null; } catch { return null; }
  }
  /** One line in the page, never a browser dialog: keep editing, or discard and go where you asked. */
  function askBeforeLeaving(next) {
    const label = state?.name?.trim() || 'this expert';
    unsaved.replaceChildren(node('span', '', `Your changes to ${label} are not saved.`));
    const keep = node('button', 'secondary-button compact-button', 'Keep editing'); keep.type = 'button';
    keep.onclick = () => { unsaved.hidden = true; pin(shownId()); };
    const leave = node('button', 'quiet-button compact-button', 'Discard them'); leave.type = 'button';
    leave.onclick = () => { unsaved.hidden = true; dropDraft(); dirty = false; void show(next, { force: true }); };
    unsaved.append(keep, leave); unsaved.hidden = false; pin(shownId()); keep.focus();
  }

  /** Show an Expert ('' keeps what is on the page, or the first one; 'new' is a blank one). Unsaved changes are
   *  kept when you only come back to the tab, and never dropped without your yes. */
  async function show(arg = '', { force = false } = {}) {
    if (state && !force && (arg === '' || arg === shownId())) { pin(shownId()); renderStrip(); renderTeam(); return; }
    if (dirty && !force) { askBeforeLeaving(arg); return; }
    if (arg) pin(arg);
    const mine = ++token; dirty = false; pictureChange = null; say(''); unsaved.hidden = true;
    try { await loadList(); } catch (error) { say(`Your experts are unavailable: ${error.message}`); experts = []; }
    if (mine !== token) return;
    loadPictures();
    const id = arg === 'new' ? '' : (arg || experts[0]?.template_id || '');
    current = null; let saved = null;
    if (id) {
      try { saved = await request('/api/experts/' + encodeURIComponent(id)); } catch (error) { say(error.status === 404 ? 'That expert is not available. Choose another one.' : error.message); }
      if (mine !== token) return;
      current = saved;
    }
    spec = JSON.parse(JSON.stringify(current?.expert || DEFAULT_EXPERT));
    spec.team.preferred = spec.team.preferred || '';
    try { library = ((await request('/api/personal-library')).items || []); } catch { library = []; }
    const offered = await loadOptions(); if (mine !== token) return;
    state = {
      name: current?.name || '', description: current?.description || '', instructions: spec.instructions || '',
      refs: (current?.personal_library_refs || []).map(ref => ({ item_id: ref.item_id, version_id: ref.version_id })),
      talk: { profile: spec.team.talk.profile, model: spec.team.talk.model || '' },
      coordinate: { profile: spec.team.coordinate.profile, model: spec.team.coordinate.model || '' },
      work: [...spec.team.work], preferred: spec.team.preferred, replies: spec.team.talk.effort || '',
      account: { talk: spec.team.talk.account_id || '', coordinate: spec.team.coordinate.account_id || '' },
      backup: { talk: spec.team.talk.backup?.profile || '', coordinate: spec.team.coordinate.backup?.profile || '' },
      ask: spec.ask_me_first || 'auto', place: spec.workspace?.kind === 'sandboxed' ? 'sandboxed' : 'host',
      offered,
    };
    const kept = readDraft(shownId());
    if (kept) {
      Object.assign(state, kept); dirty = true;
      const back = node('span', '', 'Your unsaved changes are back. ');
      const discard = node('button', 'quiet-button compact-button', 'Discard them'); discard.type = 'button';
      discard.onclick = () => { dropDraft(); dirty = false; void show(shownId(), { force: true }); };
      status.replaceChildren(back, discard);
    }
    pictureUrl = current ? pictures.get(current.lineage_id) || '' : '';
    if (current && !pictureUrl) request('/api/experts/' + encodeURIComponent(current.template_id) + '/picture').then(r => {
      if (mine === token && typeof r?.picture === 'string' && r.picture.startsWith('data:image/png;base64,') && !pictureChange) { pictureUrl = r.picture; renderPicture(); }
    }).catch(() => {});
    pin(shownId()); renderStrip(); render();
  }

  const changed = () => { dirty = true; keepDraft(); };
  let renderPicture = () => {}; let renderTeam = () => {};

  function render() {
    body.replaceChildren();
    // 1. What does this expert do? The picture first, then the name and the description.
    const one = section('does', 'What Does This Expert Do?');
    if (current) {
      const tools = node('div', 'experts-tools');
      tools.append(node('span', 'experts-version', `Version ${current.version || 1}`));
      const chat = node('button', 'secondary-button compact-button', 'Chat'); chat.type = 'button';
      chat.onclick = () => openExpertChat(current, { request }).catch(error => say(error.message));
      const channels = node('button', 'secondary-button compact-button', 'Channels'); channels.type = 'button';
      channels.onclick = () => openChannels(current, { request });
      tools.append(chat, channels); one.heading.append(tools);
    }
    const identity = node('div', 'experts-identity');
    const picture = node('div', 'experts-picture');
    const face = node('button', 'experts-picture-face'); face.type = 'button'; face.setAttribute('aria-label', 'Choose a picture');
    const input = node('input', 'expert-picture-input'); input.type = 'file'; input.accept = 'image/png,image/jpeg,image/webp'; input.hidden = true;
    const choose = node('button', 'secondary-button compact-button', 'Choose a picture'); choose.type = 'button';
    const remove = node('button', 'quiet-button compact-button', 'Remove'); remove.type = 'button';
    face.onclick = choose.onclick = () => input.click();
    renderPicture = () => {
      face.replaceChildren();
      if (pictureUrl) { const img = node('img'); img.src = pictureUrl; img.alt = ''; face.append(img); }
      else face.append(node('span', 'experts-picture-empty', 'Picture'));
      remove.hidden = !pictureUrl;
    };
    input.onchange = async () => {
      const file = input.files?.[0]; if (!file) return;
      try { const url = await squarePng(file); pictureChange = { png_base64: url.slice(url.indexOf(',') + 1) }; pictureUrl = url; changed(); renderPicture(); }
      catch { say('That picture could not be read. Choose a PNG, JPEG or WebP image.'); }
    };
    remove.onclick = () => { pictureChange = { remove: true }; input.value = ''; pictureUrl = ''; changed(); renderPicture(); };
    const pictureButtons = node('div', 'experts-picture-buttons'); pictureButtons.append(choose, remove, input);
    picture.append(face, pictureButtons);
    renderPicture();
    const words = node('div', 'experts-words');
    const name = node('input'); name.type = 'text'; name.required = true; name.maxLength = 160; name.value = state.name; name.placeholder = 'For example: Research partner';
    name.oninput = () => { state.name = name.value; changed(); };
    const description = node('textarea'); description.rows = 3; description.maxLength = 1000; description.value = state.description;
    description.placeholder = 'What it does for the people who message it.';
    description.oninput = () => { state.description = description.value; changed(); };
    // The section's title asks the question; its box answers it (as "What's the Job?" on Run project).
    const does = node('label', 'project-field'); does.append(node('span', 'sr-only', 'What does this expert do?'), description);
    words.append(does, field('Expert name', name));
    const pictureLine = help('Teams shows it as the app icon. Kept on this computer, not in its versions.'); pictureLine.classList.add('experts-picture-line');
    identity.append(picture, words, pictureLine); one.box.append(identity);

    // 2. Identity instructions: one box that grows with its words; it never scrolls on its own.
    const two = section('identity', 'Identity Instructions');
    const instructions = node('textarea', 'experts-instructions'); instructions.maxLength = 32768; instructions.rows = 6; instructions.value = state.instructions;
    instructions.placeholder = 'Who it is, what it does for people, and what it must never share.';
    instructions.setAttribute('aria-label', 'Identity instructions');
    const grow = () => { instructions.style.height = 'auto'; instructions.style.height = `${Math.max(instructions.scrollHeight + 2, 140)}px`; };
    instructions.oninput = () => { state.instructions = instructions.value; changed(); grow(); };
    two.box.append(instructions, help('It reads these at every message. You can change them at any time.'));

    // 3. Knowledge and Skills: Run project's own searchable chooser.
    const three = section('knowledge', 'Knowledge and Skills');
    const add = node('button', 'secondary-button'); add.type = 'button';
    add.innerHTML = '<svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5zM4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5"/></svg>';
    add.append(node('span', '', 'Add knowledge & abilities'));
    const chosen = node('ul', 'experts-library');
    const renderLibrary = () => {
      chosen.replaceChildren();
      for (const ref of state.refs) {
        const item = library.find(entry => entry.item_id === ref.item_id);
        const kind = item?.kind === 'skill' ? 'Skill' : item?.kind === 'note' ? 'Knowledge' : item ? 'Reference' : '';
        const row = node('li', 'personal-library-selected'); row.append(node('span', '', item ? `${item.name} · ${kind}` : 'Saved Library item'));
        const drop = node('button', 'quiet-button', 'Remove'); drop.type = 'button'; drop.setAttribute('aria-label', `Remove ${item?.name || 'this Library item'}`);
        drop.onclick = () => { state.refs = state.refs.filter(r => r.item_id !== ref.item_id); changed(); renderLibrary(); };
        row.append(drop); chosen.append(row);
      }
      if (!state.refs.length) chosen.append(node('li', 'experts-empty', 'None chosen yet.'));
    };
    add.onclick = async () => {
      const next = await openLibraryPicker({ title: `What knowledge and abilities does ${state.name || 'this expert'} need?`, refs: state.refs, ownerScope: getData()?.draft_owner_scope || '', request });
      if (!next) return;
      state.refs = next.refs.map(ref => ({ item_id: ref.item_id, version_id: ref.version_id }));
      for (const item of next.resolved?.items || []) if (!library.some(entry => entry.item_id === item.item_id)) library.push(item);
      changed(); renderLibrary();
    };
    renderLibrary();
    const selection = node('div', 'personal-library-selection'); selection.append(add);
    three.box.append(selection, chosen, help('From My Library. Its coordinator and Workers load them natively, on every AI that can run them.'));

    // 4. AI team: one table. A choice where a model meets a role is the whole setting.
    const four = section('team', 'AI Team');
    const table = node('div', 'experts-team');
    const below = node('div', 'experts-team-below');
    four.box.append(table, below);
    renderTeam = () => {
      // A choice rebuilds the table; the control that had focus keeps it.
      const focused = four.box.contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') : '';
      renderTable(table); renderBelow(below);
      if (focused) [...four.box.querySelectorAll('[aria-label]')].find(el => el.getAttribute('aria-label') === focused)?.focus();
    };
    renderTeam();

    // 5. Ask me first, as it is.
    const five = section('ask', 'Ask Me First');
    const askRow = node('div', 'expert-segment'); askRow.setAttribute('role', 'radiogroup'); askRow.setAttribute('aria-label', 'Ask me first');
    const askHint = node('small', 'field-help'); const group = nextId('expert-ask');
    for (const [value, [text]] of Object.entries(ASK)) {
      const radio = node('input'); radio.type = 'radio'; radio.name = group; radio.value = value; radio.checked = value === state.ask;
      radio.onchange = () => { state.ask = value; askHint.textContent = ASK[value][1]; changed(); };
      const l = node('label', 'expert-segment-option'); l.append(radio, node('span', '', text)); askRow.append(l);
    }
    askHint.textContent = ASK[state.ask][1]; five.box.append(askRow, askHint);

    // 6. Where it works, and what it may do there.
    const six = section('where', 'Where It Works');
    const level = node('p', 'experts-level'); const placeHint = help('');
    const explain = () => { level.replaceChildren(node('span', '', 'What it may do: '), node('strong', '', LEVELS[state.place])); placeHint.textContent = PLACE_HINTS[state.place]; };
    six.heading.append(toggle('Where it works', PLACES, state.place, value => {
      state.place = value; changed(); explain();
      if (value === 'sandboxed') {
        // Nothing of the Expert works on this computer itself: Claude talks and coordinates, with no backup.
        for (const role of ['talk', 'coordinate']) if (state[role].profile !== 'claude-code') state[role] = { profile: 'claude-code', model: '' };
        state.backup = { talk: '', coordinate: '' };
      }
      renderTeam();
    }));
    explain(); six.box.append(level, placeHint);

    // The end: a new version on Save.
    const footer = node('div', 'experts-footer');
    const note = help(current ? 'Saving makes a new version. Its next message follows it; running jobs finish with what they started with.' : 'Save makes this expert. Choose it on Run project, or give it a channel.');
    const error = node('p', 'experts-error'); error.setAttribute('role', 'alert'); error.hidden = true;
    const discard = node('button', 'secondary-button', 'Discard changes'); discard.type = 'button';
    discard.onclick = () => { dropDraft(); dirty = false; void show(shownId(), { force: true }); };
    const save = node('button', 'primary-button', 'Save'); save.type = 'submit';
    const actions = node('div', 'launch-actions'); actions.append(discard, save);
    footer.append(note, error, actions);
    body.append(one.box, two.box, three.box, four.box, five.box, six.box, footer);
    requestAnimationFrame(grow);

    form.onsubmit = async event => {
      event.preventDefault(); error.hidden = true;
      if (!state.name.trim()) { error.textContent = 'Give the expert a name.'; error.hidden = false; name.focus(); return; }
      let expert;
      try {
        expert = { instructions: state.instructions.trim(), ask_me_first: state.ask,
          workspace: { kind: state.place, access: LEVEL_VALUES[state.place] }, team: readTeam(state) };
      } catch (issue) { error.textContent = issue.message; error.hidden = false; return; }
      const payload = { name: state.name.trim(), description: state.description.trim(), expert, personal_library_refs: state.refs };
      save.disabled = discard.disabled = true; say('Saving…');
      try {
        const saved = current
          ? await request('/api/experts/' + encodeURIComponent(current.template_id), { method: 'PATCH', body: { ...payload, expected_catalog_revision: current.catalog_revision } })
          : await request('/api/experts', { method: 'POST', body: payload });
        if (pictureChange && saved?.template_id) {
          await request('/api/experts/' + encodeURIComponent(saved.template_id) + '/picture', { method: 'PUT', body: pictureChange });
          pictures.delete(saved.lineage_id);
        }
        dropDraft(); dirty = false; expertSaved(saved);
        await show(saved.template_id, { force: true });
        say(current?.version > 1 ? `Saved as version ${current.version}. Its next message follows it.` : 'Expert saved.');
      } catch (issue) {
        say(''); error.textContent = issue.message || 'Could not save. Your changes are still here.'; error.hidden = false;
      } finally { save.disabled = discard.disabled = false; }
    };
  }

  // The table: rows are each AI's models, grouped under the AI; columns are the three roles.
  function renderTable(table) {
    const rows = teamRows(state.offered, { team: { talk: state.talk, coordinate: state.coordinate, work: state.work } });
    const sandboxed = state.place === 'sandboxed';
    const grid = node('table', 'experts-team-table');
    const head = node('thead'); const headRow = node('tr');
    // Each role says once, in plain words, what it is.
    for (const [text, line, cls] of [['AI and model', '', 'experts-col-model'], ['Talks to people', 'Answers at once', ''],
      ['Coordinates', 'Plans and hands out the work', ''], ['Workers', 'Do the work, several at once', '']]) {
      const th = node('th', cls, text); th.scope = 'col'; if (line) th.append(node('small', 'experts-col-line', line)); headRow.append(th);
    }
    head.append(headRow); grid.append(head);
    const talkName = nextId('experts-talk'); const coordinateName = nextId('experts-coordinate');
    const several = state.work.length > 1;
    // The three choices where one model meets the three roles.
    const cells = (ai, line) => {
      const unavailable = line.status === 'unavailable';
      const measured = speeds[line.id || ai.usual];
      const talkCell = node('td');
      if (TALKERS.has(ai.profile)) {
        const on = state.talk.profile === ai.profile && state.talk.model === line.id;
        const radio = node('input'); radio.type = 'radio'; radio.name = talkName; radio.checked = on;
        radio.disabled = (unavailable && !on) || (sandboxed && ai.profile !== 'claude-code');
        radio.setAttribute('aria-label', `${ai.name} ${line.name} talks to people`);
        radio.onchange = () => { state.talk = { profile: ai.profile, model: line.id }; if (state.backup.talk === ai.profile) state.backup.talk = ''; changed(); renderTeam(); };
        talkCell.append(radio);
        if (measured) talkCell.append(node('small', 'experts-time', `about ${measured.median_s} s`));
      } else {
        const none = node('span', 'experts-none', '–'); none.title = `${ai.name} can coordinate and work; it does not talk to people.`;
        none.setAttribute('aria-label', none.title); talkCell.append(none);
      }
      const coordinateCell = node('td');
      const on = state.coordinate.profile === ai.profile && state.coordinate.model === line.id;
      const radio = node('input'); radio.type = 'radio'; radio.name = coordinateName; radio.checked = on;
      radio.disabled = (unavailable && !on) || (sandboxed && ai.profile !== 'claude-code');
      radio.setAttribute('aria-label', `${ai.name} ${line.name} coordinates`);
      radio.onchange = () => { state.coordinate = { profile: ai.profile, model: line.id }; if (state.backup.coordinate === ai.profile) state.backup.coordinate = ''; changed(); renderTeam(); };
      coordinateCell.append(radio);
      // Workers: any number; with several, one may be Preferred.
      const workCell = node('td', 'experts-work');
      const entry = line.id || ai.profile;
      const ticked = state.work.includes(entry);
      const box = node('input'); box.type = 'checkbox'; box.checked = ticked; box.disabled = unavailable && !ticked;
      box.setAttribute('aria-label', `${ai.name} ${line.name} may work`);
      box.onchange = () => {
        state.work = box.checked ? [...state.work, entry] : state.work.filter(value => value !== entry);
        if (!state.work.includes(state.preferred)) state.preferred = '';
        changed(); renderTeam();
      };
      workCell.append(box);
      if (ticked && several) {
        const star = node('button', 'experts-star'); star.type = 'button';
        const first = state.preferred === entry;
        star.setAttribute('aria-pressed', String(first));
        star.setAttribute('aria-label', `${ai.name} ${line.name} preferred`);
        star.title = first ? 'Preferred. Press again for no preference.' : 'Make this one preferred';
        star.innerHTML = `<svg viewBox="0 0 24 24" fill="${first ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/></svg>`;
        if (first) star.append(node('span', '', 'Preferred'));
        star.onclick = () => { state.preferred = first ? '' : entry; changed(); renderTeam(); };
        workCell.append(star);
      }
      return [talkCell, coordinateCell, workCell];
    };
    for (const ai of rows) {
      const group = node('tbody', 'experts-team-group');
      // Each AI is one line, carrying the three choices for its usual model ("Its usual model" names it when the
      // install says which); its named models open under it, and stay open while one of them is chosen.
      const usualName = ai.models.find(m => m.id === ai.usual)?.name;
      const inUse = ai.models.some(m => (state.talk.profile === ai.profile && state.talk.model === m.id)
        || (state.coordinate.profile === ai.profile && state.coordinate.model === m.id) || state.work.includes(m.id));
      const open = inUse || openAis.has(ai.profile);
      const title = node('tr', 'experts-team-ai'); const th = node('th', 'experts-col-model'); th.scope = 'row';
      const who = node('span', 'experts-ai-name'); who.append(aiMark(ai.profile), node('strong', '', ai.name));
      th.append(who, node('small', 'experts-ai-usual', usualName ? `Its usual model · ${usualName}` : 'Its usual model'));
      if (ai.models.length && !inUse) {
        const more = node('button', 'experts-more'); more.type = 'button'; more.setAttribute('aria-expanded', String(open));
        more.setAttribute('aria-label', `${ai.name}: ${open ? 'fewer' : 'more'} models`);
        more.textContent = open ? 'Fewer models' : `${ai.models.length} more models`;
        more.onclick = () => { if (openAis.has(ai.profile)) openAis.delete(ai.profile); else openAis.add(ai.profile); renderTeam(); };
        th.append(more);
      }
      title.append(th, ...cells(ai, { id: '', name: 'its usual model', status: 'available', reason: '' })); group.append(title);
      if (open) {
        for (const line of ai.models) {
          const tr = node('tr', 'experts-team-model'); const label = node('th', 'experts-col-model', line.name); label.scope = 'row';
          if (line.status === 'saved') label.append(node('small', 'field-help', ' · saved'));
          if (line.status === 'unavailable' && line.reason) tr.title = line.reason;
          tr.append(label, ...cells(ai, line)); group.append(tr);
        }
      }
      if (notSetUp(ai.profile, getData())) {
        const tr = node('tr', 'experts-team-missing'); const td = node('td'); td.colSpan = 4;
        const link = node('a', 'secondary-button compact-button', `Connect ${ai.name}`); link.href = '/#connections';
        td.append(node('span', '', `${ai.name} is not set up on this computer yet.`), link); tr.append(td); group.append(tr);
      }
      grid.append(group);
    }
    const scroller = node('div', 'experts-team-fit'); scroller.append(grid);
    table.replaceChildren(scroller);
    if (!state.offered) table.append(help('The list of models is unavailable right now. Your saved choices are kept; reload to try again.'));
    table.append(help('Times are to a finished reply, measured on this computer.'));
    gridKeys(grid);
  }

  /** The table works as one control: one stop for Tab, arrows move between choices, Space or Enter picks; a click
   *  anywhere in a cell picks that cell's choice. */
  function gridKeys(grid) {
    const rows = [...grid.querySelectorAll('tbody tr')].filter(tr => tr.querySelector('input, button'));
    const controls = tr => [...tr.querySelectorAll('input:not(:disabled), button:not(:disabled)')];
    const all = rows.flatMap(controls);
    let active = all.find(c => c.getAttribute('aria-label') === lastGridControl) || all.find(c => c.checked) || all[0];
    const rove = next => { for (const c of all) c.tabIndex = c === next ? 0 : -1; active = next; lastGridControl = next?.getAttribute('aria-label') || ''; };
    rove(active);
    grid.addEventListener('focusin', event => { if (all.includes(event.target)) rove(event.target); });
    grid.addEventListener('click', event => {
      const cell = event.target.closest('td'); if (!cell || event.target.closest('input, button, a')) return;
      cell.querySelector('input:not(:disabled)')?.click();
    });
    grid.addEventListener('keydown', event => {
      const control = event.target; const tr = control.closest('tr'); const r = rows.indexOf(tr); if (r < 0 || !all.includes(control)) return;
      const list = controls(tr); const i = list.indexOf(control);
      const column = [...tr.children].indexOf(control.closest('th, td'));
      let next = null;
      if (event.key === 'ArrowRight') next = list[i + 1];
      else if (event.key === 'ArrowLeft') next = list[i - 1];
      else if (event.key === 'Home') next = list[0];
      else if (event.key === 'End') next = list.at(-1);
      else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        const step = event.key === 'ArrowDown' ? 1 : -1;
        for (let k = r + step; k >= 0 && k < rows.length && !next; k += step) {
          const there = controls(rows[k]);
          next = there.find(c => [...rows[k].children].indexOf(c.closest('th, td')) === column) || null;
        }
      } else if (event.key === 'Enter' && control.matches('input')) { event.preventDefault(); control.click(); return; }
      else return;
      event.preventDefault(); if (next) next.focus();
    });
  }

  // Under the table: how quickly it replies, and the accounts each role signs in with.
  function renderBelow(below) {
    below.replaceChildren();
    const replies = node('div', 'experts-row'); replies.append(node('span', 'experts-row-label', 'Replies'),
      toggle('Replies', [['low', 'Quick'], ['', 'Usual']], state.replies, value => { state.replies = value; changed(); }));
    const accounts = node('details', 'experts-accounts'); accounts.append(node('summary', '', 'Accounts'));
    // Opened, its rows are built again from the newest account list (accounts can be connected meanwhile).
    accounts.open = accountsOpen;
    accounts.ontoggle = () => {
      if (accounts.open === accountsOpen) return;
      accountsOpen = accounts.open; renderBelow(below); below.querySelector('.experts-accounts > summary')?.focus();
    };
    const ready = getData()?.provider_accounts || [];
    const PROVIDERS = { 'claude-code': ['claude', 'anthropic'], 'codex-cli': ['codex', 'openai'], 'grok-build': ['grok', 'xai'] };
    for (const [role, title] of [['talk', 'Talks to people'], ['coordinate', 'Coordinates']]) {
      const profile = state[role].profile;
      const row = node('div', 'experts-account');
      const who = node('div', 'experts-account-who'); who.append(aiMark(profile), node('strong', '', `${title} · ${AI_NAME[profile] || profile}`));
      const mine = ready.filter(a => a.status === 'ready' && (PROVIDERS[profile] || []).includes(String(a.provider || '').toLowerCase()));
      const signs = [['', 'This computer’s sign-in'], ...mine.map(a => [a.account_id, a.label || a.provider])];
      // A saved account that is not ready (or not loaded yet) stays chosen, never dropped.
      if (!signs.some(([v]) => v === state.account[role])) signs.push([state.account[role], 'Its saved account (not ready now)']);
      const signIn = node('div', 'experts-row'); signIn.append(node('span', 'experts-row-label', 'Signs in with'),
        toggle(`${title}: account`, signs, state.account[role], value => { state.account[role] = value; if (value) state.backup[role] = ''; changed(); renderBelow(below); }));
      const backups = [['', 'Nobody'], ...AIS.filter(([p]) => p !== profile).map(([p, t]) => [p, t])];
      const backup = node('div', 'experts-row'); backup.append(node('span', 'experts-row-label', `If ${AI_NAME[profile] || profile} is at its limit`),
        toggle(`${title}: backup`, backups, state.backup[role], value => { state.backup[role] = value; changed(); }));
      const locked = Boolean(state.account[role]) || state.place === 'sandboxed';
      for (const input of backup.querySelectorAll('input')) input.disabled = locked && input.value !== '';
      row.append(who, signIn, backup);
      if (locked) row.append(help(state.place === 'sandboxed' ? 'In a sandboxed workspace nobody takes over.' : 'Another AI takes over only with this computer’s sign-in.'));
      accounts.append(row);
    }
    accounts.append(help('The one that takes over answers only work that has not started yet.'));
    below.append(replies, accounts);
  }

  return { show, refresh: () => { if (state) renderTeam(); } };
}
