from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from workers_projects_runtime import codex_authority as owner
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime


def developer(text):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer',
            'content': [{'type': 'input_text', 'text': text}]}}


def write_event(path, event):
    with path.open('a') as handle:
        handle.write(json.dumps(event) + '\n')


def fixture(tmp_path, text='Stable A\n\nMemory A\nFact guard\nFeeling A'):
    home = tmp_path / '.codex'
    home.mkdir()
    path = home / 'rollout.jsonl'
    write_event(path, {'type': 'session_meta', 'payload': {'id': 'session-a'}})
    write_event(path, developer(text))
    with sqlite3.connect(home / 'state_1.sqlite') as connection:
        connection.execute('CREATE TABLE threads(id TEXT, rollout_path TEXT)')
        connection.execute('INSERT INTO threads VALUES (?,?)', ('session-a', str(path)))
    tail = 'Memory A\nFact guard\nFeeling A'
    current = owner.units(text, tail if text.endswith(tail) else '')
    return home, path, current


def test_exact_declared_tail_preserves_memory_guard_and_feeling():
    current = owner.units('Stable\n\nMemory\nFact guard\nFeeling', 'Memory\nFact guard\nFeeling')
    assert current == {'stable': 'Stable', 'dynamic': 'Memory\nFact guard\nFeeling'}
    with pytest.raises(owner.AuthorityUnconfirmed, match='not_suffix'):
        owner.units('Stable\nTail\nUnexpected', 'Tail')


def test_initial_authority_is_reconciled_without_control_or_duplicate(tmp_path):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    assert owner.missing_units(current, state) == []
    assert state['offset'] == path.stat().st_size
    assert owner.reconcile(home, 'session-a', current, state) == state


@pytest.mark.parametrize('unit', ['stable', 'dynamic'])
def test_changed_unit_only_is_missing_and_exact_new_frame_recovers(tmp_path, unit):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    current[unit] += ' current'
    state = owner.reconcile(home, 'session-a', current, state)
    assert owner.missing_units(current, state) == (['stable', 'dynamic'] if unit == 'stable' else ['dynamic'])
    write_event(path, developer(current[unit]))
    if unit == 'stable':
        write_event(path, developer(current['dynamic']))
    assert owner.missing_units(current, owner.reconcile(home, 'session-a', current, state)) == []


def test_lookalike_user_content_cannot_ack_authority(tmp_path):
    home, path, current = fixture(tmp_path, 'Old')
    current = {'stable': 'Current', 'dynamic': 'Memory'}
    write_event(path, {'type': 'response_item', 'payload': {
        'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Current Memory'}]}})
    assert owner.missing_units(current, owner.reconcile(home, 'session-a', current, {})) == ['stable', 'dynamic']


@pytest.mark.parametrize('replacement', [False, True])
def test_compaction_invalidates_prior_delivery_and_checks_replacement(tmp_path, replacement):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    items = [developer('Stable A\n\nMemory A\nFact guard\nFeeling A')['payload']] if replacement else []
    write_event(path, {'type': 'compacted', 'payload': {'replacement_history': items}})
    state = owner.reconcile(home, 'session-a', current, state)
    assert owner.missing_units(current, state) == ([] if replacement else ['stable', 'dynamic'])


def test_replaced_or_truncated_ledger_does_not_retain_ack(tmp_path):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    path.unlink()
    write_event(path, {'type': 'session_meta', 'payload': {'id': 'session-a'}})
    assert owner.missing_units(current, owner.reconcile(home, 'session-a', current, state)) == ['stable', 'dynamic']


def test_partial_native_record_is_not_acknowledged(tmp_path):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    current['dynamic'] += ' current'
    record = json.dumps(developer(current['dynamic']))
    with path.open('a') as handle:
        handle.write(record)
    state = owner.reconcile(home, 'session-a', current, state)
    assert owner.missing_units(current, state) == ['dynamic']
    with path.open('a') as handle:
        handle.write('\n')
    assert owner.missing_units(current, owner.reconcile(home, 'session-a', current, state)) == []


