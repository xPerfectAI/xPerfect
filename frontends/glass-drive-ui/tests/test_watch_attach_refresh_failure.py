"""An accepted Watch attach stays one attach for the owner who made it. If the Files refresh after
the attach fails, the page clears its owner; the accepted upload must still not come back as
ready, after the owner returns or after a reload, and a repeated attach of the same upload must
not add another copy. Runs the real files.js drafts and Files client with Watch's exact attach
handlers under Node, against a server that replays an accepted request key like the runtime."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src/glass_drive_ui/static"
node = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to run the page scripts")


def _watch_attach_block() -> str:
    source = (STATIC / "watch.js").read_text()
    start = source.index("const attachStarted = new Set();")
    return source[start:source.index("const watchDraft = createFileDraft(", start)]


_HARNESS = r"""
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', {value: webcrypto});
class Element {
  constructor() { this.children = []; this.listeners = {}; this.value = ''; this.hidden = false; this.classList = {add() {}, remove() {}, toggle() {}}; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute() {} removeAttribute() {} focus() {}
}
globalThis.document = {createElement: () => new Element()};
Object.defineProperty(globalThis, 'navigator', {value: {platform: 'test'}, configurable: true});
// Session storage survives a reload of the same tab.
const stored = new Map();
Object.defineProperty(globalThis, 'sessionStorage', {value: {
  getItem: (k) => (stored.has(k) ? stored.get(k) : null), setItem: (k, v) => stored.set(k, String(v)), removeItem: (k) => stored.delete(k)}});
const ownerA = 'a'.repeat(64), ownerB = 'b'.repeat(64);
const receipts = new Map(); const bindings = new Map(); let projections = 0; let listingFails = false;
const refused = new Set(); const loseResponse = new Set(); let holdNextAttach = null;
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
globalThis.fetch = async (url, options = {}) => {
  if (url === '/api/storage') return response({limit_bytes: null});
  if (url.startsWith('/api/file-uploads?')) {
    const id = new URL(url, 'https://example.invalid').searchParams.get('draft_id');
    return response({items: [...receipts.values()].filter((x) => x.draft_id === id)});
  }
  if (url === '/api/file-uploads') {
    const body = JSON.parse(options.body), receipt = {...body, upload_id: 'fil_' + receipts.size, state: 'pending'};
    receipts.set(receipt.upload_id, receipt); return response(receipt, 201);
  }
  if (url.startsWith('/api/workspace/') && options.method === 'POST') {
    // Like the runtime: an accepted request key replays its binding; a new key projects again.
    const body = JSON.parse(options.body);
    if (holdNextAttach) { const release = holdNextAttach; holdNextAttach = null; await release; }
    if (body.upload_ids.some((id) => refused.has(id))) return response({detail: 'Not enough storage for this file'}, 507);
    if (!bindings.has(body.idempotency_key)) {
      bindings.set(body.idempotency_key, {uploads: body.upload_ids, directory: body.directory});
      projections += body.upload_ids.length;
    }
    if (body.upload_ids.some((id) => loseResponse.delete(id))) throw new Error('connection lost');
    return response({items: body.upload_ids.map((id) => ({upload_id: id})), state: 'available'}, 201);
  }
  if (url.startsWith('/api/workspace/')) {
    if (listingFails) return response({detail: 'Files are unavailable (503).'}, 503);
    return response({items: [], can_write: true, drag_out_targets: []});
  }
  throw new Error('Unexpected request ' + url);
};
globalThis.XMLHttpRequest = class {
  constructor() { this.upload = {}; }
  open(method, url) { this.id = url.split('/')[3]; }
  setRequestHeader() {}
  send(file) { const r = receipts.get(this.id); Object.assign(r, {state: 'ready', received_bytes: file.size, sha256: 'verified', revision: 'r1'});
    this.status = 200; this.responseText = JSON.stringify(r); queueMicrotask(() => this.onload()); }
};
const tick = () => new Promise((resolve) => setTimeout(resolve, 30));
const errors = []; const fileError = (message) => errors.push(message);
const filesAttach = new Element();

// One Watch page: the Files client, its draft and Watch's exact attach handlers. Each page loads its
// own files.js instance, as a reload does, so only session storage carries over.
let pages = 0;
async function page() {
  const {createFileDraft, createWorkspaceFiles} = await import(`${FILES_MODULE}?page=${++pages}`);
  let watchDraft;
  const workspaceFiles = createWorkspaceFiles(new Proxy({workerId: 'worker-test', csrf: () => '', canUpload: () => true,
    // Like Watch: a failed Files listing clears the draft owner until access is verified again.
    onAccess: (canWrite) => { if (!canWrite) watchDraft.setOwnerScope(null); }},
    {get(target, name) { return name in target ? target[name] : new Element(); }}));
  const handlers = new Function('workspaceFiles', 'filesAttach', 'fileError', 'getDraft', `
    const watchDraftProxy = new Proxy({}, {get: (_, name) => { const d = getDraft(); const v = d[name]; return typeof v === 'function' ? v.bind(d) : v; }});
    return (function (watchDraft) { __BLOCK__; return {attachReadyUpload, attachPendingUploads, unattachedIds}; })(watchDraftProxy);`)(
    workspaceFiles, filesAttach, fileError, () => watchDraft);
  watchDraft = createFileDraft({input: new Element(), drop: new Element(), list: new Element(), help: new Element(),
    csrf: () => '', scope: 'watch.worker-test', quietReady: true, dropOpensPicker: false,
    onReady: handlers.attachReadyUpload});
  return {draft: watchDraft, handlers, files: workspaceFiles};
}

"""

_REFRESH_FAILURE = r"""
// 1. The attach is accepted, then the Files refresh fails and clears the page's owner.
let first = await page();
first.draft.setOwnerScope(ownerA); await tick();
listingFails = true;
const blob = new Blob(['synthetic bytes']); blob.name = 'dropped.txt';
first.draft.addFiles([blob]); await tick(); await tick();
assert.equal(projections, 1);
assert.equal(first.draft.ownerScope(), null);
const uploadId = [...receipts.keys()][0];

// 2. The owner comes back: the accepted upload is not left waiting as ready.
listingFails = false;
first.draft.setOwnerScope(ownerA); await tick();
assert.deepEqual(first.draft.readyIds(), []);

// 3. Reload: a new page with the same session storage does not offer it again.
let second = await page();
second.draft.setOwnerScope(ownerA); await tick();
assert.deepEqual(second.draft.readyIds(), []);

// 4. Even if the local record is lost, attaching the same upload again replays the accepted attach.
for (const k of [...stored.keys()]) if (k.includes('.attached.')) stored.delete(k);
let third = await page();
third.draft.setOwnerScope(ownerA); await tick();
assert.deepEqual(third.draft.readyIds(), [uploadId]);
await third.handlers.attachPendingUploads(); await tick();
assert.equal(projections, 1);
assert.deepEqual(third.draft.readyIds(), []);

// 5. Owner isolation: an attach accepted for A while B is signed in changes nothing for B.
let fourth = await page();
fourth.draft.setOwnerScope(ownerA); await tick();
const blob2 = new Blob(['second synthetic bytes']); blob2.name = 'second.txt';
listingFails = true; fourth.draft.addFiles([blob2]); await tick(); await tick();
const secondUpload = [...receipts.keys()][1];
listingFails = false;
fourth.draft.setOwnerScope(ownerB); await tick();
assert.deepEqual(fourth.draft.readyIds(), []);
const keysB = [...stored.keys()].filter((k) => k.includes(ownerB) && k.includes('.attached.'));
assert.ok(keysB.every((k) => !stored.get(k).includes(secondUpload)));
fourth.draft.setOwnerScope(ownerA); await tick();
assert.deepEqual(fourth.draft.readyIds(), []);
assert.equal(projections, 2);
assert.deepEqual(errors, []);
console.log('pass');
"""


@node
def test_an_accepted_attach_is_not_offered_again_after_a_failed_refresh_or_reload_and_attaches_once():
    _run(_REFRESH_FAILURE)


def _run(scenario: str) -> None:
    module = (STATIC / "files.js").as_uri()
    block = _watch_attach_block().replace("`", "\\`").replace("${", "\\${")
    script = f"const FILES_MODULE = {json.dumps(module)};\n" + _HARNESS.replace("__BLOCK__", block) + scenario
    result = subprocess.run(["node", "--input-type=module", "--eval", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip() == "pass"


_PARTIAL_BATCH = r"""
// Three drops are refused at first, so all three wait with Add to workspace, filed for the root folder.
let tab = await page();
tab.draft.setOwnerScope(ownerA); await tick();
refused.add('fil_0'); refused.add('fil_1'); refused.add('fil_2');
tab.draft.addFiles(['a.txt', 'b.txt', 'c.txt'].map((name) => { const b = new Blob([name]); b.name = name; return b; }));
await tick(); await tick(); await tick();
assert.deepEqual([...tab.draft.readyIds()].sort(), ['fil_0', 'fil_1', 'fil_2']);
assert.equal(projections, 0); assert.equal(errors.length, 3);

// The user opens another folder, then retries all three: A is applied but its answer is lost,
// B is refused again and C is accepted. C still goes where it was first filed.
await tab.files.open('docs');
refused.delete('fil_0'); refused.delete('fil_2'); loseResponse.add('fil_0');
await tab.handlers.attachPendingUploads(); await tick();
const destinations = () => [...bindings.values()].map((b) => [b.uploads[0], b.directory]).sort();
assert.deepEqual(destinations(), [['fil_0', ''], ['fil_2', '']]);
assert.equal(projections, 2);
assert.deepEqual([...tab.draft.readyIds()].sort(), ['fil_0', 'fil_1']);

// Retrying from the other folder replays A's accepted attach instead of adding a copy.
await tab.handlers.attachPendingUploads(); await tick();
assert.equal(projections, 2);
assert.deepEqual([...tab.draft.readyIds()].sort(), ['fil_1']);

// A later drop is not held back by the upload that is still refused.
const later = new Blob(['d']); later.name = 'd.txt';
tab.draft.addFiles([later]); await tick(); await tick();
assert.equal(projections, 3);
assert.deepEqual([...tab.draft.readyIds()].sort(), ['fil_1']);

// Once B can be stored, it goes to the folder it was first filed for.
refused.delete('fil_1');
await tab.handlers.attachPendingUploads(); await tick();
assert.deepEqual([...tab.draft.readyIds()].sort(), []);
assert.equal(projections, 4);
assert.deepEqual(destinations(), [['fil_0', ''], ['fil_1', ''], ['fil_2', ''], ['fil_3', 'docs']]);
console.log('pass');
"""


@node
def test_a_refused_upload_neither_hides_accepted_uploads_nor_strands_later_ones_and_retries_keep_their_folder():
    _run(_PARTIAL_BATCH)


_FOLDER_SNAPSHOT = r"""
// Two uploads come back ready after a reload, never attempted: no folder is filed for them yet.
let tab = await page();
tab.draft.setOwnerScope(ownerA); await tick();
const draftId = stored.get(`xperfect.files.draft.watch.worker-test.${ownerA}`);
for (const name of ['r1.txt', 'r2.txt']) {
  const upload_id = 'fil_' + receipts.size;
  receipts.set(upload_id, {upload_id, draft_id: draftId, name, relative_path: name, size_bytes: 2, state: 'ready',
    received_bytes: 2, sha256: 'verified', revision: 'r1'});
}
tab.draft.setOwnerScope(null); tab.draft.setOwnerScope(ownerA); await tick();
assert.deepEqual([...tab.draft.readyIds()].sort(), ['fil_0', 'fil_1']);

// Add while folder a is shown, then open folder b while the first request is still being sent.
await tab.files.open('a');
let release; holdNextAttach = new Promise((resolve) => { release = resolve; });
const adding = tab.handlers.attachPendingUploads();
await tick();
await tab.files.open('b');
release(); await adding; await tick();
assert.deepEqual([...bindings.values()].map((b) => [b.uploads[0], b.directory]).sort(), [['fil_0', 'a'], ['fil_1', 'a']]);
assert.deepEqual(tab.draft.readyIds(), []);
console.log('pass');
"""


@node
def test_every_upload_in_one_add_goes_to_the_folder_shown_when_adding_even_if_files_moves_on():
    _run(_FOLDER_SNAPSHOT)
