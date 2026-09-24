"""Focused regression coverage for Watch file access refresh and draft controls."""
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "glass_drive_ui" / "static"


def test_watch_access_refresh_preserves_upload_area_during_same_owner_recheck():
    source = (STATIC / "watch.js").read_text()
    start = source.index("async function activateWatchDraft")
    end = source.index("\nasync function ", start + 10)
    activation = source[start:end]
    assert "const hadOwnerScope = Boolean(watchDraft.ownerScope());" in activation
    assert "if (!hadOwnerScope) filesUploadArea.hidden = true;" in activation
    assert "setWatchDraftBusy(true);" in activation
    assert "setWatchDraftBusy(false);" in activation


def test_watch_live_failure_enters_explicit_unavailable_state():
    source = (STATIC / "watch.js").read_text()
    assert "function showWorkspaceUnavailable" in source
    assert "if (!response.ok) {" in source
    assert "showWorkspaceUnavailable(response.status);" in source


def test_workspace_file_failure_does_not_render_an_empty_list():
    source = (STATIC / "files.js").read_text()
    assert "let listingError = '';" in source
    assert "Files are unavailable for this workspace view." in source
    assert "} else if (!entries.length)" in source


def test_draft_ready_rows_keep_remove_direct_and_replacement_picker_single_file():
    source = (STATIC / "files.js").read_text()
    assert "const directRemove = document.createElement('button')" in source
    assert "input.multiple = false;" in source
    assert "input.multiple = true;" in source
    assert "const readyRefs = () =>" in source


def test_workspace_file_404_renders_error_state_instead_of_empty_state():
    module = STATIC / "files.js"
    script = r'''
import assert from 'node:assert/strict';
class Element {
  constructor(){this.children=[];this.listeners={};this.hidden=false;this.textContent='';this.open=false;}
  addEventListener(name,fn){(this.listeners[name] ||= []).push(fn);}
  replaceChildren(...children){this.children=children;}
  append(...children){this.children.push(...children);}
  appendChild(child){this.children.push(child);return child;}
  setAttribute(name,value){this[name]=value;} removeAttribute(name){delete this[name];}
  focus(){} close(){} showModal(){}
}
const controls={};
const opts=new Proxy({workerId:'worker-test',csrf:()=>'',canUpload:()=>false,onAccess:()=>{}},
  {get(target,name){return name in target?target[name]:(controls[name]??=new Element());}});
globalThis.document={createElement:()=>new Element(),body:new Element()};
globalThis.fetch=async()=>({ok:false,status:404,json:async()=>({detail:'Worker not found.'})});
const files=createWorkspaceFiles(opts); await files.open();
assert.equal(controls.status.hidden,false);
assert.equal(controls.list.children.length,1);
assert.equal(controls.list.children[0].className,'file-error');
assert.equal(controls.list.children[0].textContent,'Files are unavailable for this workspace view.');
console.log('pass');
'''
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", f"import {{createWorkspaceFiles}} from {json.dumps(module.as_uri())};\n" + script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pass"


