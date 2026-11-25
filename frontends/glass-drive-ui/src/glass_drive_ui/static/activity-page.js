/* Activity: one line per message or run — which Expert or Worker, when, where, by whom, what they asked — with
   big filter buttons, a time range, status and a search, all applied by xPerfect. A line opens in place to its trail. */
import { libraryRequest } from './personal-library.js?v=20261024polish1';
import { aiMark, channelMark, loadBrandAssets } from './experts.js?v=20261125page1';

const node = (tag, cls = '', text = '') => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
const STATUS = { waiting_for_you: 'Waiting for you', working: 'Working', answered: 'Answered', done: 'Done', stopped: 'Stopped', failed: 'Failed' };
const TIMES = [['hour', 'Last hour'], ['today', 'Today'], ['week', '7 days'], ['month', '30 days'], ['all', 'All time']];
const AI_NAMES = { 'claude-code': 'Claude', 'codex-cli': 'Codex', 'grok-build': 'Grok' };
const CHANNELS = { teams: 'Teams', slack: 'Slack', chat: 'Chat', schedule: 'Schedule', xperfect: 'xPerfect' };
// The same words Activity has always used for its events; anything else reads as a sentence.
const EVENTS = {
  'worker.created': 'Workspace created', 'worker.ready': 'Workspace ready', 'worker.metadata_updated': 'Workspace details updated',
  'worker.metadata.updated': 'Workspace details updated', 'worker.duplicated': 'Workspace duplicated', 'run.queued': 'Work queued',
  'run.started': 'Work started', 'run.completed': 'Work completed', 'run.failed': 'Work needs attention',
};
const STACK = '<path d="m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5"/>';
const CLOCK = '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>';
const FIRST_BUTTONS = 12;
const PAGE = 30;
const LOOK_EVERY_MS = 20000;

export function eventLabel(type) {
  const value = String(type || 'workspace.activity');
  if (EVENTS[value]) return EVENTS[value];
  const words = value.replaceAll('.', ' ').replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function glyph(paths) {
  const mark = node('span', 'activity-glyph'); mark.setAttribute('aria-hidden', 'true');
  mark.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
  return mark;
}
function xperfectMark() { const img = node('img', 'activity-brand'); img.src = '/static/brand-symbol.svg'; img.alt = ''; img.width = 18; img.height = 18; return img; }

/** Day headings in the viewer's own time: Today, Yesterday, then the date. */
export function dayLabel(at, now = new Date()) {
  const when = new Date(at); if (Number.isNaN(when.getTime())) return '';
  const start = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((start(now) - start(when)) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return when.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', ...(when.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) });
}
const clock = (at, seconds = false) => { const d = new Date(at); return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) }); };

/** The start of a time button, as a UTC instant: the read takes a start and an end. */
export function rangeStart(choice, now = new Date()) {
  if (choice === 'hour') return new Date(now.getTime() - 3600000).toISOString();
  if (choice === 'today') return new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
  if (choice === 'week') return new Date(now.getTime() - 7 * 86400000).toISOString();
  if (choice === 'month') return new Date(now.getTime() - 30 * 86400000).toISOString();
  return '';
}

export function personWords(item) {
  if (item.person?.you) return 'You';
  return item.person?.name || 'Someone';
}

