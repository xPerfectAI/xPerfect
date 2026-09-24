from __future__ import annotations

import hashlib
import json
import os
import pty
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import fcntl

from .account_native_launch import AccountLaunchPurpose, GuardedAccountLauncher, NativeAccountLaunch

from .control_plane import (
    LEGACY_CREDENTIAL_CLEANUP_REASON,
    ControlPlaneConflict,
    ControlPlaneError,
)
from .inference_broker import (
    InferenceBrokerError,
    inference_broker_config_from_environment,
)


SAFE_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ANSI_ESCAPE = re.compile(
    r"\x1B(?:\][^\x07]*?(?:\x07|\x1B\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)
MAX_SETUP_OUTPUT_CHARS = 32_000
MAX_SETUP_INPUT_BYTES = 1_024
PROVIDER_VERIFY_HEARTBEAT_INTERVAL_SECONDS = 10.0
PROVIDER_SETUP_ENV_ALLOWLIST = {
    "ALL_PROXY",
    "COLORTERM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NODE_EXTRA_CA_CERTS",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}

_SETUP_URL = re.compile(r"https://[^\s'\"<>]+")
_CODEX_DEVICE_CODE = re.compile(
    r"(?i:one-time\s+code(?:\s*\([^)]*\))?)\s*:?\s*"
    r"([A-Z0-9]{4,8}-[A-Z0-9]{4,8})(?![A-Z0-9-])",
)
_CODEX_SECURITY_SETTINGS_URL = "https://chatgpt.com/#settings/Security"


def _provider_setup_guidance(provider: str, output: str) -> dict[str, str | bool]:
    """Extract bounded, clickable guidance without trusting arbitrary CLI output as a URL."""

    normalized_provider = str(provider or "").strip().lower()
    canonical_provider = (
        "codex"
        if normalized_provider in {"codex", "openai"}
        else "claude"
        if normalized_provider in {"claude", "anthropic"}
        else normalized_provider or "unknown"
    )
    setup_url = ""
    for match in _SETUP_URL.finditer(str(output or "")):
        candidate = match.group(0).rstrip(".,;:)")
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            continue
        hostname = str(parsed.hostname or "").lower()
        if normalized_provider in {"codex", "openai"}:
            if hostname == "auth.openai.com" and parsed.path.rstrip("/") == "/codex/device":
                setup_url = candidate
                break
        elif normalized_provider in {"grok", "xai"}:
            if hostname in {"auth.x.ai", "accounts.x.ai"}:
                setup_url = candidate
                break
        elif normalized_provider in {"claude", "anthropic"}:
            is_native_claude_login = (
                hostname == "claude.com"
                and parsed.path.rstrip("/") == "/cai/oauth/authorize"
            )
            if hostname in {"claude.ai", "console.anthropic.com"} or is_native_claude_login:
                setup_url = candidate
                break

    setup_code = ""
    if setup_url and normalized_provider in {"codex", "openai"}:
        code_match = _CODEX_DEVICE_CODE.search(str(output or ""))
        if code_match:
            setup_code = code_match.group(1).upper()

    input_required = False
    if setup_url and normalized_provider in {"claude", "anthropic"}:
        code_values = parse_qs(urlsplit(setup_url).query).get("code", [])
        input_required = any(str(value).lower() == "true" for value in code_values)

    return {
        "provider": canonical_provider,
        "setup_url": setup_url,
        "setup_code": setup_code,
        "help_url": (
            _CODEX_SECURITY_SETTINGS_URL
            if normalized_provider in {"codex", "openai"}
            else ""
        ),
        "input_required": input_required,
    }


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def provider_setup_binary(provider: str) -> str | None:
    """Resolve the native setup CLI from the same canonical worker settings used at runtime."""

    normalized = str(provider or "").strip().lower()
    if normalized in {"codex", "openai"}:
        executable = "codex"
        env_names = ("WPR_CODEX_BIN", "WPR_CODEX_CLI_PATH")
    elif normalized in {"claude", "anthropic"}:
        executable = "claude"
        env_names = ("WPR_CLAUDE_CODE_BIN", "WPR_CLAUDE_CODE_PATH")
    elif normalized in {"grok", "xai"}:
        executable = "grok"
        env_names = ("WPR_GROK_BIN",)
    else:
        return None
    configured = [str(os.environ.get(name) or "").strip() for name in env_names]
    configured = [value for value in configured if value]
    for value in configured:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            continue
        if resolved := shutil.which(value):
            return resolved
    # An explicit but invalid binary is deployment drift; do not silently select another CLI.
    if configured:
        return None
    return shutil.which(executable)


def provider_platform_support(
    *,
    provider: str,
    auth_method: str,
    platform_name: str | None = None,
    native_homes=None,
) -> str:
    """Return deployment-owned support truth; clients cannot opt themselves into a route."""

    normalized_provider = str(provider or "").strip().lower()
    normalized_method = str(auth_method or "").strip().lower()
    current_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_method == "api_key":
        from .native_api_keys import enabled, support
        if enabled() or str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user":
            return support(normalized_provider, native_homes)
    if normalized_method in {"api_key", "enterprise_route"}:
        if normalized_provider in {"codex", "openai"}:
            try:
                broker_config = inference_broker_config_from_environment()
            except InferenceBrokerError:
                return "broker_configuration_invalid"
            if broker_config is not None:
                return "supported"
    if normalized_method == "api_key":
        return (
            "managed_connection_required"
            if _env_enabled("GLASSHIVE_PROVIDER_SECRET_STORE_ENABLED")
            else "secret_store_required"
        )
    if normalized_method == "enterprise_route":
        return "managed_connection_required"
    if normalized_method != "subscription":
        return "supported"
    if str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user":
        isolation_mode = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION") or ""
        ).strip().lower()
        if isolation_mode != "per_worker_container":
            return "isolated_substrate_required"
    if normalized_provider in {"claude", "anthropic"}:
        if current_platform == "darwin":
            return "unsupported_macos_host"
        if not _env_enabled("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH"):
            return "provider_permission_required"
        return "supported" if provider_setup_binary(normalized_provider) else "setup_cli_required"
    if normalized_provider in {"codex", "openai"}:
        if not _env_enabled("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS"):
            return "proof_required"
        return "supported" if provider_setup_binary(normalized_provider) else "setup_cli_required"
    if normalized_provider in {"grok", "xai"}:
        return "supported" if provider_setup_binary(normalized_provider) else "setup_cli_required"
    return "proof_required"


