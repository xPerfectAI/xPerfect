"""Crash-recoverable native credential copy/refresh under an existing account lease.

The caller owns durable lease admission and blocks all member execution while an
unfinished projection exists. This module never grants authority or releases a lease.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from typing import Callable
from threading import local

from .provider_credential_artifacts import CredentialArtifact


class CredentialProjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectionBinding:
    tenant_id: str
    owner_id: str
    account_id: str
    lease_id: str
    worker_id: str
    workspace_id: str
    run_id: str
    attempt_id: str
    container_id: str
    member_uid: int

    def __post_init__(self):
        values = asdict(self)
        if any(not isinstance(value, str) or not value or '\0' in value
               for key, value in values.items() if key != 'member_uid'):
            raise ValueError('Exact credential projection identity is required')
        if not 20001 <= self.member_uid <= 60000:
            raise ValueError('Invalid native member identity')


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()




class CredentialProjection:
    def __init__(self, *, account_root: Path, member_home: Path, receipt: Path,
                 binding: ProjectionBinding, artifacts: tuple[CredentialArtifact, ...],
                 assert_lease: Callable[[], None], stop_member: Callable[[], None]):
        self.account_root = Path(account_root)
        self.member_home = Path(member_home)
        self.receipt = Path(receipt)
        self.binding = binding
        self.artifacts = artifacts
        self.assert_lease = assert_lease
        self.stop_member = stop_member
        self._pin_state = local()
        if (self.account_root == self.member_home or self.account_root in self.member_home.parents
                or self.member_home in self.account_root.parents
                or self.account_root in self.receipt.parents or self.member_home in self.receipt.parents):
            raise CredentialProjectionError('Account, member and control roots must be separate')
        parent = self.receipt.parent
        if parent != parent.resolve(strict=True) or parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o077:
            raise CredentialProjectionError('Projection receipt requires private control storage')
        if not artifacts or len({item.account_path for item in artifacts}) != len(artifacts):
            raise ValueError('Distinct adapter credential artifacts are required')
        if len({item.member_path for item in artifacts}) != len(artifacts):
            raise ValueError('Distinct private artifact destinations are required')

    @property
    def _directory_fds(self) -> dict[Path, int] | None:
        return getattr(self._pin_state, "directories", None)

    @_directory_fds.setter
    def _directory_fds(self, value: dict[Path, int] | None) -> None:
        self._pin_state.directories = value

    @staticmethod
    def _open_directory(path: Path) -> int:
        if not path.is_absolute() or '..' in path.parts:
            raise CredentialProjectionError('Credential directory path is invalid')
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor); descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _relative(relative: str) -> PurePosixPath:
        value = PurePosixPath(relative)
        if value.is_absolute() or '..' in value.parts or str(value) in {'', '.'}:
            raise CredentialProjectionError('Credential artifact path is invalid')
        return value

    @contextmanager
    def _pinned(self):
        if self._directory_fds is not None:
            yield
            return
        handles = {}
        entered = False
        try:
            for root in (self.account_root, self.member_home, self.receipt.parent):
                if root in handles: continue
                fd = self._open_directory(root)
                handles[root] = fd
                info = os.fstat(fd)
                forbidden = 0o077 if root == self.receipt.parent else 0o007
                if info.st_uid != os.geteuid() or info.st_mode & forbidden:
                    raise CredentialProjectionError('Credential root is not a real private directory')
            for artifact in self.artifacts:
                for root, relative in ((self.account_root, artifact.account_path), (self.member_home, artifact.member_path)):
                    current = root
                    for part in self._relative(relative).parts[:-1]:
                        child = current / part
                        if child not in handles:
                            if root == self.member_home:
                                try: os.mkdir(part, mode=0o700, dir_fd=handles[current])
                                except FileExistsError: pass
                            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[current])
                            handles[child] = fd
                            info = os.fstat(fd)
                            owners = {os.geteuid(), self.binding.member_uid} if root == self.member_home else {os.geteuid()}
                            if info.st_uid not in owners or info.st_mode & 0o007:
                                raise CredentialProjectionError('Credential artifact parent is not private')
                        current = child
            self._directory_fds = handles
            entered = True
            yield
        except OSError as exc:
            if entered:
                raise
            raise CredentialProjectionError('Credential directory is unavailable or replaced') from exc
        finally:
            self._directory_fds = None
            for fd in handles.values(): os.close(fd)

    def _identities(self) -> dict:
        return {str(path): [os.fstat(fd).st_dev, os.fstat(fd).st_ino]
                for path, fd in self._directory_fds.items()}

    def _verify_directory_names(self) -> None:
        # Operations use held descriptors. This check detects detachment for quarantine;
        # it does not authorize a subsequent path-based write.
        for path, fd in self._directory_fds.items():
            current = self._open_directory(path)
            try:
                expected, actual = os.fstat(fd), os.fstat(current)
                if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                    raise CredentialProjectionError('Credential directory identity changed')
            finally:
                os.close(current)

    def _path(self, root: Path, relative: str, *, create=False) -> Path:
        del create
        value = self._relative(relative)
        with self._pinned():
            path = root / value
            if path.parent not in self._directory_fds:
                raise CredentialProjectionError('Credential artifact parent is not declared')
            return path

    def _exists(self, path: Path) -> bool:
        with self._pinned():
            try: os.stat(path.name, dir_fd=self._directory_fds[path.parent], follow_symlinks=False)
            except FileNotFoundError: return False
            return True

    def _unlink(self, path: Path) -> None:
        with self._pinned():
            fd = self._directory_fds[path.parent]
            os.unlink(path.name, dir_fd=fd)
            os.fsync(fd)

    def _read(self, path: Path, *, native: bool, limit: int) -> bytes:
        with self._pinned():
            return self._read_pinned(path, native=native, limit=limit)

    def _read_pinned(self, path: Path, *, native: bool, limit: int) -> bytes:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory_fds[path.parent])
            with os.fdopen(descriptor, 'rb') as handle:
                before = os.fstat(handle.fileno())
                allowed_owners = {os.geteuid(), self.binding.member_uid} if native else {os.geteuid()}
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_uid not in allowed_owners or before.st_mode & 0o007
                        or before.st_size > limit):
                    raise CredentialProjectionError('Credential artifact ownership or type is invalid')
                content = handle.read(limit + 1)
                after = os.fstat(handle.fileno())
                if (len(content) > limit or not content
                        or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise CredentialProjectionError('Credential artifact changed while reading')
                return content
        except OSError as exc:
            raise CredentialProjectionError('Credential artifact is unavailable') from exc

    def _atomic(self, path: Path, content: bytes):
        with self._pinned():
            parent = self._directory_fds[path.parent]
            temporary = '.xperfect-credential-' + secrets.token_hex(16)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(content); handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                self._verify_directory_names()
            finally:
                try: os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError: pass

    def _save(self, value):
        with self._pinned():
            self._verify_directory_names()
            value.setdefault("directory_identities", self._identities())
            self._atomic(self.receipt, json.dumps(value, sort_keys=True).encode())

    def _load(self):
        with self._pinned():
            value = json.loads(self._read(self.receipt, native=False, limit=65536))
            if value.get("directory_identities") != self._identities():
                raise CredentialProjectionError("Credential directory binding changed")
        if value.get('binding') != asdict(self.binding):
            raise CredentialProjectionError('Credential projection identity changed')
        expected = [asdict(item) for item in self.artifacts]
        if value.get('artifacts') != expected:
            raise CredentialProjectionError('Credential artifact contract changed')
        return value

    def prepare(self):
        with self._pinned():
            self._prepare_pinned()

    def _prepare_pinned(self):
        self.assert_lease()
        self.stop_member()
        self.assert_lease()
        if self._exists(self.receipt):
            raise CredentialProjectionError('An unfinished credential projection requires recovery')
        # Check all artifacts and destinations before making any credential visible.
        contents = []
        for item in self.artifacts:
            source = self._path(self.account_root, item.account_path)
            target = self._path(self.member_home, item.member_path, create=True)
            if self._exists(target):
                raise CredentialProjectionError('Private member credential destination already exists')
            contents.append(self._read(source, native=False, limit=item.max_bytes))
        value = {'binding': asdict(self.binding), 'artifacts': [asdict(item) for item in self.artifacts],
                 'phase': 'preparing', 'initial_hashes': [_digest(content) for content in contents],
                 'refresh_hashes': None}
        self._save(value)
        for item, content in zip(self.artifacts, contents):
            self.assert_lease()
            self._atomic(self._path(self.member_home, item.member_path), content)
        value['phase'] = 'active'
        self._save(value)

    def finish(self):
        with self._pinned():
            self._finish_pinned()

    def _finish_pinned(self):
        value = self._load()
        self.assert_lease()
        # This must prove the exact container/member has no surviving processes.
        # Caller keeps its lease and quarantine until this and all persistence succeed.
        self.stop_member()
        self.assert_lease()
        if value['phase'] == 'preparing':
            # No native launch was authorized; partial projection is cleanup only.
            contents = None
        elif value['phase'] in {'active', 'refreshing'}:
            contents = [self._read(self._path(self.member_home, item.member_path), native=True,
                                   limit=item.max_bytes) for item in self.artifacts]
            hashes = [_digest(content) for content in contents]
            if value['phase'] == 'refreshing' and hashes != value['refresh_hashes']:
                raise CredentialProjectionError('Refreshed credential changed during recovery')
            for index, item in enumerate(self.artifacts):
                current = _digest(self._read(self._path(self.account_root, item.account_path),
                                              native=False, limit=item.max_bytes))
                allowed = {value['initial_hashes'][index]}
                if value['phase'] == 'refreshing' and item.refresh:
                    allowed.add(hashes[index])
                if current not in allowed:
                    raise CredentialProjectionError('Canonical credential revision changed')
                if not item.refresh and hashes[index] != value['initial_hashes'][index]:
                    raise CredentialProjectionError('Immutable projected credential changed')
            value['phase'] = 'refreshing'
            value['refresh_hashes'] = hashes
            self._save(value)
            for index, (item, content) in enumerate(zip(self.artifacts, contents)):
                self.assert_lease()
                if item.refresh:
                    target = self._path(self.account_root, item.account_path)
                    current = _digest(self._read(target, native=False, limit=item.max_bytes))
                    if current not in {value['initial_hashes'][index], hashes[index]}:
                        raise CredentialProjectionError('Canonical credential changed before persistence')
                    if current != hashes[index]:
                        self._atomic(target, content)
        elif value['phase'] not in {'cleanup', 'complete'}:
            raise CredentialProjectionError('Credential recovery phase is invalid')
        self.assert_lease()
        value['phase'] = 'cleanup'
        self._save(value)
        for item in self.artifacts:
            target = self._path(self.member_home, item.member_path)
            if self._exists(target):
                self._read(target, native=True, limit=item.max_bytes)
                self._unlink(target)
        value['phase'] = 'complete'
        self._save(value)
