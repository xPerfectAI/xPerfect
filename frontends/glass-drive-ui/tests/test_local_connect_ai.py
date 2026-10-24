"""Local client setup reuses the launcher's existing stdio MCP command."""
import json
import subprocess
from pathlib import Path
from fastapi.testclient import TestClient
from glass_drive_ui.server import create_app
from test_server import FakeRuntimeClient

STATIC=Path(__file__).parents[1]/'src/glass_drive_ui/static'


def test_local_commands_are_ready_without_oauth_and_carry_no_credentials(monkeypatch):
    command=['python3','/opt/xperfect/xperfect','mcp','--state-dir','/tmp/synthetic-xperfect-state']
    monkeypatch.setenv('XPERFECT_LOCAL_MCP_COMMAND',json.dumps(command))
    monkeypatch.setenv('XPERFECT_LOCAL_CHECKOUT','/opt/xperfect')
    monkeypatch.setenv('XPERFECT_LOCAL_MCP_URL','http://127.0.0.1:8767/mcp/')
    client=TestClient(create_app(runtime_client=FakeRuntimeClient()))
    result=client.get('/api/connect-ai')
    assert result.status_code==200
    data=result.json()
    assert data['configuration_status']=='ready'
    assert set(data['clients'])=={'codex','claude'}
    assert data['mcp_url']=='http://127.0.0.1:8767/mcp/'
    assert 'No sign-in' in data['configuration_note']
    for name,entry in data['clients'].items():
        assert entry['transport']=='stdio'
        assert '--state-dir /tmp/synthetic-xperfect-state' in entry['add_command']
        assert '8767' not in entry['add_command']
        assert 'callback_uri' not in entry
        assert entry['setup_prompt'] in data['guided_prompt']
    assert '--client-id' not in data['guided_prompt']
    assert '--oauth' not in data['guided_prompt']


def test_local_manual_ui_has_both_native_commands_and_no_sign_in_wall():
    source=(STATIC/'control-plane.js').read_text()
    start=source.index('function renderConnectAi()')
    end=source.index('\nfunction ',start+10)
    actual=source[start:end]
    script=r'''
import assert from 'node:assert/strict';
const elements=new Map(),commands=[];
const el=()=>({textContent:'',hidden:false,disabled:false,children:[],replaceChildren(...children){this.children=children;},querySelector(){return null;}});
const document={getElementById:id=>{if(!elements.has(id))elements.set(id,el());return elements.get(id);}};
const node=(tag,cls,text)=>({...el(),textContent:text});
const copyText=()=>{},connectAiClientCard=(title,steps)=>({title,steps});
const commandRow=(title,text)=>{commands.push({title,text});return {querySelector:()=>({textContent:text})};};
const referenceRow=commandRow;
let connectAiLoadError='';
let connectAi={configuration_status:'ready',mcp_url:'http://127.0.0.1:8767/mcp/',server_name:'local',guided_prompt:'Connect locally',source:{},clients:{
 codex:{transport:'stdio',add_command:'codex mcp add local -- xperfect mcp',card_note:'No sign-in is needed.'},
 claude:{transport:'stdio',add_command:'claude mcp add local -- xperfect mcp',card_note:'No sign-in is needed.'}}};
'''+actual+r'''
renderConnectAi();
assert.ok(commands.some(row=>row.title==='Codex · Add server'&&row.text.includes('codex mcp add')));
assert.ok(commands.some(row=>row.title==='Claude Code · Add server'&&row.text.includes('claude mcp add')));
assert.ok(!commands.some(row=>row.title.includes('Sign in')));
assert.equal(document.getElementById('connect-ai-server-address').hidden,true);
assert.equal(document.getElementById('connect-ai-registration-details').hidden,true);
assert.match(document.getElementById('connect-ai-manual-intro').textContent,/No sign-in/);
assert.equal(document.getElementById('copy-connect-ai-prompt').disabled,false);
assert.ok(document.getElementById('connect-ai-clients').children.every(row=>row.steps.includes('No sign-in')));
connectAi={...connectAi,clients:{codex:{config_toml:'[mcp_servers.hosted]',login_command:'codex mcp login hosted'}}};
commands.length=0;renderConnectAi();
assert.equal(document.getElementById('connect-ai-server-address').hidden,false);
assert.equal(document.getElementById('connect-ai-registration-details').hidden,false);
assert.ok(commands.some(row=>row.title==='Codex · 2. Sign in'&&row.text==='codex mcp login hosted'));
'''
    result=subprocess.run(['node','--input-type=module'],input=script,text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
