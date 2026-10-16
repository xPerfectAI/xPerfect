import json
import sys
from pathlib import Path

import pytest
from workers_projects_runtime.direct_runtime import DIRECT_PROVIDERS, DirectModelRuntime
from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
from workers_projects_runtime.profile_registry import require_worker_profile, UnsupportedWorkerProfileError
from workers_projects_runtime.openclaw_runtime import RuntimeConfigurationError
from workers_projects_runtime.conversation_provider import GLASSHIVE_MODELS, _native_terminal_text, _native_usage, _native_public_updates


def worker(tmp_path, profile="direct-xai", **bundle):
    return {"worker_id": "wrk_direct_fixture", "tenant_id": "local", "owner_id": "owner", "name": "SDK fixture",
            "profile": profile, "execution_mode": "host", "model": DIRECT_PROVIDERS[profile]["model"],
            "workspace_root": str(tmp_path / "workspace"), "trusted_run_lane": "conversation",
            "bootstrap_bundle_json": json.dumps({"run_mode": "conversation", **bundle})}


@pytest.mark.parametrize("profile", list(DIRECT_PROVIDERS))
def test_registered_shared_worker_profile_uses_same_supervised_lifecycle(tmp_path, profile):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "state"))
    selected = runtime._runtime_for_worker(worker(tmp_path, profile))
    assert isinstance(selected, DirectModelRuntime)
    assert selected.runtime_name == profile
    assert require_worker_profile(profile).native_transport == "api"
    assert selected.requires_run_start_identity is True
    assert selected._host_process_observer is None
    with pytest.raises(UnsupportedWorkerProfileError):
        runtime._runtime_for_profile(profile, "docker")
    selected.preflight_worker_profile(profile, "host")


@pytest.mark.parametrize("profile", list(DIRECT_PROVIDERS))
def test_exact_model_effort_and_stdio_instruction_projection(tmp_path, monkeypatch, profile):
    runtime = DirectModelRuntime(profile, base_dir=str(tmp_path / "state"))
    monkeypatch.setenv(DIRECT_PROVIDERS[profile]["key"], "synthetic-not-used")
    w = worker(tmp_path, profile, env={DIRECT_PROVIDERS[profile]["effort_env"]: "high"},
               developer_instructions="Exact signed policy", direct_model_settings={"max_tokens": 512})
    info = runtime.prepare_worker_workspace(w)
    command, env = runtime._build_command(w, "Exact current request", info)
    request = json.loads(runtime._command_stdin_text(w, "Exact current request", info))
    assert command == [sys.executable, str(Path(__file__).parents[1] / "src/workers_projects_runtime/direct_runner.py")]
    assert request["model"] == w["model"] and request["provider"] == DIRECT_PROVIDERS[profile]["provider"]
    assert request["effort"] == "high" and request["instruction"] == "Exact current request"
    assert request["instructions"].startswith("Exact signed policy")
    assert request["model_settings"] == {"max_tokens": 512}
    assert "synthetic-not-used" not in json.dumps(request)
    assert env[DIRECT_PROVIDERS[profile]["key"]] == "synthetic-not-used"
    receipt = runtime._native_provider_authority_receipt(w, command=command, run_id="run_exact", model=w["model"])
    assert receipt["runtime"] == profile and receipt["placement"] == "direct_sdk_instructions_stdin"


def test_direct_stream_decodes_existing_public_and_terminal_protocol():
    events = [
        {"type": "grok.session.started", "session_id": "direct:fixture", "model": "grok-4.7"},
        {"type": "grok.session.update", "session_id": "direct:fixture", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "42"}}},
        {"type": "grok.result", "session_id": "direct:fixture", "stop_reason": "end_turn", "output": "42", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]
    text = "\n".join(map(json.dumps, events))
    for profile in DIRECT_PROVIDERS:
        assert _native_terminal_text(profile, text) == "42"
        assert len(_native_public_updates(profile, text)) == 1
        assert _native_usage(profile, text)["total_tokens"] == 12


def test_stateless_does_not_reuse_history(tmp_path):
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    w = worker(tmp_path, env={"GLASSHIVE_PROVIDER_SESSION_MODE": "stateless"})
    info = runtime.prepare_worker_workspace(w)
    request = json.loads(runtime._command_stdin_text(w, "New isolated request", info))
    assert request["history_enabled"] is False


def test_direct_does_not_advertise_unsupported_native_tools_or_nonreasoning_grok():
    row = GLASSHIVE_MODELS["direct-xai:grok-4.7"]
    payload = row.api_payload(readiness={"status": "synthetic"})
    assert payload["capabilities"]["execution_backend"] == "direct_api"
    assert payload["capabilities"]["native_tools"] is False
    assert payload["capabilities"]["connected_tools"] is True
    assert "none" not in row.effort_choices


def test_unregistered_model_options_fail_before_provider_request(tmp_path):
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    w = worker(tmp_path, direct_model_settings={"base_url": "https://other.example"})
    info = runtime.prepare_worker_workspace(w)
    with pytest.raises(RuntimeConfigurationError, match="Unsupported Direct model settings"):
        runtime._command_stdin_text(w, "Any request", info)


def test_direct_projects_codex_connections_bearer_env_and_tool_restrictions():
    from workers_projects_runtime.direct_runtime import direct_mcp_servers
    bundle = {"codex_config_append": '''
[mcp_servers.fixture]
url="https://example.test/mcp"
bearer_token_env_var="EXACT_ATTEMPT_TOKEN"
enabled_tools=["read"]
disabled_tools=["write"]
'''}
    projected = direct_mcp_servers(bundle, {"EXACT_ATTEMPT_TOKEN": "synthetic-secret"})
    assert projected == [{"name": "fixture", "type": "http", "url": "https://example.test/mcp",
        "headers": [{"name": "Authorization", "value": "Bearer synthetic-secret"}],
        "enabled_tools": ["read"], "disabled_tools": ["write"]}]
    assert "synthetic-secret" not in json.dumps(bundle)
    with pytest.raises(RuntimeConfigurationError, match="unavailable environment"):
        direct_mcp_servers(bundle, {})


def test_direct_tool_receipt_preserves_real_provider_call_identity():
    from workers_projects_runtime.conversation_provider import _native_tool_evidence
    values = [
        {"type":"grok.session.started","session_id":"direct:fixture"},
        {"type":"grok.session.update","session_id":"direct:fixture","update":{
            "sessionUpdate":"tool_call","toolCallId":"call_exact","status":"in_progress","rawInput":{"id":"item"},
            "_meta":{"x.ai/tool":{"name":"fixture_read","input":{"id":"item"}}}}},
        {"type":"grok.session.update","session_id":"direct:fixture","update":{
            "sessionUpdate":"tool_call_update","toolCallId":"call_exact","status":"completed","rawOutput":"actual result"}},
    ]
    result = _native_tool_evidence("direct-xai", "\n".join(map(json.dumps, values)))
    assert result["omitted_results"] == 0
    assert result["results"][0]["id"] == "call_exact"
    assert result["results"][0]["name"] == "fixture_read"


def test_explicit_subscription_cannot_fall_back_to_configured_deployment_key(tmp_path, monkeypatch):
    import workers_projects_runtime.direct_runtime as module
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    w = worker(tmp_path)
    info = runtime.prepare_worker_workspace(w)
    monkeypatch.setenv("XAI_API_KEY", "deployment-key-must-not-be-used")
    monkeypatch.setattr(module, "mission_provider_account_selection", lambda _w: object())
    monkeypatch.setattr(module, "apply_bound_provider_account_environment", lambda *a, **k: None)
    with pytest.raises(RuntimeConfigurationError, match="must supply an API key"):
        runtime._build_command(w, "Any", info)


def test_direct_final_tail_and_receipt_match_actual_stdin_authority(tmp_path):
    import hashlib
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    w = worker(tmp_path, developer_instructions="Stable authority\n\nExact final tail",
        declared_developer_instruction_tail="Exact final tail", messaging_delivery_control={"version":1, "audio_eligible":True})
    info = runtime.prepare_worker_workspace(w)
    request = json.loads(runtime._command_stdin_text(w, "Any", info))
    assert request["instructions"].endswith("Exact final tail")
    assert request["instructions"].count("Conversation final-result protocol") == 1
    receipt = runtime._native_provider_authority_receipt(w,
        command=[sys.executable, str(Path(__file__).parents[1] / "src/workers_projects_runtime/direct_runner.py")], run_id="run", model=w["model"])
    assert receipt["authority_sha256"] == hashlib.sha256(request["instructions"].encode()).hexdigest()
    broken = {**w, "bootstrap_bundle_json": json.dumps({"run_mode":"conversation", "developer_instructions":"Wrong tail", "declared_developer_instruction_tail":"Exact final tail", "messaging_delivery_control":{"version":1,"audio_eligible":True}})}
    with pytest.raises(Exception, match="does not end with its declared tail"):
        runtime._command_stdin_text(broken, "Any", info)


@pytest.mark.parametrize("status,kind", [(401,"provider_auth_missing"),(403,"provider_request_rejected"),(429,"provider_rate_limited"),(400,"provider_request_rejected"),(503,"provider_unavailable")])
def test_direct_safe_typed_error_survives_existing_failure_classification(tmp_path, status, kind):
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    w = worker(tmp_path)
    failure, _ = runtime._provider_failure_for_process(worker=w, run_id="run", stdout=json.dumps({"type":"direct.error", "status_code":status, "error_type":"SyntheticSDKError"}), stderr="", exit_code=1)
    assert failure.failure_class == kind and failure.structured
    assert "SyntheticSDKError" in failure.diagnostic_summary


