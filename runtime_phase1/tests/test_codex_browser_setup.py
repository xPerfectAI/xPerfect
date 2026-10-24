"""Named-account browser login reuses the native setup and verification path."""
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
import pytest
from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.provider_accounts import ProviderSetupManager, _provider_setup_guidance


def native_url(callback='http://127.0.0.1:1455/auth/callback'):
    return 'https://auth.openai.com/oauth/authorize?' + urlencode(dict(client_id='synthetic-client',
        response_type='code',redirect_uri=callback,code_challenge='synthetic-challenge',
        code_challenge_method='S256',state='synthetic-state'))


def local_host(monkeypatch):
    for key in ('XPERFECT_STORAGE_ROOT','XPERFECT_EXECUTION_PROFILE','GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION',
                'GLASSHIVE_ENTERPRISE_MODE','WPR_ENTERPRISE_MODE'):
        monkeypatch.delenv(key,raising=False)
    for key,value in dict(GLASSHIVE_SECURITY_MODE='local',GLASSHIVE_AUTH_MODE='local',
            WPR_DEFAULT_EXECUTION_MODE='host',GLASSHIVE_PUBLIC_BASE_URL='http://127.0.0.1:8780').items():
        monkeypatch.setenv(key,value)


def commands_manager(monkeypatch):
    manager=object.__new__(ProviderSetupManager)
    manager.homes=SimpleNamespace(native_launcher=None,owner_root_resolver=None)
    monkeypatch.setattr(manager,'_binary',lambda _: '/usr/local/bin/codex')
    return manager


def test_local_host_uses_browser_login_and_same_verifier(monkeypatch):
    local_host(monkeypatch)
    manager=commands_manager(monkeypatch)
    assert manager._commands('codex')==(['/usr/local/bin/codex','login'],['/usr/local/bin/codex','login','status'])
    assert manager._commands('openai')[0]==['/usr/local/bin/codex','login']
    assert manager._commands('codex',contained=True)[0][-1]=='--device-auth'


@pytest.mark.parametrize('key,value',[
    ('GLASSHIVE_SECURITY_MODE','multi_user'),('GLASSHIVE_AUTH_MODE','enterprise'),
    ('WPR_DEFAULT_EXECUTION_MODE','docker'),('GLASSHIVE_PUBLIC_BASE_URL','https://operator.example.invalid'),
    ('GLASSHIVE_PUBLIC_BASE_URL','http://user@127.0.0.1:8780'),
    ('GLASSHIVE_PUBLIC_BASE_URL','http://[invalid'),('GLASSHIVE_PUBLIC_BASE_URL',''),
    ('XPERFECT_EXECUTION_PROFILE','local-linux'),('XPERFECT_STORAGE_ROOT','/var/lib/synthetic-storage'),
    ('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION','per_worker_container'),
    ('GLASSHIVE_ENTERPRISE_MODE','true'),('WPR_ENTERPRISE_MODE','true')])
def test_other_deployments_keep_device_flow(monkeypatch,key,value):
    local_host(monkeypatch);monkeypatch.setenv(key,value)
    assert commands_manager(monkeypatch)._commands('codex')[0][-1]=='--device-auth'


@pytest.mark.parametrize('attribute,value',[('native_launcher',object()),('owner_root_resolver',lambda *_: Path('/synthetic'))])
def test_managed_accounts_never_use_host_callback(monkeypatch,attribute,value):
    local_host(monkeypatch);manager=commands_manager(monkeypatch);setattr(manager.homes,attribute,value)
    assert manager._commands('codex')[0][-1]=='--device-auth'


@pytest.mark.parametrize('callback',['http://127.0.0.1:1455/auth/callback','http://localhost:54321/auth/callback','http://[::1]:1455/auth/callback'])
def test_native_browser_url_has_no_device_code_or_security_detour(callback):
    url=native_url(callback);result=_provider_setup_guidance('codex','If browser did not open: '+url)
    assert result['setup_url']==url
    assert result['setup_flow']=='browser'
    assert result['setup_code']==result['help_url']==''
    assert result['input_required'] is False


