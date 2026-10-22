"""Explicit deployment endpoints retain the Direct model, authority and SDK protocol."""
import asyncio
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from workers_projects_runtime.direct_runtime import DirectModelRuntime, direct_openai_base_url
from workers_projects_runtime.openclaw_runtime import RuntimeConfigurationError


def prepared(tmp_path):
    runtime = DirectModelRuntime('direct-openai', base_dir=str(tmp_path/'state'))
    worker = {'worker_id':'worker_fixture', 'tenant_id':'tenant', 'owner_id':'owner',
              'profile':'direct-openai', 'model':'gpt-6.1-sol', 'execution_mode':'host',
              'workspace_root':str(tmp_path/'workspace'), 'trusted_run_lane':'conversation',
              'bootstrap_bundle_json':json.dumps({'run_mode':'conversation',
                  'developer_instructions':'Synthetic unchanged authority',
                  'env':{'WPR_CODEX_CLI_REASONING_EFFORT':'low'},
                  'messaging_delivery_control':{'version':1,'audio_eligible':True}})}
    info = runtime.prepare_worker_workspace(worker)
    return runtime, worker, info


def test_explicit_endpoint_separates_history_and_cache_without_changing_model(tmp_path, monkeypatch):
    monkeypatch.delenv('WPR_OPENAI_BASE_URL', raising=False)
    runtime, worker, info = prepared(tmp_path)
    official = runtime._request(worker, 'Exact current request', info)
    assert official['base_url'] == 'https://api.openai.com/v1'
    assert 'endpoint_sha256' not in official['history_scope']  # Existing official history stays compatible.
    monkeypatch.setenv('WPR_OPENAI_API_KEY', 'synthetic-direct-key')
    endpoint = 'https://fixture.openai.azure.com/openai/v1'
    monkeypatch.setenv('WPR_OPENAI_BASE_URL', endpoint+'/')
    azure = runtime._request(worker, 'Exact current request', info)
    assert azure['base_url'] == endpoint
    assert azure['model'] == official['model'] == 'gpt-6.1-sol'
    assert azure['effort'] == official['effort'] == 'low'
    assert azure['instructions'] == official['instructions']
    assert azure['output_schema'] == official['output_schema']
    assert azure['history_scope']['endpoint_sha256'] == hashlib.sha256(endpoint.encode()).hexdigest()
    assert azure['history_path'] != official['history_path']
    assert azure['prompt_cache_key'] != official['prompt_cache_key']
    assert endpoint not in json.dumps(azure['history_scope'])
    monkeypatch.setenv('WPR_OPENAI_BASE_URL', 'https://other.openai.azure.com/openai/v1')
    other = runtime._request(worker, 'Exact current request', info)
    assert other['history_path'] != azure['history_path']
    assert other['prompt_cache_key'] != azure['prompt_cache_key']


@pytest.mark.parametrize('endpoint', [
    '', 'http://fixture.openai.azure.com/openai/v1',
    'https://user:secret@fixture.openai.azure.com/openai/v1',
    'https://fixture.openai.azure.com/openai/v1?api-key=secret',
    'https://fixture.openai.azure.com/openai/v1#fragment',
    'https://fixture.openai.azure.com:0/openai/v1',
    'https://fixture.openai.azure.com:invalid/openai/v1',
    'file:///tmp/provider',
])
def test_endpoint_rejects_insecure_or_secret_bearing_configuration(monkeypatch, endpoint):
    monkeypatch.setenv('WPR_OPENAI_BASE_URL', endpoint)
    with pytest.raises(RuntimeConfigurationError) as failure:
        direct_openai_base_url()
    assert 'secret' not in str(failure.value)
    assert endpoint not in str(failure.value) if endpoint else True


