from fastapi.testclient import TestClient
from glass_drive_ui.server import create_app
from test_server import FakeRuntimeClient


def test_native_key_methods_and_private_password_submission(monkeypatch):
    runtime=FakeRuntimeClient()
    runtime.health_response['native_api_key_support']={'codex':'supported','claude':'supported','grok':'setup_cli_required'}
    checked=[]
    def connect(account_id,value):
        checked.append((account_id,value=='synthetic-key'))
        return {'status':'ready','message':'Key accepted'}
    runtime.connect_provider_api_key=connect
    client=TestClient(create_app(runtime_client=runtime))
    options={item['provider']:item for item in client.get('/api/control-plane').json()['provider_options']}
    assert 'api_key' in options['claude']['methods']
    assert 'api_key' not in options['grok']['methods']
    created=client.post('/api/provider-accounts',json={'provider':'claude','label':'Native key','auth_method':'api_key'})
    assert created.status_code==200
    assert runtime.provider_account_requests[-1]['secret_locator']=='native-home://auto'
    response=client.post('/api/provider-accounts/acct_synthetic/credentials',json={'value':'synthetic-key'})
    assert response.status_code==200 and checked==[('acct_synthetic',True)]
    assert 'synthetic-key' not in response.text
    malformed=client.post('/api/provider-accounts/acct_synthetic/credentials',json={'value':{'key':'synthetic-key'}})
    assert malformed.status_code==422 and 'synthetic-key' not in malformed.text