def test_oversized_new_history_is_bounded_and_fail_closed(tmp_path, monkeypatch):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    monkeypatch.setattr(owner, 'MAX_LEDGER_BYTES', 256)
    write_event(path, {'type': 'event_msg', 'payload': {'text': 'x' * 2000}})
    assert owner.missing_units(current, owner.reconcile(home, 'session-a', current, state)) == ['stable', 'dynamic']


def test_foreign_native_thread_or_outside_home_cannot_supply_proof(tmp_path):
    home, path, current = fixture(tmp_path)
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'foreign'}}) + '\n')
    with pytest.raises(owner.AuthorityUnconfirmed, match='session_mismatch'):
        owner.reconcile(home, 'session-a', current, {})
    foreign = tmp_path / 'foreign.jsonl'
    foreign.write_text(json.dumps(developer('Current')) + '\n')
    assert owner.rollout_path(home, 'no-session', foreign) is None


@pytest.mark.parametrize('field,value', [('session_key', 'other'), ('context_epoch', 'other'), ('runtime', 'claude-code')])
def test_manifest_binding_never_crosses_session_epoch_or_runtime(field, value):
    manifest = {'session_key': 'session-a', 'context_epoch': 'epoch-a', 'runtime': 'codex-cli'}
    manifest[field] = value
    with pytest.raises(owner.AuthorityUnconfirmed, match='session_changed'):
        owner.session_state(manifest, 'session-a', 'epoch-a')


def runtime_fixture(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / 'runtime'))
    worker = {'worker_id': 'wrk_qa', 'profile': 'codex-cli', 'execution_mode': 'host',
              'trusted_run_lane': 'conversation',
              'bootstrap_bundle_json': json.dumps({'run_mode': 'conversation',
                 'developer_instructions': 'Stable\n\nMemory', 'declared_developer_instruction_tail': 'Memory', 'env': {}})}
    return runtime, worker


def test_fresh_and_unchanged_turn_keep_direct_exec(tmp_path):
    runtime, worker = runtime_fixture(tmp_path)
    command = ['codex', 'exec', '-']
    root = tmp_path / 'run'
    root.mkdir()
    assert runtime._prepare_conversation_authority_command(worker, command, run_root=root, timeout_sec=20) is command
    runtime._write_session_key(worker['worker_id'], 'session-a')
    home = runtime._host_codex_home(worker)
    home.mkdir(parents=True, exist_ok=True)
    ledger = home / 'rollout.jsonl'
    write_event(ledger, developer('Stable\n\nMemory'))
    with sqlite3.connect(home / 'state_1.sqlite') as connection:
        connection.execute('CREATE TABLE threads(id TEXT, rollout_path TEXT)')
        connection.execute('INSERT INTO threads VALUES (?,?)', ('session-a', str(ledger)))
    assert runtime._prepare_conversation_authority_command(worker, command, run_root=root, timeout_sec=20) is command
    assert not (root / 'native-authority-context.json').exists()


def test_fresh_runtime_placeholder_keeps_direct_exec_without_resume_preflight(tmp_path):
    runtime, worker = runtime_fixture(tmp_path)
    worker['workspace_dir'] = str(tmp_path / 'workspace')
    info = runtime._host_runtime_info(worker)
    assert info.session_key == f"codex-worker:{worker['worker_id']}"
    assert runtime._read_provider_session_key(worker) == info.session_key
    command = ['codex', 'exec', '-']
    root = tmp_path / 'run'
    root.mkdir()

    prepared = runtime._prepare_conversation_authority_command(
        worker, command, run_root=root, timeout_sec=20,
    )

    assert prepared is command
    assert not (root / 'native-authority-context.json').exists()
    assert not (root / 'native-authority-preflight.py').exists()
    assert 'codex_authority' not in owner.read_json(runtime._session_meta_path(worker['worker_id']))


