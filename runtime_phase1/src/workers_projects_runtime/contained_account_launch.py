"""Durable account containment; a CLI transport PID is never native authority."""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import uuid
from typing import Protocol

from .control_plane import ControlPlaneError


class AccountContainmentUncertain(ControlPlaneError):
    pass


@dataclass(frozen=True)
class AccountGeneration:
    account_home: Path
    lease_id: str
    generation: str
    uid: int
    device: int
    inode: int
    container_id: str = ''
    substrate_id: str = ''

    @property
    def name(self) -> str:
        return 'xperfect-account-' + self.generation


@dataclass(frozen=True)
class AccountStopReceipt:
    lease_id: str
    generation: str
    container_id: str
    all_children_absent: bool
    ownership_reclaimed: bool


class AccountContainerBackend(Protocol):
    """Trusted dedicated-container adapter, never an unrestricted subprocess shim.

    Start must use generation.name even before its ID can be persisted. Recovery
    can therefore resolve a create-response/controller crash without guessing.
    finish must validate the exact identity, stop/remove the whole container,
    prove absence through a successful daemon inventory, then reclaim its tree.
    """
    def descriptor(self, generation: AccountGeneration): ...
    def identity(self) -> str: ...
    def start(self, request, generation: AccountGeneration, **options): ...
    def finish(self, generation: AccountGeneration) -> AccountStopReceipt: ...


