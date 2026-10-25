from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from xperfect_evidence.manifest import file_plan, source_event


PACKAGE = Path(__file__).resolve().parents[1]


def command(*arguments):
    return subprocess.run([sys.executable, *arguments], env={**os.environ, "PYTHONPATH": str(PACKAGE / "src")},
                          capture_output=True, text=True, timeout=10)


def test_shipped_manifests_match_synthetic_input_bytes():
    from xperfect_evidence.files import Root
    from xperfect_evidence.evaluation import case_manifest
    example = Root(PACKAGE / "examples")
    source = source_event(example.json("source-event.json"))
    inputs = Root(PACKAGE / "examples" / "inputs")
    for item in source["inputs"]:
        inputs.snapshot(item["relative_path"], expected=item)
    case_manifest(example.json("cases.json"))
    file_plan(example.json("package.json")["outputs"])


def test_prepare_check_and_stage_as_cli_user(tmp_path):
    private = tmp_path / "example"
    prepared = command(str(PACKAGE / "examples" / "prepare.py"), str(private))
    assert prepared.returncode == 0, prepared.stderr
    checked = command("-m", "xperfect_evidence", "check", "--kind", "source", "--manifest", str(private / "source-event.json"))
    assert checked.returncode == 0 and json.loads(checked.stdout)["valid"] is True
    staged = command("-m", "xperfect_evidence", "stage", "--manifest", str(private / "source-event.json"),
                     "--source", str(private / "source"), "--state", str(private / "state"))
    assert staged.returncode == 0 and json.loads(staged.stdout)["state"] == "staged"
    replay = command("-m", "xperfect_evidence", "stage", "--manifest", str(private / "source-event.json"),
                     "--source", str(private / "source"), "--state", str(private / "state"))
    assert replay.returncode == 0 and replay.stdout == staged.stdout


def test_cli_errors_do_not_export_private_paths(tmp_path):
    missing = tmp_path / "private-missing-input.json"
    result = command("-m", "xperfect_evidence", "check", "--kind", "source", "--manifest", str(missing))
    assert result.returncode == 2 and not result.stdout
    assert str(tmp_path) not in result.stderr and "blocked" in result.stderr


def test_validator_policy_cli_pins_installed_code():
    from xperfect_evidence.validators import validator_identity
    result = command("-m", "xperfect_evidence", "validator-policy")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {**validator_identity(), "require_source_review": True}
