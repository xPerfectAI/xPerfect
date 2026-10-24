// Keep an unfinished request across sign-in and reload, within its authenticated owner.
export function createLaunchDraft({ fields, exactSelects = [], storage = () => window.sessionStorage }) {
  let owner = '';
  let request = null;
  let edited = false;
  const key = () => `xperfect.launch-draft.v1.${owner}`;
  const defaults = Object.fromEntries(
    Object.entries(fields).map(([id, field]) => [id, String(field?.value || '')]),
  );
  const values = () => Object.fromEntries(Object.entries(fields).map(([id, field]) => [id, field.value]));
  function restoreValue(id, field, value) {
    const defaultValue = field.dataset?.draftDefault ?? defaults[id];
    const candidate = value == null ? defaultValue : String(value);
    const options = Array.from(field?.options || []);
    if (value != null && candidate !== '' && exactSelects.includes(id) && field.options
        && !options.some(option => String(option.value) === candidate)) {
      const unavailable = field.ownerDocument.createElement('option');
      unavailable.value = candidate;
      unavailable.textContent = 'Selected option unavailable — choose another';
      unavailable.disabled = true;
      unavailable.dataset.unavailable = 'true';
      field.appendChild(unavailable);
      field.value = candidate;
      return;
    }
    if (!options.length) {
      field.value = candidate;
      return;
    }
    if (options.some(option => String(option.value) === candidate)) {
      field.value = candidate;
      return;
    }
    field.value = options.some(option => String(option.value) === defaultValue)
      ? defaultValue
      : String(options[0]?.value || '');
  }
  function save() {
    if (!owner) return;
    try { storage().setItem(key(), JSON.stringify({ version: 1, values: values(), request })); } catch { /* Draft storage is optional. */ }
  }
  for (const field of Object.values(fields)) {
    for (const event of ['input', 'change']) field.addEventListener(event, () => { edited = true; save(); });
  }
  return {
    peekValue(scope, id) {
      if (!/^[a-f0-9]{64}$/.test(String(scope || '')) || !fields[id]) return null;
      if (scope === owner || (!owner && edited)) return String(fields[id].value || '');
      try {
        const saved = JSON.parse(storage().getItem(`xperfect.launch-draft.v1.${scope}`) || 'null');
        return saved?.version === 1 && typeof saved.values?.[id] === 'string' ? saved.values[id] : null;
      } catch { return null; }
    },
    bindOwner(scope) {
      if (!/^[a-f0-9]{64}$/.test(String(scope || '')) || scope === owner) return;
      const preserveTyping = !owner && edited;
      owner = scope;
      request = null;
      let saved = null;
      try { saved = JSON.parse(storage().getItem(key()) || 'null'); } catch { /* Missing or invalid drafts are empty. */ }
      if (!preserveTyping) {
        for (const [id, field] of Object.entries(fields)) {
          const savedValue = saved?.version === 1 && typeof saved.values?.[id] === 'string'
            ? saved.values[id]
            : null;
          restoreValue(id, field, savedValue);
        }
        if (typeof saved?.request?.signature === 'string' && typeof saved.request.key === 'string') request = saved.request;
      }
      edited = false;
      save();
    },
    requestKey(payload, makeKey = () => globalThis.crypto.randomUUID()) {
      const signature = JSON.stringify(payload);
      if (request?.signature !== signature) request = { signature, key: makeKey() };
      save();
      return request.key;
    },
    markUnknown(payload, scope = owner) {
      if (!request || scope !== owner || (payload !== undefined && request.signature !== JSON.stringify(payload))) return false;
      request.unknown = true;
      save();
      return true;
    },
    unknownRequest(payload) { return request?.unknown === true && request.signature === JSON.stringify(payload); },
    confirmRequest(payload, scope) {
      if (scope !== owner || request?.signature !== JSON.stringify(payload)) return false;
      delete request.unknown;
      save();
      return true;
    },
    unknownPayload(scope) {
      if (scope !== owner || request?.unknown !== true) return null;
      try { return JSON.parse(request.signature); } catch { return null; }
    },
    restartUnknown(payload, scope) {
      if (scope !== owner || request?.unknown !== true || request.signature !== JSON.stringify(payload)) return false;
      request = null;
      save();
      return true;
    },
    clearRequest() { request = null; save(); },
    clear() {
      request = null;
      edited = false;
      for (const [id, field] of Object.entries(fields)) restoreValue(id, field, null);
      if (owner) { try { storage().removeItem(key()); } catch { /* Storage may be unavailable. */ } }
    },
  };
}