def _identity_segment(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:24]


def _valid_account_id(value: str) -> bool:
    account_id = str(value or "")
    return bool(SAFE_ACCOUNT_ID.fullmatch(account_id) and account_id not in {".", ".."})


class ProviderAccountHomeManager:
    """Owns provider-native homes outside workspace storage."""

    def __init__(self, root: Path, *, owner_root_resolver: Callable[[str, str], Path] | None = None,
                 native_launcher: GuardedAccountLauncher | None = None) -> None:
        self.root = Path(root)
        self.owner_root_resolver = owner_root_resolver
        if native_launcher is not None:
            from .contained_account_launch import ContainedAccountLauncher
            if not isinstance(native_launcher, ContainedAccountLauncher):
                raise ControlPlaneError('Account setup requires the typed contained launcher capability')
        self.native_launcher = native_launcher
        self._owner_roots: dict[tuple[str, str], tuple[Path, int, int]] = {}
        self._owner_roots_lock = threading.RLock()
        if self.root.is_symlink():
            raise ControlPlaneError("Provider account root is not a safe managed directory")
        if owner_root_resolver is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._private(self.root)

    def _private(self, path: Path) -> None:
        if os.name != "nt":
            path.chmod(0o700)

    def _resolved_owner_root(self, tenant_id: str, owner_id: str) -> Path:
        if self.owner_root_resolver is None or not tenant_id or not owner_id:
            raise ControlPlaneError("A trusted provisioned owner root is required")
        root = self.owner_root_resolver(tenant_id, owner_id)
        try:
            if not isinstance(root, Path) or not root.is_absolute() or root != root.resolve(strict=True):
                raise ControlPlaneError("Provisioned owner root must be an absolute real directory")
            info = root.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ControlPlaneError("Provisioned owner root must belong to the trusted service")
        except OSError as exc:
            raise ControlPlaneError("Provisioned owner root is unavailable") from exc
        identity = (root, info.st_dev, info.st_ino)
        with self._owner_roots_lock:
            previous = self._owner_roots.setdefault((tenant_id, owner_id), identity)
            if previous != identity:
                raise ControlPlaneError("Provider owner root changed; an explicit stopped-state migration is required")
        return root

    def ensure_home(self, *, tenant_id: str, owner_id: str, account_id: str, provider: str) -> Path:
        if provider not in {"codex", "claude", "openai", "anthropic", "grok", "xai", "custom"}:
            raise ControlPlaneError("Unsupported provider")
        account_home = self.account_home_path(tenant_id=tenant_id, owner_id=owner_id, account_id=account_id)
        self.assert_native_quiescent(account_home)
        directories = ([account_home.parent, account_home] if self.owner_root_resolver is not None
                       else [account_home.parent.parent, account_home.parent, account_home])
        for directory in directories:
            if directory.is_symlink():
                raise ControlPlaneError("Provider account home is not a safe managed directory")
            directory.mkdir(exist_ok=True)
            self._private(directory)
        provider_home = account_home / ("codex" if provider in {"codex", "openai"} else "grok" if provider in {"grok", "xai"} else "claude")
        if provider_home.is_symlink():
            raise ControlPlaneError("Provider account home is not a safe managed directory")
        provider_home.mkdir(exist_ok=True)
        self._private(provider_home)
        return account_home

    def account_home_path(self, *, tenant_id: str, owner_id: str, account_id: str) -> Path:
        if not _valid_account_id(account_id):
            raise ControlPlaneError("Provider account id is invalid")
        legacy = self.root / _identity_segment(tenant_id) / _identity_segment(owner_id) / account_id
        if self.owner_root_resolver is None:
            return legacy
        if os.path.lexists(legacy):
            raise ControlPlaneError("Existing provider account storage requires an explicit stopped-state migration")
        root = self._resolved_owner_root(tenant_id, owner_id)
        accounts = root / "provider-accounts"
        if os.path.lexists(accounts):
            info = accounts.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ControlPlaneError("Provider account directory is not private trusted storage")
        return accounts / account_id

    def remove_home(self, *, tenant_id: str, owner_id: str, account_id: str) -> None:
        account_home = self.account_home_path(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
        )
        self.assert_native_quiescent(account_home)
        managed_root = account_home.parent if self.owner_root_resolver is not None else self.root
        if not account_home.exists() and not account_home.is_symlink():
            return
        if account_home.is_symlink():
            resolved_root = managed_root.resolve(strict=True)
            resolved_parent = account_home.parent.resolve(strict=True)
            if resolved_root != resolved_parent and resolved_root not in resolved_parent.parents:
                raise ControlPlaneError(
                    "Provider account home is outside the managed credential root"
                )
            account_home.unlink()
            return
        resolved_root = managed_root.resolve(strict=True)
        resolved_home = account_home.resolve(strict=True)
        if resolved_root not in resolved_home.parents:
            raise ControlPlaneError("Provider account home is outside the managed credential root")
        shutil.rmtree(resolved_home)

    def runtime_environment(self, *, provider: str, account_home: Path) -> dict[str, str]:
        if provider in {"codex", "openai"}:
            target = account_home / "codex"
            target.mkdir(parents=True, exist_ok=True)
            self._private(target)
            return {"CODEX_HOME": str(target)}
        if provider in {"claude", "anthropic"}:
            target = account_home / "claude"
            target.mkdir(parents=True, exist_ok=True)
            self._private(target)
            return {
                "CLAUDE_CONFIG_DIR": str(target),
                "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(target),
            }
        if provider in {"grok", "xai"}:
            target = account_home / "grok"
            target.mkdir(parents=True, exist_ok=True)
            self._private(target)
            # Credentials are shared only behind the account lease; native sessions
            # and model configuration stay in each worker's private GROK_HOME.
            return {"GROK_AUTH_PATH": str(target / "auth.json")}
        raise ControlPlaneError("Unsupported provider account home")

    def prepare_interactive_home(self, *, provider: str, account_home: Path) -> None:
        """Make a verified Claude login immediately reusable by its interactive CLI."""

        if provider not in {"claude", "anthropic"}:
            return
        config_dir = account_home / "claude"
        config_path = config_dir / ".claude.json"
        try:
            metadata = config_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size > 1_048_576
            ):
                raise ControlPlaneError("Claude account state is not a safe managed file")
            state = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlPlaneError("Claude account state is unavailable") from exc
        if not isinstance(state, dict):
            raise ControlPlaneError("Claude account state is invalid")
        if state.get("hasCompletedOnboarding") is True:
            return
        state["hasCompletedOnboarding"] = True
        serialized = json.dumps(state, indent=2, sort_keys=True) + "\n"
        temp_fd, temp_name = tempfile.mkstemp(
            dir=config_dir,
            prefix=".glasshive-claude-onboarding-",
        )
        try:
            os.fchmod(temp_fd, 0o600)
            with os.fdopen(temp_fd, "w", encoding="utf-8", closefd=True) as handle:
                temp_fd = -1
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, config_path)
            directory_fd = os.open(
                config_dir,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _managed_root(self, account_home: Path) -> Path:
        if self.owner_root_resolver is None:
            return self.root.resolve(strict=True)
        with self._owner_roots_lock:
            for root, device, inode in self._owner_roots.values():
                if account_home.parent == root / "provider-accounts" and _valid_account_id(account_home.name):
                    info = root.stat()
                    if (info.st_dev, info.st_ino) == (device, inode) and root == root.resolve(strict=True):
                        return root
        raise ControlPlaneError("Account home is not in a verified provisioned owner root")

    def require_native_launcher(self) -> None:
        profile = os.environ.get('XPERFECT_EXECUTION_PROFILE', '')
        if profile not in {'', 'local-linux', 'hosted-xfs'}:
            raise ControlPlaneError('Unknown packaged execution profile')
        if (self.owner_root_resolver is not None or profile) and self.native_launcher is None:
            raise ControlPlaneError("Managed account setup requires the verified native quota launch guard")

    def assert_native_quiescent(self, account_home: Path) -> None:
        if self.native_launcher is not None:
            self.native_launcher.assert_quiescent(account_home)

    def _native_request(self, command: list[str], *, account_home: Path, purpose: AccountLaunchPurpose,
                        environment: dict[str, str], lease_id: str = "") -> NativeAccountLaunch:
        self.require_native_launcher()
        if self.native_launcher is not None:
            root = self._managed_root(account_home)
            try:
                relative = account_home.relative_to(root)
            except ValueError:
                raise ControlPlaneError('Native account home is outside its trusted root') from None
            expected_depth = 2 if self.owner_root_resolver is not None else 3
            if (not account_home.is_absolute() or account_home != account_home.resolve(strict=True)
                    or len(relative.parts) != expected_depth or not _valid_account_id(relative.name)):
                raise ControlPlaneError('Native account home is not a canonical managed account')
            if (not command or not all(isinstance(item, str) and "\0" not in item for item in command)
                    or not Path(command[0]).is_absolute()):
                raise ControlPlaneError("Guarded native setup requires a trusted absolute executable")
            if any(name.startswith("LD_") or name in {"PYTHONHOME", "PYTHONPATH"} for name in environment):
                raise ControlPlaneError("Unsafe loader environment is not allowed for managed account setup")
        return NativeAccountLaunch(account_home, tuple(command), dict(environment), purpose, lease_id)

    def popen_native(self, command: list[str], *, account_home: Path, purpose: AccountLaunchPurpose, **options):
        environment = dict(options.pop("env", {}))
        options.setdefault("cwd", str(account_home))
        if self.native_launcher is not None and (options.get("shell") or Path(options["cwd"]) != account_home):
            raise ControlPlaneError("Managed native setup requires its account directory and direct execution")
        request = self._native_request(command, account_home=account_home, purpose=purpose, environment=environment, lease_id=options.pop("lease_id", ""))
        if self.native_launcher is None:
            return subprocess.Popen(command, env=environment, **options)
        from .contained_account_launch import ContainedAccountProcess
        process = self.native_launcher.popen(request, **options)
        if not isinstance(process, ContainedAccountProcess):
            raise ControlPlaneError('Native account launch did not return a contained process handle')
        return process

    def run_native(self, command: list[str], *, account_home: Path, purpose: AccountLaunchPurpose, **options):
        environment = dict(options.pop("env", {}))
        options.setdefault("cwd", str(account_home))
        if self.native_launcher is not None and (options.get("shell") or Path(options["cwd"]) != account_home):
            raise ControlPlaneError("Managed native setup requires its account directory and direct execution")
        request = self._native_request(command, account_home=account_home, purpose=purpose, environment=environment, lease_id=options.pop("lease_id", ""))
        if self.native_launcher is None:
            return subprocess.run(command, env=environment, **options)
        result = self.native_launcher.run(request, **options)
        self.assert_native_quiescent(account_home)
        return result

    def tighten_permissions(self, *, account_home: Path) -> None:
        """Validate and privatize credential state through no-follow directory descriptors."""

        if os.name == "nt":
            return
        self.assert_native_quiescent(account_home)
        resolved_root = self._managed_root(account_home)
        lexical_home = Path(os.path.abspath(account_home))
        try:
            relative_home = lexical_home.relative_to(resolved_root)
        except ValueError as exc:
            raise ControlPlaneError(
                "Provider account home is outside the managed credential root"
            ) from exc
        if not relative_home.parts:
            raise ControlPlaneError("Provider account home is too broad")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(resolved_root, directory_flags)
        try:
            for part in relative_home.parts:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            self._tighten_directory_fd(directory_fd)
        except (OSError, ValueError) as exc:
            raise ControlPlaneError(
                "Provider account home contains unsafe credential state"
            ) from exc
        finally:
            os.close(directory_fd)

    @staticmethod
    def _is_codex_arg0_helper(directory_fd: int, name: str, relative: tuple[str, ...], entry) -> bool:
        # Codex creates argv[0] aliases to its executable inside this exact temp subtree.
        # These are executable aliases, never credential files; do not follow or chmod them.
        if (len(relative) != 4 or relative[:3] != ("codex", "tmp", "arg0")
                or not relative[3].startswith("codex-arg0")
                or not relative[3][len("codex-arg0"):].isalnum()
                or name not in {"apply_patch", "applypatch", "codex-execve-wrapper", "codex-linux-sandbox"}
                or entry.st_nlink != 1):
            return False
        binary = provider_setup_binary("codex")
        if not binary:
            return False
        target = os.readlink(name, dir_fd=directory_fd)
        if not Path(target).is_absolute():
            return False
        try:
            expected = Path(binary).resolve(strict=True)
            actual = Path(target).resolve(strict=True)
            if not expected.is_file() or actual != Path(target):
                return False
            if actual != expected:
                # The Codex npm launcher creates argv[0] aliases to its native
                # executable, which lives inside the install's trusted vendor
                # package rather than at the launcher path.
                package_root = expected.parent.parent
                parts = actual.relative_to(package_root).parts
                if not (
                    len(parts) == 7
                    and parts[:2] == ("node_modules", "@openai")
                    and parts[2].startswith("codex-")
                    and parts[3] == "vendor"
                    and parts[5:] == ("bin", "codex")
                ):
                    return False
                info = actual.stat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or info.st_mode & 0o022
                        or not info.st_mode & 0o111):
                    return False
        except (OSError, ValueError):
            return False
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return (current.st_dev, current.st_ino) == (entry.st_dev, entry.st_ino)

    @staticmethod
    def _is_stopped_chromium_singleton_link(directory_fd: int, name: str,
                                            relative: tuple[str, ...], entry) -> bool:
        """Recognize only Chromium's private, stopped-account singleton links."""

        if name not in {"SingletonSocket", "SingletonCookie", "SingletonLock"} or entry.st_nlink != 1:
            return False
        if relative == (".config", "chromium"):
            pass
        elif (
            name == "SingletonCookie"
            and len(relative) == 2
            and relative[0] == ".tmp"
            and re.fullmatch(r"org\.chromium\.[A-Za-z0-9_.-]{1,96}", relative[1])
        ):
            # Chromium also leaves a duplicate cookie link beside its private
            # temporary profile. It is transient and has the same numeric
            # singleton target as the config link above.
            pass
        else:
            return False
        try:
            target = os.readlink(name, dir_fd=directory_fd)
        except OSError:
            return False
        patterns = {
            "SingletonSocket": r"/workspace/account/\.tmp/org\.chromium\.[A-Za-z0-9_.-]{1,96}/SingletonSocket",
            "SingletonCookie": r"[0-9]{1,32}",
            "SingletonLock": r"[A-Za-z0-9_.-]{1,128}-[0-9]{1,12}",
        }
        if not re.fullmatch(patterns[name], target):
            return False
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return (current.st_dev, current.st_ino) == (entry.st_dev, entry.st_ino)

    def _tighten_directory_fd(self, directory_fd: int, relative: tuple[str, ...] = ()) -> None:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.geteuid():
            raise ControlPlaneError("Provider account directory ownership is unsafe")
        os.fchmod(directory_fd, 0o700)
        for name in os.listdir(directory_fd):
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if entry.st_uid != os.geteuid():
                raise ControlPlaneError("Provider account entry ownership is unsafe")
            if stat.S_ISLNK(entry.st_mode):
                if self._is_codex_arg0_helper(directory_fd, name, relative, entry):
                    continue
                if (self.native_launcher is not None
                        and self._is_stopped_chromium_singleton_link(
                            directory_fd, name, relative, entry
                        )):
                    os.unlink(name, dir_fd=directory_fd)
                    continue
                raise ControlPlaneError("Provider account home contains an unsafe link")
            if stat.S_ISDIR(entry.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                        raise ControlPlaneError("Provider account directory changed during validation")
                    self._tighten_directory_fd(child_fd, (*relative, name))
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISSOCK(entry.st_mode) or stat.S_ISFIFO(entry.st_mode):
                # A stopped, exact native container can leave transient IPC nodes
                # (for example Chromium's SingletonSocket).  They are not durable
                # account state.  Remove only a single-link node after an inode
                # recheck; keep rejecting every other special file and all links.
                if self.native_launcher is None:
                    raise ControlPlaneError("Provider account home contains an unsafe file")
                if entry.st_nlink != 1:
                    raise ControlPlaneError("Provider account IPC node is shared")
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (entry.st_dev, entry.st_ino):
                    raise ControlPlaneError("Provider account IPC node changed during validation")
                os.unlink(name, dir_fd=directory_fd)
                continue
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise ControlPlaneError("Provider account home contains an unsafe file")
            os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
            secured = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (secured.st_dev, secured.st_ino) != (entry.st_dev, entry.st_ino):
                raise ControlPlaneError("Provider account file changed during validation")

    def require_supported_route(
        self,
        *,
        provider: str,
        auth_method: str,
        execution_mode: str,
        platform_name: str,
        hosted_consumer_auth_enabled: bool,
    ) -> None:
        normalized_provider = str(provider).strip().lower()
        if normalized_provider in {"claude", "anthropic"} and auth_method == "subscription":
            if execution_mode == "host" and platform_name == "darwin":
                raise ControlPlaneError(
                    "macOS host-native Claude supports only the current OS user's account; use an approved enterprise route"
                )
            if not hosted_consumer_auth_enabled:
                raise ControlPlaneError(
                    "Hosted Claude consumer login requires explicit provider permission or a supported contract"
                )


@dataclass
class _SetupSession:
    account_id: str
    tenant_id: str
    owner_id: str
    provider: str
    process: subprocess.Popen[bytes]
    master_fd: int
    lock_file: Any
    lease_id: str
    lease_stop: threading.Event = field(default_factory=threading.Event)
    lease_thread: threading.Thread | None = None
    output: str = ""
    started_at: float = field(default_factory=time.time)
    reader_done: bool = False
    input_submitted: bool = False
    finalizing: bool = False
    released: bool = False


class ProviderSetupManager:
    """Runs provider-native sign-in in a private per-user home.

    Setup output is capped and held in memory only. Provider credentials remain in the
    provider's own native home, outside workspace storage and the control-plane database.
    """

    def __init__(
        self,
        *,
        store: Any,
        home_root: Path,
        reconcile_provider_account_binding: Callable[[Path], None] | None = None,
        homes: ProviderAccountHomeManager | None = None,
        owner_root_resolver: Callable[[str, str], Path] | None = None,
        native_launcher: GuardedAccountLauncher | None = None,
    ) -> None:
        self.store = store
        if homes is not None and (owner_root_resolver is not None or native_launcher is not None):
            raise ControlPlaneError("Use the shared account manager or its resolver, not two authorities")
        self.homes = homes if homes is not None else ProviderAccountHomeManager(home_root, owner_root_resolver=owner_root_resolver, native_launcher=native_launcher)
        self.reconcile_provider_account_binding = reconcile_provider_account_binding
        self._sessions: dict[str, _SetupSession] = {}
        self._lock = threading.RLock()

    def _reconcile_if_isolated(self, account_home: Path) -> None:
        self.homes.assert_native_quiescent(account_home)
        isolation = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION") or ""
        ).strip().lower()
        if isolation != "per_worker_container":
            return
        if self.reconcile_provider_account_binding is None:
            raise ControlPlaneError(
                "The reviewed provider-account container substrate is unavailable"
            )
        self.reconcile_provider_account_binding(account_home)

    def _binary(self, provider: str) -> str:
        binary = provider_setup_binary(provider)
        if not binary:
            raise ControlPlaneError(f"{provider.title()} CLI is not installed in this GlassHive runtime")
        return binary

    def _environment(self, *, provider: str, account_home: Path) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in PROVIDER_SETUP_ENV_ALLOWLIST
        }
        environment.update(self.homes.runtime_environment(provider=provider, account_home=account_home))
        # Provider CLIs sometimes consult HOME even when their documented config-home override is
        # present. Keep every incidental login file inside the same private account tree.
        environment["HOME"] = str(account_home)
        environment["NO_COLOR"] = "1"
        if provider in {"grok", "xai"}:
            environment.update(GROK_HOME=str(account_home / "grok"), GROK_DISABLE_AUTOUPDATER="1",
                GROK_TELEMETRY_ENABLED="false", GROK_TELEMETRY_TRACE_UPLOAD="false", GROK_EXTERNAL_OTEL="0")
        return environment

    def _commands(self, provider: str, *, contained: bool = False) -> tuple[list[str], list[str]]:
        binary = self._binary(provider)
        if provider in {"codex", "openai"}:
            return [binary, "login", "--device-auth"], [binary, "login", "status"]
        if provider in {"claude", "anthropic"}:
            return [binary, "auth", "login", "--claudeai"], [binary, "auth", "status", "--json"]
        if provider in {"grok", "xai"}:
            if contained:
                # The immutable native account image does not mount the service
                # virtualenv or source checkout. Its shared image carries the
                # verifier and uses the system interpreter.
                verifier = [
                    "/usr/bin/python3", "-I", "-m", "workers_projects_runtime.grok_auth",
                    "--binary", binary,
                ]
            else:
                # Host/legacy account homes keep the existing local helper path.
                verifier = [sys.executable, str(Path(__file__).with_name("grok_auth.py")),
                            "--binary", binary]
            return [binary, "login", "--device-auth"], verifier
        raise ControlPlaneError("Unsupported provider setup")

    def _account(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, Any]:
        account = self.store.get_provider_account_record(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if account is None:
            raise ControlPlaneError("Provider account not found for this user")
        from .current_native_account import is_current_account
        if is_current_account(account):
            raise ControlPlaneError("Use Verify for the existing OS sign-in; named account setup does not change it")
        if str(account.get("auth_method") or "") != "subscription":
            raise ControlPlaneError("Interactive setup is only available for provider subscription accounts")
        current_support = provider_platform_support(
            provider=str(account.get("provider") or ""),
            auth_method=str(account.get("auth_method") or ""),
        )
        if current_support != "supported":
            raise ControlPlaneError("This provider account setup is not supported by the current deployment")
        return account

    def _append_output(self, session: _SetupSession, chunk: bytes) -> None:
        text = ANSI_ESCAPE.sub("", chunk.decode("utf-8", errors="replace"))
        text = "".join(character for character in text if character in "\n\r\t" or ord(character) >= 32)
        with self._lock:
            session.output = (session.output + text)[-MAX_SETUP_OUTPUT_CHARS:]

    def _read_output(self, session: _SetupSession) -> None:
        try:
            while True:
                try:
                    chunk = os.read(session.master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self._append_output(session, chunk)
        finally:
            with self._lock:
                session.reader_done = True

    def start(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, object]:
        account = self._account(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        provider = str(account.get("provider") or "").strip().lower()
        account_home = self.homes.account_home_path(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
        )
        setup_command, _ = self._commands(provider)
        with self._lock:
            current = self._sessions.get(account_id)
            if current is not None and current.process.poll() is None:
                raise ControlPlaneConflict("Provider account setup is already running")
            active_sessions = [
                session for session in self._sessions.values() if session.process.poll() is None
            ]
            if len(active_sessions) >= 8:
                raise ControlPlaneConflict("Provider account setup capacity is temporarily full")
            if sum(
                session.tenant_id == tenant_id and session.owner_id == owner_id
                for session in active_sessions
            ) >= 2:
                raise ControlPlaneConflict("This user already has the maximum active account setups")
            try:
                lease = self.store.acquire_provider_lease(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    lane="provider-setup",
                    worker_id="provider-setup",
                    run_id=f"setup:{account_id}",
                    ttl_seconds=60,
                    allowed_statuses=(
                        "disconnected",
                        "connecting",
                        "ready",
                        "action_required",
                        "unavailable",
                        "error",
                    ),
                    required_recovery_code=str(account.get("recovery_code") or ""),
                )
                self._reconcile_if_isolated(account_home)
                account_home = self.homes.ensure_home(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    account_id=account_id,
                    provider=provider,
                )
                environment = self._environment(provider=provider, account_home=account_home)
                lock_path = account_home / ".setup.lock"
                lock_file = lock_path.open("a+b")
                if os.name != "nt":
                    lock_path.chmod(0o600)
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    lock_file.close()
                    raise ControlPlaneConflict("Provider account setup is already running") from exc
            except Exception:
                if "lease" in locals():
                    try:
                        self.store.update_provider_account_status(
                            account_id=account_id,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                            status="action_required",
                            reconnect_reason=LEGACY_CREDENTIAL_CLEANUP_REASON,
                            recovery_code="credential_cleanup_failed",
                        )
                    finally:
                        self.homes.assert_native_quiescent(account_home)
                        self.store.release_provider_lease(
                            lease_id=str(lease.get("lease_id") or ""),
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                        )
                raise
            master_fd, slave_fd = pty.openpty()
            try:
                terminal_attributes = termios.tcgetattr(slave_fd)
                terminal_attributes[3] &= ~(
                    termios.ECHO | getattr(termios, "ECHONL", 0)
                )
                termios.tcsetattr(slave_fd, termios.TCSANOW, terminal_attributes)
                process = self.homes.popen_native(
                    setup_command,
                    account_home=account_home, purpose="setup", lease_id=str(lease["lease_id"]),
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(account_home),
                    env=environment,
                    start_new_session=True,
                    close_fds=True,
                    # Direct local children retain the flock. Packaged launchers
                    # instead persist exact container generation before launch and
                    # deny reuse until all-child absence is confirmed.
                    pass_fds=(lock_file.fileno(),),
                )
            except Exception:
                os.close(master_fd)
                os.close(slave_fd)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
                self.homes.assert_native_quiescent(account_home)
                self.store.release_provider_lease(
                    lease_id=str(lease.get("lease_id") or ""),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                raise
            finally:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            session = _SetupSession(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                provider=provider,
                process=process,
                master_fd=master_fd,
                lock_file=lock_file,
                lease_id=str(lease.get("lease_id") or ""),
            )
            self._sessions[account_id] = session
            self._start_setup_lease_heartbeat(session)
            threading.Thread(target=self._read_output, args=(session,), daemon=True).start()
        try:
            self.store.update_provider_account_status(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                status="connecting",
            )
        except Exception:
            self._terminate_session_process(session)
            self._release_session(session)
            with self._lock:
                self._sessions.pop(account_id, None)
            raise
        return self.status(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id, verify=False)

    @staticmethod
    def _setup_input_bytes(value: str) -> bytes:
        normalized = str(value or "").strip()
        if len(normalized) > MAX_SETUP_INPUT_BYTES:
            raise ControlPlaneError("The authentication code is too long")
        if not normalized or any(
            not 0x21 <= ord(character) <= 0x7E for character in normalized
        ):
            raise ControlPlaneError("Enter the authentication code shown by the provider")
        encoded = normalized.encode("ascii")
        # A physical Enter key arrives as CR in raw terminal UIs such as Ink;
        # canonical line discipline maps it to NL for ordinary readline clients.
        return encoded + b"\r"

    def submit_input(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
        value: str,
    ) -> dict[str, object]:
        account = self._account(
            account_id=account_id, tenant_id=tenant_id, owner_id=owner_id
        )
        payload = self._setup_input_bytes(value)
        with self._lock:
            session = self._sessions.get(account_id)
            if (
                session is None
                or session.tenant_id != tenant_id
                or session.owner_id != owner_id
                or session.process.poll() is not None
            ):
                raise ControlPlaneConflict("Provider account setup is not waiting for input")
            if str(account.get("provider") or "").strip().lower() not in {"claude", "anthropic"}:
                raise ControlPlaneConflict("This provider sign-in does not accept browser input")
            if session.input_submitted:
                raise ControlPlaneConflict("The authentication code was already submitted")
            guidance = _provider_setup_guidance(session.provider, session.output)
            if not guidance.get("input_required"):
                raise ControlPlaneConflict("Provider account setup is not waiting for input")
            try:
                write_fd = os.dup(session.master_fd)
            except OSError as exc:
                raise ControlPlaneConflict(
                    "Provider account setup is no longer waiting for input"
                ) from exc
            # Reserve the one submission before releasing the process-wide lock.
            # The duplicate pins this exact PTY even if Cancel/Restart closes and
            # recycles the session's original descriptor before the write completes.
            session.input_submitted = True
            session.output = ""
        written = 0
        try:
            while written < len(payload):
                count = os.write(write_fd, payload[written:])
                if count <= 0:
                    raise OSError("provider input closed")
                written += count
        except OSError as exc:
            owns_cleanup = False
            with self._lock:
                if self._sessions.get(account_id) is session:
                    self._terminate_session_process(session)
                    self._sessions.pop(account_id, None)
                    owns_cleanup = True
            if owns_cleanup:
                self._release_session(session)
                self.store.update_provider_account_status(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    status="action_required",
                    reconnect_reason="Provider sign-in input could not be delivered; restart sign-in",
                )
            raise ControlPlaneConflict(
                "Provider account setup is no longer waiting for input"
            ) from exc
        finally:
            os.close(write_fd)
        return self.status(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)

    def _verify(self, *, provider: str, environment: dict[str, str], account_home: Path, lease_id: str = "") -> bool:
        _, status_command = self._commands(
            provider, contained=self.homes.native_launcher is not None
        )
        try:
            result = self.homes.run_native(
                status_command,
                account_home=account_home, purpose="verify", lease_id=lease_id,
                cwd=str(account_home),
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _release_session(self, session: _SetupSession) -> None:
        with self._lock:
            if session.released:
                return
            self._terminate_session_process(session)
            home = self.homes.account_home_path(tenant_id=session.tenant_id, owner_id=session.owner_id, account_id=session.account_id)
            self.homes.assert_native_quiescent(home)
            session.lease_stop.set()
            if session.lease_thread is not None:
                session.lease_thread.join(timeout=2)
            try:
                os.close(session.master_fd)
            except OSError:
                pass
            try:
                fcntl.flock(session.lock_file.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            session.lock_file.close()
            self.store.release_provider_lease(
                lease_id=session.lease_id,
                tenant_id=session.tenant_id,
                owner_id=session.owner_id,
            )
            session.released = True

    def _start_setup_lease_heartbeat(self, session: _SetupSession) -> None:
        def heartbeat() -> None:
            while not session.lease_stop.wait(10):
                try:
                    self.store.heartbeat_provider_lease(
                        lease_id=session.lease_id,
                        tenant_id=session.tenant_id,
                        owner_id=session.owner_id,
                        ttl_seconds=60,
                    )
                except (ControlPlaneError, OSError, sqlite3.OperationalError):
                    self._terminate_session_process(session)
                    try:
                        self.store.update_provider_account_status(
                            account_id=session.account_id,
                            tenant_id=session.tenant_id,
                            owner_id=session.owner_id,
                            status="action_required",
                            reconnect_reason="Provider setup lease was lost; reconnect safely",
                        )
                    except (ControlPlaneError, OSError, sqlite3.OperationalError):
                        pass
                    return

        session.lease_thread = threading.Thread(
            target=heartbeat,
            name=f"glasshive-provider-setup-lease-{session.account_id[:24]}",
            daemon=True,
        )
        session.lease_thread.start()

    @staticmethod
    def _terminate_session_process(session: _SetupSession) -> None:
        from .contained_account_launch import ContainedAccountProcess
        if isinstance(session.process, ContainedAccountProcess):
            session.process.stop_and_confirm()
            return
        if session.process.poll() is not None:
            return
        try:
            os.killpg(session.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            session.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                session.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def shutdown(self) -> None:
        """Stop each setup independently; uncertain containers keep their leases."""
        with self._lock:
            sessions = list(self._sessions.values())
        failure = None
        for session in sessions:
            try:
                self._terminate_session_process(session)
                self._release_session(session)
                with self._lock:
                    if self._sessions.get(session.account_id) is session:
                        self._sessions.pop(session.account_id, None)
            except Exception as exc:
                failure = failure or exc
            try:
                self.store.update_provider_account_status(
                    account_id=session.account_id,
                    tenant_id=session.tenant_id,
                    owner_id=session.owner_id,
                    status="action_required",
                    reconnect_reason="Provider setup stopped because GlassHive shut down",
                )
            except ControlPlaneError:
                pass
        if failure is not None:
            raise failure

    def status(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
        verify: bool = True,
    ) -> dict[str, object]:
        account = self._account(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        provider = str(account.get("provider") or "").strip().lower()
        account_home = self.homes.account_home_path(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
        )
        finalization_in_progress = False
        with self._lock:
            session = self._sessions.get(account_id)
            if session is not None and (session.tenant_id != tenant_id or session.owner_id != owner_id):
                raise ControlPlaneError("Provider account not found for this user")
            return_code = session.process.poll() if session is not None else None
            if session is None or session.input_submitted:
                output = ""
            else:
                output = session.output
            if session is not None and return_code is not None:
                if session.finalizing:
                    finalization_in_progress = True
                else:
                    session.finalizing = True
        if session is not None and return_code is None:
            guidance = _provider_setup_guidance(provider, output)
            if session.input_submitted:
                guidance["input_required"] = False
            return {
                "account_id": account_id,
                "status": "connecting",
                "instructions": output,
                "complete": False,
                "input_submitted": session.input_submitted,
                **guidance,
            }
        if session is not None and finalization_in_progress:
            guidance = _provider_setup_guidance(provider, output)
            if session.input_submitted:
                guidance["input_required"] = False
            return {
                "account_id": account_id,
                "status": "connecting",
                "instructions": output,
                "complete": False,
                "input_submitted": session.input_submitted,
                **guidance,
            }
        verification_lease: dict[str, Any] | None = None
        verification_lease_stop = threading.Event()
        verification_lease_lost = threading.Event()
        verification_lease_thread: threading.Thread | None = None
        if session is None:
            verification_lease = self.store.acquire_provider_lease(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                lane="provider-verify",
                worker_id="provider-verify",
                run_id=f"verify:{account_id}",
                ttl_seconds=60,
                allowed_statuses=(str(account.get("status") or "action_required"),),
                required_recovery_code=str(account.get("recovery_code") or ""),
            )
        try:
            if verification_lease is not None:
                lease_id = str(verification_lease.get("lease_id") or "")

                def renew_verification_lease() -> None:
                    while not verification_lease_stop.wait(
                        PROVIDER_VERIFY_HEARTBEAT_INTERVAL_SECONDS
                    ):
                        try:
                            self.store.heartbeat_provider_lease(
                                lease_id=lease_id,
                                tenant_id=tenant_id,
                                owner_id=owner_id,
                                ttl_seconds=120,
                            )
                        except (ControlPlaneError, OSError, sqlite3.OperationalError):
                            verification_lease_lost.set()
                            return

                # Extend before any Docker/image work, then keep the exclusive
                # lease alive across both seals and the provider CLI check.
                self.store.heartbeat_provider_lease(
                    lease_id=lease_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    ttl_seconds=120,
                )
                heartbeat_thread = threading.Thread(
                    target=renew_verification_lease,
                    name=f"glasshive-provider-verify-lease-{account_id[:24]}",
                    daemon=True,
                )
                heartbeat_thread.start()
                verification_lease_thread = heartbeat_thread

            def require_verification_lease() -> None:
                if verification_lease_lost.is_set():
                    raise ControlPlaneError(
                        "Provider verification lease was lost; check the connection again"
                    )

            if session is not None:
                self._terminate_session_process(session)
            self.homes.assert_native_quiescent(account_home)
            self._reconcile_if_isolated(account_home)
            require_verification_lease()
            account_home = self.homes.ensure_home(
                tenant_id=tenant_id,
                owner_id=owner_id,
                account_id=account_id,
                provider=provider,
            )
            environment = self._environment(provider=provider, account_home=account_home)
            authenticated = verify and self._verify(
                provider=provider, environment=environment, account_home=account_home,
                lease_id=session.lease_id if session is not None else str(verification_lease["lease_id"])
            )
            require_verification_lease()
            if authenticated:
                self.homes.prepare_interactive_home(
                    provider=provider,
                    account_home=account_home,
                )
                # Provider status commands may recreate private cache wrappers
                # after the pre-verification seal. Reconcile again while the
                # exclusive verify lease is still held, then perform the final
                # descriptor-based host validation.
                self._reconcile_if_isolated(account_home)
                require_verification_lease()
                self.homes.tighten_permissions(account_home=account_home)
                status = "ready"
                reason = ""
            elif session is not None and return_code not in {None, 0}:
                status = "error"
                reason = "Provider sign-in did not complete"
            else:
                status = "action_required"
                reason = "Complete provider sign-in to use this account"
            if verification_lease is not None:
                self.store.heartbeat_provider_lease(
                    lease_id=str(verification_lease.get("lease_id") or ""),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    ttl_seconds=120,
                )
                require_verification_lease()
            updated = self.store.update_provider_account_status(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                status=status,
                reconnect_reason=reason,
                verified=authenticated,
                recovery_code="" if authenticated else None,
            )
        except Exception:
            self.store.update_provider_account_status(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                status="action_required",
                reconnect_reason="Provider credentials need a safe connection check",
                recovery_code="credential_cleanup_failed",
            )
            raise
        finally:
            if session is not None:
                with self._lock:
                    if self._sessions.get(account_id) is session:
                        try:
                            self._release_session(session)
                        except Exception:
                            session.finalizing = False
                            raise
                        self._sessions.pop(account_id, None)
            elif verification_lease is not None:
                verification_lease_stop.set()
                self.homes.assert_native_quiescent(account_home)
                if verification_lease_thread is not None:
                    verification_lease_thread.join(timeout=2)
                self.store.release_provider_lease(
                    lease_id=str(verification_lease.get("lease_id") or ""),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
        return {
            "account_id": account_id,
            "status": updated.get("status", status),
            "instructions": output,
            "complete": True,
            **_provider_setup_guidance(provider, output),
        }

    def cancel(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, object]:
        self._account(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        with self._lock:
            session = self._sessions.get(account_id)
            if session is None or session.tenant_id != tenant_id or session.owner_id != owner_id:
                raise ControlPlaneError("Provider account setup is not running")
            self._terminate_session_process(session)
            self._sessions.pop(account_id, None)
        self._release_session(session)
        updated = self.store.update_provider_account_status(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            status="action_required",
            reconnect_reason="Provider setup was cancelled",
        )
        return {"account_id": account_id, "status": updated.get("status"), "complete": True}

    def disconnect(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, object]:
        account = self.store.get_provider_account_record(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if account is None:
            raise ControlPlaneError("Provider account not found for this user")
        with self._lock:
            session = self._sessions.get(account_id)
            if session is not None and (session.tenant_id != tenant_id or session.owner_id != owner_id):
                raise ControlPlaneError("Provider account not found for this user")
            if session is not None:
                self._terminate_session_process(session)
                self._sessions.pop(account_id, None)
        if session is not None:
            self._release_session(session)

        disconnect_lease = self.store.acquire_provider_lease(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            lane="provider-disconnect",
            worker_id="provider-disconnect",
            run_id=f"disconnect:{account_id}",
            ttl_seconds=60,
            allowed_statuses=(
                "disconnected",
                "connecting",
                "ready",
                "action_required",
                "unavailable",
                "error",
            ),
        )
        provider_logout_confirmed: bool | None = None
        try:
            self.store.update_provider_account_status(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                status="action_required",
                reconnect_reason="Disconnect in progress",
            )
            provider = str(account.get("provider") or "").strip().lower()
            account_home = self.homes.account_home_path(
                tenant_id=tenant_id,
                owner_id=owner_id,
                account_id=account_id,
            )
            self._reconcile_if_isolated(account_home)
            from .native_api_keys import native_key_account
            if account_home.exists() and not account_home.is_symlink() and not native_key_account(account):
                environment = self._environment(provider=provider, account_home=account_home)
                binary_name = "codex" if provider in {"codex", "openai"} else "claude"
                binary_env = (
                    "WPR_CODEX_CLI_PATH"
                    if binary_name == "codex"
                    else "WPR_CLAUDE_CODE_PATH"
                )
                binary = str(os.environ.get(binary_env) or "").strip() or shutil.which(binary_name)
                if not binary:
                    provider_logout_confirmed = False
                else:
                    logout_command = (
                        [binary, "logout"]
                        if binary_name == "codex"
                        else [binary, "auth", "logout"]
                    )
                    try:
                        logout_result = self.homes.run_native(
                            logout_command,
                            account_home=account_home, purpose="logout", lease_id=str(disconnect_lease["lease_id"]),
                            cwd=str(account_home),
                            env=environment,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=12,
                            check=False,
                        )
                        provider_logout_confirmed = logout_result.returncode == 0
                    except (OSError, subprocess.TimeoutExpired):
                        provider_logout_confirmed = False
            self.homes.remove_home(
                tenant_id=tenant_id,
                owner_id=owner_id,
                account_id=account_id,
            )
            updated = self.store.disconnect_provider_account(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        except Exception as exc:
            try:
                self.store.update_provider_account_status(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    status="action_required",
                    reconnect_reason="Could not remove private account data. Retry Remove.",
                    recovery_code="credential_cleanup_failed",
                )
            finally:
                self.homes.assert_native_quiescent(account_home)
                self.store.release_provider_lease(
                    lease_id=str(disconnect_lease["lease_id"]),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            raise ControlPlaneError(
                "GlassHive could not remove its private account data. Retry Remove."
            ) from exc
        if native_key_account(account):
            message = "Removed from xPerfect. Revoke the API key at its provider if you also want to disable it there."
        elif provider_logout_confirmed is True:
            message = "Removed from GlassHive."
        elif provider_logout_confirmed is False:
            message = "Removed from GlassHive. Provider sign-out could not be confirmed."
        else:
            message = "Removed from GlassHive. No local provider session was present."
        return {
            "account_id": account_id,
            "status": str(updated.get("status") or "disconnected"),
            "complete": True,
            "provider_logout_confirmed": provider_logout_confirmed,
            "message": message,
        }
