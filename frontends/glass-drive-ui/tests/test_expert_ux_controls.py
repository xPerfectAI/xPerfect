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


def test_the_ai_team_offers_every_model_the_install_has_and_keeps_a_saved_one():
    page = (STATIC / 'experts-page.js').read_text()
    # No model list of the page's own: the models come from the install's catalog.
    assert 'const MODELS' not in page and 'const MODELS' not in (STATIC / 'experts.js').read_text()
    assert "request('/api/expert-ai-options')" in page
    rows = re.search(r"export function modelName\(option\) \{.*?\n\}\n", page, re.S)
    team = re.search(r"export function teamRows\(options, spec\) \{.*?\n\}\n", page, re.S)
    read = re.search(r"export function readTeam\(state\) \{.*?\n\}\n", page, re.S)
    assert rows and team and read, 'team helpers'
    run_node("import assert from 'node:assert/strict';\n"
             "const AIS = [['claude-code', 'Claude'], ['codex-cli', 'Codex'], ['grok-build', 'Grok']];\n"
             + rows.group(0).replace('export ', '') + team.group(0).replace('export ', '') + read.group(0).replace('export ', '') +
             "const options = [{profile: 'codex-cli', usual: 'codex-cli:native-default', models: ["
             "{id: 'codex-cli:gpt-6.2-sol', label: 'Codex / GPT-6.2 Sol', status: 'available'},"
             "{id: 'codex-cli:gpt-6-sol', label: 'Codex / GPT-6 Sol', status: 'available'}]},"
             "{profile: 'claude-code', usual: 'claude-code:opus', models: [{id: 'claude-code:haiku', label: 'Claude / Haiku', status: 'available'}]}];\n"
             "const spec = {team: {talk: {profile: 'claude-code', model: 'claude-code:sonnet-5-5'}, coordinate: {profile: 'codex-cli', model: ''},"
             " work: ['claude-code', 'codex-cli:gpt-6-sol']}};\n"
             "const rows = teamRows(options, spec);\n"
             "assert.deepEqual(rows.map(r => r.profile), ['claude-code', 'codex-cli', 'grok-build']);\n"
             # Every model the install has, by its own name under its AI; the usual one stays its own choice.
             "assert.deepEqual(rows[1].models.map(m => m.name), ['GPT-6.2 Sol', 'GPT-6 Sol']);\n"
             "assert.equal(rows[1].usual, 'codex-cli:native-default');\n"
             # A model saved exactly stays as saved, under its own AI only.
             "assert.deepEqual(rows[0].models.map(m => [m.id, m.status]), [['claude-code:haiku', 'available'], ['claude-code:sonnet-5-5', 'saved']]);\n"
             "assert.equal(rows[1].models.some(m => m.id === 'claude-code:sonnet-5-5'), false);\n"
             "assert.deepEqual(rows[2].models, []);\n"
             # What the page saves: the chosen models, the Workers, and Preferred only when it is one of them.
             "const state = {talk: {profile: 'claude-code', model: 'claude-code:haiku'}, coordinate: {profile: 'codex-cli', model: ''},"
             " work: ['claude-code', 'codex-cli:gpt-6-sol'], preferred: 'codex-cli:gpt-6-sol', replies: 'low',"
             " account: {talk: '', coordinate: ''}, backup: {talk: 'codex-cli', coordinate: ''}};\n"
             "const saved = readTeam(state);\n"
             "assert.deepEqual(saved.talk, {profile: 'claude-code', account_id: '', backup: {profile: 'codex-cli', account_id: ''}, effort: 'low', model: 'claude-code:haiku'});\n"
             "assert.deepEqual(saved.work, ['claude-code', 'codex-cli:gpt-6-sol']); assert.equal(saved.preferred, 'codex-cli:gpt-6-sol');\n"
             "assert.equal('preferred' in readTeam({...state, preferred: 'grok-build'}), false);\n"
             "assert.throws(() => readTeam({...state, work: []}), /at least one AI/);\n")
    # New Experts still talk on the faster model; the others are one choice away.
    assert "talk: { profile: 'claude-code', account_id: '', backup: null, effort: 'low', model: 'claude-code:haiku' }" in (STATIC / 'experts.js').read_text()