def test_direct_mission_preserves_combined_worker_authority_and_completion_contract(tmp_path):
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    w = {**worker(tmp_path, developer_instructions="Developer-specific condition", agents_md="Exact worker notes", run_mode="mission"), "trusted_run_lane":"mission"}
    info = runtime.prepare_worker_workspace(w)
    request = json.loads(runtime._command_stdin_text(w, "Do the task", info))
    assert "Exact worker notes" in request["instructions"]
    assert "Developer-specific condition" in request["instructions"]
    assert request["instruction"] != "Do the task"
    assert "Do the task" in request["instruction"]


def test_direct_api_key_preparation_does_not_need_native_cli(tmp_path, monkeypatch):
    import workers_projects_runtime.native_api_keys as keys
    monkeypatch.setattr("workers_projects_runtime.provider_accounts.provider_setup_binary", lambda _p: None)
    keys._prepare_native_key("codex", "synthetic", tmp_path, None)
    keys._prepare_native_key("claude", "synthetic", tmp_path, None)


@pytest.mark.parametrize("audio", ["skip", "eligible"])
def test_sdk_part_phase_and_message_boundaries_keep_commentary_out_of_public_voice(tmp_path, monkeypatch, audio):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import pydantic_ai
    from pydantic_ai import PartStartEvent, PartEndEvent, TextPart, AgentRunResultEvent
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.usage import RunUsage
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    commentary = TextPart('{"type":"assistant_response","voice":"eligible","content":"PRIVATE COMMENTARY","tool_name":null}', provider_details={"phase":"commentary"})
    final = TextPart(json.dumps({"type":"assistant_response","voice":audio,"content":"Useful answer","tool_name":None}), provider_details={"phase":"final_answer"})
    result = SimpleNamespace(usage=RunUsage(input_tokens=10, output_tokens=2), all_messages=lambda: [ModelResponse(parts=[commentary, final])])
    class FakeAgent:
        def __init__(self,*args,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        @asynccontextmanager
        async def run_stream_events(self,*args,**kwargs):
            async def events():
                yield PartStartEvent(index=0,part=commentary)
                yield PartEndEvent(index=0,part=commentary)
                yield PartStartEvent(index=1,part=final)
                yield PartEndEvent(index=1,part=final)
                yield AgentRunResultEvent(result)
            yield events()
    captured=[]
    monkeypatch.setattr(pydantic_ai,"Agent",FakeAgent)
    monkeypatch.setattr(direct_runner,"emit",captured.append)
    monkeypatch.setenv("XAI_API_KEY","synthetic-not-used")
    asyncio.run(direct_runner.execute({"provider":"xai","model":"grok-4.7","history_path":str(tmp_path/'history.json'),
        "history_scope":{"worker":"fixture"},"history_enabled":True,"session_id":"direct:fixture","run_id":"run_exact",
        "mcp_servers":[],"instructions":"Exact policy","instruction":"Any","effort":"low"}))
    log="\n".join(map(json.dumps,captured))
    assert "PRIVATE COMMENTARY" not in log
    assert _native_terminal_text("direct-xai",log).endswith('null}')
    public=_native_spoken_content("direct-xai",log,None,{"version":1,"audio_eligible":True})
    assert public and public["text"] == "Useful answer"
    assert public["delivery_disposition"]["audio"] == audio
    assert captured[-1]["output"] == final.content
    assert "PRIVATE COMMENTARY" in (tmp_path/'history.json').read_text()  # protocol history remains faithful


@pytest.mark.parametrize("profile,settings", [("direct-xai",{"temperature":.5}), ("direct-openai",{"top_p":.9}), ("direct-anthropic",{"service_tier":"priority"})])
def test_direct_does_not_accept_settings_the_official_sdk_silently_discards(tmp_path, profile, settings):
    runtime=DirectModelRuntime(profile,base_dir=str(tmp_path/'state'))
    w=worker(tmp_path,profile,direct_model_settings=settings)
    info=runtime.prepare_worker_workspace(w)
    with pytest.raises(RuntimeConfigurationError):
        runtime._command_stdin_text(w,"Any",info)


@pytest.mark.parametrize("stage,kind", [("tool_connection","native_mcp_unavailable"),("tool_execution","native_mcp_unavailable"),("history_read","unsupported_runtime_configuration"),("sdk_dependency","runtime_dependency_missing"),("provider_request","provider_unavailable")])
def test_direct_local_failures_do_not_blame_provider_auth(tmp_path, stage, kind):
    runtime=DirectModelRuntime("direct-xai",base_dir=str(tmp_path/'state'))
    error,_=runtime._provider_failure_for_process(worker=worker(tmp_path),run_id="run",stdout=json.dumps({"type":"direct.error","status_code":None,"stage":stage,"error_type":"SyntheticFailure"}),stderr="",exit_code=1)
    assert error.failure_class==kind and error.structured
    assert "connection was rejected" not in error.user_message


def test_direct_stage_events_include_cross_process_wall_clock(capsys):
    from workers_projects_runtime.direct_runner import emit
    emit({"type":"direct.stage","stage":"provider_client_ready"})
    event=json.loads(capsys.readouterr().out)
    assert event['executor_elapsed_ms'] >= 0
    assert event['observed_at_ms'] > 1_700_000_000_000


def test_official_openai_sdk_preserves_max_effort_and_ultrafast_tier(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from workers_projects_runtime import direct_runner
    import openai
    original=openai.DefaultAsyncHttpxClient
    captured=[]
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(401,json={'error':{'message':'Synthetic auth rejection','type':'invalid_request_error'}})
    class MockClient(original):
        def __init__(self,*args,**kwargs):
            kwargs['transport']=httpx.MockTransport(handler)
            super().__init__(*args,**kwargs)
    monkeypatch.setattr(openai,'DefaultAsyncHttpxClient',MockClient)
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-not-used')
    observed_events = []
    monkeypatch.setattr(direct_runner,'emit',observed_events.append)
    with pytest.raises(Exception):
        asyncio.run(direct_runner.execute({'provider':'openai','model':'gpt-6.1-sol','effort':'max',
            'model_settings':{'service_tier':'ultrafast'},'instructions':'Exact policy','instruction':'Any',
            'history_path':str(tmp_path/'history.json'),'history_scope':{'worker':'synthetic'},'session_id':'direct:fixture',
            'run_id':'run_exact','mcp_servers':[]}))
    assert len(captured)==1
    assert captured[0]['model']=='gpt-6.1-sol'
    assert captured[0]['reasoning']['effort']=='max'
    started = next(e for e in observed_events if e.get('type') == 'grok.session.started')
    assert started['requestedReasoningEffort'] == 'max'
    assert captured[0]['service_tier']=='ultrafast'
    assert captured[0]['store'] is False
    assert 'reasoning.encrypted_content' in captured[0]['include']
    assert 'max' in GLASSHIVE_MODELS['direct-openai:gpt-6.1-sol'].effort_choices

@pytest.mark.parametrize('provider', ['xai', 'openai', 'anthropic'])
@pytest.mark.parametrize('voice', ['eligible', 'skip', 'invalid_plain', 'missing_voice', 'invalid_enum', 'extra_field', 'unknown_graph', 'wrong_tool', 'duplicate_key', 'empty_object', 'empty_skip'])
def test_real_sdk_typed_output_contract_survives_tool_round_and_streams(tmp_path, monkeypatch, voice, provider):
    """Exercise the real SDK loop: tool result -> same native schema -> typed public text."""
    import asyncio
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel, DeltaToolCall
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models import openai as openai_model
    schema = conversation_output_schema({'version':1,'tools':[{'name':'lc_transfer_fixture','description':'Synthetic transfer'}]}, {'version': 1, 'audio_eligible': True})
    calls, accepted, captured = [], [], []
    async def actual_tool() -> str:
        accepted.append('authoritative action receipt')
        return accepted[-1]
    async def model_stream(messages, info):
        calls.append(info.model_request_parameters)
        if len(calls) == 1:
            yield {0: DeltaToolCall(name='actual_tool', json_args='{}', tool_call_id='call_action')}
            return
        assert any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
        if voice == 'invalid_plain':
            yield 'Plain response must not become voice eligible.'
            return
        if voice not in {'eligible','skip','empty_skip'}:
            value={'type':'assistant_response','tool_name':None,'voice':'eligible','content':'Must not complete.'}
            if voice=='missing_voice':value.pop('voice')
            elif voice=='invalid_enum':value['voice']='invalid'
            elif voice=='extra_field':value['extra']='not allowed'
            elif voice=='unknown_graph':value.update(type='tool_call',tool_name='lc_missing')
            elif voice=='wrong_tool':value['tool_name']='lc_transfer_fixture'
            elif voice=='empty_object':value={}
            raw=json.dumps(value)
            if voice=='duplicate_key':raw=raw.replace('"voice": "eligible"','"voice": "eligible", "voice": "skip"')
            if provider=='anthropic':yield raw
            else:yield {0: DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=raw,tool_call_id='result_invalid')}
            return
        if voice=='empty_skip':
            raw=json.dumps({'type':'assistant_response','tool_name':None,'voice':'skip','content':''})
            if provider=='anthropic':yield raw
            else:yield {0:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=raw,tool_call_id='result_empty')}
            return
        prefix = '{"type":"assistant_response","tool_name":null,"voice":"' + voice + '","content":"'
        if provider=='anthropic':yield prefix+'Forty-two'
        else:yield {0:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=prefix+'Forty-two',tool_call_id='result_exact')}
        # The event transport must expose partial raw text before terminal output.
        assert any('Forty-two' in json.dumps(event) for event in captured)
        if provider=='anthropic':yield '."}'
        else:yield {0:DeltaToolCall(json_args='."}')}
    from pydantic_ai.profiles.anthropic import anthropic_model_profile
    profile=anthropic_model_profile('claude-opus-5-5') if provider=='anthropic' else None
    model = FunctionModel(stream_function=model_stream,profile=profile)
    original_agent = pydantic_ai.Agent
    def make_agent(*args, **kwargs):
        return original_agent(*args, tools=[actual_tool], **kwargs)
    monkeypatch.setattr(pydantic_ai, 'Agent', make_agent)
    monkeypatch.setattr(openai_model, 'OpenAIResponsesModel', lambda *a, **k: model)
    from pydantic_ai.models import anthropic as anthropic_model
    monkeypatch.setattr(anthropic_model, 'AnthropicModel', lambda *a, **k: model)
    monkeypatch.setattr(direct_runner, 'emit', captured.append)
    credential = {'xai':'XAI_API_KEY','openai':'OPENAI_API_KEY','anthropic':'ANTHROPIC_API_KEY'}[provider]
    native_model = {'xai':'grok-4.7','openai':'gpt-6.1-sol','anthropic':'claude-opus-5-5'}[provider]
    monkeypatch.setenv(credential, 'synthetic-not-used')
    request = {'provider': provider, 'model': native_model, 'effort': 'low',
        'instructions': 'Unchanged owning authority', 'instruction': 'Synthetic tool then reply',
        'output_schema': schema, 'mcp_servers': [], 'history_enabled': False,
        'history_path': str(tmp_path/'history.json'), 'history_scope': {'worker': 'fixture'},
        'session_id': 'direct:fixture', 'run_id': 'exact_run'}
    if voice not in {'eligible','skip','empty_skip'}:
        with pytest.raises(Exception):
            asyncio.run(direct_runner.execute(request))
    else:
        asyncio.run(direct_runner.execute(request))
    assert len(accepted) == 1 and len(calls) == 2  # no replay / hidden repair round
    assert all(call.output_mode == ('native' if provider=='anthropic' else 'tool') for call in calls)
    for call in calls:
        schema=call.output_object.json_schema if provider=='anthropic' else call.output_tools[0].parameters_json_schema
        assert schema['properties']['voice']['enum']==['eligible','skip']
    log = '\n'.join(map(json.dumps, captured))
    markers=[event for event in captured if event.get('stage') in {'sdk_first_raw_text','sdk_first_raw_output_arguments'}]
    assert len(markers)==1
    assert set(markers[0])=={'type','stage','part_kind'}
    assert markers[0]['part_kind']==('text' if provider=='anthropic' or voice=='invalid_plain' else 'output_tool_arguments')
    public_indices=[i for i,event in enumerate(captured)if event.get('update',{}).get('sessionUpdate')=='agent_message_chunk']
    if public_indices:assert captured.index(markers[0])<public_indices[0]
    if voice not in {'eligible','skip','empty_skip'}:
        assert not any(event.get('type') == 'grok.result' for event in captured)
        public = _native_spoken_content('direct-xai', log, {'version':1,'tools':[{'name':'lc_transfer_fixture'}]}, {'version':1, 'audio_eligible':True})
        assert public is None or public['delivery_disposition']['audio'] != 'eligible'
    else:
        result = json.loads(captured[-1]['output'])
        assert result['voice'] == ('skip' if voice=='empty_skip' else voice)
        assert result['content'] == ('' if voice=='empty_skip' else 'Forty-two.')
        public = _native_spoken_content('direct-xai', log, None, {'version':1, 'audio_eligible':True})
        if voice=='empty_skip':
            assert public is None or public['delivery_disposition']['audio']=='skip'
        else:
            assert public['delivery_disposition']['audio'] == voice
            assert public['text'] == 'Forty-two.'


