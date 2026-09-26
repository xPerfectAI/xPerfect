import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from workers_projects_runtime.execution_profile import packaged_linux
from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
from workers_projects_runtime.workspace_box import WorkspaceBox, WorkspaceBoxUnavailable, WorkspaceMemberBinding
from workers_projects_runtime.workspace_files import WorkspaceFiles
from workers_projects_runtime.store import Store


def service_module():
    path = Path(__file__).parents[2] / 'deployment/linux/service.py'
    spec = importlib.util.spec_from_file_location('linux_service', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profile_never_falls_back_on_typo(monkeypatch):
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE', 'local-linx')
    with pytest.raises(RuntimeError, match='Unsupported'):
        packaged_linux()


def test_packaged_isolated_member_uses_guarded_volume_and_separate_control(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'runtime.db'))
    project = store.create_project('owner', 'Fixture', 'Task', 'codex-cli')
    worker = store.create_worker(project['project_id'], 'owner', 'Member', 'worker',
                                 'codex-cli', 'docker', 'codex', 'exact')
    config = {'XPERFECT_EXECUTION_PROFILE': 'local-linux',
              'XPERFECT_SHARED_VOLUME_ROOT': str(tmp_path / 'data'),
              'XPERFECT_CONTROL_ROOT': str(tmp_path / 'control'),
              'XPERFECT_SHARED_VOLUME_NAME': 'fixture-data',
              'XPERFECT_SHARED_IMAGE': 'fixture-image',
              'XPERFECT_SHARED_NETWORK': 'fixture-workers',
              'XPERFECT_SHARED_MEMORY_BYTES': '536870912',
              'XPERFECT_SHARED_PIDS_LIMIT': '256'}
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr('workers_projects_runtime.packaged_linux.account_launcher_from_environment', lambda: None)
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / 'runtime'))
    runtime.configure_execution_workspaces(store)
    member = runtime._runtime_for_worker(worker)
    assert member.sandbox.box.file_placement == 'member_private'
    assert member.sandbox.box.control_root == tmp_path / 'control/workspaces'
    assert member.sandbox.box.network == 'fixture-workers'
    assert WorkspaceFiles(store).private_root == tmp_path / 'data/managed-files'
    with pytest.raises(WorkspaceBoxUnavailable, match='persisted'):
        runtime._runtime_for_worker({**worker, 'workspace_id': ''})


def test_service_config_requires_auth_and_rejects_hosted_local_identity(tmp_path):
    module = service_module()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'profile': 'local-linux', 'environment': {}}))
    config.chmod(0o600)
    with pytest.raises(ValueError, match='authentication'):
        module.load_environment(config, 'runtime')
    config.write_text(json.dumps({'profile': 'hosted-xfs', 'environment': {'WPR_API_TOKEN': 'synthetic'}}))
    with pytest.raises(ValueError, match='multi-user'):
        module.load_environment(config, 'runtime')


def test_service_environment_does_not_inherit_ambient_auth(tmp_path, monkeypatch):
    module = service_module()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'profile': 'local-linux', 'environment': {'WPR_API_TOKEN': 'synthetic', 'WPR_DEFAULT_OWNER_ID': 'fixture-owner', 'GLASSHIVE_DEFAULT_OWNER_ID': 'fixture-owner'}}))
    config.chmod(0o600)
    monkeypatch.setenv('OPENAI_API_KEY', 'ambient-must-not-inherit')
    environment = module.load_environment(config, 'runtime')
    assert 'OPENAI_API_KEY' not in environment
    assert environment['GLASSHIVE_HOST_WORKERS_ENABLED'] == '0'
    assert environment['WPR_DB_PATH'] == '/control/runtime.db'
    assert environment['XPERFECT_SHARED_VOLUME_ROOT'] == '/data'
    assert len(module.commands('runtime')) == 1
    assert 'http://runtime:8766' in module.commands('mcp')[0]
    config.chmod(0o644)
    with pytest.raises(ValueError, match='private regular'):
        module.load_environment(config, 'runtime')


def test_box_rejects_extra_attached_frontend_network(tmp_path):
    binding = WorkspaceMemberBinding('wsp_one', 'wrk_one', 'tenant', 'owner', 20001)
    box = WorkspaceBox(volume_root=tmp_path / 'data', control_root=tmp_path / 'control',
        volume_name='fixture-data', image='fixture-image', binding=binding,
        memory_bytes=1024, pids_limit=64, network='fixture-workers')
    box.supervisor.mkdir(parents=True)
    image = 'sha256:' + '1' * 64
    (box.supervisor / 'image.json').write_text(json.dumps({'image_id': image}))
    mounts = [{'Type': 'volume', 'Name': 'fixture-data', 'Destination': target,
               'RW': target == '/workspace/data'} for target in ('/workspace/data', '/workspace/common')]
    declared = [{'Type': 'volume', 'Source': 'fixture-data', 'Target': target,
                 'VolumeOptions': {'Subpath': 'execution_workspaces/wsp_one/' + subpath, 'NoCopy': True}}
                for target, subpath in (('/workspace/data', 'native'), ('/workspace/common', 'common'))]
    record = {'Id': '2' * 64, 'Image': image, 'Mounts': mounts,
              'Config': {'Labels': {'xperfect.workspace': 'wsp_one', 'xperfect.owner': binding.owner_digest},
                         'User': '65534:65534', 'Healthcheck': {'Test': ['NONE']}, 'Entrypoint': ['/bin/sleep'], 'Cmd': ['infinity']},
              'HostConfig': {'IpcMode': 'none', 'Mounts': declared, 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                             'SecurityOpt': ['no-new-privileges'], 'NetworkMode': 'fixture-workers',
                             'Memory': 1024, 'MemorySwap': 1024, 'PidsLimit': 64},
              'NetworkSettings': {'Networks': {'fixture-workers': {}}}}
    box.docker = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps([record]), '')
    assert box._inspect()['Id'] == '2' * 64
    record['NetworkSettings']['Networks']['fixture-frontend'] = {}
    with pytest.raises(WorkspaceBoxUnavailable, match='isolation contract'):
        box._inspect()


@pytest.mark.parametrize('role', ['runtime', 'ui', 'mcp'])
def test_every_packaged_role_uses_the_shared_writable_link_store(tmp_path, monkeypatch, role):
    # Packaged roots are read-only, and a link minted by one role is opened by the
    # UI and revoked by the runtime, so all roles use one store on /links.
    from workers_projects_runtime.signed_links import link_ref_state_path

    module = service_module()
    config = tmp_path / 'config.json'
    environment = {'WPR_API_TOKEN': 'synthetic', 'WPR_DEFAULT_OWNER_ID': 'fixture-owner',
                   'GLASSHIVE_DEFAULT_OWNER_ID': 'fixture-owner',
                   'GLASSHIVE_HUMAN_AUTH_MODE': 'local_password', 'GLASSHIVE_MCP_API_KEY': 'distinct'}
    config.write_text(json.dumps({'profile': 'local-linux', 'environment': environment}))
    config.chmod(0o600)
    path = Path(module.load_environment(config, role)['GLASSHIVE_LINK_REF_STATE_PATH'])
    assert path == Path('/links/link-refs.sqlite3')
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH', str(path))
    assert link_ref_state_path() == path


def _launch_module():
    path = Path(__file__).parents[2] / 'deployment/linux/launch.py'
    spec = importlib.util.spec_from_file_location('linux_launch', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launch_mounts_one_narrow_link_store_into_every_service_role(tmp_path, monkeypatch):
    # Watch and file links are minted by runtime, MCP and UI but opened by the UI.
    # One dedicated volume carries only that store; auth and control state stay private.
    import io
    module = _launch_module()
    calls = []
    image, native = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64

    configs = []

    def docker(endpoint, *args, data=None):
        calls.append(args)
        if args[:1] == ('cp',) and data:
            import tarfile
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                if 'config.json' in archive.getnames():
                    configs.append(json.loads(archive.extractfile('config.json').read()))
        if args[:1] == ('run',) and 'keygen' in args:
            return json.dumps({'kty': 'RSA', 'n': 'synthetic', 'e': 'AQAB', 'kid': args[-1]})
        if args[:1] == ('info',):
            return json.dumps({'OSType': 'linux', 'CgroupVersion': '2'})
        if args[:2] == ('image', 'inspect'):
            return args[-1]
        if args[1:2] == ('ls',):
            return ''
        if args[:1] == ('create',):
            return 'c' * 64
        if args[:1] == ('inspect',):
            return 'true'
        return ''

    class Health:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(module, 'docker', docker)
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *a, **k: Health())
    with (tmp_path / 'credentials.json').open('w') as credentials:
        receipt = module.launch(endpoint='unix:///var/run/docker.sock', name='xperfect-fixture', image=image,
                                native_image=native, ui_port=18880, mcp_port=18867, credentials=credentials)

    assert receipt['volumes']['links'] == 'xperfect-fixture-links'
    assert ('volume', 'create', '--label', 'xperfect.package=xperfect-fixture', 'xperfect-fixture-links') in calls
    creates = {call[call.index('--label') + 3].split('=', 1)[1]: call
               for call in calls if call[:1] == ('create',)}
    assert set(creates) == {'runtime', 'ui', 'mcp'}
    for role, call in creates.items():
        mounts = [call[i + 1] for i, value in enumerate(call) if value == '--mount']
        assert 'type=volume,src=xperfect-fixture-links,dst=/links,volume-nocopy' in mounts, role
        assert not any('ui-state' in mount for mount in mounts) or role == 'ui'
    # Links handed to a host MCP client must open on the published operator UI.
    assert len(configs) == 3
    assert {config['environment']['GLASSHIVE_OPERATOR_BASE_URL'] for config in configs} == {
        'http://127.0.0.1:18880'}
    for config in configs:
        env = config['environment']
        assert env['GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION'] == 'per_worker_container'
        assert env['GLASSHIVE_ENABLE_NATIVE_API_KEYS'] == '1'
        assert env['GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS'] == '1'
        assert env['GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH'] == '1'


