from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import Callable
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from .auth import multi_user_security_enabled
from .bootstrap import apply_bootstrap
from .codex_plugins import (
    OPENAI_PLUGIN_MARKETPLACE_COMMIT,
    OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH,
    OPENAI_PLUGIN_MARKETPLACE_ORIGIN,
)
from .mission_provider_accounts import apply_bound_provider_account_environment
from .openclaw_release import (
    OPENCLAW_RUNTIME_FAST_URI_VERSION,
    OPENCLAW_RUNTIME_LOCK_SHA256,
    OPENCLAW_RUNTIME_VERSION,
    require_reviewed_openclaw_version,
    reviewed_openclaw_env,
    stage_reviewed_openclaw_lock,
)

from concurrent.futures import ThreadPoolExecutor

from urllib.parse import quote, urlencode, urlparse

from .bootstrap import (
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    apply_bootstrap,
    bootstrap_bundle_for,
)

from .models import normalize_worker_resource_class, worker_resource_memory_bytes

from .openclaw_runtime import HostCapacityError


logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


SAFE_DOCKER_EXEC_ENV_KEYS = {
    "PATH",
    "SHELL",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "PYTHONIOENCODING",
    # These selectors point only at control-plane-validated provider/workspace
    # homes. Without them the CLIs silently fall back to process-global state.
    "CODEX_HOME",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "GROK_HOME",
    "GROK_AUTH_PATH",
    "GROK_DISABLE_AUTOUPDATER",
    "GROK_TELEMETRY_ENABLED",
    "GROK_TELEMETRY_TRACE_UPLOAD",
    "GROK_EXTERNAL_OTEL",
    "GROK_CURSOR_MCPS_ENABLED",
    "GROK_CLAUDE_MCPS_ENABLED",
    # Provider keys are run-scoped: the worker launch script unsets them before
    # handing control to the post-run interactive shell.
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_SERVICE_TIER",
    "ANTHROPIC_BASE_URL",
    "API_TIMEOUT_MS",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
    "WPR_CODEX_CLI_BASE_URL",
    "WPR_CODEX_CLI_ENV_KEY",
    "WPR_CODEX_CLI_MODEL_PROVIDER",
    "WPR_CODEX_CLI_USE_CUSTOM_PROVIDER",
    "WPR_CODEX_CLI_WIRE_API",
    "WPR_CODEX_CHROME_PLUGIN_ROOT",
    "CODEX_CHROME_PLUGIN_ROOT",
    "WPR_CODEX_NODE_REPL_PATH",
    "CODEX_NODE_REPL_PATH",
    "WPR_OPENCLAW_BASE_URL",
    "WPR_OPENCLAW_ENV_KEY",
    "WPR_OPENCLAW_MODEL_PROVIDER",
    "WPR_OPENCLAW_USE_CUSTOM_PROVIDER",
    "WPR_OPENCLAW_WIRE_API",
    "OPENCLAW_DISABLE_BONJOUR",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
}
VNC_PASSWORD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~!@#$%^&*+=?"


_CLAUDE_WORKSPACE_ONBOARDING_SCRIPT = r'''import json
import os
import secrets
import stat
import sys
from pathlib import Path

workspace_home = Path(sys.argv[1])
workspace_home.mkdir(mode=0o700, parents=True, exist_ok=True)
directory_stat = workspace_home.lstat()
if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid not in {0, os.geteuid()}:
    raise RuntimeError("unsafe Claude workspace configuration directory")
target = workspace_home / ".claude.json"
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
state = {}
try:
    descriptor = os.open(target, flags)
except FileNotFoundError:
    descriptor = -1
if descriptor >= 0:
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size > 1_048_576
        ):
            raise RuntimeError("unsafe Claude workspace configuration file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("unsafe Claude workspace configuration file")
        raw = os.read(descriptor, metadata.st_size)
        state = json.loads(raw.decode("utf-8")) if raw else {}
    finally:
        os.close(descriptor)
if not isinstance(state, dict):
    raise RuntimeError("invalid Claude workspace configuration")
if state.get("hasCompletedOnboarding") is True:
    raise SystemExit(0)
state["hasCompletedOnboarding"] = True
payload = (json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
temporary = workspace_home / (".glasshive-onboarding-" + secrets.token_hex(12))
temporary_descriptor = os.open(
    temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    view = memoryview(payload)
    while view:
        written = os.write(temporary_descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]
    os.fsync(temporary_descriptor)
finally:
    os.close(temporary_descriptor)
os.replace(temporary, target)
directory_descriptor = os.open(workspace_home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
'''


def _provider_account_seal_script(mount_target: str) -> str:
    """Return the fail-closed rootless-namespace credential normalization script."""

    quoted_mount = shlex.quote(str(mount_target))
    return (
        "set -euo pipefail; "
        "command -v setfacl >/dev/null 2>&1; "
        f"test -d {quoted_mount}; "
        # Provider CLIs may leave executable-wrapper symlinks in their private
        # caches. Unlink the entries without following them before applying
        # host ownership or ACL changes to real directories and files.
        f"find {quoted_mount} -xdev -type l -delete; "
        f"test -z \"$(find {quoted_mount} -xdev -type f -links +1 -print -quit)\"; "
        f"test -z \"$(find {quoted_mount} -xdev ! -type d ! -type f -print -quit)\"; "
        f"find {quoted_mount} -xdev -type d -exec chown 0:0 -- {{}} +; "
        f"find {quoted_mount} -xdev -type f -exec chown 0:0 -- {{}} +; "
        f"find {quoted_mount} -xdev -type d -exec chmod 0700 -- {{}} +; "
        f"find {quoted_mount} -xdev -type f -exec chmod 0600 -- {{}} +; "
        f"find {quoted_mount} -xdev -exec setfacl -b -- {{}} +; "
        f"find {quoted_mount} -xdev -type d -exec setfacl -k -- {{}} +"
    )


_CODEX_PROVIDER_AUTH_LINK_SCRIPT = r"""
import grp
import os
import pwd
import stat
import sys

mount_path, home_name, workspace_home_path, identity = sys.argv[1:5]
user_value, separator, group_value = identity.partition(":")


def numeric_user(value: str) -> int:
    return int(value) if value.isdigit() else pwd.getpwnam(value).pw_uid


def numeric_group(value: str, user_id: int) -> int:
    if not value:
        return pwd.getpwuid(user_id).pw_gid
    return int(value) if value.isdigit() else grp.getgrnam(value).gr_gid


worker_uid = numeric_user(user_value)
worker_gid = numeric_group(group_value if separator else "", worker_uid)
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC

mount_fd = os.open(mount_path, directory_flags)
try:
    provider_home_fd = os.open(home_name, directory_flags, dir_fd=mount_fd)
    try:
        auth_fd = os.open("auth.json", file_flags, dir_fd=provider_home_fd)
        try:
            metadata = os.fstat(auth_fd)
            mode = stat.S_IMODE(metadata.st_mode)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError("Codex account authentication is not a safe regular file")
            if metadata.st_size <= 0 or metadata.st_size > 16 * 1024 * 1024:
                raise RuntimeError("Codex account authentication has an unsafe size")
            if metadata.st_uid not in {0, worker_uid}:
                raise RuntimeError("Codex account authentication has an unexpected owner")
            if metadata.st_gid not in {0, worker_gid}:
                raise RuntimeError("Codex account authentication has an unexpected group")
            if mode & 0o007 or mode & 0o111:
                raise RuntimeError("Codex account authentication has unsafe permissions")
        finally:
            os.close(auth_fd)
    finally:
        os.close(provider_home_fd)
finally:
    os.close(mount_fd)

workspace_home_fd = os.open(workspace_home_path, directory_flags)
try:
    try:
        os.mkdir(".codex", 0o700, dir_fd=workspace_home_fd)
    except FileExistsError:
        pass
    codex_home_fd = os.open(".codex", directory_flags, dir_fd=workspace_home_fd)
    try:
        codex_metadata = os.fstat(codex_home_fd)
        if codex_metadata.st_uid not in {0, worker_uid}:
            raise RuntimeError("Workspace Codex home has an unexpected owner")
        # Keep the bind-mounted home owned by the host runtime. Container-root
        # chown maps through the rootless user namespace and would make the
        # persisted .codex tree inaccessible to later host-side bootstrap work.
        # The reviewed workspace ACL contract grants the worker access.

        expected_target = os.path.join(mount_path, home_name, "auth.json")
        try:
            existing = os.stat("auth.json", dir_fd=codex_home_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.symlink(expected_target, "auth.json", dir_fd=codex_home_fd)
        else:
            if not stat.S_ISLNK(existing.st_mode):
                raise RuntimeError("Workspace Codex auth path is not the managed account link")
            if os.readlink("auth.json", dir_fd=codex_home_fd) != expected_target:
                raise RuntimeError("Workspace Codex auth link selects an unexpected account path")
        os.fsync(codex_home_fd)
    finally:
        os.close(codex_home_fd)
finally:
    os.close(workspace_home_fd)
""".strip()

PARALLEL_CLEAN_ROOM_POLICY_LABEL = "com.viventium.parallel-clean-room.policy"

PARALLEL_CLEAN_ROOM_ROLE_LABEL = "com.viventium.parallel-clean-room.role"

PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_ROLE = "provider-proxy"

PARALLEL_CLEAN_ROOM_BROKER_PROXY_ROLE = "broker-proxy"

PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE = "mission-network"

PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL = (
    "com.viventium.parallel-clean-room.worker-container"
)

PARALLEL_CLEAN_ROOM_BROKER_ALIAS = "host.docker.internal"

PARALLEL_CLEAN_ROOM_PROXY_IMAGE = "viventium-parallel-work-proxy:local"

PARALLEL_CLEAN_ROOM_PROXY_USER = "glasshive"

PARALLEL_CLEAN_ROOM_PROXY_ENTRYPOINT = ("python", "/app/proxy.py")

PARALLEL_CLEAN_ROOM_PROXY_TMPFS = (
    "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
)

PARALLEL_CLEAN_ROOM_FORBIDDEN_CONTAINER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CLAUDE_",
    "GCP_",
    "GITHUB_",
    "GITLAB_",
    "GOOGLE_",
    "OPENAI_",
    "PORTKEY_",
)

PARALLEL_CLEAN_ROOM_TMPFS = (
    "/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777",
    "/run:rw,nosuid,nodev,noexec,size=64m,mode=755",
    "/run/glasshive:rw,nosuid,nodev,noexec,size=16m,mode=700,uid=1200,gid=1201",
    "/run/screen:rw,nosuid,nodev,noexec,size=8m,mode=1777,uid=1200,gid=1201",
    "/var/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
    "/var/log/supervisor:rw,nosuid,nodev,noexec,size=64m,mode=755",
    "/opt/selenium/logs:rw,nosuid,nodev,noexec,size=256m,mode=755",
    "/opt/selenium/assets:rw,nosuid,nodev,noexec,size=256m,mode=755",
)

_DOCKER_OBJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

_DOCKER_NETWORK_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")

_DOCKER_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")

_DOCKER_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")

_RESOURCE_MEMORY_BYTES_PATH_KEY = "__glasshive_resource_memory_bytes"


PARALLEL_CLEAN_ROOM_POLICY_LABEL = "com.viventium.parallel-clean-room.policy"
PARALLEL_CLEAN_ROOM_ROLE_LABEL = "com.viventium.parallel-clean-room.role"
PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_ROLE = "provider-proxy"
PARALLEL_CLEAN_ROOM_BROKER_PROXY_ROLE = "broker-proxy"
PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE = "mission-network"
PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL = (
    "com.viventium.parallel-clean-room.worker-container"
)
PARALLEL_CLEAN_ROOM_BROKER_ALIAS = "host.docker.internal"
PARALLEL_CLEAN_ROOM_PROXY_IMAGE = "viventium-parallel-work-proxy:local"
PARALLEL_CLEAN_ROOM_PROXY_USER = "glasshive"
PARALLEL_CLEAN_ROOM_PROXY_ENTRYPOINT = ("python", "/app/proxy.py")
PARALLEL_CLEAN_ROOM_PROXY_TMPFS = (
    "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
)
PARALLEL_CLEAN_ROOM_FORBIDDEN_CONTAINER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CLAUDE_",
    "GCP_",
    "GITHUB_",
    "GITLAB_",
    "GOOGLE_",
    "OPENAI_",
    "PORTKEY_",
)
PARALLEL_CLEAN_ROOM_TMPFS = (
    "/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777",
    "/run:rw,nosuid,nodev,noexec,size=64m,mode=755",
    "/run/glasshive:rw,nosuid,nodev,noexec,size=16m,mode=700,uid=1200,gid=1201",
    "/run/screen:rw,nosuid,nodev,noexec,size=8m,mode=1777,uid=1200,gid=1201",
    "/var/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
    "/var/log/supervisor:rw,nosuid,nodev,noexec,size=64m,mode=755",
    "/opt/selenium/logs:rw,nosuid,nodev,noexec,size=256m,mode=755",
    "/opt/selenium/assets:rw,nosuid,nodev,noexec,size=256m,mode=755",
)
_DOCKER_OBJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DOCKER_NETWORK_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_DOCKER_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_DOCKER_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RESOURCE_MEMORY_BYTES_PATH_KEY = "__glasshive_resource_memory_bytes"

AI_WORKER_BROWSER_EXTENSION_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
AI_WORKER_BROWSER_EXTENSION_IDS = {
    "claude": "fcoeoabgfenejglbffodgkkbkcdhcgfn",
    "codex": "hehggadaopoacecdllhhajmbjkdcmajg",
}
AI_WORKER_BROWSER_NATIVE_HOSTS = {
    "claude": "com.anthropic.claude_code_browser_extension",
    "codex": "com.openai.codexextension",
}
AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS = (
    "/etc/chromium/policies/managed/glasshive-ai-worker-extensions.json",
    "/etc/opt/chrome/policies/managed/glasshive-ai-worker-extensions.json",
)
from .grok_artifact import GROK_ARTIFACT_PROVENANCE, docker_install_instruction as grok_docker_install_instruction

AI_WORKER_CODEX_NPM_SPEC = (
    os.environ.get("WPR_SANDBOX_CODEX_NPM_SPEC", "@openai/codex@0.155.0-alpha.9.2").strip()
    or "@openai/codex@0.155.0-alpha.9.2"
)
AI_WORKER_CLAUDE_CODE_NPM_SPEC = (
    os.environ.get("WPR_SANDBOX_CLAUDE_CODE_NPM_SPEC", "@anthropic-ai/claude-code@2.1.263").strip()
    or "@anthropic-ai/claude-code@2.1.263"
)
AI_WORKER_BASE_IMAGE = os.environ.get(
    "WPR_SANDBOX_BASE_IMAGE",
    "selenium/standalone-chromium:4.46.0-20260707@sha256:3400b92f1cddb2dfaaf358654e8f7d83d7be45192fb73c5f28c25faa28d36504",
).strip()
# This snapshot is the first reviewed Ubuntu archive that contains the same
# curl/libcurl build already present in the pinned Selenium base image.
AI_WORKER_APT_SNAPSHOT = "20260801T000000Z"
AI_WORKER_PYTHON_LOCK_PATH = (
    Path(__file__).resolve().parents[2] / "workstation-requirements.lock"
)
AI_WORKER_PYTHON_LOCK_SHA256 = hashlib.sha256(
    AI_WORKER_PYTHON_LOCK_PATH.read_bytes()
).hexdigest()


def _stage_reviewed_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        delete=False,
    ) as handle:
        staged = Path(handle.name)
    try:
        # Reviewed inputs are read-only in sealed releases. A same-directory
        # replacement keeps retries atomic even when a prior copy is 0444.
        shutil.copy2(source, staged)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def _enabled_ai_worker_browser_extension_names() -> tuple[str, ...]:
    raw = (
        os.environ.get("GLASSHIVE_AI_WORKER_BROWSER_EXTENSIONS")
        or os.environ.get("WPR_AI_WORKER_BROWSER_EXTENSIONS")
        or "none"
    ).strip()
    if not raw or raw.lower() in {"0", "false", "no", "none", "off"}:
        return ()
    if raw.lower() in {"1", "true", "yes", "all", "on"}:
        return tuple(AI_WORKER_BROWSER_EXTENSION_IDS)
    names: list[str] = []
    unknown: list[str] = []
    for part in raw.replace(";", ",").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in AI_WORKER_BROWSER_EXTENSION_IDS:
            unknown.append(name)
            continue
        if name not in names:
            names.append(name)
    if unknown:
        known = ", ".join(sorted(AI_WORKER_BROWSER_EXTENSION_IDS))
        raise ValueError(f"Unknown AI worker browser extension(s): {', '.join(unknown)}. Expected one of: {known}, all, none")
    return tuple(names)


def _enabled_ai_worker_browser_extensions() -> dict[str, str]:
    return {name: AI_WORKER_BROWSER_EXTENSION_IDS[name] for name in _enabled_ai_worker_browser_extension_names()}


def _ai_worker_browser_extension_policy_json() -> str:
    extensions = _enabled_ai_worker_browser_extensions()
    return json.dumps(
        {
            "ExtensionInstallForcelist": [
                f"{extension_id};{AI_WORKER_BROWSER_EXTENSION_UPDATE_URL}"
                for extension_id in extensions.values()
            ]
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _ai_worker_browser_extension_check_script() -> str:
    extensions = _enabled_ai_worker_browser_extensions()
    extension_ids = " ".join(shlex.quote(extension_id) for extension_id in extensions.values())
    native_host_pairs = " ".join(
        shlex.quote(f"{AI_WORKER_BROWSER_NATIVE_HOSTS[name]}:{extension_id}")
        for name, extension_id in extensions.items()
    )
    policy_paths = " ".join(shlex.quote(path) for path in AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS)
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"extension_ids=({extension_ids})",
            f"native_host_pairs=({native_host_pairs})",
            f"policy_paths=({policy_paths})",
            "require_profile=0",
            'if [ "${1:-}" = "--require-profile" ]; then require_profile=1; fi',
            'for policy in "${policy_paths[@]}"; do',
            '  test -f "$policy"',
            '  grep -Fq "ExtensionInstallForcelist" "$policy"',
            '  for extension_id in "${extension_ids[@]}"; do',
            f'    grep -Fq "${{extension_id}};{AI_WORKER_BROWSER_EXTENSION_UPDATE_URL}" "$policy"',
            "  done",
            "done",
            'profile_root="${CHROME_USER_DATA_DIR:-${HOME:-/workspace/.wpr-home}/.config/chromium}"',
            "missing=0",
            'for extension_id in "${extension_ids[@]}"; do',
            '  if [ -d "$profile_root/Default/Extensions/$extension_id" ] || [ -d "$profile_root/Extensions/$extension_id" ]; then',
            '    printf "%s profile-installed\\n" "$extension_id"',
            "  else",
            '    printf "%s policy-present profile-pending\\n" "$extension_id"',
            "    missing=1",
            "  fi",
            "done",
            'home_dir="${HOME:-/workspace/.wpr-home}"',
            'native_host_dir="${home_dir}/.config/chromium/NativeMessagingHosts"',
            'for pair in "${native_host_pairs[@]}"; do',
            '  host_name="${pair%%:*}"',
            '  extension_id="${pair#*:}"',
            '  manifest="${native_host_dir}/${host_name}.json"',
            '  if [ -f "$manifest" ]; then',
            '    path_value="$(python3 - "$manifest" "$host_name" "$extension_id" <<\'PY\' || true',
            "import json, sys",
            "manifest, host_name, extension_id = sys.argv[1:4]",
            "try:",
            "    data = json.load(open(manifest, encoding='utf-8'))",
            "except Exception:",
            "    data = {}",
            "allowed = data.get('allowed_origins') if isinstance(data, dict) else []",
            "expected_origin = f'chrome-extension://{extension_id}/'",
            "if data.get('name') == host_name and expected_origin in (allowed or []):",
            "    print(data.get('path') or '')",
            "PY",
            ')"',
            '    if [ -n "$path_value" ] && [ -x "$path_value" ]; then',
            '      printf "%s native-host-installed\\n" "$host_name"',
            "    else",
            '      printf "%s native-host-manifest-present host-path-pending\\n" "$host_name"',
            "    fi",
            "  else",
            '    printf "%s native-host-pending\\n" "$host_name"',
            "  fi",
            "done",
            'if [ "$require_profile" = "1" ] && [ "$missing" = "1" ]; then exit 2; fi',
            'printf "glasshive browser extension policy ok\\n"',
        ]
    )


def _ai_worker_browser_native_host_bootstrap_script() -> str:
    enabled = set(_enabled_ai_worker_browser_extension_names())
    install_claude = "1" if "claude" in enabled else "0"
    install_codex = "1" if "codex" in enabled else "0"
    claude_extension_id = shlex.quote(AI_WORKER_BROWSER_EXTENSION_IDS["claude"])
    codex_extension_id = shlex.quote(AI_WORKER_BROWSER_EXTENSION_IDS["codex"])
    claude_host_name = shlex.quote(AI_WORKER_BROWSER_NATIVE_HOSTS["claude"])
    codex_host_name = shlex.quote(AI_WORKER_BROWSER_NATIVE_HOSTS["codex"])
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"claude_extension_id={claude_extension_id}",
            f"codex_extension_id={codex_extension_id}",
            f"claude_host_name={claude_host_name}",
            f"codex_host_name={codex_host_name}",
            f"install_claude_native_host={install_claude}",
            f"install_codex_native_host={install_codex}",
            'home_dir="${HOME:-/workspace/.wpr-home}"',
            'native_host_dirs=("${home_dir}/.config/chromium/NativeMessagingHosts" "${home_dir}/.config/google-chrome/NativeMessagingHosts")',
            "write_manifest() {",
            '  local manifest_path="$1" host_name="$2" description="$3" host_path="$4" extension_id="$5"',
            '  mkdir -p "$(dirname "$manifest_path")"',
            '  python3 - "$manifest_path" "$host_name" "$description" "$host_path" "$extension_id" <<\'PY\'',
            "import json, sys",
            "from pathlib import Path",
            "manifest_path, host_name, description, host_path, extension_id = sys.argv[1:6]",
            "data = {",
            "    'name': host_name,",
            "    'description': description,",
            "    'path': host_path,",
            "    'type': 'stdio',",
            "    'allowed_origins': [f'chrome-extension://{extension_id}/'],",
            "}",
            "Path(manifest_path).write_text(json.dumps(data, indent=2) + '\\n', encoding='utf-8')",
            "PY",
            "}",
            "remove_disabled_extension_state() {",
            '  local extension_id="$1" host_name="$2"',
            '  local profile_root',
            '  for profile_root in "${home_dir}/.config/chromium" "${home_dir}/.config/google-chrome"; do',
            '    if [ -d "$profile_root" ]; then',
            '      find "$profile_root" -path "*/Extensions/${extension_id}" -prune -exec rm -rf {} + 2>/dev/null || true',
            "    fi",
            "  done",
            '  local native_dir',
            '  for native_dir in "${native_host_dirs[@]}"; do',
            '    rm -f "${native_dir}/${host_name}.json" 2>/dev/null || true',
            "  done",
            "}",
            "install_claude_native_host() {",
            '  local claude_bin="${WPR_CLAUDE_CODE_BIN:-}"',
            '  if [ -z "$claude_bin" ]; then claude_bin="$(command -v claude || true)"; fi',
            '  if [ -z "$claude_bin" ] || [ ! -x "$claude_bin" ]; then',
            '    printf "claude-code native-host pending: claude binary not found\\n"',
            "    return 0",
            "  fi",
            '  local host_path="${home_dir}/.claude/chrome/chrome-native-host"',
            '  mkdir -p "$(dirname "$host_path")"',
            '  python3 - "$host_path" "$claude_bin" <<\'PY\'',
            "import shlex, sys",
            "from pathlib import Path",
            "host_path, claude_bin = sys.argv[1:3]",
            "Path(host_path).write_text('#!/usr/bin/env sh\\nexec ' + shlex.quote(claude_bin) + ' --chrome-native-host\\n', encoding='utf-8')",
            "PY",
            '  chmod 0755 "$host_path"',
            '  for native_dir in "${native_host_dirs[@]}"; do',
            '    write_manifest "${native_dir}/${claude_host_name}.json" "$claude_host_name" "Claude Code Browser Extension Native Host" "$host_path" "$claude_extension_id"',
            "  done",
            '  printf "claude-code native-host installed\\n"',
            "}",
            "codex_arch_dir() {",
            '  case "$(uname -m)" in',
            "    x86_64|amd64) printf 'x64' ;;",
            "    aarch64|arm64) printf 'arm64' ;;",
            "    *) return 1 ;;",
            "  esac",
            "}",
            "find_codex_extension_host() {",
            '  local arch_dir; arch_dir="$(codex_arch_dir)" || return 1',
            "  local roots=()",
            '  if [ -n "${WPR_CODEX_CHROME_PLUGIN_ROOT:-}" ]; then roots+=("${WPR_CODEX_CHROME_PLUGIN_ROOT}"); fi',
            '  if [ -n "${CODEX_CHROME_PLUGIN_ROOT:-}" ]; then roots+=("${CODEX_CHROME_PLUGIN_ROOT}"); fi',
            '  if [ -n "${CODEX_HOME:-}" ]; then roots+=("${CODEX_HOME}/plugins/cache/openai-bundled/chrome/latest"); fi',
            '  roots+=("${home_dir}/.codex/plugins/cache/openai-bundled/chrome/latest")',
            '  roots+=("/usr/local/share/glasshive/openai-bundled/chrome/latest")',
            '  roots+=("/opt/openai-bundled/chrome/latest")',
            '  if [ -n "${CODEX_HOME:-}" ]; then roots+=("${CODEX_HOME}/plugins/cache/openai-bundled/chrome/"*); fi',
            '  roots+=("${home_dir}/.codex/plugins/cache/openai-bundled/chrome/"*)',
            '  roots+=("/usr/local/share/glasshive/openai-bundled/chrome/"*)',
            '  roots+=("/opt/openai-bundled/chrome/"*)',
            '  local root host',
            '  for root in "${roots[@]}"; do',
            '    host="${root}/extension-host/linux/${arch_dir}/extension-host"',
            '    if [ -x "$host" ]; then printf "%s\\n" "$host"; return 0; fi',
            "  done",
            "  return 1",
            "}",
            "write_codex_extension_host_config() {",
            '  local host_path="$1"',
            '  local root; root="$(cd "$(dirname "$host_path")/../../.." && pwd)"',
            '  local codex_bin="${WPR_CODEX_BIN:-}"',
            '  if [ -z "$codex_bin" ]; then codex_bin="$(command -v codex || true)"; fi',
            '  local node_bin; node_bin="$(command -v node || true)"',
            '  local node_repl="${WPR_CODEX_NODE_REPL_PATH:-${CODEX_NODE_REPL_PATH:-}}"',
            '  if [ -z "$node_repl" ]; then node_repl="$(command -v node_repl || true)"; fi',
            '  if [ -z "$codex_bin" ] || [ -z "$node_bin" ] || [ -z "$node_repl" ]; then',
            '    printf "codex native-host config pending: codex/node/node_repl path missing\\n"',
            "    return 1",
            "  fi",
            '  python3 - "$host_path" "$root" "$codex_bin" "$node_bin" "$node_repl" "$codex_extension_id" <<\'PY\'',
            "import json, sys",
            "from pathlib import Path",
            "host_path, root, codex_bin, node_bin, node_repl, extension_id = sys.argv[1:7]",
            "data = {",
            "    'schemaVersion': 1,",
            "    'channel': 'prod',",
            "    'browserClientPath': str(Path(root) / 'scripts' / 'browser-client.mjs'),",
            "    'codexCliPath': codex_bin,",
            "    'extensionId': extension_id,",
            "    'nodePath': node_bin,",
            "    'nodeReplPath': node_repl,",
            "    'proxyHost': '127.0.0.1',",
            "    'proxyPort': 0,",
            "}",
            "Path(host_path).with_name('extension-host-config.json').write_text(json.dumps(data, indent=2) + '\\n', encoding='utf-8')",
            "PY",
            "}",
            "install_codex_native_host() {",
            "  local host_path",
            '  if ! host_path="$(find_codex_extension_host)"; then',
            '    printf "codex native-host pending: extension-host bundle not found\\n"',
            "    return 0",
            "  fi",
            '  if ! write_codex_extension_host_config "$host_path"; then return 0; fi',
            '  for native_dir in "${native_host_dirs[@]}"; do',
            '    write_manifest "${native_dir}/${codex_host_name}.json" "$codex_host_name" "Codex chrome native messaging host" "$host_path" "$codex_extension_id"',
            "  done",
            '  printf "codex native-host installed\\n"',
            "}",
            'if [ "$install_claude_native_host" = "1" ]; then install_claude_native_host; else remove_disabled_extension_state "$claude_extension_id" "$claude_host_name"; printf "claude-code native-host disabled\\n"; fi',
            'if [ "$install_codex_native_host" = "1" ]; then install_codex_native_host; else remove_disabled_extension_state "$codex_extension_id" "$codex_host_name"; printf "codex native-host disabled\\n"; fi',
        ]
    )


