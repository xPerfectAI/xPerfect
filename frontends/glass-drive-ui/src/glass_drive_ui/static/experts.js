/* Experts people can talk to: one form for New and Edit, Channels with Connect, and Needs you. */
import { libraryRequest, libraryModal } from './personal-library.js?v=20261024polish1';

const node = (tag, cls = '', text = '') => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
const AIS = [['claude-code', 'Claude'], ['codex-cli', 'Codex'], ['grok-build', 'Grok']];
const PROVIDER = { 'claude-code': ['claude', 'anthropic'], 'codex-cli': ['codex', 'openai'], 'grok-build': ['grok', 'xai'] };
const ASK = {
  always: ['Always ask', 'You approve every reply to another person and every job before it happens.'],
  auto: ['Auto', 'It asks you when a request is unusual or outside its role. Otherwise it carries on.'],
  off: ['Off', 'Nobody is asked. Its instructions still apply.'],
};
export const DEFAULT_EXPERT = {
  instructions: '', ask_me_first: 'auto', workspace: { kind: 'host', access: 'full' },
  team: { talk: { profile: 'claude-code', account_id: '', backup: null, effort: 'low', model: 'claude-code:haiku' },
          coordinate: { profile: 'codex-cli', account_id: '', backup: null, effort: '' },
          work: ['claude-code', 'codex-cli'] },
};

// Which model talks: '' is the deployment's usual model; every other option saves that exact model.
export const MODELS = { 'claude-code': [['', 'Its usual model', 'claude-code:opus'], ['claude-code:opus', 'Opus', 'claude-code:opus'],
                                        ['claude-code:haiku', 'Haiku', 'claude-code:haiku']],
                        'codex-cli': [['', 'Its usual model', 'codex-cli:native-default'],
                                      ['codex-cli:gpt-6.2-sol', 'GPT-6.2 Sol', 'codex-cli:gpt-6.2-sol']] };

/** The talking model choices; a model saved exactly stays selected as saved, also one the list does not name. */
export function modelOptions(profile, keep = '') {
  const options = MODELS[profile] || [['', 'Its usual model', '']];
  const unnamed = keep && keep.startsWith(profile + ':') && !options.some(([value]) => value === keep);
  return unnamed ? [...options, [keep, keep.slice(profile.length + 1), keep]] : options;
}

function labelled(text, control, hint = '') {
  const wrapper = node('label', 'personal-library-field expert-field'); wrapper.append(node('span', '', text), control);
  if (hint) wrapper.append(node('small', 'expert-hint', hint));
  return wrapper;
}
function select(options, value) {
  const s = node('select'); for (const [v, t] of options) { const o = node('option', '', t); o.value = v; s.append(o); } s.value = value; return s;
}
function accountOptions(accounts, profile) {
  const providers = PROVIDER[profile] || [];
  return [['', 'This computer’s sign-in'], ...accounts.filter(a => a.status === 'ready' && providers.includes(String(a.provider || '').toLowerCase()))
    .map(a => [a.account_id, a.label || a.provider])];
}

