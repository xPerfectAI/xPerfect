import json
import hashlib
import os
from pathlib import Path
import socket
import stat
import subprocess
from types import SimpleNamespace

import pytest

from workers_projects_runtime.account_container import DockerAccountContainerBackend, PackagedAccountLauncher, ACCOUNT_MOUNT
from workers_projects_runtime.account_native_launch import NativeAccountLaunch
from workers_projects_runtime.contained_account_launch import AccountGeneration, AccountContainmentUncertain
from workers_projects_runtime.storage_quota import QuotaUnavailable


class Docker:
    def __init__(self):self.value=None;self.calls=[];self.fail_inventory=False;self.extra_mount=False
    def __call__(self, args, check=True):
        self.calls.append(args)
        if args[0]=='inspect':
            return subprocess.CompletedProcess(args,0,json.dumps([self.value])) if self.value else subprocess.CompletedProcess(args,1,'')
        if args[:3]==['container','ls','-a']:
            if self.fail_inventory:raise AccountContainmentUncertain('synthetic inventory unavailable')
            return subprocess.CompletedProcess(args,0,'')
        if args[0]=='create':
            mount=args[args.index('--mount')+1];subpath=mount.split('volume-subpath=')[1].split(',')[0]
            labels={args[i+1].split('=',1)[0]:args[i+1].split('=',1)[1] for i,a in enumerate(args) if a=='--label'}
            self.value={'Id':'a'*64,'Image':'sha256:'+'b'*64,'Config':{'User':'100001:100001','Entrypoint':['/bin/sleep'],'Cmd':['infinity'],'Labels':labels,'Healthcheck':{'Test':['NONE']}},
                'HostConfig':{'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true'],'IpcMode':'none','NetworkMode':'synthetic-network',
                  'Memory':123456789,'MemorySwap':123456789,'PidsLimit':32,'Mounts':[{'Type':'volume','Source':'synthetic-data','Target':ACCOUNT_MOUNT,'VolumeOptions':{'Subpath':subpath,'NoCopy':True}}]},
                'NetworkSettings':{'Networks':{'synthetic-network':{}}},
                'Mounts':[{'Type':'volume','Name':'synthetic-data','Destination':ACCOUNT_MOUNT,'RW':True}]}
            if self.extra_mount:self.value['Mounts'].append({'Type':'bind','Destination':'/control'})
            return subprocess.CompletedProcess(args,0,'a'*64)
        if args[0]=='rm':self.value=None
        return subprocess.CompletedProcess(args,0,'')


@pytest.fixture
def backend(tmp_path):
    data=tmp_path/'data';home=data/'owner'/'acct_fixture';home.mkdir(parents=True)
    info=home.stat();generation=AccountGeneration(home,'lease-fixture','c'*32,100001,info.st_dev,info.st_ino)
    docker=Docker();transports=[]
    value=DockerAccountContainerBackend(data_root=data,volume_name='synthetic-data',image_id='sha256:'+'b'*64,network='synthetic-network',memory_bytes=123456789,pids_limit=32,
        docker=docker,popen=lambda command,**options:transports.append((command,options)) or 'transport')
    return value,docker,generation,transports


def test_dedicated_container_mount_and_secret_delivery_are_exact(backend,monkeypatch):
    value,docker,generation,transports=backend
    monkeypatch.setattr(os,'geteuid',lambda:0)
    ownership=[];monkeypatch.setattr(value,'_ownership',lambda home,uid:ownership.append((home,uid)))
    request=NativeAccountLaunch(generation.account_home,('/usr/local/bin/claude','auth','status'),{'HOME':str(generation.account_home),'ANTHROPIC_API_KEY':'synthetic-secret'},'verify',generation.lease_id)
    transport,identifier=value.start(request,generation,stdin=subprocess.PIPE,pass_fds=(999,))
    assert transport=='transport' and identifier=='a'*64
    command,options=transports[0]
    assert command[:3]==['docker','exec','-i']
    assert 'workers_projects_runtime.storage_quota_guard' in command
    assert 'workers_projects_runtime.account_native_entry' in command
    assert 'synthetic-secret' not in json.dumps(docker.calls+ [command])
    assert 'ANTHROPIC_API_KEY' not in options['env'] and 'pass_fds' not in options
    payload=json.loads(next(generation.account_home.glob('.xperfect-launch-*')).read_text())
    assert payload['environment']['HOME']==ACCOUNT_MOUNT
    assert payload['environment']['TMPDIR'].startswith(ACCOUNT_MOUNT+'/')
    assert payload['environment']['USER']=='account-100001'
    receipt=value.finish(generation)
    assert receipt.all_children_absent and receipt.ownership_reclaimed
    assert ownership==[(generation.account_home,100001),(generation.account_home,0)]
    assert not list(generation.account_home.glob('.xperfect-launch-*'))


def test_foreign_mount_stops_before_any_native_exec(backend,monkeypatch):
    value,docker,generation,transports=backend
    monkeypatch.setattr(os,'geteuid',lambda:0);monkeypatch.setattr(value,'_ownership',lambda *_:None)
    docker.extra_mount=True
    request=NativeAccountLaunch(generation.account_home,('/usr/local/bin/codex',),{},'verify',generation.lease_id)
    with pytest.raises(AccountContainmentUncertain):value.start(request,generation)
    assert not transports


