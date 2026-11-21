import hashlib
import json
from types import SimpleNamespace
import pytest
from datetime import UTC, datetime, timedelta

from test_coordinator_application import app_core, accept
from workers_projects_runtime.coordinator import Goal
from workers_projects_runtime.coordinator_application import (
    EVENTS, queue_progress, publications, notification_fence, finish_notification,
)
from workers_projects_runtime.conversation_provider import ConversationProvider, _native_commentary_events, _native_terminal_text
from workers_projects_runtime.profile_runtime import CodexCliRuntime


def native_message(text='Synthetic public progress.', phase='commentary', turn='current', item='message-a'):
    return {'type': 'response_item', 'native_turn_id': turn, 'native_item_id': item, 'payload': {
        'type': 'message', 'role': 'assistant', 'phase': phase,
        'content': [{'type':'output_text','text':text}],
        'internal_chat_message_metadata_passthrough': {'turn_id':turn}}}


def test_real_rollout_only_complete_public_current_input_and_final_unchanged(tmp_path, monkeypatch):
    runtime = object.__new__(CodexCliRuntime)
    instruction = 'Synthetic requested outcome.'
    from workers_projects_runtime.worker_context_mcp import context_tool_manifest
    stdin = json.dumps({'instruction':instruction,'context':[],'context_manifest':{},
        'context_contract':context_tool_manifest()['inherited_context_contract']})
    (tmp_path/'instruction.stdin').write_text(stdin)
    (tmp_path/'stdout.log').write_text('')
    transcript = (tmp_path/'stdout.log', 'claim-a')
    monkeypatch.setattr(runtime, '_attempt_transcript', lambda *args: transcript)
    monkeypatch.setattr(runtime, '_run_root', lambda *args: tmp_path)
    rows = [ {'type':'turn_context','payload':{'turn_id':'current'}},
        {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':stdin}],
          'internal_chat_message_metadata_passthrough':{'turn_id':'current'}}}]
    for phase in ('commentary','analysis','final_answer'):
        row=native_message(phase=phase); row.pop('native_turn_id'); row.pop('native_item_id'); rows.append(row)
    mismatch=native_message(turn='foreign');mismatch.pop('native_turn_id');mismatch.pop('native_item_id');rows.append(mismatch)
    rows.append({'type':'response_item','payload':{'type':'function_call_output','output':'private tool data',
         'internal_chat_message_metadata_passthrough':{'turn_id':'current'}}})
    path=tmp_path/'rollout.jsonl';path.write_text('\n'.join(map(json.dumps,rows))+'\n'+json.dumps(native_message('Incomplete record.')))
    monkeypatch.setattr(runtime, '_codex_run_rollout_path', lambda *args, **kw: path)
    worker={'worker_id':'worker-a','_provider_activity_attempt_id':'attempt-a'}
    digest=hashlib.sha256(instruction.encode()).hexdigest()
    log=runtime.provider_rollout_commentary_log(worker,'run-a',instruction_sha256=digest)
    events=_native_commentary_events(log,{'run_id':'run-a','active_attempt_id':'attempt-a'})
    assert [e['progress_text'] for e in events]==['Synthetic public progress.']
    assert runtime.provider_rollout_commentary_log(worker,'run-a',instruction_sha256='0'*64)==''
    monkeypatch.setattr(runtime,'_attempt_transcript',lambda *args: None)
    assert runtime.provider_rollout_commentary_log(worker,'run-a',instruction_sha256=digest)==''
    terminal='\n'.join(map(json.dumps,[{'type':'item.completed','item':{'type':'agent_message','text':'Final result.'}}, {'type':'turn.completed'}]))
    assert _native_terminal_text('codex-cli',terminal)=='Final result.'


