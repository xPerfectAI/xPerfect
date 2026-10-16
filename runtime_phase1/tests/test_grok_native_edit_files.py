"""Typed successful native edits use the retained selected-file artifact owner."""
import hashlib
import json
from pathlib import Path

import pytest

from workers_projects_runtime.grok_runtime import HostGrokBuildRuntime
from workers_projects_runtime.openclaw_runtime import RuntimeInfo
from test_conversation_provider import lifecycle_owned_test_clients


def fixture(tmp_path, *, conversation=False, path='drawing.svg', status='completed',
            kind='write', read_only=False, changed=False, session='native-session'):
    runtime = HostGrokBuildRuntime(str(tmp_path / 'runtime'))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    source = Path(path) if Path(path).is_absolute() else workspace / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('<svg>synthetic</svg>')
    worker = {'worker_id': 'worker-test', 'owner_id': 'owner-test', 'profile': 'grok-build',
              'workspace_dir': str(workspace), '_active_run_id': 'run-test',
              '_run_attempt_id': 'attempt-test', 'bootstrap_bundle_json': json.dumps(
                  {'run_mode': 'conversation'} if conversation else {})}
    info = RuntimeInfo('grok-build', 'grok-selected', '', None, None, None,
                       str(tmp_path / 'state'), str(workspace), None)
    meta = {'x.ai/tool': {'version': 1, 'kind': kind, 'read_only': read_only,
                         'input': {'path': str(source)}}}
    events = [{'type': 'grok.session.started', 'session_id': 'native-session', 'model': 'grok-selected'},
              {'type': 'grok.session.update', 'session_id': session, 'update': {
                  'sessionUpdate': 'tool_call', 'toolCallId': 'call-test', '_meta': meta}},
              {'type': 'grok.session.update', 'session_id': session, 'update': {
                  'sessionUpdate': 'tool_call_update', 'toolCallId': 'call-test', 'status': status,
                  'content': [{'type': 'diff', 'path': str(source), 'oldText': '',
                               'newText': '<svg>synthetic</svg>'}]}},
              {'type': 'grok.result', 'session_id': 'native-session', 'stop_reason': 'end_turn',
               'output': 'Created `drawing.svg`.'}]
    stdout = '\n'.join(map(json.dumps, events))
    root = runtime._run_root(worker['worker_id'], worker['_active_run_id'])
    root.mkdir(parents=True, exist_ok=True)
    runtime._claim_run_transcript(root, worker['_run_attempt_id'])
    (root / 'stdout.log').write_text(stdout)
    scope = {'run_id': 'run-test', 'worker_id': 'worker-test', 'owner_id': 'owner-test',
             'workspace_dir': str(workspace), 'attempt_id': 'attempt-test',
             'file_output_transport': 'artifact_sha256'}
    (root / 'native-image-scope.json').write_text(json.dumps(scope))
    if changed:
        source.write_text('unrelated later bytes')
    return runtime, worker, info, root, stdout, source