@pytest.mark.parametrize('issuer', ['runtime', 'mcp'])
def test_packaged_links_minted_by_any_role_open_through_the_ui(tmp_path, monkeypatch, issuer):
    # Reproduces the independent review: a Watch link minted by MCP or runtime
    # must open through the operator UI's /r route, exactly like a UI-minted one.
    import sys
    from urllib.parse import urlsplit
    from fastapi.testclient import TestClient

    ui_root = Path(__file__).parents[2] / 'frontends/glass-drive-ui'
    monkeypatch.syspath_prepend(str(ui_root / 'tests'))
    monkeypatch.syspath_prepend(str(ui_root / 'src'))
    from glass_drive_ui.auth_gateway import HumanAuthGateway
    from glass_drive_ui.runtime_client import RuntimeClient
    from glass_drive_ui.server import create_app
    from test_packaged_local_auth import PASSWORD, login
    from workers_projects_runtime.signed_links import create_signed_link_ref, sign_link_token

    module = service_module()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'profile': 'local-linux', 'environment': {
        'WPR_API_TOKEN': 'fixture-runtime-secret', 'WPR_DEFAULT_OWNER_ID': 'existing-owner',
        'GLASSHIVE_DEFAULT_OWNER_ID': 'existing-owner', 'GLASSHIVE_HUMAN_AUTH_MODE': 'local_password',
        'GLASSHIVE_MCP_API_KEY': 'distinct'}}))
    config.chmod(0o600)
    stores = {role: Path(module.load_environment(config, role)['GLASSHIVE_LINK_REF_STATE_PATH'])
              for role in ('runtime', 'mcp', 'ui')}
    in_tmp = lambda role: tmp_path / 'volumes' / stores[role].relative_to('/')

    for key, value in {'GLASSHIVE_HUMAN_AUTH_MODE': 'local_password', 'GLASSHIVE_SECURITY_MODE': 'local',
                       'GLASSHIVE_DEFAULT_OWNER_ID': 'existing-owner',
                       'GLASSHIVE_LOCAL_AUTH_NAMESPACE': 'fixture-deployment',
                       'GLASSHIVE_AUTH_STATE_PATH': str(tmp_path / 'auth.sqlite3'),
                       'GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY': 'fixture-private-throttle-key-1234567890',
                       'GLASSHIVE_WATCH_SESSION_STATE_PATH': str(tmp_path / 'watch.sqlite3'),
                       'GLASSHIVE_OPERATOR_BASE_URL': 'http://testserver', 'VIVENTIUM_ENV_FILE': '',
                       'WPR_API_TOKEN': 'fixture-runtime-secret'}.items():
        monkeypatch.setenv(key, value)
    for key in ('GLASSHIVE_TRUST_INBOUND_IDENTITY', 'GLASSHIVE_PUBLIC_LINKS_ONLY', 'GLASSHIVE_SIGNED_LINK_SECRET'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(RuntimeClient, '_request', lambda *a, **k: {})
    HumanAuthGateway.from_env().provision_local_owner(password=PASSWORD)

    in_tmp(issuer).parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH', str(in_tmp(issuer)))
    worker = {'worker_id': 'wrk_1', 'tenant_id': 'local', 'owner_id': 'existing-owner', 'state': 'ready'}
    if issuer == 'mcp':
        from workers_projects_runtime.mcp_server import _signed_view_steer_url
        monkeypatch.setenv('GLASSHIVE_OPERATOR_BASE_URL', 'http://127.0.0.1:18880')
        minted = urlsplit(_signed_view_steer_url(worker, 'prj_one', 'web'))
        assert (minted.scheme, minted.netloc) == ('http', '127.0.0.1:18880')
        ref = minted.path.rsplit('/', 1)[-1]
    else:
        token = sign_link_token(kind='worker_view', worker_id='wrk_1', tenant_id='local',
                                owner_id='existing-owner')
        ref = create_signed_link_ref(token=token, target_url='/watch/wrk_1?surface=desktop')

    in_tmp('ui').parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH', str(in_tmp('ui')))
    client = TestClient(create_app())
    assert login(client).status_code == 200
    opened = client.get('/r/' + ref, follow_redirects=False)
    assert opened.status_code == 307, opened.text
    assert urlsplit(opened.headers['location']).path == '/watch/wrk_1'

    # Closing the worker revokes through the runtime's store; the UI honors it.
    from workers_projects_runtime.signed_links import revoke_signed_link_refs_for_worker
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH', str(in_tmp('runtime')))
    revoke_signed_link_refs_for_worker('wrk_1')
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH', str(in_tmp('ui')))
    assert client.get('/r/' + ref, follow_redirects=False).status_code in {401, 403, 404, 410}


# Hosted OIDC/TLS/XFS profile composition.

def _hosted_env(role, **overrides):
    base = {'WPR_API_TOKEN': 'synthetic-api', 'GLASSHIVE_SECURITY_MODE': 'multi_user',
            'GLASSHIVE_AUTH_MODE': 'signed_internal_assertion', 'GLASSHIVE_ENTERPRISE_TENANT_ID': 'xperfect-fixture',
            'GLASSHIVE_OIDC_ISSUER': 'https://idp.example.test/realms/xperfect', 'GLASSHIVE_OIDC_PRINCIPAL_CLAIM': 'sub',
            'GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT': 'false'}
    if role == 'ui':
        base.update(GLASSHIVE_HUMAN_AUTH_MODE='oidc', GLASSHIVE_OIDC_CLIENT_ID='xperfect-ui',
                    GLASSHIVE_OIDC_CLIENT_SECRET='synthetic-secret',
                    GLASSHIVE_OIDC_REDIRECT_URI='https://app.example.test/auth/oidc/callback',
                    GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE='/ui-state/assertion-key.pem',
                    GLASSHIVE_INTERNAL_ASSERTION_KEY_ID='xperfect-ui-1')
    if role == 'mcp':
        base.update(GLASSHIVE_MCP_OAUTH_ISSUER='https://idp.example.test/realms/xperfect',
                    GLASSHIVE_MCP_PUBLIC_URL='https://mcp.example.test/mcp',
                    GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES='https://mcp.example.test/mcp',
                    GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES='glasshive:access',
                    GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS='xperfect-mcp',
                    GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE='/mcp-state/assertion-key.pem',
                    GLASSHIVE_INTERNAL_ASSERTION_KEY_ID='xperfect-mcp-1')
    if role == 'runtime':
        base.update(GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE='/control/assertion-jwks.json',
                    GLASSHIVE_BOOTSTRAP_SOURCE_SECRET='synthetic-source-key')
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _write_config(tmp_path, profile, environment):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'profile': profile, 'environment': environment}))
    config.chmod(0o600)
    return config


@pytest.mark.parametrize('role', ['runtime', 'ui', 'mcp'])
def test_hosted_roles_share_one_login_store_and_keep_secrets_separate(tmp_path, role):
    module = service_module()
    env = module.load_environment(_write_config(tmp_path, 'hosted-xfs', _hosted_env(role)), role)
    if role in {'ui', 'mcp'}:
        assert env['GLASSHIVE_AUTH_STATE_PATH'] == '/auth/auth.sqlite3'
    else:
        assert env['XPERFECT_STORAGE_ROOT'] == '/data' and 'GLASSHIVE_OIDC_CLIENT_SECRET' not in env
    if role == 'mcp':
        assert env['GLASSHIVE_MCP_TLS_CERT_FILE'] == '/mcp-state/tls/cert.pem'


@pytest.mark.parametrize('role,override,message', [
    ('runtime', {'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE': '/control/assertion-key.pem'}, 'signing key'),
    ('mcp', {'GLASSHIVE_OIDC_CLIENT_SECRET': 'leak'}, 'client secret'),
    ('mcp', {'GLASSHIVE_MCP_API_KEY': 'static'}, 'OAuth'),
    ('ui', {'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE': '/mcp-state/assertion-key.pem'}, 'own key'),
    ('ui', {'GLASSHIVE_OIDC_REDIRECT_URI': 'http://app.example.test/auth/oidc/callback'}, 'HTTPS redirect'),
    ('ui', {'GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT': 'true'}, 'preapprove'),
    ('runtime', {'GLASSHIVE_ENTERPRISE_TENANT_ID': 'local'}, 'deployment tenant'),
    ('mcp', {'GLASSHIVE_OIDC_ISSUER': 'http://idp.example.test'}, 'HTTPS OIDC issuer'),
    ('mcp', {'GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS': None}, 'OAuth issuer'),
    ('runtime', {'GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE': None}, 'verification key set'),
    ('ui', {'GLASSHIVE_BOOTSTRAP_SOURCE_SECRET': 'leak'}, 'Only the runtime'),
    ('mcp', {'GLASSHIVE_BOOTSTRAP_SOURCE_SECRET': 'leak'}, 'Only the runtime'),
])
def test_hosted_roles_fail_closed_with_a_short_remedy(tmp_path, role, override, message):
    module = service_module()
    with pytest.raises(ValueError, match=message):
        module.load_environment(_write_config(tmp_path, 'hosted-xfs', _hosted_env(role, **override)), role)


