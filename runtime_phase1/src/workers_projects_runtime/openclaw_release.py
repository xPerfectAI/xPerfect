from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping, Sequence


OPENCLAW_RUNTIME_VERSION = "2026.7.1-2"
OPENCLAW_RUNTIME_TARBALL = (
    "https://registry.npmjs.org/openclaw/-/openclaw-2026.7.1-2.tgz"
)
OPENCLAW_RUNTIME_INTEGRITY = (
    "sha512-ycF3yPcbjN6bUPeaUx6Mh6vze1hQWoD3CT/wWcmD7a8xaHHHRUaAlaq+lFxMHf1ssEgODVAwjlzYqp2twkYZ7g=="
)
OPENCLAW_RUNTIME_LOCK_SHA256 = (
    "b0cdcd1f4d842bebb20967dbdfb154d3725dabb510962240171a2cca67da4fde"
)
OPENCLAW_RUNTIME_PACKAGE_SHA256 = (
    "9d5518aafdafeb78e060eeb433f4b00019fccb91c94e62e3a88a0031895a3577"
)
OPENCLAW_RUNTIME_OVERRIDES = {
    "@hono/node-server": "2.0.12",
    "@modelcontextprotocol/sdk": "1.30.0",
    "brace-expansion": "5.0.9",
    "fast-uri": "3.1.4",
    "tar": "7.5.22",
}
OPENCLAW_RUNTIME_FAST_URI_VERSION = OPENCLAW_RUNTIME_OVERRIDES["fast-uri"]
OPENCLAW_RUNTIME_LOCK_DIR = Path(__file__).resolve().parents[2] / "runtime_locks" / "openclaw"


def reviewed_openclaw_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    reviewed = dict(env or {})
    # Loopback gateway binding does not suppress native mDNS advertising.
    reviewed["OPENCLAW_DISABLE_BONJOUR"] = "1"
    return reviewed


def require_reviewed_openclaw_version(reported: str) -> str:
    fields = str(reported or "").strip().split()
    actual = fields[1] if len(fields) >= 2 and fields[0].lower() == "openclaw" else (fields[0] if fields else "")
    if actual != OPENCLAW_RUNTIME_VERSION:
        shown = actual or "unverified"
        raise RuntimeError(
            f"GlassHive requires OpenClaw {OPENCLAW_RUNTIME_VERSION}; configured runtime reported {shown}."
        )
    return actual


def verify_reviewed_openclaw_binary(
    command: str | Sequence[str],
    *,
    timeout_seconds: float = 10,
) -> str:
    argv = shlex.split(command) if isinstance(command, str) else [str(part) for part in command]
    if not argv:
        raise RuntimeError(f"GlassHive requires OpenClaw {OPENCLAW_RUNTIME_VERSION}; no runtime command was configured.")
    try:
        result = subprocess.run(
            [*argv, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"GlassHive requires OpenClaw {OPENCLAW_RUNTIME_VERSION}; the configured runtime version could not be verified."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"GlassHive requires OpenClaw {OPENCLAW_RUNTIME_VERSION}; the configured runtime version could not be verified."
        )
    return require_reviewed_openclaw_version(result.stdout or result.stderr)


def validate_reviewed_openclaw_lock(lock_root: Path | None = None) -> None:
    lock_root = lock_root or OPENCLAW_RUNTIME_LOCK_DIR
    package_path = lock_root / "package.json"
    lock_path = lock_root / "package-lock.json"
    try:
        package_bytes = package_path.read_bytes()
        lock_bytes = lock_path.read_bytes()
        package = json.loads(package_bytes)
        lock = json.loads(lock_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenClaw runtime lock is missing or unreadable; refusing an unreviewed install.") from exc

    package_sha = hashlib.sha256(package_bytes).hexdigest()
    lock_sha = hashlib.sha256(lock_bytes).hexdigest()
    packages = lock.get("packages", {})
    openclaw = packages.get("node_modules/openclaw", {})
    installed_override_versions = {
        name: {
            package.get("version")
            for path, package in packages.items()
            if path.endswith(f"node_modules/{name}") and isinstance(package, dict)
        }
        for name in OPENCLAW_RUNTIME_OVERRIDES
    }
    if not (
        package_sha == OPENCLAW_RUNTIME_PACKAGE_SHA256
        and lock_sha == OPENCLAW_RUNTIME_LOCK_SHA256
        and package.get("dependencies", {}).get("openclaw") == OPENCLAW_RUNTIME_TARBALL
        and package.get("overrides") == OPENCLAW_RUNTIME_OVERRIDES
        and openclaw.get("version") == OPENCLAW_RUNTIME_VERSION
        and openclaw.get("integrity") == OPENCLAW_RUNTIME_INTEGRITY
        and all(
            installed_override_versions[name] == {version}
            for name, version in OPENCLAW_RUNTIME_OVERRIDES.items()
        )
    ):
        raise RuntimeError("OpenClaw runtime lock does not match the reviewed release; refusing an unreviewed install.")


def stage_reviewed_openclaw_lock(destination: Path) -> None:
    lock_root = OPENCLAW_RUNTIME_LOCK_DIR
    validate_reviewed_openclaw_lock(lock_root)
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        target = destination / name
        with tempfile.NamedTemporaryFile(
            dir=destination,
            prefix=f".{name}.",
            delete=False,
        ) as handle:
            staged = Path(handle.name)
        try:
            # The reviewed source is intentionally read-only in sealed releases. Replacing a
            # same-directory temporary file keeps retries idempotent without making prior output
            # writable or exposing a partially copied build input.
            shutil.copy2(lock_root / name, staged)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
