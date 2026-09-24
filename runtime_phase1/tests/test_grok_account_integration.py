import json
from pathlib import Path
import pytest
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager
from workers_projects_runtime.mission_provider_accounts import apply_bound_provider_account_environment
from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
from workers_projects_runtime.bootstrap import bootstrap_env_for, CLEAN_ROOM_HOME_AUTHORITY_PATHS


def test_grok_account_uses_auth_path_without_sharing_session_home(tmp_path):
    homes=ProviderAccountHomeManager(tmp_path/'accounts')
    account=homes.ensure_home(tenant_id='tenant',owner_id='owner',account_id='account-1',provider='grok')
    env=homes.runtime_environment(provider='grok',account_home=account)
    assert env=={'GROK_AUTH_PATH':str(account/'grok/auth.json')}
    assert 'GROK_HOME' not in env
    worker={'profile':'grok-build','bootstrap_bundle_json':json.dumps({'provider_account':{'account_id':'account-1'}}),
            '_glasshive_provider_account_bound':True,'_glasshive_provider_account_env':env}
    projected=apply_bound_provider_account_environment(worker,{'XAI_API_KEY':'synthetic','GROK_HOME':'/private/worker'},runtime_name='grok-build')
    assert 'XAI_API_KEY' not in projected
    assert projected['GROK_HOME']=='/private/worker'
    assert projected['GROK_AUTH_PATH']==env['GROK_AUTH_PATH']


def test_unbound_grok_account_cannot_override_credential_path():
    worker={'bootstrap_bundle_json':json.dumps({'provider_account':{'account_id':'account-1'}}),
            '_glasshive_provider_account_env':{'GROK_AUTH_PATH':'/other-owner/auth.json'}}
    with pytest.raises(RuntimeErrorBase,match='not validated'):
        apply_bound_provider_account_environment(worker,{},runtime_name='grok-build')


def test_enterprise_xai_key_is_projected_only_to_grok(monkeypatch):
    monkeypatch.setenv('GLASSHIVE_ENTERPRISE_MODE','true')
    monkeypatch.setenv('GLASSHIVE_PROJECT_PROVIDER_ENV','false')
    bundle=json.dumps({'env':{'XAI_API_KEY':'synthetic','OPENAI_API_KEY':'other'}})
    assert bootstrap_env_for({'profile':'grok-build','bootstrap_bundle_json':bundle})=={'XAI_API_KEY':'synthetic'}
    assert 'XAI_API_KEY' not in bootstrap_env_for({'profile':'codex-cli','bootstrap_bundle_json':bundle})
    assert 'XAI_API_KEY' not in bootstrap_env_for({'profile':'claude-code','bootstrap_bundle_json':bundle})
    assert '.grok/auth.json' in CLEAN_ROOM_HOME_AUTHORITY_PATHS


def test_host_bound_grok_auth_preserves_worker_private_native_home(tmp_path,monkeypatch):
    runtime=HostGrokBuildRuntime(str(tmp_path/'runtime'))
    auth=tmp_path/'selected-account/grok/auth.json';auth.parent.mkdir(parents=True);auth.write_text('{}')
    worker={'worker_id':'worker-1','profile':'grok-build','execution_mode':'host','bootstrap_bundle_json':json.dumps({'provider_account':{'account_id':'account-1'}}),
            '_glasshive_provider_account_bound':True,'_glasshive_provider_account_env':{'GROK_AUTH_PATH':str(auth)}}
    monkeypatch.setattr(runtime,'_host_env',lambda _: {})
    env=runtime._native_environment(worker,host=True)
    assert env['GROK_AUTH_PATH']==str(auth)
    assert env['GROK_HOME']==str(runtime._grok_home(worker))
