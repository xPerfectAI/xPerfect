"""An ACKed native interruption is an operator stop, not a provider failure."""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

import pytest

from workers_projects_runtime import native_input as owner
from workers_projects_runtime.grok_control import submit_control
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.openclaw_runtime import WorkerInterruptedError, WorkerPausedError
from test_codex_native_midstream import fake_native, launch, ready_context


def cancel_context(root):
    return {'version':1,'protocol':'codex-app-server','workerId':'worker-synthetic',
        'runId':'run-synthetic','attemptId':'attempt-synthetic','nativeSessionId':'session-synthetic',
        'nativeTurnId':'turn-synthetic','nativeSessionActive':False,
        'nativeTerminalStatus':'interrupted','nativeCancelAcknowledged':True}


@pytest.mark.parametrize('order', ['ack-first', 'notification-first', 'rejected', 'lost-ack'])
@pytest.mark.parametrize('status', ['interrupted', 'failed', 'completed'])
def test_real_rpc_reader_cancel_order_and_terminal_owner(tmp_path, order, status):
    cli=fake_native(tmp_path)
    code=cli.read_text()
    marker=" emit({'id': v['id'], 'result': result})"
    before='''
 if method == 'turn/interrupt' and os.environ['CANCEL_ORDER'] in ('notification-first','rejected','lost-ack'):
  notify('turn/completed', turn={'id':'turn-synthetic','status':os.environ['CANCEL_STATUS']})
  if os.environ['CANCEL_ORDER'] == 'rejected':
   emit({'id':v['id'],'error':{'code':-32000,'message':'Synthetic interrupt rejected'}});continue
  if os.environ['CANCEL_ORDER'] == 'lost-ack':
   break
'''
    code=code.replace(marker,before+marker)
    code+='''
 if method == 'turn/interrupt' and os.environ['CANCEL_ORDER'] == 'ack-first':
  notify('turn/completed', turn={'id':'turn-synthetic','status':os.environ['CANCEL_STATUS']})
'''
    cli.write_text(code)
    runtime=HostCodexCliRuntime(str(tmp_path/'runtime'))
    root=runtime._run_root('worker-synthetic','run-synthetic');root.mkdir(parents=True)
    launch_value,_=launch(tmp_path)
    path=root/'native-input-context.json'
    owner.publish(path,{'version':1,'protocol':'codex-app-server','workerId':'worker-synthetic',
        'runId':'run-synthetic','attemptId':'attempt-synthetic','launch':launch_value})
    env={**os.environ,'PYTHONPATH':str(Path(owner.__file__).parent.parent),
        'NATIVE_RECEIPT':str(tmp_path/'requests.jsonl'),'CANCEL_ORDER':order,'CANCEL_STATUS':status}
    process=subprocess.Popen([sys.executable,'-m','workers_projects_runtime.native_input',str(path),sys.executable,str(cli)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
    process.stdin.write('Synthetic cancel acceptance');process.stdin.close();process.stdin=None
    try:
        active=ready_context(path,process)
        deadline=time.monotonic()+2
        while not (root/'native-control').is_dir() and time.monotonic()<deadline:time.sleep(.005)
        result=submit_control(root/'native-control',{'run_id':'run-synthetic','attempt_id':'attempt-synthetic',
            'session_id':active['nativeSessionId'],'action':'cancel'},timeout=2)
        stdout,stderr=process.communicate(timeout=4)
        ack=order in {'ack-first','notification-first'}
        assert result['status']==('cancel_requested' if ack else 'rejected'),(result,stdout,stderr)
        terminal=owner.read_object(path)
        assert terminal['nativeTerminalStatus']==status and terminal['nativeCancelAcknowledged'] is ack
        assert terminal['nativeSessionId']=='session-synthetic' and terminal['nativeTurnId']=='turn-synthetic'
        assert terminal['nativeSessionActive'] is False
        interrupted=status=='interrupted' and ack
        assert process.returncode==(130 if interrupted else 0 if status=='completed' else 1),(stdout,stderr)
        if interrupted:
            assert any(json.loads(line).get('type')=='turn.interrupted' for line in stdout.splitlines())
            with pytest.raises(WorkerInterruptedError,match='cancelled by the operator'):
                runtime._finalize_stop_reason('worker-synthetic','run-synthetic',attempt_id='attempt-synthetic')
        else:
            runtime._finalize_stop_reason('worker-synthetic','run-synthetic',attempt_id='attempt-synthetic')
        requests=[json.loads(line) for line in (tmp_path/'requests.jsonl').read_text().splitlines()]
        interrupt=[r for r in requests if r.get('method')=='turn/interrupt']
        assert len(interrupt)==1 and interrupt[0]['params']=={'threadId':'session-synthetic','turnId':'turn-synthetic'}
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=3)


@pytest.mark.parametrize('change', [
    {'protocol':'untrusted'}, {'workerId':'other'}, {'runId':'other'}, {'attemptId':'other'},
    {'nativeSessionActive':True}, {'nativeTerminalStatus':'failed'}, {'nativeTerminalStatus':'completed'},
    {'nativeCancelAcknowledged':False}, {'nativeCancelAcknowledged':'true'},
    {'nativeSessionId':''}, {'nativeTurnId':''}, {'nativeSessionId':{'bad':'type'}},
    {'nativeTurnId':['bad-type']}, {'nativeSessionActive':'false'}])
def test_unconfirmed_stale_or_different_terminal_never_becomes_operator_cancel(tmp_path,change):
    runtime=HostCodexCliRuntime(str(tmp_path/'runtime'))
    root=runtime._run_root('worker-synthetic','run-synthetic');root.mkdir(parents=True)
    owner.publish(root/'native-input-context.json',{**cancel_context(root),**change})
    runtime._finalize_stop_reason('worker-synthetic','run-synthetic',attempt_id='attempt-synthetic')


def test_existing_pause_reason_wins_before_native_cancel_receipt(tmp_path):
    runtime=HostCodexCliRuntime(str(tmp_path/'runtime'))
    root=runtime._run_root('worker-synthetic','run-synthetic');root.mkdir(parents=True)
    owner.publish(root/'native-input-context.json',cancel_context(root))
    runtime._note_stop_reason('worker-synthetic','paused',run_id='run-synthetic',attempt_id='attempt-synthetic')
    with pytest.raises(WorkerPausedError):
        runtime._finalize_stop_reason('worker-synthetic','run-synthetic',attempt_id='attempt-synthetic')


def test_failed_new_launch_cannot_inherit_a_previous_cancel_receipt(tmp_path,monkeypatch):
    runtime=HostCodexCliRuntime(str(tmp_path/'runtime'))
    root=runtime._run_root('worker-synthetic','run-synthetic');root.mkdir(parents=True)
    context={**cancel_context(root),'launch':{'thread':{},'turn':{},'images':[],'resumed':False}}
    path=root/'native-input-context.json';owner.publish(path,context)
    class FailedRpc:
        def call(self,*args):raise owner.NativeInputError('native_control_rejected')
        def close(self):pass
    previous={sig:signal.getsignal(sig) for sig in (signal.SIGHUP,signal.SIGINT,signal.SIGTERM)}
    try:
        assert owner.codex_relay(path,[],initialized_rpc=FailedRpc(),instruction='Synthetic new launch')==1
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)
    saved=owner.read_object(path)
    assert 'nativeCancelAcknowledged' not in saved and 'nativeTerminalStatus' not in saved
    runtime._finalize_stop_reason('worker-synthetic','run-synthetic',attempt_id='attempt-synthetic')
