"""Unknown launches recover through the actual draft owner and submit handler."""
import json
from pathlib import Path
import re
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def run(case):
    source = (STATIC / 'app.js').read_text()
    safe = re.search(r'^function canRetryLaunchWithSameKey\(.*?^}\n', source, re.M | re.S).group(0)
    show = re.search(r'^  function showLaunchRecovery\(.*?^  }\n', source, re.M | re.S).group(0)
    start = source.index("  form.addEventListener('submit', async (event) => {")
    handler = source[start:source.index('\n\n  const unfinishedLaunch', start)]
    restore = source[source.index('  const unfinishedLaunch'):source.index('\n\n  const requestedAddWorker')]
    script = r'''
import assert from 'node:assert/strict';
import {createLaunchDraft} from DRAFT;
class Field {
 constructor(value=''){this.value=value;this.dataset={};this.handlers={};this.selectedOptions=[{disabled:false}];this.hidden=false;}
 addEventListener(event,fn){(this.handlers[event]||=[]).push(fn);}
 dispatchEvent(event){for(const fn of this.handlers[event.type]||[])fn(event);}
 focus(){}append(){}
}
const ids=['description','success_criteria','context','worker-name','worker-choice','workspace-choice','execution-workspace','provider-account-selection','provider-account-policy','codex-effort','claude-effort','grok-effort','openclaw-effort','workspace-type','workspace-mode','workspace-file-placement','launch-surface','schedule-text','workspace-option'];
const fields=Object.fromEntries(ids.map(id=>[id,new Field()]));
const document={getElementById:id=>fields[id],querySelectorAll:()=>[],createElement:()=>({})};
Object.assign(fields.description,{value:'Exact unchanged job'});fields.context.value='Keep the supplied bytes';fields['success_criteria'].value='Return the answer';
fields['worker-choice'].value=fields['workspace-choice'].value='new';fields['worker-name'].value='Analyst';
fields['provider-account-selection'].value='chosen-account';fields['provider-account-selection'].dataset.accountState='ready';fields['provider-account-policy'].value='personal_required';
fields['workspace-option'].value='new:codex-cli';fields['workspace-type'].value='host';fields['workspace-mode'].value='isolated';fields['workspace-file-placement'].value='common';fields['launch-surface'].value='terminal';fields['codex-effort'].value='high';
const cache=new Map(),owner='a'.repeat(64),other='b'.repeat(64);
const storage={getItem:key=>cache.get(key)||null,setItem:(key,value)=>cache.set(key,value),removeItem:key=>cache.delete(key)};
const freshDraft=()=>createLaunchDraft({fields,storage:()=>storage});
let launchDraftState=freshDraft();launchDraftState.bindOwner(owner);
let bootstrap={draft_owner_scope:owner},identityTransition=false,launchInFlight=false,submit;
const form={addEventListener(_event,fn){submit=fn;}};
const select=fields['workspace-option'],providerAccount=fields['provider-account-selection'],providerAccountPolicy=fields['provider-account-policy'];
const workspaceType=fields['workspace-type'],workspaceMode=fields['workspace-mode'],workspaceFilePlacement=fields['workspace-file-placement'],launchSurface=fields['launch-surface'];
const codexEffort=fields['codex-effort'],claudeEffort=fields['claude-effort'],grokEffort=fields['grok-effort'],openclawEffort=fields['openclaw-effort'];
const scheduleText=fields['schedule-text'],scheduleButton=new Field(),button=new Field(),status=new Field(),help={};
const launchRecovery=new Field(),launchRecoveryHelp=new Field(),launchStartAgain=new Field();let launchRecoveryTarget=null;
const window={location:{href:'https://operator.example.invalid/',assign(url){navigations.push(url);}}};
const calls=[],navigations=[];let resets=0;
const fileDraft={blocked:()=>false,readyRefs:()=>[{upload_id:'exact-file',revision:'exact-revision'}],reset(){resets++;}};
const selectedWorkerEffort=()=>select.value.startsWith('new:')?codexEffort.value:'';
const syncWorkspaceUI=()=>({statusText:'Starting…'}),syncDirectEffort=async()=>{},withAuth=url=>url,csrfToken='csrf';
const launchFailureMessage=error=>error.message,responseError=async response=>Object.assign(new Error('Unavailable'),{status:response.status});
const watchHref=value=>value,showLaunchedWorkspace=()=>{},rememberCapabilityReview=()=>'',setActiveView=()=>{};
let reply=async()=>{throw new Error('Reply lost');};
const fetch=async(_url,options)=>{calls.push(JSON.parse(options.body));return reply();};
let refreshFailure=false;const refreshBootstrap=async()=>{if(refreshFailure)throw new Error('Catalog unavailable');};
SAFE
SHOW
HANDLER
const send=id=>submit({preventDefault(){},submitter:{id:id||'launch-button'}});
const reload=()=>{launchDraftState=freshDraft();launchDraftState.bindOwner(bootstrap.draft_owner_scope);RESTORE};
const saved=()=>JSON.parse(cache.get(`xperfect.launch-draft.v1.${owner}`));
const choose=selection=>{select.value=selection;fields['worker-choice'].value=selection.startsWith('open:')?'existing':'new';select.dispatchEvent(new Event('input'));};
''' + case
    script = (script.replace('DRAFT', json.dumps((STATIC / 'launch-draft.js').as_uri()))
              .replace('SAFE', safe).replace('SHOW', show).replace('HANDLER', handler).replace('RESTORE', restore))
    result = subprocess.run(['node', '--input-type=module'], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_unknown_reload_requires_visible_explicit_restart_and_preserves_exact_draft():
    run(r'''
await send();assert.equal(calls.length,1);const first=calls[0];assert.equal(saved().request.unknown,true);
reload();assert.equal(launchRecovery.hidden,false);assert.equal(launchStartAgain.textContent,'Start again');assert(launchRecoveryHelp.textContent.includes('may start a second job'));
await send();assert.equal(calls.length,1,'ordinary submit cannot repeat uncertain creation');
await send('launch-start-again');assert.equal(calls.length,2);assert.notEqual(calls[1].idempotency_key,first.idempotency_key);
const {idempotency_key:firstKey,...original}=first,{idempotency_key:nextKey,...restarted}=calls[1];assert.deepEqual(restarted,original);
assert.equal(resets,0);assert.equal(fields.description.value,'Exact unchanged job');assert.equal(providerAccount.value,'chosen-account');assert.equal(codexEffort.value,'high');
''')


def test_existing_and_copy_immediate_retry_keep_the_original_native_key():
    for selection in ['open:worker-1', 'duplicate:worker-1']:
        run('choose(' + json.dumps(selection) + r''');
await send();const first=calls[0];reload();assert.equal(launchStartAgain.textContent,'Retry job');assert(launchRecoveryHelp.textContent.includes('does not create another'));
await send('launch-start-again');assert.equal(calls.length,2);assert.deepEqual(calls[1],first);assert.equal(resets,0);
await send();assert.equal(calls.length,3);assert.deepEqual(calls[2],first);
''')


def test_shared_creation_and_schedule_retry_require_explicit_new_request():
    for setup, submitter in [
        ("fields['workspace-choice'].value='existing';fields['execution-workspace'].value='shared-1';", 'launch-button'),
        ("choose('open:worker-1');scheduleText.value='tomorrow';", 'schedule-button'),
        ("choose('duplicate:worker-1');scheduleText.value='tomorrow';", 'schedule-button'),
    ]:
        run(setup + 'await send(' + json.dumps(submitter) + r''');
const first=calls[0];reload();assert.equal(launchStartAgain.textContent,'Start again');
await send('launch-start-again');assert.equal(calls.length,2);assert.notEqual(calls[1].idempotency_key,first.idempotency_key);
const {idempotency_key:firstKey,...original}=first,{idempotency_key:nextKey,...restarted}=calls[1];assert.deepEqual(restarted,original);
''')


def test_stale_owner_or_changed_request_cannot_clear_an_unknown_request():
    run(r'''
await send();const original=saved().request;fields.description.value='Updated job';fields.description.dispatchEvent(new Event('input'));
await send('launch-start-again');assert.equal(calls.length,1);assert.deepEqual(saved().request,original);assert(status.textContent.includes('job changed'));
fields.description.value='Exact unchanged job';reload();bootstrap={draft_owner_scope:other};launchDraftState.bindOwner(other);
await send('launch-start-again');assert.equal(calls.length,1);assert.deepEqual(saved().request,original);
launchDraftState.bindOwner(owner);assert.equal(launchDraftState.restartUnknown(JSON.parse(original.signature),other),false);
assert.equal(launchDraftState.markUnknown({description:'Different'},owner),false);
''')


def test_unavailable_account_blocks_explicit_restart_without_losing_original_receipt():
    run(r'''
await send();const original=saved().request;providerAccount.dataset.accountState='needs_attention';
await send('launch-start-again');assert.equal(calls.length,1);assert.deepEqual(saved().request,original);assert(status.textContent.includes('Reconnect'));
providerAccount.dataset.accountState='ready';await send('launch-start-again');assert.equal(calls.length,2);assert.notEqual(calls[1].idempotency_key,calls[0].idempotency_key);
''')


def test_double_click_and_late_other_owner_reply_cannot_repeat_or_mutate_work():
    run(r'''
await send();let release;reply=()=>new Promise(resolve=>{release=resolve;});
const retry=send('launch-start-again');await send('launch-start-again');assert.equal(calls.length,2);
assert.equal(saved().request.unknown,true,'pending dispatch must survive a page close before catch');
bootstrap={draft_owner_scope:other};launchDraftState.bindOwner(other);release({ok:true,json:async()=>({watch_url:'/watch/old-worker'})});await retry;
assert.equal(resets,0);assert.equal(navigations.length,0);assert.equal(launchDraftState.unknownPayload(other),null);
launchDraftState.bindOwner(owner);assert(launchDraftState.unknownPayload(owner),'late foreign-owner response cannot settle the old request');
''')


def test_confirmed_rejection_keeps_its_key_without_remaining_unknown():
    run(r'''
reply=async()=>({ok:false,status:409});await send();const first=calls[0];
assert.equal(launchDraftState.unknownPayload(owner),null);assert.equal(launchRecovery.hidden,true);
await send();assert.equal(calls.length,2);assert.deepEqual(calls[1],first);
assert.equal(launchDraftState.confirmRequest({description:'Different'},owner),false);
''')


def test_saved_worker_account_rejection_does_not_change_a_safe_retry_identity_or_key():
    for selection in ['open:worker-1', 'duplicate:worker-1']:
        run('choose(' + json.dumps(selection) + r''');
await send();const first=calls[0];reply=async()=>({ok:false,status:409});
await send('launch-start-again');assert.equal(calls.length,2);assert.deepEqual(calls[1],first);
assert.equal(select.value,first.workspace_option);assert.equal(resets,0);assert.equal(launchDraftState.unknownPayload(owner),null);
await send();assert.deepEqual(calls[2],first);
''')


def test_confirmed_launch_is_not_unknown_when_its_followup_catalog_refresh_fails():
    for result, submitter, expected in [
        ({'status': 'scheduled', 'scheduled_for': 'Tomorrow'}, 'schedule-button', 'Scheduled for Tomorrow.'),
        ({'status': 'action_required', 'worker_id': 'copied-worker', 'capabilities_requiring_reapproval': 1},
         'launch-button', 'Workspace copied. Review 1 capability before running it.'),
        ({'worker_id': 'created-worker'}, 'launch-button', 'Project started. Open it from Workspaces.'),
    ]:
        run('scheduleText.value="tomorrow";refreshFailure=true;reply=async()=>({ok:true,json:async()=>('
            + json.dumps(result) + ')});await send(' + json.dumps(submitter) + ');'
            + 'assert.equal(status.textContent,' + json.dumps(expected) + r''');
assert.equal(calls.length,1);assert.equal(launchRecovery.hidden,true);assert.equal(launchDraftState.unknownPayload(owner),null);
''')
