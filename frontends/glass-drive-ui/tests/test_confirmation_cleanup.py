"""Use the actual confirmation controller with bounded synthetic HTTP and DOM."""
import base64
import json
from pathlib import Path
import subprocess

STATIC = Path(__file__).parents[1] / 'src' / 'glass_drive_ui' / 'static'

SHIM = r'''
import assert from 'node:assert/strict';
const cache=new Map(),calls=[];
globalThis.sessionStorage={getItem:key=>cache.get(key)||null,setItem:(key,value)=>cache.set(key,value),removeItem:key=>cache.delete(key)};
globalThis.window={location:{hash:'#change_id=change-1&token=synthetic-confirmation-token',replace(value){this.destination=value;}}};
globalThis.history={replaceState(){window.location.hash='';}};
let elements;
function resetElements(){elements=new Map();globalThis.document={querySelector:selector=>{
  if(!elements.has(selector))elements.set(selector,{textContent:'',hidden:selector==='#confirm-back',disabled:selector==='#confirm-button',handlers:{},addEventListener(name,fn){this.handlers[name]=fn;}});
  return elements.get(selector);
}};}
resetElements();
const node=selector=>document.querySelector(selector);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const response=(value,status=200)=>({ok:status<400,status,json:async()=>value});
const pending={change_id:'change-1',change_type:'library_upgrade',status:'pending',target_id:'worker-1',target_label:'Research',capability_label:'Editorial skill',payload:{replaces_grant_id:'grant-old'},effective_scopes:[],expires_at:2000000000};
let confirmResult={status:'confirmed',applied:{native_effect:'next_native_start'}},confirmStatus=200,cleanupResult={native_cleanup:{state:'removed'},native_effect:'next_native_start'},cleanupStatus=200;
globalThis.fetch=async(path,options={})=>{calls.push({path,options});
 if(path==='/auth/session')return response({csrf_token:'synthetic-csrf'});
 if(path==='/api/pending-changes/change-1')return response(pending);
 if(path==='/api/pending-changes/change-1/confirm')return response(confirmResult,confirmStatus);
 if(path==='/api/workspaces/worker-1/capability-grants/grant-old')return response(cleanupResult,cleanupStatus);
 throw new Error(`Unexpected request ${path}`);
};
async function load(suffix=''){await import(MODULE+'#'+suffix);await tick();}
'''


def run(case):
    module = 'data:text/javascript;base64,' + base64.b64encode((STATIC / 'confirm.js').read_bytes()).decode()
    script = SHIM.replace('MODULE', json.dumps(module)) + case
    result = subprocess.run(['node', '--input-type=module'], input=script,
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_approved_update_names_next_start_and_structured_busy_error_is_plain_text():
    run(r'''
pending.payload.library_snapshot={version:'2.0.0'};
await load();assert.equal(node('#confirm-button').disabled,false);
assert.equal(node('#confirm-button').textContent,'Approve upgrade');
assert.equal(node('#confirm-capability').textContent,'Editorial skill · Version 2.0.0');
assert.match(node('#confirm-intro').textContent,/next native start/);
confirmStatus=409;confirmResult={detail:{code:'library_worker_busy',message:'Available when the worker is idle'}};
await node('#confirm-button').handlers.click();assert.equal(node('#confirm-status').textContent,'Available when the worker is idle');
assert.equal(node('#confirm-button').disabled,false);assert.equal(node('#confirm-button').hidden,false);
confirmStatus=200;confirmResult={status:'confirmed',applied:{native_effect:'next_native_start'}};
await node('#confirm-button').handlers.click();
assert.match(node('#confirm-status').textContent,/next native start/);assert.ok(!/loaded/i.test(node('#confirm-status').textContent));
assert.equal(node('#confirm-button').hidden,true);assert.equal(cache.has('glasshive.pending-confirmation'),false);
assert.equal(calls.find(call=>call.path.endsWith('/confirm')).options.headers['X-GlassHive-CSRF'],'synthetic-csrf');
''')


def test_cleanup_recovery_reloads_exact_confirmed_target_and_retries_only_existing_delete():
    run(r'''
await load();confirmResult={status:'confirmed',applied:{replaced_grant_id:'grant-old',native_effect:'next_native_start',native_cleanup:{state:'unconfirmed'}}};
await node('#confirm-button').handlers.click();
assert.equal(node('#confirm-button').textContent,'Retry file cleanup');assert.match(node('#confirm-status').textContent,/have not been removed/);
const saved=JSON.parse(cache.get('glasshive.pending-confirmation'));
assert.deepEqual(saved,{changeId:'change-1',cleanup:{workerId:'worker-1',grantId:'grant-old'}});assert.ok(!('token' in saved));
pending.status='confirmed';resetElements();await load('?cleanup-reload');
assert.equal(node('#confirm-button').textContent,'Retry file cleanup');assert.equal(node('.confirm-notice').hidden,true);
cleanupStatus=409;cleanupResult={detail:{code:'library_worker_busy',message:'Available when the worker is idle'}};
await node('#confirm-button').handlers.click();assert.equal(node('#confirm-status').textContent,'Available when the worker is idle');assert.equal(node('#confirm-button').disabled,false);
cleanupStatus=200;cleanupResult={native_effect:'next_native_start',native_cleanup:{state:'removed',retained:[{path:'edited.md'}]}};
await node('#confirm-button').handlers.click();
assert.match(node('#confirm-status').textContent,/Edited or shared files were kept/);assert.match(node('#confirm-status').textContent,/next native start/);
assert.equal(cache.has('glasshive.pending-confirmation'),false);assert.equal(node('#confirm-button').hidden,true);
assert.equal(node('#confirm-intro').textContent,'This workspace change is approved.');
assert.equal(calls.filter(call=>call.path.endsWith('/confirm')).length,1);
const deletes=calls.filter(call=>call.options.method==='DELETE');assert.equal(deletes.length,2);assert.ok(deletes.every(call=>call.path==='/api/workspaces/worker-1/capability-grants/grant-old'));
assert.equal(deletes[0].options.headers['X-GlassHive-CSRF'],'synthetic-csrf');
''')


def test_cleanup_recovery_rejects_a_different_worker_or_replaced_grant():
    run(r'''
window.location.hash='';cache.set('glasshive.pending-confirmation',JSON.stringify({changeId:'change-1',cleanup:{workerId:'worker-other',grantId:'grant-old'}}));
pending.status='confirmed';await load();
assert.match(node('#confirm-status').textContent,/no longer match/);assert.equal(node('#confirm-button').disabled,true);
await node('#confirm-button').handlers.click();assert.equal(calls.filter(call=>call.options.method==='DELETE').length,0);
''')
