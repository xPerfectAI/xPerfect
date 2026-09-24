"""Deterministic contract checks for the Allowed AI settings surface."""

import json
from pathlib import Path
import subprocess


STATIC = Path(__file__).parents[1] / "src" / "glass_drive_ui" / "static"


def node(script):
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_policy_defaults_and_empty_selection_keep_distinct_meanings():
    result = node(
        f"""
        import {{
          defaultAllowedAiPolicy,
          normalizeAllowedAiPolicy,
          allowedAiSavePayload,
          allowedAiPolicyWarning,
          allowedAiRoute,
        }} from {json.dumps((STATIC / 'allowed-ai-policy.js').as_uri())};
        const project = defaultAllowedAiPolicy('project');
        const workspace = defaultAllowedAiPolicy('workspace');
        const empty = normalizeAllowedAiPolicy(
          {{version: 1, mode: 'selected', harnesses: []}}, 'project'
        );
        console.log(JSON.stringify({{
          project, workspace, empty,
          projectRoute: allowedAiRoute('project', 'project/example'),
          workspaceRoute: allowedAiRoute('workspace', 'workspace/example'),
          payload: allowedAiSavePayload('project/example', 7, empty),
          warnings: [
            allowedAiPolicyWarning(empty),
            allowedAiPolicyWarning({{version:1, mode:'selected', harnesses:[{{profile:'codex-cli', models:{{mode:'all',ids:[]}}, connections:{{mode:'selected',ids:[]}}}}]}}),
            allowedAiPolicyWarning({{version:1, mode:'selected', harnesses:[{{profile:'codex-cli', models:{{mode:'all',ids:[]}}, connections:{{mode:'all',ids:[]}}}}]}}),
          ],
        }}));
        """
    )
    assert result["project"] == {"version": 1, "mode": "all_authorized", "harnesses": []}
    assert result["workspace"] == {"version": 1, "mode": "inherit", "harnesses": []}
    assert result["empty"] == {"version": 1, "mode": "selected", "harnesses": []}
    assert result["projectRoute"] == "/api/projects/project%2Fexample/execution-policy"
    assert result["workspaceRoute"] == "/api/workspaces/workspace%2Fexample/execution-policy"
    assert result["payload"] == {
        "expected_revision": 7,
        "policy": {"version": 1, "mode": "selected", "harnesses": []},
    }
    assert result["warnings"] == [
        "No harnesses selected. New starts will be blocked until you choose an option.",
        "A selected harness has no model or connection options. New starts using it will be blocked.",
        "",
    ]


def test_policy_normalization_preserves_nested_selected_empty_and_rejects_invalid_shape():
    result = node(
        f"""
        import {{normalizeAllowedAiPolicy}} from {json.dumps((STATIC / 'allowed-ai-policy.js').as_uri())};
        const selected = normalizeAllowedAiPolicy({{
          version: 1,
          mode: 'selected',
          harnesses: [{{profile: 'codex-cli', models: {{mode: 'selected', ids: []}}, connections: {{mode: 'all', ids: []}}}}]
        }}, 'workspace');
        let invalid = '';
        try {{ normalizeAllowedAiPolicy({{version: 1, mode: 'inherit', harnesses: [{{profile: 'codex-cli'}}]}}, 'project'); }}
        catch (error) {{ invalid = error.message; }}
        console.log(JSON.stringify({{selected, invalid}}));
        """
    )
    assert result["selected"] == {
        "version": 1,
        "mode": "selected",
        "harnesses": [
            {
                "profile": "codex-cli",
                "models": {"mode": "selected", "ids": []},
                "connections": {"mode": "all", "ids": []},
            }
        ],
    }
    assert result["invalid"]


def test_options_are_typed_and_do_not_echo_secret_or_locator_fields():
    result = node(
        f"""
        import {{normalizeAllowedAiOptions}} from {json.dumps((STATIC / 'allowed-ai-policy.js').as_uri())};
        const value = normalizeAllowedAiOptions({{
          scope_id: 'project/example',
          harnesses: [{{profile: 'codex-cli', label: 'Codex', models: [
            {{id: 'codex:model-a', label: 'Model A', status: 'available'}},
            {{id: 'codex:model-b', label: 'Model B', status: 'unavailable', reason: 'Provider is offline'}}
          ], connections: [
            {{id: 'acct/sub', label: 'Team subscription', kind: 'subscription', status: 'available', secret: 'must-not-render'}},
            {{id: 'route/api', label: 'Configured route', kind: 'configured_route', status: 'busy', reason: 'Queued capacity'}}
          ]}}]
        }});
        console.log(JSON.stringify(value));
        """
    )
    assert result["scope_id"] == "project/example"
    assert result["harnesses"][0]["models"][1]["reason"] == "Provider is offline"
    assert result["harnesses"][0]["connections"][1]["status"] == "busy"
    serialized = json.dumps(result)
    assert "must-not-render" not in serialized
    assert "secret" not in serialized


