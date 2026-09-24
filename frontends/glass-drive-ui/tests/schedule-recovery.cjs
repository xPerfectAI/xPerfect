const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../src/glass_drive_ui/static/control-plane.js'), 'utf8');
const schedule = {
  definition_id: 'definition-test', worker_id: 'worker-test', instruction: 'Test task',
  active: true, enabled: true, recurrence_type: 'once', timezone_name: 'UTC',
  starts_at: '2026-09-23T09:00:00Z', next_occurrence_at: '2026-09-23T09:00:00Z',
};
let tokenSerial = 0;

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.hidden = false;
    this.classList = { toggle() {} };
  }
  set textContent(value) { this.children = []; this._text = String(value); }
  get textContent() { return (this._text || '') + this.children.map((child) => child.textContent || '').join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  setAttribute() {}
}

function fixture({ storage = new Map(), owner = 'owner-test' } = {}) {
  const nodes = { 'schedule-list': new Element('div'), 'recurring-schedule-status': new Element('p') };
  const context = vm.createContext({
    console, Map, Set, Date, Intl, URLSearchParams, CustomEvent: class {},
    crypto: { randomUUID: () => `test-token-${++tokenSerial}` },
    sessionStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    window: { dispatchEvent() {} },
    document: {
      getElementById: (id) => nodes[id] || null,
      querySelector: () => null,
      createElement: (tag) => new Element(tag),
      createTextNode: (value) => { const element = new Element('#text'); element.textContent = value; return element; },
    },
  });
  vm.runInContext(source.replace(/^import[\s\S]*?from\s+['"][^'"]+['"];\n/gm, '').replace(/^export /gm, ''), context);
  context.schedule = { ...schedule };
  context.me = { tenant_id: 'tenant-test', user_id: owner };
  context.mockApi = {
    withAuth: (value) => value,
    responseMessage: async () => 'Temporary error',
    postJson: async () => ({}),
  };
  vm.runInContext('api = mockApi; controlPlane = { me }; restoreUncertainScheduleRunKeys(); recurringSchedules = { items: [schedule] };', context);
  const success = (payload) => ({ ok: true, json: async () => payload });
  context.fetch = async () => success({ items: [context.schedule] });
  return { context, nodes, storage, success, run: (expression) => vm.runInContext(expression, context) };
}

function findButton(root, label) {
  if (root.tagName === 'button' && root.textContent === label) return root;
  for (const child of root.children) {
    const found = findButton(child, label);
    if (found) return found;
  }
  return null;
}

