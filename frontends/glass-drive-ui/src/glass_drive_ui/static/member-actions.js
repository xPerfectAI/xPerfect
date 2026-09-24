import { workspaceLifecycleControl } from './launch-policy.js';

export function renderMemberActions(container, member, { request, refresh, status }) {
  const controls = document.createElement('div');
  controls.className = 'member-actions';
  const route = `/api/worker/${encodeURIComponent(member.worker_id)}`;
  const state = String(member.state || '');
  const closed = ['terminating', 'termination_failed', 'terminated'].includes(state);
  const lifecycle = workspaceLifecycleControl(state);
  async function perform(button, action, payload) {
    button.disabled = true;
    status('Applying member action…');
    try {
      await request(`${route}/${action}`, 'POST', payload);
      await refresh();
      status('Member state refreshed.');
    } catch (error) { status(error.message); button.disabled = false; }
  }
  if (!lifecycle.hidden && !lifecycle.disabled) {
    const button = document.createElement('button');
    button.textContent = lifecycle.label; button.type = 'button'; button.disabled = lifecycle.disabled;
    button.addEventListener('click', () => perform(button, `action/${lifecycle.action}`));
    controls.append(button);
  }
  if (['queued','running','resuming'].includes(state)) {
    const interrupt = document.createElement('button');
    interrupt.textContent = 'Interrupt'; interrupt.type = 'button';
    interrupt.addEventListener('click', () => perform(interrupt, 'action/interrupt'));
    controls.append(interrupt);
  }
  const watch = document.createElement('a');
  watch.href = `/watch/${encodeURIComponent(member.worker_id)}`; watch.textContent = 'Open live view';
  controls.append(watch);
  if (!closed) {
    const close = document.createElement('details'); close.className = 'member-close';
    close.innerHTML = '<summary>More actions</summary><p>Close this worker permanently? Its current work stops.</p><button type="button">Close this worker</button>';
    close.querySelector('button').addEventListener('click', event => perform(event.currentTarget, 'action/terminate'));
    controls.append(close);
  }
  container.append(controls);
}
