import json
import pytest
from workers_projects_runtime.profile_runtime import CodexCliRuntime, HostCodexCliRuntime, RuntimeErrorBase
from workers_projects_runtime.run_evidence import build_constraint_ledger, _constraint_compliance

@pytest.mark.parametrize('effort', ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
def test_native_worker_preserves_requested_supported_effort(tmp_path, effort):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker = {'worker_id': 'wrk_synthetic', 'profile': 'codex-cli', 'execution_mode': 'host',
              'bootstrap_bundle_json': json.dumps({'env': {'WPR_CODEX_CLI_REASONING_EFFORT': effort}})}
    assert runtime._codex_reasoning_effort_for_worker(worker) == effort
    assert worker['_effort_projection']['effective'] == effort

@pytest.mark.parametrize('runtime_type, effort', [(HostCodexCliRuntime, 'unsupported'), (CodexCliRuntime, 'ultra')])
def test_unsupported_effort_is_rejected_instead_of_substituted(tmp_path, monkeypatch, runtime_type, effort):
    monkeypatch.setenv('WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS', 'low,medium,high')
    runtime = runtime_type(base_dir=str(tmp_path))
    worker = {'worker_id': 'wrk_synthetic', 'profile': 'codex-cli', 'execution_mode': 'host',
              'bootstrap_bundle_json': json.dumps({'env': {'WPR_CODEX_CLI_REASONING_EFFORT': effort}})}
    with pytest.raises(RuntimeErrorBase, match='Unsupported .* effort'):
        runtime._codex_reasoning_effort_for_worker(worker)
    assert '_effort_projection' not in worker


def test_goal_is_retained_without_authoritative_keyword_extraction(tmp_path):
    goal = '  Read /Users/synthetic/notes.txt.\nUse only sources through May 2026.\n## Recent conversation context\nDo not exclude seed firms. Deliver 4 PDF reports.  '
    ledger = build_constraint_ledger(instruction=goal, worker={}, run_id='run-synthetic')
    assert ledger['original_request'] == goal
    assert all(value == [] for value in ledger['constraints'].values())
    assert all(value == [] for value in ledger['outputs'].values())
    assert ledger['coverage_expectations'] == []
    assert ledger['seed_entities_or_files'] == []
    (tmp_path / 'result.txt').write_text('Sources retrieved in June 2026.')
    assert _constraint_compliance(tmp_path, ledger)['status'] == 'not_applicable'


def test_typed_output_authority_survives_without_interpreting_required_prose():
    contract = {'version': 1, 'run_id': 'run-a',
                'source': {'source_event_id': 'event-a', 'source_revision': 1, 'surface': 'web'},
                'output': {'mode': 'replace', 'required': ['Keep a PDF example in the explanation.'], 'forbidden': [],
                           'formats': ['txt'], 'forbidden_formats': []}}
    ledger = build_constraint_ledger(instruction='New exact objective', run_id='run-a', worker={
        'bootstrap_bundle_json': json.dumps({'viventium_continuation_contract': contract,
            'viventium_constraint_source': {'version': 1, 'instruction': 'Original objective'}})})
    assert ledger['outputs']['required'] == contract['output']['required']
    assert ledger['outputs']['format_expectations'] == ['txt']
    assert ledger['original_output_source'] == 'Original objective'


def test_retained_typed_authority_preserves_owner_paths_but_redacts_secrets():
    contract = {'version': 1, 'run_id': 'run-a',
                'source': {'source_event_id': 'event-a', 'source_revision': 1, 'surface': 'web'},
                'output': {'mode': 'replace', 'required': ['  Read /Users/synthetic/report.txt password=synthetic-secret  '],
                           'forbidden': [], 'formats': ['txt'], 'forbidden_formats': []}}
    ledger = build_constraint_ledger(instruction='Read /Users/synthetic/report.txt password=synthetic-secret',
        run_id='run-a', worker={'bootstrap_bundle_json': json.dumps({
            'viventium_continuation_contract': contract,
            'viventium_constraint_source': {'version': 1, 'instruction': 'Original objective'}})})
    assert 'synthetic-secret' not in json.dumps(ledger)
    assert ledger['outputs']['required'] == ['  Read /Users/synthetic/report.txt password=[REDACTED]  ']
    assert ledger['typed_output_contract']['required'] == ledger['outputs']['required']


def test_model_owned_semantics_do_not_create_synthetic_diagnostic_warning(tmp_path):
    from workers_projects_runtime.run_evidence import summarize_run_evidence_result
    ledger = build_constraint_ledger(instruction='Deliver useful work.', worker={}, run_id='run-a')
    result = summarize_run_evidence_result({'constraint_compliance': _constraint_compliance(tmp_path, ledger)})
    assert not any(item['reason'] == 'constraint diagnostic warning' for item in result['warning_reasons'])
