"""Native transcript wake hints preserve durable reads and stream safety gates."""

import asyncio
from pathlib import Path
import select
import threading
import time
from types import SimpleNamespace

import pytest

from workers_projects_runtime.native_log_notifications import NativeLogNotifications
from workers_projects_runtime.run_notifications import RunNotifications
from workers_projects_runtime.service import WorkersProjectsService


def test_run_signal_is_scoped_retained_during_read_and_cleaned():
    async def check():
        bus = RunNotifications()
        one, two = bus.subscribe(), bus.subscribe()
        one.observe('run-a')
        two.observe('run-b')
        # Signal arrives after subscription but before the later wait/read completes.
        thread = threading.Thread(target=bus.notify, args=('run-a',))
        thread.start()
        thread.join()
        await asyncio.wait_for(one.wait(timeout=10), timeout=0.1)
        assert not two.event.is_set()
        one.observe('run-c')
        bus.notify('run-a')
        await asyncio.sleep(0)
        assert not one.event.is_set()
        one.close()
        two.close()
        assert bus._subscribers == {}
    asyncio.run(check())


def test_missing_notification_reconciles_with_bounded_timeout():
    async def check():
        bus = RunNotifications()
        subscription = bus.subscribe()
        subscription.observe('run-a')
        started = time.monotonic()
        await subscription.wait(timeout=0.01)
        assert 0.005 <= time.monotonic() - started < 0.5
        subscription.close()
    asyncio.run(check())


@pytest.mark.parametrize('next_runs', [('run-b',), ('run-b', 'run-a'), ('',)])
def test_queued_hint_cannot_wake_a_rebound_or_closed_subscription(next_runs):
    async def check():
        bus = RunNotifications()
        subscription = bus.subscribe()
        subscription.observe('run-a')
        bus.notify('run-a')
        for run_id in next_runs:
            if run_id:
                subscription.observe(run_id)
            else:
                subscription.close()
        await asyncio.sleep(0)
        assert not subscription.event.is_set()
        subscription.close()
    asyncio.run(check())


@pytest.mark.parametrize('change', ['wrong-worker', 'wrong-attempt', 'terminal', 'empty-attempt', 'exact'])
def test_native_wake_fences_exact_active_attempt(change):
    service = object.__new__(WorkersProjectsService)
    run = {'worker_id': 'worker-a', 'active_attempt_id': 'attempt-a', 'state': 'running'}
    observation = {'worker_id': 'worker-a', 'run_id': 'run-a', 'attempt_id': 'attempt-a',
                   'provider': 'grok', 'event': {'event_type': 'provider.output.available', 'payload': {}}}
    if change == 'wrong-worker': observation['worker_id'] = 'worker-b'
    if change == 'wrong-attempt': observation['attempt_id'] = 'attempt-b'
    if change == 'terminal': run['state'] = 'cancelled'
    if change == 'empty-attempt': observation['attempt_id'] = run['active_attempt_id'] = ''
    hints = []
    service.store = SimpleNamespace(get_run=lambda _: run)
    service.run_notifications = SimpleNamespace(notify=hints.append)
    service._observe_native_event(observation)
    assert hints == (['run-a'] if change == 'exact' else [])


@pytest.mark.skipif(not hasattr(select, 'kqueue'), reason='Kernel file notifications are capability-gated')
def test_file_write_before_wait_and_rotation_rearm_without_lost_wakeup(tmp_path):
    path = tmp_path / 'native.jsonl'
    path.write_text('')
    with NativeLogNotifications(path) as notification:
        notification.prepare()
        with path.open('a') as output:
            output.write('{}\n')
            output.flush()
        started = time.monotonic()
        notification.wait(threading.Event(), timeout=1)
        assert time.monotonic() - started < 0.1
        path.rename(tmp_path / 'old.jsonl')
        path.write_text('')
        notification.prepare()
        with path.open('a') as output:
            output.write('{}\n')
            output.flush()
        started = time.monotonic()
        notification.wait(threading.Event(), timeout=1)
        assert time.monotonic() - started < 0.1
        path.unlink()
        notification.prepare()
        assert notification._fd is None
    assert notification._queue is None