def test_hosted_ui_and_mcp_serve_tls_while_local_stays_plain(monkeypatch):
    module = service_module()
    assert '--ssl-certfile' in module.commands('ui', 'hosted-xfs')[0]
    assert '--ssl-certfile' not in module.commands('ui', 'local-linux')[0]
    from workers_projects_runtime import mcp_server
    monkeypatch.setenv('GLASSHIVE_MCP_TLS_CERT_FILE', '/mcp-state/tls/cert.pem')
    monkeypatch.delenv('GLASSHIVE_MCP_TLS_KEY_FILE', raising=False)
    with pytest.raises(RuntimeError, match='both'):
        mcp_server._mcp_tls_files()
    monkeypatch.setenv('GLASSHIVE_MCP_TLS_KEY_FILE', '/mcp-state/tls/key.pem')
    assert mcp_server._mcp_tls_files() == {'ssl_certfile': '/mcp-state/tls/cert.pem', 'ssl_keyfile': '/mcp-state/tls/key.pem'}
    monkeypatch.delenv('GLASSHIVE_MCP_TLS_CERT_FILE')
    monkeypatch.delenv('GLASSHIVE_MCP_TLS_KEY_FILE')
    assert mcp_server._mcp_tls_files() == {}


class _Front:
    """A live TLS front door: UI health, or MCP metadata naming its resource."""

    status = 200

    def __init__(self, url):
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if '/.well-known/oauth-protected-resource' in self.url:
            host = self.url.split('/.well-known/', 1)[0]
            return json.dumps({'resource': host + '/mcp'}).encode()
        return b'{}'


def _hosted_input(tmp_path, **overrides):
    secret, cert, key = tmp_path / 'client-secret', tmp_path / 'cert.pem', tmp_path / 'key.pem'
    for path, text in ((secret, 'synthetic-client-secret'), (cert, 'CERT'), (key, 'KEY')):
        path.write_text(text)
        path.chmod(0o600)
    value = {'public_url': 'https://app.example.test:18443', 'mcp_public_url': 'https://mcp.example.test:18444/mcp',
             'issuer': 'https://idp.example.test:18445/realms/xperfect', 'client_id': 'xperfect-ui',
             'client_secret_file': str(secret), 'tenant_id': 'xperfect-fixture',
             'tls_certificate_file': str(cert), 'tls_key_file': str(key),
             'xfs_mount': '/srv/xperfect-data', 'xfs_device': '/dev/loop7',
             'extra_hosts': {'idp.example.test': 'host-gateway'}}
    value.update(overrides)
    return value


@pytest.mark.parametrize('override,message', [
    ({'issuer': 'http://idp.example.test/realms/x'}, 'issuer must be an HTTPS URL'),
    ({'public_url': 'https://app.example.test/path'}, 'origin without a path'),
    ({'tenant_id': 'local'}, 'tenant_id must name'),
    ({'storage_limit_bytes': 10}, 'storage_limit_bytes'),
    ({'xfs_mount': 'relative/data'}, 'xfs_mount'),
    ({'unexpected': True}, 'missing or unknown'),
])
def test_hosted_input_fails_before_any_docker_mutation(tmp_path, override, message):
    module = _launch_module()
    with pytest.raises(ValueError, match=message):
        module.validate_hosted(_hosted_input(tmp_path, **override))


def test_hosted_input_requires_a_private_client_secret(tmp_path):
    module = _launch_module()
    value = _hosted_input(tmp_path)
    Path(value['client_secret_file']).chmod(0o644)
    with pytest.raises(ValueError, match='private'):
        module.validate_hosted(value)


def test_launch_hosted_composes_xfs_oidc_tls_roles(tmp_path, monkeypatch):
    import io
    import tarfile
    module = _launch_module()
    calls, configs, copies = [], {}, {}
    image, native = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64

    def docker(endpoint, *args, data=None):
        calls.append(args)
        if args[:1] == ('cp',) and data:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                names = archive.getnames()
                copies.setdefault(args[-1].split(':', 1)[0], []).extend(names)
                if 'config.json' in names:
                    configs[args[-1].split(':', 1)[0]] = json.loads(archive.extractfile('config.json').read())
        if args[:1] == ('info',):
            return json.dumps({'OSType': 'linux', 'CgroupVersion': '2', 'SecurityOptions': ['name=seccomp']})
        if args[:2] == ('image', 'inspect'):
            return args[-1]
        if args[1:2] == ('ls',):
            return ''
        if args[:1] == ('run',) and 'keygen' in args:
            kid = args[args.index('--kid') + 1]
            return json.dumps({'kty': 'RSA', 'n': 'x', 'e': 'AQAB', 'kid': kid, 'alg': 'RS256', 'use': 'sig'})
        if args[:1] == ('create',):
            return args[args.index('--name') + 1] + '-id'
        if args[:1] == ('inspect',):
            return 'true'
        return ''

    seen = {'urls': []}

    def urlopen(url, timeout=None, context=None):
        seen['urls'].append(url)
        seen.update(context=context)
        return _Front(url)

    monkeypatch.setattr(module, 'docker', docker)
    monkeypatch.setattr(module.urllib.request, 'urlopen', urlopen)
    receipt = module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted', image=image,
                                   native_image=native, ui_port=18443, mcp_port=18444, hosted=_hosted_input(tmp_path))
    assert receipt['profile'] == 'hosted-xfs' and receipt['storage_limit_bytes'] == 5_000_000_000
    assert ('volume', 'create', '--driver', 'local', '--opt', 'type=none', '--opt', 'o=bind',
            '--opt', 'device=/srv/xperfect-data', '--label', 'xperfect.package=xperfect-hosted',
            'xperfect-hosted-data') in calls
    creates = {call[call.index('--label') + 3].split('=', 1)[1]: call for call in calls if call[:1] == ('create',)}
    for role, call in creates.items():
        mounts = [call[i + 1] for i, value in enumerate(call) if value == '--mount']
        caps = [call[i + 1] for i, value in enumerate(call) if value == '--cap-add']
        assert ('SYS_ADMIN' in caps) is (role == 'runtime')
        assert ('--device' in call) is (role == 'runtime')
        assert any('dst=/auth' in mount for mount in mounts) is (role != 'runtime')
        assert call[call.index('--restart') + 1] == 'unless-stopped'
        assert 'idp.example.test:host-gateway' in call
    ids = {role: 'xperfect-hosted-' + role + '-id' for role in ('runtime', 'ui', 'mcp')}
    secrets_holders = [role for role, identity in ids.items()
                       if configs[identity]['environment'].get('GLASSHIVE_OIDC_CLIENT_SECRET')]
    assert secrets_holders == ['ui']
    assert all(configs[identity]['profile'] == 'hosted-xfs' for identity in ids.values())
    for identity in ids.values():
        env = configs[identity]['environment']
        assert env['GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION'] == 'per_worker_container'
        assert env['GLASSHIVE_ENABLE_NATIVE_API_KEYS'] == '1'
        assert env['GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS'] == '1'
        assert env['GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH'] == '1'
    assert 'assertion-jwks.json' in copies[ids['runtime']] and 'tls/key.pem' not in copies[ids['runtime']]
    assert {'tls/cert.pem', 'tls/key.pem'} <= set(copies[ids['ui']]) and {'tls/cert.pem', 'tls/key.pem'} <= set(copies[ids['mcp']])
    kids = receipt['signing_key_ids']
    assert kids['ui'] != kids['mcp']
    assert configs[ids['ui']]['environment']['GLASSHIVE_INTERNAL_ASSERTION_KEY_ID'] == kids['ui']
    assert configs[ids['mcp']]['environment']['GLASSHIVE_INTERNAL_ASSERTION_KEY_ID'] == kids['mcp']
    assert 'GLASSHIVE_MCP_API_KEY' not in configs[ids['mcp']]['environment']
    assert seen['urls'][-2:] == ['https://app.example.test:18443/health',
                                 'https://mcp.example.test:18444/.well-known/oauth-protected-resource/mcp']
    assert seen['context'] is not None
    assert 'SSL_CERT_FILE' not in configs[ids['ui']]['environment']
    # Every role's generated config is accepted by the in-image loader.
    service = service_module()
    for role, identity in ids.items():
        state = 'control' if role == 'runtime' else role + '-state'
        path = tmp_path / f'{role}-config.json'
        path.write_text(json.dumps(configs[identity]))
        path.chmod(0o600)
        service.load_environment(path, role)


