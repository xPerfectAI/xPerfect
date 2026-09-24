import json
import pytest
from workers_projects_runtime.grok_projection import mcp_servers_for_bundle
from workers_projects_runtime.native_team import project_native_events
from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from workers_projects_runtime.grok_artifact import docker_install_instruction, GROK_LINUX_SHA256
from workers_projects_runtime.bootstrap import _write_claude_project_files


def test_authorized_broker_header_projects_without_new_authority():
    bundle={'claude_project_mcp':{'mcpServers':{'authorized-broker':{'type':'http','url':'https://example.invalid/mcp','headers':{'Authorization':'Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}'}}}}}
    servers=mcp_servers_for_bundle(bundle,{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'synthetic-run-grant'})
    assert servers==[{'type':'http','name':'authorized-broker','url':'https://example.invalid/mcp','headers':[{'name':'Authorization','value':'Bearer synthetic-run-grant'}]}]
    assert bundle['claude_project_mcp']['mcpServers']['authorized-broker']['headers']['Authorization'].endswith('}')
    with pytest.raises(ValueError,match='unavailable'):
        mcp_servers_for_bundle(bundle,{})
    assert mcp_servers_for_bundle({}, {'UNRELATED_TOKEN':'synthetic'})==[]


def test_selected_empty_removes_explicit_grok_tools_but_keeps_runtime_tools():
    bundle = {
        "grok_mcp_servers": [
            {"name": "private-tool", "type": "http", "url": "https://example.test/private"}
        ],
        "claude_project_mcp": {
            "mcpServers": {
                "private-tool": {"type": "http", "url": "https://example.test/private"},
                "xperfect-context": {"type": "http", "url": "https://example.test/context"},
                "xperfect-peers": {"type": "http", "url": "https://example.test/peers"},
            }
        },
        "_configured_removed_mcp_servers": ["private-tool"],
    }
    assert [item["name"] for item in mcp_servers_for_bundle(bundle, {})] == [
        "xperfect-context",
        "xperfect-peers",
    ]


def test_grok_projection_merges_explicit_and_runtime_mcp_servers():
    bundle = {
        "grok_mcp_servers": [
            {"name": "private-tool", "type": "http", "url": "https://example.test/private"}
        ],
        "claude_project_mcp": {
            "mcpServers": {
                "private-tool": {"type": "http", "url": "https://example.test/private"},
                "xperfect-coordinator": {"type": "http", "url": "https://example.test/coordinator"},
            }
        },
    }
    assert [item["name"] for item in mcp_servers_for_bundle(bundle, {})] == [
        "private-tool",
        "xperfect-coordinator",
    ]


def test_grok_acp_tools_do_not_collide_with_generated_claude_compat_file(tmp_path):
    bundle = {"claude_project_mcp": {"mcpServers": {
        "xperfect-coordinator": {"type": "http", "url": "https://example.test/mcp"}
    }}}
    _write_claude_project_files(tmp_path, bundle)
    assert "xperfect-coordinator" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    _write_claude_project_files(tmp_path, bundle, grok_acp=True)
    assert json.loads((tmp_path / ".mcp.json").read_text()) == {"mcpServers": {}}
    assert [server["name"] for server in mcp_servers_for_bundle(bundle, {})] == [
        "xperfect-coordinator"
    ]


def test_grok_projection_does_not_send_disabled_explicit_server():
    from workers_projects_runtime.worker_configuration import configured_connections

    bundle = {
        "grok_mcp_servers": [
            {"name": "paused", "type": "http", "url": "https://example.test/paused", "disabled": True}
        ],
        "claude_project_mcp": {
            "paused": {"type": "http", "url": "https://example.test/older"}
        },
    }
    assert mcp_servers_for_bundle(bundle, {}) == []
    assert configured_connections(bundle, profile="grok-build") == {}


def test_native_child_events_require_typed_identity_and_status():
    event={'type':'grok.session.update','session_id':'parent','update':{'sessionUpdate':'subagent_spawned','parent_session_id':'parent','child_session_id':'child','subagent_id':'child','subagent_type':'research','description':'private'}}
    projected=project_native_events('grok',event)
    assert projected[0]['event_type']=='provider.child.started'
    assert 'private' not in json.dumps(projected)
    event['update']['parent_session_id']='unrelated'
    assert project_native_events('grok',event)==[]
    event['update']={'sessionUpdate':'subagent_finished','child_session_id':'child','subagent_id':'child','status':'cancelled','output':'private'}
    assert project_native_events('grok',event)[0]['event_type']=='provider.child.stopped'
    event['update']['status']='invented'
    assert project_native_events('grok',event)==[]


