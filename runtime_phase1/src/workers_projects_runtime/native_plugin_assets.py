"""Private copies of already-selected native Codex package assets.

Selection and active-version semantics remain Codex's. This is filesystem projection,
not installation, enablement, connector authorization, or mutable plugin data sync.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
from functools import cmp_to_key, lru_cache
from pathlib import Path


class NativeAssetError(ValueError):
    pass


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*")
_VERSION = re.compile(r"[A-Za-z0-9_.+-]+")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([A-Za-z0-9.-]+))?(?:\+([A-Za-z0-9.-]+))?")


def source_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def source_cache() -> Path:
    value = os.environ.get("GLASSHIVE_HOST_CODEX_PLUGIN_CACHE") or os.environ.get("WPR_HOST_CODEX_PLUGIN_CACHE")
    return Path(value).expanduser() if value else source_home() / "plugins/cache"


def _checked(path: Path, *, directory=False, private=False):
    info = path.lstat()
    if (not path.is_absolute() or path.resolve(strict=True) != path or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or (private and info.st_mode & 0o077)
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise NativeAssetError("Native package storage is not an owned canonical asset path")
    return info


def _checked_chain(root: Path, path: Path, *, private=False):
    relative = path.relative_to(root)
    _checked(root, directory=True, private=private)
    current = root
    for part in relative.parts:
        current /= part
        _checked(current, directory=True, private=private)


def _semver(value):
    match = _SEMVER.fullmatch(value)
    if not match:
        return None
    core = tuple(map(int, match.group(1, 2, 3)))
    if any(part > 2**64 - 1 for part in core):
        return None
    pre, build = match[4], match[5]
    if build and any(not item for item in build.split('.')):
        return None
    if pre and any(not item or (item.isdigit() and len(item) > 1 and item[0] == "0") for item in pre.split('.')):
        return None
    return core, pre, build


def _identifiers(left, right, *, build=False):
    a, b = left.split('.') if left else [], right.split('.') if right else []
    for x, y in zip(a, b):
        if x == y:
            continue
        if x.isdigit() and y.isdigit():
            if int(x) != int(y):
                return (int(x) > int(y)) - (int(x) < int(y))
            if build:
                return (len(x) > len(y)) - (len(x) < len(y))
        if x.isdigit() != y.isdigit():
            return -1 if x.isdigit() else 1
        return (x > y) - (x < y)
    return (len(a) > len(b)) - (len(a) < len(b))


def _compare(left, right):
    # Native installed.rs uses Rust semver::Version::cmp (total ordering),
    # including build metadata; invalid semver uses lexical comparison.
    a, b = _semver(left), _semver(right)
    if a is None or b is None:
        return (left > right) - (left < right)
    if a[0] != b[0]:
        return (a[0] > b[0]) - (a[0] < b[0])
    if a[1] != b[1]:
        if a[1] is None or b[1] is None:
            return 1 if a[1] is None else -1
        pre = _identifiers(a[1], b[1])
        if pre:
            return pre
    return _identifiers(a[2], b[2], build=True)


def active_version(base: Path) -> str | None:
    if not base.exists():
        return None
    _checked(base, directory=True)
    versions = [entry.name for entry in os.scandir(base)
                if entry.is_dir(follow_symlinks=False) and entry.name not in {'.', '..'}
                and _VERSION.fullmatch(entry.name)]
    if "local" in versions:
        return "local"
    ordered = sorted(versions, key=cmp_to_key(_compare))
    return ordered[-1] if ordered else None


def selected_plugins(config: dict) -> set[str]:
    if (config.get("features") or {}).get("plugins") is False:
        return set()
    return {name for name, entry in (config.get("plugins") or {}).items()
            if _ID.fullmatch(name) and isinstance(entry, dict) and entry.get("enabled", True) is True}


def _package(cache: Path, identity: str, version: str | None = None) -> tuple[Path, str]:
    if not _ID.fullmatch(identity):
        raise NativeAssetError("Invalid native plugin identity")
    name, market = identity.split('@')
    base = cache / market / name
    _checked_chain(cache, base)
    version = version or active_version(base)
    if not version or not _VERSION.fullmatch(version) or version in {'.', '..'}:
        raise NativeAssetError("The selected native plugin has no installed version")
    package = base / version
    _checked(package, directory=True)
    manifests = [package / '.codex-plugin/plugin.json', package / '.claude-plugin/plugin.json']
    manifest = next((path for path in manifests if path.is_file()), None)
    if manifest is None:
        raise NativeAssetError("The selected native package has no manifest")
    _checked_chain(package, manifest.parent)
    _checked(manifest)
    if json.loads(manifest.read_text()).get('name') != name:
        raise NativeAssetError("Native package identity does not match its manifest")
    return package, version


def _file_bytes(path: Path):
    before = _checked(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise NativeAssetError("Native package changed during validation")
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read()
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise NativeAssetError("Native package changed during validation")
        return data, 0o700 if opened.st_mode & 0o111 else 0o600
    finally:
        os.close(descriptor)


@lru_cache(maxsize=8192)
def _cached_digest(path: str, identity: tuple):
    data, mode = _file_bytes(Path(path))
    after = _checked(Path(path))
    observed = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink)
    if observed != identity:
        raise NativeAssetError("Native package changed during hashing")
    return hashlib.sha256(data).digest(), mode


def _digest(path: Path):
    info = _checked(path)
    return _cached_digest(str(path), (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                          info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink))


def _inventory(root: Path, *, private=False) -> dict[Path, tuple[bytes, int]]:
    _checked(root, directory=True, private=private)
    result = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        _checked_chain(root, parent, private=private)
        for name in directories:
            _checked(parent / name, directory=True, private=private)
        for name in files:
            path = parent / name
            _checked(path, private=private)
            result[path.relative_to(root)] = _digest(path)
    return result


def _ensure_private_path(root: Path, path: Path):
    _checked(root, directory=True, private=True)
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        _checked(current, directory=True, private=True)


def project_selected(config: dict, account: Path, *, owner_home: Path, cache: Path):
    selected = selected_plugins(config)
    if not selected:
        return []
    _checked(owner_home, directory=True)
    _checked(owner_home / 'config.toml')
    if cache.is_relative_to(owner_home):
        _checked_chain(owner_home, cache)
    else:
        _checked(cache.parent, directory=True)
        _checked(cache, directory=True)
    owner_selection = selected_plugins(tomllib.loads((owner_home / 'config.toml').read_text()))
    if not selected <= owner_selection:
        raise NativeAssetError("A new native plugin selection needs its native approval first")
    projected = []
    for identity in sorted(selected):
        source, version = _package(cache, identity)
        name, market = identity.split('@')
        target_base = account / 'plugins/cache' / market / name
        _ensure_private_path(account, target_base.parent)
        if target_base.exists() or target_base.is_symlink():
            _checked(target_base, directory=True, private=True)
            current = active_version(target_base)
            # A local or newer cached version can override the exact admitted one.
            if (current and _compare(current, version) > 0) or (current == 'local' and version != 'local'):
                raise NativeAssetError("A different native plugin version would override the selected version")
        source_files = _inventory(source)
        target = target_base / version
        if target.exists() or target.is_symlink():
            _checked_chain(account, target, private=True)
            existing = _inventory(target, private=True)
            if set(existing) != set(source_files) or any(existing[path] != values for path, values in source_files.items()):
                raise NativeAssetError("A native plugin package conflicts with the selected source assets")
        else:
            stage = Path(tempfile.mkdtemp(prefix='.xperfect-native-assets-', dir=target_base.parent))
            try:
                package = stage / name / version
                _ensure_private_path(stage, package)
                for path, (digest, mode) in source_files.items():
                    data, actual_mode = _file_bytes(source / path)
                    if hashlib.sha256(data).digest() != digest or actual_mode != mode:
                        raise NativeAssetError("Native plugin assets changed during copying")
                    destination = package / path
                    _ensure_private_path(package, destination.parent)
                    with destination.open('xb') as stream:
                        stream.write(data)
                    destination.chmod(mode)
                # Revalidate source and candidate before one atomic package publication.
                if _inventory(source) != source_files or _inventory(package, private=True) != source_files:
                    raise NativeAssetError("Native plugin assets changed during projection")
                if active_version(source.parent) != version:
                    raise NativeAssetError("Native plugin selection changed during projection")
                if target_base.exists():
                    os.rename(package, target)
                else:
                    os.rename(package.parent, target_base)
            finally:
                shutil.rmtree(stage)
        if active_version(target_base) != version:
            raise NativeAssetError("The account would load a different native plugin version")
        projected.append({'plugin': identity, 'version': version})
    return projected


class AccountAssets:
    """Reconstruct native immutable asset trust without a transient worker or receipt."""
    def __init__(self, account_home: Path):
        self.root = account_home
        self.cache = source_cache()
        self.packages = {}

    def file_mode(self, directory_fd: int, name: str, relative: tuple[str, ...], entry) -> int | None:
        if relative[:3] != ('codex', 'plugins', 'cache'):
            return None
        if len(relative) < 6:
            return None  # Native install metadata is private data, never executable code.
        market, plugin, version = relative[3:6]
        identity = plugin + '@' + market
        key = (identity, version)
        if key not in self.packages:
            owner = source_home()
            if self.cache.is_relative_to(owner):
                _checked_chain(owner, self.cache)
            else:
                _checked(self.cache.parent, directory=True)
                _checked(self.cache, directory=True)
            source, _ = _package(self.cache, identity, version)
            _inventory(source)  # Reject hidden source links/special files anywhere in this package.
            source_active = active_version(source.parent)
            target_active = active_version(self.root / 'codex/plugins/cache' / market / plugin)
            if source_active != target_active:
                raise NativeAssetError("Native account package version conflicts with its canonical source")
            self.packages[key] = source
        path = self.packages[key].joinpath(*relative[6:], name)
        _checked_chain(self.packages[key], path.parent)
        digest, mode = _digest(path)
        descriptor = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                raise NativeAssetError("Native account asset changed during validation")
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                actual = stream.read()
            if hashlib.sha256(actual).digest() != digest:
                raise NativeAssetError("Native account asset differs from its canonical code package")
        finally:
            os.close(descriptor)
        return mode