@dataclass
class SandboxInfo:
    container_name: str
    container_id: str | None
    state: str
    workspace_dir: str
    home_dir: str
    pid: int | None
    image: str
    novnc_port: int | None = None
    selenium_port: int | None = None
    openclaw_port: int | None = None
    security_options: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    mounts: tuple[tuple[str, str], ...] = ()
    desktop_auth_ready: bool | None = None
    desktop_auth_fingerprint: str | None = None


    execution_policy: str = ""

    image_id: str = ""

    image_reference: str = ""

    runtime_user: str = ""

    entrypoint: tuple[str, ...] | None = None

    command: tuple[str, ...] | None = None

    expected_image_id: str = ""

    expected_runtime_user: str = ""

    expected_entrypoint: tuple[str, ...] | None = None

    expected_command: tuple[str, ...] | None = None

    network_mode: str = ""

    attached_networks: tuple[str, ...] = ()

    pid_mode: str | None = None

    ipc_mode: str | None = None

    uts_mode: str | None = None

    userns_mode: str | None = None

    cgroupns_mode: str | None = None

    read_only_rootfs: bool = False

    privileged: bool | None = None

    cap_add: tuple[str, ...] | None = None

    cap_drop: tuple[str, ...] = ()

    extra_hosts: tuple[str, ...] = ()

    bind_mount_targets: tuple[str, ...] = ()

    bind_mount_pairs: tuple[tuple[str, str], ...] = ()

    mount_records: tuple[tuple[str, str, str], ...] = ()

    bind_mount_options: tuple[tuple[str, str, bool, str, str], ...] = ()

    tmpfs_targets: tuple[str, ...] = ()

    tmpfs_options: tuple[tuple[str, tuple[str, ...]], ...] = ()

    port_bindings: tuple[tuple[int, str, int], ...] = ()

    environment: tuple[tuple[str, str], ...] = ()

    expected_environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FreshSandboxInspection:
    status: str
    sandbox: SandboxInfo | None = None
    reason: str = ""

@dataclass(frozen=True)
class ConfiguredSandboxImage:
    image_id: str
    runtime_user: str
    entrypoint: tuple[str, ...] | None
    command: tuple[str, ...] | None
    environment: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class DockerResourceUsage:
    child_processes: int
    threads: int
    available_memory_bytes: int
    available_disk_bytes: int
    running_worker_containers: int
    running_worker_ids: tuple[str, ...] = ()
    worker_process_counts: tuple[tuple[str, int, int], ...] = ()
    process_probe_ok: bool = True
    memory_probe_ok: bool = True
    disk_probe_ok: bool = True

_DOCKER_SIZE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}

def _docker_size_bytes(value: object) -> int | None:
    raw = str(value or "").strip().split("/", 1)[0].strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)", raw, re.IGNORECASE)
    if not match:
        return None
    multiplier = _DOCKER_SIZE_UNITS.get(match.group(2).lower())
    return int(float(match.group(1)) * multiplier) if multiplier else None

def _docker_command_tuple(
    value: object,
) -> tuple[bool, tuple[str, ...] | None]:
    if value is None:
        return True, None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return True, tuple(value)
    return False, None

def _docker_user_is_root(value: object) -> bool:
    user = str(value or "").strip().split(":", 1)[0].strip().lower()
    return not user or user in {"0", "root"}

def _docker_environment_tuple(
    value: object,
) -> tuple[bool, tuple[tuple[str, str], ...]]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        return False, ()
    environment: dict[str, str] = {}
    for item in value:
        name, separator, raw_value = item.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or name in environment
        ):
            return False, ()
        environment[name] = raw_value
    return True, tuple(sorted(environment.items()))

def _docker_tmpfs_records(
    value: object,
) -> tuple[bool, tuple[tuple[str, tuple[str, ...]], ...]]:
    if not isinstance(value, dict):
        return False, ()
    records: list[tuple[str, tuple[str, ...]]] = []
    for raw_target, raw_options in value.items():
        if not isinstance(raw_target, str) or not raw_target.startswith("/"):
            return False, ()
        if not isinstance(raw_options, str):
            return False, ()
        options = tuple(raw_options.split(","))
        if (
            not options
            or any(not option for option in options)
            or len(set(options)) != len(options)
        ):
            return False, ()
        records.append((raw_target, tuple(sorted(options))))
    return True, tuple(sorted(records))

def _safe_docker_exec_env(env: dict[str, str] | None) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in (env or {}).items()
        if value is not None and (key in SAFE_DOCKER_EXEC_ENV_KEYS or key.startswith("LC_"))
    }