/** The Expert's own settings, added to the existing name and description fields. */
function expertFields(body, spec, accounts, speeds) {
  const s = JSON.parse(JSON.stringify(spec || DEFAULT_EXPERT));
  const instructions = node('textarea'); instructions.rows = 6; instructions.maxLength = 32768; instructions.value = s.instructions || '';
  instructions.placeholder = 'Who it is, what it does for people, and what it must never share.';
  body.append(labelled('Instructions', instructions, 'It reads these at every message. You can change them at any time.'));

  const team = node('fieldset', 'expert-team'); team.append(node('legend', '', 'AI team'));
  const talk = select(AIS.slice(0, 2), s.team.talk.profile);
  // Which model talks; each option shows its median time to a finished reply on this computer.
  const talkModel = node('select'); let measured = {};
  const fillModels = (keep) => {
    const options = modelOptions(talk.value, keep);
    talkModel.replaceChildren(...options.map(([value, text, id]) => {
      const o = node('option', '', measured[id] ? `${text} · about ${measured[id].median_s} s here` : text); o.value = value; return o; }));
    talkModel.value = options.some(([value]) => value === keep) ? keep : '';
  };
  fillModels(s.team.talk.model || '');
  speeds?.then(items => { measured = Object.fromEntries((items || []).map(item => [item.model, item])); fillModels(talkModel.value); });
  const replies = select([['low', 'Quick'], ['', 'Usual']], s.team.talk.effort || '');
  const coordinate = select(AIS, s.team.coordinate.profile);
  const workers = node('div', 'expert-workers');
  const work = new Map();
  for (const [value, text] of AIS) {
    const box = node('input'); box.type = 'checkbox'; box.value = value; box.checked = s.team.work.includes(value);
    const l = node('label', 'expert-check'); l.append(box, node('span', '', text)); workers.append(l); work.set(value, box);
  }
  team.append(labelled('Talks to people', talk, 'Answers at once and hands work to the coordinator.'),
              labelled('Model', talkModel, 'Time to a finished reply, measured on this computer.'),
              labelled('Replies', replies),
              labelled('Coordinates the work', coordinate),
              labelled('May work', workers, 'Workers run in parallel on the job.'));
  const accountsBox = node('details', 'expert-accounts'); accountsBox.append(node('summary', '', 'Accounts'));
  const roleAccounts = {};
  const fill = (box, options, value) => {
    box.replaceChildren(...options.map(([v, t]) => { const o = node('option', '', t); o.value = v; return o; }));
    box.value = options.some(([v]) => v === value) ? value : '';
  };
  // A backup is another AI on this computer's sign-in; it takes only work that has not started.
  const backupOptions = profile => [['', 'No backup'], ...AIS.filter(([p]) => p !== profile).map(([p, t]) => [`${p}|`, t])];
  for (const [role, title, picker] of [['talk', 'Talks', talk], ['coordinate', 'Coordinates', coordinate]]) {
    const main = select(accountOptions(accounts, s.team[role].profile), s.team[role].account_id || '');
    const backup = select(backupOptions(s.team[role].profile),
      s.team[role].backup ? `${s.team[role].backup.profile}|${s.team[role].backup.account_id || ''}` : '');
    roleAccounts[role] = { main, backup };
    accountsBox.append(labelled(`${title}: main`, main), labelled(`${title}: backup`, backup, 'Used only for work that has not started.'));
    const sync = () => { backup.disabled = Boolean(main.value); if (main.value) backup.value = ''; };
    main.onchange = sync; sync();
    picker.onchange = () => { fill(main, accountOptions(accounts, picker.value), main.value); fill(backup, backupOptions(picker.value), backup.value); sync(); };
  }
  accountsBox.append(node('p', 'expert-hint', 'A backup works on this computer’s sign-in for now; a second account of the same AI comes later. One sign-in runs one Worker of its AI at a time, so tick several AI types for parallel work. API keys stay optional.'));
  team.append(accountsBox); body.append(team);

  const ask = node('fieldset', 'expert-ask'); ask.append(node('legend', '', 'Ask me first'));
  const askHint = node('small', 'expert-hint');
  const askRow = node('div', 'expert-segment'); let askValue = s.ask_me_first;
  const group = 'expert-ask-' + Math.random().toString(36).slice(2);
  for (const [value, [text]] of Object.entries(ASK)) {
    const radio = node('input'); radio.type = 'radio'; radio.name = group; radio.value = value; radio.checked = value === askValue;
    radio.onchange = () => { askValue = value; askHint.textContent = ASK[value][1]; };
    const l = node('label', 'expert-segment-option'); l.append(radio, node('span', '', text)); askRow.append(l);
  }
  askHint.textContent = ASK[askValue][1]; ask.append(askRow, askHint); body.append(ask);

  const place = select([['host', 'This computer'], ['sandboxed', 'Sandboxed workspace']], s.workspace.kind);
  // Only what its Workers really run at: full access on this computer, the workspace in a sandbox.
  const levels = { host: [['full', 'Full access']], sandboxed: [['workspace', 'Its workspace only']] };
  const access = select(levels[place.value] || levels.host, '');
  const placeHint = node('small', 'expert-hint');
  const explain = () => {
    const options = levels[place.value] || levels.host;
    access.replaceChildren(...options.map(([v, t]) => { const o = node('option', '', t); o.value = v; return o; }));
    access.value = options[0][0]; access.disabled = options.length === 1;
    placeHint.textContent = place.value === 'host'
      ? 'It can read and change what you can on this computer. Your instructions and Ask me first are the guard, not a wall. Narrower permissions on this computer come later.'
      : 'It works in its own sandboxed workspace and its Workers reach only that workspace. Claude talks and coordinates there with its own tools off, so all the work happens inside the sandbox. This needs containers on the computer or server running xPerfect.';
    if (place.value === 'sandboxed') {
      // Nothing of the Expert works on this computer itself: Claude talks and coordinates, with no backup.
      for (const [role, picker] of [['talk', talk], ['coordinate', coordinate]]) {
        if (picker.value !== 'claude-code') { picker.value = 'claude-code'; picker.onchange?.(); }
        if (roleAccounts[role]) roleAccounts[role].backup.value = '';
      }
    }
  };
  place.onchange = explain; explain();
  const where = node('div', 'expert-where'); where.append(labelled('Where it works', place), labelled('What it may do', access), placeHint); body.append(where);
  const pickedAccount = talk.onchange; talk.onchange = () => { pickedAccount?.(); fillModels(talkModel.value); };

  return () => {
    const allowed = [...work].filter(([, box]) => box.checked).map(([value]) => value);
    if (!allowed.length) throw new Error('Choose at least one AI that may work.');
    const backupOf = role => { const v = roleAccounts[role].backup.value; if (!v) return null; const [profile, account_id] = v.split('|'); return { profile, account_id }; };
    return { instructions: instructions.value.trim(), ask_me_first: askValue,
      workspace: { kind: place.value, access: access.value },
      team: { talk: { profile: talk.value, account_id: roleAccounts.talk.main.value, backup: backupOf('talk'), effort: replies.value, model: talkModel.value },
              coordinate: { profile: coordinate.value, account_id: roleAccounts.coordinate.main.value, backup: backupOf('coordinate'), effort: '' },
              work: allowed } };
  };
}