def test_needs_you_pops_up_on_every_page_including_the_worker_viewers():
    stamp = re.search(r"experts\.js\?v=(\w+)", (STATIC / 'needs-you.js').read_text()).group(1)
    for page in ('conversation.html', 'watch.html', 'desktop.html', 'terminal.html'):
        html = (STATIC / page).read_text()
        assert f'src="/static/needs-you.js?v={stamp}"' in html, page
        assert f'href="/static/needs-you.css?v={stamp}"' in html, page
    assert f'href="/static/needs-you.css?v={stamp}"' in (STATIC / 'index.html').read_text()
    # One stamp for every import of the Experts files: a page that still names an older one would keep the old code.
    for path in sorted(STATIC.glob('*')):
        if path.suffix in {'.js', '.html'}:
            for found in re.findall(r"(?:experts|needs-you)\.(?:js|css)\?v=(\w+)", path.read_text()):
                assert found == stamp, (path.name, found)
    # One owner for the pop-up's look, readable on pages without the main theme.
    assert 'needs-you-toast' not in (STATIC / 'experts.css').read_text()
    toast = (STATIC / 'needs-you.css').read_text()
    for token in ('var(--overlay-surface,', 'var(--text,', 'var(--line-strong,', 'prefers-color-scheme:light'):
        assert token in toast, token


def test_shared_folder_note_shows_once_a_second_person_could_join():
    source = (STATIC / 'experts.js').read_text()
    note = re.search(r"^export function sharedFolderNote\(.*?^}", source, re.M | re.S)
    assert note, 'sharedFolderNote'
    assert 'sharedFolderNote(kind, channel.people.length, channel.people_waiting.length)' in source
    run_node("import assert from 'node:assert/strict';\n" + note.group(0).replace('export ', '') + """
assert.equal(sharedFolderNote('host', 1, 0), '');
assert.equal(sharedFolderNote('host', 0, 1), '');
assert.match(sharedFolderNote('host', 1, 1), /own folder/);
assert.match(sharedFolderNote('host', 1, 1), /can still read another person/);
assert.match(sharedFolderNote('host', 2, 0), /sandboxed workspace/);
assert.equal(sharedFolderNote('sandboxed', 3, 2), '');
""")


def test_the_chat_page_shows_the_reply_while_it_is_written():
    source = (STATIC / 'conversation.js').read_text()
    assert "} else if (turn.partial_reply && !turn.blocker) {" in source
    assert "messages.push(assistantMessage(turn.partial_reply, true));" in source
    run_node("import assert from 'node:assert/strict';\n"
             "const markdown={render:t=>t};\nfunction node(tag,text,cls){return {tag,cls,innerHTML:''};}\n"
             + function('assistantMessage', source) + """
assert.equal(assistantMessage('Hello', true).cls, 'message assistant writing');
assert.equal(assistantMessage('Hello').cls, 'message assistant');
""")
    page = (STATIC / 'conversation.html').read_text()
    assert 'conversation.js?v=20261124stream1' in page and 'conversation.css?v=20261124stream1' in page


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
 observed_duration_seconds:600,observed_input_tokens:0,observed_output_tokens:27000,observed_usage_coverage:'complete'};
const summary=providerAccountUsage(account);
assert(summary.includes('33 runs · 2 failed'));
assert(!summary.includes('Tokens'));assert(!summary.includes('worker time'));
assert(!providerTimestamp('Verified',account.last_verified_at).includes(':'));
const details=providerAccountUsage(account,true);
assert(details.includes('27,000 output'));assert(details.includes('0 input'));assert(details.includes('worker time'));
assert(!details.includes('Verified'));assert(!details.includes('33 runs'));
assert(details.includes('Cached input may be counted separately'));
const historical=providerAccountUsage({...account,observed_usage_coverage:'unknown'},true);
assert(historical.includes('Usage: Unknown'));assert(!historical.includes('27,000 output'));
const partial=providerAccountUsage({...account,observed_usage_coverage:'partial',observed_unknown_runs:1},true);
assert(partial.includes('Usage: Partial'));assert(partial.includes('1 run has unknown usage'));
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


