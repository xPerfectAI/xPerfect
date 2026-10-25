function node(tag, text = '') { const element = document.createElement(tag); element.textContent = text; return element; }

export function renderAdmissionHolds(container, holds, request, reload) {
  container.replaceChildren();
  for (const hold of holds.filter(item => item.active)) {
    const row = node('div'); row.className = 'member-note';
    const reason = hold.kind === 'spend' ? 'Spend settings paused new work' : hold.reason_code === 'transport_integrity' ? 'New work paused for a connection check' : 'New work paused';
    row.append(node('span', `${reason}. Active work continues. `));
    const resume = node('button', 'Resume'); resume.type = 'button';
    resume.addEventListener('click', async () => {
      resume.disabled = true;
      try {
        await request(`/api/admission-holds/${encodeURIComponent(hold.hold_id)}/resume`, 'POST', { expected_generation: hold.generation });
        await reload();
      } catch (error) { row.append(node('span', error.message || 'Settings changed. Reload before resuming.')); resume.disabled = false; }
    });
    row.append(resume); container.append(row);
  }
}

export function mountSpendControls(container, { projectId = '', request }) {
  const root = node('details'); root.className = 'allowed-ai-scope';
  root.append(node('summary', 'Work pauses and spend'));
  const body = node('div'); body.className = 'allowed-ai-scope-content'; root.append(body); container.append(root);
  const status = node('p'); status.className = 'member-note'; status.setAttribute('role', 'status'); body.append(status);
  const scopeLabel = node('label', 'Apply to '); const scope = node('select');
  scope.append(new Option('My account', 'owner'));
  if (projectId) scope.append(new Option('This project', 'project'));
  scopeLabel.append(scope); body.append(scopeLabel);
  const holds = node('div'); body.append(holds);
  const form = node('form'); form.className = 'allowed-ai-form'; body.append(form);
  let snapshot = null; let disposed = false; let generation = 0;
  const path = () => scope.value === 'project' ? `/api/projects/${encodeURIComponent(projectId)}/spend-policy` : '/api/spend-policy';
  async function load() {
    const current = ++generation; status.textContent = 'Loading settings…'; form.hidden = true; holds.replaceChildren();
    try {
      const [policy, list] = await Promise.all([request(path()), request('/api/admission-holds')]);
      if (disposed || current !== generation) return;
      snapshot = policy; draw();
      renderAdmissionHolds(holds, (list.items || []).filter(item => item.scope_type === scope.value && item.scope_id === (scope.value === 'project' ? projectId : '')), request, load);
      status.textContent = '';
    } catch (error) { if (!disposed && current === generation) { form.replaceChildren(); status.textContent = error.message || 'Settings unavailable. Reload to try again.'; } }
  }
  function draw() {
    form.replaceChildren(); form.hidden = false;
    const targetPath = path(); const expectedRevision = snapshot.revision; const formGeneration = generation;
    const policy = snapshot.policy || {};
    const enabledLabel = node('label', 'Pause new work at a spend threshold '); const enabled = node('input'); enabled.type = 'checkbox'; enabled.checked = Boolean(policy.enabled); enabledLabel.append(enabled); form.append(enabledLabel);
    const currencyLabel = node('label', 'Currency '); const currency = node('input'); currency.value = policy.currency || 'USD'; currency.maxLength = 3; currencyLabel.append(currency); form.append(currencyLabel);
    const amountLabel = node('label', 'Threshold '); const amount = node('input'); amount.inputMode = 'decimal'; amount.value = policy.limit_amount || ''; amount.placeholder = 'Amount'; amountLabel.append(amount); form.append(amountLabel);
    const unknownLabel = node('label', 'When usage is unknown '); const unknown = node('select'); unknown.append(new Option('Pause new work', 'pause'), new Option('Reserve an amount for each invocation', 'reserve')); unknown.value = policy.unknown_policy || 'pause'; unknownLabel.append(unknown); form.append(unknownLabel);
    const reserveLabel = node('label', 'Reserve per invocation '); const reserve = node('input'); reserve.inputMode = 'decimal'; reserve.value = policy.reserve_amount || '0'; reserveLabel.append(reserve); form.append(reserveLabel);
    const showReserve = () => { reserveLabel.hidden = unknown.value !== 'reserve'; }; unknown.addEventListener('change', showReserve); showReserve();
    form.append(node('p', 'Counts reported spend since this setting was first saved, until changed. Active work and delayed usage can exceed the threshold. This is not a hard billing cap.'));
    const save = node('button', 'Save spend settings'); save.type = 'submit'; form.append(save);
    form.onsubmit = async event => {
      event.preventDefault();
      if (formGeneration !== generation || targetPath !== path()) return;
      save.disabled = true; status.textContent = 'Saving…';
      try {
        await request(targetPath, 'PUT', { expected_revision: expectedRevision, policy: { version: 1, enabled: enabled.checked,
          currency: currency.value, limit_amount: amount.value || '0', unknown_policy: unknown.value, reserve_amount: reserve.value || '0' } });
        await load(); status.textContent = 'Saved. Resume any saved pause when ready.';
      } catch (error) { status.textContent = error.message || 'Settings changed. Reload before saving.'; save.disabled = false; }
    };
  }
  const pause = node('button', 'Pause new work'); pause.type = 'button'; body.append(pause);
  pause.addEventListener('click', async () => {
    pause.disabled = true;
    try { await request('/api/admission-holds', 'POST', { scope_type: scope.value, scope_id: scope.value === 'project' ? projectId : '', reason_code: 'operator_pause' }); await load(); }
    catch (error) { status.textContent = error.message; }
    finally { pause.disabled = false; }
  });
  const reload = node('button', 'Reload settings'); reload.type = 'button'; reload.addEventListener('click', load); body.append(reload);
  scope.addEventListener('change', load);
  let loaded = false;
  root.addEventListener('toggle', () => { if (root.open && !loaded) { loaded = true; load(); } });
  return () => { disposed = true; generation++; root.remove(); };
}
