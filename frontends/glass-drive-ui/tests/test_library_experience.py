"""Actual shared Library display behavior; product browser acceptance is separate."""
import json
import re
from pathlib import Path
import subprocess

from test_personal_library_ui import FAKE_DOM


STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def run_node(body):
    code = ("import assert from 'node:assert/strict';\n"
            f"import * as sizing from {json.dumps((STATIC / 'library-panel.js').as_uri())};\n"
            f"import * as library from {json.dumps((STATIC / 'personal-library.js').as_uri())};\n"
            + body)
    result = subprocess.run(['node', '--input-type=module'], input=code,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout


PANEL_DOM = r'''
class Element {
 constructor(){this.listeners=new Map();this.attributes={};this.style={};this.children=[];this.isConnected=true;this.id='';this.capture=null;this.classes=new Set();this.classList={add:name=>this.classes.add(name),remove:name=>this.classes.delete(name),contains:name=>this.classes.has(name)};}
 setAttribute(name,value){this.attributes[name]=value;}
 addEventListener(name,handler){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(handler);}
 removeEventListener(name,handler){this.listeners.get(name)?.delete(handler);}
 emit(name,event={}){for(const handler of [...(this.listeners.get(name)||[])])handler(event);}
 prepend(child){child.parentNode=this;this.children.unshift(child);}
 getBoundingClientRect(){return {width:parseFloat(this.style.width)||680};}
 setPointerCapture(id){this.capture=id;}
 hasPointerCapture(id){return this.capture===id;}
 releasePointerCapture(id){if(this.capture!==id)return;this.capture=null;this.emit('lostpointercapture',{pointerId:id});}
 remove(){this.isConnected=false;if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(child=>child!==this);if(this.capture!==null)this.releasePointerCapture(this.capture);}
}
const frames=new Map();let serial=0;
globalThis.requestAnimationFrame=fn=>{const id=++serial;frames.set(id,fn);return id;};
globalThis.cancelAnimationFrame=id=>frames.delete(id);
const flush=()=>{for(const [id,fn] of [...frames]){frames.delete(id);fn();}};
const store=new Map(),writes=[];
globalThis.localStorage={getItem:key=>store.get(key)||null,setItem(key,value){writes.push({key,value});store.set(key,value);}};
globalThis.window=new Element();window.innerWidth=1440;window.innerHeight=900;
globalThis.document={createElement:()=>new Element(),getElementById:()=>null};
const makePanel=()=>{const panel=new Element();panel.id='library';return panel;};
const pointer=(pointerId,x,button=0)=>({pointerId,clientX:x,button,preventDefault(){this.prevented=true;}});
const key=(name,shiftKey=false)=>({key:name,shiftKey,preventDefault(){this.prevented=true;}});
'''


def test_width_defaults_and_limits_fit_desktop_mobile_and_narrow_split_views():
    run_node(r'''
assert.equal(sizing.libraryPanelWidth(undefined,1440),840);
for(const bad of [0,-1,'garbage',null,NaN])assert.equal(sizing.libraryPanelWidth(bad,1440),840);
assert.equal(sizing.libraryPanelWidth(2000,2560),1200);
assert.equal(sizing.libraryPanelWidth(12,1440),360);
for(const viewport of [200,240,320,375,600,1440]){
 const width=sizing.libraryPanelWidth(840,viewport);
 assert.ok(width>0 && width<=viewport-24,`No overflow at viewport ${viewport}: ${width}`);
}
''')


def test_saved_watch_and_picker_widths_are_separate_and_accessibly_described():
    run_node(PANEL_DOM + r'''
store.set('xperfect.display.library-width.watch','720');store.set('xperfect.display.library-width.picker','960');
const panel=makePanel(),dispose=sizing.attachLibraryPanel(panel),handle=panel.children[0];
assert.equal(panel.style.width,'720px');assert.equal(handle.attributes.role,'separator');assert.equal(handle.attributes['aria-controls'],'library');
assert.equal(handle.attributes['aria-orientation'],'vertical');assert.equal(handle.attributes['aria-valuenow'],'720');
dispose();assert.equal(window.listeners.get('resize').size,0);assert.equal(panel.children.length,0);
const picker=makePanel(),release=sizing.attachLibraryPanel(picker,{dialog:true});assert.equal(picker.style.width,'960px');release();
''')


def test_optional_blocked_storage_does_not_disable_resizing_or_keyboard():
    run_node(PANEL_DOM + r'''
globalThis.localStorage={getItem(){throw new Error('blocked');},setItem(){throw new Error('blocked');}};
const panel=makePanel(),dispose=sizing.attachLibraryPanel(panel),handle=panel.children[0];
assert.equal(panel.style.width,'840px');const event=key('ArrowLeft');handle.emit('keydown',event);
assert.equal(panel.style.width,'860px');assert.equal(event.prevented,true);dispose();
''')


def test_drag_tracks_the_owning_pointer_batches_frames_and_persists_once_settled():
    run_node(PANEL_DOM + r'''
const panel=makePanel(),dispose=sizing.attachLibraryPanel(panel),handle=panel.children[0];
handle.emit('pointerdown',pointer(1,800,1));handle.emit('pointermove',pointer(1,600));flush();assert.equal(panel.style.width,'840px');
handle.emit('pointerdown',pointer(7,800));assert.equal(handle.capture,7);assert.ok(panel.classes.has('library-is-resizing'));
handle.emit('pointermove',pointer(8,600));flush();assert.equal(panel.style.width,'840px');
handle.emit('pointermove',pointer(7,760));handle.emit('pointermove',pointer(7,720));assert.equal(frames.size,1);assert.equal(writes.length,0);
flush();assert.equal(panel.style.width,'920px');handle.emit('pointerup',pointer(8,720));assert.ok(panel.classes.has('library-is-resizing'));
handle.emit('pointerup',pointer(7,720));assert.equal(handle.capture,null);assert.equal(panel.classes.has('library-is-resizing'),false);
assert.deepEqual(writes,[{key:'xperfect.display.library-width.watch',value:'920'}]);assert.equal(frames.size,0);dispose();
''')


def test_centered_picker_drag_accounts_for_both_edges_and_pointer_cancel_settles():
    run_node(PANEL_DOM + r'''
const panel=makePanel(),dispose=sizing.attachLibraryPanel(panel,{dialog:true}),handle=panel.children[0];
handle.emit('pointerdown',pointer(4,700));handle.emit('pointermove',pointer(4,650));flush();assert.equal(panel.style.width,'940px');
handle.emit('pointercancel',pointer(4,650));assert.equal(panel.classes.has('library-is-resizing'),false);assert.equal(handle.capture,null);
assert.deepEqual(writes,[{key:'xperfect.display.library-width.picker',value:'940'}]);dispose();
''')


def test_keyboard_reset_and_resize_keep_the_preferred_width_across_viewports():
    run_node(PANEL_DOM + r'''
const panel=makePanel(),dispose=sizing.attachLibraryPanel(panel),handle=panel.children[0];
handle.emit('keydown',key('ArrowLeft',true));assert.equal(panel.style.width,'920px');
handle.emit('keydown',key('ArrowRight'));assert.equal(panel.style.width,'900px');
handle.emit('keydown',key('Home'));assert.equal(panel.style.width,'360px');
handle.emit('keydown',key('End'));assert.equal(panel.style.width,'1200px');
window.innerWidth=320;window.emit('resize');assert.equal(panel.style.width,'296px');assert.equal(handle.attributes['aria-valuemax'],'296');
window.innerWidth=1440;window.emit('resize');assert.equal(panel.style.width,'1200px');
handle.emit('dblclick');assert.equal(panel.style.width,'840px');assert.equal(store.get('xperfect.display.library-width.watch'),'840');
dispose();assert.equal(window.listeners.get('resize').size,0);
''')


def test_dispose_mid_drag_releases_capture_class_frames_and_any_future_mutations():
    run_node(PANEL_DOM + r'''
const panel=makePanel(),dispose=sizing.attachLibraryPanel(panel),handle=panel.children[0];
handle.emit('pointerdown',pointer(7,800));handle.emit('pointermove',pointer(7,700));assert.equal(frames.size,1);
dispose();assert.equal(handle.capture,null);assert.equal(panel.classes.has('library-is-resizing'),false);assert.equal(frames.size,0);
assert.equal(window.listeners.get('resize').size,0);assert.equal(panel.children.length,0);
const settledWidth=panel.style.width,settledWrites=writes.length;
handle.emit('pointermove',pointer(7,500));handle.emit('lostpointercapture',pointer(7,500));handle.emit('keydown',key('End'));handle.emit('dblclick');flush();
assert.equal(panel.style.width,settledWidth);assert.equal(writes.length,settledWrites);assert.equal(frames.size,0);
dispose();assert.equal(window.listeners.get('resize').size,0);
''')


def test_library_availability_and_professional_categories_do_not_claim_native_readiness():
    run_node(r'''
for(const [kind,category] of [['note','knowledge'],['knowledge','knowledge'],['skill','knowledge'],['bundle','knowledge'],['native','tools'],['approved','tools']]){
 const active={kind,state:'active'},archived={kind,state:'archived'};
 assert.equal(library.libraryItemMatches(active,'available',category),true);
 assert.equal(library.libraryItemMatches(active,'archived',category),false);
 assert.equal(library.libraryItemMatches(archived,'available',category),false);
 assert.equal(library.libraryItemMatches(archived,'archived',category),true);
 for(const item of [active,archived])assert.equal(library.libraryItemMatches(item,'all','all'),true);
 const opposite=category==='tools'?'knowledge':'tools';assert.equal(library.libraryItemMatches(active,'all',opposite),false);
}
// Active means an unarchived saved item; a review/setup status never becomes an installed claim.
assert.equal(library.libraryItemMatches({kind:'native',state:'active',status:'needs_setup'},'available','tools'),true);
assert.equal(library.libraryStatus({state:'needs_setup'}),'Needs native setup');
assert.equal(library.libraryItemMatches({kind:'native',state:'archived',status:'enabled'},'available','tools'),false);
''')


def test_real_browser_filters_persist_display_only_and_keep_archived_items_unselectable():
    run_node(FAKE_DOM.replace("import assert from 'node:assert/strict';", '') + r'''
const store=new Map();globalThis.localStorage={getItem:key=>store.get(key)||null,setItem:(key,value)=>store.set(key,value)};
const active={item_id:'tool-one',version_id:'tool-v1',version:1,kind:'native',name:'Research tool',state:'active'};
const archived={item_id:'note-one',version_id:'note-v1',version:1,kind:'note',name:'Earlier brief',state:'archived'};
const reads=[];const request=async(path)=>{reads.push(path);return {items:path.endsWith('true')?[active,archived]:[active]};};
const mount=new Element();const browser=library.createLibraryBrowser({mount,approved:false,onPick:()=>{},request});
browser.setAccess(true,'owner-one');await tick();assert.equal(reads[0],'/api/personal-library?include_archived=false');
assert.ok(nodes(mount).some(node=>node.textContent==='Research tool'));
const filters=nodes(mount).find(node=>node['aria-label']==='Library availability');
filters.children.find(node=>node.textContent==='Archived').onclick();await tick();
assert.equal(reads.at(-1),'/api/personal-library?include_archived=true');
assert.equal(nodes(mount).some(node=>node.textContent==='Research tool'),false);
assert.ok(nodes(mount).some(node=>node.textContent==='Earlier brief'));
assert.equal(nodes(mount).find(node=>node['aria-label']==='Select Earlier brief').disabled,true);
assert.deepEqual(JSON.parse(store.get('xperfect.display.library-filter')),{scope:'archived',category:'all'});
filters.children.find(node=>node.textContent==='All').onclick();await tick();
const kinds=nodes(mount).find(node=>node['aria-label']==='Knowledge and tools');
kinds.children.find(node=>node.textContent==='Tools & plugins').onclick();
assert.ok(nodes(mount).some(node=>node.textContent==='Research tool'));
assert.equal(nodes(mount).some(node=>node.textContent==='Earlier brief'),false);
assert.deepEqual(JSON.parse(store.get('xperfect.display.library-filter')),{scope:'all',category:'tools'});
browser.destroy();
const nextMount=new Element(),next=library.createLibraryBrowser({mount:nextMount,approved:false,request});
next.setAccess(true,'owner-two');await tick();
assert.equal(nodes(nextMount).find(node=>node['aria-label']==='Library availability').children.find(node=>node.textContent==='All')['aria-pressed'],'true');
assert.equal(nodes(nextMount).find(node=>node['aria-label']==='Knowledge and tools').children.find(node=>node.textContent==='Tools & plugins')['aria-pressed'],'true');
next.destroy();
''')


def test_aborting_and_reopening_real_pickers_releases_each_picker_resize_listener():
    run_node(FAKE_DOM.replace("import assert from 'node:assert/strict';", '') + r'''
let sharedResizeListeners;
for(let index=0;index<6;index++){
 const controller=new AbortController();
 const result=library.openLibraryPicker({ownerScope:'owner-one',signal:controller.signal,request:async()=>({items:[],library:[]})});
 await tick();assert.equal(displayListeners.get('resize').size,2);assert.equal(body.children.length,1);
 controller.abort();assert.equal(await result,null);
 const remaining=new Set(displayListeners.get('resize'));
 // The application-wide action menu owns one listener; every picker owns and releases another.
 assert.equal(remaining.size,1);
 if(index===0)sharedResizeListeners=remaining;else assert.deepEqual(remaining,sharedResizeListeners);
 assert.equal(body.children.length,0);assert.equal(animationFrames.size,0);
}
''')


def test_picker_footer_stays_outside_scrolling_material_and_retains_exact_apply_and_cancel():
    run_node(FAKE_DOM.replace("import assert from 'node:assert/strict';", '') + r'''
const ref={item_id:'note-exact',version_id:'revision-exact'},requests=[];
const item={...ref,kind:'note',name:'Onboarding brief',state:'active',version:2};
const request=async(path,options={})=>{requests.push({path,options});
 if(path.endsWith('/resolve'))return {items:[item],upload_ids:[],upload_revisions:[]};
 return {items:[item],library:[]};};
const result=library.openLibraryPicker({ownerScope:'owner-one',refs:[ref],request});await tick();
const dialog=body.children[0],all=nodes(dialog),form=all.find(n=>n.tagName==='FORM');
const material=all.find(n=>(n.className||'').split(' ').includes('personal-library-form-body'));
const footer=all.find(n=>n.className==='personal-library-footer');
assert.equal(footer.parentNode,form);assert.equal(nodes(material).includes(footer),false);
const use=nodes(footer).find(n=>n.textContent==='Use selected'),cancel=nodes(footer).find(n=>n.textContent==='Cancel');
assert.ok(use&&cancel,'Apply and Cancel stay together in the fixed footer');
assert.equal(use.disabled,false);use.click();await tick();assert.deepEqual((await result).refs,[ref]);
const resolve=requests.find(r=>r.path.endsWith('/resolve'));assert.deepEqual(resolve.options.body.refs,[ref]);
assert.equal(body.children.length,0);
const before=requests.filter(r=>r.path.endsWith('/resolve')).length;
const cancelled=library.openLibraryPicker({ownerScope:'owner-one',refs:[ref],request});await tick();
nodes(body).find(n=>n.textContent==='Cancel').click();assert.equal(await cancelled,null);
assert.equal(requests.filter(r=>r.path.endsWith('/resolve')).length,before,'Cancel never applies a selection');
assert.equal(body.children.length,0);
''')


def test_picker_card_title_toggles_the_exact_native_checkbox_once_while_details_only_inspect():
    run_node(FAKE_DOM.replace("import assert from 'node:assert/strict';", '') + r'''
globalThis.localStorage={getItem:()=>JSON.stringify({scope:'all',category:'all'}),setItem(){}};
const active={item_id:'skill-one',version_id:'skill-v2',current_version_id:'skill-v2',version:2,kind:'note',name:'Evidence review',state:'active'};
const archived={item_id:'old-one',version_id:'old-v1',version:1,kind:'note',name:'Old brief',state:'archived'};
const reads=[],picked=[];
const request=async(path,options={})=>{reads.push({path,options});if(path.startsWith('/api/personal-library?'))return {items:[active,archived]};if(path.endsWith('/resolve'))return {items:[]};return {...active,payload:{text:'Exact saved knowledge'}};};
const mount=new Element(),browser=library.createLibraryBrowser({mount,request,approved:false,onPick:refs=>picked.push(refs)});
browser.setAccess(true,'owner-one');await tick();
const title=nodes(mount).find(node=>node.textContent==='Evidence review'),check=nodes(mount).find(node=>node['aria-label']==='Select Evidence review');
let changes=0;check.addEventListener('change',()=>changes++);
title.click();assert.equal(check.checked,true);assert.equal(title['aria-pressed'],'true');assert.equal(changes,1);
assert.deepEqual(browser.selection(),[{item_id:'skill-one',version_id:'skill-v2'}]);assert.ok(nodes(mount).some(node=>node.textContent==='1 selected'));
title.click();assert.equal(check.checked,false);assert.equal(title['aria-pressed'],'false');assert.equal(changes,2);assert.deepEqual(browser.selection(),[]);
title.click();assert.equal(changes,3);
const details=nodes(title.parentNode.parentNode).find(node=>node.textContent==='Details');details.click();await tick();
assert.equal(changes,3);assert.deepEqual(browser.selection(),[{item_id:'skill-one',version_id:'skill-v2'}]);
assert.ok(reads.some(item=>item.path==='/api/personal-library/skill-one?version_id=skill-v2'));
assert.ok(nodes(body).some(node=>node.textContent==='Exact saved knowledge'));
const archivedTitle=nodes(mount).find(node=>node.textContent==='Old brief'),archivedCheck=nodes(mount).find(node=>node['aria-label']==='Select Old brief');
assert.equal(archivedTitle.disabled,true);assert.equal(archivedCheck.disabled,true);archivedTitle.click();archivedCheck.click();assert.equal(archivedCheck.checked,false);
assert.deepEqual(browser.selection(),[{item_id:'skill-one',version_id:'skill-v2'}]);
nodes(mount).find(node=>node.textContent==='Use selected').click();await tick();assert.deepEqual(picked,[[{item_id:'skill-one',version_id:'skill-v2'}]]);
browser.setAccess(false,'');assert.deepEqual(browser.selection(),[]);assert.equal(body.children.length,0);browser.destroy();
''')


def test_main_library_card_title_still_opens_details_without_selecting_or_mutating():
    run_node(FAKE_DOM.replace("import assert from 'node:assert/strict';", '') + r'''
globalThis.localStorage={getItem:()=>null,setItem(){}};
const item={item_id:'note-one',version_id:'v1',current_version_id:'v1',version:1,kind:'note',name:'Company background',state:'active'};
const calls=[],mount=new Element(),browser=library.createLibraryBrowser({mount,approved:false,request:async(path,options={})=>{calls.push({path,options});return path.startsWith('/api/personal-library?')?{items:[item]}:{...item,payload:{text:'Background detail'}};}});
browser.setAccess(true,'owner-one');await tick();nodes(mount).find(node=>node.textContent==='Company background').click();await tick();
assert.ok(nodes(body).some(node=>node.textContent==='Background detail'));assert.deepEqual(browser.selection(),[]);
assert.equal(calls.some(call=>call.options.method),false);browser.destroy();assert.equal(body.children.length,0);
''')


def test_watch_only_remembers_explicit_tab_choices_and_rejects_unknown_or_unauthorized_saved_tabs():
    source = (STATIC / 'watch.js').read_text()
    match = re.search(r'^function selectLibraryTab\(.*?^}', source, re.M | re.S)
    assert match
    start = source.index("filesToggle?.addEventListener('click', () => {")
    handler = source[start:source.index("filesClose?.addEventListener('click'", start)]
    run_node(match.group(0) + r'''
const writes=[];let stored=null,opened=0,focused=0,added=0,saved=0;
globalThis.localStorage={getItem:()=>stored,setItem:(key,value)=>writes.push({key,value})};
let libraryManager=true,pendingLibraryTab='',listedFileActivityKey='',latestFileActivityKey='current';
const currentLibraryOwnerScope=()=>libraryManager?'verified-owner':'';
const panels=Object.fromEntries(['files','added','saved'].map(name=>[`watch-library-${name}`,{hidden:true}]));
const tabs=['files','added','saved'].map(name=>({id:`watch-library-tab-${name}`,attrs:{},setAttribute(key,value){this.attrs[key]=value;},getAttribute:()=>`watch-library-${name}`,focus(){focused++;}}));
globalThis.document={querySelectorAll:()=>tabs,getElementById:id=>panels[id]};
const refreshAddedLibrary=()=>added++,watchLibrary={refresh:()=>saved++};
const filesPanel={hidden:true},filesToggle={addEventListener(name,fn){this[name]=fn;},setAttribute(){}},workspaceFiles={open:()=>opened++},filesClose={focus:()=>focused++};
''' + handler + r'''
const current=()=>tabs.find(tab=>tab.attrs['aria-selected']==='true').id;
filesToggle.click();assert.equal(current(),'watch-library-tab-files');assert.equal(writes.length,0);
selectLibraryTab('saved',false,true);assert.equal(current(),'watch-library-tab-saved');assert.deepEqual(writes,[{key:'xperfect.display.library-tab',value:'saved'}]);
stored='saved';filesPanel.hidden=true;filesToggle.click();assert.equal(current(),'watch-library-tab-saved');assert.equal(writes.length,1);
stored='unknown-tab';filesPanel.hidden=true;filesToggle.click();assert.equal(current(),'watch-library-tab-files');assert.equal(writes.length,1);
libraryManager=false;stored='saved';filesPanel.hidden=true;const savedReads=saved;filesToggle.click();assert.equal(current(),'watch-library-tab-files');assert.equal(saved,savedReads);assert.equal(writes.length,1);
assert.equal(opened,4);assert.equal(added,0);
''')


def test_cards_show_item_descriptions_as_text_and_keep_generic_fallback():
    run_node(FAKE_DOM.replace("import assert from 'node:assert/strict';", '') + r'''
const mount=new Element();
const items=[
 {item_id:'skill-specific',version_id:'v1',version:1,name:'Assay quality',kind:'skill',state:'active',payload:{description:'Check assay results against the supplied method.'}},
 {item_id:'note-specific',version_id:'v2',version:1,name:'Study methods',kind:'note',state:'active',payload:{description:'<b>Review the study plan.</b>'}},
 {item_id:'skill-unknown',version_id:'v3',version:1,name:'No description',kind:'skill',state:'active',payload:{}}
];
const browser=library.createLibraryBrowser({mount,approved:false,request:async()=>({items})});
browser.setAccess(true,'owner');await tick();
const purposes=nodes(mount).filter(node=>node.className==='library-ability-purpose');
assert.deepEqual(purposes.map(node=>node.textContent),['Check assay results against the supplied method.','<b>Review the study plan.</b>','A specialized way of doing the work']);
assert.ok(purposes.every(node=>!node.innerHTML),'Description is text, never active HTML');
browser.setAccess(false,'');assert.equal(mount.hidden,true);assert.equal(nodes(mount).some(node=>node.textContent==='Check assay results against the supplied method.'),false);
''')