def test_create_card_uses_new_worker_setup_from_existing_or_copy_without_losing_job():
    source = (STATIC / 'app.js').read_text()
    start = source.index("      const name = document.getElementById('worker-name');", source.index('expertPicker = createExpertPicker'))
    end = source.index('      const value = `new:${expert.profile}`;', start)
    body = source[start:end]
    run_node("import assert from 'node:assert/strict';\n" + r"""
for(const value of ['open:worker-a','duplicate:worker-a','new:claude-code']){
 const select={value,dispatchEvent(){}};
 const fields={'description':{value:'Exact goal',focus(){this.focused=true}},'worker-name':{value:'My own name'},'worker-choice':{value:'existing'},
  'worker-type':{value:'new:claude-code'},'worker-choice-title':{focus(){this.focused=true}}};
 const document={getElementById:id=>fields[id]};
 let automaticExpertName='Automatic expert',preparedExpertWorkerId='prepared',renders=0;
 const bootstrap={};const onboarding={render(){renders++}};
 function renderWorkspaceOptions(target,data,choice){assert.equal(data,bootstrap);target.value=choice;}
 function act(options){const expert=null;
""" + body + r"""
 }
 act({custom:true});
 assert.equal(select.value,'new:claude-code');assert.equal(fields['worker-choice'].value,'new');
 assert.equal(renders,1);assert.equal(fields['worker-name'].value,'My own name');
 assert(fields['worker-choice-title'].focused);assert.equal(preparedExpertWorkerId,'');
 fields.description.value='';act({custom:true});assert(fields.description.focused,'An empty job is the next required step');
}
""")


def test_launch_label_updates_preserve_icon_and_truthful_recovery_text():
    source = (STATIC / 'app.js').read_text()
    run_node("import assert from 'node:assert/strict';\nconst workspaceMeta=()=>({buttonText:'Run Project',help:'Exact help'});\n"
             + function('syncWorkspaceUI', source) + r"""
const label={textContent:'Start fresh copy'}, icon={retained:true};
const button={children:[icon,label],querySelector:()=>label};const help={};
syncWorkspaceUI({value:'new:codex-cli'},{},button,help);
assert.equal(label.textContent,'Run Project');assert.equal(button.children[0],icon);assert.equal(help.textContent,'Exact help');
const legacy={textContent:''};syncWorkspaceUI({value:''},{},legacy,help);assert.equal(legacy.textContent,'Run Project');
""")


def test_a_teams_channel_says_connecting_until_microsofts_socket_holds():
    # The first real test: a new bot was refused for its first seconds. The pill says so, the page looks again
    # by itself until the socket holds, and a lasting refusal is said once on the card.
    source = (STATIC / 'experts.js').read_text()
    state = re.search(r"^export function channelState\(.*?^}", source, re.M | re.S)
    assert state, 'channelState'
    assert "node('span', 'expert-pill', channelState(channel))" in source
    assert "if (channels.some(c => c.state === 'connected' && c.connecting)) again = setTimeout(later, 3000);" in source
    assert "list.querySelector('details[open]') || list.contains(document.activeElement)" in source  # not mid-typing
    assert "if (channel.refusal && channel.state === 'connected') card.append(" in source  # never on an Off card
    # Teams does not allow the app yet: the line, a note for the administrator, and the page follows the
    # runtime's own re-check.
    assert "if (step.step === 'blocked' || step.step === 'admin_waiting') {" in source
    assert "Copy a note for your administrator" in source
    assert "timer = setTimeout(() => poll(channel), 10000); return;" in source
    run_node("import assert from 'node:assert/strict';\n" + state.group(0).replace('export ', '') + """
assert.equal(channelState({state: 'connected', online: true, connecting: false, first_message: true}), 'Connected');
assert.equal(channelState({state: 'connected', online: true, connecting: false, first_message: false}), 'Online · no message yet');
assert.equal(channelState({state: 'connected', online: false, connecting: true}), 'Connecting…');
assert.equal(channelState({state: 'connected', online: false, connecting: false}), 'Connected · offline');
assert.equal(channelState({state: 'off', online: true, connecting: false}), 'Off');
assert.equal(channelState({state: 'setup', online: false, connecting: false}), 'Not connected');
""")