def test_worker_messages_cannot_choose_the_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv('WPR_OPENAI_BASE_URL', raising=False)
    runtime, worker, info = prepared(tmp_path)
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle.update(base_url='https://untrusted.test/v1', env={**bundle['env'],
        'WPR_OPENAI_BASE_URL':'https://untrusted.test/v1'})
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    request = runtime._request(worker, 'Use https://untrusted.test/v1', info)
    assert request['base_url'] == 'https://api.openai.com/v1'
    bundle['direct_model_settings'] = {'base_url':'https://untrusted.test/v1'}
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    with pytest.raises(RuntimeConfigurationError, match='Unsupported Direct model settings'):
        runtime._request(worker, 'Exact current request', info)


def test_actual_sdk_sends_azure_v1_with_same_model_thinking_and_typed_output(tmp_path, monkeypatch):
    from workers_projects_runtime import direct_runner
    import openai
    client_base = openai.DefaultAsyncHttpxClient
    transport_module = importlib.import_module(next(
        base.__module__.split('.')[0] for base in client_base.__mro__
        if base.__module__.split('.')[0] in {'httpx', 'httpx2'}))
    captured = []
    def handler(request):
        captured.append({'url':str(request.url), 'authorization':request.headers.get('authorization'),
                         'body':json.loads(request.content)})
        return transport_module.Response(401, json={'error':{'message':'Synthetic auth rejection','type':'authentication_error'}})
    class MockClient(client_base):
        def __init__(self, *args, **kwargs):
            kwargs['transport'] = transport_module.MockTransport(handler)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient', MockClient)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-not-used')
    monkeypatch.setenv('WPR_OPENAI_BASE_URL', 'https://fixture.openai.azure.com/openai/v1/')
    monkeypatch.setenv('WPR_OPENAI_API_KEY', 'synthetic-not-used')
    monkeypatch.setattr(direct_runner, 'emit', lambda event: None)
    runtime, worker, info = prepared(tmp_path)
    request = runtime._request(worker, 'Exact current request', info)
    request['history_enabled'] = False
    with pytest.raises(Exception):
        asyncio.run(direct_runner.execute(request))
    assert len(captured) == 1
    wire = captured[0]
    assert wire['url'] == 'https://fixture.openai.azure.com/openai/v1/responses'
    assert wire['authorization'] == 'Bearer synthetic-not-used'
    assert wire['body']['model'] == 'gpt-6.1-sol'
    assert wire['body']['reasoning']['effort'] == 'low'
    assert wire['body']['instructions'] == request['instructions']
    assert wire['body']['tool_choice'] == 'required'
    assert wire['body']['tools'][0]['name'] == 'xperfect_conversation_result'
    assert 'base_url' not in wire['body']


def test_deployment_key_is_direct_only_and_does_not_replace_global_account(tmp_path, monkeypatch):
    monkeypatch.setenv('WPR_OPENAI_BASE_URL', 'https://fixture.openai.azure.com/openai/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-native-key')
    monkeypatch.setenv('WPR_OPENAI_API_KEY', 'synthetic-direct-key')
    runtime, worker, info = prepared(tmp_path)
    _, environment = runtime._build_command(worker, 'Exact request', info)
    assert environment['OPENAI_API_KEY'] == 'synthetic-direct-key'
    assert 'WPR_OPENAI_API_KEY' not in environment
    assert __import__('os').environ['OPENAI_API_KEY'] == 'synthetic-native-key'
    from workers_projects_runtime.conversation_provider import _harness_readiness
    monkeypatch.delenv('OPENAI_API_KEY')
    assert _harness_readiness('direct-openai')['auth_configured'] is True