def test_launch_hosted_trusts_a_private_issuer_ca_only_where_needed(tmp_path, monkeypatch):
    import io, tarfile, ssl
    module = _launch_module()
    configs, copies = {}, {}
    def docker(endpoint, *args, data=None):
        if args[:1] == ('cp',) and data:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                target = args[-1].split(':', 1)[0]
                copies.setdefault(target, []).extend(archive.getnames())
                if 'config.json' in archive.getnames():
                    configs[target] = json.loads(archive.extractfile('config.json').read())
        if args[:1] == ('info',):
            return json.dumps({'OSType': 'linux', 'CgroupVersion': '2'})
        if args[:2] == ('image', 'inspect'):
            return args[-1]
        if args[:1] == ('run',):
            return json.dumps({'kty': 'RSA', 'n': 'x', 'e': 'AQAB', 'kid': args[args.index('--kid') + 1]})
        if args[:1] == ('create',):
            return args[args.index('--name') + 1] + '-id'
        return 'true' if args[:1] == ('inspect',) else ''
    monkeypatch.setattr(module, 'docker', docker)
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda url, **k: _Front(url))
    monkeypatch.setattr(module.ssl, 'create_default_context', lambda cafile=None: object())
    ca = tmp_path / 'ca.pem'
    ca.write_text('CA')
    module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted', image='sha256:' + 'a' * 64,
                         native_image='sha256:' + 'b' * 64, ui_port=18443, mcp_port=18444,
                         hosted=_hosted_input(tmp_path, tls_ca_file=str(ca)))
    for role in ('ui', 'mcp'):
        env = configs['xperfect-hosted-' + role + '-id']['environment']
        assert env['SSL_CERT_FILE'] == '/' + role + '-state/issuer-ca.pem'
        assert 'issuer-ca.pem' in copies['xperfect-hosted-' + role + '-id']
    assert 'SSL_CERT_FILE' not in configs['xperfect-hosted-runtime-id']['environment']


def _hosted_fake_docker(calls, configs):
    import io
    import tarfile

    def docker(endpoint, *args, data=None):
        calls.append(args)
        if args[:1] == ('cp',) and data:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                if 'config.json' in archive.getnames():
                    configs[args[-1].split(':', 1)[0]] = json.loads(archive.extractfile('config.json').read())
        if args[:1] == ('info',):
            return json.dumps({'OSType': 'linux', 'CgroupVersion': '2'})
        if args[:2] == ('image', 'inspect'):
            return args[-1]
        if args[:1] == ('run',):
            return json.dumps({'kty': 'RSA', 'n': 'x', 'e': 'AQAB', 'kid': args[args.index('--kid') + 1]})
        if args[:1] == ('create',):
            return args[args.index('--name') + 1] + '-id'
        return 'true' if args[:1] == ('inspect',) else ''
    return docker


def _mutations(calls):
    return [call for call in calls if call[:1] in {('run',), ('create',), ('cp',), ('start',)} or call[1:2] == ('create',)]


def test_the_documented_hosted_recipe_launches_exactly_as_written(tmp_path, monkeypatch):
    """The doc's input and command run unchanged, apart from real private file paths."""
    import re
    module = _launch_module()
    doc = (Path(__file__).resolve().parents[2] / 'docs' / 'deployment-hosted.md').read_text()
    documented = json.loads(re.search(r'```json\n(.*?)```', doc, re.S).group(1))
    command = re.search(r'```\n(python3 deployment/linux/launch.py --profile hosted-xfs.*?)```', doc, re.S).group(1)
    ui_port = int(re.search(r'--ui-port (\d+)', command).group(1))
    mcp_port = int(re.search(r'--mcp-port (\d+)', command).group(1))
    files = _hosted_input(tmp_path)
    hosted = {**documented, **{k: files[k] for k in ('client_secret_file', 'tls_certificate_file', 'tls_key_file')}}
    calls, configs, urls = [], {}, []
    monkeypatch.setattr(module, 'docker', _hosted_fake_docker(calls, configs))
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda url, **k: urls.append(url) or _Front(url))
    receipt = module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                                   image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                                   ui_port=ui_port, mcp_port=mcp_port, hosted=hosted)
    assert receipt['ui_url'] == documented['public_url'] and receipt['mcp_url'] == documented['mcp_public_url']
    # Readiness used exactly the advertised front doors.
    assert urls[-2:] == [documented['public_url'] + '/health',
                         documented['mcp_public_url'].rsplit('/mcp', 1)[0] + '/.well-known/oauth-protected-resource/mcp']
    publishes = [call[call.index('--publish') + 1] for call in calls if call[:1] == ('create',) and '--publish' in call]
    assert sorted(publishes) == sorted([f"{documented['bind_address']}:{ui_port}:8780",
                                        f"{documented['bind_address']}:{mcp_port}:8767"])
    ui_env = configs['xperfect-hosted-ui-id']['environment']
    assert ui_env['GLASSHIVE_OIDC_REDIRECT_URI'] == documented['public_url'] + '/auth/oidc/callback'


@pytest.mark.parametrize('ports,message', [
    ((443, 8443), 'public_url port \\(18443\\) must equal --ui-port \\(443\\)'),
    ((18443, 443), 'mcp_public_url port \\(18444\\) must equal --mcp-port \\(443\\)'),
])
def test_advertised_urls_must_name_the_published_ports(tmp_path, monkeypatch, ports, message):
    module = _launch_module()
    calls = []
    monkeypatch.setattr(module, 'docker', _hosted_fake_docker(calls, {}))
    with pytest.raises(ValueError, match=message):
        module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                             image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                             ui_port=ports[0], mcp_port=ports[1], hosted=_hosted_input(tmp_path))
    assert calls == []


@pytest.mark.parametrize('field,value', [
    ('public_url', 'https://app.localhost:18443'),
    ('issuer', 'https://idp.localhost:18445/realms/xperfect'),
    ('mcp_public_url', 'https://10.0.0.8:18444/mcp'),
    ('issuer', 'https://127.0.0.1:18445'),
])
def test_names_the_sign_in_gateway_refuses_fail_before_docker(tmp_path, field, value):
    module = _launch_module()
    with pytest.raises(ValueError, match='DNS name your users resolve'):
        module.validate_hosted(_hosted_input(tmp_path, **{field: value}))


def test_mcp_url_must_be_the_mcp_path(tmp_path):
    module = _launch_module()
    with pytest.raises(ValueError, match='end in /mcp'):
        module.validate_hosted(_hosted_input(tmp_path, mcp_public_url='https://mcp.example.test:18444/'))


def test_generated_ui_config_starts_the_real_sign_in_gateway(tmp_path, monkeypatch):
    auth_gateway = pytest.importorskip('glass_drive_ui.auth_gateway')
    module = _launch_module()
    calls, configs = [], {}
    monkeypatch.setattr(module, 'docker', _hosted_fake_docker(calls, configs))
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda url, **k: _Front(url))
    module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                         image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                         ui_port=18443, mcp_port=18444, hosted=_hosted_input(tmp_path))
    import os
    for name in list(os.environ):
        if name.startswith(('GLASSHIVE_', 'WPR_')):
            monkeypatch.delenv(name)
    for name, value in configs['xperfect-hosted-ui-id']['environment'].items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('GLASSHIVE_AUTH_STATE_PATH', str(tmp_path / 'auth.sqlite3'))
    gateway = auth_gateway.HumanAuthGateway.from_env()
    assert gateway.mode == 'oidc'


@pytest.mark.parametrize('metadata', [None, {'resource': 'https://elsewhere.example.test/mcp'}])
def test_hosted_launch_is_not_ready_until_the_mcp_front_door_answers(tmp_path, monkeypatch, metadata):
    module = _launch_module()
    calls, urls = [], []

    class WrongResource(_Front):
        def read(self):
            return json.dumps(metadata).encode()

    def urlopen(url, **kwargs):
        urls.append(url)
        if '/.well-known/oauth-protected-resource' in url:
            if metadata is None:
                raise OSError('synthetic MCP unavailable')
            return WrongResource(url)
        return _Front(url)

    monkeypatch.setattr(module, 'docker', _hosted_fake_docker(calls, {}))
    monkeypatch.setattr(module.urllib.request, 'urlopen', urlopen)
    with pytest.raises(RuntimeError, match='not reachable at their public URLs'):
        module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                             image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                             ui_port=18443, mcp_port=18444, hosted=_hosted_input(tmp_path), ready_timeout_s=0.3)
    assert any('/.well-known/oauth-protected-resource/mcp' in url for url in urls)


def test_preapproval_uses_the_one_role_vocabulary(monkeypatch):
    module = _launch_module()
    execs = []
    monkeypatch.setattr(module, 'docker', lambda endpoint, *args, data=None: execs.append((args, data)) or 'ok')
    with pytest.raises(ValueError, match='member, tenant_admin, viewer'):
        module.preapprove(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                          identity=b'{"subject": "stable-subject", "role": "admin"}')
    assert execs == []
    module.preapprove(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                      identity=b'{"subject": "stable-subject", "role": "tenant_admin"}')
    assert len(execs) == 1


