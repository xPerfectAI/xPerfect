/* Native details own state; native popovers escape scrolling panels and dialogs. */
const openMenus = new Set();
let listening = false;
export function attachActionMenu(details, trigger, panel, { menu = true, signal } = {}) {
  if (signal?.aborted) return () => {};
  let openingIndex = 0;
  const topLayer = typeof panel.showPopover === 'function';
  panel.classList.add('action-menu-panel');
  if (topLayer) panel.setAttribute('popover', 'auto');
  if (menu) { trigger.setAttribute('aria-haspopup', 'menu'); panel.setAttribute('role', 'menu'); }
  const items = () => [...panel.querySelectorAll('button:not(:disabled), a[href]')];
  const position = () => {
    if (!details.open || !details.isConnected) { close(); return; }
    const anchor = trigger.getBoundingClientRect();
    const below = innerHeight - anchor.bottom - 12, above = anchor.top - 12;
    const up = below < Math.min(panel.scrollHeight, 340) && above > below;
    panel.style.maxHeight = `${Math.max(48, Math.min(420, up ? above : below))}px`;
    panel.style.maxWidth = `${Math.max(160, innerWidth - 24)}px`;
    panel.style.left = `${Math.max(12, Math.min(anchor.right - panel.offsetWidth, innerWidth - panel.offsetWidth - 12))}px`;
    panel.style.top = `${Math.max(12, up ? anchor.top - panel.offsetHeight - 6 : anchor.bottom + 6)}px`;
  };
  const close = (focus = false) => {
    details.open = false; openMenus.delete(api); trigger.setAttribute('aria-expanded', 'false');
    if (topLayer && panel.matches(':popover-open')) panel.hidePopover();
    if (focus && trigger.isConnected) trigger.focus();
  };
  const api = { details, panel, close, position };
  const options = signal ? { signal } : undefined;
  signal?.addEventListener('abort', () => close(), { once: true });
  trigger.setAttribute('aria-expanded', 'false');
  details.addEventListener('toggle', () => {
    trigger.setAttribute('aria-expanded', String(details.open));
    if (!details.open) { close(); return; }
    for (const other of openMenus) if (other !== api) other.close();
    openMenus.add(api);
    if (topLayer && !panel.matches(':popover-open')) panel.showPopover();
    position();
    if (menu) {
      for (const item of items()) { item.setAttribute('role', 'menuitem'); item.tabIndex = -1; }
      items()[openingIndex]?.focus(); openingIndex = 0;
    }
  }, options);
  panel.addEventListener('toggle', event => { if (event.newState === 'closed' && details.open) close(); }, options);
  details.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); return; }
    if (event.key === 'Tab' && menu) { close(true); return; }
    if (!menu || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault(); const buttons = items(), index = buttons.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : index < 0
      ? (event.key === 'ArrowUp' ? buttons.length - 1 : 0) : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
    if (!details.open) { openingIndex = next; details.open = true; }
    buttons[next]?.focus();
  }, options);
  panel.addEventListener('click', event => {
    const action = event.target.closest('button, a[href]');
    if (action && !action.dataset.menuKeepOpen) close();
    else if (action) position();
  }, options);
  if (!listening) {
    listening = true;
    document.addEventListener('pointerdown', event => { for (const open of openMenus) if (!open.details.contains(event.target)) open.close(); });
    document.addEventListener('focusin', event => { for (const open of openMenus) if (!open.details.contains(event.target)) open.close(); });
    window.addEventListener('resize', () => { for (const open of openMenus) open.position(); });
    document.addEventListener('scroll', () => { for (const open of openMenus) open.position(); }, true);
  }
  return () => close();
}
