"""Causal source/DOM-fixture checks; integrated browser QA is a separate gate."""
import json
from pathlib import Path
import subprocess

STATIC = Path(__file__).parents[1] / 'src/glass_drive_ui/static'


def node(script):
    result = subprocess.run(['node', '--input-type=module', '--eval', script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def function(source, name, following):
    return source[source.index('function ' + name):source.index(following, source.index('function ' + name))]


def test_progress_is_typed_ready_is_not_complete_and_close_failure_wins():
    result = node(f"""
      import {{workspaceProgressModel,watchOutputModel}} from {json.dumps((STATIC/'delivery-presenter.js').as_uri())};
      const states=['created','starting','resuming','queued','running','completed','failed','cancelled','interrupted','paused','idle','stopped','ready','unknown','constructor'];
      const all=states.map(runState=>workspaceProgressModel({{runState}}));
      const ready=workspaceProgressModel({{workerState:'ready'}});
      const closing=workspaceProgressModel({{runState:'completed',workerState:'termination_failed'}});
      const malicious=watchOutputModel({{latest_run:{{state:'queued',instruction:'Operator steer instruction'}},latest_output:'completed and successful',worker:{{state:'running'}}}},'debug');
      const completed=watchOutputModel({{latest_run:{{state:'completed'}},latest_output:'<script>plain result</script>',worker:{{state:'ready'}}}},'debug');
      console.log(JSON.stringify({{all,ready,closing,malicious,completed}}));
    """)
    assert [x['label'] for x in result['all']] == ['Starting','Starting','Starting','Queued','Working','Complete','Needs attention','Cancelled','Interrupted','Paused','Idle','Stopped','Ready','Status unavailable','Status unavailable']
    assert result['ready']['label'] == 'Ready'
    assert result['closing']['label'] == 'Needs attention'
    assert result['malicious']['label'] == 'Queued'
    assert result['malicious']['result'] == ''
    assert 'completed and successful' in result['malicious']['technical']
    assert result['completed']['result'] == '<script>plain result</script>'
    assert result['completed']['technical'] == 'debug'


def test_account_summary_uses_catalog_labels_status_fallback_and_copy_authority():
    result = node(f"""
      import {{workerAccountSummary as summary}} from {json.dumps((STATIC/'launch-policy.js').as_uri())};
      const data={{new_workspace_options:[{{profile:'provider-new',label:'Configured assistant'}}],bootstrap_sections:{{provider_accounts:'ready'}},
        provider_accounts:[{{account_id:'a',label:'Selected account',status:'ready'}},{{account_id:'b',label:'Expired account',status:'action_required'}}],
        existing_workspaces:[{{worker_id:'w',profile:'provider-new',provider_readiness:{{readiness:'ready',label:'Saved account',account_id:'a'}}}}]}};
      const base={{workspaceValue:'new:provider-new',policy:'personal_required',accountId:'a',data}};
      console.log(JSON.stringify([
        summary(base),summary({{...base,accountId:'b'}}),summary({{...base,policy:'personal_preferred'}}),
        summary({{...base,policy:'legacy'}}),summary({{...base,accountId:''}}),
        summary({{...base,workspaceValue:'duplicate:w'}}),summary({{...base,workspaceValue:'open:missing'}}),
        summary({{...base,data:{{...data,bootstrap_sections:{{provider_accounts:'unavailable'}}}}}})
      ]));
    """)
    assert result == ['Configured assistant · Selected account','Configured assistant · Expired account · Needs attention',
      'Configured assistant · Selected account · Deployment fallback allowed','Configured assistant · Deployment-managed account selected',
      'Configured assistant · Personal account required','Configured assistant · Saved account · Reapproval required after copy',
      'Worker · Account status unavailable','Configured assistant · Account status unavailable']


def test_watch_renderer_clears_stale_result_and_preserves_action_failure_during_poll():
    source=(STATIC/'watch.js').read_text()
    rendered=function(source,'renderOutputContent(', '\nasync function postAction')
    result=node(f"""
      import {{watchOutputModel}} from {json.dumps((STATIC/'delivery-presenter.js').as_uri())};
      const node=()=>({{textContent:'stale',hidden:false,open:true,setAttribute:()=>{{}}}});
      const statusLabel=node(),resultPanelTitle=node(),latestOutputInline=node(),latestOutputHuman=node(),latestOutputFull=node(),latestOutputTechnical=node(),resultTechnical=node(),resultToggle=node(),resultToggleAction=node(),resultPanel=node();
      let currentSummary='',currentFullOutput='',currentResultText='',currentRunState='',actionFailure=null,opened=false;
      const openResultPanel=()=>{{opened=true;}}; const closeResultPanel=()=>{{}};
      const syncResultActions=()=>{{}}; const syncArtifactList=()=>{{}};
      const summarizeOutput=data=>{{currentRunState=data?.latest_run?.state || '';return watchOutputModel(data,'trace');}};
      {rendered}
      renderOutput({{latest_run:{{state:'completed'}},latest_output:'useful result'}});
      const completed={{text:latestOutputFull.textContent,hidden:latestOutputFull.hidden}};
      showActionFailure(new Error('synthetic network failure'));
      renderOutput({{latest_run:{{state:'running'}}}});
      const failure={{summary:latestOutputHuman.textContent,result:latestOutputFull.textContent,hidden:latestOutputFull.hidden,technical:latestOutputTechnical.textContent,expanded:resultTechnical.open,opened}};
      actionFailure=null;renderOutput({{latest_run:{{state:'completed'}},latest_output:'new result'}});
      console.log(JSON.stringify({{completed,failure,recovered:latestOutputFull.textContent}}));
    """)
    assert result['completed']=={'text':'useful result','hidden':False}
    assert result['failure']['result']=='' and result['failure']['hidden']
    assert result['failure']['technical']=='synthetic network failure'
    assert result['failure']['opened'] and not result['failure']['expanded']
    assert result['recovered']=='new result'


def test_restore_preserves_novice_disclosure_default_preference_and_files_hooks():
    html=(STATIC/'index.html').read_text(); app=(STATIC/'app.js').read_text();watch=(STATIC/'watch.html').read_text()
    assert html.index('id="workspace-option"') < html.index('<summary>More settings</summary>')
    assert html.index('id="provider-account-selection"') < html.index('<summary>More settings</summary>')
    assert html.index('id="success_criteria"') < html.index('<summary>More settings</summary>')
    assert html.index('id="context"') < html.index('<summary>More settings</summary>')
    assert 'id="connect-worker-account"' in html
    assert 'id="provider-account-policy"' in html
    assert app.count('data-effort-profile') >= 1
    assert 'id="default-worker-profile"' in html and 'default_worker_profile: defaultWorker?.value' in app
    assert '<option value="named,ephemeral,legacy" selected>' in html
    assert "workspaceKindFilter?.value || 'named,ephemeral,legacy'" in app
    assert "providerAccount.dataset.explicitAccountId = providerAccount.value" in app
    assert app.count('syncAccountSummary();') >= 5
    assert 'generation !== bootstrapGeneration' in app
    assert 'launchDraftState.bindOwner(bootstrap.draft_owner_scope)' in app
    assert 'id="watch-file-move-dialog"' in watch and 'id="watch-files-panel"' in watch
    assert '<details id="result-technical" class="result-technical" hidden>' in watch


def test_first_paint_and_floating_surfaces_do_not_depend_on_transparency():
    css=(STATIC/'styles.css').read_text()
    composer=css.split('.composer-frame {',1)[1].split('}',1)[0]
    assert 'backdrop-filter' not in composer and 'animation:' not in composer
    assert '--overlay-surface: #080b10;' in css
    for selector in ['.watch-files-panel {','.file-move-dialog {','.file-row-menu-items, .file-row-menu > button {','.result-panel {','.more-menu {']:
        assert 'background: var(--overlay-surface)' in css.split(selector,1)[1].split('}',1)[0]
    assert '.file-row-menu > button { background: var(--overlay-surface); }' in css


def test_failed_instruction_retains_draft_and_success_only_reports_acceptance():
    source=(STATIC/'watch.js').read_text()
    submit='async '+function(source,'submitFooterInstruction(', '\nfunction setOverlay')
    result=node(f"""
      let steerInput={{value:'Keep this instruction'}},failure=null,output=null,actionFailure=null;
      let currentRunState='running'; const STEERABLE_RUN_STATES=new Set(['queued','running','settling']);
      const workspaceApiBase='/synthetic'; const withAuth=x=>x; const csrfHeaders=x=>x;
      const autoResizeSteerInput=()=>{{}}; const closeResultPanel=()=>{{}};
      const renderOutputContent=x=>{{output=x;}}; const showActionFailure=x=>{{failure=x.message;}};
      let fetch=async()=>({{ok:false,text:async()=>'Synthetic rejected action'}});
      {submit}
      await submitFooterInstruction('steer');
      const rejected={{draft:steerInput.value,failure,output}};
      fetch=async()=>({{ok:true}}); await submitFooterInstruction('steer');
      console.log(JSON.stringify({{rejected,accepted:{{draft:steerInput.value,output}}}}));
    """)
    assert result['rejected']=={'draft':'Keep this instruction','failure':'Synthetic rejected action','output':None}
    assert result['accepted']['draft']==''
    assert result['accepted']['output']['label']=='Guidance accepted'
    assert 'immediately' not in result['accepted']['output']['summary']


def test_account_selection_and_policy_events_refresh_the_actual_summary():
    source=(STATIC/'app.js').read_text()
    summary=source[source.index('  const syncAccountSummary ='):source.index('\n  };',source.index('  const syncAccountSummary ='))+5]
    events=source[source.index("  select.addEventListener('change', () => {",source.index('async function main')):source.index("  workspaceType?.addEventListener('change'",source.index('async function main'))]
    result=node(f"""
      import {{workerAccountSummary}} from {json.dumps((STATIC/'launch-policy.js').as_uri())};
      const control=value=>({{value,dataset:{{}},selectedOptions:[{{textContent:'Selected account'}}],handlers:{{}},addEventListener(type,handler){{this.handlers[type]=handler;}}}});
      const select=control('new:test'),providerAccount=control('a'),providerAccountPolicy=control('personal_required');
      const workerAccountSummaryNode={{textContent:''}},providerAccountHelp={{}},connectWorkerAccount=null,button={{}},help={{}};
      const workspaceMode=control('isolated'),workspaceFilePlacementField={{hidden:true}},workspaceFilePlacement=control('common'),workspaceModeHelp={{textContent:''}},workspaceType=control('sandboxed');
      const bootstrap={{new_workspace_options:[{{profile:'test',label:'Assistant'}}],provider_accounts:[{{account_id:'a',label:'First',status:'ready'}},{{account_id:'b',label:'Second',status:'ready'}}]}};
      const syncWorkspaceUI=()=>{{}}; const renderLaunchProviderAccounts=()=>{{}}; const syncWorkspaceModeUI=()=>{{}}; const syncLaunchChoiceUI=()=>{{}};
      {summary}
      {events}
      providerAccount.handlers.change(); const first=workerAccountSummaryNode.textContent;
      providerAccount.value='b';providerAccount.handlers.change();const second=workerAccountSummaryNode.textContent;
      providerAccountPolicy.value='legacy';providerAccountPolicy.handlers.change();const policy=workerAccountSummaryNode.textContent;
      select.value='open:missing';select.handlers.change();const saved=workerAccountSummaryNode.textContent;
      console.log(JSON.stringify({{first,second,policy,saved}}));
    """)
    assert result=={'first':'Assistant · First','second':'Assistant · Second','policy':'Assistant · Deployment-managed account selected','saved':'Worker · Account status unavailable'}


def test_explicit_strict_account_survives_readiness_and_catalog_refresh():
    source = (STATIC / 'app.js').read_text()
    rendered = function(source, 'renderLaunchProviderAccounts(', '\nfunction renderDefaultWorkerOptions(')
    result = node(f"""
      import {{credentialPolicyTransition,preferredProviderAccountId}} from {json.dumps((STATIC/'launch-policy.js').as_uri())};
      const document={{createElement:()=>({{value:'',textContent:'',disabled:false,dataset:{{}}}})}};
      const account=()=>({{
        value:'',dataset:{{}},options:[],disabled:false,
        get selectedOptions(){{return this.options.filter(option=>option.value===this.value);}},
        replaceChildren(...options){{this.options=options;this.value=options[0]?.value||'';}}
      }});
      const policy=()=>({{value:'personal_required',dataset:{{}},disabled:false}});
      const a={{account_id:'a',provider:'codex',label:'A',status:'ready',is_default:true}};
      const b={{account_id:'b',provider:'codex',label:'B',status:'ready',is_default:false}};
      const ready={{provider_accounts:[a,b],profile_account_providers:{{'codex-cli':['codex']}},bootstrap_sections:{{provider_accounts:'ready'}}}};
      {rendered}
      const selected=account(), strict=policy(), help={{textContent:''}};
      renderLaunchProviderAccounts(selected,strict,help,ready,'new:codex-cli');
      selected.value='b';selected.dataset.explicitChoice='true';selected.dataset.explicitProfile='codex-cli';
      selected.dataset.explicitAccountId='b';selected.dataset.explicitAccountLabel='B';
      renderLaunchProviderAccounts(selected,strict,help,{{...ready,provider_accounts:[a,{{...b,status:'action_required'}}]}},'new:codex-cli');
      const actionRequired={{value:selected.value,state:selected.dataset.accountState,disabled:selected.selectedOptions[0].disabled,help:help.textContent,policy:strict.value}};
      renderLaunchProviderAccounts(selected,strict,help,{{...ready,provider_accounts:[],bootstrap_sections:{{provider_accounts:'unavailable'}}}},'new:codex-cli');
      const unavailable={{value:selected.value,state:selected.dataset.accountState,disabled:selected.disabled}};
      renderLaunchProviderAccounts(selected,strict,help,ready,'new:codex-cli');
      const recovered={{value:selected.value,state:selected.dataset.accountState}};
      renderLaunchProviderAccounts(selected,strict,help,{{...ready,profile_account_providers:{{'codex-cli':['codex'],'openclaw-general':[]}}}},'new:openclaw-general');
      const unsupported={{policy:strict.value,forced:strict.dataset.forcedLegacy}};
      renderLaunchProviderAccounts(selected,strict,help,{{...ready,provider_accounts:[],profile_account_providers:{{}},bootstrap_sections:{{provider_accounts:'unavailable'}}}},'new:codex-cli');
      const outageAfterSwitch={{value:selected.value,state:selected.dataset.accountState,disabled:selected.disabled,policy:strict.value}};
      const neutral=account(), neutralPolicy=policy(), neutralHelp={{textContent:''}};
      renderLaunchProviderAccounts(neutral,neutralPolicy,neutralHelp,{{...ready,provider_accounts:[{{...a,is_default:false}},b]}},'new:codex-cli');
      console.log(JSON.stringify({{actionRequired,unavailable,recovered,unsupported,outageAfterSwitch,neutral:{{value:neutral.value,label:neutral.selectedOptions[0].textContent,state:neutral.dataset.accountState,disabled:neutral.disabled,help:neutralHelp.textContent}}}}));
    """)
    assert result['actionRequired'] == {
        'value': 'b', 'state': 'needs_attention', 'disabled': True,
        'help': 'This account needs attention. Reconnect it or choose another ready account.',
        'policy': 'personal_required',
    }
    assert result['unavailable'] == {'value': 'b', 'state': 'catalog_unavailable', 'disabled': True}
    assert result['recovered'] == {'value': 'b', 'state': 'ready'}
    assert result['unsupported'] == {'policy': 'legacy', 'forced': 'true'}
    assert result['outageAfterSwitch'] == {'value': 'b', 'state': 'catalog_unavailable', 'disabled': True, 'policy': 'personal_required'}
    assert result['neutral'] == {
        'value': '', 'label': 'Choose an account', 'state': 'choose', 'disabled': False,
        'help': 'Choose one of your connected accounts.',
    }
