"""Cold startup shares current identity data without treating supersession as a failure."""
import shutil
import subprocess
from pathlib import Path


def test_startup_waits_for_current_bootstrap_and_keeps_real_failures_visible():
    source = (Path(__file__).parents[1] / "src/glass_drive_ui/static/app.js").read_text()
    refresh = source[source.index("  const currentBootstrapResult = () => {"):source.index("\n  function stopHivePolling()")]
    owner_start = source.index("function bindWorkspaceInteractionOwner(")
    owner = source[owner_start:source.index("\n}\n", owner_start) + 3]
    marker = source.index("    const initialBootstrap = await refreshBootstrap();")
    startup = source[source.rfind("  try {", 0, marker):source.index("\n  for (const toggle", marker)]
    nav = source[source.index("  function setActiveView(view,"):source.index("\n  window.addEventListener('glasshive:control-plane-updated'")]
    nav_start = source.index("  tabs.forEach((tab) => {", source.index("  function setActiveView(view,"))
    nav_click = source[nav_start:source.index("\n\n", nav_start)]
    assert nav_start < marker, "navigation must respond before startup finishes loading"
    node = shutil.which("node")
    assert node, "Node is required for the startup race check"
    harness = r"""
const assert = require('node:assert/strict');
function harness() {
  let bootstrap = null, bootstrapGeneration = 0, bootstrapFlight = null, identityTransition = false, workerChoiceGeneration = 0;
  let csrfToken = '';
  let workspaceInteractionOwnerScope='',workspaceSteerInFlight=new Map();
  const loads = [], rendered = [], admin = [];
  const loadBootstrap = () => new Promise((resolve, reject) => loads.push({resolve, reject}));
  const select = {value:'new:codex-cli', disabled:false}, button = {disabled:false};
  const scheduleButton = {disabled:false}, providerAccount = {disabled:false}, providerAccountPolicy = {disabled:false};
  const status = {textContent:''}, help = {}, workspaceType = {disabled:false}, workspaceTypeHelp = {};
  const launchSurface = {value:''}, providerAccountHelp = {}, workspaceMode = {}, workspaceModeHelp = {};
  const workspaceFilePlacementField = {}, workspaceFilePlacement = {};
  const defaultWorker = {}, codexEffort = {}, claudeEffort = {}, openclawEffort = {};
  const workspaceKindFilter = {value:'named'}, workspaceSearch = {value:''}, workspaceTagFilter = {value:''};
  const workspaceCatalogStatus = {}, templateStartStatus = {}, catalogState = {generation:0, loading:false};
  const form = {classList:{add(name) { this.added = name; }}};
  const launchDraftState = {bindOwner() {}}, fileDraft = {setOwnerScope() {}};
  const document = {getElementById: () => null};
  const fetchCatalogPage = async () => ({items:[]});
  const decorateCatalogWorkspace = value => value;
  const renderCurrentUser = identity => rendered.push(identity.id);
  const onboarding = {render() {}};
  const renderWorkspaceOptions = () => {}, renderLaunchProviderAccounts = () => {};
  const syncAccountSummary = () => {}, syncPreferenceControls = () => {}, renderWorkspaceHive = () => {};
  const workspaceViewData = () => ({}), hivePrefs = () => ({}), refreshWorkspaceCatalog = async () => {};
  const renderActivity = () => {}, renderWorkspaceTypeOptions = () => {}, syncWorkspaceModeUI = () => {};
  const syncWorkspaceUI = (_select, data) => { assert.ok(data.identity); button.disabled = false; };
  const getJson = () => {}, patchJson = () => {};
  const initializeStorageAdmin = ({identity}) => { admin.push(identity.id); };
  const frame = {dataset:{}};
  let activeView = 'schedules';
  const tabs = ['project', 'schedules'].map(name => ({dataset:{viewTab:name},
    setAttribute() {}, removeAttribute() {}, addEventListener(_type, handler) { this.click = handler; }}));
  const viewRegistry = new Map(tabs.map(tab => [tab.dataset.viewTab, {hidden:false}]));
  const window = {location:{pathname:'/', search:'', hash:'#schedules'}, history:{replaceState() {}}};
  const stopHivePolling = () => {}, startHivePolling = () => {}, refreshControlPlane = async () => {};
OWNER_SOURCE
REFRESH_SOURCE
NAV_SOURCE
NAV_CLICK_SOURCE
  setActiveView('schedules', {updateHash:false});
  async function startup() {
STARTUP_SOURCE
  }
  return {refreshBootstrap, startup, loads, rendered, admin, button, status,
    invalidate() { identityTransition = true; bootstrapGeneration += 1; },
    resume() { identityTransition = false; },
    current() { return bootstrap; },
    clickProject() { tabs[0].click({button:0, preventDefault() {}}); },
    currentView() { return frame.dataset.activeView; }};
}
const data = id => ({identity:{id}, draft_owner_scope:id, csrf_token:'synthetic',
  bootstrap_sections:{workspace_catalog:'ready', workspace_templates:'ready'}, existing_workspaces:[]});
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  for (const completionOrder of ['older-first', 'newer-first']) {
    const cold = harness();
    const startup = cold.startup(), afterSave = cold.refreshBootstrap();
    cold.clickProject();
    assert.equal(cold.currentView(), 'project', 'an early click updates the visible view immediately');
    assert.equal(cold.loads.length, 2, 'a post-save refresh must fetch new account state');
    if (completionOrder === 'older-first') {
      cold.loads[0].resolve({...data('owner-a'), revision:'before-save'});
      await flush();
      assert.deepEqual(cold.admin, [], 'startup waits for the newer account snapshot');
      cold.loads[1].resolve({...data('owner-a'), revision:'after-save'});
    } else {
      cold.loads[1].resolve({...data('owner-a'), revision:'after-save'});
      await afterSave;
      cold.loads[0].resolve({...data('owner-a'), revision:'before-save'});
    }
    await Promise.all([startup, afterSave]);
    assert.equal(cold.current().revision, 'after-save');
    assert.equal(cold.currentView(), 'project', 'loading cannot restore the earlier Schedules view');
    assert.deepEqual(cold.admin, ['owner-a']);
    assert.equal(cold.button.disabled, false);
    assert.equal(cold.status.textContent, '');
  }

  for (const oldOutcome of ['resolve', 'reject']) {
    const changing = harness();
    let settled = false;
    const first = changing.startup().then(() => { settled = true; });
    changing.invalidate();
    assert.equal(await changing.refreshBootstrap(), null, 'a transition cannot reuse prior-owner data');
    changing.resume();
    const current = changing.refreshBootstrap();
    assert.equal(changing.loads.length, 2);
    if (oldOutcome === 'resolve') changing.loads[0].resolve(data('old-owner'));
    else changing.loads[0].reject(new Error('old owner request failed'));
    await flush();
    assert.equal(settled, false, 'superseded startup waits for the new identity');
    changing.loads[1].resolve(data('current-owner'));
    await Promise.all([first, current]);
    assert.deepEqual(changing.rendered, ['current-owner']);
    assert.deepEqual(changing.admin, ['current-owner']);
    assert.equal(changing.status.textContent, '');
  }

  const leaving = harness(), cancelled = leaving.startup();
  leaving.invalidate();
  leaving.loads[0].resolve(data('old-owner'));
  await cancelled;
  assert.equal(leaving.current(), null);
  assert.deepEqual(leaving.admin, []);
  assert.deepEqual(leaving.rendered, []);
  assert.equal(leaving.status.textContent, '');

  const unavailable = harness(), failed = unavailable.startup();
  unavailable.loads[0].reject(new Error('Account details are unavailable. Refresh to retry.'));
  await failed;
  assert.equal(unavailable.button.disabled, true);
  assert.equal(unavailable.status.textContent, 'Account details are unavailable. Refresh to retry.');
  assert.deepEqual(unavailable.admin, []);
})().catch(error => { console.error(error); process.exit(1); });
"""
    harness = (harness.replace("OWNER_SOURCE", owner).replace("REFRESH_SOURCE", refresh).replace("STARTUP_SOURCE", startup)
               .replace("NAV_SOURCE", nav).replace("NAV_CLICK_SOURCE", nav_click))
    result = subprocess.run([node, "-e", harness], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
