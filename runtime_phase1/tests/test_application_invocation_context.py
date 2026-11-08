"""Complete current application context cannot retain prior cognition or alter Native commitments."""
import copy
import json

import pytest
from test_account_api import account_client
from test_coordinator_application import app_core

from workers_projects_runtime.bootstrap import apply_bootstrap, canonicalize_viventium_feeling_projection
from workers_projects_runtime.service import application_invocation_bundle, native_execution_bindings, WorkersProjectsService


def stored():
    return {'application_context_format':'opaque_v1','agents_md':'Previous memory and state.',
        'viventium_feelings_projection':{'version':1,'enabled':False},
        'glasshive_capability_broker':{'grant_id':'old-grant','url':'http://old.invalid'},
        'env':{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'old-bearer','WPR_CODEX_CLI_REASONING_EFFORT':'high'},
        'callbacks':{'origin_ref':'original-origin','events_webhook_url':'http://127.0.0.1/owned'},
        'provider_model':'synthetic-worker','viventium_launch_authority':{'version':1,'kind':'conversation_orchestrator','execution_mode':'host','worker_model':'synthetic-worker','worker_reasoning_effort':'high'},
        'viventium_delegation_identity':{'version':1,'idempotency_key':'original-key'},
        'viventium_delegation_context':{'version':1,'source_event_id':'original-source'}}


def fresh():
    return {'application_context_format':'opaque_v1','agents_md':'Current memory and state.',
        'inherited_conversation':{'version':1,'conversation_id':'synthetic-conversation','sources':[{'id':'original','role':'user','text':'Exact original task.'}]},
        'env':{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'current-bearer'},
        'glasshive_capability_broker':{'grant_id':'current-grant','url':'http://current.invalid'}}


