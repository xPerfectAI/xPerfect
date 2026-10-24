"""Run the real paged picker/bootstrap owners with native select semantics."""
import json
from pathlib import Path
import re
import subprocess

STATIC = Path(__file__).parents[1] / "src/glass_drive_ui/static"


def run(body):
    source = (STATIC / "app.js").read_text()

    def function(name):
        match = re.search(rf"^function {name}\(.*?^}}\n", source, re.M | re.S)
        assert match, name
        return match.group(0)

    helpers = "\n".join(function(name) for name in ("bindWorkspaceInteractionOwner", "renderWorkspaceOptions", "runnableDefaultOption", "uniqueWorkspaces", "findWorkspace"))
    browse = source[source.index("  const loadWorkerChoices = async"):source.index("  const currentBootstrapResult =")]
    refresh = source[source.index("  const currentBootstrapResult ="):source.index("\n  function stopHivePolling()")]
    harness = r'''
import assert from 'node:assert/strict';
import {createLaunchDraft} from DRAFT;
import {credentialPolicyTransition} from POLICY;
import {mountWorkerOnboarding} from ONBOARDING;
class Element {
 constructor(tag='input',value=''){this.tag=tag;this._value=value;this.children=[];this.dataset={};this.disabled=false;this.handlers={};}
 get options(){return this.children.flatMap(child=>child.tag==='optgroup'?child.children:[child]);}
 get value(){return this._value;}
 set value(value){this._value=this.tag==='select'&&!this.options.some(item=>item.value===value)?'':value;}
 replaceChildren(...items){this.children=items;this._value=this.options.find(item=>!item.disabled)?.value||'';}
 appendChild(item){this.children.push(item);}
 querySelectorAll(){return this.options;}
 addEventListener(type,handler){this.handlers[type]=handler;}
 dispatchEvent(event){this.handlers[event.type]?.(event);}
}
const scopeA='a'.repeat(64),scopeB='b'.repeat(64);
const worker=n=>({worker_id:'worker-'+n,name:'Worker '+n,profile:'codex-cli',workspace_kind:n%2?'legacy':'ephemeral',workspace_id:'place-'+n,workspace_name:'Workspace '+n,execution_workspace_mode:'shared'});
function harness(savedValue=null,savedLocation=null){
 let bootstrap=null,bootstrapGeneration=0,bootstrapFlight=null,identityTransition=false,workerChoiceGeneration=0,csrfToken='';
 let workspaceInteractionOwnerScope='',workspaceSteerInFlight=new Map();
 const select=new Element('select'),providerAccountPolicy={value:'personal_required',dataset:{}};
 const elements={'workspace-option':select,'provider-account-policy':providerAccountPolicy};
 for(const id of ['worker-choice','workspace-choice'])elements[id]=new Element('input','new');
 for(const id of ['worker-type','existing-worker','copy-worker','execution-workspace'])elements[id]=new Element('select');
 for(const id of ['new-worker-fields','existing-worker-field','workspace-location-choices','saved-worker-location','existing-workspace-field','new-workspace-fields','execution-workspace-help'])elements[id]=new Element('div');
 const document={createElement:tag=>new Element(tag),getElementById:id=>elements[id],querySelectorAll:()=>[]};
 const workspaceOptionLabel=item=>item.name||'Selected workspace';
 HELPERS
 const storage=new Map();
 if(savedValue||savedLocation)storage.set('xperfect.launch-draft.v1.'+scopeA,JSON.stringify({version:1,values:{'workspace-option':savedValue||'new:codex-cli',...(savedLocation?{'workspace-choice':'existing','execution-workspace':savedLocation}:{})}}));
 const launchDraftState=createLaunchDraft({fields:{'workspace-option':select,'workspace-choice':elements['workspace-choice'],'execution-workspace':elements['execution-workspace']},storage:()=>({getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)})});
 let incoming={draft_owner_scope:scopeA,csrf_token:'scope-a',existing_workspaces:[],
  new_workspace_options:[{value:'new:codex-cli',profile:'codex-cli',label:'Codex'}],default_workspace_option:'new:codex-cli',
  provider_accounts:[{account_id:'account-a',provider:'codex',label:'Account A',status:'ready'}],profile_account_providers:{'codex-cli':['codex']},
  bootstrap_sections:{preferences:'ready',provider_accounts:'ready',workspace_catalog:'ready',workspace_templates:'ready'}};
 const loads=[],details=[];
 let page=async()=>({items:Array.from({length:25},(_,n)=>worker(n+1)),next_cursor:'page-2'});
 let detail=async()=>({...worker(26),provider_account:{account_id:'account-a',policy:'personal_required'}});
 let members=async()=>({workspace_id:'place-26',mode:'shared',default_worker_id:'worker-26',members:[worker(26)]});
 const loadBootstrap=async()=>structuredClone(incoming);
 const fetchCatalogPage=async request=>{loads.push(request);return page(request);};
 const getJson=async path=>{details.push(path);return path.startsWith('/api/execution-workspaces/')?members(path):detail(path);};
 const decorateCatalogWorkspace=value=>value;
 const onboarding=mountWorkerOnboarding({root:document,getData:()=>bootstrap,onChoice:value=>{select.value=value;}});
 const fileDraft={setOwnerScope(){}};
 const launchLibrary={setOwnerScope(){}},mainLibrary={setAccess(){}},expertPicker={update(){}},signedToken='';
 const catalogState={generation:0,items:[],loading:false};
 const providerAccount={},providerAccountHelp={},defaultWorker={},codexEffort={},claudeEffort={},openclawEffort={};
 const workspaceKindFilter={value:'named,ephemeral,legacy'},workspaceSearch={value:''},workspaceTagFilter={value:''};
 const workspaceMode={},workspaceFilePlacementField={},workspaceFilePlacement={},workspaceModeHelp={},workspaceType={},button={},help={};
 const templateStartStatus={},workspaceCatalogStatus={},status={};
 const renderCurrentUser=()=>{},renderLaunchProviderAccounts=()=>{},syncAccountSummary=()=>{},syncPreferenceControls=()=>{};
 const renderWorkspaceHive=()=>{},workspaceViewData=()=>({}),hivePrefs=()=>({}),refreshWorkspaceCatalog=async()=>{};
 const renderActivity=()=>{},syncWorkspaceUI=()=>{},syncWorkspaceModeUI=()=>{};
 BROWSE
 REFRESH
 return {refresh:refreshBootstrap,browse:loadWorkerChoices,select,loads,details,elements,current:()=>bootstrap,
  page(fn){page=fn;},detail(fn){detail=fn;},owner(scope){incoming={...incoming,draft_owner_scope:scope,csrf_token:scope};},
  members(fn){members=fn;},
  invalidate(){identityTransition=true;bootstrapGeneration++;}};
}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
'''
    harness = (harness.replace("DRAFT", json.dumps((STATIC / "launch-draft.js").as_uri()))
               .replace("POLICY", json.dumps((STATIC / "launch-policy.js").as_uri()))
               .replace("ONBOARDING", json.dumps((STATIC / "worker-onboarding.js").as_uri()))
               .replace("HELPERS", helpers).replace("BROWSE", browse).replace("REFRESH", refresh))
    result = subprocess.run(["node", "--input-type=module"], input=harness + body, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_off_page_saved_worker_is_loaded_by_exact_owner_checked_identity():
    run(r'''
for(const prefix of ['open:','duplicate:']){
 const ui=harness(prefix+'worker-26');await ui.refresh();
 assert.equal(ui.select.value,prefix+'worker-26');assert.equal(ui.select.dataset.workerChoiceState,'');
 assert.deepEqual(ui.details,['/api/workspaces/worker-26']);
 assert.equal(ui.loads.length,1,'startup must not load every page');
 assert.equal(ui.loads[0].kind,'named,ephemeral,legacy');
 assert.equal(ui.current().existing_workspaces.find(item=>item.worker_id==='worker-26').provider_readiness.label,'Account A');
 assert.equal(ui.current().worker_catalog.items.length,25);assert.equal(ui.current().worker_catalog.next_cursor,'page-2');
}
for(const outcome of ['missing','foreign']){
 const ui=harness('open:worker-26');
 ui.detail(async()=>{if(outcome==='missing')throw new Error('Worker is unavailable');return worker(99);});
 await ui.refresh();assert.equal(ui.select.value,'open:worker-26');
 assert.equal(ui.select.dataset.workerChoiceState,'unavailable');
 assert(!ui.current().existing_workspaces.some(item=>item.worker_id==='worker-26'));
 assert(ui.current().worker_choice_error.message);assert.notEqual(ui.select.value,'new:codex-cli');
}
''')


def test_worker_search_and_pagination_keep_selection_and_fence_stale_responses():
    run(r'''
const ui=harness('open:worker-26');await ui.refresh();
ui.page(async request=>({items:[worker(27)],next_cursor:null}));await ui.browse({append:true});
assert.equal(ui.loads.at(-1).cursor,'page-2');assert.equal(ui.current().worker_catalog.items.length,26);
assert.equal(ui.select.value,'open:worker-26');
const old=deferred(),current=deferred();let count=0;
ui.page(()=>++count===1?old.promise:current.promise);
const a=ui.browse({search:'old search'}),b=ui.browse({search:'new search'});
current.resolve({items:[{...worker(40),name:'Newest name'}],next_cursor:null});await b;
old.resolve({items:[worker(41)],next_cursor:null});await a;
assert.equal(ui.current().worker_catalog.search,'new search');assert.equal(ui.current().worker_catalog.items[0].worker_id,'worker-40');
assert(!ui.current().existing_workspaces.some(item=>item.worker_id==='worker-41'));assert.equal(ui.select.value,'open:worker-26');
const late=deferred();ui.page(()=>late.promise);const previousOwner=ui.browse({search:'previous owner'});
ui.owner(scopeB);ui.page(async()=>({items:[worker(90)],next_cursor:null}));await ui.refresh();
late.resolve({items:[worker(50)],next_cursor:null});await previousOwner;
assert.equal(ui.current().draft_owner_scope,scopeB);assert.deepEqual(ui.current().existing_workspaces.map(item=>item.worker_id),['worker-90']);
assert.equal(ui.select.value,'new:codex-cli');
''')


def test_failed_search_keeps_loaded_choices_and_exact_current_selection():
    run(r'''
const ui=harness('duplicate:worker-26');await ui.refresh();
ui.page(async()=>{throw new Error('Catalog temporarily unavailable');});await ui.browse({search:'Research'});
assert.equal(ui.select.value,'duplicate:worker-26');assert.equal(ui.current().worker_catalog.items.length,25);
assert.equal(ui.current().worker_catalog.error,'Catalog temporarily unavailable');
assert.equal(ui.current().worker_catalog.loading,false);
''')


def test_saved_off_page_shared_location_is_verified_and_never_becomes_a_new_workspace():
    run(r'''
for(const outcome of ['ready','missing','foreign','isolated']){
 const ui=harness('new:codex-cli','place-26');
 ui.members(async()=>{
  if(outcome==='missing')throw new Error('Workspace is unavailable');
  return {workspace_id:outcome==='foreign'?'place-99':'place-26',mode:outcome==='isolated'?'isolated':'shared',members:[worker(26)]};
 });
 await ui.refresh();
 assert.deepEqual(ui.details,['/api/execution-workspaces/place-26/members']);
 assert.equal(ui.loads.length,1);assert.equal(ui.select.value,'new:codex-cli');
 assert.equal(ui.elements['workspace-choice'].value,'existing');
 assert.equal(ui.elements['execution-workspace'].value,'place-26');
 assert.equal(ui.elements['execution-workspace'].dataset.workspaceChoiceState,outcome==='ready'?'':'unavailable');
 assert.equal(ui.current().execution_workspace_choices[0].workspace_id,'place-26');
 assert.equal(Boolean(ui.current().workspace_choice_error),outcome!=='ready');
}
''')


def test_location_lookup_preserves_newest_human_choice_and_rejects_late_previous_owner():
    run(r'''
const ui=harness('new:codex-cli','place-26');await ui.refresh();
const waiting=deferred();ui.members(()=>waiting.promise);const refresh=ui.refresh();
for(let i=0;i<12;i++)await Promise.resolve();
ui.elements['execution-workspace'].value='place-2';ui.elements['execution-workspace'].dispatchEvent(new Event('input'));
waiting.resolve({workspace_id:'place-26',mode:'shared',members:[worker(26)]});await refresh;
assert.equal(ui.elements['execution-workspace'].value,'place-2');
assert.equal(ui.elements['execution-workspace'].dataset.workspaceChoiceState,'');
const other=harness('new:codex-cli','place-26'),old=deferred();other.members(()=>old.promise);
const a=other.refresh();for(let i=0;i<12;i++)await Promise.resolve();
other.owner(scopeB);await other.refresh();old.resolve({workspace_id:'place-26',mode:'shared',members:[worker(26)]});await a;
assert.equal(other.current().draft_owner_scope,scopeB);assert.deepEqual(other.current().execution_workspace_choices,[]);
assert.equal(other.elements['workspace-choice'].value,'new');assert.equal(other.elements['execution-workspace'].value,'');
''')
