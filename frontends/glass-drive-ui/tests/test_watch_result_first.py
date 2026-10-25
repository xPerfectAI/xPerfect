"""Actual Watch presentation functions, with typed run/surface and native-input cases."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / "src/glass_drive_ui/static"


def function(source, name):
    match = re.search(rf"^function {name}\(.*?^}}\n", source, re.M | re.S)
    assert match, name
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_real_watch_result_presentation_follows_runs_and_keeps_takeover_available():
    source = (STATIC / "watch.js").read_text()
    functions = "\n".join(function(source, name) for name in (
        "terminalRunPhase", "ownerResponseNeeded", "closeResultPanel", "openResultPanel",
        "syncTerminalResultPresentation"))
    script = f"""
import {{watchOutputModel}} from {json.dumps((STATIC/'delivery-presenter.js').as_uri())};
let focused=null;
const element=()=>({{hidden:true,open:false,dataset:{{}},textContent:'',setAttribute(name,value){{this[name]=value;}},focus(){{focused=this;}}}});
const watchApp=element(),terminalLog=element(),terminalLogSummary=element(),resultClose=element(),
 resultPanel=element(),resultToggle=element(),resultToggleAction=element(),latestOutputFull=element();
let currentFullOutput='',activeSurface='terminal',currentDesktopAvailable=false,
 settledTerminalRun='',terminalResultPreferred=false,actionFailure=null;
{functions}
const view=()=>({{preferred:terminalResultPreferred,resultFirst:watchApp.dataset.resultFirst,
 panelHidden:resultPanel.hidden,logOpen:terminalLog.open,summaryHidden:terminalLogSummary.hidden,
 closeHidden:resultClose.hidden}});
const data=(id,state,extra={{}})=>({{latest_run:{{run_id:id,state,active_attempt_id:'attempt-a'}},
 worker:{{state:state==='completed'?'ready':state}},latest_output:state==='completed'?'Here is the useful answer.':'',...extra}});
const step=(value)=>{{const output=watchOutputModel(value,'native trace');
 currentFullOutput=[output.summary,output.result,output.technical].filter(Boolean).join('\\n');
 latestOutputFull.hidden=!output.result;syncTerminalResultPresentation(value);return view();}};
let openWrites=0, logOpen=terminalLog.open;
Object.defineProperty(terminalLog,'open',{{get:()=>logOpen,set:value=>{{openWrites++;logOpen=value;}}}});
const running=step(data('run-a','running'));
const writesAfterOpen=openWrites;
step(data('run-a','running'));step(data('run-a','running'));
if(openWrites!==writesAfterOpen) throw new Error('Unchanged running refresh rewrote the iframe ancestor open attribute');
const completedText=step(data('run-a','completed'));
terminalLog.open=true;
const logChoiceSurvives=step(data('run-a','completed'));
closeResultPanel();
const closedByUser=step(data('run-a','completed'));
openResultPanel();closeResultPanel({{restoreFocus:true}});
if(focused!==resultToggle)throw new Error('Closing a result did not restore its trigger focus');
const newRun=step(data('run-b','queued'));
const failure=step(data('run-b','failed'));
const pending=step(data('run-b','failed',{{native_control:{{read_only:false,pending_requests:[{{kind:'permission'}}]}}}}));
const completedFile=step(data('run-c','completed',{{deliverable:{{kind:'file',workspace_path:'answer.txt',open_url:'/answer'}}}}));
currentDesktopAvailable=true;
const nativeDesktop=step(data('run-c','completed'));
currentDesktopAvailable=false;activeSurface='desktop';
const chosenDesktop=step(data('run-c','completed'));
activeSurface='terminal';
const noRun=step(data('','completed'));
const doneAfterRetry=step({{...data('run-d','completed'),latest_run:{{run_id:'run-d',state:'completed',active_attempt_id:'attempt-b'}}}});
console.log(JSON.stringify({{running,completedText,logChoiceSurvives,closedByUser,newRun,failure,pending,
 completedFile,nativeDesktop,chosenDesktop,noRun,doneAfterRetry}}));