def test_direct_owning_output_schema_is_projected_only_for_conversation(tmp_path):
    runtime = DirectModelRuntime('direct-xai', base_dir=str(tmp_path/'state'))
    current = worker(tmp_path, messaging_delivery_control={'version':1, 'audio_eligible':True})
    info = runtime.prepare_worker_workspace(current)
    request = json.loads(runtime._command_stdin_text(current, 'Any', info))
    assert request['output_schema'] == runtime._conversation_output_schema(current)
    mission = {**worker(tmp_path, run_mode='mission', direct_core_archive={'owner':'core'}), 'trusted_run_lane':'mission'}
    info = runtime.prepare_worker_workspace(mission)
    mission_request = json.loads(runtime._command_stdin_text(mission, 'Any', info))
    assert mission_request['output_schema'] is None and mission_request['core_archive'] is None

@pytest.mark.parametrize('provider,model,credential', [
    ('xai', 'grok-4.7', 'XAI_API_KEY'),
    ('openai', 'gpt-6.1-sol', 'OPENAI_API_KEY'),
    ('anthropic', 'claude-opus-5-5', 'ANTHROPIC_API_KEY'),
    ('anthropic', 'claude-sonnet-5', 'ANTHROPIC_API_KEY'),
])
def test_official_sdk_wire_keeps_plain_sampling_tools_and_owning_authority(tmp_path, monkeypatch, provider, model, credential):
    import asyncio
    import importlib
    httpx = importlib.import_module('httpx2' if provider == 'anthropic' else 'httpx')
    sdk = importlib.import_module('anthropic' if provider == 'anthropic' else 'openai')
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    import pydantic_ai
    original_agent = pydantic_ai.Agent
    async def fixture_action() -> str:
        return 'synthetic action'
    monkeypatch.setattr(pydantic_ai, 'Agent', lambda *a, **k: original_agent(*a, tools=[fixture_action], **k))
    original = sdk.DefaultAsyncHttpxClient
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(401, json={'error': {'message': 'Synthetic auth rejection', 'type':'authentication_error'}})
    class MockClient(original):
        def __init__(self, *args, **kwargs):
            timeout=kwargs['timeout']
            assert timeout.read is None and timeout.connect==5.0 and timeout.pool==600 and timeout.write==600
            kwargs['transport'] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(sdk, 'DefaultAsyncHttpxClient', MockClient)
    monkeypatch.setenv(credential, 'synthetic-not-used')
    monkeypatch.setattr(direct_runner, 'emit', lambda _e: None)
    schema = conversation_output_schema(None, {'version':1, 'audio_eligible':True})
    with pytest.raises(Exception) as failure:
        asyncio.run(direct_runner.execute({'provider':provider, 'model':model, 'effort':'high',
            'instructions':'Unchanged owning authority', 'instruction':'Synthetic request',
            'output_schema':schema, 'mcp_servers':[], 'history_enabled':False,
            'history_path':str(tmp_path/'history.json'), 'history_scope':{'worker':'fixture'},
            'session_id':'direct:fixture', 'run_id':'exact_run'}))
    assert len(captured) == 1, str(failure.value)  # official SDK reached wire; no validation-before-launch rejection
    output = captured[0].get('output_config', {}) if provider == 'anthropic' else captured[0].get('text', {})
    assert ('format' in output)==(provider=='anthropic')  # SDK-declared forced-tool capability owns strategy
    assert any(tool.get('name') == 'fixture_action' for tool in captured[0]['tools'])
    choice = captured[0].get('tool_choice')
    assert choice == ({'type':'auto'} if provider == 'anthropic' else 'required')
    assert any(tool.get('name')==direct_runner.OUTPUT_TOOL_NAME for tool in captured[0]['tools'])==(provider!='anthropic')
    if provider=='anthropic':
        assert captured[0]['thinking']=={'type':'adaptive'}
        assert output['format']['type']=='json_schema'
    if provider == 'anthropic':
        assert [item['text'] for item in captured[0]['system']] == ['Unchanged owning authority']
    else:
        assert captured[0]['instructions'] == 'Unchanged owning authority'
    # SDK schema validation/eligible/skip/invalid outputs are checked in the
    # real SDK continuation test above; source authority gains no hidden template.


def test_sdk_schema_union_projection_preserves_delivery_contract():
    import copy
    from jsonschema import Draft202012Validator
    from workers_projects_runtime.direct_runner import sdk_output_schema
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    original = conversation_output_schema(None, {'version':1, 'audio_eligible':True})
    before = copy.deepcopy(original)
    projected = sdk_output_schema(original)
    assert original == before
    assert projected['properties']['tool_name']['anyOf'] == [{'type':'string'}, {'type':'null'}]
    for value in [
        {'type':'assistant_response','tool_name':None,'voice':'eligible','content':'42'},
        {'type':'assistant_response','tool_name':None,'voice':'skip','content':'42'},
        {'type':'assistant_response','tool_name':'unavailable','voice':'eligible','content':'42'},
        {'type':'assistant_response','tool_name':None,'voice':'invalid','content':'42'},
        {'type':'assistant_response','tool_name':None,'content':'42'},
        'plain text',
    ]:
        assert Draft202012Validator(original).is_valid(value) == Draft202012Validator(projected).is_valid(value)


def test_signed_broker_keeps_canonical_names_but_other_mcp_stays_namespaced():
    from workers_projects_runtime.direct_runtime import direct_mcp_servers
    broker = {'version': 1, 'name': 'signed-fixture', 'url': 'https://example.test/broker'}
    bundle = {'glasshive_capability_broker': broker, 'claude_project_mcp': {'mcpServers': {
        'signed-fixture': {'url': broker['url']},
        'other-fixture': {'url': 'https://example.test/other'},
    }}}
    servers = direct_mcp_servers(bundle, {'GLASSHIVE_CAPABILITY_BROKER_TOKEN': 'synthetic'})
    values = {server['name']: server for server in servers}
    assert values['signed-fixture']['preserve_tool_names'] is True
    assert 'preserve_tool_names' not in values['other-fixture']


