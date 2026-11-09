"""A new explicit owner Retry can recover retained work after transport expiry."""
from datetime import UTC,datetime,timedelta
import copy,json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_coordinator_application import app_core,prepare_dispatch,delegation_values
from test_coordinator_dispatch_recovery import rejected_dispatch,failed_preparation
from test_account_api import account_client,account_headers
from workers_projects_runtime.coordinator import CoordinatorService,CoordinatorConfig,ApplicationBinding,Route,Goal,Control,Dispatch,CoordinatorConflict
from workers_projects_runtime.coordinator_application import dispatch_input,dispatch_notification_ref,finish_notification,notification_fence,prepare_goal
from workers_projects_runtime.coordinator_api import install_coordinator_routes


def advance(monkeypatch):
    import workers_projects_runtime.coordinator_application as module
    original=module.datetime
    class FutureDateTime(original):
        @classmethod
        def now(cls,tz=None):return original.now(UTC)+timedelta(days=1)
    monkeypatch.setattr(module,'datetime',FutureDateTime)


def test_explicit_retry_creates_one_current_event_without_changing_expired_history(app_core,monkeypatch):
    core,cid,_=app_core;details=prepare_dispatch(core,cid);old_values=delegation_values()
    failed_preparation(core,cid,details,old_values);old=rejected_dispatch(core,cid)
    advance(monkeypatch)
    assert core.recover_dispatches('local','owner',cid)==[]
    with pytest.raises(CoordinatorConflict):core.dispatch('local','owner',cid,Dispatch(goal_id='goal',route_id='primary'))
    assert core.goal_snapshot('local','owner',cid,'goal')['actions']==['retry','stop']
    control=Control(action='retry',idempotency_key='fresh-after-expiry')
    accepted=core.control('local','owner',cid,'goal',control)
    current=dispatch_input(core,'local','owner',cid,'goal')
    assert current['notification_ref']!=old['callback_id']
    assert current['redispatch_generation']==1
    assert current['dispatch_digest']==details['dispatch_digest']
    assert current['instruction']==details['instruction'] and current['source_turn_id']==details['source_turn_id']
    assert core.store.get_callback_outbox(old['callback_id'])==old
    new=core.store.get_callback_outbox(current['notification_ref'])
    assert new['status']=='pending' and new['expires_at']>old['expires_at']
    assert core.control('local','owner',cid,'goal',control)==accepted
    assert core.store.get_callback_outbox(new['callback_id'])==new
    assert dispatch_input(core,'local','owner',cid,'goal')['redispatch_generation']==1
    assert notification_fence(core.service,old)=='source_changed'
    fresh=copy.deepcopy(old_values);fresh['request_digest']='new-current-context'
    with pytest.raises(CoordinatorConflict,match='superseded'):
        prepare_goal(core,'local','owner',cid,'goal',current['dispatch_digest'],fresh,old['callback_id'])
    with pytest.raises(CoordinatorConflict,match='superseded'):
        prepare_goal(core,'local','owner',cid,'goal',current['dispatch_digest'],fresh)
    work=prepare_goal(core,'local','owner',cid,'goal',current['dispatch_digest'],fresh,current['notification_ref'])
    assert core.service.started==[work['worker_id']]
    assert core._goal(cid,'goal')['source_turn_id']=='source'
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM callback_outbox WHERE event_type='coordinator.dispatch_due'").fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0]==1


def test_expired_retry_intent_and_event_recover_after_lost_response_without_new_generation(app_core,monkeypatch):
    core,cid,_=app_core;prepare_dispatch(core,cid);rejected_dispatch(core,cid);advance(monkeypatch)
    control=Control(action='retry',idempotency_key='lost-new-generation-response')
    accepted=core.control('local','owner',cid,'goal',control)
    current=dispatch_input(core,'local','owner',cid,'goal');event=core.store.get_callback_outbox(current['notification_ref'])
    with core.store._connect() as conn:
        conn.execute("UPDATE coordinator_actions SET response_json='' WHERE conversation_id=? AND idempotency_key=?",(cid,control.idempotency_key))
    restarted=CoordinatorService(core.store,core.service,core.provider)
    restarted.recover_retry_actions('local','owner',cid)
    assert restarted.control('local','owner',cid,'goal',control)==accepted
    assert core.store.get_callback_outbox(event['callback_id'])==event
    restarted.recover_dispatches('local','owner',cid)
    assert core.store.get_callback_outbox(event['callback_id'])==event
    with core.store._connect() as conn:assert conn.execute("SELECT COUNT(*) FROM callback_outbox WHERE event_type='coordinator.dispatch_due'").fetchone()[0]==2


