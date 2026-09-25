// A launch the server accepted is durable before the page moves. Show an ordinary
// link to its workspace first, so a tab that does not follow script navigation still
// leaves the person one click from their work instead of a disabled "Starting".

export function watchHref(value, base) {
  if (typeof value !== 'string' || !value.trim()) return '';
  let url;
  try {
    url = new URL(value, base);
  } catch {
    return '';
  }
  return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
}

export function showLaunchedWorkspace(status, href, createLink) {
  const link = createLink();
  link.href = href;
  link.className = 'card-action';
  link.textContent = 'Open workspace';
  status.replaceChildren('Project started. ', link);
  return link;
}
