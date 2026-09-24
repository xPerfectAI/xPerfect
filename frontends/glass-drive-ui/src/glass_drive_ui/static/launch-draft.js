// Keep an unfinished request across sign-in and reload, within its authenticated owner.
export function createLaunchDraft({ fields, storage = () => window.sessionStorage }) {
  let owner = '';
  let request = null;
  let edited = false;
  const key = () => `xperfect.launch-draft.v1.${owner}`;
  const defaults = Object.fromEntries(
    Object.entries(fields).map(([id, field]) => [id, String(field?.value || '')]),
  );
  const values = () => Object.fromEntries(Object.entries(fields).map(([id, field]) => [id, field.value]));
  function restoreValue(id, field, value) {
    const candidate = value == null ? defaults[id] : String(value);
    const options = Array.from(field?.options || []);
    if (!options.length) {
      field.value = candidate;
      return;
    }
    if (options.some(option => String(option.value) === candidate)) {
      field.value = candidate;
      return;
    }
    const defaultValue = defaults[id];
    field.value = options.some(option => String(option.value) === defaultValue)
      ? defaultValue
      : String(options[0]?.value || '');
  }
  function save() {
    if (!owner) return;
    try { storage().setItem(key(), JSON.stringify({ version: 1, values: values(), request })); } catch { /* Draft storage is optional. */ }
  }
  for (const field of Object.values(fields)) field.addEventListener('input', () => { edited = true; save(); });
  return {
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
    clearRequest() { request = null; save(); },
    clear() {
      request = null;
      edited = false;
      for (const [id, field] of Object.entries(fields)) restoreValue(id, field, null);
      if (owner) { try { storage().removeItem(key()); } catch { /* Storage may be unavailable. */ } }
    },
  };
}
