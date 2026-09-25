"""Supported package upgrade: exact shape, idle proof, state preservation, rollback, commit.

A stateful fake Docker builds containers from the launcher's real create arguments,
keeps named volumes as byte maps and runs the upgrade helpers' copy/restore on them.
It proves the launcher's transaction logic, not native or hosted acceptance.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deployment.linux import launch as linux_launch  # noqa: E402
from deployment.linux import upgrade as linux_upgrade  # noqa: E402

OLD, NEW, NATIVE = 'sha256:' + 'a' * 64, 'sha256:' + 'e' * 64, 'sha256:' + 'b' * 64
NAME = 'xperfect-fixture'
IDLE_ERROR = b'Traceback (most recent call last):\n  ...\nValueError: GlassHive active work must be quiesced before continuity\n'
SCHEMA_ERROR = b'Traceback (most recent call last):\n  ...\nValueError: GlassHive continuity contains an unreviewed schema shape\n'


def _tar(name: str, payload: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        item = tarfile.TarInfo(name)
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))
    return output.getvalue()


def run_state_tool(program: str, argv: list[str], placed: dict[str, dict[str, bytes]]):
    """Run the real upgrade STATE_TOOL on byte-map volumes laid out on disk, then read them back."""
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        for target, files in placed.items():
            folder = root / target.lstrip('/')
            folder.mkdir(parents=True, exist_ok=True)
            for relative, payload in files.items():
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
        marker = "STATE, BACKUP = Path('/state'), Path('/backup')"
        assert program.count(marker) == 1
        program = program.replace(marker, f"STATE, BACKUP = Path({str(root / 'state')!r}), Path({str(root / 'backup')!r})")
        result = subprocess.run([sys.executable, '-I', '-c', program, *argv], capture_output=True)
        for target, files in placed.items():
            folder = root / target.lstrip('/')
            files.clear()
            files.update({path.relative_to(folder).as_posix(): path.read_bytes()
                          for path in folder.rglob('*') if path.is_file()})
        return subprocess.CompletedProcess(argv, result.returncode, result.stdout, result.stderr)


class FakeDocker:
    def __init__(self):
        self.images = {OLD, NEW, NATIVE}
        self.volumes: dict[str, dict[str, bytes]] = {}
        self.networks: dict[str, dict] = {}
        self.containers: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.live_idle = True
        self.offline_idle = True
        # Whether each release's continuity can review the package's schema.
        self.running_reviews = True
        self.incoming_reviews = True
        self.incoming_blind = False  # the new release does not see the running release's work
        self.activity_predicate = True  # the running release has its own work predicate
        self.operating_system = 'Ubuntu 24.04 LTS'
        # The profile-settings table each image declares (None: an image without one).
        self.image_settings = {}
        self.busy_images: set[str] = set()
        self.proofs: list = []
        self.volume_labels: dict[str, dict] = {}
        self.unhealthy_images: set[str] = set()
        self.fail_start_once: set[str] = set()
        self.on_start = None
        self.on_stop = None
        self.fail_rm_once: set[str] = set()
        self.fail_calls_once: list[tuple] = []
        self.counter = 0
        # Stored-file attachments the running release registered but never published.
        self.unpublished: list[str] = []
        self.recovery_images = {NEW}  # images whose continuity carries the recovery
        self.unpublished_refusal: bytes | None = None
        self.recovery_report = None  # a replacement report, to test report validation
        self.recovery: list[tuple[str, str]] = []
        self.before_retire = None  # runs between the review and the retire step
        self.keygens: list[tuple] = []
        self.keygen_fails = False
        self.retire_crash = False  # the retire step stops without a typed result
        # Open runs and leases an earlier release left on closed workers, and the idle boxes they held.
        self.closed_work: list[dict] = []
        self.closed_boxes: list[str] = []
        self.closed_work_refusal: bytes | None = None
        self.closed_work_report = None
        self.settlement: list[tuple[str, str, bool]] = []

    # -- helpers ----------------------------------------------------------
    def find(self, ident):
        if ident in self.containers:
            return self.containers[ident]
        for record in self.containers.values():
            if record['Name'] == '/' + ident:
                return record
        return None

    def volume_at(self, record, target):
        for mount in record['HostConfig']['Mounts']:
            if mount['Target'] == target and mount['Type'] == 'volume':
                return self.volumes.setdefault(mount['Source'], {})
        raise KeyError(target)

    def _create(self, args):
        spec = {'labels': {}, 'mounts': [], 'caps': [], 'hosts': [], 'devices': [], 'bindings': {}}
        index = 0
        while index < len(args) and args[index].startswith('--'):
            flag = args[index]
            if flag in {'--read-only'}:
                index += 1
                continue
            value = args[index + 1]
            index += 2
            if flag == '--name':
                spec['name'] = value
            elif flag == '--label':
                key, _, item = value.partition('=')
                spec['labels'][key] = item
            elif flag == '--network':
                spec['network'] = value
            elif flag == '--network-alias':
                spec['alias'] = value
            elif flag == '--mount':
                fields = dict(item.split('=', 1) if '=' in item else (item, True) for item in value.split(','))
                mount = {'Type': fields['type'], 'Source': fields['src'], 'Target': fields['dst']}
                if fields['type'] == 'volume':
                    mount['VolumeOptions'] = {'NoCopy': bool(fields.get('volume-nocopy'))}
                spec['mounts'].append(mount)
            elif flag == '--tmpfs':
                spec['tmpfs'] = dict([value.split(':', 1)])
            elif flag == '--cap-add':
                spec['caps'].append('CAP_' + value)
            elif flag == '--add-host':
                spec['hosts'].append(value)
            elif flag == '--device':
                host, container, permissions = value.split(':')
                spec['devices'].append({'PathOnHost': host, 'PathInContainer': container, 'CgroupPermissions': permissions})
            elif flag == '--publish':
                address, port, target = value.split(':')
                spec['bindings'][target + '/tcp'] = [{'HostIp': address, 'HostPort': port}]
            elif flag == '--restart':
                spec['restart'] = value
            elif flag in {'--cap-drop', '--security-opt'}:
                spec.setdefault(flag, value)
        image, command = args[index], list(args[index + 1:])
        if spec['name'] in {record['Name'][1:] for record in self.containers.values()}:
            return subprocess.CompletedProcess(args, 1, b'', b'name in use')
        self.counter += 1
        identity = hashlib.sha256(f'container-{self.counter}'.encode()).hexdigest()
        self.containers[identity] = {
            'Id': identity, 'Name': '/' + spec['name'], 'Image': image, 'State': {'Running': False},
            'Config': {'Labels': spec['labels'], 'Cmd': command, 'Image': image},
            'HostConfig': {'ReadonlyRootfs': True, 'Privileged': False, 'CapDrop': ['ALL'], 'CapAdd': spec['caps'] or None,
                           'SecurityOpt': [spec['--security-opt']], 'Tmpfs': spec['tmpfs'], 'Mounts': spec['mounts'],
                           'NetworkMode': spec['network'], 'RestartPolicy': {'Name': spec.get('restart', 'no')},
                           'PortBindings': spec['bindings'], 'Devices': spec['devices'] or None,
                           'ExtraHosts': spec['hosts'] or None},
            'NetworkSettings': {'Networks': {spec['network']: {'Aliases': [spec['alias']]}}}}
        return subprocess.CompletedProcess(args, 0, identity.encode(), b'')

    PROGRAMS = {linux_upgrade.QUIESCENT: 'running', linux_upgrade.INCOMING_QUIESCENT: 'incoming',
                linux_upgrade.RUNNING_ACTIVITY: 'activity'}

    def proof(self, args, *, idle, kind):
        self.proofs.append((kind, args[args.index('--name') + 1].rsplit('-', 2)[-2:] if '--name' in args else 'exec'))
        fail = lambda error: subprocess.CompletedProcess(args, 1, b'', error)
        if kind == 'activity' and not self.activity_predicate:
            return fail(b"AttributeError: module has no attribute '_require_quiescent'\n")
        if kind != 'activity' and not (self.incoming_reviews if kind == 'incoming' else self.running_reviews):
            return fail(SCHEMA_ERROR)
        if self.unpublished:  # every release's continuity refuses a target that cannot settle
            return fail(b'ValueError: GlassHive Files projection must settle before continuity\n')
        if self.closed_work:  # a closed worker's open run looks like active work to every release
            return fail(b'ValueError: GlassHive active work must be quiesced before continuity\n')
        if kind == 'incoming' and self.incoming_blind:
            idle = True
        return subprocess.CompletedProcess(args, 0, b'', b'') if idle else fail(IDLE_ERROR)

    def unpublished_review(self, args, *, program):
        image = args[args.index('--entrypoint') + 2]
        apply = "'--apply'" in program
        self.recovery.append(('retire' if apply else 'review', image))
        if apply and self.before_retire:
            self.before_retire()
        if image not in self.recovery_images:
            return subprocess.CompletedProcess(args, 2, b'', b"native_continuity: error: argument operation: "
                                               b"invalid choice: 'reconcile-unpublished'\n")
        if self.unpublished_refusal:
            return subprocess.CompletedProcess(args, 1, b'', self.unpublished_refusal)
        if apply:
            expect = program.split("'--expect','", 1)[1].split("'", 1)[0].split(',')
            if sorted(expect) != sorted(self.unpublished):
                return subprocess.CompletedProcess(args, 1, b'', b'ValueError: GlassHive Files unpublished '
                                                   b'targets changed since they were reviewed\n')
        targets = [{'projection_id': item, 'upload_id': 'fil_' + item[4:], 'sha256': 'a' * 64, 'size_bytes': 3}
                   for item in self.unpublished]
        report = {'unsettled': len(targets), 'applied': bool(apply and targets), 'targets': targets}
        if apply:
            self.unpublished = []
            if self.retire_crash:
                return subprocess.CompletedProcess(args, 1, b'', b'Killed\n')
        if self.recovery_report is not None:
            report = self.recovery_report
        return subprocess.CompletedProcess(args, 0, json.dumps(report).encode(), b'')

    def closed_work_review(self, args, *, program):
        image = args[args.index('--entrypoint') + 2]
        apply = "'--apply'" in program
        socket = 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock' in args
        self.settlement.append(('settle' if apply else 'review', image, socket))
        if image not in self.recovery_images:
            return subprocess.CompletedProcess(args, 2, b'', b"native_continuity: error: argument operation: "
                                               b"invalid choice: 'reconcile-closed-work'\n")
        if self.closed_work_refusal:
            return subprocess.CompletedProcess(args, 1, b'', self.closed_work_refusal)
        identities = sorted([item['worker_id'] for item in self.closed_work]
                            + [value for item in self.closed_work for value in item['runs'] + item['leases']])
        if apply:
            expect = program.split("'--expect','", 1)[1].split("'", 1)[0].split(',')
            if sorted(expect) != identities:
                return subprocess.CompletedProcess(args, 1, b'', b'ValueError: GlassHive closed-worker work '
                                                   b'changed since it was reviewed\n')
        report = {'closed_workers': len(self.closed_work), 'applied': bool(apply and self.closed_work),
                  'targets': [dict(item) for item in self.closed_work],
                  'boxes_released': list(self.closed_boxes) if apply else []}
        if apply:
            self.closed_work = []
        if self.closed_work_report is not None:
            report = self.closed_work_report
        return subprocess.CompletedProcess(args, 0, json.dumps(report).encode(), b'')

    def _helper(self, args):
        mounts = [args[i + 1] for i, value in enumerate(args) if value == '--mount']
        program = args[args.index('-c') + 1]
        if program == linux_upgrade.IMAGE_SETTINGS:
            image = args[args.index('--entrypoint') + 2]
            table = self.image_settings.get(image, linux_launch.PROFILE_SETTINGS)
            return subprocess.CompletedProcess(args, 0, (json.dumps(table) + '\n').encode(), b'')
        if "'reconcile-unpublished'" in program:
            return self.unpublished_review(args, program=program)
        if "'reconcile-closed-work'" in program:
            return self.closed_work_review(args, program=program)
        if program in self.PROGRAMS:
            stopped = '-stopped-' in args[args.index('--name') + 1]
            return self.proof(args, idle=self.offline_idle if stopped else self.live_idle, kind=self.PROGRAMS[program])
        return run_state_tool(program, args[args.index('-c') + 2:], {
            fields['dst']: self.volumes.setdefault(fields['src'], {})
            for fields in (dict(item.split('=', 1) for item in mount.split(',') if '=' in item) for mount in mounts)})

    # -- docker CLI -------------------------------------------------------
    def __call__(self, endpoint, *args, data=None, timeout=90):
        self.calls.append(args)
        ok = lambda out=b'': subprocess.CompletedProcess(args, 0, out if isinstance(out, bytes) else out.encode(), b'')
        fail = lambda: subprocess.CompletedProcess(args, 1, b'', b'Error: private detail /Users/someone')
        for prefix in list(self.fail_calls_once):
            if args[:len(prefix)] == prefix:
                self.fail_calls_once.remove(prefix)
                return fail()
        if args[:1] == ('info',):
            if '{{.OperatingSystem}}' in args:
                return ok(self.operating_system)
            return ok(json.dumps({'OSType': 'linux', 'CgroupVersion': '2'}))
        if args[:2] == ('image', 'inspect'):
            return ok(args[-1]) if args[-1] in self.images else fail()
        if args[1:2] == ('ls',):
            return ok()
        if args[:2] == ('volume', 'create'):
            if args[-1] in self.volumes:
                return fail()
            self.volumes[args[-1]] = {}
            self.volume_labels[args[-1]] = dict(args[i + 1].split('=', 1) for i, value in enumerate(args)
                                                if value == '--label')
            return ok(args[-1])
        if args[:2] == ('volume', 'inspect'):
            if args[-1] not in self.volumes:
                return fail()
            return ok(json.dumps(self.volume_labels.get(args[-1], {})) if '{{json .Labels}}' in args else '{}')
        if args[:2] == ('volume', 'rm'):
            self.volume_labels.pop(args[-1], None)
            return ok() if self.volumes.pop(args[-1], None) is not None else fail()
        if args[:1] == ('ps',):
            selector = args[args.index('--filter') + 1]
            if selector.startswith('network='):
                return ok('\n'.join(identity for identity, record in self.containers.items()
                                     if selector.removeprefix('network=') in record['NetworkSettings']['Networks']))
            volume = selector.removeprefix('volume=')
            return ok('\n'.join(identity for identity, record in self.containers.items()
                                 if any(mount['Source'] == volume for mount in record['HostConfig']['Mounts'])))
        if args[:2] == ('network', 'create'):
            if args[-1] in self.networks:
                return fail()
            self.networks[args[-1]] = {'Options': dict(args[i + 1].split('=', 1) for i, value in enumerate(args)
                                                       if value == '--opt')}
            return ok()
        if args[:2] == ('network', 'inspect'):
            network = self.networks.get(args[-1])
            return ok(json.dumps(network['Options'])) if network is not None else fail()
        if args[:2] == ('network', 'rm'):
            if args[-1] not in self.networks or any(
                    record['State']['Running'] and args[-1] in record['NetworkSettings']['Networks']
                    for record in self.containers.values()):
                return fail()
            del self.networks[args[-1]]
            return ok()
        if args[:2] == ('network', 'disconnect'):
            record = self.find(args[-1])
            if record is None or args[-2] not in record['NetworkSettings']['Networks']:
                return fail()
            del record['NetworkSettings']['Networks'][args[-2]]
            return ok()
        if args[:2] == ('network', 'connect'):
            record = self.find(args[-1])
            if args[-2] not in self.networks or record is None:
                return fail()
            record['NetworkSettings']['Networks'][args[-2]] = {'Aliases': [args[3]]}
            return ok()
        if args[:1] == ('create',):
            return self._create(list(args[1:]))
        if args[:1] == ('inspect',):
            record = self.find(args[-1])
            if record is None:
                return fail()
            fmt = args[2]
            if fmt == '{{json .}}':
                return ok(json.dumps(record))
            if fmt == '{{.Name}}':
                return ok(record['Name'])
            if fmt == '{{.State.Running}}':
                return ok('true' if record['State']['Running'] else 'false')
            if fmt == '{{.Id}}':
                return ok(record['Id'])
        if args[:1] == ('cp',):
            if args[1] == '-':
                identity, target = args[2].split(':', 1)
                with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                    for member in archive.getmembers():
                        if member.isfile():
                            self.volume_at(self.find(identity), target)[member.name] = archive.extractfile(member).read()
                return ok()
            source, path = args[1].split(':', 1)
            target, name = path.rsplit('/', 1)
            payload = self.volume_at(self.find(source), target).get(name)
            return ok(_tar(name, payload)) if payload is not None else fail()
        if args[:1] == ('stop',):
            record = self.find(args[-1])
            if record is None:
                return fail()
            record['State']['Running'] = False
            if self.on_stop:
                self.on_stop(record)
            return ok()
        if args[:1] == ('start',):
            record = self.find(args[-1])
            if record['Id'] in self.fail_start_once:
                self.fail_start_once.discard(record['Id'])
                return fail()
            record['State']['Running'] = True
            if self.on_start:
                self.on_start(record)
            return ok()
        if args[:1] == ('rename',):
            self.find(args[1])['Name'] = '/' + args[2]
            return ok()
        if args[:1] == ('rm',):
            record = self.find(args[-1])
            if record is None or (record['State']['Running'] and '--force' not in args):
                return fail()
            if record['Id'] in self.fail_rm_once:
                self.fail_rm_once.discard(record['Id'])
                return fail()
            del self.containers[record['Id']]
            return ok()
        if args[:1] == ('exec',):
            record = self.find(args[1])
            program = args[args.index('-c') + 1]
            if program in self.PROGRAMS:
                if record['Image'] in self.busy_images:
                    return subprocess.CompletedProcess(args, 1, b'', IDLE_ERROR)
                if record['Image'] == NEW:  # the upgraded runtime checks its own migrated state
                    return ok()
                return self.proof(args, idle=self.live_idle, kind=self.PROGRAMS[program])
            healthy = record['State']['Running'] and record['Image'] not in self.unhealthy_images
            return ok(json.dumps({'status': 'ok' if healthy else 'starting'}))
        if args[:1] == ('run',):
            if '--provision-local-owner' in args:
                return ok()
            if 'keygen' in args:  # the image's signer generator: private key in its state, public key out
                self.keygens.append(args)
                mount = dict(item.split('=', 1) for item in args[args.index('--mount') + 1].split(',') if '=' in item)
                volume = self.volumes.setdefault(mount['src'], {})
                if 'assertion-key.pem' in volume or self.keygen_fails:
                    return subprocess.CompletedProcess(args, 1, b'', b'FileExistsError')
                kid = args[args.index('--kid') + 1]
                volume['assertion-key.pem'] = b'synthetic private key ' + kid.encode()
                return ok(json.dumps({'kty': 'RSA', 'n': 'n-' + kid, 'e': 'AQAB', 'kid': kid, 'alg': 'RS256', 'use': 'sig'}))
            return self._helper(list(args))
        raise AssertionError(args)

    def launcher_docker(self, endpoint, *args, data=None):
        """launch.docker's contract: stdout text, RuntimeError on failure."""
        result = self(endpoint, *args, data=data)
        if result.returncode:
            raise RuntimeError('Docker package operation failed: ' + args[0])
        return result.stdout.decode().strip()


