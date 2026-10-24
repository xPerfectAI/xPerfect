// Display preferences only. No account, capability or worker state is stored here.
export function libraryPanelWidth(value, viewport, preferred = 840) {
  const maximum = Math.max(1, Math.min(1200, viewport - 24));
  const minimum = Math.min(360, maximum);
  const width = Number(value);
  return Math.round(Math.min(maximum, Math.max(minimum, Number.isFinite(width) && width > 0 ? width : preferred)));
}

export function attachLibraryPanel(panel, { dialog = false } = {}) {
  const key = `xperfect.display.library-width.${dialog ? 'picker' : 'watch'}`;
  let preferred = 840;
  try { preferred = Number(localStorage.getItem(key)) || preferred; } catch { /* Optional preference. */ }
  let frame = 0;
  let disposed = false;
  let drag = null;
  const handle = document.createElement('div');
  handle.className = 'library-resize-handle';
  handle.tabIndex = 0;
  handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-orientation', 'vertical');
  handle.setAttribute('aria-label', 'Resize Library');
  if (panel.id) handle.setAttribute('aria-controls', panel.id);
  handle.title = 'Drag to resize. Use arrow keys, or double-click to reset.';
  panel.prepend(handle);
  function apply() {
    if (disposed) return;
    const width = libraryPanelWidth(preferred, window.innerWidth);
    panel.style.width = `${width}px`;
    handle.setAttribute('aria-valuemin', String(libraryPanelWidth(360, window.innerWidth)));
    handle.setAttribute('aria-valuemax', String(Math.max(1, Math.min(1200, window.innerWidth - 24))));
    handle.setAttribute('aria-valuenow', String(width));
    handle.setAttribute('aria-valuetext', `${width} pixels wide`);
  }
  function persist() { if (disposed) return; try { localStorage.setItem(key, String(preferred)); } catch { /* Resizing still works. */ } }
  function stop(event) {
    if (disposed || !drag || (event.pointerId != null && event.pointerId !== drag.pointerId)) return;
    drag = null; panel.classList.remove('library-is-resizing');
    cancelAnimationFrame(frame); apply(); persist();
    if (handle.hasPointerCapture?.(event.pointerId)) handle.releasePointerCapture(event.pointerId);
  }
  handle.addEventListener('pointerdown', event => {
    if (disposed || event.button !== 0) return;
    drag = { x: event.clientX, width: panel.getBoundingClientRect().width, pointerId: event.pointerId };
    handle.setPointerCapture(event.pointerId); panel.classList.add('library-is-resizing'); event.preventDefault();
  });
  handle.addEventListener('pointermove', event => {
    if (disposed || !drag || event.pointerId !== drag.pointerId) return;
    preferred = libraryPanelWidth(drag.width + (drag.x - event.clientX) * (dialog ? 2 : 1), window.innerWidth);
    cancelAnimationFrame(frame); frame = requestAnimationFrame(apply);
  });
  handle.addEventListener('pointerup', stop);
  handle.addEventListener('pointercancel', stop);
  handle.addEventListener('lostpointercapture', stop);
  handle.addEventListener('dblclick', () => { if (disposed) return; preferred = 840; apply(); persist(); });
  handle.addEventListener('keydown', event => {
    if (disposed || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const step = event.shiftKey ? 80 : 20;
    preferred = event.key === 'Home' ? 360 : event.key === 'End' ? 1200
      : libraryPanelWidth(panel.getBoundingClientRect().width + (event.key === 'ArrowLeft' ? step : -step), window.innerWidth);
    apply(); persist(); event.preventDefault();
  });
  window.addEventListener('resize', apply); apply();
  return () => {
    if (disposed) return; disposed = true; cancelAnimationFrame(frame);
    const pointerId = drag?.pointerId; drag = null; panel.classList.remove('library-is-resizing');
    if (pointerId != null && handle.hasPointerCapture?.(pointerId)) handle.releasePointerCapture(pointerId);
    window.removeEventListener('resize', apply); handle.remove();
  };
}

const watchPanel = typeof document === 'undefined' ? null : document.getElementById('watch-files-panel');
if (watchPanel) attachLibraryPanel(watchPanel);