def test_native_sdk_canonical_broker_tools_match_authority_and_keep_other_namespaces(monkeypatch, tmp_path):
    import asyncio
    import pydantic_ai
    from pydantic_ai.toolsets import FunctionToolset
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models import openai as openai_model
    import pydantic_ai.mcp
    import fastmcp
    from workers_projects_runtime import direct_runner
    catalog = []
    canonical = 'worker_delegate_once_mcp_fixture'
    async def launcher():
        return 'not called'
    def make_toolset(*args, **kwargs):
        tools = FunctionToolset()
        tools.add_function(launcher, name=canonical)
        return tools
    async def model_stream(messages, info):
        catalog.extend(tool.name for tool in info.function_tools)
        yield 'Done.'
    monkeypatch.setattr(pydantic_ai.mcp, 'MCPToolset', make_toolset)
    monkeypatch.setattr(fastmcp, 'Client', lambda config: object())
    monkeypatch.setattr(openai_model, 'OpenAIResponsesModel', lambda *a, **k: FunctionModel(stream_function=model_stream))
    monkeypatch.setattr(direct_runner, 'emit', lambda value: None)
    monkeypatch.setenv('XAI_API_KEY', 'synthetic-not-used')
    asyncio.run(direct_runner.execute({'provider': 'xai', 'model': 'grok-4.7', 'effort': 'low',
        'instructions': 'Authorized host tools: ' + canonical, 'instruction': 'Synthetic input',
        'history_enabled': False, 'history_path': str(tmp_path/'history.json'),
        'history_scope': {'worker': 'fixture'}, 'session_id': 'direct:fixture', 'run_id': 'exact_run',
        'mcp_servers': [
            {'name': 'signed-fixture', 'url': 'https://example.test/broker', 'preserve_tool_names': True},
            {'name': 'other-fixture', 'url': 'https://example.test/other'},
        ]}))
    assert catalog == [canonical, 'other-fixture_' + canonical]


def _core_archive_fixture(snapshot='a' * 64, archive_value=None):
    import hashlib
    raw = '<semantic_compaction version="1">' + json.dumps(archive_value or {'facts':['retained archive']}) + '</semantic_compaction>'
    return {'protocol':'main_context_v1', 'owner':'core', 'snapshot_sha256':snapshot,
            'archive':raw, 'archive_sha256':hashlib.sha256(raw.encode()).hexdigest()}


def _contextual_fixture(archive, goal='Current goal', **other):
    return json.dumps({'context':[], 'context_manifest':other,
        'instruction':'UNIQUE BEFORE\n<viventium_main_continuity_v1>\n' + archive['archive'] + '\n</viventium_main_continuity_v1>\n' + goal}, ensure_ascii=False)


def test_direct_core_archive_is_only_projected_from_trusted_typed_current_owner():
    from workers_projects_runtime.conversation_provider import ChatCompletionRequest, _direct_core_archive_projection
    archive = _core_archive_fixture()
    capsule = '<viventium_main_continuity_v1>\n' + archive['archive'] + '\n</viventium_main_continuity_v1>'
    metadata = {'main_context_protocol':'main_context_v1', 'main_context_owner':'core',
                'main_context_snapshot_sha256':'a'*64, 'turn_context':capsule}
    payload = ChatCompletionRequest(model='direct-xai:grok-4.7',messages=[{'role':'user','content':'Current user goal'}],metadata=metadata)
    model = GLASSHIVE_MODELS[payload.model]
    assert _direct_core_archive_projection(payload, model) == archive
    for change in [{'main_context_owner':'provider_legacy'}, {'main_context_protocol':''}, {'main_context_snapshot_sha256':''},
                   {'turn_context':capsule + capsule}, {'turn_context':capsule.replace('version="1"','version="2"')}]:
        altered = payload.model_copy(update={'metadata':payload.metadata.model_copy(update=change)})
        assert _direct_core_archive_projection(altered, model) is None
    assert _direct_core_archive_projection(payload, GLASSHIVE_MODELS['grok-build:grok-4.7']) is None


def test_exact_archive_projection_preserves_every_unique_byte_image_and_native_part():
    import hashlib
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, ToolCallPart, ToolReturnPart, ThinkingPart, BinaryContent
    from workers_projects_runtime.direct_runner import core_archive_history_record, core_archive_request_projection
    archive = _core_archive_fixture()
    text = _contextual_fixture(archive, 'Old unique goal with file evidence', unique_file='file-fixture')
    image = BinaryContent(b'unique-image',media_type='image/png')
    current = ModelRequest(parts=[UserPromptPart([text,image]), UserPromptPart('Unique tool-added user content')])
    thinking = ThinkingPart('',id='reasoning_exact',signature='encrypted_exact',provider_name='openai')
    response = ModelResponse(parts=[thinking,ToolCallPart('fixture_read',{'file':'file-fixture'},'call_exact')],provider_response_id='response_exact')
    tool_result = ModelRequest(parts=[ToolReturnPart('fixture_read',{'answer':'Retained tool result'},'call_exact')])
    messages = [current,response,tool_result]
    result = SimpleNamespace(all_messages=lambda:messages, new_messages=lambda:messages)
    marked = core_archive_history_record(result, {'instruction':text}, archive)
    assert current.metadata is None  # raw SDK object did not change
    from dataclasses import replace
    later = replace(current, parts=[UserPromptPart([text,image]), current.parts[1]])
    later_result = SimpleNamespace(all_messages=lambda:[*marked,later],new_messages=lambda:[later])
    combined = core_archive_history_record(later_result, {'instruction':text}, archive)
    projected = core_archive_request_projection(combined,archive)
    assert projected[0] is marked[0]  # first trusted provider prefix remains exact
    changed = projected[-1].parts[0].content
    encoded_archive = json.dumps(archive['archive'],ensure_ascii=False)[1:-1]
    assert changed[0] == text.replace(encoded_archive,'',1)
    assert changed[1] is image
    assert projected[-1].parts[1] is current.parts[1]
    assert projected[1] is response and projected[2] is tool_result
    assert response.parts[0].signature == 'encrypted_exact'
    assert response.parts[1].tool_call_id == tool_result.parts[0].tool_call_id == 'call_exact'
    assert marked[0].parts[0].content[0] == text  # persistent evidence stays byte-exact
    assert core_archive_request_projection(messages,archive) == messages  # unmarked legacy
    assert core_archive_request_projection(marked,_core_archive_fixture(archive_value={'facts':['new archive']})) == marked
    mismatched = {**marked[0].metadata['xperfect_core_input'],'input_sha256':'b'*64}
    marked[0].metadata = {'xperfect_core_input':mismatched}
    assert core_archive_request_projection(marked,archive) == marked


def test_core_archive_hook_preserves_raw_history_across_actual_sdk_tool_and_second_turn(tmp_path,monkeypatch):
    import asyncio
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel,DeltaToolCall
    from pydantic_ai.messages import ModelRequest,UserPromptPart,ToolCallPart,ToolReturnPart,ModelMessagesTypeAdapter
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner
    archive = _core_archive_fixture()
    calls, tool_calls = [], []
    async def actual_tool():
        tool_calls.append('actual receipt')
        return tool_calls[-1]
    def archive_count(messages):
        return sum(json.loads(part.content)['instruction'].count(archive['archive'])
                   for message in messages if isinstance(message,ModelRequest)
                   for part in message.parts if isinstance(part,UserPromptPart) and isinstance(part.content,str))
    async def stream(messages,info):
        calls.append(messages)
        if len(calls)==1:
            yield {0:DeltaToolCall(name='actual_tool',json_args='{}',tool_call_id='call_exact')}
        else:
            assert archive_count(messages)==1
            if len(calls)==3:
                assert any(isinstance(part,ToolCallPart) and part.tool_call_id=='call_exact'for message in messages for part in message.parts)
                assert any(isinstance(part,ToolReturnPart) and part.tool_call_id=='call_exact'for message in messages for part in message.parts)
                assert 'New unique user goal' in str(messages)
            if len(calls)>=3:
                assert messages[0].parts == calls[0][0].parts  # cacheable first prefix unchanged
                assert 'First unique user goal' in str(messages)
            if len(calls)==4:
                assert 'Third unique user goal' in str(messages)
            yield 'Done.'
    original_agent = pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai,'Agent',lambda *args,**kwargs:original_agent(*args,tools=[actual_tool],**kwargs))
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**k:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner,'emit',lambda value:None)
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used')
    request = {'provider':'xai','model':'grok-4.7','effort':'low','instructions':'Owning authority','mcp_servers':[],
               'history_path':str(tmp_path/'history.json'),'history_scope':{'worker':'fixture'},'history_enabled':True,
               'session_id':'direct:fixture','run_id':'run_first','core_archive':archive,
               'instruction':_contextual_fixture(archive,'First unique user goal')}
    asyncio.run(direct_runner.execute(request))
    asyncio.run(direct_runner.execute({**request,'run_id':'run_second','instruction':_contextual_fixture(archive,'New unique user goal')}))
    asyncio.run(direct_runner.execute({**request,'run_id':'run_third','instruction':_contextual_fixture(archive,'Third unique user goal')}))
    retained=ModelMessagesTypeAdapter.validate_json(json.loads((tmp_path/'history.json').read_text())['messages'])
    assert archive_count(retained)==3
    assert len(tool_calls)==1 and len(calls)==4


def test_current_archive_must_be_present_and_exact_before_projection():
    from workers_projects_runtime.direct_runner import _core_archive_input
    archive = _core_archive_fixture()
    text = _contextual_fixture(archive, 'The current input remains intact')
    assert _core_archive_input({'core_archive':archive,'instruction':text}) == archive
    for change in [{'owner':'provider_legacy'}, {'protocol':''}, {'snapshot_sha256':''}, {'archive_sha256':'b'*64}]:
        assert _core_archive_input({'core_archive':{**archive,**change},'instruction':text}) is None
    assert _core_archive_input({'core_archive':archive,'instruction':_contextual_fixture(_core_archive_fixture(archive_value={'facts':['other']}))}) is None