def test_failed_inventory_is_not_absence_or_reclaim(backend,monkeypatch):
    value,docker,generation,_=backend
    docker.fail_inventory=True
    monkeypatch.setattr(value,'_ownership',lambda *_:pytest.fail('ownership reclaimed without absence'))
    with pytest.raises(AccountContainmentUncertain):value.finish(generation)


def test_account_selector_cannot_import_another_home(backend):
    value,_,generation,_=backend
    request=NativeAccountLaunch(generation.account_home,('/usr/local/bin/codex',),{'CODEX_HOME':'/other/account'},'verify',generation.lease_id)
    with pytest.raises(Exception,match='inside the selected account'):value._prepare_payload(request,generation)


def test_native_account_image_contains_grok_verification_helper():
    recipe = (Path(__file__).parents[1] / 'containers' / 'Dockerfile.shared').read_text()
    assert 'src/workers_projects_runtime/grok_acp.py' in recipe
    assert 'src/workers_projects_runtime/grok_auth.py' in recipe


@pytest.mark.parametrize('kind', ['socket', 'fifo'])
def test_stopped_native_cleanup_removes_transient_ipc_nodes(backend, kind, monkeypatch):
    value, _, generation, _ = backend
    # macOS forbids fchown even when the caller owns the temporary directory;
    # the packaged Linux path performs the real ownership transfer.
    monkeypatch.setattr(os, 'fchown', lambda *_: None)
    path = generation.account_home / 'transient-ipc'
    sock = None
    if kind == 'socket':
        sock = socket.socket(socket.AF_UNIX)
        previous = os.getcwd()
        os.chdir(generation.account_home)
        try:
            sock.bind(path.name)
        finally:
            os.chdir(previous)
    else:
        os.mkfifo(path, 0o600)
    try:
        value._ownership(generation.account_home, os.geteuid())
        assert not path.exists()
    finally:
        if sock is not None:
            sock.close()


def test_hosted_constructor_rejects_control_on_data_filesystem(tmp_path,monkeypatch):
    import sys
    from workers_projects_runtime.account_container import create_packaged_account_launcher
    data=tmp_path/'data';control=tmp_path/'control';data.mkdir();control.mkdir()
    monkeypatch.setattr(sys,'platform','linux');monkeypatch.setattr(os,'geteuid',lambda:0)
    with pytest.raises(Exception,match='separate filesystem'):
        create_packaged_account_launcher(execution_profile='hosted-xfs',control_root=control,data_root=data,
            volume_name='synthetic-data',image_id='sha256:'+'b'*64,network='synthetic-network',memory_bytes=123456789,pids_limit=32)


def test_packaged_account_storage_attests_exact_owner_and_project(tmp_path):
    data = tmp_path / 'data'
    digest = hashlib.sha256(b'tenant\0owner-a').hexdigest()
    owner = data / 'owners' / digest
    accounts = owner / 'provider-accounts'
    home = accounts / 'acct_test'
    home.mkdir(parents=True)
    projects = {owner: (73, True), accounts: (73, True), home: (73, True)}
    backend = DockerAccountContainerBackend(data_root=data, volume_name='synthetic-data',
        image_id='sha256:'+'b'*64, network='synthetic-network', memory_bytes=123456789,
        pids_limit=32, project_info=lambda path: projects[path])
    launcher = PackagedAccountLauncher.__new__(PackagedAccountLauncher)
    launcher.backend = backend
    snapshot = SimpleNamespace(root=owner, project_id=73, hard_enforced=True)
    assert launcher.verify_owner_storage('tenant', 'owner-a', snapshot) is True
    assert launcher.verify_owner_storage('tenant', 'owner-b', snapshot) is False
    assert launcher.verify_owner_storage('tenant', 'owner-a',
        SimpleNamespace(root=owner, project_id=73, hard_enforced=False)) is False
    projects[home] = (74, True)
    assert launcher.verify_owner_storage('tenant', 'owner-a', snapshot) is False
    projects[home] = (73, False)
    assert launcher.verify_owner_storage('tenant', 'owner-a', snapshot) is False
    home.rmdir()
    accounts.rmdir()
    assert launcher.verify_owner_storage('tenant', 'owner-a', snapshot) is True
    backend.project_info = lambda _: (_ for _ in ()).throw(QuotaUnavailable('synthetic unavailable'))
    assert launcher.verify_owner_storage('tenant', 'owner-a', snapshot) is False


def test_packaged_account_storage_rejects_symlink_escape_and_device(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    owner = data / 'owners' / hashlib.sha256(b'tenant\0owner').hexdigest()
    accounts = owner / 'provider-accounts'
    accounts.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (accounts / 'acct_test').symlink_to(outside, target_is_directory=True)
    backend = DockerAccountContainerBackend(data_root=data, volume_name='synthetic-data',
        image_id='sha256:'+'b'*64, network='synthetic-network', memory_bytes=123456789,
        pids_limit=32, project_info=lambda _: (73, True))
    snapshot = SimpleNamespace(root=owner, project_id=73, hard_enforced=True)
    assert backend.verify_owner_storage('tenant', 'owner', snapshot) is False
    (accounts / 'acct_test').unlink()
    (accounts / 'acct_test').mkdir()
    original_lstat = Path.lstat
    def foreign_device(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == accounts / 'acct_test':
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_dev=info.st_dev + 1)
        return info
    monkeypatch.setattr(Path, 'lstat', foreign_device)
    assert backend.verify_owner_storage('tenant', 'owner', snapshot) is False
