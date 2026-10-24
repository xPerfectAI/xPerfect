"""Native file-name modal uses existing file transport and authority checks."""
import json
from pathlib import Path
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def run(body):
    script = r'''
import assert from 'node:assert/strict';
import {requestFileName,createWorkspaceFiles} from MODULE;
class Element {
 constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.value='';this.hidden=false;this.disabled=false;this.open=false;this.isConnected=true;this.textContent='';this.classList={add(){},remove(){}};}
 append(...children){for(const child of children){if(child&&typeof child==='object')child.parent=this;this.children.push(child);}}
 appendChild(child){this.append(child);return child;}
 replaceChildren(...children){this.children=[];this.append(...children);}
 setAttribute(name,value){this[name]=value;}removeAttribute(name){delete this[name];}
 addEventListener(name,fn,options){(this.handlers[name]||=[]).push({fn,once:options?.once});}
 async fire(name,event={}){for(const item of [...this.handlers[name]||[]]){await item.fn(event);if(item.once)this.handlers[name]=this.handlers[name].filter(value=>value!==item);}}
 showModal(){this.open=true;}
 close(){this.open=false;void this.fire('close');}
 remove(){this.isConnected=false;this.parent.children=this.parent.children.filter(item=>item!==this);}
 focus(){this.focused=true;}select(){this.selected=true;}
 reportValidity(){return !this.required || Boolean(this.value);}
}
const body=new Element('body'),opener=new Element('button');
globalThis.document={body,activeElement:opener,createElement:tag=>new Element(tag)};
globalThis.window={prompt:()=>{throw new Error('Legacy browser prompt must not be used');}};
const descendants=(root)=>[root,...root.children.flatMap(descendants)];
const activeDialog=()=>body.children.find(item=>item.tag==='dialog');
const child=(root,tag)=>descendants(root).find(item=>item.tag===tag);
const button=(root,text)=>descendants(root).find(item=>item.tag==='button'&&item.textContent===text);
const submit=async(dialog,value)=>{const input=child(dialog,'input');input.value=value;await child(dialog,'form').fire('submit',{preventDefault(){}});};
'''.replace('MODULE', json.dumps((STATIC / 'files.js').as_uri()))
    result = subprocess.run(['node', '--input-type=module'], input=script + body, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_name_dialog_save_cancel_escape_and_abort_preserve_focus():
    run(r'''
let answer=requestFileName({title:'Rename file',value:'original.txt'});let dialog=activeDialog(),input=child(dialog,'input');
assert.equal(dialog.className,'file-move-dialog');assert.equal(child(dialog,'h3').id,dialog['aria-labelledby']);
assert.equal(input.value,'original.txt');assert.equal(input.focused,true);assert.equal(input.selected,true);assert.equal(child(dialog,'label').htmlFor,input.id);
await submit(dialog,'');assert.equal(dialog.open,true);assert.equal(body.children.length,1);
await submit(dialog,'renamed.txt');assert.equal(await answer,'renamed.txt');assert.equal(body.children.length,0);assert.equal(opener.focused,true);
opener.focused=false;answer=requestFileName({title:'New folder',folder:true,action:'Create folder'});dialog=activeDialog();
assert(button(dialog,'Create folder'));assert.equal(child(dialog,'span').textContent,'Folder name');await button(dialog,'Cancel').fire('click');assert.equal(await answer,null);assert.equal(opener.focused,true);
answer=requestFileName({title:'Rename folder'});dialog=activeDialog();await dialog.fire('cancel');dialog.close();assert.equal(await answer,null);
const controller=new AbortController();answer=requestFileName({title:'Rename file',signal:controller.signal});controller.abort();assert.equal(await answer,null);assert.equal(body.children.length,0);
assert.equal(await requestFileName({title:'Rename file',signal:controller.signal}),null);assert.equal(body.children.length,0);
''')


def test_name_dialog_reports_unavailable_native_modal_without_legacy_fallback():
    run(r'''
document.createElement=tag=>{const item=new Element(tag);if(tag==='dialog')item.showModal=()=>{throw new Error('Native modal unavailable');};return item;};
await assert.rejects(requestFileName({title:'Rename file'}),/name editor could not open/);
assert.equal(body.children.length,0);assert.equal(opener.focused,true);
''')


def test_workspace_rename_and_folder_keep_exact_ids_revisions_validation_and_cancel():
    run(r'''
const controls={},calls=[];let writeAllowed=true;
const opts=new Proxy({workerId:'worker-exact',csrf:()=> 'synthetic-csrf',canUpload:()=>writeAllowed,onAccess(){}},
 {get(target,name){return name in target?target[name]:(controls[name]??=new Element());}});
const receipt={file_id:'file/exact',name:'original.txt',path:'notes/original.txt',revision:'revision-exact',size:1,is_dir:false};
globalThis.fetch=async(url,options={})=>{calls.push({url,...options});return {ok:true,status:200,json:async()=>options.method&&options.method!=='GET'?{}:{items:[receipt],can_write:true}};};
const files=createWorkspaceFiles(opts);await files.open('notes');
const fileMenuOpener=new Element('summary');controls.optionsMenu.querySelector=selector=>selector==='summary'?fileMenuOpener:null;
const rename=descendants(controls.list).find(item=>item.tag==='button'&&item.textContent==='Rename');
let pending=rename.fire('click');await submit(activeDialog(),'changed.txt');await pending;
let mutations=calls.filter(call=>call.method==='PATCH');assert.equal(mutations.length,1);assert.equal(mutations[0].url,'/api/workspace/worker-exact/files/file%2Fexact');
assert.deepEqual(JSON.parse(mutations[0].body),{path:'notes/changed.txt',revision:'revision-exact'});assert.equal(mutations[0].headers['X-GlassHive-CSRF'],'synthetic-csrf');
pending=descendants(controls.list).find(item=>item.tag==='button'&&item.textContent==='Rename').fire('click');await submit(activeDialog(),'bad/name');await pending;
assert.equal(calls.filter(call=>call.method==='PATCH').length,1);assert.match(controls.status.textContent,/without slashes/);
pending=controls.folderButton.fire('click');await button(activeDialog(),'Cancel').fire('click');await pending;assert.equal(calls.filter(call=>call.method==='POST').length,0);assert.equal(fileMenuOpener.focused,true);
pending=controls.folderButton.fire('click');await submit(activeDialog(),'Research');await pending;
const folder=calls.find(call=>call.method==='POST');assert.equal(folder.url,'/api/workspace/worker-exact/directories');assert.deepEqual(JSON.parse(folder.body),{path:'notes/Research'});
pending=controls.folderButton.fire('click');writeAllowed=false;await submit(activeDialog(),'Denied');await pending;assert.equal(calls.filter(call=>call.method==='POST').length,1);
''')


def test_all_four_name_actions_use_shared_modal_and_keep_existing_path_guards():
    source = (STATIC / 'files.js').read_text()
    assert 'window.prompt(' not in source
    assert source.count('await requestFileName(') == 4
    assert 'epoch !== ownerEpoch || !items.includes(item) || revision !== item.revision' in source
    assert 'destination !== moveDirectory' in source
    assert 'currentDirectory !== directory' in source
