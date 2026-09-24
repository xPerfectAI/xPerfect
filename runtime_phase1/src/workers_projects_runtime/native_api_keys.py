"""Owner-scoped API keys for isolated native CLI routes.

Credentials stay in the existing private account home, never the metadata store.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .control_plane import ControlPlaneError
from .native_key_reader import NativeKeyReadError, key_value, validate_key

_KEY_FILE = "api-key.json"
_PROVIDERS = {
    "codex": ("OPENAI_API_KEY", "https://api.openai.com/v1/models"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1/models"),
    "claude": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/models"),
    "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/models"),
    "grok": ("XAI_API_KEY", "https://api.x.ai/v1/models"),
    "xai": ("XAI_API_KEY", "https://api.x.ai/v1/models"),
}
_RUNTIME_PROVIDERS = {"codex-cli": "codex", "claude-code": "claude", "grok-build": "grok"}


def enabled() -> bool:
    if os.environ.get("GLASSHIVE_ENABLE_NATIVE_API_KEYS", "").lower() not in {"1", "true", "yes", "on"}:
        return False
    security = os.environ.get("GLASSHIVE_SECURITY_MODE", "").lower()
    execution = os.environ.get("WPR_DEFAULT_EXECUTION_MODE", "").lower()
    profile = os.environ.get("XPERFECT_EXECUTION_PROFILE", "").lower()
    if execution == "docker":
        return (
            profile in {"hosted-xfs", "local-linux"}
            and os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "").lower() == "per_worker_container"
            and (security == "multi_user" or (security == "local" and profile == "local-linux"))
        )
    return execution == "host" and security != "multi_user" and not profile


def support(provider: str, homes=None) -> str:
    """Advertise a native key only with the actual contained account capability."""
    from .provider_accounts import provider_setup_binary

    if provider not in _PROVIDERS or not enabled():
        return "unavailable"
    if os.environ.get("GLASSHIVE_SECURITY_MODE", "").lower() == "multi_user":
        if homes is None or getattr(homes, "owner_root_resolver", None) is None:
            return "isolated_substrate_required"
    if homes is not None:
        try:
            homes.require_native_launcher()
        except ControlPlaneError:
            return "isolated_substrate_required"
    elif os.environ.get("XPERFECT_EXECUTION_PROFILE", "") in {"hosted-xfs", "local-linux"}:
        return "isolated_substrate_required"
    return "supported" if provider_setup_binary(provider) else "setup_cli_required"


def native_key_account(account: dict) -> bool:
    return account.get("auth_method") == "api_key" and str(account.get("secret_locator") or "") == "native-home://api-key"


def _value(value: object) -> str:
    try:
        return validate_key(value)
    except NativeKeyReadError as exc:
        raise ControlPlaneError(str(exc)) from exc


def read_key(path: Path) -> str:
    """Refuse redirected/shared credentials; return bytes only to the trusted child projection."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("unsafe file")
            payload = json.loads(stream.read(16385))
            return _value(payload["value"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ControlPlaneError("Private API key is missing or unsafe; reconnect this account") from exc


def project_key(worker: dict, env: dict[str, str], runtime_name: str) -> None:
    path = worker.get("_glasshive_provider_api_key_file")
    if path is None:
        return
    if not enabled() or not worker.get("_glasshive_provider_account_bound"):
        raise ControlPlaneError("Native API keys require a validated account binding")
    provider = _RUNTIME_PROVIDERS.get(runtime_name)
    if provider is None:
        raise ControlPlaneError("This runtime does not support a native API key")
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY"):
        env.pop(name, None)
    if worker.get("execution_mode") == "docker":
        home = Path(str(worker.get("_glasshive_provider_account_mount_host") or ""))
        target = str(worker.get("_glasshive_provider_account_mount_target") or "")
        key_file = Path(str(path))
        if (target != "/workspace/.provider-account" or not home.is_absolute()
                or key_file != home / _KEY_FILE):
            raise ControlPlaneError("Native API key is outside the selected account mount")
        read_key(key_file)
        env["GLASSHIVE_NATIVE_API_KEY_FILE"] = f"{target}/{_KEY_FILE}"
        env["GLASSHIVE_NATIVE_API_KEY_PROVIDER"] = provider
        return
    if worker.get("execution_mode") != "host":
        raise ControlPlaneError("Native API keys require a supported execution substrate")
    env[_PROVIDERS[provider][0]] = read_key(Path(str(path)))
    if provider == "claude":
        _require_claude_key_route(env, Path(str(path)).parent)
    # Codex exec reads CODEX_API_KEY directly; OPENAI_API_KEY remains its native
    # provider setting. Neither value is placed on the stored worker record.
    if provider == "codex":
        env["CODEX_API_KEY"] = env["OPENAI_API_KEY"]


def _container_key_value(
    path: str, *, expected_mount: str = "/workspace/.provider-account"
) -> str:
    """Read only the exact private key mount inside a leased worker container."""
    try:
        return key_value(path, expected_mount=expected_mount)
    except NativeKeyReadError as exc:
        raise ControlPlaneError(str(exc)) from exc


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    try:
        sys.stdout.write(_container_key_value(sys.argv[1]))
    except ControlPlaneError:
        raise SystemExit(1) from None


def _require_claude_key_route(env: dict[str, str], home: Path, homes=None, lease_id: str = "") -> None:
    from .provider_accounts import provider_setup_binary
    binary = provider_setup_binary("claude")
    if not binary:
        raise ControlPlaneError("Install Claude Code before connecting its key")
    try:
        runner = subprocess.run if homes is None else lambda command, **options: homes.run_native(command, account_home=home, purpose="api-key-verify", lease_id=lease_id, **options)
        result = runner([binary, "auth", "status", "--json"], env=env, cwd=home,
                        capture_output=True, text=True, timeout=12, check=False)
        status = json.loads(result.stdout)
        if result.returncode != 0 or status.get("authMethod") != "api_key" or status.get("apiKeySource") != "ANTHROPIC_API_KEY":
            raise ValueError("another auth route selected")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ControlPlaneError("Claude Code did not select this API key. Check the host's provider policy and native sign-in settings") from exc


def _prepare_native_key(provider: str, value: str, home: Path, homes, lease_id: str = "") -> None:
    from .provider_accounts import PROVIDER_SETUP_ENV_ALLOWLIST, provider_setup_binary
    env = {name: content for name, content in os.environ.items() if name in PROVIDER_SETUP_ENV_ALLOWLIST}
    env.update(homes.runtime_environment(provider=provider, account_home=home))
    env["HOME"] = str(home)
    if provider in {"codex", "openai"}:
        command = [provider_setup_binary(provider), "-c", 'cli_auth_credentials_store="file"', "login", "--with-api-key"]
        try:
            result = homes.run_native(command, account_home=home, purpose="api-key-login", lease_id=lease_id, input=value + "\n", text=True, env=env, cwd=home,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=False)
            if result.returncode != 0:
                raise ValueError("native login failed")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ControlPlaneError("Codex could not save this key in its private native home") from exc
    elif provider in {"claude", "anthropic"}:
        env["ANTHROPIC_API_KEY"] = value
        _require_claude_key_route(env, home, homes, lease_id)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_key(provider: str, value: str) -> tuple[bool, str]:
    """An authentication check, not a model entitlement or completed-work claim."""
    env_name, url = _PROVIDERS[provider]
    headers = {"Accept": "application/json"}
    if env_name == "ANTHROPIC_API_KEY":
        headers.update({"x-api-key": value, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {value}"
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=12) as response:
            if response.status != 200:
                return False, "Provider did not accept the connection check"
            payload = json.loads(response.read(1_048_577))
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                return False, "Provider returned an invalid connection response"
    except HTTPError as exc:
        if exc.code in {401, 403}:
            return False, "Provider rejected this key or its permission to list models"
        if exc.code == 429:
            return False, "Provider rate limit reached; retry the connection check"
        return False, "Provider connection check failed; retry later"
    except (OSError, URLError, ValueError):
        return False, "Provider could not be reached; check the network and retry"
    return True, "API key accepted. Model access and available credit are checked when work runs."


class NativeApiKeyManager:
    def __init__(self, store, homes):
        self.store, self.homes = store, homes

    def connect(self, *, account_id: str, tenant_id: str, owner_id: str, value: str | None = None) -> dict:
        account = self.store.get_provider_account_record(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        if account is None:
            raise ControlPlaneError("Provider account not found for this user")
        provider = str(account.get("provider") or "")
        if not native_key_account(account) or support(provider, self.homes) != "supported":
            raise ControlPlaneError("Native API-key connection is unavailable for this account")
        if value is not None:
            value = _value(value)
        lease = self.store.acquire_provider_lease(
            account_id=account_id, tenant_id=tenant_id, owner_id=owner_id,
            lane="provider-key", worker_id="provider-key", run_id=f"key:{account_id}",
            ttl_seconds=60, allowed_statuses=("disconnected", "ready", "action_required", "error", "unavailable"),
        )
        home = None
        try:
            home = self.homes.ensure_home(tenant_id=tenant_id, owner_id=owner_id, account_id=account_id, provider=provider)
            self.homes.tighten_permissions(account_home=home)
            key = value if value is not None else read_key(home / _KEY_FILE)
            accepted, message = check_key(provider, key)
            self.store.heartbeat_provider_lease(lease_id=lease["lease_id"], tenant_id=tenant_id, owner_id=owner_id, ttl_seconds=60)
            if accepted:
                _prepare_native_key(provider, key, home, self.homes, str(lease["lease_id"]))
            if accepted and value is not None:
                fd, temporary = tempfile.mkstemp(prefix=".key-", dir=home)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump({"value": key}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, home / _KEY_FILE)
                    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                self.homes.tighten_permissions(account_home=home)
            updated = self.store.update_provider_account_status(
                account_id=account_id, tenant_id=tenant_id, owner_id=owner_id,
                status="ready" if accepted else "action_required", reconnect_reason="" if accepted else message,
                verified=accepted,
            )
            return {"account_id": account_id, "status": updated["status"], "complete": True, "message": message}
        except Exception:
            self.store.update_provider_account_status(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id, status="action_required", reconnect_reason="API key needs a safe connection check")
            raise
        finally:
            if home is not None:
                self.homes.assert_native_quiescent(home)
            self.store.release_provider_lease(lease_id=lease["lease_id"], tenant_id=tenant_id, owner_id=owner_id)
