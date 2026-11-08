"""Existing worker actions materialize the same selected uploads as new delegations."""
import base64,copy,hashlib,hmac,json,time
from pathlib import Path
import pytest

from test_account_api import account_client,account_headers
from test_coordinator_application_actions import bind_goal
from workers_projects_runtime.bootstrap import apply_bootstrap
from workers_projects_runtime.service import application_invocation_bundle
from workers_projects_runtime.upload_projection import project_bootstrap_upload_files


def extracted(file_id='selected-csv',owner='owner-a'):
    return {'file_id':file_id,'filename':file_id+'.csv','source':'text','text':'item,quantity,price\nalpha,7,13\n',
            'bytes':29,'user':owner}


def carrier(bundle):
    raw=base64.b64encode(json.dumps(bundle).encode()).decode();stamp=str(int(time.time()))
    signature=hmac.new(b'synthetic-action-upload-key',f'v1\n{stamp}\n{raw}'.encode(),hashlib.sha256).hexdigest()
    return raw,{**account_headers(),'X-GlassHive-Bootstrap-Timestamp':stamp,'X-GlassHive-Bootstrap-Signature':signature}


def test_shared_projection_preserves_selected_identity_representation_and_existing_files():
    selected=extracted();other=extracted('unselected-csv');bundle={'application_context_format':'opaque_v1',
        'glasshive_upload_context':{'tool_uploaded_files':[selected,other]},
        'viventium_delegation_packet':{'selected_files':[{'ordinal':0,'ref':selected['file_id']}]},
        'files':[{'scope':'workspace','path':'kept.txt','content':'Existing current file.'}]}
    original=copy.deepcopy(bundle)
    projected=project_bootstrap_upload_files(bundle,tenant_id='tenant-a',owner_id='owner-a')
    assert bundle==original
    assert projected['files'][0]==bundle['files'][0]
    attached=projected['files'][1]
    assert attached['file_id']==selected['file_id'] and attached['source']=='text'
    assert attached['content']==selected['text'] and attached['bytes']==selected['bytes']
    assert 'source_path' not in attached
    assert {entry.get('file_id') for entry in projected['files']}=={None,'selected-csv'}
    assert project_bootstrap_upload_files(projected,tenant_id='tenant-a',owner_id='owner-a')==projected
    bundle['viventium_delegation_packet']['selected_files']=[]
    assert project_bootstrap_upload_files(bundle,tenant_id='tenant-a',owner_id='owner-a')['files']==bundle['files']


@pytest.mark.parametrize('field,value',[('user','foreign-owner'),('tenant_id','foreign-tenant')])
def test_selected_upload_cannot_cross_authenticated_owner_or_tenant(field,value):
    record=extracted();record[field]=value
    with pytest.raises(ValueError,match='authenticated owner'):
        project_bootstrap_upload_files({'glasshive_upload_context':{'tool_uploaded_files':[record]}},tenant_id='tenant-a',owner_id='owner-a')


def test_original_upload_path_cannot_escape_existing_upload_projection(monkeypatch):
    import workers_projects_runtime.upload_projection as projection
    monkeypatch.setattr(projection,'_owner_sources_for_file_ids',lambda *args,**kwargs:{})
    bundle={'glasshive_upload_context':{'tool_uploaded_files':[{'file_id':'original-pdf','filename':'original.pdf','source':'local','filepath':'/outside-owned-uploads/private.pdf'}]}}
    result=project_bootstrap_upload_files(bundle,tenant_id='tenant-a',owner_id='owner-a')
    assert all('source_path' not in entry for entry in result['files'])
    assert all(entry.get('content')!='Original private bytes.' for entry in result['files'])


def test_signed_same_worker_action_materializes_selected_text_and_keeps_exact_hash(account_client,monkeypatch,tmp_path):
    monkeypatch.setenv('GLASSHIVE_PROVIDER_BOOTSTRAP_SIGNATURE_SECRET','synthetic-action-upload-key')
    core,cid,work=bind_goal(account_client);selected=extracted()
    bundle={'application_context_format':'opaque_v1','env':{'GLASSHIVE_CAPABILITY_BROKER_TOKEN':'synthetic-current-token'},
            'glasshive_upload_context':{'tool_uploaded_files':[selected]},
            'inherited_conversation':{'version':1,'conversation_id':'original-app-conversation','sources':[{'id':'original-source','role':'user','text':'Use the original CSV.'}]}}
    encoded,headers=carrier(bundle)
    body={'action':'message','instruction':'Continue the original CSV work.','idempotencyKey':'same-worker-file-recovery',
          'application_bootstrap_bundle_b64':encoded}
    url=f'/v1/coordinator/conversations/{cid}/goals/goal/actions'
    response=account_client.post(url,params={'run_id':work['current_run_id']},headers=headers,json=body)
    assert response.status_code==202,response.text
    run_id=response.json()['runId'];run=core.store.get_run(run_id)
    assert run['worker_id']==work['worker_id']
    staged=core.service._run_local_bundles[run_id]
    assert len(staged['files'])==1 and staged['files'][0]['file_id']==selected['file_id']
    effective=application_invocation_bundle(json.loads(core.store.get_worker(work['worker_id'])['bootstrap_bundle_json']),staged)
    workspace=tmp_path/'materialized'
    apply_bootstrap(home_dir=tmp_path/'home',workspace_dir=workspace,runtime_name='codex-cli',
        worker={'bootstrap_bundle_json':json.dumps(effective)},copy_file=lambda *_:None,copy_tree=lambda *_:None)
    file=workspace/staged['files'][0]['path'];raw=file.read_bytes()
    assert raw==selected['text'].encode()
    assert hashlib.sha256(raw).hexdigest()==hashlib.sha256(selected['text'].encode()).hexdigest()
    # Immutable action receipt reads do not project or reserve a second effect.
    replay=account_client.post(url,params={'run_id':run_id},headers={**headers,**account_headers()},json=body)
    assert replay.status_code==202,replay.text
    assert replay.json()['runId']==run_id
    with core.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM active_work_action_uses WHERE idempotency_key=?',(body['idempotencyKey'],)).fetchone()[0]==1


def test_signature_and_owner_failures_cannot_reserve_an_upload_action(account_client,monkeypatch):
    monkeypatch.setenv('GLASSHIVE_PROVIDER_BOOTSTRAP_SIGNATURE_SECRET','synthetic-action-upload-key')
    core,cid,work=bind_goal(account_client)
    bundle={'application_context_format':'opaque_v1','glasshive_upload_context':{'tool_uploaded_files':[extracted(owner='foreign-owner')]}}
    encoded,headers=carrier(bundle);body={'action':'message','instruction':'Use the current file.','idempotencyKey':'foreign-action-upload-denied','application_bootstrap_bundle_b64':encoded}
    url=f'/v1/coordinator/conversations/{cid}/goals/goal/actions'
    forged=account_client.post(url,params={'run_id':work['current_run_id']},headers=account_headers(),json=body)
    assert forged.status_code==403,forged.text
    foreign=account_client.post(url,params={'run_id':work['current_run_id']},headers=headers,json=body)
    assert foreign.status_code==400,foreign.text
    with core.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM coordinator_actions WHERE conversation_id=?',(cid,)).fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM active_work_action_uses WHERE idempotency_key=?',(body['idempotencyKey'],)).fetchone()[0]==0
