"""The real setup presenter follows typed native flow, with legacy device compatibility."""
from pathlib import Path
import subprocess

from glass_drive_ui import server


def run_case(case):
    source=(Path(server.STATIC_DIR)/'control-plane.js').read_text()
    source=source[source.index('function showSetup(payload) {'):source.index('\nasync function submitProviderSetupInput')]
    script=r'''
import assert from 'node:assert/strict';
const ids=['provider-setup-panel','provider-setup-instructions','provider-setup-guidance','provider-setup-link',
'provider-setup-code','provider-setup-code-row','provider-setup-input-form','provider-setup-input',
'provider-setup-help','provider-setup-technical','restart-provider-setup','add-provider-account','connect-ai-advanced'];
const nodes=new Map(ids.map(id=>[id,{hidden:false,textContent:'',href:'',value:'',dataset:{},open:false}]));
globalThis.document={getElementById:id=>nodes.get(id),querySelectorAll:()=>[]};
let activeSetupAccount='acct_synthetic',status='';
const setProviderStatus=value=>status=value;
const availableProviderOptions=()=>[{provider:'codex'}];
const node=id=>nodes.get('provider-setup-'+id);
''' +source+'\n'+case
    subprocess.run(['node','--input-type=module','--eval',script],check=True,capture_output=True,text=True)


def test_typed_browser_flow_never_demands_device_code_or_opens_technical_fallback():
    run_case(r'''
node('technical').open=true;node('technical').dataset.autoOpened='true';
showSetup({provider:'codex',setup_flow:'browser',setup_url:'https://auth.openai.com/oauth/authorize?synthetic=true',
    instructions:'Native browser sign-in is waiting.',help_url:'https://chatgpt.com/#settings/Security',complete:false});
assert.equal(node('guidance').textContent,'Continue sign-in in your browser. Return here when you are done.');
assert.equal(node('technical').open,false);assert.equal(node('technical').dataset.autoOpened,undefined);
assert.equal(node('code-row').hidden,true);assert.equal(node('input-form').hidden,true);
assert.equal(node('help').hidden,true);assert.equal(node('link').hidden,false);assert.equal(status,'');
// The reviewed typed link is sufficient even if this native status has no raw text yet.
showSetup({provider:'codex',setup_flow:'browser',setup_url:'https://auth.openai.com/oauth/authorize?synthetic=true',complete:false});
assert.equal(node('guidance').textContent,'Continue sign-in in your browser. Return here when you are done.');
''')


def test_legacy_and_typed_device_payloads_keep_code_and_security_guidance():
    run_case(r'''
for(const setup_flow of [undefined,'device_code']){
showSetup({provider:'codex',setup_flow,setup_url:'https://auth.openai.com/codex/device',setup_code:'TEST-CODE',
    help_url:'https://chatgpt.com/#settings/Security',instructions:'Enter the one-time code.',complete:false});
assert.equal(node('guidance').textContent,'Open sign-in, then enter the code.');
assert.equal(node('code').textContent,'TEST-CODE');assert.equal(node('code-row').hidden,false);
assert.equal(node('help').hidden,false);assert.equal(node('technical').open,false);assert.equal(status,'');
}
''')


def test_unknown_or_incomplete_native_guidance_stays_truthful():
    run_case(r'''
for(const payload of [
{provider:'codex',setup_flow:'unknown',setup_url:'',instructions:'Native sign-in needs attention.'},
{provider:'codex',setup_flow:'browser',setup_url:'',instructions:'Native sign-in needs attention.'},
{provider:'codex',setup_url:'https://auth.openai.com/codex/device',instructions:'The native one-time code is unavailable.'}
]){
showSetup({...payload,complete:false});assert.equal(node('technical').open,true);
assert.equal(node('guidance').textContent,'Use the provider instructions below.');
assert.equal(status,'Sign-in details changed. Follow the technical details below.');
}
showSetup({provider:'codex',setup_flow:'unknown',setup_url:'',instructions:'',complete:false});
assert.equal(node('guidance').textContent,'Starting secure sign-in…');assert.equal(node('technical').open,false);
''')


def test_authorization_code_and_completed_routes_keep_their_existing_controls():
    run_case(r'''
showSetup({provider:'claude',setup_flow:'authorization_code',setup_url:'https://claude.com/cai/oauth/authorize?code=true',
input_required:true,instructions:'Paste the returned code.',complete:false});
assert.equal(node('input-form').hidden,false);assert.equal(node('guidance').textContent,'Open sign-in, then paste the code here.');
node('input').value='synthetic-code';
showSetup({provider:'codex',setup_flow:'browser',complete:true,status:'ready'});
assert.equal(node('panel').hidden,true);assert.equal(node('input').value,'');assert.equal(status,'Connected.');
''')
