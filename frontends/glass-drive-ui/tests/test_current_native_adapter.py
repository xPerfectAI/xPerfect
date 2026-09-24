"""Exercise the real control-plane GET adapter with its native-controls consumer."""
import base64
import json
from pathlib import Path
import re
import shutil
import subprocess

from glass_drive_ui import server


def test_check_again_fetches_authenticated_url_through_control_plane_adapter():
    root = Path(server.STATIC_DIR)
    def module_url(path):
        source = path.read_text()
        source = re.sub(r"from '([.]/[^']+)'", lambda match: 'from ' + json.dumps(module_url(root / match[1].split('?')[0])), source)
        if path.name == 'control-plane.js':
            source += '\nexport function mountFixture(dependencies, data) { api = dependencies; controlPlane = data; renderProviderAccounts(); }\n'
        return 'data:text/javascript;base64,' + base64.b64encode(source.encode()).decode()
    code = r'''
import assert from 'node:assert/strict';
const {mountFixture}=await import(MODULE);
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.dataset={};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  before(element){elements.set(element.id,element);}
  setAttribute(){}
  addEventListener(name,fn){this.handlers[name]=fn;}
}
const elements=new Map([['provider-account-list',new Element('div')]]);
globalThis.document={createElement:tag=>new Element(tag),getElementById:id=>elements.get(id)||null,querySelector:()=>null};
globalThis.localStorage={getItem:()=>null};
let requested=[]; let posts=0; let fail=false;
globalThis.fetch=async url=>{
  requested.push(url);
  return {ok:!fail,json:async()=>({state:'ready_to_try'})};
};
mountFixture({withAuth:path=>`/authenticated${path}`,responseMessage:async()=> 'Synthetic check unavailable',postJson:async()=>{posts++;return {state:'sign_in_required',complete:false};}}, {current_native_claude:{available:true},provider_accounts:[]});
const [button,message]=elements.get('existing-claude-connection').children;
assert.equal(requested.length,0);assert.equal(posts,0);
await button.handlers.click();assert.equal(posts,1);assert.equal(button.textContent,'Check again');
await button.handlers.click();
assert.deepEqual(requested,['/authenticated/api/provider-accounts/current-native/claude']);
assert.equal(button.textContent,'Use existing Claude sign-in');assert.equal(posts,1);
await button.handlers.click();fail=true;await button.handlers.click();
assert.equal(message.textContent,'Synthetic check unavailable');assert.equal(button.disabled,false);
console.log('real control-plane Check again adapter pass');
'''.replace('MODULE', json.dumps(module_url(root / 'control-plane.js')))
    result = subprocess.run([shutil.which('node'), '--input-type=module'], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