"""
    result = json.loads(subprocess.run(["node", "--input-type=module", "--eval", script],
                                      check=True, text=True, capture_output=True).stdout)
    for name in ("completedText", "failure", "completedFile", "doneAfterRetry"):
        assert result[name] == dict(preferred=True, resultFirst="true", panelHidden=False,
                                    logOpen=False, summaryHidden=False, closeHidden=True), name
    assert result["running"]["logOpen"] and result["running"]["summaryHidden"]
    assert result["running"]["panelHidden"]
    assert result["logChoiceSurvives"]["logOpen"] and not result["logChoiceSurvives"]["panelHidden"]
    assert result["closedByUser"]["logOpen"] and result["closedByUser"]["panelHidden"]
    assert not result["closedByUser"]["preferred"]
    for name in ("newRun", "pending", "nativeDesktop", "chosenDesktop", "noRun"):
        assert not result[name]["preferred"] and result[name]["logOpen"] and result[name]["summaryHidden"], name
    # Pending native input remains visible; it is not dismissed as a completed answer.
    assert not result["pending"]["panelHidden"]


def test_watch_uses_native_collapsible_log_and_same_human_result_renderer():
    html = (STATIC / "watch.html").read_text()
    source = (STATIC / "watch.js").read_text()
    assert '<details id="terminal-log" class="watch-terminal-log" open>' in html
    assert '<summary id="terminal-log-summary" hidden>Technical log</summary>' in html
    assert html.index('id="terminal-log"') < html.index('class="watch-stage"') < html.index('id="desktop-frame"')
    refresh = source[source.index("async function refresh() {"):source.index("for (const button of document.querySelectorAll('[data-action]')") ]
    assert refresh.index("syncTerminalResultPresentation(data);") < refresh.index("setSurface(activeSurface, { force: false });")
    assert "!terminalResultPreferred && !resultPanel.hidden" in source
    assert "else if (!resultPanel.hidden) closeResultPanel({ restoreFocus: true });" in source
    assert "resultMarkdown.render(output.result, { resultArtifacts: currentResultArtifacts })" in source
    assert "resultMarkdown.render(text, { resultArtifacts: currentResultArtifacts })" in source
    assert "currentResultArtifacts = data.artifacts?.items || [];" in source
    assert "frame.src = url;" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_failed_watch_and_workspace_card_share_safe_typed_reason_not_raw_json():
    app = (STATIC / "app.js").read_text()
    summarize = function(app, "summarizeLive")
    script = f"""
import assert from 'node:assert/strict';
import {{workspaceDeliveryModel,watchOutputModel}} from {json.dumps((STATIC/'delivery-presenter.js').as_uri())};
{summarize}
const raw=JSON.stringify({{type:'turn.failed',error:{{message:'private provider diagnostic'}}}});
const message='The selected model is not available for this account. Check the account and model before trying again.';
const failed={{latest_run:{{state:'failed',failure_structured:1,failure_user_message:message}},latest_output:raw,
 worker:{{state:'ready'}},deliverable:{{open_url:'/old'}}}};
assert.equal(workspaceDeliveryModel(failed).summary,message);
assert.equal(workspaceDeliveryModel(failed).primary,null);
assert.equal(watchOutputModel(failed).summary,message);
assert.equal(watchOutputModel(failed).result,'');
assert(watchOutputModel(failed).technical.includes(raw));
assert.equal(summarizeLive(failed),message);
const old={{...failed,latest_run:{{state:'failed',failure_structured:0,failure_user_message:'untrusted old text'}}}};
assert.equal(summarizeLive(old),'The run did not complete. Review the details before trying again.');
assert(!workspaceDeliveryModel(old).summary.includes('turn.failed'));
assert(!watchOutputModel(old).summary.includes('turn.failed'));
for (const [state,expected] of [['queued','Queued follow-up is waiting for this workspace.'],['running','Workspace is running now.'],['completed',`Completed · ${{raw}}`]]) {{
 assert.equal(summarizeLive({{...failed,deliverable:null,latest_run:{{state}}}}),expected);
}}
assert.equal(summarizeLive({{...failed,native_control:{{read_only:false,pending_requests:[{{kind:'permission'}}]}}}}),'The worker is waiting for your response.');
assert.equal(summarizeLive({{worker:{{state:'ready'}},latest_output:raw}}),'Workspace is ready for the next instruction.');
console.log('true');
"""
    subprocess.run(["node", "--input-type=module", "--eval", script], check=True, text=True, capture_output=True)