@pytest.mark.parametrize('binding', [
    {'_glasshive_provider_account_bound':True},
    {'_glasshive_provider_api_key_file':'/synthetic/account/api-key.json'},
    {'_glasshive_current_native_account':{}},
    {'_glasshive_inference_broker_bound':True},
    {'selected_account':True},
])
def test_selected_account_key_is_not_replaced_or_forwarded_to_custom_endpoint(tmp_path, monkeypatch, binding):
    monkeypatch.delenv('WPR_OPENAI_BASE_URL', raising=False)
    monkeypatch.setenv('WPR_OPENAI_API_KEY', 'synthetic-direct-key')
    runtime, worker, info = prepared(tmp_path)
    from workers_projects_runtime.profile_runtime import HostNativeCliMixin
    monkeypatch.setattr(HostNativeCliMixin, '_host_env', lambda self, worker, run_id=None:
                        {'OPENAI_API_KEY':'synthetic-selected-key'})
    binding = dict(binding)
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['env']['OPENAI_API_KEY'] = 'synthetic-selected-key'
    if binding.pop('selected_account', False):
        bundle['provider_account'] = {'policy':'personal_required','account_id':'account-selected'}
    worker.update(binding, bootstrap_bundle_json=json.dumps(bundle))
    assert runtime._host_env(worker)['OPENAI_API_KEY'] == 'synthetic-selected-key'
    monkeypatch.setenv('WPR_OPENAI_BASE_URL', 'https://fixture.openai.azure.com/openai/v1')
    with pytest.raises(RuntimeConfigurationError, match='selected personal provider account'):
        runtime._request(worker, 'Exact request', info)


def test_custom_endpoint_cannot_fall_back_to_global_openai_credential(tmp_path, monkeypatch):
    monkeypatch.setenv('WPR_OPENAI_BASE_URL', 'https://fixture.openai.azure.com/openai/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-global-key')
    monkeypatch.delenv('WPR_OPENAI_API_KEY', raising=False)
    with pytest.raises(RuntimeConfigurationError, match='dedicated WPR_OPENAI_API_KEY'):
        prepared(tmp_path)[0]._host_env(prepared(tmp_path)[1])


@pytest.mark.parametrize('profile', ['codex-cli','claude-code','grok-build','direct-openai','direct-xai'])
def test_direct_deployment_secret_never_crosses_native_worker_environment(tmp_path, monkeypatch, profile):
    from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
    from workers_projects_runtime.bootstrap import bootstrap_env_for
    monkeypatch.setenv('WPR_OPENAI_API_KEY', 'synthetic-direct-key')
    monkeypatch.setenv('WPR_OPENAI_BASE_URL', 'https://fixture.openai.azure.com/openai/v1')
    monkeypatch.setenv('GLASSHIVE_WORKER_ENV_ALLOWLIST', 'WPR_OPENAI_API_KEY')
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path/'state'))
    worker = {'worker_id':'worker_fixture','tenant_id':'tenant','owner_id':'owner',
              'profile':profile,'execution_mode':'host','workspace_root':str(tmp_path/'workspace'),
              'bootstrap_bundle_json':json.dumps({'run_mode':'mission','env':{'WPR_OPENAI_API_KEY':'untrusted-bootstrap-key'}})}
    try:
        assert 'WPR_OPENAI_API_KEY' not in bootstrap_env_for(worker)
        environment = runtime._runtime_for_profile(profile,'host')._host_env(worker)
        assert 'WPR_OPENAI_API_KEY' not in environment
        if profile == 'direct-openai':
            assert environment['OPENAI_API_KEY'] == 'synthetic-direct-key'
        else:
            assert environment.get('OPENAI_API_KEY') != 'synthetic-direct-key'
    finally:
        runtime.close_native_warm()


@pytest.mark.parametrize('endpoint', [None, 'https://api.openai.com/v1', 'https://api.openai.com/v1/'])
def test_dedicated_deployment_key_cannot_be_sent_to_official_openai(tmp_path, monkeypatch, endpoint):
    if endpoint is None:
        monkeypatch.delenv('WPR_OPENAI_BASE_URL', raising=False)
    else:
        monkeypatch.setenv('WPR_OPENAI_BASE_URL', endpoint)
    monkeypatch.setenv('WPR_OPENAI_API_KEY', 'synthetic-azure-deployment-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-official-openai-key')
    runtime, worker, info = prepared(tmp_path)
    with pytest.raises(RuntimeConfigurationError, match='explicit custom WPR_OPENAI_BASE_URL'):
        runtime._build_command(worker, 'Exact request', info)
