// Optional owner configuration. Runtime admission and native evidence remain authoritative.
export function mountWorkerConfiguration(disclosure, member, { requestHeaders = headers => headers } = {}) {
  const root = document.createElement('section');
  root.className = 'worker-configuration';
  disclosure.append(root);
  let current = null;
  let busy = false;
  let disposed = false;
  let loaded = false;
  const controller = new AbortController();
  const route = `/api/workers/${encodeURIComponent(member.worker_id)}/configuration`;
  root.innerHTML = '<p class="member-note" role="status" aria-live="polite" data-config-status></p><button type="button" data-config-reload hidden>Reload settings</button><div data-config-content></div>';
  const status = text => { if (!disposed) root.querySelector('[data-config-status]').textContent = text; };
  const reload = root.querySelector('[data-config-reload]');
  function text(tag, value, className = '') {
    const node = document.createElement(tag); node.textContent = value; node.className = className; return node;
  }
  async function request(method = 'GET', body) {
    const response = await fetch(route, { method, credentials:'same-origin', cache:'no-store', signal:controller.signal,
      headers:requestHeaders({ 'Content-Type':'application/json' }), ...(body === undefined ? {} : { body:JSON.stringify(body) }) });
    let result; try { result = await response.json(); } catch { result = {}; }
    if (!response.ok) { const error = new Error('Configuration unavailable'); error.status = response.status; error.code = result?.detail?.code; throw error; }
    if (result.worker_id !== member.worker_id || !Number.isInteger(result.revision) || !result.requested?.context || !result.requested?.tools || !result.requested?.background || !result.effective?.context || !result.effective?.tools || !result.effective?.background || !Array.isArray(result.available_sources) || !Array.isArray(result.available_tools)) throw new Error('Unsupported configuration');
    return result;
  }
  function choices(container, catalog, selected, group) {
    container.replaceChildren();
    const items = [...catalog];
    for (const id of selected) if (!items.some(item => item.id === id)) items.push({ id, label:group === 'source' ? 'Unavailable source' : 'Unavailable tool', status:'unavailable' });
    for (const item of items) {
      const label = document.createElement('label');
      const checkbox = document.createElement('input'); checkbox.type='checkbox'; checkbox.dataset.choice=group; checkbox.value=item.id; checkbox.checked=selected.includes(item.id);
      checkbox.disabled = item.status !== 'available' && !checkbox.checked;
      label.append(checkbox, text('span', `${item.label}${item.status === 'available' ? '' : ' · Needs attention'}`)); container.append(label);
    }
    if (!items.length) container.append(text('p', group === 'source' ? 'No context sources are available yet.' : 'No connected tools are available yet.', 'member-note'));
  }
  function sourcePreview(source, label) {
    const disclosure = document.createElement('details'); disclosure.className='worker-source-preview';
    disclosure.append(text('summary',`View ${label}`));
    const progress=text('p','','member-note'); progress.setAttribute('role','status'); progress.tabIndex=-1;
    const body=document.createElement('pre');
    const more=text('button','Load more'); more.type='button'; more.hidden=true;
    disclosure.append(progress,body,more);
    let next=0; let revision=null; let fetching=false; let opened=false;
    async function read() {
      if (fetching || next === null || disposed) return;
      fetching=true; more.disabled=true; progress.textContent='Loading source…';
      try {
        const response=await fetch(`${route}/context/${encodeURIComponent(source.id)}?offset=${next}&max_chars=12000`, { credentials:'same-origin', cache:'no-store', signal:controller.signal, headers:requestHeaders({}) });
        if (!response.ok) throw new Error('Source unavailable');
        const page=await response.json();
        if (page.source_id !== source.id || typeof page.sha256 !== 'string' || typeof page.text !== 'string' || !Number.isInteger(page.chars) || page.offset !== next || (page.next_offset !== null && (!Number.isInteger(page.next_offset) || page.next_offset <= next))) throw new Error('Source unavailable');
        if (revision && revision !== page.sha256) throw new Error('Source changed');
        if (disposed) return;
        revision=page.sha256; body.append(document.createTextNode(page.text)); next=page.next_offset;
        more.hidden=next === null;
        progress.textContent=next === null ? `Complete source · ${page.chars} characters` : `Showing ${next} of ${page.chars} characters`;
      } catch(error) {
        if (!disposed) { body.textContent=''; more.hidden=true; progress.textContent=error.message === 'Source changed' ? 'This source changed. Reload settings to read the current version.' : 'This source is no longer available. Reload settings to check access.'; }
      } finally { fetching=false; more.disabled=false; }
    }
    disclosure.addEventListener('toggle',()=>{ if (disclosure.open && !opened) { opened=true; read(); } });
    more.addEventListener('click',async () => { await read(); if (!disposed) { if (more.hidden) progress.focus(); else more.focus(); } });
    return disclosure;
  }
  function render(value) {
    const content = root.querySelector('[data-config-content]');
    content.innerHTML = '<form class="worker-config-form"><fieldset><legend>Context</legend><label>Use<select name="contextMode"><option value="inherit">All available context</option><option value="selected">Choose sources</option></select></label><div data-source-choices></div></fieldset><fieldset><legend>Connected tools</legend><label>Use<select name="toolMode"><option value="inherit">All authorized connections</option><option value="selected">Choose connected tools</option></select></label><div data-tool-choices></div><p class="member-note">The assistant’s own tools stay available.</p></fieldset><fieldset><legend>Background work</legend><label><input type="checkbox" name="backgroundEnabled"> Allow new background work</label><p class="member-note">Turning this off does not stop work already running.</p></fieldset><details class="worker-config-limits"><summary>Limits</summary><label>Concurrent runs<input type="number" name="parallel" min="1" max="32" step="1" required></label><label>Context characters included with each turn<input type="number" name="inline" min="0" max="262144" step="1" required></label><p class="member-note">Other capacity limits still apply. Remaining context uses the runtime’s retrieval path.</p></details><button type="submit">Save</button></form><details class="worker-config-effective"><summary>Current configuration</summary><p class="member-note">This shows configured access. Run activity shows what the worker actually used.</p><div data-effective></div></details>';
    const form = content.querySelector('form');
    const selected = value.requested;
    form.elements.contextMode.value=selected.context.mode;
    form.elements.toolMode.value=selected.tools.mode;
    if (value.effective.tools.selection_supported === false) {
      form.elements.toolMode.querySelector('option[value="selected"]').disabled=true;
      if (selected.tools.mode === 'inherit') form.elements.toolMode.disabled=true;
      form.elements.toolMode.closest('fieldset').append(text('p','This worker uses a fixed set of connected tools.','member-note'));
    }
    form.elements.backgroundEnabled.checked=selected.background.enabled;
    form.elements.parallel.value=selected.background.max_parallel_runs;
    form.elements.inline.value=selected.context.inline_chars;
    const sources = content.querySelector('[data-source-choices]');
    const tools = content.querySelector('[data-tool-choices]');
    choices(sources,value.available_sources,selected.context.source_ids,'source');
    choices(tools,value.available_tools,selected.tools.mcp_server_ids,'tool');
    if (value.effective.tools.selection_supported === false) for (const input of tools.querySelectorAll('input')) input.disabled=true;
    function toggleChoices() { sources.hidden=form.elements.contextMode.value !== 'selected'; tools.hidden=form.elements.toolMode.value !== 'selected'; }
    form.elements.contextMode.addEventListener('change',toggleChoices); form.elements.toolMode.addEventListener('change',toggleChoices); toggleChoices();
    const effective = content.querySelector('[data-effective]');
    const sourceNames = new Map(value.available_sources.map(item => [item.id,item.label]));
    const toolNames = new Map(value.available_tools.map(item => [item.id,item.label]));
    const sourceList = document.createElement('ul');
    for (const source of value.effective.context.sources || []) {
      const delivery = { inline:'Included with each turn', retrieval:'Available through retrieval', mixed:'Included and retrievable', unavailable:'Needs attention' }[source.delivery] || 'Needs attention';
      const label=source.label || sourceNames.get(source.id) || 'Context source';
      const line=text('li',`${label} · ${delivery}`);
      if (source.delivery !== 'unavailable' && value.available_sources.some(item=>item.id === source.id && item.status === 'available')) line.append(sourcePreview(source,label));
      sourceList.append(line);
    }
    effective.append(text('h4','Context'));
    effective.append(sourceList.childElementCount ? sourceList : text('p','No context sources are configured.','member-note'));
    effective.append(text('p',`${Number(value.effective.context.inline_chars) || 0} characters inline · ${Number(value.effective.context.retrievable_chars) || 0} available through retrieval`,'member-note'));
    effective.append(text('h4','Connected tools'),text('p',(value.effective.tools.mcp_server_ids || []).map(id => toolNames.get(id) || 'Connected tool').join(', ') || 'No worker tool connections are configured.','member-note'));
    const background = value.effective.background;
    effective.append(text('h4','Background work'),text('p',background.enabled ? `Allowed · up to ${background.max_parallel_runs} concurrent run${background.max_parallel_runs === 1 ? '' : 's'}` : 'New background work is off.','member-note'));
    const issues = value.effective.issues || [];
    if (issues.length) {
      const list = document.createElement('ul');
      for (const issue of issues) list.append(text('li',issue.message || 'A configured source or tool needs attention.'));
      effective.append(text('h4','Needs attention'),list);
      status('Some configured sources or tools need attention. Open Current configuration for details.');
    }
    form.addEventListener('submit',async event => {
      event.preventDefault(); if (busy) return;
      const values = kind => Array.from(form.querySelectorAll(`input[data-choice="${kind}"]:checked`),input=>input.value);
      const payload = { expected_revision:current.revision,
        context:{ mode:form.elements.contextMode.value, source_ids:form.elements.contextMode.value === 'selected' ? values('source') : [], inline_chars:Number(form.elements.inline.value) },
        tools:{ mode:form.elements.toolMode.value, mcp_server_ids:form.elements.toolMode.value === 'selected' ? values('tool') : [] },
        background:{ enabled:form.elements.backgroundEnabled.checked, max_parallel_runs:Number(form.elements.parallel.value) } };
      busy=true; reload.disabled=true;
      for (const control of form.elements) control.disabled=true;
      status('Saving settings…');
      try {
        current=await request('PUT',payload);
        if (disposed) return;
        status('Saved.'); render(current); root.querySelector('form button[type=submit]')?.focus();
      } catch(error) {
        if (disposed) return;
        status(error.code === 'selection_unavailable' || error.code === 'configuration_needs_attention' ? 'A selected source or tool is no longer available. Reload settings to update the selection.' : error.status === 409 ? 'Settings changed elsewhere. Reload settings before making another change.' : error.status === 401 || error.status === 403 ? 'Sign in as the workspace owner to change these settings.' : 'The save could not be confirmed. Reload settings before trying again.');
        // Keep the draft visible; reload must fetch the authoritative revision before another save.
      } finally { busy=false; reload.disabled=false; if (!disposed && form.isConnected && form.querySelector('button[type=submit]').disabled) reload.focus(); }
    });
  }
  async function load() {
    if (busy || disposed) return;
    busy=true; reload.disabled=true; reload.hidden=false; status('Loading settings…');
    try {
      current=await request(); if (disposed) return;
      loaded=true; status(''); render(current);
    } catch(error) {
      if (disposed) return;
      root.querySelector('[data-config-content]').replaceChildren();
      status(error.status === 401 || error.status === 403 ? 'Sign in as the workspace owner to view these settings.' : 'Advanced settings are not available for this worker. Try reloading.');
    } finally { busy=false; reload.disabled=false; }
  }
  const open = () => { if (disclosure.open && !loaded) load(); };
  disclosure.addEventListener('toggle',open); reload.addEventListener('click',load); open();
  return () => { disposed=true; controller.abort(); disclosure.removeEventListener('toggle',open); root.remove(); };
}