def test_whole_current_frame_replaces_old_app_fields_and_bootstraps_without_legacy_projection(tmp_path):
    original=stored();current=fresh();before=copy.deepcopy(original)
    with pytest.raises(ValueError,match='Opaque context'):
        canonicalize_viventium_feeling_projection(original)
    result=application_invocation_bundle(original,current)
    assert original==before
    assert 'viventium_feelings_projection' not in result
    assert result['agents_md']==current['agents_md']
    assert result['glasshive_capability_broker']==current['glasshive_capability_broker']
    assert result['env']=={'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'current-bearer','WPR_CODEX_CLI_REASONING_EFFORT':'high'}
    assert native_execution_bindings(result)==native_execution_bindings(original)
    assert canonicalize_viventium_feeling_projection(result)==result
    apply_bootstrap(home_dir=tmp_path/'home',workspace_dir=tmp_path/'workspace',runtime_name='codex-cli',
        worker={'bootstrap_bundle_json':json.dumps(result)},copy_file=lambda *_:None,copy_tree=lambda *_:None)
    text=(tmp_path/'workspace/AGENTS.md').read_text()
    assert 'Current memory and state.' in text and 'Previous memory and state.' not in text


@pytest.mark.parametrize('key',['provider_model','provider_capabilities','access_mode','run_mode','execution_policy','callbacks',
    'glasshive_capability_authorization','capability_authorization','glasshive_capability_requirement',
    'viventium_delegation_identity','viventium_delegation_context','viventium_delegation_assertion','viventium_delegation_packet','viventium_constraint_source'])
def test_current_context_cannot_change_or_create_native_binding(key):
    original=stored();current=fresh();current[key]='different-native-binding'
    with pytest.raises(ValueError,match='Native execution binding'):
        application_invocation_bundle(original,current)


def test_current_context_respects_existing_launch_alias_and_effort_owners():
    original=stored();current=fresh();current['launch_authority']=original['viventium_launch_authority']
    result=application_invocation_bundle(original,current)
    assert result['viventium_launch_authority']==original['viventium_launch_authority']
    assert 'launch_authority' not in result
    current['launch_authority']={**current['launch_authority'],'worker_model':'different'}
    with pytest.raises(ValueError,match='Native launch binding'):application_invocation_bundle(original,current)
    current=fresh();current['env']['WPR_CODEX_CLI_REASONING_EFFORT']='low'
    with pytest.raises(ValueError,match='Native effort policy'):application_invocation_bundle(original,current)


@pytest.mark.parametrize('current',[{}, {'application_context_format':'legacy'},None])
def test_missing_or_nonopaque_frame_cannot_fall_back_to_previous_context(current):
    with pytest.raises(ValueError):application_invocation_bundle(stored(),current)


def test_runtime_marker_projects_native_bindings_without_cognition_or_any_stored_bearer():
    original=stored();worker={'bootstrap_bundle_json':json.dumps(original)}
    result=WorkersProjectsService._runtime_worker_for_run(worker,{'runtime_bundle_json':json.dumps({'application_invocation_context_required':True})})
    assert result['_requires_application_invocation_context'] is True
    assert json.loads(result['bootstrap_bundle_json'])==native_execution_bindings(original)
    assert 'old-bearer' not in result['bootstrap_bundle_json'] and 'Previous memory' not in result['bootstrap_bundle_json']


def test_real_action_owner_replaces_bad_context_before_native_admission_and_restart_cannot_use_old_bearer(account_client,monkeypatch):
    from test_account_api import account_headers
    from test_coordinator_application_actions import bind_goal,signed_action
    from workers_projects_runtime.broker_admission import BrokerAdmissionError
    monkeypatch.setenv('GLASSHIVE_PROVIDER_BOOTSTRAP_SIGNATURE_SECRET','synthetic-action-context-key')
    core,cid,work=bind_goal(account_client)
    worker=core.store.get_worker(work['worker_id']);old=json.loads(worker['bootstrap_bundle_json'])
    authorization={'version':1,'status':'pending_admission','authorization_ref':'synthetic-native-authorization',
        'origin_ref':work['origin_ref'],'scope_fingerprint':'synthetic-owned-scope','max_expires_at':'2099-01-01T00:00:00Z'}
    old.update({'application_context_format':'opaque_v1','viventium_feelings_projection':{'version':1,'enabled':False},
        'agents_md':'Prior memory must disappear.','glasshive_capability_authorization':authorization,
        'env':{**old.get('env',{}),'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'old-stored-bearer'}})
    core.store.update_worker(work['worker_id'],bootstrap_bundle_json=json.dumps(old))
    body,headers=signed_action({'action':'message','instruction':'Keep the exact original task.', 'idempotencyKey':'current-complete-context'})
    response=account_client.post(f'/v1/coordinator/conversations/{cid}/goals/goal/actions',params={'run_id':work['current_run_id']},headers=headers,json=body)
    assert response.status_code==202,response.text
    run=core.store.get_run(response.json()['runId']);observed=[]
    class AdmissionObserved(Exception):pass
    def observe(current,*args,**kwargs):
        prepared=json.loads(current['bootstrap_bundle_json']);observed.append(prepared)
        assert 'viventium_feelings_projection' not in prepared
        assert 'Prior memory' not in current['bootstrap_bundle_json']
        assert prepared['glasshive_capability_authorization']==authorization
        assert 'GLASSHIVE_CAPABILITY_BROKER_TOKEN' not in prepared['env']
        raise AdmissionObserved()
    monkeypatch.setattr(core.service,'_run_local_admitted_worker',observe)
    with pytest.raises(AdmissionObserved):core.service._run_local_worker(core.store.get_worker(work['worker_id']),run)
    core.service.discard_run_local_bundle(run['run_id'])
    with pytest.raises(BrokerAdmissionError,match='refreshed'):
        core.service._run_local_worker(core.store.get_worker(work['worker_id']),run)
    assert len(observed)==1
    assert json.loads(core.store.get_worker(work['worker_id'])['bootstrap_bundle_json'])==old


@pytest.mark.parametrize('access',['ask','full'])
def test_host_application_continuation_keeps_placement_and_auth_without_docker_new_launch_validator(app_core,access):
    from test_coordinator_application import prepare_dispatch,delegation_values
    from workers_projects_runtime.coordinator_application import prepare_goal,validate_action_bundle
    core,cid,_=app_core;details=prepare_dispatch(core,cid);values=delegation_values()
    values['bootstrap_bundle']['launch_authority']['worker_access']=access
    prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],values)
    core.service._bootstrap_bundle_for=lambda worker:json.loads(worker['bootstrap_bundle_json'])
    current={'application_context_format':'opaque_v1','agents_md':'Current complete worker context.',
             'env':{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'current-invocation-bearer'}}
    assert validate_action_bundle(core,'local','owner',cid,'goal',current)==current
    worker=core.store.get_worker(core._goal(cid,'goal')['worker_id'])
    effective=application_invocation_bundle(json.loads(worker['bootstrap_bundle_json']),current)
    assert effective['launch_authority']['worker_access']==access
    assert effective['launch_authority']['execution_mode']=='host'


@pytest.mark.parametrize('change',['access','mode','model','effort','policy'])
def test_host_continuation_cannot_escalate_or_change_bound_native_policy(app_core,change):
    from test_coordinator_application import prepare_dispatch,delegation_values
    from workers_projects_runtime.coordinator_application import prepare_goal,validate_action_bundle
    from workers_projects_runtime.coordinator import CoordinatorScopeError
    core,cid,_=app_core;details=prepare_dispatch(core,cid);values=delegation_values()
    values['bootstrap_bundle']['launch_authority']['worker_access']='ask'
    prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],values)
    core.service._bootstrap_bundle_for=lambda worker:json.loads(worker['bootstrap_bundle_json'])
    authority=copy.deepcopy(values['bootstrap_bundle']['launch_authority'])
    current={'application_context_format':'opaque_v1','launch_authority':authority}
    if change=='access':authority['worker_access']='full'
    elif change=='mode':authority['execution_mode']='docker'
    elif change=='model':authority['worker_model']='different'
    elif change=='effort':authority['worker_reasoning_effort']='low'
    else:current['execution_policy']='different-policy'
    with pytest.raises(CoordinatorScopeError):validate_action_bundle(core,'local','owner',cid,'goal',current)


def test_docker_continuation_still_uses_its_existing_clean_room_validation(app_core):
    from test_coordinator_application import prepare_dispatch,delegation_values
    from workers_projects_runtime.coordinator_application import prepare_goal,validate_action_bundle
    from workers_projects_runtime.service import ParallelExecutionIsolationError
    core,cid,_=app_core;details=prepare_dispatch(core,cid)
    prepare_goal(core,'local','owner',cid,'goal',details['dispatch_digest'],delegation_values())
    worker_id=core._goal(cid,'goal')['worker_id']
    core.store.update_worker(worker_id,execution_mode='docker')
    core.service._bootstrap_bundle_for=lambda worker:json.loads(worker['bootstrap_bundle_json'])
    with pytest.raises(ParallelExecutionIsolationError):
        validate_action_bundle(core,'local','owner',cid,'goal',{'application_context_format':'opaque_v1',
            'env':{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'not-a-docker-new-launch-grant'}})
