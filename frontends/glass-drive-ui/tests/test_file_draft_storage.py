"""Optional browser storage must not interrupt file transport or accepted results."""
import json
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('failure', ['access', 'read', 'write', 'remove'])
def test_file_drafts_and_attach_work_when_browser_storage_fails(failure):
    module = Path(__file__).parents[1] / 'src/glass_drive_ui/static/files.js'
    script = r'''
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', {value:webcrypto});
class Element {
  constructor() { this.children=[]; this.listeners={}; this.value=''; this.classList={add(){},remove(){}}; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  replaceChildren(...children) { this.children=children; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute() {} removeAttribute() {}
}
globalThis.document={createElement:()=>new Element()};
Object.defineProperty(globalThis,'navigator',{value:{platform:'test'},configurable:true});
const stored=new Map();
const ownerA='a'.repeat(64), ownerB='b'.repeat(64);
// A failed removal must not resurrect a stale persisted draft in this page.
stored.set(`xperfect.files.draft.launch.${ownerA}`, 'saved-a');
const storage={
  getItem(k) { if (failure==='read') throw new Error('storage read denied'); return stored.get(k)||null; },
  setItem(k,v) { if (failure==='write') throw new Error('storage full'); stored.set(k,v); },
  removeItem(k) { if (failure==='remove') throw new Error('storage remove denied'); stored.delete(k); },
};
Object.defineProperty(globalThis,'sessionStorage',{get(){if(failure==='access')throw new Error('storage denied');return storage;}});
let calls=[], receipts=new Map(), attachCalls=[], rejectAttach=true;
const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
globalThis.fetch=async(url,options={})=>{
  calls.push(url);
  if(url==='/api/storage')return response({limit_bytes:null});
  if(url.startsWith('/api/file-uploads?')) {
    const id=new URL(url,'https://example.invalid').searchParams.get('draft_id');
    return response({items:[...receipts.values()].filter(x=>x.draft_id===id)});
  }
  if(url==='/api/file-uploads') {
    const body=JSON.parse(options.body), receipt={...body,upload_id:'upload-'+receipts.size,state:'pending'};
    receipts.set(receipt.upload_id,receipt);return response(receipt,201);
  }
  if(url.startsWith('/api/workspace/')) {
    if(options.method==='POST') {
      attachCalls.push(JSON.parse(options.body));
      if(rejectAttach){rejectAttach=false;throw new Error('connection lost');}
      return response({state:'accepted'});
    }
    return response({items:[],can_write:true,drag_out_targets:[]});
  }
  throw new Error('Unexpected request '+url);
};
globalThis.XMLHttpRequest=class {
  constructor(){this.upload={};}
  open(method,url){this.id=url.split('/')[3];}
  setRequestHeader(){}
  send(file){const receipt=receipts.get(this.id);Object.assign(receipt,{state:'ready',received_bytes:file.size,sha256:'verified',revision:'r1'});this.status=200;this.responseText=JSON.stringify(receipt);queueMicrotask(()=>this.onload());}
};
const tick=()=>new Promise(resolve=>setTimeout(resolve,25));
const draft=createFileDraft({input:new Element(),drop:new Element(),list:new Element(),help:new Element(),csrf:()=>''});
draft.setOwnerScope(ownerA);await tick();assert.equal(draft.blocked(),false);
const firstDraft=new URL(calls.find(x=>x.startsWith('/api/file-uploads?')),'https://example.invalid').searchParams.get('draft_id');
const blob=new Blob(['synthetic bytes']);blob.name='input.txt';draft.addFiles([blob]);await tick();
assert.equal(draft.items[0].state,'ready');assert.equal(draft.readyIds().length,1);
const uploadId=draft.readyIds()[0];draft.dismissReady(uploadId);assert.deepEqual(draft.readyIds(),[]);
draft.setOwnerScope(ownerB);await tick();assert.deepEqual(draft.readyIds(),[]);assert.equal(draft.items.length,0);
draft.setOwnerScope(ownerA);await tick();assert.equal(draft.items[0].upload_id,uploadId);assert.deepEqual(draft.readyIds(),[]);
draft.reset();draft.setOwnerScope(null);draft.setOwnerScope(ownerA);await tick();
assert.equal(draft.items.length,0);assert.equal(draft.blocked(),false);
const lastDraft=new URL(calls.filter(x=>x.startsWith('/api/file-uploads?')).at(-1),'https://example.invalid').searchParams.get('draft_id');
assert.notEqual(lastDraft,firstDraft);
// An ambiguous attach failure keeps the same key even when storage cannot write.
const options=new Proxy({workerId:'worker-test',csrf:()=>'',canUpload:()=>true,onAccess:()=>{}},{get(target,name){return name in target?target[name]:new Element();}});
const workspace=createWorkspaceFiles(options);
await assert.rejects(workspace.attach([uploadId]),/connection lost/);
await workspace.attach([uploadId]);
assert.equal(attachCalls[0].idempotency_key,attachCalls[1].idempotency_key);
console.log('pass');
'''
    result = subprocess.run(
        ['node', '--input-type=module', '--eval',
         f'import {{createFileDraft,createWorkspaceFiles}} from {json.dumps(module.as_uri())};\n'
         f'const failure={json.dumps(failure)};\n' + script],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'pass'
