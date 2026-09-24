import json
from pathlib import Path
import pytest
from workers_projects_runtime.run_evidence import build_constraint_ledger, build_run_evidence, summarize_run_evidence_result


def evidence(tmp_path, *, instruction='Review the note and report the result.', output='Reviewed and saved the note.', contract=None, exit_code=0, error=''):
    worker={'worker_id':'wrk_fixture','profile':'claude-code','execution_mode':'host'}
    if contract is not None:
        worker['bootstrap_bundle_json']=json.dumps({'viventium_continuation_contract': {'version':1,'run_id':'run_fixture','source':{'source_event_id':'event_fixture','source_revision':2,'surface':'web'},'output':contract}})
    ledger=build_constraint_ledger(instruction=instruction,worker=worker,run_id='run_fixture')
    return build_run_evidence(worker=worker,run_id='run_fixture',runtime_name='claude-code',model='fixture-model',command=['fixture'],env={},workspace_dir=tmp_path,stdout_text='',stderr_text='',output_text=output,error_text=error,exit_code=exit_code,timeout_seconds=60,stop_reason='process_exit',constraint_ledger=ledger)


@pytest.mark.parametrize('output',['Reviewed and saved the note.','FINAL REPORT:\nReviewed and saved the note.'])
def test_prose_does_not_create_missing_artifact_warning(tmp_path,output):
    workspace=tmp_path/'workspace';workspace.mkdir()
    outside=tmp_path/'authorized-output';outside.mkdir()
    artifact=outside/'review.txt';artifact.write_text('Reviewed document.\n')
    result=evidence(workspace,instruction=f'Review and save the handoff at {artifact}. Keep the original TXT file and seed document.',output=output)
    completion=result['completion_compliance']
    assert completion['status']=='not_applicable'
    assert completion['missing_required_artifact_types']==[]
    assert completion['inventory_scope']=='workspace'
    assert not any(x['reason'] in {'completion diagnostic warning','final report marker missing'} for x in result['evidence_result']['warning_reasons'])
    assert artifact.read_text()=='Reviewed document.\n'
    assert result['artifacts']['count']==0


def contract(formats):
    return {'mode':'replace','required':[],'forbidden':[],'formats':formats,'forbidden_formats':[]}


def test_exact_typed_format_is_required_and_verified(tmp_path):
    missing=evidence(tmp_path,contract=contract(['txt']))
    assert missing['completion_compliance']['status']=='fail'
    assert {'reason':'typed output formats missing','formats':['txt']} in missing['evidence_result']['failure_reasons']
    (tmp_path/'answer.txt').write_text('Actual output\n')
    present=evidence(tmp_path,contract=contract(['txt']))
    assert present['completion_compliance']['status']=='pass'
    assert present['evidence_result']['failure_reasons']==[]


def test_zero_byte_typed_output_remains_missing(tmp_path):
    (tmp_path/'answer.txt').touch()
    result=evidence(tmp_path,contract=contract(['txt']))
    assert result['completion_compliance']['missing_required_artifact_types']==['txt']
    assert result['evidence_result']['status']=='fail'


@pytest.mark.parametrize(('output','error','code'),[('','','0'),('Useful partial output','process failed',1)])
def test_real_empty_and_failed_native_result_still_fail(tmp_path,output,error,code):
    result=evidence(tmp_path,output=output,error=error,exit_code=int(code))
    assert result['evidence_result']['status']=='fail'


def test_legacy_completion_guess_is_not_promoted_to_user_warning():
    summary=summarize_run_evidence_result({'completion_compliance':{'status':'fail','issues':[{'reason':'required artifact types missing','missing_required_artifact_types':['txt']}]},'final_output':{'output_chars':50,'status':'ok','error_present':False,'has_final_report':False}})
    assert summary=={'status':'pass','failure_reasons':[],'warning_reasons':[]}


def test_run_evidence_reference_keeps_exact_run_after_next_completion(tmp_path):
    from workers_projects_runtime.run_evidence import write_run_evidence
    first=write_run_evidence(tmp_path,{'run_id':'run_a','value':'first'},'run_a')
    second=write_run_evidence(tmp_path,{'run_id':'run_b','value':'second'},'run_b')
    assert first != second
    assert json.loads(first.read_text())=={'run_id':'run_a','value':'first'}
    assert json.loads(second.read_text())=={'run_id':'run_b','value':'second'}
    assert json.loads((tmp_path/'glasshive-run/evidence.json').read_text())['run_id']=='run_b'