def test_missing_saved_choice_has_distinct_safe_identity_and_remembers_connection_kind():
    result = node(
        f"""
        import {{missingAllowedAiOption}} from {json.dumps((STATIC / 'allowed-ai-policy.js').as_uri())};
        const remembered = new Map([['acct/sub-a', {{label:'Team subscription',kind:'subscription',secret:'must-not-render'}}]]);
        console.log(JSON.stringify({{
          models: [missingAllowedAiOption('provider:model-a','model'), missingAllowedAiOption('provider:model-b','model')],
          remembered: missingAllowedAiOption('acct/sub-a','connection',remembered),
          unknown: missingAllowedAiOption('acct/key-b','connection',remembered),
        }}));
        """
    )
    assert [item["label"] for item in result["models"]] == [
        "Saved model · provider:model-a", "Saved model · provider:model-b"
    ]
    assert result["remembered"]["label"] == "Team subscription"
    assert result["remembered"]["kind"] == "subscription"
    assert result["unknown"]["label"] == "Saved connection · acct/key-b"
    assert result["unknown"]["kind"] == "other"
    assert "secret" not in json.dumps(result)


def test_selecting_exact_choices_starts_with_every_current_harness_and_dynamic_all():
    result = node(
        f"""
        import {{selectedAllowedAiPolicy}} from {json.dumps((STATIC / 'allowed-ai-policy.js').as_uri())};
        const options = {{scope_id:'project/example', harnesses:[
          {{profile:'codex-cli',label:'Codex',models:[{{id:'model-a',label:'A',status:'available'}}],connections:[{{id:'sub-a',label:'Subscription',status:'available',kind:'subscription'}}]}},
          {{profile:'claude-code',label:'Claude',models:[{{id:'model-b',label:'B',status:'unavailable'}}],connections:[{{id:'key-b',label:'API key',status:'busy',kind:'api_key'}}]}}
        ]}};
        const fresh = selectedAllowedAiPolicy(options);
        const explicit = {{version:1,mode:'selected',harnesses:[{{profile:'codex-cli',models:{{mode:'selected',ids:['model-a']}},connections:{{mode:'selected',ids:['sub-a']}}}}]}};
        const retained = selectedAllowedAiPolicy(options, explicit);
        console.log(JSON.stringify({{fresh,retained}}));
        """
    )
    assert [item["profile"] for item in result["fresh"]["harnesses"]] == ["codex-cli", "claude-code"]
    assert all(item["models"] == {"mode": "all", "ids": []} and item["connections"] == {"mode": "all", "ids": []} for item in result["fresh"]["harnesses"])
    assert result["retained"] == {
        "version": 1, "mode": "selected", "harnesses": [{
            "profile": "codex-cli",
            "models": {"mode": "selected", "ids": ["model-a"]},
            "connections": {"mode": "selected", "ids": ["sub-a"]},
        }],
    }


def test_exact_choice_defaults_include_authorized_and_busy_only():
    result = node(
        f"""
        import {{selectableExactIds}} from {json.dumps((STATIC / 'allowed-ai-policy.js').as_uri())};
        console.log(JSON.stringify(selectableExactIds([
          {{id:'ready',status:'available'}},
          {{id:'queue',status:'busy'}},
          {{id:'offline',status:'unavailable'}},
          {{id:'blocked',status:'denied'}},
          {{id:'unknown',status:'unknown'}}
        ])));
        """
    )
    assert result == ["ready", "queue"]


