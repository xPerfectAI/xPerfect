import json
from pathlib import Path
import subprocess

from glass_drive_ui import server


def test_direct_model_effort_uses_runtime_catalog_and_preserves_native_controls():
    source=(Path(server.STATIC_DIR)/'app.js').read_text()
    start=source.index('let directProfileCatalogPromise;')
    end=source.index('\nfunction syncLaunchChoiceUI(',start)
    body=source[start:end]
    script='''
const assert=require('node:assert/strict');
const effort={dataset:{},value:'',disabled:true,replaceChildren(...options){this.options=options;this.value=options[0]?.value||''}};
const field={hidden:true}; const selector={value:'new:direct-anthropic'};
const document={getElementById:()=>effort,querySelector:()=>field,createElement:()=>({})};
const catalog={items:[{profile:'direct-anthropic',models:[{effort_choices:['default','low','medium','high','max'],recommended_effort:'high'}]},
 {profile:'direct-openai',models:[{effort_choices:['default','low','medium','high','xhigh'],recommended_effort:'high'}]}]};
let requests=0; const fetch=async(url)=>{assert.equal(url,'/api/member-profiles');requests++;return {ok:true,json:async()=>catalog}};
''' +body+'''
(async()=>{
 await syncDirectEffort('direct-anthropic',selector);
 assert.equal(field.hidden,false);assert.equal(effort.disabled,false);
 assert.deepEqual(effort.options.map(x=>x.value),['default','low','medium','high','max']);
 assert.equal(effort.value,'high');
 effort.value='low';await syncDirectEffort('direct-anthropic',selector);assert.equal(effort.value,'low');
 selector.value='new:direct-openai';await syncDirectEffort('direct-openai',selector);
 assert.deepEqual(effort.options.map(x=>x.value),['default','low','medium','high','xhigh']);
 assert.equal(effort.value,'high');assert.equal(requests,1);
 selector.value='new:codex-cli';await syncDirectEffort('codex-cli',selector);assert.equal(field.hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
    assert "field.dataset.effortProfile !== profile" in source
    index=(Path(server.STATIC_DIR)/'index.html').read_text()
    assert 'id="direct-effort"' in index
    assert '<option value="none">None</option>' in index  # native choices remain intact


def test_direct_efforts_reach_the_existing_worker_bootstrap():
    for profile,key in [('direct-openai','WPR_CODEX_CLI_REASONING_EFFORT'),('direct-anthropic','WPR_CLAUDE_CODE_EFFORT'),('direct-xai','WPR_GROK_REASONING_EFFORT')]:
        for effort in ['low','medium','high']:
            bundle=server._bootstrap_bundle_with_effort({},profile,effort)
            assert bundle['env'][key]==effort


def test_loading_direct_effort_cannot_submit_previous_provider_value():
    source=(Path(server.STATIC_DIR)/'app.js').read_text()
    start=source.index('function selectedWorkerEffort(')
    end=source.index('\nfunction syncWorkspaceUI(',start)
    script='''const assert=require('node:assert/strict');
const effort={value:'max',disabled:true,dataset:{profile:'direct-anthropic'}};
const document={getElementById:()=>effort};
'''+source[start:end]+'''
assert.equal(selectedWorkerEffort('new:direct-xai',{}),'');
effort.disabled=false;assert.equal(selectedWorkerEffort('new:direct-xai',{}),'');
effort.dataset.profile='direct-xai';effort.value='high';assert.equal(selectedWorkerEffort('new:direct-xai',{}),'high');
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
    submit=source[source.index("  form.addEventListener('submit', async"):]
    assert submit.index('await syncDirectEffort(directProfile, select)') < submit.index('const payload = {')