def test_test_in_teams_waits_until_a_first_message_came_through():
    # The first real test: before the app was installed, "Test in Teams" (a chat opened by the bot's id) opened an
    # existing chat with another person. It is offered only once a first message has come through, and the page
    # says whose chat it should open.
    source = (STATIC / 'experts.js').read_text()
    links = re.search(r"^export function teamsLinks\(.*?^}", source, re.M | re.S)
    assert links, 'teamsLinks'
    assert "for (const link of teamsLinks(channel))" in source and source.count("l/chat/0/0") == 1
    assert "Test in Teams opens your chat with ${name}. If another chat opens, send nothing there." in source
    assert "Press Test in Teams and send it a message" not in source
    run_node("import assert from 'node:assert/strict';\n" + links.group(0).replace('export ', '') + """
const app = {app_id: 'bot-synthetic', install_link: 'https://teams.invalid/install'};
const labels = channel => teamsLinks(channel).map(link => link.label);
assert.deepEqual(labels({state: 'connected', online: true, first_message: false, app}), ['Install in Teams']);
assert.deepEqual(labels({state: 'connected', online: false, first_message: false, app}), ['Install in Teams']);
assert.deepEqual(labels({state: 'connected', online: true, first_message: true, app}), ['Test in Teams']);  // installed: no Install
assert.deepEqual(labels({state: 'connected', online: true, first_message: true, app: {app_id: 'bot-synthetic'}}), ['Test in Teams']);
// Hurdle 15: once an administrator added the app, the developer copy's install link is a dead end: not offered.
assert.deepEqual(labels({state: 'connected', online: true, first_message: false, app: {...app, added_by_admin: 'yes'}}), []);
// The administrator's route: the catalogue's own link opens the app, ready to add; never the blocked developer copy.
const listed = {...app, catalogue: 'listed', install_link: 'https://teams.microsoft.com/l/app/catalogue-synthetic?tenantId=t'};
assert.deepEqual(teamsLinks({state: 'connected', first_message: false, app: listed}), [{label: 'Open in Teams', href: listed.install_link, style: 'secondary-button'}]);
assert.deepEqual(labels({state: 'connected', first_message: false, app: {...listed, catalogue: 'installed'}}), ['Open in Teams']);
assert.deepEqual(labels({state: 'connected', first_message: false, app: {...app, catalogue: 'pending'}}), []);
assert.deepEqual(labels({state: 'connected', first_message: true, app: listed}), ['Test in Teams']);
""")


def test_the_person_is_told_up_front_what_microsoft_and_teams_will_ask():
    # The first real test's hurdles 1, 2, 4, 5 and 7: where to open the sign-in, a phone approval, Teams asking web or
    # desktop, a change still spreading, and which account and organisation the app was made in.
    source = (STATIC / 'experts.js').read_text()
    for words in ("where your work account is signed in (any browser, or your phone)",
                  "Microsoft may ask you to approve on your phone.", "The code lasts about 15 minutes.",
                  "Creating your Teams app with ${step.account}…", "Made with ${channel.app.account}",
                  "Teams may ask web or desktop, either works", "press Install in Teams again",
                  "Microsoft may still be applying this. If Teams says Permissions needed, wait a few minutes and press Install again."):
        assert words in source, words


def test_the_owner_sets_the_teams_apps_public_details():
    # Hurdle 10: the install window showed "Developer" and example.com. The card offers the four details Teams shows,
    # and the page's server forwards them to the runtime, which checks and applies them.
    source = (STATIC / 'experts.js').read_text()
    for words in ("function appDetails(channel)", "'Details Teams shows for this app'", "card.append(appDetails(channel));",
                  "'/details', { method: 'POST'", "Microsoft needs all four; the links start with https://.",
                  "input.maxLength = 32"):
        assert words in source, words
    server = (STATIC.parent / 'server.py').read_text()
    assert '@app.post("/api/channels/{binding_id}/details")' in server
    assert '+ "/details", payload)' in server
    assert '.expert-details[open]{display:grid;gap:8px}' in (STATIC / 'experts.css').read_text()


def test_an_already_signed_in_connect_names_the_account_on_its_one_press():
    # Hurdle 11: the Connect button itself names the account ("Connect Teams as …"), its press is the yes, and a small
    # "Use another Microsoft account" sits beside it; no second dialog.
    source = (STATIC / 'experts.js').read_text()
    for words in ("async function nameConnect(channel, go, box)", "`Connect Teams as ${who.account}`",
                  "'Use another Microsoft account'", "connect(channel, { as_account: go.dataset.account || '' })",
                  "connect(channel, { other_account: true })", "void nameConnect(channel, go, connectBox);"):
        assert words in source, words
    assert "step.step === 'confirm'" not in source  # no second dialog
    server = (STATIC.parent / 'server.py').read_text()
    assert 'choice = {"as_account": str(payload.get("as_account") or "")[:320], "other_account": bool(payload.get("other_account")),' in server
    assert '.expert-other-account{' in (STATIC / 'experts.css').read_text()