@pytest.mark.parametrize('callback',['https://callback.example.invalid/auth/callback','http://127.0.0.1:1455/unreviewed',
    'http://user@127.0.0.1:1455/auth/callback','http://127.0.0.1:1455/auth/callback#fragment',
    'http://127.0.0.1:1455/auth/callback?extra=true','http://127.0.0.1:99999/auth/callback'])
def test_unreviewed_callback_not_offered(callback):
    result = _provider_setup_guidance('codex',native_url(callback))
    assert result['setup_url']=='' and result['setup_flow']=='unknown'


@pytest.mark.parametrize('before,after',[
    ('auth.openai.com','signin.example.invalid'),('https://','http://'),
    ('https://auth.openai.com','https://' + 'user@' + 'auth.openai.com'),
    ('https://auth.openai.com','https://auth.openai.com:444'),('/oauth/authorize?','/unreviewed?'),
    ('state=synthetic-state','state=synthetic-state&state=another'),
    ('code_challenge_method=S256','code_challenge_method=plain'),
    ('state=synthetic-state','state=synthetic-state#fragment')])
def test_only_reviewed_https_authorization_is_clickable(before,after):
    assert _provider_setup_guidance('codex',native_url().replace(before,after))['setup_url']==''


def test_browser_setup_preserves_private_home_lease_and_native_verification(tmp_path,monkeypatch):
    local_host(monkeypatch);monkeypatch.setenv('GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS','true')
    monkeypatch.setenv('OPENAI_API_KEY','must-not-reach-native-login');monkeypatch.setenv('WPR_API_TOKEN','must-not-reach-native-login')
    capture=tmp_path/'capture.json';cli=tmp_path/'synthetic-codex'
    cli.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
home=Path(os.environ['CODEX_HOME'])
if sys.argv[1:]==['login']:
    home.mkdir(parents=True,exist_ok=True)
    (home/'auth.json').write_text('{"synthetic":true}')
    Path(__CAPTURE__).write_text(json.dumps({'HOME':os.environ.get('HOME'),'CODEX_HOME':str(home),'OPENAI_API_KEY':os.environ.get('OPENAI_API_KEY'),'WPR_API_TOKEN':os.environ.get('WPR_API_TOKEN')}))
    print(__URL__,flush=True)
    raise SystemExit(0)
if sys.argv[1:]==['login','status']:
    raise SystemExit(0 if (home/'auth.json').exists() else 1)
raise SystemExit(2)
'''.replace('__CAPTURE__',repr(str(capture))).replace('__URL__',repr(native_url())))
    cli.chmod(0o700);monkeypatch.setenv('WPR_CODEX_CLI_PATH',str(cli))
    store=ControlPlaneStore(str(tmp_path/'runtime.db'))
    account=store.create_provider_account(tenant_id='tenant-a',owner_id='user-a',provider='codex',label='Personal Codex',auth_method='subscription',platform_support='supported',secret_locator='native-home://auto')
    manager=ProviderSetupManager(store=store,home_root=tmp_path/'accounts')
    try:
        result=manager.start(account_id=account['account_id'],tenant_id='tenant-a',owner_id='user-a');until=time.monotonic()+5
        while not result['complete'] and time.monotonic()<until:
            time.sleep(.02);result=manager.status(account_id=account['account_id'],tenant_id='tenant-a',owner_id='user-a')
        assert result['status']=='ready' and result['setup_url']==native_url()
        assert result['setup_code']==result['help_url']==''
        env=json.loads(capture.read_text());home=Path(env['CODEX_HOME'])
        assert env['OPENAI_API_KEY'] is None and env['WPR_API_TOKEN'] is None
        assert home.parent==Path(env['HOME']) and home.is_relative_to(tmp_path/'accounts')
        assert (home/'auth.json').stat().st_mode & 0o077==0
        assert store.active_provider_lease(account['account_id'],'provider-setup') is None
        assert store.get_provider_account_record(account_id=account['account_id'],tenant_id='tenant-a',owner_id='user-a')['status']=='ready'
    finally:
        manager.shutdown()