def test_changed_resumed_authority_wraps_inside_supervised_child(tmp_path):
    runtime, worker = runtime_fixture(tmp_path)
    runtime._write_session_key(worker['worker_id'], 'session-a')
    root = tmp_path / 'run'
    root.mkdir()
    command = ['codex', 'exec', 'resume', 'session-a', '-']
    prepared = runtime._prepare_conversation_authority_command(worker, command, run_root=root, timeout_sec=20)
    assert prepared[0] == sys.executable
    context = owner.read_json(Path(prepared[-1]))
    assert context['command'] == command
    assert context['session'] == 'session-a'
    assert context['declared_tail'] == 'Memory'
    assert Path(prepared[1]).parent == root
    assert Path(prepared[1]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('available', [False, True])
def test_host_command_and_authority_use_same_available_native_resume(tmp_path, available):
    runtime, worker = runtime_fixture(tmp_path)
    worker['workspace_root'] = str(tmp_path / 'workspace')
    worker['model'] = 'gpt-6.1-sol'
    bundle = json.loads(worker['bootstrap_bundle_json'])
    bundle['provider_model'] = 'gpt-6.1-sol'
    bundle['env']['WPR_CODEX_CLI_REASONING_EFFORT'] = 'high'
    worker['bootstrap_bundle_json'] = json.dumps(bundle)
    runtime._write_session_key(worker['worker_id'], 'session-a')
    home = runtime._host_codex_home(worker)
    home.mkdir(parents=True, exist_ok=True)
    ledger = home / 'rollout.jsonl'
    if available:
        write_event(ledger, developer('Stable\n\nMemory'))
    with sqlite3.connect(home / 'state_1.sqlite') as connection:
        connection.execute('CREATE TABLE threads(id TEXT, rollout_path TEXT)')
        connection.execute('INSERT INTO threads VALUES (?,?)', ('session-a', str(ledger)))
    (home / 'config.toml').write_text('developer_instructions = "Stable\\n\\nMemory"\n')
    command, _ = runtime._build_command(worker, 'Use the current authority.', runtime._host_runtime_info(worker))
    assert ('resume' in command) is available
    assert 'model_reasoning_effort="high"' in command
    assert ('model="gpt-6.1-sol"' if available else 'gpt-6.1-sol') in command
    root = tmp_path / 'run'
    root.mkdir()
    prepared = runtime._prepare_conversation_authority_command(
        worker, command, run_root=root, timeout_sec=20,
    )
    assert prepared is command
    assert not (root / 'native-authority-context.json').exists()
    assert not (root / 'native-authority-preflight.py').exists()
    state = owner.read_json(runtime._session_meta_path(worker['worker_id']))
    assert ('codex_authority' in state) is available


def test_same_native_manifest_preserves_ack_and_different_epoch_clears(tmp_path):
    runtime, worker = runtime_fixture(tmp_path)
    path = runtime._session_meta_path(worker['worker_id'])
    runtime._write_session_key(worker['worker_id'], 'session-a', context_epoch='epoch-a')
    value = owner.read_json(path)
    owner.write_json(path, {**value, 'codex_authority': {'delivered': {'stable': 'exact'}}})
    runtime._write_session_key(worker['worker_id'], 'session-a')
    assert owner.read_json(path)['codex_authority']['delivered']['stable'] == 'exact'
    runtime._write_session_key(worker['worker_id'], 'session-a', context_epoch='epoch-b')
    assert 'codex_authority' not in owner.read_json(path)


def fake_cli(tmp_path):
    script = tmp_path / 'fake-codex'
    script.write_text('''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
if sys.argv[1]!='app-server':
 Path(os.environ['EXEC_RECEIPT']).write_text(json.dumps({'pid':os.getpid(),'argv':sys.argv[1:],'stdin':sys.stdin.read()}));sys.exit(0)
for line in sys.stdin:
 request=json.loads(line);method=request.get('method');params=request.get('params',{})
 with open(os.environ['RPC_RECEIPT'],'a') as log:log.write(json.dumps(request)+'\\n')
 if method=='initialized':continue
 if method=='thread/inject_items':
  if os.environ.get('MODE')=='timeout':time.sleep(30)
  if os.environ.get('MODE')!='no-persist':
   with open(os.environ['LEDGER'],'a') as out:
    for item in params['items']:out.write(json.dumps({'type':'response_item','payload':item})+'\\n')
  if os.environ.get('MODE')=='uncertain':sys.exit(1)
 if os.environ.get('MODE')=='reject' and method=='thread/inject_items':response={'error':{'code':-1}}
 else:response={'result':{'thread':{'id':params.get('threadId'),**({'path':os.environ['RESUME_PATH']} if os.environ.get('RESUME_PATH') else {})}} if method=='thread/resume' else {}}
 print(json.dumps({'id':request['id'],**response}),flush=True)
''')
    script.chmod(0o700)
    return script


def subprocess_context(tmp_path, monkeypatch, mode='healthy'):
    home, path, _ = fixture(tmp_path)
    manifest = tmp_path / 'session.json'
    owner.write_json(manifest, {'session_key': 'session-a', 'context_epoch': '', 'runtime': 'codex-cli'})
    cli = fake_cli(tmp_path)
    context = {'authority': 'Stable A\n\nMemory B\nFact guard\nFeeling B',
       'declared_tail': 'Memory B\nFact guard\nFeeling B', 'session': 'session-a', 'epoch': '',
       'manifest_path': str(manifest), 'codex_home': str(home), 'binary': str(cli),
       'command': [str(cli), 'exec', 'resume', 'session-a', '-'],
       'deadline_monotonic': time.monotonic() + 10,
       'receipt_path': str(tmp_path / 'authority.json'), 'failure_path': str(tmp_path / 'failure.json')}
    owner.write_json(Path(context['receipt_path']), {'materialized': True})
    for name, value in {'LEDGER': path, 'RPC_RECEIPT': tmp_path / 'rpc.jsonl',
                        'EXEC_RECEIPT': tmp_path / 'exec.json', 'MODE': mode}.items():
        monkeypatch.setenv(name, str(value))
    return context, path, manifest


def test_preflight_injects_only_changed_exact_tail_then_exec_same_pid_and_stdin(tmp_path, monkeypatch):
    context, path, manifest = subprocess_context(tmp_path, monkeypatch)
    # This resumed turn starts from the prior exact native fresh-frame acknowledgement.
    initial = owner.units('Stable A\n\nMemory A\nFact guard\nFeeling A', 'Memory A\nFact guard\nFeeling A')
    state = owner.reconcile(Path(context['codex_home']), 'session-a', initial, {})
    owner.save_state(manifest, 'session-a', '', state)
    context_path = tmp_path / 'context.json'
    owner.write_json(context_path, context)
    process = subprocess.Popen([sys.executable, owner.__file__, str(context_path)], stdin=subprocess.PIPE)
    process.communicate('Synthetic current goal'.encode(), timeout=5)
    assert process.returncode == 0
    executed = owner.read_json(tmp_path / 'exec.json')
    assert executed == {'pid': process.pid, 'argv': ['exec', 'resume', 'session-a', '-'], 'stdin': 'Synthetic current goal'}
    calls = [json.loads(line) for line in (tmp_path / 'rpc.jsonl').read_text().splitlines()]
    assert calls[0]['params']['capabilities'] == {'experimentalApi': False}
    resume = next(call for call in calls if call.get('method') == 'thread/resume')
    assert resume['params'] == {'threadId': 'session-a', 'excludeTurns': True}
    injected = next(call for call in calls if call.get('method') == 'thread/inject_items')['params']['items']
    assert injected == [developer(context['declared_tail'])['payload']]
    receipt = owner.read_json(Path(context['receipt_path']))
    assert receipt['injected_units'] == ['dynamic']
    assert receipt['delivered_dynamic_sha256'] == owner.digests(owner.units(context['authority'], context['declared_tail']))['dynamic']
    assert receipt['authority_preflight_ms'] > 0


@pytest.mark.parametrize('mode', ['reject', 'no-persist', 'uncertain'])
def test_unconfirmed_delivery_never_executes_or_claims_ack(tmp_path, monkeypatch, mode):
    context, path, manifest = subprocess_context(tmp_path, monkeypatch, mode)
    context_path = tmp_path / 'context.json'
    owner.write_json(context_path, context)
    result = subprocess.run([sys.executable, owner.__file__, str(context_path)], capture_output=True, timeout=5)
    assert result.returncode == 78
    assert not (tmp_path / 'exec.json').exists()
    assert 'codex_authority' not in owner.read_json(manifest)
    assert owner.read_json(Path(context['failure_path']))['code'] == 'authority_update_unconfirmed'
    if mode == 'uncertain':
        # Exact persisted native bytes recover the lost ACK without repeating injection.
        monkeypatch.setenv('MODE', 'healthy')
        receipt = owner.synchronize(context)
        assert receipt['injected_units'] == []
        assert len([json.loads(line) for line in (tmp_path / 'rpc.jsonl').read_text().splitlines()
                    if json.loads(line).get('method') == 'thread/inject_items']) == 1


def test_cancel_supervised_group_kills_preflight_and_never_executes(tmp_path, monkeypatch):
    context, path, manifest = subprocess_context(tmp_path, monkeypatch, 'timeout')
    context_path = tmp_path / 'context.json'
    owner.write_json(context_path, context)
    process = subprocess.Popen([sys.executable, owner.__file__, str(context_path)], start_new_session=True)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if (tmp_path / 'rpc.jsonl').exists() and 'thread/inject_items' in (tmp_path / 'rpc.jsonl').read_text():
            break
        time.sleep(0.01)
    else:
        process.kill()
        pytest.fail('preflight did not start')
    os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=3)
    assert process.returncode < 0
    assert not (tmp_path / 'exec.json').exists()
    assert 'codex_authority' not in owner.read_json(manifest)