/** New expert and Edit expert share one dialog. Save makes a new version. */
export async function openExpertForm({ item = null, request = libraryRequest, signal, onSaved, accounts = [] } = {}) {
  let current = null;
  if (item) { try { current = await request('/api/experts/' + encodeURIComponent(item.template_id), { signal }); } catch { current = null; } }
  let name, description, read;
  const dialog = libraryModal(item ? 'Edit expert' : 'New expert', { action: 'Save', signal, onSave: async ({ signal: saveSignal }) => {
    const expert = read();
    const body = { name: name.value.trim(), description: description.value.trim(), expert, personal_library_refs: dialog.libraryRefs ? dialog.libraryRefs() : [] };
    const saved = item
      ? await request('/api/experts/' + encodeURIComponent(item.template_id), { method: 'PATCH', signal: saveSignal, body: { ...body, expected_catalog_revision: (current || item).catalog_revision } })
      : await request('/api/experts', { method: 'POST', signal: saveSignal, body });
    onSaved?.(saved);
  } });
  name = node('input'); name.required = true; name.maxLength = 160; name.value = item?.name || '';
  description = node('textarea'); description.rows = 2; description.maxLength = 1000; description.value = current?.description ?? item?.description ?? '';
  dialog.body.append(labelled('Expert name', name), labelled('What does this expert do?', description));
  const speeds = request('/api/experts-talk-speeds', { signal }).then(r => r.items || []).catch(() => []);
  read = expertFields(dialog.body, current?.expert, accounts, speeds);
  // Knowledge and Skills come from My Library; an exact saved version stays with the Expert.
  const chosen = new Map(((current || item)?.personal_library_refs || []).map(ref => [ref.item_id, ref]));
  const library = node('fieldset', 'expert-team'); library.append(node('legend', '', 'Knowledge and Skills'));
  const libraryList = node('div', 'expert-library'); library.append(libraryList, node('small', 'expert-hint', 'From My Library. Its coordinator and Workers load them natively, on every AI that can run them.'));
  dialog.body.insertBefore(library, dialog.body.querySelector('.expert-team'));
  try {
    const items = ((await request('/api/personal-library', { signal })).items || []).filter(entry => entry.state !== 'archived');
    if (!items.length) libraryList.append(node('p', 'expert-hint', 'Your Library is empty. Add knowledge or a skill folder in Library.'));
    for (const entry of items) {
      const box = node('input'); box.type = 'checkbox'; box.checked = chosen.has(entry.item_id);
      box.onchange = () => { if (box.checked) chosen.set(entry.item_id, { item_id: entry.item_id, version_id: entry.version_id }); else chosen.delete(entry.item_id); };
      const kind = entry.kind === 'skill' ? 'Skill' : entry.kind === 'note' ? 'Knowledge' : 'Reference';
      const label = node('label', 'expert-check'); label.append(box, node('span', '', `${entry.name} · ${kind}`)); libraryList.append(label);
    }
  } catch { libraryList.append(node('p', 'expert-hint', 'My Library is unavailable right now. Your other choices are kept.')); }
  const readExpert = read; read = () => readExpert();
  dialog.libraryRefs = () => [...chosen.values()];
  if (item) dialog.body.append(node('p', 'expert-hint', 'Saving makes a new version. Its next message follows it; running jobs finish with what they started with.'));
  name.focus();
  return dialog;
}

