"""Run the shared menu against native-like details, popover and focus transitions."""
import json
from pathlib import Path
import subprocess

import pytest

STATIC = Path(__file__).parents[1] / "src/glass_drive_ui/static"

HARNESS = r'''
import assert from 'node:assert/strict';
const pending=new Map();
class Target {
 constructor(){this.handlers=new Map();}
 addEventListener(name,fn,options){if(options?.signal?.aborted)return;if(!this.handlers.has(name))this.handlers.set(name,new Set());this.handlers.get(name).add(fn);options?.signal?.addEventListener('abort',()=>this.handlers.get(name).delete(fn),{once:true});}
 removeEventListener(name,fn){this.handlers.get(name)?.delete(fn);}
 emit(name,event={}){event.target??=this;for(const fn of this.handlers.get(name)||[])fn(event);}
}
const document=new Target();document.activeElement=null;globalThis.document=document;
globalThis.window=new Target();globalThis.innerWidth=800;globalThis.innerHeight=600;
const flush=()=>{for(let n=0;pending.size&&n<20;n++){const batch=[...pending];pending.clear();for(const [target,event] of batch)target.emit('toggle',event);}assert.equal(pending.size,0);};
class Element extends Target {
 constructor(tag='div'){
  super();this.tag=tag;this.children=[];this.parent=null;this.attributes={};this.dataset={};this.style={};
  this.disabled=false;this.hidden=false;this.isConnected=true;this._open=false;this.popover=false;
  this.classList={add:()=>{}};this.rect={top:100,bottom:140,right:360};
  this.naturalWidth=220;this.naturalHeight=160;this.scrollHeight=160;
 }
 append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
 contains(node){for(let p=node;p;p=p.parent)if(p===this)return true;return false;}
 setAttribute(name,value){this.attributes[name]=value;}
 get open(){return this._open;}
 set open(value){value=Boolean(value);if(value===this._open)return;this._open=value;pending.set(this,{newState:value?'open':'closed'});}
 showPopover(){this.popover=true;pending.set(this,{newState:'open'});}
 hidePopover(){if(!this.popover)return;this.popover=false;pending.set(this,{newState:'closed'});}
 matches(selector){assert.equal(selector,':popover-open');return this.popover;}
 querySelectorAll(selector){
  assert.equal(selector,'button:not(:disabled), a[href]');
  const walk=node=>node.children.flatMap(child=>[child,...walk(child)]);
  return walk(this).filter(node=>node.tag==='button'&&!node.disabled||node.tag==='a'&&node.href);
 }
 getBoundingClientRect(){return this.rect;}
 get offsetWidth(){return Math.min(this.naturalWidth,parseFloat(this.style.maxWidth)||Infinity);}
 get offsetHeight(){return Math.min(this.naturalHeight,parseFloat(this.style.maxHeight)||Infinity);}
 focus(){if(this.disabled||!this.isConnected)return;for(let p=this;p;p=p.parent)if(p.hidden||p.attributes.popover&&!p.popover)return;document.activeElement=this;document.emit('focusin',{target:this});}
 closest(){for(let p=this;p;p=p.parent)if(p.tag==='button'||p.tag==='a'&&p.href)return p;return null;}
 key(key){const event={key,target:this,prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}};for(let p=this;p&&!event.stopped;p=p.parent)p.emit('keydown',event);return event;}
}
const make=({native=true,menu=true,disabled=false,signal}={})=>{
 const details=new Element('details'),trigger=new Element('summary'),panel=new Element();
 const first=new Element('button'),unavailable=new Element('button'),last=new Element('a');
 unavailable.disabled=true;last.href='/safe';if(disabled){first.disabled=true;last.href='';}
 panel.append(first,unavailable,last);details.append(trigger,panel);
 if(!native){panel.showPopover=undefined;panel.hidePopover=undefined;}
 const dispose=attachActionMenu(details,trigger,panel,{menu,signal});
 const open=()=>{trigger.focus();details.open=true;flush();};
 return {details,trigger,panel,first,unavailable,last,open,dispose};
};
'''