def setup_goal(core,cid):
    accept(core,cid, 'Synthetic requested outcome.')
    core.accept_goals('local','owner',cid,'source',[Goal(id='goal',text='Synthetic requested outcome.')])
    conv=core._conversation('local','owner',cid)
    conv['project_id']=json.loads(conv['config_json'])['scope']['project_id']
    worker=core.store.create_worker(conv['project_id'],'owner','Synthetic','general','codex-cli','synthetic','stub','synthetic')
    run=core.store.create_run(worker['worker_id'],conv['project_id'],'Synthetic requested outcome.')
    with core.store._connect() as conn:
        conn.execute("UPDATE runs SET state='running',active_attempt_id='attempt-a' WHERE run_id=?",(run['run_id'],))
        conn.execute("UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='goal'",(worker['worker_id'],run['run_id'],cid))
    core.service.runtime=SimpleNamespace(provider_commentary_log=lambda *a,**k:('codex-cli',json.dumps(native_message())))
    return conv,worker,core.store.get_run(run['run_id'])


def progress_row(core):
    with core.store._connect() as conn:
        return dict(conn.execute("SELECT * FROM callback_outbox WHERE event_type=?",(EVENTS[5],)).fetchone())


def test_goal_progress_dedup_and_durable_publication_same_source(app_core):
    core,cid,_=app_core;conv,worker,run=setup_goal(core,cid)
    queue_progress(core,conv);queue_progress(core,conv)
    with core.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM callback_outbox WHERE event_type=?',(EVENTS[5],)).fetchone()[0]==1
    row=progress_row(core)
    assert notification_fence(core.service,row)==''
    item=publications(core,'local','owner',cid)['publications'][0]
    assert item['type']==EVENTS[5] and item['source_turn_id']=='source'
    assert item['data']['goal_id']=='goal' and 'turn_id' not in item['data']
    assert not {'output_files','result_revision'} & item['data'].keys()
    from workers_projects_runtime.coordinator import CoordinatorScopeError
    with pytest.raises(CoordinatorScopeError):
        publications(core,'local','foreign',cid)


def test_progress_current_attempt_terminal_cancel_and_foreign_scope(app_core):
    core,cid,_=app_core;conv,worker,run=setup_goal(core,cid);queue_progress(core,conv);row=progress_row(core)
    for sql in ["UPDATE runs SET active_attempt_id='attempt-b' WHERE run_id=?", "UPDATE runs SET state='completed' WHERE run_id=?"]:
        with core.store._connect() as conn: conn.execute(sql,(run['run_id'],))
        assert notification_fence(core.service,row)!=''
        with core.store._connect() as conn: conn.execute("UPDATE runs SET state='running',active_attempt_id='attempt-a' WHERE run_id=?",(run['run_id'],))
    with core.store._connect() as conn: conn.execute("UPDATE coordinator_goals SET intent_state='cancelled' WHERE conversation_id=?",(cid,))
    assert notification_fence(core.service,row)!=''
    with core.store._connect() as conn: conn.execute("UPDATE coordinator_goals SET intent_state='accepted' WHERE conversation_id=?",(cid,))
    foreign={**row,'owner_id':'foreign'}
    assert notification_fence(core.service,foreign)!=''
    payload=json.loads(row['payload_json']);payload['data']['progress_text']='Forged text.'
    assert notification_fence(core.service,{**row,'payload_json':json.dumps(payload)})!=''


def test_progress_expiry_records_failure_without_blocking_work(app_core):
    core,cid,_=app_core;conv,worker,run=setup_goal(core,cid);queue_progress(core,conv);row=progress_row(core)
    with core.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET status='delivering',delivery_lease_token='lease-a',delivery_generation=1 WHERE callback_id=?",(row['callback_id'],))
        row=dict(conn.execute('SELECT * FROM callback_outbox WHERE callback_id=?',(row['callback_id'],)).fetchone())
    assert finish_notification(core.store,row,state='expired',error='delivery_expired')
    assert core._goal(cid,'goal')['blocker']==''
    with core.store._connect() as conn:
        assert conn.execute('SELECT status FROM callback_outbox WHERE callback_id=?',(row['callback_id'],)).fetchone()[0]=='expired'


