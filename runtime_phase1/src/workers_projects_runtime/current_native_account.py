"""Explicit local OS sign-in connection; never imports or stores credential bytes."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .control_plane import ControlPlaneError

PREFIX = 'native-home://current-claude/'
MARKER = '_glasshive_current_native_account'


def enabled() -> bool:
    return (os.environ.get('GLASSHIVE_SECURITY_MODE', '').lower() in {'', 'local', 'legacy_compatibility'}
            and os.environ.get('WPR_DEFAULT_EXECUTION_MODE', '').lower() == 'host'
            and not any(os.environ.get(name, '').lower() in {'1','true','yes','on'}
                        for name in ('GLASSHIVE_ENTERPRISE_MODE', 'WPR_ENTERPRISE_MODE'))
            and not os.environ.get('XPERFECT_STORAGE_ROOT'))


def is_current_account(account: dict) -> bool:
    return (account.get('provider') == 'claude' and account.get('auth_method') == 'subscription'
            and str(account.get('secret_locator') or '').startswith(PREFIX))


def require_local(homes=None) -> None:
    if not enabled() or (homes is not None and homes.owner_root_resolver is not None):
        raise ControlPlaneError('Existing OS sign-in is available only on a single-user local host')


@dataclass(frozen=True)
class NativeReadiness:
    state: str
    message: str
    identity: str = field(default='', repr=False)
    auth_source: str = ''

    def public(self) -> dict:
        return {'state': self.state, 'message': self.message, 'auth_source': self.auth_source,
                'install_url': 'https://code.claude.com/docs/en/setup',
                'login_command': 'claude auth login'}


def owner_environment() -> dict[str, str]:
    from .profile_runtime import _native_cli_status_env
    environment = _native_cli_status_env()
    environment.pop('CODEX_HOME', None)
    home = Path(environment.get('HOME', ''))
    if not home.is_absolute() or home.is_symlink() or not home.is_dir():
        raise ControlPlaneError('The local native sign-in home is unavailable')
    return environment


def readiness() -> NativeReadiness:
    require_local()
    from .provider_accounts import provider_setup_binary
    binary = provider_setup_binary('claude')
    if not binary:
        return NativeReadiness('cli_missing', 'Install Claude Code, then choose Check again')
    try:
        result = subprocess.run([binary, 'auth', 'status', '--json'], env=owner_environment(),
                                capture_output=True, text=True, timeout=8, check=False)
        if len(result.stdout) > 32768:
            raise ValueError('oversized status')
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError('invalid status')
    except subprocess.TimeoutExpired:
        return NativeReadiness('unavailable', 'Claude sign-in check timed out. Check again')
    except (OSError, ValueError, ControlPlaneError):
        return NativeReadiness('unavailable', 'Claude sign-in could not be checked. Run claude doctor, then check again')
    if result.returncode != 0 or payload.get('loggedIn') is not True:
        return NativeReadiness('sign_in_required', 'Sign in with claude auth login, then choose Check again')
    if payload.get('authMethod') != 'claude.ai' or payload.get('apiProvider') != 'firstParty':
        return NativeReadiness('different_auth_route', 'Claude selected another authentication method. Use your Claude subscription sign-in or connect its supported account route')
    email, organization = payload.get('email'), payload.get('orgId')
    if not isinstance(email, str) or not email.strip() or not isinstance(organization, str) or not organization.strip():
        return NativeReadiness('identity_unavailable', 'Claude did not report a stable account identity. Update Claude Code, then check again')
    identity = hashlib.sha256(json.dumps([email.strip().casefold(), organization], separators=(',', ':')).encode()).hexdigest()
    return NativeReadiness('ready_to_try', 'Existing Claude sign-in is ready to try', identity, 'current_os_claude_subscription')


def verify_identity(expected: str) -> NativeReadiness:
    status = readiness()
    if status.state != 'ready_to_try':
        raise ControlPlaneError(status.message)
    if status.identity != expected:
        raise ControlPlaneError('The OS Claude sign-in changed. Reconnect the selected account; another account will not be used')
    return status


def bound_identity(worker: dict, runtime_name: str = 'claude-code') -> str:
    from .mission_provider_accounts import mission_provider_account_selection
    require_local()
    selection = mission_provider_account_selection(worker)
    marker = worker.get(MARKER)
    if (runtime_name != 'claude-code' or worker.get('execution_mode') != 'host'
            or not worker.get('_glasshive_provider_account_bound') or selection is None
            or not isinstance(marker, dict) or marker.get('account_id') != selection.account_id
            or not marker.get('lease_id')):
        raise ControlPlaneError('Existing native sign-in requires its validated local account lease')
    assertion = worker.get('_glasshive_current_native_assert_lease')
    if not callable(assertion):
        raise ControlPlaneError('Existing native sign-in lease authority is unavailable')
    assertion()
    identity = marker.get('identity')
    if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
        raise ControlPlaneError('Existing native sign-in account identity is invalid')
    return identity


# These inputs select credentials, organizations, or API routes independently of
# the verified OS subscription. Keep this policy specific to current-native use.
_CURRENT_NATIVE_AUTH_ENV = frozenset({
    'ANTHROPIC_CUSTOM_HEADERS', 'CLAUDE_CODE_USE_ANTHROPIC_AWS',
    'CLAUDE_CODE_USE_MANTLE', 'ANTHROPIC_AWS_API_KEY', 'ANTHROPIC_AWS_BASE_URL',
    'ANTHROPIC_AWS_WORKSPACE_ID', 'ANTHROPIC_BEDROCK_BASE_URL',
    'ANTHROPIC_BEDROCK_MANTLE_BASE_URL', 'ANTHROPIC_FOUNDRY_API_KEY',
    'ANTHROPIC_FOUNDRY_AUTH_TOKEN', 'ANTHROPIC_FOUNDRY_BASE_URL',
    'ANTHROPIC_FOUNDRY_RESOURCE', 'ANTHROPIC_VERTEX_BASE_URL',
    'ANTHROPIC_VERTEX_PROJECT_ID', 'ANTHROPIC_WORKSPACE_ID',
    'CLAUDE_CODE_SKIP_BEDROCK_AUTH', 'CLAUDE_CODE_SKIP_VERTEX_AUTH',
    'CLAUDE_CODE_SKIP_FOUNDRY_AUTH', 'CLAUDE_CODE_SKIP_MANTLE_AUTH',
})
_CURRENT_NATIVE_OWNER_ENV = frozenset({'HOME', 'USER', 'LOGNAME'})
_CURRENT_NATIVE_AUTH_SETTINGS = frozenset({'apiKeyHelper', 'awsAuthRefresh', 'awsCredentialExport'})


def _clear_current_auth_environment(environment: dict) -> None:
    from .mission_provider_accounts import _CLAUDE_CONFLICTING_ENV
    for name in _CLAUDE_CONFLICTING_ENV | _CURRENT_NATIVE_AUTH_ENV:
        environment.pop(name, None)


def project_current_settings(worker: dict, settings: dict) -> None:
    """Explicit --settings can override process env; bind both producer paths."""
    bound_identity(worker)
    for name in _CURRENT_NATIVE_AUTH_SETTINGS:
        settings.pop(name, None)
    environment = settings.get('env')
    if environment is not None:
        if not isinstance(environment, dict):
            raise ControlPlaneError('Claude environment settings must be an object')
        _clear_current_auth_environment(environment)
        for name in _CURRENT_NATIVE_OWNER_ENV | {'CLAUDE_CONFIG_DIR'}:
            environment.pop(name, None)


def project_current_environment(worker: dict, environment: dict, runtime_name: str) -> None:
    expected = bound_identity(worker, runtime_name)
    verify_identity(expected)
    _clear_current_auth_environment(environment)
    # Keep workspace sessions/config isolated; use the provider's existing OS store.
    trusted_owner = owner_environment()
    for name in _CURRENT_NATIVE_OWNER_ENV:
        environment.pop(name, None)
        if name in trusted_owner:
            environment[name] = trusted_owner[name]
    environment['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = ''


class CurrentNativeAccountManager:
    def __init__(self, store, homes):
        self.store, self.homes = store, homes

    def connect(self, *, tenant_id: str, owner_id: str) -> dict:
        require_local(self.homes)
        status = readiness()
        if status.state != 'ready_to_try':
            return {'complete': False, **status.public()}
        account = self.store.create_provider_account(tenant_id=tenant_id, owner_id=owner_id,
            provider='claude', label='Existing Claude sign-in', auth_method='subscription',
            platform_support='local_current_native', secret_locator=PREFIX+status.identity)
        return self.verify(account_id=account['account_id'], tenant_id=tenant_id, owner_id=owner_id)

    def verify(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict:
        require_local(self.homes)
        account = self.store.get_provider_account_record(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        if not account or not is_current_account(account):
            raise ControlPlaneError('Existing native account was not found for this user')
        lease = self.store.acquire_provider_lease(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id,
            lane='claude-code:current-native:verify', worker_id='current-native-connect', run_id='verify', ttl_seconds=30,
            allowed_statuses=('ready','disconnected','action_required'), required_recovery_code='')
        try:
            try:
                status = verify_identity(account['secret_locator'][len(PREFIX):])
            except ControlPlaneError as exc:
                self.store.update_provider_account_status(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id,
                    status='action_required', reconnect_reason=str(exc))
                return {'account_id': account_id, 'status': 'action_required', 'complete': True, 'message': str(exc)}
            result = self.store.update_provider_account_status(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id,
                status='ready', reconnect_reason='', verified=True)
            return {'account_id': account_id, 'status': 'ready', 'complete': True, **status.public(), 'account': result}
        finally:
            self.store.release_provider_lease(lease_id=lease['lease_id'], tenant_id=tenant_id, owner_id=owner_id)

    def disconnect(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict:
        require_local(self.homes)
        account = self.store.get_provider_account_record(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        if not account or not is_current_account(account):
            raise ControlPlaneError('Existing native account was not found for this user')
        # Store atomically refuses every unreleased current-native lease, even expired.
        result = self.store.disconnect_provider_account(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id,
            reconnect_reason='Disconnected from xPerfect; native Claude sign-in was retained')
        return {'account_id': account_id, 'status': result['status'], 'complete': True, 'message': result['reconnect_reason']}
