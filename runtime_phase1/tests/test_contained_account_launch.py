from dataclasses import replace
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from workers_projects_runtime.account_native_launch import NativeAccountLaunch
from workers_projects_runtime.contained_account_launch import (
    AccountContainmentUncertain, AccountStopReceipt, ContainedAccountLauncher,
)
from workers_projects_runtime.control_plane import ControlPlaneError
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager, ProviderSetupManager


class Transport:
    returncode = 0
    def poll(self): return self.returncode
    def communicate(self, **options): return ('synthetic output', '')
    def wait(self, **options): return self.returncode
    def kill(self): self.returncode=-9


class Backend:
    def identity(self):return 'synthetic-substrate'
    def __init__(self): self.starts=[]; self.stops=[]; self.absent=True; self.transport=Transport()
    def start(self, request, generation, **options):
        self.starts.append((request,generation,options))
        return self.transport, 'a'*64
    def finish(self, generation):
        self.stops.append(generation)
        return AccountStopReceipt(generation.lease_id,generation.generation,generation.container_id or 'a'*64,self.absent,True)


@pytest.fixture
def contained(tmp_path):
    backend=Backend()
    launcher=ContainedAccountLauncher(control_root=tmp_path/'control',backend=backend)
    homes=ProviderAccountHomeManager(tmp_path/'accounts',native_launcher=launcher)
    home=homes.ensure_home(tenant_id='tenant',owner_id='owner',account_id='acct_fixture',provider='codex')
    request=NativeAccountLaunch(home,('/synthetic/provider','login'),{},'setup','lease-synthetic')
    return homes,home,launcher,backend,request


def test_injected_launcher_is_authority_without_quota_resolver(contained,monkeypatch):
    homes,home,launcher,backend,request=contained
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('direct launch bypass'))
    process=homes.popen_native(list(request.command),account_home=home,purpose='setup',lease_id=request.lease_id,env={})
    assert len(backend.starts)==1 and backend.starts[0][1].uid==100001
    assert not hasattr(process,'pid')
    process.stop_and_confirm()
    result=homes.run_native(['/synthetic/provider'],account_home=home,purpose='verify',lease_id=request.lease_id,env={})
    assert result.returncode==0 and len(backend.stops)==2


@pytest.mark.parametrize('profile',['local-linux','hosted-xfs','invalid-profile'])
def test_packaged_profile_requires_launcher_even_without_quota(tmp_path,monkeypatch,profile):
    monkeypatch.setenv('XPERFECT_EXECUTION_PROFILE',profile)
    homes=ProviderAccountHomeManager(tmp_path/'accounts')
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('unguarded packaged run'))
    with pytest.raises(ControlPlaneError):homes.run_native(['/synthetic/provider'],account_home=tmp_path,purpose='verify',env={})


def test_local_host_without_package_preserves_direct_route(tmp_path,monkeypatch):
    monkeypatch.delenv('XPERFECT_EXECUTION_PROFILE',raising=False)
    calls=[];monkeypatch.setattr(subprocess,'run',lambda *a,**k:calls.append((a,k)) or 'local result')
    homes=ProviderAccountHomeManager(tmp_path/'accounts')
    assert homes.run_native(['/synthetic/provider'],account_home=tmp_path,purpose='verify',env={})=='local result'
    assert len(calls)==1


def test_exited_leader_still_requires_container_absence_before_release(contained):
    homes,home,launcher,backend,request=contained
    process=launcher.popen(request)
    assert process.poll()==0
    session=SimpleNamespace(process=process)
    backend.absent=False
    with pytest.raises(AccountContainmentUncertain):ProviderSetupManager._terminate_session_process(session)
    with pytest.raises(AccountContainmentUncertain):homes.tighten_permissions(account_home=home)
    with pytest.raises(AccountContainmentUncertain):homes.remove_home(tenant_id='tenant',owner_id='owner',account_id='acct_fixture')
    assert launcher.ledger.pending(home).lease_id==request.lease_id
    backend.absent=True
    ProviderSetupManager._terminate_session_process(session)
    assert launcher.ledger.pending(home) is None


def test_crash_restart_requires_exact_lease_and_stable_uid(contained):
    _,home,launcher,backend,request=contained
    process=launcher.popen(request)
    with pytest.raises(AccountContainmentUncertain):launcher.recover(home,lease_id=request.lease_id)
    # Emulate controller death releasing its OS lock; native containment survives.
    os.close(process._lock_fd);process._lock_fd=-1
    restarted=ContainedAccountLauncher(control_root=launcher.ledger.root,backend=backend)
    with pytest.raises(AccountContainmentUncertain):restarted.recover(home,lease_id='wrong-lease')
    restarted.recover(home,lease_id=request.lease_id)
    next_process=restarted.popen(replace(request,lease_id='next-lease'))
    assert next_process.generation.uid==process.generation.uid
    assert next_process.generation.generation!=process.generation.generation
    next_process.stop_and_confirm()