def test_a_teams_app_still_offered_beyond_personal_chats_says_so():
    # Hurdle 14: if narrowing the app to personal chats failed, the card says it plainly.
    source = (STATIC / 'experts.js').read_text()
    assert "if (channel.app.scopes && channel.app.scopes !== 'personal') card.append(" in source
    assert "Teams still offers this app in teams and group chats; it answers only in personal chats." in source


def test_the_owner_can_say_an_administrator_added_the_app():
    source = (STATIC / 'experts.js').read_text()
    for words in ("function adminButton(channel, added)", "'An administrator added it for me'", "'Undo: show Install in Teams again'",
                  "it appears in your Teams by itself (Apps, Built for your org; this can take a few hours)",
                  "'/added-by-admin', { method: 'POST', body: { added } }"):
        assert words in source, words
    assert '@app.post("/api/channels/{binding_id}/added-by-admin")' in (STATIC.parent / 'server.py').read_text()


def test_an_administrator_adds_the_app_to_the_organisations_teams_apps_with_one_press():
    # While Teams refuses custom apps for the person, the card offers one press for a Teams administrator: the button
    # names the change, the hint says what it makes visible and how to undo it, Microsoft's own page is the yes, and
    # the page follows the setup until the app is in the organisation's Teams apps.
    source = (STATIC / 'experts.js').read_text()
    for words in ("function adminRoute(channel, primary)", "`Add ${name} to my organisation's Teams apps`",
                  "connect(channel, { as_admin: true })", "where people may find it as your organisation's settings allow",
                  "only the people listed here get an answer", "An administrator can delete it again in the Teams admin center.",
                  "if (step.step === 'admin_sign_in') {", "sign in with a Teams administrator account of this organisation",
                  "approving it there is the yes", "if (step.step === 'adding') {", "if (step.admin) box.append(...adminRoute(channel, true));",
                  "if (catalogue === 'pending') {", "if (channel.admin_route) card.append(...adminRoute(channel, false));",
                  "is in your organisation's Teams apps and installed for you",
                  "Teams shows the details of the version there",
                  "One press for a Teams administrator is not available on this xPerfect yet",
                  "For the Teams app made with ${step.account}",
                  "“Read and write to all app catalogs”, “Manage user's installed Teams apps” and “Sign in and read user profile”",
                  "If an administrator added this app to your organisation’s Teams apps, Teams shows the details of the version there",
                  "Narrowing it takes a new version of the app",
                  "This app was made before xPerfect narrowed new apps to personal chats"):
        assert words in source, words
    server = (STATIC.parent / 'server.py').read_text()
    assert '"as_admin": bool(payload.get("as_admin"))}' in server


def test_the_ai_choices_show_each_ais_own_mark_from_the_worker_pages():
    page = (STATIC / 'experts-page.js').read_text()
    # Each AI heads its group of models with its own mark and name; each role's account row carries it too.
    assert "who.append(aiMark(ai.profile), node('strong', '', ai.name))" in page
    assert "who.append(aiMark(profile), node('strong', '', `${title} · ${AI_NAME[profile] || profile}`))" in page
    source = (STATIC / 'experts.js').read_text()
    # The same images the Worker pages use; theme.css keeps them legible in the dark theme.
    assert "img.src = `/static/provider-${name}.svg?v=${MARK_VERSION}`" in source
    for name in ('claude', 'codex', 'grok'):
        assert (STATIC / f'provider-{name}.svg').exists(), name
    assert '.provider-mark.expert-ai-mark{width:18px;height:18px' in (STATIC / 'experts.css').read_text()