def menu_check(body):
    script = (f"import {{attachActionMenu}} from {json.dumps((STATIC / 'action-menu.js').as_uri())};\n"
              + HARNESS + body)
    result = subprocess.run(["node", "--input-type=module"], input=script,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr + result.stdout


def test_static_files_toolbars_group_actions_in_the_shared_menu_panel():
    from html.parser import HTMLParser

    class Toolbar(HTMLParser):
        def __init__(self, identity):
            super().__init__()
            self.identity = identity
            self.stack = []
            self.found = False
            self.buttons = []

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag not in {"input", "meta", "link", "br", "hr", "img", "path"}:
                self.stack.append((tag, values))
            if tag == "details" and values.get("id") == self.identity:
                self.found = True
            if tag == "button" and any(a.get("id") == self.identity for _, a in self.stack):
                self.buttons.append((values.get("id"), any("file-row-menu-items" in a.get("class", "").split() for _, a in self.stack)))

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    self.stack = self.stack[:index]
                    break

    for page, identity, expected in [
        ("index.html", "project-file-options", ["project-add-folder"]),
        ("watch.html", "watch-file-options", ["watch-add-folder", "watch-new-folder", "watch-file-trash-open"]),
    ]:
        toolbar = Toolbar(identity)
        toolbar.feed((STATIC / page).read_text())
        assert toolbar.found
        assert toolbar.buttons == [(identity, True) for identity in expected]


@pytest.mark.parametrize("surface", ["draft", "watch"])
def test_actual_files_toolbar_mount_uses_shared_keyboard_escape_and_outside_close(surface):
    app = (STATIC / "app.js").read_text()
    start = app.index("  const fileOptions = document.getElementById('project-file-options');")
    draft_mount = app[start:app.index("  const fileDraft =", start)]
    mount = draft_mount if surface == "draft" else f'''
const {{createWorkspaceFiles}}=await import({json.dumps((STATIC / "files.js").as_uri())});
const controls={{optionsMenu:details}};
const opts=new Proxy({{workerId:'worker-exact',csrf:()=>''}},{{get(target,name){{
 return name in target?target[name]:(controls[name]??=new Element());
}}}});
createWorkspaceFiles(opts);
'''
    menu_check(r'''
const details=new Element('details'),trigger=new Element('summary'),panel=new Element();
const first=new Element('button'),last=new Element('button');panel.append(first,last);details.append(trigger,panel);
details.querySelector=selector=>selector==='summary'?trigger:selector==='.file-row-menu-items'?panel:null;
document.getElementById=id=>id==='project-file-options'?details:null;
''' + mount + r'''
assert.equal(details.handlers.get('toggle').size,1);
trigger.key('ArrowDown');flush();
assert.equal(details.open,true);assert.equal(panel.popover,true);assert.equal(document.activeElement,first);
first.key('ArrowDown');assert.equal(document.activeElement,last);
last.key('Escape');flush();assert.equal(details.open,false);assert.equal(panel.popover,false);assert.equal(document.activeElement,trigger);
trigger.key('ArrowDown');flush();document.emit('pointerdown',{target:new Element('button')});flush();
assert.equal(details.open,false);assert.equal(panel.popover,false);
''')


def test_keyboard_opens_and_navigates_enabled_items_and_escape_restores_trigger():
    menu_check(r'''
const m=make();assert.equal(m.trigger.attributes['aria-expanded'],'false');
const open=m.trigger.key('ArrowDown');flush();
assert.equal(open.prevented,true);assert.equal(m.details.open,true);assert.equal(m.panel.popover,true);
assert.equal(document.activeElement,m.first);assert.equal(m.first.attributes.role,'menuitem');
assert.equal(m.unavailable.attributes.role,undefined);
for(const [key,target] of [['ArrowDown',m.last],['ArrowDown',m.first],['ArrowUp',m.last],['Home',m.first],['End',m.last]]){
 const event=document.activeElement.key(key);assert.equal(event.prevented,true);assert.equal(document.activeElement,target);
}
const escape=m.last.key('Escape');flush();
assert.equal(escape.prevented,true);assert.equal(escape.stopped,true);assert.equal(m.details.open,false);
assert.equal(m.panel.popover,false);assert.equal(m.trigger.attributes['aria-expanded'],'false');assert.equal(document.activeElement,m.trigger);
m.trigger.key('ArrowUp');flush();assert.equal(document.activeElement,m.last);
const tab=m.last.key('Tab');flush();assert.equal(tab.prevented,false);assert.equal(m.details.open,false);
''')


def test_outside_focus_pointer_and_native_light_dismiss_close_without_stealing_focus():
    menu_check(r'''
const m=make(),outside=new Element('button');m.open();
document.emit('pointerdown',{target:m.first});assert.equal(m.details.open,true);
outside.focus();flush();assert.equal(m.details.open,false);assert.equal(document.activeElement,outside);
m.open();document.emit('pointerdown',{target:outside});flush();assert.equal(m.details.open,false);
m.open();m.panel.hidePopover();flush();assert.equal(m.details.open,false);assert.equal(m.trigger.attributes['aria-expanded'],'false');
m.open();m.panel.emit('click',{target:m.first});flush();assert.equal(m.details.open,false);
m.first.dataset.menuKeepOpen='true';m.open();m.panel.emit('click',{target:m.first});flush();assert.equal(m.details.open,true);
m.dispose();flush();assert.equal(m.panel.popover,false);
''')


def test_single_open_global_listeners_and_disconnected_owner_cleanup():
    menu_check(r'''
const a=make(),b=make();a.open();b.details.open=true;flush();
assert.equal(a.details.open,false);assert.equal(a.panel.popover,false);assert.equal(b.details.open,true);
for(const event of ['pointerdown','focusin','scroll'])assert.equal(document.handlers.get(event).size,1);
assert.equal(window.handlers.get('resize').size,1);
b.details.isConnected=false;window.emit('resize');flush();assert.equal(b.details.open,false);
a.open();a.dispose();flush();document.emit('pointerdown',{target:new Element()});assert.equal(a.details.open,false);
''')


@pytest.mark.parametrize("viewport,anchor,panel_size,opens_up", [
    ((800,600), (100,140,360), (220,160), False),
    ((800,600), (540,580,790), (220,160), True),
    ((320,500), (20,60,45), (600,900), False),
    ((320,500), (420,460,310), (600,900), True),
])
def test_panel_flips_and_fits_the_viewport(viewport, anchor, panel_size, opens_up):
    menu_check(f'''
globalThis.innerWidth={viewport[0]};globalThis.innerHeight={viewport[1]};
const m=make();m.trigger.rect={{top:{anchor[0]},bottom:{anchor[1]},right:{anchor[2]}}};
m.panel.naturalWidth={panel_size[0]};m.panel.naturalHeight=m.panel.scrollHeight={panel_size[1]};m.open();
const left=parseFloat(m.panel.style.left),top=parseFloat(m.panel.style.top);
assert.ok(left>=12);assert.ok(left+m.panel.offsetWidth<=innerWidth-12);
assert.ok(top>=12);assert.ok(top+m.panel.offsetHeight<=innerHeight-12);
assert.equal(top<m.trigger.rect.top,{str(opens_up).lower()});
''')


def test_form_panels_keep_native_tab_and_arrow_behavior_and_fallback_still_closes():
    menu_check(r'''
const m=make({menu:false,native:false});m.open();
assert.equal(m.panel.attributes.role,undefined);assert.equal(document.activeElement,m.trigger);
for(const key of ['Tab','ArrowDown','ArrowUp']){const event=m.trigger.key(key);assert.equal(event.prevented,false);assert.equal(m.details.open,true);}
m.trigger.key('Escape');flush();assert.equal(m.details.open,false);assert.equal(document.activeElement,m.trigger);
''')


def test_owner_abort_disposes_open_popup_and_all_local_listeners():
    menu_check(r'''
const controller=new AbortController(),m=make({signal:controller.signal});m.open();
assert.equal(m.panel.popover,true);controller.abort();flush();
assert.equal(m.details.open,false);assert.equal(m.panel.popover,false);
assert.equal(m.trigger.attributes['aria-expanded'],'false');
for(const element of [m.details,m.panel])for(const handlers of element.handlers.values())assert.equal(handlers.size,0);
m.trigger.key('ArrowDown');flush();assert.equal(m.details.open,false);
const stopped=new AbortController();stopped.abort();const inactive=make({signal:stopped.signal});
assert.equal(inactive.details.handlers.size,0);
''')


def test_keep_open_detail_expansion_repositions_after_content_growth():
    menu_check(r'''
const m=make();m.trigger.rect={top:350,bottom:390,right:500};m.panel.naturalHeight=m.panel.scrollHeight=120;
m.first.dataset.menuKeepOpen='true';m.open();assert.ok(parseFloat(m.panel.style.top)>m.trigger.rect.bottom);
m.panel.naturalHeight=m.panel.scrollHeight=300;m.panel.emit('click',{target:m.first});flush();
assert.equal(m.details.open,true);assert.equal(document.activeElement,m.first);assert.equal(m.panel.popover,true);
assert.ok(parseFloat(m.panel.style.top)<m.trigger.rect.top);assert.ok(parseFloat(m.panel.style.top)+m.panel.offsetHeight<=innerHeight-12);
''')
