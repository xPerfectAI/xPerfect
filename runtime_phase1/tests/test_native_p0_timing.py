"""Timing is optional and cannot change the existing admission or queue contracts."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from workers_projects_runtime.conversation_provider import ConversationProvider
from workers_projects_runtime.service import WorkersProjectsService
import workers_projects_runtime.native_model_selection as timing


@pytest.mark.parametrize('state,missing', [('queued',False), ('cancelled',False), ('queued',True)])
def test_start_lock_timings_preserve_exact_start_fence_and_release(state, missing, monkeypatch):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY','1')
    logger=Mock()
    monkeypatch.setattr(timing,'_logger',logger)
    monkeypatch.setattr(timing.time,'monotonic_ns',Mock(side_effect=[0,2_000_000,9_000_000,10_000_000,11_000_000]))
    # The event helper also reads the local monotonic clock. Assert ordering/duration separately.
    provider=ConversationProvider.__new__(ConversationProvider)
    provider._start_lock=threading.RLock()
    current={'run_id':'synthetic-run','state':state}
    provider.store=SimpleNamespace(get_provider_request=lambda _:None if missing else current,
                                   get_run=lambda _:{'worker_id':'synthetic-worker','state':'queued'})
    provider.service=SimpleNamespace(start_assigned_run=Mock())
    provider._deadline_reached=lambda _:False
    if missing:
        with pytest.raises(HTTPException) as caught:
            provider._start_assigned_run_before_deadline('synthetic-request',run_id='synthetic-run',worker_id='synthetic-worker')
        assert caught.value.status_code==404
    else:
        result=provider._start_assigned_run_before_deadline('synthetic-request',run_id='synthetic-run',worker_id='synthetic-worker')
        assert result==(current,state=='queued')
    assert provider.service.start_assigned_run.call_count==(1 if state=='queued' and not missing else 0)
    assert provider._start_lock.acquire(blocking=False)
    provider._start_lock.release()
    rows=[json.loads(call.args[1]) for call in logger.info.call_args_list]
    assert [row['stage'] for row in rows]==['start_lock_acquired','start_lock_pre_release']
    assert rows[0]['waitMs']==2
    assert rows[1]['heldUntilPreReleaseMs']==8
    assert rows[0]['requestHash']==rows[1]['requestHash']
    assert 'synthetic-request' not in json.dumps(rows)


def test_executor_markers_use_same_existing_worker_generation_and_submission(monkeypatch):
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY','1')
    logger=Mock(); monkeypatch.setattr(timing,'_logger',logger)
    service=WorkersProjectsService.__new__(WorkersProjectsService)
    service.store=SimpleNamespace(get_worker=lambda _:{'state':'ready'}, has_active_operator_pause=lambda _:False,
                                  has_unconfirmed_host_run_start=lambda _:False)
    service._trusted_run_lane=lambda _:'conversation'
    service._bootstrap_bundle_for=lambda _:{'run_mode':'conversation'}
    service._processors_lock=threading.Lock()
    service._shutdown_event=threading.Event()
    service._active_processors=set(); service._processor_generations={}
    submitted=[]; entries=[]
    def submit(method,*args):
        submitted.append(args)
        return method(*args)
    service.conversation_executor=SimpleNamespace(submit=submit)
    service.executor=SimpleNamespace(submit=Mock(side_effect=AssertionError('wrong lane')))
    service._process_worker_queue_parallel=lambda *args:entries.append(args)
    service._ensure_worker_processor('synthetic-worker')
    assert submitted==entries==[('synthetic-worker',1)]
    rows=[json.loads(call.args[1]) for call in logger.info.call_args_list]
    assert [row['stage'] for row in rows]==['executor_submit','executor_entry']
    assert rows[0]['workerHash']==rows[1]['workerHash']
    assert rows[0]['generation']==rows[1]['generation']==1
    assert 'synthetic-worker' not in json.dumps(rows)
