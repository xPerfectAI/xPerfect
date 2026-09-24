"""Watch first drop: files dropped before Files has loaded are kept, only for the
viewer's own verified account scope. Runs the real watch.js functions under Node
with a files.js-shaped fake (adapted from the independent review harness)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WATCH_JS = Path(__file__).resolve().parents[1] / "src/glass_drive_ui/static/watch.js"

_HARNESS = r"""
let filesCanWrite; let watchDraftScopeLoad = 0; let watchDraftScopeController = null;
const errors = []; const added = []; const fileError = (m) => errors.push(m);
const filesPanel = { hidden: true }; const filesToggle = { setAttribute() {} }; const filesUploadArea = { hidden: true };
const filesStatus = { textContent: '', hidden: true };
let serverScope = 'a'.repeat(64); let filesMode = 'owner'; let filesDelay = 5;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const watchDraft = (() => { let scope = null; return {
  ownerScope: () => scope, setOwnerScope: (v) => { scope = typeof v === 'string' && /^[a-f0-9]{64}$/.test(v) ? v : null; },
  setBusy() {}, addFiles: (list) => added.push(list.map((e) => e.relativePath)) }; })();
const setWatchDraftBusy = (busy) => watchDraft.setBusy(busy);
globalThis.fetch = async () => { await sleep(5); return { ok: true, json: async () => ({ draft_owner_scope: serverScope }) }; };
// Like files.js open(): clears status, superseded loads return silently, failures
// write the reason into the Files status and call onAccess(false), never reject.
let listLoadSequence = 0;
const workspaceFiles = { async open() { const sequence = ++listLoadSequence; filesStatus.textContent = ''; try {
  await sleep(filesDelay); if (filesMode === 'fail') throw new Error('Files are unavailable (503).');
  if (sequence !== listLoadSequence) return; onAccess(filesMode === 'owner');
} catch (error) { if (sequence === listLoadSequence) { filesStatus.textContent = error.message; onAccess(false); } } } };
__WATCH__
const onAccess = (canWrite) => {__ONACCESS__};
function fileEntry(name) { return { isFile: true, isDirectory: false, name, file: (ok) => setTimeout(() => ok({ name, size: 1 }), 2) }; }
function dirEntry(name, children) { let batches = [children.slice(0, 2), children.slice(2), []]; return { isFile: false, isDirectory: true, name,
  createReader: () => ({ readEntries: (ok) => setTimeout(() => ok(batches.shift() || []), 3) }) }; }
function transfer(entries) { const items = entries.map((entry) => ({ kind: 'file', webkitGetAsEntry: () => entry,
  getAsFile: () => (entry.isFile ? { name: entry.name, size: 1 } : null) })); return { items, files: [], clear() { items.length = 0; } }; }
// The DataTransfer empties as soon as the drop event returns.
function drop(entries) { const t = transfer(entries); queueWatchDrop(t); t.clear(); return watchDropQueue; }
const scenario = process.argv[2];
(async () => {
  if (scenario === 'owner') { await drop([fileEntry('a.txt'), dirEntry('dir', [fileEntry('x'), fileEntry('y'), dirEntry('sub', [fileEntry('z')])])]); }
  if (scenario === 'viewer') { filesMode = 'viewer'; await drop([fileEntry('a.txt')]); }
  if (scenario === 'scope-change') { const p = drop([dirEntry('dir', [fileEntry('x'), fileEntry('y'), fileEntry('w')])]);
    await sleep(22); watchDraft.setOwnerScope('b'.repeat(64)); await p; }
  if (scenario === 'listing-fails') { filesMode = 'fail'; await drop([fileEntry('a.txt')]); }
  if (scenario === 'two-drops') { filesDelay = 30; drop([fileEntry('first.txt')]); await sleep(5);
    await drop([fileEntry('second.txt')]); }
  console.log(JSON.stringify({ errors, added }));
})();
"""


def _extract(source: str, start: str) -> str:
    index = source.index(start)
    return source[index:source.index("\n}\n", index) + 2]


def _run(tmp_path, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = WATCH_JS.read_text()
    functions = "\n".join(_extract(source, marker) for marker in (
        "async function settledWatchDraftScope", "function captureDroppedFiles",
        "function queueWatchDrop", "async function addWatchDrop", "async function activateWatchDraft"))
    state = "\n".join(re.search(pattern, source, re.M).group(0) for pattern in (
        r"^let watchDraftActivation = Promise\.resolve\(\);$", r"^let watchDropQueue = Promise\.resolve\(\);$"))
    on_access = re.search(r"onAccess: \(canWrite\) => \{(.*?)\n  \},", source, re.S).group(1)
    script = tmp_path / f"watch-{scenario}.mjs"
    script.write_text(_HARNESS.replace("__WATCH__", state + "\n" + functions).replace("__ONACCESS__", on_access))
    result = subprocess.run([node, str(script), scenario], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_an_owners_first_drop_keeps_files_and_folders_once(tmp_path):
    assert _run(tmp_path, "owner") == {"errors": [], "added": [["a.txt", "dir/x", "dir/y", "dir/sub/z"]]}


def test_a_viewer_drop_is_refused_with_the_viewer_wording(tmp_path):
    outcome = _run(tmp_path, "viewer")
    assert outcome["added"] == [] and any("workspace member" in e for e in outcome["errors"])


def test_a_failed_listing_is_not_reported_as_viewer_denial(tmp_path):
    outcome = _run(tmp_path, "listing-fails")
    assert outcome["added"] == []
    assert any("could not load" in e and "503" in e for e in outcome["errors"]), outcome
    assert not any("workspace member" in e for e in outcome["errors"]), outcome


def test_an_account_change_during_a_folder_walk_is_refused(tmp_path):
    outcome = _run(tmp_path, "scope-change")
    assert outcome["added"] == [] and any("account changed" in e for e in outcome["errors"])


def test_two_quick_drops_are_both_kept_in_order(tmp_path):
    outcome = _run(tmp_path, "two-drops")
    assert outcome == {"errors": [], "added": [["first.txt"], ["second.txt"]]}, outcome
