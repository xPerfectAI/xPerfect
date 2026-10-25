from pathlib import Path
import json
import re
import subprocess


SOURCE = Path(__file__).parents[1] / "src/glass_drive_ui/static/expert-picker.js"


def test_expert_picker_preserves_exact_owner_keys_review_and_worker_identity():
    source = SOURCE.read_text()
    source = re.sub(r"^import .*;$", "", source, flags=re.M).replace("export ", "")
    harness = r'''
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto'); global.crypto = webcrypto;
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.textContent = ''; this.classList = { toggle() {} }; }
  append(...items) { this.children.push(...items); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  dataset = {};
}
global.document = { createElement: tag => new Element(tag) };
const memory = new Map(); global.sessionStorage = { getItem: key => memory.get(key), setItem: (key, value) => memory.set(key, value) };
let consent = false; const reviewLibraryUse = async () => consent;
function walk(e) { return [e, ...e.children.flatMap(walk)]; }
'''
    cases = r'''
(async () => {
  assert.equal(await expertRequestKey(['owner-a', 'source-v1']), await expertRequestKey(['owner-a', 'source-v1']));
  assert.notEqual(await expertRequestKey(['owner-a', 'source-v1']), await expertRequestKey(['owner-b', 'source-v1']));
  assert.notEqual(await expertRequestKey(['owner-a', 'source-v1']), await expertRequestKey(['owner-a', 'source-v2']));
  const form = new Element('form'); const calls = []; let pendingImport = null;
  const pack = { id: 'synthetic-expert', name: 'Maya · Research Analyst', role: 'Research Analyst', profile: 'claude-code', content_hash: 'sha256:exact', abilities: [] };
  const refs = [{ item_id: 'pli_exact_skill', version_id: 'plv_exact_skill' }];
  const request = async (path, options = {}) => {
    calls.push([path, options]);
    if (path === '/api/workspace-starters') return { items: [pack] };
    if (path.endsWith('/import')) { if (pendingImport) return await pendingImport; return { template: { template_id: 'wst_exact', personal_library_refs: refs } }; }
    if (path.endsWith('/instantiate')) return { workspace: { worker_id: 'wrk_exact', name: 'Maya', profile: 'claude-code' }, approvals_required: [{}] };
    if (path === '/api/workspace/wrk_exact/library' && options.method === 'POST') return { pending: [] };
    if (path === '/api/workspace/wrk_exact/library') return { items: [{ ...refs[0], kind: 'skill', name: 'Exact analysis method', active: true }] };
    if (path === '/api/workspaces/wrk_exact') return { worker_id: 'wrk_exact', name: 'Maya', profile: 'claude-code' };
    throw new Error('unexpected request ' + path);
  };
  const chosen = [];
  const picker = createExpertPicker({ form, request, onChoose: item => chosen.push(item) });
  const section = walk(form).find(e => e.attrs['aria-label'] === 'Choose an expert');
  assert.equal(section.hidden, true, 'Do not show a failed catalog before owner discovery starts');
  const opening = picker.update({ draft_owner_scope: 'owner-a', workspace_templates: [] });
  assert.equal(section.hidden, false);
  assert(walk(section).some(e => e.textContent === 'Loading experts…'));
  await opening;
  assert(!walk(section).some(e => e.textContent.includes('could not be loaded')));
  walk(form).find(e => e.tag === 'button' && e.textContent === 'Choose expert').onclick();
  const settings = { providerAccount: { policy: 'personal_required', account_id: 'acct_exact' }, effort: 'high', name: 'Maya' };
  assert.equal(await picker.prepare(settings), null); // A saved worker is not native approval.
  assert.equal(calls.filter(([path, o]) => path.endsWith('/library') && o.method === 'POST').length, 0);
  const firstImport = calls.find(([path]) => path.endsWith('/import'))[1].body;
  const firstInstance = calls.find(([path]) => path.endsWith('/instantiate'))[1].body;
  assert.deepEqual(firstImport.provider_account, settings.providerAccount); assert.equal(firstImport.effort, 'high');
  consent = true;
  const worker = await picker.prepare(settings);
  assert.equal(worker.worker_id, 'wrk_exact');
  const imports = calls.filter(([path]) => path.endsWith('/import'));
  const instances = calls.filter(([path]) => path.endsWith('/instantiate'));
  assert.equal(imports[1][1].body.idempotency_key, firstImport.idempotency_key);
  assert.equal(instances[1][1].body.idempotency_key, firstInstance.idempotency_key);
  assert.deepEqual(calls.find(([path, o]) => path.endsWith('/library') && o.method === 'POST')[1].body.refs, refs);
  const before = instances.length;
  let finish; pendingImport = new Promise(resolve => { finish = resolve; });
  const flight = picker.prepare(settings);
  while (!finish || calls.filter(([path]) => path.endsWith('/import')).length < 3) await new Promise(resolve => setImmediate(resolve));
  await picker.update({ draft_owner_scope: 'owner-b', workspace_templates: [] });
  finish({ template: { template_id: 'wst_stale', personal_library_refs: refs } });
  await assert.rejects(flight, /account or expert choice changed/);
  assert.equal(calls.filter(([path]) => path.endsWith('/instantiate')).length, before);
  assert.equal(picker.selection(), null);
  assert.equal(picker.owner(), 'owner-b');
  const emptyForm = new Element('form');
  const emptyPicker = createExpertPicker({ form: emptyForm, request: async () => ({ items: [] }) });
  await emptyPicker.update({ draft_owner_scope: 'owner-a' });
  assert(walk(emptyForm).some(e => e.textContent === 'No experts to show. You can create your own worker below.'));
  assert(!walk(emptyForm).some(e => e.textContent.includes('unavailable')));
  const failedForm = new Element('form');
  const failedPicker = createExpertPicker({ form: failedForm, request: async () => { throw new Error('Service offline'); } });
  await failedPicker.update({ draft_owner_scope: 'owner-a' });
  assert(walk(failedForm).some(e => e.textContent.includes('Experts are unavailable: Service offline')));
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(["node", "-e", harness + source + cases], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_expert_styles_keep_tokens_responsive_and_reduced_motion():
    css = SOURCE.with_suffix('.css').read_text()
    assert "var(--panel-strong)" in css and "var(--text)" in css
    assert "prefers-reduced-motion" in css
    assert "@media(max-width:540px)" in css


def test_saved_template_shows_its_bound_account_without_editable_ignored_choices():
    source = SOURCE.with_name('app.js').read_text()
    helper = source[source.index('  function syncExpertControls()'):source.index('  const syncAccountSummary =')]
    script = r'''
