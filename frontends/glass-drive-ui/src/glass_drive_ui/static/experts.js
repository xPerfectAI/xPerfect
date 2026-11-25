/* Experts people can talk to: one form for New and Edit, Channels with Connect, and Needs you. */
import { libraryRequest, libraryModal } from './personal-library.js?v=20261024polish1';

const node = (tag, cls = '', text = '') => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
const AIS = [['claude-code', 'Claude'], ['codex-cli', 'Codex'], ['grok-build', 'Grok']];
const PROVIDER = { 'claude-code': ['claude', 'anthropic'], 'codex-cli': ['codex', 'openai'], 'grok-build': ['grok', 'xai'] };
const MARK_VERSION = '20261024vector1';
/** The AI's own mark, as the Worker pages already show it; theme.css keeps it legible in the dark theme. */
export function aiMark(profile) {
  const name = PROVIDER[profile]?.[0] || '';
  const img = node('img', 'provider-mark expert-ai-mark'); img.alt = ''; img.width = 18; img.height = 18;
  if (name) { img.dataset.provider = name; img.src = `/static/provider-${name}.svg?v=${MARK_VERSION}`; } else img.hidden = true;
  return img;
}
/** A choice of AI with its mark beside it, kept in step with the choice. */
function withMark(choice) {
  const pick = node('span', 'expert-ai-pick'); let mark = aiMark(choice.value); pick.append(mark, choice);
  choice.addEventListener('change', () => { const next = aiMark(choice.value); mark.replaceWith(next); mark = next; });
  return pick;
}
// Channel marks of xPerfect's own: no third-party logo ships in this repository.
const GLYPHS = {
  teams: '<path d="M6 7.5h7a2.5 2.5 0 0 1 2.5 2.5v4a2.5 2.5 0 0 1-2.5 2.5H9.2L6 19v-2.5a2.5 2.5 0 0 1-2.5-2.5v-4A2.5 2.5 0 0 1 6 7.5z"/>'
       + '<path d="M9 7.5V7a2.5 2.5 0 0 1 2.5-2.5h7A2.5 2.5 0 0 1 21 7v4a2.5 2.5 0 0 1-2.5 2.5H18v2.3l-2.5-2.3"/>',
  slack: '<rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><path d="M10.4 7.5 9.3 16.5M14.7 7.5l-1.1 9M7.6 10.4h9.3M7.1 13.6h9.3"/>',
};
export const CHANNEL_NAMES = { teams: 'Microsoft Teams', slack: 'Slack' };
/** A channel's mark: the neutral one, or the brand's own mark an owner placed in this install's state (brand-assets),
 *  shown unaltered beside the plain-text name and never on a button. */