export async function openExpertChat(item, { request = libraryRequest } = {}) {
  const chat = await request('/api/experts/' + encodeURIComponent(item.template_id) + '/chat', { method: 'POST', body: {} });
  location.assign('/conversation?id=' + encodeURIComponent(chat.conversation_id));
}

/** On this computer all of an Expert's jobs share one work folder: say so once a second person could be added. */
export function sharedFolderNote(kind, people, waiting) {
  if (kind !== 'host' || people + waiting < 2) return '';
  return 'Jobs for everyone here share one work folder on this computer, so a job you approve for one person could read another person’s job files. To keep each person’s work apart, choose a sandboxed workspace in Edit.';
}

/** A Teams card's links. "Test in Teams" opens a chat by the bot's id; before the app is installed and a first message
 *  came through, Teams opened another person's chat with it (first real test), so it is offered only after that. */
export function teamsLinks(channel) {
  const links = [];
  if (channel.app.install_link) links.push({ label: 'Install in Teams', href: channel.app.install_link, style: 'secondary-button' });
  if (channel.first_message && channel.app.app_id) {
    links.push({ label: 'Test in Teams', href: 'https://teams.microsoft.com/l/chat/0/0?users=28:' + encodeURIComponent(channel.app.app_id), style: 'primary-button' });
  }
  return links;
}

/** A channel's pill: Connecting… until Microsoft's socket holds, and ready only once a first message came through. */
export function channelState(channel) {
  if (channel.state === 'off') return 'Off';
  if (channel.state !== 'connected') return 'Not connected';
  if (channel.online) return channel.first_message ? 'Connected' : 'Online · no message yet';
  return channel.connecting ? 'Connecting…' : 'Connected · offline';
}

