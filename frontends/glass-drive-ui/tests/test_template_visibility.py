"""A newly created template instance must survive the current catalog filters."""
import subprocess
from pathlib import Path
import re


def test_template_success_reveals_new_workspace_and_failure_keeps_filters():
    source = (Path(__file__).parents[1] / 'src/glass_drive_ui/static/app.js').read_text()
    start = source.index("  templateStartForm?.addEventListener('submit'")
    handler = source[start:source.index("\n\n  savePreferences?.", start)]
    script = r'''
const assert=require('node:assert/strict');
async function run(fail,approvals,refreshFail=false){
 let submit;
 const templateStartForm={addEventListener(_event,fn){submit=fn;}};
 const templateSelect={value:'template-1'},templateStartButton={dataset:{},disabled:false},templateInstanceName={value:'New analyst'},templateStartStatus={};
 const workspaceSearch={value:'Old worker'},workspaceTagFilter={value:'old-tag'},workspaceKindFilter={value:'ephemeral'},inactiveToggle={checked:false};
 const bootstrap={workspace_templates:[{}]},window={location:{hash:''}};
 let visible=false,request,creates=0;
 const postJson=async(path,payload)=>{creates++;request={path,payload};if(fail)throw new Error('Not created');return {workspace:{worker_id:'new-worker',duplication_report:{reapproval_items:Array(approvals).fill({action_id:'review-1'})}}};};
 const refreshBootstrap=async()=>{visible=workspaceSearch.value===''&&workspaceTagFilter.value===''&&workspaceKindFilter.value==='named,ephemeral,legacy'&&inactiveToggle.checked;if(refreshFail)throw new Error('Catalog unavailable after creation');};
 const rememberCapabilityReview=(worker,count)=>{assert.equal(worker,'new-worker');assert.equal(count,approvals);return 'library';};
HANDLER
 await submit({preventDefault(){}});
 assert.equal(request.path,'/api/workspace-templates/template-1/instantiate');assert.equal(request.payload.name,'New analyst');
 assert.equal(creates,1,'catalog failure must not replay confirmed creation');
 if(fail){assert.equal(visible,false);assert.equal(workspaceSearch.value,'Old worker');assert.equal(inactiveToggle.checked,false);assert(templateStartButton.dataset.idempotencyKey);assert.equal(templateInstanceName.value,'New analyst');assert(templateStartStatus.textContent.includes('Retry will use the same request key'));}
 else{assert.equal(visible,true);assert.equal(templateStartButton.dataset.idempotencyKey,undefined);assert.equal(templateInstanceName.value,'');assert(!templateStartStatus.textContent.includes('Turn on Inactive'));assert(!templateStartStatus.textContent.includes('Retry will use the same request key'));assert.equal(window.location.hash,approvals?'library':'');if(!approvals)assert.equal(templateStartStatus.textContent,`Workspace created and paused.${refreshFail?' Reload this page to see it.':''}`);else assert(templateStartStatus.textContent.includes('Review 1 capability'));}
}
(async()=>{await run(false,0);await run(false,1);await run(true,0);await run(false,0,true);await run(false,1,true);})().catch(error=>{console.error(error);process.exit(1)});
'''.replace('HANDLER', handler)
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_confirmed_template_keeps_exact_review_when_browser_storage_is_unavailable():
    static = Path(__file__).parents[1] / 'src/glass_drive_ui/static'
    app = (static / 'app.js').read_text()
    control = (static / 'control-plane.js').read_text()

    def function(source, name):
        match = re.search(rf'^(?:export )?function {name}\(.*?^}}\n', source, re.M | re.S)
        assert match, name
        return match.group(0).replace('export function', 'function', 1)

    start = app.index("  templateStartForm?.addEventListener('submit'")
    handler = app[start:app.index('\n\n  savePreferences?.', start)]
    helpers = '\n'.join((function(control, 'persistCapabilityReview'),
                         function(control, 'capabilityReviewRequest'),
                         function(app, 'rememberCapabilityReview')))
    script = r'''
const assert=require('node:assert/strict');
async function run(denyAll){
 let pendingCapabilityReview=null,submit,creates=0;
 const CAPABILITY_REVIEW_KEY='optional-review-hint';
 const sessionStorage={getItem(){if(denyAll)throw new Error('Storage blocked');return null;},setItem(){throw new Error('Storage full');},removeItem(){if(denyAll)throw new Error('Storage blocked');}};
 const templateStartForm={addEventListener(_event,fn){submit=fn;}};
 const templateSelect={value:'template-1'},templateStartButton={dataset:{},disabled:false},templateInstanceName={value:'Analyst'},templateStartStatus={};
 const workspaceSearch={value:''},workspaceTagFilter={value:''},workspaceKindFilter={value:'named'},inactiveToggle={checked:false};
 const bootstrap={workspace_templates:[{}]},window={location:{hash:''}};
 const item={action_id:'grant-1',kind:'library',reference:'source-1',resolution:'review',route:'library'};
 const postJson=async()=>{creates++;return {workspace:{worker_id:'exact-copied-worker',duplication_report:{reapproval_items:[item]}}};};
 const refreshBootstrap=async()=>{throw new Error('Catalog unavailable after creation');};
HELPERS
HANDLER
 await submit({preventDefault(){}});
 assert.equal(creates,1);assert.equal(window.location.hash,'library');
 assert.equal(templateStartStatus.textContent,'Workspace copied. Review 1 capability before running it.');
 assert.equal(templateStartButton.dataset.idempotencyKey,undefined);
 assert.deepEqual(capabilityReviewRequest(),{workerId:'exact-copied-worker',count:1,items:[item]});
 rememberCapabilityReview('count-only-worker',2,[]);
 assert.deepEqual(capabilityReviewRequest(),{workerId:'count-only-worker',count:2,items:[]});
 persistCapabilityReview(null);assert.equal(capabilityReviewRequest(),null);
}
(async()=>{await run(false);await run(true);})().catch(error=>{console.error(error);process.exit(1)});
'''.replace('HELPERS', helpers).replace('HANDLER', handler)
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
