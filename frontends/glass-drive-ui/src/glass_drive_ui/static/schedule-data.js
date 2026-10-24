// The plain message edits one field. Other application data remains unchanged.
export function parseRequestData(raw = '{}') {
  const value = parseScheduleJson(raw || '{}');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Extra details must be an object.');
  return value;
}

export function withRequestMessage(raw, message, originalMessage = '') {
  const value = parseRequestData(raw);
  if (message !== originalMessage) {
    if (message) Object.defineProperty(value, 'message', { value: message, enumerable: true, writable: true, configurable: true });
    else delete value.message;
  }
  return value;
}

export function requestDataRows(value) {
  const entries = Object.entries(value);
  if (entries.some(([, item]) => item !== null && typeof item === 'object')) return null;
  return entries.map(([key, item]) => ({ key, type: item === null ? 'null' : typeof item, value: item === null ? '' : String(item) }));
}

export function requestDataFromRows(rows) {
  const value = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (!key && !row.value) continue;
    if (!key) throw new Error('Give each extra detail a name.');
    if (Object.hasOwn(value, key)) throw new Error(`The detail “${key}” appears twice.`);
    let item = row.value;
    if (row.type === 'number') {
      if (!String(item).trim() || !Number.isFinite(Number(item))) throw new Error(`Enter a number for “${key}”.`);
      item = Number(item);
      if (Number.isInteger(item) && !Number.isSafeInteger(item)) throw new Error(`Use Edit as JSON for the very large number in “${key}”.`);
    } else if (row.type === 'boolean') {
      if (!['true', 'false'].includes(String(item))) throw new Error(`Choose true or false for “${key}”.`);
      item = String(item) === 'true';
    } else if (row.type === 'null') item = null;
    Object.defineProperty(value, key, { value: item, enumerable: true, writable: true, configurable: true });
  }
  return value;
}

// Use the browser's native source-aware JSON parser for integers that cannot fit
// in a JavaScript Number. Native raw JSON keeps their exact decimal digits.
export function parseScheduleJson(raw) {
  return JSON.parse(raw, (_key, value, context) => {
    if (typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value)) {
      if (!context?.source || typeof JSON.rawJSON !== 'function') throw new Error('This app has very large numbers. Open it in a current browser to keep them exact.');
      return JSON.rawJSON(context.source);
    }
    return value;
  });
}

export function hasPreciseNumbers(value) {
  if (typeof JSON.isRawJSON === 'function' && JSON.isRawJSON(value)) return true;
  return Boolean(value && typeof value === 'object' && Object.values(value).some(hasPreciseNumbers));
}

export function effectiveRunOutcome(item = {}) {
  // The receipt state owns progress. Error/outcome codes only describe it.
  return String(item.state || item.outcome || item.status || 'pending');
}

export function plainRunOutcome(outcome, webhook = true) {
  const labels = {
    accepted: 'Received by the app', http_accepted: 'Received by the app',
    pending: webhook ? 'Sending…' : 'Waiting to start…', delivering: 'Sending…',
    queued: 'Waiting to start…', running: 'Working…', scheduled: 'Ready to start',
    completed: 'Done', succeeded: 'Done', done: 'Done',
    unacknowledged: 'Receipt not confirmed — sending again may repeat this action',
    rejected: 'Couldn’t send — check the connection', delivery_expired: 'Delivery time passed', expired: 'Delivery time passed',
    superseded: 'Replaced by your changes', cancelled: 'Stopped',
    failed: 'Couldn’t finish — help needed', action_required: 'Help needed', missed: 'Missed while unavailable',
  };
  return labels[outcome] || String(outcome || '').replaceAll('_', ' ');
}