def test_bundle_projection_cannot_inherit_caller_claimed_archive_ownership():
    from workers_projects_runtime.conversation_provider import ChatCompletionRequest, ConversationProvider
    payload=ChatCompletionRequest(model='direct-xai:grok-4.7',messages=[{'role':'user','content':'Synthetic current goal'}],
        metadata={'bootstrap_bundle':{'direct_core_archive':_core_archive_fixture()},'main_context_owner':'provider_legacy'})
    bundle=ConversationProvider._native_bundle(None,payload,GLASSHIVE_MODELS[payload.model],'high')
    assert 'direct_core_archive' not in bundle


@pytest.mark.parametrize('output_first',[True,False])
@pytest.mark.parametrize('extra_output',[False,True])
@pytest.mark.parametrize('voice',['eligible','skip'])
def test_typed_output_tool_cannot_skip_coemitted_native_function(tmp_path,monkeypatch,output_first,extra_output,voice):
    import asyncio
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel,DeltaToolCall
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    from workers_projects_runtime.conversation_provider import _native_spoken_content,ConversationProvider
    from types import SimpleNamespace
    calls,accepted,emitted=[],[],[]
    content='Forty-two. 🙂'
    final=json.dumps({'type':'assistant_response','tool_name':None,'voice':voice,'content':content})
    async def actual_tool():
        accepted.append('authoritative receipt')
        return accepted[-1]
    async def stream(messages,info):
        calls.append(messages)
        yield 'Private unstructured narration must never become speech.'
        index=0 if output_first else 1
        yield {index:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=final[:-2],tool_call_id='result_exact')}
        assert any('Forty-two' in json.dumps(event)for event in emitted)
        yield {index:DeltaToolCall(json_args=final[-2:])}
        yield {1-index:DeltaToolCall(name='actual_tool',json_args='{}',tool_call_id='call_same_response')}
        if extra_output:
            losing=json.dumps({'type':'assistant_response','tool_name':None,'voice':'eligible','content':'Losing terminal must stay private.'})
            yield {2:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=losing,tool_call_id='result_losing')}
    original=pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai,'Agent',lambda *a,**k:original(*a,tools=[actual_tool],**k))
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**k:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner,'emit',emitted.append)
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used')
    asyncio.run(direct_runner.execute({'provider':'xai','model':'grok-4.7','effort':'low','instructions':'Exact owning authority',
        'instruction':'Synthetic current request','mcp_servers':[], 'history_enabled':False,
        'history_path':str(tmp_path/'history.json'),'history_scope':{'worker':'fixture'},'session_id':'direct:fixture','run_id':'same_response',
        'output_schema':conversation_output_schema(None,{'version':1,'audio_eligible':True})}))
    assert accepted==['authoritative receipt'] and len(calls)==1
    terminal=emitted[-1]
    assert json.loads(terminal['output'])['content']==content
    assert terminal['context_usage'] == {'source': 'unavailable', 'unmeasured_tool_results': True}
    assert 'Private unstructured' not in json.dumps(emitted)
    assert 'Losing terminal' not in json.dumps(emitted)
    ranges=terminal['deliveryDispositionRanges']
    assert ranges[0]['start']==0 and ranges[-1]['end']==len(content.encode('utf-16-le'))//2
    assert ranges[0]['disposition']['audio']==voice
    control={'version':1,'audio_eligible':True}
    stdout='\n'.join(json.dumps(event)for event in emitted)
    public=_native_spoken_content('direct-xai',stdout,None,control)
    assert public is not None and public['text']==content and public['delivery_disposition']['audio']==voice
    service=SimpleNamespace(runtime=SimpleNamespace(provider_activity_log=lambda worker,run:('direct-xai',stdout)),
        render_provider_native_images=lambda request,run,output,**kwargs:output)
    owner=SimpleNamespace(service=service,store=SimpleNamespace(get_worker=lambda worker:{'worker_id':'fixture','worker_profile':'direct-xai'}))
    assert ConversationProvider._native_delivery_ranges(owner,{}, {'worker_id':'fixture','run_id':'same_response'},content)==ranges


def test_invalid_sdk_output_is_not_mislabeled_as_unavailable_connection(tmp_path):
    runtime=DirectModelRuntime('direct-xai',base_dir=str(tmp_path/'state'))
    w=worker(tmp_path)
    for event in [{'type':'direct.error','error_type':'ValidationError','stage':'output_validation'},
                  {'type':'direct.error','error_type':'UnexpectedModelBehavior','stage':'provider_request'}]:
        failure,_=runtime._provider_failure_for_process(worker=w,run_id='invalid',stdout=json.dumps(event),stderr='',exit_code=1)
        assert failure.failure_class=='provider_response_invalid' and not failure.retryable


@pytest.mark.parametrize('profile,provider',[('direct-openai','openai'),('direct-anthropic','anthropic'),('direct-xai','xai')])
def test_direct_profile_authentication_contract_preserves_native_alternatives(profile,provider):
    from workers_projects_runtime.control_plane import profile_provider_account_issue
    assert profile_provider_account_issue(profile,{'provider':provider,'auth_method':'api_key'})==''
    assert 'API-key account' in profile_provider_account_issue(profile,{'provider':provider,'auth_method':'subscription'})
    native={'direct-openai':'codex-cli','direct-anthropic':'claude-code','direct-xai':'grok-build'}[profile]
    assert profile_provider_account_issue(native,{'provider':provider,'auth_method':'subscription'})==''


@pytest.mark.parametrize('typed',[False,True])
def test_completed_native_tool_pair_survives_stop_during_following_generation(tmp_path,monkeypatch,typed):
    import asyncio
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel,DeltaToolCall
    from pydantic_ai.models import openai as openai_model
    from pydantic_ai.messages import ModelMessagesTypeAdapter,ToolCallPart,ToolReturnPart
    from workers_projects_runtime import direct_runner
    history_path=tmp_path/'history.json'
    calls,actions,retained=[],[],[]
    async def actual_tool():
        actions.append('accepted action')
        return actions[-1]
    async def stream(messages,info):
        calls.append(messages)
        if len(calls)==1:
            yield {0:DeltaToolCall(name='actual_tool',json_args='{}',tool_call_id='call_before_stop')}
        elif len(calls)==2:
            checkpoint=json.loads(history_path.read_text())
            retained.extend(ModelMessagesTypeAdapter.validate_json(checkpoint['messages']))
            assert any(isinstance(part,ToolReturnPart) and part.tool_call_id=='call_before_stop'for message in retained for part in message.parts)
            raise asyncio.CancelledError()
        else:
            assert any(isinstance(part,ToolCallPart) and part.tool_call_id=='call_before_stop'for message in messages for part in message.parts)
            assert any(isinstance(part,ToolReturnPart) and part.tool_call_id=='call_before_stop'for message in messages for part in message.parts)
            if typed:
                raw=json.dumps({'type':'assistant_response','tool_name':None,'voice':'eligible','content':'Recovered without replaying the accepted action.'})
                yield {0:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=raw,tool_call_id='recovered_result')}
            else:yield 'Recovered without replaying the accepted action.'
    original=pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai,'Agent',lambda *a,**k:original(*a,tools=[actual_tool],**k))
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**k:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner,'emit',lambda value:None)
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used')
    request={'provider':'xai','model':'grok-4.7','effort':'low','instructions':'Exact authority','instruction':'First accepted source',
        'mcp_servers':[],'history_enabled':True,'history_path':str(history_path),'history_scope':{'worker':'fixture'},
        'session_id':'direct:fixture','run_id':'run_stopped'}
    if typed:
        from workers_projects_runtime.agent_builder_control import conversation_output_schema
        request['output_schema']=conversation_output_schema(None,{'version':1,'audio_eligible':True})
    with pytest.raises(asyncio.CancelledError):asyncio.run(direct_runner.execute(request))
    assert len(actions)==1
    asyncio.run(direct_runner.execute({**request,'instruction':'New user direction','run_id':'run_recovered'}))
    assert len(actions)==1 and len(calls)==3


@pytest.mark.parametrize('first_voice,last_voice',[('eligible','skip'),('skip','eligible')])
def test_tool_output_keeps_typed_interim_text_and_mixed_voice_ranges(tmp_path,monkeypatch,first_voice,last_voice):
    import asyncio
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel,DeltaToolCall
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    from workers_projects_runtime.conversation_provider import _native_spoken_content
    calls,actions,events=[],[],[]
    first=json.dumps({'type':'assistant_response','tool_name':None,'voice':first_voice,'content':'Independent answer 🙂'})
    final=json.dumps({'type':'assistant_response','tool_name':None,'voice':last_voice,'content':'Actual receipt returned.'})
    async def actual_tool():
        actions.append('actual receipt')
        return actions[-1]
    async def stream(messages,info):
        calls.append(messages)
        if len(calls)==1:
            yield first
            yield {0:DeltaToolCall(name='actual_tool',json_args='{}',tool_call_id='mixed_action')}
        else:
            yield {0:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=final,tool_call_id='mixed_result')}
    original=pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai,'Agent',lambda *a,**k:original(*a,tools=[actual_tool],**k))
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**k:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner,'emit',events.append)
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used')
    request={'provider':'xai','model':'grok-4.7','effort':'high','instructions':'Exact owning authority','instruction':'Synthetic request',
        'mcp_servers':[],'history_enabled':False,'history_path':str(tmp_path/'history'),'history_scope':{},
        'session_id':'direct:fixture','run_id':'mixed','output_schema':conversation_output_schema(None,{'version':1,'audio_eligible':True})}
    asyncio.run(direct_runner.execute(request))
    assert actions==['actual receipt'] and len(calls)==2
    terminal=events[-1]
    assert json.loads(terminal['output'])['content']=='Independent answer 🙂\n\nActual receipt returned.'
    assert [r['disposition']['audio']for r in terminal['deliveryDispositionRanges']]==[first_voice,last_voice]
    public=_native_spoken_content('direct-xai','\n'.join(map(json.dumps,events)),None,{'version':1,'audio_eligible':True})
    assert public is not None and 'Independent answer' in public['text']