def test_grok_permission_events_project_typed_identity_without_request_arguments():
    requested = {
        'type': 'grok.permission.requested',
        'session_id': 'session-a',
        'request_id': 'request-a',
        'method': 'x.ai/ask_user_question',
        'request': {'questions': [{'question': 'private prompt'}]},
    }
    projected = project_native_events('grok', requested)
    assert projected == [{
        'event_type': 'provider.native.input.requested',
        'payload': {
            'sessionId': 'session-a',
            'requestId': 'request-a',
            'method': 'x.ai/ask_user_question',
            'observedAt': projected[0]['payload']['observedAt'],
        },
    }]
    assert 'private prompt' not in json.dumps(projected)
    resolved = project_native_events('grok', {
        'type': 'grok.permission.response_submitted',
        'session_id': 'session-a',
        'request_id': 'request-a',
        'outcome': 'selected',
        'option_id': 'private-option',
    })
    assert resolved[0]['event_type'] == 'provider.native.input.resolved'
    assert resolved[0]['payload']['outcome'] == 'selected'
    assert project_native_events('grok', {**requested, 'method': 'future/unknown'}) == []


def test_liveness_uses_retry_state_not_error_prose(tmp_path):
    runtime=HostGrokBuildRuntime(str(tmp_path))
    def observation(update):
        return runtime._provider_liveness_observation(json.dumps({'type':'grok.session.update','update':update}),run_id='run',line_sequence=1,model='exact')
    assert observation({'sessionUpdate':'retry_state','type':'retrying'})['kind']=='internal_retry'
    assert observation({'sessionUpdate':'retry_state','type':'failed'}) is None
    assert observation({'sessionUpdate':'tool_call_update','status':'completed'})['kind']=='meaningful_progress'
    assert observation({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'retrying'}})['kind']=='meaningful_progress'


def test_container_artifact_recipe_is_pinned_for_both_architectures():
    recipe=docker_install_instruction()
    assert 'grok-1.0.34-' in recipe and 'sha256sum -c -' in recipe
    assert 'install.sh' not in recipe
    for digest in GROK_LINUX_SHA256.values(): assert digest in recipe


def test_docker_account_acl_is_exact_auth_directory_and_supports_refresh(tmp_path):
    import subprocess
    from workers_projects_runtime.docker_sandbox import DockerSandboxManager
    runtime=DockerSandboxManager(base_dir=str(tmp_path))
    home=tmp_path/'account';(home/'grok').mkdir(parents=True)
    (home/'grok/auth.json').write_text('{}')
    calls=[]
    runtime._docker_exec=lambda name,command,**kwargs: calls.append((command,kwargs)) or subprocess.CompletedProcess([],0,'','')
    worker={'_glasshive_provider_account_bound':True,'_glasshive_provider_account_mount_host':str(home),
            '_glasshive_provider_account_mount_target':'/workspace/.provider-account',
            '_glasshive_provider_account_env':{'GROK_AUTH_PATH':'/workspace/.provider-account/grok/auth.json'}}
    runtime._grant_provider_account_access('fixture',worker)
    assert len(calls)==2
    assert calls[0][1]['user']=='root'
    assert 'setfacl -R' not in calls[0][0][2]
    assert '/workspace/.provider-account/grok/auth.json' in calls[0][0][2]
    assert 'd:u:' in calls[0][0][2]
    assert 'test -x /workspace/.provider-account/grok/auth.json' not in calls[1][0][2]
    worker['_glasshive_provider_account_env']['GROK_AUTH_PATH']='/workspace/.provider-account/other/auth.json'
    with pytest.raises(RuntimeError,match='exact selected'):
        runtime._grant_provider_account_access('fixture',worker)


def test_service_persists_grok_native_session_and_child_projection():
    from types import SimpleNamespace
    from workers_projects_runtime.service import WorkersProjectsService
    updates=[];events=[]
    service=object.__new__(WorkersProjectsService)
    service.store=SimpleNamespace(get_run=lambda _: {'run_id':'run','worker_id':'worker'},get_worker=lambda _: {'project_id':'project'},
        update_run=lambda run,**kw: updates.append(kw),add_event=lambda *args,**kw: events.append((args,kw)))
    service._observe_native_event({'worker_id':'worker','run_id':'run','provider':'grok',
        'event':{'event_type':'provider.session.started','payload':{'sessionId':'native'}}})
    assert updates[0]['native_session_id']=='native'
    assert json.loads(updates[0]['native_capabilities_json'])['provider']=='grok'
    assert len(events)==1