def test_preflight_deadline_cannot_continue_to_exec(tmp_path, monkeypatch):
    context, path, manifest = subprocess_context(tmp_path, monkeypatch, 'timeout')
    context['deadline_monotonic'] = time.monotonic() + 0.15
    with pytest.raises(owner.AuthorityUnconfirmed, match='timeout'):
        owner.synchronize(context)
    assert 'codex_authority' not in owner.read_json(manifest)
    assert not (tmp_path / 'exec.json').exists()


def test_reconciliation_uses_checkpoint_without_database_or_whole_history(tmp_path, monkeypatch):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    monkeypatch.setattr(owner.sqlite3, 'connect', lambda *a, **kw: pytest.fail('cached path should not query SQLite'))
    assert owner.reconcile(home, 'session-a', current, state) == state
    write_event(path, {'type': 'event_msg', 'payload': {'type': 'task_complete'}})
    next_state = owner.reconcile(home, 'session-a', current, state)
    assert next_state['offset'] == path.stat().st_size
    assert next_state['delivered'] == state['delivered']


def test_manifest_ack_write_rejects_replaced_session(tmp_path):
    path = tmp_path / 'session.json'
    owner.write_json(path, {'session_key': 'replacement', 'context_epoch': '', 'runtime': 'codex-cli'})
    before = path.read_bytes()
    with pytest.raises(owner.AuthorityUnconfirmed, match='session_changed'):
        owner.save_state(path, 'old', '', {'delivered': {'stable': 'old'}})
    assert path.read_bytes() == before


