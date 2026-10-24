"""Library UI data contracts and affected surface guards. Real browser QA is separate."""
import json
from pathlib import Path
import subprocess

STATIC = Path(__file__).parents[1] / 'src' / 'glass_drive_ui' / 'static'


def node_script(script):
    result = subprocess.run(['node', '--input-type=module', '--eval',
        f"import * as library from {json.dumps((STATIC / 'personal-library.js').as_uri())};\n" + script],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_library_selection_keeps_exact_versions():
    node_script(r'''
import assert from 'node:assert/strict';
const ref = {item_id:'item-1',version_id:'version-1'};
assert.deepEqual(library.uniqueLibraryRefs([ref,ref]),[ref]);
assert.throws(()=>library.uniqueLibraryRefs([{item_id:'item-1'}]),/no saved version/);
assert.throws(()=>library.uniqueLibraryRefs([ref,{...ref,version_id:'version-2'}]),/Two versions/);
''')


def test_library_requests_preserve_typed_errors_without_claiming_ready():
    node_script(r'''
import assert from 'node:assert/strict';
globalThis.document={cookie:'glasshive_csrf=synthetic%20token'};
let seen;
globalThis.fetch=async(path,options)=>{seen={path,options};return {ok:true,status:200,json:async()=>({items:[]})};};
await library.libraryRequest('/api/personal-library',{method:'POST',body:{kind:'note',text:'Synthetic note'}});
assert.equal(seen.options.headers['X-GlassHive-CSRF'],'synthetic token');
assert.equal(seen.options.credentials,'same-origin');
assert.equal(seen.options.cache,'no-store');
globalThis.fetch=async()=>({ok:false,status:409,json:async()=>({detail:{code:'worker_busy',message:'Worker is busy.',recovery:'Wait until it is idle.'}})});
await assert.rejects(library.libraryRequest('/api/workspace/w/library/a',{method:'DELETE'}),error=>error.status===409&&error.code==='worker_busy'&&/idle/.test(error.message));
globalThis.document.cookie='glasshive_csrf=%xx';
globalThis.fetch=async()=>({ok:true,status:204});
assert.deepEqual(await library.libraryRequest('/api/personal-library/a',{method:'DELETE'}),{});
assert.match(library.libraryStatus({state:'placed'}),/not confirmed/);
assert.match(library.libraryStatus({state:'needs_setup'}),/Needs native setup/);
assert.match(library.libraryStatus({state:'ready_for_next_start'}),/next message/);
''')


def test_watch_keeps_files_first_and_private_manager_gates_and_full_undo():
    html = (STATIC / 'watch.html').read_text()
    source = (STATIC / 'watch.js').read_text()
    assert html.index('id="watch-library-tab-files"') < html.index('id="watch-library-tab-added"') < html.index('id="watch-library-tab-saved"')
    assert 'id="watch-files-list"' in html
    assert "libraryManager = data?.can_manage_workspace === true && !signedToken" in source
    assert 'watchLibrary?.setAccess(allowed, owner);' in source
    assert "['queued', 'running', 'settling']" in source
    assert '/library/${encodeURIComponent(item.application_id)}/undo' in source
    assert 'can_undo' in source or 'undo_available' in source
    assert 'await workspaceFiles.refresh()' in source
    assert 'Keep for reuse' in source


def test_knowledge_editor_uses_one_upload_transport_and_complete_edit_payload():
    source = (STATIC / 'personal-library.js').read_text()
    assert "import { createFileDraft, requestFileName, formatBytes }" in source
    assert "scope: 'personal-library'" in source
    assert 'next.upload_ids = ready.length' in source
    assert "['Edit', async () =>" in source
    assert 'const detail = await request(`/api/personal-library/${encodeURIComponent(item.item_id)}`)' in source
    assert "new TextDecoder('utf-8', { fatal: true })" in source
    assert "next.profiles = ['claude-code']" in source
    assert '.claude/skills/${packageName}/${within}' in source
    assert 'binary assets intact' in source
    assert "signal?.addEventListener('abort', abort" in source
    assert 'pickerController?.abort(); ownerLifecycle.abort();' in source
    assert '/api/file-uploads' not in source


def test_job_and_chat_use_same_picker_and_keep_context_truth():
    launch = (STATIC / 'app.js').read_text()
    chat = (STATIC / 'conversation.js').read_text()
    html = (STATIC / 'index.html').read_text()
    assert "scope: 'run-project'" in launch
    assert 'fileRefs = fileDraft.readyRefs();' in launch
    assert 'launchLibrary.resolve(selectedLibraryRefs)' in launch
    assert 'library_refs: selectedLibraryRefs, confirm_library: confirmLibrary' in launch
    assert 'file_upload_ids:' in launch and 'file_upload_revisions:' in launch
    assert 'createLibrarySelection' in chat
    assert 'snapshot.library?.refs' in chat
    assert 'library_refs:selectedLibrary' in chat
    assert "['binary', 'over_limit']" in chat
    assert 'Their full references stay available to its workers.' in chat
    assert 'id="personal-library-main"' in html
    assert 'id="project-library-selection"' in html


FAKE_DOM = r'''
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) Object.defineProperty(globalThis,'crypto',{value:webcrypto});
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this.className='';this.dataset={};this.style={};this.hidden=false;this.disabled=false;this.value='';this.isConnected=true;this.classList={add:()=>{},remove:()=>{}};}
  append(...children){for(const child of children){if(child){if(child.parentNode)child.parentNode.children=child.parentNode.children.filter(value=>value!==child);child.parentNode=this;this.children.push(child);}}}
  appendChild(child){this.append(child);return child;}
  prepend(...children){for(const child of children){if(child)child.parentNode=this;}this.children.unshift(...children);}
  replaceChildren(...children){this.children=[];this.append(...children);}
  querySelector(selector){return this.children.flatMap(nodes).find(node=>selector.startsWith('.')?(node.className||'').split(' ').includes(selector.slice(1)):node.tagName===selector.toUpperCase())||null;}
  closest(selector){for(let node=this;node;node=node.parentNode)if(selector.startsWith('.')&&node.className.split(' ').includes(selector.slice(1)))return node;return null;}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,fn){(this.listeners[name] ||= []).push(fn);}
  removeEventListener(name,fn){this.listeners[name]=(this.listeners[name]||[]).filter(value=>value!==fn);}
  dispatch(name,event={}){for(const fn of this.listeners[name]||[])fn(event);}
  showModal(){this.open=true;}
  close(){if(!this.open)return;this.open=false;this.dispatch('close');}
  remove(){this.isConnected=false;this.parentNode.children=this.parentNode.children.filter(child=>child!==this);}
  click(){if(this.disabled)return;const event={target:this};const checkbox=this.tagName==='INPUT'&&this.type==='checkbox';if(checkbox)this.checked=!this.checked;this.onclick?.(event);this.dispatch('click',event);if(checkbox){this.onchange?.(event);this.dispatch('change',event);}}
  getBoundingClientRect(){return {width:parseFloat(this.style.width)||680};}
  focus(){document.activeElement=this;}
  select(){}
  reportValidity(){return true;}
}
const body=new Element('body');
globalThis.document={cookie:'',body,addEventListener(){},createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),activeElement:null};
const displayListeners=new Map(),animationFrames=new Map();let nextAnimationFrame=0;
globalThis.window={innerWidth:1440,innerHeight:900,dispatchEvent(){},addEventListener(name,fn){if(!displayListeners.has(name))displayListeners.set(name,new Set());displayListeners.get(name).add(fn);},removeEventListener(name,fn){displayListeners.get(name)?.delete(fn);}};
globalThis.requestAnimationFrame=fn=>{const id=++nextAnimationFrame;animationFrames.set(id,fn);return id;};
globalThis.cancelAnimationFrame=id=>animationFrames.delete(id);
const nodes=(element)=>[element,...(element.children||[]).flatMap(nodes)];
const tick=()=>new Promise(resolve=>setImmediate(resolve));
'''


def test_private_editor_gets_complete_version_preserves_failed_draft_and_flushes_on_owner_change():
    node_script(FAKE_DOM + r'''
const mount=new Element();
let editReads=0;
const request=async(path,options={})=>{
  if(path.startsWith('/api/personal-library?')) return {items:[{item_id:'item-1',version_id:'v-1',current_version_id:'v-1',version:1,kind:'note',name:'Useful note',state:'active',payload:{}}]};
  if(path==='/api/personal-library/item-1' && !options.method){editReads++;return {item_id:'item-1',version_id:'v-1',current_version_id:'v-1',version:1,kind:'note',name:'Useful note',payload:{text:'Complete retained text'}};}
  if(options.method==='PATCH')throw new Error('Library is temporarily unavailable.');
  throw new Error(`Unexpected request ${path}`);
};
const browser=library.createLibraryBrowser({mount,request,approved:false});
browser.setAccess(true,'owner-one');await tick();
const edit=nodes(mount).find(node=>node.textContent==='Edit');edit.onclick();await tick();
assert.equal(editReads,1);
const dialog=body.children[0],form=nodes(dialog).find(node=>node.tagName==='FORM');
const input=nodes(dialog).find(node=>node.tagName==='TEXTAREA');
assert.equal(input.value,'Complete retained text');input.value='Retained after failure';
form.dispatch('submit',{preventDefault(){}});await tick();
assert.equal(dialog.open,true);assert.equal(input.value,'Retained after failure');
assert.ok(nodes(dialog).some(node=>node.textContent==='Library is temporarily unavailable.'&&node.hidden===false));
nodes(dialog).find(node=>node.textContent==='Cancel').click();
assert.equal(document.activeElement['aria-label'],'Options for Useful note');
nodes(mount).find(node=>node.textContent==='Write a note').click();
const noteDialog=body.children[0];
nodes(noteDialog).find(node=>node.textContent==='Cancel').click();
assert.equal(document.activeElement['aria-label'],'Add to My Library');
browser.setAccess(false,'');
assert.equal(body.children.length,0);assert.equal(mount.hidden,true);assert.equal(nodes(mount).filter(node=>node.textContent==='Useful note').length,0);
''')


def test_late_owner_response_cannot_repopulate_private_library():
    node_script(FAKE_DOM + r'''
const mount=new Element();let complete;
const browser=library.createLibraryBrowser({mount,approved:false,request:()=>new Promise(resolve=>{complete=resolve;})});
browser.setAccess(true,'owner-one');browser.setAccess(false,'');
complete({items:[{item_id:'item-1',version_id:'v-1',version:1,kind:'note',name:'Private previous owner',state:'active'}]});await tick();
assert.equal(mount.hidden,true);assert.ok(!nodes(mount).some(node=>node.textContent==='Private previous owner'));
''')


def test_closed_target_blocks_apply_without_blocking_private_library_browsing():
    node_script(FAKE_DOM + r'''
const mount=new Element();let picked=0,resolved=0;
const browser=library.createLibraryBrowser({mount,approved:false,onPick:()=>{picked++;},request:async(path)=>{
 if(path.endsWith('/resolve')){resolved++;return {items:[]};}
 return {items:[{item_id:'item-1',version_id:'version-1',version:1,kind:'note',name:'Useful note',state:'active'}]};
}});
browser.setAccess(true,'owner-one');await tick();
const check=nodes(mount).find(node=>node['aria-label']==='Select Useful note');check.checked=true;check.onchange();
const choose=nodes(mount).find(node=>node.textContent==='Use selected');
browser.setPickAvailable(false,'This workspace is closed');assert.equal(choose.disabled,true);await choose.onclick();
assert.equal(picked,0);assert.equal(resolved,0);assert.ok(nodes(mount).some(node=>node.textContent==='Useful note'));
browser.setPickAvailable(true);assert.equal(choose.disabled,false);await choose.onclick();assert.equal(picked,1);assert.equal(resolved,1);
browser.destroy();
''')


def test_target_closing_during_review_or_setup_cannot_send_a_mutation():
    source = (STATIC / 'watch.js').read_text()
    start = source.index('onPick: async (refs, resolved, { signal }) => {')
    end = source.index("}, pickLabel: 'Add here'", start) + 1
    apply = 'const applySelected = ' + source[start + len('onPick: '):end] + ';'
    node_script(FAKE_DOM + r'''
let closed=false,posted=0,approve;
const libraryTargetClosed=()=>closed,currentLibraryOwnerScope=()=> 'owner-one';
const libraryManager=true,librarySnapshot={worker:{name:'Exact worker'}},workerId='worker-one';
const reviewLibraryUse=()=>new Promise(resolve=>{approve=resolve;});
const libraryRequest=async()=>{posted++;};
const workspaceFiles={refresh:async()=>{}},refreshAddedLibrary=async()=>{},selectLibraryTab=()=>{};
''' + apply + r'''
const pending=applySelected([{item_id:'i',version_id:'v'}],{items:[{kind:'skill'}]},{signal:new AbortController().signal});
closed=true;approve(true);await assert.rejects(pending,/workspace is closed/);assert.equal(posted,0);
closed=false;
const view=library.askNativeLibrarySetup({workerId:'worker-one',item:{},canStart:()=>!closed,request:libraryRequest});
const input=nodes(view.dialog).find(node=>node.tagName==='TEXTAREA');input.value='Keep this setup draft';
closed=true;nodes(view.dialog).find(node=>node.tagName==='FORM').dispatch('submit',{preventDefault(){}});await tick();
assert.equal(posted,0);assert.equal(view.dialog.open,true);assert.equal(input.value,'Keep this setup draft');
assert.ok(nodes(view.dialog).some(node=>/workspace is closed/.test(node.textContent||'')));view.close();
''')


def test_approved_review_rechecks_target_after_resource_lookup():
    source = (STATIC / 'watch.js').read_text()
    start = source.index('function libraryUndoButton(')
    end = source.index("\nfor (const button of document.querySelectorAll('.watch-library-tabs", start)
    actual = source[start:end] + '\n' + source[source.index('function libraryTargetClosed()'):source.index('\nlet libraryAddedEpoch')]
    node_script(FAKE_DOM + r'''
const mount=new Element(),status=new Element('p');document.getElementById=id=>id==='watch-library-added-list'?mount:status;
let libraryManager=true,libraryAddedEpoch=0,librarySnapshot={worker:{state:'completed'}},finishResources,posted=0;
const controller=new AbortController(),watchLibrary={signal:()=>controller.signal},workerId='worker-one';
const currentLibraryOwnerScope=()=> 'owner-one',libraryStatus=library.libraryStatus;
const libraryRequest=async(path,options={})=>{
 if(options.method){posted++;return {};}
 if(path==='/api/control-plane?sections=resources')return new Promise(resolve=>{finishResources=resolve;});
 if(path.startsWith('/api/personal-library/'))return {payload:{library_id:'approved-one'}};
 return {items:[{application_id:'application-one',item_id:'item-one',version_id:'version-one',name:'Approved item',state:'needs_review',kind:'approved'}]};
};
''' + actual + r'''
await refreshAddedLibrary();const setup=nodes(mount).find(node=>node.textContent==='Review');const pending=setup.onclick();await tick();
librarySnapshot.worker.state='terminated';finishResources({library:[{library_id:'approved-one',scopes:[]}]});await pending;
assert.equal(posted,0);assert.match(status.textContent,/workspace is closed/);assert.equal(setup.disabled,true);
''')


def test_review_names_the_selected_saved_worker_and_never_the_hidden_new_name():
    node_script(r'''
import assert from 'node:assert/strict';
assert.equal(library.libraryWorkerTarget({value:'open:worker-a',label:'Research · Codex · Account A · Workspace A',name:'Stale new name'}),'Research · Codex · Account A · Workspace A');
assert.equal(library.libraryWorkerTarget({value:'existing:worker-a',label:'Research',name:'Stale new name'}),'Research');
assert.match(library.libraryWorkerTarget({value:'duplicate:worker-a',label:'Research',name:'Stale new name'}),/copy of Research/);
assert.equal(library.libraryWorkerTarget({value:'new:claude-code',label:'Claude Code',name:'Editor'}),'Editor');
assert.equal(library.libraryWorkerTarget({value:'new:codex-cli',label:'Codex worker'}),'the new Codex worker');
assert.equal(library.libraryWorkerTarget(), 'the new worker');
''')
    app = (STATIC / 'app.js').read_text()
    watch = (STATIC / 'watch.js').read_text()
    assert "data.library_action_required && !data.capabilities_requiring_reapproval" not in app
    assert 'watchHref(data.library_watch_url || data.watch_url' in app
    assert "requestedLibraryEntry = new URLSearchParams(window.location.search).get('library') === 'added'" in watch
    assert "if (allowed && requestedLibraryEntry)" in watch


def test_review_explains_declared_profile_mismatch_before_acceptance():
    node_script(FAKE_DOM + r'''
const pending=library.reviewLibraryUse([{item_id:'skill-one',kind:'skill',name:'Report skill',version:1,payload:{profiles:['claude-code']}}], 'Selected worker', {profile:'codex-cli'});
const dialog=body.children.at(-1);
assert.ok(nodes(dialog).some(node=>/Not available for this worker type/.test(node.textContent||'')));
nodes(dialog).find(node=>node.textContent==='Cancel').onclick();assert.equal(await pending,false);
''')


def test_library_menu_actions_keep_visible_labels_as_accessible_names():
    source = (STATIC / 'personal-library.js').read_text()
    assert "button.setAttribute('aria-label', title); button.onclick = () => { menu.open = false" in source
    assert "action.setAttribute('aria-label', title);" in source


def test_library_add_files_button_opens_existing_input_and_refuses_old_owner():
    node_script(FAKE_DOM + r'''
globalThis.sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
globalThis.fetch=async()=>({ok:true,status:200,json:async()=>({items:[]})});
const mount=new Element();
const browser=library.createLibraryBrowser({mount,approved:false,request:async()=>({items:[]})});
browser.setAccess(true,'a'.repeat(64));await tick();
const add=nodes(mount).find(node=>node.textContent==='Add files');add.onclick();await tick();
const dialog=body.children[0];
const input=nodes(dialog).find(node=>node.tagName==='INPUT'&&node.type==='file');
let opened=0;input.click=()=>{opened++;};
const button=nodes(dialog).find(node=>node.tagName==='BUTTON'&&node.textContent==='Add files');
button.onclick();assert.equal(opened,1);
browser.setAccess(false,'');button.onclick();assert.equal(opened,1);
''')


def test_skill_folder_skips_os_metadata_but_keeps_real_binary_rejection():
    node_script(FAKE_DOM + r'''
const encoded=text=>new TextEncoder().encode(text).buffer;
const file=(name,path,content)=>({name,webkitRelativePath:`editor/${path}`,size:content.byteLength,arrayBuffer:async()=>content});
for(const binary of [false,true]){
  const mount=new Element();let saved;
  const request=async(path,options={})=>{if(options.method==='POST')saved=options.body;return {items:[]};};
  const browser=library.createLibraryBrowser({mount,approved:false,request});browser.setAccess(true,'a'.repeat(64));await tick();
  nodes(mount).find(node=>node.textContent==='Add a skill folder').onclick();
  const dialog=body.children.at(-1),form=nodes(dialog).find(node=>node.tagName==='FORM');
  nodes(dialog).find(node=>node.tagName==='INPUT'&&node.type==='text').value='Editorial skill';
  const folder=nodes(dialog).find(node=>node.tagName==='INPUT'&&node.type==='file');
  folder.files=[file('SKILL.md','SKILL.md',encoded('---\nname: editor\n---\nFollow the editorial guide.')),file('.DS_Store','.DS_Store',new Uint8Array([255]).buffer),file('._SKILL.md','._SKILL.md',new Uint8Array([255]).buffer)];
  if(binary)folder.files.push(file('image.png','assets/image.png',new Uint8Array([255,254]).buffer));
  folder.onchange();assert.ok(nodes(dialog).some(node=>node.textContent==='Skipped 2 system files.'));
  form.dispatch('submit',{preventDefault(){}});await tick();await tick();
  if(binary){assert.equal(saved,undefined);assert.equal(dialog.open,true);assert.ok(nodes(dialog).some(node=>/not a text file/.test(node.textContent||'')));}
  else{assert.equal(saved.files.length,1);assert.equal(saved.files[0].path,'.claude/skills/editor/SKILL.md');assert.deepEqual(saved.profiles,['claude-code']);}
  browser.destroy();
}
assert.deepEqual(library.skillFolderFiles([{name:'Thumbs.db'},{name:'desktop.ini'},{name:'real.md'}]).files,[{name:'real.md'}]);
''')


def test_capture_retry_reuses_uncertain_key_but_completed_capture_gets_new_key():
    node_script(FAKE_DOM + r'''
const mount=new Element(),keys=[];let fail=false;
const request=async(path,options={})=>{
  if(path.endsWith('/capture')){keys.push(options.body.idempotency_key);if(fail){fail=false;throw new Error('temporary failure');}}
  return {items:[]};
};
const browser=library.createLibraryBrowser({mount,workerId:'worker-1',approved:false,request});browser.setAccess(true,'a'.repeat(64));await tick();
async function capture(){
  const pending=browser.capture({file_id:'file-1',revision:'revision-1',name:'notes.txt'});await tick();
  const dialog=body.children.at(-1),form=nodes(dialog).find(node=>node.tagName==='FORM');
  form.dispatch('submit',{preventDefault(){}});return pending;
}
await capture();await capture();assert.notEqual(keys[0],keys[1]);
fail=true;await assert.rejects(capture(),/temporary failure/);await capture();assert.equal(keys[2],keys[3]);
browser.destroy();
''')


def test_delete_shows_exact_retained_holders_before_sending_delete():
    node_script(FAKE_DOM + r'''
const mount=new Element();let deleted=false;
const request=async(path,options={})=>{
  if(options.method==='DELETE'){deleted=true;return {};}
  if(path==='/api/personal-library/item-1')return {item_id:'item-1',name:'Shared guide',holders:[{holder_kind:'bundle',holder_id:'bundle-1',label:'Bundle: Onboarding'},{holder_kind:'template',holder_id:'template-1',label:'Template: Researcher'}]};
  return {items:[{item_id:'item-1',version_id:'v-1',kind:'note',name:'Shared guide',version:1,state:'active'}]};
};
const browser=library.createLibraryBrowser({mount,approved:false,request});browser.setAccess(true,'a'.repeat(64));await tick();
nodes(mount).find(node=>node.textContent==='Delete from My Library').onclick();await tick();
assert.equal(deleted,false);const dialog=body.children.at(-1);
assert.ok(nodes(dialog).some(node=>node.textContent==='Bundle: Onboarding'));
assert.ok(nodes(dialog).some(node=>node.textContent==='Template: Researcher'));
nodes(dialog).find(node=>node.tagName==='FORM').dispatch('submit',{preventDefault(){}});await tick();assert.equal(deleted,true);
browser.destroy();
''')


def test_unconfirmed_native_cleanup_is_not_reported_as_removed_and_offers_same_delete_retry():
    node_script(r'''
import assert from 'node:assert/strict';
const pending=library.libraryRemovalOutcome({native_cleanup:{state:'unconfirmed'}});
assert.equal(pending.needsRetry,true);assert.match(pending.message,/have not been removed/);
assert.equal(library.libraryRemovalOutcome({native_cleanup:{state:'removed'}}).needsRetry,false);
assert.match(library.libraryRemovalOutcome({native_cleanup:{retained:[{path:'edited.md'}]}}).message,/copies were kept/);
assert.match(library.libraryStatus({state:'enabled'}),/Worker load not confirmed/);
assert.match(library.libraryStatus({state:'cleanup_unconfirmed'}),/files still need cleanup/);
''')
    watch = (STATIC / 'watch.js').read_text()
    assert "retry.textContent = 'Retry file cleanup'" in watch
    assert 'await remove.onclick()' in watch
    assert "item.state === 'cleanup_unconfirmed' ? 'Retry file cleanup'" in watch


def test_added_here_reload_restores_logical_undo_and_uses_same_owner_endpoint():
    source = (STATIC / 'watch.js').read_text()
    start = source.index('function libraryUndoButton(')
    end = source.index("\nfor (const button of document.querySelectorAll('.watch-library-tabs", start)
    actual = source[start:end] + '\n' + source[source.index('function libraryTargetClosed()'):source.index('\nlet libraryAddedEpoch')]
    node_script(FAKE_DOM + r'''
const mount=new Element(),status=new Element('p');
document.getElementById=id=>id==='watch-library-added-list'?mount:status;
let libraryManager=true,libraryAddedEpoch=0,librarySnapshot={latest_run:{state:'completed'}};
const controller=new AbortController(),watchLibrary={signal:()=>controller.signal},watchDraft={ownerScope:()=> 'owner-one'},workerId='worker-1';
const currentLibraryOwnerScope=()=>watchDraft.ownerScope();
const libraryStatus=library.libraryStatus;
let calls=[],restored=false,refreshed=0;
const workspaceFiles={refresh:async()=>{refreshed++;}};
const libraryRequest=async(path,options={})=>{
 calls.push({path,options});
 if(options.method==='POST'){restored=true;return {state:'restored'};}
 return {items:[{application_id:'app-1',item_id:'item-1',version_id:'version-1',name:'Knowledge guide',kind:'knowledge',state:restored?'in_files':'removed',active:restored,undo_available:!restored}]};
};
''' + actual + r'''
await refreshAddedLibrary();
assert.ok(nodes(mount).some(node=>node.textContent==='Removed · Undo available'));
assert.ok(!nodes(mount).some(node=>node.textContent==='Remove here'));
const undo=nodes(mount).find(node=>node.textContent==='Undo');assert.equal(undo.disabled,false);await undo.onclick();
const restoredCalls=calls.filter(call=>call.options.method==='POST');assert.equal(restoredCalls.length,1);
assert.equal(restoredCalls[0].path,'/api/workspace/worker-1/library/app-1/undo');assert.equal(refreshed,1);
assert.ok(nodes(mount).some(node=>/Files ready/.test(node.textContent||'')));
librarySnapshot={latest_run:{state:'running'}};assert.equal(libraryUndoButton({application_id:'app-1'},status).disabled,true);
assert.match(library.libraryRemovalOutcome({retained_capabilities:[{library_id:'library-1',references:1}]}).message,/stays enabled for another selected Library item/);
''')


def test_delivered_capture_never_substitutes_newer_path_only_file_bytes():
    source = (STATIC / 'watch.js').read_text()
    start = source.index('function syncResultActions(')
    end = source.index('\nfunction formatArtifactSize(', start)
    actual = source[start:end]
    node_script(FAKE_DOM + r'''
const resultActions=new Element(),filesPanel=new Element(),filesToggle=new Element(),filesStatus=new Element();
let libraryManager=true,currentRevision='published-revision',saved=0,opened=[];
const watchDraft={ownerScope:()=> 'owner-one'};
const currentLibraryOwnerScope=()=>watchDraft.ownerScope();
const fileError=message=>{filesStatus.textContent=message;filesStatus.hidden=false;};
const selectLibraryTab=()=>{};
const workspaceFiles={open:async directory=>{opened.push(directory);filesStatus.textContent='';}};
const watchLibrary={capture:async file=>{assert.equal(file.revision,'published-revision');if(file.revision!==currentRevision)throw new Error('File changed. Refresh Files to choose its current version.');saved++;}};
''' + actual + r'''
syncResultActions({kind:'file',file_id:'file-1',revision:'published-revision'});
const exact=nodes(resultActions).find(node=>node.textContent==='Keep for reuse');assert.ok(exact);
currentRevision='newer-revision';await exact.onclick();assert.equal(saved,0);assert.match(filesStatus.textContent,/File changed/);
syncResultActions({kind:'file',source:'workspace_file',workspace_path:'reports/result.txt'});
assert.ok(!nodes(resultActions).some(node=>node.textContent==='Keep for reuse'));
const current=nodes(resultActions).find(node=>node.textContent==='Save current file…');await current.onclick();
assert.deepEqual(opened,['reports']);assert.equal(saved,0);assert.match(filesStatus.textContent,/current version may differ/);
await assert.rejects(keepDeliveredFile({kind:'file',workspace_path:'reports/result.txt'}),/no saved file version/);assert.equal(saved,0);
''')


def test_added_here_deep_link_starts_existing_files_owner_check_before_consuming_request():
    source = (STATIC / 'watch.js').read_text()
    start = source.index('function syncLibraryAccess() {')
    end = source.index('\nfunction selectLibraryTab(', start)
    actual = source[start:end] + '\n' + source[source.index('function libraryTargetClosed()'):source.index('\nlet libraryAddedEpoch')]
    node_script(FAKE_DOM + r'''
const tabs={added:new Element(),saved:new Element()},addedMount=new Element(),filesPanel=new Element(),filesToggle=new Element();
document.getElementById=id=>id==='watch-library-added-list'?addedMount:tabs[id.endsWith('added')?'added':'saved'];
document.querySelectorAll=()=>[];document.querySelector=()=>({id:'watch-library-tab-files'});
let libraryManager=false,scope='',pendingLibraryTab='',requestedLibraryEntry=true,requestedLibraryPrepared=false,libraryAddedEpoch=0,librarySnapshot={latest_run:null};
const watchDraft={ownerScope:()=>scope},watchLibrary={setAccess(){},setPickAvailable(){}};
const currentLibraryOwnerScope=()=>libraryManager?scope:'';let opened=0,selected=[];
const workspaceFiles={setKeepForReuseAccess(){},open:async()=>{opened++;await tick();scope='verified-owner';syncLibraryAccess();}};
const selectLibraryTab=name=>selected.push(name);
''' + actual + r'''
syncLibraryAccess();assert.equal(opened,0);assert.equal(requestedLibraryEntry,true);
libraryManager=true;filesPanel.hidden=true;syncLibraryAccess();
assert.equal(opened,1);assert.equal(filesPanel.hidden,false);assert.equal(requestedLibraryEntry,true);
await tick();await tick();assert.equal(requestedLibraryEntry,false);assert.equal(selected.at(-1),'added');
syncLibraryAccess();assert.equal(opened,1);assert.equal(tabs.added.hidden,false);
''')


def test_closed_manager_can_capture_with_readonly_files_but_viewer_or_shared_link_cannot():
    source = (STATIC / 'watch.js').read_text()
    start = source.index('async function activateWatchDraft(')
    end = source.index('\nasync function ', start + 10)
    activation = source[start:end]
    current_owner = source[source.index('function currentLibraryOwnerScope()'):source.index('\nlet librarySnapshot')]
    snapshot = source[source.index('function syncWatchLibrarySnapshot('):source.index('\nfunction syncLibraryAccess(')]
    node_script(FAKE_DOM + r'''
let libraryManager=true,libraryOwnerScope='',librarySnapshot=null,signedToken='',watchDraftScopeLoad=0,watchDraftScopeController=null,filesCanWrite=false,draftScope=null;
const filesUploadArea=new Element();let bootstrapReads=0,captured=[];
const watchDraft={ownerScope:()=>draftScope,setOwnerScope:value=>{draftScope=value;}};
const setWatchDraftBusy=()=>{};const fileError=message=>{throw new Error(message);};
const owner='a'.repeat(64);
document.cookie='glasshive_csrf=synthetic-logged-in-cookie';
globalThis.fetch=async path=>{assert.equal(path,'/api/bootstrap');bootstrapReads++;return {ok:true,json:async()=>({draft_owner_scope:owner,csrf_token:'synthetic'})};};
''' + current_owner + r'''
const browser=library.createLibraryBrowser({mount:new Element(),workerId:'closed-worker',approved:false,request:async(path,options={})=>{if(path.endsWith('/capture'))captured.push(options.body);return {items:[]};}});
const syncLibraryAccess=()=>{const scope=currentLibraryOwnerScope();browser.setAccess(Boolean(scope),scope);};
''' + snapshot + activation + r'''
await activateWatchDraft(false);await tick();assert.equal(bootstrapReads,1);assert.equal(currentLibraryOwnerScope(),owner);assert.equal(draftScope,null);assert.equal(filesUploadArea.hidden,true);
const pending=browser.capture({file_id:'file-closed',revision:'revision-closed',name:'report.txt'});await tick();
nodes(body.children.at(-1)).find(node=>node.tagName==='FORM').dispatch('submit',{preventDefault(){}});await pending;
assert.equal(captured.length,1);assert.equal(captured[0].file_id,'file-closed');
for(const mode of ['viewer','shared-link']){
 signedToken=mode==='shared-link'?'synthetic-view-token':'';
 syncWatchLibrarySnapshot({can_manage_workspace:mode!=='viewer'});await activateWatchDraft(false);
 assert.equal(currentLibraryOwnerScope(),'');assert.equal(draftScope,null);assert.equal(filesUploadArea.hidden,true);
 await browser.capture({file_id:'file-closed',revision:'revision-closed',name:'report.txt'});
 assert.equal(captured.length,1);assert.equal(bootstrapReads,1);
}
browser.destroy();
''')


def test_picker_excludes_disabled_and_removed_approved_catalog_entries():
    node_script(FAKE_DOM + r'''
const mount=new Element();
const request=async path=>path.startsWith('/api/personal-library')?{items:[]}:{library:[
 {library_id:'live',status:'available',version:'1.0.0',manifest:{label:'Available capability'}},
 {library_id:'disabled',status:'disabled',version:'1.0.0',manifest:{label:'Disabled capability'}},
 {library_id:'removed',status:'removed',version:'1.0.0',manifest:{label:'Removed capability'}}]};
const browser=library.createLibraryBrowser({mount,request});browser.setAccess(true,'a'.repeat(64));await tick();await tick();
assert.ok(nodes(mount).some(node=>node.textContent==='Available capability'));
assert.ok(!nodes(mount).some(node=>node.textContent==='Disabled capability'||node.textContent==='Removed capability'));
browser.destroy();
''')


def test_library_refresh_preserves_row_focus_or_returns_to_search_when_archived():
    node_script(FAKE_DOM + r'''
const mount=new Element();let state='active',includeArchived=false;
const item=()=>({item_id:'item-one',version_id:'version-two',current_version_id:'version-two',version:2,kind:'note',name:'Synthetic note',state});
const browser=library.createLibraryBrowser({mount,approved:false,request:async(path,options={})=>{
 if(path.startsWith('/api/personal-library?'))return {items:state==='active'||includeArchived?[item()]:[]};
 if(path.endsWith('/archive')){state=options.body.archived?'archived':'active';return {};}
 throw Error(`Unexpected request ${path}`);
}});
browser.setAccess(true,'owner-one');await tick();
const options=()=>nodes(mount).find(node=>node['aria-label']==='Options for Synthetic note');
options().focus();await browser.refresh();assert.equal(document.activeElement,options());
const search=nodes(mount).find(node=>node['aria-label']==='Search My Library');
nodes(mount).find(node=>node.textContent==='Archive').click();await tick();
assert.equal(state,'archived');assert.equal(document.activeElement,search);
includeArchived=true;nodes(mount).find(node=>node.textContent==='All').click();await tick();options().focus();
nodes(mount).find(node=>node.textContent==='Restore').click();await tick();
assert.equal(state,'active');assert.equal(document.activeElement,options());
search.focus();await browser.refresh();assert.equal(document.activeElement,search,'an unrelated live control must retain focus');
browser.destroy();
''')