class Health:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *exc): return False


@pytest.fixture
def package(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(linux_launch, 'docker', fake.launcher_docker)
    monkeypatch.setattr(linux_upgrade, '_docker', fake)
    monkeypatch.setattr(linux_upgrade.base, 'docker', fake.launcher_docker)

    def urlopen(url, timeout=None, **kwargs):
        ui = fake.find(f'{NAME}-ui')
        if ui is None or not ui['State']['Running'] or ui['Image'] in fake.unhealthy_images:
            raise OSError('not ready')
        return Health()
    monkeypatch.setattr(linux_launch.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(linux_upgrade.time, 'sleep', lambda seconds: None)
    with (tmp_path / 'credentials.json').open('w') as credentials:
        receipt = linux_launch.launch(endpoint='unix:///docker.sock', name=NAME, image=OLD, native_image=NATIVE,
                                      ui_port=18880, mcp_port=18867, credentials=credentials)
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    # Real state the upgrade must carry: a runtime DB, sign-ins, links and owner files.
    fake.volumes[f'{NAME}-control']['runtime.db'] = b'runtime rows v1'
    fake.volumes[f'{NAME}-ui-state']['auth.sqlite3'] = b'owner sign-in'
    fake.volumes[f'{NAME}-links']['link-refs.sqlite3'] = b'links'
    fake.volumes[f'{NAME}-data']['managed-files/abc/fil_1.blob'] = b'owner file bytes'
    fake.calls.clear()
    return fake, path, receipt


def _runner(path, **kwargs):
    return linux_upgrade.Upgrade('unix:///docker.sock', path, **kwargs)


def _config(fake, role='runtime'):
    record = fake.find(f'{NAME}-{role}')
    return json.loads(fake.volume_at(record, '/' + linux_upgrade.STATE[role])['config.json'])


def _mutations(fake):
    return [call for call in fake.calls if call[:1] in {('stop',), ('rename',), ('create',), ('start',), ('rm',)}
            or call[:2] in {('volume', 'create'), ('volume', 'rm'), ('cp', '-'), ('network', 'create'),
                            ('network', 'rm'), ('network', 'connect'), ('network', 'disconnect')}]


def test_every_launched_role_matches_the_upgrade_shape_and_is_recreated_identically(package):
    fake, path, receipt = package
    create_calls = {}
    original = {}
    for role in linux_upgrade.ROLES:
        record = fake.find(f'{NAME}-{role}')
        shape = linux_upgrade.role_shape(record, profile='local-linux', name=NAME, role=role,
                                         volumes=receipt['volumes'], networks=receipt['networks'])
        create_calls[role] = linux_launch.role_create_args(
            profile='local-linux', name=NAME, role=role, volumes=receipt['volumes'], networks=receipt['networks'],
            publish=shape['publish'], extra_hosts=shape['extra_hosts'], device=shape['device'])
        original[role] = fake.find(f'{NAME}-{role}')['HostConfig']
    assert create_calls['ui'][create_calls['ui'].index('--publish') + 1] == '127.0.0.1:18880:8780'
    assert create_calls['mcp'][create_calls['mcp'].index('--publish') + 1] == '127.0.0.1:18867:8767'
    _runner(path).upgrade(service_image=NEW)
    for role in linux_upgrade.ROLES:
        # The new container has exactly the launcher's host configuration.
        assert fake.find(f'{NAME}-{role}')['HostConfig'] == original[role]
        assert fake.find(f'{NAME}-{role}')['Image'] == NEW


def test_hosted_shape_keeps_device_extra_hosts_restart_and_auth_volume(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(linux_launch, 'docker', fake.launcher_docker)
    secret, cert, key = (tmp_path / name for name in ('secret', 'cert.pem', 'key.pem'))
    for path in (secret, cert, key):
        path.write_text('synthetic')
        path.chmod(0o600)
    hosted = {'public_url': 'https://app.example.test:18443', 'mcp_public_url': 'https://mcp.example.test:18444/mcp',
              'issuer': 'https://idp.example.test/realms/x', 'client_id': 'ui', 'client_secret_file': str(secret),
              'tenant_id': 'fixture', 'tls_certificate_file': str(cert), 'tls_key_file': str(key),
              'xfs_mount': '/srv/data', 'xfs_device': '/dev/vdc', 'extra_hosts': {'idp.example.test': 'host-gateway'},
              'models': {'grok-build': 'grok-exact-1'}}
    original_run = fake.__call__

    def with_keygen(endpoint, *args, data=None, timeout=90):
        if args[:1] == ('run',) and 'keygen' in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(
                {'kty': 'RSA', 'n': 'x', 'e': 'AQAB', 'kid': args[args.index('--kid') + 1]}).encode(), b'')
        return original_run(endpoint, *args, data=data, timeout=timeout)
    monkeypatch.setattr(linux_launch, 'docker', lambda endpoint, *args, data=None: (
        lambda result: (_ for _ in ()).throw(RuntimeError(args[0])) if result.returncode else result.stdout.decode().strip()
    )(with_keygen(endpoint, *args, data=data)))
    monkeypatch.setattr(linux_launch, '_wait_until_hosted_ready', lambda *a, **k: None)
    receipt = linux_launch.launch_hosted(endpoint='unix:///docker.sock', name=NAME, image=OLD, native_image=NATIVE,
                                         ui_port=18443, mcp_port=18444, hosted=hosted)
    assert receipt['models'] == {'grok-build': 'grok-exact-1'}
    runtime_env = json.loads(fake.volumes[f'{NAME}-control']['config.json'])['environment']
    assert runtime_env['WPR_MODEL_GROK_BUILD'] == 'grok-exact-1'
    for role in ('ui', 'mcp'):
        assert 'WPR_MODEL_GROK_BUILD' not in json.loads(fake.volumes[f'{NAME}-{role}-state']['config.json'])['environment']
    shapes = {role: linux_upgrade.role_shape(fake.find(f'{NAME}-{role}'), profile='hosted-xfs', name=NAME, role=role,
                                             volumes=receipt['volumes'], networks=receipt['networks'])
              for role in linux_upgrade.ROLES}
    assert shapes['runtime']['device'] == '/dev/vdc'
    assert shapes['ui']['extra_hosts'] == {'idp.example.test': 'host-gateway'}
    for role in linux_upgrade.ROLES:
        rebuilt = linux_launch.role_create_args(profile='hosted-xfs', name=NAME, role=role, volumes=receipt['volumes'],
                                                networks=receipt['networks'], publish=shapes[role]['publish'],
                                                extra_hosts=shapes[role]['extra_hosts'], device=shapes['runtime']['device'])
        launched = next(call for call in fake.calls if call[:1] == ('create',) and f'{NAME}-{role}' in call)
        assert tuple(['create'] + rebuilt[1:]) == launched[:len(rebuilt)]


def test_upgrade_preserves_state_and_changes_only_controller_native_and_model(package):
    fake, path, receipt = package
    before = {name: dict(files) for name, files in fake.volumes.items()}
    old_ids = dict(receipt['containers'])
    previous_env = _config(fake)['environment']
    result = _runner(path).upgrade(service_image=NEW, models={'grok-build': 'grok-exact-2'})
    assert result['status'] == 'awaiting_commit'
    new = json.loads(path.read_text())
    assert new['service_image'] == NEW and new['models'] == {'grok-build': 'grok-exact-2'}
    assert set(new['containers'].values()).isdisjoint(old_ids.values())
    env = _config(fake)['environment']
    assert env['XPERFECT_CONTROLLER_ID'] == new['containers']['runtime']
    assert env['WPR_MODEL_GROK_BUILD'] == 'grok-exact-2'
    changed = {key for key in set(env) | set(previous_env) if env.get(key) != previous_env.get(key)}
    assert changed == {'XPERFECT_CONTROLLER_ID', 'WPR_MODEL_GROK_BUILD'}
    for name in (f'{NAME}-ui-state', f'{NAME}-mcp-state', f'{NAME}-links', f'{NAME}-data'):
        assert fake.volumes[name] == before[name]
    assert fake.volumes[f'{NAME}-control']['runtime.db'] == b'runtime rows v1'
    # Previous containers are kept, stopped and renamed for rollback.
    for role, identity in old_ids.items():
        record = fake.containers[identity]
        assert record['State']['Running'] is False and record['Name'].startswith(f'/{NAME}-{role}.pre-upgrade-')
    assert fake.volumes[result['backup_volume']]


def test_active_work_is_refused_before_anything_changes(package):
    fake, path, receipt = package
    fake.live_idle = False
    with pytest.raises(linux_upgrade.UpgradeError, match='active work must be quiesced') as caught:
        _runner(path).upgrade(service_image=NEW)
    assert 'Nothing was changed' in str(caught.value) and '/Users/' not in str(caught.value)
    assert _mutations(fake) == []
    assert not linux_upgrade.journal_path(path).exists()
    assert json.loads(path.read_text()) == receipt


def test_work_started_after_the_live_check_restores_the_previous_service(package):
    fake, path, receipt = package
    fake.offline_idle = False
    with pytest.raises(linux_upgrade.UpgradeError, match='previous version is running again'):
        _runner(path).upgrade(service_image=NEW)
    for role, identity in receipt['containers'].items():
        record = fake.containers[identity]
        assert record['State']['Running'] and record['Name'] == f'/{NAME}-{role}' and record['Image'] == OLD
    assert not any(name.startswith(f'{NAME}-upgrade-') for name in fake.volumes)
    assert not any(call[:1] == ('create',) for call in fake.calls)
    assert not linux_upgrade.journal_path(path).exists()
    assert json.loads(path.read_text())['containers'] == receipt['containers']


def test_a_new_version_that_never_becomes_healthy_is_rolled_back_to_exact_state(package):
    fake, path, receipt = package
    fake.unhealthy_images.add(NEW)
    before = {name: dict(files) for name, files in fake.volumes.items()}

    def migrate(record):
        # The new runtime migrates its database before failing its health check.
        if record['Image'] == NEW and record['Name'].endswith('-runtime'):
            fake.volume_at(record, '/control')['runtime.db'] = b'runtime rows v2 (migrated)'
    fake.on_start = migrate
    runner = _runner(path, ready_timeout=0)
    with pytest.raises(linux_upgrade.UpgradeError, match='previous version is running again'):
        runner.upgrade(service_image=NEW)
    for name, files in before.items():
        assert fake.volumes[name] == files, name
    for role, identity in receipt['containers'].items():
        record = fake.containers[identity]
        assert record['State']['Running'] and record['Name'] == f'/{NAME}-{role}' and record['Image'] == OLD
    assert {record['Image'] for record in fake.containers.values()} == {OLD}
    assert json.loads(path.read_text())['containers'] == receipt['containers']
    assert not linux_upgrade.journal_path(path).exists()


def test_rollback_after_a_successful_upgrade_returns_exact_previous_state(package):
    fake, path, receipt = package
    before = {name: dict(files) for name, files in fake.volumes.items()}
    runner = _runner(path)
    runner.upgrade(service_image=NEW, models={'grok-build': 'grok-exact-2'})
    # The new version runs for a while: migrates and records a new sign-in.
    fake.volume_at(fake.find(f'{NAME}-runtime'), '/control')['runtime.db'] = b'runtime rows v2'
    fake.volume_at(fake.find(f'{NAME}-ui'), '/ui-state')['auth.sqlite3'] = b'newer sign-in'
    original_bytes = linux_upgrade.journal_path(path).exists() and json.loads(
        linux_upgrade.journal_path(path).read_text())['previous_receipt_text']
    result = runner.rollback()
    assert result['status'] == 'rolled_back' and result['restored_state']
    assert path.read_text() == original_bytes
    for name, files in before.items():
        assert fake.volumes[name] == files, name
    assert 'WPR_MODEL_GROK_BUILD' not in _config(fake)['environment']
    assert _config(fake)['environment']['XPERFECT_CONTROLLER_ID'] == receipt['containers']['runtime']
    assert json.loads(path.read_text()) == receipt
    assert {record['Image'] for record in fake.containers.values()} == {OLD}
    assert not linux_upgrade.journal_path(path).exists()


def test_commit_removes_only_the_previous_containers_and_backup(package):
    fake, path, receipt = package
    runner = _runner(path)
    result = runner.upgrade(service_image=NEW)
    committed = runner.commit()
    assert committed['status'] == 'committed'
    assert result['backup_volume'] not in fake.volumes
    assert not set(receipt['containers'].values()) & set(fake.containers)
    new = json.loads(path.read_text())
    assert all(fake.containers[identity]['State']['Running'] for identity in new['containers'].values())
    assert fake.volumes[f'{NAME}-data']['managed-files/abc/fil_1.blob'] == b'owner file bytes'
    with pytest.raises(FileNotFoundError):
        runner.rollback()


def test_an_interrupted_rollback_resumes_from_its_journal(package):
    fake, path, receipt = package
    fake.unhealthy_images.add(NEW)
    fake.fail_start_once.add(receipt['containers']['runtime'])
    runner = _runner(path, ready_timeout=0)
    with pytest.raises(linux_upgrade.UpgradeError, match='automatic rollback stopped'):
        runner.upgrade(service_image=NEW)
    assert json.loads(linux_upgrade.journal_path(path).read_text())['phase'] == 'restored'
    # The previous version may already serve and write again; a rerun must not restore over it.
    fake.volumes[f'{NAME}-control']['runtime.db'] = b'written after the restore'
    fake.calls.clear()
    assert runner.rollback()['status'] == 'rolled_back'
    assert not any(call[:1] == ('run',) for call in fake.calls)
    assert fake.volumes[f'{NAME}-control']['runtime.db'] == b'written after the restore'
    for role, identity in receipt['containers'].items():
        assert fake.containers[identity]['State']['Running'] and fake.containers[identity]['Name'] == f'/{NAME}-{role}'
    assert {record['Image'] for record in fake.containers.values()} == {OLD}


def test_receipt_and_running_container_identity_must_agree_unless_adopted(package):
    fake, path, receipt = package
    stale = {**receipt, 'containers': {role: 'f' * 64 for role in receipt['containers']}}
    path.write_text(json.dumps(stale))
    with pytest.raises(linux_upgrade.UpgradeError, match='--adopt-running-containers'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []
    assert _runner(path).upgrade(service_image=NEW, adopt=True)['status'] == 'awaiting_commit'


@pytest.mark.parametrize('change', ['cap', 'mount', 'image', 'label'])
def test_an_unsupported_container_shape_is_refused_before_changes(package, change):
    fake, path, receipt = package
    record = fake.find(f'{NAME}-runtime')
    if change == 'cap':
        record['HostConfig']['CapAdd'].append('CAP_SYS_PTRACE')
    elif change == 'mount':
        record['HostConfig']['Mounts'].append({'Type': 'bind', 'Source': '/', 'Target': '/host'})
    elif change == 'image':
        record['Image'] = 'sha256:' + 'c' * 64
    else:
        record['Config']['Labels']['xperfect.package'] = 'xperfect-other'
    with pytest.raises(linux_upgrade.UpgradeError):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []


def test_one_open_upgrade_at_a_time_and_no_empty_upgrade(package):
    fake, path, receipt = package
    with pytest.raises(linux_upgrade.UpgradeError, match='already runs this image'):
        _runner(path).upgrade(service_image=OLD)
    _runner(path).upgrade(service_image=NEW)
    with pytest.raises(linux_upgrade.UpgradeError, match='already open'):
        _runner(path).upgrade(service_image=OLD)


def test_unknown_images_and_public_receipts_are_refused(package):
    fake, path, receipt = package
    with pytest.raises(linux_upgrade.UpgradeError, match='could not be verified'):
        _runner(path).upgrade(service_image='sha256:' + '9' * 64)
    path.chmod(0o644)
    with pytest.raises(linux_upgrade.UpgradeError, match='private'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []


def test_idle_failure_reason_never_leaks_paths_or_tracebacks():
    assert linux_upgrade._reason(IDLE_ERROR) == 'GlassHive active work must be quiesced before continuity'
    assert linux_upgrade._reason(b'Error response: /Users/someone/private failed\n') == ''
    assert linux_upgrade._reason(b'xperfect-upgrade: a restore transaction is still open; commit or roll it back first\n') \
        == 'a restore transaction is still open; commit or roll it back first'


@pytest.mark.parametrize('value,message', [
    ({'grok': 'x'}, 'Unknown worker profile'),
    ({'grok-build': 'two words'}, 'exact model ID'),
    ({'grok-build': ''}, 'exact model ID'),
    ({'openclaw': 'a', 'openclaw-general': 'b'}, 'same model setting'),
])
def test_model_choices_are_exact_and_never_guessed(value, message):
    with pytest.raises(ValueError, match=message):
        linux_launch.validate_models(value)
    assert linux_launch.validate_models(None) == {}
    assert linux_launch.parse_model_arguments(['grok-build=grok-4.6']) == {'grok-build': 'grok-4.6'}
    with pytest.raises(ValueError, match='--model PROFILE='):
        linux_launch.parse_model_arguments(['grok-4.6'])


def test_launcher_model_table_is_the_runtime_profile_registry():
    from workers_projects_runtime.profile_registry import PROFILES
    assert linux_launch.MODEL_ENVIRONMENTS == {item.profile: item.model_environment for item in PROFILES}


def test_local_launch_writes_models_only_to_the_runtime(package):
    fake, path, receipt = package
    assert receipt['models'] == {}
    assert not any(key.startswith('WPR_MODEL_') for key in _config(fake)['environment'])


# -- rollback safety: nothing changes until the backup, idle and storage proofs hold --

def _upgraded(package):
    fake, path, receipt = package
    runner = _runner(path)
    result = runner.upgrade(service_image=NEW)
    new = json.loads(path.read_text())['containers']
    # The new version serves and writes its own state.
    fake.volumes[f'{NAME}-control']['runtime.db'] = b'runtime rows v2'
    fake.calls.clear()
    return fake, path, receipt, runner, result, new


def _unchanged_new_version(fake, path, new, phase='awaiting_commit'):
    assert all(fake.containers[identity]['State']['Running'] for identity in new.values())
    assert fake.volumes[f'{NAME}-control']['runtime.db'] == b'runtime rows v2'
    assert json.loads(linux_upgrade.journal_path(path).read_text())['phase'] == phase
    assert json.loads(path.read_text())['containers'] == new


@pytest.mark.parametrize('damage', ['missing', 'foreign', 'altered'])
def test_rollback_proves_its_backup_before_touching_the_new_version(package, damage):
    fake, path, receipt, runner, result, new = _upgraded(package)
    backup = result['backup_volume']
    if damage == 'missing':
        fake.volumes.pop(backup)
    elif damage == 'foreign':
        fake.volume_labels[backup]['xperfect.upgrade'] = 'another'
    else:
        fake.volumes[backup]['before/control/runtime.db'] = b'tampered'
    with pytest.raises(linux_upgrade.UpgradeError, match='backup') as caught:
        runner.rollback()
    assert 'Nothing was' in str(caught.value) and '/Users/' not in str(caught.value)
    assert _mutations(fake) == []
    _unchanged_new_version(fake, path, new)
    if damage == 'missing':
        assert backup not in fake.volumes  # never recreated empty by a mount


def test_rollback_refuses_while_the_new_version_has_active_work(package):
    fake, path, receipt, runner, result, new = _upgraded(package)
    fake.busy_images.add(NEW)
    with pytest.raises(linux_upgrade.UpgradeError, match='new version is not idle: GlassHive active work'):
        runner.rollback()
    assert _mutations(fake) == []
    _unchanged_new_version(fake, path, new)
    fake.busy_images.clear()
    assert runner.rollback()['idle_proof'] == 'runtime records and workspace containers'


def _box(fake, identity='f' * 64):
    fake.containers[identity] = {
        'Id': identity, 'Name': '/xperfect-box-demo', 'Image': NATIVE, 'State': {'Running': False},
        'Config': {'Labels': {'xperfect.workspace': 'wsp_demo'}},
        'HostConfig': {'Mounts': [{'Type': 'volume', 'Source': f'{NAME}-data', 'Target': '/workspace/data'}]}}


def test_rollback_refuses_workspace_containers_the_new_version_created(package):
    fake, path, receipt, runner, result, new = _upgraded(package)
    _box(fake)
    with pytest.raises(linux_upgrade.UpgradeError, match='created 1 workspace container'):
        runner.rollback()
    assert _mutations(fake) == []
    _unchanged_new_version(fake, path, new)
    del fake.containers['f' * 64]
    assert runner.rollback()['status'] == 'rolled_back'


def test_work_started_while_stopping_the_new_version_serves_it_again(package):
    fake, path, receipt, runner, result, new = _upgraded(package)
    fake.on_stop = lambda record: record['Id'] == new['runtime'] and _box(fake)
    with pytest.raises(linux_upgrade.UpgradeError, match='created 1 workspace container'):
        runner.rollback()
    _unchanged_new_version(fake, path, new)
    assert not any(call[:1] == ('rm',) or (call[:1] == ('run',) and 'restore' in call) for call in fake.calls)


def _registry(*owners):
    import sqlite3
    with tempfile.TemporaryDirectory() as scratch:
        database = Path(scratch, 'registry.sqlite3')
        connection = sqlite3.connect(database)
        connection.execute('CREATE TABLE storage_owner_projects (project_id INTEGER PRIMARY KEY AUTOINCREMENT, '
                           'tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, UNIQUE(tenant_id, owner_id))')
        connection.executemany('INSERT INTO storage_owner_projects (tenant_id, owner_id) VALUES (?, ?)',
                               [('default', owner) for owner in owners])
        connection.commit()
        connection.close()
        return database.read_bytes()


@pytest.mark.parametrize('when', ['before rollback', 'while stopping'])
def test_rollback_never_rewinds_storage_allocated_to_a_new_owner(package, when):
    fake, path, receipt = package
    fake.volumes[f'{NAME}-control']['storage/registry.sqlite3'] = _registry('grace')
    runner = _runner(path)
    runner.upgrade(service_image=NEW)
    new = json.loads(path.read_text())['containers']
    fake.volumes[f'{NAME}-control']['runtime.db'] = b'runtime rows v2'
    grew = _registry('grace', 'ada')

    def allocate(record=None):
        if record is None or record['Id'] == new['runtime']:
            fake.volumes[f'{NAME}-control']['storage/registry.sqlite3'] = grew
    if when == 'before rollback':
        allocate()
    else:
        fake.on_stop = allocate
    fake.calls.clear()
    with pytest.raises(linux_upgrade.UpgradeError, match='owners gained storage after the upgrade'):
        runner.rollback()
    # Refused before any restore: the new version serves again with its storage records.
    _unchanged_new_version(fake, path, new)
    assert fake.volumes[f'{NAME}-control']['storage/registry.sqlite3'] == grew
    assert not any(call[:1] == ('rm',) for call in fake.calls)


def test_rollback_restores_the_registry_when_no_owner_was_added(package):
    fake, path, receipt = package
    fake.volumes[f'{NAME}-control']['storage/registry.sqlite3'] = _registry('grace')
    runner = _runner(path)
    runner.upgrade(service_image=NEW)
    result = runner.rollback()
    assert result['status'] == 'rolled_back'
    assert fake.volumes[f'{NAME}-control']['storage/registry.sqlite3'] == _registry('grace')
    assert result['changed_since_upgrade'] == ['control']  # the new configuration named its controller


def test_rollback_keeps_the_new_version_when_a_previous_container_is_missing(package):
    fake, path, receipt, runner, result, new = _upgraded(package)
    del fake.containers[receipt['containers']['ui']]
    with pytest.raises(linux_upgrade.UpgradeError, match='previous ui container is missing'):
        runner.rollback()
    assert _mutations(fake) == []
    _unchanged_new_version(fake, path, new)


def test_an_interrupted_commit_can_only_be_finished(package):
    fake, path, receipt, runner, result, new = _upgraded(package)
    fake.fail_rm_once.add(receipt['containers']['ui'])
    with pytest.raises(linux_upgrade.UpgradeError):
        runner.commit()
    assert json.loads(linux_upgrade.journal_path(path).read_text())['phase'] == 'committing'
    with pytest.raises(linux_upgrade.UpgradeError, match='commit of this upgrade is in progress'):
        runner.rollback()
    assert all(fake.containers[identity]['State']['Running'] for identity in new.values())
    assert runner.commit()['status'] == 'committed'
    assert not set(receipt['containers'].values()) & set(fake.containers)
    assert result['backup_volume'] not in fake.volumes


def test_a_helper_that_outlives_its_timeout_is_stopped(monkeypatch):
    calls = []

    def docker(endpoint, *args, data=None, timeout=90):
        calls.append(args)
        if args[:1] == ('run',):
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, 1, b'', b'')
    monkeypatch.setattr(linux_upgrade, '_docker', docker)
    with pytest.raises(linux_upgrade.UpgradeError, match='restore step timed out and was stopped'):
        linux_upgrade._helper('unix:///docker.sock', image=OLD, name=NAME, txn='abc', purpose='restore',
                              mounts=[], command=[], entrypoint='python3')
    assert calls[-1] == ('rm', '--force', f'{NAME}-upgrade-abc-restore')


def test_unexpected_errors_are_reported_without_private_detail(package):
    fake, path, receipt = package

    def fail(record):
        if record['Image'] == NEW and record['Name'].endswith('-runtime'):
            raise OSError('/Users/someone/private/receipt.json')
    fake.on_start = fail
    with pytest.raises(linux_upgrade.UpgradeError, match='previous version is running again: OSError') as caught:
        _runner(path).upgrade(service_image=NEW)
    assert '/Users/' not in str(caught.value)


def test_a_new_workspace_image_is_named_for_new_workspaces_only(package):
    fake, path, receipt = package
    other = 'sha256:' + 'd' * 64
    fake.images.add(other)
    result = _runner(path).upgrade(service_image=NEW, native_image=other)
    assert result['native_image'] == other and 'existing workspaces keep' in result['native_image_note']
    assert json.loads(path.read_text())['upgrade']['from_native_image'] == NATIVE


def test_the_operator_profile_name_is_kept_for_a_shared_model_setting(package):
    fake, path, receipt = package
    result = _runner(path).upgrade(service_image=NEW, models={'openclaw': 'openai/exact-1'})
    assert result['models'] == {'openclaw': 'openai/exact-1'}


# -- existing packages: state an earlier release wrote, earlier receipts, Docker Desktop --

def test_state_the_running_release_cannot_review_is_proved_by_the_incoming_release(package):
    fake, path, receipt = package
    fake.running_reviews = False  # e.g. its tables were laid out by an earlier release
    result = _runner(path).upgrade(service_image=NEW)
    assert result['status'] == 'awaiting_commit'
    assert json.loads(path.read_text())['upgrade']['idle_proof'] == 'new version (inherited state)'
    # The incoming release reviews what it inherits; the running release still vouches for
    # its own work, live and again once the package is stopped.
    assert ('incoming', ['live', 'incoming']) in fake.proofs and ('activity', 'exec') in fake.proofs
    assert ('incoming', ['stopped', 'incoming']) in fake.proofs
    assert ('activity', ['stopped', 'activity']) in fake.proofs
    assert not any(kind == 'running' and where != 'exec' for kind, where in fake.proofs)


def test_the_running_release_stays_the_authority_on_its_own_work(package):
    fake, path, receipt = package
    # It cannot review its schema and reports work the new release does not recognise.
    fake.running_reviews, fake.live_idle, fake.incoming_blind = False, False, True
    with pytest.raises(linux_upgrade.UpgradeError, match="running version's own work: GlassHive active work") as caught:
        _runner(path).upgrade(service_image=NEW)
    assert 'Nothing was changed' in str(caught.value)
    assert _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()


def test_a_running_release_without_its_work_predicate_is_refused(package):
    fake, path, receipt = package
    fake.running_reviews, fake.activity_predicate = False, False
    with pytest.raises(linux_upgrade.UpgradeError, match="running version's own work: its state could not be proved idle"):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []


def test_work_the_new_release_cannot_see_after_the_stop_restores_the_previous_service(package):
    fake, path, receipt = package
    fake.running_reviews, fake.offline_idle, fake.incoming_blind = False, False, True
    with pytest.raises(linux_upgrade.UpgradeError, match='previous version is running again'):
        _runner(path).upgrade(service_image=NEW)
    assert ('activity', ['stopped', 'activity']) in fake.proofs
    for role, identity in receipt['containers'].items():
        assert fake.containers[identity]['State']['Running'] and fake.containers[identity]['Image'] == OLD


def test_the_running_release_keeps_proving_state_it_can_review(package):
    fake, path, receipt = package
    _runner(path).upgrade(service_image=NEW)
    assert json.loads(path.read_text())['upgrade']['idle_proof'] == 'running version'
    assert not any(kind == 'incoming' for kind, _ in fake.proofs)


@pytest.mark.parametrize('running_reviews', [True, False])
def test_active_work_is_refused_by_either_release(package, running_reviews):
    fake, path, receipt = package
    fake.live_idle, fake.running_reviews = False, running_reviews
    with pytest.raises(linux_upgrade.UpgradeError, match='active work must be quiesced') as caught:
        _runner(path).upgrade(service_image=NEW)
    # The running release's own work check refuses before any costly inherited-state review.
    assert "running version's own work: GlassHive active work" in str(caught.value)
    assert 'Nothing was changed' in str(caught.value)
    assert not any(kind == 'incoming' for kind, _ in fake.proofs)
    assert _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()


def test_state_neither_release_can_review_is_refused(package):
    fake, path, receipt = package
    fake.running_reviews = fake.incoming_reviews = False
    with pytest.raises(linux_upgrade.UpgradeError, match='cannot be reviewed') as caught:
        _runner(path).upgrade(service_image=NEW)
    assert str(caught.value).count('unreviewed schema shape') == 2
    assert _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()


def test_work_started_after_an_incoming_proof_restores_the_previous_service(package):
    fake, path, receipt = package
    fake.running_reviews, fake.offline_idle = False, False
    with pytest.raises(linux_upgrade.UpgradeError, match='previous version is running again'):
        _runner(path).upgrade(service_image=NEW)
    for role, identity in receipt['containers'].items():
        assert fake.containers[identity]['State']['Running'] and fake.containers[identity]['Image'] == OLD
    assert not any(call[:1] == ('create',) for call in fake.calls)


def test_a_model_only_change_needs_the_running_release_proof(package):
    fake, path, receipt = package
    fake.running_reviews = False
    with pytest.raises(linux_upgrade.UpgradeError, match='running version: GlassHive continuity'):
        _runner(path).upgrade(service_image=OLD, models={'grok-build': 'grok-exact-2'})
    assert not any(kind == 'incoming' for kind, _ in fake.proofs)


def _legacy(path, receipt, **changes):
    legacy = {key: value for key, value in receipt.items() if key != 'models'}
    legacy['volumes'] = {role: volume for role, volume in receipt['volumes'].items() if role != 'links'}
    legacy.update(changes)
    text = json.dumps(legacy, indent=1)
    path.write_text(text)
    return text


def test_an_earlier_receipt_is_completed_from_the_package_and_rolled_back_byte_for_byte(package):
    fake, path, receipt = package
    assert fake.volume_labels[f'{NAME}-links'] == {'xperfect.package': NAME}
    original = _legacy(path, receipt)
    runner = _runner(path)
    result = runner.upgrade(service_image=NEW)
    upgraded = json.loads(path.read_text())
    assert upgraded['volumes']['links'] == f'{NAME}-links'
    assert upgraded['upgrade']['receipt_completed'] == ['links'] and result['status'] == 'awaiting_commit'
    runner.rollback()
    assert path.read_text() == original


def test_an_earlier_receipt_with_replaced_containers_needs_adoption(package):
    fake, path, receipt = package
    _legacy(path, receipt, containers={role: 'f' * 64 for role in receipt['containers']}, service_image=NEW)
    with pytest.raises(linux_upgrade.UpgradeError, match='--adopt-running-containers'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []
    fake.images.add('sha256:' + 'd' * 64)
    result = _runner(path).upgrade(service_image='sha256:' + 'd' * 64, adopt=True)
    assert result['status'] == 'awaiting_commit'
    assert json.loads(path.read_text())['volumes']['links'] == f'{NAME}-links'


@pytest.mark.parametrize('damage', ['foreign volume', 'missing on one role', 'unlabelled volume', 'other field missing'])
def test_an_earlier_receipt_is_completed_only_from_exact_package_evidence(package, damage):
    fake, path, receipt = package
    _legacy(path, receipt)
    if damage == 'foreign volume':
        for mount in fake.find(f'{NAME}-mcp')['HostConfig']['Mounts']:
            if mount['Target'] == '/links':
                mount['Source'] = 'xperfect-other-links'
    elif damage == 'missing on one role':
        record = fake.find(f'{NAME}-ui')
        record['HostConfig']['Mounts'] = [m for m in record['HostConfig']['Mounts'] if m['Target'] != '/links']
    elif damage == 'unlabelled volume':
        fake.volume_labels[f'{NAME}-links'] = {}
    else:
        legacy = json.loads(path.read_text())
        del legacy['volumes']['mcp-state']
        path.write_text(json.dumps(legacy))
    with pytest.raises(linux_upgrade.UpgradeError, match='links|incomplete'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()


def test_docker_desktop_socket_is_accepted_for_local_packages_only():
    record_mounts = lambda source: [
        {'Type': 'volume', 'Source': f'{NAME}-control', 'Target': '/control', 'VolumeOptions': {'NoCopy': True}},
        {'Type': 'volume', 'Source': f'{NAME}-links', 'Target': '/links', 'VolumeOptions': {'NoCopy': True}},
        {'Type': 'volume', 'Source': f'{NAME}-data', 'Target': '/data', 'VolumeOptions': {'NoCopy': True}},
        {'Type': 'bind', 'Source': source, 'Target': '/var/run/docker.sock'}]
    volumes = {role: f'{NAME}-{role}' for role in ('data', 'control', 'ui-state', 'mcp-state', 'links', 'auth')}
    networks = {'frontend': f'{NAME}-frontend', 'workers': f'{NAME}-workers'}

    def shape(profile, source, desktop=True):
        hosted = profile == 'hosted-xfs'
        record = {'Id': 'a' * 64, 'Image': OLD, 'State': {'Running': True},
                  'Config': {'Labels': {'xperfect.package': NAME, 'xperfect.role': 'runtime'},
                             'Cmd': linux_launch.role_command('runtime')},
                  'HostConfig': {'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                                 'CapAdd': ['CAP_CHOWN', 'CAP_FOWNER', 'CAP_DAC_OVERRIDE'] + (['CAP_SYS_ADMIN'] if hosted else []),
                                 'SecurityOpt': ['no-new-privileges'], 'Tmpfs': {'/tmp': linux_upgrade.TMPFS},
                                 'Mounts': record_mounts(source), 'NetworkMode': networks['frontend'],
                                 'RestartPolicy': {'Name': 'unless-stopped' if hosted else 'no'}, 'PortBindings': {},
                                 'Devices': [{'PathOnHost': '/dev/vdc', 'PathInContainer': '/dev/vdc', 'CgroupPermissions': 'r'}] if hosted else None},
                  'NetworkSettings': {'Networks': {networks['frontend']: {}, networks['workers']: {}}}}
        return linux_upgrade.role_shape(record, profile=profile, name=NAME, role='runtime', volumes=volumes,
                                        networks=networks, docker_desktop=desktop)
    for source in ('/var/run/docker.sock', linux_upgrade.DOCKER_DESKTOP_SOCKET):
        assert shape('local-linux', source)['id'] == 'a' * 64
    assert shape('hosted-xfs', '/var/run/docker.sock')['device'] == '/dev/vdc'
    with pytest.raises(linux_upgrade.UpgradeError, match='supported package shape'):
        shape('hosted-xfs', linux_upgrade.DOCKER_DESKTOP_SOCKET)
    with pytest.raises(linux_upgrade.UpgradeError, match='supported package shape'):
        shape('local-linux', '/home/someone/docker.sock')
    # Only an endpoint that reports Docker Desktop records the proxy path.
    with pytest.raises(linux_upgrade.UpgradeError, match='supported package shape'):
        shape('local-linux', linux_upgrade.DOCKER_DESKTOP_SOCKET, desktop=False)


@pytest.mark.parametrize('operating_system,accepted', [('Docker Desktop', True), ('Ubuntu 24.04 LTS', False)])
def test_the_endpoint_decides_whether_the_docker_desktop_socket_is_accepted(package, operating_system, accepted):
    fake, path, receipt = package
    for mount in fake.find(f'{NAME}-runtime')['HostConfig']['Mounts']:
        if mount['Target'] == '/var/run/docker.sock':
            mount['Source'] = linux_upgrade.DOCKER_DESKTOP_SOCKET
    fake.operating_system = operating_system
    if accepted:
        assert _runner(path).upgrade(service_image=NEW)['status'] == 'awaiting_commit'
    else:
        with pytest.raises(linux_upgrade.UpgradeError, match='supported package shape'):
            _runner(path).upgrade(service_image=NEW)
        assert _mutations(fake) == []


# -- packages created by an earlier launcher: settings added since, never changed --

SINCE = ('GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION', 'GLASSHIVE_ENABLE_NATIVE_API_KEYS',
         'GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS', 'GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH',
         'XPERFECT_WORKER_NETWORK')


def _older_launcher(fake, **keep):
    """Rewrite the package as an earlier launcher made it: without SINCE, on a shared workers bridge."""
    fake.networks[f'{NAME}-workers']['Options'] = {}
    for role in linux_upgrade.ROLES:
        volume = fake.volume_at(fake.find(f'{NAME}-{role}'), '/' + linux_upgrade.STATE[role])
        config = json.loads(volume['config.json'])
        config['environment'] = {k: v for k, v in config['environment'].items() if k not in SINCE}
        config['environment'].update(keep.get(role, {}))
        volume['config.json'] = json.dumps(config).encode()


def test_settings_an_earlier_launcher_did_not_write_are_added_and_rolled_back(package):
    fake, path, receipt = package
    _older_launcher(fake)
    before = {name: dict(files) for name, files in fake.volumes.items()}
    old = {role: _config(fake, role)['environment'] for role in linux_upgrade.ROLES}
    runner = _runner(path)
    result = runner.upgrade(service_image=NEW)
    assert result['settings_added'] == {role: sorted(SINCE) for role in linux_upgrade.ROLES}
    assert json.loads(path.read_text())['upgrade']['settings_added'] == result['settings_added']
    for role in linux_upgrade.ROLES:
        new = _config(fake, role)['environment']
        assert {key: new[key] for key in SINCE} == {key: linux_launch.PROFILE_SETTINGS['local-linux'][key] for key in SINCE}
        assert linux_launch.PROFILE_SETTINGS is linux_launch._packaged_service().PROFILE_SETTINGS or \
            linux_launch.PROFILE_SETTINGS == linux_launch._packaged_service().PROFILE_SETTINGS
        assert {k: v for k, v in new.items() if k not in SINCE and k != 'XPERFECT_CONTROLLER_ID'} == {
            k: v for k, v in old[role].items() if k != 'XPERFECT_CONTROLLER_ID'}
    runner.rollback()
    for name, files in before.items():
        assert fake.volumes[name] == files, name


def test_a_setting_any_role_holds_differently_is_kept_everywhere(package):
    fake, path, receipt = package
    _older_launcher(fake, ui={'GLASSHIVE_ENABLE_NATIVE_API_KEYS': '0'})
    result = _runner(path).upgrade(service_image=NEW)
    assert _config(fake, 'ui')['environment']['GLASSHIVE_ENABLE_NATIVE_API_KEYS'] == '0'
    for role in ('runtime', 'mcp'):  # an operator choice is not partly overridden
        assert 'GLASSHIVE_ENABLE_NATIVE_API_KEYS' not in _config(fake, role)['environment']
    assert all('GLASSHIVE_ENABLE_NATIVE_API_KEYS' not in keys for keys in result['settings_added'].values())
    assert result['settings_kept'] == ['GLASSHIVE_ENABLE_NATIVE_API_KEYS']


def test_missing_settings_alone_make_a_same_image_upgrade_meaningful(package):
    fake, path, receipt = package
    with pytest.raises(linux_upgrade.UpgradeError, match='already runs this image'):
        _runner(path).upgrade(service_image=OLD)
    _older_launcher(fake)
    result = _runner(path).upgrade(service_image=OLD)
    assert result['status'] == 'awaiting_commit' and set(result['settings_added']) == set(linux_upgrade.ROLES)
    assert json.loads(path.read_text())['upgrade']['idle_proof'] == 'running version'


def test_a_configuration_from_another_profile_is_refused(package):
    fake, path, receipt = package
    volume = fake.volume_at(fake.find(f'{NAME}-mcp'), '/mcp-state')
    config = json.loads(volume['config.json'])
    config['profile'] = 'hosted-xfs'
    volume['config.json'] = json.dumps(config).encode()
    with pytest.raises(linux_upgrade.UpgradeError, match='another package profile'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []


def test_settings_come_only_from_the_image_that_will_run_them(package):
    fake, path, receipt = package
    _older_launcher(fake)
    fake.image_settings = {OLD: None, NEW: None}  # images that declare no settings
    with pytest.raises(linux_upgrade.UpgradeError, match='already runs this image'):
        _runner(path).upgrade(service_image=OLD)
    result = _runner(path).upgrade(service_image=NEW)
    assert 'settings_added' not in result
    assert not any(key in _config(fake, role)['environment'] for role in linux_upgrade.ROLES for key in SINCE)


@pytest.mark.parametrize('table', ['not json', {'local-linux': {'lowercase': '1', 'GLASSHIVE_SECURITY_MODE': 'local'}},
                                   {'local-linux': {'GLASSHIVE_ENABLE_NATIVE_API_KEYS': '1'}}])
def test_malformed_image_settings_are_refused_before_changes(package, table):
    fake, path, receipt = package
    fake.image_settings = {NEW: table}
    if table == 'not json':
        fake.image_settings = {}
        original = fake._helper
        fake._helper = lambda args: (subprocess.CompletedProcess(args, 0, b'<html>', b'')
                                     if linux_upgrade.IMAGE_SETTINGS in args else original(args))
    with pytest.raises(linux_upgrade.UpgradeError, match='package settings'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()


def test_a_configuration_without_a_security_mode_is_refused(package):
    fake, path, receipt = package
    volume = fake.volume_at(fake.find(f'{NAME}-ui'), '/ui-state')
    config = json.loads(volume['config.json'])
    del config['environment']['GLASSHIVE_SECURITY_MODE']
    volume['config.json'] = json.dumps(config).encode()
    with pytest.raises(linux_upgrade.UpgradeError, match='no security mode'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []
    # With nothing to complete, a plain image upgrade is not refused for it (on the
    # earlier shared bridge, since this image declares no isolated worker network).
    fake.image_settings = {NEW: None}
    fake.networks[f'{NAME}-workers']['Options'] = {}
    assert _runner(path).upgrade(service_image=NEW)['status'] == 'awaiting_commit'


def test_the_profile_settings_are_exactly_what_a_fresh_launch_writes(package):
    fake, path, receipt = package
    for role in linux_upgrade.ROLES:
        environment = _config(fake, role)['environment']
        assert {key: environment[key] for key in linux_launch.PROFILE_SETTINGS['local-linux']} == \
            linux_launch.PROFILE_SETTINGS['local-linux']


PACKAGE_SECRET = 'XPERFECT_FIXTURE_PACKAGE_SECRET'


def _table_requires_a_runtime_secret(monkeypatch):
    # The hosted table's shape on this local fixture: one random secret held only by the runtime.
    assert linux_launch.PROFILE_SECRETS['hosted-xfs'] == {'runtime': ('GLASSHIVE_BOOTSTRAP_SOURCE_SECRET',)}
    monkeypatch.setitem(linux_launch.PROFILE_SECRETS, 'local-linux', {'runtime': (PACKAGE_SECRET,)})


def test_a_secret_an_earlier_launcher_did_not_generate_is_added_by_name_only_and_rolled_back(package, monkeypatch):
    fake, path, receipt = package
    _table_requires_a_runtime_secret(monkeypatch)
    before = {name: dict(files) for name, files in fake.volumes.items()}
    old = {role: _config(fake, role)['environment'] for role in linux_upgrade.ROLES}
    runner = _runner(path)
    # The missing secret alone makes a same-image upgrade meaningful.
    result = runner.upgrade(service_image=OLD)
    assert result['status'] == 'awaiting_commit' and result['secrets_added'] == {'runtime': [PACKAGE_SECRET]}
    value = _config(fake, 'runtime')['environment'][PACKAGE_SECRET]
    assert len(value) >= 64
    for role in ('ui', 'mcp'):
        assert _config(fake, role)['environment'] == old[role]
    recorded = json.loads(path.read_text())
    assert recorded['upgrade']['secrets_added'] == {'runtime': [PACKAGE_SECRET]}
    # Only the name is journaled, reported or recorded; the value is never on argv.
    written = path.read_text() + linux_upgrade.journal_path(path).read_text() + json.dumps(result)
    assert value not in written and not any(value in ' '.join(call) for call in fake.calls)
    runner.rollback()
    for name, files in before.items():
        assert fake.volumes[name] == files, name


def test_a_package_that_holds_its_secret_keeps_it(package, monkeypatch):
    fake, path, receipt = package
    _table_requires_a_runtime_secret(monkeypatch)
    volume = fake.volume_at(fake.find(f'{NAME}-runtime'), '/control')
    config = json.loads(volume['config.json'])
    config['environment'][PACKAGE_SECRET] = 'existing-package-secret'
    volume['config.json'] = json.dumps(config).encode()
    with pytest.raises(linux_upgrade.UpgradeError, match='already runs this image'):
        _runner(path).upgrade(service_image=OLD)
    result = _runner(path).upgrade(service_image=NEW)
    assert 'secrets_added' not in result
    assert _config(fake, 'runtime')['environment'][PACKAGE_SECRET] == 'existing-package-secret'


def test_a_blank_secret_is_missing_and_a_failed_start_takes_the_new_one_back(package, monkeypatch):
    fake, path, receipt = package
    _table_requires_a_runtime_secret(monkeypatch)
    volume = fake.volume_at(fake.find(f'{NAME}-runtime'), '/control')
    config = json.loads(volume['config.json'])
    config['environment'][PACKAGE_SECRET] = '  '  # the runtime strips it: no key at all
    volume['config.json'] = json.dumps(config).encode()
    fake.unhealthy_images.add(NEW)
    before = {name: dict(files) for name, files in fake.volumes.items()}
    runner = _runner(path, ready_timeout=0)
    with pytest.raises(linux_upgrade.UpgradeError, match='previous version is running again'):
        runner.upgrade(service_image=NEW)
    for name, files in before.items():
        assert fake.volumes[name] == files, name
    fake.unhealthy_images.discard(NEW)
    result = _runner(path).upgrade(service_image=OLD)
    assert result['secrets_added'] == {'runtime': [PACKAGE_SECRET]}
    assert len(_config(fake, 'runtime')['environment'][PACKAGE_SECRET]) >= 64



UNPUBLISHED = 'prj_' + 'c' * 32


def _recovery_record(path):
    return sorted(path.parent.glob(path.name + '.files-recovery-*.json'))


def test_an_unpublished_attachment_blocks_every_proof_until_the_new_release_retires_it(package):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    with pytest.raises(linux_upgrade.UpgradeError, match='must settle before continuity'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == [] and fake.recovery == [] and not linux_upgrade.journal_path(path).exists()
    fake.calls.clear()
    result = _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    assert result['status'] == 'awaiting_commit' and result['files_recovered'] == [UNPUBLISHED]
    assert json.loads(path.read_text())['upgrade']['files_recovered'] == [UNPUBLISHED]
    # The new release reviewed, then retired, before any idle proof ran; the proofs are unchanged.
    assert fake.recovery == [('review', NEW), ('retire', NEW)] and fake.unpublished == []
    first_proof = next(i for i, call in enumerate(fake.calls) if call[:1] == ('exec',) or (
        call[:1] == ('run',) and any('-live-' in item for item in call)))
    recovery_runs = [i for i, call in enumerate(fake.calls)
                     if call[:1] == ('run',) and any(item.endswith(('-files-review', '-files-recover')) for item in call)]
    assert len(recovery_runs) == 2 and max(recovery_runs) < first_proof
    record, = _recovery_record(path)
    assert record.stat().st_mode & 0o077 == 0
    assert json.loads(record.read_text())['retired'] == [UNPUBLISHED]
    assert _runner(path).commit()['status'] == 'committed'


def test_recovery_needs_a_new_image_that_carries_it(package):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    _older_launcher(fake)  # a same-image upgrade that has settings to add
    with pytest.raises(linux_upgrade.UpgradeError, match='needs a new image that carries the recovery. Nothing'):
        _runner(path).upgrade(service_image=OLD, recover_unpublished=True)
    fake.recovery_images = set()
    with pytest.raises(linux_upgrade.UpgradeError, match='does not carry this recovery. Nothing was changed'):
        _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    assert fake.unpublished == [UNPUBLISHED] and _mutations(fake) == [] and _recovery_record(path) == []
    assert not linux_upgrade.journal_path(path).exists()


@pytest.mark.parametrize('report', [
    {'unsettled': 1, 'applied': True, 'targets': [{'projection_id': UNPUBLISHED}]},  # a review that applied
    {'unsettled': 2, 'applied': False, 'targets': [{'projection_id': UNPUBLISHED}]},
    {'unsettled': 1, 'applied': False, 'targets': [{'projection_id': '../x'}]},
    'not a report',
])
def test_an_unexpected_recovery_report_changes_nothing(package, report):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    fake.recovery_report = report
    with pytest.raises(linux_upgrade.UpgradeError, match='unexpected recovery report. Nothing was changed'):
        _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    assert fake.recovery == [('review', NEW)] and fake.unpublished == [UNPUBLISHED]
    assert _mutations(fake) == [] and _recovery_record(path) == []


def test_a_target_that_may_be_published_refuses_with_its_reason(package):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    fake.unpublished_refusal = (b'Traceback (most recent call last):\n  File "/opt/x.py"\n'
                                b'ValueError: GlassHive Files projection may be published (a staged copy exists)\n')
    with pytest.raises(linux_upgrade.UpgradeError) as refused:
        _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    assert 'may be published (a staged copy exists). Nothing was changed' in str(refused.value)
    assert '/opt/' not in str(refused.value)
    assert fake.unpublished == [UNPUBLISHED] and _mutations(fake) == [] and _recovery_record(path) == []


def test_work_found_after_recovery_says_what_was_already_retired(package):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    fake.live_idle = False
    with pytest.raises(linux_upgrade.UpgradeError) as refused:
        _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    message = str(refused.value)
    assert 'not idle' in message and 'already retired (1)' in message and 'Nothing was changed' not in message
    assert fake.unpublished == [] and _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()
    record, = _recovery_record(path)
    assert json.loads(record.read_text())['retired'] == [UNPUBLISHED]


def test_nothing_unpublished_makes_recovery_a_no_op(package):
    fake, path, receipt = package
    result = _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    assert result['status'] == 'awaiting_commit' and 'files_recovered' not in result
    assert fake.recovery == [('review', NEW)] and _recovery_record(path) == []
    assert json.loads(path.read_text())['upgrade']['files_recovered'] == []



def test_a_set_that_changed_after_review_is_not_retired(package):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    later = 'prj_' + 'd' * 32
    fake.before_retire = lambda: fake.unpublished.append(later)  # a new attach on the running version
    with pytest.raises(linux_upgrade.UpgradeError, match='changed since they were reviewed. Nothing was changed'):
        _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    assert fake.unpublished == [UNPUBLISHED, later] and _mutations(fake) == []
    record, = _recovery_record(path)
    assert json.loads(record.read_text())['status'] == 'refused'


def test_a_retire_step_without_a_result_is_reported_as_unknown(package):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    fake.retire_crash = True
    with pytest.raises(linux_upgrade.UpgradeError) as refused:
        _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    message = str(refused.value)
    assert 'whether they were retired is unknown' in message and 'Nothing else was changed' in message
    assert 'Nothing was changed' not in message and _mutations(fake) == []
    record, = _recovery_record(path)
    assert json.loads(record.read_text())['status'] == 'retiring'


def test_the_recovery_record_keeps_identities_and_hashes_only(package):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    record = json.loads(_recovery_record(path)[0].read_text())
    assert record['status'] == 'retired' and record['retired'] == [UNPUBLISHED]
    assert {key for item in record['reviewed'] for key in item} == {'projection_id', 'upload_id', 'sha256',
                                                                     'size_bytes'}


def test_rollback_after_recovery_says_the_retired_files_are_not_restored(package):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    runner = _runner(path)
    runner.upgrade(service_image=NEW, recover_unpublished=True)
    result = runner.rollback()
    assert result['status'] == 'rolled_back' and result['files_recovered_not_restored'] == [UNPUBLISHED]
    assert 'no stored-file key' in result['files_note']



def test_a_retire_step_that_times_out_is_reported_as_unknown(package, monkeypatch):
    fake, path, receipt = package
    fake.unpublished = [UNPUBLISHED]
    original = linux_upgrade._helper

    def slow(endpoint, **kwargs):
        if kwargs.get('purpose') == 'files-recover':
            raise linux_upgrade.UpgradeError('The upgrade files-recover step timed out and was stopped')
        return original(endpoint, **kwargs)
    monkeypatch.setattr(linux_upgrade, '_helper', slow)
    with pytest.raises(linux_upgrade.UpgradeError, match='did not finish in time; whether they were retired is unknown'):
        _runner(path).upgrade(service_image=NEW, recover_unpublished=True)
    assert _mutations(fake) == []


ROLE_MAP = {'xperfect-admins': 'tenant_admin', 'xperfect-users': 'member'}
ROLE_KEYS = ('GLASSHIVE_OIDC_ROLE_CLAIM', 'GLASSHIVE_OIDC_ROLE_MAP_JSON')


@pytest.fixture
def hosted_package(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(linux_upgrade, '_docker', fake)

    def launcher(endpoint, *args, data=None):
        if args[:1] == ('run',) and 'keygen' in args:
            return json.dumps({'kty': 'RSA', 'n': 'x', 'e': 'AQAB', 'kid': args[args.index('--kid') + 1]})
        return fake.launcher_docker(endpoint, *args, data=data)
    monkeypatch.setattr(linux_launch, 'docker', launcher)
    monkeypatch.setattr(linux_launch, '_wait_until_hosted_ready', lambda *a, **k: None)
    monkeypatch.setattr(linux_upgrade.time, 'sleep', lambda seconds: None)
    secret, cert, key = (tmp_path / name for name in ('secret', 'cert.pem', 'key.pem'))
    for item in (secret, cert, key):
        item.write_text('synthetic')
        item.chmod(0o600)
    hosted = {'public_url': 'https://app.example.test:18443', 'mcp_public_url': 'https://mcp.example.test:18444/mcp',
              'issuer': 'https://idp.example.test/realms/x', 'client_id': 'ui', 'client_secret_file': str(secret),
              'tenant_id': 'fixture', 'tls_certificate_file': str(cert), 'tls_key_file': str(key),
              'xfs_mount': '/srv/data', 'xfs_device': '/dev/vdc'}
    receipt = linux_launch.launch_hosted(endpoint='unix:///docker.sock', name=NAME, image=OLD, native_image=NATIVE,
                                         ui_port=18443, mcp_port=18444, hosted=hosted)
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    # Real state the upgrade must carry: runtime rows, sign-ins and links.
    fake.volumes[f'{NAME}-control']['runtime.db'] = b'runtime rows v1'
    fake.volumes[f'{NAME}-auth']['auth.sqlite3'] = b'admitted people'
    fake.volumes[f'{NAME}-links']['link-refs.sqlite3'] = b'links'
    fake.calls.clear()
    return fake, path, receipt


def _mapping():
    return linux_launch.validate_role_mapping('roles', ROLE_MAP)


def test_a_role_mapping_reaches_only_the_sign_in_services_and_rolls_back(hosted_package):
    fake, path, receipt = hosted_package
    before = {name: dict(files) for name, files in fake.volumes.items()}
    text = path.read_text()
    runner = _runner(path)
    result = runner.upgrade(service_image=OLD, role_map=ROLE_MAP)  # the mapping alone is a meaningful change
    assert result['status'] == 'awaiting_commit' and result['role_mapping'] == _mapping()
    assert 'sign in as a mapped tenant_admin' in result['role_mapping_note']
    assert 'admission only decides who may sign in' in result['role_mapping_note']
    for role in ('ui', 'mcp'):
        env = _config(fake, role)['environment']
        assert env['GLASSHIVE_OIDC_ROLE_CLAIM'] == 'roles' and json.loads(env['GLASSHIVE_OIDC_ROLE_MAP_JSON']) == ROLE_MAP
    assert not set(ROLE_KEYS) & set(_config(fake, 'runtime')['environment'])
    recorded = json.loads(path.read_text())
    assert recorded['role_mapping'] == _mapping() and recorded['upgrade']['role_mapping_changed'] is True
    runner.rollback()
    for name, files in before.items():
        assert fake.volumes[name] == files, name
    assert path.read_text() == text


def test_an_unchanged_mapping_is_no_upgrade_and_removal_says_stored_roles_remain(hosted_package):
    fake, path, receipt = hosted_package
    runner = _runner(path)
    runner.upgrade(service_image=OLD, role_map=ROLE_MAP)
    runner.commit()
    with pytest.raises(linux_upgrade.UpgradeError, match='already runs this image'):
        _runner(path).upgrade(service_image=OLD, role_map=ROLE_MAP)
    with pytest.raises(linux_upgrade.UpgradeError, match='already runs this image'):
        _runner(path).upgrade(service_image=OLD)  # an upgrade without role options keeps the mapping
    result = _runner(path).upgrade(service_image=OLD, clear_roles=True)
    assert result['role_mapping'] == 'removed' and 'Roles stored while the map was on remain' in result['role_mapping_note']
    for role in ('ui', 'mcp'):
        assert not set(ROLE_KEYS) & set(_config(fake, role)['environment'])
    assert 'role_mapping' not in json.loads(path.read_text())


def test_role_mapping_is_for_hosted_packages_only(package):
    fake, path, receipt = package
    with pytest.raises(linux_upgrade.UpgradeError, match='hosted packages only'):
        _runner(path).upgrade(service_image=NEW, role_map=ROLE_MAP)
    assert _mutations(fake) == []


def test_differing_ui_and_mcp_mappings_are_refused_unchanged(hosted_package):
    fake, path, receipt = hosted_package
    volume = fake.volume_at(fake.find(f'{NAME}-mcp'), '/mcp-state')
    config = json.loads(volume['config.json'])
    config['environment'].update(linux_launch.role_environment(_mapping()))
    volume['config.json'] = json.dumps(config).encode()
    with pytest.raises(linux_upgrade.UpgradeError, match='unreadable or differ.*--role-map.*--no-role-map'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []
    # The role options rewrite both configurations, so they repair it.
    result = _runner(path).upgrade(service_image=OLD, role_map=ROLE_MAP)
    assert result['role_mapping'] == _mapping()
    assert json.loads(_config(fake, 'ui')['environment']['GLASSHIVE_OIDC_ROLE_MAP_JSON']) == ROLE_MAP


@pytest.mark.parametrize('extra,message', [
    (['--role-map', 'xperfect-admins=tenant_admin', '--no-role-map'], 'not both'),
    (['--role-map', 'xperfect-admins=service'], 'member, viewer or tenant_admin'),
    (['--role-map', 'tenant_admin'], 'PROVIDER_ROLE=ROLE'),
    (['--role-map', 'a=member', '--role-map', 'a=viewer'], 'once per provider role'),
    (['--role-claim', 'roles'], 'needs a role_map'),
    (['--role-map', 'staff=tenant_admin', '--role-claim', 'email'], 'not a profile claim'),
    (['--role-map', 'ada@example.com=tenant_admin'], 'not people'),
])
def test_role_options_are_refused_before_any_change(hosted_package, extra, message):
    fake, path, receipt = hosted_package
    with pytest.raises(linux_upgrade.UpgradeError, match=message):
        linux_upgrade.main(['upgrade', '--service-image', NEW, '--docker-host', 'unix:///docker.sock',
                            '--receipt', str(path), *extra])
    assert _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()


def test_a_direct_call_cannot_map_the_service_role(hosted_package):
    fake, path, receipt = hosted_package
    with pytest.raises(linux_upgrade.UpgradeError, match='member, viewer or tenant_admin'):
        _runner(path).upgrade(service_image=OLD, role_map={'x': 'service'})
    assert _mutations(fake) == []


def test_a_new_map_keeps_the_current_claim_unless_another_is_named(hosted_package):
    fake, path, receipt = hosted_package
    runner = _runner(path)
    runner.upgrade(service_image=OLD, role_map=ROLE_MAP, role_claim='groups')
    runner.commit()
    _runner(path).upgrade(service_image=OLD, role_map={'xperfect-admins': 'tenant_admin'})
    assert _config(fake, 'mcp')['environment']['GLASSHIVE_OIDC_ROLE_CLAIM'] == 'groups'



LOCAL_KEYS = ('GLASSHIVE_LOCAL_HUMAN_ASSERTION', 'GLASSHIVE_INTERNAL_ASSERTION_ISSUER',
              'GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE', 'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE',
              'GLASSHIVE_INTERNAL_ASSERTION_KEY_ID', 'GLASSHIVE_INTERNAL_ASSERTION_TTL_SECONDS',
              'GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE')


def _channel(fake):
    ui, runtime, mcp = (_config(fake, role)['environment'] for role in ('ui', 'runtime', 'mcp'))
    control = fake.volume_at(fake.find(f'{NAME}-runtime'), '/control')
    ui_state = fake.volume_at(fake.find(f'{NAME}-ui'), '/ui-state')
    return ui, runtime, mcp, control, ui_state


def _earlier_local_package(fake):
    """As an earlier launcher left a local package: no confirmation channel anywhere."""
    for role in linux_upgrade.ROLES:
        volume = fake.volume_at(fake.find(f'{NAME}-{role}'), '/' + linux_upgrade.STATE[role])
        config = json.loads(volume['config.json'])
        for key in LOCAL_KEYS:
            config['environment'].pop(key, None)
        if role == 'runtime':
            config['environment'].pop('GLASSHIVE_HUMAN_AUTH_MODE', None)
            volume.pop('assertion-jwks.json', None)
        if role == 'ui':
            volume.pop('assertion-key.pem', None)
        volume['config.json'] = json.dumps(config).encode()


def test_a_fresh_local_package_signs_in_the_ui_and_verifies_in_the_runtime_only(package):
    fake, path, receipt = package
    ui, runtime, mcp, control, ui_state = _channel(fake)
    kid = ui['GLASSHIVE_INTERNAL_ASSERTION_KEY_ID']
    assert receipt['signing_key_ids'] == {'ui': kid} and kid.startswith('xperfect-local-ui-')
    assert ui['GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE'] == '/ui-state/assertion-key.pem'
    assert 'assertion-key.pem' in ui_state and 'assertion-key.pem' not in control
    assert runtime['GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE'] == '/control/assertion-jwks.json'
    assert 'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE' not in runtime
    assert [key['kid'] for key in json.loads(control['assertion-jwks.json'])['keys']] == [kid]
    assert not any('d' in key for key in json.loads(control['assertion-jwks.json'])['keys'])
    assert not set(LOCAL_KEYS) & set(mcp)
    for key in ('GLASSHIVE_LOCAL_HUMAN_ASSERTION', 'GLASSHIVE_INTERNAL_ASSERTION_ISSUER',
                'GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE'):
        assert ui[key] == runtime[key]


def test_an_earlier_local_package_gains_the_channel_in_one_reversible_upgrade(package):
    fake, path, receipt = package
    _earlier_local_package(fake)
    before = {name: dict(files) for name, files in fake.volumes.items()}
    text = path.read_text()
    runner = _runner(path)
    result = runner.upgrade(service_image=OLD)  # the missing channel alone makes a same-image upgrade meaningful
    kid = result['local_assertion']['ui_key_id']
    assert result['local_assertion']['added'] and 'confirm workspace sharing' in result['local_assertion_note']
    ui, runtime, mcp, control, ui_state = _channel(fake)
    assert ui['GLASSHIVE_INTERNAL_ASSERTION_KEY_ID'] == kid and 'assertion-key.pem' in ui_state
    assert json.loads(control['assertion-jwks.json'])['keys'][0]['kid'] == kid
    assert runtime['GLASSHIVE_HUMAN_AUTH_MODE'] == 'local_password' and not set(LOCAL_KEYS) & set(mcp)
    assert json.loads(path.read_text())['signing_key_ids'] == {'ui': kid}
    runner.rollback()
    for name, files in before.items():
        assert fake.volumes[name] == files, name
    assert path.read_text() == text


def test_the_channel_is_kept_across_upgrades(package):
    fake, path, receipt = package
    ui_before, *_, ui_state = _channel(fake)
    key_bytes = ui_state['assertion-key.pem']
    fake.keygens.clear()
    result = _runner(path).upgrade(service_image=NEW)
    ui_after, runtime, mcp, control, ui_state = _channel(fake)
    assert 'local_assertion' not in result and fake.keygens == []
    assert ui_after['GLASSHIVE_INTERNAL_ASSERTION_KEY_ID'] == ui_before['GLASSHIVE_INTERNAL_ASSERTION_KEY_ID']
    assert ui_state['assertion-key.pem'] == key_bytes
    _runner(path).commit()
    with pytest.raises(linux_upgrade.UpgradeError, match='already runs this image and configuration'):
        _runner(path).upgrade(service_image=NEW)  # a present channel is not a reason to upgrade again


def test_a_signer_that_cannot_be_made_returns_the_earlier_package_unchanged(package):
    fake, path, receipt = package
    _earlier_local_package(fake)
    before = {name: dict(files) for name, files in fake.volumes.items()}
    fake.keygen_fails = True
    with pytest.raises(linux_upgrade.UpgradeError, match='previous version is running again.*UI signing key'):
        _runner(path).upgrade(service_image=OLD)
    for name, files in before.items():
        assert fake.volumes[name] == files, name
    assert not _runner(path).journal_path.exists()


@pytest.mark.parametrize('damage', ['runtime_verifies_nothing', 'runtime_holds_private_key', 'mcp_holds_key'])
def test_a_partial_or_misplaced_channel_is_refused_unchanged(package, damage):
    fake, path, receipt = package
    role, key, value = {'runtime_verifies_nothing': ('runtime', 'GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE', None),
                        'runtime_holds_private_key': ('runtime', 'GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE',
                                                      '/ui-state/assertion-key.pem'),
                        'mcp_holds_key': ('mcp', 'GLASSHIVE_INTERNAL_ASSERTION_KEY_ID', 'stolen')}[damage]
    volume = fake.volume_at(fake.find(f'{NAME}-{role}'), '/' + linux_upgrade.STATE[role])
    config = json.loads(volume['config.json'])
    if value is None:
        config['environment'].pop(key)
    else:
        config['environment'][key] = value
    volume['config.json'] = json.dumps(config).encode()
    with pytest.raises(linux_upgrade.UpgradeError, match='incomplete or misplaced. Nothing was changed'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == []


def test_hosted_packages_keep_their_own_signers(hosted_package):
    fake, path, receipt = hosted_package
    result = _runner(path).upgrade(service_image=NEW)
    assert 'local_assertion' not in result
    assert 'GLASSHIVE_LOCAL_HUMAN_ASSERTION' not in _config(fake, 'ui')['environment']


# -- workers network isolation ------------------------------------------------
ISOLATED = {linux_launch.ICC_OPTION: 'false'}


def _attached(fake, identity):
    return fake.find(identity)['NetworkSettings']['Networks']


def test_a_fresh_package_isolates_only_its_workers_network(package):
    fake, path, receipt = package
    assert fake.networks[f'{NAME}-workers']['Options'] == ISOLATED
    assert fake.networks[f'{NAME}-frontend']['Options'] == {}
    assert _attached(fake, receipt['containers']['runtime'])[f'{NAME}-workers'] == {'Aliases': ['runtime']}


def test_an_earlier_shared_workers_network_is_isolated_and_rollback_restores_it(package):
    fake, path, receipt = package
    _older_launcher(fake)
    previous = receipt['containers']['runtime']
    runner = _runner(path)
    runner.upgrade(service_image=NEW)
    upgraded = json.loads(path.read_text())['containers']['runtime']
    assert fake.networks[f'{NAME}-workers']['Options'] == ISOLATED
    assert _attached(fake, upgraded)[f'{NAME}-workers'] == {'Aliases': ['runtime']}
    assert f'{NAME}-workers' not in _attached(fake, previous)
    runner.rollback()
    assert fake.networks[f'{NAME}-workers']['Options'] == {}
    assert _attached(fake, previous)[f'{NAME}-workers'] == {'Aliases': ['runtime']}
    assert fake.find(previous)['State']['Running'] is True


def test_a_committed_isolation_needs_no_second_migration(package):
    fake, path, receipt = package
    _older_launcher(fake)
    runner = _runner(path)
    runner.upgrade(service_image=NEW)
    runner.commit()
    fake.calls.clear()
    runner.upgrade(service_image=OLD)
    assert not [call for call in fake.calls if call[:2] in {('network', 'rm'), ('network', 'create')}]
    assert fake.networks[f'{NAME}-workers']['Options'] == ISOLATED


def test_an_image_that_needs_container_traffic_is_refused_on_an_isolated_package(package):
    fake, path, receipt = package
    earlier = {k: v for k, v in linux_launch.PROFILE_SETTINGS['local-linux'].items() if k != 'XPERFECT_WORKER_NETWORK'}
    fake.image_settings = {NEW: {'local-linux': earlier}}
    with pytest.raises(linux_upgrade.UpgradeError, match='isolates its workspace containers'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()


def test_a_workers_network_another_container_uses_is_not_replaced(package):
    fake, path, receipt = package
    _older_launcher(fake)
    # A stopped workspace container the idle proof did not see still names the bridge.
    stray = json.loads(json.dumps(fake.find(receipt['containers']['ui'])))
    stray.update({'Id': 'f' * 64, 'Name': '/xperfect-wsp-fixture', 'State': {'Running': False},
                  'NetworkSettings': {'Networks': {f'{NAME}-workers': {'Aliases': []}}}})
    fake.containers[stray['Id']] = stray
    with pytest.raises(linux_upgrade.UpgradeError, match='still uses the workers network'):
        _runner(path).upgrade(service_image=NEW)
    previous = receipt['containers']['runtime']
    assert fake.networks[f'{NAME}-workers']['Options'] == {}
    assert _attached(fake, previous)[f'{NAME}-workers'] == {'Aliases': ['runtime']}
    assert fake.find(previous)['State']['Running'] is True
    assert not linux_upgrade.journal_path(path).exists()


@pytest.mark.parametrize('step', [('network', 'rm'), ('network', 'create'), ('network', 'connect')])
def test_an_interrupted_isolation_is_restored_by_the_automatic_rollback(package, step):
    fake, path, receipt = package
    _older_launcher(fake)
    fake.fail_calls_once.append(step)
    with pytest.raises(linux_upgrade.UpgradeError, match='previous version is running again'):
        _runner(path).upgrade(service_image=NEW)
    previous = receipt['containers']['runtime']
    assert fake.networks[f'{NAME}-workers']['Options'] == {}
    assert _attached(fake, previous)[f'{NAME}-workers'] == {'Aliases': ['runtime']}
    assert all(fake.find(identity)['State']['Running'] for identity in receipt['containers'].values())
    assert not linux_upgrade.journal_path(path).exists()


# -- local-QA fault authority: explicit, local-only, runtime-only, never journaled --

QA_AUTHORITY = {
    'VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE': 'xpf_coord_001',
    'VIVENTIUM_LOCAL_QA_CASE_ID': 'XPF-COORD-001',
    'VIVENTIUM_LOCAL_QA_CASE_TOKEN': 'case-token-' + 'x' * 40,
    'VIVENTIUM_LOCAL_QA_SESSION_REF': 'session-' + 'y' * 20,
    'VIVENTIUM_LOCAL_QA_CANDIDATE_DIGEST': 'sha256:' + 'c' * 64,
    'VIVENTIUM_LOCAL_QA_COMPONENT_ARTIFACT_DIGEST': 'sha256:' + 'd' * 64,
}


def test_local_qa_authority_reaches_only_the_runtime_and_rollback_removes_it(package):
    fake, path, receipt = package
    runner = _runner(path)
    assert runner.upgrade(service_image=NEW, local_qa_authority=dict(QA_AUTHORITY))['status'] == 'awaiting_commit'
    runtime = _config(fake)['environment']
    assert {key: runtime[key] for key in QA_AUTHORITY} == QA_AUTHORITY
    for role in ('ui', 'mcp'):
        assert not set(QA_AUTHORITY) & set(_config(fake, role)['environment'])
    journal = linux_upgrade.journal_path(path).read_text()
    assert QA_AUTHORITY['VIVENTIUM_LOCAL_QA_CASE_TOKEN'] not in journal
    assert json.loads(journal)['local_qa_authority'] == 'set'

    runner.rollback()

    assert not set(QA_AUTHORITY) & set(_config(fake)['environment'])


@pytest.mark.parametrize('authority', [
    {key: value for key, value in QA_AUTHORITY.items() if key != 'VIVENTIUM_LOCAL_QA_CASE_TOKEN'},
    {**QA_AUTHORITY, 'EXTRA_SETTING': 'value'},
    {**QA_AUTHORITY, 'VIVENTIUM_LOCAL_QA_SESSION_REF': '  '},
    {**QA_AUTHORITY, 'VIVENTIUM_LOCAL_QA_SESSION_REF': 'line\nbreak'},
])
def test_an_invalid_local_qa_authority_changes_nothing(package, authority):
    fake, path, receipt = package
    with pytest.raises(linux_upgrade.UpgradeError, match='Nothing was changed'):
        _runner(path).upgrade(service_image=NEW, local_qa_authority=authority)
    assert not _mutations(fake)
    assert not linux_upgrade.journal_path(path).exists()


def test_a_hosted_package_refuses_the_local_qa_authority(package, monkeypatch):
    fake, path, receipt = package
    monkeypatch.setattr(linux_upgrade, 'validate_receipt', lambda value: ('hosted-xfs', NAME))
    with pytest.raises(linux_upgrade.UpgradeError, match='only for a local package'):
        _runner(path).upgrade(service_image=NEW, local_qa_authority=dict(QA_AUTHORITY))
    assert not _mutations(fake)


def test_a_later_upgrade_clears_the_local_qa_authority(package):
    fake, path, receipt = package
    runner = _runner(path)
    runner.upgrade(service_image=NEW, local_qa_authority=dict(QA_AUTHORITY))
    runner.commit()
    runner.upgrade(service_image=NEW, clear_local_qa=True)
    assert not set(QA_AUTHORITY) & set(_config(fake)['environment'])


CLOSED = {'worker_id': 'wrk_' + '9' * 10, 'runs': ['run_' + 'd' * 10], 'leases': ['hrl_' + 'e' * 32]}
CLOSED_IDENTITIES = sorted([CLOSED['worker_id'], *CLOSED['runs'], *CLOSED['leases']])


def _closed_work_record(path):
    return sorted(path.parent.glob(path.name + '.closed-work-*.json'))


def test_a_closed_workers_open_work_blocks_every_proof_until_the_new_release_settles_it(package):
    fake, path, receipt = package
    fake.closed_work, fake.closed_boxes = [dict(CLOSED)], ['xperfect-wsp-synthetic']
    with pytest.raises(linux_upgrade.UpgradeError, match='active work must be quiesced'):
        _runner(path).upgrade(service_image=NEW)
    assert _mutations(fake) == [] and fake.settlement == [] and not linux_upgrade.journal_path(path).exists()
    fake.calls.clear()
    result = _runner(path).upgrade(service_image=NEW, settle_closed_work=True)
    assert result['status'] == 'awaiting_commit'
    assert result['closed_work_settled'] == {'settled': CLOSED_IDENTITIES, 'boxes_released': ['xperfect-wsp-synthetic']}
    assert json.loads(path.read_text())['upgrade']['closed_work_settled'] == CLOSED_IDENTITIES
    # The new release reviewed, then settled, with Docker for the generation proof, before any idle proof ran.
    assert fake.settlement == [('review', NEW, True), ('settle', NEW, True)] and fake.closed_work == []
    first_proof = next(i for i, call in enumerate(fake.calls) if call[:1] == ('exec',) or (
        call[:1] == ('run',) and any('-live-' in item for item in call)))
    settlement_runs = [i for i, call in enumerate(fake.calls)
                       if call[:1] == ('run',) and any(item.endswith(('-closed-review', '-closed-settle')) for item in call)]
    assert len(settlement_runs) == 2 and max(settlement_runs) < first_proof
    record, = _closed_work_record(path)
    assert record.stat().st_mode & 0o077 == 0
    evidence = json.loads(record.read_text())
    assert evidence['status'] == 'settled' and evidence['reviewed'] == [CLOSED]
    assert evidence['boxes_released'] == ['xperfect-wsp-synthetic']
    assert _runner(path).commit()['status'] == 'committed'


def test_closed_work_settlement_needs_a_new_image_that_carries_it_and_refuses_with_its_reason(package):
    fake, path, receipt = package
    fake.closed_work = [dict(CLOSED)]
    _older_launcher(fake)  # a same-image upgrade that has settings to add
    with pytest.raises(linux_upgrade.UpgradeError, match='needs a new image that carries it. Nothing'):
        _runner(path).upgrade(service_image=OLD, settle_closed_work=True)
    fake.recovery_images = set()
    with pytest.raises(linux_upgrade.UpgradeError, match='does not carry this recovery. Nothing was changed'):
        _runner(path).upgrade(service_image=NEW, settle_closed_work=True)
    fake.recovery_images = {NEW}
    fake.closed_work_refusal = (b'Traceback (most recent call last):\n  File "/opt/x.py"\n'
                                b"ValueError: GlassHive closed worker's recorded generation is not proved stopped\n")
    with pytest.raises(linux_upgrade.UpgradeError) as refused:
        _runner(path).upgrade(service_image=NEW, settle_closed_work=True)
    assert 'is not proved stopped. Nothing was changed' in str(refused.value) and '/opt/' not in str(refused.value)
    assert fake.closed_work == [CLOSED] and _mutations(fake) == [] and _closed_work_record(path) == []
    assert not linux_upgrade.journal_path(path).exists()


@pytest.mark.parametrize('report', [
    {'closed_workers': 1, 'applied': True, 'targets': [CLOSED], 'boxes_released': []},  # a review that applied
    {'closed_workers': 2, 'applied': False, 'targets': [CLOSED], 'boxes_released': []},
    {'closed_workers': 1, 'applied': False, 'targets': [{**CLOSED, 'runs': ['../x']}], 'boxes_released': []},
    {'closed_workers': 1, 'applied': False, 'targets': [CLOSED], 'boxes_released': ['xperfect-wsp-early']},
    'not a report',
])
def test_an_unexpected_closed_work_report_changes_nothing(package, report):
    fake, path, receipt = package
    fake.closed_work = [dict(CLOSED)]
    fake.closed_work_report = report
    with pytest.raises(linux_upgrade.UpgradeError, match='unexpected closed-worker report. Nothing was changed'):
        _runner(path).upgrade(service_image=NEW, settle_closed_work=True)
    assert [step for step, _, _ in fake.settlement] == ['review'] and fake.closed_work == [CLOSED]
    assert _mutations(fake) == [] and _closed_work_record(path) == []


def test_work_found_after_settlement_says_what_was_already_settled(package):
    fake, path, receipt = package
    fake.closed_work = [dict(CLOSED)]
    fake.live_idle = False
    with pytest.raises(linux_upgrade.UpgradeError) as refused:
        _runner(path).upgrade(service_image=NEW, settle_closed_work=True)
    message = str(refused.value)
    assert 'not idle' in message and 'already settled (3 identities)' in message
    assert 'Nothing was changed' not in message
    assert fake.closed_work == [] and _mutations(fake) == [] and not linux_upgrade.journal_path(path).exists()
    record, = _closed_work_record(path)
    assert json.loads(record.read_text())['status'] == 'settled'


def test_nothing_to_settle_is_a_no_op_and_rollback_says_settled_work_is_not_restored(package):
    fake, path, receipt = package
    result = _runner(path).upgrade(service_image=NEW, settle_closed_work=True)
    assert 'closed_work_settled' not in result and _closed_work_record(path) == []
    assert _runner(path).rollback()['status'] == 'rolled_back'
    fake.closed_work = [dict(CLOSED)]
    runner = _runner(path)
    runner.upgrade(service_image=NEW, settle_closed_work=True)
    result = runner.rollback()
    assert result['status'] == 'rolled_back' and result['closed_work_settled_not_restored'] == CLOSED_IDENTITIES