def test_fresh_remember_ack_is_native_persisted_not_configured(tmp_path):
    runtime, worker = runtime_fixture(tmp_path)
    worker['_active_run_id'] = 'run-current'
    runtime._remember_native_session_key(worker, 'session-a')
    manifest = runtime._session_meta_path(worker['worker_id'])
    assert 'codex_authority' not in owner.read_json(manifest)
    home = runtime._host_codex_home(worker)
    home.mkdir(parents=True, exist_ok=True)
    ledger = home / 'rollout.jsonl'
    write_event(ledger, developer('Stable\n\nMemory'))
    with sqlite3.connect(home / 'state_1.sqlite') as connection:
        connection.execute('CREATE TABLE threads(id TEXT, rollout_path TEXT)')
        connection.execute('INSERT INTO threads VALUES (?,?)', ('session-a', str(ledger)))
    runtime._remember_native_session_key(worker, 'session-a')
    assert owner.read_json(manifest)['codex_authority']['delivered'] == owner.digests(runtime._authority_units(worker))


def test_next_attempt_clears_previous_preflight_failure_before_native_exec(tmp_path):
    runtime, worker = runtime_fixture(tmp_path)
    root = tmp_path / 'run'
    root.mkdir()
    owner.write_json(root / 'native-authority-failure.json', {'code': 'authority_update_unconfirmed'})
    command = ['codex', 'exec', '-']
    assert runtime._prepare_conversation_authority_command(worker, command, run_root=root, timeout_sec=20) is command
    assert not (root / 'native-authority-failure.json').exists()


