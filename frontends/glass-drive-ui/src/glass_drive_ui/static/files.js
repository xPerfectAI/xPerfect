// Shared file transport for kickoff and Watch. File bytes are never kept in browser storage.
export const formatBytes = (bytes) => {
  const value = Math.max(0, Number(bytes) || 0);
  if (value < 1000) return `${value} B`;
  const unit = Math.min(4, Math.floor(Math.log(value) / Math.log(1000)));
  return `${(value / 1000 ** unit).toFixed(unit === 1 ? 1 : 2)} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`;
};

const stateLabel = (state) => ({ pending: 'Waiting', checking: 'Preparing', receiving: 'Uploading', ready: 'Ready', failed: 'Needs retry', cancelling: 'Removing', cleanup_failed: 'Could not remove', cancelled: 'Cancelled', expired: 'Expired' }[state] || state);

const key = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;

// Browser storage is optional. Keep retry keys and hints usable for this page
// when access is denied or storage is full; this fallback does not survive reload.
const transientFileHints = new Map();
const fileHints = {
  getItem(name) {
    if (transientFileHints.has(name)) return transientFileHints.get(name);
    try { return sessionStorage.getItem(name); } catch { return null; }
  },
  setItem(name, value) {
    transientFileHints.set(name, value);
    try { sessionStorage.setItem(name, value); } catch { /* Keep the page-local hint. */ }
  },
  removeItem(name) {
    // Remember removal even if persisted storage still contains the old value.
    transientFileHints.set(name, null);
    try { sessionStorage.removeItem(name); } catch { /* Do not revive the old hint in this page. */ }
  },
};

// A bounded-memory fingerprint anchors a reselected File to the original bytes.
// The server computes its own SHA-256 receipt for accepted content.
async function fileFingerprint(file, cancelled = () => false) {
  if (!globalThis.crypto?.subtle) throw new Error('Secure file verification is unavailable in this browser.');
  const chunkSize = 4 * 1024 * 1024;
  let digest = new Uint8Array();
  for (let offset = 0; offset < Math.max(1, file.size); offset += chunkSize) {
    if (cancelled()) return null;
    const chunk = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
    const input = new Uint8Array(digest.length + chunk.length);
    input.set(digest);
    input.set(chunk, digest.length);
    digest = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
  }
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function request(url, { method = 'GET', body, csrf = '', signal } = {}) {
  const response = await fetch(url, {
    method, credentials: 'same-origin', cache: 'no-store', signal,
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(csrf ? { 'X-GlassHive-CSRF': csrf } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { /* Server may return plain text. */ }
    throw new Error(typeof detail === 'object' ? (detail.message || detail.recovery || `Request failed (${response.status})`)
      : (detail || `Request failed (${response.status})`));
  }
  return response.status === 204 ? {} : response.json();
}

function uploadContent(id, file, csrf, progress, register) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    register(xhr);
    xhr.open('PUT', `/api/file-uploads/${encodeURIComponent(id)}/content`);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    if (csrf) xhr.setRequestHeader('X-GlassHive-CSRF', csrf);
    xhr.upload.onprogress = (event) => progress(event.loaded);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); } catch { reject(new Error('Upload receipt was invalid.')); }
      } else {
        let message = `Upload failed (${xhr.status}).`;
        try { const detail = JSON.parse(xhr.responseText).detail; message = typeof detail === 'object' ? detail.message || message : detail || message; } catch { /* Keep status. */ }
        reject(new Error(message));
      }
    };
    xhr.onerror = () => reject(new Error('Upload connection failed. Retry the file.'));
    xhr.onabort = () => reject(new Error('Upload cancelled.'));
    xhr.send(file);
  });
}

async function droppedFiles(transfer) {
  const items = Array.from(transfer?.items || []).filter((item) => item.kind === 'file');
  const fallback = Array.from(transfer?.files || []);
  const handles = items.map((item) => item.getAsFileSystemHandle?.()).filter(Boolean);
  const entries = items.map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  async function walkHandle(handle, prefix = '') {
    const path = `${prefix}${handle.name}`;
    if (handle.kind === 'file') return [{ file: await handle.getFile(), relativePath: path }];
    const result = [];
    for await (const child of handle.values()) result.push(...await walkHandle(child, `${path}/`));
    return result;
  }
  if (handles.length === items.length && handles.length) {
    const resolved = await Promise.allSettled(handles);
    if (resolved.every((result) => result.status === 'fulfilled' && result.value)) {
      const result = [];
      for (const { value: handle } of resolved) result.push(...await walkHandle(handle));
      if (result.length) return result;
    }
  }
  async function walkEntry(entry, prefix = '') {
    const path = `${prefix}${entry.name}`;
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      return [{ file, relativePath: path }];
    }
    const reader = entry.createReader();
    const result = [];
    while (true) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) result.push(...await walkEntry(child, `${path}/`));
    }
    return result;
  }
  if (entries.length) {
    const result = [];
    for (const entry of entries) result.push(...await walkEntry(entry));
    if (result.length) return result;
  }
  return fallback.map((file) => ({ file, relativePath: file.webkitRelativePath || file.name }));
}

const INTERNAL_FILE_TYPE = 'application/x-xperfect-workspace-file';