const assert = require('node:assert/strict');
const providerAccountPolicy = {}, providerAccountHelp = {};
const providerAccount = { dataset: {}, replaceChildren(item) { this.option = item; } };
const fields = [{ hidden:false }, { hidden:false }];
const document = { createElement: () => ({}), querySelectorAll: () => fields };
let selected = { kind:'template', provider_account_ref:{ policy:'personal_required', account_id:'acct_exact' } };
const expertPicker = { selection: () => selected }, select = { value:'new:claude-code' };
const bootstrap = { provider_accounts:[{ account_id:'acct_exact', label:'My work account', status:'ready' }] };
HELPER
syncExpertControls();
assert.equal(providerAccount.value,'acct_exact'); assert.equal(providerAccount.disabled,true);
assert.equal(providerAccount.option.textContent,'My work account');
assert.equal(providerAccountPolicy.value,'personal_required'); assert.equal(providerAccountPolicy.disabled,true);
assert.equal(providerAccount.dataset.accountState,'ready'); assert.ok(fields.every(field => field.hidden));
assert.match(providerAccountHelp.textContent,/saved with this expert/);
bootstrap.provider_accounts[0].status='error'; syncExpertControls();
assert.equal(providerAccount.value,'acct_exact'); assert.equal(providerAccount.dataset.accountState,'needs_attention');
selected={kind:'starter'}; providerAccount.disabled=false; providerAccount.value='user-picked'; fields[0].hidden=false;
syncExpertControls();
assert.equal(providerAccount.disabled,false); assert.equal(providerAccount.value,'user-picked'); assert.equal(fields[0].hidden,false);
'''.replace('HELPER', helper)
    result = subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_claude_effort_choices_match_the_runtime_owner_without_losing_high():
    import ast
    project = Path(__file__).parents[3]
    runtime = ast.parse((project / 'runtime_phase1/src/workers_projects_runtime/runtime_requirements.py').read_text())
    canonical = next(ast.literal_eval(item.value) for item in runtime.body
        if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'CLAUDE_CODE_EFFORT_LEVELS' for target in item.targets))
    html = SOURCE.with_name('index.html').read_text()
    choices = re.search(r'<select id="claude-effort".*?</select>', html, re.S).group(0)
    assert set(re.findall(r'<option value="([^"]*)"', choices)) == (set(canonical) - {'default'}) | {''}
    source = ast.parse((project / 'frontends/glass-drive-ui/src/glass_drive_ui/server.py').read_text())
    helper = next(item for item in source.body if isinstance(item, ast.FunctionDef) and item.name == '_bootstrap_bundle_with_effort')
    claude = next(item for item in helper.body if isinstance(item, ast.If) and ast.unparse(item.test) == "profile == 'claude-code'")
    assert ast.literal_eval(claude.body[0].test.comparators[0]) == set(canonical)


def test_catalog_retry_is_explicit_single_flight_and_fenced_to_current_owner():
    source = re.sub(r"^import .*;$", "", SOURCE.read_text(), flags=re.M).replace("export ", "")
    script = r'''
