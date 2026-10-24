"""Causal onboarding/consumer checks; real browser acceptance remains separate."""
import json
from pathlib import Path
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def node(source):
    result = subprocess.run(['node', '--input-type=module'], input=source, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def onboarding(body):
    source = r'''
import assert from 'node:assert/strict';
import {mountWorkerOnboarding,workerChoiceLabel,hostAccountLabel} from MODULE;
import {createLaunchDraft} from DRAFT;
class Element {
  constructor(tag='input',value=''){this.tag=tag;this._value=String(value);this.children=[];this.handlers={};this.hidden=false;this.disabled=false;this.dataset={};this.events=[];}
  get options(){return this.tag==='select'?this.children.flatMap(child=>child.tag==='optgroup'?child.children:[child]):undefined;}
  get value(){return this._value;}
  set value(value){const candidate=String(value);this._value=this.tag==='select'&&!this.options.some(option=>option.value===candidate)?'':candidate;}
  replaceChildren(...children){this.children=children;this._value=this.options?.find(option=>!option.disabled)?.value || '';}
  appendChild(child){this.children.push(child);child.parentElement=this;return child;}
  addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);}
  dispatchEvent(event){this.events.push(event.type);for(const handler of this.handlers[event.type]||[])handler(event);return true;}
  focus(){this.focused=true;}
}
const option=(value,label=value,disabled=false)=>{const item=new Element('option',value);item.textContent=label;item.disabled=disabled;return item;};
const fields=new Map();const put=(id,tag='input',value='')=>{const item=new Element(tag,value);fields.set(id,item);return item;};
for(const id of ['worker-choice','workspace-choice'])put(id,'input','new');
put('workspace-type','input','sandboxed');
for(const id of ['workspace-option','worker-type','existing-worker','copy-worker','execution-workspace'])put(id,'select');
for(const id of ['new-worker-fields','existing-worker-field','workspace-location-choices','saved-worker-location','existing-workspace-field','new-workspace-fields','execution-workspace-help','worker-catalog-controls','copy-worker-search','existing-worker-search','copy-worker-details'])put(id,'div');
const workerRadios=['new','existing'].map(value=>new Element('input',value));
const locationRadios=['new','existing'].map(value=>new Element('input',value));
const root={getElementById:id=>fields.get(id),createElement:tag=>new Element(tag),querySelectorAll:selector=>selector.includes('worker-choice-radio')?workerRadios:selector.includes('workspace-choice-radio')?locationRadios:[]};
const select=fields.get('workspace-option'),choices=[];
const workerA={worker_id:'a',name:'Research partner',profile:'codex-cli',profile_label:'Codex',workspace_id:'private-a',workspace_name:'Research desk',execution_workspace_mode:'isolated',provider_readiness:{readiness:'ready',label:'Research account'}};
const workerB={worker_id:'b',name:'Design partner',profile:'claude-code',profile_label:'Claude Code',workspace_id:'shared-b',workspace_name:'Design studio',execution_workspace_mode:'shared',provider_readiness:{readiness:'ready',label:'Design account'}};
let data={new_workspace_options:[{value:'new:codex-cli',profile:'codex-cli',label:'Codex'},{value:'new:claude-code',profile:'claude-code',label:'Claude Code'},{value:'new:direct-xai',profile:'direct-xai',label:'Direct xAI'}],existing_workspaces:[workerA,workerB]};
function nativeChoices(value='new:codex-cli'){
 select.replaceChildren(...data.new_workspace_options.map(item=>option(item.value,item.label)),...data.existing_workspaces.map(item=>option('open:'+item.worker_id)),...data.existing_workspaces.map(item=>option('duplicate:'+item.worker_id)));
 select.value=value;
}
nativeChoices();
const adapter=mountWorkerOnboarding({root,getData:()=>data,onChoice:value=>{choices.push(value);nativeChoices(value);select.dispatchEvent(new Event('change'));}});
const radio=(items,value)=>{for(const item of items)item.checked=item.value===value;items.find(item=>item.value===value).dispatchEvent(new Event('change'));};
const change=(id,value)=>{const item=fields.get(id);item.value=value;item.dispatchEvent(new Event('change'));};
'''.replace('MODULE', json.dumps((STATIC / 'worker-onboarding.js').as_uri())).replace('DRAFT', json.dumps((STATIC / 'launch-draft.js').as_uri()))
    node(source + body)


def test_saved_worker_labels_show_exact_type_account_and_workspace():
    onboarding(r'''
adapter.render();assert.equal(workerChoiceLabel(workerA),'Research partner · Codex · Research account · Research desk');
const labels=fields.get('existing-worker').options.map(item=>item.textContent);
assert(labels.includes(workerChoiceLabel(workerA)));assert(labels.includes(workerChoiceLabel(workerB)));
assert.match(workerChoiceLabel({...workerA,provider_readiness:{}}),/Account status unavailable/);
assert.match(workerChoiceLabel({...workerA,provider_readiness:{policy:'legacy'}}),/Host account/);
''')


def test_new_reuse_copy_keep_exact_native_type_and_separate_credentials():
    onboarding(r'''
adapter.render();change('worker-type','new:direct-xai');assert.equal(select.value,'new:direct-xai');
radio(workerRadios,'existing');assert.equal(fields.get('existing-worker-field').hidden,false);
change('existing-worker','a');assert.equal(select.value,'open:a');assert.equal(fields.get('worker-choice').value,'existing');
assert.equal(fields.get('new-worker-fields').hidden,true);assert.match(fields.get('saved-worker-location').textContent,/Research desk/);
radio(workerRadios,'new');assert.equal(select.value,'new:codex-cli');
change('copy-worker','b');assert.equal(select.value,'duplicate:b');assert.equal(fields.get('worker-type').value,'new:claude-code');assert.equal(fields.get('worker-type').disabled,true);
const copiedScope=fields.get('saved-worker-location').textContent;
assert.match(copiedScope,/copied files, project notes, worker type and AI account/);
assert.match(copiedScope,/Chat history, browser sign-ins and tool approvals stay separate/);
assert.equal(fields.get('workspace-location-choices').hidden,true);
change('copy-worker','');assert.equal(select.value,'new:claude-code');assert.equal(fields.get('worker-type').disabled,false);
assert.deepEqual(choices,['new:direct-xai','open:a','new:codex-cli','duplicate:b','new:claude-code']);
''')


def test_empty_existing_worker_is_disabled_without_inventing_identity():
    onboarding(r'''
data={...data,existing_workspaces:[]};nativeChoices();adapter.render();
assert.equal(workerRadios.find(item=>item.value==='existing').disabled,true);
assert.equal(fields.get('existing-worker').options.length,1);assert.equal(fields.get('existing-worker').value,'');
assert.equal(fields.get('copy-worker').value,'');assert.equal(select.value,'new:codex-cli');assert.equal(choices.length,0);
''')


def test_pending_existing_choice_survives_a_catalog_render_before_selection():
    onboarding(r'''
adapter.render();radio(workerRadios,'existing');
assert.equal(fields.get('worker-choice').value,'existing');assert.equal(fields.get('existing-worker').value,'');
data={...data,existing_workspaces:[workerA,workerB]};adapter.render();
assert.equal(fields.get('worker-choice').value,'existing');assert.equal(workerRadios.find(item=>item.value==='existing').checked,true);
assert.equal(fields.get('existing-worker-field').hidden,false);assert.equal(fields.get('new-worker-fields').hidden,true);
assert.equal(select.value,'new:codex-cli');assert.equal(choices.length,0);
''')


def test_no_saved_draft_keeps_declared_default_instead_of_reusing_first_worker():
    onboarding(r'''
select.replaceChildren();const storage=new Map();
const draft=createLaunchDraft({fields:{'workspace-option':select},storage:()=>({getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)})});
// Saved workers come first in the actual native launch menu. They are choices, not defaults.
select.replaceChildren(option('open:a'),option('new:codex-cli'),option('new:direct-xai'));
select.value='new:direct-xai';select.dataset.draftDefault='new:direct-xai';
draft.bindOwner('b'.repeat(64));adapter.render();
assert.equal(select.value,'new:direct-xai');assert.equal(fields.get('worker-choice').value,'new');assert.equal(fields.get('worker-type').value,'new:direct-xai');assert.equal(choices.length,0);
''')


def test_existing_workspace_requires_shared_membership_not_private_rebinding():
    onboarding(r'''
adapter.render();radio(locationRadios,'existing');const location=fields.get('execution-workspace');
assert.equal(fields.get('existing-workspace-field').hidden,false);assert.equal(fields.get('new-workspace-fields').hidden,true);
assert.equal(location.options.find(item=>item.value==='private-a').disabled,true);assert.equal(location.options.find(item=>item.value==='shared-b').disabled,false);
change('execution-workspace','shared-b');assert.equal(location.value,'shared-b');assert.equal(select.value,'new:codex-cli');assert.equal(choices.length,0);
assert.match(fields.get('execution-workspace-help').textContent,/account and native session stay private/);
change('copy-worker','a');assert.equal(fields.get('workspace-choice').value,'new');assert.equal(fields.get('existing-workspace-field').hidden,true);
''')


def test_catalog_refresh_preserves_exact_worker_and_removes_foreign_choices():
    onboarding(r'''
const storage=new Map();const draft=createLaunchDraft({fields:{'worker-choice':fields.get('worker-choice')},storage:()=>({getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)})});draft.bindOwner('a'.repeat(64));
adapter.render();change('existing-worker','b');
data={...data,existing_workspaces:[{...workerA,name:'Updated name'},workerB]};nativeChoices('open:b');adapter.render();
assert.equal(fields.get('existing-worker').value,'b');assert.equal(select.value,'open:b');assert.equal(choices.at(-1),'open:b');
data={...data,draft_owner_scope:'b'.repeat(64),existing_workspaces:[{...workerA,worker_id:'c',name:'New owner worker',workspace_id:'new-place'}]};nativeChoices('new:codex-cli');draft.bindOwner('b'.repeat(64));adapter.render();
assert(!fields.get('existing-worker').options.some(item=>['a','b'].includes(item.value)));
assert.equal(fields.get('existing-worker').value,'');assert.equal(fields.get('copy-worker').value,'');assert.equal(fields.get('worker-choice').value,'new');assert.equal(select.value,'new:codex-cli');
''')


def test_native_options_exist_before_draft_restore_and_keep_exact_account():
    onboarding(r'''
const storage=new Map(),owner='a'.repeat(64);put('description','textarea','');put('provider-account-selection','select');const account=fields.get('provider-account-selection');
account.replaceChildren(option('account-a'),option('account-b'));
const argumentsForDraft=()=>({fields:{'workspace-option':select,'provider-account-selection':account,description:fields.get('description')},storage:()=>({getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)})});
const draft=createLaunchDraft(argumentsForDraft());draft.bindOwner(owner);select.value='new:direct-xai';account.value='account-b';fields.get('description').value='Keep this exact job';
for(const id of ['workspace-option','provider-account-selection','description'])fields.get(id).dispatchEvent(new Event('input'));
select.replaceChildren();select.value='new:direct-xai';assert.equal(select.value,'');
nativeChoices();account.replaceChildren(option('account-a'),option('account-b'));const restored=createLaunchDraft(argumentsForDraft());restored.bindOwner(owner);adapter.render();
assert.equal(select.value,'new:direct-xai');assert.equal(fields.get('worker-type').value,'new:direct-xai');assert.equal(account.value,'account-b');assert.equal(fields.get('description').value,'Keep this exact job');
''')


def test_connected_account_uses_one_bootstrap_flight_and_does_not_replace_type():
    app = (STATIC / 'app.js').read_text()
    a = app.index('  let accountRefreshFlight = null;')
    b = app.index('\n  initializeControlPlane(', a)
    c = app.index("  window.addEventListener('xperfect:account-connected'")
    d = app.index('\n\n  providerAccountPolicy?', c)
    node(r'''
import assert from 'node:assert/strict';const handlers={};let calls=0,resolveFlight;const flight=new Promise(resolve=>{resolveFlight=resolve;});
const window={addEventListener:(type,handler)=>{handlers[type]=handler;}};
const select={value:'new:direct-xai'},providerAccount={value:'old',options:[{value:'new-account'}],events:[],dispatchEvent(event){this.events.push(event.type);}};
const providerAccountPolicy={},status={};let identityTransition=false;
const refreshBootstrap=()=>{calls++;return flight;},selectConnectedClaudeAccount=()=>{},syncAccountSummary=()=>{};
''' + app[a:b] + '\n' + app[c:d] + r'''
handlers['glasshive:control-plane-updated']({detail:{connectedAccount:{account_id:'new-account',profile:'direct-xai'}}});
handlers['xperfect:account-connected']({detail:{account_id:'new-account',profile:'direct-xai'}});assert.equal(calls,1);assert.equal(providerAccount.value,'old');
resolveFlight();for(let i=0;i<12;i++)await Promise.resolve();assert.equal(calls,1);assert.equal(providerAccount.value,'new-account');assert.deepEqual(providerAccount.events,['change']);
handlers['xperfect:account-connected']({detail:{account_id:'foreign',profile:'codex-cli'}});for(let i=0;i<8;i++)await Promise.resolve();assert.equal(providerAccount.value,'new-account');assert.equal(select.value,'new:direct-xai');assert.equal(calls,1);
''')


def test_direct_worker_accepts_only_advertised_account_methods_and_keeps_explicit_mismatch():
    app = (STATIC / 'app.js').read_text()
    a = app.index('function renderLaunchProviderAccounts(')
    b = app.index('\nfunction renderDefaultWorkerOptions', a)
    policy = json.dumps((STATIC / 'launch-policy.js').as_uri())
    onboarding('import {credentialPolicyTransition,preferredProviderAccountId} from ' + policy + ';\n' + app[a:b] + r'''
globalThis.document=root;
const account=put('provider-account-selection','select'),policy=new Element('input','personal_required'),help=new Element('div');
const catalog={bootstrap_sections:{provider_accounts:'ready'},profile_account_providers:{'direct-openai':['codex','openai']},new_workspace_options:[{profile:'direct-openai',account_auth_methods:['api_key']}],provider_accounts:[
 {account_id:'subscription',provider:'codex',auth_method:'subscription',label:'Subscription account',status:'ready',is_default:true},
 {account_id:'key',provider:'codex',auth_method:'api_key',label:'API account',status:'ready'},
 {account_id:'foreign-provider-key',provider:'claude',auth_method:'api_key',label:'Other provider',status:'ready'}]};
renderLaunchProviderAccounts(account,policy,help,catalog,'new:direct-openai');
assert.equal(account.value,'key');assert.equal(account.dataset.accountState,'ready');assert.deepEqual(account.options.map(item=>item.value),['key']);
account.dataset.explicitChoice='true';account.dataset.explicitProfile='direct-openai';account.dataset.explicitAccountId='subscription';account.dataset.explicitAccountLabel='Explicit subscription';
renderLaunchProviderAccounts(account,policy,help,catalog,'new:direct-openai');
assert.equal(account.value,'subscription');assert.equal(account.dataset.accountState,'needs_attention');assert.equal(account.options.find(item=>item.value==='subscription').disabled,true);
assert(!account.options.some(item=>item.value==='foreign-provider-key'));assert.equal(policy.value,'personal_required');
''')


def test_account_menu_uses_typed_auth_state_and_clears_stale_identity_without_fetching():
    module = json.dumps((STATIC / 'account-menu.js').as_uri())
    node('import assert from "node:assert/strict"; import {renderAccountIdentity} from ' + module + r''';
const ids=['current-user-control','current-user-label','profile-menu-status','switch-account','local-sign-out'];
const fields=new Map(ids.map(id=>[id,{hidden:true,textContent:'stale',title:'stale'}]));const root={getElementById:id=>fields.get(id)};
globalThis.fetch=()=>{throw new Error('Identity presentation must use the existing bootstrap');};
renderAccountIdentity({display_name:'Synthetic owner',email:'owner@example.invalid',provider_switch_visible:true},true,root);
assert.equal(fields.get('current-user-label').textContent,'Synthetic owner · owner@example.invalid');assert.equal(fields.get('switch-account').hidden,false);assert.equal(fields.get('local-sign-out').hidden,false);assert.equal(fields.get('profile-menu-status').hidden,true);
renderAccountIdentity({display_name:'Old signed-in identity',email:'stale@example.invalid',provider_switch_visible:true},false,root);
assert.equal(fields.get('current-user-label').textContent,'Local access');assert.equal(fields.get('current-user-label').title,'Local access');assert.equal(fields.get('switch-account').hidden,true);assert.equal(fields.get('local-sign-out').hidden,true);assert.equal(fields.get('profile-menu-status').hidden,false);
renderAccountIdentity({provider_switch_visible:'true'},true,root);assert.equal(fields.get('current-user-label').textContent,'Signed in');assert.equal(fields.get('switch-account').hidden,true);assert.equal(fields.get('local-sign-out').hidden,false);
renderAccountIdentity({},false,{getElementById:()=>null});
''')


def test_unknown_launch_receipt_survives_reload_without_cross_owner_or_new_job_leak():
    onboarding(r'''
const storage=new Map(),owner='a'.repeat(64),other='b'.repeat(64);
const create=()=>{const field=new Element('textarea');const instance=createLaunchDraft({fields:{description:field},storage:()=>({getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)})});return {instance,field};};
const payload={description:'Exact original job',workspace_option:'new:codex-cli',provider_account_id:'exact-account'};
const first=create();first.instance.bindOwner(owner);first.field.value=payload.description;first.field.dispatchEvent(new Event('input'));
assert.equal(first.instance.requestKey(payload,()=> 'original-request'),'original-request');first.instance.markUnknown();assert.equal(first.instance.unknownRequest(payload),true);
const reloaded=create();reloaded.instance.bindOwner(owner);assert.equal(reloaded.field.value,payload.description);assert.equal(reloaded.instance.unknownRequest({...payload}),true);
assert.equal(reloaded.instance.requestKey(payload,()=> 'must-not-replace'),'original-request');assert.equal(reloaded.instance.unknownRequest(payload),true);
reloaded.instance.bindOwner(other);assert.equal(reloaded.field.value,'');assert.equal(reloaded.instance.unknownRequest(payload),false);
const originalAgain=create();originalAgain.instance.bindOwner(owner);assert.equal(originalAgain.instance.unknownRequest(payload),true);
const changed={...payload,description:'An explicitly changed new job'};assert.equal(originalAgain.instance.unknownRequest(changed),false);
assert.equal(originalAgain.instance.requestKey(changed,()=> 'new-request'),'new-request');assert.equal(originalAgain.instance.unknownRequest(changed),false);
// A confirmed rejection does not call markUnknown: safe retry retains its existing key.
assert.equal(originalAgain.instance.requestKey(changed,()=> 'duplicate-request'),'new-request');originalAgain.instance.clearRequest();assert.equal(originalAgain.instance.unknownRequest(changed),false);
''')


def test_actual_launch_error_status_distinguishes_rejected_and_uncertain_creation():
    app = (STATIC / 'app.js').read_text()
    a = app.index('async function responseError(')
    b = app.index('\nasync function getJson', a)
    c = app.index('      if (!error.status || error.status >= 500)')
    d = app.index("      if (error.code === 'provider_account_required')", c)
    node('import {watchHref} from ' + json.dumps((STATIC / 'launch-result.js').as_uri()) + ';\n' + r'''
import assert from 'node:assert/strict';const responseMessage=async()=> 'Confirmed server rejection';
const window={location:{href:'https://operator.example.invalid/#project'}},document={createElement:tag=>({tag})};
''' + app[a:b] + r'''
async function evaluate(error){const originalPayload={description:'Exact job'},launchOwnerScope='owner-a',bootstrap={draft_owner_scope:'owner-a'},identityTransition=false,status={links:[],append(...items){this.links.push(...items);}};let refreshes=0,unknown=false,recoveryShown=null;const launchDraftState={markUnknown:(saved,scope)=>{assert.deepEqual(saved,originalPayload);assert.equal(scope,launchOwnerScope);unknown=true;},confirmRequest:()=>{unknown=false;}};
const refreshBootstrap=async()=>{refreshes++;},launchFailureMessage=error=>error.message,showLaunchRecovery=(saved,scope)=>{recoveryShown={saved,scope};};
''' + app[c:d] + r'''
return {recovery:recoveryShown,status,refreshes,unknown};}
for(const code of [400,401,403,409,422,500,503]){
 const response={status:code,clone:()=>({json:async()=>({detail:{code:'typed_error',message:'Rejected',...(code===503?{watch_url:'/watch/exact-worker'}:{})}})})};
 const error=await responseError(response,'Fallback');assert.equal(error.status,code);assert.equal(error.code,'typed_error');
 const outcome=await evaluate(error);assert.equal(Boolean(outcome.recovery),code>=500);assert.equal(outcome.refreshes,code>=500?1:0);assert.equal(outcome.unknown,code>=500);
 if(code===503){assert.equal(error.recoveryUrl,'/watch/exact-worker');assert.equal(outcome.status.links.find(item=>item.tag==='a').href,'https://operator.example.invalid/watch/exact-worker');}
}
const lost=await evaluate(new Error('Transport ended'));assert(lost.recovery);assert.equal(lost.refreshes,1);assert.equal(lost.unknown,true);
''')


def test_known_missing_native_type_is_disabled_only_on_host_and_exact_saved_choice_survives():
    onboarding(r'''
fields.get('workspace-type').value='host';
data={...data,new_workspace_options:data.new_workspace_options.map(item=>({...item,host_cli_installed:item.profile==='codex-cli'?false:null}))};
nativeChoices('new:codex-cli');adapter.render();
const selected=fields.get('worker-type');
assert.equal(selected.value,'new:codex-cli');assert.equal(select.value,'new:codex-cli');
assert(selected.options.find(item=>item.value==='new:codex-cli').disabled);
assert.match(selected.options.find(item=>item.value==='new:codex-cli').textContent,/Not installed on this computer/);
assert(!selected.options.find(item=>item.value==='new:claude-code').disabled);
change('workspace-type','sandboxed');
assert.equal(selected.value,'new:codex-cli');assert(!selected.options.find(item=>item.value==='new:codex-cli').disabled);
assert.equal(choices.length,0);
''')


def test_search_is_in_existing_or_optional_copy_not_default_new_and_still_finds_offpage_workers():
    onboarding(r'''
const details=fields.get('copy-worker-details'),controls=fields.get('worker-catalog-controls');
adapter.render();assert(controls.hidden);assert.equal(controls.parentElement,fields.get('copy-worker-search'));
details.open=true;details.dispatchEvent(new Event('toggle'));
assert(!controls.hidden);assert.equal(fields.get('copy-worker').value,'');
assert.equal(controls.parentElement,fields.get('copy-worker-search'));
radio(workerRadios,'existing');assert(!controls.hidden);
assert.equal(controls.parentElement,fields.get('existing-worker-search'));
change('existing-worker','a');radio(workerRadios,'new');
const found={...workerB,worker_id:'offpage',name:'Found expert'};
data={...data,existing_workspaces:[...data.existing_workspaces,found],worker_catalog:{items:[found],search:'Found expert'}};
adapter.render();assert(!controls.hidden);
assert(fields.get('copy-worker').options.some(item=>item.value==='offpage'));
change('copy-worker','offpage');assert.equal(select.value,'duplicate:offpage');
fields.get('existing-worker').value='';radio(workerRadios,'existing');
assert.equal(fields.get('worker-choice').value,'existing');assert(!fields.get('existing-worker-field').hidden);
assert.equal(controls.parentElement,fields.get('existing-worker-search'));
''')


def test_shared_account_label_is_host_local_and_organization_only_when_managed():
    onboarding(r'''
assert.equal(hostAccountLabel(),'Host account');assert.equal(hostAccountLabel({},true),'Host account fallback');
assert.equal(hostAccountLabel({managed_deployment:true}),'Organization account');
assert.equal(hostAccountLabel({managed_deployment:true},true),'Organization account fallback');
assert.match(workerChoiceLabel({...workerA,provider_readiness:{policy:'legacy'}},{managed_deployment:true}),/Organization account/);
''')