def test_channels_show_marks_of_xperfects_own_and_an_installs_brand_mark_only_beside_the_name():
    source = (STATIC / 'experts.js').read_text()
    # Buttons always carry the neutral mark, never a brand's own.
    assert "add.prepend(channelMark('teams'))" in source and "slack.prepend(channelMark('slack'))" in source
    # The card's title: the mark beside the plain-text name; a brand's mark only when this install placed one.
    assert "title.append(channelMark(channel.kind, brand), node('strong', '', CHANNEL_NAMES[channel.kind] || channel.label))" in source
    assert "export const CHANNEL_NAMES = { teams: 'Microsoft Teams', slack: 'Slack' };" in source
    assert "Microsoft Teams is a trademark of the Microsoft group of companies." in source
    # No third-party channel logo ships in the repository.
    assert not [p.name for p in STATIC.rglob('*') if re.search(r'(teams|slack)', p.name, re.I) and p.suffix in ('.svg', '.png')]
    loader = re.search(r"let brandAssets = null;\n.*?\n}\n", source, re.S)
    assert loader, 'loader'
    run_node("import assert from 'node:assert/strict';\n" + loader.group(0).replace('export ', '') + "\n"
             "assert.deepEqual(await loadBrandAssets(async () => ({ ok: true, json: async () => ({ channels: { teams: '/brand-assets/channel-teams.svg' } }) })),"
             " { teams: '/brand-assets/channel-teams.svg' });\n"
             "brandAssets = null; assert.deepEqual(await loadBrandAssets(async () => { throw new Error('offline'); }), {});\n"
             "brandAssets = null; assert.deepEqual(await loadBrandAssets(async () => ({ ok: false })), {});\n")


def test_an_expert_has_a_picture_that_teams_shows_as_its_app_icon():
    source = (STATIC / 'experts.js').read_text()
    page = (STATIC / 'experts-page.js').read_text()
    assert "const PICTURE_SIZE = 192;" in source and "canvas.toDataURL('image/png')" in source
    # Cut to a centred square in the browser; sent after the Expert is saved, so a new Expert gets it too.
    assert "drawImage(bitmap, (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side, 0, 0, size, size)" in source
    assert "if (pictureChange && saved?.template_id) {" in page
    assert "+ '/picture', { method: 'PUT', body: pictureChange });" in page
    assert "Teams shows it as the app icon. Kept on this computer, not in its versions." in page
    assert "`Teams shows ${name}'s picture as its app icon.`" in source
    assert "`${name}'s picture is not in Teams yet: ${channel.app.picture_error}`" in source


def test_the_page_forwards_an_experts_picture_to_the_runtime():
    server = (STATIC.parent / 'server.py').read_text()
    assert '@app.get("/api/experts/{expert_id}/picture")' in server and '@app.put("/api/experts/{expert_id}/picture")' in server
    assert '_expert_forward(request, "PUT", "/v1/experts/" + _recurring_path_id(expert_id, "expert id") + "/picture", payload)' in server


def test_the_owners_card_shows_the_words_a_held_job_would_send():
    # On Always ask the hand-off's words are part of what the owner approves: they are on the card.
    source = (STATIC / 'experts.js').read_text()
    assert "if (item.kind === 'job' && item.payload.tell) card.append(node('p', 'needs-you-what', `Will tell the person: ${item.payload.tell}`));" in source


def test_an_ai_not_set_up_on_this_computer_says_so_with_the_way_to_connect_it():
    page = (STATIC / 'experts-page.js').read_text()
    helper = re.search(r"export function workerAccountLine\(\{.*?\n\}\n", page, re.S)
    assert helper, 'workerAccountLine'
    run_node("import assert from 'node:assert/strict';\n" + helper.group(0).replace('export ', '') +
             "const local = {host: true, sandboxed: false};\n"
             "const line = (o) => workerAccountLine({name: 'Codex', place: 'host', need: local, ...o});\n"
             # No account ready: say so where the runtime needs one, and block new Workers there.
             "assert.deepEqual(line({workerAccount: 'none'}), {text: 'Codex Workers need a connected Codex account.', action: 'Connect Codex', blocks: true});\n"
             # Sandboxed on a local install: the runtime does not apply the rule, so nothing is said or blocked.
             "assert.equal(line({workerAccount: 'none', place: 'sandboxed'}), null);\n"
             # On a packaged install it applies everywhere.
             "assert.equal(line({workerAccount: 'none', place: 'sandboxed', need: {host: true, sandboxed: true}}).blocks, true);\n"
             # Exactly one (the default or the only one): fine. Several with no default: choose one.
             "assert.equal(line({workerAccount: 'one'}), null);\n"
             "assert.match(line({workerAccount: 'several'}).text, /mark one as the default in Connections/);\n"
             "assert.match(line({workerAccount: 'several', inRole: true}).text, /choose it under Accounts/);\n"
             # An account chosen for that AI's role is what its Workers use.
             "assert.equal(line({workerAccount: 'none', roleAccount: 'acct-1'}), null);\n"
             # The tool missing on this computer and no account: not set up; unknown is never a problem.
             "assert.equal(line({workerAccount: 'none', cliInstalled: false}).text, 'Codex is not set up on this computer yet.');\n"
             "assert.equal(line({workerAccount: 'unknown'}), null); assert.equal(line({workerAccount: 'none', need: null}), null);\n")
    # The answer comes from the runtime's read, for the place the expert is set to; no provider table of the page's own.
    assert "need: r.workers_need_account || null" in page and "workerAccount: offered?.worker_account || 'unknown'" in page
    assert "need: state.need, place: state.place" in page
    assert "'codex-cli': ['codex', 'openai']" not in page
    assert "box.disabled = (unavailable || Boolean(accountLines[ai.profile]?.blocks)) && !ticked;" in page
    assert "const link = node('a', 'secondary-button compact-button', line.action); link.href = '/#connections';" in page
    # Opened before the account data has loaded, the page draws the team again once it is there.
    assert "if (!getData()) { const wait = (tries = 0) => setTimeout(() => { if (mine !== token) return; if (getData()) renderTeam();" in page


