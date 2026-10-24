"""Exercise account refresh focus against the real renderer and shared menu."""
import re

from test_action_menu import STATIC, menu_check


def connection_check(body):
    source = (STATIC / 'control-plane.js').read_text()
    functions = '\n'.join(re.search(r'^(?:async )?function ' + name + r'\(.*?^}', source, re.M | re.S).group(0)
                          for name in ['retainProviderAccountFocus', 'verifyProviderAccount', 'reconnectProviderAccount', 'subscriptionRouteAvailable', 'renderProviderAccounts'])
    menu_check(functions + r'''
Object.defineProperty(Element.prototype,'childElementCount',{get(){return this.children.length;}});
const descendants=e=>e.children.flatMap(child=>[child,...descendants(child)]);
const matches=(e,selector)=>selector==='summary'?e.tag==='summary':selector==='[data-account-action]'?Boolean(e.dataset.accountAction):e.className?.split(' ').includes(selector.slice(1));
const menuItems=Element.prototype.querySelectorAll;
Element.prototype.querySelectorAll=function(selector){return selector==='button:not(:disabled), a[href]'?menuItems.call(this,selector):descendants(this).filter(e=>matches(e,selector));};
Element.prototype.querySelector=function(selector){return this.querySelectorAll(selector)[0]||null;};
Element.prototype.closest=function(selector){for(let e=this;e;e=e.parent)if(selector==='button, a[href]'?(e.tag==='button'||e.tag==='a'):matches(e,selector))return e;return null;};
Element.prototype.replaceChildren=function(...children){for(const old of descendants(this)){old.isConnected=false;if(document.activeElement===old)document.activeElement=document.body;}this.children=[];this.append(...children);};
const node=(tag,cls='',text='')=>{const e=new Element(tag);e.className=cls;e.textContent=text;e.classList.toggle=()=>{};return e;};
document.createElement=tag=>node(tag);
Element.prototype.after=function(element){element.parent=this.parent;this.parent.children.splice(this.parent.children.indexOf(this)+1,0,element);};
Element.prototype.remove=function(){this.parent.children=this.parent.children.filter(e=>e!==this);this.isConnected=false;};
document.body=node('body');const list=node('div'),native=node('div'),outside=node('button');
document.getElementById=id=>id==='provider-account-list'?list:id==='existing-claude-connection'?native:null;
document.querySelector=()=>null;
let connectionMenuAbort=null,activeSetupAccount='',status='',shown=null,requested=[];
const attachExistingClaudeConnection=()=>{},capabilityReviewRequest=()=>null,providerAccountDetails=()=>'',providerAccountUsage=()=> 'Observed usage',statusChip=s=>node('span','',s);
const removeProviderAccount=()=>{},pollSetup=()=>{};
const setProviderStatus=value=>status=value,showSetup=value=>shown=value;
let complete,fail;
const api={postJson:path=>{requested.push(path);return new Promise((resolve,reject)=>{complete=resolve;fail=reject;});}};
const account={account_id:'account/a',label:'Synthetic account',provider:'codex',auth_method:'subscription',platform_support:'supported',status:'ready',observed_input_tokens:12};
let controlPlane={provider_accounts:[account],provider_options:[{provider:'codex',subscription_support:'supported'}]};
const loadControlPlane=async()=>{renderProviderAccounts();flush();};
const row=()=>list.children[0],trigger=()=>row().querySelector('summary');
const button=label=>descendants(row()).find(e=>e.tag==='button'&&e.textContent===label);
''' + body)


def test_actual_connection_mount_uses_shared_menu_usage_and_rerender_disposal():
    connection_check(r'''
renderProviderAccounts();trigger().key('ArrowDown');flush();
const details=trigger().parent,panel=details.children[1];
assert.equal(panel.popover,true);assert.equal(document.activeElement,button('Usage details'));
const usage=button('Usage details');usage.emit('click');panel.emit('click',{target:usage});
assert.equal(usage.attributes['aria-expanded'],'true');assert.equal(details.open,true);assert.equal(panel.popover,true);
usage.key('End');assert.equal(document.activeElement,button('Remove'));
document.activeElement.key('Escape');flush();assert.equal(document.activeElement,trigger());assert.equal(panel.popover,false);
trigger().key('ArrowDown');flush();renderProviderAccounts();flush();
assert.equal(panel.popover,false);assert.equal(details.open,false);assert.equal(details.handlers.get('keydown').size,0);
''')


def test_connection_test_restores_same_account_trigger_after_successful_refresh():
    connection_check(r'''
renderProviderAccounts();trigger().key('ArrowDown');flush();
const oldTrigger=trigger(),test=button('Test connection'),panel=oldTrigger.parent.children[1];test.focus();
const request=verifyProviderAccount(account,test);panel.emit('click',{target:test});flush();
assert.equal(document.activeElement,oldTrigger);assert.equal(panel.popover,false);
complete({status:'ready',message:'Connected'});await request;flush();
assert.notEqual(trigger(),oldTrigger);assert.equal(document.activeElement,trigger());assert.equal(shown.message,'Connected');
assert.deepEqual(requested,['/api/provider-accounts/account%2Fa/verify']);
assert.equal(document.handlers.get('focusin').size,1);
''')


def test_failed_test_returns_to_trigger_and_restores_retry_without_auth_change():
    connection_check(r'''
renderProviderAccounts();trigger().key('ArrowDown');flush();
const test=button('Test connection'),panel=trigger().parent.children[1];test.focus();
const request=verifyProviderAccount(account,test);panel.emit('click',{target:test});flush();
fail(new Error('Synthetic verification unavailable'));await request;flush();
assert.equal(document.activeElement,trigger());assert.equal(test.disabled,false);assert.equal(test.textContent,'Test connection');
assert.equal(status,'Synthetic verification unavailable');assert.equal(requested.length,1);
''')


def test_later_refresh_does_not_steal_focus_when_user_moves_elsewhere():
    connection_check(r'''
renderProviderAccounts();trigger().key('ArrowDown');flush();button('Test connection').focus();
const request=verifyProviderAccount(account,button('Test connection'));outside.focus();
complete({status:'ready'});await request;flush();assert.equal(document.activeElement,outside);
''')


def test_native_claude_verify_restores_its_same_primary_control():
    connection_check(r'''
account.auth_source='current_os_claude_subscription';renderProviderAccounts();
const old=button('Verify');old.focus();const request=verifyProviderAccount(account,old);
complete({status:'ready',message:'Existing sign-in checked'});await request;flush();
assert.notEqual(button('Verify'),old);assert.equal(document.activeElement,button('Verify'));
assert.equal(status,'Existing sign-in checked');assert.equal(shown,null);
''')


def test_native_api_key_reconnect_keeps_inline_entry_and_cancel_without_request():
    connection_check(r'''
Object.assign(account,{auth_method:'api_key',credential_route:'native',status:'disconnected'});renderProviderAccounts();
const reconnect=button('Reconnect & test');reconnect.focus();await reconnectProviderAccount(account,reconnect);
const form=descendants(row()).find(e=>e.tag==='form'),input=form.children[0];
assert.equal(input.type,'password');assert.equal(document.activeElement,input);assert.equal(reconnect.hidden,true);
assert.equal(requested.length,0);form.children[2].emit('click');
assert.equal(form.isConnected,false);assert.equal(input.value,'');assert.equal(reconnect.hidden,false);assert.equal(requested.length,0);
''')
