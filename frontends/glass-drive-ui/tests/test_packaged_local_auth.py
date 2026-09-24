"""The local package has one persistent owner, not a network-trusted operator."""
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from glass_drive_ui.auth_gateway import HumanAuthGateway, AuthGatewayError
from glass_drive_ui.auth_admin import main as admin_main
from glass_drive_ui.server import create_app

PASSWORD = 'fixture-only-Strong-password-1234567890'


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    config = {'GLASSHIVE_HUMAN_AUTH_MODE': 'local_password',
              'GLASSHIVE_SECURITY_MODE': 'local', 'GLASSHIVE_DEFAULT_OWNER_ID': 'existing-owner',
              'GLASSHIVE_LOCAL_AUTH_NAMESPACE': 'fixture-deployment',
              'GLASSHIVE_AUTH_STATE_PATH': str(tmp_path / 'auth.sqlite3'),
              'GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY': 'fixture-private-throttle-key-1234567890',
              'WPR_API_TOKEN': 'fixture-runtime-secret'}
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('GLASSHIVE_TRUST_INBOUND_IDENTITY', raising=False)
    return HumanAuthGateway.from_env()


def login(client, password=PASSWORD):
    assert client.get('/login').status_code == 200
    return client.post('/auth/email/login', json={'password': password, 'return_to': '/'},
        headers={'Origin': 'http://testserver', 'X-GlassHive-CSRF': client.cookies['glasshive_login_csrf']})


def test_provision_restart_rotation_retains_owner(gateway):
    with pytest.raises(RuntimeError, match='safely provisioned'):
        gateway.validate_local_owner()
    assert gateway.provision_local_owner(password=PASSWORD)['user_id'] == 'existing-owner'
    session = gateway.authenticate_local_password(login_email=gateway.LOCAL_LOGIN, password=PASSWORD, source='fixture')
    restarted = HumanAuthGateway.from_env()
    restarted.validate_local_owner()
    assert restarted.resolve_session(session['token'])['user_id'] == 'existing-owner'
    # OIDC sessions cannot authenticate to a local deployment.
    oidc_session = gateway.create_session('existing-owner')
    assert restarted.resolve_session(oidc_session['token']) is None
    restarted.provision_local_owner(password='replacement-fixture-password-9876543210')
    assert gateway.resolve_session(session['token']) is None
    rotated = gateway.authenticate_local_password(login_email=gateway.LOCAL_LOGIN,
        password='replacement-fixture-password-9876543210', source='fixture')
    assert gateway.resolve_session(rotated['token'])['user_id'] == 'existing-owner'


@pytest.mark.parametrize('sql', [
    "UPDATE auth_principals SET user_id='different-owner'",
    "UPDATE auth_principals SET issuer='different-namespace'",
    "UPDATE auth_principals SET role='viewer'",
    "UPDATE auth_principals SET disabled_at=1",
    "UPDATE auth_local_credentials SET disabled_at=1",
    "UPDATE auth_local_credentials SET password_phc='$argon2id$corrupt'",
    "UPDATE auth_local_credentials SET password_phc='$argon2id$v=19$m=999999999,t=2,p=1$bad$bad'",
])
def test_startup_rejects_corrupt_mismatched_disabled_state(gateway, sql):
    gateway.provision_local_owner(password=PASSWORD)
    with sqlite3.connect(gateway.state_path) as conn:
        conn.execute(sql)
    with pytest.raises(RuntimeError, match='safely provisioned'):
        create_app()


def test_local_identity_and_private_state_cannot_silently_migrate(gateway, monkeypatch):
    gateway.provision_local_owner(password=PASSWORD)
    with pytest.raises(AuthGatewayError, match='fixed owner'):
        gateway.provision_local_password(subject='owner', login_email='changed@example.invalid', password=PASSWORD)
    monkeypatch.setenv('GLASSHIVE_DEFAULT_OWNER_ID', 'different-owner')
    with pytest.raises(AuthGatewayError, match='migration'):
        HumanAuthGateway.from_env().provision_local_owner(password=PASSWORD)
    gateway.state_path.chmod(0o644)
    with pytest.raises(RuntimeError, match='owner-only'):
        HumanAuthGateway.from_env()


def test_stdin_provision_does_not_output_password(gateway, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'password': PASSWORD})))
    assert admin_main(['provision-local-owner', '--stdin-json']) == 0
    result = capsys.readouterr()
    assert PASSWORD not in result.out + result.err
    assert json.loads(result.out)['user_id'] == 'existing-owner'
    assert PASSWORD.encode() not in gateway.state_path.read_bytes()