def test_unsaved_changes_are_kept_until_saved_or_discarded_and_asked_about_in_the_page():
    page = (STATIC / 'experts-page.js').read_text()
    assert 'confirm(' not in page and 'alert(' not in page, 'no browser dialog'
    # Choosing another expert with changes asks in the page: keep editing, or discard and go.
    assert "unsaved.replaceChildren(node('span', '', `Your changes to ${label} are not saved.`));" in page
    assert "'Keep editing'" in page and "if (dirty && !force) { askBeforeLeaving(arg); return; }" in page
    # Kept in this tab, per expert (a new one per owner), without the install's model list; a reload brings them back,
    # also before the page's account data has loaded.
    assert "if (id && id !== 'new') return `xperfect.expert-draft:${id}`;" in page
    assert "return owner ? `xperfect.expert-draft:${owner}:new` : '';" in page
    assert "const { offered, need, ...kept } = state;" in page
    assert "Your unsaved changes are back. " in page
    # Saving or discarding forgets them.
    assert page.count('dropDraft()') >= 3


def test_the_ai_team_table_says_what_each_role_is_and_works_as_one_control():
    page = (STATIC / 'experts-page.js').read_text()
    for words in ("['Talks to people', 'Answers at once', '']", "['Coordinates', 'Plans and hands out the work', '']",
                  "['Workers', 'Do the work, several at once', '']"):
        assert words in page, words
    # A click anywhere in a cell picks its choice; arrows move through the table; one Tab stop.
    assert "cell.querySelector('input:not(:disabled)')?.click();" in page
    assert "for (const c of all) c.tabIndex = c === next ? 0 : -1;" in page
    for key in ('ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'):
        assert f"'{key}'" in page, key
    # A choice rebuilds the table without losing keyboard focus.
    assert "if (focused) [...four.box.querySelectorAll('[aria-label]')].find(el => el.getAttribute('aria-label') === focused)?.focus();" in page