export function createFileDraft({ input, folderInput, drop, list, help, budget, status = null, addButton = null,
  clearAllButton = null, csrf, scope = 'launch', quietReady = false, dropOpensPicker = true,
  management = null, onChange = () => {}, onReady = null }) {
  let ownerScope = null;
  let ownerEpoch = 0;
  let ownerController = new AbortController();
  let storageKey = '';
  let draftId = '';
  let fingerprintKey = '';
  let dismissedKey = '';
  let fingerprints = {};
  const attached = new Set();
  const saveFingerprints = () => { if (ownerScope) fileHints.setItem(fingerprintKey, JSON.stringify(fingerprints)); };
  const draftRequest = (url, options = {}) => request(url, { ...options, signal: ownerController.signal });
  const items = [];
  let restoring = true;
  const pendingPicks = [];
  const transferQueue = [];
  let activeTransfers = 0;
  const parallelTransfers = 3;
  let limits = null;
  const selected = new Set();
  let mutating = 0;
  let organizing = false;
  let exporting = false;
  let moveDirectory = '';
  let moveIds = [];
  let moveReturnFocus = null;
  let replacing = null;
  let clearing = false;
  let busy = false;

  const usableLimit = (field) => limits && Number.isFinite(Number(limits[field])) && limits[field] !== null
    ? Number(limits[field]) : null;
  const visibleItems = () => items.filter((item) => !attached.has(item.upload_id));
  const readyIds = () => ownerScope ? visibleItems().filter((item) => item.state === 'ready').map((item) => item.upload_id) : [];
  const readyRefs = () => ownerScope ? visibleItems()
    .filter((item) => item.state === 'ready' && item.upload_id && item.revision)
    .map((item) => ({ upload_id: item.upload_id, revision: item.revision })) : [];
  const blocked = () => !ownerScope || restoring || mutating > 0 || organizing || clearing || busy || replacing !== null || visibleItems().some((item) =>
    item.state !== 'ready' || item.replacementTarget || !item.revision || (management && item.pathUncertain));
  const showHelp = (message) => { help.textContent = message; help.hidden = !message; };
  const showStatus = (message) => {
    if (!status) return;
    status.textContent = message;
    status.hidden = !message;
  };
  function syncManagement() {
    if (!management) return;
    for (const id of selected) {
      if (!visibleItems().some((item) => item.upload_id === id && item.state === 'ready' && item.revision)) selected.delete(id);
    }
    management.batch.hidden = !selected.size;
    management.selectedCount.textContent = `${selected.size} selected`;
    management.move.disabled = !selected.size || mutating > 0 || organizing || busy;
    management.exportLink.disabled = !selected.size || exporting || busy;
  }
  async function changePath(item, relativePath, allowBatch = false) {
    if (item.state !== 'ready' || !item.upload_id || !item.revision || mutating || (organizing && !allowBatch)) return false;
    if (relativePath === item.relative_path) return false;
    const epoch = ownerEpoch;
    mutating += 1; render();
    try {
      const receipt = await draftRequest(`/api/file-uploads/${encodeURIComponent(item.upload_id)}`, {
        method: 'PATCH', csrf: csrf(), body: { relative_path: relativePath, revision: item.revision },
      });
      if (epoch !== ownerEpoch) return false;
      if (receipt.state !== 'ready' || receipt.upload_id !== item.upload_id || !receipt.revision || receipt.relative_path !== relativePath) {
        throw new Error('The saved file path could not be confirmed.');
      }
      Object.assign(item, receipt, { error: '', pathUncertain: false });
      render();
      return true;
    } catch (error) {
      if (epoch !== ownerEpoch) return false;
      try {
        const current = await draftRequest(`/api/file-uploads/${encodeURIComponent(item.upload_id)}`);
        if (epoch !== ownerEpoch) return false;
        if (current.upload_id !== item.upload_id || current.state !== 'ready' || !current.revision) throw new Error('Receipt is incomplete.');
        Object.assign(item, current, { pathUncertain: false });
      } catch { item.pathUncertain = true; }
      item.error = `Could not organize file: ${error.message}${item.pathUncertain ? ' Reload to confirm the saved path.' : ` Saved path: ${item.relative_path}.`}`;
      render();
      return false;
    } finally { if (epoch === ownerEpoch) { mutating -= 1; render(); } }
  }
  function folderPaths() {
    const folders = new Set();
    for (const item of visibleItems().filter((entry) => entry.state === 'ready')) {
      const parts = (item.relative_path || item.name).split('/').slice(0, -1);
      for (let depth = 1; depth <= parts.length; depth += 1) folders.add(parts.slice(0, depth).join('/'));
    }
    return folders;
  }
  function renderMoveFolders() {
    if (!management) return;
    management.location.textContent = moveDirectory ? `/${moveDirectory}` : 'Draft root';
    management.parent.hidden = !moveDirectory;
    management.folders.replaceChildren();
    const prefix = moveDirectory ? `${moveDirectory}/` : '';
    const children = [...folderPaths()].filter((path) => path.startsWith(prefix)
      && !path.slice(prefix.length).includes('/'));
    for (const path of children) {
      const row = document.createElement('li');
      const open = document.createElement('button'); open.type = 'button';
      open.textContent = `📁 ${path.slice(prefix.length)}`;
      open.addEventListener('click', () => { moveDirectory = path; renderMoveFolders(); });
      row.append(open); management.folders.append(row);
    }
  }
  function pickMove(ids, focusTarget = document.activeElement) {
    if (!management || !ids.length || management.dialog.open) return;
    moveIds = [...ids];
    moveDirectory = '';
    moveReturnFocus = focusTarget;
    management.error.hidden = true;
    renderMoveFolders();
    management.dialog.showModal();
    management.cancel.focus();
  }
  function render() {
    list.replaceChildren();
    for (const item of visibleItems()) {
      const row = document.createElement('li');
      row.className = 'file-row';
      const progress = item.state === 'receiving' ? ` · ${formatBytes(item.received_bytes || 0)} of ${formatBytes(item.size_bytes)}` : '';
      const label = document.createElement('span');
      if (management && item.state === 'ready' && item.upload_id) {
        label.className = 'draft-file-entry';
        const choose = document.createElement('input'); choose.type = 'checkbox';
        choose.checked = selected.has(item.upload_id);
        choose.disabled = busy || !item.revision;
        choose.setAttribute('aria-label', `Select ${item.relative_path || item.name}`);
        choose.addEventListener('change', () => {
          if (choose.checked) selected.add(item.upload_id);
          else selected.delete(item.upload_id);
          syncManagement();
        });
        const download = document.createElement(item.revision ? 'a' : 'span');
        if (item.revision) {
          download.href = `/api/file-uploads/${encodeURIComponent(item.upload_id)}/content?${new URLSearchParams({ revision: item.revision })}`;
          download.download = item.name;
          download.draggable = false;
        }
        download.textContent = item.relative_path || item.name;
        const size = document.createElement('small'); size.textContent = formatBytes(item.size_bytes);
        label.append(choose, download, size);
      } else {
        label.textContent = `${item.relative_path || item.name} · ${formatBytes(item.size_bytes)} · ${stateLabel(item.state)}${progress}${item.error ? ` · ${item.error}` : ''}`;
      }
      const actions = document.createElement('span'); actions.className = 'file-row-actions';
      if (management && item.state === 'ready' && item.upload_id && item.revision) {
        const download = document.createElement('a'); download.className = 'file-choose';
        download.href = `/api/file-uploads/${encodeURIComponent(item.upload_id)}/content?${new URLSearchParams({ revision: item.revision })}`;
        download.download = item.name; download.draggable = false; download.textContent = 'Download';
        download.setAttribute('aria-label', `Download ${item.relative_path || item.name}`);
        actions.append(download);
      }
      if (item.state === 'receiving') {
        const meter = document.createElement('progress');
        meter.max = item.size_bytes || 1; meter.value = item.received_bytes || 0;
        meter.setAttribute('aria-label', `Uploading ${item.name}`);
        row.appendChild(meter);
      }
      if (item.state === 'failed' && item.file) {
        const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry'; retry.disabled = busy;
        retry.addEventListener('click', () => enqueue(item)); actions.appendChild(retry);
      }
      if (item.state === 'failed' && !item.file) {
        const hint = document.createElement('small'); hint.textContent = 'Reselect this file to retry.'; actions.appendChild(hint);
      }
      const replace = document.createElement('button'); replace.type = 'button'; replace.textContent = 'Replace';
      replace.setAttribute('aria-label', `Replace ${item.relative_path || item.name}`);
      replace.disabled = busy || clearing || ['cancelling', 'cleanup_failed'].includes(item.state);
      replace.addEventListener('click', () => {
        if (replace.disabled) return;
        replacing = item;
        input.multiple = false;
        input.value = '';
        input.click();
      });
      actions.appendChild(replace);
      if (management && item.state === 'ready' && item.upload_id && item.revision) {
        const directRemove = document.createElement('button'); directRemove.type = 'button'; directRemove.textContent = 'Remove';
        directRemove.disabled = busy;
        directRemove.setAttribute('aria-label', `Remove ${item.relative_path || item.name}`);
        directRemove.addEventListener('click', () => void discard(item, { announce: true, returnFocus: true }));
        actions.appendChild(directRemove);
        const menu = document.createElement('details'); menu.className = 'file-row-menu';
        const summary = document.createElement('summary'); summary.textContent = '⋯';
        summary.tabIndex = busy ? -1 : 0;
        summary.setAttribute('aria-disabled', String(busy));
        summary.setAttribute('aria-label', `More options for ${item.relative_path || item.name}`);
        const menuItems = document.createElement('div'); menuItems.className = 'file-row-menu-items';
        const rename = document.createElement('button'); rename.type = 'button'; rename.textContent = 'Rename'; rename.disabled = busy;
        rename.addEventListener('click', async () => {
          menu.open = false;
          const name = window.prompt('New name', item.name);
          if (name === null || name === item.name) return;
          const clean = name.trim();
          if (!clean || clean === '.' || clean === '..' || /[/\\]/.test(clean)) { showHelp('Choose a name without slashes.'); return; }
          const parent = (item.relative_path || item.name).split('/').slice(0, -1).join('/');
          await changePath(item, [parent, clean].filter(Boolean).join('/'));
        });
        const move = document.createElement('button'); move.type = 'button'; move.textContent = 'Move'; move.disabled = busy;
        move.addEventListener('click', () => { menu.open = false; pickMove([item.upload_id], summary); });
        menuItems.append(rename, move); menu.append(summary, menuItems); actions.append(menu);
      } else {
        const remove = document.createElement('button'); remove.type = 'button';
        remove.textContent = item.cleanupInFlight ? 'Removing…'
          : item.state === 'cancelling' || item.state === 'cleanup_failed' ? 'Retry remove'
          : ['checking', 'pending', 'receiving'].includes(item.state) ? 'Cancel' : 'Remove';
        remove.disabled = busy || Boolean(item.cleanupInFlight);
        remove.setAttribute('aria-label', `${remove.textContent} ${item.name}`);
        remove.addEventListener('click', () => void discard(item, { announce: true, returnFocus: true })); actions.appendChild(remove);
      }
      if (item.state === 'ready' && item.error) {
        const error = document.createElement('small'); error.setAttribute('role', 'alert');
        error.textContent = item.error; actions.append(error);
      }
      if (management && item.state === 'ready' && !item.revision) {
        const error = document.createElement('small'); error.setAttribute('role', 'alert');
        error.textContent = 'File receipt is incomplete. Reload to retry.'; actions.append(error);
      }
      row.append(label, actions); list.appendChild(row);
    }
    const visible = visibleItems();
    if (clearAllButton) {
      clearAllButton.hidden = !visible.length;
      clearAllButton.disabled = busy || clearing || replacing !== null;
      clearAllButton.setAttribute('aria-label', `Clear all ${visible.length} file${visible.length === 1 ? '' : 's'}`);
    } else if (visible.length) {
      const controls = document.createElement('li');
      controls.className = 'file-draft-actions';
      const clear = document.createElement('button'); clear.type = 'button'; clear.textContent = 'Clear all files';
      clear.disabled = busy || clearing || replacing !== null;
      clear.setAttribute('aria-label', 'Clear all files');
      clear.addEventListener('click', () => void clearAll());
      controls.append(clear);
      list.append(controls);
    }
    syncManagement();
    const ready = readyIds().length;
    showHelp(!quietReady && ready < visible.length ? `${ready} of ${visible.length} files ready` : '');
    if (budget && limits) {
      const available = usableLimit('available_bytes');
      const limit = usableLimit('limit_bytes');
      const kind = limits.native_hard_enforcement === true ? 'storage' : 'upload budget';
      const remaining = limit === null ? '' : available === null ? `Checking ${kind}…`
        : available <= limit * 0.1 ? `${formatBytes(available)} ${kind} left. Ask an admin for more space.`
          : `${formatBytes(available)} ${kind} available`;
      const perFile = usableLimit('max_file_bytes');
      budget.textContent = [remaining, perFile === null ? '' : `Max ${formatBytes(perFile)} per file`].filter(Boolean).join(' · ');
      budget.hidden = !budget.textContent;
    }
    onChange({ readyIds: readyIds(), blocked: blocked(), items });
  }
  async function refreshBudget() {
    if (!ownerScope) return;
    const epoch = ownerEpoch;
    try { const data = await draftRequest('/api/storage'); if (epoch === ownerEpoch) { limits = data; render(); } }
    catch (error) { if (epoch === ownerEpoch) { limits = null; if (budget) { budget.textContent = `Could not check storage: ${error.message}`; budget.hidden = false; } } }
  }
  function forget(item) {
    const index = items.indexOf(item);
    if (index < 0) return;
    items.splice(index, 1);
    selected.delete(item.upload_id);
    if (item.upload_id) { delete fingerprints[item.upload_id]; saveFingerprints(); }
    for (const replacement of items.filter((entry) => entry.replacementTarget === item)) {
      replacement.replacementTarget = null;
      if (replacement.replacementError) replacement.error = '';
      replacement.replacementError = false;
      if (replacement.state === 'ready' && !clearing && onReady && !replacement.onReadyStarted) {
        replacement.onReadyStarted = true;
        void onReady(replacement, ownerScope);
      }
    }
    render(); void refreshBudget();
  }
  async function discard(item, { announce = false, returnFocus = false } = {}) {
    if (!items.includes(item) || item.cleanupInFlight) return;
    const epoch = ownerEpoch;
    item.cancelRequested = true;
    if (item.queued) {
      const position = transferQueue.indexOf(item);
      if (position >= 0) transferQueue.splice(position, 1);
      item.queued = false;
    }
    item.cleanupInFlight = true;
    item.state = 'cancelling'; item.error = '';
    item.xhr?.abort(); render();
    if (announce) showStatus(`Removing ${item.name}…`);
    try {
      if (!item.upload_id && item.initPromise) {
        try { const receipt = await item.initPromise; item.upload_id = receipt.upload_id; }
        catch { /* Recover the same registration request below. */ }
      }
      if (epoch !== ownerEpoch) return;
      if (!item.upload_id && item.idempotency_key && item.initStarted) {
        const receipt = await draftRequest('/api/file-uploads', { method: 'POST', csrf: csrf(), body: {
          draft_id: draftId, name: item.name, relative_path: item.relative_path || item.name, size_bytes: item.size_bytes,
          idempotency_key: item.idempotency_key,
        } });
        item.upload_id = receipt.upload_id;
      }
      if (epoch !== ownerEpoch) return;
      if (!item.upload_id) {
        forget(item);
        if (announce) showStatus(`Removed ${item.name} from this draft.`);
        if (returnFocus) (clearAllButton && !clearAllButton.hidden ? clearAllButton : addButton)?.focus();
        return;
      }
      const path = `/api/file-uploads/${encodeURIComponent(item.upload_id)}`;
      let receipt = await draftRequest(path, { method: 'DELETE', csrf: csrf() });
      if (epoch !== ownerEpoch) return;
      if (receipt.state === 'cancelling') {
        for (let attempt = 0; attempt < 20 && receipt.state === 'cancelling'; attempt += 1) {
          await new Promise((resolve) => setTimeout(resolve, 500));
          if (epoch !== ownerEpoch) return;
          const data = await draftRequest(`/api/file-uploads?draft_id=${encodeURIComponent(draftId)}`);
          if (epoch !== ownerEpoch) return;
          receipt = (data.items || []).find((entry) => entry.upload_id === item.upload_id) || receipt;
        }
      }
      if (receipt.state !== 'cancelled' && receipt.state !== 'expired') {
        throw new Error('Cancellation is still in progress. Retry remove to confirm cleanup.');
      }
      forget(item);
      if (announce) showStatus(`Removed ${item.name} from this draft.`);
      if (returnFocus) (clearAllButton && !clearAllButton.hidden ? clearAllButton : addButton)?.focus();
    } catch (error) {
      if (epoch !== ownerEpoch) return;
      item.state = 'cleanup_failed';
      item.error = `Cleanup not confirmed: ${error.message}`;
      render();
      if (announce) showStatus(`Could not confirm removal of ${item.name}. It is still listed; retry remove.`);
    } finally {
      item.cleanupInFlight = false;
      if (epoch === ownerEpoch && items.includes(item)) {
        render();
        if (returnFocus) [...list.querySelectorAll('button')]
          .find((button) => button.getAttribute('aria-label') === `Retry remove ${item.name}`)?.focus();
      }
    }
  }
  async function replaceItem(item, file) {
    if (!items.includes(item) || !ownerScope || busy || !file) return;
    const parent = (item.relative_path || item.name).split('/').slice(0, -1).join('/');
    const relativePath = [parent, file.name || item.name].filter(Boolean).join('/');
    addFiles([{ file, relativePath }], item);
  }
  async function clearAll() {
    if (clearing || busy || !ownerScope) return;
    const epoch = ownerEpoch;
    const count = visibleItems().length;
    clearing = true;
    render();
    showStatus(`Removing ${count} file${count === 1 ? '' : 's'} from this draft…`);
    try {
      for (const item of [...visibleItems()]) {
        await discard(item);
        if (epoch !== ownerEpoch) return;
      }
    } finally {
      if (epoch === ownerEpoch) {
        clearing = false;
        render();
        const left = visibleItems().length;
        showStatus(left ? `${left} file${left === 1 ? '' : 's'} could not be removed. Retry remove on each remaining file.`
          : 'All files removed from this draft.');
        addButton?.focus();
      }
    }
  }
  clearAllButton?.addEventListener('click', () => void clearAll());
  async function finishReplacement(item, epoch) {
    const target = item.replacementTarget;
    if (!target) return true;
    if (epoch !== ownerEpoch) return false;
    if (!items.includes(target)) {
      item.replacementTarget = null;
      return true;
    }
    await discard(target);
    if (epoch !== ownerEpoch) return false;
    if (items.includes(target)) {
      item.replacementError = true;
      item.error = 'New file is ready. The old file was kept; retry removing it to finish replacement.';
      showStatus(`The new ${item.name} is ready, but the original remains. Retry removing the old file.`);
      render();
      return false;
    }
    showStatus(target.name === item.name ? `Replaced ${item.name}.` : `Replaced ${target.name} with ${item.name}.`);
    return true;
  }
  async function start(item) {
    if (!item.file || item.cancelRequested || item.running) return;
    const epoch = ownerEpoch;
    item.running = true;
    item.state = 'checking'; item.error = ''; render();
    try {
      const fingerprint = item.verifiedFile === item.file ? item.fingerprint
        : await fileFingerprint(item.file, () => item.cancelRequested || epoch !== ownerEpoch);
      if (item.cancelRequested || epoch !== ownerEpoch) return;
      if (item.fingerprint && fingerprint !== item.fingerprint) {
        item.file = null;
        throw new Error('Reselected file content differs from the original. Remove it and add the new file separately.');
      }
      item.fingerprint = fingerprint;
      item.verifiedFile = item.file;
      item.state = 'pending'; render();
      if (!item.upload_id) {
        item.idempotency_key ||= key();
        item.initStarted = true;
        item.initPromise = draftRequest('/api/file-uploads', { method: 'POST', csrf: csrf(), body: {
          draft_id: draftId, name: item.name, relative_path: item.relative_path || item.name, size_bytes: item.size_bytes,
          idempotency_key: item.idempotency_key,
        } });
        const receipt = await item.initPromise;
        if (epoch !== ownerEpoch) return;
        item.upload_id = receipt.upload_id;
        fingerprints[item.upload_id] = fingerprint; saveFingerprints();
        if (item.cancelRequested) return;
        if (receipt.state === 'ready') {
          Object.assign(item, receipt, { file: null }); render();
          if (!(await finishReplacement(item, epoch))) return;
          if (onReady && epoch === ownerEpoch && !item.onReadyStarted) {
            item.onReadyStarted = true;
            await onReady(item, ownerScope);
          }
          return;
        }
        if (['cancelled', 'expired'].includes(receipt.state)) throw new Error('Upload expired or was cancelled. Remove it and add the file again.');
      }
      if (item.cancelRequested) return;
      item.state = 'receiving'; render();
      const complete = await uploadContent(item.upload_id, item.file, csrf(), (received) => {
        if (epoch === ownerEpoch) { item.received_bytes = received; render(); }
      }, (xhr) => { item.xhr = xhr; });
      if (item.cancelRequested || epoch !== ownerEpoch) return;
      if (complete.state !== 'ready' || Number(complete.size_bytes) !== item.file.size
          || Number(complete.received_bytes) !== item.file.size || !complete.sha256) {
        throw new Error('File content could not be verified. Retry this file.');
      }
      Object.assign(item, complete, { state: 'ready', received_bytes: item.size_bytes });
      item.file = null; item.xhr = null; render(); void refreshBudget();
      if (!(await finishReplacement(item, epoch))) return;
      if (onReady && epoch === ownerEpoch && !item.onReadyStarted) {
        item.onReadyStarted = true;
        await onReady(item, ownerScope);
      }
    } catch (error) {
      if (epoch === ownerEpoch && !item.cancelRequested) {
        item.state = 'failed'; item.error = error.message; render();
        if (!quietReady) showHelp(`${item.name}: ${error.message}`);
        if (item.replacementTarget) showStatus(`Could not replace ${item.replacementTarget.name}. The original is still here; retry the new file or remove it.`);
      }
    } finally { item.running = false; item.initPromise = null; }
  }
  function pump() {
    while (activeTransfers < parallelTransfers && transferQueue.length) {
      const item = transferQueue.shift();
      item.queued = false;
      if (item.cancelRequested || !items.includes(item)) continue;
      const epoch = ownerEpoch;
      activeTransfers += 1;
      void start(item).finally(() => { if (epoch === ownerEpoch) { activeTransfers -= 1; pump(); } });
    }
  }
  function enqueue(item) {
    if (item.queued || item.running || item.cancelRequested) return;
    item.state = 'queued'; item.queued = true;
    transferQueue.push(item); render(); pump();
  }
  function addFiles(files, replacementTarget = null) {
    if (!ownerScope) { showHelp('Wait for your account to load before adding files.'); return; }
    if (busy) { showHelp('Checking your account. Try again in a moment.'); return; }
    const picked = Array.from(files || []).map((entry) => entry.file ? entry : {
      file: entry, relativePath: entry.webkitRelativePath || entry.name,
    });
    if (restoring) {
      pendingPicks.push(...picked);
      showHelp('Restoring previous files before adding new files…');
      return;
    }
    if (picked.length) showStatus('');
    const maxCount = usableLimit('max_batch_files');
    const maxBatch = usableLimit('max_batch_bytes');
    const replacementSize = replacementTarget && items.includes(replacementTarget) ? replacementTarget.size_bytes : 0;
    const replacementCount = replacementTarget && items.includes(replacementTarget) ? 1 : 0;
    const currentSize = items.reduce((sum, item) => sum + item.size_bytes, 0) - replacementSize;
    const currentCount = items.length - replacementCount;
    if (maxCount !== null && currentCount + picked.length > maxCount) { showHelp(`This batch allows ${maxCount} files.`); return; }
    if (maxBatch !== null && currentSize + picked.reduce((sum, entry) => sum + entry.file.size, 0) > maxBatch) { showHelp(`This batch allows ${formatBytes(maxBatch)}.`); return; }
    let intakeError = '';
    for (const { file, relativePath } of picked) {
      const relative_path = String(relativePath || file.name).replace(/^\/+/, '');
      const maxFile = usableLimit('max_file_bytes');
      if (maxFile !== null && file.size > maxFile) { intakeError = `${file.name} exceeds the ${formatBytes(maxFile)} file limit.`; continue; }
      const removing = items.find((item) => ['cancelling', 'cleanup_failed'].includes(item.state)
        && item.relative_path === relative_path && item.size_bytes === file.size);
      if (removing) { intakeError = `Finish removing ${file.name} before selecting it again.`; continue; }
      const restored = items.find((item) => !item.file && item.state === 'failed'
        && item.relative_path === relative_path && item.size_bytes === file.size);
      if (restored) {
        if (!restored.fingerprint) { restored.error = 'Original fingerprint is unavailable. Remove this upload, then add the file again.'; render(); continue; }
        restored.file = file; restored.error = ''; enqueue(restored);
      }
      else {
        const item = { name: file.name, relative_path, size_bytes: file.size, state: 'pending', file,
          replacementTarget: replacementTarget && items.includes(replacementTarget) ? replacementTarget : null };
        if (item.replacementTarget) showStatus(`Adding a replacement for ${item.replacementTarget.name}. The original stays until the new file is ready.`);
        items.push(item); enqueue(item);
      }
    }
    render();
    if (intakeError) showHelp(intakeError);
  }
  input.addEventListener('change', () => {
    if (busy) { input.value = ''; return; }
    const files = Array.from(input.files || []);
    const target = replacing;
    replacing = null;
    input.value = '';
    input.multiple = true;
    if (target && files.length > 1) showHelp('Choose one file to replace this file.');
    else if (target && files[0]) void replaceItem(target, files[0]);
    else addFiles(files);
    render();
  });
  input.addEventListener('cancel', () => { replacing = null; input.multiple = true; render(); });
  folderInput?.addEventListener('change', () => {
    if (busy) { folderInput.value = ''; return; }
    addFiles(folderInput.files); folderInput.value = '';
  });
  if (management) {
    management.clear.addEventListener('click', () => { selected.clear(); render(); });
    management.move.addEventListener('click', () => pickMove([...selected]));
    management.exportLink.addEventListener('click', async () => {
      if (!selected.size || !ownerScope || exporting) return;
      const epoch = ownerEpoch;
      const receipts = [...selected].map((id) => items.find((item) => item.upload_id === id));
      if (receipts.some((item) => !item || item.state !== 'ready' || !item.revision)) return;
      const payload = { upload_ids: receipts.map((item) => item.upload_id), revisions: receipts.map((item) => item.revision) };
      exporting = true;
      management.exportLink.disabled = true;
      try {
        await draftRequest('/api/file-uploads/export/prepare', { method: 'POST', csrf: csrf(), body: payload });
        if (epoch !== ownerEpoch) return;
        const form = document.createElement('form');
        form.method = 'post';
        form.action = '/api/file-uploads/export';
        form.hidden = true;
        const field = (name, value) => {
          const input = document.createElement('input'); input.type = 'hidden';
          input.name = name; input.value = value; form.append(input);
        };
        field('draft_export_csrf', csrf());
        payload.upload_ids.forEach((id) => field('upload_ids', id));
        payload.revisions.forEach((revision) => field('revisions', revision));
        document.body.append(form);
        form.submit();
        setTimeout(() => form.remove(), 30000);
      } catch (error) { if (epoch === ownerEpoch) showHelp(`Could not download selected files: ${error.message}`); }
      finally { if (epoch === ownerEpoch) { exporting = false; syncManagement(); } }
    });
    management.parent.addEventListener('click', () => {
      moveDirectory = moveDirectory.split('/').slice(0, -1).join('/');
      renderMoveFolders();
    });
    management.newFolder.addEventListener('click', () => {
      const name = window.prompt('New folder name');
      if (name === null) return;
      const clean = name.trim();
      if (!clean || clean === '.' || clean === '..' || /[/\\]/.test(clean)) {
        management.error.textContent = 'Choose a folder name without slashes.';
        management.error.hidden = false;
        return;
      }
      moveDirectory = [moveDirectory, clean].filter(Boolean).join('/');
      management.error.hidden = true;
      renderMoveFolders();
    });
    management.cancel.addEventListener('click', () => management.dialog.close());
    management.dialog.addEventListener('close', () => {
      if (moveReturnFocus?.isConnected) moveReturnFocus.focus();
      else management.move.focus();
      moveIds = [];
    });
    management.confirm.addEventListener('click', async () => {
      if (organizing || !moveIds.length) return;
      const ids = [...moveIds];
      const epoch = ownerEpoch;
      organizing = true;
      management.confirm.disabled = true;
      management.error.hidden = true;
      render();
      const failures = [];
      let changed = 0;
      try {
        for (const id of ids) {
          const item = items.find((entry) => entry.upload_id === id && entry.state === 'ready');
          if (!item) { failures.push('A selected file is no longer ready.'); continue; }
          const destination = [moveDirectory, item.name].filter(Boolean).join('/');
          if (destination === item.relative_path) continue;
          if (await changePath(item, destination, true)) { selected.delete(id); changed += 1; }
          else failures.push(`${item.name}: ${item.error || 'Move could not be confirmed.'}`);
          if (epoch !== ownerEpoch) return;
        }
      } finally {
        if (epoch === ownerEpoch) {
          organizing = false;
          management.confirm.disabled = false;
          render();
        }
      }
      if (epoch !== ownerEpoch) return;
      if (failures.length) {
        management.error.textContent = failures.join(' ');
        management.error.hidden = false;
      } else {
        management.dialog.close();
        showHelp(changed ? `${changed} file${changed === 1 ? '' : 's'} moved.` : 'Already in this folder.');
      }
    });
  }
  if (dropOpensPicker) {
    drop.addEventListener('click', () => { if (!busy) input.click(); });
    drop.addEventListener('keydown', (event) => { if (!busy && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); input.click(); } });
  }
  for (const type of ['dragenter', 'dragover']) drop.addEventListener(type, (event) => {
    if (busy) return;
    if (Array.from(event.dataTransfer?.types || []).includes(INTERNAL_FILE_TYPE)) return;
    if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
    event.preventDefault(); drop.classList.add('is-dragging');
  });
  drop.addEventListener('dragleave', () => drop.classList.remove('is-dragging'));
  drop.addEventListener('drop', (event) => {
    if (busy) return;
    if (Array.from(event.dataTransfer?.types || []).includes(INTERNAL_FILE_TYPE)) return;
    event.preventDefault(); drop.classList.remove('is-dragging');
    void addDrop(event.dataTransfer);
  });
  async function addDrop(transfer) {
    const epoch = ownerEpoch;
    try { const picked = await droppedFiles(transfer); if (epoch === ownerEpoch) addFiles(picked); }
    catch (error) { if (epoch === ownerEpoch) showHelp(`Could not read dropped folder: ${error.message}`); }
  }
  async function restore(epoch) {
    let restored = false;
    try {
      const data = await draftRequest(`/api/file-uploads?draft_id=${encodeURIComponent(draftId)}`);
      if (epoch !== ownerEpoch) return;
      for (const receipt of data.items || []) {
        if (receipt.state !== 'cancelled' && !items.some((item) => item.upload_id === receipt.upload_id)) {
          items.push({ ...receipt, relative_path: receipt.relative_path || receipt.name,
            file: null, fingerprint: fingerprints[receipt.upload_id] || '',
            ...(receipt.state === 'ready' ? {} : receipt.state === 'cancelling' ? {
              error: 'Cancellation is in progress. Retry remove to confirm cleanup.',
            } : {
              state: 'failed', error: 'Browser file bytes are unavailable after reload. Reselect to retry.',
            }),
          });
        }
      }
      restored = true;
    } catch (error) { if (epoch !== ownerEpoch) return; showHelp(`Could not restore selected files: ${error.message}. Reload to retry.`); }
    if (restored) {
      restoring = false; render();
      if (pendingPicks.length) addFiles(pendingPicks.splice(0));
    }
    void refreshBudget();
  }
  function setOwnerScope(value) {
    const next = typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : null;
    if (next === ownerScope) {
      if (!next && value !== null) showHelp('Your account could not be verified. Refresh to add files.');
      return;
    }
    ownerEpoch += 1;
    ownerController.abort();
    ownerController = new AbortController();
    for (const item of items) item.xhr?.abort();
    items.length = 0;
    pendingPicks.length = 0;
    transferQueue.length = 0;
    activeTransfers = 0;
    mutating = 0;
    organizing = false;
    exporting = false;
    replacing = null;
    clearing = false;
    selected.clear();
    attached.clear();
    fingerprints = {};
    limits = null;
    ownerScope = next;
    storageKey = '';
    draftId = '';
    fingerprintKey = '';
    dismissedKey = '';
    input.value = '';
    if (folderInput) folderInput.value = '';
    if (management) {
      management.confirm.disabled = false;
      if (management.dialog.open) management.dialog.close();
    }
    showHelp('');
    showStatus('');
    if (budget) { budget.textContent = ''; budget.hidden = true; }
    restoring = true;
    if (ownerScope) {
      storageKey = `xperfect.files.draft.${scope}.${ownerScope}`;
      draftId = fileHints.getItem(storageKey) || key();
      fileHints.setItem(storageKey, draftId);
      fingerprintKey = `${storageKey}.fingerprints.${draftId}`;
      dismissedKey = `${storageKey}.attached.${draftId}`;
      try { fingerprints = JSON.parse(fileHints.getItem(fingerprintKey) || '{}'); } catch { fingerprints = {}; }
      try {
        const dismissed = JSON.parse(fileHints.getItem(dismissedKey) || '[]');
        if (Array.isArray(dismissed)) dismissed.forEach((id) => attached.add(id));
      } catch { /* A damaged local hint does not change server receipts. */ }
    }
    render();
    if (ownerScope) void restore(ownerEpoch);
    else if (value !== null) showHelp('Your account could not be verified. Refresh to add files.');
  }
  function setBusy(value) {
    busy = Boolean(value);
    input.disabled = busy;
    if (folderInput) folderInput.disabled = busy;
    if (management) {
      management.clear.disabled = busy;
      management.parent.disabled = busy;
      management.newFolder.disabled = busy;
      management.confirm.disabled = busy || organizing;
    }
    render();
  }
  render();
  return { readyIds, readyRefs, blocked, items, addFiles, addDrop, refreshBudget, setOwnerScope, setBusy, ownerScope: () => ownerScope, dismissReady(uploadId) {
    if (!ownerScope) return;
    if (!items.some((item) => item.upload_id === uploadId && item.state === 'ready')) return;
    attached.add(uploadId);
    fileHints.setItem(dismissedKey, JSON.stringify([...attached]));
    render();
  }, reset() {
    if (!ownerScope) return;
    fileHints.removeItem(storageKey);
    fileHints.removeItem(fingerprintKey);
    fileHints.removeItem(dismissedKey);
  } };
}

