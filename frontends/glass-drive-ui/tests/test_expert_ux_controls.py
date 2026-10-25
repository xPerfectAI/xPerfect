"""Current account actions and Watch composer keep the useful native controls."""
from pathlib import Path
import re
import subprocess


STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def function(name, source):
    match = re.search(r'^function ' + re.escape(name) + r'\(.*?^}', source, re.M | re.S)
    assert match, name
    return match.group(0)


def run_node(code):
    result = subprocess.run(['node', '--input-type=module'], input=code,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout


def test_connection_details_explain_the_account_without_exposing_protocol_ids():
    source = (STATIC / 'control-plane.js').read_text()
    run_node("import assert from 'node:assert/strict';\n"
             "const PROVIDER_LABELS={codex:'Codex',claude:'Claude Code',grok:'Grok Build',xai:'Grok Build'};\n"
             + function('providerAccountDetails', source) + """
assert.equal(providerAccountDetails({provider:'codex',auth_method:'subscription'}),'Codex · Subscription');
assert.equal(providerAccountDetails({provider:'claude',auth_method:'api_key'}),'Claude Code · API key');
for(const provider of ['grok','xai']) assert.equal(providerAccountDetails({provider,auth_method:'api_key'}),'Grok · SpaceXSI · API key');
assert.equal(providerAccountDetails({provider:'custom_runtime',auth_method:'enterprise_route'}),'custom runtime · Managed connection');
""")



def test_usage_summary_is_readable_and_token_details_are_explicit():
    source = (STATIC / 'control-plane.js').read_text()
    run_node("import assert from 'node:assert/strict';\n"
             + function('providerTimestamp', source) + function('observedDuration', source)
             + function('providerAccountUsage', source) + """
const account={last_verified_at:1792800000,observed_runs:33,observed_failures:2,
 observed_duration_seconds:600,observed_input_tokens:0,observed_output_tokens:27000};
const summary=providerAccountUsage(account);
assert(summary.includes('33 runs · 2 failed'));
assert(!summary.includes('Tokens'));assert(!summary.includes('worker time'));
assert(!providerTimestamp('Verified',account.last_verified_at).includes(':'));
const details=providerAccountUsage(account,true);
assert(details.includes('27,000 output'));assert(details.includes('0 input'));assert(details.includes('worker time'));
assert(!details.includes('Verified'));assert(!details.includes('33 runs'));
assert(details.includes('Cached input may be counted separately'));
assert.equal(providerAccountUsage({}), '');
""")


def test_ask_in_chat_only_exposes_an_owned_reusable_project():
    source = (STATIC / 'watch.js').read_text()
    run_node("import assert from 'node:assert/strict';\n" + function('syncAskChat', source) + """
const askChatButton={hidden:true,dataset:{}};let signedToken='';
const owned={can_manage_workspace:true,worker:{project_id:'project-a',workspace_kind:'named',state:'ready'}};
syncAskChat(owned);assert.equal(askChatButton.hidden,false);assert.equal(askChatButton.dataset.projectId,'project-a');
for(const changed of [{...owned,can_manage_workspace:false},{...owned,work_ref:'helper'},
 {...owned,worker:{...owned.worker,workspace_kind:'ephemeral'}},
 {...owned,worker:{...owned.worker,state:'terminated'}}]){
 syncAskChat(changed);assert.equal(askChatButton.hidden,true);assert.equal(askChatButton.dataset.projectId,'');
}
signedToken='signed-view';syncAskChat(owned);assert.equal(askChatButton.hidden,true);
""")


def test_watch_grows_for_a_long_instruction_without_consuming_the_entire_viewport():
    source = (STATIC / 'watch.js').read_text()
    run_node("import assert from 'node:assert/strict';\n" + function('autoResizeSteerInput', source) + """
const steerInput={style:{},scrollHeight:1200};let layouts=0;const syncWatchLayout=()=>layouts++;
const window={innerHeight:1000};autoResizeSteerInput();assert.equal(steerInput.style.height,'360px');
window.innerHeight=500;autoResizeSteerInput();assert.equal(steerInput.style.height,'210px');
steerInput.scrollHeight=35;autoResizeSteerInput();assert.equal(steerInput.style.height,'52px');
assert.equal(layouts,3);
""")


def test_composer_hints_float_without_hiding_blocking_state_or_queue_controls():
    styles = (STATIC / 'styles.css').read_text()
    guidance = styles[styles.index('.steer-guidance {'):styles.index('.steer-guidance-chip {')]
    assert 'position: absolute;' in guidance
    assert 'bottom: calc(100% + 8px);' in guidance
    assert 'opacity: 0;' in guidance
    assert '.watch-footer:focus-within .steer-guidance' in guidance
    assert '.watch-footer:has(textarea:disabled) .steer-guidance' in guidance
    assert '#send-button[data-mode="queue"]' in guidance
    assert 'flex: 1 1 100%;' not in styles[styles.index('.connection-more {'):styles.index('.connection-more-note')]
    page = (STATIC / 'watch.html').read_text()
    assert 'aria-describedby="steer-guidance"' in page
    assert 'aria-live="polite"' in page



def test_ready_subscription_menu_can_sign_in_again_without_changing_other_routes():
    source = (STATIC / 'control-plane.js').read_text()
    run_node("import assert from 'node:assert/strict';\n"
             + function('subscriptionRouteAvailable', source)
             + function('renderProviderAccounts', source) + """
class Element {
 constructor(tag='',text=''){this.tag=tag;this.textContent=text;this.children=[];this.handlers={};this.dataset={};this.attrs={};this.classList={toggle(){}};}
 get childElementCount(){return this.children.length;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(k,fn){this.handlers[k]=fn;}
 querySelectorAll(){return [];}
}
const node=(tag,cls,text='')=>new Element(tag,text),list=new Element(),native=new Element();
const document={getElementById:id=>id==='provider-account-list'?list:id==='existing-claude-connection'?native:null,querySelector:()=>null,addEventListener(){}};
const window={addEventListener(){}};let connectionMenuAbort=null,activeSetupAccount='';
const attachExistingClaudeConnection=()=>{},capabilityReviewRequest=()=>null,providerAccountDetails=()=>'',providerAccountUsage=()=>'',statusChip=s=>node('span','',s),attachActionMenu=()=>{};
let calls=[];const reconnectProviderAccount=(a,b)=>calls.push([a.account_id,b.textContent]);const verifyProviderAccount=()=>{};
const api={},pollSetup=()=>{};
let controlPlane={provider_options:[{provider:'codex',subscription_support:'supported'},{provider:'claude',subscription_support:'supported'},{provider:'grok',subscription_support:'supported'}]};
const base={account_id:'account-a',provider:'codex',auth_method:'subscription',platform_support:'supported',status:'ready'};
const descendants=e=>[e,...e.children.flatMap(descendants)];
for(const [change,expected] of [[{},true],[{provider:'claude'},true],[{provider:'grok'},true],[{provider:'openai'},true],
 [{status:'action_required',recovery_code:'credential_cleanup_failed'},true],
 [{auth_method:'api_key'},false],[{auth_method:'enterprise_route'},false],
 [{platform_support:'unsupported'},false],[{provider:'unknown'},false],
 [{auth_source:'current_os_claude_subscription'},false],[{status:'connecting'},false],
 [{status:'disconnected'},false],[{status:'action_required'},false]]){
 const account={...base,...change};controlPlane.provider_accounts=[account];renderProviderAccounts();
 const buttons=descendants(list).filter(e=>e.tag==='button');const action=buttons.find(e=>e.textContent==='Sign in again');
 assert.equal(Boolean(action),expected,JSON.stringify(change));
 if(action){assert.equal(action.attrs.role,'menuitem');assert.equal(action.tabIndex,-1);action.handlers.click();assert.deepEqual(calls.pop(),['account-a','Sign in again']);}
 if(account.status==='ready' && !account.auth_source)assert(buttons.some(e=>e.textContent==='Test connection'));
}
""")


def test_sign_in_again_reuses_setup_and_restores_its_label_after_request_error():
    source = (STATIC / 'control-plane.js').read_text()
    handler = re.search(r'^async function reconnectProviderAccount\(.*?^}', source, re.M | re.S).group(0)
    run_node("import assert from 'node:assert/strict';\n" + handler + """
const account={account_id:'account/a',auth_method:'subscription'};let activeSetupAccount='',setupPollTimer=null,status='',shown=null,stopped=0,calls=[];
const setProviderStatus=s=>status=s,showSetup=s=>shown=s,stopSetupPolling=()=>stopped++,pollSetup=()=>{};
const window={setTimeout:(fn,ms)=>{assert.equal(fn,pollSetup);assert.equal(ms,900);return 19;}};
const api={postJson:async path=>{calls.push(path);throw new Error('Account is busy');}};
const button={textContent:'Sign in again',disabled:false};await reconnectProviderAccount(account,button);
assert.deepEqual(calls,['/api/provider-accounts/account%2Fa/setup']);assert.equal(status,'Account is busy');assert.equal(button.textContent,'Sign in again');assert.equal(button.disabled,false);
api.postJson=async path=>{calls.push(path);return {status:'connecting',setup_flow:'browser'};};
await reconnectProviderAccount(account,button);assert.equal(activeSetupAccount,account.account_id);assert.equal(shown.status,'connecting');assert.equal(stopped,1);assert.equal(setupPollTimer,19);
assert.deepEqual(calls,['/api/provider-accounts/account%2Fa/setup','/api/provider-accounts/account%2Fa/setup']);
""")


def test_workspace_steer_waits_for_truthful_live_state_and_keeps_the_draft():
    source = (STATIC / 'app.js').read_text()
    run_node("import assert from 'node:assert/strict';\n" + function('syncTileSteerAvailability', source) + """
const input={value:'Keep this work request',disabled:false},send={disabled:false};
const tile={dataset:{workerId:'worker-a',ownerScope:'owner-a'},querySelector:s=>s.includes('textarea')?input:send};
const pendingWorkspaceSteer=()=>null;
syncTileSteerAvailability(tile,'unknown');assert.equal(input.disabled,true);assert.equal(send.disabled,true);
assert.equal(input.placeholder,'Checking workspace status…');assert.equal(input.value,'Keep this work request');
syncTileSteerAvailability(tile,'terminated');assert.equal(input.disabled,true);assert.equal(send.disabled,true);
assert.equal(input.placeholder,'This workspace is closed');
syncTileSteerAvailability(tile,'ready');assert.equal(input.disabled,false);assert.equal(send.disabled,false);
assert.equal(input.value,'Keep this work request');
""")