@pytest.mark.parametrize('spelled,canonical,port', [
    ('https://MCP.Example.test:443/mcp', 'https://mcp.example.test/mcp', 443),
    ('https://mcp.example.test.:18444/mcp/', 'https://mcp.example.test:18444/mcp', 18444),
])
def test_one_canonical_mcp_url_reaches_readiness_and_allowed_hosts(tmp_path, monkeypatch, spelled, canonical, port):
    module = _launch_module()
    calls, configs, urls = [], {}, []
    monkeypatch.setattr(module, 'docker', _hosted_fake_docker(calls, configs))
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda url, **k: urls.append(url) or _Front(url))
    receipt = module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                                   image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                                   ui_port=18443, mcp_port=port,
                                   hosted=_hosted_input(tmp_path, mcp_public_url=spelled), ready_timeout_s=1)
    mcp_env = configs['xperfect-hosted-mcp-id']['environment']
    assert receipt['mcp_url'] == canonical == mcp_env['GLASSHIVE_MCP_PUBLIC_URL']
    assert mcp_env['GLASSHIVE_MCP_ALLOWED_HOSTS'] == canonical.split('/')[2]


@pytest.mark.parametrize('name', ['https://127.1', 'https://2130706433', 'https://0x7f.0.0.1'])
def test_ipv4_shorthand_is_an_address_not_a_name(tmp_path, name):
    module = _launch_module()
    with pytest.raises(ValueError, match='DNS name your users resolve'):
        module.validate_hosted(_hosted_input(tmp_path, public_url=name))


def test_mcp_accepts_the_host_clients_send_for_a_default_port(monkeypatch):
    from workers_projects_runtime.mcp_server import _allowed_host_values_from_setting
    assert 'app.example.test' in _allowed_host_values_from_setting('App.Example.test:443')
    assert _allowed_host_values_from_setting('App.Example.test:18444') == ['app.example.test:18444']


def test_each_profile_trusts_only_its_managed_upload_root(tmp_path, monkeypatch):
    """Files materializes an uploaded draft only from a declared trusted root."""
    module = _launch_module()
    calls, configs = [], {}
    monkeypatch.setattr(module, 'docker', _hosted_fake_docker(calls, configs))
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda url, **k: _Front(url))
    module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                         image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                         ui_port=18443, mcp_port=18444, hosted=_hosted_input(tmp_path))
    # Hosted uploads live inside each owner's quota project, not a shared folder.
    assert configs['xperfect-hosted-runtime-id']['environment']['WPR_BOOTSTRAP_SOURCE_ROOTS'] == '/data/owners'
    for role in ('ui', 'mcp'):
        assert 'WPR_BOOTSTRAP_SOURCE_ROOTS' not in configs[f'xperfect-hosted-{role}-id']['environment']
    source = Path(module.__file__).read_text()
    local_runtime = source[source.index('def launch('):source.index('HOSTED_ROLES')]
    assert "'WPR_BOOTSTRAP_SOURCE_ROOTS': '/data/managed-files'" in local_runtime


@pytest.mark.parametrize('launched_with_secret', [True, False])
def test_a_hosted_runtime_can_publish_an_owner_file_it_stored(tmp_path, monkeypatch, launched_with_secret):
    """The launched runtime configuration signs and accepts its own stored-file source.

    Without the per-package source key (as an earlier launcher wrote hosted packages)
    every attach of a stored file to a hosted workspace is refused.
    """
    from workers_projects_runtime import bootstrap

    module = _launch_module()
    calls, configs = [], {}
    monkeypatch.setattr(module, 'docker', _hosted_fake_docker(calls, configs))
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda url, **k: _Front(url))
    receipt = module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                                   image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                                   ui_port=18443, mcp_port=18444, hosted=_hosted_input(tmp_path))
    holders = [role for role in ('runtime', 'ui', 'mcp')
               if configs[f'xperfect-hosted-{role}-id']['environment'].get('GLASSHIVE_BOOTSTRAP_SOURCE_SECRET')]
    assert holders == ['runtime']
    key = configs['xperfect-hosted-runtime-id']['environment']['GLASSHIVE_BOOTSTRAP_SOURCE_SECRET']
    assert key not in json.dumps(receipt) and not any(key in ' '.join(map(str, call)) for call in calls)
    path = tmp_path / 'runtime-config.json'
    config = configs['xperfect-hosted-runtime-id']
    if not launched_with_secret:
        # An earlier launcher's hosted runtime: the image now refuses to start it with a remedy.
        config = {**config, 'environment': {k: v for k, v in config['environment'].items()
                                            if k != 'GLASSHIVE_BOOTSTRAP_SOURCE_SECRET'}}
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        # Upgrade and continuity helpers still read it; only serving is refused.
        environment = service_module().load_environment(path, 'runtime')
        with pytest.raises(ValueError, match='stored-file key; run the upgrade'):
            service_module().require_serving(environment, 'runtime')
    else:
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        environment = service_module().load_environment(path, 'runtime')
    owners = tmp_path / 'owners'
    stored = owners / 'owner-key' / 'managed-files' / 'fil_fixture.blob'
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b'synthetic stored bytes')
    monkeypatch.delenv('GLASSHIVE_BOOTSTRAP_SOURCE_SECRET', raising=False)
    for key in ('GLASSHIVE_SECURITY_MODE', 'GLASSHIVE_BOOTSTRAP_SOURCE_SECRET'):
        if key in environment:
            monkeypatch.setenv(key, environment[key])
    monkeypatch.setenv('WPR_BOOTSTRAP_SOURCE_ROOTS', str(owners))
    worker = {'tenant_id': 'fixture-tenant', 'owner_id': 'usr_fixture'}
    entry = {'source_path_token': bootstrap.sign_bootstrap_source_path(stored, **worker)}
    if launched_with_secret:
        assert bootstrap.resolve_authorized_bootstrap_source_path(entry, stored, worker) == stored.resolve()
        other = {'tenant_id': 'fixture-tenant', 'owner_id': 'usr_other'}
        with pytest.raises(PermissionError, match='not authorized'):
            bootstrap.resolve_authorized_bootstrap_source_path(entry, stored, other)
    else:
        assert entry['source_path_token'] == ''
        with pytest.raises(PermissionError, match='not authorized'):
            bootstrap.resolve_authorized_bootstrap_source_path(entry, stored, worker)


@pytest.mark.parametrize('value', [None, '  '])
def test_a_hosted_runtime_without_its_stored_file_key_does_not_serve(tmp_path, value):
    module = service_module()
    environment = module.load_environment(
        _write_config(tmp_path, 'hosted-xfs', _hosted_env('runtime', GLASSHIVE_BOOTSTRAP_SOURCE_SECRET=value)),
        'runtime')
    with pytest.raises(ValueError, match='stored-file key; run the upgrade'):
        module.require_serving(environment, 'runtime')
    module.require_serving(module.load_environment(_write_config(tmp_path, 'hosted-xfs', _hosted_env('runtime')),
                                                   'runtime'), 'runtime')
    for role in ('ui', 'mcp'):
        module.require_serving(module.load_environment(_write_config(tmp_path, 'hosted-xfs', _hosted_env(role)),
                                                       role), role)


ROLE_MAP = {'xperfect-admins': 'tenant_admin', 'xperfect-users': 'member'}


def _launch_with(tmp_path, monkeypatch, **overrides):
    module = _launch_module()
    calls, configs = [], {}
    monkeypatch.setattr(module, 'docker', _hosted_fake_docker(calls, configs))
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda url, **k: _Front(url))
    receipt = module.launch_hosted(endpoint='unix:///var/run/docker.sock', name='xperfect-hosted',
                                   image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                                   ui_port=18443, mcp_port=18444, hosted=_hosted_input(tmp_path, **overrides))
    return receipt, {role: configs[f'xperfect-hosted-{role}-id'] for role in ('runtime', 'ui', 'mcp')}


def test_role_mapping_is_off_by_default(tmp_path, monkeypatch):
    receipt, configs = _launch_with(tmp_path, monkeypatch)
    assert 'role_mapping' not in receipt
    for config in configs.values():
        assert not {'GLASSHIVE_OIDC_ROLE_CLAIM', 'GLASSHIVE_OIDC_ROLE_MAP_JSON'} & set(config['environment'])


def test_an_explicit_role_mapping_is_visible_and_reaches_only_the_sign_in_services(tmp_path, monkeypatch):
    receipt, configs = _launch_with(tmp_path, monkeypatch, role_claim='roles', role_map=ROLE_MAP)
    assert receipt['role_mapping'] == {'claim': 'roles', 'map': dict(sorted(ROLE_MAP.items()))}
    for role in ('ui', 'mcp'):
        env = configs[role]['environment']
        assert env['GLASSHIVE_OIDC_ROLE_CLAIM'] == 'roles'
        assert json.loads(env['GLASSHIVE_OIDC_ROLE_MAP_JSON']) == ROLE_MAP
    assert 'GLASSHIVE_OIDC_ROLE_MAP_JSON' not in configs['runtime']['environment']
    service = service_module()
    for role, config in configs.items():  # the in-image loader accepts every generated config
        path = tmp_path / f'{role}.json'
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        service.load_environment(path, role)