class AccountLaunchLedger:
    def __init__(self, control_root: Path):
        self.root = Path(control_root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if self.root.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ControlPlaneError('Account launch control storage must be private')
        self.path = self.root / 'account-launches.sqlite3'
        if self.path.is_symlink():
            raise ControlPlaneError('Account launch ledger is unsafe')
        with self.connect() as connection:
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS account_uids(home TEXT PRIMARY KEY, uid INTEGER UNIQUE NOT NULL);
              CREATE TABLE IF NOT EXISTS launches(home TEXT PRIMARY KEY, lease TEXT NOT NULL,
                generation TEXT UNIQUE NOT NULL, uid INTEGER NOT NULL, device INTEGER NOT NULL,
                inode INTEGER NOT NULL, container TEXT NOT NULL DEFAULT '', substrate TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS completions(generation TEXT PRIMARY KEY, home TEXT NOT NULL,
                lease TEXT NOT NULL, container TEXT NOT NULL, substrate TEXT NOT NULL, uid INTEGER NOT NULL, stopped_at REAL NOT NULL);
            ''')
        self.path.chmod(0o600)
        for path in (self.root, self.root.parent):
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)

    def acquire(self, home):
        path = self.root / (hashlib.sha256(str(home).encode()).hexdigest() + '.lock')
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise AccountContainmentUncertain('Another controller owns this account launch') from None

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA synchronous=FULL')
        return connection

    @staticmethod
    def _value(row):
        return AccountGeneration(Path(row['home']), row['lease'], row['generation'], row['uid'],
                                 row['device'], row['inode'], row['container'], row['substrate'])

    def active_generations(self) -> tuple[AccountGeneration, ...]:
        """Includes uncertain starts for conservative packaged capacity accounting."""
        with self.connect() as connection:
            return tuple(self._value(row) for row in connection.execute('SELECT * FROM launches ORDER BY home'))

    def pending(self, home: Path):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM launches WHERE home=?', (str(home),)).fetchone()
        return self._value(row) if row else None

    def reserve(self, request, *, substrate_id: str):
        home = request.account_home
        if not substrate_id:
            raise ControlPlaneError('Account substrate identity is unavailable')
        if not request.lease_id or home != home.resolve(strict=True):
            raise ControlPlaneError('Contained account launch requires its exact lease and real home')
        info = home.stat()
        if self.root == home or self.root.is_relative_to(home):
            raise ControlPlaneError('Account control state cannot be inside the native account')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM launches WHERE home=?', (str(home),)).fetchone():
                raise AccountContainmentUncertain('Account launch recovery is required before reuse')
            row = connection.execute('SELECT uid FROM account_uids WHERE home=?', (str(home),)).fetchone()
            uid = row['uid'] if row else connection.execute('SELECT COALESCE(MAX(uid),100000)+1 FROM account_uids').fetchone()[0]
            if not 100001 <= uid <= 200000:
                raise ControlPlaneError('Account native identity capacity is exhausted')
            if not row:
                connection.execute('INSERT INTO account_uids VALUES (?,?)', (str(home), uid))
            value = AccountGeneration(home, request.lease_id, uuid.uuid4().hex, uid, info.st_dev, info.st_ino, substrate_id=substrate_id)
            connection.execute('INSERT INTO launches VALUES (?,?,?,?,?,?,?,?)',
                               (str(home), value.lease_id, value.generation, uid, info.st_dev, info.st_ino, '', substrate_id))
        return value

    def attach(self, value, container_id):
        if len(container_id) != 64 or any(c not in '0123456789abcdef' for c in container_id):
            raise AccountContainmentUncertain('Account container identity is invalid')
        with self.connect() as connection:
            changed = connection.execute('UPDATE launches SET container=? WHERE home=? AND lease=? AND generation=? AND container=?',
                (container_id, str(value.account_home), value.lease_id, value.generation, '')).rowcount
            if changed != 1:
                raise AccountContainmentUncertain('Account launch generation changed')
        return AccountGeneration(value.account_home, value.lease_id, value.generation, value.uid,
                                 value.device, value.inode, container_id, value.substrate_id)

    def complete(self, value, receipt):
        if (not isinstance(receipt, AccountStopReceipt) or receipt.lease_id != value.lease_id or receipt.generation != value.generation
                or (value.container_id and receipt.container_id != value.container_id)
                or receipt.all_children_absent is not True or receipt.ownership_reclaimed is not True):
            raise AccountContainmentUncertain('Exact account container absence and reclaim are required')
        info = value.account_home.stat()
        if (info.st_dev, info.st_ino) != (value.device, value.inode):
            raise AccountContainmentUncertain('Account home identity changed during containment')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            changed = connection.execute('DELETE FROM launches WHERE home=? AND lease=? AND generation=? AND container=?',
                (str(value.account_home), value.lease_id, value.generation, value.container_id)).rowcount
            if changed != 1:
                raise AccountContainmentUncertain('Account launch completion fence changed')
            connection.execute('INSERT INTO completions VALUES (?,?,?,?,?,?,?)',
                (value.generation, str(value.account_home), value.lease_id, receipt.container_id,
                 value.substrate_id, value.uid, time.time()))


class ContainedAccountProcess:
    """Popen-like I/O transport with an explicit, durable containment lifetime."""
    def __init__(self, launcher, generation, transport, lock_fd):
        self.launcher, self.generation, self.transport = launcher, generation, transport
        self._lock = threading.RLock()
        self._receipt = None
        self._lock_fd = lock_fd

    def __del__(self):
        # Controller object loss releases only the coordination lock. The durable
        # pending row still denies reuse until explicit container recovery.
        descriptor = getattr(self, '_lock_fd', -1)
        if descriptor >= 0:
            try: os.close(descriptor)
            except OSError: pass
            self._lock_fd = -1

    def poll(self):
        return self.transport.poll()

    def communicate(self, *args, **kwargs):
        return self.transport.communicate(*args, **kwargs)

    @property
    def returncode(self):
        return self.transport.returncode

    def stop_and_confirm(self):
        with self._lock:
            if self._receipt is None:
                self._receipt = self.launcher.finish(self.generation)
                # Native absence is already established independently. Reap only
                # this owned local Docker transport; its PID is never native proof.
                try:
                    try:
                        self.transport.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.transport.kill()
                        self.transport.wait(timeout=2)
                finally:
                    os.close(self._lock_fd)
                    self._lock_fd = -1
            return self._receipt


class ContainedAccountLauncher:
    def __init__(self, *, control_root: Path, backend: AccountContainerBackend):
        self.ledger = AccountLaunchLedger(control_root)
        self.backend = backend

    def inventory(self):
        """Typed exact-container descriptors, including pending/recovery reservations.

        The resource owner independently inspects declared IDs for live process/
        thread counts. A missing ID is a pending reservation, never zero capacity.
        """
        return tuple(self.backend.descriptor(value) for value in self.ledger.active_generations())

    def assert_quiescent(self, account_home: Path):
        if self.ledger.pending(account_home) is not None:
            raise AccountContainmentUncertain('Account container is active or awaits recovery')

    def finish(self, generation):
        if self.backend.identity() != generation.substrate_id:
            raise AccountContainmentUncertain('Account recovery substrate changed')
        receipt = self.backend.finish(generation)
        self.ledger.complete(generation, receipt)
        return receipt

    def recover(self, account_home: Path, *, lease_id: str):
        lock_fd = self.ledger.acquire(account_home)
        try:
            value = self.ledger.pending(account_home)
            if value is None:
                return
            if value.lease_id != lease_id:
                raise AccountContainmentUncertain('Account recovery requires its exact pending lease')
            return self.finish(value)
        finally:
            os.close(lock_fd)

    def popen(self, request, **options):
        lock_fd = self.ledger.acquire(request.account_home)
        generation = None
        try:
            generation = self.ledger.reserve(request, substrate_id=self.backend.identity())
            transport, container_id = self.backend.start(request, generation, **options)
            generation = self.ledger.attach(generation, container_id)
            return ContainedAccountProcess(self, generation, transport, lock_fd)
        except BaseException:
            try:
                if generation is not None:
                    # A failed response can follow successful creation. Exact
                    # precommitted name recovery is required before row removal.
                    self.finish(generation)
            finally:
                os.close(lock_fd)
            raise

    def run(self, request, **options):
        timeout = options.pop('timeout', None)
        check = options.pop('check', False)
        input_value = options.pop('input', None)
        if options.pop('capture_output', False):
            if 'stdout' in options or 'stderr' in options:
                raise ValueError('capture_output conflicts with explicit streams')
            options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if input_value is not None:
            options['stdin'] = subprocess.PIPE
        process = self.popen(request, **options)
        try:
            stdout, stderr = process.communicate(input=input_value, timeout=timeout)
            result = subprocess.CompletedProcess(request.command, process.returncode, stdout, stderr)
        finally:
            process.stop_and_confirm()
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, request.command, result.stdout, result.stderr)
        return result