export function channelMark(kind, brand = {}) {
  if (brand[kind]) {
    const img = node('img', 'channel-mark channel-brand-mark'); img.src = brand[kind]; img.alt = ''; img.width = 20; img.height = 20;
    return img;
  }
  const mark = node('span', 'channel-mark'); mark.setAttribute('aria-hidden', 'true');
  mark.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${GLYPHS[kind] || GLYPHS.teams}</svg>`;
  return mark;
}
let brandAssets = null;
/** The marks an owner placed in this install's own state, if any (served by this page: the content policy stays). */
export function loadBrandAssets(get = (url) => fetch(url, { credentials: 'same-origin' })) {
  if (!brandAssets) brandAssets = Promise.resolve().then(() => get('/brand-assets.json'))
    .then(r => (r && r.ok ? r.json() : {})).then(j => (j && j.channels) || {}).catch(() => ({}));
  return brandAssets;
}
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
                                        ['claude-code:sonnet', 'Sonnet', 'claude-code:sonnet'], ['claude-code:haiku', 'Haiku', 'claude-code:haiku']],
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
    const l = node('label', 'expert-check'); l.append(box, aiMark(value), node('span', '', text)); workers.append(l); work.set(value, box);
  }
  team.append(labelled('Talks to people', withMark(talk), 'Answers at once and hands work to the coordinator.'),
              labelled('Model', talkModel, 'Time to a finished reply, measured on this computer.'),
              labelled('Replies', replies),
              labelled('Coordinates the work', withMark(coordinate)),
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
const PICTURE_SIZE = 192;
/** Any image, cut to a centred square and drawn at 192 by 192 (the size Teams wants for an app's colour icon). */
export async function squarePng(file, size = PICTURE_SIZE) {
  const bitmap = await createImageBitmap(file);
  const side = Math.min(bitmap.width, bitmap.height);
  const canvas = document.createElement('canvas'); canvas.width = size; canvas.height = size;
  canvas.getContext('2d').drawImage(bitmap, (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side, 0, 0, size, size);
  return canvas.toDataURL('image/png');
}
/** An Expert's picture: kept in this install's state, not in its versions; Teams shows it as its app icon. */
function pictureField(item, request, signal) {
  const field = node('div', 'personal-library-field expert-field'); field.append(node('span', '', 'Picture'));
  const row = node('div', 'expert-picture');
  const preview = node('img', 'expert-picture-preview'); preview.alt = ''; preview.width = 64; preview.height = 64; preview.hidden = true;
  const empty = node('span', 'expert-picture-empty', 'None');
  const input = node('input', 'expert-picture-input'); input.type = 'file'; input.accept = 'image/png,image/jpeg,image/webp';
  const choose = node('button', 'secondary-button', 'Choose a picture'); choose.type = 'button'; choose.onclick = () => input.click();
  const remove = node('button', 'secondary-button', 'Remove'); remove.type = 'button'; remove.hidden = true;
  let change = null;
  const show = (url) => { preview.src = url || ''; preview.hidden = !url; empty.hidden = !!url; remove.hidden = !url; };
  if (item) request('/api/experts/' + encodeURIComponent(item.template_id) + '/picture', { signal }).then(r => show(r?.picture || '')).catch(() => {});
  input.onchange = async () => {
    const file = input.files?.[0]; if (!file) return;
    const url = await squarePng(file); change = { png_base64: url.slice(url.indexOf(',') + 1) }; show(url);
  };
  remove.onclick = () => { change = { remove: true }; input.value = ''; show(''); };
  const controls = node('div', 'expert-picture-controls'); controls.append(choose, remove, input);
  row.append(preview, empty, controls);
  field.append(row, node('small', 'expert-hint', 'Teams shows it as the app icon. Kept on this computer, not in its versions.'));
  return { field, changed: () => !!change, body: () => change };
}

export async function openExpertForm({ item = null, request = libraryRequest, signal, onSaved, accounts = [] } = {}) {
  let current = null;
  if (item) { try { current = await request('/api/experts/' + encodeURIComponent(item.template_id), { signal }); } catch { current = null; } }
  let name, description, read, picture;
  const dialog = libraryModal(item ? 'Edit expert' : 'New expert', { action: 'Save', signal, onSave: async ({ signal: saveSignal }) => {
    const expert = read();
    const body = { name: name.value.trim(), description: description.value.trim(), expert, personal_library_refs: dialog.libraryRefs ? dialog.libraryRefs() : [] };
    const saved = item
      ? await request('/api/experts/' + encodeURIComponent(item.template_id), { method: 'PATCH', signal: saveSignal, body: { ...body, expected_catalog_revision: (current || item).catalog_revision } })
      : await request('/api/experts', { method: 'POST', signal: saveSignal, body });
    if (picture.changed() && saved?.template_id) {
      await request('/api/experts/' + encodeURIComponent(saved.template_id) + '/picture', { method: 'PUT', signal: saveSignal, body: picture.body() });
    }
    onSaved?.(saved);
  } });
  name = node('input'); name.required = true; name.maxLength = 160; name.value = item?.name || '';
  description = node('textarea'); description.rows = 2; description.maxLength = 1000; description.value = current?.description ?? item?.description ?? '';
  picture = pictureField(item, request, signal);
  dialog.body.append(labelled('Expert name', name), labelled('What does this expert do?', description), picture.field);
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
  const catalogue = channel.app.catalogue;
  // In the organisation's catalogue (the administrator's route): its link opens the app there, ready to add.
  if (channel.app.install_link && (catalogue === 'listed' || catalogue === 'installed') && !channel.first_message) {
    links.push({ label: 'Open in Teams', href: channel.app.install_link, style: 'secondary-button' });
  }
  // Once an administrator added the app, this link opens the developer copy, which stays blocked: not offered.
  // Once a first message came through, the app is installed for the owner; this card is theirs alone.
  else if (channel.app.install_link && !catalogue && !channel.app.added_by_admin && !channel.first_message) links.push({ label: 'Install in Teams', href: channel.app.install_link, style: 'secondary-button' });
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
  // An administrator's one press: Microsoft's own sign-in and consent page is the yes; the button names the change.
  function adminRoute(channel, primary) {
    const name = channel.app?.app_name || item.name;
    const button = node('button', primary ? 'primary-button' : 'secondary-button', `Add ${name} to my organisation's Teams apps`); button.type = 'button';
    button.onclick = () => connect(channel, { as_admin: true });
    const hint = node('p', 'expert-hint', `For a Teams administrator. Microsoft asks you to sign in and to approve, on its own page, three permissions: “Read and write to all app catalogs”, “Manage user's installed Teams apps” and “Sign in and read user profile”. ${name} then goes into your organisation's Teams apps, where people may find it as your organisation's settings allow; only the people listed here get an answer. It is installed for you when you sign in with the account that made it. An administrator can delete it again in the Teams admin center.`);
    return [button, hint];
  }
  // An administrator who published or deployed the app makes the developer copy's install link a dead end.
  function adminButton(channel, added) {
    const button = node('button', 'expert-other-account', added ? 'An administrator added it for me' : 'Undo: show Install in Teams again'); button.type = 'button';
    button.onclick = async () => {
      try { await request('/api/channels/' + encodeURIComponent(channel.binding_id) + '/added-by-admin', { method: 'POST', body: { added } }); await load(); }
      catch (error) { status.textContent = error.message; }
    };
    return button;
  }
  // The app's public details, which Teams shows to everyone who adds it, instead of the tool's placeholders.
  function appDetails(channel) {
    const about = node('details', 'expert-details'); about.append(node('summary', '', 'Details Teams shows for this app'));
    const inputs = {};
    for (const [key, label, value] of [['developer', 'Developer name', channel.app.developer], ['website', 'Website', channel.app.website_url],
      ['privacy', 'Privacy policy link', channel.app.privacy_url], ['terms', 'Terms of use link', channel.app.terms_url]]) {
      const input = node('input'); input.name = key; input.value = value || '';
      if (key === 'developer') input.maxLength = 32; else { input.type = 'url'; input.placeholder = 'https://'; }
      inputs[key] = input; about.append(labelled(label, input));
    }
    const keep = node('button', 'secondary-button', 'Save details'); keep.type = 'button';
    keep.onclick = async () => {
      status.textContent = 'Saving the details in Teams…';
      try {
        await request('/api/channels/' + encodeURIComponent(channel.binding_id) + '/details', { method: 'POST', body: Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value])) });
        status.textContent = 'Saved: Teams shows them on the app’s page.'; await load();
      } catch (error) { status.textContent = error.message; }
    };
    about.append(node('p', 'expert-hint', 'Teams shows these to everyone who adds the app. Microsoft needs all four; the links start with https://.'), keep);
    if (channel.app.catalogue_id || channel.app.added_by_admin) about.append(node('p', 'expert-hint', 'This app is in your organisation’s Teams apps, and Teams shows the details of the version there. Saving here changes the app’s own copy; what Teams shows changes when an administrator adds a new version of the app there.'));
    else about.append(node('p', 'expert-hint', 'If an administrator added this app to your organisation’s Teams apps, Teams shows the details of the version there, and changing them there takes a new version that an administrator adds.'));
    return about;
  }
  async function save(channel, people) {
    await request('/api/channels/' + encodeURIComponent(channel.binding_id), { method: 'PATCH', body: { people } }); await load();
  }
  async function connect(channel, choice = {}) {
    stop(); status.textContent = choice.as_account ? 'Creating your Teams app…' : 'Starting Microsoft sign-in…';
    try { show(channel, await request('/api/channels/' + encodeURIComponent(channel.binding_id) + '/connect', { method: 'POST', body: choice })); }
    catch (error) { status.textContent = error.message; }
  }
  // Already signed in: the button names the account and organisation, so its one press is the yes (another
  // organisation's login can sit on the same computer); a small button beside it takes another account.
  async function nameConnect(channel, go, box) {
    try {
      const who = (await request('/api/channels/' + encodeURIComponent(channel.binding_id) + '/connect')).signed_in_as;
      if (!who || !who.account || !go.isConnected) return;
      go.textContent = `Connect Teams as ${who.account}`; go.dataset.account = who.account;
      const other = node('button', 'expert-other-account', 'Use another Microsoft account'); other.type = 'button';
      other.onclick = () => connect(channel, { other_account: true });
      box.append(other);
      if (who.tenant_id) box.append(node('small', 'expert-hint', `Organisation ID ${who.tenant_id}`));
    } catch { /* the plain Connect still works: it signs in with Microsoft's code */ }
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
      box.append(node('p', '', 'Open Microsoft’s page where your work account is signed in (any browser, or your phone) and enter this code. Use the account that should own this Teams app. Microsoft may ask you to approve on your phone. The code lasts about 15 minutes.'), code, copy, link);
      status.textContent = 'Waiting for your sign-in…'; timer = setTimeout(() => poll(channel), 2000); return;
    }
    if (step.step === 'creating') { status.textContent = step.account ? `Creating your Teams app with ${step.account}…` : 'Creating your Teams app…'; timer = setTimeout(() => poll(channel), 2000); return; }
    if (step.step === 'admin_sign_in') {
      const link = node('a', 'primary-button', 'Open Microsoft sign-in'); link.href = step.url; link.target = '_blank'; link.rel = 'noopener';
      const code = node('code', 'expert-code', step.code);
      const copy = node('button', 'secondary-button', 'Copy code'); copy.type = 'button';
      copy.onclick = async () => { try { await navigator.clipboard.writeText(step.code); copy.textContent = 'Copied'; } catch { copy.textContent = 'Select the code'; } };
      box.append(node('p', '', 'Open Microsoft’s page, sign in with a Teams administrator account of this organisation and enter this code. Microsoft then shows what this setup asks for; approving it there is the yes. The code lasts about 15 minutes.'));
      if (step.account) box.append(node('p', 'expert-hint', `For the Teams app made with ${step.account}${step.tenant_id ? ` · organisation ID ${step.tenant_id}` : ''}.`));
      box.append(code, copy, link);
      status.textContent = 'Waiting for the administrator’s sign-in…'; timer = setTimeout(() => poll(channel), 2000); return;
    }
    if (step.step === 'adding') { status.textContent = step.progress || 'Adding it to your organisation’s Teams apps…'; timer = setTimeout(() => poll(channel), 2000); return; }
    if (step.step === 'blocked' || step.step === 'admin_waiting') {
      // Teams does not allow the app yet: the one thing to ask, a note to send, and the page goes on by itself;
      // an administrator can instead add it to the organisation's Teams apps with one press.
      const waiting = step.step === 'admin_waiting';
      const now = node('button', step.admin ? 'secondary-button' : 'primary-button', 'Check now'); now.type = 'button'; now.onclick = () => connect(channel);
      if (step.admin) box.append(...adminRoute(channel, true));
      else box.append(node('p', 'expert-hint', 'One press for a Teams administrator is not available on this xPerfect yet: it needs a Microsoft sign-in for administrators, which the owner of this xPerfect sets up. Meanwhile, send the note to your administrator.'));
      if (step.note) {  // no note while Microsoft is still applying a change already made
        const note = node('p', 'expert-hint', step.note); note.style.whiteSpace = 'pre-line';
        const copy = node('button', 'secondary-button', 'Copy a note for your administrator'); copy.type = 'button';
        copy.onclick = async () => { try { await navigator.clipboard.writeText(step.note); copy.textContent = 'Copied'; } catch { copy.textContent = 'Select the note below'; } };
        box.append(copy); if (!waiting) box.append(now); box.append(note);
      } else if (!waiting) box.append(now);
      status.textContent = step.error || ''; if (!waiting) timer = setTimeout(() => poll(channel), 10000); return;
    }
    if (step.step === 'error') {
      const again = node('button', 'primary-button', 'Connect again'); again.type = 'button'; again.onclick = () => connect(channel);
      box.append(again); status.textContent = step.error || 'Setup stopped. Press Connect to try again.'; return;
    }
    status.textContent = ''; void load();
  }
  function render(channels, kind, brand = {}) {
    list.replaceChildren();
    const teams = channels.find(c => c.kind === 'teams');
    if (!teams) {
      const add = node('button', 'primary-button', 'Add Teams'); add.type = 'button'; add.prepend(channelMark('teams'));
      add.onclick = async () => { const created = await request(path, { method: 'POST', body: { kind: 'teams' } }); await load(); await connect(created); };
      const slack = node('button', 'secondary-button', 'Slack · next'); slack.type = 'button'; slack.disabled = true; slack.prepend(channelMark('slack'));
      list.append(add, slack); return;
    }
    for (const channel of channels) {
      const card = node('section', 'expert-channel'); card.dataset.binding = channel.binding_id;
      const head = node('div', 'expert-channel-head');
      const title = node('span', 'expert-channel-title');
      title.append(channelMark(channel.kind, brand), node('strong', '', CHANNEL_NAMES[channel.kind] || channel.label));
      head.append(title, node('span', 'expert-pill', channelState(channel))); card.append(head);
      if (channel.low_disk) card.append(node('p', 'expert-notice expert-notice-alert', 'Cannot answer right now: this computer is low on disk. Messages wait and are answered when space returns.'));
      if (channel.refusal && channel.state === 'connected') card.append(node('p', 'expert-notice expert-notice-alert', `Microsoft refused the Teams connection (“${channel.refusal}”). It is tried again by itself every few minutes.`));
      const connectBox = node('div', 'expert-connect'); card.append(connectBox);
      if (!channel.app.app_id) {
        const go = node('button', 'primary-button', 'Connect'); go.type = 'button'; go.onclick = () => connect(channel, { as_account: go.dataset.account || '' }); connectBox.append(go);
        void nameConnect(channel, go, connectBox);
      } else {
        const links = node('div', 'expert-links'); const name = channel.app.app_name || item.name;
        for (const link of teamsLinks(channel)) { const a = node('a', link.style, link.label); a.href = link.href; a.target = '_blank'; a.rel = 'noopener'; links.append(a); }
        const toggle = node('button', 'secondary-button', channel.state === 'connected' ? 'Turn off' : 'Turn on'); toggle.type = 'button';
        toggle.onclick = async () => { await request('/api/channels/' + encodeURIComponent(channel.binding_id), { method: 'PATCH', body: { state: channel.state === 'connected' ? 'off' : 'connected' } }); await load(); };
        links.append(toggle); card.append(links);
        // Which Microsoft account and organisation the app was made in, as Microsoft reported them.
        if (!channel.app.scopes && channel.app.app_id) card.append(node('p', 'expert-hint', 'This app was made before xPerfect narrowed new apps to personal chats, so Teams may still offer it in teams and group chats; it answers only in personal chats. Narrowing it takes a new version of the app; for an app in your organisation’s Teams apps, an administrator adds that version there.'));
        if (channel.app.scopes && channel.app.scopes !== 'personal') card.append(node('p', 'expert-hint', 'Teams still offers this app in teams and group chats; it answers only in personal chats. Narrowing it takes a new version of the app; for an app in your organisation’s Teams apps, an administrator adds that version there.'));
        if (channel.app.account) card.append(node('p', 'expert-hint', `Made with ${channel.app.account}${channel.app.tenant_id ? ` · organisation ID ${channel.app.tenant_id}` : ''}.`));
        if (channel.app.picture_digest) card.append(node('p', 'expert-hint', `Teams shows ${name}'s picture as its app icon.`));
        else if (channel.app.picture_error) card.append(node('p', 'expert-notice', `${name}'s picture is not in Teams yet: ${channel.app.picture_error}`));
        const catalogue = channel.app.catalogue;
        if (channel.first_message) card.append(node('p', 'expert-hint', `Test in Teams opens your chat with ${name}. If another chat opens, send nothing there.`));
        else if (catalogue === 'installed') card.append(node('p', 'expert-hint', `${name} is in your organisation's Teams apps and installed for you. Write in its own chat, named ${name}: it is ready once that message arrives. Open in Teams goes straight to it.`));
        else if (catalogue === 'listed') card.append(node('p', 'expert-hint', `${name} is in your organisation's Teams apps. Press Open in Teams, then Add, and write in its own chat, named ${name}: it is ready once that message arrives. Microsoft can take a few minutes before Add works.`));
        else if (catalogue === 'pending') {
          card.append(node('p', 'expert-notice', `${name}'s Teams app is made, but it is not in your organisation's Teams apps yet.`));
          if (channel.admin_route) card.append(...adminRoute(channel, true));
        }
        else if (channel.app.added_by_admin) card.append(node('p', 'expert-hint', `An administrator added ${name} for you: it appears in your Teams by itself (Apps, Built for your org; this can take a few hours). Then write in its own chat, named ${name}.`), adminButton(channel, false));
        else if (channel.state === 'connected' && channel.online) card.append(node('p', 'expert-hint', `Press Install in Teams and add ${name}; Teams may ask web or desktop, either works, and if the add window does not appear, press Install in Teams again. Then send it a message in its own chat, named ${name}: it is ready once that message arrives. Microsoft may still be applying this. If Teams says Permissions needed, wait a few minutes and press Install again.`));
        if (!channel.first_message && !channel.app.added_by_admin && !catalogue) {
          card.append(adminButton(channel, true));
          if (channel.admin_route) card.append(...adminRoute(channel, false));
        }
        card.append(appDetails(channel));
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
      card.append(people);
      if (brand[channel.kind] && channel.kind === 'teams') card.append(node('p', 'expert-hint expert-trademark', 'Microsoft Teams is a trademark of the Microsoft group of companies.'));
      list.append(card);
    }
    // Until Microsoft's socket holds, look again shortly, so the pill moves on by itself.
    // Never while the person is typing or has the details open: a re-load rebuilds the cards.
    const later = () => { if (list.querySelector('details[open]') || list.contains(document.activeElement)) again = setTimeout(later, 3000); else void load(); };
    clearTimeout(again); if (channels.some(c => c.state === 'connected' && c.connecting)) again = setTimeout(later, 3000);
  }
  async function load() { try { render((await request(path)).items || [], await place, await loadBrandAssets()); } catch (error) { status.textContent = error.message; } }
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
      if (item.kind === 'job' && item.payload.tell) card.append(node('p', 'needs-you-what', `Will tell the person: ${item.payload.tell}`));
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