const assert=require('node:assert/strict');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.textContent='';this.dataset={};this.classList={toggle(){}};}
 append(...nodes){this.children.push(...nodes);} prepend(...nodes){this.children.unshift(...nodes);}
 replaceChildren(...nodes){this.children=nodes;} setAttribute(key,value){this.attrs[key]=String(value);}
}
global.document={createElement:tag=>new Element(tag)};
global.sessionStorage={getItem:()=>null,setItem(){}};
function walk(e){return [e,...e.children.flatMap(walk)];}
''' + source + r'''
(async()=>{
 const form=new Element('form'),requests=[];let respond=async()=>{throw new Error('Temporary outage');};
 const picker=createExpertPicker({form,request:async(path,options)=>{requests.push({path,options});return respond();}});
 const section=walk(form).find(e=>e.attrs['aria-label']==='Choose an expert');
 const retry=walk(form).find(e=>e.attrs['aria-label']==='Retry loading experts');
 assert.equal(retry.hidden,true);
 await picker.update({draft_owner_scope:'owner-a'});assert.equal(requests.length,1);assert.equal(retry.hidden,false);
 await picker.update({draft_owner_scope:'owner-a'});assert.equal(requests.length,1,'Same-owner bootstrap does not poll failed catalog');
 let finish;respond=()=>new Promise(resolve=>finish=resolve);
 const loading=retry.onclick();assert.equal(requests.length,2);assert.equal(retry.hidden,true);
 await retry.onclick();assert.equal(requests.length,2,'Only one retry can run');
 finish({items:[{id:'one',name:'Demo',profile:'claude-code',abilities:[]}]});await loading;
 assert.equal(retry.hidden,true);assert(walk(form).some(e=>e.textContent==='Choose expert'));
 await picker.update({draft_owner_scope:'owner-a'});assert.equal(requests.length,2);
 // Another failure is independently retryable, then switching owners cancels its authority.
 respond=async()=>{throw new Error('Another outage');};
 await picker.update({draft_owner_scope:'owner-b'});assert.equal(retry.hidden,false);
 respond=()=>new Promise(resolve=>finish=resolve);const old=retry.onclick();
 const staleRequest=requests.at(-1);respond=async()=>({items:[]});
 await picker.update({draft_owner_scope:'owner-c'});assert.equal(staleRequest.options.signal.aborted,true);
 finish({items:[{id:'private-old',name:'Old private name',profile:'claude-code'}]});await old;
 assert(!walk(form).some(e=>e.textContent.includes('Old private name')));assert.equal(retry.hidden,true);
 assert(walk(form).some(e=>e.textContent==='No experts to show. You can create your own worker below.'));
 const afterEmpty=requests.length;await picker.update({draft_owner_scope:'owner-c'});assert.equal(requests.length,afterEmpty,'Valid empty catalog is not retried automatically');
 await picker.update(null);assert.equal(section.hidden,true);assert.equal(retry.hidden,true);
 await retry.onclick();assert.equal(requests.length,afterEmpty,'Hidden signed-out gallery cannot issue requests');
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_function_first_cards_use_stable_artwork_and_preserve_custom_expert_identity():
    source = re.sub(r"^import .*;$", "", SOURCE.read_text(), flags=re.M).replace("export ", "")
    script = r'''
const assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.textContent='';this.dataset={};this.classList={toggle(){}};}
 append(...nodes){this.children.push(...nodes);}prepend(...nodes){this.children.unshift(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}setAttribute(key,value){this.attrs[key]=String(value);}
}
global.document={createElement:tag=>new Element(tag)};
const memory=new Map();global.sessionStorage={getItem:key=>memory.get(key),setItem:(key,value)=>memory.set(key,value)};
const walk=e=>[e,...e.children.flatMap(walk)];
''' + source + r'''
(async()=>{
 const known={id:'bioanalytical-scientist',name:'A renamed fictional expert',role:'Bioanalytical Scientist',profile:'claude-code',abilities:[{included:true,name:'Exact methods'}]};
 const unknown={...known,id:'unknown-public-expert',name:'Theo'};
 const saved={template_id:'bioanalytical-scientist',name:'Theo',role:'Read these files and produce the exact requested output.',profile:'claude-code',personal_library_refs:[]};
 const form=new Element('form');let items=[known,unknown],reads=0;
 const picker=createExpertPicker({form,request:async()=>{reads++;return {items};}});
 await picker.update({draft_owner_scope:'owner-one',workspace_templates:[saved]});
 assert(walk(form).some(e=>e.textContent==='Experts'));assert(walk(form).some(e=>e.textContent==='My experts'));
 let cards=walk(form).filter(e=>e.className==='expert-card');
 assert.equal(walk(cards[0]).find(e=>e.tag==='h3').textContent,known.role);
 assert.equal(walk(cards[0]).find(e=>e.className==='expert-name').textContent,known.name);
 const image=walk(cards[0]).find(e=>e.tag==='img');assert.equal(image.src,'/static/experts/bioanalytical-scientist.webp?v=20261024gouache1');
 assert.equal(image.width,88);assert.equal(image.height,88);assert.equal(image.alt,'');image.onerror();assert.equal(image.hidden,true);
 assert(walk(cards[0]).some(e=>e.className==='expert-avatar-fallback'&&e.innerHTML.includes('<svg')));
 assert.equal(walk(cards[1]).filter(e=>e.tag==='img').length,0,'a matching name or role must not assign another expert portrait');
 const disclosure=cards[0].children.find(e=>e.tag==='details');assert(walk(disclosure).some(e=>e.textContent==='Exact methods'));
 assert(!cards[0].children.some(e=>e.className==='expert-abilities'),'ability lists must be behind the existing disclosure');
 const pick=walk(cards[0]).find(e=>e.className==='expert-choose');assert(pick.attrs['aria-label'].startsWith('Choose expert: Bioanalytical Scientist'));
 pick.onclick();assert.deepEqual(picker.selection(),{...known,kind:'starter'});assert.equal(reads,1,'selection must not import or launch');
 walk(form).find(e=>e.textContent==='My experts').onclick();cards=walk(form).filter(e=>e.className==='expert-card');
 assert.equal(walk(cards[0]).find(e=>e.tag==='h3').textContent,'Theo');
 assert.notEqual(walk(cards[0]).find(e=>e.tag==='h3').textContent,saved.role,'a saved instruction is not a profession');
 const savedDetails=cards[0].children.find(e=>e.tag==='details');
 assert(walk(savedDetails).some(e=>e.textContent==='Role and focus: '+saved.role),'keep supplied role available without promoting it into a title');
 assert.equal(walk(cards[0]).filter(e=>e.tag==='img').length,0);
 assert(walk(cards[0]).some(e=>e.textContent.includes('Starts a new worker')));
 walk(cards[0]).find(e=>e.className==='expert-choose').onclick();
 const restoredForm=new Element('form');
 const restored=createExpertPicker({form:restoredForm,request:async()=>({items})});
 await restored.update({draft_owner_scope:'owner-one',workspace_templates:[saved]});
 assert.equal(restored.selection().kind,'template','the same public ID must not change a saved selection into a starter');
 assert.equal(walk(restoredForm).find(e=>e.textContent==='My experts').attrs['aria-pressed'],'true');
 assert(walk(restoredForm).some(e=>e.className==='expert-choose'&&e.attrs['aria-pressed']==='true'),'restore the selected card in its own collection');
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(['node','-e',script],text=True,capture_output=True,timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_portrait_assets_are_small_local_files_for_the_public_catalog():
    static = SOURCE.parent
    names = ['bioanalytical-scientist','clinical-biostatistician','finance-director','growth-marketer','regulatory-affairs-specialist']
    sizes = []
    for name in names:
        path = static / 'experts' / (name + '.webp')
        raw = path.read_bytes()
        assert raw[:4] == b'RIFF' and raw[8:12] == b'WEBP'
        assert 1000 < len(raw) < 32000
        sizes.append(len(raw))
    assert sum(sizes) < 100000



def test_expert_card_tints_keep_text_readable_and_colours_in_the_shared_owner():
    from test_theme_appearance import _contrast
    css = SOURCE.with_name('theme.css').read_text()
    tokens = {name: (light.lower(), dark.lower()) for name, light, dark in re.findall(
        r"(--[a-z-]+):\s*light-dark\((#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)", css)}
    component = SOURCE.with_suffix('.css').read_text()
    assert 'light-dark(' not in component
    for mode in (0, 1):
        base = tokens['--panel-strong'][mode]
        for name in ['teal','blue','ochre','coral','violet']:
            hue = tokens['--expert-' + name][mode]
            for amount in [0, .015, .03, .045, .06]:
                channels = [round(int(hue[i:i+2],16)*amount + int(base[i:i+2],16)*(1-amount)) for i in (1,3,5)]
                background = '#' + ''.join(f'{value:02x}' for value in channels)
                for foreground in ['--text','--muted']:
                    assert _contrast(tokens[foreground][mode], background) >= 4.5


def test_provider_renderers_keep_current_vector_assets_and_expert_save_name_is_clear():
    for filename in ['control-plane.js','watch.html']:
        text = SOURCE.with_name(filename).read_text()
        assert not re.search(r'provider-(?:codex|claude|grok)\.png', text)
    app = SOURCE.with_name('app.js').read_text()
    assert '${workspaceTileTitle(workspace)} expert' in app
    assert '${workspaceTileTitle(workspace)} template' not in app



def test_saved_expert_panel_keeps_plain_copy_when_the_renderer_updates_it():
    source = SOURCE.with_name('app.js').read_text()
    helper = re.search(r'^function renderWorkspaceTemplateOptions\(.*?^}', source, re.M | re.S).group(0)
    script = "const assert=require('node:assert/strict');\n" + helper + r'''
const select={value:'',replaceChildren(...items){this.items=items;this.value=items[0].value;}};
const button={},meta={};const document={getElementById:id=>({'workspace-template-select':select,'workspace-template-start':button,'workspace-template-meta':meta})[id],createElement:()=>({dataset:{}})};
const workspaceProfileLabel=()=> 'Claude Code';
for(const workspace_templates of [[],[{template_id:'saved-one',name:'My expert',profile:'claude-code',library_refs:[]}]]){
 select.value='';renderWorkspaceTemplateOptions({workspace_templates});
 assert.equal(meta.textContent,'Starts a new worker with this expert’s saved setup. Any new access still needs your review.');
 assert.equal(select.items[0].textContent,workspace_templates.length?'Choose a saved expert':'No saved experts yet');
}
select.value='saved-one';renderWorkspaceTemplateOptions({workspace_templates:[{template_id:'saved-one',name:'My expert',profile:'claude-code',library_refs:[{}]}]});
assert.equal(select.value,'saved-one');assert(meta.textContent.includes('1 Library capability approval will be required.'));
'''
    result = subprocess.run(['node','-e',script],text=True,capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr
