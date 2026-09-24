"""Account transitions must reject late bootstrap and catalog responses."""
import json
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("case", ["bootstrap", "catalog", "signout", "late_error"])
def test_late_account_responses_do_not_restore_old_identity(case):
    source = Path(__file__).parents[1] / "src/glass_drive_ui/static/app.js"
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(SOURCE, 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };
const fetches=[], catalogs=[], bindings=[], handlers={};
const logout=deferred();
const context = vm.createContext({
  fetch: () => { const request=deferred();fetches.push(request);return request.promise; },
  fetchCatalogPage: () => { const request=deferred();catalogs.push(request);return request.promise; },
  withAuth: value => value, responseMessage: async () => 'Failed request',
  launchDraftState: {bindOwner: value => bindings.push(value)},
  fileDraft: {setOwnerScope: () => {}},
  select: {value:''}, catalogState: {generation:0,loading:false,items:[]},
  workspaceKindFilter: {value:'named'},workspaceSearch: {value:''},workspaceTagFilter: {value:''},
  providerAccount:{},providerAccountPolicy:{},providerAccountHelp:{},workerAccountSummaryNode:null,defaultWorker:{},
  workspaceMode:{},workspaceFilePlacementField:{},workspaceFilePlacement:{},workspaceModeHelp:{},workspaceType:{},
  codexEffort:{},claudeEffort:{},openclawEffort:{},templateStartStatus:{},workspaceCatalogStatus:{},
  button:{},help:{},status:{},csrfToken:'',
  renderCurrentUser:()=>{},decorateCatalogWorkspace: value=>value,renderWorkspaceOptions:()=>{},
  renderLaunchProviderAccounts:()=>{},syncPreferenceControls:()=>{},renderWorkspaceHive:()=>{},
  renderActivity:()=>{},syncWorkspaceUI:()=>{},syncWorkspaceModeUI:()=>{},syncLaunchChoiceUI:()=>{},hivePrefs:()=>({}),workspaceViewData:()=>({}),
  refreshWorkspaceCatalog:async()=>{},
  switchAccount:{addEventListener:(name,callback)=>{handlers.switch=callback;}},
  localSignOut:{addEventListener:(name,callback)=>{handlers.signout=callback;}},
  signOut:()=>logout.promise,window:{alert:()=>{}},
});
vm.runInContext(between('  let bootstrap = null;', '  let activeView =') +
  between('async function loadBootstrap()', 'function withAuth(') +
  between('  const refreshBootstrap = async () => {', '  function stopHivePolling()') +
  between("  switchAccount?.addEventListener('click'", '  function startHivePolling()') +
  '\nthis.refresh=refreshBootstrap;this.current=()=>({owner:bootstrap?.draft_owner_scope,csrf:csrfToken});',context);
const data = owner => ({draft_owner_scope:owner,csrf_token:'csrf-'+owner,existing_workspaces:[],bootstrap_sections:{}});
const response = owner => ({ok:true,json:async()=>data(owner)});
const tick = async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
async function run() {
  const a=context.refresh();
  if (CASE === 'catalog') { fetches[0].resolve(response('A'));await tick();assert.equal(catalogs.length,1); }
  if (CASE === 'signout') {
    const exit=handlers.signout();
    await context.refresh();assert.equal(fetches.length,1);
    fetches[0].resolve(response('A'));await a;
    assert.equal(bindings.length,0);assert.equal(context.current().csrf,'');
    logout.resolve({});await exit;return;
  }
  const b=context.refresh();fetches[1].resolve(response('B'));await tick();
  catalogs.at(-1).resolve({items:[]});await b;
  if (CASE === 'catalog') catalogs[0].resolve({items:[{worker_id:'A-only'}]});
  else if (CASE === 'late_error') fetches[0].reject(new Error('Old account request failed'));
  else fetches[0].resolve(response('A'));
  await a;
  assert.deepEqual(bindings,['B']);
  assert.equal(context.current().owner,'B');assert.equal(context.current().csrf,'csrf-B');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run(
        ["node", "-e", "const SOURCE=" + json.dumps(str(source)) + ";const CASE=" + json.dumps(case) + ";" + script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
