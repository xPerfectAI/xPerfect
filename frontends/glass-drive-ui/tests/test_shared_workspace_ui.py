from pathlib import Path
import json
import subprocess

import glass_drive_ui.server as server_module


STATIC = Path(server_module.STATIC_DIR)


def test_kickoff_exposes_shared_workspace_choice_and_preserves_isolated_default():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'id="workspace-mode" name="workspace_mode"' in index
    assert '<option value="isolated" selected>Separate workspace (default)</option>' in index
    assert '<option value="shared">Shared workspace</option>' in index
    assert 'id="workspace-file-placement" name="workspace_file_placement"' in index
    assert 'value="member_private">Private files per member</option>' in index
    assert 'workspace_mode: workspaceMode?.value || \'isolated\'' in app
    assert 'workspace_file_placement: workspaceFilePlacement?.value || \'common\'' in app
    server = Path(server_module.__file__).read_text(encoding="utf-8")
    assert "shared_new_workspace_required" in server


def test_workspace_members_ui_surfaces_readiness_and_native_selection_controls():
    members = (STATIC / "workspace-members.js").read_text(encoding="utf-8")
    create = (STATIC / "member-create.js").read_text(encoding="utf-8")

    assert "Shared workspace unavailable:" in members
    assert "providerAccounts" in members
    assert "provider_account_policy" in create
    assert "provider_account_id" in create
    assert "effort" in create
    assert "shared_workspace" in create


def test_kickoff_draft_selects_keep_valid_defaults_after_reload():
    draft = (STATIC / "launch-draft.js").read_text(encoding="utf-8")
    assert "const defaults = Object.fromEntries" in draft
    assert "const options = Array.from(field?.options || [])" in draft
    assert "field.value = options.some(option => String(option.value) === defaultValue)" in draft


def test_shared_choice_uses_supported_placement_and_explains_linux_requirement():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    start = app.index("function syncWorkspaceModeUI(")
    mode_function = app[start:app.index("\nfunction launchFailureMessage(", start)]
    failure_function = app[app.index("function launchFailureMessage(", start):app.index("\nasync function main()", start)]
    script = f"""
      const workspaceTypeHelp = {{textContent:''}};
      const document = {{getElementById: () => workspaceTypeHelp}};
      {mode_function}
      {failure_function}
      const mode = {{value:'shared',options:[{{value:'isolated'}},{{value:'shared',disabled:false}}]}};
      const sandbox = {{value:'sandboxed',disabled:false,dataset:{{description:'Managed compute.'}}}};
      const host = {{value:'host',disabled:false,dataset:{{description:'This computer.'}}}};
      const type = {{value:'host',options:[sandbox,host],get selectedOptions() {{ return [this.options.find(x => x.value === this.value)]; }} }};
      const placement = {{hidden:true}}, help = {{textContent:''}};
      syncWorkspaceModeUI(mode,placement,null,help,{{value:'new:codex-cli'}},type);
      const shared = {{mode:mode.value,type:type.value,hostDisabled:host.disabled,placementHidden:placement.hidden,help:help.textContent}};
      mode.value='shared';
      syncWorkspaceModeUI(mode,placement,null,help,{{value:'open:saved'}},type);
      const saved = {{mode:mode.value,sharedDisabled:mode.options[1].disabled}};
      console.log(JSON.stringify({{shared,saved,error:launchFailureMessage({{code:'shared_linux_runtime_required',message:'raw code'}})}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True, text=True, check=True,
    )
    actual = json.loads(result.stdout)
    assert actual["shared"]["mode"] == "shared"
    assert actual["shared"]["type"] == "sandboxed"
    assert actual["shared"]["hostDisabled"] is True
    assert actual["shared"]["placementHidden"] is False
    assert "Linux host" in actual["shared"]["help"]
    assert actual["saved"] == {"mode": "isolated", "sharedDisabled": True}
    assert "shared_linux_runtime_required" not in actual["error"]
    assert "Linux host" in actual["error"]