class DockerSandboxManager:
    _build_lock = Lock()
    _desktop_credentials_lock = Lock()
    _default_image = "workers-projects-runtime-workstation:phase1-node22-docs8-openclaw2026.7.1-6"
    _provider_account_mount_target = "/workspace/.provider-account"

    def __init__(self, base_dir: str | None = None, *, create_directories: bool = True) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parents[2] / "data"
        self.runtime_root = self.base_dir / "docker_sandboxes"
        self.build_root = self.runtime_root / "build"
        if create_directories:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            self.build_root.mkdir(parents=True, exist_ok=True)
        self.image = os.environ.get("WPR_SANDBOX_IMAGE", self._default_image)
        self._managed_image = not bool(str(os.environ.get("WPR_SANDBOX_IMAGE") or "").strip())
        self.base_image = AI_WORKER_BASE_IMAGE
        self.user = os.environ.get("WPR_SANDBOX_USER", "seluser")
        self.home_mount = os.environ.get("WPR_SANDBOX_HOME", "/workspace/.wpr-home")
        self.workspace_mount = os.environ.get("WPR_SANDBOX_WORKSPACE", "/workspace/project")
        self.service_tmp_dir = os.environ.get("WPR_SANDBOX_SERVICE_TMPDIR", "/tmp").strip() or "/tmp"
        self.term_value = os.environ.get("WPR_SANDBOX_TERM", "xterm-256color")
        self.display_value = os.environ.get("WPR_SANDBOX_DISPLAY", ":99.0")
        self.chromium_binary = (
            os.environ.get("WPR_SANDBOX_CHROMIUM_BINARY", "/usr/bin/chromium-base").strip()
            or "/usr/bin/chromium-base"
        )
        self.chromium_userns_security_opt = (
            os.environ.get("WPR_SANDBOX_CHROMIUM_USERNS_SECURITY_OPT", "seccomp=unconfined").strip()
            or "seccomp=unconfined"
        )
        self.novnc_container_port = int(os.environ.get("WPR_SANDBOX_NOVNC_PORT", "7900"))
        self.selenium_container_port = int(os.environ.get("WPR_SANDBOX_SELENIUM_PORT", "4444"))
        self.openclaw_container_port = int(os.environ.get("WPR_SANDBOX_OPENCLAW_PORT", "18789"))
        self.vnc_no_password = self._env_flag("WPR_SANDBOX_VNC_NO_PASSWORD", False)
        if self.vnc_no_password and not (
            str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "single_user"
            and self._env_flag("WPR_ALLOW_INSECURE_LOCAL_DESKTOP", False)
        ):
            raise RuntimeError(
                "GlassHive refuses a passwordless desktop unless explicit insecure local single-user mode is enabled"
            )
        self.memory_limit = os.environ.get("WPR_SANDBOX_MEMORY", "3g").strip()
        self.memory_swap_limit = os.environ.get("WPR_SANDBOX_MEMORY_SWAP", self.memory_limit).strip()
        self.cpu_limit = os.environ.get("WPR_SANDBOX_CPUS", "2").strip()
        # cgroup pids counts Linux tasks (including threads). Align the
        # container ceiling with the conservative per-mission thread
        # reservation instead of allowing a single worker 4096 tasks while the
        # workstation-wide admission guard targets 2048.
        self.pids_limit = os.environ.get("WPR_SANDBOX_PIDS_LIMIT", "512").strip()
        self.inspect_timeout_sec = float(os.environ.get("WPR_DOCKER_INSPECT_TIMEOUT_SEC", "5") or "5")
        self.inspect_cache_ttl_sec = float(os.environ.get("WPR_DOCKER_INSPECT_CACHE_TTL_SEC", "5") or "5")
        self.inspect_stale_ttl_sec = float(os.environ.get("WPR_DOCKER_INSPECT_STALE_TTL_SEC", "60") or "60")
        self._inspect_cache: dict[str, tuple[float, SandboxInfo]] = {}
        self.image_inspect_timeout_sec = float(os.environ.get("WPR_DOCKER_IMAGE_INSPECT_TIMEOUT_SEC", "15") or "15")
        self.image_build_timeout_sec = float(os.environ.get("WPR_DOCKER_IMAGE_BUILD_TIMEOUT_SEC", "900") or "900")
        self.image_check_ttl_sec = float(os.environ.get("WPR_DOCKER_IMAGE_CHECK_TTL_SEC", "300") or "300")
        self._image_checked_at: float = 0.0
        self._openclaw_image_checked_at: float = 0.0
        self.novnc_health_timeout_sec = float(os.environ.get("WPR_SANDBOX_NOVNC_HEALTH_TIMEOUT_SEC", "1.5") or "1.5")
        self.novnc_health_cache_ttl_sec = float(os.environ.get("WPR_SANDBOX_NOVNC_HEALTH_CACHE_TTL_SEC", "10") or "10")
        self.novnc_self_heal = self._env_flag("WPR_SANDBOX_NOVNC_SELF_HEAL", True)
        self._novnc_health_cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._resource_usage_cache: tuple[float, DockerResourceUsage] | None = None

    def _invalidate_inspect_cache(self, worker_id: str) -> None:
        self._inspect_cache.pop(worker_id, None)
        self._novnc_health_cache.pop(worker_id, None)

    def _env_flag(self, name: str, default: bool) -> bool:
        raw = str(os.environ.get(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def _private_acl_required(self) -> bool:
        return multi_user_security_enabled()

    def ensure_ready(
        self,
        worker: dict,
        runtime_name: str,
        *,
        start_if_paused: bool = True,
        repair_paths: bool = True,
    ) -> SandboxInfo:
        self._require_docker()
        execution_policy = str(
            bootstrap_bundle_for(worker).get("execution_policy") or ""
        ).strip()
        clean_room = execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        paths = self._paths(worker["worker_id"])
        container_paths: dict[str, Path | int] = dict(paths)
        resource_memory_bytes = self._resource_memory_bytes_from_worker(worker)
        if resource_memory_bytes is not None:
            container_paths[_RESOURCE_MEMORY_BYTES_PATH_KEY] = resource_memory_bytes
        self._ensure_host_dirs(paths)
        private_acl_required = self._private_acl_required()
        container_name = self._container_name(worker["worker_id"])
        sandbox: SandboxInfo | None = None
        needs_idle_prime = False
        needs_path_repair = False
        clean_room_image_prepared = False
        clean_room_container_id = ""

        if clean_room:
            # First-run image preparation may build the configured workstation,
            # but the admission evidence below always comes from a separate,
            # uncached Docker image inspection before removal or secret seed.
            self._ensure_image()
            clean_room_image_prepared = True
            initial_boundary = self.parallel_clean_room_readiness()
            if initial_boundary.get("ready") is not True:
                reason = str(initial_boundary.get("reason") or "unknown_error").strip()
                raise RuntimeError(
                    "Parallel clean-room proxy substrate is unavailable"
                    f": {reason}"
                )
            self._ensure_parallel_clean_room_mission_network(container_name)
            # Attest or remove the existing generation before writing any
            # invocation-fresh broker grant into its bind-mounted home. A
            # legacy wide-network container must never observe the transition.
            # This probe deliberately bypasses the ordinary inspection cache:
            # stale UI/status data is not authority to project a fresh secret.
            fresh_inspection = self.inspect_fresh(worker["worker_id"])
            if fresh_inspection.status == "unavailable":
                inspection_subject = (
                    "image inspection"
                    if "image" in fresh_inspection.reason
                    else "sandbox inspection"
                )
                raise RuntimeError(
                    f"Fresh Parallel clean-room {inspection_subject} is unavailable"
                    f": {fresh_inspection.reason or 'unknown_error'}"
                )
            if fresh_inspection.status == "confirmed_absent":
                sandbox = None
            elif (
                fresh_inspection.status == "present"
                and fresh_inspection.sandbox is not None
            ):
                sandbox = fresh_inspection.sandbox
            else:
                raise RuntimeError(
                    "Fresh Parallel clean-room sandbox inspection is unavailable"
                    ": invalid_inspection_result"
                )
            if sandbox is not None and not self._sandbox_matches_parallel_clean_room_policy(
                sandbox
            ):
                if not self._worker_state_allows_substrate_recreate(worker):
                    raise RuntimeError(
                        "Existing Parallel clean-room sandbox does not attest the required execution policy"
                    )
                expected_container_id = str(sandbox.container_id or "").strip()
                if not expected_container_id:
                    raise RuntimeError(
                        "Existing Parallel clean-room sandbox generation cannot be verified for replacement"
                    )
                self.terminate(
                    worker["worker_id"],
                    expected_container_id=expected_container_id,
                )
                # Exact-ID removal only proves that generation is gone. Probe
                # the name again so a replacement generation cannot inherit a
                # grant seeded for the container we just attested.
                replacement_inspection = self.inspect_fresh(worker["worker_id"])
                if replacement_inspection.status == "unavailable":
                    inspection_subject = (
                        "image inspection"
                        if "image" in replacement_inspection.reason
                        else "sandbox inspection"
                    )
                    raise RuntimeError(
                        f"Fresh Parallel clean-room {inspection_subject} is unavailable"
                        f" after replacement: {replacement_inspection.reason or 'unknown_error'}"
                    )
                if replacement_inspection.status != "confirmed_absent":
                    raise RuntimeError(
                        "Parallel clean-room sandbox generation changed during replacement"
                    )
                sandbox = None
                needs_idle_prime = True
                needs_path_repair = True

            if sandbox is None:
                # Reserve the predictable Docker name and bind-mount generation with an inert
                # canonical container before any invocation-fresh authority is written. Docker
                # creation is fail-closed on name collision; the clean-room command is started
                # only after the exact reserved generation and proxy boundary are re-attested.
                self._invalidate_inspect_cache(worker["worker_id"])
                self._create_container(
                    container_name,
                    container_paths,
                    execution_policy=execution_policy,
                )
                self._invalidate_inspect_cache(worker["worker_id"])
                reservation_inspection = self.inspect_fresh(worker["worker_id"])
                reserved_sandbox = reservation_inspection.sandbox
                if (
                    reservation_inspection.status != "present"
                    or reserved_sandbox is None
                    or not self._sandbox_matches_parallel_clean_room_policy(
                        reserved_sandbox
                    )
                ):
                    raise RuntimeError(
                        "Parallel clean-room sandbox generation reservation could not be attested"
                    )
                sandbox = reserved_sandbox
                needs_idle_prime = True
                needs_path_repair = True

            # Admission/readiness caches are only status hints. Re-attest the live internal
            # network and both proxy generations immediately before projecting any invocation
            # authority into the worker's mounted home.
            boundary = self.parallel_clean_room_readiness()
            if boundary.get("ready") is not True:
                reason = str(boundary.get("reason") or "unknown_error").strip()
                raise RuntimeError(
                    "Fresh Parallel clean-room network boundary is unavailable"
                    f": {reason}"
                )
            self._ensure_parallel_clean_room_mission_network(container_name)

            # Boundary inspection spans multiple Docker objects. Bind the
            # authorization decision back to the same exact worker generation
            # immediately before projecting the invocation-fresh grant.
            expected_boundary_container_id = (
                str(sandbox.container_id or "").strip()
                if sandbox is not None
                else ""
            )
            boundary_inspection = self.inspect_fresh(worker["worker_id"])
            boundary_sandbox = boundary_inspection.sandbox
            if expected_boundary_container_id:
                generation_is_unchanged = bool(
                    boundary_inspection.status == "present"
                    and boundary_sandbox is not None
                    and str(boundary_sandbox.container_id or "").strip()
                    == expected_boundary_container_id
                    and self._sandbox_matches_parallel_clean_room_policy(
                        boundary_sandbox
                    )
                )
            else:
                generation_is_unchanged = (
                    boundary_inspection.status == "confirmed_absent"
                )
            if not generation_is_unchanged:
                raise RuntimeError(
                    "Parallel clean-room sandbox generation changed during boundary attestation"
                )
            if boundary_sandbox is not None:
                sandbox = boundary_sandbox

        if runtime_name == "codex-cli" and not clean_room:
            private_dir = paths["worker_root"] / ".private"
            if self._provider_account_mount(worker) is None:
                self._restore_workspace_codex_auth(paths["home_dir"], private_dir)
            else:
                self._prepare_workspace_codex_auth_overlay(
                    paths["home_dir"], private_dir
                )

        self._seed_bootstrap(
            paths["home_dir"],
            paths["workspace_dir"],
            runtime_name,
            worker,
            trusted_state_dir=paths["state_dir"],
        )
        if clean_room:
            clean_room_container_id = str(sandbox.container_id or "").strip()
            post_seed_inspection = self.inspect_fresh(worker["worker_id"])
            post_seed_sandbox = post_seed_inspection.sandbox
            if (
                not clean_room_container_id
                or post_seed_inspection.status != "present"
                or post_seed_sandbox is None
                or str(post_seed_sandbox.container_id or "").strip()
                != clean_room_container_id
                or not self._sandbox_matches_parallel_clean_room_policy(
                    post_seed_sandbox
                )
            ):
                raise RuntimeError(
                    "Parallel clean-room sandbox generation changed after authority projection"
                )
            sandbox = post_seed_sandbox
        # This runs before every fast/existing-container return. Older releases
        # widened bind mounts recursively, including runtime.env/auth.json; the
        # per-worker marker is written only after a complete no-follow repair.
        if (
            (clean_room or private_acl_required)
            and paths["worker_root"].exists()
        ):
            self._ensure_worker_permissions_migrated(paths["worker_root"])
        if not clean_room:
            sandbox = self.inspect(worker["worker_id"])
        if (
            sandbox is not None
            and not clean_room
            and (
                self._sandbox_needs_chromium_userns_recreate(sandbox)
                or self._sandbox_needs_network_recreate(worker["worker_id"], sandbox)
                or self._sandbox_needs_provider_mount_recreate(worker, sandbox)
                or self._sandbox_needs_desktop_auth_recreate(worker["worker_id"], sandbox)
            )
            and self._worker_state_allows_substrate_recreate(worker)
        ):
            self._docker(["rm", "-f", sandbox.container_name], check=False)
            self._remove_isolated_network(sandbox.container_name)
            self._invalidate_inspect_cache(worker["worker_id"])
            sandbox = None
            needs_idle_prime = True
            needs_path_repair = True
        if sandbox is None:
            # A persisted container ID cannot attest network/capability/mount
            # policy. Automatic clean-room workers must use Docker inspect.
            fast_sandbox = None if clean_room else self.fast_sandbox_from_worker(worker)
            if fast_sandbox is not None:
                if not private_acl_required:
                    return fast_sandbox
                sandbox = fast_sandbox
            if sandbox is None and not clean_room_image_prepared:
                self._ensure_image()
            if sandbox is None:
                self._invalidate_inspect_cache(worker["worker_id"])
                if clean_room:
                    self._create_container(
                        container_name,
                        container_paths,
                        execution_policy=execution_policy,
                    )
                else:
                    self._create_container(
                        container_name,
                        container_paths,
                        worker=worker,
                    )
                self._invalidate_inspect_cache(worker["worker_id"])
                sandbox = self.inspect(worker["worker_id"])
                needs_idle_prime = True
                needs_path_repair = True
        if sandbox is None:
            raise RuntimeError("Failed to create worker sandbox")
        if sandbox.state == "paused" and start_if_paused:
            self._invalidate_inspect_cache(worker["worker_id"])
            self._docker(
                [
                    "unpause",
                    clean_room_container_id if clean_room else container_name,
                ]
            )
            self._invalidate_inspect_cache(worker["worker_id"])
            sandbox = (
                self.inspect_fresh(worker["worker_id"]).sandbox
                if clean_room
                else self.inspect(worker["worker_id"])
            )
        elif sandbox.state in {"created", "exited", "dead"}:
            self._invalidate_inspect_cache(worker["worker_id"])
            self._docker(
                ["start", clean_room_container_id if clean_room else container_name]
            )
            self._invalidate_inspect_cache(worker["worker_id"])
            sandbox = (
                self.inspect_fresh(worker["worker_id"]).sandbox
                if clean_room
                else self.inspect(worker["worker_id"])
            )
            needs_idle_prime = True
            needs_path_repair = True
        if clean_room and (
            sandbox is None
            or str(sandbox.container_id or "").strip() != clean_room_container_id
            or not self._sandbox_matches_parallel_clean_room_policy(sandbox)
        ):
            raise RuntimeError(
                "Parallel clean-room sandbox generation changed during exact startup"
            )
        if sandbox is None:
            raise RuntimeError("Failed to start worker sandbox")
        if (
            private_acl_required
            or needs_path_repair
            or (
                repair_paths
                and self._env_flag("WPR_REPAIR_RUNNING_CONTAINER_ROOTS", False)
            )
        ):
            self._ensure_container_writable_paths(sandbox.container_name, self._default_writable_container_paths())
        if not clean_room and self._provider_account_mount(worker) is not None:
            self._grant_provider_account_access(sandbox.container_name, worker)
        if not clean_room:
            self._repair_provider_temp_ownership(sandbox.container_name)
        self._harden_secret_runtime_files(sandbox.container_name)
        if needs_idle_prime:
            self._set_plain_background(sandbox.container_name)
        if needs_idle_prime and self._env_flag("WPR_IDLE_DESKTOP_PRIME_BROWSER", True):
            try:
                self._prime_idle_desktop(sandbox.container_name)
            except Exception as exc:
                self._record_idle_desktop_prime(worker["worker_id"], sandbox, status="failed", detail=str(exc))
                raise
            self._record_idle_desktop_prime(worker["worker_id"], sandbox, status="launched")
        return sandbox

    def inspect(self, worker_id: str) -> SandboxInfo | None:
        now = time.monotonic()
        cached = self._inspect_cache.get(worker_id)
        if cached and cached[0] + self.inspect_cache_ttl_sec > now:
            return cached[1]
        container_name = self._container_name(worker_id)
        result = self._docker(
            ["inspect", container_name],
            check=False,
            capture_output=True,
            timeout_sec=self.inspect_timeout_sec,
        )
        if result.returncode != 0:
            if cached and cached[0] + self.inspect_stale_ttl_sec > now:
                return cached[1]
            return None
        sandbox = self._sandbox_from_inspect_output(worker_id, result.stdout)
        if sandbox is None:
            if cached and cached[0] + self.inspect_stale_ttl_sec > now:
                return cached[1]
            return None
        self._inspect_cache[worker_id] = (now, sandbox)
        return sandbox

    def inspect_fresh(
        self,
        worker_id: str,
        *,
        require_configured_image: bool = True,
    ) -> FreshSandboxInspection:
        """Probe Docker directly for clean-room secret admission authority."""
        container_name = self._container_name(worker_id)
        try:
            result = self._docker(
                ["inspect", container_name],
                check=False,
                capture_output=True,
                timeout_sec=self.inspect_timeout_sec,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return FreshSandboxInspection(
                status="unavailable",
                reason="docker_inspect_failed",
            )
        if result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").lower()
            if result.returncode == 1 and (
                "no such object" in detail or "no such container" in detail
            ):
                if require_configured_image:
                    configured_image, image_reason = (
                        self._inspect_configured_image_fresh()
                    )
                    if configured_image is None:
                        return FreshSandboxInspection(
                            status="unavailable",
                            reason=image_reason,
                        )
                self._invalidate_inspect_cache(worker_id)
                return FreshSandboxInspection(
                    status="confirmed_absent",
                    reason="docker_confirmed_container_absent",
                )
            return FreshSandboxInspection(
                status="unavailable",
                reason=(
                    "docker_inspect_timeout"
                    if result.returncode == 124
                    else "docker_inspect_failed"
                ),
            )
        sandbox = self._sandbox_from_inspect_output(
            worker_id,
            result.stdout,
            require_valid_container_id=True,
        )
        if sandbox is None:
            return FreshSandboxInspection(
                status="unavailable",
                reason="docker_inspect_malformed",
            )
        if not require_configured_image:
            self._inspect_cache[worker_id] = (time.monotonic(), sandbox)
            return FreshSandboxInspection(status="present", sandbox=sandbox)
        configured_image, image_reason = self._inspect_configured_image_fresh()
        if configured_image is None:
            return FreshSandboxInspection(
                status="unavailable",
                reason=image_reason,
            )
        sandbox.expected_image_id = configured_image.image_id
        sandbox.expected_runtime_user = configured_image.runtime_user
        sandbox.expected_entrypoint = configured_image.entrypoint
        sandbox.expected_command = configured_image.command
        sandbox.expected_environment = configured_image.environment
        self._inspect_cache[worker_id] = (time.monotonic(), sandbox)
        return FreshSandboxInspection(status="present", sandbox=sandbox)

    def _inspect_configured_image_fresh(
        self,
    ) -> tuple[ConfiguredSandboxImage | None, str]:
        try:
            result = self._docker(
                ["image", "inspect", self.image],
                check=False,
                capture_output=True,
                timeout_sec=self.image_inspect_timeout_sec,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None, "docker_image_inspect_failed"
        if result.returncode != 0:
            return (
                None,
                "docker_image_inspect_timeout"
                if result.returncode == 124
                else "docker_image_inspect_failed",
            )
        try:
            payload = json.loads(result.stdout or "[]")
        except (json.JSONDecodeError, TypeError):
            return None, "docker_image_inspect_malformed"
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
        ):
            return None, "docker_image_inspect_malformed"
        entry = payload[0]
        image_id = entry.get("Id")
        config = entry.get("Config")
        if (
            not isinstance(image_id, str)
            or not _DOCKER_IMAGE_ID.fullmatch(image_id)
            or not isinstance(config, dict)
            or "User" not in config
            or "Cmd" not in config
            or "Env" not in config
        ):
            return None, "docker_image_inspect_malformed"
        runtime_user = config.get("User")
        entrypoint_valid, entrypoint = _docker_command_tuple(
            config.get("Entrypoint")
        )
        command_valid, command = _docker_command_tuple(config.get("Cmd"))
        environment_valid, environment = _docker_environment_tuple(
            config.get("Env")
        )
        if (
            not isinstance(runtime_user, str)
            or not entrypoint_valid
            or not command_valid
            or not environment_valid
        ):
            return None, "docker_image_inspect_malformed"
        if (
            self.user != "seluser"
            or runtime_user != self.user
            or _docker_user_is_root(runtime_user)
            or _docker_user_is_root(self.user)
        ):
            return None, "configured_image_user_policy_mismatch"
        return (
            ConfiguredSandboxImage(
                image_id=image_id,
                runtime_user=runtime_user,
                entrypoint=entrypoint,
                command=command,
                environment=environment,
            ),
            "",
        )

    def _sandbox_from_inspect_output(
        self,
        worker_id: str,
        output: str | None,
        *,
        require_valid_container_id: bool = False,
    ) -> SandboxInfo | None:
        try:
            payload = json.loads(output or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
        ):
            return None
        entry = payload[0]
        raw_container_id = entry.get("Id")
        if require_valid_container_id and (
            not isinstance(raw_container_id, str)
            or not _DOCKER_CONTAINER_ID.fullmatch(raw_container_id)
        ):
            return None
        raw_image_id = entry.get("Image")
        state = entry.get("State") or {}
        host_config = entry.get("HostConfig") or {}
        network_settings = entry.get("NetworkSettings") or {}
        if not isinstance(state, dict):
            state = {}
        if not isinstance(host_config, dict):
            host_config = {}
        if not isinstance(network_settings, dict):
            network_settings = {}
        attached_networks = network_settings.get("Networks") or {}
        if not isinstance(attached_networks, dict):
            attached_networks = {}
        status = str(state.get("Status") or "unknown")
        if bool(state.get("Paused")):
            status = "paused"
        pid = state.get("Pid")
        ports = network_settings.get("Ports") or {}
        if not ports or (
            isinstance(ports, dict)
            and ports
            and all(binding in (None, []) for binding in ports.values())
        ):
            # Docker Desktop does not materialize published ports for an internal
            # network. Preserve HostConfig evidence so a legacy container that
            # requested ports is a parsed policy mismatch (and can be replaced),
            # not an ambiguous/malformed generation.
            ports = host_config.get("PortBindings") or {}
        if not isinstance(ports, dict):
            ports = {}
        port_bindings: list[tuple[int, str, int]] = []
        ports_valid = True
        for raw_port, raw_bindings in ports.items():
            if raw_bindings is None:
                continue
            match = re.fullmatch(r"([1-9][0-9]*)/tcp", str(raw_port))
            if not match or not isinstance(raw_bindings, list) or not raw_bindings:
                ports_valid = False
                break
            for binding in raw_bindings:
                if not isinstance(binding, dict):
                    ports_valid = False
                    break
                host_ip = binding.get("HostIp")
                host_port = binding.get("HostPort")
                unassigned_port = host_port == ""
                if (
                    not isinstance(host_ip, str)
                    or not isinstance(host_port, str)
                    or (
                        not unassigned_port
                        and (
                            not host_port.isdigit()
                            or not 1 <= int(host_port) <= 65535
                        )
                    )
                ):
                    ports_valid = False
                    break
                port_bindings.append(
                    (
                        int(match.group(1)),
                        host_ip,
                        0 if unassigned_port else int(host_port),
                    )
                )
            if not ports_valid:
                break
        raw_config = entry.get("Config")
        config = raw_config if isinstance(raw_config, dict) else {}
        environment_valid, environment = _docker_environment_tuple(
            config.get("Env")
        )
        labels = config.get("Labels") or {} if isinstance(config, dict) else {}
        if not isinstance(labels, dict):
            labels = {}
        raw_mounts = entry.get("Mounts")
        mounts = raw_mounts if isinstance(raw_mounts, list) else []
        mounts_valid = isinstance(raw_mounts, list) and all(
            isinstance(mount, dict)
            and isinstance(mount.get("Type"), str)
            and bool(str(mount.get("Type") or ""))
            and isinstance(mount.get("Source"), str)
            and bool(str(mount.get("Source") or ""))
            and isinstance(mount.get("Destination"), str)
            and bool(str(mount.get("Destination") or ""))
            and (
                str(mount.get("Type") or "") != "bind"
                or (
                    isinstance(mount.get("RW"), bool)
                    and isinstance(mount.get("Mode"), str)
                    and isinstance(mount.get("Propagation"), str)
                )
            )
            for mount in mounts
        )
        if not isinstance(mounts, list):
            mounts = []
        tmpfs = host_config.get("Tmpfs") or {}
        tmpfs_valid, tmpfs_options = _docker_tmpfs_records(tmpfs)
        image_reference = config.get("Image")
        runtime_user = config.get("User")
        entrypoint_valid, entrypoint = _docker_command_tuple(
            config.get("Entrypoint")
        )
        command_valid, command = _docker_command_tuple(config.get("Cmd"))
        pid_mode = host_config.get("PidMode")
        ipc_mode = host_config.get("IpcMode")
        uts_mode = host_config.get("UTSMode")
        userns_mode = host_config.get("UsernsMode")
        cgroupns_mode = host_config.get("CgroupnsMode")
        if require_valid_container_id and (
            not isinstance(raw_image_id, str)
            or not _DOCKER_IMAGE_ID.fullmatch(raw_image_id)
            or not isinstance(raw_config, dict)
            or "Image" not in config
            or not isinstance(image_reference, str)
            or not image_reference.strip()
            or "User" not in config
            or not isinstance(runtime_user, str)
            or "Entrypoint" not in config
            or not entrypoint_valid
            or "Cmd" not in config
            or not command_valid
            or not isinstance(pid_mode, str)
            or not isinstance(ipc_mode, str)
            or not isinstance(uts_mode, str)
            or not isinstance(userns_mode, str)
            or not isinstance(cgroupns_mode, str)
            or not environment_valid
            or not ports_valid
            or not mounts_valid
            or not tmpfs_valid
        ):
            return None
        cap_add: tuple[str, ...] | None = None
        if "CapAdd" in host_config:
            raw_cap_add = host_config.get("CapAdd")
            if raw_cap_add is None:
                cap_add = ()
            elif isinstance(raw_cap_add, list):
                cap_add = tuple(str(capability) for capability in raw_cap_add)
        return SandboxInfo(
            container_name=self._container_name(worker_id),
            container_id=str(entry.get("Id") or "").strip() or None,
            state=status,
            workspace_dir=str(self._paths(worker_id)["workspace_dir"]),
            home_dir=str(self._paths(worker_id)["home_dir"]),
            pid=int(pid) if isinstance(pid, int) and pid > 0 and status == "running" else None,
            image=self.image,
            novnc_port=self._host_port_for(ports, self.novnc_container_port),
            selenium_port=self._host_port_for(ports, self.selenium_container_port),
            openclaw_port=self._host_port_for(ports, self.openclaw_container_port),
            security_options=tuple(
                str(option)
                for option in (host_config.get("SecurityOpt") or [])
                if option
            ),
            execution_policy=(
                str(labels.get(PARALLEL_CLEAN_ROOM_POLICY_LABEL) or "")
                if isinstance(labels, dict)
                else ""
            ),
            image_id=str(raw_image_id or ""),
            image_reference=str(image_reference or ""),
            runtime_user=str(runtime_user or ""),
            entrypoint=entrypoint,
            command=command,
            network_mode=str(host_config.get("NetworkMode") or ""),
            attached_networks=tuple(
                sorted(str(network) for network in attached_networks)
            ),
            pid_mode=pid_mode if isinstance(pid_mode, str) else None,
            ipc_mode=ipc_mode if isinstance(ipc_mode, str) else None,
            uts_mode=uts_mode if isinstance(uts_mode, str) else None,
            userns_mode=userns_mode if isinstance(userns_mode, str) else None,
            cgroupns_mode=(
                cgroupns_mode if isinstance(cgroupns_mode, str) else None
            ),
            read_only_rootfs=host_config.get("ReadonlyRootfs") is True,
            privileged=(
                host_config.get("Privileged")
                if isinstance(host_config.get("Privileged"), bool)
                else None
            ),
            cap_add=cap_add,
            cap_drop=tuple(
                str(capability)
                for capability in (host_config.get("CapDrop") or [])
                if capability
            ),
            extra_hosts=tuple(
                str(extra_host)
                for extra_host in (host_config.get("ExtraHosts") or [])
                if extra_host
            ),
            bind_mount_targets=tuple(
                sorted(
                    str(mount.get("Destination") or "")
                    for mount in mounts
                    if isinstance(mount, dict)
                    and str(mount.get("Type") or "") == "bind"
                    and mount.get("Destination")
                )
            ),
            bind_mount_pairs=tuple(
                sorted(
                    (
                        str(mount.get("Source") or ""),
                        str(mount.get("Destination") or ""),
                    )
                    for mount in mounts
                    if isinstance(mount, dict)
                    and str(mount.get("Type") or "") == "bind"
                    and mount.get("Source")
                    and mount.get("Destination")
                )
            ),
            mount_records=tuple(
                sorted(
                    (
                        str(mount.get("Type") or ""),
                        str(mount.get("Source") or ""),
                        str(mount.get("Destination") or ""),
                    )
                    for mount in mounts
                    if isinstance(mount, dict)
                    and mount.get("Type")
                    and mount.get("Source")
                    and mount.get("Destination")
                )
            ),
            bind_mount_options=tuple(
                sorted(
                    (
                        str(mount.get("Source") or ""),
                        str(mount.get("Destination") or ""),
                        bool(mount.get("RW")),
                        str(mount.get("Mode") or ""),
                        str(mount.get("Propagation") or ""),
                    )
                    for mount in mounts
                    if isinstance(mount, dict)
                    and str(mount.get("Type") or "") == "bind"
                )
            ),
            tmpfs_targets=tuple(
                sorted(str(target) for target in tmpfs)
                if isinstance(tmpfs, dict)
                else ()
            ),
            tmpfs_options=tmpfs_options,
            port_bindings=tuple(sorted(port_bindings)),
            environment=environment,
        )

    def pause(
        self,
        worker_id: str,
        *,
        expected_container_id: str | None = None,
    ) -> SandboxInfo:
        sandbox = self.inspect(worker_id)
        expected_id = str(expected_container_id or "").strip()
        if expected_id and (
            sandbox is None
            or str(sandbox.container_id or "").strip() != expected_id
        ):
            raise RuntimeError("Worker sandbox generation changed before pause")
        if sandbox is None:
            return SandboxInfo(
                container_name=self._container_name(worker_id),
                container_id=None,
                state="missing",
                workspace_dir=str(self._paths(worker_id)["workspace_dir"]),
                home_dir=str(self._paths(worker_id)["home_dir"]),
                pid=None,
                image=self.image,
                openclaw_port=None,
            )
        if sandbox.state == "running":
            target = expected_id or sandbox.container_name
            result = self._docker(
                ["pause", target], check=False, capture_output=True
            )
            if result.returncode != 0:
                raise RuntimeError("Docker pause could not be confirmed")
            self._invalidate_inspect_cache(worker_id)
        confirmed = self.inspect(worker_id)
        if expected_id and (
            confirmed is None
            or str(confirmed.container_id or "").strip() != expected_id
        ):
            raise RuntimeError("Worker sandbox generation changed during pause")
        return confirmed or sandbox

    def terminate(
        self,
        worker_id: str,
        *,
        expected_container_id: str | None = None,
        expected_absent: bool = False,
        execution_policy: str = "",
    ) -> SandboxInfo:
        container_name = self._container_name(worker_id)
        requested_clean_room = (
            str(execution_policy or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        )
        if requested_clean_room:
            fresh = self.inspect_fresh(
                worker_id,
                require_configured_image=False,
            )
            if fresh.status == "unavailable":
                raise RuntimeError(
                    "Fresh Parallel clean-room sandbox termination inspection is unavailable"
                    f": {fresh.reason or 'unknown_error'}"
                )
            sandbox = fresh.sandbox if fresh.status == "present" else None
        else:
            sandbox = self.inspect(worker_id)

        clean_room = requested_clean_room or (
            sandbox is not None
            and sandbox.execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        )
        mission_network = (
            str(sandbox.network_mode or "").strip()
            if clean_room and sandbox is not None
            else self._parallel_clean_room_mission_network_name(container_name)
            if clean_room
            else ""
        )
        expected_id = str(expected_container_id or "").strip()
        if expected_absent and expected_id:
            raise RuntimeError("Worker sandbox termination identity is contradictory")
        if expected_absent and sandbox is not None:
            raise RuntimeError("Worker sandbox generation changed before termination")
        if expected_id and sandbox is not None and (
            str(sandbox.container_id or "").strip() != expected_id
        ):
            raise RuntimeError("Worker sandbox generation changed before termination")

        removal_target = expected_id or (
            sandbox.container_name if sandbox is not None else container_name
        )
        if sandbox is not None:
            self._docker(
                ["rm", "-f", removal_target],
                check=False,
                capture_output=True,
            )
            self._invalidate_inspect_cache(worker_id)

        confirmation_target = expected_id or container_name
        confirmation = self._docker(
            ["inspect", confirmation_target],
            check=False,
            capture_output=True,
            timeout_sec=self.inspect_timeout_sec,
        )
        detail = str(confirmation.stderr or confirmation.stdout or "").lower()
        absence_confirmed = confirmation.returncode != 0 and (
            (not expected_id and not clean_room)
            or "no such object" in detail
            or "no such container" in detail
        )
        if not absence_confirmed:
            raise RuntimeError(
                f"Docker sandbox {confirmation_target} is still running after termination; "
                "removal could not be confirmed"
            )
        if clean_room:
            self._remove_parallel_clean_room_mission_network(
                container_name,
                network_name=mission_network or None,
            )
        elif sandbox is not None:
            self._remove_isolated_network(container_name)
        return SandboxInfo(
            container_name=confirmation_target,
            container_id=None,
            state="terminated",
            workspace_dir=str(self._paths(worker_id)["workspace_dir"]),
            home_dir=str(self._paths(worker_id)["home_dir"]),
            pid=None,
            image=self.image,
            openclaw_port=None,
        )

    def project_parallel_clean_room_run_secrets(
        self,
        worker_id: str,
        *,
        expected_container_id: str,
        run_id: str,
        env: dict[str, str],
    ) -> dict[str, str]:
        container_id = str(expected_container_id or "").strip()
        clean_run_id = str(run_id or "").strip()
        if not _DOCKER_CONTAINER_ID.fullmatch(container_id):
            raise RuntimeError(
                "Parallel clean-room sandbox generation is unavailable for run authority"
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", clean_run_id):
            raise RuntimeError("Parallel clean-room run identity is invalid")
        if set(env) != {"GLASSHIVE_CAPABILITY_BROKER_TOKEN"}:
            raise RuntimeError("Parallel clean-room run authority scope is invalid")
        grant = str(env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,16384}", grant):
            raise RuntimeError("Parallel clean-room run authority is invalid")

        container_name = self._container_name(worker_id)
        boundary = self.parallel_clean_room_readiness()
        if boundary.get("ready") is not True:
            reason = str(boundary.get("reason") or "unknown_error").strip()
            raise RuntimeError(
                "Fresh Parallel clean-room network boundary is unavailable"
                f": {reason}"
            )
        self._ensure_parallel_clean_room_mission_network(container_name)
        before = self.inspect_fresh(worker_id)
        sandbox = before.sandbox
        if (
            before.status != "present"
            or sandbox is None
            or sandbox.state != "running"
            or str(sandbox.container_id or "").strip() != container_id
            or not self._sandbox_matches_parallel_clean_room_policy(sandbox)
        ):
            raise RuntimeError(
                "Parallel clean-room sandbox generation changed before run authority projection"
            )

        secret_root = f"/run/glasshive/{clean_run_id}"
        prepared = self._docker_exec(
            container_id,
            [
                "bash",
                "-c",
                (
                    "set -e; umask 077; "
                    f"mkdir -p {shlex.quote(secret_root)}; "
                    f"chmod 700 {shlex.quote(secret_root)}"
                ),
            ],
            user=self.user,
        )
        if prepared.returncode != 0:
            raise RuntimeError(
                "Parallel clean-room tmpfs authority directory could not be prepared"
            )

        env_file = f"{secret_root}/secret-runtime.env"
        keys_file = f"{secret_root}/secret-runtime.keys"
        for destination, content in (
            (
                env_file,
                "export GLASSHIVE_CAPABILITY_BROKER_TOKEN="
                f"{shlex.quote(grant)}\n",
            ),
            (keys_file, "GLASSHIVE_CAPABILITY_BROKER_TOKEN\n"),
        ):
            written = self._docker_exec(
                container_id,
                [
                    "bash",
                    "-c",
                    (
                        "set -e; umask 077; "
                        f"cat > {shlex.quote(destination)}; "
                        f"chmod 600 {shlex.quote(destination)}"
                    ),
                ],
                user=self.user,
                input_text=content,
            )
            if written.returncode != 0:
                raise RuntimeError(
                    "Parallel clean-room run authority could not be projected"
                )
        after = self.inspect_fresh(worker_id)
        if (
            after.status != "present"
            or after.sandbox is None
            or str(after.sandbox.container_id or "").strip() != container_id
            or not self._sandbox_matches_parallel_clean_room_policy(after.sandbox)
        ):
            raise RuntimeError(
                "Parallel clean-room sandbox generation changed during run authority projection"
            )
        return {"env_file": env_file, "keys_file": keys_file}

    def clear_parallel_clean_room_run_secrets(
        self,
        worker_id: str,
        *,
        expected_container_id: str,
        run_id: str,
    ) -> None:
        container_id = str(expected_container_id or "").strip()
        clean_run_id = str(run_id or "").strip()
        if not _DOCKER_CONTAINER_ID.fullmatch(container_id) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", clean_run_id
        ):
            raise RuntimeError(
                "Parallel clean-room run authority cleanup identity is invalid"
            )
        inspection = self.inspect_fresh(worker_id)
        sandbox = inspection.sandbox
        if inspection.status == "confirmed_absent":
            return
        if (
            inspection.status != "present"
            or sandbox is None
            or str(sandbox.container_id or "").strip() != container_id
        ):
            # Never mutate a replacement generation. The exact old container's
            # tmpfs disappeared with that generation.
            return
        secret_root = f"/run/glasshive/{clean_run_id}"
        cleared = self._docker_exec(
            container_id,
            ["rm", "-rf", "--", secret_root],
            user=self.user,
        )
        if cleared.returncode != 0:
            detail = str(cleared.stderr or cleared.stdout or "").lower()
            if "no such container" not in detail and "no such object" not in detail:
                raise RuntimeError(
                    "Parallel clean-room run authority cleanup could not be confirmed"
                )

    def exec_command(
        self,
        worker_id: str,
        runtime_name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        worker: dict | None = None,
    ) -> list[str]:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name, repair_paths=False)
        docker_command = [
            "docker",
            "exec",
            "-i",
            "-u",
            self.user,
            "-w",
            self.workspace_mount,
            "-e",
            f"HOME={self.home_mount}",
            "-e",
            f"TERM={self.term_value}",
        ]
        merged_env = _safe_docker_exec_env(env)
        secret_keys = {
            key
            for key in merged_env
            if key.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
            or key in {"AWS_ACCESS_KEY_ID", "PORTKEY_VIRTUAL_KEY"}
        }
        if secret_keys:
            raise ValueError(
                "Secret-bearing worker commands must use GlassHive's managed execution path so values stay out of argv"
            )
        for key, value in sorted(merged_env.items()):
            if value is None:
                continue
            docker_command.extend(["-e", f"{key}={value}"])
        docker_command.append(sandbox.container_name)
        docker_command.extend(command)
        return docker_command

    def terminal_attach_command(self, worker_id: str, runtime_name: str, session_name: str = "operator") -> list[str]:
        sandbox = self.ensure_ready({"worker_id": worker_id}, runtime_name=runtime_name)
        self._ensure_screen_runtime_dir(
            sandbox.container_name,
            clean_room=getattr(sandbox, "execution_policy", "")
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        )
        return [
            "docker",
            "exec",
            "-it",
            "-u",
            self.user,
            "-w",
            self.workspace_mount,
            "-e",
            f"HOME={self.home_mount}",
            "-e",
            f"TERM={self.term_value}",
            "-e",
            f"TMPDIR={self._browser_tmp_dir()}",
            "-e",
            f"XDG_CACHE_HOME={self._browser_cache_dir()}",
            "-e",
            f"XDG_CONFIG_HOME={self._browser_config_dir()}",
            sandbox.container_name,
            "screen",
            "-xRR",
            session_name,
        ]

    def list_screen_sessions(self, worker_id: str, runtime_name: str, *, worker: dict | None = None) -> list[str]:
        resolved_worker = worker or {"worker_id": worker_id}
        exact_release_probe = "_compute_release_container_id" in resolved_worker
        expected_container_id = str(
            resolved_worker.get("_compute_release_container_id") or ""
        ).strip()
        if exact_release_probe:
            container_name = expected_container_id or self._container_name(worker_id)
        else:
            sandbox = self.ensure_ready(
                resolved_worker, runtime_name=runtime_name, repair_paths=False
            )
            container_name = sandbox.container_name
            self._ensure_screen_runtime_dir(
                container_name,
                clean_room=getattr(sandbox, "execution_policy", "")
                == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
            )
        result = self._docker_exec(
            container_name,
            ["bash", "-c", "screen -ls || true"],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
        )
        if exact_release_probe and result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").lower()
            if "no such container" in detail or "no such object" in detail:
                return []
        output = "\n".join(filter(None, [(result.stdout or "").strip(), (result.stderr or "").strip()]))
        sessions: list[str] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if "\t(" not in line or "." not in line:
                continue
            head = line.split("\t", 1)[0].strip()
            if "." not in head:
                continue
            sessions.append(head.split(".", 1)[1].strip())
        return sessions

    def screen_session_pid(
        self,
        worker_id: str,
        runtime_name: str,
        session_name: str,
        *,
        worker: dict | None = None,
    ) -> int | None:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.fast_sandbox_from_worker(resolved_worker) or self.inspect(worker_id)
        if sandbox is None:
            return None
        try:
            self._ensure_screen_runtime_dir(
                sandbox.container_name,
                clean_room=getattr(sandbox, "execution_policy", "")
                == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
            )
        except RuntimeError as exc:
            # This lookup is read-only. A running session already proves its socket tree
            # is usable; re-preparing it (for example a chmod the hardened clean-room
            # user may no longer perform after start) must not hide a live generation
            # from restart identity verification. ``screen -ls`` below stays authoritative.
            logger.debug(
                "Screen runtime directory preparation skipped for pid lookup in %s: %s",
                sandbox.container_name,
                exc,
            )
        script = r"""
target="$1"
screen -ls | awk -v target="$target" '
  /^[[:space:]]*[0-9]+[.]/ {
    socket=$1;
    split(socket, parts, ".");
    name=socket;
    sub(/^[0-9]+[.]/, "", name);
    if (name == target) { print parts[1]; exit; }
  }
'
"""
        result = self._docker_exec(
            sandbox.container_name,
            ["bash", "-lc", script, "bash", session_name],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
        )
        if result.returncode != 0:
            return None
        lines = (result.stdout or "").strip().splitlines()
        if not lines:
            return None
        candidate = lines[0].strip()
        return int(candidate) if candidate.isdigit() else None

    def start_screen_session(
        self,
        worker_id: str,
        runtime_name: str,
        session_name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        worker: dict | None = None,
    ) -> subprocess.CompletedProcess[str]:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.fast_sandbox_from_worker(resolved_worker) or self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        self._ensure_screen_runtime_dir(
            sandbox.container_name,
            clean_room=getattr(sandbox, "execution_policy", "")
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        )
        merged_env = {
            **self._desktop_env(),
            **_safe_docker_exec_env(env),
        }
        self.stop_screen_session(worker_id, runtime_name, session_name, worker=resolved_worker, missing_ok=True)
        return self._docker_exec(
            sandbox.container_name,
            ["screen", "-DmS", session_name, *command],
            env=merged_env,
            cwd=self.workspace_mount,
            detach=True,
        )

    def ensure_container_writable_paths(
        self,
        worker_id: str,
        runtime_name: str,
        container_paths: list[str],
        *,
        worker: dict | None = None,
    ) -> None:
        if not container_paths:
            return
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = (
            self.fast_sandbox_from_worker(resolved_worker)
            or self.inspect(worker_id)
            or self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        )
        self._ensure_container_writable_paths(sandbox.container_name, container_paths)

    def harden_worker_host_tree(self, worker_id: str) -> None:
        """Reassert host-side owner-only modes without following worker links."""

        self._harden_host_worker_tree(self.paths(worker_id)["worker_root"])

    def stop_screen_session(
        self,
        worker_id: str,
        runtime_name: str,
        session_name: str,
        *,
        worker: dict | None = None,
        missing_ok: bool = False,
        expected_container_id: str | None = None,
    ) -> None:
        resolved_worker = worker or {"worker_id": worker_id}
        expected_id = str(expected_container_id or "").strip()
        container_name = expected_id or self._container_name(worker_id)
        if not expected_id and not self._worker_state_allows_fast_exec(resolved_worker):
            sandbox = self.inspect(worker_id)
            if sandbox is None:
                if missing_ok:
                    return
                raise RuntimeError(f"Worker sandbox {container_name} is not running")
            container_name = sandbox.container_name
        script = "\n".join(
            [
                "target=$1",
                "matching_sockets() {",
                "  screen -ls | awk -v target=\"$target\" '",
                "    /^[[:space:]]*[0-9]+[.]/ {",
                "      socket=$1;",
                "      name=socket;",
                "      sub(/^[0-9]+[.]/, \"\", name);",
                "      if (name == target) print socket;",
                "    }",
                "  '",
                "}",
                "sockets=$(matching_sockets)",
                "if [ -z \"$sockets\" ]; then exit 42; fi",
                "for socket in $sockets; do",
                "  screen -S \"$socket\" -X quit >/dev/null 2>&1 || true",
                "done",
                "for _attempt in $(seq 1 20); do",
                "  sockets=$(matching_sockets)",
                "  if [ -z \"$sockets\" ]; then exit 0; fi",
                "  sleep 0.1",
                "done",
                "printf 'Screen session remains after quit: %s\\n' \"$sockets\" >&2",
                "exit 43",
            ]
        )
        result = self._docker_exec(
            container_name,
            ["bash", "-c", script, "glasshive-stop-screen", session_name],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
        )
        detail_lower = str(result.stderr or result.stdout or "").lower()
        confirmed_missing = bool(
            expected_id
            and missing_ok
            and (
                "no such container" in detail_lower
                or "no such object" in detail_lower
            )
        )
        if result.returncode != 0 and not (
            (missing_ok and result.returncode == 42) or confirmed_missing
        ):
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Failed to stop screen session {session_name}: {detail}")

    def terminate_run_processes(
        self,
        worker_id: str,
        runtime_name: str,
        run_id: str,
        *,
        worker: dict | None = None,
        missing_ok: bool = False,
        expected_container_id: str | None = None,
    ) -> None:
        resolved_worker = worker or {"worker_id": worker_id}
        expected_id = str(expected_container_id or "").strip()
        container_name = expected_id or self._container_name(worker_id)
        if not expected_id and not self._worker_state_allows_fast_exec(resolved_worker):
            sandbox = self.inspect(worker_id)
            if sandbox is None:
                raise RuntimeError(f"Worker sandbox {container_name} is not running")
            container_name = sandbox.container_name
        run_root = f"{self.home_mount}/.glasshive-runs/{run_id}"
        script = "\n".join(
            [
                "needle=$GLASSHIVE_STOP_RUN_ROOT",
                "run_id=$GLASSHIVE_STOP_RUN_ID",
                "self_pid=$$",
                "self_pgid=$(ps -o pgid= -p \"$self_pid\" | tr -d ' ')",
                "matching_pids() {",
                "  arg_pids=$(ps -eo pid=,stat=,args= | while read -r pid stat args; do",
                "    [ \"$pid\" = \"$self_pid\" ] && continue",
                "    [ \"${stat#Z}\" = \"$stat\" ] || continue",
                "    [ \"${args#*\"$needle\"}\" = \"$args\" ] || printf '%s\\n' \"$pid\"",
                "  done)",
                "  env_pids=$(for process_env in /proc/[0-9]*/environ; do",
                "    pid=${process_env#/proc/}; pid=${pid%%/*}",
                "    [ \"$pid\" = \"$self_pid\" ] && continue",
                "    tr '\\0' '\\n' < \"$process_env\" 2>/dev/null | grep -Fxq \"GLASSHIVE_ACTIVE_RUN_ID=$run_id\" && printf '%s\\n' \"$pid\"",
                "  done)",
                "  printf '%s\\n%s\\n' \"$arg_pids\" \"$env_pids\" | awk -v self_pid=\"$self_pid\" 'NF && $1 != self_pid' | sort -u",
                "}",
                "descendants() { "
                "for parent in \"$@\"; do "
                "children=$(ps -eo pid=,ppid= | awk -v p=\"$parent\" '$2 == p { print $1 }'); "
                "if [ -n \"$children\" ]; then descendants $children; fi; "
                "printf '%s\\n' \"$parent\"; "
                "done; "
                "}",
                "process_spec() {",
                "  pid=$1",
                "  [ -r \"/proc/$pid/stat\" ] || return 0",
                "  stat=$(cat \"/proc/$pid/stat\" 2>/dev/null) || return 0",
                "  rest=${stat#*) }",
                "  set -- $rest",
                "  state=$1",
                "  start_time=${20}",
                "  [ \"${state#Z}\" = \"$state\" ] || return 0",
                "  printf '%s:%s\\n' \"$pid\" \"$start_time\"",
                "}",
                "capture_specs() {",
                "  for pid in \"$@\"; do process_spec \"$pid\"; done",
                "}",
                "capture_pgids() {",
                "  for pid in \"$@\"; do",
                "    pgid=$(ps -o pgid= -p \"$pid\" 2>/dev/null | tr -d ' ')",
                "    [ -n \"$pgid\" ] && [ \"$pgid\" != \"$self_pgid\" ] && printf '%s\\n' \"$pgid\"",
                "  done",
                "}",
                "alive_pids() {",
                "  for spec in \"$@\"; do",
                "    pid=${spec%%:*}",
                "    expected_start=${spec#*:}",
                "    current=$(process_spec \"$pid\")",
                "    [ \"$current\" = \"$pid:$expected_start\" ] && printf '%s\\n' \"$pid\"",
                "  done",
                "}",
                "merge_specs() {",
                "  printf '%s\\n%s\\n' \"$1\" \"$2\" | awk 'NF' | sort -u",
                "}",
                "alive_pgids() {",
                "  for pgid in \"$@\"; do",
                "    ps -eo pgid=,stat= | awk -v target=\"$pgid\" '",
                "      $1 == target && $2 !~ /^Z/ { found=1 }",
                "      END { exit found ? 0 : 1 }",
                "    ' && printf '%s\\n' \"$pgid\"",
                "  done",
                "}",
                "signal_groups() {",
                "  signal=$1",
                "  shift",
                "  for pgid in \"$@\"; do kill \"-$signal\" -- \"-$pgid\" >/dev/null 2>&1 || true; done",
                "}",
                "pids=$(matching_pids)",
                "if [ -z \"$pids\" ]; then exit 0; fi",
                "targets=$(descendants $pids | awk -v self_pid=\"$self_pid\" 'NF && $1 != self_pid' | sort -u)",
                "specs=$(capture_specs $targets)",
                "pgids=$(capture_pgids $targets | sort -u)",
                "for pid in $(alive_pids $specs); do kill -TERM \"$pid\" >/dev/null 2>&1 || true; done",
                "signal_groups TERM $pgids",
                "for _attempt in $(seq 1 20); do",
                "  discovered=$(matching_pids)",
                "  if [ -n \"$discovered\" ]; then",
                "    discovered=$(descendants $discovered | awk -v self_pid=\"$self_pid\" 'NF && $1 != self_pid' | sort -u)",
                "    specs=$(merge_specs \"$specs\" \"$(capture_specs $discovered)\")",
                "    pgids=$(merge_specs \"$pgids\" \"$(capture_pgids $discovered)\")",
                "  fi",
                "  tracked=$(alive_pids $specs)",
                "  if [ -n \"$tracked\" ]; then",
                "    expanded=$(descendants $tracked | awk -v self_pid=\"$self_pid\" 'NF && $1 != self_pid' | sort -u)",
                "    specs=$(merge_specs \"$specs\" \"$(capture_specs $expanded)\")",
                "    pgids=$(merge_specs \"$pgids\" \"$(capture_pgids $expanded)\")",
                "  fi",
                "  survivors=$(alive_pids $specs)",
                "  surviving_groups=$(alive_pgids $pgids)",
                "  if [ -z \"$survivors\" ] && [ -z \"$surviving_groups\" ]; then exit 0; fi",
                "  for pid in $survivors; do kill -TERM \"$pid\" >/dev/null 2>&1 || true; done",
                "  signal_groups TERM $surviving_groups",
                "  sleep 0.1",
                "done",
                "for pid in $survivors; do kill -KILL \"$pid\" >/dev/null 2>&1 || true; done",
                "signal_groups KILL $surviving_groups",
                "for _attempt in $(seq 1 20); do",
                "  discovered=$(matching_pids)",
                "  if [ -n \"$discovered\" ]; then",
                "    discovered=$(descendants $discovered | awk -v self_pid=\"$self_pid\" 'NF && $1 != self_pid' | sort -u)",
                "    specs=$(merge_specs \"$specs\" \"$(capture_specs $discovered)\")",
                "    pgids=$(merge_specs \"$pgids\" \"$(capture_pgids $discovered)\")",
                "  fi",
                "  tracked=$(alive_pids $specs)",
                "  if [ -n \"$tracked\" ]; then",
                "    expanded=$(descendants $tracked | awk -v self_pid=\"$self_pid\" 'NF && $1 != self_pid' | sort -u)",
                "    specs=$(merge_specs \"$specs\" \"$(capture_specs $expanded)\")",
                "    pgids=$(merge_specs \"$pgids\" \"$(capture_pgids $expanded)\")",
                "  fi",
                "  survivors=$(alive_pids $specs)",
                "  surviving_groups=$(alive_pgids $pgids)",
                "  if [ -z \"$survivors\" ] && [ -z \"$surviving_groups\" ]; then exit 0; fi",
                "  for pid in $survivors; do kill -KILL \"$pid\" >/dev/null 2>&1 || true; done",
                "  signal_groups KILL $surviving_groups",
                "  sleep 0.1",
                "done",
                "printf 'Run processes remain after TERM/KILL: pids=%s groups=%s\\n' \\",
                "  \"$(printf '%s' \"$survivors\" | tr '\\n' ' ')\" \\",
                "  \"$(printf '%s' \"$surviving_groups\" | tr '\\n' ' ')\" >&2",
                "exit 43",
            ]
        )
        if expected_id:
            script = "\n".join(
                [
                    f"captured_run_root={shlex.quote(run_root)}",
                    '[ "$GLASSHIVE_STOP_RUN_ROOT" = "$captured_run_root" ] || exit 44',
                    script,
                ]
            )
        result = self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env={
                **self._desktop_env(),
                "GLASSHIVE_STOP_RUN_ROOT": run_root,
                "GLASSHIVE_STOP_RUN_ID": run_id,
            },
            cwd=self.workspace_mount,
        )
        detail_lower = str(result.stderr or result.stdout or "").lower()
        confirmed_missing = bool(
            expected_id
            and missing_ok
            and (
                "no such container" in detail_lower
                or "no such object" in detail_lower
            )
        )
        if result.returncode != 0 and not confirmed_missing:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            if not detail:
                detail = f"docker exec exited with status {result.returncode}"
            raise RuntimeError(
                f"Failed to terminate exact run processes for {run_id}: {detail}"
            )

    def desktop_action(
        self,
        worker_id: str,
        runtime_name: str,
        action: str,
        *,
        url: str | None = None,
        session_name: str | None = None,
        worker: dict | None = None,
        on_exit: Callable[[], None] | None = None,
        max_runtime_seconds: float | None = None,
    ) -> dict[str, object]:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.fast_sandbox_from_worker(resolved_worker) or self.ensure_ready(
            resolved_worker,
            runtime_name=runtime_name,
            repair_paths=False,
        )
        normalized = action.strip().lower().replace("-", "_")
        command = self._desktop_action_command(normalized, url=url, session_name=session_name)
        if not command:
            raise ValueError(f"Unsupported desktop action: {action}")
        merged_env = {
            **self._desktop_env(),
        }
        if resolved_worker.get("_glasshive_provider_account_bound"):
            apply_bound_provider_account_environment(
                resolved_worker,
                merged_env,
                runtime_name=runtime_name,
            )
        exec_options: dict[str, object] = {}
        if on_exit is not None:
            exec_options.update(
                {
                    "on_exit": on_exit,
                    "max_runtime_seconds": max_runtime_seconds,
                }
            )
        result = self._docker_exec(
            sandbox.container_name,
            command,
            env=merged_env,
            cwd=self.workspace_mount,
            detach=on_exit is None,
            fire_and_forget=True,
            **exec_options,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Desktop action {action} failed: {detail}")
        return {
            "action": normalized,
            "container_name": sandbox.container_name,
            "view_url": self._view_url_from_sandbox(worker_id, sandbox),
            "status": "launched",
        }

    def describe(self, worker_id: str) -> dict[str, object]:
        sandbox = self.inspect(worker_id)
        paths = self._paths(worker_id)
        view_health = self._desktop_view_health(worker_id, sandbox)
        view_url = self._view_url_from_sandbox(worker_id, sandbox) if view_health.get("healthy") else None
        return {
            "driver": "docker",
            "image": sandbox.image if sandbox else self.image,
            "container_name": self._container_name(worker_id),
            "container_id": sandbox.container_id if sandbox else None,
            "state": sandbox.state if sandbox else "missing",
            "workspace_dir": str(paths["workspace_dir"]),
            "home_dir": str(paths["home_dir"]),
            "pid": sandbox.pid if sandbox else None,
            "novnc_port": sandbox.novnc_port if sandbox else None,
            "selenium_port": sandbox.selenium_port if sandbox else None,
            "openclaw_port": sandbox.openclaw_port if sandbox else None,
            "view_url": view_url,
            "view_available": bool(view_url),
            "view_health": view_health,
            "desktop_prime": self._read_idle_desktop_prime(worker_id),
        }

    def view_url(self, worker_id: str) -> str | None:
        sandbox = self.inspect(worker_id)
        return self._view_url_from_sandbox(worker_id, sandbox)

    def _view_url_from_sandbox(self, worker_id: str, sandbox: SandboxInfo | None) -> str | None:
        if sandbox is None or sandbox.novnc_port is None:
            return None
        credentials = self._desktop_credentials(worker_id)
        query = urlencode(
            {
                **({"password": credentials["vnc_password"]} if not self.vnc_no_password else {}),
                "autoconnect": "1",
                "resize": "scale",
                "reconnect": "1",
                "show_dot": "1",
            }
        )
        return f"http://127.0.0.1:{sandbox.novnc_port}/?{query}"

    def _desktop_view_health(self, worker_id: str, sandbox: SandboxInfo | None) -> dict[str, object]:
        if sandbox is None or sandbox.state != "running" or sandbox.novnc_port is None:
            return {"healthy": False, "reason": "desktop_not_running"}
        if sandbox.desktop_auth_ready is False or self._sandbox_needs_desktop_auth_recreate(worker_id, sandbox):
            return {"healthy": False, "reason": "desktop_authentication_required"}
        now = time.monotonic()
        cached = self._novnc_health_cache.get(worker_id)
        if cached and cached[0] > now:
            return cached[1]
        healthy = self._novnc_http_ready(sandbox.novnc_port)
        repaired = False
        if not healthy and self.novnc_self_heal:
            repaired = self._repair_novnc_proxy(sandbox)
            if repaired:
                time.sleep(0.75)
                healthy = self._novnc_http_ready(sandbox.novnc_port)
        health = {
            "healthy": healthy,
            "repaired": repaired,
            "reason": "ok" if healthy else "novnc_unhealthy",
        }
        self._novnc_health_cache[worker_id] = (now + self.novnc_health_cache_ttl_sec, health)
        return health

    def _novnc_http_ready(self, port: int) -> bool:
        target = f"http://127.0.0.1:{port}/core/rfb.js"
        request = Request(target, headers={"Cache-Control": "no-cache"})
        try:
            with urlopen(request, timeout=self.novnc_health_timeout_sec) as response:
                return 200 <= int(response.status) < 300 and bool(response.read(1))
        except (OSError, URLError, TimeoutError, ValueError):
            return False

    def _repair_novnc_proxy(self, sandbox: SandboxInfo) -> bool:
        script = "\n".join(
            [
                "set +e",
                "supervisorctl stop novnc >/dev/null 2>&1 || true",
                f"listen_port={shlex.quote(str(self.novnc_container_port))}",
                f"vnc_port={shlex.quote(os.environ.get('WPR_SANDBOX_VNC_PORT', '5900'))}",
                "pids=$(ps -eo pid=,args= | awk -v listen=\"$listen_port\" '",
                "  index($0, \"websockify\") && index($0, listen) { print $1; next }",
                "  index($0, \"novnc_proxy\") && index($0, \"--listen \" listen) { print $1; next }",
                "')",
                "for pid in $pids; do [ \"$pid\" = \"$$\" ] || kill \"$pid\" >/dev/null 2>&1 || true; done",
                "sleep 0.3",
                "for pid in $pids; do [ \"$pid\" = \"$$\" ] || kill -KILL \"$pid\" >/dev/null 2>&1 || true; done",
                f"mkdir -p {shlex.quote(self.service_tmp_dir)}",
                (
                    f"TMPDIR={shlex.quote(self.service_tmp_dir)} "
                    "nohup /opt/bin/noVNC/utils/novnc_proxy "
                    "--listen \"$listen_port\" --vnc \"localhost:${vnc_port}\" "
                    ">/tmp/glasshive-novnc-repair.out 2>/tmp/glasshive-novnc-repair.err &"
                ),
            ]
        )
        result = self._docker_exec(
            sandbox.container_name,
            ["bash", "-c", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
            },
            cwd=self.workspace_mount,
            user="root",
        )
        return result.returncode == 0

    def _default_browser_url(self) -> str:
        html = (
            "<!doctype html><html><head><meta charset='utf-8' />"
            "<style>"
            "html,body{height:100%;margin:0;background:#000;color:#e8ebef;"
            "font-family:system-ui,-apple-system,sans-serif}"
            "body{display:grid;place-items:center}"
            ".wrap{max-width:540px;padding:24px;text-align:center}"
            "h1{font-size:clamp(28px,4vw,48px);margin:0 0 10px;letter-spacing:-.04em}"
            "p{margin:0;color:rgba(232,235,239,.74);font-size:16px;line-height:1.5}"
            "</style></head><body><div class='wrap'>"
            "<h1>GlassHive</h1>"
            "<p>Your worker is preparing the result. This view will become the delivered page when it is ready.</p>"
            "</div></body></html>"
        )
        return f"data:text/html,{quote(html)}"

    def _browser_tmp_dir(self) -> str:
        return f"{self.home_mount}/tmp"

    def _browser_cache_dir(self) -> str:
        return f"{self.home_mount}/.cache"

    def _browser_config_dir(self) -> str:
        return f"{self.home_mount}/.config"

    def _prepare_chromium_profile_script(self) -> str:
        preferences_path = f"{self._browser_config_dir()}/chromium/Default/Preferences"
        return "\n".join(
            [
                "if command -v glasshive-browser-native-host-bootstrap >/dev/null 2>&1; then glasshive-browser-native-host-bootstrap; fi",
                f"mkdir -p {shlex.quote(str(Path(preferences_path).parent))}",
                "python3 - <<'PY'",
                "import json",
                "from pathlib import Path",
                f"path = Path({preferences_path!r})",
                "try:",
                "    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}",
                "except Exception:",
                "    data = {}",
                "if not isinstance(data, dict):",
                "    data = {}",
                "bookmark_bar = data.setdefault('bookmark_bar', {})",
                "if isinstance(bookmark_bar, dict):",
                "    bookmark_bar['show_on_all_tabs'] = False",
                "browser = data.setdefault('browser', {})",
                "if isinstance(browser, dict):",
                "    browser['show_home_button'] = False",
                "path.write_text(json.dumps(data, sort_keys=True, separators=(',', ':')), encoding='utf-8')",
                "PY",
            ]
        )

    def _chromium_launch_args(self, *, start_maximized: bool = False, new_window: bool = False, new_tab: bool = False) -> list[str]:
        args = [
            self.chromium_binary,
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
        ]
        if start_maximized:
            args.append("--start-maximized")
        if new_window:
            args.append("--new-window")
        if new_tab:
            args.append("--new-tab")
        return args

    def _chromium_launch_command(
        self,
        url: str,
        *,
        start_maximized: bool = False,
        new_window: bool = False,
        new_tab: bool = False,
    ) -> list[str]:
        return [
            *self._chromium_launch_args(
                start_maximized=start_maximized,
                new_window=new_window,
                new_tab=new_tab,
            ),
            url,
        ]

    def _chromium_launch_line(
        self,
        url: str,
        *,
        start_maximized: bool = False,
        new_window: bool = False,
        new_tab: bool = False,
    ) -> str:
        return shlex.join(
            self._chromium_launch_command(
                url,
                start_maximized=start_maximized,
                new_window=new_window,
                new_tab=new_tab,
            )
        )

    def _chromium_launch_script(
        self,
        url: str,
        *,
        start_maximized: bool = False,
        new_window: bool = False,
        new_tab: bool = False,
        replace_shell: bool = True,
    ) -> str:
        launch = self._chromium_launch_line(
            url,
            start_maximized=start_maximized,
            new_window=new_window,
            new_tab=new_tab,
        )
        return "\n".join(
            [
                self._prepare_chromium_profile_script(),
                f"{'exec ' if replace_shell else ''}{launch}",
            ]
        )

    def _default_writable_container_paths(self) -> list[str]:
        return [
            self.workspace_mount,
            self.home_mount,
            self._browser_tmp_dir(),
            self._browser_cache_dir(),
            self._browser_config_dir(),
        ]

    def _desktop_env(self) -> dict[str, str]:
        env = {
            "HOME": self.home_mount,
            "TERM": self.term_value,
            "DISPLAY": self.display_value,
            "TMPDIR": self._browser_tmp_dir(),
            "XDG_CACHE_HOME": self._browser_cache_dir(),
            "XDG_CONFIG_HOME": self._browser_config_dir(),
        }
        for key in (
            "WPR_CODEX_CHROME_PLUGIN_ROOT",
            "CODEX_CHROME_PLUGIN_ROOT",
            "WPR_CODEX_NODE_REPL_PATH",
            "CODEX_NODE_REPL_PATH",
        ):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _container_name(self, worker_id: str) -> str:
        token = worker_id.replace("_", "-").lower()
        return f"wpr-{token}"

    @staticmethod
    def _worker_state_allows_fast_exec(worker: dict | None) -> bool:
        state = str((worker or {}).get("state") or "").strip().lower()
        return state in {"ready", "running", "failed", "cancelled", "interrupted"}

    @staticmethod
    def _worker_state_allows_substrate_recreate(worker: dict | None) -> bool:
        state = str((worker or {}).get("state") or "").strip().lower()
        return state in {"ready", "failed", "cancelled", "interrupted"} or (
            state == "running" and bool((worker or {}).get("_glasshive_task_run"))
        ) or (
            state == "starting"
            and (worker or {}).get("_pre_run_substrate_recreate_allowed") is True
        )

    def _sandbox_needs_chromium_userns_recreate(self, sandbox: SandboxInfo) -> bool:
        if not self._env_flag("WPR_SANDBOX_ALLOW_CHROMIUM_USERNS", True):
            return False
        security_options = getattr(sandbox, "security_options", None)
        if security_options is None:
            return False
        return self.chromium_userns_security_opt not in security_options

    def _network_name_for_container(self, container_name: str) -> str:
        clean = "".join(
            character if character.isalnum() or character in {"-", "_"} else "-"
            for character in str(container_name or "")
        ).strip("-_")
        if not clean:
            raise RuntimeError("Worker sandbox container name is invalid")
        configured = str(os.environ.get("WPR_SANDBOX_ISOLATED_NETWORK") or "").strip()
        if configured:
            if not all(character.isalnum() or character in {"-", "_"} for character in configured):
                raise RuntimeError("Worker sandbox network name is invalid")
            return configured[:63]
        deployment_id = hashlib.sha256(
            str(self.runtime_root.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        # All workstations for one deployment share the same ICC-disabled bridge.
        # Docker's bridge isolation then denies A→B traffic; separate bridges can
        # be routed through the host and do not provide tenant isolation.
        return f"wpr-workers-{deployment_id}"

    def _network_name(self, worker_id: str) -> str:
        return self._network_name_for_container(self._container_name(worker_id))

    def _sandbox_needs_network_recreate(self, worker_id: str, sandbox: SandboxInfo) -> bool:
        networks = tuple(getattr(sandbox, "networks", ()) or ())
        # Older test/compatibility projections did not retain network metadata. Real Docker inspect
        # always reports it; an explicit shared/default network must never be accepted.
        return bool(networks) and networks != (self._network_name(worker_id),)

    def _provider_account_mount(self, worker: dict | None) -> tuple[str, str] | None:
        candidate = worker or {}
        raw_host = str(
            candidate.get("_glasshive_provider_account_mount_host") or ""
        ).strip()
        raw_target = str(
            candidate.get("_glasshive_provider_account_mount_target") or ""
        ).strip()
        if not raw_host and not raw_target:
            return None
        if not candidate.get("_glasshive_provider_account_bound"):
            raise RuntimeError(
                "Provider account mount was not validated by the GlassHive control plane"
            )
        if raw_target != self._provider_account_mount_target:
            raise RuntimeError("Provider account mount target is invalid")
        host = Path(raw_host)
        if not host.is_absolute() or host.is_symlink() or not host.is_dir():
            raise RuntimeError("Provider account mount source is invalid")
        resolved = host.resolve(strict=True)
        if resolved == Path(resolved.anchor):
            raise RuntimeError("Provider account mount source is too broad")
        return str(resolved), raw_target

    def terminate_containers_mounting_provider_account(self, account_home: Path) -> list[str]:
        """Remove stale GlassHive containers that still expose one account home.

        A hard API crash can outlive the durable DB lease while Docker keeps the
        bind mount.  Reuse is allowed only after every matching mount is gone.
        """

        candidate = Path(account_home)
        if not candidate.exists() and not candidate.is_symlink():
            return []
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
            raise RuntimeError("Provider account home is not a safe managed directory")
        resolved_home = str(candidate.resolve(strict=True))
        listed = self._docker(
            ["ps", "-aq", "--filter", "name=^/wpr-"],
            check=False,
            capture_output=True,
            timeout_sec=self.inspect_timeout_sec,
        )
        if listed.returncode != 0:
            raise RuntimeError("GlassHive could not inspect stale provider-account mounts")
        container_ids = [value.strip() for value in str(listed.stdout or "").splitlines() if value.strip()]
        if not container_ids:
            return []
        inspected = self._docker(
            ["inspect", *container_ids],
            check=False,
            capture_output=True,
            timeout_sec=max(self.inspect_timeout_sec, 10),
        )
        if inspected.returncode != 0:
            raise RuntimeError("GlassHive could not inspect stale provider-account containers")
        try:
            payload = json.loads(inspected.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("GlassHive provider-account mount inspection returned invalid data") from exc
        removed: list[str] = []
        for entry in payload if isinstance(payload, list) else []:
            mounts = entry.get("Mounts") if isinstance(entry, dict) else None
            if not isinstance(mounts, list) or not any(
                str(mount.get("Source") or "") == resolved_home
                and str(mount.get("Destination") or "") == self._provider_account_mount_target
                for mount in mounts
                if isinstance(mount, dict)
            ):
                continue
            container_name = str(entry.get("Name") or "").lstrip("/")
            container_id = str(entry.get("Id") or "").strip()
            if not container_name.startswith("wpr-") or not container_id:
                raise RuntimeError("GlassHive found an unrecognized provider-account mount owner")
            removed_result = self._docker(
                ["rm", "-f", container_id],
                check=False,
                capture_output=True,
                timeout_sec=max(self.inspect_timeout_sec, 10),
            )
            if removed_result.returncode != 0:
                raise RuntimeError("GlassHive could not remove a stale provider-account mount")
            self._remove_isolated_network(container_name)
            removed.append(container_name)
        return removed

    def repair_provider_account_access(self, account_home: Path) -> None:
        """Normalize a private account tree through the rootless Docker user namespace."""

        candidate = Path(account_home)
        if not candidate.exists() and not candidate.is_symlink():
            return
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
            raise RuntimeError("Provider account home is not a safe managed directory")
        resolved = candidate.resolve(strict=True)
        if resolved == Path(resolved.anchor):
            raise RuntimeError("Provider account home is too broad")
        self._ensure_image()
        mount_target = self._provider_account_mount_target
        result = self._docker(
            [
                "run", "--rm", "--network", "none", "--read-only",
                "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
                "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
                "--pids-limit", "64", "--memory", "128m", "--cpus", "0.25",
                "--user", "0:0", "--mount",
                # A bind mount is writable by default. Docker's --mount parser
                # accepts explicit key=value options, but a bare `rw` field is
                # rejected before the repair container starts.
                f"type=bind,src={resolved},dst={mount_target}",
                "--entrypoint", "bash", self.image, "-c",
                _provider_account_seal_script(mount_target),
            ],
            check=False,
            capture_output=True,
            timeout_sec=max(self.inspect_timeout_sec, 30),
        )
        if result.returncode != 0:
            raise RuntimeError("GlassHive could not safely normalize provider-account credentials")

    def _sandbox_needs_provider_mount_recreate(
        self, worker: dict | None, sandbox: SandboxInfo
    ) -> bool:
        expected = self._provider_account_mount(worker)
        actual = tuple(
            mount
            for mount in (getattr(sandbox, "mounts", ()) or ())
            if mount[1] == self._provider_account_mount_target
        )
        return actual != (() if expected is None else (expected,))

    @staticmethod
    def _require_private_codex_overlay_dir(private_dir: Path) -> None:
        if os.path.lexists(private_dir):
            metadata = os.lstat(private_dir)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise RuntimeError("Workspace private authentication state is unsafe")
            return
        private_dir.mkdir(mode=0o700)
        if os.name != "nt":
            private_dir.chmod(0o700)

    def _prepare_workspace_codex_auth_overlay(
        self,
        home_dir: Path,
        private_dir: Path,
    ) -> None:
        """Hide any workspace login outside all mission container mounts."""

        codex_home = home_dir / ".codex"
        if not os.path.lexists(codex_home):
            return
        if codex_home.is_symlink() or not codex_home.is_dir():
            raise RuntimeError("Workspace Codex home is not a safe managed directory")
        self._require_private_codex_overlay_dir(private_dir)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        codex_home_fd = os.open(codex_home, directory_flags)
        private_fd = os.open(private_dir, directory_flags)
        try:
            backup_name = "codex-workspace-auth.json"
            try:
                auth = os.stat("auth.json", dir_fd=codex_home_fd, follow_symlinks=False)
            except FileNotFoundError:
                auth = None
            try:
                backup = os.stat(backup_name, dir_fd=private_fd, follow_symlinks=False)
            except FileNotFoundError:
                backup = None
            if auth is not None and stat.S_ISREG(auth.st_mode):
                mode = stat.S_IMODE(auth.st_mode)
                if (
                    auth.st_nlink != 1
                    or auth.st_size <= 0
                    or auth.st_size > 16 * 1024 * 1024
                    or mode & 0o077
                    or backup is not None
                ):
                    raise RuntimeError("Workspace Codex authentication cannot be safely preserved")
                os.rename(
                    "auth.json",
                    backup_name,
                    src_dir_fd=codex_home_fd,
                    dst_dir_fd=private_fd,
                )
                os.fsync(codex_home_fd)
                os.fsync(private_fd)
            elif auth is not None:
                expected_target = "/workspace/.provider-account/codex/auth.json"
                if (
                    not stat.S_ISLNK(auth.st_mode)
                    or os.readlink("auth.json", dir_fd=codex_home_fd) != expected_target
                ):
                    raise RuntimeError("Workspace Codex authentication has an unsafe type")
            if backup is not None:
                mode = stat.S_IMODE(backup.st_mode)
                if (
                    not stat.S_ISREG(backup.st_mode)
                    or backup.st_nlink != 1
                    or backup.st_size <= 0
                    or backup.st_size > 16 * 1024 * 1024
                    or mode & 0o077
                ):
                    raise RuntimeError("Workspace Codex authentication backup is unsafe")
        finally:
            os.close(private_fd)
            os.close(codex_home_fd)

    def _restore_workspace_codex_auth(
        self,
        home_dir: Path,
        private_dir: Path,
        *,
        expected_target: str = "/workspace/.provider-account/codex/auth.json",
    ) -> None:
        """Remove the mission overlay and restore any workspace-local Codex login."""

        codex_home = home_dir / ".codex"
        if not os.path.lexists(codex_home):
            return
        if codex_home.is_symlink() or not codex_home.is_dir():
            raise RuntimeError("Workspace Codex home is not a safe managed directory")
        if not os.path.lexists(private_dir):
            private_dir.mkdir(mode=0o700)
            if os.name != "nt":
                private_dir.chmod(0o700)
        self._require_private_codex_overlay_dir(private_dir)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        codex_home_fd = os.open(codex_home, directory_flags)
        private_fd = os.open(private_dir, directory_flags)
        try:
            backup_name = "codex-workspace-auth.json"
            try:
                auth = os.stat("auth.json", dir_fd=codex_home_fd, follow_symlinks=False)
            except FileNotFoundError:
                auth = None
            if auth is not None and stat.S_ISLNK(auth.st_mode):
                if os.readlink("auth.json", dir_fd=codex_home_fd) != expected_target:
                    raise RuntimeError("Workspace Codex auth link selects an unexpected account path")
                os.unlink("auth.json", dir_fd=codex_home_fd)
                auth = None
            elif auth is not None and not stat.S_ISREG(auth.st_mode):
                raise RuntimeError("Workspace Codex authentication has an unsafe type")
            try:
                backup = os.stat(backup_name, dir_fd=private_fd, follow_symlinks=False)
            except FileNotFoundError:
                backup = None
            if backup is not None:
                if auth is not None:
                    raise RuntimeError("Workspace Codex authentication restore is ambiguous")
                mode = stat.S_IMODE(backup.st_mode)
                if (
                    not stat.S_ISREG(backup.st_mode)
                    or backup.st_nlink != 1
                    or backup.st_size <= 0
                    or backup.st_size > 16 * 1024 * 1024
                    or mode & 0o077
                ):
                    raise RuntimeError("Workspace Codex authentication backup is unsafe")
                os.rename(
                    backup_name,
                    "auth.json",
                    src_dir_fd=private_fd,
                    dst_dir_fd=codex_home_fd,
                )
            os.fsync(codex_home_fd)
            os.fsync(private_fd)
        finally:
            os.close(private_fd)
            os.close(codex_home_fd)

    def _grant_provider_account_access(
        self,
        container_name: str,
        worker: dict,
    ) -> None:
        """Give only this rootless container's worker user access to the selected home."""

        mount = self._provider_account_mount(worker)
        if mount is None:
            return
        mount_target = Path(mount[1])
        raw_environment = worker.get("_glasshive_provider_account_env")
        if not isinstance(raw_environment, dict) or not raw_environment:
            raise RuntimeError("Provider account mount is missing its private runtime home")
        is_codex_home = set(raw_environment) == {"CODEX_HOME"}
        is_claude_home = set(raw_environment) == {"CLAUDE_SECURESTORAGE_CONFIG_DIR"}
        is_grok_home = set(raw_environment) == {"GROK_AUTH_PATH"}
        if not is_codex_home and not is_claude_home and not is_grok_home:
            raise RuntimeError("Provider account mount contains invalid runtime selectors")
        runtime_env_name = "CODEX_HOME" if is_codex_home else "GROK_AUTH_PATH" if is_grok_home else "CLAUDE_SECURESTORAGE_CONFIG_DIR"
        runtime_home_value = raw_environment[runtime_env_name]
        runtime_home = Path(str(runtime_home_value or "").strip())
        quoted_mount = shlex.quote(str(mount_target))
        quoted_home = shlex.quote(str(runtime_home))
        container_user_name = self.user.split(":", 1)[0] or self.user
        container_user = shlex.quote(container_user_name)
        if is_codex_home:
            expected_codex_home = Path(self.home_mount) / ".codex"
            if runtime_home != expected_codex_home:
                raise RuntimeError("Codex must use its exact persistent workspace home")
            # Keep account credentials in the selected provider home while all
            # plugin, MCP, connector, and session state stays in the reusable
            # workspace home. Codex's file auth backend opens/truncates auth.json,
            # so the exact live symlink also carries safe token refreshes back to
            # the account store without copying credential bytes.
            prepared = self._docker_exec(
                container_name,
                [
                    "python3",
                    "-c",
                    _CODEX_PROVIDER_AUTH_LINK_SCRIPT,
                    str(mount_target),
                    "codex",
                    self.home_mount,
                    self.user,
                ],
                cwd=self.workspace_mount,
                user="root",
            )
            if prepared.returncode != 0:
                raise RuntimeError(
                    "The selected Codex account or workspace contains unsafe authentication state"
                )
            quoted_provider_home = shlex.quote(str(mount_target / "codex"))
            quoted_auth = shlex.quote(str(mount_target / "codex" / "auth.json"))
            grant_script = (
                "set -e; "
                "command -v setfacl >/dev/null 2>&1; "
                f"test -d {quoted_mount}; test -d {quoted_provider_home}; test -f {quoted_auth}; "
                f"setfacl -m u:{container_user}:x {quoted_mount}; "
                f"setfacl -m u:{container_user}:x {quoted_provider_home}; "
                f"setfacl -m u:{container_user}:rw {quoted_auth}"
            )
        elif is_grok_home:
            if runtime_home != mount_target / "grok" / "auth.json":
                raise RuntimeError("Grok must use the exact selected account auth file")
            quoted_provider_home = shlex.quote(str(runtime_home.parent))
            # Native token refresh atomically replaces auth.json. Only the
            # selected provider directory receives create/rename permission.
            grant_script = (
                "set -e; command -v setfacl >/dev/null 2>&1; "
                f"test -d {quoted_mount}; test -d {quoted_provider_home}; "
                f"test ! -L {quoted_provider_home}; test -f {quoted_home}; test ! -L {quoted_home}; "
                f"setfacl -m u:{container_user}:x {quoted_mount}; "
                f"setfacl -m u:{container_user}:rwx,d:u:{container_user}:rwx {quoted_provider_home}; "
                f"setfacl -m u:{container_user}:rw {quoted_home}"
            )
        else:
            try:
                runtime_home.relative_to(mount_target)
            except ValueError as exc:
                raise RuntimeError("Provider account runtime home is outside its private mount") from exc
            if runtime_home == mount_target:
                raise RuntimeError("Provider account runtime home must select one provider directory")
            prepared = self._docker_exec(
                container_name,
                ["python3", "-c", _CLAUDE_WORKSPACE_ONBOARDING_SCRIPT, self.home_mount],
                cwd=self.workspace_mount,
                user=self.user,
            )
            if prepared.returncode != 0:
                raise RuntimeError("The Claude workspace contains unsafe native configuration state")
            grant_script = (
                "set -e; "
                "command -v setfacl >/dev/null 2>&1; "
                "claude_path=$(readlink -f \"$(command -v claude)\"); "
                "grep -Fq CLAUDE_SECURESTORAGE_CONFIG_DIR \"$claude_path\"; "
                f"test -d {quoted_mount}; test -d {quoted_home}; "
                f"setfacl -R -m u:{container_user}:rwX {quoted_mount}; "
                f"find {quoted_mount} -type d -exec setfacl -m d:u:{container_user}:rwX {{}} +"
            )
        granted = self._docker_exec(
            container_name,
            ["bash", "-c", grant_script],
            cwd=self.workspace_mount,
            user="root",
        )
        if granted.returncode != 0:
            raise RuntimeError(
                "Provider account isolation requires POSIX ACL support in the reviewed worker image"
            )

        verify_script = (
            f"set -e; test -r {quoted_home}; test -w {quoted_home}; test -x {quoted_home}"
        )
        if is_grok_home:
            verify_script = (
                f"set -e; test -r {quoted_home}; test -w {quoted_home}; "
                f"test -w {quoted_provider_home}; test -x {quoted_provider_home}"
            )
        if is_codex_home:
            quoted_auth_link = shlex.quote(str(runtime_home / "auth.json"))
            quoted_auth_target = shlex.quote(str(mount_target / "codex" / "auth.json"))
            verify_script += (
                f"; test -L {quoted_auth_link}; "
                f"test \"$(readlink {quoted_auth_link})\" = {quoted_auth_target}; "
                f"test -r {quoted_auth_link}; test -w {quoted_auth_link}"
            )
        verified = self._docker_exec(
            container_name,
            ["bash", "-c", verify_script],
            cwd=self.workspace_mount,
            user=self.user,
        )
        if verified.returncode != 0:
            raise RuntimeError(
                "The selected provider account home is not accessible to its isolated worker"
            )

    def _ensure_isolated_network(self, container_name: str) -> str:
        network_name = self._network_name_for_container(container_name)
        inspected = self._docker(
            ["network", "inspect", network_name],
            check=False,
            capture_output=True,
            timeout_sec=self.inspect_timeout_sec,
        )
        if inspected.returncode == 0:
            try:
                payload = json.loads(inspected.stdout or "[]")
                entry = payload[0]
            except (json.JSONDecodeError, IndexError, TypeError):
                entry = None
            if entry is not None:
                options = entry.get("Options") if isinstance(entry, dict) else None
                if not isinstance(options, dict) or str(
                    options.get("com.docker.network.bridge.enable_icc") or ""
                ).lower() != "false":
                    raise RuntimeError("Worker sandbox network does not disable inter-container communication")
                return network_name
        created = self._docker(
            [
                "network",
                "create",
                "--driver",
                "bridge",
                "--opt",
                "com.docker.network.bridge.enable_icc=false",
                "--label",
                "com.glasshive.isolated-worker-network=true",
                network_name,
            ],
            check=False,
            capture_output=True,
            timeout_sec=self.inspect_timeout_sec,
        )
        if created.returncode != 0:
            raise RuntimeError(
                f"Failed to create isolated worker network {network_name}: "
                f"{(created.stderr or created.stdout or '').strip()[-1000:]}"
            )
        return network_name

    def _remove_isolated_network(self, container_name: str) -> None:
        self._docker(
            ["network", "rm", self._network_name_for_container(container_name)],
            check=False,
            capture_output=True,
            timeout_sec=self.inspect_timeout_sec,
        )

    def fast_sandbox_from_worker(self, worker: dict | None) -> SandboxInfo | None:
        if not worker or not self._worker_state_allows_fast_exec(worker):
            return None
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            return None
        # State/workspace directories are projected before container startup so
        # operators can inspect paths early. They are not evidence that Docker
        # has created the workstation. Only use the shortcut when the caller has
        # real container evidence, then validate it through inspect/cache.
        if not str(worker.get("container_id") or "").strip():
            return None
        sandbox = self.inspect(worker_id)
        if (
            sandbox is not None
            and (
                self._sandbox_needs_chromium_userns_recreate(sandbox)
                or self._sandbox_needs_network_recreate(worker_id, sandbox)
                or self._sandbox_needs_provider_mount_recreate(worker, sandbox)
            )
        ):
            return None
        return sandbox

    def paths(self, worker_id: str) -> dict[str, Path]:
        worker_root = self.runtime_root / "workers" / worker_id
        state_dir = worker_root / "state"
        workspace_dir = state_dir / "workspace"
        home_dir = state_dir / "home"
        return {
            "worker_root": worker_root,
            "state_dir": state_dir,
            "workspace_dir": workspace_dir,
            "home_dir": home_dir,
        }

    def _paths(self, worker_id: str) -> dict[str, Path]:
        return self.paths(worker_id)

    def _ensure_host_dirs(self, paths: dict[str, Path]) -> None:
        paths["workspace_dir"].mkdir(parents=True, exist_ok=True)
        paths["home_dir"].mkdir(parents=True, exist_ok=True)
        # Selenium's password-enabled VNC bootstrap writes its credential file
        # below $HOME/.vnc before the interactive worker starts.  The host home
        # bind masks the image's home, so create this private directory here.
        vnc_dir = paths["home_dir"] / ".vnc"
        vnc_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            if self._private_acl_required():
                for key in ("worker_root", "state_dir", "workspace_dir", "home_dir"):
                    paths[key].chmod(0o700)
            vnc_dir.chmod(0o700)

    def _seed_bootstrap(
        self,
        home_dir: Path,
        workspace_dir: Path,
        runtime_name: str,
        worker: dict,
        *,
        trusted_state_dir: Path | None = None,
    ) -> None:
        apply_bootstrap(
            home_dir=home_dir,
            workspace_dir=workspace_dir,
            runtime_name=runtime_name,
            worker=worker,
            copy_file=self._copy_file,
            copy_tree=self._copy_tree,
            trusted_state_dir=trusted_state_dir,
        )

    def _copy_file(self, src: Path, dest: Path) -> None:
        if not src.exists() or os.path.lexists(dest):
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def _copy_tree(self, src: Path, dest: Path) -> None:
        if not src.exists() or dest.exists():
            return
        shutil.copytree(src, dest, dirs_exist_ok=True)

    def _require_docker(self) -> None:
        if shutil.which("docker") is None:
            raise RuntimeError("Docker CLI is required for sandboxed workers but was not found on PATH")

    def prepare_image(self) -> None:
        """Ensure the reviewed workstation image exists before user traffic begins."""

        self._require_docker()
        self._ensure_image()

    def _ensure_image(self) -> None:
        now = time.monotonic()
        if self._image_checked_at and self._image_checked_at + self.image_check_ttl_sec > now:
            return
        inspected = self._docker(
            ["image", "inspect", self.image],
            check=False,
            capture_output=True,
            timeout_sec=self.image_inspect_timeout_sec,
        )
        if inspected.returncode == 0 and self._image_has_reviewed_provenance(inspected.stdout):
            self._image_checked_at = now
            return
        if inspected.returncode == 0 and not self._managed_image:
            raise RuntimeError(
                "Configured worker image is missing the reviewed GlassHive provenance labels"
            )
        with self._build_lock:
            now = time.monotonic()
            if self._image_checked_at and self._image_checked_at + self.image_check_ttl_sec > now:
                return
            inspected = self._docker(
                ["image", "inspect", self.image],
                check=False,
                capture_output=True,
                timeout_sec=self.image_inspect_timeout_sec,
            )
            if inspected.returncode == 0 and self._image_has_reviewed_provenance(inspected.stdout):
                self._image_checked_at = now
                return
            if inspected.returncode == 0 and not self._managed_image:
                raise RuntimeError(
                    "Configured worker image is missing the reviewed GlassHive provenance labels"
                )
            if "@sha256:" not in self.base_image:
                raise RuntimeError(
                    "GlassHive worker base image must be pinned by immutable digest"
                )
            dockerfile = self.build_root / "Dockerfile"
            stage_reviewed_openclaw_lock(self.build_root / "openclaw-runtime-lock")
            _stage_reviewed_file(
                AI_WORKER_PYTHON_LOCK_PATH,
                self.build_root / "workstation-requirements.lock",
            )
            extension_policy = _ai_worker_browser_extension_policy_json()
            extension_policy_source = AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS[0]
            extension_policy_dirs = " ".join(
                shlex.quote(str(Path(path).parent))
                for path in AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS
            )
            extension_policy_writes = " && ".join(
                [
                    f"printf '%s\\n' {shlex.quote(extension_policy)} > {shlex.quote(extension_policy_source)}",
                    *(
                        f"cp {shlex.quote(extension_policy_source)} {shlex.quote(path)}"
                        for path in AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS[1:]
                    ),
                ]
            )
            extension_check_script_lines = " ".join(
                shlex.quote(line)
                for line in _ai_worker_browser_extension_check_script().splitlines()
            )
            native_host_bootstrap_script_lines = " ".join(
                shlex.quote(line)
                for line in _ai_worker_browser_native_host_bootstrap_script().splitlines()
            )
            npm_worker_specs = " ".join(
                shlex.quote(spec)
                for spec in (
                    AI_WORKER_CODEX_NPM_SPEC,
                    AI_WORKER_CLAUDE_CODE_NPM_SPEC,
                )
            )
            dockerfile.write_text(
                "\n".join(
                    [
                        f"FROM {self.base_image}",
                        "LABEL com.glasshive.workstation.provenance=reviewed-v1",
                        f"LABEL com.glasshive.workstation.base-image={self.base_image}",
                        f"LABEL com.glasshive.workstation.apt-snapshot={AI_WORKER_APT_SNAPSHOT}",
                        f"LABEL com.glasshive.workstation.codex-spec={AI_WORKER_CODEX_NPM_SPEC}",
                        f"LABEL com.glasshive.workstation.claude-spec={AI_WORKER_CLAUDE_CODE_NPM_SPEC}",
                        f"LABEL com.glasshive.workstation.grok-artifacts={GROK_ARTIFACT_PROVENANCE}",
                        f"LABEL com.glasshive.workstation.openai-plugins-commit={OPENAI_PLUGIN_MARKETPLACE_COMMIT}",
                        f"LABEL com.glasshive.workstation.openclaw-version={OPENCLAW_RUNTIME_VERSION}",
                        f"LABEL com.glasshive.workstation.openclaw-lock-sha256={OPENCLAW_RUNTIME_LOCK_SHA256}",
                        f"LABEL com.glasshive.workstation.python-lock-sha256={AI_WORKER_PYTHON_LOCK_SHA256}",
                        "LABEL com.glasshive.workstation.provider-account-acl=required-v1",
                        "USER root",
                        f"RUN find /etc/apt -type f -name '*.sources' -exec sed -ri 's#https?://(archive|security).ubuntu.com/ubuntu/?#https://snapshot.ubuntu.com/ubuntu/{AI_WORKER_APT_SNAPSHOT}/#g' {{}} +",
                        "RUN apt-get update && apt-get install -y --no-install-recommends acl bash ca-certificates curl file fonts-dejavu git gnupg jq less libreoffice-calc libreoffice-impress libreoffice-writer nano openssh-client pandoc pcmanfm poppler-utils procps python-is-python3 python3-pip ripgrep screen tmux tree vim wmctrl x11-utils xdotool xterm && rm -rf /var/lib/apt/lists/*",
                        "RUN if [ ! -x /usr/bin/locale-check ]; then printf '%s\\n' '#!/bin/sh' 'locale_value=${1:-C.UTF-8}' 'echo LANG=$locale_value' 'echo LC_ALL=$locale_value' > /usr/bin/locale-check && chmod +x /usr/bin/locale-check; fi",
                        "RUN arch=$(dpkg --print-architecture) && case \"$arch\" in amd64) node_sha=eed0c5f0ab411f28783f81f051fcf7928ae8bf833e2df11f48f3fa78270025cb ;; arm64) node_sha=0018ee0bf997fce587042a2382941dcfac9f404fb994a5c3267fe7d12d8d837b ;; *) echo \"unsupported architecture: $arch\" >&2; exit 1 ;; esac && curl -fsSL \"https://deb.nodesource.com/node_22.x/pool/main/n/nodejs/nodejs_22.23.2-1nodesource1_${arch}.deb\" -o /tmp/nodejs.deb && echo \"${node_sha}  /tmp/nodejs.deb\" | sha256sum -c - && apt-get update && apt-get install -y --no-install-recommends /tmp/nodejs.deb && rm -f /tmp/nodejs.deb && node --version && npm --version && rm -rf /var/lib/apt/lists/*",
                        grok_docker_install_instruction(),
                        f"RUN npm install -g --cache /tmp/glasshive-npm-cache {npm_worker_specs} && npm cache clean --force --cache /tmp/glasshive-npm-cache && rm -rf /tmp/glasshive-npm-cache /root/.npm /home/seluser/.npm",
                        (
                            f"RUN git init {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)} "
                            f"&& git -C {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)} remote add origin {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_ORIGIN)} "
                            f"&& git -C {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)} fetch --depth 1 origin {OPENAI_PLUGIN_MARKETPLACE_COMMIT} "
                            f"&& git -C {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)} checkout --detach FETCH_HEAD "
                            f"&& test \"$(git -C {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)} rev-parse HEAD)\" = {OPENAI_PLUGIN_MARKETPLACE_COMMIT} "
                            f"&& test \"$(git -C {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)} remote get-url origin)\" = {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_ORIGIN)} "
                            f"&& test -z \"$(git -C {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)} status --porcelain)\" "
                            f"&& test \"$(jq -r .name {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)}/.agents/plugins/marketplace.json)\" = openai-curated "
                            f"&& git config --system --add safe.directory {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)} "
                            f"&& chmod -R a-w {shlex.quote(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)}"
                        ),
                        "COPY openclaw-runtime-lock/ /opt/glasshive-openclaw-runtime/",
                        (
                            "RUN cd /opt/glasshive-openclaw-runtime "
                            "&& npm ci --omit=dev --cache /tmp/glasshive-openclaw-npm-cache "
                            "&& node -e \"const v=require('./node_modules/openclaw/package.json').version;"
                            f"if(v!=='{OPENCLAW_RUNTIME_VERSION}')throw new Error('OpenClaw version drift: '+v)\" "
                            "&& node -e \"const v=require('./node_modules/fast-uri/package.json').version;"
                            f"if(v!=='{OPENCLAW_RUNTIME_FAST_URI_VERSION}')throw new Error('fast-uri override drift: '+v)\" "
                            "&& ln -sf /opt/glasshive-openclaw-runtime/node_modules/.bin/openclaw /usr/local/bin/openclaw "
                            "&& openclaw --version | "
                            f"grep -Fq 'OpenClaw {OPENCLAW_RUNTIME_VERSION} (' "
                            "&& npm cache clean --force --cache /tmp/glasshive-openclaw-npm-cache "
                            "&& rm -rf /tmp/glasshive-openclaw-npm-cache /root/.npm /home/seluser/.npm"
                        ),
                        "COPY workstation-requirements.lock /opt/glasshive-workstation-requirements.lock",
                        "RUN pip3 install --no-cache-dir --require-hashes --no-deps -r /opt/glasshive-workstation-requirements.lock",
                        f"RUN mkdir -p {extension_policy_dirs} && {extension_policy_writes}",
                        f"RUN printf '%s\\n' {extension_check_script_lines} > /usr/local/bin/glasshive-browser-extension-check && chmod +x /usr/local/bin/glasshive-browser-extension-check && glasshive-browser-extension-check",
                        f"RUN printf '%s\\n' {native_host_bootstrap_script_lines} > /usr/local/bin/glasshive-browser-native-host-bootstrap && chmod +x /usr/local/bin/glasshive-browser-native-host-bootstrap",
                        "RUN mkdir -p /workspace/project /workspace/.wpr-home",
                        "USER seluser",
                        "WORKDIR /workspace/project",
                        "ENV SHELL=/bin/bash",
                        "ENV DISPLAY=:99.0",
                        "ENV TERM=xterm-256color",
                        "ENV OPENCLAW_DISABLE_BONJOUR=1",
                        "",
                    ]
                )
            )
            result = self._docker(
                ["build", "-t", self.image, str(self.build_root)],
                check=False,
                capture_output=True,
                timeout_sec=self.image_build_timeout_sec,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to build sandbox image {self.image}: {(result.stderr or result.stdout or '').strip()[-2000:]}")
            self._image_checked_at = time.monotonic()

    def _expected_image_provenance(self) -> dict[str, str]:
        return {
            "com.glasshive.workstation.provenance": "reviewed-v1",
            "com.glasshive.workstation.base-image": self.base_image,
            "com.glasshive.workstation.apt-snapshot": AI_WORKER_APT_SNAPSHOT,
            "com.glasshive.workstation.codex-spec": AI_WORKER_CODEX_NPM_SPEC,
            "com.glasshive.workstation.claude-spec": AI_WORKER_CLAUDE_CODE_NPM_SPEC,
            "com.glasshive.workstation.grok-artifacts": GROK_ARTIFACT_PROVENANCE,
            "com.glasshive.workstation.openai-plugins-commit": OPENAI_PLUGIN_MARKETPLACE_COMMIT,
            "com.glasshive.workstation.openclaw-version": OPENCLAW_RUNTIME_VERSION,
            "com.glasshive.workstation.openclaw-lock-sha256": OPENCLAW_RUNTIME_LOCK_SHA256,
            "com.glasshive.workstation.python-lock-sha256": AI_WORKER_PYTHON_LOCK_SHA256,
            "com.glasshive.workstation.provider-account-acl": "required-v1",
        }

    def _image_has_reviewed_provenance(self, raw_inspect: str) -> bool:
        try:
            payload = json.loads(raw_inspect or "[]")
            labels = payload[0]["Config"]["Labels"]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            return False
        if not isinstance(labels, dict):
            return False
        return all(
            str(labels.get(key) or "") == value
            for key, value in self._expected_image_provenance().items()
        )

    def _create_container(
        self,
        container_name: str,
        paths: dict[str, Path | int],
        *,
        execution_policy: str = "",
        worker: dict | None = None,
        resource_memory_bytes: int | None = None,
    ) -> None:
        if resource_memory_bytes is None and isinstance(worker, dict):
            resource_memory_bytes = self._resource_memory_bytes_from_worker(worker)
        if resource_memory_bytes is None:
            raw_resource_memory = paths.get(_RESOURCE_MEMORY_BYTES_PATH_KEY)
            if raw_resource_memory is not None:
                resource_memory_bytes = int(raw_resource_memory)
        clean_room = execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        if execution_policy and not clean_room:
            raise RuntimeError(
                f"Unsupported Docker execution policy: {execution_policy}"
            )
        if clean_room and (
            self.user != "seluser" or _docker_user_is_root(self.user)
        ):
            raise RuntimeError(
                "Parallel clean-room execution requires the canonical "
                "non-root seluser"
            )
        clean_room_configuration: dict[str, str] | None = None
        if clean_room:
            clean_room_configuration, reason = (
                self._parallel_clean_room_configuration(
                    require_proxy_containers=False
                )
            )
            if clean_room_configuration is None:
                if reason == "parallel_clean_room_network_unconfigured":
                    raise RuntimeError(
                        "Parallel clean-room execution requires a dedicated internal network"
                    )
                raise RuntimeError(
                    "Parallel clean-room execution requires a dedicated provider proxy"
                )
            clean_room_configuration = {
                **clean_room_configuration,
                "mission_network": self._parallel_clean_room_mission_network_name(
                    container_name
                ),
            }
        network_name = None if clean_room else self._ensure_isolated_network(container_name)
        provider_mount = None if clean_room else self._provider_account_mount(worker)
        desktop_credentials: dict[str, str] | None = None
        secret_env_path: Path | None = None
        if not clean_room:
            worker_id = str((worker or {}).get("worker_id") or container_name).strip()
            desktop_credentials = self._desktop_credentials(worker_id, paths=paths)  # type: ignore[arg-type]
            secret_dir = paths.get("state_dir") or paths["home_dir"].parent  # type: ignore[union-attr]
            secret_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            secret_env_path = secret_dir / f".desktop-env-{secrets.token_hex(12)}"  # type: ignore[operator]
            descriptor = os.open(
                secret_env_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
                secret_file.write(
                    f"SE_VNC_PASSWORD={desktop_credentials['vnc_password']}\n"
                )
                secret_file.write(
                    f"SE_ROUTER_PASSWORD={desktop_credentials['router_password']}\n"
                )
            if os.name != "nt":
                secret_env_path.chmod(0o600)
        command = [
            *(["create"] if clean_room else ["run", "-d"]),
            "--init",
            "--name",
            container_name,
            "--hostname",
            container_name,
            "--workdir",
            self.workspace_mount,
            "-e",
            f"HOME={self.home_mount}",
            "-e",
            f"TERM={self.term_value}",
            "-e",
            f"TMPDIR={self.service_tmp_dir}",
            "-e",
            f"XDG_CACHE_HOME={self._browser_cache_dir()}",
            "-e",
            f"XDG_CONFIG_HOME={self._browser_config_dir()}",
            "-e",
            f"SE_VNC_NO_PASSWORD={'1' if self.vnc_no_password else '0'}",
            *(
                [
                    "-e",
                    f"SE_ROUTER_USERNAME={desktop_credentials['router_username']}",
                    "-e",
                    "SE_MASK_SECRETS=true",
                    "--env-file",
                    str(secret_env_path),
                ]
                if not clean_room
                and desktop_credentials is not None
                and secret_env_path is not None
                else []
            ),
            *(
                [
                    "-e",
                    f"HTTP_PROXY={clean_room_configuration['provider_proxy_url']}",
                    "-e",
                    f"HTTPS_PROXY={clean_room_configuration['provider_proxy_url']}",
                    "-e",
                    (
                        "NO_PROXY="
                        f"{clean_room_configuration['provider_proxy_hostname']},"
                        f"{PARALLEL_CLEAN_ROOM_BROKER_ALIAS},localhost,127.0.0.1"
                    ),
                    "--user",
                    self.user,
                    "--ipc=private",
                    "--cgroupns=private",
                    "--network",
                    clean_room_configuration["mission_network"],
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--read-only",
                    "--label",
                    (
                        f"{PARALLEL_CLEAN_ROOM_POLICY_LABEL}="
                        f"{PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}"
                    ),
                    *[
                        item
                        for tmpfs in PARALLEL_CLEAN_ROOM_TMPFS
                        for item in ("--tmpfs", tmpfs)
                    ],
                ]
                if clean_room and clean_room_configuration is not None
                else [
                    "--network",
                    str(network_name),
                    *self._host_gateway_args(),
                    *self._chromium_sandbox_args(),
                ]
            ),
            *(
                []
                if clean_room
                else [
                    "-p",
                    f"127.0.0.1::{self.novnc_container_port}",
                    "-p",
                    f"127.0.0.1::{self.selenium_container_port}",
                    "-p",
                    f"127.0.0.1::{self.openclaw_container_port}",
                ]
            ),
            "--shm-size",
            os.environ.get("WPR_SANDBOX_SHM_SIZE", "1g"),
            *(
                [
                    "--mount",
                    (
                        f"type=bind,src={paths['workspace_dir']},"
                        f"dst={self.workspace_mount},bind-propagation=rprivate"
                    ),
                    "--mount",
                    (
                        f"type=bind,src={paths['home_dir']},"
                        f"dst={self.home_mount},bind-propagation=rprivate"
                    ),
                ]
                if clean_room
                else [
                    "-v",
                    f"{paths['workspace_dir']}:{self.workspace_mount}",
                    "-v",
                    f"{paths['home_dir']}:{self.home_mount}",
                ]
            ),
            self.image,
        ]
        try:
            if provider_mount is not None:
                command[-1:-1] = ["-v", f"{provider_mount[0]}:{provider_mount[1]}"]
            self._insert_resource_limits(
                command,
                resource_memory_bytes=resource_memory_bytes,
            )
            result = self._docker(command, check=False, capture_output=True)
        finally:
            if secret_env_path is not None:
                secret_env_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worker sandbox {container_name}: {(result.stderr or result.stdout or '').strip()[-2000:]}")

    @staticmethod
    def _desktop_auth_fingerprint(vnc_password: str, router_username: str, router_password: str) -> str:
        payload = "\0".join((vnc_password, router_username, router_password)).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _desktop_credentials(
        self,
        worker_id: str,
        *,
        paths: dict[str, Path] | None = None,
    ) -> dict[str, str]:
        resolved_paths = paths or self._paths(worker_id)
        state_dir = resolved_paths.get("state_dir")
        if state_dir is None:
            state_dir = resolved_paths["home_dir"].parent / ".glasshive-state"
        credential_path = state_dir / "desktop-credentials.json"
        with self._desktop_credentials_lock:
            if credential_path.exists():
                try:
                    payload = json.loads(credential_path.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Worker desktop credentials are unreadable; refusing an insecure fallback") from exc
                credentials = {
                    "vnc_password": str(payload.get("vnc_password") or ""),
                    "router_username": str(payload.get("router_username") or ""),
                    "router_password": str(payload.get("router_password") or ""),
                }
                if not all(credentials.values()):
                    raise RuntimeError("Worker desktop credentials are incomplete; refusing an insecure fallback")
                if os.name != "nt":
                    credential_path.chmod(0o600)
                return credentials

            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                state_dir.chmod(0o700)
            credentials = {
                # The VNC protocol uses the first eight password bytes. Eight random
                # printable characters provide the strongest useful secret without
                # pretending that classic VNC accepts more than eight bytes.
                "vnc_password": "".join(
                    secrets.choice(VNC_PASSWORD_ALPHABET) for _ in range(8)
                ),
                "router_username": f"glasshive-{secrets.token_hex(4)}",
                "router_password": secrets.token_urlsafe(32),
            }
            descriptor = os.open(credential_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                serialized = (json.dumps(credentials, sort_keys=True) + "\n").encode("utf-8")
                os.write(descriptor, serialized)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if os.name != "nt":
                credential_path.chmod(0o600)
            return credentials

    def _sandbox_needs_desktop_auth_recreate(self, worker_id: str, sandbox: SandboxInfo) -> bool:
        if self.vnc_no_password:
            return False
        if getattr(sandbox, "desktop_auth_ready", None) is None:
            return False
        if not sandbox.desktop_auth_ready or not getattr(sandbox, "desktop_auth_fingerprint", None):
            return True
        credentials = self._desktop_credentials(worker_id)
        return sandbox.desktop_auth_fingerprint != self._desktop_auth_fingerprint(
            credentials["vnc_password"],
            credentials["router_username"],
            credentials["router_password"],
        )

    def _host_gateway_args(self) -> list[str]:
        if not self._env_flag("WPR_SANDBOX_ADD_HOST_GATEWAY", True):
            return []
        return ["--add-host", "host.docker.internal:host-gateway"]

    def _chromium_sandbox_args(self) -> list[str]:
        if not self._env_flag("WPR_SANDBOX_ALLOW_CHROMIUM_USERNS", True):
            return []
        return ["--security-opt", self.chromium_userns_security_opt]

    def _insert_resource_limits(
        self,
        command: list[str],
        *,
        resource_memory_bytes: int | None = None,
    ) -> None:
        resource_args: list[str] = []
        if resource_memory_bytes is not None:
            if isinstance(resource_memory_bytes, bool) or int(resource_memory_bytes) <= 0:
                raise ValueError("Worker resource memory must be a positive byte count")
            exact_memory = f"{int(resource_memory_bytes)}b"
            resource_args.extend(
                ["--memory", exact_memory, "--memory-swap", exact_memory]
            )
        else:
            if self.memory_limit:
                resource_args.extend(["--memory", self.memory_limit])
            if self.memory_swap_limit:
                resource_args.extend(["--memory-swap", self.memory_swap_limit])
        if self.cpu_limit:
            resource_args.extend(["--cpus", self.cpu_limit])
        if self.pids_limit:
            resource_args.extend(["--pids-limit", self.pids_limit])
        if not resource_args:
            return
        image_index = len(command) - 1
        command[image_index:image_index] = resource_args

    @staticmethod
    def _resource_memory_bytes_from_worker(worker: dict) -> int | None:
        if "resource_class" not in worker and "resource_memory_bytes" not in worker:
            return None
        resource_class = normalize_worker_resource_class(
            worker.get("resource_class") or "standard"
        )
        raw_memory = worker.get("resource_memory_bytes")
        if raw_memory in {None, ""}:
            memory_bytes = worker_resource_memory_bytes(resource_class)
        elif isinstance(raw_memory, bool) or int(raw_memory) <= 0:
            raise ValueError("Worker resource memory must be a positive byte count")
        else:
            memory_bytes = int(raw_memory)
        if (
            resource_class == "light"
            and memory_bytes != worker_resource_memory_bytes("light")
        ):
            raise ValueError("Light worker memory must use the trusted server value")
        return memory_bytes

    def _docker(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        timeout_sec: float | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["docker", *args]
        raw_timeout = os.environ.get("WPR_DOCKER_COMMAND_TIMEOUT_SEC", "60").strip()
        if timeout_sec is None:
            try:
                timeout_sec = float(raw_timeout)
            except ValueError:
                timeout_sec = 60.0
        timeout_sec = timeout_sec if timeout_sec and timeout_sec > 0 else None
        try:
            return subprocess.run(
                command,
                check=check,
                text=True,
                stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                timeout=timeout_sec,
                input=input_text,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stderr = (stderr + f"\nDocker command timed out after {timeout_sec:g}s").strip()
            if check:
                raise RuntimeError(stderr) from exc
            return subprocess.CompletedProcess(command, returncode=124, stdout=stdout, stderr=stderr)

    def _docker_exec(
        self,
        container_name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        detach: bool = False,
        fire_and_forget: bool = False,
        user: str | None = None,
        on_exit: Callable[[], None] | None = None,
        max_runtime_seconds: float | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = ["exec"]
        if input_text is not None:
            args.append("-i")
        if detach:
            args.append("-d")
        if input_text is not None:
            args.append("-i")
        args.extend(["-u", user or self.user])
        if cwd:
            args.extend(["-w", cwd])
        env_file: Path | None = None
        if env:
            env_dir = self.runtime_root / ".docker-exec-env"
            env_dir.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                env_dir.chmod(0o700)
            env_file = env_dir / f"exec-{secrets.token_hex(16)}.env"
            descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    for key, value in sorted(env.items()):
                        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
                            raise ValueError("Docker exec environment contains an invalid variable name")
                        text_value = str(value)
                        if any(character in text_value for character in "\r\n\0"):
                            raise ValueError("Docker exec environment values must be single-line text")
                        output.write(f"{key}={text_value}\n")
            except Exception:
                env_file.unlink(missing_ok=True)
                raise
            if os.name != "nt":
                env_file.chmod(0o600)
            args.extend(["--env-file", str(env_file)])
        args.append(container_name)
        args.extend(command)
        raw_timeout = os.environ.get("WPR_DOCKER_EXEC_TIMEOUT_SEC", "15").strip()
        try:
            timeout_sec = float(raw_timeout) if raw_timeout else None
        except ValueError:
            timeout_sec = None
        if fire_and_forget:
            full_command = ["docker", *args]
            spawned = self._spawn_detached_docker_exec(
                full_command,
                cleanup_path=env_file,
                on_exit=on_exit,
                max_runtime_seconds=max_runtime_seconds,
            )
            if not spawned and env_file is not None:
                env_file.unlink(missing_ok=True)
            return subprocess.CompletedProcess(
                full_command,
                returncode=0 if spawned else 1,
                stdout="",
                stderr="" if spawned else "Docker exec could not start",
            )
        try:
            docker_options: dict[str, object] = {
                "check": False,
                "capture_output": True,
                "timeout_sec": timeout_sec,
            }
            if input_text is not None:
                docker_options["input_text"] = input_text
            return self._docker(args, **docker_options)
        finally:
            if env_file is not None:
                env_file.unlink(missing_ok=True)

    def require_reviewed_openclaw(self, container_name: str) -> str:
        result = self._docker_exec(
            container_name,
            ["openclaw", "--version"],
            env=reviewed_openclaw_env({}),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"GlassHive requires OpenClaw {OPENCLAW_RUNTIME_VERSION}; "
                "the workstation runtime version could not be verified."
            )
        return require_reviewed_openclaw_version(result.stdout or result.stderr)

    def require_reviewed_openclaw_image(self) -> str:
        now = time.monotonic()
        if (
            self._openclaw_image_checked_at
            and self._openclaw_image_checked_at + self.image_check_ttl_sec > now
        ):
            return OPENCLAW_RUNTIME_VERSION
        self._ensure_image()
        result = self._docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "-e",
                "OPENCLAW_DISABLE_BONJOUR=1",
                "--entrypoint",
                "openclaw",
                self.image,
                "--version",
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"GlassHive requires OpenClaw {OPENCLAW_RUNTIME_VERSION}; "
                "the workstation image runtime version could not be verified."
            )
        version = require_reviewed_openclaw_version(result.stdout or result.stderr)
        self._openclaw_image_checked_at = time.monotonic()
        return version

    @staticmethod
    def _spawn_detached_docker_exec(
        full_command: list[str],
        *,
        cleanup_path: Path | None = None,
        on_exit: Callable[[], None] | None = None,
        max_runtime_seconds: float | None = None,
    ) -> bool:
        # Start a tiny shell trampoline instead of invoking the Docker CLI inside
        # the request path. Docker Desktop can take seconds to accept an
        # interactive exec; the HTTP/UI path must return immediately.
        cleanup = ""
        if cleanup_path is not None:
            quoted_path = shlex.quote(str(cleanup_path))
            cleanup = (
                f"cleanup_path={quoted_path}; "
                "trap 'rm -f -- \"$cleanup_path\"' EXIT HUP INT TERM; "
            )
        launch = ["sh", "-lc", f"{cleanup}sleep 0.1; {shlex.join(full_command)}"]
        try:
            process = subprocess.Popen(
                launch,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False
        if on_exit is not None:
            def wait_for_exit() -> None:
                timed_out = False
                try:
                    process.wait(timeout=max_runtime_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                except Exception:
                    logger.exception("Failed to monitor an interactive provider session")
                try:
                    # Removing the credential-bearing container is the authoritative
                    # stop.  Host signals only reap the detached docker-exec client;
                    # they do not stop the native process inside the container.
                    on_exit()
                except Exception:
                    logger.exception("Failed to close an interactive provider session")
                if timed_out:
                    try:
                        try:
                            process.wait(timeout=15)
                            return
                        except subprocess.TimeoutExpired:
                            pass
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            return
                        except (AttributeError, OSError):
                            try:
                                process.terminate()
                            except ProcessLookupError:
                                return
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                return
                            except (AttributeError, OSError):
                                try:
                                    process.kill()
                                except ProcessLookupError:
                                    return
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                logger.error(
                                    "Detached interactive provider client did not stop after cleanup",
                                    extra={"pid": process.pid},
                                )
                    finally:
                        if cleanup_path is not None:
                            try:
                                cleanup_path.unlink(missing_ok=True)
                            except OSError:
                                logger.warning(
                                    "Failed to remove a detached provider client environment file",
                                    extra={"pid": process.pid},
                                )

            Thread(
                target=wait_for_exit,
                name="glasshive-interactive-provider-session",
                daemon=True,
            ).start()
        return True

    def _ensure_container_writable_paths(self, container_name: str, container_paths: list[str]) -> None:
        safe_paths = [path for path in container_paths if path and path.startswith("/")]
        if not safe_paths:
            return
        quoted_paths = " ".join(shlex.quote(path) for path in safe_paths)
        container_user = shlex.quote(self.user.split(":", 1)[0] or self.user)
        host_uid = shlex.quote(str(os.getuid()))
        private_acl_required = self._private_acl_required()
        symlink_sensitive = any("/.config" in path for path in safe_paths)
        if symlink_sensitive:
            access_acl = (
                f"u:{container_user}:rwX,u:root:rwX,u:{host_uid}:rwX,"
                "g::---,o::---,m::rwX"
                if private_acl_required
                else f"u:{container_user}:rwX,u:{host_uid}:rwX"
            )
            default_acl = (
                f"d:u:{container_user}:rwX,d:u:root:rwX,d:u:{host_uid}:rwX,"
                "d:g::---,d:o::---,d:m::rwX"
                if private_acl_required
                else f"d:u:{container_user}:rwX,d:u:{host_uid}:rwX"
            )
            fallback = (
                "exit 1"
                if private_acl_required
                else (
                    f"find {quoted_paths} -xdev ! -type s ! -type l "
                    "-exec chmod u+rwX,go-rwx {} +"
                )
            )
            script = (
                "set -e; "
                f"mkdir -p {quoted_paths}; "
                f"find {quoted_paths} -xdev ! -type s ! -type l ! -user {container_user} "
                f"-exec chown -h {container_user} {{}} +; "
                "if command -v setfacl >/dev/null 2>&1 "
                f"&& setfacl -R -m {access_acl} -P {quoted_paths} 2>/dev/null; then "
                f"find {quoted_paths} -type d -exec setfacl -m {default_acl} {{}} + 2>/dev/null || true; "
                f"else {fallback}; fi"
            )
        elif private_acl_required:
            access_acl = (
                f"u:{container_user}:rwX,u:root:rwX,u:{host_uid}:rwX,"
                "g::---,o::---,m::rwX"
            )
            default_acl = (
                f"d:u:{container_user}:rwX,d:u:root:rwX,d:u:{host_uid}:rwX,"
                "d:g::---,d:o::---,d:m::rwX"
            )
            script = (
                "set -e; "
                f"mkdir -p {quoted_paths}; "
                "command -v setfacl >/dev/null 2>&1; "
                f"setfacl -R -m {access_acl} {quoted_paths}; "
                f"find {quoted_paths} -type d -exec setfacl -m {default_acl} {{}} +"
            )
        else:
            explicit_local_compatibility = (
                str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "")
                .strip()
                .lower()
                == "local"
            )
            if explicit_local_compatibility:
                script = (
                    "set -e; "
                    f"mkdir -p {quoted_paths}; "
                    "if command -v setfacl >/dev/null 2>&1 "
                    f"&& setfacl -R -m u:{container_user}:rwX,u:{host_uid}:rwX {quoted_paths} 2>/dev/null; then "
                    f"find {quoted_paths} -type d -exec setfacl -m d:u:{container_user}:rwX,d:u:{host_uid}:rwX {{}} + 2>/dev/null || true; "
                    "else "
                    f"chmod -R a+rwX {quoted_paths} 2>/dev/null || true; "
                    "fi"
                )
            else:
            # A local bind mount still contains worker-private state. Repair
            # ownership for ordinary entries and keep group/other access closed;
            # a world-writable fallback would expose that state on Linux hosts.
                operations: list[str] = ["set -e"]
                for path in safe_paths:
                    quoted_path = shlex.quote(path)
                    operations.extend(
                        [
                            f"mkdir -p {quoted_path}",
                            f"find {quoted_path} -xdev ! -type s ! -type l "
                            f"! -user {container_user} -exec chown -h {container_user} {{}} +",
                            f"find {quoted_path} -xdev ! -type s ! -type l "
                            "-exec chmod u+rwX,go-rwx {} +",
                        ]
                    )
                script = "; ".join(operations)
        result = self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
            },
            cwd=self.workspace_mount,
            user="root",
        )
        if result.returncode != 0:
            if private_acl_required:
                raise RuntimeError(
                    "Enterprise GlassHive requires POSIX ACL support for private worker paths"
                )
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Failed to prepare writable sandbox paths in {container_name}: {detail}")

    def _repair_provider_temp_ownership(self, container_name: str) -> None:
        runtime_user = shlex.quote(self.user.split(":", 1)[0] or self.user)
        temp_root = shlex.quote(self._browser_tmp_dir())
        script = (
            "set -e; "
            f"runtime_uid=$(id -u {runtime_user}); "
            f"runtime_gid=$(id -g {runtime_user}); "
            f"provider_tmp={temp_root}/claude-${{runtime_uid}}; "
            'if [ -d "$provider_tmp" ]; then '
            'chown -R "${runtime_uid}:${runtime_gid}" "$provider_tmp"; '
            'chmod 700 "$provider_tmp"; '
            "fi"
        )
        result = self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
            },
            cwd=self.workspace_mount,
            user="root",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Failed to repair provider temp ownership in {container_name}: {detail}")

    def _harden_secret_runtime_files(self, container_name: str) -> None:
        user = shlex.quote(self.user)
        secret_dir = shlex.quote(f"{self.home_mount}/.glasshive")
        script = (
            "set -e; "
            f"for file in {secret_dir}/secret-runtime.env {secret_dir}/secret-runtime.keys; do "
            '[ -e "$file" ] || continue; '
            f"chown {user} \"$file\" 2>/dev/null || true; "
            'chmod 600 "$file" 2>/dev/null || true; '
            "done"
        )
        self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
            },
            cwd=self.workspace_mount,
            user="root",
        )

    def _ensure_screen_runtime_dir(
        self,
        container_name: str,
        *,
        clean_room: bool = False,
    ) -> None:
        screen_user = self.user.split(":", 1)[0] or self.user
        screen_dir = f"/run/screen/S-{screen_user}"
        if clean_room:
            # The hardened container drops CAP_CHOWN. Its dedicated tmpfs is
            # created with the canonical runtime uid/gid, so prepare screen's
            # private socket tree as that same user and fail closed if Docker
            # did not honor the ownership contract.
            script = (
                "set -e; umask 077; "
                f"mkdir -p {shlex.quote(screen_dir)}; "
                f"chmod 1777 /run/screen; chmod 700 {shlex.quote(screen_dir)}"
            )
            exec_user = self.user
        else:
            script = (
                "set -e; "
                "mkdir -p /run/screen "
                f"{shlex.quote(screen_dir)}; "
                "chmod 1777 /run/screen; "
                f"chown {shlex.quote(self.user)} {shlex.quote(screen_dir)} 2>/dev/null || true; "
                f"chmod 700 {shlex.quote(screen_dir)}"
            )
            exec_user = "root"
        result = self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
            },
            cwd=self.workspace_mount,
            user=exec_user,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Failed to prepare screen runtime directory in {container_name}: {detail}")

    def _set_plain_background(self, container_name: str) -> None:
        script = (
            "for i in $(seq 1 60); do "
            f"DISPLAY={shlex.quote(self.display_value)} timeout 2s xsetroot -solid black >/dev/null 2>&1 || true; "
            "sleep 0.5; "
            "done"
        )
        self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
            detach=True,
            fire_and_forget=True,
        )

    def _prime_idle_desktop(self, container_name: str) -> None:
        launch_script = "\n".join(
            [
                self._prepare_chromium_profile_script(),
                f"nohup {self._chromium_launch_line(self._default_browser_url(), new_window=True)} >/dev/null 2>&1 &",
                "sleep 1",
                "wmctrl -xa chromium.Chromium || wmctrl -a Chromium || true",
            ]
        )
        result = self._docker_exec(
            container_name,
            ["bash", "-lc", launch_script],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-800:]
            raise RuntimeError(f"Idle desktop prime failed: {detail or f'exit code {result.returncode}'}")

    def _idle_desktop_prime_marker_path(self, worker_id: str) -> Path:
        return self._paths(worker_id)["state_dir"] / "desktop-prime.json"

    def _record_idle_desktop_prime(
        self,
        worker_id: str,
        sandbox: SandboxInfo,
        *,
        status: str,
        detail: str = "",
    ) -> None:
        payload = {
            "schema": "glasshive.desktop_prime.v1",
            "status": status,
            "updated_at": _utc_iso(),
            "container_name": sandbox.container_name,
            "container_id": sandbox.container_id,
            "image": sandbox.image,
            "default_browser_url": self._default_browser_url(),
        }
        if detail:
            payload["detail"] = detail[-800:]
        path = self._idle_desktop_prime_marker_path(worker_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            path.chmod(0o600)
        except OSError:
            return

    def _read_idle_desktop_prime(self, worker_id: str) -> dict[str, object] | None:
        path = self._idle_desktop_prime_marker_path(worker_id)
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _desktop_action_command(
        self,
        action: str,
        *,
        url: str | None = None,
        session_name: str | None = None,
    ) -> list[str] | None:
        safe_url = (url or "").strip() or self._default_browser_url()
        workspace = shlex.quote(self.workspace_mount)
        title = {
            "terminal": "WPR Shell",
            "files": "WPR Files",
            "codex": "Codex CLI",
            "claude": "Claude Code",
            "openclaw": "OpenClaw CLI",
        }
        if action == "terminal":
            attach_script = f"cd {workspace}; exec bash --noprofile --norc"
            if session_name:
                session_literal = shlex.quote(session_name)
                attach_script = (
                    f"cd {workspace}; "
                    f"SESSION={session_literal}; "
                    "for _ in $(seq 1 180); do "
                    "if screen -ls | grep -Fq \".${SESSION}\"; then exec screen -xRR \"$SESSION\"; fi; "
                    "sleep 1; "
                    "done; "
                    "printf '\\nLive session %s was not found. Opening a shell instead.\\n' \"$SESSION\"; "
                    "exec bash --noprofile --norc"
                )
            return [
                "xterm",
                "-bg",
                "black",
                "-fg",
                "#f5f5f5",
                "-fa",
                "Monospace",
                "-fs",
                "11",
                "-geometry",
                "140x40",
                "-T",
                "WPR Live Run" if session_name else title["terminal"],
                "-e",
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                attach_script,
            ]
        if action == "files":
            return ["pcmanfm", self.workspace_mount]
        if action == "browser":
            launch_script = self._chromium_launch_script(safe_url, start_maximized=True, new_tab=True)
            return [
                "bash",
                "-lc",
                launch_script,
            ]
        if action == "focus_browser":
            return [
                "bash",
                "-lc",
                "wmctrl -xa chromium.Chromium || wmctrl -a Chromium || xdotool search --onlyvisible --class chromium windowactivate || true",
            ]
        if action == "codex":
            return [
                "xterm",
                "-fa",
                "Monospace",
                "-fs",
                "11",
                "-geometry",
                "150x44",
                "-bg",
                "black",
                "-fg",
                "#f5f5f5",
                "-T",
                title["codex"],
                "-e",
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                f"cd {workspace}; exec codex",
            ]
        if action == "claude":
            return [
                "xterm",
                "-fa",
                "Monospace",
                "-fs",
                "11",
                "-geometry",
                "150x44",
                "-bg",
                "black",
                "-fg",
                "#f5f5f5",
                "-T",
                title["claude"],
                "-e",
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                f"cd {workspace}; exec claude --dangerously-skip-permissions",
            ]
        if action == "openclaw":
            return [
                "xterm",
                "-fa",
                "Monospace",
                "-fs",
                "11",
                "-geometry",
                "150x44",
                "-bg",
                "black",
                "-fg",
                "#f5f5f5",
                "-T",
                title["openclaw"],
                "-e",
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                (
                    f"cd {workspace}; "
                    "if [ -f \"$HOME/.wpr-openclaw/openclaw.env\" ]; then "
                    "source \"$HOME/.wpr-openclaw/openclaw.env\"; "
                    "fi; "
                    "echo 'OpenClaw workstation shell ready.'; "
                    "echo 'Useful commands: openclaw status | openclaw sessions | openclaw tui'; "
                    "exec bash"
                ),
            ]
        return None

    def _host_port_for(self, ports: dict[str, object], container_port: int) -> int | None:
        binding = ports.get(f"{container_port}/tcp")
        if not binding or not isinstance(binding, list):
            return None
        first = binding[0] or {}
        host_port = str(first.get("HostPort") or "").strip()
        return int(host_port) if host_port.isdigit() else None


    @staticmethod
    def _parallel_clean_room_configuration(
        *, require_proxy_containers: bool
    ) -> tuple[dict[str, str] | None, str]:
        network = str(
            os.environ.get("WPR_PARALLEL_CLEAN_ROOM_NETWORK") or ""
        ).strip()
        if not network or not _DOCKER_OBJECT_NAME.fullmatch(network):
            return None, "parallel_clean_room_network_unconfigured"

        provider_proxy_url = str(
            os.environ.get("WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL") or ""
        ).strip()
        try:
            parsed_provider_proxy = urlparse(provider_proxy_url)
            provider_proxy_hostname = str(parsed_provider_proxy.hostname or "")
            provider_proxy_port = parsed_provider_proxy.port
        except ValueError:
            parsed_provider_proxy = None
            provider_proxy_hostname = ""
            provider_proxy_port = None
        if (
            parsed_provider_proxy is None
            or parsed_provider_proxy.scheme not in {"http", "https"}
            or not provider_proxy_hostname
            or not _DOCKER_NETWORK_ALIAS.fullmatch(provider_proxy_hostname)
            or provider_proxy_hostname
            in {"localhost", "127.0.0.1", PARALLEL_CLEAN_ROOM_BROKER_ALIAS}
            or parsed_provider_proxy.username is not None
            or parsed_provider_proxy.password is not None
            or parsed_provider_proxy.query
            or parsed_provider_proxy.fragment
            or parsed_provider_proxy.path not in {"", "/"}
            or provider_proxy_port is None
        ):
            return None, "parallel_clean_room_provider_proxy_unconfigured"

        configuration = {
            "network": network,
            "provider_proxy_url": provider_proxy_url,
            "provider_proxy_hostname": provider_proxy_hostname,
        }
        if not require_proxy_containers:
            return configuration, ""

        provider_proxy_container = str(
            os.environ.get("WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER")
            or ""
        ).strip()
        if not provider_proxy_container or not _DOCKER_OBJECT_NAME.fullmatch(
            provider_proxy_container
        ):
            return None, "parallel_clean_room_provider_proxy_unconfigured"
        broker_proxy_container = str(
            os.environ.get("WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER") or ""
        ).strip()
        if not broker_proxy_container or not _DOCKER_OBJECT_NAME.fullmatch(
            broker_proxy_container
        ):
            return None, "parallel_clean_room_broker_proxy_unconfigured"
        provider_egress_network = str(
            os.environ.get("WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK")
            or ""
        ).strip()
        if (
            not provider_egress_network
            or not _DOCKER_OBJECT_NAME.fullmatch(provider_egress_network)
            or provider_egress_network == network
        ):
            return None, "parallel_clean_room_provider_egress_network_unconfigured"
        proxy_image = str(
            os.environ.get("VIVENTIUM_PARALLEL_PROXY_IMAGE")
            or PARALLEL_CLEAN_ROOM_PROXY_IMAGE
        ).strip()
        if (
            not proxy_image
            or len(proxy_image) > 255
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", proxy_image)
        ):
            return None, "parallel_clean_room_proxy_image_unconfigured"
        api_port = str(os.environ.get("VIVENTIUM_LC_API_PORT") or "3180").strip()
        if not api_port.isdigit() or not 1 <= int(api_port) <= 65535:
            return None, "parallel_clean_room_proxy_upstream_unconfigured"
        configuration.update(
            {
                "provider_proxy_container": provider_proxy_container,
                "broker_proxy_container": broker_proxy_container,
                "provider_egress_network": provider_egress_network,
                "proxy_image": proxy_image,
                "api_port": api_port,
            }
        )
        return configuration, ""

    @staticmethod
    def _parallel_clean_room_mission_network_name(container_name: str) -> str:
        configuration, reason = DockerSandboxManager._parallel_clean_room_configuration(
            require_proxy_containers=False
        )
        if configuration is None:
            raise RuntimeError(
                "Parallel clean-room mission network configuration is unavailable"
                f": {reason}"
            )
        normalized_container = str(container_name or "").strip()
        if not normalized_container or not _DOCKER_OBJECT_NAME.fullmatch(
            normalized_container
        ):
            raise RuntimeError("Parallel clean-room container identity is invalid")
        digest = hashlib.sha256(normalized_container.encode("utf-8")).hexdigest()[:16]
        suffix = f"-m-{digest}"
        base = str(configuration["network"])
        return f"{base[: 128 - len(suffix)]}{suffix}"

    def _ensure_parallel_clean_room_mission_network(
        self, container_name: str
    ) -> str:
        configuration, reason = self._parallel_clean_room_configuration(
            require_proxy_containers=True
        )
        if configuration is None:
            raise RuntimeError(
                "Parallel clean-room proxy substrate is unavailable"
                f": {reason}"
            )
        network_name = self._parallel_clean_room_mission_network_name(container_name)
        provider = configuration["provider_proxy_container"]
        broker = configuration["broker_proxy_container"]

        def inspect_network() -> dict | None:
            result = self._docker(
                ["network", "inspect", network_name],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            return self._docker_inspect_entry(result)

        def attest_members(network: dict | None) -> tuple[set[str], dict[str, str]]:
            labels = network.get("Labels") if isinstance(network, dict) else None
            members = network.get("Containers") if isinstance(network, dict) else None
            if (
                not isinstance(network, dict)
                or network.get("Name") != network_name
                or network.get("Driver") != "bridge"
                or network.get("Internal") is not True
                or not isinstance(labels, dict)
                or labels.get(PARALLEL_CLEAN_ROOM_POLICY_LABEL)
                != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                or labels.get(PARALLEL_CLEAN_ROOM_ROLE_LABEL)
                != PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE
                or labels.get(PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL)
                != container_name
                or not isinstance(members, dict)
            ):
                raise RuntimeError(
                    "Parallel clean-room mission network policy could not be attested"
                )
            member_names: set[str] = set()
            member_ids: dict[str, str] = {}
            for container_id, member in members.items():
                if (
                    not isinstance(container_id, str)
                    or not _DOCKER_CONTAINER_ID.fullmatch(container_id)
                    or not isinstance(member, dict)
                    or not isinstance(member.get("Name"), str)
                ):
                    raise RuntimeError(
                        "Parallel clean-room mission network membership is unavailable"
                    )
                member_name = str(member["Name"])
                member_names.add(member_name)
                member_ids[member_name] = container_id
            return member_names, member_ids

        create_args = [
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--label",
            (
                f"{PARALLEL_CLEAN_ROOM_POLICY_LABEL}="
                f"{PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}"
            ),
            "--label",
            (
                f"{PARALLEL_CLEAN_ROOM_ROLE_LABEL}="
                f"{PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE}"
            ),
            "--label",
            f"{PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL}={container_name}",
            network_name,
        ]

        def create_network():
            return self._docker(
                create_args,
                check=False,
                capture_output=True,
                timeout_sec=10,
            )

        network = inspect_network()
        if network is None:
            create = create_network()
            if create.returncode != 0:
                # A concurrent creator may have won the deterministic name.
                # Accept only the subsequently attested exact network; all
                # other failures may be address-pool pressure from attested
                # orphan mission networks left by an interrupted container
                # teardown. Reconcile those exact objects once, then retry the
                # same deterministic network before preserving the run.
                network = inspect_network()
                if network is None:
                    try:
                        self.repair_parallel_clean_room_mission_networks()
                    except Exception:
                        # The capacity result below remains the public truth;
                        # cleanup itself is fail-closed and never broad-prunes
                        # unlabeled or foreign Docker objects.
                        pass
                    create = create_network()
                    network = inspect_network()
                if create.returncode != 0 and network is None:
                    raise HostCapacityError(
                        "Parallel clean-room mission network capacity is temporarily unavailable.",
                        capacity_class="docker_network",
                    )
            else:
                network = inspect_network()
        required = {provider, broker}
        allowed = required | {container_name}
        member_names, member_ids = attest_members(network)
        if not member_names.issubset(allowed):
            raise RuntimeError(
                "Parallel clean-room mission network contains a foreign endpoint"
            )
        missing_proxies = required - member_names
        for proxy, alias in (
            (provider, configuration["provider_proxy_hostname"]),
            (broker, PARALLEL_CLEAN_ROOM_BROKER_ALIAS),
        ):
            if proxy not in missing_proxies:
                continue
            connected = self._docker(
                [
                    "network",
                    "connect",
                    "--alias",
                    alias,
                    network_name,
                    proxy,
                ],
                check=False,
                capture_output=True,
                timeout_sec=10,
            )
            if connected.returncode != 0:
                raise RuntimeError(
                    "Parallel clean-room proxy could not join the mission network"
                )
        if missing_proxies:
            network = inspect_network()
            member_names, member_ids = attest_members(network)
            if not member_names.issubset(allowed):
                raise RuntimeError(
                    "Parallel clean-room mission network contains a foreign endpoint"
                )
        if not required.issubset(member_names):
            raise RuntimeError(
                "Parallel clean-room mission network is missing its proxies"
            )
        critical_aliases = {
            configuration["provider_proxy_hostname"],
            PARALLEL_CLEAN_ROOM_BROKER_ALIAS,
        }
        for member_name in sorted(member_names):
            result = self._docker(
                ["inspect", member_name],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            endpoint = self._docker_inspect_entry(result)
            endpoint_networks = (
                (endpoint.get("NetworkSettings") or {}).get("Networks")
                if isinstance(endpoint, dict)
                and isinstance(endpoint.get("NetworkSettings"), dict)
                else None
            )
            attachment = (
                endpoint_networks.get(network_name)
                if isinstance(endpoint_networks, dict)
                else None
            )
            required_alias = (
                configuration["provider_proxy_hostname"]
                if member_name == provider
                else (
                    PARALLEL_CLEAN_ROOM_BROKER_ALIAS
                    if member_name == broker
                    else ""
                )
            )
            raw_aliases = (
                attachment.get("Aliases") if isinstance(attachment, dict) else None
            )
            # Docker Desktop reports the initial worker network attachment with
            # ``Aliases: null`` when no aliases were requested.  That is the
            # canonical no-authority state for the worker, while proxy aliases
            # must remain explicit and exact.
            aliases = [] if not required_alias and raw_aliases is None else raw_aliases
            if (
                endpoint is None
                or endpoint.get("Id") != member_ids[member_name]
                or not isinstance(aliases, list)
                or not all(isinstance(alias, str) for alias in aliases)
            ):
                raise RuntimeError(
                    "Parallel clean-room mission network endpoint could not be attested"
                )
            if (
                required_alias
                and (
                    required_alias not in aliases
                    or bool((critical_aliases - {required_alias}).intersection(aliases))
                )
            ) or (
                not required_alias and bool(critical_aliases.intersection(aliases))
            ):
                raise RuntimeError(
                    "Parallel clean-room mission network proxy alias could not be attested"
                )
        return network_name

    def repair_parallel_clean_room_mission_networks(self) -> tuple[str, ...]:
        """Reattach exact proxies after Compose recreates their generations.

        Dynamic Docker network attachments are not part of the proxy Compose
        model and are dropped when either proxy is recreated.  Enumerate only
        policy- and role-labeled mission networks, verify their worker label
        resolves to the deterministic network name, then reuse the strict
        per-network attestation/repair path.
        """

        configuration, reason = self._parallel_clean_room_configuration(
            require_proxy_containers=True
        )
        if configuration is None:
            raise RuntimeError(
                "Parallel clean-room proxy substrate is unavailable"
                f": {reason}"
            )
        result = self._docker(
            [
                "network",
                "ls",
                "--filter",
                (
                    f"label={PARALLEL_CLEAN_ROOM_POLICY_LABEL}="
                    f"{PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}"
                ),
                "--filter",
                (
                    f"label={PARALLEL_CLEAN_ROOM_ROLE_LABEL}="
                    f"{PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE}"
                ),
                "--format",
                "{{.Name}}",
            ],
            check=False,
            capture_output=True,
            timeout_sec=5,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Parallel clean-room mission network discovery is unavailable"
            )
        names = tuple(
            sorted(
                {
                    line.strip()
                    for line in str(result.stdout or "").splitlines()
                    if line.strip()
                }
            )
        )
        repaired: list[str] = []
        for network_name in names:
            if not _DOCKER_OBJECT_NAME.fullmatch(network_name):
                raise RuntimeError(
                    "Parallel clean-room mission network identity is invalid"
                )
            inspect_result = self._docker(
                ["network", "inspect", network_name],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            network = self._docker_inspect_entry(inspect_result)
            labels = network.get("Labels") if isinstance(network, dict) else None
            worker_container = (
                labels.get(PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL)
                if isinstance(labels, dict)
                else None
            )
            if (
                isinstance(worker_container, str)
                and _DOCKER_OBJECT_NAME.fullmatch(worker_container)
                and self._parallel_clean_room_mission_network_name(worker_container)
                != network_name
            ):
                # Mission networks are runtime-namespace scoped.  A prior or
                # side-by-side runtime can leave a correctly labeled network
                # behind, and that foreign object must neither be modified nor
                # prevent recovery of this runtime's exact networks.
                continue
            if (
                not isinstance(network, dict)
                or network.get("Name") != network_name
                or network.get("Driver") != "bridge"
                or network.get("Internal") is not True
                or not isinstance(labels, dict)
                or labels.get(PARALLEL_CLEAN_ROOM_POLICY_LABEL)
                != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                or labels.get(PARALLEL_CLEAN_ROOM_ROLE_LABEL)
                != PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE
                or not isinstance(worker_container, str)
                or not _DOCKER_OBJECT_NAME.fullmatch(worker_container)
            ):
                raise RuntimeError(
                    "Parallel clean-room mission network recovery policy could not be attested"
                )
            worker_result = self._docker(
                ["inspect", worker_container],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            worker_entry = self._docker_inspect_entry(worker_result)
            if worker_entry is None:
                detail = str(worker_result.stderr or worker_result.stdout or "").lower()
                if not (
                    worker_result.returncode != 0
                    and ("no such object" in detail or "no such container" in detail)
                ):
                    raise RuntimeError(
                        "Parallel clean-room worker generation could not be inspected"
                    )
                self._ensure_parallel_clean_room_mission_network(worker_container)
                self._remove_parallel_clean_room_mission_network(
                    worker_container,
                    network_name=network_name,
                )
                continue
            self._ensure_parallel_clean_room_mission_network(worker_container)
            repaired.append(network_name)
        return tuple(repaired)

    def _remove_parallel_clean_room_mission_network(
        self,
        container_name: str,
        network_name: str | None = None,
    ) -> None:
        expected_network = self._parallel_clean_room_mission_network_name(
            container_name
        )
        target_network = str(network_name or expected_network).strip()
        if target_network != expected_network:
            raise RuntimeError(
                "Parallel clean-room mission network identity changed before removal"
            )
        result = self._docker(
            ["network", "inspect", target_network],
            check=False,
            capture_output=True,
            timeout_sec=2,
        )
        if result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").lower()
            if "no such network" in detail or "not found" in detail:
                return
            raise RuntimeError(
                "Parallel clean-room mission network removal could not be attested"
            )
        network = self._docker_inspect_entry(result)
        labels = network.get("Labels") if isinstance(network, dict) else None
        members = network.get("Containers") if isinstance(network, dict) else None
        if (
            not isinstance(network, dict)
            or network.get("Name") != target_network
            or network.get("Driver") != "bridge"
            or network.get("Internal") is not True
            or not isinstance(labels, dict)
            or labels.get(PARALLEL_CLEAN_ROOM_POLICY_LABEL)
            != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            or labels.get(PARALLEL_CLEAN_ROOM_ROLE_LABEL)
            != PARALLEL_CLEAN_ROOM_MISSION_NETWORK_ROLE
            or labels.get(PARALLEL_CLEAN_ROOM_WORKER_CONTAINER_LABEL)
            != container_name
            or not isinstance(members, dict)
        ):
            raise RuntimeError(
                "Parallel clean-room mission network removal policy could not be attested"
            )

        roles_seen: set[str] = set()
        removable: list[str] = []
        for container_id, member in members.items():
            member_name = member.get("Name") if isinstance(member, dict) else None
            if (
                not isinstance(container_id, str)
                or not _DOCKER_CONTAINER_ID.fullmatch(container_id)
                or not isinstance(member_name, str)
                or not _DOCKER_OBJECT_NAME.fullmatch(member_name)
            ):
                raise RuntimeError(
                    "Parallel clean-room mission network endpoint could not be attested"
                )
            endpoint_result = self._docker(
                ["inspect", member_name],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            endpoint = self._docker_inspect_entry(endpoint_result)
            config = endpoint.get("Config") if isinstance(endpoint, dict) else None
            endpoint_labels = config.get("Labels") if isinstance(config, dict) else None
            role = (
                endpoint_labels.get(PARALLEL_CLEAN_ROOM_ROLE_LABEL)
                if isinstance(endpoint_labels, dict)
                else None
            )
            if (
                endpoint is None
                or endpoint.get("Id") != container_id
                or not isinstance(endpoint_labels, dict)
                or endpoint_labels.get(PARALLEL_CLEAN_ROOM_POLICY_LABEL)
                != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                or role
                not in {
                    PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_ROLE,
                    PARALLEL_CLEAN_ROOM_BROKER_PROXY_ROLE,
                }
                or role in roles_seen
            ):
                raise RuntimeError(
                    "Parallel clean-room mission network contains a foreign endpoint"
                )
            roles_seen.add(role)
            removable.append(member_name)
        if roles_seen != {
            PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_ROLE,
            PARALLEL_CLEAN_ROOM_BROKER_PROXY_ROLE,
        }:
            raise RuntimeError(
                "Parallel clean-room mission network proxy membership is incomplete"
            )
        for member_name in removable:
            disconnected = self._docker(
                ["network", "disconnect", "-f", target_network, member_name],
                check=False,
                capture_output=True,
                timeout_sec=10,
            )
            if disconnected.returncode != 0:
                raise RuntimeError(
                    "Parallel clean-room mission network endpoint could not be disconnected"
                )
        removed = self._docker(
            ["network", "rm", target_network],
            check=False,
            capture_output=True,
            timeout_sec=10,
        )
        if removed.returncode != 0:
            raise RuntimeError(
                "Parallel clean-room mission network removal could not be confirmed"
            )

    @staticmethod
    def _parallel_clean_room_readiness_result(
        *,
        ready: bool,
        reason: str,
        network: str = "unverified",
        provider_proxy: str = "unverified",
        broker_proxy: str = "unverified",
    ) -> dict[str, object]:
        return {
            "ready": ready,
            "reason": reason,
            "policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
            "attestations": {
                "network": network,
                "providerProxy": provider_proxy,
                "brokerProxy": broker_proxy,
            },
        }

    @staticmethod
    def _docker_inspect_entry(result: subprocess.CompletedProcess[str]) -> dict | None:
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, list) or len(payload) != 1:
            return None
        return payload[0] if isinstance(payload[0], dict) else None

    def _inspect_parallel_proxy_image_fresh(
        self, image_reference: str
    ) -> tuple[ConfiguredSandboxImage | None, str]:
        try:
            result = self._docker(
                ["image", "inspect", image_reference],
                check=False,
                capture_output=True,
                timeout_sec=self.image_inspect_timeout_sec,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None, "probe_unavailable"
        image = self._docker_inspect_entry(result)
        if image is None:
            return None, "unavailable"
        image_id = image.get("Id")
        config = image.get("Config")
        if (
            not isinstance(image_id, str)
            or not _DOCKER_IMAGE_ID.fullmatch(image_id)
            or not isinstance(config, dict)
        ):
            return None, "malformed"
        entrypoint_valid, entrypoint = _docker_command_tuple(
            config.get("Entrypoint")
        )
        command_valid, command = _docker_command_tuple(config.get("Cmd"))
        environment_valid, environment = _docker_environment_tuple(config.get("Env"))
        runtime_user = config.get("User")
        if (
            not isinstance(runtime_user, str)
            or runtime_user != PARALLEL_CLEAN_ROOM_PROXY_USER
            or _docker_user_is_root(runtime_user)
            or not entrypoint_valid
            or entrypoint != PARALLEL_CLEAN_ROOM_PROXY_ENTRYPOINT
            or not command_valid
            or command is not None
            or not environment_valid
        ):
            return None, "policy_mismatch"
        return (
            ConfiguredSandboxImage(
                image_id=image_id,
                runtime_user=runtime_user,
                entrypoint=entrypoint,
                command=command,
                environment=environment,
            ),
            "",
        )

    def parallel_clean_room_readiness(self) -> dict[str, object]:
        """Attest the exact automatic Parallel network boundary without mutation.

        Ordinary ``HTTP_PROXY``/``HTTPS_PROXY`` variables are intentionally not
        evidence. The mission network must be Docker-internal and policy-labeled,
        while the separately named provider and broker proxies must be running,
        Docker-healthchecked, policy-labeled, and attached under the aliases the
        worker will actually use.
        """

        configuration, reason = self._parallel_clean_room_configuration(
            require_proxy_containers=True
        )
        if configuration is None:
            network_status = (
                "unconfigured"
                if reason == "parallel_clean_room_network_unconfigured"
                else "unverified"
            )
            return self._parallel_clean_room_readiness_result(
                ready=False, reason=reason, network=network_status
            )

        network_name = configuration["network"]
        try:
            network_result = self._docker(
                ["network", "inspect", network_name],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
        except Exception:
            return self._parallel_clean_room_readiness_result(
                ready=False,
                reason="parallel_clean_room_network_probe_unavailable",
            )
        network = self._docker_inspect_entry(network_result)
        if network is None:
            return self._parallel_clean_room_readiness_result(
                ready=False, reason="parallel_clean_room_network_unavailable"
            )
        network_labels = network.get("Labels") or {}
        if (
            str(network.get("Name") or "") != network_name
            or str(network.get("Driver") or "") != "bridge"
            or network.get("Internal") is not True
            or not isinstance(network_labels, dict)
            or str(network_labels.get(PARALLEL_CLEAN_ROOM_POLICY_LABEL) or "")
            != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        ):
            return self._parallel_clean_room_readiness_result(
                ready=False,
                reason="parallel_clean_room_network_policy_mismatch",
                network="policy_mismatch",
            )
        network_members = network.get("Containers")
        if not isinstance(network_members, dict) or not all(
            isinstance(container_id, str)
            and _DOCKER_CONTAINER_ID.fullmatch(container_id)
            and isinstance(member, dict)
            for container_id, member in network_members.items()
        ):
            return self._parallel_clean_room_readiness_result(
                ready=False,
                reason="parallel_clean_room_network_membership_unavailable",
                network="unverified",
            )

        proxy_image, proxy_image_status = self._inspect_parallel_proxy_image_fresh(
            configuration["proxy_image"]
        )
        if proxy_image is None:
            return self._parallel_clean_room_readiness_result(
                ready=False,
                reason=f"parallel_clean_room_proxy_image_{proxy_image_status}",
                network="internal",
            )

        def inspect_proxy(
            *, container_name: str, role: str, required_alias: str
        ) -> tuple[dict | None, str]:
            try:
                result = self._docker(
                    ["inspect", container_name],
                    check=False,
                    capture_output=True,
                    timeout_sec=2,
                )
            except Exception:
                return None, "probe_unavailable"
            proxy = self._docker_inspect_entry(result)
            if proxy is None:
                return None, "unavailable"
            state = proxy.get("State") or {}
            health = state.get("Health") or {}
            if not isinstance(state, dict) or state.get("Running") is not True:
                return None, "not_running"
            if not isinstance(health, dict) or str(health.get("Status") or "") != "healthy":
                return None, "unhealthy"
            config = proxy.get("Config") or {}
            labels = config.get("Labels") or {} if isinstance(config, dict) else {}
            if (
                not isinstance(labels, dict)
                or str(labels.get(PARALLEL_CLEAN_ROOM_POLICY_LABEL) or "")
                != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                or str(labels.get(PARALLEL_CLEAN_ROOM_ROLE_LABEL) or "") != role
            ):
                return None, "policy_mismatch"
            host_config = proxy.get("HostConfig")
            mounts = proxy.get("Mounts")
            image_id = proxy.get("Image")
            image_reference = config.get("Image") if isinstance(config, dict) else None
            runtime_user = config.get("User") if isinstance(config, dict) else None
            entrypoint_valid, entrypoint = _docker_command_tuple(
                config.get("Entrypoint") if isinstance(config, dict) else None
            )
            command_valid, command = _docker_command_tuple(
                config.get("Cmd") if isinstance(config, dict) else None
            )
            environment_valid, environment = _docker_environment_tuple(
                config.get("Env") if isinstance(config, dict) else None
            )
            expected_environment = dict(proxy_image.environment)
            expected_environment.update(
                {
                    "VIVENTIUM_PARALLEL_PROXY_ROLE": (
                        "provider"
                        if role == PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_ROLE
                        else "broker"
                    ),
                    "VIVENTIUM_PARALLEL_PROXY_UPSTREAM": (
                        "http://host.docker.internal:"
                        f"{configuration['api_port']}"
                    ),
                    "VIVENTIUM_PARALLEL_PROXY_PORT": "8080",
                }
            )
            tmpfs_valid, tmpfs_options = _docker_tmpfs_records(
                host_config.get("Tmpfs") if isinstance(host_config, dict) else None
            )
            expected_tmpfs_valid, expected_tmpfs = _docker_tmpfs_records(
                {
                    target: options
                    for target, options in (
                        item.split(":", 1)
                        for item in PARALLEL_CLEAN_ROOM_PROXY_TMPFS
                    )
                }
            )
            extra_hosts = (
                host_config.get("ExtraHosts")
                if isinstance(host_config, dict)
                else None
            )
            cap_add = (
                host_config.get("CapAdd") if isinstance(host_config, dict) else None
            )
            cap_drop = (
                host_config.get("CapDrop") if isinstance(host_config, dict) else None
            )
            security_options = (
                host_config.get("SecurityOpt")
                if isinstance(host_config, dict)
                else None
            )
            if (
                image_id != proxy_image.image_id
                or image_reference != configuration["proxy_image"]
                or runtime_user != PARALLEL_CLEAN_ROOM_PROXY_USER
                or not entrypoint_valid
                or entrypoint != proxy_image.entrypoint
                or not command_valid
                or command != proxy_image.command
                or not environment_valid
                or dict(environment) != expected_environment
                or any(
                    name.startswith(PARALLEL_CLEAN_ROOM_FORBIDDEN_CONTAINER_ENV_PREFIXES)
                    for name, _value in environment
                )
                or not isinstance(host_config, dict)
                # Both reviewed proxy roles are the only dual-homed members.
                # The broker needs the egress-side host gateway to reach the
                # exact Core MCP route; workers remain internal-only.
                or host_config.get("NetworkMode")
                != configuration["provider_egress_network"]
                or host_config.get("PidMode") not in {"", "private"}
                or host_config.get("IpcMode") != "private"
                or host_config.get("UTSMode") not in {"", "private"}
                or host_config.get("UsernsMode") != ""
                or host_config.get("CgroupnsMode") != "private"
                or host_config.get("ReadonlyRootfs") is not True
                or host_config.get("Privileged") is not False
                or (cap_add is not None and cap_add != () and cap_add != [])
                or not isinstance(cap_drop, list)
                or {str(item).upper() for item in cap_drop} != {"ALL"}
                or security_options != ["no-new-privileges:true"]
                or extra_hosts != ["host.docker.internal:host-gateway"]
                or not tmpfs_valid
                or not expected_tmpfs_valid
                or tmpfs_options != expected_tmpfs
                or mounts != []
                or host_config.get("PortBindings") != {}
                or (
                    host_config.get("Devices") is not None
                    and host_config.get("Devices") != ()
                    and host_config.get("Devices") != []
                )
                or (
                    host_config.get("DeviceRequests") is not None
                    and host_config.get("DeviceRequests") != ()
                    and host_config.get("DeviceRequests") != []
                )
                or (
                    host_config.get("Binds") is not None
                    and host_config.get("Binds") != ()
                    and host_config.get("Binds") != []
                )
                or host_config.get("PublishAllPorts") is not False
                or host_config.get("Memory") != 128 * 1024 * 1024
                or host_config.get("NanoCpus") != 500_000_000
                or host_config.get("PidsLimit") != 64
            ):
                return None, "policy_mismatch"
            network_settings = proxy.get("NetworkSettings") or {}
            networks = (
                network_settings.get("Networks") or {}
                if isinstance(network_settings, dict)
                else {}
            )
            attached = networks.get(network_name) if isinstance(networks, dict) else None
            aliases = attached.get("Aliases") or [] if isinstance(attached, dict) else []
            if required_alias not in aliases:
                return None, "network_alias_mismatch"
            expected_networks = {
                network_name,
                configuration["provider_egress_network"],
            }
            attached_networks = set(networks)
            mission_prefix = f"{network_name}-m-"
            mission_networks = attached_networks - expected_networks
            if (
                not expected_networks.issubset(attached_networks)
                or any(
                    not re.fullmatch(
                        re.escape(mission_prefix) + r"[0-9a-f]{16}",
                        candidate,
                    )
                    for candidate in mission_networks
                )
            ):
                return None, "network_set_mismatch"
            return proxy, "healthy"

        provider_proxy, provider_status = inspect_proxy(
            container_name=configuration["provider_proxy_container"],
            role=PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_ROLE,
            required_alias=configuration["provider_proxy_hostname"],
        )
        if provider_proxy is None:
            return self._parallel_clean_room_readiness_result(
                ready=False,
                reason=f"parallel_clean_room_provider_proxy_{provider_status}",
                network="internal",
                provider_proxy=provider_status,
            )
        broker_proxy, broker_status = inspect_proxy(
            container_name=configuration["broker_proxy_container"],
            role=PARALLEL_CLEAN_ROOM_BROKER_PROXY_ROLE,
            required_alias=PARALLEL_CLEAN_ROOM_BROKER_ALIAS,
        )
        if broker_proxy is None:
            return self._parallel_clean_room_readiness_result(
                ready=False,
                reason=f"parallel_clean_room_broker_proxy_{broker_status}",
                network="internal",
                provider_proxy="healthy",
                broker_proxy=broker_status,
            )
        provider_id = provider_proxy.get("Id")
        broker_id = broker_proxy.get("Id")
        if (
            not isinstance(provider_id, str)
            or not _DOCKER_CONTAINER_ID.fullmatch(provider_id)
            or not isinstance(broker_id, str)
            or not _DOCKER_CONTAINER_ID.fullmatch(broker_id)
            or provider_id not in network_members
            or broker_id not in network_members
        ):
            return self._parallel_clean_room_readiness_result(
                ready=False,
                reason="parallel_clean_room_proxy_membership_mismatch",
                network="internal",
                provider_proxy="policy_mismatch",
                broker_proxy="policy_mismatch",
            )

        critical_aliases = {
            configuration["provider_proxy_hostname"],
            PARALLEL_CLEAN_ROOM_BROKER_ALIAS,
        }
        for endpoint_id in network_members:
            if endpoint_id in {provider_id, broker_id}:
                continue
            try:
                endpoint_result = self._docker(
                    ["inspect", endpoint_id],
                    check=False,
                    capture_output=True,
                    timeout_sec=2,
                )
            except Exception:
                endpoint_result = None
            endpoint = (
                self._docker_inspect_entry(endpoint_result)
                if endpoint_result is not None
                else None
            )
            endpoint_networks = (
                (endpoint.get("NetworkSettings") or {}).get("Networks")
                if isinstance(endpoint, dict)
                and isinstance(endpoint.get("NetworkSettings"), dict)
                else None
            )
            attached = (
                endpoint_networks.get(network_name)
                if isinstance(endpoint_networks, dict)
                else None
            )
            aliases = attached.get("Aliases") if isinstance(attached, dict) else None
            if (
                endpoint is None
                or endpoint.get("Id") != endpoint_id
                or not isinstance(aliases, list)
                or not all(isinstance(alias, str) for alias in aliases)
            ):
                return self._parallel_clean_room_readiness_result(
                    ready=False,
                    reason="parallel_clean_room_network_endpoint_probe_unavailable",
                    network="unverified",
                    provider_proxy="healthy",
                    broker_proxy="healthy",
                )
            if critical_aliases.intersection(aliases):
                return self._parallel_clean_room_readiness_result(
                    ready=False,
                    reason="parallel_clean_room_network_alias_ambiguous",
                    network="policy_mismatch",
                    provider_proxy="healthy",
                    broker_proxy="healthy",
                )
            return self._parallel_clean_room_readiness_result(
                ready=False,
                reason="parallel_clean_room_base_network_foreign_endpoint",
                network="policy_mismatch",
                provider_proxy="healthy",
                broker_proxy="healthy",
            )
        return self._parallel_clean_room_readiness_result(
            ready=True,
            reason="",
            network="internal",
            provider_proxy="healthy",
            broker_proxy="healthy",
        )

    def resource_usage(self) -> DockerResourceUsage:
        """Measure Docker VM and GlassHive container headroom fail-closed.

        Host ``ps`` cannot see Linux-VM process trees on Docker Desktop. This
        probe uses Docker's own process/stat surfaces, and an explicit Docker
        disk budget (or a locally observable Docker root/settings capacity), so
        automatic missions cannot be admitted on fabricated host-only zeros.
        """

        now = time.monotonic()
        cached = self._resource_usage_cache
        if cached and cached[0] + 2.0 > now:
            return cached[1]
        unavailable = DockerResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=0,
            available_disk_bytes=0,
            running_worker_containers=0,
            running_worker_ids=(),
            process_probe_ok=False,
            memory_probe_ok=False,
            disk_probe_ok=False,
        )
        try:
            info_result = self._docker(
                ["info", "--format", "{{json .}}"],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            ps_result = self._docker(
                ["ps", "--format", "{{.Names}}"],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            stats_result = self._docker(
                ["stats", "--no-stream", "--format", "{{json .}}"],
                check=False,
                capture_output=True,
                timeout_sec=3,
            )
        except Exception:
            self._resource_usage_cache = (now, unavailable)
            return unavailable
        if any(
            result.returncode != 0
            for result in (info_result, ps_result, stats_result)
        ):
            self._resource_usage_cache = (now, unavailable)
            return unavailable
        try:
            info = json.loads(info_result.stdout or "{}")
            total_memory = int(info.get("MemTotal") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            total_memory = 0
            info = {}
        running_names = {
            line.strip()
            for line in (ps_result.stdout or "").splitlines()
            if line.strip()
        }
        try:
            worker_directories = list((self.runtime_root / "workers").iterdir())
        except FileNotFoundError:
            # A fresh runtime has no legacy worker directories yet. Docker VM
            # headroom is still measured below before the first admission.
            worker_directories = []
        worker_ids_by_name = {
            self._container_name(entry.name): entry.name
            for entry in worker_directories
            if entry.is_dir() and not entry.is_symlink()
        }
        worker_names = sorted(running_names & worker_ids_by_name.keys())
        running_worker_ids = tuple(worker_ids_by_name[name] for name in worker_names)

        memory_used = 0
        memory_probe_ok = total_memory > 0
        for line in (stats_result.stdout or "").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                memory_probe_ok = False
                continue
            used = _docker_size_bytes(row.get("MemUsage"))
            if used is None:
                memory_probe_ok = False
            else:
                memory_used += used

        def _container_process_counts(name: str) -> tuple[int, int] | None:
            result = self._docker(
                # Docker Desktop delegates this format to the daemon's ps.
                # Unlike host ps, recent Docker versions reject field names
                # with the output-suppression suffix (``pid=,tid=``).
                ["top", name, "-eo", "pid,tid"],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            if result.returncode != 0:
                return None
            processes: set[int] = set()
            threads: set[int] = set()
            for line in (result.stdout or "").splitlines():
                parts = line.split()
                if len(parts) != 2:
                    continue
                try:
                    process_id, thread_id = (int(part) for part in parts)
                except ValueError:
                    continue
                processes.add(process_id)
                threads.add(thread_id)
            return len(processes), len(threads)

        with ThreadPoolExecutor(max_workers=min(4, max(1, len(worker_names)))) as pool:
            counts = list(pool.map(_container_process_counts, worker_names))
        process_probe_ok = all(item is not None for item in counts)
        child_processes = sum((item or (0, 0))[0] for item in counts)
        threads = sum((item or (0, 0))[1] for item in counts)
        worker_process_counts = tuple(
            (
                worker_id,
                int((count or (0, 0))[0]),
                int((count or (0, 0))[1]),
            )
            for worker_id, count in zip(running_worker_ids, counts)
        )

        available_disk = self._docker_vm_available_disk_bytes(running_names)
        disk_probe_ok = available_disk is not None
        if available_disk is None:
            # Linux exposes DockerRootDir directly; managed installs may also
            # supply an explicit VM budget. This fallback remains conservative
            # by subtracting Docker's reported current allocation.
            try:
                disk_result = self._docker(
                    ["system", "df", "--format", "{{json .}}"],
                    check=False,
                    capture_output=True,
                    timeout_sec=3,
                )
            except Exception:
                disk_result = None
            docker_disk_used = 0
            disk_probe_ok = bool(disk_result and disk_result.returncode == 0)
            for line in ((disk_result.stdout if disk_result else "") or "").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    disk_probe_ok = False
                    continue
                size = _docker_size_bytes(row.get("Size"))
                if size is None:
                    disk_probe_ok = False
                else:
                    docker_disk_used += size
            disk_budget = self._docker_disk_budget_bytes(info)
            if disk_budget is None:
                disk_probe_ok = False
                available_disk = 0
            else:
                available_disk = max(0, disk_budget - docker_disk_used)
        if disk_probe_ok:
            try:
                host_free = int(shutil.disk_usage(self.runtime_root).free)
            except OSError:
                disk_probe_ok = False
                host_free = 0
            available_disk = max(0, min(host_free, int(available_disk or 0)))

        usage = DockerResourceUsage(
            child_processes=child_processes,
            threads=threads,
            available_memory_bytes=max(0, total_memory - memory_used),
            available_disk_bytes=available_disk,
            running_worker_containers=len(worker_names),
            running_worker_ids=running_worker_ids,
            worker_process_counts=worker_process_counts,
            process_probe_ok=process_probe_ok,
            memory_probe_ok=memory_probe_ok,
            disk_probe_ok=disk_probe_ok,
        )
        self._resource_usage_cache = (now, usage)
        return usage

    def cached_resource_usage(self, *, max_age_seconds: float = 30.0) -> DockerResourceUsage | None:
        cached = self._resource_usage_cache
        if not cached or cached[0] + max(1.0, max_age_seconds) <= time.monotonic():
            return None
        return cached[1]

    def _docker_vm_available_disk_bytes(self, running_names: set[str]) -> int | None:
        names = sorted(running_names)[:4]
        commands: list[list[str]] = [
            ["exec", name, "df", "-B1", "--output=size,used,avail", "/"]
            for name in names
        ]
        # Background/startup-only fallback for a clean Docker Desktop install.
        # The first running services may use BusyBox ``df`` and reject GNU's
        # ``--output`` option. Always retain one networkless, already-inspected
        # workstation probe after those cheaper candidates. Atomic delegation
        # consumes only the cached snapshot and never launches this probe.
        commands.append(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "df",
                self.image,
                "-B1",
                "--output=size,used,avail",
                "/",
            ]
        )
        for command in commands:
            result = self._docker(
                command,
                check=False,
                capture_output=True,
                timeout_sec=4,
            )
            if result.returncode != 0:
                continue
            numeric_rows = [
                line.split()
                for line in (result.stdout or "").splitlines()
                if len(line.split()) >= 3 and all(part.isdigit() for part in line.split()[-3:])
            ]
            if numeric_rows:
                return int(numeric_rows[-1][-1])
        return None

    def _docker_disk_budget_bytes(self, info: dict[str, object]) -> int | None:
        raw_budget = str(os.environ.get("WPR_DOCKER_DISK_BUDGET_MB") or "").strip()
        if raw_budget:
            try:
                return max(0, int(raw_budget)) * 1024**2
            except ValueError:
                return None
        docker_root = Path(str(info.get("DockerRootDir") or "")).expanduser()
        if docker_root.is_dir():
            try:
                return int(shutil.disk_usage(docker_root).total)
            except OSError:
                return None
        # Docker Desktop persists its configured VM disk ceiling here. Avoid a
        # guessed default: if the setting cannot be proven, readiness remains
        # false until the compiler supplies WPR_DOCKER_DISK_BUDGET_MB.
        settings_candidates = (
            Path.home()
            / "Library/Group Containers/group.com.docker/settings-store.json",
            Path.home()
            / "Library/Group Containers/group.com.docker/settings.json",
        )
        for settings_path in settings_candidates:
            try:
                settings = json.loads(settings_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            raw_size = settings.get("diskSizeMiB") if isinstance(settings, dict) else None
            if isinstance(raw_size, dict):
                raw_size = raw_size.get("value")
            try:
                size_mib = int(raw_size or 0)
            except (TypeError, ValueError):
                continue
            if size_mib > 0:
                return size_mib * 1024**2
        return None







    def _sandbox_matches_parallel_clean_room_policy(
        self, sandbox: SandboxInfo
    ) -> bool:
        configuration, _reason = self._parallel_clean_room_configuration(
            require_proxy_containers=False
        )
        if configuration is None:
            return False
        try:
            mission_network = self._parallel_clean_room_mission_network_name(
                sandbox.container_name
            )
        except RuntimeError:
            return False
        security_options = {
            str(option).strip().lower() for option in sandbox.security_options
        }
        expected_tmpfs_mapping = {
            value.split(":", 1)[0]: value.split(":", 1)[1]
            for value in PARALLEL_CLEAN_ROOM_TMPFS
        }
        expected_tmpfs_valid, expected_tmpfs_options = _docker_tmpfs_records(
            expected_tmpfs_mapping
        )
        if not expected_tmpfs_valid:
            return False
        expected_tmpfs = set(expected_tmpfs_mapping)
        expected_bind_mount_pairs = {
            (str(sandbox.workspace_dir), self.workspace_mount),
            (str(sandbox.home_dir), self.home_mount),
        }
        expected_mount_records = {
            (
                "bind",
                str(sandbox.workspace_dir),
                self.workspace_mount,
            ),
            (
                "bind",
                str(sandbox.home_dir),
                self.home_mount,
            ),
        }
        expected_bind_mount_options = {
            (
                str(sandbox.workspace_dir),
                self.workspace_mount,
                True,
                "",
                "rprivate",
            ),
            (
                str(sandbox.home_dir),
                self.home_mount,
                True,
                "",
                "rprivate",
            ),
        }
        expected_environment = dict(sandbox.expected_environment)
        expected_environment.update(
            {
                "HOME": self.home_mount,
                "TERM": self.term_value,
                "TMPDIR": self.service_tmp_dir,
                "XDG_CACHE_HOME": self._browser_cache_dir(),
                "XDG_CONFIG_HOME": self._browser_config_dir(),
                "SE_VNC_NO_PASSWORD": "1" if self.vnc_no_password else "0",
                "HTTP_PROXY": configuration["provider_proxy_url"],
                "HTTPS_PROXY": configuration["provider_proxy_url"],
                "NO_PROXY": (
                    f"{configuration['provider_proxy_hostname']},"
                    f"{PARALLEL_CLEAN_ROOM_BROKER_ALIAS},localhost,127.0.0.1"
                ),
            }
        )
        environment_names = {name for name, _value in sandbox.environment}
        return bool(
            str(sandbox.container_id or "").strip()
            and sandbox.execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            and sandbox.image_id == sandbox.expected_image_id
            and sandbox.image_reference == self.image
            and sandbox.runtime_user == self.user
            and sandbox.runtime_user == sandbox.expected_runtime_user
            and not _docker_user_is_root(sandbox.runtime_user)
            and sandbox.entrypoint == sandbox.expected_entrypoint
            and sandbox.command == sandbox.expected_command
            and sandbox.network_mode == mission_network
            and set(sandbox.attached_networks) == {mission_network}
            and sandbox.pid_mode in {"", "private"}
            and sandbox.ipc_mode == "private"
            and sandbox.uts_mode in {"", "private"}
            and sandbox.userns_mode == ""
            and sandbox.cgroupns_mode == "private"
            and sandbox.read_only_rootfs
            and sandbox.privileged is False
            and sandbox.cap_add == ()
            and {capability.upper() for capability in sandbox.cap_drop} == {"ALL"}
            and security_options == {"no-new-privileges:true"}
            and sandbox.extra_hosts == ()
            and set(sandbox.bind_mount_targets)
            == {self.workspace_mount, self.home_mount}
            and len(sandbox.bind_mount_pairs) == 2
            and set(sandbox.bind_mount_pairs) == expected_bind_mount_pairs
            and len(sandbox.mount_records) == 2
            and set(sandbox.mount_records) == expected_mount_records
            and len(sandbox.bind_mount_options) == 2
            and set(sandbox.bind_mount_options) == expected_bind_mount_options
            and set(sandbox.tmpfs_targets) == expected_tmpfs
            and sandbox.tmpfs_options == expected_tmpfs_options
            # The isolated mission network is deliberately not externally
            # publishable. Glass Drive remains the owner-scoped work/control
            # surface; any direct worker port mapping is policy drift.
            and sandbox.port_bindings == ()
            and dict(sandbox.environment) == expected_environment
            and not any(
                name.startswith(PARALLEL_CLEAN_ROOM_FORBIDDEN_CONTAINER_ENV_PREFIXES)
                for name in environment_names
            )
        )

    @staticmethod
    def _harden_private_directory(path: Path) -> None:
        """Validate/chmod one service-owned directory without following links."""

        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeError(f"Private sandbox directory is unavailable: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"Private sandbox path is not a real directory: {path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise RuntimeError(f"Private sandbox directory has an unexpected owner: {path}")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RuntimeError(f"Private sandbox directory could not be secured: {path}") from exc
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeError(f"Private sandbox directory changed during validation: {path}")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)

    def _migrate_existing_worker_permissions(self) -> None:
        workers_root = self.runtime_root / "workers"
        try:
            workers_root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        self._harden_private_directory(workers_root)
        for entry in os.scandir(workers_root):
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                # Finder metadata and similarly harmless service-owned files
                # may coexist at the collection root. Keep them private; only
                # real directories are interpreted as worker identities.
                if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                    raise RuntimeError(
                        f"Unexpected owner in private sandbox worker root: {entry.name}"
                    )
                descriptor = os.open(
                    entry.path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise RuntimeError(
                            f"Sandbox worker-root file changed during validation: {entry.name}"
                        )
                    os.fchmod(descriptor, 0o600)
                finally:
                    os.close(descriptor)
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(
                    f"Unexpected entry in private sandbox worker root: {entry.name}"
                )
            self._ensure_worker_permissions_migrated(Path(entry.path))

    @staticmethod
    def _worker_permissions_marker(worker_root: Path) -> Path:
        return worker_root / ".host-permissions-v3"

    @classmethod
    def _ensure_worker_permissions_migrated(cls, worker_root: Path) -> None:
        # Keep this helper safe when invoked independently of ``_ensure_host_dirs``
        # (for example by startup repair or a narrowly mocked caller). The
        # collection root is created and secured by the manager constructor;
        # never create missing ancestors or follow a pre-planted worker link.
        try:
            worker_root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        cls._harden_private_directory(worker_root)
        marker = cls._worker_permissions_marker(worker_root)
        try:
            metadata = marker.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise RuntimeError(
                    f"Sandbox permission marker is not trustworthy: {marker}"
                )
            cls._harden_worker_permissions_marker(marker, metadata)
            return
        cls._harden_host_worker_tree(worker_root)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags, 0o600)
        except FileExistsError:
            # Another local service process may have completed the same
            # migration. Validate the now-existing entry exactly once; broken
            # symlinks must fail closed instead of recursing forever.
            try:
                raced = marker.lstat()
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"Sandbox permission marker raced during migration: {marker}"
                ) from exc
            if (
                stat.S_ISLNK(raced.st_mode)
                or not stat.S_ISREG(raced.st_mode)
                or (hasattr(os, "getuid") and raced.st_uid != os.getuid())
            ):
                raise RuntimeError(
                    f"Sandbox permission marker is not trustworthy: {marker}"
                )
            cls._harden_worker_permissions_marker(marker, raced)
            return
        try:
            os.write(descriptor, b"owner-only-v3\n")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _harden_worker_permissions_marker(marker: Path, metadata: os.stat_result) -> None:
        """Revalidate/chmod the migration marker without following a raced link."""

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags)
        except OSError as exc:
            raise RuntimeError(
                f"Sandbox permission marker could not be secured: {marker}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeError(
                    f"Sandbox permission marker changed during validation: {marker}"
                )
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)


    @classmethod
    def _harden_host_worker_tree(cls, worker_root: Path) -> None:
        if not worker_root.exists():
            return
        cls._harden_private_directory(worker_root)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(worker_root, flags)
        try:
            cls._harden_host_tree_fd(root_fd)
        finally:
            os.close(root_fd)

    @classmethod
    def _harden_host_tree_fd(cls, directory_fd: int) -> None:
        """Harden descendants using no-follow dirfd operations.

        Worker-controlled symlinks are intentionally ignored. ``Path.chmod``
        follows symlinks on supported platforms and would turn a sandbox link
        into a mutation gadget against arbitrary service-owned host paths.
        """

        non_executable_suffixes = {
            ".env",
            ".json",
            ".jsonl",
            ".md",
            ".toml",
            ".txt",
            ".yaml",
            ".yml",
        }
        for entry in os.scandir(directory_fd):
            try:
                metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise RuntimeError(
                    f"Sandbox descendant has an unexpected owner: {entry.name}"
                )
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    continue
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise RuntimeError(
                            f"Sandbox directory changed during hardening: {entry.name}"
                        )
                    os.fchmod(child_fd, 0o700)
                    cls._harden_host_tree_fd(child_fd)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                # Sockets/FIFOs/devices remain unreachable behind the private
                # directory boundary; do not open a worker-controlled special
                # file while hardening.
                continue
            try:
                file_fd = os.open(
                    entry.name,
                    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | nofollow,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                continue
            try:
                opened = os.fstat(file_fd)
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise RuntimeError(
                        f"Sandbox file changed during hardening: {entry.name}"
                    )
                suffix = Path(entry.name).suffix.lower()
                executable = suffix not in non_executable_suffixes and bool(
                    metadata.st_mode & stat.S_IXUSR
                )
                os.fchmod(file_fd, 0o700 if executable else 0o600)
            finally:
                os.close(file_fd)