@pytest.mark.parametrize('overrides,message', [
    ({'role_map': {'xperfect-admins': 'service'}}, 'member, viewer or tenant_admin'),
    ({'role_map': {'xperfect-admins': 'owner'}}, 'member, viewer or tenant_admin'),
    ({'role_map': {}}, 'at least one'),
    ({'role_map': ['xperfect-admins']}, 'at least one'),
    ({'role_map': {' padded ': 'member'}}, 'exact identity-provider role or group values'),
    ({'role_claim': 'roles'}, 'needs a role_map'),
    ({'role_claim': 'role s', 'role_map': ROLE_MAP}, 'claim name'),
    ({'role_claim': 'realm_access.roles', 'role_map': ROLE_MAP}, 'top-level claim'),
    ({'role_claim': 'preferred_username', 'role_map': ROLE_MAP}, 'not a profile claim'),
    ({'role_map': {'ada@example.com': 'tenant_admin'}}, 'not people'),
    ({'role_map': {'staff': ['tenant_admin']}}, 'member, viewer or tenant_admin'),
])
def test_role_mapping_input_is_refused_before_any_docker_change(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        _launch_module().validate_hosted(_hosted_input(tmp_path, **overrides))


@pytest.mark.parametrize('mapped', [False, True])
def test_the_generated_mcp_configuration_decides_administrator_authority(tmp_path, monkeypatch, mapped):
    """The launched MCP configuration, loaded as the image loads it, drives the real verifier's role."""
    from workers_projects_runtime import mcp_oauth

    overrides = {'role_claim': 'roles', 'role_map': ROLE_MAP} if mapped else {}
    _, configs = _launch_with(tmp_path, monkeypatch, **overrides)
    path = tmp_path / 'mcp.json'
    path.write_text(json.dumps(configs['mcp']))
    path.chmod(0o600)
    environment = service_module().load_environment(path, 'mcp')
    for key in list(mcp_oauth.os.environ):
        if key.startswith(('GLASSHIVE_', 'WPR_')):
            monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        if key.startswith(('GLASSHIVE_', 'WPR_')):
            monkeypatch.setenv(key, value)
    auth = tmp_path / 'auth'  # the container's /auth volume: the UI's admitted-people store
    auth.mkdir(mode=0o700)
    import sqlite3
    with sqlite3.connect(auth / 'auth.sqlite3') as store:
        store.execute('CREATE TABLE auth_principals (user_id TEXT PRIMARY KEY, disabled_at REAL)')
    monkeypatch.setenv('GLASSHIVE_AUTH_STATE_PATH', str(auth / 'auth.sqlite3'))
    verifier, _ = mcp_oauth.oauth_from_env()
    admin, member = {'roles': ['xperfect-admins']}, {'roles': ['xperfect-users']}
    effective = lambda claims, admitted: (lambda token: token and mcp_oauth._least_privileged_role(admitted, token))(
        verifier._role(claims))
    if mapped:
        assert effective(admin, 'tenant_admin') == 'tenant_admin'
        assert effective(admin, 'member') == 'member'  # the stored role bounds MCP (a browser sign-in replaces it)
        assert effective(member, 'tenant_admin') == 'member'
        assert verifier._role({}) is None and verifier._role({'roles': ['unknown']}) is None  # refused
    else:
        # Without an operator map a token never confers administrator authority over MCP.
        assert effective(admin, 'tenant_admin') == 'member' and verifier._role({}) == 'member'



# The local package's human-confirmation channel: the UI signs, the runtime verifies, MCP neither.

def _local_launch(tmp_path, monkeypatch):
    """Launch a local package against a fake Docker whose volumes are directories and whose
    key generation runs the image's own generator, so the real services can load the result."""
    import io
    import sys
    import tarfile
    module, service = _launch_module(), service_module()
    roots = {target: tmp_path / 'volumes' / target.strip('/') for target in
             ('/control', '/ui-state', '/mcp-state', '/links', '/data')}
    for root in roots.values():
        root.mkdir(parents=True)
    calls, identities = [], {}

    def docker(endpoint, *args, data=None):
        calls.append(args)
        if args[:1] == ('run',) and 'keygen' in args:
            mounts = [args[i + 1] for i, value in enumerate(args) if value == '--mount']
            state = args[args.index('--state') + 1]
            assert mounts == [f'type=volume,src=xperfect-fixture-{state.strip("/")},dst={state},volume-nocopy']
            return subprocess.run([sys.executable, '-I', '-c', service.KEYGEN, str(roots[state]),
                                   args[args.index('--kid') + 1]], check=True, capture_output=True, text=True).stdout
        if args[:1] == ('cp',):
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive.getmembers():
                    target = roots[args[2].split(':', 1)[1]] / member.name
                    target.write_bytes(archive.extractfile(member).read())
                    target.chmod(member.mode)
            return ''
        if args[:1] == ('info',):
            return json.dumps({'OSType': 'linux', 'CgroupVersion': '2'})
        if args[:2] == ('image', 'inspect'):
            return args[-1]
        if args[1:2] == ('ls',):
            return ''
        if args[:1] == ('create',):
            role = args[args.index('--label') + 3].split('=', 1)[1]
            identities[role] = {'runtime': '1', 'ui': '2', 'mcp': '3'}[role] * 64
            return identities[role]
        if args[:1] == ('inspect',):
            return 'true'
        return ''

    class Health:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(module, 'docker', docker)
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *a, **k: Health())
    with (tmp_path / 'credentials.json').open('w') as credentials:
        receipt = module.launch(endpoint='unix:///var/run/docker.sock', name='xperfect-fixture',
                                image='sha256:' + 'a' * 64, native_image='sha256:' + 'b' * 64,
                                ui_port=18880, mcp_port=18867, credentials=credentials)
    # Each role's configuration, as that role's own service start reads and validates it.
    environments = {role: service.load_environment(roots[target] / 'config.json', role)
                    for role, target in (('runtime', '/control'), ('ui', '/ui-state'), ('mcp', '/mcp-state'))}
    return receipt, roots, environments, calls, json.loads((tmp_path / 'credentials.json').read_text())


def test_local_launch_keeps_the_signing_key_in_the_ui_state_and_gives_the_runtime_only_the_public_key(
        tmp_path, monkeypatch):
    receipt, roots, environments, calls, _ = _local_launch(tmp_path, monkeypatch)
    ui, runtime, mcp = environments['ui'], environments['runtime'], environments['mcp']
    kid = receipt['signing_key_ids']['ui']
    assert ui['GLASSHIVE_INTERNAL_ASSERTION_KEY_ID'] == kid
    # The private key exists once, owner-only, in the UI's own state.
    key_files = sorted(path.relative_to(tmp_path) for path in (tmp_path / 'volumes').rglob('assertion-key.pem'))
    assert key_files == [Path('volumes/ui-state/assertion-key.pem')]
    assert (roots['/ui-state'] / 'assertion-key.pem').stat().st_mode & 0o777 == 0o600
    # The runtime receives exactly the public half of that key, and nothing that signs.
    jwks = json.loads((roots['/control'] / 'assertion-jwks.json').read_text())
    assert [key['kid'] for key in jwks['keys']] == [kid] and not any('d' in key for key in jwks['keys'])
    assert runtime['GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE'] == '/control/assertion-jwks.json'
    assert 'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE' not in runtime
    assert not {key for key in mcp if 'ASSERTION' in key} and not list(roots['/mcp-state'].glob('assertion-*'))
    # The key is made before any service exists; the runtime has its public key before it starts.
    first_create = next(i for i, call in enumerate(calls) if call[:1] == ('create',))
    keygen = next(i for i, call in enumerate(calls) if 'keygen' in call)
    assert keygen < first_create
    assert {'--network', 'none', '--read-only', '--cap-drop', 'ALL'} <= set(calls[keygen])
    runtime_id = '1' * 64
    copies = [i for i, call in enumerate(calls) if call[:1] == ('cp',) and call[2] == runtime_id + ':/control']
    assert len(copies) == 2 and max(copies) < calls.index(('start', runtime_id))


@pytest.mark.parametrize('role,change,message', [
    ('mcp', {'GLASSHIVE_INTERNAL_ASSERTION_KEY_ID': 'k'}, 'MCP must hold no sign-in assertion keys'),
    ('mcp', {'GLASSHIVE_LOCAL_HUMAN_ASSERTION': '1'}, 'MCP must hold no sign-in assertion keys'),
    ('runtime', {'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE': '/ui-state/assertion-key.pem'},
     'Runtime must not receive the identity signing key'),
    ('ui', {'GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL': 'https://elsewhere.example.test/jwks'},
     'The UI signs; only the runtime verifies'),
    ('ui', {'GLASSHIVE_LOCAL_HUMAN_ASSERTION': None}, 'channel is incomplete'),
    ('runtime', {'GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE': None}, 'channel is incomplete'),
    ('ui', {'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE': '/links/assertion-key.pem'},
     'requires its own key in its private state'),
    ('runtime', {'GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE': '/links/assertion-jwks.json'},
     'requires its public verification key set'),
    ('runtime', {'GLASSHIVE_HUMAN_AUTH_MODE': None}, 'requires its public verification key set'),
])
def test_a_misplaced_or_partial_confirmation_channel_does_not_start(tmp_path, monkeypatch, role, change, message):
    _, roots, _, _, _ = _local_launch(tmp_path, monkeypatch)
    path = roots[{'runtime': '/control', 'ui': '/ui-state', 'mcp': '/mcp-state'}[role]] / 'config.json'
    config = json.loads(path.read_text())
    for key, value in change.items():
        if value is None:
            config['environment'].pop(key)
        else:
            config['environment'][key] = value
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=message):
        service_module().load_environment(path, role)