def setup_provider(core,cid,conv,worker,run):
    session=core.store.upsert_provider_session(tenant_id='local',owner_id='owner',conversation_id=cid,agent_id='coordinator',
        model_id='codex-cli:synthetic',project_id=conv['project_id'],worker_id=worker['worker_id'],workspace_dir='/synthetic',access_mode='workspace')
    record,_=core.store.create_provider_request(tenant_id='local',owner_id='owner',session_id=session['session_id'],
        idempotency_key='synthetic-public-progress',message_id='source',stream_id='source',requested_history_count=1)
    with core.store._connect() as conn:
        conn.execute("UPDATE provider_requests SET run_id=?,state='running',replay_decision_json=? WHERE request_id=?",(run['run_id'],json.dumps({'instruction_sha256':hashlib.sha256(run['instruction'].encode()).hexdigest()}),record['request_id']))
        conn.execute('UPDATE coordinator_turns SET request_id=? WHERE conversation_id=? AND turn_id=?',(record['request_id'],cid,'source'))
    return core.store.get_provider_request(record['request_id'])


def test_provider_activity_retains_public_commentary_once_per_exact_attempt(app_core):
    core,cid,_=app_core;conv,worker,run=setup_goal(core,cid)
    record=setup_provider(core,cid,conv,worker,run)
    core.service.runtime.provider_activity_log=lambda *a: ('codex-cli','')
    provider=SimpleNamespace(store=core.store,service=core.service)
    ConversationProvider._sync_native_activity(provider,record,run)
    ConversationProvider._sync_native_activity(provider,record,run)
    rows=core.store.list_provider_activity(record['request_id'])
    assert len(rows)==1 and rows[0]['event_type']=='public-commentary'
    assert json.loads(rows[0]['payload_json'])['attempt_id']=='attempt-a'


def test_turn_progress_uses_exact_provider_owner_and_survives_reader(app_core):
    core,cid,_=app_core;conv,worker,run=setup_goal(core,cid);record=setup_provider(core,cid,conv,worker,run)
    with core.store._connect() as conn: conn.execute('DELETE FROM coordinator_goals WHERE conversation_id=?',(cid,))
    event=_native_commentary_events(json.dumps(native_message()),run)[0]
    core.store.add_provider_activity(record['request_id'],'public-commentary','Native public progress',event)
    queue_progress(core,conv);row=progress_row(core)
    assert notification_fence(core.service,row)==''
    data=publications(core,'local','owner',cid)['publications'][0]['data']
    assert data['turn_id']=='source' and data['request_id']==record['request_id'] and 'goal_id' not in data
    with core.store._connect() as conn: conn.execute("UPDATE provider_requests SET owner_id='foreign' WHERE request_id=?",(record['request_id'],))
    assert notification_fence(core.service,row)!=''


def test_progress_uses_existing_transport_and_snapshot_is_immutable(app_core):
    from workers_projects_runtime.schedule_targets import dispatch_pending
    import sqlite3
    core,cid,_=app_core;conv,worker,run=setup_goal(core,cid);queue_progress(core,conv)
    row=progress_row(core)
    with pytest.raises(sqlite3.IntegrityError):
        with core.store._connect() as conn:
            conn.execute("UPDATE callback_outbox SET payload_json='{}' WHERE callback_id=?",(row['callback_id'],))
    sent=[]
    def send(**values):
        sent.append(values)
        return SimpleNamespace(state='accepted',error_code='',retryable=False,http_status=202,timings_ms={})
    core.service.schedule_webhook_transport=SimpleNamespace(send=send)
    ids=dispatch_pending(core.service)
    core.service.schedule_transport_executor.shutdown(wait=True)
    assert ids==[row['callback_id']] and len(sent)==1
    assert core.store.get_callback_outbox(row['callback_id'])['status']=='http_accepted'
    assert json.loads(sent[0]['body'])['type']==EVENTS[5]