export function createWorkspaceFiles({ workerId, list, status, directoryLabel, more, folderButton, csrf,
  moveButton, exportLink, selectionStatus, selectVisible, clearSelection, batch,
  trashList, trashStatus, trashPanel, trashOpen, trashClose, optionsMenu,
  notice, noticeText, noticeUndo, noticeClose,
  moveDialog, moveLocation, moveParent, moveFolders, moveMore, moveError, moveConfirm, moveCancel, moveCancelBottom,
  canUpload = () => true, onAccess = () => {}, audienceLabel = null }) {
  const base = `/api/workspace/${encodeURIComponent(workerId)}`;
  let directory = '';
  let directoryDisplayName = '';
  let cursor = '';
  let listLoadSequence = 0;
  let canWrite = false;
  let listingError = '';
  let dragOut = false;
  const entries = [];
  const selected = new Map();
  let pendingUndo = null;
  let moveEntries = [];
  let moveDirectory = '';
  let moveCursor = '';
  let moveLoadSequence = 0;
  let moveReturnFocus = null;
  const url = (path) => `${base}${path}`;
  const setStatus = (message) => { status.textContent = message; status.hidden = !message; };
  function showNotice(message, undo = null) {
    pendingUndo = undo;
    noticeText.textContent = message;
    noticeUndo.hidden = !undo;
    notice.hidden = false;
  }
  function syncSelection(message = '') {
    const count = selected.size;
    batch.hidden = !count;
    moveButton.disabled = !count || !canWrite;
    exportLink.disabled = !count;
    selectionStatus.textContent = message || (count ? `${count} selected` : '');
  }
  async function mutate(path, method, body) {
    return request(url(path), { method, body, csrf: csrf() });
  }
  async function refreshTrash() {
    if (!canWrite || trashPanel.hidden) { trashList.replaceChildren(); trashStatus.textContent = ''; return; }
    try {
      const data = await request(url('/files/trash'));
      if (!canWrite || trashPanel.hidden) return;
      trashList.replaceChildren();
      for (const entry of data.items || []) {
        const row = document.createElement('li'); row.className = 'file-row';
        const label = document.createElement('span'); label.textContent = entry.path || entry.name;
        const restore = document.createElement('button'); restore.type = 'button'; restore.textContent = 'Undo delete';
        restore.setAttribute('aria-label', `Restore ${entry.path || entry.name}`);
        restore.addEventListener('click', async () => {
          restore.disabled = true;
          try {
            await mutate(`/files/${encodeURIComponent(entry.file_id)}/restore`, 'POST', { undo_id: entry.undo_id });
            await open(directory); await refreshTrash(); showNotice(`${entry.name} restored.`);
          } catch (error) { trashStatus.textContent = `${entry.path}: ${error.message}`; restore.disabled = false; }
        });
        row.append(label, restore); trashList.append(row);
      }
      trashStatus.textContent = (data.items || []).length ? '' : 'Nothing to restore.';
    } catch (error) { trashStatus.textContent = `Could not load deleted files: ${error.message}`; }
  }
  async function moveEntry(entry, targetDirectory) {
    const destination = [targetDirectory, entry.name].filter(Boolean).join('/');
    if (destination === entry.path) return false;
    await mutate(`/files/${encodeURIComponent(entry.file_id)}`, 'PATCH', {
      path: destination, revision: entry.revision,
    });
    selected.delete(entry.file_id);
    return true;
  }
  async function showMoveFolder(path = '', append = false) {
    const sequence = ++moveLoadSequence;
    if (!append) { moveDirectory = path; moveCursor = ''; moveFolders.replaceChildren(); }
    moveLocation.textContent = moveDirectory ? `/${moveDirectory}` : 'Workspace root';
    moveParent.hidden = !moveDirectory;
    moveMore.hidden = true;
    moveError.hidden = true;
    moveConfirm.disabled = true;
    try {
      const query = new URLSearchParams({ directory: moveDirectory });
      if (moveCursor) query.set('cursor', moveCursor);
      const data = await request(url(`/files?${query}`));
      if (sequence !== moveLoadSequence || !moveDialog.open) return;
      for (const folder of (data.items || []).filter((item) => item.is_dir)) {
        const row = document.createElement('li'); row.className = 'file-row';
        const button = document.createElement('button'); button.type = 'button';
        button.textContent = `📁 ${folder.name}`;
        button.addEventListener('click', () => void showMoveFolder(folder.path));
        row.append(button); moveFolders.append(row);
      }
      moveCursor = data.next_cursor || '';
      moveMore.hidden = !moveCursor;
      moveConfirm.disabled = false;
    } catch (error) {
      if (sequence !== moveLoadSequence) return;
      moveError.textContent = `Could not load folders: ${error.message}`;
      moveError.hidden = false;
    }
  }
  function pickMove(entriesToMove, focusTarget = document.activeElement) {
    if (!canWrite || !entriesToMove.length || moveDialog.open) return;
    moveEntries = [...entriesToMove];
    moveReturnFocus = focusTarget;
    moveDialog.showModal();
    moveCancel.focus();
    void showMoveFolder('');
  }
  async function confirmMove() {
    moveConfirm.disabled = true;
    const failures = [];
    let moved = 0;
    for (const entry of moveEntries) {
      try { if (await moveEntry(entry, moveDirectory)) moved += 1; }
      catch (error) { failures.push(`${entry.path}: ${error.message}`); }
    }
    directoryLabel.tabIndex = -1;
    moveReturnFocus = directoryLabel;
    moveDialog.close();
    await open(directory);
    if (failures.length) {
      const message = `${moved} moved; ${failures.length} failed. ${failures.join(' ')}`;
      if (selected.size) syncSelection(message);
      else setStatus(message);
    } else showNotice(moved ? `${moved} moved.` : 'Already in this folder.');
  }
  function internalDrag(event, entry) {
    if (!canWrite || event.target.closest('a, button, input')) { event.preventDefault(); return; }
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData(INTERNAL_FILE_TYPE, JSON.stringify({
      file_id: entry.file_id, path: entry.path, name: entry.name, revision: entry.revision,
    }));
  }
  async function internalDrop(event, targetDirectory) {
    if (!canWrite || !Array.from(event.dataTransfer?.types || []).includes(INTERNAL_FILE_TYPE)) return;
    event.preventDefault(); event.stopPropagation();
    try {
      const entry = JSON.parse(event.dataTransfer.getData(INTERNAL_FILE_TYPE));
      if (!entry.file_id || !entry.path || !entry.name || !entry.revision) throw new Error('File drag data is incomplete.');
      const moved = await moveEntry(entry, targetDirectory);
      await open(directory);
      showNotice(moved ? `${entry.name} moved.` : `${entry.name} is already in this folder.`);
    } catch (error) { setStatus(`Could not move file: ${error.message}`); }
  }
  function render() {
    list.replaceChildren();
    directoryLabel.textContent = !directory ? 'Workspace root'
      : directory === 'inputs' ? 'Attached files'
        : directoryDisplayName ? `Attached file: ${directoryDisplayName}` : `/${directory}`;
    folderButton.hidden = !canWrite;
    trashOpen.hidden = !canWrite;
    optionsMenu.hidden = !canWrite;
    if (!canWrite) trashPanel.hidden = true;
    more.hidden = !cursor;
    const back = document.createElement('li');
    if (directory) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = '← Parent folder';
      button.addEventListener('click', () => void open(directory.split('/').slice(0, -1).join('/')));
      back.append(button); list.append(back);
    }
    for (const entry of entries) {
      if (selected.has(entry.file_id)) selected.set(entry.file_id, entry);
      const visibleName = entry.path === 'inputs' ? 'Attached files' : entry.display_name || entry.name;
      const row = document.createElement('li'); row.className = 'file-row workspace-file-entry';
      const choose = document.createElement('input'); choose.type = 'checkbox';
      choose.checked = selected.has(entry.file_id);
      choose.setAttribute('aria-label', `Select ${visibleName}`);
      choose.addEventListener('change', () => {
        if (choose.checked) selected.set(entry.file_id, entry);
        else selected.delete(entry.file_id);
        syncSelection();
      });
      const label = entry.is_dir ? document.createElement('button') : document.createElement('a');
      label.className = 'file-entry-name';
      label.textContent = `${entry.display_name ? '📎 ' : entry.is_dir ? '📁 ' : ''}${visibleName}`;
      const meta = document.createElement('small'); meta.className = 'file-entry-meta';
      if (!entry.is_dir) meta.textContent = formatBytes(entry.size_bytes);
      if (entry.is_dir) {
        label.type = 'button';
        label.setAttribute('aria-label', `Open ${visibleName}`);
        label.addEventListener('click', () => void open(entry.path));
        row.addEventListener('dragover', (event) => {
          if (Array.from(event.dataTransfer?.types || []).includes(INTERNAL_FILE_TYPE)) event.preventDefault();
        });
        row.addEventListener('drop', (event) => void internalDrop(event, entry.path));
      } else {
        label.href = url(`/files/${encodeURIComponent(entry.file_id)}/content?${new URLSearchParams({ revision: entry.revision })}`);
        label.download = entry.name;
        label.draggable = dragOut;
        if (dragOut) {
          label.title = 'Drag a copy to your computer';
          label.addEventListener('dragstart', (event) => {
            event.stopPropagation();
            event.dataTransfer.effectAllowed = 'copy';
            const safeName = entry.name.replace(/[:\r\n]/g, '_');
            event.dataTransfer.setData('DownloadURL', `application/octet-stream:${safeName}:${label.href}`);
          });
        }
      }
      const nameWrap = document.createElement('span'); nameWrap.className = 'file-entry-text';
      nameWrap.append(label); if (meta.textContent) nameWrap.append(meta);
      row.append(choose, nameWrap);
      if (!entry.is_dir) {
        const download = document.createElement('a'); download.className = 'file-choose';
        download.href = label.href; download.download = entry.name; download.draggable = false;
        download.textContent = 'Download';
        download.setAttribute('aria-label', `Download ${entry.name}`);
        row.append(download);
      }
      if (canWrite) {
        const menu = document.createElement('details'); menu.className = 'file-row-menu';
        const summary = document.createElement('summary'); summary.textContent = '⋯';
        summary.setAttribute('aria-label', `More options for ${entry.name}`);
        const menuItems = document.createElement('div'); menuItems.className = 'file-row-menu-items';
        const rename = document.createElement('button'); rename.type = 'button'; rename.textContent = 'Rename';
        rename.addEventListener('click', async () => {
          menu.open = false;
          const next = window.prompt('New name', entry.name);
          if (next === null || next === entry.name) return;
          const name = next.trim();
          if (!name || name === '.' || name === '..' || /[/\\]/.test(name)) { setStatus('Choose a name without slashes.'); return; }
          const path = [entry.path.split('/').slice(0, -1).join('/'), name].filter(Boolean).join('/');
          try { await mutate(`/files/${encodeURIComponent(entry.file_id)}`, 'PATCH', { path, revision: entry.revision }); await open(directory); showNotice(`${entry.name} renamed.`); }
          catch (error) { setStatus(error.message); }
        });
        const move = document.createElement('button'); move.type = 'button'; move.textContent = 'Move';
        move.addEventListener('click', () => { menu.open = false; pickMove([entry], summary); });
        const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Delete';
        remove.setAttribute('aria-label', `Delete ${entry.name}`);
        remove.addEventListener('click', async () => {
          menu.open = false;
          try {
            const result = await mutate(`/files/${encodeURIComponent(entry.file_id)}`, 'DELETE', { revision: entry.revision });
            selected.delete(entry.file_id);
            await open(directory);
            showNotice(`${entry.name} deleted.`, result.undo_id ? { fileId: entry.file_id, undoId: result.undo_id, name: entry.name } : null);
          }
          catch (error) { setStatus(error.message); }
        });
        menuItems.append(rename, move, remove); menu.append(summary, menuItems); row.append(menu);
      }
      row.draggable = canWrite;
      row.addEventListener('dragstart', (event) => internalDrag(event, entry));
      list.append(row);
    }
    if (listingError) {
      const unavailable = document.createElement('li');
      unavailable.className = 'file-error';
      unavailable.textContent = 'Files are unavailable for this workspace view.';
      list.append(unavailable);
    } else if (!entries.length) {
      const empty = document.createElement('li');
      empty.className = 'file-empty';
      empty.textContent = directory ? 'This folder is empty.' : canWrite ? 'No files yet. Add files above.' : 'No files yet.';
      list.append(empty);
    }
    syncSelection();
  }
  async function open(path = '', append = false) {
    const sequence = ++listLoadSequence;
    if (!append) { directory = path; cursor = ''; entries.length = 0; }
    setStatus('');
    try {
      const query = new URLSearchParams({ directory });
      if (cursor) query.set('cursor', cursor);
      const data = await request(url(`/files?${query}`));
      if (sequence !== listLoadSequence) return;
      listingError = '';
      directoryDisplayName = data.directory_display_name || '';
      entries.push(...(data.items || []));
      cursor = data.next_cursor || '';
      canWrite = data.can_write === true && canUpload();
      onAccess(canWrite);
      if (audienceLabel) {
        const audience = { member: 'Available to this worker', workspace: 'Available to this workspace' }[data.scope?.kind];
        audienceLabel.textContent = audience || '';
        audienceLabel.hidden = !audience;
      }
      const platform = globalThis.navigator?.userAgentData?.platform || globalThis.navigator?.platform || '';
      const browser = globalThis.navigator?.userAgent || '';
      const target = /mac/i.test(platform) ? 'chromium_macos' : /win/i.test(platform) ? 'chromium_windows' : '';
      dragOut = data.drag_out_supported === true && (data.drag_out_targets || []).includes(target) && /Chrome|Edg/.test(browser);
      render();
      if (!trashPanel.hidden) void refreshTrash();
    } catch (error) {
      if (sequence === listLoadSequence) {
        canWrite = false;
        onAccess(false);
        listingError = entries.length ? '' : (error.message || 'Files are unavailable.');
        setStatus(error.message);
        render();
      }
    }
  }
  folderButton.addEventListener('click', async () => {
    optionsMenu.open = false;
    const name = window.prompt('New folder name');
    if (name === null || !name.trim()) return;
    const clean = name.trim();
    if (clean === '.' || clean === '..' || /[/\\]/.test(clean)) { setStatus('Choose a folder name without slashes.'); return; }
    const path = [directory, clean].filter(Boolean).join('/');
    try { await mutate('/directories', 'POST', { path }); await open(directory); showNotice('Folder created.'); }
    catch (error) { setStatus(error.message); }
  });
  trashOpen.addEventListener('click', () => {
    optionsMenu.open = false;
    if (!canWrite) return;
    trashPanel.hidden = false;
    void refreshTrash();
    trashClose.focus();
  });
  trashClose.addEventListener('click', () => { trashPanel.hidden = true; trashOpen.focus(); });
  noticeClose.addEventListener('click', () => { notice.hidden = true; pendingUndo = null; });
  noticeUndo.addEventListener('click', async () => {
    if (!pendingUndo) return;
    const target = pendingUndo;
    noticeUndo.disabled = true;
    try {
      await mutate(`/files/${encodeURIComponent(target.fileId)}/restore`, 'POST', { undo_id: target.undoId });
      await open(directory);
      if (!trashPanel.hidden) await refreshTrash();
      showNotice(`${target.name} restored.`);
    } catch (error) { setStatus(`Could not restore ${target.name}: ${error.message}`); }
    finally { noticeUndo.disabled = false; }
  });
  moveCancel.addEventListener('click', () => moveDialog.close());
  moveCancelBottom.addEventListener('click', () => moveDialog.close());
  moveDialog.addEventListener('close', () => {
    moveLoadSequence += 1;
    if (moveReturnFocus?.isConnected) moveReturnFocus.focus();
    else moveButton.focus();
    moveEntries = [];
  });
  moveParent.addEventListener('click', () => void showMoveFolder(moveDirectory.split('/').slice(0, -1).join('/')));
  moveMore.addEventListener('click', () => void showMoveFolder(moveDirectory, true));
  moveConfirm.addEventListener('click', () => void confirmMove());
  list.addEventListener('dragover', (event) => {
    if (canWrite && Array.from(event.dataTransfer?.types || []).includes(INTERNAL_FILE_TYPE)) event.preventDefault();
  });
  list.addEventListener('drop', (event) => void internalDrop(event, directory));
  selectVisible.addEventListener('click', () => {
    for (const entry of entries) selected.set(entry.file_id, entry);
    render();
  });
  clearSelection.addEventListener('click', () => { selected.clear(); render(); });
  exportLink.addEventListener('click', (event) => {
    event.preventDefault();
    if (!selected.size) return;
    const form = document.createElement('form');
    form.method = 'post'; form.action = url('/files/export'); form.hidden = true;
    const field = (name, value) => {
      const input = document.createElement('input'); input.type = 'hidden';
      input.name = name; input.value = value; form.append(input);
    };
    field('workspace_export_csrf', csrf());
    for (const fileId of selected.keys()) field('file_ids', fileId);
    document.body.append(form);
    form.submit();
    setTimeout(() => form.remove(), 30000);
  });
  moveButton.addEventListener('click', () => pickMove([...selected.values()]));
  more.addEventListener('click', () => void open(directory, true));
  return { open, refresh: () => open(directory), attach: async (uploadIds) => {
    if (!uploadIds.length) return;
    const attachKey = `xperfect.files.attach.${workerId}.${JSON.stringify([directory, uploadIds])}`;
    const idempotencyKey = fileHints.getItem(attachKey) || key();
    fileHints.setItem(attachKey, idempotencyKey);
    const data = await mutate('/files', 'POST', { upload_ids: uploadIds, idempotency_key: idempotencyKey, directory });
    fileHints.removeItem(attachKey);
    await open(directory);
    return data;
  } };
}