export function initActivityPage({ mount, request = libraryRequest, view = null, now = () => new Date() } = {}) {
  if (!mount) return null;
  const state = { who: new Set(), time: 'all', statuses: new Set(), q: '', items: [], cursor: '', subjects: null,
                  showAll: false, open: new Set(), loading: null, generation: 0, waiting: [] };
  const pictures = new Map();
  let brand = {};
  loadBrandAssets().then(value => { brand = value || {}; }).catch(() => {});

  // ---------------------------------------------------------------- the filters
  const filters = node('div', 'activity-filters'); filters.setAttribute('role', 'group'); filters.setAttribute('aria-label', 'Filters');
  const whoRow = node('div', 'activity-filter-row'); const whoChips = node('div', 'activity-chips');
  whoRow.append(node('span', 'activity-filter-label', 'Who'), whoChips);
  const timeRow = node('div', 'activity-filter-row'); const timeChips = node('div', 'activity-segment');
  timeChips.setAttribute('role', 'group'); timeChips.setAttribute('aria-label', 'When');
  timeRow.append(node('span', 'activity-filter-label', 'When'), timeChips);
  const statusRow = node('div', 'activity-filter-row'); const statusChips = node('div', 'activity-chips');
  statusRow.append(node('span', 'activity-filter-label', 'Status'), statusChips);
  const searchRow = node('div', 'activity-search');
  const search = node('input'); search.type = 'search'; search.placeholder = 'Search words, people or names';
  search.setAttribute('aria-label', 'Search words, people or names'); search.autocomplete = 'off';
  const clear = node('button', 'quiet-button activity-clear', 'Clear filters'); clear.type = 'button'; clear.hidden = true;
  searchRow.append(search, clear);
  filters.append(whoRow, timeRow, statusRow, searchRow);
  const fresh = node('button', 'activity-new'); fresh.type = 'button'; fresh.hidden = true;
  const list = node('div', 'activity-lines');
  const empty = node('div', 'activity-empty'); empty.hidden = true; empty.setAttribute('role', 'status');
  const more = node('button', 'secondary-button activity-more', 'Show more'); more.type = 'button'; more.hidden = true;
  mount.replaceChildren(filters, fresh, list, empty, more);

  const chip = (label, pressed, onClick, face = null) => {
    const button = node('button', 'activity-chip'); button.type = 'button';
    button.setAttribute('aria-pressed', String(Boolean(pressed)));
    if (face) button.append(face);
    button.append(node('span', 'activity-chip-name', label));
    button.addEventListener('click', onClick);
    return button;
  };
  function expertFace(subject) {
    const face = node('span', 'activity-face'); face.setAttribute('aria-hidden', 'true');
    face.append(glyph(STACK));
    const key = subject.id || subject.template_id;
    if (subject.template_id && key) {
      if (!pictures.has(key)) {
        pictures.set(key, request('/api/experts/' + encodeURIComponent(subject.template_id) + '/picture')
          .then(r => (typeof r?.picture === 'string' && r.picture.startsWith('data:image/png;base64,') ? r.picture : ''))
          .catch(() => ''));
      }
      pictures.get(key).then(url => { if (!url) return; const img = node('img'); img.src = url; img.alt = ''; face.replaceChildren(img); });
    }
    return face;
  }
  function workerFace(ai) { const face = node('span', 'activity-face activity-face-ai'); face.setAttribute('aria-hidden', 'true'); face.append(aiMark(ai)); return face; }
  function chatFace() { const face = node('span', 'activity-face activity-face-ai'); face.setAttribute('aria-hidden', 'true'); face.append(xperfectMark()); return face; }
  const faceFor = subject => (subject.type === 'expert' ? expertFace(subject) : subject.type === 'chat' ? chatFace() : workerFace(subject.ai));

  function drawFilters() {
    const subjects = state.subjects || { experts: [], workers: [], chat: false };
    const buttons = [
      ...subjects.experts.map(s => ['x:' + s.id, s.name || 'Expert', expertFace({ ...s, type: 'expert' })]),
      ...subjects.workers.map(s => ['w:' + s.id, s.name || 'Worker', workerFace(s.ai)]),
      ...(subjects.chat ? [['chat', 'Chat', chatFace()]] : []),
    ];
    const shown = state.showAll ? buttons : buttons.slice(0, FIRST_BUTTONS);
    whoChips.replaceChildren(
      chip('All', state.who.size === 0, () => { state.who.clear(); reload(); }),
      ...shown.map(([key, label, face]) => chip(label, state.who.has(key), () => { state.who.has(key) ? state.who.delete(key) : state.who.add(key); reload(); }, face)),
    );
    if (buttons.length > FIRST_BUTTONS && !state.showAll) {
      const all = node('button', 'quiet-button', `Show all ${buttons.length}`); all.type = 'button';
      all.addEventListener('click', () => { state.showAll = true; drawFilters(); });
      whoChips.append(all);
    }
    timeChips.replaceChildren(...TIMES.map(([key, label]) => chip(label, state.time === key, () => { state.time = key; reload(); })));
    statusChips.replaceChildren(...Object.entries(STATUS).map(([key, label]) => {
      const button = chip(label, state.statuses.has(key), () => { state.statuses.has(key) ? state.statuses.delete(key) : state.statuses.add(key); reload(); });
      button.dataset.status = key; return button;
    }));
    clear.hidden = !filtered();
  }
  const filtered = () => Boolean(state.who.size || state.time !== 'all' || state.statuses.size || state.q);
  let typing = 0;
  search.addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(() => { state.q = search.value.trim(); reload(); }, 300); });
  clear.addEventListener('click', () => { state.who.clear(); state.time = 'all'; state.statuses.clear(); state.q = ''; search.value = ''; reload(); });

  // ---------------------------------------------------------------- the read
  function body(cursor = '') {
    const who = [...state.who];
    return {
      experts: who.filter(k => k.startsWith('x:')).map(k => k.slice(2)),
      workers: who.filter(k => k.startsWith('w:')).map(k => k.slice(2)),
      chat: state.who.has('chat'), since: rangeStart(state.time, now()), statuses: [...state.statuses],
      q: state.q, cursor, limit: PAGE,
    };
  }
  async function read(cursor = '') { return request('/api/activity/lines', { method: 'POST', body: body(cursor) }); }

  async function reload() {
    const generation = ++state.generation;
    drawFilters();
    fresh.hidden = true; state.waiting = [];
    try {
      const page = await read();
      if (generation !== state.generation) return;
      state.items = page.items || []; state.cursor = page.next_cursor || '';
      if (page.subjects) state.subjects = page.subjects;
      drawFilters(); drawLines(); openFromAddress();
    } catch {
      if (generation !== state.generation) return;
      state.items = []; state.cursor = '';
      list.replaceChildren(); more.hidden = true;
      empty.hidden = false; empty.replaceChildren(node('p', '', 'Activity is temporarily unavailable—refresh to retry'));
    }
  }
  more.addEventListener('click', async () => {
    if (!state.cursor) return;
    more.disabled = true;
    try {
      const page = await read(state.cursor);
      state.items = [...state.items, ...(page.items || [])]; state.cursor = page.next_cursor || '';
      drawLines();
    } catch { more.textContent = 'Could not load more—try again'; } finally { more.disabled = false; }
  });

  // ---------------------------------------------------------------- lines
  function channelPart(item) {
    const kind = item.channel?.kind || '';
    if (!kind) return null;
    const part = node('span', 'activity-channel');
    part.append(kind === 'teams' || kind === 'slack' ? channelMark(kind, brand) : kind === 'schedule' ? glyph(CLOCK) : xperfectMark(),
      node('span', '', CHANNELS[kind] || kind));
    return part;
  }
  function lineFor(item) {
    const line = node('article', 'activity-line'); line.dataset.line = item.line_id;
    const main = node('button', 'activity-line-main'); main.type = 'button';
    const trailId = 'activity-trail-' + item.line_id;
    main.setAttribute('aria-expanded', 'false'); main.setAttribute('aria-controls', trailId);
    const text = node('span', 'activity-line-text');
    const who = node('span', 'activity-line-who');
    who.append(node('strong', '', item.subject?.name || (item.subject?.type === 'chat' ? 'Chat' : item.subject?.type === 'expert' ? 'Expert' : 'Worker')));
    const where = channelPart(item); if (where) who.append(where);
    if (item.kind !== 'workspace') who.append(node('span', 'activity-person', personWords(item)));
    const words = item.kind === 'workspace' ? eventLabel(item.event_type) : (item.preview ? `“${item.preview}”` : '(no text)');
    text.append(who, node('span', item.kind === 'workspace' || item.preview ? 'activity-line-words' : 'activity-line-words activity-none', words));
    const side = node('span', 'activity-line-side');
    if (item.status) { const status = node('span', 'activity-status', STATUS[item.status] || item.status); status.dataset.status = item.status; side.append(status); }
    const time = node('time', 'activity-time', clock(item.at)); time.dateTime = item.at; side.append(time);
    main.append(faceFor(item.subject || {}), text, side);
    const actions = node('div', 'activity-line-actions');
    if (item.review_id) {
      const decide = node('button', 'primary-button activity-decide', 'Decide'); decide.type = 'button';
      decide.addEventListener('click', () => goToRequest(item.review_id));
      actions.append(decide);
    }
    if (item.open?.conversation_id) { const a = node('a', 'quiet-button', 'Open chat'); a.href = '/conversation?id=' + encodeURIComponent(item.open.conversation_id); actions.append(a); }
    else if (item.open?.worker_id) { const a = node('a', 'quiet-button', 'Open workspace'); a.href = '/watch/' + encodeURIComponent(item.open.worker_id); actions.append(a); }
    const trail = node('ol', 'activity-trail'); trail.id = trailId; trail.hidden = true;
    main.addEventListener('click', () => toggle(item, line, main, trail));
    line.append(main, actions, trail);
    if (state.open.has(item.line_id)) toggle(item, line, main, trail, true);
    return line;
  }
  function drawLines() {
    const parts = []; let day = '';
    for (const item of state.items) {
      const label = dayLabel(item.at, now());
      if (label !== day) { day = label; parts.push(node('h3', 'activity-day', label)); }
      parts.push(lineFor(item));
    }
    list.replaceChildren(...parts);
    more.hidden = !state.cursor; more.textContent = 'Show more';
    empty.hidden = Boolean(state.items.length);
    if (!state.items.length) {
      if (filtered()) {
        const reset = node('button', 'secondary-button', 'Clear filters'); reset.type = 'button'; reset.addEventListener('click', () => clear.click());
        empty.replaceChildren(node('p', '', 'Nothing matches these filters.'), reset);
      } else empty.replaceChildren(node('p', '', 'No activity yet'));
    }
  }

  // ---------------------------------------------------------------- one line opened in place
  function stepWords(step, item) {
    const channel = CHANNELS[item.channel?.kind] || 'the channel';
    const ai = AI_NAMES[step.ai] || step.ai || '';
    switch (step.step) {
      case 'received': return item.kind === 'run' ? 'Work asked' : item.person?.you ? `You wrote in ${channel}` : `Message from ${personWords(item)} on ${channel}`;
      case 'event': return eventLabel(step.event_type) + (ai ? ` · ${ai}` : '');
      case 'scheduled': return 'Started by a schedule';
      case 'asked_you': return step.what === 'job' ? 'Asks you first: the job waits for you' : 'Asks you first: the reply waits for you';
      case 'decided': return { approved: 'You approved', rejected: 'You said no', withdrawn: 'The request was withdrawn' }[step.state] || 'Decided';
      case 'delivery': {
        const what = step.what === 'handoff' ? 'Message about the job' : 'Reply';
        return { sent: `${what} sent to ${channel}`, held: `${what} held until you decide`, pending: `${what} being sent`, sending: `${what} being sent`,
          not_confirmed: `${channel} did not confirm it arrived`, failed: `${what} not delivered`, dropped: `${what} not sent` }[step.state] || what;
      }
      case 'job': return { started: 'Job started', done: 'Job done', failed: 'Job failed', stopped: 'Job stopped', withdrawn: 'Job stopped' }[step.state] || 'Job';
      case 'worker': return `Worker: ${[ai, step.model].filter(Boolean).join(' · ')} · ${{ completed: 'done', failed: 'failed', cancelled: 'stopped', interrupted: 'stopped' }[step.state] || 'working'}`;
      default: return '';
    }
  }
  async function toggle(item, line, main, trail, force = false) {
    const opening = force || main.getAttribute('aria-expanded') !== 'true';
    main.setAttribute('aria-expanded', String(opening)); trail.hidden = !opening; line.classList.toggle('is-open', opening);
    if (!opening) { state.open.delete(item.line_id); setAddress(''); return; }
    state.open.add(item.line_id); if (!force) setAddress(item.line_id);
    trail.replaceChildren(node('li', 'activity-step activity-muted', 'Loading…'));
    try {
      const detail = await request('/api/activity/lines/' + encodeURIComponent(item.line_id));
      trail.replaceChildren(...(detail.trail || []).map(step => {
        const entry = node('li', 'activity-step');
        const at = node('time', 'activity-step-time', clock(step.at, true)); at.dateTime = step.at || '';
        entry.append(at, node('span', '', stepWords(step, item)));
        if (step.step === 'asked_you' && step.state === 'pending' && step.review_id) {
          const go = node('button', 'quiet-button', 'Go to it'); go.type = 'button'; go.addEventListener('click', () => goToRequest(step.review_id)); entry.append(go);
        }
        if (step.step === 'job' && step.conversation_id) {
          const a = node('a', 'quiet-button', 'Open the job'); a.href = '/conversation?id=' + encodeURIComponent(step.conversation_id); entry.append(a);
        }
        return entry;
      }));
      if (!trail.children.length) trail.replaceChildren(node('li', 'activity-step activity-muted', 'Nothing more was recorded.'));
    } catch { trail.replaceChildren(node('li', 'activity-step activity-muted', 'The details could not be loaded—try again.')); }
  }
  function goToRequest(reviewId) {
    const card = document.querySelector(`[data-review="${CSS.escape(reviewId)}"]`);
    if (!card) return;
    card.setAttribute('tabindex', '-1'); card.scrollIntoView({ block: 'center', behavior: 'smooth' }); card.focus({ preventScroll: true });
  }
  function setAddress(lineId) {
    try { history.replaceState(history.state, '', lineId ? '#activity/' + lineId : '#activity'); } catch { /* an address it cannot write changes nothing */ }
  }
  function openFromAddress() {
    const match = /^#activity\/([A-Za-z0-9_-]+)$/.exec(location.hash || '');
    if (!match || state.open.has(match[1])) return;
    const line = list.querySelector(`[data-line="${match[1]}"]`);
    if (line) { line.querySelector('.activity-line-main')?.click(); line.scrollIntoView({ block: 'center' }); }
  }

  // ---------------------------------------------------------------- newer lines, quietly
  const atTop = () => list.getBoundingClientRect().top >= -8;
  function addNewer(items) {
    state.items = [...items, ...state.items.filter(item => !items.some(n => n.line_id === item.line_id))];
    state.waiting = []; fresh.hidden = true; drawLines();
  }
  fresh.addEventListener('click', () => { addNewer(state.waiting); list.scrollIntoView({ block: 'start', behavior: 'smooth' }); });
  async function look() {
    if (document.visibilityState !== 'visible' || (view && view.hidden) || state.loading) return;
    const generation = state.generation;
    state.loading = read().catch(() => null);
    const page = await state.loading; state.loading = null;
    if (!page || generation !== state.generation) return;
    const known = new Map(state.items.map(item => [item.line_id, item]));
    // What he is reading never moves: a known line only changes its status in place.
    for (const item of page.items || []) {
      const old = known.get(item.line_id);
      if (old && old.status !== item.status) {
        old.status = item.status; if (item.review_id) old.review_id = item.review_id; else delete old.review_id;
        const line = list.querySelector(`[data-line="${item.line_id}"]`);
        const fresher = lineFor(old); if (line) line.replaceWith(fresher);
      }
    }
    const top = state.items[0];
    const newer = [];
    for (const item of page.items || []) { if (top && item.line_id === top.line_id) break; if (!known.has(item.line_id)) newer.push(item); }
    if (!newer.length) return;
    if (page.subjects) { state.subjects = page.subjects; drawFilters(); }
    if (atTop()) { addNewer(newer); return; }
    state.waiting = newer; fresh.hidden = false; fresh.textContent = `${newer.length} new`;
  }
  const timer = setInterval(look, LOOK_EVERY_MS);

  reload();
  return { reload, look, stop: () => clearInterval(timer), state };
}

let page = null;
let watching = null;
/** The page's regular refresh: the first time Activity is shown it draws its lines from xPerfect's own read; after
 *  that it only looks for newer ones, so nothing he is reading moves. The bootstrap's events are not drawn. */
export function renderActivity() {
  const view = document.getElementById('activity-view');
  const mount = document.getElementById('activity-list');
  if (!view || !mount) return;
  const show = () => { if (view.hidden) return; if (page) page.look(); else page = initActivityPage({ mount, view }); };
  if (!watching) {
    watching = new MutationObserver(show);
    watching.observe(view, { attributes: true, attributeFilter: ['hidden'] });
  }
  show();
}
