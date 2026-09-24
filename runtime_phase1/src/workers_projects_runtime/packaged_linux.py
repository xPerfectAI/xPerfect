"""Measure the configured Linux substrate before exposing native account launch."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess

from .execution_profile import packaged_linux
from .workspace_box import WorkspaceBoxUnavailable


def _docker(arguments):
    result = subprocess.run(['docker', *arguments], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise WorkspaceBoxUnavailable('Packaged Linux substrate verification failed')
    return result.stdout


def account_launcher_from_environment():
    if not packaged_linux():
        return None
    from .account_container import create_packaged_account_launcher
    data = Path(os.environ['XPERFECT_SHARED_VOLUME_ROOT'])
    control = Path(os.environ['XPERFECT_CONTROL_ROOT'])
    for path in (data, control):
        info = path.lstat()
        if (not path.is_absolute() or path.resolve(strict=True) != path
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()):
            raise WorkspaceBoxUnavailable('Packaged storage mount identity is invalid')
    image = os.environ['XPERFECT_SHARED_IMAGE']
    controller_id = os.environ['XPERFECT_CONTROLLER_ID']
    volume = os.environ['XPERFECT_SHARED_VOLUME_NAME']
    network = os.environ['XPERFECT_SHARED_NETWORK']
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image) or not re.fullmatch(r'[a-f0-9]{64}', controller_id):
        raise WorkspaceBoxUnavailable('Packaged image/controller identities are required')
    info = json.loads(_docker(['info', '--format', '{{json .}}']))
    if info.get('OSType') != 'linux' or str(info.get('CgroupVersion')) != '2':
        raise WorkspaceBoxUnavailable('Linux cgroup v2 is required')
    if _docker(['image', 'inspect', '--format', '{{.Id}}', image]).strip() != image:
        raise WorkspaceBoxUnavailable('Native image identity changed')
    record = json.loads(_docker(['inspect', controller_id]))[0]
    mounted = {item['Destination']: item for item in record['Mounts']}
    if (record['Id'] != controller_id or record['State'].get('Running') is not True
            or mounted.get(str(data), {}).get('Type') != 'volume'
            or mounted[str(data)].get('Name') != volume
            or str(control) not in mounted or mounted[str(control)].get('Type') != 'volume'
            or mounted[str(control)].get('Name') == volume
            or network not in record['NetworkSettings']['Networks']):
        raise WorkspaceBoxUnavailable('Controller data/control/network mounts changed')
    # A fresh challenge checks the live named-volume view. The probe receives
    # only this disposable public nonce subtree, never account or control data.
    name = '.substrate-' + secrets.token_hex(16)
    probe = data / name
    probe.mkdir(mode=0o755)
    probe.chmod(0o755)  # Service umask is private; this subtree contains only a public nonce.
    nonce = secrets.token_hex(32)
    try:
        challenge = probe / 'challenge'
        challenge.write_text(nonce)
        challenge.chmod(0o444)
        program = '''import json,pathlib,pwd,ctypes
import workers_projects_runtime.account_native_entry
ctypes.CDLL("libseccomp.so.2")
ids=(20001,60000,100001,200000)
assert all(pwd.getpwuid(uid).pw_name == ("member-" if uid<100000 else "account-")+str(uid) for uid in ids)
print(json.dumps({"nonce":pathlib.Path("/probe/challenge").read_text(),"uids":list(ids)}))'''
        observed = json.loads(_docker(['run', '--rm', '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '65534:65534',
            '--ipc', 'none', '--no-healthcheck', '--memory', str(128 * 1024**2), '--pids-limit', '16',
            '--mount', f'type=volume,src={volume},dst=/probe,volume-subpath={name},readonly,volume-nocopy',
            '--entrypoint', '/usr/bin/python3', image, '-I', '-m',
            'workers_projects_runtime.storage_quota_guard', '--', '/usr/bin/python3', '-I', '-c', program]))
        if observed != {'nonce': nonce, 'uids': [20001, 60000, 100001, 200000]}:
            raise WorkspaceBoxUnavailable('Live native volume/image proof did not match')
    finally:
        (probe / 'challenge').unlink(missing_ok=True)
        probe.rmdir()
    return create_packaged_account_launcher(
        execution_profile=os.environ['XPERFECT_EXECUTION_PROFILE'], control_root=control,
        data_root=data, volume_name=volume, image_id=image, network=network,
        memory_bytes=int(os.environ.get('XPERFECT_ACCOUNT_MEMORY_BYTES', str(1024**3))),
        pids_limit=int(os.environ.get('XPERFECT_ACCOUNT_PIDS_LIMIT', '256')))


def recover_account_launches(binder):
    """Resolve durable exact account generations before admitting any new work."""
    launcher = binder.homes.native_launcher
    if launcher is None or binder.store is None:
        raise WorkspaceBoxUnavailable('Packaged account recovery is unavailable')
    for generation in launcher.ledger.active_generations():
        with binder.store._connect() as conn:
            row = conn.execute('SELECT * FROM provider_account_leases WHERE lease_id=?',
                               (generation.lease_id,)).fetchone()
        if row is None:
            raise WorkspaceBoxUnavailable('Pending account generation has no exact lease')
        lease = dict(row)
        expected = binder.homes.account_home_path(tenant_id=lease['tenant_id'], owner_id=lease['owner_id'],
                                                  account_id=lease['account_id'])
        if expected != generation.account_home:
            raise WorkspaceBoxUnavailable('Pending account generation owner changed')
        receipt = launcher.recover(expected, lease_id=generation.lease_id)
        if (receipt is None or receipt.lease_id != generation.lease_id
                or receipt.generation != generation.generation
                or not receipt.all_children_absent or not receipt.ownership_reclaimed):
            raise WorkspaceBoxUnavailable('Account recovery did not prove complete containment stop')
        if lease['released_at'] is None:
            binder.store.release_provider_lease(lease_id=generation.lease_id,
                tenant_id=lease['tenant_id'], owner_id=lease['owner_id'])