def _use_environment(monkeypatch, environment, roots):
    """Apply a role's generated environment in-process, with its volume paths on this machine."""
    import os
    for key in [key for key in os.environ if key.startswith(('GLASSHIVE_', 'WPR_', 'XPERFECT_'))]:
        monkeypatch.delenv(key)
    for key, value in environment.items():
        if key in {'PATH', 'LANG'}:
            continue
        for target, root in roots.items():
            if value == target or value.startswith(target + '/'):
                value = str(root) + value[len(target):]
        monkeypatch.setenv(key, value)


def test_a_signed_in_owner_confirmation_reaches_the_runtime_guard_only_through_the_channel(tmp_path, monkeypatch):
    """Reproduces the r17 403 with a package launched before the channel existed, then shows the
    generated channel carries a signed-in owner's confirmation past the unchanged runtime guard."""
    import sys
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app as runtime_app

    ui_root = Path(__file__).parents[2] / 'frontends/glass-drive-ui'
    monkeypatch.syspath_prepend(str(ui_root / 'tests'))
    monkeypatch.syspath_prepend(str(ui_root / 'src'))
    from glass_drive_ui.auth_gateway import HumanAuthGateway
    from glass_drive_ui.runtime_client import RuntimeClient
    from glass_drive_ui.server import create_app as ui_app
    from test_packaged_local_auth import login

    _, roots, environments, _, credentials = _local_launch(tmp_path, monkeypatch)
    sent = []

    def relay(self, method, path, *, json_body=None):
        sent.append((method, path, json_body, self._request_headers() or {}))
        return {}

    monkeypatch.setattr(RuntimeClient, '_request', relay)
    earlier = {key: value for key, value in environments['ui'].items()
               if 'ASSERTION' not in key}

    def confirm_through_ui(ui_environment):
        _use_environment(monkeypatch, ui_environment, roots)
        monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
        gateway = HumanAuthGateway.from_env()
        try:
            gateway.validate_local_owner()
        except RuntimeError:
            gateway.provision_local_owner(password=credentials['ui_password'])
        client = TestClient(ui_app())
        assert login(client, password=credentials['ui_password']).status_code == 200
        sent.clear()
        response = client.post('/api/pending-changes/chg_fixture/confirm', json={'confirmation_token': 'fixture-confirmation-token'},
                               headers={'Origin': 'http://testserver',
                                        'X-GlassHive-CSRF': client.cookies['glasshive_csrf']})
        assert response.status_code == 200, response.text
        (method, path, body, headers), = sent
        assert (method, path) == ('POST', '/v1/pending-changes/chg_fixture/confirm')
        return headers

    before = confirm_through_ui(earlier)
    after = confirm_through_ui(environments['ui'])
    assert 'X-GlassHive-User-Assertion' not in before and 'X-GlassHive-User-Assertion' in after

    _use_environment(monkeypatch, environments['runtime'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    with TestClient(runtime_app(db_path=str(tmp_path / 'runtime.db'), runtime_backend='stub',
                                reconcile_on_startup=False)) as runtime:
        def confirm(headers):
            return runtime.post('/v1/pending-changes/chg_fixture/confirm', json={'confirmation_token': 'fixture-confirmation-token'},
                                headers=headers)
        refused = confirm(before)
        assert refused.status_code == 403
        assert refused.json()['detail'] == 'An authenticated human confirmation session is required'
        passed = confirm(after)  # past the guard, the change itself is unknown to this runtime
        assert passed.status_code == 404, passed.text
        replayed = confirm(after)
        assert replayed.status_code in {401, 403} and 'already used' in replayed.text
        # MCP holds neither key: all it can send is its service credential, which cannot confirm.
        assert confirm({'X-WPR-Token': environments['mcp']['WPR_API_TOKEN']}).status_code == 403


def test_a_signed_in_owners_workspace_link_passes_the_runtime_view_gate(tmp_path, monkeypatch):
    """Run Project sends the browser to its workspace link. Before redirecting, the UI checks the
    link with the runtime using the viewer assertion it signs; the runtime must accept that read
    (first start once showed "workspace link is no longer available" instead of the live view)."""
    import os
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app as runtime_app

    ui_root = Path(__file__).parents[2] / 'frontends/glass-drive-ui'
    monkeypatch.syspath_prepend(str(ui_root / 'tests'))
    monkeypatch.syspath_prepend(str(ui_root / 'src'))
    from glass_drive_ui.auth_gateway import HumanAuthGateway
    from glass_drive_ui.runtime_client import RuntimeClient
    from glass_drive_ui.server import create_app as ui_app
    from glass_drive_ui.signed_links import create_signed_link_ref, sign_link_token
    from test_packaged_local_auth import login

    _, roots, environments, _, credentials = _local_launch(tmp_path, monkeypatch)
    db_path = str(tmp_path / 'runtime.db')
    _use_environment(monkeypatch, environments['ui'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    gateway = HumanAuthGateway.from_env()
    gateway.provision_local_owner(password=credentials['ui_password'])
    owner, tenant = gateway.local_owner_id, os.environ.get('GLASSHIVE_ENTERPRISE_TENANT_ID') or 'local'

    _use_environment(monkeypatch, environments['runtime'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    from workers_projects_runtime.openclaw_runtime import StubRuntime

    class DesktopRuntime(StubRuntime):
        def describe_worker(self, worker):
            return {'mode': 'desktop', 'runtime': 'claude-code', 'gateway_url': 'http://127.0.0.1:61001/gateway',
                    'view_url': 'http://127.0.0.1:61002/?autoconnect=1&password=synthetic-desktop-secret'}

    store = runtime_app(db_path=db_path, runtime_backend='stub', reconcile_on_startup=False).state.store
    project = store.create_project(owner, 'First result', 'Write a short note', 'claude-code', tenant_id=tenant)
    worker = store.create_worker(project['project_id'], owner, 'Claude Code', 'main', 'claude-code',
                                 'stub', 'stub', 'stub', tenant_id=tenant)

    sent = []

    def relay(self, method, path, *, json_body=None):
        sent.append((method, path, self._request_headers() or {}))
        return {}

    monkeypatch.setattr(RuntimeClient, '_request', relay)
    _use_environment(monkeypatch, environments['ui'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    browser = TestClient(ui_app())
    assert login(browser, password=credentials['ui_password']).status_code == 200
    token = sign_link_token(kind='worker_view', worker_id=worker['worker_id'], tenant_id=tenant, owner_id=owner)
    ref = create_signed_link_ref(
        token=token, target_url=f"/watch/{worker['worker_id']}?project_id={project['project_id']}&surface=terminal")
    gate_path = f"/v1/workers/{worker['worker_id']}/view-opened"

    def link_assertion():
        sent.clear()
        opened = browser.get(f'/r/{ref}', follow_redirects=False)
        assert opened.status_code == 307, opened.text
        (_, _, headers), = [call for call in sent if call[:2] == ('POST', gate_path)]
        assert 'X-GlassHive-User-Assertion' in headers
        return headers

    opening, mutation, confirmation, reading = (link_assertion() for _ in range(4))

    _use_environment(monkeypatch, environments['runtime'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    with TestClient(runtime_app(db_path=db_path, runtime_backend='stub', runtime=DesktopRuntime(),
                                reconcile_on_startup=False)) as runtime:
        gate = runtime.post(gate_path, headers=opening)
        assert gate.status_code == 204, gate.text
        assert 'worker.view_opened' in [event['event_type'] for event in store.list_events(worker['worker_id'])]
        # The link's assertion only looks: it cannot change the workspace or confirm a change.
        paused = runtime.post(f"/v1/workers/{worker['worker_id']}/pause", headers=mutation)
        assert paused.status_code == 403 and 'viewer' in paused.text.lower()
        confirmed = runtime.post('/v1/pending-changes/chg_fixture/confirm', headers=confirmation,
                                 json={'confirmation_token': 'fixture-confirmation-token'})
        assert confirmed.status_code == 403, confirmed.text
        # Nor can it read the desktop's interactive credential or gateway, which the service can.
        live_path = f"/v1/workers/{worker['worker_id']}/live"
        viewed = runtime.get(live_path, headers=reading)
        assert viewed.status_code == 200, viewed.text
        assert 'synthetic-desktop-secret' not in viewed.text and '61001' not in viewed.text
        service = runtime.get(live_path, headers={'X-WPR-Token': reading['X-WPR-Token']})
        assert 'synthetic-desktop-secret' in service.text


def test_local_link_viewer_polls_never_heal_while_the_owner_still_does(tmp_path, monkeypatch):
    """A browser holding only a workspace link polls the live view through the UI's local fallback:
    forwarded viewer role headers, no assertion. Those polls must never finish a stale run or start
    its queued successor; the signed-in owner's poll still heals."""
    import os
    from fastapi.testclient import TestClient
    from workers_projects_runtime.api import create_app as runtime_app
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from workers_projects_runtime.store import RunRestorationState

    ui_root = Path(__file__).parents[2] / 'frontends/glass-drive-ui'
    monkeypatch.syspath_prepend(str(ui_root / 'tests'))
    monkeypatch.syspath_prepend(str(ui_root / 'src'))
    from glass_drive_ui.auth_gateway import HumanAuthGateway
    from glass_drive_ui.runtime_client import RuntimeClient
    from glass_drive_ui.server import create_app as ui_app
    from glass_drive_ui.signed_links import sign_link_token
    from test_packaged_local_auth import login

    class FinishedNativeRun(StubRuntime):
        def __init__(self):
            super().__init__()
            self.collected = []

        def collect_completed_run(self, worker, run_id=None):
            self.collected.append(str(worker['worker_id']))
            return {'state': 'completed', 'output_text': 'done', 'error_text': ''}

    _, roots, environments, _, credentials = _local_launch(tmp_path, monkeypatch)
    db_path = str(tmp_path / 'runtime.db')
    _use_environment(monkeypatch, environments['ui'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    gateway = HumanAuthGateway.from_env()
    gateway.provision_local_owner(password=credentials['ui_password'])
    owner, tenant = gateway.local_owner_id, os.environ.get('GLASSHIVE_ENTERPRISE_TENANT_ID') or 'local'

    _use_environment(monkeypatch, environments['runtime'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    store = runtime_app(db_path=db_path, runtime_backend='stub', reconcile_on_startup=False).state.store
    project = store.create_project(owner, 'Live view', 'Watch the work', 'claude-code', tenant_id=tenant)
    worker = store.create_worker(project['project_id'], owner, 'Claude Code', 'main', 'claude-code',
                                 'stub', 'stub', 'stub', tenant_id=tenant)
    stale = store.create_run(worker['worker_id'], project['project_id'], 'finished natively',
                             state=RunRestorationState.RUNNING)
    queued = store.create_run(worker['worker_id'], project['project_id'], 'queued follow-up', state='queued')
    store.update_worker(worker['worker_id'], state='running', last_run_id=stale['run_id'])
    live_path = f"/v1/workers/{worker['worker_id']}/live"

    sent = []

    def relay(self, method, path, **_kwargs):
        sent.append((method, path, self._request_headers() or {}))
        return {}

    monkeypatch.setattr(RuntimeClient, '_request', relay)

    def ui_poll(browser, **params):
        sent.clear()
        polled = browser.get(f"/api/worker/{worker['worker_id']}/live", params=params)
        assert polled.status_code == 200, polled.text
        return next(headers for method, path, headers in sent if (method, path) == ('GET', live_path))

    _use_environment(monkeypatch, environments['ui'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    link = sign_link_token(kind='worker_view', worker_id=worker['worker_id'], tenant_id=tenant, owner_id=owner)
    viewer_poll = ui_poll(TestClient(ui_app()), gh_token=link)
    assert viewer_poll.get('X-Viventium-User-Role') == 'viewer'
    assert 'X-GlassHive-User-Assertion' not in viewer_poll
    signed_in = TestClient(ui_app())
    assert login(signed_in, password=credentials['ui_password']).status_code == 200
    owner_poll = ui_poll(signed_in)
    assert 'X-GlassHive-User-Assertion' in owner_poll

    _use_environment(monkeypatch, environments['runtime'], roots)
    monkeypatch.setenv('VIVENTIUM_ENV_FILE', '')
    runtime = FinishedNativeRun()
    app = runtime_app(db_path=db_path, runtime_backend='stub', runtime=runtime, reconcile_on_startup=False)
    started = []
    monkeypatch.setattr(app.state.service, '_ensure_worker_processor', started.append)
    client = TestClient(app)
    for _ in range(2):
        assert client.get(live_path, headers=viewer_poll).status_code == 200
    assert runtime.collected == [] and started == []
    assert store.get_run(stale['run_id'])['state'] == 'running'
    assert store.get_run(queued['run_id'])['state'] == 'queued'
    assert client.get(live_path, headers=owner_poll).status_code == 200
    assert runtime.collected == [worker['worker_id']] and started == [worker['worker_id']]
    assert store.get_run(stale['run_id'])['state'] == 'completed'


@pytest.mark.parametrize(('declared', 'options', 'isolated'), [
    ('isolated', {'com.docker.network.bridge.enable_icc': 'false'}, True),
    ('isolated', {}, False),
    ('isolated', {'com.docker.network.bridge.enable_icc': 'true'}, False),
    ('', {'com.docker.network.bridge.enable_icc': 'false'}, False),
])
def test_native_launch_needs_a_workers_bridge_that_isolates_containers(tmp_path, monkeypatch, declared, options, isolated):
    from workers_projects_runtime import packaged_linux as substrate
    data, control = tmp_path / 'data', tmp_path / 'control'
    data.mkdir()
    control.mkdir()
    image, controller = 'sha256:' + 'a' * 64, 'b' * 64
    for key, value in {'XPERFECT_EXECUTION_PROFILE': 'local-linux', 'XPERFECT_SHARED_VOLUME_ROOT': str(data.resolve()),
                       'XPERFECT_CONTROL_ROOT': str(control.resolve()), 'XPERFECT_SHARED_IMAGE': image,
                       'XPERFECT_CONTROLLER_ID': controller, 'XPERFECT_SHARED_VOLUME_NAME': 'fixture-data',
                       'XPERFECT_SHARED_NETWORK': 'fixture-workers', 'XPERFECT_WORKER_NETWORK': declared}.items():
        monkeypatch.setenv(key, value)

    class Probed(Exception):
        pass

    def docker(arguments):
        if arguments[:1] == ['info']:
            return json.dumps({'OSType': 'linux', 'CgroupVersion': '2'})
        if arguments[:2] == ['image', 'inspect']:
            return image + '\n'
        if arguments == ['inspect', controller]:
            return json.dumps([{'Id': controller, 'State': {'Running': True},
                                'Mounts': [{'Destination': str(data.resolve()), 'Type': 'volume', 'Name': 'fixture-data'},
                                           {'Destination': str(control.resolve()), 'Type': 'volume', 'Name': 'fixture-control'}],
                                'NetworkSettings': {'Networks': {'fixture-workers': {}}}}])
        if arguments[:2] == ['network', 'inspect']:
            assert arguments[-1] == 'fixture-workers'
            return json.dumps(options)
        if arguments[:1] == ['run']:
            raise Probed  # past the network proof: the live volume challenge comes next
        raise AssertionError(arguments)
    monkeypatch.setattr(substrate, '_docker', docker)
    if isolated:
        with pytest.raises(Probed):
            substrate.account_launcher_from_environment()
    else:
        with pytest.raises(WorkspaceBoxUnavailable, match='refuse traffic between containers'):
            substrate.account_launcher_from_environment()


def test_every_packaged_profile_declares_the_isolated_worker_network():
    module = service_module()
    assert {profile: settings['XPERFECT_WORKER_NETWORK'] for profile, settings in module.PROFILE_SETTINGS.items()} == {
        'local-linux': 'isolated', 'hosted-xfs': 'isolated'}


@pytest.mark.parametrize('config', [
    {'model': 'claude-code:claude-opus-5-5', 'effort': 'medium'},
    {'model': 'm', 'effort': 'e', 'max_goals': 1000, 'wake_on_results': False, 'developer_instructions': '',
     'scope': {'execution_mode': 'docker', 'project_id': 'prj_1', 'workspace_id': 'wsp_1'},
     'routes': [{'id': 'codex', 'profile': 'codex-cli', 'model': 'codex-cli:gpt-6-sol', 'effort': 'medium',
                 'execution_mode': 'docker', 'connection_id': 'acct_1', 'resource_class': 'light'}]},
    {'effort': 'medium'},
    {'model': 'm', 'effort': 'e', 'extra': True},
    {'model': 'm', 'effort': 'e', 'max_goals': 9},
    {'model': 'm', 'effort': 'e', 'scope': {'execution_mode': 'cloud'}},
    {'model': 'm', 'effort': 'e', 'scope': {'workspace_id': 'wsp_1'}},
    {'model': 'm', 'effort': 'e', 'routes': [{'id': 'r', 'profile': 'p', 'model': 'm', 'effort': 'e',
                                              'execution_mode': 'docker', 'extra': 1}]},
    {'model': 'm', 'effort': 'e', 'routes': [{'id': 'r', 'profile': 'p', 'model': 'm', 'effort': 'e'}]},
])
def test_the_launcher_accepts_a_coordinator_config_exactly_when_the_runtime_does(config):
    """The package launcher carries the runtime's coordinator configuration without importing it;
    its shape check must never accept what the runtime would reject."""
    import pydantic
    from workers_projects_runtime.coordinator import CoordinatorConfig

    try:
        CoordinatorConfig.model_validate(config)
        runtime_accepts = True
    except pydantic.ValidationError:
        runtime_accepts = False
    try:
        _launch_module().validate_coordinator_config(config)
        launcher_accepts = True
    except ValueError:
        launcher_accepts = False
    assert launcher_accepts == runtime_accepts
