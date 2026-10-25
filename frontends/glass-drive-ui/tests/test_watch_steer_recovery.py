"""Execute the real Watch submit handler: a control conflict retains the draft."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_steer_generation_conflict_retains_draft_until_accepted():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the shipped Watch handler")
    path = Path(__file__).parents[1] / "src/glass_drive_ui/static/watch.js"
    source = path.read_text()
    start = source.index("async function submitFooterInstruction(mode)")
    end = source.index("\nfunction setOverlay", start)
    handler = source[start:end]
    presenter = source[source.index("function showActionFailure(error)"):source.index("\nfunction renderOutput(data)")]
    script = """
const assert = require('node:assert/strict');
const steerInput = {value:'Keep this new instruction'};
const STEERABLE_RUN_STATES = new Set(['running']);
const currentRunState = 'running';
const workspaceApiBase = '/api/workspace/demo-worker';
const withAuth = x => x, csrfHeaders = x => x;
let accepted = false, actionFailure = null, resized = 0, presented = 0;
let sent = [], rendered = [];
const resultTechnical = {open:true};
const openResultPanel = () => {};
const fetch = async (url, options) => {
  sent.push({url, body:JSON.parse(options.body)});
  return {ok:accepted, text:async()=>JSON.stringify({detail:{
    code:'active_work_generation_changed',
    message:'Current work changed. Check its status and try again.',retryable:true}})};
};
const autoResizeSteerInput = () => resized++;
const closeResultPanel = () => {};
const renderOutputContent = output => {presented++; rendered.push(output);};
PRESENTER
HANDLER
(async()=>{
  await submitFooterInstruction('steer');
  assert.equal(steerInput.value,'Keep this new instruction');
  assert.equal(resized,0); assert.equal(presented,1);
  assert.equal(actionFailure.summary,'Current work changed. Check its status and try again.');
  assert.doesNotMatch(actionFailure.summary,/active_work_generation_changed|retryable|detail/);
  assert.match(actionFailure.technical,/active_work_generation_changed/);
  assert.equal(resultTechnical.open,false);
  assert.equal(sent[0].url,'/api/workspace/demo-worker/steer');
  accepted = true;
  await submitFooterInstruction('steer');
  assert.deepEqual(sent[1].body,sent[0].body);
  assert.equal(steerInput.value,''); assert.equal(presented,2);
  assert.equal(actionFailure,null);
  showActionFailure(new Error('network disconnected'));
  assert.match(actionFailure.summary,/Check the workspace status/);
  assert.equal(actionFailure.technical,'network disconnected');
  showActionFailure(new Error(JSON.stringify({detail:{message:'This view cannot control the workspace.',recovery:'Open your own workspace.'}})));
  assert.equal(actionFailure.summary,'This view cannot control the workspace. Open your own workspace.');
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("PRESENTER", presenter).replace("HANDLER", handler)
    result = subprocess.run([node, "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