def test_saving_keeps_the_coordinators_effort_and_a_new_ai_drops_the_old_ais_account():
    page = (STATIC / 'experts-page.js').read_text()
    helpers = [re.search(r"export function " + name + r"\(.*?\n\}\n", page, re.S) for name in ('chooseRole', 'readTeam')]
    assert all(helpers), 'chooseRole, readTeam'
    run_node("import assert from 'node:assert/strict';\n" + ''.join(h.group(0).replace('export ', '') for h in helpers) + """
const providers = {'claude-code': ['claude', 'anthropic'], 'codex-cli': ['codex', 'openai']};
const accounts = [{account_id: 'acct-claude', provider: 'Anthropic'}, {account_id: 'acct-codex', provider: 'openai'}];
const map = {accounts, providers};
const saved = () => ({talk: {profile: 'claude-code', model: 'claude-code:haiku'}, coordinate: {profile: 'claude-code', model: 'claude-code:opus'},
  coordinatorEffort: {profile: 'claude-code', model: 'claude-code:opus', effort: 'xhigh'}, work: ['claude-code'], preferred: '', replies: 'low',
  account: {talk: '', coordinate: 'acct-claude'}, backup: {talk: 'codex-cli', coordinate: ''}});
// Saving keeps a stored coordinator effort the page does not show.
let state = saved();
assert.equal(readTeam(state).coordinate.effort, 'xhigh');
// Another coordinator may not offer it: it goes back to that AI's own; the saved choice brings it back.
chooseRole(state, 'coordinate', 'codex-cli', '', map); assert.equal(readTeam(state).coordinate.effort, '');
chooseRole(state, 'coordinate', 'claude-code', 'claude-code:haiku', map); assert.equal(readTeam(state).coordinate.effort, '');
chooseRole(state, 'coordinate', 'claude-code', 'claude-code:opus', map); assert.equal(readTeam(state).coordinate.effort, 'xhigh');
// A draft kept before this rule (no saved effort in it) saves the AI's own effort, as before.
assert.equal(readTeam({...saved(), coordinatorEffort: undefined}).coordinate.effort, '');
// The same AI on another model keeps its account; another AI drops it, so Codex is never saved with a Claude account.
state = saved();
chooseRole(state, 'coordinate', 'claude-code', 'claude-code:haiku', map); assert.equal(state.account.coordinate, 'acct-claude');
chooseRole(state, 'coordinate', 'codex-cli', 'codex-cli:gpt-6-sol', map); assert.equal(readTeam(state).coordinate.account_id, '');
// An account the install's map gives to the new AI too stays; with no account data yet, it cannot be shown to, and goes.
state = saved(); chooseRole(state, 'coordinate', 'codex-cli', '', {accounts, providers: {...providers, 'codex-cli': ['anthropic']}});
assert.equal(state.account.coordinate, 'acct-claude');
state = saved(); chooseRole(state, 'coordinate', 'codex-cli', ''); assert.equal(state.account.coordinate, '');
// A backup that is the new AI itself goes; the talking role follows the same rule.
state = {...saved(), account: {talk: 'acct-claude', coordinate: ''}};
chooseRole(state, 'talk', 'codex-cli', '', map);
assert.deepEqual([state.talk, state.backup.talk, state.account.talk], [{profile: 'codex-cli', model: ''}, '', '']);
""")
    # The table's two radios and the sandbox switch all choose through it, with the install's own account map.
    for call in ("chooseRole(state, 'talk', ai.profile, line.id, accountMap())", "chooseRole(state, 'coordinate', ai.profile, line.id, accountMap())",
                 "chooseRole(state, role, 'claude-code', '', accountMap())"):
        assert call in page, call
    assert 'state.talk = {' not in page and 'state.coordinate = {' not in page and "state[role] = { profile: 'claude-code'" not in page


def test_what_workers_need_is_asked_again_when_the_page_is_shown_again():
    page = (STATIC / 'experts-page.js').read_text()
    reader = re.search(r"export function optionsReader\(request\) \{.*?\n\}\n", page, re.S)
    assert reader, 'optionsReader'
    run_node("import assert from 'node:assert/strict';\n" + reader.group(0).replace('export ', '') + """
const answers = [{items: [{profile: 'codex-cli', worker_account: 'none'}], workers_need_account: {host: true}},
                 {items: [{profile: 'codex-cli', worker_account: 'one'}], workers_need_account: {host: true}}];
let asked = 0;
const read = optionsReader(path => { assert.equal(path, '/api/expert-ai-options'); asked += 1;
  return asked <= 2 ? Promise.resolve(answers[asked - 1]) : Promise.reject(new Error('synthetic outage')); });
assert.equal((await read()).items[0].worker_account, 'none');
// The owner connected an account meanwhile: the next showing has the runtime's new answer.
assert.equal((await read()).items[0].worker_account, 'one');
// An answer that fails keeps the last one; with none yet, the page says the list is unavailable.
assert.deepEqual(await read(), {items: answers[1].items, need: {host: true}});
assert.equal(asked, 3);
assert.equal(await optionsReader(() => Promise.reject(new Error('synthetic outage')))(), null);
""")
    # Coming back to the page re-enters show() with the same expert: there it asks again and redraws the table.
    back = page.split('async function show(')[1].split('if (dirty && !force)')[0]
    assert 'void loadOptions().then(answer => { if (mine !== token || !state || !answer) return; state.offered = answer.items; state.need = answer.need; renderTeam(); });' in back
    assert 'return readOptions();' in page and 'const readOptions = optionsReader(request);' in page