def test_output_tool_never_completes_after_coemitted_native_tool_failure(tmp_path,monkeypatch):
    import asyncio
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel,DeltaToolCall
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    events,attempts=[],[]
    async def actual_tool():
        attempts.append('attempted, not accepted')
        raise RuntimeError('Synthetic native failure')
    async def stream(messages,info):
        raw=json.dumps({'type':'assistant_response','tool_name':None,'voice':'eligible','content':'Independent answer.'})
        yield {0:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=raw,tool_call_id='failed_result'),
               1:DeltaToolCall(name='actual_tool',json_args='{}',tool_call_id='failed_action')}
    original=pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai,'Agent',lambda *a,**k:original(*a,tools=[actual_tool],**k))
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**k:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner,'emit',events.append)
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used')
    with pytest.raises(Exception):
        asyncio.run(direct_runner.execute({'provider':'xai','model':'grok-4.7','effort':'high','instructions':'Exact authority',
            'instruction':'Synthetic request','mcp_servers':[],'history_enabled':False,'history_path':str(tmp_path/'history'),
            'history_scope':{},'session_id':'direct:fixture','run_id':'failed',
            'output_schema':conversation_output_schema(None,{'version':1,'audio_eligible':True})}))
    assert attempts==['attempted, not accepted']
    assert not any(event['type']=='grok.result'for event in events)
    assert not any(event.get('update',{}).get('status')=='completed'for event in events)


def test_reserved_output_tool_cannot_alias_an_ordinary_capability(tmp_path,monkeypatch):
    import asyncio
    import pydantic_ai
    from pydantic_ai import Tool
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    called=[]
    async def ordinary_action():called.append('must not run')
    async def stream(messages,info):
        called.append('must not reach provider')
        yield 'Not permitted'
    original=pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai,'Agent',lambda *a,**k:original(*a,tools=[Tool(ordinary_action,name=direct_runner.OUTPUT_TOOL_NAME)],**k))
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**k:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner,'emit',lambda event:None)
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used')
    with pytest.raises(UserError):
        asyncio.run(direct_runner.execute({'provider':'xai','model':'grok-4.7','effort':'high','instructions':'Exact authority',
            'instruction':'Synthetic request','mcp_servers':[],'history_enabled':False,'history_path':str(tmp_path/'history'),
            'history_scope':{},'session_id':'direct:fixture','run_id':'collision',
            'output_schema':conversation_output_schema(None,{'version':1,'audio_eligible':True})}))
    assert called==[]


@pytest.mark.parametrize('case',['exact','changed_content','changed_voice','changed_format','key_order','unicode_escape','extension','tool_gap','tool_started_before_text','ordinary_repeat','incomplete'])
def test_reserved_final_tool_confirms_only_exact_adjacent_complete_public_text(tmp_path,monkeypatch,case):
    import asyncio
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel,DeltaToolCall
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    first={'type':'assistant_response','tool_name':None,'voice':'eligible','content':'Shared prefix then old. 🙂'}
    old=json.dumps(first,separators=(',',':'),ensure_ascii=False)
    new={**first}
    if case=='changed_content':new['content']='Shared prefix then new.'
    if case=='changed_voice':new['voice']='skip'
    if case=='extension':new['content']+=' Added new information.'
    raw=json.dumps(new,separators=(',',':'),ensure_ascii=False)
    if case=='changed_format':raw=json.dumps(new,indent=2,ensure_ascii=False)
    if case=='key_order':raw=json.dumps({key:new[key]for key in ['voice','tool_name','type','content']},ensure_ascii=False)
    if case=='unicode_escape':raw=json.dumps(new,separators=(',',':'),ensure_ascii=True)
    confirms=case in {'exact','changed_format','key_order','unicode_escape','ordinary_repeat'}
    if case=='incomplete':raw=raw[:-1]
    calls,actions,events=[],[],[]
    async def actual_tool():
        actions.append('real receipt')
        return actions[-1]
    def generations():
        return {event['meta']['streamStartMs']for event in events if event.get('update',{}).get('sessionUpdate')=='agent_message_chunk'}
    async def stream(messages,info):
        calls.append(messages)
        if len(calls)==1:
            if case=='tool_started_before_text':
                yield {0:DeltaToolCall(name='actual_tool',json_args='{}',tool_call_id='early_gap_action')}
            yield old
            if case=='tool_started_before_text':return
            if case in {'tool_gap','ordinary_repeat'}:
                yield {0:DeltaToolCall(name='actual_tool',json_args='{}',tool_call_id='gap_action')}
                return
        if case=='ordinary_repeat'and len(calls)==2:yield old
        common=0
        while common<min(len(old),len(raw))and old[common]==raw[common]:common+=1
        cut=max(1,common//2)
        yield {0:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=raw[:cut],tool_call_id='terminal_exact')}
        if case not in {'tool_gap','tool_started_before_text','ordinary_repeat'}:assert len(generations())==1
        # A matching prefix alone creates no duplicate generation. The first
        # divergent byte flushes immediately, before the terminal object closes.
        if common<len(raw):
            yield {0:DeltaToolCall(json_args=raw[cut:common+1])}
            if not confirms and case not in {'incomplete','changed_voice'}:assert len(generations())==2
            if case=='changed_voice':
                # The voice value must be complete before the existing owner
                # can release a typed content prefix. Then divergence flushes
                # before the terminal object completes.
                boundary=raw.index('\"content\":\"')+len('\"content\":\"')+len('Shared')
                yield {0:DeltaToolCall(json_args=raw[common+1:boundary])}
                assert len(generations())==2
                yield {0:DeltaToolCall(json_args=raw[boundary:])}
            else:yield {0:DeltaToolCall(json_args=raw[common+1:])}
        else:yield {0:DeltaToolCall(json_args=raw[cut:])}
    original=pydantic_ai.Agent
    monkeypatch.setattr(pydantic_ai,'Agent',lambda *a,**k:original(*a,tools=[actual_tool],**k))
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**k:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner,'emit',events.append)
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used')
    request={'provider':'xai','model':'grok-4.7','effort':'high','instructions':'Exact authority','instruction':'Synthetic request',
        'mcp_servers':[],'history_enabled':False,'history_path':str(tmp_path/'history'),'history_scope':{},
        'session_id':'direct:fixture','run_id':'echo','output_schema':conversation_output_schema(None,{'version':1,'audio_eligible':True})}
    if case=='incomplete':
        with pytest.raises(Exception):asyncio.run(direct_runner.execute(request))
        assert not any(event['type']=='grok.result'for event in events)
        assert len(generations())==1
    else:
        asyncio.run(direct_runner.execute(request))
        expected=first['content']if confirms and case!='ordinary_repeat'else first['content']+'\n\n'+new['content']
        assert json.loads(events[-1]['output'])['content']==expected
        assert len(generations())==(1 if confirms and case!='ordinary_repeat'else 2)
        completions=[event for event in events if event.get('update',{}).get('sessionUpdate')=='response_completed']
        assert len(completions)==(1 if confirms and case!='ordinary_repeat'else 2)
        if case=='changed_voice':assert [r['disposition']['audio']for r in events[-1]['deliveryDispositionRanges']]==['eligible','skip']
    assert actions==(['real receipt']if case in {'tool_gap','tool_started_before_text','ordinary_repeat'}else [])
    raw_events=[event for event in events if event.get('stage') in {'sdk_first_raw_text','sdk_first_raw_output_arguments'}]
    assert raw_events==[{'type':'direct.stage','stage':'sdk_first_raw_text','part_kind':'text'},
        {'type':'direct.stage','stage':'sdk_first_raw_output_arguments','part_kind':'output_tool_arguments'}]
    assert all(set(event)=={'type','stage','part_kind'}for event in raw_events)  # no prompt/text/auth content


@pytest.mark.parametrize('constraint',['disables_thinking','with_thinking_unsupported','no_native_schema'])
def test_output_mode_preserves_actual_request_thinking_or_fails_unsupported(tmp_path,monkeypatch,constraint):
    import asyncio
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models import anthropic as model_module
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    profile={'supports_forced_tool_choice':True,'supports_json_schema_output':constraint!='no_native_schema',
        'supports_forced_tool_choice_with_thinking':constraint!='with_thinking_unsupported',
        'forced_tool_choice_disables_thinking':constraint in {'disables_thinking','no_native_schema'},
        'anthropic_supports_adaptive_thinking':True}
    requests=[]
    async def stream(messages,info):
        requests.append(info.model_request_parameters)
        assert info.model_request_parameters.output_mode=='native'
        yield json.dumps({'type':'assistant_response','tool_name':None,'voice':'skip','content':'Preserved thinking.'})
    monkeypatch.setattr(model_module,'AnthropicModel',lambda *a,**k:FunctionModel(stream_function=stream,profile=profile))
    monkeypatch.setattr(direct_runner,'emit',lambda event:None)
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-not-used')
    request={'provider':'anthropic','model':'synthetic-capability-model','effort':'high','instructions':'Exact authority',
        'instruction':'Synthetic request','mcp_servers':[],'history_enabled':False,'history_path':str(tmp_path/'history'),
        'history_scope':{},'session_id':'direct:fixture','run_id':'thinking',
        'output_schema':conversation_output_schema(None,{'version':1,'audio_eligible':True})}
    if constraint=='no_native_schema':
        with pytest.raises(ValueError,match='cannot preserve'):asyncio.run(direct_runner.execute(request))
        assert requests==[]
    else:
        asyncio.run(direct_runner.execute(request))
        assert len(requests)==1


