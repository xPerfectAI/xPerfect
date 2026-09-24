import json
import base64
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from glass_drive_ui import server as server_module
from glass_drive_ui.server import create_app
from glass_drive_ui.runtime_client import RuntimeClient
from test_server import FakeRuntimeClient, _FakeOidcHumanAuth


def runtime_fixture():
    runtime=FakeRuntimeClient();calls=[]
    runtime.health_response['current_native_claude']={'available':True,'provider':'claude','execution_mode':'host'}
    runtime.current_native_claude_status=lambda: calls.append('GET') or {'state':'sign_in_required','message':'Sign in'}
    runtime.connect_current_native_claude=lambda: calls.append('POST') or {'state':'ready_to_try','status':'ready','account_id':'acct_current','complete':True}
    return runtime,calls


def test_current_native_facade_uses_explicit_post_and_typed_runtime_capability():
    runtime,calls=runtime_fixture();client=TestClient(create_app(runtime_client=runtime))
    assert client.get('/api/control-plane').json()['current_native_claude']=={'available':True}
    assert calls==[]
    assert client.get('/api/provider-accounts/current-native/claude').json()['state']=='sign_in_required'
    assert calls==['GET']
    result=client.post('/api/provider-accounts/current-native/claude')
    assert result.status_code==200 and result.json()['account_id']=='acct_current'
    assert calls==['GET','POST']


@pytest.mark.parametrize('capability',[None,True,{'available':'true'},{'available':True,'provider':'claude','execution_mode':'docker'}])
def test_missing_or_malformed_local_capability_never_connects(capability):
    runtime,calls=runtime_fixture();runtime.health_response['current_native_claude']=capability
    client=TestClient(create_app(runtime_client=runtime))
    assert client.get('/api/control-plane').json()['current_native_claude']=={'available':False}
    assert client.post('/api/provider-accounts/current-native/claude').status_code==409
    assert calls==[]


def test_current_native_facade_retains_session_and_csrf_controls(monkeypatch):
    runtime,calls=runtime_fixture()
    monkeypatch.setattr(server_module.HumanAuthGateway,'from_env',lambda:_FakeOidcHumanAuth())
    client=TestClient(create_app(runtime_client=runtime))
    route='/api/provider-accounts/current-native/claude'
    assert client.post(route).status_code==403
    client.cookies.set('glasshive_session','opaque-session')
    client.cookies.set('glasshive_csrf','synthetic-csrf')
    assert client.post(route).status_code==403
    assert client.post(route,headers={'Origin':'https://attacker.example.invalid','X-GlassHive-CSRF':'synthetic-csrf'}).status_code==403
    assert calls==[]
    response=client.post(route,headers={'Origin':'http://testserver','X-GlassHive-CSRF':'synthetic-csrf'})
    assert response.status_code==200,response.text
    assert calls==['POST']


def test_runtime_client_uses_fixed_paths_without_identity_or_auth_payload(monkeypatch):
    client=RuntimeClient('http://127.0.0.1:8766');calls=[]
    monkeypatch.setattr(client,'_request',lambda *args,**kwargs:calls.append((args,kwargs)) or {})
    client.current_native_claude_status();client.connect_current_native_claude()
    assert calls==[(('GET','/v1/provider-accounts/current-native/claude'),{}),(('POST','/v1/provider-accounts/current-native/claude'),{})]