def test_unavailable_kernel_queue_falls_back_without_losing_observer(monkeypatch, tmp_path):
    def unavailable():
        raise OSError("Synthetic kernel notification unavailability")
    monkeypatch.setattr(select, 'kqueue', unavailable, raising=False)
    with NativeLogNotifications(tmp_path / 'missing') as notification:
        notification.prepare()
        assert notification._queue is None
        stopped = threading.Event()
        stopped.set()
        notification.wait(stopped, timeout=10)


def test_no_kernel_watch_and_missing_file_keep_stop_and_timeout(monkeypatch, tmp_path):
    monkeypatch.delattr(select, 'kqueue', raising=False)
    with NativeLogNotifications(tmp_path / 'missing') as notification:
        notification.prepare()
        stopped = threading.Event()
        stopped.set()
        notification.wait(stopped, timeout=10)
        assert notification._fd is None


async def native_stream_delay(*, wake: bool, transcript_delay: float = 0.01):
    from test_conversation_provider import _spoken_family_fixture
    state, provider, payload = _spoken_family_fixture()
    provider._sync = lambda record: record
    provider._native_effective_model = lambda *_: payload.model
    original_log = provider.service.runtime.provider_activity_log
    ready, first_read = threading.Event(), threading.Event()
    def log(*args):
        first_read.set()
        return original_log(*args) if ready.is_set() else ('grok-build', '')
    provider.service.runtime.provider_activity_log = log
    bus = RunNotifications()
    if wake:
        provider.service.run_notifications = bus
    clock = {}
    def produce():
        assert first_read.wait(2)
        time.sleep(transcript_delay)
        clock['written'] = time.monotonic()
        ready.set()
        bus.notify('run-a')
    producer = threading.Thread(target=produce)
    producer.start()
    class Connection:
        async def is_disconnected(self): return False
    stream = provider._stream_chunks(state['record'], payload, Connection())
    try:
        async for row in stream:
            if 'Useful public words.' in row:
                clock['emitted'] = time.monotonic()
                break
    finally:
        await stream.aclose()
        producer.join()
    assert state['public_receipts'], 'Public bytes still require the durable authoring receipt'
    assert bus._subscribers == {}, 'Disconnected streams must release run subscriptions'
    return (clock['emitted'] - clock['written']) * 1000


def test_actual_stream_wakes_on_native_hint_before_reconciliation_timer():
    async def check():
        fixed_delay = await native_stream_delay(wake=False)
        signalled_delay = await native_stream_delay(wake=True)
        assert fixed_delay > 100
        assert signalled_delay < fixed_delay / 2
    asyncio.run(check())


def test_repeated_hints_coalesce_without_delay_or_generation_regression():
    async def check():
        bus = RunNotifications()
        subscriber = bus.subscribe()
        subscriber.metrics = {'coalescedHints': 0, 'deliveredHints': 0}
        subscriber.observe('run-a')
        for _ in range(1000):
            bus.notify('run-a')
        await asyncio.sleep(0)
        assert subscriber.event.is_set()
        assert subscriber.metrics == {'coalescedHints': 999, 'deliveredHints': 1}
        subscriber.observe('run-a')
        bus.notify('run-a')
        await asyncio.sleep(0)
        assert subscriber.event.is_set()
        assert subscriber.metrics['deliveredHints'] == 2
        subscriber.close()
    asyncio.run(check())


@pytest.mark.parametrize('after', [
    {'run_id': 'run-a', 'state': 'completed'},
    {'run_id': 'run-a', 'state': 'cancelled'},
    {'run_id': 'run-b', 'state': 'queued', 'fallback_state': 'started'},
])
def test_provider_transition_wakes_old_and_new_run_only_after_transition(after):
    from workers_projects_runtime.conversation_provider import ConversationProvider
    hints = []
    provider = object.__new__(ConversationProvider)
    provider.service = SimpleNamespace(run_notifications=SimpleNamespace(notify=hints.append))
    before = {'run_id': 'run-a', 'state': 'running'}
    provider._notify_request_transition(before, before)
    assert hints == []
    provider._notify_request_transition(before, after)
    assert set(hints) == {'run-a', after['run_id']}


