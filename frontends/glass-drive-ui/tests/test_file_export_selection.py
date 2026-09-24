"""Large read-only Watch selections use the native download body, never the URL."""
import json
from pathlib import Path
import subprocess


def test_watch_export_submits_every_selected_id_in_native_form():
    module = Path(__file__).parents[1] / 'src/glass_drive_ui/static/files.js'
    script = r'''
import assert from 'node:assert/strict';
class Element {
  constructor(){this.children=[];this.listeners={};this.textContent='';}
  addEventListener(name,fn){this.listeners[name]=fn;}
  replaceChildren(...children){this.children=children;}
  append(...children){this.children.push(...children);}
  appendChild(child){this.children.push(child);}
  setAttribute(){} removeAttribute(){}
  submit(){this.submitted=true;} remove(){}
}
const controls={};
const opts=new Proxy({workerId:'worker-test',csrf:()=> 'csrf-test',canUpload:()=>false,onAccess:()=>{}},
 {get(target,name){return name in target?target[name]:(controls[name]??=new Element());}});
Object.defineProperty(globalThis,'navigator',{value:{platform:'test'},configurable:true});
globalThis.document={createElement:()=>new Element(),body:new Element()};
globalThis.setTimeout=()=>0;
const ids=Array.from({length:2000},(_,i)=>'fen_'+i.toString(16).padStart(32,'0'));
globalThis.fetch=async()=>({ok:true,status:200,json:async()=>({can_write:false,items:ids.map((file_id,i)=>({file_id,name:`file-${i}.txt`,path:`file-${i}.txt`,size_bytes:0,revision:'rev'}))})});
const files=createWorkspaceFiles(opts);await files.open();
assert.equal(controls.exportLink.disabled,true);
controls.selectVisible.listeners.click();assert.equal(controls.exportLink.disabled,false);
controls.exportLink.listeners.click({preventDefault(){}});
assert.equal(document.body.children.length,1);
const form=document.body.children[0];
assert.equal(form.submitted,true);assert.equal(form.method,'post');
assert.equal(form.action,'/api/workspace/worker-test/files/export');
assert.deepEqual(form.children.filter(x=>x.name==='file_ids').map(x=>x.value),ids);
assert.equal(form.children.find(x=>x.name==='workspace_export_csrf').value,'csrf-test');
controls.clearSelection.listeners.click();assert.equal(controls.exportLink.disabled,true);
controls.exportLink.listeners.click({preventDefault(){}});assert.equal(document.body.children.length,1);
console.log('pass');
'''
    result = subprocess.run(['node','--input-type=module','--eval',
        f'import {{createWorkspaceFiles}} from {json.dumps(module.as_uri())};\n'+script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'pass'