@pytest.mark.parametrize('change',[{}, {'tool_name':'lc_transfer_to_recipient_b'}, {'voice':'skip'}, {'type':'assistant_response','tool_name':None}, {'content':'Transfer  context.'}])
def test_semantic_confirmation_preserves_graph_identity_and_actual_content(change):
    from workers_projects_runtime.direct_runner import _could_confirm_public_envelope
    graph={'version':1,'tools':[{'name':'lc_transfer_to_recipient_a'},{'name':'lc_transfer_to_recipient_b'}]}
    delivery={'version':1,'audio_eligible':True}
    previous={'type':'tool_call','tool_name':'lc_transfer_to_recipient_a','voice':'eligible','content':'Transfer context.'}
    current={**previous,**change}
    raw=json.dumps({key:current[key]for key in ['voice','tool_name','type','content']},indent=2)
    for end in range(len(raw)):
        assert isinstance(_could_confirm_public_envelope(raw[:end],previous,graph,delivery),bool)
    assert _could_confirm_public_envelope(raw,previous,graph,delivery)==(not change)


def test_semantic_confirmation_handles_every_incremental_json_close_without_raising():
    from workers_projects_runtime.direct_runner import _could_confirm_public_envelope
    previous={'type':'assistant_response','tool_name':None,'voice':'eligible','content':'A brace } and an emoji 🙂.'}
    raw=json.dumps(previous,ensure_ascii=True,indent=2)
    for end in range(len(raw)+1):
        assert _could_confirm_public_envelope(raw[:end],previous,None,{'version':1,'audio_eligible':True})
    raw=json.dumps({**previous,'content':'A brace } and NEW content.'},ensure_ascii=True)
    assert not _could_confirm_public_envelope(raw,previous,None,{'version':1,'audio_eligible':True})


@pytest.mark.parametrize('voice',['eligible','skip'])
def test_semantic_confirmation_cannot_hide_sdk_graph_continuation(tmp_path,monkeypatch,voice):
    import asyncio
    import pydantic_ai
    from pydantic_ai.models.function import FunctionModel,DeltaToolCall
    from pydantic_ai.models import openai as openai_model
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import conversation_output_schema
    graph={'version':1,'tools':[{'name':'lc_transfer_to_recipient_a'},{'name':'lc_transfer_to_recipient_b'}]}
    delivery={'version':1,'audio_eligible':True}
    first={'type':'assistant_response','tool_name':None,'voice':'eligible','content':'Independent answer.'}
    terminal={'type':'tool_call','tool_name':'lc_transfer_to_recipient_b','voice':voice,'content':'Transfer context.'}
    events=[]
    async def stream(messages,info):
        yield json.dumps(first)
        yield {0:DeltaToolCall(name=direct_runner.OUTPUT_TOOL_NAME,json_args=json.dumps(terminal),tool_call_id='graph_result')}
    monkeypatch.setattr(openai_model,'OpenAIResponsesModel',lambda *a,**k:FunctionModel(stream_function=stream))
    monkeypatch.setattr(direct_runner,'emit',events.append)
    monkeypatch.setenv('XAI_API_KEY','synthetic-not-used')
    asyncio.run(direct_runner.execute({'provider':'xai','model':'grok-4.7','effort':'high','instructions':'Exact authority',
        'instruction':'Synthetic request','mcp_servers':[],'history_enabled':False,'history_path':str(tmp_path/'history'),
        'history_scope':{},'session_id':'direct:fixture','run_id':'graph',
        'output_schema':conversation_output_schema(graph,delivery),'graph_control':graph,'delivery_control':delivery}))
    actual=json.loads(events[-1]['output'])
    assert actual['type']=='tool_call'and actual['tool_name']=='lc_transfer_to_recipient_b'and actual['voice']==voice
    assert actual['content']=='Independent answer.\n\nTransfer context.'
    assert events[-1]['deliveryDispositionRanges'][-1]['disposition']['audio']==voice


@pytest.mark.parametrize("profile", list(DIRECT_PROVIDERS))
def test_private_json_contract_reaches_existing_sdk_typed_output_without_public_envelope(tmp_path, profile):
    from workers_projects_runtime.agent_builder_control import PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT
    runtime = DirectModelRuntime(profile, base_dir=str(tmp_path / "state"))
    w = worker(tmp_path, profile, access_mode="read_only", provider_capabilities={
        "native_tools": False, "response_contract": PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT},
        env={"GLASSHIVE_PROVIDER_SESSION_MODE": "stateless"}, developer_instructions="Exact internal authority")
    info = runtime.prepare_worker_workspace(w)
    request = runtime._request(w, "Return the complete internal object.", info)
    assert request["output_schema"] == {"type": "object", "additionalProperties": True}
    assert request["response_contract"] == PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT
    assert request["graph_control"] is None and request["delivery_control"] is None
    assert not request["history_enabled"] and request["mcp_servers"] == []
    raw = json.dumps({"summary": "Keep all constraints", "label": "FINAL REPORT"})
    assert runtime._select_native_agent_output(w, info, [raw]) == raw
    assert runtime._select_native_agent_output(w, info, [raw + "\nFINAL REPORT\nUnexpected ACK"]) == raw + "\nFINAL REPORT\nUnexpected ACK"


@pytest.mark.parametrize("change", ["native_tools", "access", "stateful", "boolean_version", "extra_contract"])
def test_private_json_contract_never_widens_its_existing_authority(tmp_path, change):
    from workers_projects_runtime.agent_builder_control import PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT
    contract = dict(PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT)
    bundle = {"access_mode": "read_only", "provider_capabilities": {"native_tools": False, "response_contract": contract},
        "env": {"GLASSHIVE_PROVIDER_SESSION_MODE": "stateless"}}
    if change == "native_tools": bundle["provider_capabilities"]["native_tools"] = True
    elif change == "access": bundle["access_mode"] = "full"
    elif change == "stateful": bundle["env"] = {}
    elif change == "boolean_version": contract["version"] = True
    else: contract["extra"] = "public"
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    w = worker(tmp_path, **bundle)
    if change in {"boolean_version", "extra_contract"}:
        assert runtime._conversation_output_schema(w) is None
    else:
        with pytest.raises(RuntimeConfigurationError, match="isolated zero-tool authority"):
            runtime._conversation_output_schema(w)


@pytest.mark.parametrize("private_unsupported", [False, True])
def test_private_json_existing_sdk_output_tool_preserves_object_instead_of_final_ack(tmp_path, monkeypatch, private_unsupported):
    import asyncio
    import pydantic_ai
    from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
    from pydantic_ai.models.function import FunctionModel, DeltaToolCall
    from workers_projects_runtime import direct_runner
    payload = {"version": 1, "summary": "Keep the exact unfinished condition.", "pendingAsks": ["Wait for owner input."], "nested": {"label": "FINAL REPORT"}}
    def respond(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[TextPart("Internal summary returned."), ToolCallPart(info.output_tools[0].name, payload)])
        return ModelResponse(parts=[TextPart(json.dumps(payload))])
    async def streamed(messages, info):
        if info.output_tools:
            yield "Internal summary returned."
            yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=json.dumps(payload), tool_call_id="private-output")}
        else:
            yield json.dumps(payload)
    RealAgent = pydantic_ai.Agent
    class InternalAgent(RealAgent):
        def __init__(self, model, **kwargs):
            super().__init__(FunctionModel(respond, stream_function=streamed), **kwargs)
    if private_unsupported:
        from pydantic_ai.models import openai as openai_model
        original_model = openai_model.OpenAIResponsesModel
        def unsupported_native(*args, **kwargs):
            kwargs["profile"] = {**kwargs["profile"], "supports_json_schema_output": False}
            return original_model(*args, **kwargs)
        monkeypatch.setattr(openai_model, "OpenAIResponsesModel", unsupported_native)
    captured = []
    monkeypatch.setattr(pydantic_ai, "Agent", InternalAgent)
    monkeypatch.setattr(direct_runner, "emit", captured.append)
    monkeypatch.setenv("XAI_API_KEY", "synthetic-not-used")
    asyncio.run(direct_runner.execute({"provider": "xai", "model": "grok-4.7", "effort": "low",
        "history_path": str(tmp_path / 'history.json'), "history_scope": {"worker": "fixture"},
        "history_enabled": False, "session_id": "private:fixture", "run_id": "internal-exact",
        "mcp_servers": [], "instructions": "Internal authority", "instruction": "Return the internal object.",
        "output_schema": {"type": "object", "additionalProperties": True}, "graph_control": None, "delivery_control": None,
        **({"response_contract": {"version": 1, "visibility": "private_machine_state", "format": "json_object"}} if private_unsupported else {})}))
    assert json.loads(captured[-1]["output"]) == payload
    assert captured[-1]["type"] == "grok.result"
    assert "Internal summary returned." not in captured[-1]["output"]