def test_native_connection_ui_click_states_and_draft_preservation():
    node=shutil.which('node')
    assert node, 'Node is required for the UI state proof'
    path='data:text/javascript;base64,'+base64.b64encode((Path(server_module.STATIC_DIR)/'native-controls.js').read_bytes()).decode()
    code='''
import assert from 'node:assert/strict';
const {attachExistingClaudeConnection,currentClaudeConnectionView,selectConnectedClaudeAccount}=await import(MODULE);
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.hidden=false;this.disabled=false;this.value='';}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(){}
  addEventListener(name,fn){this.handlers[name]=fn;}
  dispatchEvent(event){this.event=event.type;}
}
globalThis.document={createElement:tag=>new Element(tag)};
const panel=new Element('div');const calls=[];let connected=null;
const api={capability:{available:true},postJson:async path=>{calls.push(['POST',path]);return calls.length===1?{state:'sign_in_required',complete:false}:{state:'ready_to_try',status:'ready',complete:true,account_id:'acct_current'};},getJson:async path=>{calls.push(['GET',path]);return {state:'ready_to_try'};},onConnected:async result=>{connected=result;}};
attachExistingClaudeConnection(panel,api);
let [button,message,help]=panel.children;
assert.equal(button.textContent,'Use existing Claude sign-in');assert.equal(help.hidden,true);assert.equal(calls.length,0);
await button.handlers.click();assert.equal(button.textContent,'Check again');assert.equal(help.hidden,false);assert.equal(help.open,undefined);
await button.handlers.click();assert.equal(button.textContent,'Use existing Claude sign-in');assert.equal(calls[1][0],'GET');assert.equal(connected,null);
await button.handlers.click();assert.equal(connected.account_id,'acct_current');assert.deepEqual(calls.map(c=>c[0]),['POST','GET','POST']);
assert.equal(currentClaudeConnectionView({available:false}).visible,false);
for(const state of ['cli_missing','identity_unavailable','different_auth_route','unavailable']) assert.equal(currentClaudeConnectionView({available:true},{state}).action,'check');
const selection=new Element('select');selection.options=[{value:'acct_current'}];selection.value='acct_previous';
const policy={value:'personal_preferred'};const draft={goal:'Keep this exact user draft',profile:'new:codex-cli',model:'configured-model'};
const args={connected:{profile:'claude-code',account_id:'acct_current'},workspaceValue:draft.profile,accountSelect:selection,policySelect:policy};
assert.equal(selectConnectedClaudeAccount(args),false);assert.equal(selection.value,'acct_previous');assert.equal(draft.profile,'new:codex-cli');
assert.equal(selectConnectedClaudeAccount({...args,workspaceValue:'new:claude-code'}),true);assert.equal(policy.value,'personal_required');assert.equal(selection.value,'acct_current');assert.equal(draft.goal,'Keep this exact user draft');assert.equal(draft.model,'configured-model');
console.log('native UI click states and draft preservation pass');
'''.replace('MODULE',json.dumps(path))
    result=subprocess.run([node,'--input-type=module'],input=code,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr


def test_pending_native_input_reveals_the_existing_renderer_from_live_state():
    root = Path(server_module.STATIC_DIR)
    native_source = (root / 'native-controls.js').read_text()
    app_source = (root / 'app.js').read_text()
    watch_source = (root / 'watch.js').read_text()
    assert '/api/worker/' in native_source
    assert '/v1/workers/' not in native_source
    assert 'updateLive(data?.native_control || null)' in app_source
    assert "'More · response needed'" in native_source
    assert 'attachNativeControls' in watch_source

    node = shutil.which('node')
    assert node, 'Node is required for the pending native input proof'
    path = 'data:text/javascript;base64,' + base64.b64encode(native_source.encode()).decode()
    code = r'''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(MODULE);
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.dataset={};this.style={};this.isConnected=true;this.open=false;this.hidden=false;this.disabled=false;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(){}
  addEventListener(name,fn){this.handlers[name]=fn;}
  closest(selector){return selector==='details'?this.parentDetails:null;}
  querySelector(selector){return selector===':scope > summary'?this.summary:null;}
}
globalThis.setInterval=()=>0; globalThis.clearInterval=()=>{};
globalThis.document={createElement:tag=>new Element(tag)};
const parent=new Element('details'); const heading=new Element('summary'); heading.textContent='More'; parent.summary=heading;
const mount=new Element('div'); mount.parentDetails=parent;
const controller=attachNativeControls(mount,'worker-native',{getJson:async()=>({}),postJson:async()=>({status:'permission_submitted'})});
controller.updateLive({run_id:'run-native',attempt_id:'attempt-native',available:true,pending_requests:[{request_id:'request-native',method:'session/request_permission',request:{toolCall:{title:'Run command',rawInput:'echo exact command'},options:[{optionId:'allow',name:'Allow'}]}}]});
assert.equal(parent.open,true); assert.equal(heading.textContent,'More · response needed');
const panel=mount.children[0]; assert.equal(panel.open,true); assert.equal(panel.children[2].children.length,1);
const requestSection=panel.children[2].children[0]; assert.equal(requestSection.children.some((child)=>child.tag==='pre' && child.textContent==='echo exact command'),true);
assert.equal(panel.children[1].textContent,'Grok is waiting for your response.');
assert.equal(panel.children[4].disabled,false); assert.equal(panel.children[5].disabled,false);
console.log('pending native input live renderer proof pass');
'''.replace('MODULE', json.dumps(path))
    result = subprocess.run([node, '--input-type=module'], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_native_renderer_announces_each_new_request_once_with_its_deadline():
    root = Path(server_module.STATIC_DIR)
    native_source = (root / 'native-controls.js').read_text()
    node = shutil.which('node')
    assert node, 'Node is required for the pending native input proof'
    path = 'data:text/javascript;base64,' + base64.b64encode(native_source.encode()).decode()
    code = r'''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(MODULE);
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.isConnected=true;this.open=false;this.disabled=false;this.style={};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(){}
  addEventListener(name,fn){this.handlers[name]=fn;}
  closest(){return null;}
}
globalThis.setInterval=()=>0; globalThis.clearInterval=()=>{};
globalThis.document={createElement:tag=>new Element(tag)};
const mount=new Element('div'); const calls=[];
let getCount=0;
const controller=attachNativeControls(mount,'worker-native',{getJson:async()=>{getCount+=1;return {};},postJson:async()=>({status:'permission_submitted'}),onResponseNeeded:(value)=>calls.push(value)});
const request={request_id:'request-1',method:'session/request_permission',expires_at:1700000060,request:{toolCall:{title:'Run command'},options:[{optionId:'allow',name:'Allow'}]}};
const live=(pending)=>controller.updateLive({run_id:'run',attempt_id:'attempt',available:true,pending_requests:pending});
live([request]); live([request]);
assert.deepEqual(calls,[{waiting:true,isNew:true},{waiting:true,isNew:false}]);
const section=mount.children[0].children[2].children[0];
const deadline=section.children.find((child)=>child.tag==='p');
assert.equal(deadline.textContent,`Respond by ${new Date(1700000060*1000).toLocaleTimeString()}, or this request expires.`);
live([{...request,request_id:'request-long',request:{...request.request,toolCall:{title:'Run command',rawInput:{command:'x'.repeat(400)}}}}]);
const long=mount.children[0].children[2].children[0].children.find((child)=>child.tag==='pre');
assert.equal(long.style.whiteSpace,'pre-wrap'); assert.equal(long.style.overflowWrap,'anywhere');
live([]); assert.deepEqual(calls.at(-1),{waiting:false,isNew:false});
live([{...request,request_id:'request-2'}]); assert.deepEqual(calls.at(-1),{waiting:true,isNew:true});
// Long native context wraps inside the panel so every offered option stays reachable.
assert.equal(section.style.minInlineSize,'0');
const context=section.children.find((child)=>child.tag==='pre'); assert.equal(context,undefined);
controller.updateLive(null); assert.deepEqual(calls.at(-1),{waiting:false,isNew:false});
// With no running turn there is nothing to refresh; the state GET would only fail.
mount.children[0].open=true; await controller.refresh(); assert.equal(getCount,0);
console.log('watch native request announcement proof pass');
'''.replace('MODULE', json.dumps(path))
    result = subprocess.run([node, '--input-type=module'], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_native_permission_shows_one_time_choice_before_persistent_grants():
    node = shutil.which('node')
    assert node, 'Node is required for the native permission UI proof'
    path = 'data:text/javascript;base64,' + base64.b64encode(
        (Path(server_module.STATIC_DIR) / 'native-controls.js').read_bytes()
    ).decode()
    code = r'''
import assert from 'node:assert/strict';
const {attachNativeControls}=await import(MODULE);
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.open=false;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(){}
  addEventListener(name,handler){this.handlers[name]=handler;}
  closest(){return null;}
}
globalThis.setInterval=()=>0; globalThis.clearInterval=()=>{};
globalThis.document={createElement:tag=>new Element(tag)};
const mount=new Element('div'); const calls=[];
const controller=attachNativeControls(mount,'worker',{getJson:async()=>({}),postJson:async(_path,payload)=>{calls.push(payload);return {status:'permission_submitted'};}});
controller.updateLive({run_id:'run',attempt_id:'attempt',available:true,pending_requests:[{
  request_id:'request',method:'session/request_permission',request:{options:[
    {optionId:'always',kind:'allow_always',name:'Yes, and do not ask again'},
    {optionId:'once',kind:'allow_once',name:'Yes, proceed'},
    {optionId:'reject-once',kind:'reject_once',name:'No, not this time'},
    {optionId:'reject-always',kind:'reject_always',name:'Never allow'},
  ]}}]});
const section=mount.children[0].children[2].children[0];
assert.equal(section.children[1].textContent,'Yes, proceed');
assert.equal(section.children[2].textContent,'No, not this time');
const more=section.children[3];
assert.equal(more.tag,'details'); assert.equal(more.open,false);
assert.equal(more.children[0].textContent,'More choices');
assert.deepEqual(more.children.slice(1).map(x=>x.textContent),['Yes, and do not ask again','Never allow']);
assert.equal(section.children.at(-1).textContent,'Cancel request');
section.children[1].handlers.click();
assert.equal(calls[0].option_id,'once');
assert.equal(calls[0].request_id,'request');
'''.replace('MODULE', json.dumps(path))
    result = subprocess.run([node, '--input-type=module'], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_watch_states_a_typed_native_stop_instead_of_a_generic_failure():
    node = shutil.which('node')
    assert node, 'Node is required for the Watch failure proof'
    presenter = (Path(server_module.STATIC_DIR) / 'delivery-presenter.js').as_uri()
    code = f'''
import assert from 'node:assert/strict';
const {{watchOutputModel}} = await import({json.dumps(presenter)});
const raw = 'grok-build exited with code 2: Grok stopped after you declined its request.';
const typed = watchOutputModel({{latest_run:{{state:'failed',failure_structured:1,failure_user_message:'Grok stopped after you declined its request.'}},latest_output:raw,worker:{{state:'ready'}}}});
assert.equal(typed.summary, 'Grok stopped after you declined its request.');
assert.equal(typed.label, 'Needs attention'); assert.equal(typed.result, ''); assert.equal(typed.technical, raw);
const unclassified = watchOutputModel({{latest_run:{{state:'failed',failure_structured:0,failure_user_message:'could not classify'}},latest_output:raw,worker:{{state:'ready'}}}});
assert.equal(unclassified.summary, 'The run did not complete. Review the details before trying again.');
const running = watchOutputModel({{latest_run:{{state:'running',failure_structured:1,failure_user_message:'stale'}},worker:{{state:'running'}}}});
assert.equal(running.summary, 'The worker is working on your project.');
const closed = watchOutputModel({{latest_run:{{state:'failed',failure_structured:1,failure_user_message:'This work could not finish. You can retry it.'}},worker:{{state:'terminated'}}}});
assert.equal(closed.summary, 'This work could not finish. The workspace is closed. Start new work from Workspaces.');
assert.match(closed.technical, /You can retry it/);
'''
    result = subprocess.run([node, '--input-type=module'], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_background_watch_tab_title_says_grok_needs_the_owner():
    source = (Path(server_module.STATIC_DIR) / 'watch.js').read_text()
    start = source.index('function syncDocumentTitle(')
    title_fn = source[start:source.index('\nfunction ownerResponseNeeded', start)]
    # The live poll passes typed pending owner input, never text matching.
    assert 'syncDocumentTitle(currentProjectTitle, worker.name, ownerResponseNeeded(data));' in source
    start = source.index('function ownerResponseNeeded(')
    needed_fn = source[start:source.index('\n}\n', start) + 2]
    node = shutil.which('node')
    assert node, 'Node is required for the Watch title proof'
    code = title_fn + needed_fn + r'''
const document = {title: ''}; globalThis.document = document;
const pending = {native_control: {read_only: false, pending_requests: [{request_id: 'r', method: 'session/request_permission'}]}};
syncDocumentTitle('Project', 'Worker', ownerResponseNeeded(pending));
if (document.title !== 'Response needed · xPerfect | Worker - Project') throw new Error(document.title);
syncDocumentTitle('Project', 'Worker', ownerResponseNeeded({native_control: {...pending.native_control, read_only: true}}));
if (document.title !== 'xPerfect | Worker - Project') throw new Error(document.title);
syncDocumentTitle('Project', 'Worker', ownerResponseNeeded({native_control: null}));
if (document.title !== 'xPerfect | Worker - Project') throw new Error(document.title);
'''
    result = subprocess.run([node, '--input-type=module'], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_watch_steer_hint_recovers_after_a_transient_outage():
    source = (Path(server_module.STATIC_DIR) / 'watch.js').read_text()
    def fn(name, following):
        start = source.index('function ' + name)
        return source[start:source.index(following, start)]
    code = fn('syncSteerAvailability(', '\nfunction autoResizeSteerInput') + fn('showWorkspaceUnavailable(', '\nasync function refresh') + r'''
const node = () => ({textContent: '', hidden: false, disabled: false, placeholder: '', dataset: {}});
const guidancePrimary = node(), guidanceQueue = node(), steerInput = node(), sendButton = node(), title = node(), subtitle = node(),
  statePill = node(), overlay = node(), overlayTitle = node(), overlayDetail = node(), overlayLabel = node(), stageResultText = node(), runToggleButton = node();
const stage = {dataset: {}}, signInLink = node();
const window = {location: {pathname: '/watch/wrk_one', search: '?project_id=prj_one', hash: ''}};
    let currentDisplayState, currentRunState, currentDesktopAvailable, currentDeliverable, currentSummary, currentFullOutput, currentResultText, actionFailure;
const clearAttachedView = () => {}, renderOutputContent = () => {}, syncResultActions = () => {}, syncArtifactList = () => {}, syncSendAffordance = () => {};
showWorkspaceUnavailable(502);
if (guidancePrimary.textContent !== 'Workspace status is unavailable right now.') throw new Error('5xx: ' + guidancePrimary.textContent);
syncSteerAvailability('running');
if (guidancePrimary.textContent !== 'Send redirects now' || steerInput.disabled) throw new Error('recovered: ' + guidancePrimary.textContent);
showWorkspaceUnavailable(403);
if (guidancePrimary.textContent !== 'This workspace is unavailable to this account.') throw new Error('403: ' + guidancePrimary.textContent);
if (!signInLink.hidden) throw new Error('403 must not offer sign-in as an access bypass');
showWorkspaceUnavailable(401);
if (guidancePrimary.textContent !== 'Your sign-in ended. Sign in again to continue.') throw new Error('401: ' + guidancePrimary.textContent);
if (overlayTitle.textContent !== 'Sign in to continue' || statePill.textContent !== 'Sign-in needed') throw new Error('401 status');
if (steerInput.placeholder !== 'Sign in to use this workspace' || !sendButton.disabled) throw new Error('401 composer');
if (signInLink.hidden || signInLink.href !== '/login?return_to=%2Fwatch%2Fwrk_one%3Fproject_id%3Dprj_one') throw new Error('401 recovery link');
syncSteerAvailability('terminated');
if (!guidancePrimary.textContent.startsWith('This workspace is closed.')) throw new Error('closed: ' + guidancePrimary.textContent);
'''
    node = shutil.which('node')
    assert node, 'Node is required for the Watch recovery proof'
    result = subprocess.run([node, '--input-type=module'], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_watch_places_required_native_input_above_the_growing_file_list():
    html = (Path(server_module.STATIC_DIR) / 'watch.html').read_text()
    panel = html[html.index('id="result-panel"'):html.index('id="latest-output-human"')]
    # Files accumulate per turn; a pending request must not fall below them.
    assert panel.index('id="result-actions"') < panel.index('id="native-controls"') < panel.index('id="artifact-list"')