def test_timeout_stops_descendants_before_return_or_retains_pending(contained):
    _,home,launcher,backend,request=contained
    def timeout(**_):raise subprocess.TimeoutExpired('synthetic',1)
    backend.transport.communicate=timeout
    with pytest.raises(subprocess.TimeoutExpired):launcher.run(request,timeout=1)
    assert launcher.ledger.pending(home) is None
    backend.absent=False
    with pytest.raises(AccountContainmentUncertain):launcher.run(request,timeout=1)
    assert launcher.ledger.pending(home) is not None


def test_failed_creation_response_recovers_exact_precommitted_generation(contained):
    _,home,launcher,backend,request=contained
    def failed(request,generation,**_):
        backend.starts.append((request,generation,{}))
        raise OSError('synthetic response loss')
    backend.start=failed
    with pytest.raises(OSError):launcher.popen(request)
    assert backend.stops[-1].generation==backend.starts[-1][1].generation
    assert launcher.ledger.pending(home) is None
    backend.absent=False
    with pytest.raises(AccountContainmentUncertain):launcher.popen(request)
    assert launcher.ledger.pending(home) is not None


def test_uncertain_stop_does_not_release_existing_setup_lease(contained):
    import threading
    _,home,launcher,backend,request=contained
    process=launcher.popen(request)
    backend.absent=False
    releases=[]
    homes=SimpleNamespace(account_home_path=lambda **_:home,assert_native_quiescent=launcher.assert_quiescent)
    manager=ProviderSetupManager(store=SimpleNamespace(release_provider_lease=lambda **kw:releases.append(kw)),home_root=home,homes=homes)
    session=SimpleNamespace(process=process,tenant_id='tenant',owner_id='owner',account_id='acct_fixture',
                            lease_stop=threading.Event(),lease_thread=None,released=False)
    with pytest.raises(AccountContainmentUncertain):manager._release_session(session)
    assert releases==[] and launcher.ledger.pending(home).lease_id==request.lease_id
    backend.absent=True;process.stop_and_confirm()


def test_receipt_wrong_lease_or_generation_cannot_complete_ledger(contained):
    _,home,launcher,backend,request=contained
    process=launcher.popen(request)
    correct=backend.finish(process.generation)
    for receipt in (replace(correct,lease_id='other'),replace(correct,generation='wrong'),replace(correct,container_id='b'*64)):
        with pytest.raises(AccountContainmentUncertain):launcher.ledger.complete(process.generation,receipt)
        assert launcher.ledger.pending(home) is not None
    process.stop_and_confirm()


def test_recovery_cannot_switch_daemon_or_release_without_durable_receipt(contained):
    _,home,launcher,backend,request=contained
    process=launcher.popen(request)
    backend.identity=lambda:'different-daemon'
    with pytest.raises(AccountContainmentUncertain,match='substrate changed'):process.stop_and_confirm()
    assert backend.stops==[]
    backend.identity=lambda:'synthetic-substrate'
    process.stop_and_confirm()
    with launcher.ledger.connect() as connection:
        receipt=connection.execute('SELECT * FROM completions').fetchone()
    assert receipt['lease']==request.lease_id and receipt['container']=='a'*64
    assert receipt['generation']==process.generation.generation


def test_untyped_launcher_rejected_before_any_launch(tmp_path):
    class Unsafe:
        def popen(self,*a,**k):pytest.fail('uncontained launch')
    with pytest.raises(ControlPlaneError,match='typed contained launcher'):
        ProviderAccountHomeManager(tmp_path/'accounts',native_launcher=Unsafe())


@pytest.mark.parametrize('kind', ['socket', 'fifo'])
def test_permission_tightening_removes_stopped_native_ipc(contained, kind):
    homes, home, _, _, _ = contained
    path = home / 'transient-ipc'
    sock = None
    if kind == 'socket':
        sock = socket.socket(socket.AF_UNIX)
        previous = os.getcwd()
        os.chdir(home)
        try:
            sock.bind(path.name)
        finally:
            os.chdir(previous)
    else:
        os.mkfifo(path, 0o600)
    try:
        homes.tighten_permissions(account_home=home)
        assert not path.exists()
    finally:
        if sock is not None:
            sock.close()