def test_operator_route_inventory_denies_unauthenticated_requests(gateway):
    gateway.provision_local_owner(password=PASSWORD)
    app = create_app()
    client = TestClient(app)
    public = {'/health','/login','/auth/config','/auth/session','/auth/email/login','/favicon.ico'}
    checked = 0
    for route in app.routes:
        path = getattr(route, 'path', '')
        if path in public or path.startswith(('/static','/r/','/w/','/docs','/redoc','/openapi')):
            continue
        if not getattr(route, 'methods', None):
            continue
        for name in getattr(route, 'param_convertors', {}):
            path = path.replace('{'+name+'}', 'wrk_fixture').replace('{'+name+':path}', 'workers/wrk_fixture')
        method = 'GET' if 'GET' in route.methods else next(iter(route.methods))
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code in {401,403}, (path,method,response.status_code)
        checked += 1
    assert checked > 60
    for fake in ('fixture-runtime-secret', 'fixture-native-bearer', 'fixture-mcp-key'):
        response = client.get('/api/control-plane', headers={'Authorization': 'Bearer '+fake,
            'X-WPR-Token': fake, 'X-Viventium-User-Id': 'existing-owner', 'Origin': 'http://testserver'})
        assert response.status_code == 401
    for path in ('/novnc/wrk_fixture/websockify','/ws/workers/wrk_fixture/terminal'):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path):
                pass


def test_browser_unlock_csrf_reload_rotation_and_origin(gateway):
    gateway.provision_local_owner(password=PASSWORD)
    client = TestClient(create_app())
    assert client.get('/auth/config').json()['login_methods'] == ['local_password']
    assert client.get('/', headers={'Accept': 'text/html'}, follow_redirects=False).status_code == 303
    client.get('/login')
    headers={'X-GlassHive-CSRF': client.cookies['glasshive_login_csrf']}
    assert client.post('/auth/email/login', json={'password': PASSWORD}, headers=headers).status_code == 403
    assert client.post('/auth/email/login', json={'password': PASSWORD},
        headers={**headers,'Origin':'https://attacker.example.invalid'}).status_code == 403
    assert login(client).status_code == 200
    identity = client.get('/auth/session').json()
    assert identity['authenticated'] and identity['user_id'] == 'existing-owner'
    assert client.get('/').status_code == 200
    assert client.post('/auth/logout').status_code == 403
    restarted = TestClient(create_app())
    restarted.cookies.update(client.cookies)
    assert restarted.get('/auth/session').json()['user_id'] == 'existing-owner'
    gateway.provision_local_owner(password='replacement-fixture-password-9876543210')
    assert restarted.get('/api/control-plane').status_code == 401


def test_wrong_password_lockout_survives_restart(gateway):
    gateway.provision_local_owner(password=PASSWORD)
    for _ in range(5):
        with pytest.raises(AuthGatewayError):
            gateway.authenticate_local_password(login_email=gateway.LOCAL_LOGIN, password='incorrect', source='fixture')
    with pytest.raises(AuthGatewayError):
        HumanAuthGateway.from_env().authenticate_local_password(login_email=gateway.LOCAL_LOGIN, password=PASSWORD, source='fixture')


def _view_cookie(worker_id='wrk_1', secret='fixture-runtime-secret'):
    import base64, hashlib, hmac, time
    payload = {'v': 1, 'kind': 'worker_view', 'worker_id': worker_id, 'tenant_id': 'local',
               'owner_id': 'existing-owner', 'path': '', 'exp': int(time.time()) + 900}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(',', ':'))
                                       .encode()).decode().rstrip('=')
    token = encoded + '.' + hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return 'glasshive_gh_token_' + hashlib.sha256(worker_id.encode()).hexdigest()[:24], token


def test_watch_view_cookie_does_not_block_the_signed_in_owner(gateway):
    # Launch opens Watch through a signed view link, which leaves a read-only
    # view cookie for the browser. The owner's session must stay the authority
    # for its next launch; the view cookie alone still authorizes nothing.
    gateway.provision_local_owner(password=PASSWORD)
    name, token = _view_cookie()
    owner = TestClient(create_app())
    assert login(owner).status_code == 200
    assert owner.get('/').status_code == 200
    owner.cookies.set(name, token)
    launched = owner.post('/api/launch', json={'goal': 'Synthetic follow-up project.'},
                          headers={'Origin': 'http://testserver',
                                   'X-GlassHive-CSRF': owner.cookies['glasshive_csrf']})
    assert 'read-only' not in launched.text
    assert launched.status_code not in {401, 403}, launched.text

    viewer = TestClient(create_app())
    viewer.cookies.set(name, token)
    denied = viewer.post('/api/launch', json={'goal': 'Synthetic follow-up project.'},
                         headers={'Origin': 'http://testserver'})
    assert denied.status_code in {401, 403}
