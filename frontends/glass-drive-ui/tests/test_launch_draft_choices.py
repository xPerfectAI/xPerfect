"""Restore exact unsent request choices through the actual draft/bootstrap presenters."""
import json
from pathlib import Path
import re
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def run(case):
    source = (STATIC / 'app.js').read_text()

    def function(name):
        match = re.search(rf'^(?:async )?function {name}\(.*?^}}\n', source, re.M | re.S)
        assert match, name
        return match.group(0)

    helpers = '\n'.join(function(name) for name in (
        'bindWorkspaceInteractionOwner', 'renderWorkspaceOptions', 'runnableDefaultOption', 'uniqueWorkspaces', 'findWorkspace',
        'renderWorkspaceTypeOptions', 'renderLaunchProviderAccounts', 'renderDefaultWorkerOptions',
        'syncPreferenceControls', 'syncDirectEffort', 'syncWorkspaceModeUI',
    ))
    draft_start = source.index('  launchDraftState = createLaunchDraft(')
    draft = source[draft_start:source.index('\n  const select =', draft_start)]
    refresh = source[source.index('  const currentBootstrapResult ='):source.index('\n  function stopHivePolling()')]
    submit_start = source.index("  form.addEventListener('submit', async (event) => {")
    guard = source[submit_start + len("  form.addEventListener('submit', async (event) => {"):source.index('    launchInFlight = true;', submit_start)]
    validation = source[source.index('  function clearLaunchValidation()'):source.index("  form.addEventListener('input', clearLaunchValidation)")]
    direct_start = source.index("    if (String(select.value).startsWith('new:direct-'))", submit_start)
    direct_guard = source[direct_start:source.index('    const payload = {', direct_start)]
    script = r'''
import assert from 'node:assert/strict';
import {createLaunchDraft} from DRAFT;
import {workerTypeOption} from ONBOARDING;
import {credentialPolicyTransition,preferredProviderAccountId} from POLICY;
class Element {
 constructor(tag='input',value=''){this.tag=tag;this._value=value;this.children=[];this.dataset={};this.disabled=false;this.handlers={};this.ownerDocument=document;}
 get options(){return this.tag==='select'?this.children.flatMap(item=>item.tag==='optgroup'?item.children:[item]):undefined;}
 get selectedOptions(){return this.options?.filter(item=>item.value===this.value)||[];}
 get value(){return this._value;}
 set value(value){this._value=this.tag==='select'&&!this.options.some(item=>item.value===value)?'':value;}
 replaceChildren(...items){this.children=items;this._value=this.options?.find(item=>!item.disabled)?.value||'';}
 appendChild(item){this.children.push(item);return item;}
 querySelectorAll(){return this.options||[];}
 addEventListener(type,handler){(this.handlers[type]||=[]).push(handler);}
 dispatchEvent(event){for(const handler of this.handlers[event.type]||[])handler(event);}
 focus(){}
}
const elements={};
const document={createElement:tag=>new Element(tag),getElementById:id=>elements[id],querySelectorAll:()=>[]};
const option=value=>{const item=new Element('option');item.value=value;return item;};
for(const id of ['description','success_criteria','context','schedule-text','worker-name','grok-effort','worker-choice','workspace-choice'])elements[id]=new Element();
for(const id of ['workspace-option','provider-account-selection','provider-account-policy','execution-workspace','workspace-mode','workspace-file-placement','workspace-type','launch-surface','codex-effort','claude-effort','openclaw-effort','direct-effort','default-worker-profile'])elements[id]=new Element('select');
const fill=(id,values)=>elements[id].replaceChildren(...values.map(option));
fill('provider-account-policy',['personal_required','personal_preferred','legacy']);
fill('workspace-mode',['isolated','shared']);fill('workspace-file-placement',['common','member_private']);
fill('launch-surface',['desktop','terminal']);fill('codex-effort',['','low','high','xhigh']);
fill('claude-effort',['','max','xhigh']);fill('openclaw-effort',['','high','max']);
elements['worker-choice'].value=elements['workspace-choice'].value='new';
for(const id of ['workspace-type-help','workspace-mode-help'])elements[id]=new Element('div');
const cache=new Map(),scopeA='a'.repeat(64),scopeB='b'.repeat(64);
const storage={getItem:key=>cache.get(key)||null,setItem:(key,value)=>cache.set(key,value),removeItem:key=>cache.delete(key)};
globalThis.window={sessionStorage:storage};
const fields=elements;
let launchDraftState;
DRAFT_SETUP
let bootstrap=null,bootstrapGeneration=0,bootstrapFlight=null,identityTransition=false,workerChoiceGeneration=0,csrfToken='';
 let workspaceInteractionOwnerScope='',workspaceSteerInFlight=new Map();
const select=elements['workspace-option'],providerAccount=elements['provider-account-selection'],providerAccountPolicy=elements['provider-account-policy'];
const defaultWorker=elements['default-worker-profile'],codexEffort=elements['codex-effort'],claudeEffort=elements['claude-effort'],openclawEffort=elements['openclaw-effort'];
const workspaceType=elements['workspace-type'],workspaceMode=elements['workspace-mode'],workspaceFilePlacement=elements['workspace-file-placement'];
const workspaceModeHelp=elements['workspace-mode-help'],workspaceFilePlacementField={},providerAccountHelp={},templateStartStatus={},workspaceCatalogStatus={},status={dataset:{}},button={},help={};
let workspaceCatalogState='active';
const catalogState={generation:0},workspaceKindFilter={value:'named,ephemeral,legacy'},workspaceSearch={value:''},workspaceTagFilter={value:''};
const fileDraft={setOwnerScope(){},blocked:()=>false},onboarding={render(){}};
const launchLibrary={setOwnerScope(){},refs:()=>[]},mainLibrary={setAccess(){}},signedToken='';
// This fixture starts without a selected expert; bootstrap still updates its owner-bound picker.
const expertBootstrapUpdates=[];
const expertPicker={update:async value=>{expertBootstrapUpdates.push(value);},selection:()=>null,prepare(){throw new Error('No expert selected');}};
const selectedLibraryRefs=[];
const renderCurrentUser=()=>{},syncAccountSummary=()=>{},renderWorkspaceHive=()=>{},workspaceViewData=()=>({}),hivePrefs=()=>({}),renderActivity=()=>{},syncWorkspaceUI=()=>{};
const workspaceOptionLabel=item=>item.name,decorateCatalogWorkspace=value=>value,refreshWorkspaceCatalog=async()=>{},getJson=async()=>{throw new Error('Unexpected exact lookup');};
let incoming={draft_owner_scope:scopeA,csrf_token:'synthetic',identity:{},
 default_workspace_option:'new:codex-cli',default_workspace_type:'host',default_launch_surface:'desktop',
 workspace_type_options:[{value:'sandboxed',label:'Sandbox'},{value:'host',label:'Your Computer'}],
 new_workspace_options:[{value:'new:codex-cli',profile:'codex-cli'},{value:'new:direct-openai',profile:'direct-openai',account_auth_methods:['api_key']}],
 user_preferences:{codex_reasoning_effort:'low',claude_effort:'max',openclaw_effort:'high'},
 bootstrap_sections:{preferences:'ready',provider_accounts:'ready',workspace_catalog:'ready',workspace_templates:'ready'},existing_workspaces:[],
 profile_account_providers:{'codex-cli':['codex'],'direct-openai':['codex']},
 provider_accounts:[{account_id:'default-a',provider:'codex',auth_method:'api_key',status:'ready',is_default:true,label:'Default A'},
 {account_id:'chosen-b',provider:'codex',auth_method:'subscription',status:'action_required',label:'Chosen B'}]};
const loadBootstrap=async()=>structuredClone(incoming),fetchCatalogPage=async()=>({items:[]});
let directProfileCatalogPromise;
let modelEfforts=['default','high','xhigh'];
const fetch=async()=>({ok:true,json:async()=>({items:[{profile:'direct-openai',models:[{recommended_effort:'high',effort_choices:modelEfforts}]}]})});
HELPERS
REFRESH
let launchInFlight=false;
VALIDATION
const scheduleText=elements['schedule-text'],scheduleButton={};
async function submitGuard(){const event={preventDefault(){},submitter:{id:'launch-button'}}; GUARD; return true;}
async function directGuard(){DIRECT_GUARD; return true;}
const saved=values=>cache.set(`xperfect.launch-draft.v1.${scopeA}`,JSON.stringify({version:1,values}));
const persisted=()=>JSON.parse(cache.get(`xperfect.launch-draft.v1.${scopeA}`)).values;
''' + case
    script = (script.replace('DRAFT_SETUP', draft).replace('HELPERS', helpers).replace('REFRESH', refresh)
              .replace('DIRECT_GUARD', direct_guard).replace('GUARD', guard).replace('VALIDATION', validation)
              .replace('DRAFT', json.dumps((STATIC / 'launch-draft.js').as_uri()))
              .replace('POLICY', json.dumps((STATIC / 'launch-policy.js').as_uri()))
              .replace('ONBOARDING', json.dumps((STATIC / 'worker-onboarding.js').as_uri())))
    result = subprocess.run(['node', '--input-type=module'], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_reload_keeps_strict_unready_or_removed_account_and_recovers_same_identity():
    for state in ['action_required', 'disconnected', 'removed']:
        run('const state=' + json.dumps(state) + r''';
if(state==='removed')incoming.provider_accounts=incoming.provider_accounts.filter(item=>item.account_id!=='chosen-b');
else incoming.provider_accounts.find(item=>item.account_id==='chosen-b').status=state;
saved({'workspace-option':'new:codex-cli','provider-account-selection':'chosen-b','provider-account-policy':'personal_required','workspace-type':'sandboxed','launch-surface':'terminal','codex-effort':'high'});
await refreshBootstrap();
assert.equal(providerAccount.value,'chosen-b');assert.equal(providerAccount.dataset.accountState,'needs_attention');
assert.equal(providerAccount.selectedOptions[0].disabled,true);assert.equal(providerAccountPolicy.value,'personal_required');
assert.equal(workspaceType.value,'sandboxed');assert.equal(codexEffort.value,'high');assert.equal(elements['launch-surface'].value,'terminal');
assert.equal(persisted()['provider-account-selection'],'chosen-b');assert.equal(persisted()['workspace-type'],'sandboxed');
incoming.provider_accounts.push({account_id:'chosen-b',provider:'codex',auth_method:'subscription',status:'ready',label:'Chosen B'});
incoming.provider_accounts=incoming.provider_accounts.filter(item=>item.account_id!=='chosen-b'||item.status==='ready');
await refreshBootstrap();assert.equal(providerAccount.value,'chosen-b');assert.equal(providerAccount.dataset.accountState,'ready');
assert.equal(codexEffort.value,'high','an account refresh must not replace an unsaved effort with configured low');
''')


def test_fresh_owner_uses_configured_defaults_without_retaining_previous_identity():
    run(r'''
saved({'workspace-option':'new:codex-cli','provider-account-selection':'chosen-b','provider-account-policy':'personal_required','workspace-type':'sandboxed','codex-effort':'high'});
await refreshBootstrap();
incoming={...incoming,draft_owner_scope:scopeB,provider_accounts:[{account_id:'owner-b-account',provider:'codex',auth_method:'api_key',label:'Owner B',status:'ready',is_default:true}]};
await refreshBootstrap();
assert.equal(providerAccount.value,'owner-b-account');assert.equal(providerAccount.dataset.explicitAccountId,undefined);
assert.equal(providerAccountPolicy.value,'personal_required');assert.equal(workspaceType.value,'host');assert.equal(codexEffort.value,'low');
assert.equal(cache.has(`xperfect.launch-draft.v1.${scopeA}`),true);
''')


def test_direct_effort_restores_after_catalog_load_and_incompatible_account_stays_blocked():
    run(r'''
saved({'workspace-option':'new:direct-openai','provider-account-selection':'chosen-b','provider-account-policy':'personal_required','workspace-type':'sandboxed','direct-effort':'xhigh'});
await refreshBootstrap();
assert.equal(providerAccount.value,'chosen-b');assert.equal(providerAccount.dataset.accountState,'needs_attention');
const effort=elements['direct-effort'];
document.querySelector=()=>({});
await syncDirectEffort('direct-openai',select);
assert.equal(effort.value,'xhigh');assert.equal(effort.disabled,false);assert.equal(effort.selectedOptions[0].disabled,false);
assert(!providerAccount.options.some(item=>item.value==='chosen-b'&&!item.disabled));
effort.value='high';effort.dispatchEvent(new Event('input'));await refreshBootstrap();
assert.equal(effort.value,'high');assert.equal(persisted()['direct-effort'],'high');
''')


def test_unavailable_location_and_effort_remain_unavailable_instead_of_remapping():
    run(r'''
incoming.workspace_type_options=[{value:'sandboxed',label:'Sandbox'}];
saved({'workspace-option':'new:direct-openai','provider-account-selection':'default-a','workspace-type':'host','direct-effort':'removed-effort'});
await refreshBootstrap();
assert.equal(workspaceType.value,'host');assert.equal(workspaceType.selectedOptions[0].disabled,true);
assert.equal(persisted()['workspace-type'],'host');
assert.equal(await submitGuard(),undefined);assert(status.textContent.includes('unavailable'));
const effort=elements['direct-effort'];document.querySelector=()=>({});
await syncDirectEffort('direct-openai',select);
assert.equal(effort.value,'removed-effort');assert.equal(effort.selectedOptions[0].disabled,true);
assert.equal(persisted()['direct-effort'],'removed-effort');
assert.equal(await directGuard(),undefined);assert(status.textContent.includes('Model options are not ready'));
''')


def test_unspecified_direct_effort_uses_current_model_recommendation():
    run(r'''
saved({'workspace-option':'new:direct-openai','provider-account-selection':'default-a','direct-effort':''});
await refreshBootstrap();document.querySelector=()=>({});
await syncDirectEffort('direct-openai',select);
assert.equal(elements['direct-effort'].value,'high');assert.equal(elements['direct-effort'].selectedOptions[0].disabled,false);
''')


def test_connected_account_change_event_is_persisted_for_reload():
    run(r'''
saved({'workspace-option':'new:codex-cli','provider-account-selection':'chosen-b'});
await refreshBootstrap();
providerAccount.value='default-a';providerAccount.dispatchEvent(new Event('change'));
assert.equal(persisted()['provider-account-selection'],'default-a');
''')