def test_file_download_is_visible_even_when_os_drag_out_is_not_advertised():
    module = STATIC / "files.js"
    script = r'''
import assert from 'node:assert/strict';
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this.hidden=false;this.textContent='';this.open=false;}
  addEventListener(name,fn){(this.listeners[name] ||= []).push(fn);}
  replaceChildren(...children){this.children=children;}
  append(...children){this.children.push(...children);}
  appendChild(child){this.children.push(child);return child;}
  setAttribute(name,value){this[name]=value;} focus(){} close(){} showModal(){}
}
const controls={};
const opts=new Proxy({workerId:'worker-test',csrf:()=>'',canUpload:()=>true,onAccess:()=>{}},
  {get(target,name){return name in target?target[name]:(controls[name]??=new Element());}});
globalThis.document={createElement:(tag)=>new Element(tag),body:new Element()};
Object.defineProperty(globalThis,'navigator',{value:{userAgentData:{platform:'macOS'},platform:'MacIntel',userAgent:'Chrome/150'},configurable:true});
const entry={file_id:'file-1',path:'notes.txt',name:'notes.txt',revision:'rev-1',size_bytes:12,is_dir:false};
let dragSupported=false;
globalThis.fetch=async()=>({ok:true,json:async()=>({items:[entry],can_write:true,
  drag_out_supported:dragSupported,drag_out_targets:dragSupported?['chromium_macos']:[]})});
const files=createWorkspaceFiles(opts); await files.open();
const row=controls.list.children[0];
const name=row.children.find(x=>x.className==='file-entry-text').children[0];
const direct=row.children.find(x=>x.textContent==='Download');
assert.equal(name.draggable,false);
assert.equal(direct.href,'/api/workspace/worker-test/files/file-1/content?revision=rev-1');
assert.equal(direct.draggable,false);
assert.equal(direct.download,'notes.txt');
assert.ok(!direct.href.includes('gh_token='));
dragSupported=true; await files.open();
const enabled=controls.list.children[0].children.find(x=>x.className==='file-entry-text').children[0];
assert.equal(enabled.draggable,true);
const transfer={effectAllowed:'',values:{},setData(type,value){this.values[type]=value;}};
enabled.listeners.dragstart[0]({stopPropagation(){},dataTransfer:transfer});
assert.ok(transfer.values.DownloadURL.includes('/api/workspace/worker-test/files/file-1/content?revision=rev-1'));
assert.ok(!/gh_token=|Bearer |X-WPR-Token|X-GlassHive-User-Assertion/.test(transfer.values.DownloadURL));
console.log('pass');
'''
    result = subprocess.run(
        ["node", "--input-type=module", "--eval",
         f"import {{createWorkspaceFiles}} from {json.dumps(module.as_uri())};\n" + script],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pass"


@pytest.mark.parametrize("control", ["Replace", "Replace failure", "Replace cancelled", "Clear all files", "Remove failure"])
@pytest.mark.parametrize("surface", ["launch", "watch"])
def test_file_draft_exposes_direct_replace_and_clear_all_controls(control, surface):
    module = STATIC / "files.js"
    script = r'''
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', {value:webcrypto});
class Element {
  constructor(tag='div') {
    this.tagName=tag.toUpperCase(); this.children=[]; this.listeners={}; this.value='';
    this.files=[]; this.hidden=false; this.disabled=false; this.open=false; this.isConnected=true;
    this.classList={add(){},remove(){}};
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatch(name, event={}) { for (const fn of this.listeners[name] || []) fn(event); }
  click() { if (!this.disabled) this.dispatch('click', {target:this}); }
  replaceChildren(...children) { this.children=[]; this.append(...children); }
  append(...children) { for (const child of children) { if (!child) continue; child.parentNode=this; this.children.push(child); } }
  appendChild(child) { this.append(child); return child; }
  querySelectorAll(tag) { const found=[]; const visit=(node)=>{for(const child of node.children||[]){if(child.tagName===tag.toUpperCase()) found.push(child); visit(child);}}; visit(this); return found; }
  setAttribute(name, value) { this[name]=value; }
  getAttribute(name) { return this[name] ?? null; }
  removeAttribute(name) { delete this[name]; }
  focus() {}
  remove() { this.isConnected=false; }
}
globalThis.document={createElement:(tag)=>new Element(tag), body:new Element('body')};
globalThis.window={prompt:()=>null};
const owner='a'.repeat(64), receipts=new Map(), calls=[];
const replacementFailure=control==='Replace failure';
let removeFailure=control==='Remove failure';
let nextId=1;
const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
globalThis.fetch=async(url, options={})=>{
  calls.push({url, options});
  if (url==='/api/storage') return response({limit_bytes:null,max_batch_files:1});
  if (url.startsWith('/api/file-uploads?')) {
    return response({items:[...receipts.values()].filter((item)=>item.state!=='cancelled')});
  }
  if (url==='/api/file-uploads' && options.method==='POST') {
    if (replacementFailure && nextId===2) throw new Error('quota full');
    const body=JSON.parse(options.body), id=`upload-${nextId++}`;
    const receipt={...body,upload_id:id,state:'ready',revision:`revision-${id}`,sha256:'verified',received_bytes:body.size_bytes};
    receipts.set(id,receipt); return response(receipt,201);
  }
  if (url.startsWith('/api/file-uploads/') && options.method==='DELETE') {
    if (removeFailure) throw new Error('service unavailable');
    const id=decodeURIComponent(url.split('/')[3]), receipt=receipts.get(id);
    if (!receipt) throw new Error('missing receipt');
    receipt.state='cancelled'; return response(receipt);
  }
  throw new Error(`Unexpected request ${url}`);
};
const tick=()=>new Promise((resolve)=>setTimeout(resolve,40));
const input=new Element('input'), draftList=new Element(), status=new Element(), addButton=new Element('button');
const headerClear=surface==='launch' ? new Element('button') : null;
if (headerClear) headerClear.textContent='Clear all files';
const draft=createFileDraft({input,drop:new Element(),list:draftList,help:new Element(),status,
  addButton,clearAllButton:headerClear,csrf:()=>''});
draft.setOwnerScope(owner); await tick();
if (headerClear) assert.equal(headerClear.hidden,true);
const file=(name, bytes)=>{const value=new Blob([bytes]); Object.defineProperty(value,'name',{value:name}); return value;};
draft.addFiles([file('first.txt','first')]); await tick();
assert.deepEqual(draft.readyRefs(), [{upload_id:'upload-1', revision:'revision-upload-1'}]);
const buttons=()=>{const found=[]; const visit=(node)=>{if(node.tagName==='BUTTON') found.push(node); for(const child of node.children||[]) visit(child);}; visit(draftList); return found;};
const allButtons=()=>[...buttons(),...(headerClear?[headerClear]:[])];
draft.items[0].revision = '';
assert.equal(draft.blocked(), true);
assert.deepEqual(draft.readyRefs(), []);
draft.items[0].revision = 'revision-upload-1';
assert.ok(buttons().some((button)=>button.textContent==='Replace'));
assert.ok(allButtons().some((button)=>button.textContent==='Clear all files'));
if (headerClear) assert.equal(headerClear.hidden,false);
draft.setBusy(true);
assert.equal(input.disabled,true);
assert.equal(buttons().find((button)=>button.textContent==='Replace').disabled,true);
assert.equal(allButtons().find((button)=>button.textContent==='Clear all files').disabled,true);
draft.setBusy(false);
if (control==='Replace' || control==='Replace failure' || control==='Replace cancelled') {
  buttons().find((button)=>button.textContent==='Replace').click();
  if (control==='Replace cancelled') {
    input.dispatch('cancel'); await tick();
    assert.equal(draft.items.length,1); assert.equal(draft.items[0].name,'first.txt');
    assert.equal(calls.some(({options})=>options.method==='DELETE'),false);
  } else {
    input.files=[file('second.txt','second')]; input.dispatch('change'); await tick();
    assert.equal(draft.items.some((item)=>item.name==='first.txt' && item.state==='ready'),control==='Replace failure');
    if (control==='Replace') {
      assert.equal(draft.items.length,1); assert.equal(draft.items[0].name,'second.txt');
      assert.ok(calls.some(({options})=>options.method==='DELETE'));
      if (surface==='launch') assert.match(status.textContent,/Replaced first.txt with second.txt/);
    } else {
      assert.equal(draft.items.length,2);
      assert.equal(draft.items.find((item)=>item.name==='second.txt').state,'failed');
      assert.equal(calls.some(({options})=>options.method==='DELETE'),false);
      if (surface==='launch') assert.match(status.textContent,/original is still here/);
    }
  }
} else if (control==='Remove failure') {
  buttons().find((button)=>button.textContent==='Remove').click(); await tick();
  assert.equal(draft.items.length,1);
  assert.equal(draft.items[0].state,'cleanup_failed');
  assert.ok(buttons().some((button)=>button.textContent==='Retry remove'));
  if (surface==='launch') assert.match(status.textContent,/still listed; retry remove/);
  removeFailure=false;
  buttons().find((button)=>button.textContent==='Retry remove').click(); await tick();
  assert.equal(draft.items.length,0);
  if (surface==='launch') assert.match(status.textContent,/Removed first.txt/);
} else {
  allButtons().find((button)=>button.textContent==='Clear all files').click(); await tick();
  assert.equal(draft.items.length,0);
  assert.ok([...receipts.values()].every((item)=>item.state==='cancelled'));
  if (surface==='launch') {
    assert.equal(headerClear.hidden,true);
    assert.match(status.textContent,/All files removed/);
  }
}
console.log('pass');
'''
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", f"import {{createFileDraft}} from {json.dumps(module.as_uri())};\nconst control={json.dumps(control)}, surface={json.dumps(surface)};\n" + script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pass"