/** Channels: add Teams, Connect with Microsoft's sign-in, people, Test in Teams. */
export function openChannels(item, { request = libraryRequest, signal } = {}) {
  const dialog = libraryModal(`${item.name} · Channels`, { signal });
  const list = node('div', 'expert-channels'); const status = node('p', 'expert-status'); status.setAttribute('role', 'status');
  dialog.body.append(node('p', 'expert-hint', 'People reach this expert where they already work. Only the people you allow get an answer.'), list, status);
  let timer = 0; let again = 0;
  const stop = () => { if (timer) clearTimeout(timer); timer = 0; };
  dialog.dialog.addEventListener('close', () => { stop(); clearTimeout(again); }, { once: true });
  const path = '/api/experts/' + encodeURIComponent(item.template_id) + '/channels';
  // Where its jobs run decides whether people share a folder; unknown counts as this computer.
  const place = request('/api/experts/' + encodeURIComponent(item.template_id), { signal })
    .then(saved => saved?.expert?.workspace?.kind || 'host').catch(() => 'host');

  function personRow(person, action, label) {
    const row = node('div', 'expert-person'); row.append(node('span', '', person.name || person.id));
    const button = node('button', 'secondary-button', label); button.type = 'button'; button.onclick = action; row.append(button); return row;
  }
  async function save(channel, people) {
    await request('/api/channels/' + encodeURIComponent(channel.binding_id), { method: 'PATCH', body: { people } }); await load();
  }
  async function connect(channel) {
    stop(); status.textContent = 'Starting Microsoft sign-in…';
    try { show(channel, await request('/api/channels/' + encodeURIComponent(channel.binding_id) + '/connect', { method: 'POST', body: {} })); }
    catch (error) { status.textContent = error.message; }
  }
  async function poll(channel) {
    try { show(channel, await request('/api/channels/' + encodeURIComponent(channel.binding_id) + '/connect')); }
    catch (error) { status.textContent = error.message; }
  }
  function show(channel, step) {
    const box = list.querySelector(`[data-binding="${channel.binding_id}"] .expert-connect`); if (!box) return;
    box.replaceChildren();
    if (step.step === 'sign_in') {
      const link = node('a', 'primary-button', 'Open Microsoft sign-in'); link.href = step.url; link.target = '_blank'; link.rel = 'noopener';
      const code = node('code', 'expert-code', step.code);
      const copy = node('button', 'secondary-button', 'Copy code'); copy.type = 'button';
      copy.onclick = async () => { try { await navigator.clipboard.writeText(step.code); copy.textContent = 'Copied'; } catch { copy.textContent = 'Select the code'; } };
      box.append(node('p', '', 'Sign in to Microsoft with the account that should own this Teams app, then enter this code on Microsoft’s page:'), code, copy, link);
      status.textContent = 'Waiting for your sign-in…'; timer = setTimeout(() => poll(channel), 2000); return;
    }
    if (step.step === 'creating') { status.textContent = 'Creating your Teams app…'; timer = setTimeout(() => poll(channel), 2000); return; }
    if (step.step === 'blocked') {
      // Teams does not allow the app yet: the one thing to ask, a note to send, and the page goes on by itself.
      const now = node('button', 'primary-button', 'Check now'); now.type = 'button'; now.onclick = () => connect(channel);
      if (step.note) {  // no note while Microsoft is still applying a change already made
        const note = node('p', 'expert-hint', step.note); note.style.whiteSpace = 'pre-line';
        const copy = node('button', 'secondary-button', 'Copy a note for your administrator'); copy.type = 'button';
        copy.onclick = async () => { try { await navigator.clipboard.writeText(step.note); copy.textContent = 'Copied'; } catch { copy.textContent = 'Select the note below'; } };
        box.append(copy, now, note);
      } else box.append(now);
      status.textContent = step.error || ''; timer = setTimeout(() => poll(channel), 10000); return;
    }
    if (step.step === 'error') {
      const again = node('button', 'primary-button', 'Connect again'); again.type = 'button'; again.onclick = () => connect(channel);
      box.append(again); status.textContent = step.error || 'Setup stopped. Press Connect to try again.'; return;
    }
    status.textContent = ''; void load();
  }
  function render(channels, kind) {
    list.replaceChildren();
    const teams = channels.find(c => c.kind === 'teams');
    if (!teams) {
      const add = node('button', 'primary-button', 'Add Teams'); add.type = 'button';
      add.onclick = async () => { const created = await request(path, { method: 'POST', body: { kind: 'teams' } }); await load(); await connect(created); };
      const slack = node('button', 'secondary-button', 'Slack · next'); slack.type = 'button'; slack.disabled = true;
      list.append(add, slack); return;
    }
    for (const channel of channels) {
      const card = node('section', 'expert-channel'); card.dataset.binding = channel.binding_id;
      const head = node('div', 'expert-channel-head');
      head.append(node('strong', '', channel.label), node('span', 'expert-pill', channelState(channel))); card.append(head);
      if (channel.low_disk) card.append(node('p', 'expert-notice expert-notice-alert', 'Cannot answer right now: this computer is low on disk. Messages wait and are answered when space returns.'));
      if (channel.refusal) card.append(node('p', 'expert-notice expert-notice-alert', `Microsoft refused the Teams connection (“${channel.refusal}”). It is tried again by itself every few minutes.`));
      const connectBox = node('div', 'expert-connect'); card.append(connectBox);
      if (!channel.app.app_id) {
        const go = node('button', 'primary-button', 'Connect'); go.type = 'button'; go.onclick = () => connect(channel); connectBox.append(go);
      } else {
        const links = node('div', 'expert-links'); const name = channel.app.app_name || item.name;
        for (const link of teamsLinks(channel)) { const a = node('a', link.style, link.label); a.href = link.href; a.target = '_blank'; a.rel = 'noopener'; links.append(a); }
        const toggle = node('button', 'secondary-button', channel.state === 'connected' ? 'Turn off' : 'Turn on'); toggle.type = 'button';
        toggle.onclick = async () => { await request('/api/channels/' + encodeURIComponent(channel.binding_id), { method: 'PATCH', body: { state: channel.state === 'connected' ? 'off' : 'connected' } }); await load(); };
        links.append(toggle); card.append(links);
        if (channel.first_message) card.append(node('p', 'expert-hint', `Test in Teams opens your chat with ${name}. If another chat opens, send nothing there.`));
        else if (channel.state === 'connected' && channel.online) card.append(node('p', 'expert-hint', `Press Install in Teams, add ${name}, then send it a message in its own chat, named ${name}. It is ready once that message arrives.`));
      }
      const people = node('div', 'expert-people'); people.append(node('h3', '', 'People who may message it'));
      if (!channel.people.length) people.append(node('p', 'expert-hint', 'Nobody yet.'));
      for (const person of channel.people) people.append(personRow(person, () => save(channel, channel.people.filter(p => p.id !== person.id)), 'Remove'));
      const shared = sharedFolderNote(kind, channel.people.length, channel.people_waiting.length);
      if (shared) people.append(node('p', 'expert-hint', shared));
      if (channel.people_waiting.length) {
        people.append(node('h3', '', 'Waiting to be allowed'));
        for (const person of channel.people_waiting) {
          const clean = { id: person.id, name: person.name || '', tenant: person.tenant || '', email: person.email || '' };
          people.append(personRow(person, () => save(channel, [...channel.people, clean]), 'Allow'));
        }
      }
      if (channel.unconfirmed_messages) people.append(node('p', 'expert-hint', `${channel.unconfirmed_messages} message(s) may not have arrived. They are not resent by themselves.`));
      card.append(people); list.append(card);
    }
    // Until Microsoft's socket holds, look again shortly, so the pill moves on by itself.
    clearTimeout(again); if (channels.some(c => c.state === 'connected' && c.connecting)) again = setTimeout(load, 3000);
  }
  async function load() { try { render((await request(path)).items || [], await place); } catch (error) { status.textContent = error.message; } }
  void load();
  return dialog;
}

