"""Causal checks for actual account and Library UI owners. Real Chrome QA is separate."""
import base64
import json
from pathlib import Path
import re
import subprocess

from glass_drive_ui import server

STATIC = Path(server.STATIC_DIR)

FIXTURE = r'''
export function configureFixture({dependencies, data, workspaces, noRender = false} = {}) {
  api = dependencies;
  controlPlane = data || null;
  if (workspaces) loadWorkspaceChoices = workspaces;
  if (noRender) {
    renderProviderOptionControls = () => fixtureCalls.push('account-options');
    renderProviderAccounts = () => fixtureCalls.push('accounts');
    renderConnections = () => fixtureCalls.push('connections');
    renderLibrary = () => fixtureCalls.push('library');
    renderLibraryRequestWorkspaceOptions = () => {};
    renderSchedules = () => fixtureCalls.push('schedules');
    renderConnectAi = () => fixtureCalls.push('connect-ai');
    renderGrokModelChoice = () => fixtureCalls.push('models');
    renderCapabilityReviewBanner = () => {};
    restoreUncertainScheduleRunKeys = () => {};
    restoreCapabilityReviewFromCatalog = () => {};
    reconcileCapabilityReview = async () => {};
    loadWebhookDestinations = async () => {};
    resumePendingScheduleRefreshes = () => {};
  }
}
export function configureLibraryFixture(data, workspaces, dependencies) {
  controlPlane = data; workspaceCatalog = {items: workspaces}; api = dependencies;
}
export function renderLibraryFixture() { renderLibrary(); }
export function renderProvidersFixture() { renderProviderOptionControls(); }
export const submitLibraryFixture = submitLibraryRequest;
export const announceFixture = announceConnectedAccount;
export const loadFixture = loadControlPlane;
export const fixtureCalls = [];
export const fixtureState = () => ({controlPlane, workspaceCatalog, oneOffWorkspaceCatalog,
  grokModels, scheduleLoadError, accountConnectionIntent, libraryRequestPending});
'''


def module_url(path):
    source = path.read_text()
    source = re.sub(
        r"from '([.]/[^']+)'",
        lambda match: 'from ' + json.dumps(module_url(STATIC / match[1].split('?')[0])),
        source,
    )
    if path.name == 'control-plane.js':
        source += FIXTURE
    return 'data:text/javascript;base64,' + base64.b64encode(source.encode()).decode()


SHIM = r'''
import assert from 'node:assert/strict';
const ui = await import(MODULE);
const tick = async () => { for (let i=0; i<16; i++) await Promise.resolve(); };
const deferred = () => {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.dataset={};this.attributes={};this.hidden=false;this._value='';this.classList={add(){},remove(){},toggle(){}};}
  append(...children){for(const child of children){if(child && typeof child==='object')child.parent=this;this.children.push(child);}}
  appendChild(child){this.append(child);return child;}
  replaceChildren(...children){this.children=[];this.append(...children);if(this.tag==='select')this._value=this.options[0]?.value || '';}
  setAttribute(name,value){this.attributes[name]=String(value);if(name==='hidden')this.hidden=true;}
  addEventListener(name,fn){this.handlers[name]=fn;}
  closest(tag){return this.parent?.tag===tag?this.parent:null;}
  querySelector(){return null;}
  focus(){this.focused=true;}
  get value(){return this._value;}
  set value(value){this._value=String(value);}
  get options(){return this.children.flatMap(child=>child.tag==='optgroup'?child.children:[child]);}
}
const elements = new Map();
const put = (id,tag='div') => {const e=new Element(tag);elements.set(id,e);return e;};
let query = () => null;
globalThis.document={createElement:tag=>new Element(tag),getElementById:id=>elements.get(id)||null,querySelector:selector=>query(selector),querySelectorAll:()=>[]};
globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
globalThis.sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
globalThis.CustomEvent=class {constructor(type,options){this.type=type;this.detail=options?.detail;}};
const events=[],views=[];
globalThis.window={dispatchEvent:event=>events.push(event),setTimeout:()=>1,clearTimeout(){},location:{assign:value=>{window.destination=value;}},confirm:()=>true};
const response = value => ({ok:true,json:async()=>value,text:async()=>JSON.stringify(value)});
const dependencies={withAuth:value=>value,responseMessage:async()=> 'Synthetic request unavailable',setView:value=>views.push(value),postJson:async()=>({}),deleteJson:async()=>({})};
'''