def _private_json_sdk_http_exchange(tmp_path, monkeypatch, raw, private_contract):
    import asyncio
    import importlib
    import openai
    from workers_projects_runtime import direct_runner
    from workers_projects_runtime.agent_builder_control import PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT
    original = openai.DefaultAsyncHttpxClient
    httpx = importlib.import_module("httpx2" if any(cls.__module__.startswith("httpx2")
        for cls in original.__mro__) else "httpx")
    wire, events = [], []
    def handler(request):
        wire.append(json.loads(request.content))
        message = {"type": "message", "id": "msg-synthetic", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": raw, "annotations": []}]}
        response = {"id": "resp-synthetic", "object": "response", "created_at": 1, "model": "grok-4.7",
            "status": "completed", "output": [message], "usage": {"input_tokens": 10, "output_tokens": 5,
                "total_tokens": 15, "output_tokens_details": {"reasoning_tokens": 0}}}
        sequence = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
            {"type": "response.content_part.added", "output_index": 0, "item_id": message["id"], "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", "output_index": 0, "item_id": message["id"], "content_index": 0, "delta": raw},
            {"type": "response.output_text.done", "output_index": 0, "item_id": message["id"], "content_index": 0, "text": raw},
            {"type": "response.content_part.done", "output_index": 0, "item_id": message["id"], "content_index": 0, "part": message["content"][0]},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": response},
        ]
        body = "".join("event: " + item["type"] + "\ndata: " + json.dumps({**item, "sequence_number": index}) + "\n\n"
            for index, item in enumerate(sequence))
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", MockClient)
    monkeypatch.setenv("XAI_API_KEY", "synthetic-not-used")
    monkeypatch.setattr(direct_runner, "emit", events.append)
    request = {"provider": "xai", "model": "grok-4.7", "effort": "high", "instructions": "Internal authority.",
        "instruction": "Return the internal object.", "history_path": str(tmp_path / 'history.json'),
        "history_scope": {"worker": "fixture"}, "history_enabled": False, "session_id": "private:fixture",
        "run_id": "internal-exact", "mcp_servers": [], "output_schema": {"type": "object", "additionalProperties": True},
        "graph_control": None, "delivery_control": None}
    if private_contract: request["response_contract"] = PRIVATE_MACHINE_STATE_RESPONSE_CONTRACT
    error = None
    try: asyncio.run(direct_runner.execute(request))
    except Exception as exc: error = exc
    return wire, events, error, direct_runner.safe_failure_shape(error) if error else None


@pytest.mark.parametrize("payload", [
    {"version": 1, "summary": "Keep exact unfinished wording.", "pendingAsks": ["Wait for owner input."]},
    {"approved": False, "reason": "The unfinished condition must remain exact."},
    {"version": 1, "summary": "Repaired exact unfinished wording.", "pendingAsks": ["Wait for owner input."]},
], ids=["producer", "fidelity-review", "repair-producer"])
def test_private_json_official_sdk_native_format_accepts_same_object_rejected_as_output_tool(tmp_path, monkeypatch, payload):
    raw = json.dumps(payload)
    with monkeypatch.context() as previous:
        old_wire, _, old_error, old_shape = _private_json_sdk_http_exchange(tmp_path, previous, raw, False)
    assert type(old_error).__name__ == "UnexpectedModelBehavior"
    assert old_wire[0]["tool_choice"] == "required" and not old_wire[0].get("text")
    assert old_shape["output_mode"] == "tool" and old_shape["response_shape"]["text_parts"] == 1
    assert old_shape["response_shape"]["output_tool_calls"] == 0
    with monkeypatch.context() as proposed:
        wire, events, error, _ = _private_json_sdk_http_exchange(tmp_path, proposed, raw, True)
    assert error is None, type(error).__name__
    assert len(wire) == 1 and wire[0]["text"]["format"]["type"] == "json_schema"
    assert wire[0]["text"]["format"]["schema"]["additionalProperties"] is True
    assert not wire[0].get("tools") and wire[0]["reasoning"]["effort"] == "high"
    assert json.loads(events[-1]["output"]) == payload


@pytest.mark.parametrize("raw", ['{"summary":"Keep source."}\nUnexpected acknowledgement', '{"summary":"first","summary":"second"}'])
def test_private_json_native_format_still_rejects_invalid_whole_objects_without_raw_diagnostics(tmp_path, monkeypatch, raw):
    with monkeypatch.context() as fixture:
        wire, _, error, shape = _private_json_sdk_http_exchange(tmp_path, fixture, raw, True)
    assert error is not None and wire[0]["text"]["format"]["type"] == "json_schema"
    assert shape["output_mode"] == "native"
    assert "Unexpected acknowledgement" not in json.dumps(shape)
    assert "Keep source" not in json.dumps(shape)
    assert set(shape) == {"output_mode", "response_shape", "cause_types"}


def test_direct_failure_diagnostics_reset_before_early_configuration_in_a_reused_process(tmp_path, monkeypatch):
    import asyncio
    from workers_projects_runtime import direct_runner
    direct_runner.CURRENT_OUTPUT_MODE = "native"
    direct_runner.LAST_RESPONSE_SHAPE = {"parts": 8, "finish_reason": "length"}
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(ValueError) as error:
        asyncio.run(direct_runner.execute({"provider": "xai", "model": "grok-4.7"}))
    shape = direct_runner.safe_failure_shape(error.value)
    assert shape["output_mode"] == "plain" and shape["response_shape"] == {}
    assert shape["cause_types"] == ["ValueError"]


def test_direct_403_request_refusal_does_not_assert_missing_credentials(tmp_path):
    runtime = DirectModelRuntime("direct-xai", base_dir=str(tmp_path / "state"))
    failure, _ = runtime._provider_failure_for_process(
        worker=worker(tmp_path), run_id="refused-request",
        stdout=json.dumps({"type": "direct.error", "status_code": 403,
            "error_type": "ModelHTTPError", "stage": "provider_request",
            "cause_types": ["ModelHTTPError", "PermissionDeniedError"]}),
        stderr="", exit_code=1)
    assert failure.failure_class == "provider_request_rejected"
    assert failure.structured and not failure.retryable
    assert "reconnect" not in failure.user_message.lower()
    assert "credential" not in failure.user_message.lower()
    assert "accepted work" in failure.recommended_recovery.lower()


def test_direct_sdk_tool_round_keeps_billing_sum_and_last_request_context_separate(tmp_path, monkeypatch):
    import asyncio
    import importlib
    import openai
    import pydantic_ai
    from workers_projects_runtime import direct_runner

    original_http = openai.DefaultAsyncHttpxClient
    httpx = importlib.import_module("httpx2" if any(cls.__module__.startswith("httpx2")
        for cls in original_http.__mro__) else "httpx")
    wire, events, effects = [], [], []
    async def read_fact():
        return {"code": "SC42", "quantity": 17}
    async def record_marker():
        effects.append("R1")
        return {"receipt": "R1"}
    def handler(request):
        body = json.loads(request.content)
        wire.append(body)
        number = len(wire)
        assert number <= 2
        if number == 1:
            output = [{"type": "function_call", "id": "fc_" + name,
                "call_id": "call_" + name, "name": name, "arguments": "{}", "status": "completed"}
                for name in ("read_fact", "record_marker")]
        else:
            output = [{"type": "message", "id": "msg_final", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "SC42, 17, R1.", "annotations": []}]}]
        response = {"id": f"resp_{number}", "object": "response", "created_at": number,
            "model": "grok-4.7", "status": "completed", "output": output,
            "usage": {"input_tokens": 180000 if number == 1 else 185000, "output_tokens": 9,
                "total_tokens": (180000 if number == 1 else 185000) + 9,
                "output_tokens_details": {"reasoning_tokens": 0}}}
        sequence = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}]
        for index, item in enumerate(output):
            sequence.append({"type": "response.output_item.added", "output_index": index,
                "item": {**item, "arguments": ""} if item["type"] == "function_call" else {**item, "content": []}})
            if item["type"] == "function_call":
                sequence.append({"type": "response.function_call_arguments.delta", "output_index": index,
                    "item_id": item["id"], "delta": "{}"})
            else:
                sequence.extend([
                    {"type": "response.content_part.added", "output_index": index, "item_id": item["id"],
                        "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "output_index": index, "item_id": item["id"],
                        "content_index": 0, "delta": item["content"][0]["text"]},
                ])
            sequence.append({"type": "response.output_item.done", "output_index": index, "item": item})
        sequence.append({"type": "response.completed", "response": response})
        stream = "".join("event: " + item["type"] + "\ndata: " + json.dumps({**item, "sequence_number": index}) + "\n\n"
            for index, item in enumerate(sequence))
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
    class MockClient(original_http):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})
    original_agent = pydantic_ai.Agent
    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", MockClient)
    monkeypatch.setattr(pydantic_ai, "Agent", lambda *a, **k: original_agent(*a, tools=[read_fact, record_marker], **k))
    monkeypatch.setattr(direct_runner, "emit", events.append)
    monkeypatch.setenv("XAI_API_KEY", "synthetic-not-used")
    asyncio.run(direct_runner.execute({"provider": "xai", "model": "grok-4.7", "effort": "high",
        "instructions": "Exact synthetic authority.", "instruction": "Read the fact and record one marker.",
        "history_path": str(tmp_path / "history.json"), "history_scope": {"worker": "fixture"},
        "history_enabled": True, "session_id": "direct:fixture", "run_id": "usage-fixture", "mcp_servers": []}))
    terminal = events[-1]
    assert terminal["type"] == "grok.result" and terminal["output"] == "SC42, 17, R1."
    assert effects == ["R1"] and len(wire) == 2
    assert "previous_response_id" not in wire[1]
    assert "Read the fact and record one marker." in json.dumps(wire[1]["input"])
    assert {item.get("call_id") for item in wire[1]["input"] if item.get("type") == "function_call_output"} == {
        "call_read_fact", "call_record_marker"}
    assert terminal["usage"]["input_tokens"] == 365000
    assert terminal["context_usage"] == {"source": "sdk_last_response", "input_tokens": 185000, "output_tokens": 9}
