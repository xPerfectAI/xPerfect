export function scheduleEditorType(schedule = {}) {
  const recurrenceType = String(schedule.recurrence_type || 'daily');
  const rrule = String(schedule.rrule || '').trim().toUpperCase();
  if (recurrenceType === 'rfc5545' && rrule === 'FREQ=WEEKLY') return 'weekly';
  return recurrenceType;
}

export function recurrenceSubmissionPolicy(selectedType, { intervalSeconds, timezoneName, rrule } = {}) {
  const kind = String(selectedType || 'daily');
  if (kind === 'weekly') {
    return {
      recurrenceType: 'rfc5545',
      intervalSeconds: null,
      timezoneName: String(timezoneName || 'UTC'),
      rrule: 'FREQ=WEEKLY',
    };
  }
  return {
    recurrenceType: kind,
    intervalSeconds: kind === 'interval' ? Number(intervalSeconds || 0) : null,
    timezoneName: kind === 'interval' ? 'UTC' : String(timezoneName || 'UTC'),
    rrule: kind === 'rfc5545' ? String(rrule || '').trim() : '',
  };
}

// Edits use the existing PATCH contract. Keep untouched definition fields out of
// the request so the server preserves exact stored values (including timestamp
// precision and interval seconds) instead of round-tripping through form inputs.
export function scheduleEditUpdates({ original = {}, initial = {}, current = {}, payload = {} } = {}) {
  const changed = (field) => String(current[field] ?? '') !== String(initial[field] ?? '');
  const updates = {};
  const put = (key, value) => {
    if (value !== undefined) updates[key] = value;
  };

  if (changed('instruction')) put('instruction', payload.instruction);

  const recurrenceShapeChanged = [
    'type',
    'customType',
    'intervalValue',
    'intervalUnit',
    'localTime',
    'timezone',
    'startsAt',
    'weeklyDay',
    'weeklyTime',
    'cron',
    'rrule',
  ].some(changed);
  if (recurrenceShapeChanged) {
    const raw = (key, ...fields) => (fields.some(changed) ? payload[key] : original[key]);
    const recurrenceTypeChanged = changed('type') || changed('customType');
    const selectedType = current.type === 'custom' ? current.customType : current.type;
    const weeklyAnchorChanged = selectedType === 'weekly' && [
      'type', 'customType', 'weeklyDay', 'weeklyTime', 'timezone',
    ].some(changed);
    put('recurrence_type', raw('recurrence_type', 'type', 'customType'));
    put('interval_seconds', raw('interval_seconds', 'intervalValue', 'intervalUnit'));
    put('local_time', raw('local_time', 'localTime'));
    put('timezone_name', raw('timezone_name', 'timezone'));
    if (recurrenceTypeChanged || changed('dstPolicy')) put('dst_policy', payload.dst_policy);
    put('cron_expression', raw('cron_expression', 'cron'));
    put('rrule', recurrenceTypeChanged ? payload.rrule : raw('rrule', 'rrule'));
    put('starts_at', weeklyAnchorChanged ? payload.starts_at : raw('starts_at', 'startsAt'));
    put('ends_at', changed('endsAt') ? payload.ends_at : original.ends_at);
    put('schedule_text', payload.schedule_text);
  } else {
    if (changed('dstPolicy')) put('dst_policy', payload.dst_policy);
    if (changed('endsAt')) put('ends_at', payload.ends_at);
  }

  if (changed('enabled')) put('enabled', payload.enabled);
  if (changed('overlapPolicy')) put('overlap_policy', payload.overlap_policy);
  if (changed('misfireGrace')) put('misfire_grace_seconds', payload.misfire_grace_seconds);
  if (changed('catchUpPolicy')) put('catch_up_policy', payload.catch_up_policy);
  if (changed('catchUpLimit')) put('max_catch_up_occurrences', payload.max_catch_up_occurrences);
  if (changed('jitter')) put('jitter_seconds', payload.jitter_seconds);
  return updates;
}

export function zonedDateTimeLocalValue(value, timezoneName = 'UTC') {
  const instant = new Date(String(value || ''));
  if (!Number.isFinite(instant.getTime())) return '';
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: String(timezoneName || 'UTC'),
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(instant);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    if (!values.year || !values.month || !values.day || !values.hour || !values.minute) return '';
    return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`;
  } catch (_error) {
    return '';
  }
}

const WEEKDAY_CODES = ['SU', 'MO', 'TU', 'WE', 'TH', 'FR', 'SA'];

function zonedParts(value, timezoneName) {
  const instant = value instanceof Date ? value : new Date(String(value || ''));
  if (!Number.isFinite(instant.getTime())) return null;
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: String(timezoneName || 'UTC'),
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      weekday: 'short',
      hourCycle: 'h23',
    }).formatToParts(instant);
    return Object.fromEntries(parts.map((part) => [part.type, part.value]));
  } catch (_error) {
    return null;
  }
}

function localDateTimeFromParts(parts, dayOffset = 0, localTime = '09:00') {
  if (!parts?.year || !parts.month || !parts.day || !/^\d{2}:\d{2}$/.test(String(localTime))) return '';
  const base = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day));
  if (!Number.isFinite(base)) return '';
  const shifted = new Date(base + (Number(dayOffset) || 0) * 86400000);
  const year = shifted.getUTCFullYear();
  const month = String(shifted.getUTCMonth() + 1).padStart(2, '0');
  const day = String(shifted.getUTCDate()).padStart(2, '0');
  return `${year}-${month}-${day}T${localTime}`;
}

export function weekdayAtInstant(value, timezoneName = 'UTC') {
  const parts = zonedParts(value, timezoneName);
  if (!parts?.weekday) return '';
  const weekday = String(parts.weekday).slice(0, 2).toUpperCase();
  return WEEKDAY_CODES.includes(weekday) ? weekday : '';
}

export function weeklyStartDateTimeLocal(
  weekday,
  localTime = '09:00',
  timezoneName = 'UTC',
  reference = new Date(),
) {
  const target = WEEKDAY_CODES.indexOf(String(weekday || '').toUpperCase());
  const parts = zonedParts(reference, timezoneName);
  if (target < 0 || !parts) return '';
  const today = WEEKDAY_CODES.indexOf(String(parts.weekday || '').slice(0, 2).toUpperCase());
  if (today < 0) return '';
  let offset = (target - today + 7) % 7;
  if (offset === 0 && /^\d{2}:\d{2}$/.test(String(localTime))) {
    const currentTime = `${parts.hour || '00'}:${parts.minute || '00'}`;
    if (String(localTime) <= currentTime) offset = 7;
  }
  return localDateTimeFromParts(parts, offset, localTime);
}

export function tomorrowDateTimeLocal(timezoneName = 'UTC', reference = new Date()) {
  const parts = zonedParts(reference, timezoneName);
  return localDateTimeFromParts(parts, 1, '09:00');
}
