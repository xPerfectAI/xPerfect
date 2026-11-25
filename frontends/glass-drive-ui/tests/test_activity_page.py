"""Activity page: the two forwards, the module's own rules, and how a person's words are drawn."""
from pathlib import Path
import re
import subprocess

from fastapi.testclient import TestClient

from glass_drive_ui.server import create_app

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'
SOURCE = (STATIC / 'activity-page.js').read_text()


class Runtime:
    def __init__(self):
        self.calls = []

    def _request(self, method, path, json_body=None):
        self.calls.append((method, path, json_body))
        return {'items': [], 'next_cursor': ''}


def run_node(code):
    result = subprocess.run(['node', '--input-type=module'], input=code, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout


def exported(name):
    match = re.search(r'^export function ' + re.escape(name) + r'\(.*?^}', SOURCE, re.M | re.S)
    assert match, name
    return match.group(0).replace('export ', '')


def test_the_filters_travel_in_the_body_and_a_line_id_is_checked_before_the_runtime():
    runtime = Runtime()
    client = TestClient(create_app(runtime_client=runtime))
    body = {'experts': ['lineage-a'], 'statuses': ['failed'], 'q': 'synthetic phrase', 'limit': 30}
    assert client.post('/api/activity/lines', json=body).json() == {'items': [], 'next_cursor': ''}
    assert client.get('/api/activity/lines/dB9jb252LWE').status_code == 200
    assert client.get('/api/activity/lines/not%2Fa%2Fline').status_code == 404
    assert runtime.calls == [('POST', '/v1/activity/lines', body), ('GET', '/v1/activity/lines/dB9jb252LWE', None)]


def test_day_headings_time_ranges_people_and_event_words():
    events = re.search(r'^const EVENTS = \{.*?^\};', SOURCE, re.M | re.S).group(0)
    run_node("import assert from 'node:assert/strict';\n" + events + "\n"
             + "\n".join(exported(name) for name in ('eventLabel', 'dayLabel', 'rangeStart', 'personWords', 'groupRuns')) + "\n"
             "const now = new Date(2026, 10, 25, 15, 0, 0);\n"
             "assert.equal(dayLabel(new Date(2026, 10, 25, 9, 0).toISOString(), now), 'Today');\n"
             "assert.equal(dayLabel(new Date(2026, 10, 24, 23, 59).toISOString(), now), 'Yesterday');\n"
             "assert.match(dayLabel(new Date(2026, 10, 20, 12, 0).toISOString(), now), /20/);\n"
             "assert.match(dayLabel(new Date(2025, 10, 20, 12, 0).toISOString(), now), /2025/);\n"
             "assert.equal(rangeStart('all', now), '');\n"
             "assert.equal(rangeStart('today', now), new Date(2026, 10, 25).toISOString());\n"
             "assert.equal(Date.parse(rangeStart('hour', now)), now.getTime() - 3600000);\n"
             "assert.equal(Date.parse(rangeStart('week', now)), now.getTime() - 7 * 86400000);\n"
             "assert.equal(personWords({ person: { name: '', you: false } }), 'Someone');\n"
             "assert.equal(personWords({ person: { name: 'Synthetic Person', you: false } }), 'Synthetic Person');\n"
             "assert.equal(personWords({ person: { name: '', you: true } }), 'You');\n"
             "assert.equal(eventLabel('run.queued'), 'Work queued');\n"
             "assert.equal(eventLabel('worker.created'), 'Workspace created');\n"
             "assert.equal(eventLabel('provider.session_started'), 'Provider session started');\n"
             "const rows = groupRuns([{at:'1',step:'received'},{at:'2',step:'event',event_type:'run.queued',ai:'codex-cli',run:'a'},"
             "{at:'3',step:'job',state:'started'},{at:'4',step:'event',event_type:'provider.child_updated',ai:'codex-cli',run:'a'},"
             "{at:'5',step:'event',event_type:'provider.child_updated',ai:'codex-cli',run:'a'},{at:'6',step:'event',event_type:'run.queued',ai:'claude-code',run:'b'}]);\n"
             "assert.deepEqual(rows.map(r => r.step), ['received', 'run', 'job', 'run']);\n"
             "assert.deepEqual(rows[1].labels, [{label: 'Work queued', count: 1}, {label: 'Provider child updated', count: 2}]);\n")


def test_a_persons_words_are_only_ever_text_and_the_phrase_never_enters_an_address():
    # innerHTML only ever receives this file's own constant marks, never data.
    for assigned in re.findall(r'\.innerHTML = (.+?);\n', SOURCE):
        assert assigned.startswith('`<svg') and '${paths}' in assigned, assigned
    assert "`“${item.preview}”`" in SOURCE and "'(no text)'" in SOURCE
    assert "request('/api/activity/lines', { method: 'POST', body: body(cursor) })" in SOURCE
    assert 'state.q' not in SOURCE.split('function setAddress')[1].split('function openFromAddress')[0]
    # The address keeps only the opened line's id.
    assert "'#activity/' + lineId" in SOURCE


def test_open_chat_only_from_a_proven_conversation_and_times_in_the_viewers_own_zone():
    # The rules of the renderer this page replaces: a chat link only from the conversation the runtime proved,
    # otherwise the workspace; times drawn in the viewer's own zone.
    assert "if (item.open?.conversation_id) { const a = node('a', 'quiet-button', 'Open chat'); a.href = '/conversation?id=' + encodeURIComponent(item.open.conversation_id);" in SOURCE
    assert "else if (item.open?.worker_id) { const a = node('a', 'quiet-button', 'Open workspace'); a.href = '/watch/' + encodeURIComponent(item.open.worker_id);" in SOURCE
    assert 'toLocaleTimeString(undefined' in SOURCE and 'toLocaleDateString(undefined' in SOURCE
    assert "'Activity is temporarily unavailable—refresh to retry'" in SOURCE and "'No activity yet'" in SOURCE
    # The page's regular refresh never redraws old rows over the page: it only mounts it or looks for newer lines.
    app = (STATIC / 'app.js').read_text()
    assert "import { renderActivity } from './activity-page.js?v=" in app and 'renderActivity();' in app
    assert 'export function renderActivity' not in (STATIC / 'control-plane.js').read_text()


def test_needs_you_shows_its_newest_three_and_opens_the_rest_in_place():
    experts = (STATIC / 'experts.js').read_text()
    assert 'const FIRST_CARDS = 3; let showAll = false;' in experts
    assert 'for (const item of showAll ? items : items.slice(0, FIRST_CARDS)) {' in experts
    assert "if (!showAll && items.length > FIRST_CARDS) {" in experts and '`Show all ${items.length}`' in experts
    # The count on the tab still counts every waiting request.
    assert 'badge.textContent = String(items.length)' in experts
    # A line's Decide opens the rest first when its request is not among the three.
    assert "if (!find()) document.querySelector('.needs-you-more')?.click();" in SOURCE


# A small stand-in for the page (elements, a click, a query by class or data-line) and a runtime whose cursor is a
# position, as the real one's is: a new line on top shifts nothing. The page module runs as it is, imports aside.
PAGE_HARNESS = r"""
import assert from 'node:assert/strict';
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.dataset = {}; this.attrs = {};
    this.listeners = {}; this.hidden = false; this._text = ''; this.className = ''; this.style = {}; this.disabled = false; }
  get classList() {
    const el = this; const names = () => el.className.split(/\s+/).filter(Boolean);
    return { add: (...c) => { el.className = [...new Set([...names(), ...c])].join(' '); },
      remove: (...c) => { el.className = names().filter(x => !c.includes(x)).join(' '); },
      toggle: (c, on) => { const want = on === undefined ? !names().includes(c) : on; el.className = [...names().filter(x => x !== c), ...(want ? [c] : [])].join(' '); return want; },
      contains: c => names().includes(c) };
  }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(v) { this._html = v; }
  append(...kids) { for (const k of kids) if (k && typeof k === 'object') { k.parent = this; this.children.push(k); } }
  replaceChildren(...kids) { this._text = ''; this.children = []; this.append(...kids); }
  replaceWith(other) { const p = this.parent; if (!p) return; other.parent = p; p.children[p.children.indexOf(this)] = other; this.parent = null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { for (const fn of this.listeners.click || []) fn({ target: this }); }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
  matches(sel) { const m = /^\[data-([a-z]+)="(.*)"\]$/.exec(sel); if (m) return this.dataset[m[1]] === m[2];
    return sel.startsWith('.') ? this.classList.contains(sel.slice(1)) : this.tagName === sel.toUpperCase(); }
  querySelector(sel) { for (const n of this.walk()) if (n.matches(sel)) return n; return null; }
  querySelectorAll(sel) { return [...this.walk()].filter(n => n.matches(sel)); }
  getBoundingClientRect() { return { top: 0 }; }
  scrollIntoView() {}
  focus() {}
}
globalThis.document = { createElement: t => new El(t), activeElement: null, visibilityState: 'visible', querySelector: () => null };
globalThis.window = { addEventListener() {} };
globalThis.location = { hash: '' };
globalThis.history = { state: null, replaceState(_s, _t, url) { location.hash = url; } };
globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
const libraryRequest = () => { throw new Error('the test gives its own request'); };
const aiMark = () => new El('span'); const channelMark = () => new El('span'); const loadBrandAssets = () => Promise.resolve({});
""" + re.sub(r'^import .*$', '', SOURCE, flags=re.M) + r"""
const NOW = new Date(2026, 10, 25, 12, 0, 0);
let tick = 0;
const make = (prefix, count, status = () => 'answered') => Array.from({ length: count }, (_, i) => ({
  line_id: prefix + (i + 1), kind: 'message', at: new Date(NOW.getTime() - (tick++) * 1000).toISOString(), status: status(i),
  subject: { type: 'expert', name: 'Synthetic Expert' }, channel: { kind: 'teams' }, person: { name: 'Synthetic Person', you: false },
  preview: 'Synthetic words', open: {} }));
const runtime = { lines: [], hold: null, reads: 0, trails: {} };
async function request(path, options = {}) {
  if (path === '/api/activity/lines') {
    const { cursor, limit, statuses } = options.body; runtime.reads += 1;
    if (cursor && runtime.hold) await runtime.hold;
    const matching = runtime.lines.filter(line => !statuses.length || statuses.includes(line.status));
    const start = cursor ? matching.findIndex(line => line.line_id === cursor) + 1 : 0;
    const items = matching.slice(start, start + limit).map(line => ({ ...line }));
    return { items, next_cursor: start + limit < matching.length ? items.at(-1).line_id : '',
             ...(cursor ? {} : { subjects: { experts: [], workers: [], chat: false } }) };
  }
  const line = runtime.lines.find(item => '/api/activity/lines/' + item.line_id === path);
  if (!line) throw Object.assign(new Error('Activity line not found'), { status: 404 });
  return { line: { ...line }, trail: runtime.trails[line.line_id] || [] };
}
const settle = async () => { for (let i = 0; i < 30; i += 1) await new Promise(resolve => setTimeout(resolve, 0)); };
const open = mount => {
  const shown = initActivityPage({ mount, request, now: () => NOW });
  return { shown, more: mount.children.find(c => c.classList.contains('activity-more')),
           addressed: mount.children.find(c => c.classList.contains('activity-addressed')) };
};
async function showAll(shown, more) { while (shown.state.cursor) { more.click(); await settle(); } }
const ids = items => items.map(item => item.line_id);
"""


def test_the_quiet_look_reads_down_to_the_shown_lines_so_none_is_out_of_reach():
    run_node(PAGE_HARNESS + """
// 35 lines arrive at once, more than one page: all of them go on top, and Show more goes on where it was.
runtime.lines = make('L', 40);
let { shown, more } = open(new El('div')); await settle();
assert.equal(shown.state.items.length, 30);
runtime.lines = [...make('N', 35), ...runtime.lines];
await shown.look(); await settle();
assert.deepEqual(ids(shown.state.items.slice(0, 35)), ids(runtime.lines.slice(0, 35)));
await showAll(shown, more);
assert.deepEqual(ids(shown.state.items), ids(runtime.lines));
// Far more than ten pages arrive: the newest start the list again with their own Show more; nothing is lost.
runtime.lines = [...make('M', 400), ...runtime.lines];
await shown.look(); await settle();
assert.equal(shown.state.items.length, 300);
await showAll(shown, more);
assert.deepEqual(ids(shown.state.items), ids(runtime.lines));
// Nothing shown yet, then 35 arrive: the list starts with its own Show more.
runtime.lines = [];
({ shown, more } = open(new El('div'))); await settle();
runtime.lines = make('F', 35);
await shown.look(); await settle();
await showAll(shown, more);
assert.deepEqual(ids(shown.state.items), ids(runtime.lines));
""")


def test_a_show_more_answer_that_comes_after_the_filters_changed_is_dropped():
    run_node(PAGE_HARNESS + """
runtime.lines = make('L', 40, i => (i % 13 === 0 ? 'failed' : 'answered'));
const { shown, more } = open(new El('div')); await settle();
let release; runtime.hold = new Promise(resolve => { release = resolve; });
more.click();                                   // Show more for every line: its answer is slow
shown.state.statuses.add('failed'); await shown.reload();
assert.deepEqual(ids(shown.state.items), ['L1', 'L14', 'L27', 'L40']);
release(); runtime.hold = null; await settle();
assert.deepEqual(ids(shown.state.items), ['L1', 'L14', 'L27', 'L40']);
assert.equal(shown.state.cursor, '');
""")


def test_a_lines_own_address_opens_it_also_when_it_is_older_than_the_loaded_page():
    run_node(PAGE_HARNESS + """
runtime.lines = make('L', 40);
location.hash = '#activity/L35';
const mount = new El('div');
const { shown, more, addressed } = open(mount); await settle();
// Read by its own address and opened above the list; the list itself is untouched.
assert.equal(shown.state.addressed.line_id, 'L35');
assert.equal(addressed.hidden, false);
assert.equal(addressed.querySelector('.activity-day').textContent, 'From your link · Today');  // apart from the list's own days
assert.equal(addressed.querySelector('[data-line="L35"]').querySelector('.activity-line-main').getAttribute('aria-expanded'), 'true');
assert.equal(shown.state.items.length, 30); assert.equal(ids(shown.state.items).includes('L35'), false);
// Show more reaches it: it is drawn once, in its place, still open.
await showAll(shown, more);
assert.equal(shown.state.addressed, null); assert.equal(addressed.hidden, true);
assert.equal(mount.querySelectorAll('[data-line="L35"]').length, 1);
assert.equal(mount.querySelector('[data-line="L35"]').querySelector('.activity-line-main').getAttribute('aria-expanded'), 'true');
// An address of no line (gone, or another owner's) opens nothing and breaks nothing.
location.hash = '#activity/L99';
const other = open(new El('div')); await settle();
assert.equal(other.shown.state.addressed, null); assert.equal(other.shown.state.items.length, 30);
""")


def test_why_a_reply_was_not_sent_reads_on_in_plain_words_after_the_colon():
    run_node(PAGE_HARNESS + """
runtime.lines = make('L', 2);
runtime.trails.L1 = [
  { at: runtime.lines[0].at, step: 'delivery', what: 'reply', state: 'dropped', reason: 'The message that the job started went out instead' },
  { at: runtime.lines[0].at, step: 'delivery', what: 'decision', state: 'dropped', reason: 'This conversation was stopped.' }];
location.hash = '';
const mount = new El('div');
open(mount); await settle();
mount.querySelector('[data-line="L1"]').querySelector('.activity-line-main').click(); await settle();
const words = mount.querySelector('[data-line="L1"]').querySelector('.activity-trail').children.map(step => step.textContent);
assert.ok(words.some(text => text.endsWith('Reply not sent: the message that the job started went out instead')), words);
assert.ok(words.some(text => text.endsWith('Answer after your decision not sent: this conversation was stopped')), words);
""")


def test_a_worker_waiting_for_its_own_input_leads_to_its_workspace():
    # Checked, not a fault: a Worker run that waits for native input has no request to decide, and its line carries
    # Open workspace, where that input is given.
    run_node(PAGE_HARNESS + """
runtime.lines = [{ ...make('R', 1)[0], kind: 'run', status: 'waiting_for_you', subject: { type: 'worker', name: 'Synthetic Worker', ai: 'codex-cli' },
                   channel: { kind: 'xperfect' }, person: { name: '', you: true }, open: { worker_id: 'worker-synthetic' } }];
location.hash = '';
const mount = new El('div');
open(mount); await settle();
const line = mount.querySelector('[data-line="R1"]');
assert.equal(line.querySelector('.activity-status').textContent, 'Waiting for you');
const actions = line.querySelector('.activity-line-actions').children;
assert.deepEqual(actions.map(a => [a.tagName, a.textContent, a.href]), [['A', 'Open workspace', '/watch/worker-synthetic']]);
""")


def test_the_chat_pages_link_names_the_run_project_tab_as_the_tab_does():
    page = (STATIC / 'conversation.html').read_text()
    assert 'href="/" hidden>Run Project</a>' in page and '>Run project</a>' not in page
