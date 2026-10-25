/* The app supplies an instance suffix, never a session or credential, in its HTML. */
export function authCookieName(name) {
  const suffix = document.querySelector?.('meta[name="glasshive-auth-cookie-suffix"]')?.content || '';
  return `${name}${suffix}`;
}

export function readAuthCookie(name) {
  const prefix = `${encodeURIComponent(authCookieName(name))}=`;
  const entry = document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith(prefix));
  try { return entry ? decodeURIComponent(entry.slice(prefix.length)) : ''; } catch { return ''; }
}