def test_workspace_catalog_marks_choices_outside_project_ceiling_without_remapping_ids():
    result = node(
        f"""
        import {{restrictAllowedAiOptions,selectedAllowedAiPolicy}} from {json.dumps((STATIC / 'allowed-ai-policy.js').as_uri())};
        const options = {{scope_id:'workspace/example',harnesses:[
          {{profile:'codex-cli',label:'Codex',models:[{{id:'exact-a',label:'A',status:'available'}},{{id:'exact-b',label:'B',status:'available'}}],connections:[{{id:'sub-a',label:'Subscription',status:'available',kind:'subscription'}},{{id:'key-b',label:'API key',status:'available',kind:'api_key'}}]}},
          {{profile:'claude-code',label:'Claude',models:[{{id:'claude-a',label:'C',status:'available'}}],connections:[]}}
        ]}};
        const project = {{version:1,mode:'selected',harnesses:[{{profile:'codex-cli',models:{{mode:'selected',ids:['exact-a']}},connections:{{mode:'selected',ids:['sub-a']}}}}]}};
        const restricted = restrictAllowedAiOptions(options,project);
        const saved = {{version:1,mode:'selected',harnesses:[{{profile:'codex-cli',models:{{mode:'selected',ids:['exact-b']}},connections:{{mode:'selected',ids:['key-b']}}}}]}};
        const retained = restrictAllowedAiOptions(options,project,saved);
        console.log(JSON.stringify({{restricted,retained,initial:selectedAllowedAiPolicy(restricted)}}));
        """
    )
    [codex] = result["restricted"]["harnesses"]
    assert [item["id"] for item in codex["models"]] == ["exact-a"]
    assert [item["id"] for item in codex["connections"]] == ["sub-a"]
    assert result["retained"]["harnesses"][0]["models"][1]["status"] == "denied"
    assert result["retained"]["harnesses"][0]["connections"][1]["status"] == "denied"
    assert [item["profile"] for item in result["initial"]["harnesses"]] == ["codex-cli"]


def test_workspace_draft_outside_project_limit_is_identified_without_dropping_it():
    result = node(
        f"""
        import {{restrictAllowedAiOptions,selectedOutsideProject}} from {json.dumps((STATIC / 'allowed-ai-policy.js').as_uri())};
        const catalog = {{scope_id:'w',harnesses:[
          {{profile:'codex-cli',label:'Codex',models:[{{id:'m1',label:'A',status:'available'}},{{id:'m2',label:'B',status:'available'}}],connections:[]}},
          {{profile:'claude-code',label:'Claude',models:[],connections:[]}}
        ]}};
        const project = {{version:1,mode:'selected',harnesses:[{{profile:'codex-cli',models:{{mode:'selected',ids:['m1']}},connections:{{mode:'all',ids:[]}}}}]}};
        const draft = {{version:1,mode:'selected',harnesses:[
          {{profile:'codex-cli',models:{{mode:'selected',ids:['m2']}},connections:{{mode:'all',ids:[]}}}},
          {{profile:'claude-code',models:{{mode:'all',ids:[]}},connections:{{mode:'all',ids:[]}}}}
        ]}};
        const narrowed = restrictAllowedAiOptions(catalog,project,draft);
        const valid = {{...draft,harnesses:[{{...draft.harnesses[0],models:{{mode:'selected',ids:['m1']}}}}]}};
        console.log(JSON.stringify({{
          denied:selectedOutsideProject(narrowed,draft),
          allowed:selectedOutsideProject(narrowed,valid),
          retained:narrowed.harnesses.map(h => ({{profile:h.profile,blocked:!!h.blocked,models:h.models}})),
        }}));
        """
    )
    assert result["denied"] is True
    assert result["allowed"] is False
    assert [item["profile"] for item in result["retained"]] == ["codex-cli", "claude-code"]
    assert result["retained"][0]["models"][1]["status"] == "denied"
    assert result["retained"][1]["blocked"] is True


def test_workspace_settings_mounts_policy_surface_without_changing_launch_code():
    source = (STATIC / "workspace-members.js").read_text(encoding="utf-8")
    policy = (STATIC / "allowed-ai-policy.js").read_text(encoding="utf-8")
    css = (STATIC / "allowed-ai.css").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "mountAllowedAiPolicy" in source
    assert "member.project_id" in source or "initialMember.project_id" in source
    assert ".allowed-ai-policy" in css
    assert "execution-policy" in policy
    for phrase in (
        "Use project settings",
        "All authorized options",
        "Choose AI options",
        "No harnesses selected",
        "Busy choices can queue",
    ):
        assert phrase in policy
    assert "openWorkspaceMembers" in app