def run(script):
    code = SHIM.replace('MODULE', json.dumps(module_url(STATIC / 'control-plane.js'))) + script
    result = subprocess.run(['node', '--input-type=module'], input=code, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_accounts_render_without_waiting_for_or_failing_with_unrelated_catalogs():
    run(r'''
const workspace=deferred(), oneOff=deferred(), schedules=deferred(), models=deferred(), help=deferred(), resources=deferred();
ui.configureFixture({dependencies,noRender:true,workspaces:kind=>kind==='ephemeral'?oneOff.promise:workspace.promise});
globalThis.fetch=path=>path.includes('/api/control-plane?sections=resources')?resources.promise
 : path.startsWith('/api/control-plane')?Promise.resolve(response({me:{tenant_id:'t',user_id:'u'},provider_accounts:[{account_id:'a'}]}))
 : path.includes('recurring-schedules')?schedules.promise:path.includes('native-models')?models.promise:help.promise;
await ui.loadFixture();
assert.equal(ui.fixtureCalls.filter(value=>value==='accounts').length,1);
assert.equal(ui.fixtureState().controlPlane.provider_accounts[0].account_id,'a');
workspace.reject(new Error('Workspace catalog unavailable'));oneOff.reject(new Error('One-off catalog unavailable'));
schedules.reject(new Error('Schedule catalog unavailable'));models.reject(new Error('Models unavailable'));help.reject(new Error('Help unavailable'));
await tick();
assert.equal(ui.fixtureState().workspaceCatalog.unavailable,true);
assert.equal(ui.fixtureState().grokModels.status,'catalog_unavailable');
assert.equal(ui.fixtureState().scheduleLoadError,'Schedule catalog unavailable');
assert.equal(ui.fixtureState().controlPlane.provider_accounts[0].account_id,'a');
''')


def test_late_control_plane_read_cannot_restore_a_previous_owner():
    run(r'''
const old=deferred(), current=deferred();let controlReads=0;
ui.configureFixture({dependencies,noRender:true,workspaces:async()=>({items:[]})});
globalThis.fetch=path=>path.startsWith('/api/control-plane')&&!path.includes('sections=resources')?(controlReads++===0?old.promise:current.promise):Promise.resolve(response({items:[],models:[]}));
const a=ui.loadFixture(), b=ui.loadFixture();
current.resolve(response({me:{tenant_id:'t',user_id:'new'},provider_accounts:[{account_id:'new-account'}]}));await b;
old.resolve(response({me:{tenant_id:'t',user_id:'old'},provider_accounts:[{account_id:'old-account'}]}));await a;await tick();
assert.equal(ui.fixtureState().controlPlane.me.user_id,'new');
assert.equal(ui.fixtureCalls.filter(value=>value==='accounts').length,1);
''')


def test_resource_read_cannot_replace_account_authority_or_merge_a_foreign_owner():
    run(r'''
const resources=[];
ui.configureFixture({dependencies,noRender:true,workspaces:async()=>({items:[]})});
const identity={tenant_id:'t',user_id:'owner'};
globalThis.fetch=path=>{
 if(path.includes('/api/control-plane?sections=resources')){const read=deferred();resources.push(read);return read.promise;}
 if(path.startsWith('/api/control-plane'))return Promise.resolve(response({me:identity,provider_accounts:[{account_id:'exact-account'}],provider_options:[{provider:'codex',methods:['subscription']}],sections:{provider_accounts:'ready'}}));
 return Promise.resolve(response({items:[],models:[]}));
};
await ui.loadFixture();assert.equal(resources.length,1);
resources[0].resolve(response({me:identity,connections:[{connection_id:'tool'}],library:[{library_id:'cap'}],
 provider_accounts:[{account_id:'wrong-account'}],provider_options:[{provider:'wrong'}],sections:{connections:'ready',library:'ready',provider_accounts:'unavailable'}}));
await tick();let state=ui.fixtureState().controlPlane;
assert.equal(state.provider_accounts[0].account_id,'exact-account');assert.equal(state.provider_options[0].provider,'codex');assert.equal(state.sections.provider_accounts,'ready');
assert.equal(state.connections[0].connection_id,'tool');assert.equal(state.library[0].library_id,'cap');
await ui.loadFixture();resources[1].resolve(response({me:{tenant_id:'t',user_id:'foreign'},connections:[{connection_id:'foreign-tool'}],library:[{library_id:'foreign-cap'}],sections:{connections:'ready',library:'ready'}}));
await tick();state=ui.fixtureState().controlPlane;
assert.equal(state.me.user_id,'owner');assert(!state.connections?.some(item=>item.connection_id==='foreign-tool'));assert(!state.library?.some(item=>item.library_id==='foreign-cap'));
''')


def test_library_grant_read_ignores_an_out_of_order_workspace_response():
    run(r'''
const list=put('library-list'),empty=put('library-empty');
const requests=[],posts=[],deletes=[];
ui.configureLibraryFixture({library:[{library_id:'cap',stable_id:'stable',version:'1',activation_status:'ready',manifest:{label:'Useful tool',activatable:true},scopes:['read']}],provider_accounts:[]},
 [{worker_id:'a',name:'Workspace A'},{worker_id:'b',name:'Workspace B'}],
 {...dependencies,postJson:async(path,body)=>{posts.push({path,body});return {change_id:'change',confirmation_token:'synthetic'};},deleteJson:async path=>deletes.push(path)});
globalThis.fetch=path=>{const item=deferred();requests.push({path,...item});return item.promise;};
ui.renderLibraryFixture();
const grant=list.children[0].children.find(child=>child.className==='library-grant');
const [select,button]=grant.children;
select.value='a';const a=select.handlers.change();select.value='b';const b=select.handlers.change();
assert.equal(button.disabled,true);
requests[1].resolve(response({items:[]}));await b;assert.equal(button.textContent,'Add to workspace');
requests[0].resolve(response({items:[{library_id:'cap',grant_id:'grant-a'}]}));await a;
assert.equal(button.textContent,'Add to workspace');await button.handlers.click();
assert.equal(deletes.length,0);assert.equal(posts[0].body.target_id,'b');assert.equal(posts[0].body.change_type,'library_enable');
''')


def test_library_submission_is_single_flight_and_keeps_text_on_failure():
    run(r'''
const selected=put('library-request-workspace','select');selected.value='a';
const text=put('library-request-text','textarea');text.value='Add the documented skill';const status=put('library-request-status');
const submit=new Element('button'), form={querySelector:()=>submit};const pending=deferred();let count=0;
ui.configureLibraryFixture({},[{worker_id:'a',watch_url:'/watch/a'}],{...dependencies,postJson:()=>{count++;return pending.promise;}});
const event={preventDefault(){},currentTarget:form};
const first=ui.submitLibraryFixture(event);await ui.submitLibraryFixture(event);
assert.equal(count,1);assert.equal(submit.disabled,true);pending.reject(new Error('Synthetic request failed'));await first;
assert.equal(submit.disabled,false);assert.equal(text.value,'Add the documented skill');assert.equal(status.textContent,'Synthetic request failed');
''')


def test_connection_intent_keeps_direct_profile_and_returns_only_on_ready_notice():
    run(r'''
const select=put('provider-account-provider','select'), disclosure=put('add-provider-account');
const focused=new Element('button');query=selector=>selector==='.provider-choice[aria-pressed="true"]'?focused:null;
ui.configureFixture({dependencies,noRender:true,data:{provider_options:[{provider:'codex',methods:['subscription']},{provider:'claude',methods:['api_key']}]}});
await ui.openAccountConnection({profile:'direct-openai',returnView:'project'});
assert.equal(select.value,'codex');assert.equal(disclosure.open,true);assert.equal(focused.focused,true);
ui.announceFixture({account_id:'account-1',profile:'codex-cli'});
const event=events.find(item=>item.type==='xperfect:account-connected');
assert.deepEqual(event.detail,{account_id:'account-1',profile:'direct-openai',returnView:'project'});assert.equal(views.at(-1),'project');
await ui.openAccountConnection({profile:'codex-cli',returnView:'project'});
ui.announceFixture({account_id:'account-2',profile:'claude-code'});
assert.equal(events.at(-1).detail.profile,'claude-code');
''')


def test_branded_provider_choices_keep_unavailable_reasons_and_only_supported_methods():
    run(r'''
const disclosure=put('add-provider-account');disclosure.open=true;disclosure.querySelector=()=>null;
const provider=put('provider-account-provider','select'), method=put('provider-account-method','select');
new Element('label').append(provider);new Element('label').append(method);
const choices=put('provider-choice-list'), methods=put('provider-method-choice');
const support=put('provider-support-note'), label=put('provider-account-label','input'), key=put('provider-api-key','input');
put('provider-api-key-field');put('provider-account-default','input');put('provider-account-status');
const submit=new Element('button');query=selector=>selector==='#provider-account-form button[type="submit"]'?submit:null;
ui.configureFixture({dependencies,data:{provider_accounts:[],provider_options:[
 {provider:'codex',label:'Codex',methods:[],subscription_support:'proof_required'},
 {provider:'claude',label:'Claude',methods:['api_key'],native_api_key_support:'supported'},
 {provider:'grok',label:'Grok',methods:['subscription','api_key'],native_api_key_support:'supported'}]}});
ui.renderProvidersFixture();assert.equal(choices.children.length,3);assert.equal(provider.value,'claude');assert.equal(key.required,true);
await choices.children[0].handlers.click();assert.equal(provider.value,'codex');assert.equal(submit.disabled,true);assert.match(support.textContent,/not enabled/);
await choices.children[2].handlers.click();assert.equal(provider.value,'grok');assert.equal(method.value,'subscription');assert.equal(methods.hidden,false);
const apiKeyChoice=methods.children[1].children[0];apiKeyChoice.checked=true;apiKeyChoice.handlers.change();
assert.equal(method.value,'api_key');assert.equal(key.required,true);assert.equal(submit.disabled,false);
''')


def test_direct_connection_intent_selects_supported_key_and_does_not_accept_subscription():
    run(r'''
const disclosure=put('add-provider-account');disclosure.querySelector=()=>null;
const provider=put('provider-account-provider','select'),method=put('provider-account-method','select');
new Element('label').append(provider);new Element('label').append(method);
put('provider-choice-list');put('provider-method-choice');put('provider-support-note');put('provider-account-label','input');
const key=put('provider-api-key','input');put('provider-api-key-field');put('provider-account-default','input');put('provider-account-status');
const submit=new Element('button'),nativeClaude=new Element('button');
query=selector=>selector==='#provider-account-form button[type="submit"]'?submit:selector==='#existing-claude-connection button'?nativeClaude:null;
ui.configureFixture({dependencies,data:{current_native_claude:{available:true},provider_accounts:[
 {account_id:'subscription',provider:'claude',auth_method:'subscription',status:'ready'},
 {account_id:'key',provider:'claude',auth_method:'api_key',status:'ready'}],provider_options:[
 {provider:'claude',label:'Claude',methods:['subscription','api_key'],native_api_key_support:'supported'}]}});
await ui.openAccountConnection({profile:'direct-anthropic',authMethods:['api_key'],returnView:'project'});
assert.equal(provider.value,'claude');assert.equal(method.value,'api_key');assert.equal(key.required,true);assert.notEqual(nativeClaude.focused,true);
ui.announceFixture({account_id:'subscription',profile:'claude-code'});assert.equal(events.length,0);assert.equal(views.at(-1),'connections');
ui.announceFixture({account_id:'key',profile:'claude-code'});assert.equal(events.at(-1).detail.profile,'direct-anthropic');assert.equal(views.at(-1),'project');
''')