def test_sync_notifies_after_durable_fallback_assignment():
    from workers_projects_runtime.conversation_provider import ConversationProvider, DeferredFallbackStart
    provider = object.__new__(ConversationProvider)
    before = {'run_id': 'run-a', 'state': 'running'}
    claimed = {**before, 'fallback_state': 'claimed'}
    replaced = {'run_id': 'run-b', 'state': 'queued', 'fallback_state': 'started'}
    committed, hints = [], []
    provider.service = SimpleNamespace(run_notifications=SimpleNamespace(
        notify=lambda run_id: hints.append((tuple(committed), run_id))))
    provider._request_timeout_seconds = lambda _: 10
    provider._arbitrate_deadline_if_needed = lambda record, **_: (record, {}, False)
    provider._sync_lock = threading.RLock()
    def claim(*_, **__):
        committed.append('claimed')
        return DeferredFallbackStart(claimed, {'run_id': 'run-a'})
    provider._sync_locked = claim
    def start(*_, **__):
        committed.append('attached')
        return replaced
    provider._start_serial_fallback = start
    assert provider._sync(before) == replaced
    assert hints[0] == (('claimed',), 'run-a')
    assert set(run for commits, run in hints if commits == ('claimed', 'attached')) == {'run-a', 'run-b'}


def test_terminal_run_commit_wakes_before_optional_reconciliation(monkeypatch):
    import workers_projects_runtime.service as service_module
    service = object.__new__(WorkersProjectsService)
    order = []
    terminal = {'run_id': 'run-a', 'state': 'completed'}
    service.store = SimpleNamespace(get_run=lambda _: {'worker_id': 'worker-a'},
        get_worker=lambda _: {}, finalize_run_if_state=lambda *_, **__: (order.append('commit') or terminal))
    service.files = SimpleNamespace()
    service.run_notifications = SimpleNamespace(notify=lambda run: order.append(('wake', run)))
    service._provider_request_reconciler = lambda run: order.append(('reconcile', run))
    monkeypatch.setattr(service_module, 'work_artifact_observation', lambda *_, **__: {})
    assert service._finalize_run_if_state('run-a', 'running', 'completed') == terminal
    assert order == ['commit', ('wake', 'run-a'), ('reconcile', 'run-a')]


def test_stream_summary_is_opt_in_and_contains_no_content(monkeypatch):
    from workers_projects_runtime import conversation_provider
    events = []
    monkeypatch.setenv('VIVENTIUM_VOICE_LOG_LATENCY', '1')
    monkeypatch.setattr(conversation_provider, 'native_timing_event', lambda stage, **fields: events.append((stage, fields)))
    asyncio.run(native_stream_delay(wake=True))
    summaries = [fields for stage, fields in events if stage == 'stream_observation_completed']
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary['observationPasses'] >= 2
    assert summary['deliveredHints'] >= 1
    assert summary['hintWokenWaits'] >= 1
    assert summary['waitMs'] >= 0
    assert 'Useful public words.' not in repr(summary)
    assert set(summary) == {'requestHash', 'elapsedMs', 'observationPasses', 'deliveredHints',
                            'coalescedHints', 'hintWokenWaits', 'timedOutWaits', 'waitMs'}


def test_terminal_transition_finishes_actual_stream_before_reconciliation_timer():
    from test_conversation_provider import _spoken_family_fixture
    async def check():
        state, provider, payload = _spoken_family_fixture()
        bus, first_read = RunNotifications(), threading.Event()
        provider.service.run_notifications = bus
        provider._sync = lambda record: record
        provider._native_effective_model = lambda *_: payload.model
        def log(*_):
            first_read.set()
            return 'grok-build', ''
        provider.service.runtime.provider_activity_log = log
        canonical = {'model': payload.model, 'choices': [{'message': {'content': 'Done.'},
                     'finish_reason': 'stop'}], 'usage': {}, 'glasshive': {'usage_source': 'native'}}
        provider.response_payload = lambda *_: canonical
        clock = {}
        def finish():
            assert first_read.wait(2)
            time.sleep(.01)
            before = state['record']
            state['record'] = {**before, 'state': 'completed'}
            clock['committed'] = time.monotonic()
            provider._notify_request_transition(before, state['record'])
        thread = threading.Thread(target=finish)
        thread.start()
        class Connection:
            async def is_disconnected(self): return False
        rows = [row async for row in provider._stream_chunks(state['record'], payload, Connection())]
        elapsed = time.monotonic() - clock['committed']
        thread.join()
        assert rows[-1] == 'data: [DONE]\n\n'
        assert elapsed < .1
        assert bus._subscribers == {}
    asyncio.run(check())