@pytest.mark.parametrize('unit', ['stable', 'dynamic'])
def test_older_authority_with_obsolete_suffix_does_not_ack_current_shorter_unit(tmp_path, unit):
    home, path, _ = fixture(tmp_path)
    current = {'stable': 'Current stable', 'dynamic': 'Current dynamic'}
    path.write_text('')
    write_event(path, developer(current[unit] + '\nObsolete instruction that was removed.'))
    state = owner.reconcile(home, 'session-a', current, {})
    assert unit in owner.missing_units(current, state)


def test_native_fresh_frame_exact_first_content_part_accepts_composer_not_prefix(tmp_path):
    home, path, current = fixture(tmp_path)
    full = 'Stable A\n\nMemory A\nFact guard\nFeeling A'
    item = developer(full)
    item['payload']['internal_chat_message_metadata_passthrough'] = {'content_item_kinds': [
        'generic.developer_instructions', 'host_skills.instructions', 'permissions.instructions',
        'collaboration_mode.instructions']}
    item['payload']['content'].extend([
        {'type': 'input_text', 'text': '<skills_instructions>Synthetic native skills.</skills_instructions>'},
        {'type': 'input_text', 'text': '<permissions instructions>Synthetic native permissions.</permissions instructions>'},
        {'type': 'input_text', 'text': '<collaboration_mode>Synthetic native mode.</collaboration_mode>'},
    ])
    path.write_text('')
    write_event(path, item)
    state = owner.reconcile(home, 'session-a', current, {})
    assert owner.missing_units(current, state) == []
    shorter = {'stable': 'Stable', 'dynamic': current['dynamic']}
    assert owner.missing_units(shorter, owner.reconcile(home, 'session-a', shorter, {})) == ['stable', 'dynamic']


@pytest.mark.parametrize('recovery', ['missing_manifest', 'invalid_checkpoint', 'compaction', 'mixed_injected'])
def test_later_obsolete_application_item_invalidates_historical_exact_authority(tmp_path, recovery):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    obsolete = developer(current['stable'] + '\nObsolete application instruction.')
    obsolete['payload']['internal_chat_message_metadata_passthrough'] = {'content_item_kinds': ['unknown']}
    if recovery == 'mixed_injected':
        obsolete = developer('Stable A\n\nMemory A\nFact guard\nFeeling A')
        obsolete['payload']['content'].append({'type': 'input_text', 'text': 'Obsolete application instruction.'})
        obsolete['payload']['internal_chat_message_metadata_passthrough'] = {'content_item_kinds': ['unknown', 'unknown']}
        write_event(path, obsolete)
        state = {}
    elif recovery == 'compaction':
        write_event(path, {'type': 'compacted', 'payload': {'replacement_history': [
            developer('Stable A\n\nMemory A\nFact guard\nFeeling A')['payload'], obsolete['payload']]}})
    else:
        write_event(path, obsolete)
        state = {} if recovery == 'missing_manifest' else {**state, 'offset': path.stat().st_size + 1}
    recovered = owner.reconcile(home, 'session-a', current, state)
    assert owner.missing_units(current, recovered) == ['stable', 'dynamic']