async function main() {
  {
    const { context, nodes, run } = fixture();
    const requests = [];
    context.mockApi.postJson = async (_, payload) => {
      requests.push(payload.idempotency_key);
      if (requests.length === 1) throw Error('Response lost after acceptance');
      return {};
    };
    run('renderSchedules()');
    context.button = findButton(nodes['schedule-list'], 'Run now');
    context.status = new Element('p');
    await run('runScheduleNow(schedule, button, status)');
    assert.match(context.status.textContent, /could not confirm/);
    await run('loadSchedules()');
    context.button = findButton(nodes['schedule-list'], 'Confirm prior run');
    await run('runScheduleNow(schedule, button, status)');
    assert.equal(requests.length, 2);
    assert.equal(requests[0], requests[1], 'a fresh card must replay the uncertain token');
    await run('loadSchedules()');
    context.button = findButton(nodes['schedule-list'], 'Run now');
    await run('runScheduleNow(schedule, button, status)');
    assert.notEqual(requests[1], requests[2], 'a confirmed request settles the token');
  }
  {
    const storage = new Map();
    const first = fixture({ storage });
    const requests = [];
    first.context.mockApi.postJson = async (_, payload) => {
      requests.push(payload.idempotency_key);
      throw Error('Response lost after acceptance');
    };
    first.run('renderSchedules()');
    first.context.button = findButton(first.nodes['schedule-list'], 'Run now');
    first.context.status = new Element('p');
    await first.run('runScheduleNow(schedule, button, status)');
    assert(storage.size, 'unresolved identity must survive tab reload');

    const anotherOwner = fixture({ storage, owner: 'different-owner' });
    anotherOwner.run('renderSchedules()');
    assert(findButton(anotherOwner.nodes['schedule-list'], 'Run now'), 'another owner must not inherit the token');
    assert.equal(findButton(anotherOwner.nodes['schedule-list'], 'Confirm prior run'), null);

    const reloaded = fixture({ storage });
    reloaded.run('recurringSchedules = { items: [] }; restoreCapabilityReviewFromCatalog=()=>{}; reconcileCapabilityReview=async()=>{}; renderProviderOptionControls=()=>{}; renderProviderAccounts=()=>{}; renderConnections=()=>{}; renderConnectAi=()=>{}; renderLibrary=()=>{}; renderLibraryRequestWorkspaceOptions=()=>{}; renderCapabilityReviewBanner=()=>{};');
    reloaded.context.fetch = async (url) => url.startsWith('/api/recurring-schedules')
      ? { ok: false }
      : reloaded.success(url === '/api/control-plane' ? { me: reloaded.context.me } : { items: [] });
    await reloaded.run('refreshControlPlane()');
    assert(storage.size, 'an initial list 503 must not forget the unresolved token');
    reloaded.context.fetch = async (url) => reloaded.success(url === '/api/control-plane'
      ? { me: reloaded.context.me } : { items: [reloaded.context.schedule] });
    await reloaded.run('refreshControlPlane()');
    reloaded.run('renderSchedules()');
    assert(findButton(reloaded.nodes['schedule-list'], 'Confirm prior run'));
    assert.match(reloaded.nodes['schedule-list'].textContent, /previous Run now request is unconfirmed/);
    reloaded.context.mockApi.postJson = async (_, payload) => {
      requests.push(payload.idempotency_key);
      return { status: 'scheduled' };
    };
    reloaded.context.button = findButton(reloaded.nodes['schedule-list'], 'Confirm prior run');
    reloaded.context.status = new Element('p');
    await reloaded.run('runScheduleNow(schedule, button, status)');
    assert.equal(requests[0], requests[1], 'tab reload must replay the same server identity');
    assert.equal(storage.size, 0, 'confirmed response must settle the stored identity');
    reloaded.run('renderSchedules()');
    assert(findButton(reloaded.nodes['schedule-list'], 'Run now'), 'a later distinct run is available');
    reloaded.context.button = findButton(reloaded.nodes['schedule-list'], 'Run now');
    await reloaded.run('runScheduleNow(schedule, button, status)');
    assert.notEqual(requests[1], requests[2], 'later distinct run must have a fresh identity');
  }
  {
    const { context, nodes, run, success } = fixture();
    let reads = 0;
    context.fetch = async () => {
      reads += 1;
      return success({ items: [{ scheduled_for: schedule.starts_at, state: 'pending' }] });
    };
    context.mockApi.postJson = async () => { throw Error('Response lost after acceptance'); };
    run('scheduleOccurrences.set(schedule.definition_id, []); renderSchedules()');
    context.button = findButton(nodes['schedule-list'], 'Run now');
    context.status = new Element('p');
    await run('runScheduleNow(schedule, button, status)');
    context.history = new Element('div');
    context.history.hidden = true;
    context.historyButton = new Element('button');
    await run('toggleOccurrenceHistory(schedule.definition_id, history, historyButton)');
    assert.equal(reads, 1, 'uncertain request must invalidate cached empty history');
    assert.match(context.history.textContent, /pending/);
    assert.match(context.status.textContent, /could not confirm/);
    run('scheduleOccurrences.set(schedule.definition_id, [])');
    await run('runScheduleNow(schedule, button, status, history)');
    assert.equal(reads, 2, 'already open history must revalidate after an uncertain request');
    assert.match(context.history.textContent, /pending/);
  }
  {
    const { context, nodes, run, success } = fixture();
    let finishOldRead;
    context.fetch = async () => new Promise((resolve) => { finishOldRead = resolve; });
    context.history = new Element('div');
    const oldRead = run('loadOccurrenceHistory(schedule.definition_id, history)');
    context.mockApi.postJson = async () => { throw Error('Response lost after acceptance'); };
    run('renderSchedules()');
    context.button = findButton(nodes['schedule-list'], 'Run now');
    context.status = new Element('p');
    await run('runScheduleNow(schedule, button, status)');
    finishOldRead(success({ items: [] }));
    await oldRead;
    assert.equal(run('scheduleOccurrences.has(schedule.definition_id)'), false,
      'a pre-attempt history response cannot repopulate stale cache');
  }
  {
    const { context, nodes, run, success } = fixture();
    context.fetch = async (url) => url.startsWith('/api/recurring-schedules')
      ? { ok: false }
      : success(url === '/api/control-plane' ? { me: context.me } : { items: [] });
    run('restoreCapabilityReviewFromCatalog=()=>{}; reconcileCapabilityReview=async()=>{}; renderProviderOptionControls=()=>{}; renderProviderAccounts=()=>{}; renderConnections=()=>{}; renderConnectAi=()=>{}; renderLibrary=()=>{}; renderLibraryRequestWorkspaceOptions=()=>{}; renderCapabilityReviewBanner=()=>{};');
    await run('refreshControlPlane()');
    assert.match(nodes['schedule-list'].textContent, /Test task/);
    assert.match(nodes['schedule-list'].textContent, /Showing the last loaded list/);
    context.fetch = async (url) => url.startsWith('/api/recurring-schedules')
      ? Promise.reject(Error('Network interrupted'))
      : success(url === '/api/control-plane' ? { me: context.me } : { items: [] });
    await run('refreshControlPlane()');
    assert.match(nodes['schedule-list'].textContent, /Test task/);
    assert.match(nodes['schedule-list'].textContent, /Network interrupted/);
    context.fetch = async (url) => success(url === '/api/control-plane'
      ? { me: context.me } : { items: [context.schedule] });
    await run('refreshControlPlane()');
    assert.doesNotMatch(nodes['schedule-list'].textContent, /Could not refresh/);
  }
  console.log('5 schedule recovery causal cases passed');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
