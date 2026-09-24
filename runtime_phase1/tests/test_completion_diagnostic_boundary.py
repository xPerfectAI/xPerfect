from __future__ import annotations

import json
from pathlib import Path

import pytest

from workers_projects_runtime.profile_runtime import RuntimeErrorBase, _require_successful_run_evidence
from workers_projects_runtime.run_evidence import build_constraint_ledger, build_run_evidence, write_run_evidence


def evidence_for(workspace: Path, *, output='The revised note is saved and verified.', contract=None):
    run_id = 'run_correction'
    worker = {'worker_id': 'wrk_correction', 'profile': 'claude-code', 'execution_mode': 'host'}
    if contract is not None:
        worker['bootstrap_bundle_json'] = json.dumps({'viventium_continuation_contract': {
            'version': 1, 'run_id': run_id,
            'source': {'source_event_id': 'event_correction', 'source_revision': 1, 'surface': 'web'},
            'output': contract,
        }})
    ledger = None if contract is None else build_constraint_ledger(
        instruction='Apply the requested correction.', worker=worker, run_id=run_id,
    )
    evidence = build_run_evidence(
        worker=worker, run_id=run_id, runtime_name='claude-code', model='opus',
        command=['claude'], env={}, workspace_dir=workspace,
        stdout_text=output, stderr_text='', output_text=output, error_text='', exit_code=0,
        timeout_seconds=None, stop_reason='process_exit', constraint_ledger=ledger,
    )
    path = write_run_evidence(workspace, evidence, run_id)
    return evidence, str(path.relative_to(workspace))


def test_useful_native_result_survives_missing_internal_diagnostic(tmp_path):
    evidence, path = evidence_for(tmp_path)
    assert evidence['constraint_compliance']['status'] == 'not_available'
    assert evidence['evidence_result']['status'] == 'warn'
    assert evidence['evidence_result']['failure_reasons'] == []
    status, warning = _require_successful_run_evidence(
        workspace=tmp_path, evidence_path=path, constraint_ledger_path='', run_id='run_correction',
    )
    assert status == 'warn'
    assert warning


@pytest.mark.parametrize('change', ['foreign_run', 'wrong_schema', 'missing'])
def test_missing_diagnostic_does_not_remove_exact_evidence_identity(tmp_path, change):
    evidence, path = evidence_for(tmp_path)
    if change == 'missing':
        (tmp_path / path).unlink()
    else:
        evidence['run_id' if change == 'foreign_run' else 'schema'] = 'unrelated'
        (tmp_path / path).write_text(json.dumps(evidence))
    with pytest.raises(RuntimeErrorBase, match='identity is invalid'):
        _require_successful_run_evidence(
            workspace=tmp_path, evidence_path=path, constraint_ledger_path='', run_id='run_correction',
        )


def test_empty_result_still_fails(tmp_path):
    evidence, path = evidence_for(tmp_path, output='')
    assert evidence['evidence_result']['status'] == 'fail'
    with pytest.raises(RuntimeErrorBase, match='native result is empty'):
        _require_successful_run_evidence(
            workspace=tmp_path, evidence_path=path, constraint_ledger_path='', run_id='run_correction',
        )


def test_typed_output_requirement_still_fails_when_file_is_missing(tmp_path):
    contract = {'mode': 'replace', 'required': ['Produce the PDF.'], 'forbidden': [],
                'formats': ['pdf'], 'forbidden_formats': []}
    evidence, path = evidence_for(tmp_path, contract=contract)
    assert evidence['evidence_result']['status'] == 'fail'
    assert any(r['reason'] == 'typed output formats missing' for r in evidence['evidence_result']['failure_reasons'])
    with pytest.raises(RuntimeErrorBase, match='typed output formats missing'):
        _require_successful_run_evidence(
            workspace=tmp_path, evidence_path=path, constraint_ledger_path='', run_id='run_correction',
        )


def test_invalid_document_bytes_still_fail(tmp_path):
    (tmp_path / 'report.pdf').write_text('This is not a PDF document.')
    evidence, _ = evidence_for(tmp_path)
    assert evidence['evidence_result']['status'] == 'fail'
    assert any(r['reason'] == 'professional artifact validation failed' for r in evidence['evidence_result']['failure_reasons'])


def test_existing_but_corrupt_constraint_ledger_still_fails(tmp_path):
    _, path = evidence_for(tmp_path)
    (tmp_path / 'ledger.json').write_text('{}')
    with pytest.raises(RuntimeErrorBase, match='constraint ledger'):
        _require_successful_run_evidence(
            workspace=tmp_path, evidence_path=path, constraint_ledger_path='ledger.json', run_id='run_correction',
        )


@pytest.mark.parametrize("kind", ["exit", "error", "structured_provider"])
def test_provider_and_process_failures_are_not_demoted(tmp_path, kind):
    stdout = "The operation stopped."
    if kind == "structured_provider":
        stdout = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                             "api_error_status": 403, "result": "Authentication required"})
    evidence = build_run_evidence(
        worker={"worker_id": "wrk_boundary", "profile": "claude-code", "execution_mode": "host"},
        run_id="run_correction", runtime_name="claude-code", model="opus", command=["claude"],
        env={}, workspace_dir=tmp_path, stdout_text=stdout, stderr_text="", output_text=stdout,
        error_text="native error" if kind == "error" else "",
        exit_code=1 if kind == "exit" else 0, timeout_seconds=None, stop_reason="process_exit",
        constraint_ledger=None,
    )
    assert evidence["evidence_result"]["status"] == "fail"
    path = write_run_evidence(tmp_path, evidence, "run_correction")
    with pytest.raises(RuntimeErrorBase):
        _require_successful_run_evidence(workspace=tmp_path, evidence_path=str(path.relative_to(tmp_path)),
                                         constraint_ledger_path="", run_id="run_correction")


def test_declared_diagnostic_deletion_is_not_treated_as_generation_unavailable(tmp_path):
    _, path = evidence_for(tmp_path)
    with pytest.raises(RuntimeErrorBase, match="constraint ledger was not readable"):
        _require_successful_run_evidence(workspace=tmp_path, evidence_path=path,
                                         constraint_ledger_path="deleted-ledger.json", run_id="run_correction")
