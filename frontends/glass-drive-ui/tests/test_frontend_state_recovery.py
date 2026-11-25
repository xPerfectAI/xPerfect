"""Exercise real launch guards and Activity rendering without starting work."""
import os
from pathlib import Path
import re
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def node(source):
    result = subprocess.run(['node', '--input-type=module'], input=source, text=True,
                            capture_output=True, timeout=15, env={**os.environ, 'TZ': 'America/Toronto'})
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_launch_refusals_clear_after_correction_but_receipts_remain():
    app = (STATIC / 'app.js').read_text()
    start = app.index('  function clearLaunchValidation()')
    submit = app.index("  form.addEventListener('submit', async (event) => {", start)
    helpers = app[start:submit]
    guard = app[submit + len("  form.addEventListener('submit', async (event) => {"):app.index('    launchInFlight = true;', submit)]
    upload = re.search(r'onChange: \(\{ blocked \}\) => \{(.*?)\n    \},', app, re.S).group(1)
    helpers += re.search(r'^  let uploadsBlocked = .*?;$', app, re.M).group(0)
    node(r'''
import assert from 'node:assert/strict';
let bootstrap,select,status,providerAccountPolicy,providerAccount,fileDraft,workspaceType,scheduleText,fields,launchRecoveryTarget;
let launchInFlight=false;const handlers={},form={addEventListener:(name,handler)=>{handlers[name]=handler;}};
const document={getElementById:id=>fields[id],querySelectorAll:()=>[]};
const field=(value='')=>({value,dataset:{},selectedOptions:[{disabled:false}],focus(){this.focused=true;}});
const button={removeAttribute(){}};
function reset(){
 bootstrap={draft_owner_scope:'owner'};select=field('new:codex-cli');status={dataset:{}};
 providerAccountPolicy=field('personal_required');providerAccount=field('ready-account');providerAccount.dataset.accountState='ready';
 fileDraft={blocked:()=>false};workspaceType=field('host');scheduleText=field('Tomorrow');
 fields={'worker-choice':field('new'),'workspace-choice':field('new'),'worker-type':field('new:codex-cli'),
 'execution-workspace':field('shared'),'existing-worker':field('')};launchRecoveryTarget=null;
}
''' + helpers + '\nasync function check(submitter="launch-button"){const event={preventDefault(){},submitter:{id:submitter}};\n' + guard + r'''
return true;}
const cases=[
 ()=>{bootstrap=null;},()=>{select.dataset.workerChoiceState='unavailable';},
 ()=>{fields['worker-choice'].value='existing';},()=>{select.value='';fields['worker-type'].dataset.availability='empty';},
 ()=>{fields['workspace-choice'].value='existing';fields['execution-workspace'].value='';},
 ()=>{scheduleText.value='';return 'schedule-button';},
 ()=>{fields['worker-type'].selectedOptions[0].disabled=true;},
 ...['choose','needs_attention','catalog_unavailable',''].map(state=>()=>{providerAccount.dataset.accountState=state;providerAccount.value='';}),
 ()=>{fileDraft.blocked=()=>true;},()=>{workspaceType.selectedOptions[0].disabled=true;},
];
for(const setup of cases){reset();assert.equal(await check(setup()),undefined);assert.equal(status.dataset.validation,'true');assert(status.textContent);
 handlers.input();assert.equal(status.textContent,'');assert.equal(status.dataset.validation,undefined);
 showLaunchValidation('Try a corrected choice');handlers.change();assert.equal(status.textContent,'');}
reset();assert.equal(await check(),true);assert.equal(status.dataset.validation,undefined);
for(const receipt of ['Accepted. Your job has started.','I couldn’t confirm whether this job started.']){
 status.textContent=receipt;handlers.input();handlers.change();assert.equal(status.textContent,receipt);
}
function uploadChanged(blocked){
''' + upload + r'''
}
uploadChanged(true);showLaunchValidation('Files must finish uploading.');
uploadChanged(true);assert.equal(status.textContent,'Files must finish uploading.');
uploadChanged(false);assert.equal(status.textContent,'');
status.textContent='Accepted';uploadChanged(false);assert.equal(status.textContent,'Accepted');
''')


def test_launch_library_correction_uses_validation_owner_without_clearing_receipts():
    source = (STATIC / 'app.js').read_text()
    validation = source[source.index('  function clearLaunchValidation()'):source.index("  form.addEventListener('input', clearLaunchValidation)")]
    consumer = re.search(r'^  const launchLibrary = createLibrarySelection\(.*?;\n', source, re.M).group(0)
    node(r'''
import assert from 'node:assert/strict';
const status={dataset:{}},document={getElementById:()=>({})};
const createLibrarySelection=options=>options;
''' + validation + consumer + r'''
showLaunchValidation('Your Library selection changed. Check it and try again.');
launchLibrary.onChange();assert.equal(status.textContent,'');assert.equal(status.dataset.validation,undefined);
status.textContent='I couldn’t confirm whether this job started.';
launchLibrary.onChange();assert.equal(status.textContent,'I couldn’t confirm whether this job started.');
''')