def test_same_run_exact_stable_and_dynamic_items_ack_without_losing_sibling(tmp_path):
    home, path, current = fixture(tmp_path, 'Old application authority.')
    current = {'stable': 'Current stable', 'dynamic': 'Current memory, guard and Feeling.'}
    for text in current.values():
        item = developer(text)
        item['payload']['internal_chat_message_metadata_passthrough'] = {'content_item_kinds': ['unknown']}
        write_event(path, item)
    assert owner.missing_units(current, owner.reconcile(home, 'session-a', current, {})) == []


def test_typed_native_context_updates_do_not_invalidate_unchanged_application_units(tmp_path):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    for kind in ['host_skills.instructions', 'permissions.instructions', 'collaboration_mode.instructions',
                 'multi_agent.role_instructions', 'multi_agent.mode_instructions']:
        item = developer('Native context owned by the installed SDK.')
        item['payload']['internal_chat_message_metadata_passthrough'] = {'content_item_kinds': [kind]}
        write_event(path, item)
    recovered = owner.reconcile(home, 'session-a', current, state)
    assert owner.missing_units(current, recovered) == []
    assert recovered['delivered'] == state['delivered']


def test_stable_only_change_reinjects_tail_last_and_recovers_lost_ack(tmp_path):
    home, path, current = fixture(tmp_path)
    state = owner.reconcile(home, 'session-a', current, {})
    current['stable'] = 'Current stable authority.'
    state = owner.reconcile(home, 'session-a', current, state)
    assert owner.missing_units(current, state) == ['stable', 'dynamic']
    write_event(path, developer(current['stable']))
    # Stable persisted before an interrupted inject must not claim tail-last delivery.
    recovered = owner.reconcile(home, 'session-a', current, {})
    assert owner.missing_units(current, recovered) == ['dynamic']
    write_event(path, developer(current['dynamic']))
    recovered = owner.reconcile(home, 'session-a', current, recovered)
    assert owner.missing_units(current, recovered) == []
    assert recovered['last_unit'] == 'dynamic'
    assert owner.reconcile(home, 'session-a', current, recovered) == recovered


def test_native_resume_rollout_path_recovers_without_internal_database(tmp_path, monkeypatch):
    context, path, manifest = subprocess_context(tmp_path, monkeypatch)
    (Path(context['codex_home']) / 'state_1.sqlite').unlink()
    monkeypatch.setenv('RESUME_PATH', str(path))
    receipt = owner.synchronize(context)
    assert receipt['injected_units'] == ['stable', 'dynamic']
    assert owner.read_json(manifest)['codex_authority']['path'] == str(path)


def test_foreign_native_resume_rollout_path_never_supplies_delivery(tmp_path, monkeypatch):
    context, path, manifest = subprocess_context(tmp_path, monkeypatch)
    monkeypatch.setenv('RESUME_PATH', str(tmp_path / 'foreign.jsonl'))
    (tmp_path / 'foreign.jsonl').write_text(path.read_text())
    with pytest.raises(owner.AuthorityUnconfirmed, match='rollout_path'):
        owner.synchronize(context)
    assert 'codex_authority' not in owner.read_json(manifest)
    calls = [json.loads(line) for line in (tmp_path / 'rpc.jsonl').read_text().splitlines()]
    assert not any(call.get('method') == 'thread/inject_items' for call in calls)
