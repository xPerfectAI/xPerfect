import json
import hashlib
from pathlib import Path

import pytest
from workers_projects_runtime.profile_runtime import ProfiledWorkerRuntime
from workers_projects_runtime.conversation_provider import _native_tool_evidence, CONVERSATION_TOOL_RESULT_MAX_BYTES


def line(payload):
    return json.dumps(payload, ensure_ascii=False)+'\n'


def native_turn(turn_id, instruction, *, output='Source bytes', call_id='call-current'):
    def event(payload):
        return {'type':'response_item','payload': {**payload,'internal_chat_message_metadata_passthrough':{'turn_id':turn_id}}}
    return [
        {'type':'turn_context','payload':{'turn_id':turn_id}},
        event({'type':'message','role':'user','content':[{'type':'input_text','text':instruction}]}),
        event({'type':'custom_tool_call','name':'exec','call_id':call_id,'input':'original typed call input','status':'completed'}),
        event({'type':'custom_tool_call_output','call_id':call_id,'output':[{'type':'input_text','text':output}]}),
    ]


def setup_native(tmp_path):
    runtime=ProfiledWorkerRuntime(base_dir=str(tmp_path/'private-state'))
    worker={'worker_id':'wrk_source','profile':'codex-cli','execution_mode':'host'}
    run_id='run_source'
    run=runtime.host_codex._run_root(worker['worker_id'],run_id);run.mkdir(parents=True)
    stdout=line({'type':'thread.started','thread_id':'thread-source'})+line({'type':'item.completed','item':{'type':'web_search','id':'status-only','action':{'type':'search'}}})
    (run/'stdout.log').write_text(stdout)
    (run/'instruction.stdin').write_text('exact admitted input')
    sessions=runtime.host_codex._home_dir(worker['worker_id'])/'.codex/sessions';sessions.mkdir(parents=True)
    rollout=sessions/'rollout-thread-source.jsonl'
    rollout.write_text(''.join(map(line,native_turn('old','prior input',output='exclude sibling',call_id='call-prior')+native_turn('current','exact admitted input'))))
    return runtime,worker,run_id,run,rollout


def test_actual_native_custom_call_bytes_reach_existing_evidence_owner(tmp_path):
    runtime,worker,run_id,_,_=setup_native(tmp_path)
    profile, stdout = runtime.provider_tool_evidence_log(
        worker, run_id,
        instruction_sha256=hashlib.sha256(b"exact admitted input").hexdigest(),
    )
    evidence=_native_tool_evidence(profile,stdout)
    assert len(evidence['results'])==1
    item=evidence['results'][0]
    assert item['id']=='call-current' and item['name']=='exec' and item['status']=='completed'
    assert item['arguments']['text']=='original typed call input'
    assert json.loads(item['output']['text'])==[{'type':'input_text','text':'Source bytes'}]
    assert item['output']['omitted_bytes']==0
    assert 'exclude sibling' not in json.dumps(evidence)
    assert '"sources":null' not in json.dumps(evidence)
    assert evidence['omitted_results']==1


@pytest.mark.parametrize('mismatch',['input','duplicate','thread','foreign-turn','outside-session'])
def test_rollout_requires_exact_current_input_session_and_turn(tmp_path,mismatch):
    runtime,worker,run_id,run,rollout=setup_native(tmp_path)
    if mismatch=='input': (run/'instruction.stdin').write_text('different admitted input')
    elif mismatch=='duplicate':
        with rollout.open('a') as f:f.write(''.join(map(line,native_turn('duplicate','exact admitted input',output='other result'))))
    elif mismatch=='thread': (run/'stdout.log').write_text(line({'type':'thread.started','thread_id':'other'}))
    elif mismatch=='foreign-turn':
        events=[json.loads(x) for x in rollout.read_text().splitlines()]
        events[-1]['payload']['internal_chat_message_metadata_passthrough']['turn_id']='foreign'
        rollout.write_text(''.join(map(line,events)))
    else:
        elsewhere=tmp_path/'outside.jsonl';rollout.rename(elsewhere);rollout.symlink_to(elsewhere)
    profile,stdout=runtime.provider_tool_evidence_log(worker,run_id,instruction_sha256=hashlib.sha256(b"exact admitted input").hexdigest())
    evidence=_native_tool_evidence(profile,stdout)
    assert evidence['results']==[] and evidence['omitted_results']>0


def test_rollout_output_clips_with_exact_original_hash_and_length(tmp_path):
    import hashlib
    runtime,worker,run_id,_,rollout=setup_native(tmp_path)
    output='é'*CONVERSATION_TOOL_RESULT_MAX_BYTES
    rollout.write_text(''.join(map(line,native_turn('current','exact admitted input',output=output))))
    profile,stdout=runtime.provider_tool_evidence_log(worker,run_id,instruction_sha256=hashlib.sha256(b"exact admitted input").hexdigest())
    evidence=_native_tool_evidence(profile,stdout)['results'][0]['output']
    raw=json.dumps([{'type':'input_text','text':output}],ensure_ascii=False,separators=(',',':')).encode()
    assert evidence['bytes']==len(raw)
    assert evidence['sha256']==hashlib.sha256(raw).hexdigest()
    assert evidence['omitted_bytes']==len(raw)-len(evidence['text'].encode())>0


@pytest.mark.parametrize("failure", ["response", "output"])
def test_typed_native_output_failure_keeps_returned_bytes(failure):
    events=native_turn("current", "input")
    if failure=="response": events[-1]["payload"]["is_error"]=True
    else: events[-1]["payload"]["output"]={"isError":True,"content":[{"type":"text","text":"Typed failure detail"}]}
    item=_native_tool_evidence("codex-cli", "".join(map(line,events)))["results"][0]
    assert item["status"]=="failed"
    assert json.loads(item["output"]["text"])==events[-1]["payload"]["output"]
