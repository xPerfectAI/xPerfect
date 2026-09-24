"""Draft recovery must preserve intent without crossing browser account boundaries."""
import base64
import subprocess
from pathlib import Path

from glass_drive_ui import server


def run_case(case):
    source = (Path(server.STATIC_DIR) / 'launch-draft.js').read_bytes()
    uri = 'data:text/javascript;base64,' + base64.b64encode(source).decode()
    script = f"import {{ createLaunchDraft }} from '{uri}';\n" + r'''
import assert from 'node:assert/strict';
const cache = new Map();
const storage = {getItem: key => cache.get(key) ?? null, setItem: (key,value) => cache.set(key,value), removeItem: key => cache.delete(key)};
function field() { return {value:'', addEventListener(_event,fn){this.input=fn}}; }
function draft(store = () => storage) { const fields={goal:field(),context:field()}; return {fields,state:createLaunchDraft({fields,storage:store})}; }
const a='a'.repeat(64), b='b'.repeat(64);
''' + case
    subprocess.run(['node', '--input-type=module', '--eval', script], check=True, capture_output=True, text=True)


def test_reload_retains_exact_goal_context_and_request_identity():
    run_case(r'''
const first=draft(); first.state.bindOwner(a);
first.fields.goal.value='First line\nSecond line'; first.fields.goal.input();
first.fields.context.value='Do not contact anyone.'; first.fields.context.input();
const payload={goal:first.fields.goal.value,context:first.fields.context.value,files:['input-1']};
assert.equal(first.state.requestKey(payload,()=> 'stable-request'),'stable-request');
const reload=draft(); reload.state.bindOwner(a);
assert.equal(reload.fields.goal.value,first.fields.goal.value);
assert.equal(reload.fields.context.value,first.fields.context.value);
assert.equal(reload.state.requestKey(payload,()=> 'wrong-duplicate'),'stable-request');
assert.equal(reload.state.requestKey({...payload,files:['input-2']},()=> 'changed-request'),'changed-request');
reload.state.clear();
const accepted=draft(); accepted.state.bindOwner(a); assert.equal(accepted.fields.goal.value,'');
''')


def test_account_change_never_displays_or_deletes_other_owner_draft():
    run_case(r'''
const first=draft(); first.state.bindOwner(a);
first.fields.goal.value='Private draft A'; first.fields.goal.input();
first.state.bindOwner(b); assert.equal(first.fields.goal.value,'');
first.fields.goal.value='Draft B'; first.fields.goal.input();
first.state.clear();
const aReload=draft(); aReload.state.bindOwner(a); assert.equal(aReload.fields.goal.value,'Private draft A');
const bReload=draft(); bReload.state.bindOwner(b); assert.equal(bReload.fields.goal.value,'');
''')


def test_slow_bootstrap_or_unavailable_storage_does_not_replace_typed_goal():
    run_case(r'''
const original=draft(); original.state.bindOwner(a); original.fields.goal.value='Old'; original.fields.goal.input();
const delayed=draft(); delayed.fields.goal.value='New typed goal'; delayed.fields.goal.input(); delayed.state.bindOwner(a);
assert.equal(delayed.fields.goal.value,'New typed goal');
const denied=draft(()=>{throw new Error('Storage unavailable')}); denied.state.bindOwner(a);
denied.fields.goal.value='Keep working'; denied.fields.goal.input();
assert.equal(denied.state.requestKey({goal:'Keep working'},()=> 'in-memory'),'in-memory');
assert.equal(denied.fields.goal.value,'Keep working');
''')


def test_select_draft_restores_default_and_preserves_valid_choice():
    run_case(r'''
function selectField(defaultValue='isolated') {
  return {value:defaultValue, options:['isolated','shared'].map(value => ({value})), addEventListener(_event,fn){this.input=fn}};
}
function selectDraft() {
  const fields={mode:selectField()};
  return {fields,state:createLaunchDraft({fields,storage:()=>storage})};
}
const first=selectDraft(); first.state.bindOwner(a);
assert.equal(first.fields.mode.value,'isolated');
first.fields.mode.value='shared'; first.fields.mode.input();
const reload=selectDraft(); reload.state.bindOwner(a);
assert.equal(reload.fields.mode.value,'shared');
cache.set(`xperfect.launch-draft.v1.${b}`, JSON.stringify({version:1,values:{mode:''}}));
const recovered=selectDraft(); recovered.state.bindOwner(b);
assert.equal(recovered.fields.mode.value,'isolated');
recovered.fields.mode.value='shared'; recovered.state.clear();
assert.equal(recovered.fields.mode.value,'isolated');
''')