def test_stale_old_delivery_finish_cannot_change_current_retry_blocker(app_core,monkeypatch):
    core,cid,_=app_core;prepare_dispatch(core,cid);old=rejected_dispatch(core,cid);advance(monkeypatch)
    core.control('local','owner',cid,'goal',Control(action='retry',idempotency_key='current-ref-not-old'))
    current=dispatch_input(core,'local','owner',cid,'goal')
    # Simulate an older leased HTTP response that settles after the explicit generation changed.
    with core.store._connect() as conn:
        conn.execute("UPDATE callback_outbox SET status='pending' WHERE callback_id=?",(old['callback_id'],))
        conn.execute("UPDATE coordinator_goals SET blocker='current-owned-blocker' WHERE conversation_id=?",(cid,))
    leased=core.store.claim_pending_callback(old['callback_id'])
    assert notification_fence(core.service,leased)=='source_changed'
    assert finish_notification(core.store,leased,state='rejected',error='old-error',http_status=409)
    assert core.store.get_callback_outbox(old['callback_id'])['status']=='superseded'
    assert core._goal(cid,'goal')['blocker']=='current-owned-blocker'
    assert core.store.get_callback_outbox(current['notification_ref'])['status']=='pending'


@pytest.mark.parametrize('fence',['stopped','held','reserved','disabled','foreign'])
def test_expired_explicit_retry_retains_current_native_authority_fences(app_core,monkeypatch,fence):
    core,cid,_=app_core;details=prepare_dispatch(core,cid);values=delegation_values()
    failed_preparation(core,cid,details,values);old=rejected_dispatch(core,cid);advance(monkeypatch)
    if fence=='stopped':core.control('local','owner',cid,'goal',Control(action='stop',idempotency_key='stop-old-expired'))
    elif fence=='held':
        with core.store._connect() as conn:conn.execute('UPDATE coordinator_goals SET restore_hold=1 WHERE conversation_id=?',(cid,))
    elif fence=='reserved':core.service.reserve_delegation(**values,start_run=False)
    elif fence=='disabled':
        _,_,config=app_core;core.service.webhook_destinations.update('local','owner',config.application.destination_id,enabled=False)
    with pytest.raises((CoordinatorConflict,PermissionError)):
        core.control('local','other-owner' if fence=='foreign' else 'owner',cid,'goal',Control(action='retry',idempotency_key='new-expired-denied'))
    assert core.store.get_callback_outbox(old['callback_id'])==old
    with core.store._connect() as conn:assert conn.execute("SELECT COUNT(*) FROM callback_outbox WHERE event_type='coordinator.dispatch_due'").fetchone()[0]==1


def test_real_owner_account_route_accepts_expired_goal_retry_once(account_client,monkeypatch):
    from types import SimpleNamespace
    client=account_client;core=client.app.state.coordinator;service=client.app.state.service
    core.provider._model=lambda *_,**__:SimpleNamespace(effort_choices=('high',))
    target=service.webhook_destinations.create('tenant-a','owner-a',name='Synthetic expiry adapter',url='http://127.0.0.1:12345/adapter',payload_mode='envelope')
    cid=core.create('tenant-a','owner-a',CoordinatorConfig(model='synthetic-manager',effort='high',
        routes=[Route(id='primary',profile='codex-cli',model='codex-cli:synthetic-worker',effort='high',execution_mode='host')],
        application=ApplicationBinding(destination_id=target['destination_id'],request_ref='opaque-owner-expiry')))['conversation_id']
    core.accept_turn('tenant-a','owner-a',cid,'original','Original retained request.',[Goal(id='goal',text='Original retained request.')])
    core.dispatch('tenant-a','owner-a',cid,Dispatch(goal_id='goal',route_id='primary'))
    old=rejected_dispatch(core,cid);advance(monkeypatch)
    url=f'/v1/coordinator/conversations/{cid}/goals/goal/actions';body={'action':'retry','idempotencyKey':'real-expired-owner-retry'}
    response=client.post(url,params={'run_id':''},headers=account_headers(),json=body)
    assert response.status_code==202,response.text
    receipt=client.post(url+'/receipt',params={'run_id':''},headers=account_headers(),json=body)
    assert receipt.status_code==202 and receipt.json()['idempotentReplay'] is True
    assert core.store.get_callback_outbox(old['callback_id'])==old
    with core.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM callback_outbox WHERE event_type='coordinator.dispatch_due'").fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM workers').fetchone()[0]==0


def test_existing_prepare_route_requires_current_notification_after_explicit_generation(app_core,monkeypatch):
    core,cid,_=app_core;details=prepare_dispatch(core,cid);old=rejected_dispatch(core,cid);advance(monkeypatch)
    core.control('local','owner',cid,'goal',Control(action='retry',idempotency_key='route-current-source-only'))
    current=dispatch_input(core,'local','owner',cid,'goal')
    app=FastAPI();install_coordinator_routes(app,core,lambda request:('local','owner'),lambda *_:None,
        application_delegation_values=lambda *args,**kwargs:delegation_values(),
        application_work_response=lambda work,request:{'workRef':work['work_ref']})
    client=TestClient(app);url=f'/v1/coordinator/conversations/{cid}/goals/goal/prepare'
    body={'dispatch_digest':current['dispatch_digest'],'delegation':{'title':'Original work','goal':'Make a tiny local page.','instruction':'Make a tiny local page.'}}
    assert client.post(url,json={**body,'notification_ref':old['callback_id']}).status_code==409
    assert client.post(url,json=body).status_code==409
    accepted=client.post(url,json={**body,'notification_ref':current['notification_ref']})
    assert accepted.status_code==200,accepted.text
    assert accepted.json()['goal']['run_id'] and len(core.service.started)==1