/** Needs you: requests from Experts, on top of Activity, with a count and a pop-up on any page. */
export function initNeedsYou({ request = libraryRequest, mount = null, tab = null } = {}) {
  const seen = new Set(); let first = true; let items = [];
  let section = null;
  if (mount) {
    section = node('section', 'needs-you'); section.hidden = true; section.setAttribute('aria-label', 'Needs you');
    mount.before(section);
  }
  const badge = tab ? node('span', 'needs-you-count') : null; if (badge) { badge.hidden = true; tab.append(badge); }
  const toast = node('div', 'needs-you-toast'); toast.hidden = true; toast.setAttribute('role', 'status'); document.body.append(toast);
  const age = iso => { const m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000)); return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`; };
  async function decide(item, decision, note, buttons) {
    buttons.forEach(b => { b.disabled = true; });
    try { await request('/api/expert-reviews/' + encodeURIComponent(item.review_id) + '/decision', { method: 'POST', body: { decision, expected_digest: item.payload_digest, note } }); await refresh(); }
    catch (error) { buttons.forEach(b => { b.disabled = false; }); alertIn(item, error.message); }
  }
  function alertIn(item, text) { const card = section?.querySelector(`[data-review="${item.review_id}"] .needs-you-error`); if (card) { card.textContent = text; card.hidden = false; } }
  function render() {
    if (badge) { badge.textContent = String(items.length); badge.hidden = !items.length; }
    if (!section) return;
    section.hidden = !items.length; section.replaceChildren();
    const head = node('div', 'needs-you-head'); head.append(node('h2', '', 'Needs you'));
    if ('Notification' in window && Notification.permission === 'default') {
      const allow = node('button', 'secondary-button', 'Notify me'); allow.type = 'button'; allow.onclick = () => Notification.requestPermission().then(render); head.append(allow);
    }
    section.append(head);
    for (const item of items) {
      const card = node('article', 'needs-you-card'); card.dataset.review = item.review_id;
      card.append(node('p', 'needs-you-from', `${item.from.name || 'Someone'} · ${item.channel || 'Chat'} · ${age(item.created_at)} · ${item.expert}`));
      if (item.asked) card.append(node('p', 'needs-you-asked', `Asked: “${item.asked}”`));
      if (item.reason) card.append(node('p', 'needs-you-reason', `Why it asks: ${item.reason}`));
      const what = item.kind === 'reply' ? `Will send: ${item.payload.text}` : `Will start a job: ${item.payload.goal}`;
      card.append(node('p', 'needs-you-what', what));
      if (item.kind === 'job' && item.payload.context) card.append(node('p', 'expert-hint', item.payload.context));
      const note = node('input'); note.placeholder = 'Note for the expert (optional)'; note.maxLength = 2000;
      const approve = node('button', 'primary-button', 'Approve'); approve.type = 'button';
      const reject = node('button', 'secondary-button', 'Reject'); reject.type = 'button';
      const buttons = [approve, reject];
      approve.onclick = () => decide(item, 'approve', note.value, buttons); reject.onclick = () => decide(item, 'reject', note.value, buttons);
      const actions = node('div', 'needs-you-actions'); actions.append(note, approve, reject);
      const error = node('p', 'needs-you-error'); error.hidden = true; error.setAttribute('role', 'alert');
      card.append(actions, error); section.append(card);
    }
  }
  function announce(item) {
    const text = `${item.expert} asks you first · ${item.from.name || 'Someone'} on ${item.channel || 'Chat'}`;
    toast.replaceChildren(node('span', '', text));
    const open = node('a', 'secondary-button', 'Open'); open.href = '/#activity'; toast.append(open); toast.hidden = false;
    setTimeout(() => { toast.hidden = true; }, 12000);
    if ('Notification' in window && Notification.permission === 'granted' && document.visibilityState !== 'visible') {
      try { new Notification('xPerfect: Needs you', { body: text }); } catch { /* The in-page pop-up remains. */ }
    }
  }
  // Things to know, not to act on: a backup took over, or a limit stopped a reply.
  let recent = null; let tick = 0;
  if (mount) { recent = node('section', 'expert-notices'); recent.hidden = true; recent.setAttribute('aria-label', 'Recent'); mount.before(recent); }
  const aiName = profile => (AIS.find(([p]) => p === profile) || [profile, profile || 'The AI'])[1];
  const clock = iso => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
  async function refreshNotices() {
    if (!recent) return;
    try {
      const notices = (await request('/api/expert-notices')).items || [];
      recent.hidden = !notices.length; recent.replaceChildren(node('h2', '', 'Recent'));
      for (const n of notices.slice(0, 8)) {
        if (n.kind === 'low_disk') {
          const waiting = n.waiting === 1 ? ' 1 message waits and is answered when space returns.' : n.waiting ? ` ${n.waiting} messages wait and are answered when space returns.` : ' Messages wait and are answered when space returns.';
          const line = node('p', 'expert-notice expert-notice-alert');
          line.append(node('strong', '', 'Low on disk'), node('span', '', ` · This computer has ${n.available_mb} MB free and needs ${n.required_mb} MB, so your Experts cannot answer anyone.${waiting}`));
          recent.append(line); continue;
        }
        const who = n.channel === 'chat' ? ' in your Chat' : n.person ? ` for ${n.person}` : '';
        const text = n.kind === 'backup'
          ? `Backup took over${who}: ${aiName(n.backup_ai)} answered because ${aiName(n.ai)} is at its limit${n.until ? ` until ${clock(n.until)}` : ''}.`
          : `Could not answer${who}: ${aiName(n.ai)} is at its limit and no backup took over.`;
        const line = node('p', 'expert-notice'); line.append(node('strong', '', n.expert), node('span', '', ` · ${age(n.at)} · ${text}`));
        recent.append(line);
      }
    } catch { /* Keep the last known lines; the next refresh retries. */ }
  }
  async function refresh() {
    try {
      items = (await request('/api/expert-reviews?state=pending')).items || [];
      for (const item of items) { if (!seen.has(item.review_id)) { seen.add(item.review_id); if (!first) announce(item); } }
      first = false; render();
    } catch { /* Keep the last known list; the next refresh retries. */ }
    if (tick++ % 4 === 0) void refreshNotices();
  }
  void refresh(); setInterval(refresh, 4000);
  return { refresh };
}