@pytest.mark.parametrize('conversation', [False, True])
def test_completed_native_write_retains_verified_file_without_markdown(tmp_path, conversation):
    runtime, worker, info, root, stdout, source = fixture(tmp_path, conversation=conversation)
    assert runtime._parse_output(worker, stdout, '', info) == ('native-session', 'Created `drawing.svg`.')
    scope = json.loads((root / 'native-image-scope.json').read_text())
    assert len(scope['output_files']) == 1
    file = scope['output_files'][0]
    assert file['root_path'] == 'drawing.svg' and file['bytes'] == 20
    assert file['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_text('later modification')
    images = runtime.provider_native_image_output(worker, {'run_id': 'run-test',
        'worker_id': 'worker-test', 'active_attempt_id': 'attempt-test'}, 'Created `drawing.svg`.')[1]
    assert len(images) == 1 and images[0][2].sha256 == file['sha256']


@pytest.mark.parametrize('options', [
    {'status': 'failed'}, {'status': 'cancelled'}, {'status': 'pending'},
    {'kind': 'read', 'read_only': True}, {'read_only': True},
    {'session': 'other-session'}, {'changed': True}, {'path': '.private/drawing.svg'},
    {'path': 'tmp/drawing.svg'},
])
def test_unproved_or_operational_native_files_are_not_published(tmp_path, options):
    runtime, worker, info, root, stdout, _ = fixture(tmp_path, **options)
    runtime._parse_output(worker, stdout, '', info)
    assert not json.loads((root / 'native-image-scope.json').read_text()).get('output_files')


@pytest.mark.parametrize('mode', ['stale', 'missing-owner', 'wrong-transcript', 'symlink', 'outside'])
def test_capture_keeps_attempt_and_source_root_boundaries(tmp_path, mode):
    options = {'path': str(tmp_path / 'outside' / 'drawing.svg')} if mode == 'outside' else {}
    runtime, worker, info, root, stdout, source = fixture(tmp_path, **options)
    if mode == 'stale':
        (root / 'transcript-owner.json').write_text(json.dumps({'attempt_id': 'old', 'launch_id': 'old'}))
    elif mode == 'missing-owner':
        (root / 'transcript-owner.json').unlink()
    elif mode == 'wrong-transcript':
        (root / 'stdout.log').write_text('another launch')
    elif mode == 'symlink':
        real = tmp_path / 'real.svg'
        real.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(real)
    runtime._parse_output(worker, stdout, '', info)
    assert not json.loads((root / 'native-image-scope.json').read_text()).get('output_files')


def test_native_path_is_literal_and_successive_edits_keep_final_bytes(tmp_path):
    runtime, worker, info, root, stdout, source = fixture(tmp_path, path='drawing%20two.svg')
    events = list(runtime._events(stdout))
    final = events.pop()
    second = json.loads(json.dumps(events[-1]))
    second['update']['toolCallId'] = 'call-second'
    second['update']['_meta'] = events[1]['update']['_meta']
    second['update']['content'][0]['oldText'] = '<svg>synthetic</svg>'
    second['update']['content'][0]['newText'] = '<svg>final</svg>'
    events += [second, final]
    stdout = '\n'.join(map(json.dumps, events))
    (root / 'stdout.log').write_text(stdout)
    source.write_text('<svg>final</svg>')
    runtime._parse_output(worker, stdout, '', info)
    file = json.loads((root / 'native-image-scope.json').read_text())['output_files'][0]
    assert file['root_path'] == 'drawing%20two.svg'
    assert file['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize('stream', [False, True])
def test_canonical_conversation_attaches_captured_native_file_without_markdown(
    tmp_path, monkeypatch, stream,
):
    from test_native_output_file_carrier import prepare
    from test_conversation_provider import _payload
    from test_conversation_provider_recovery import _post_bound, _result
    client, runtime, data = prepare(tmp_path, monkeypatch, publish=False)
    original_run = runtime.run_task

    def run_task(worker, instruction, timeout_sec=None, run_id=None):
        original_run(worker, instruction, timeout_sec, run_id)
        scope = {'run_id': run_id, 'worker_id': worker['worker_id'], 'owner_id': worker['owner_id'],
                 'workspace_dir': worker['workspace_dir'], 'attempt_id': worker['_run_attempt_id'],
                 'file_output_transport': 'artifact_sha256'}
        runtime.native._capture_native_output_files(worker, worker['workspace_dir'], scope,
            run_id, 'Created the file.', native_edits=((str(tmp_path / 'result.csv'),
                hashlib.sha256(data).hexdigest()),))
        return 'Created the file.'

    runtime.run_task = run_task
    response, digest = _post_bound(client, _payload(tmp_path, stream=stream))
    assert response.status_code == 200, response.text
    canonical = _result(client, digest).json()['response']
    files = canonical['glasshive']['output_files']['files']
    assert len(files) == 1 and files[0]['filename'] == 'result.csv'
    assert client.get(files[0]['download_url']).content == data
    assert canonical['choices'][0]['message']['content'] == 'Created the file.'


@pytest.mark.parametrize('field,value', [('owner_id', 'other-owner'), ('attempt_id', 'old-attempt'),
    ('run_id', 'other-run'), ('workspace_dir', '/synthetic/other-root')])
def test_native_edit_manifest_cannot_expand_accepted_scope(tmp_path, field, value):
    runtime, worker, info, root, stdout, _ = fixture(tmp_path)
    scope = json.loads((root / 'native-image-scope.json').read_text())
    scope[field] = value
    (root / 'native-image-scope.json').write_text(json.dumps(scope))
    runtime._parse_output(worker, stdout, '', info)
    assert not json.loads((root / 'native-image-scope.json').read_text()).get('output_files')


@pytest.mark.parametrize('conversation', [False, True])
@pytest.mark.parametrize('kind', ['write', 'edit'])
def test_completed_native_full_bytes_are_captured_when_old_matches_new(tmp_path, conversation, kind):
    runtime, worker, info, root, stdout, source = fixture(
        tmp_path, conversation=conversation, kind=kind,
    )
    events = list(runtime._events(stdout))
    completed = events[2]['update']['content'][0]
    events[1]['update']['content'] = [{**completed, 'oldText': ''}]
    completed['oldText'] = completed['newText']
    stdout = '\n'.join(map(json.dumps, events))
    (root / 'stdout.log').write_text(stdout)
    runtime._parse_output(worker, stdout, '', info)
    files = json.loads((root / 'native-image-scope.json').read_text())['output_files']
    assert len(files) == 1
    assert files[0]['root_path'] == 'drawing.svg'
    assert files[0]['bytes'] == len(source.read_bytes())
    assert files[0]['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize('mode', ['foreign-path', 'non-tool-event'])
def test_only_consistent_native_diff_identity_can_publish(tmp_path, mode):
    runtime, worker, info, root, stdout, _ = fixture(tmp_path)
    events = list(runtime._events(stdout))
    if mode == 'foreign-path':
        events[1]['update']['_meta']['x.ai/tool']['input']['path'] = '/synthetic/foreign.svg'
    else:
        events[2]['update']['sessionUpdate'] = 'agent_message_chunk'
    stdout = '\n'.join(map(json.dumps, events))
    (root / 'stdout.log').write_text(stdout)
    runtime._parse_output(worker, stdout, '', info)
    assert not json.loads((root / 'native-image-scope.json').read_text()).get('output_files')


@pytest.mark.parametrize('old,kind', [(None, 'write'), ('original file contents', 'edit')])
def test_acp_v1_new_file_null_and_complete_edit_content(tmp_path, old, kind):
    runtime, worker, info, root, stdout, _ = fixture(tmp_path, kind=kind)
    events = list(runtime._events(stdout))
    events[2]['update']['content'][0]['oldText'] = old
    stdout = '\n'.join(map(json.dumps, events))
    (root / 'stdout.log').write_text(stdout)
    runtime._parse_output(worker, stdout, '', info)
    assert len(json.loads((root / 'native-image-scope.json').read_text())['output_files']) == 1


@pytest.mark.parametrize('mode', ['changed-bytes', 'fragment-diff'])
def test_unavailable_expected_native_bytes_keep_existing_rejection_contract(tmp_path, mode):
    runtime, worker, info, root, stdout, _ = fixture(tmp_path, changed=mode == 'changed-bytes')
    if mode == 'fragment-diff':
        events = list(runtime._events(stdout))
        events[2]['update']['content'][0]['newText'] = 'synthetic'
        stdout = '\n'.join(map(json.dumps, events))
        (root / 'stdout.log').write_text(stdout)
    runtime._parse_output(worker, stdout, '', info)
    run = {'run_id': 'run-test', 'worker_id': 'worker-test', 'active_attempt_id': 'attempt-test'}
    assert runtime.provider_native_output_file_rejections(worker, run) == [
        {'name': 'drawing.svg', 'code': 'unreadable'}]
    assert runtime.provider_native_image_output(worker, run, 'Created the file.')[1] == []


@pytest.mark.parametrize('selection', ['automatic', 'explicit', 'both'])
@pytest.mark.parametrize('internal_path', ['work-log.md', '.private/notes.md'])
def test_operational_native_edit_is_not_a_failed_selected_attachment(
    tmp_path, caplog, selection, internal_path,
):
    runtime, worker, info, root, stdout, drawing = fixture(tmp_path)
    internal = Path(worker['workspace_dir']) / internal_path
    internal.parent.mkdir(parents=True, exist_ok=True)
    internal.write_text('Internal execution note.')
    events = list(runtime._events(stdout))
    if selection in {'automatic', 'both'}:
        call = json.loads(json.dumps(events[1]))
        call['update']['toolCallId'] = 'call-internal'
        call['update']['_meta']['x.ai/tool']['input']['path'] = str(internal)
        done = json.loads(json.dumps(events[2]))
        done['update']['toolCallId'] = 'call-internal'
        done['update']['content'] = [{'type': 'diff', 'path': str(internal),
                                     'oldText': '', 'newText': internal.read_text()}]
        events[-1:-1] = [call, done]
    if selection in {'explicit', 'both'}:
        events[-1]['output'] += f'\n[Requested file]({internal_path})'
    stdout = '\n'.join(map(json.dumps, events))
    (root / 'stdout.log').write_text(stdout)
    with caplog.at_level('INFO'):
        runtime._parse_output(worker, stdout, '', info)
    scope = json.loads((root / 'native-image-scope.json').read_text())
    assert [item['root_path'] for item in scope['output_files']] == ['drawing.svg']
    expected = [] if selection == 'automatic' else [
        {'name': internal.name, 'code': 'not_deliverable'}]
    assert scope.get('rejected_output_files', []) == expected
    run = {'run_id': 'run-test', 'worker_id': 'worker-test', 'active_attempt_id': 'attempt-test'}
    assert runtime.provider_native_output_file_rejections(worker, run) == expected
    if selection in {'automatic', 'both'}:
        omitted = [r for r in caplog.records if getattr(r, 'selection_source', '') == 'verified_native_edit']
        assert len(omitted) == 1 and omitted[0].candidate_count == 1
        assert str(internal) not in omitted[0].getMessage()
