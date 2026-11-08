"""Native result proof exposes its existing intent, hold and exact run revision."""
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_coordinator_application import app_core, packet
from workers_projects_runtime.coordinator import ApplicationBinding, Control, Goal
from workers_projects_runtime.coordinator_application import prepare_turn, queue_self_results
from workers_projects_runtime.coordinator_api import install_coordinator_routes


def completed_goal(core, config, *, tenant='local', owner='owner', state='completed', output='A useful completed result.'):
    destination=core.service.webhook_destinations.create(tenant,owner,name='Synthetic voice adapter',
        url='http://127.0.0.1:12345/voice-proof',payload_mode='envelope')
    selected=config.model_copy(update={'application':ApplicationBinding(destination_id=destination['destination_id'],
        request_ref=f'voice-proof:{tenant}:{owner}:{state}:{len(output)}')})
    cid=core.create(tenant,owner,selected)['conversation_id']
    core.accept_turn(tenant,owner,cid,'source','Make a tiny local page.')
    incoming=packet(cid)
    incoming.metadata.tenant_id=tenant
    incoming.metadata.owner_id=owner
    prepared=prepare_turn(core,tenant,owner,cid,'source',incoming)
    request=core.store.get_provider_request(prepared['request_id'])
    session=core.store.get_provider_session_by_id(request['session_id'])
    run=core.store.create_run(session['worker_id'],session['project_id'],'Original retained work')
    core.store.update_run(run['run_id'],state=state,output_text=output,terminal_result_revision=7,
        failure_class='synthetic_failure' if state=='failed' else '')
    core.accept_goals(tenant,owner,cid,'source',[Goal(id='goal',text='Make a tiny local page.')])
    canonical={'glasshive':{},'choices':[{'message':{'role':'assistant','content':output}}]}
    core.store.update_provider_request(request['request_id'],run_id=run['run_id'],state=state,response_json=json.dumps(canonical))
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_goals SET worker_id=?,run_id=? WHERE conversation_id=? AND goal_id='goal'",
            (session['worker_id'],run['run_id'],cid))
        conn.execute('UPDATE coordinator_turns SET response_json=? WHERE conversation_id=? AND turn_id=?',
            (json.dumps(canonical),cid,'source'))
    return cid,core.store.get_run(run['run_id'])


@pytest.mark.parametrize('tenant',['local','tenant-synthetic'])
@pytest.mark.parametrize('state,output',[('completed','A useful completed result.'),('failed','')])
def test_current_no_files_publisher_and_revision_are_native_owned_and_owner_scoped(app_core,tenant,state,output):
    core,_,config=app_core
    cid,run=completed_goal(core,config,tenant=tenant,owner='proof-owner',state=state,output=output)
    app=FastAPI()
    install_coordinator_routes(app,core,
        lambda request:(request.headers.get('x-test-tenant',tenant),request.headers.get('x-test-owner','proof-owner')),
        lambda *args:None)
    client=TestClient(app)
    status=client.get(f'/v1/coordinator/conversations/{cid}',params={'include_history':False})
    assert status.status_code==200,status.text
    goal=status.json()['goals'][0]
    assert goal['intent_state']=='accepted' and goal['restore_hold'] is False
    response=client.get(f'/v1/coordinator/conversations/{cid}/goals/goal/result',params={'run_id':run['run_id']})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['result_revision']==run['terminal_result_revision']
    assert result['publisher']['owner_id']=='proof-owner' and result['publisher']['run_id']==run['run_id']
    assert result['publisher']['message_id']=='source' and 'output_files' not in result
    assert result['sha256']==hashlib.sha256(output.encode()).hexdigest()
    with core.store._connect() as conn:
        event=json.loads(conn.execute("SELECT payload_json FROM callback_outbox WHERE event_type='coordinator.result_ready' AND owner_id='proof-owner'").fetchone()[0])
    assert event['principal']=={'tenant_id':tenant,'owner_id':'proof-owner'}
    assert result['result_revision']==event['data']['result_revision']
    assert 'sha256:'+result['sha256']==event['data']['result_digest']
    for headers in [{'x-test-owner':'unrelated-owner'},{'x-test-tenant':'unrelated-tenant'}]:
        denied=client.get(f'/v1/coordinator/conversations/{cid}/goals/goal/result',headers=headers)
        assert denied.status_code==403


def test_completion_winning_exact_stop_still_exposes_cancelled_owner_intent(app_core):
    core,_,config=app_core
    cid,run=completed_goal(core,config)
    core.service.stop_run=lambda *args,**kwargs:{'accepted':False,'confirmation_pending':False}
    core.control('local','owner',cid,'goal',Control(action='stop',run_id=run['run_id'],idempotency_key='stop-completed-result'))
    goal=core.goal_snapshot('local','owner',cid,'goal')
    assert goal['state']=='completed'  # Retain native process truth for existing readers.
    assert goal['intent_state']=='cancelled' and goal['restore_hold'] is False
    assert not(goal['intent_state']=='accepted' and goal['restore_hold'] is False)
    assert core.read_result('local','owner',cid,'goal')['result_revision']==run['terminal_result_revision']


@pytest.mark.parametrize('hold_owner',['coordinator_goals','coordinator_conversations'])
def test_effective_hold_is_explicit_for_goal_and_conversation_restore(app_core,hold_owner):
    core,_,config=app_core
    cid,run=completed_goal(core,config)
    with core.store._connect() as conn:
        conn.execute(f'UPDATE {hold_owner} SET restore_hold=1 WHERE conversation_id=?',(cid,))
    goal=core.goal_snapshot('local','owner',cid,'goal')
    assert goal['intent_state']=='accepted' and goal['restore_hold'] is True and goal['state']=='restore_held'
    assert not(goal['intent_state']=='accepted' and goal['restore_hold'] is False)
    assert core.read_result('local','owner',cid,'goal')['result_revision']==run['terminal_result_revision']


def test_historical_read_uses_its_own_revision_without_relabelling_current_goal(app_core):
    core,_,config=app_core
    cid,original=completed_goal(core,config)
    later=core.store.create_run(original['worker_id'],original['project_id'],'A later owned continuation')
    core.store.update_run(later['run_id'],state='completed',output_text='Later result.',terminal_result_revision=12)
    with core.store._connect() as conn:
        conn.execute('UPDATE coordinator_goals SET run_id=? WHERE conversation_id=?',(later['run_id'],cid))
        conn.execute('INSERT INTO coordinator_actions(conversation_id,goal_id,idempotency_key,payload_json,response_json) VALUES(?,?,?,?,?)',
            (cid,'goal','queued-exact-result',json.dumps({'action':'queue','run_id':original['run_id']}),json.dumps({'run_id':later['run_id']})))
    goal=core.goal_snapshot('local','owner',cid,'goal')
    old_result=core.read_result('local','owner',cid,'goal',run_id=original['run_id'])
    current=core.read_result('local','owner',cid,'goal')
    assert old_result['result_revision']==original['terminal_result_revision']
    assert current['result_revision']==core.store.get_run(later['run_id'])['terminal_result_revision']
    assert old_result['run_id']!=goal['run_id'] and current['run_id']==goal['run_id']
