"""Use declared result identity and the existing configured native Watch surface."""
import json
from pathlib import Path
import re
import subprocess

import pytest

from glass_drive_ui.server import flatten_workspaces

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def test_explicit_no_current_result_does_not_promote_a_historical_file():
    uri = (STATIC / 'delivery-presenter.js').as_uri()
    script = f"import {{workspaceDeliveryModel,watchOutputModel}} from {json.dumps(uri)};" + r'''
import assert from 'node:assert/strict';
const artifacts={items:[{path:'old.txt',open_url:'/old',download_url:'/download-old'}]};
const data={latest_run:{state:'completed'},latest_output:'Scheduled check complete.',deliverable:null,artifacts};
const model=workspaceDeliveryModel(data);
assert.equal(model.available,true);assert.equal(model.primary,null);assert.equal(model.artifacts.length,1);
assert(!watchOutputModel(data).summary.includes('old.txt'));
assert.equal(workspaceDeliveryModel({latest_run:data.latest_run,artifacts}).primary.label,'old.txt','older clients without a declaration retain their inventory fallback');
assert.equal(workspaceDeliveryModel({...data,deliverable:{workspace_path:'new.txt',open_url:'/new',download_url:'/download-new'}}).primary.label,'new.txt');
'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('surface', ['desktop', 'terminal'])
def test_saved_catalog_watch_uses_configured_default(monkeypatch, surface):
    monkeypatch.setenv('GLASSHIVE_DEFAULT_LAUNCH_SURFACE', surface)
    class Client:
        def list_workspace_catalog(self, **_kwargs):
            return {'items':[{'worker_id':'worker-one','project_id':'project-one','name':'Expert','state':'ready'}]}
    assert f'surface={surface}' in flatten_workspaces(Client())[0]['watch_url']


@pytest.mark.parametrize('surface', ['desktop', 'terminal'])
def test_library_request_follows_same_native_surface(surface):
    source = (STATIC / 'control-plane.js').read_text()
    match = re.search(r'^async function submitLibraryRequest\(event\) \{.*?^\}', source, re.M | re.S)
    assert match
    script = r'''
const assert=require('node:assert/strict');
let libraryRequestPending=false,assigned='',sent=0;
const workspaceCatalog={items:[{worker_id:'expert'}]};
const fields={'library-request-workspace':{value:'expert'},'library-request-text':{value:'Use native setup'},'library-request-status':{},'launch-surface':{value:SURFACE}};
const document={getElementById(id){return fields[id];}};
const api={async postJson(path,payload){sent++;assert.equal(path,'/api/workspace/expert/message');assert.equal(payload.message,'Use native setup');},withAuth(url){return url;}};
const window={location:{assign(url){assigned=url;}}};
HANDLER
(async()=>{await submitLibraryRequest({preventDefault(){},currentTarget:{querySelector(){return {disabled:false};}}});assert.equal(sent,1);assert.equal(assigned,'/watch/expert?surface='+SURFACE);assert.equal(libraryRequestPending,false);})().catch(e=>{console.error(e);process.exit(1)});
'''.replace('HANDLER', match.group(0)).replace('SURFACE', json.dumps(surface))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('surface', ['desktop', 'terminal'])
def test_paged_catalog_follows_configured_native_surface(surface):
    source = (STATIC / 'app.js').read_text()
    match = re.search(r'^function decorateCatalogWorkspace\(workspace\) \{.*?^\}', source, re.M | re.S)
    assert match
    script = r'''
const assert=require('node:assert/strict');
const document={getElementById(){return {value:SURFACE};}};
const rawWorkspaceState=()=> 'ready',workspaceProfileLabel=()=> 'Native worker';
const ACTIVE_STATES=new Set(),RESUME_STATES=new Set(['ready']);
HELPER
const value=decorateCatalogWorkspace({worker_id:'expert',project_id:'project',name:'Analyst'});
assert.equal(value.watch_url,'/watch/expert?project_id=project&surface='+SURFACE);
assert.equal(value.workspace_url,value.watch_url);assert.equal(value.is_resumable,true);
'''.replace('HELPER', match.group(0)).replace('SURFACE', json.dumps(surface))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
