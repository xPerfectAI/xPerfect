"""The existing Chat picker retains distinct owner-declared model choices."""
from pathlib import Path
import subprocess
from glass_drive_ui.server import STATIC_DIR


def test_connected_bedrock_picker_shows_both_exact_declared_models():
    source = (Path(STATIC_DIR) / "conversation.js").read_text()
    function = source[source.index("async function loadAssistants()"):source.index("$('composer').addEventListener('submit'")]
    script = r'''
import assert from 'node:assert/strict';
class Element { constructor(){this.children=[];this.hidden=false;this.disabled=false;this.value='';} replaceChildren(...items){this.children=items;} }
const elements=new Map(); const $=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
function node(tag,text){const result=new Element();result.textContent=text;return result;}
const conversationId='';const latest=null;const librarySelectionDirty=false;let libraryOwnerReady=false;
const renderAccountIdentity=()=>{};const chatLibrary={setOwnerScope(){}};
const connectedUnavailableMessage=()=>'';const showError=error=>{throw error;};
async function api(path){return path==='/api/bootstrap'?{provider_accounts:[{account_id:'account-a',provider:'claude',status:'ready',label:'Cloud account'}],user_preferences:{}}:
{default:{available:false},connected_account:{available:true,supported_account_ids:['account-a'],supported_accounts:[{account_id:'account-a',profile:'claude-code',auth_method:'bedrock',models:[{id:'declared-a',native_model:'native-a'},{id:'declared-b',native_model:'native-b'}]}]}};}
''' + function + r'''
await loadAssistants();
const choices=$('conversation-assistant').children;
assert.deepEqual(choices.map(item=>item.value),['declared-a','declared-b']);
assert.deepEqual(choices.map(item=>item._accountId),['account-a','account-a']);
assert.match(choices[0].textContent,/Bedrock · native-a/);
assert.match(choices[1].textContent,/Bedrock · native-b/);
assert.equal($('conversation-assistant').value,'declared-a');
assert.equal($('send').disabled,false);
'''
    subprocess.run(["node", "--input-type=module", "--eval", script], check=True, text=True, capture_output=True)
