// Owner action over an exact current roster. It never changes discovery or future-member access.
export function mountPeerPermissions(container, memberId, { request, onSaved, report }) {
  const root = document.createElement('details');
  root.className = 'peer-permissions';
  root.innerHTML = '<summary>Allow messages with current workers</summary><p data-permission-status role="status" aria-live="polite"></p><button type="button" data-options-reload hidden>Reload workers</button><div data-permission-content></div>';
  container.append(root);
  let options = null, busy = false, disposed = false, pending = null, selectionToPreserve = null;
  const status = message => { if (!disposed) root.querySelector('[data-permission-status]').textContent = message; };
  const reload = root.querySelector('[data-options-reload]');
  function text(tag,value) { const node=document.createElement(tag); node.textContent=value; return node; }
  async function load() {
    if (busy || disposed) return;
    const currentForm = root.querySelector('[data-permission-content] form');
    if (currentForm) selectionToPreserve = new Set(Array.from(currentForm.querySelectorAll('[data-permission-member]:checked'), node => node.value));
    busy=true; reload.disabled=true; root.querySelector('[data-permission-content]').replaceChildren(); status('Loading your workers…');
    try {
      const value=await request(`/api/workers/${encodeURIComponent(memberId)}/peer-access-options`);
      if (disposed) return;
      if (value.source_worker_id !== memberId || !Array.isArray(value.workspaces) || !Number.isInteger(value.max_targets) || value.includes_future_members !== false) throw new Error('peer_options_unavailable');
      options=value; pending=null; render(selectionToPreserve); selectionToPreserve=null; status('');
    } catch { status('Worker choices are unavailable. Reload to try again.'); }
    finally { busy=false; reload.disabled=false; reload.hidden=false; }
  }
  function render(preservedSelection) {
    const content=root.querySelector('[data-permission-content]');
    content.innerHTML='<form><p>Choose the current workers in your account. New workers stay excluded until you allow them.</p><div class="peer-select-actions"><button type="button" data-select-all>Select all current workers</button><button type="button" data-select-none>Clear selection</button></div><div data-workspace-choices></div><label>Messages<select name="direction"><option value="both">Both ways</option><option value="send">From this worker</option><option value="receive">To this worker</option></select></label><label><input name="wake" type="checkbox"> May start a receiving worker when idle</label><p class="member-note">Leave this off if the other worker is already running. An idle worker cannot receive a new message until this is allowed.</p><details><summary>Permission duration</summary><label>Keep access<select name="duration"><option value="persistent">Until revoked</option><option value="1">For one hour</option><option value="24">For one day</option><option value="720">For 30 days</option></select></label></details><p data-selection-summary role="status" aria-live="polite"></p><p class="member-note">This turns on access in the selected workers’ workspaces and allows messages only with these workers. Finding workers stays separate.</p><button type="submit" disabled>Allow messages both ways</button></form>';
    const form=content.querySelector('form'); const choices=content.querySelector('[data-workspace-choices]');
    const available = options.workspaces.reduce((count, workspace) => count + workspace.members.filter(member => member.worker_id !== memberId).length, 0);
    const defaultAll = preservedSelection === null && available <= options.max_targets;
    for (const workspace of options.workspaces) {
      const members=workspace.members.filter(m=>m.worker_id !== memberId);
      if (!members.length) continue;
      const group=document.createElement('fieldset'); const legend=text('legend',workspace.workspace_id===options.source_workspace_id?'This workspace':workspace.name); group.append(legend);
      const label=document.createElement('label'); const all=document.createElement('input'); all.type='checkbox'; all.dataset.workspace=workspace.workspace_id;
      label.append(all,text('span','All current workers here')); label.hidden=members.length===1; group.append(label);
      for (const member of members) {
        const label=document.createElement('label'); const input=document.createElement('input'); input.type='checkbox'; input.value=member.worker_id; input.dataset.permissionMember=workspace.workspace_id;
        input.checked=defaultAll || (preservedSelection?.has(member.worker_id) ?? false);
        label.append(input,text('span',member.name)); group.append(label);
      }
      all.addEventListener('change',()=>{ for (const input of group.querySelectorAll('[data-permission-member]')) input.checked=all.checked; update(); });
      choices.append(group);
    }
    const selected=()=>Array.from(form.querySelectorAll('[data-permission-member]:checked'),node=>node.value);
    function update() {
      const ids=selected(); const over=ids.length>options.max_targets;
      for (const group of choices.querySelectorAll('fieldset')) {
        const inputs=[...group.querySelectorAll('[data-permission-member]')]; const check=group.querySelector('[data-workspace]');
        check.checked=inputs.every(input=>input.checked); check.indeterminate=inputs.some(input=>input.checked)&&!check.checked;
      }
      form.querySelector('[data-selection-summary]').textContent=available > options.max_targets && !ids.length
        ? `${available} workers are available. Choose up to ${options.max_targets}; new workers stay excluded.`
        : over ? `Choose up to ${options.max_targets} workers.`
          : `${ids.length} current worker${ids.length===1?'':'s'} selected · ${form.elements.duration.value==='persistent'?'until revoked':'temporary access'}`;
      const button=form.querySelector('[type=submit]'); button.disabled=!ids.length||over;
      button.textContent={both:'Allow two-way messages',send:'Allow messages from this worker',receive:'Allow messages to this worker'}[form.elements.direction.value];
    }
    form.addEventListener('change',update);
    form.querySelector('[data-select-all]').disabled=available>options.max_targets || !available;
    for (const [selector,checked] of [['[data-select-all]',true],['[data-select-none]',false]]) form.querySelector(selector).addEventListener('click',()=>{ for (const input of form.querySelectorAll('[data-permission-member]')) input.checked=checked; update(); });
    update();
    form.addEventListener('submit',async event=>{
      event.preventDefault(); if (busy) return;
      const targets=selected(); if (!targets.length || targets.length>options.max_targets) return;
      const chosen=new Set(targets); const snapshots=options.workspaces.filter(w=>w.workspace_id===options.source_workspace_id||w.members.some(m=>chosen.has(m.worker_id))).map(w=>({workspace_id:w.workspace_id,revision:w.revision,member_ids:w.members.map(m=>m.worker_id)}));
      const intent={source_worker_id:memberId,target_worker_ids:targets,workspaces:snapshots,direction:form.elements.direction.value,wake:form.elements.wake.checked,enable_access:true};
      const signature=JSON.stringify({...intent,duration:form.elements.duration.value});
      if (!pending || pending.signature!==signature) pending={signature,payload:{...intent,expires_at:form.elements.duration.value==='persistent'?null:new Date(Date.now()+Number(form.elements.duration.value)*3600000).toISOString(),idempotency_key:crypto.randomUUID()}};
      busy=true; reload.disabled=true; for (const control of form.elements) control.disabled=true; status('Saving permission…');
      try {
        const result=await request('/api/peer-grants/batch','POST',pending.payload);
        if (disposed) return;
        if (!Array.isArray(result.items)||result.items.length !== targets.length*(intent.direction==='both'?2:1)||result.items.some(item=>item.status!=='active')) throw new Error('peer_permission_changed');
        pending=null; status('Message permission saved.'); await onSaved();
        if (!disposed) { root.open=false; root.querySelector('summary').focus(); report('Message permission saved for the selected current workers. Revoke each direction below.'); }
        options=null;
      } catch(error) {
        if (!disposed) {
          status(['peer_snapshot_stale','peer_policy_stale'].includes(error.message)?'The worker list or settings changed. Reload workers before allowing messages.':'Permission could not be confirmed. Reload workers to check the current state.');
          reload.disabled=false; reload.focus();
        }
      } finally { busy=false; reload.disabled=false; }
    });
  }
  root.addEventListener('toggle',()=>{ if (root.open && !options) load(); });
  reload.addEventListener('click',load);
  const cleanup=()=>{ disposed=true; root.remove(); };
  cleanup.reload=load;
  return cleanup;
}
