/* Human choices over the existing new/open/duplicate and workspace-member APIs. */
export function hostAccountLabel(data = {}, fallback = false) {
  return data.managed_deployment === true
    ? (fallback ? 'Organization account fallback' : 'Organization account')
    : (fallback ? 'Host account fallback' : 'Host account');
}

export function workerChoiceLabel(worker, data = {}) {
  const type = worker.profile_label || worker.profile || 'Worker';
  const account = worker.provider_readiness?.label
    || (worker.provider_readiness?.policy === 'legacy' ? hostAccountLabel(data) : 'Account status unavailable');
  return [worker.name || worker.workspace_label || 'AI Worker', type, account,
    worker.workspace_name || worker.workspace_label || 'Workspace',
    ...(worker.workspace_kind === 'ephemeral' ? ['One-off · kept when reused'] : [])].join(' · ');
}

export function workerTypeOption(item, host = false) {
  const unavailable = host && item.host_cli_installed === false;
  return { value: item.value,
    label: `${item.label || item.profile}${unavailable ? ' · Not installed on this computer' : ''}`,
    disabled: item.disabled === true || unavailable };
}

export function mountWorkerOnboarding({ root = document, getData, onChoice, onBrowse }) {
  const $ = id => root.getElementById(id);
  const launch = $('workspace-option');
  const workerChoice = $('worker-choice');
  const type = $('worker-type');
  const existing = $('existing-worker');
  const copy = $('copy-worker');
  const locationChoice = $('workspace-choice');
  const location = $('execution-workspace');
  const search = $('worker-search');
  const more = $('worker-load-more');
  let ownerScope = '';
  let rendering = false;
  let availability = 'loading';
  function options(select, items, placeholder = null) {
    const previous = select.value;
    const nodes = [];
    if (placeholder !== null) { const option = root.createElement('option'); option.value = ''; option.textContent = placeholder; nodes.push(option); }
    for (const item of items) {
      const option = root.createElement('option'); option.value = item.value; option.textContent = item.label;
      option.disabled = item.disabled === true; nodes.push(option);
    }
    select.replaceChildren(...nodes);
    if (nodes.some(option => option.value === previous && !option.disabled)) select.value = previous;
  }
  function choose(value) {
    if (rendering || !value) return;
    workerChoice.value = value.startsWith('open:') || value.startsWith('existing:') ? 'existing' : 'new';
    onChoice(value);
    render();
    launch.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function render(state = null) {
    const data = getData();
    if (state) availability = state;
    else if (data) availability = 'ready';
    if (!data) {
      options(type, [], availability === 'unavailable'
        ? 'Worker types unavailable — refresh to retry' : 'Loading worker types…');
      type.disabled = true;
      type.dataset.availability = availability;
      return;
    }
    rendering = true;
    const knownWorkers = data.existing_workspaces || [];
    const catalog = data.worker_catalog;
    const listedWorkers = catalog?.items || knownWorkers;
    const value = String(launch.value || '');
    const reuse = value.startsWith('open:') || value.startsWith('existing:');
    const copying = value.startsWith('duplicate:');
    const saved = knownWorkers.find(item => item.worker_id === value.split(':', 2)[1]);
    const workers = saved && !listedWorkers.some(item => item.worker_id === saved.worker_id)
      ? [saved, ...listedWorkers] : listedWorkers;
    const wantsExisting = reuse || workerChoice.value === 'existing';
    workerChoice.value = wantsExisting ? 'existing' : 'new';
    const genuinelyEmpty = !workers.length && !catalog?.next_cursor && !catalog?.search
      && !catalog?.error && !catalog?.loading;
    root.querySelectorAll('[name="worker-choice-radio"]').forEach(radio => {
      radio.checked = radio.value === workerChoice.value;
      radio.disabled = radio.value === 'existing' && genuinelyEmpty;
      if (radio.value === 'existing') radio.title = genuinelyEmpty ? 'Create your first worker with New' : '';
    });
    $('new-worker-fields').hidden = wantsExisting;
    $('existing-worker-field').hidden = !wantsExisting;
    const workerOptions = workers.map(item => ({ value: item.worker_id, label: workerChoiceLabel(item, data) }));
    if ((reuse || copying) && !saved) workerOptions.unshift({
      value: value.split(':', 2)[1], label: 'Selected worker unavailable — refresh or choose another', disabled: true,
    });
    options(existing, workerOptions, workers.length ? 'Choose an AI Worker' : 'No workers yet — choose New');
    options(copy, workerOptions, 'Start fresh');
    const host = ($('workspace-type')?.value || data.default_workspace_type) === 'host';
    const typeOptions = (data.new_workspace_options || []).map(item => workerTypeOption(item, host));
    const selectedType = saved ? `new:${saved.profile}` : value;
    options(type, typeOptions, !typeOptions.length ? 'No worker types available'
      : typeOptions.some(item => item.value === selectedType) ? null : 'Choose a worker type');
    if (reuse) existing.value = value.split(':', 2)[1];
    if (copying) copy.value = value.split(':', 2)[1];
    else copy.value = '';
    type.value = typeOptions.some(item => item.value === selectedType) ? selectedType : '';
    type.disabled = copying || !typeOptions.length;
    type.dataset.availability = typeOptions.length ? 'ready' : 'empty';
    if (copying && !saved) type.value = '';
    if (search && ownerScope !== String(data.draft_owner_scope || '')) {
      ownerScope = String(data.draft_owner_scope || '');
      search.value = catalog?.search || '';
    }
    const copyDetails = $('copy-worker-details');
    if (copying && copyDetails) copyDetails.open = true;
    const catalogControls = $('worker-catalog-controls');
    const catalogHome = $(wantsExisting ? 'existing-worker-search' : 'copy-worker-search');
    if (catalogControls && catalogHome && catalogControls.parentElement !== catalogHome) catalogHome.appendChild(catalogControls);
    if (catalogControls) catalogControls.hidden = !(wantsExisting || copyDetails?.open)
      || (genuinelyEmpty && !wantsExisting);
    if (more) { more.hidden = !catalog?.next_cursor; more.disabled = catalog?.loading === true; }
    if ($('worker-search-button')) $('worker-search-button').disabled = catalog?.loading === true;
    if ($('worker-catalog-status')) $('worker-catalog-status').textContent = catalog?.loading ? 'Loading workers…'
      : catalog?.error || data.worker_choice_error?.message || (catalog?.search && !listedWorkers.length ? 'No matching workers. Try another search.' : '');
    $('workspace-location-choices').hidden = reuse || copying;
    $('saved-worker-location').hidden = !reuse && !copying;
    $('saved-worker-location').textContent = (reuse || copying) && !saved
      ? 'The selected worker could not be verified. Refresh the list or choose another worker.' : reuse
      ? `I'll continue in ${saved?.workspace_name || saved?.workspace_label || 'this worker’s workspace'}, with its saved files and history.`
      : copying ? 'I’ll start in a new workspace with the copied files, project notes, worker type and AI account. Chat history, browser sign-ins and tool approvals stay separate.' : '';
    if (reuse || copying) locationChoice.value = 'new';
    root.querySelectorAll('[name="workspace-choice-radio"]').forEach(radio => { radio.checked = radio.value === locationChoice.value; });
    const seen = new Set();
    const locations = [...(data.execution_workspace_choices || []), ...knownWorkers]
      .filter(item => item.workspace_id && !seen.has(item.workspace_id) && seen.add(item.workspace_id));
    const locationItems = locations.map(item => ({ value: item.workspace_id,
      label: item.unavailable ? item.workspace_name : `${item.workspace_name || item.workspace_label || item.name} · ${item.execution_workspace_mode === 'shared' ? 'Shared' : 'Private — reuse its worker'}`,
      disabled: item.unavailable === true || item.execution_workspace_mode !== 'shared' }));
    const priorLocation = location.value;
    options(location, locationItems, 'Choose a shared workspace');
    if (locationItems.some(item => item.value === priorLocation)) location.value = priorLocation;
    location.dataset.workspaceChoiceState = locationItems.some(item => item.value === location.value && item.disabled)
      ? 'unavailable' : '';
    const atExisting = !reuse && !copying && locationChoice.value === 'existing';
    $('existing-workspace-field').hidden = !atExisting;
    $('new-workspace-fields').hidden = reuse || copying || atExisting;
    $('execution-workspace-help').textContent = data.workspace_choice_error?.value === location.value
      ? data.workspace_choice_error.message : locationItems.some(item => !item.disabled)
      ? 'This worker will join the selected workspace. Its account and native session stay private.'
      : 'No shared workspace is available. Choose New, or select the existing AI Worker above to keep its private workspace.';
    rendering = false;
  }
  root.querySelectorAll('[name="worker-choice-radio"]').forEach(radio => radio.addEventListener('change', () => {
    if (!radio.checked) return;
    workerChoice.value = radio.value;
    const value = radio.value === 'existing'
      ? (existing.value ? `open:${existing.value}` : '')
      : type.value;
    if (value) choose(value);
    else {
      workerChoice.value = radio.value;
      $('new-worker-fields').hidden = radio.value === 'existing';
      $('existing-worker-field').hidden = radio.value !== 'existing';
      render();
    }
    workerChoice.dispatchEvent(new Event('input', { bubbles: true }));
  }));
  const browse = () => onBrowse?.({ search: String(search?.value || '').trim() });
  $('worker-search-button')?.addEventListener('click', browse);
  search?.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); browse(); } });
  more?.addEventListener('click', () => onBrowse?.({ append: true }));
  $('workspace-type')?.addEventListener('change', render);
  $('copy-worker-details')?.addEventListener('toggle', render);
  type.addEventListener('change', () => choose(type.value));
  existing.addEventListener('change', () => { if (existing.value) choose(`open:${existing.value}`); });
  copy.addEventListener('change', () => choose(copy.value ? `duplicate:${copy.value}` : type.value));
  root.querySelectorAll('[name="workspace-choice-radio"]').forEach(radio => radio.addEventListener('change', () => {
    if (!radio.checked) return;
    locationChoice.value = radio.value;
    locationChoice.dispatchEvent(new Event('input', { bubbles: true }));
    render();
  }));
  return { render };
}
