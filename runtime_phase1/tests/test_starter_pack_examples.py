"""Calculation and failure contracts for the original starter examples.

Real model/tool/UI and domain-review acceptance are separate from these tests.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PACKS = Path(__file__).parents[1] / "src/workers_projects_runtime/starter_packs"
ROLES = (
    "clinical-biostatistician", "bioanalytical-scientist",
    "regulatory-affairs-specialist", "growth-marketer", "finance-director",
)


def run_example(role, output, *, data="data.csv"):
    pack = PACKS / role
    result = subprocess.run(
        [sys.executable, str(pack / "skill/scripts/analyze.py"),
         "--data", str(pack / "demo" / data),
         "--plan", str(pack / "demo/plan.json"), "--output-dir", str(output)],
        text=True, capture_output=True, timeout=15,
    )
    return result, json.loads(result.stdout)


@pytest.mark.parametrize("role", ROLES)
def test_starter_example_computes_review_work_without_changing_sources(role, tmp_path):
    pack = PACKS / role
    before = {p: p.read_bytes() for p in (pack / "demo").rglob("*") if p.is_file()}
    result, terminal = run_example(role, tmp_path / "results")
    assert result.returncode == 0, result.stderr
    assert terminal["status"] == "review_required"
    output = json.loads((tmp_path / "results/analysis.json").read_text())
    assert output["limitations"] and output["input_sha256"]
    assert all(p.read_bytes() == content for p, content in before.items())
    if role == "clinical-biostatistician":
        assert [(x["assigned"], x["observed"], x["missing"], x["responses"])
                for x in output["groups"]] == [(22, 20, 2, 8), (22, 20, 2, 13)]
        assert output["effect"]["risk_difference"] == pytest.approx(0.25)
        assert output["effect"]["ci95_low"] < 0 < output["effect"]["ci95_high"]
    elif role == "bioanalytical-scientist":
        assert output["slope"] == pytest.approx(0.02)
        assert output["intercept"] == pytest.approx(0.1)
        assert output["r_squared"] == pytest.approx(1)
        assert [x["sample_id"] for x in output["issues"]] == ["QC-REVIEW"]
        with (tmp_path / "results/assay-results.csv").open() as handle:
            rows = {x["sample_id"]: x for x in csv.DictReader(handle)}
        assert float(rows["SAMPLE-01"]["estimated_concentration"]) == pytest.approx(40)
    elif role == "regulatory-affairs-specialist":
        assert output["claims"] == 3
        assert [x["claim_id"] for x in output["issues"]] == ["E-02", "E-03"]
        assert output["submission_or_approval_performed"] is False
    elif role == "growth-marketer":
        assert {x["channel"]: x["cost_per_qualified_lead"] for x in output["channels"]} == {
            "Paid search": 40, "Webinar": 20, "Email": 6.25,
        }
    elif role == "finance-director":
        assert [x["operating_result"] for x in output["monthly_results"]] == [20000, 20000, -5000]
        assert output["cash_reconciliation"]["unexplained_difference"] == -1500
        assert output["cash_reconciliation"]["reconciled"] is False
        assert [x["closing_cash"] for x in output["cash_forecast"]] == [160000, 110000, 120000]


@pytest.mark.parametrize("role", ROLES)
def test_starter_example_refuses_bad_input_without_fake_analysis(role, tmp_path):
    output = tmp_path / "bad-results"
    result, terminal = run_example(role, output, data="bad-input.csv")
    assert result.returncode == 2
    assert terminal["status"] == "needs_input"
    assert terminal["analysis_complete"] is False
    assert not (output / "analysis.json").exists()
    assert json.loads((output / "input-issue.json").read_text())["status"] == "needs_input"


@pytest.mark.parametrize("role", ROLES)
def test_starter_example_preserves_existing_output(role, tmp_path):
    output = tmp_path / "retained"
    output.mkdir()
    retained = output / "important-work.txt"
    retained.write_text("Existing user work")
    result, terminal = run_example(role, output)
    assert result.returncode == 2
    assert terminal["analysis_complete"] is False
    assert retained.read_text() == "Existing user work"
    assert list(output.iterdir()) == [retained]


def finance_bridge():
    path = PACKS / "finance-director/curated/variance-analysis/references/variance_bridge.py"
    spec = importlib.util.spec_from_file_location("starter_finance_bridge", path)
    module = importlib.util.module_from_spec(spec)
    before = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


def test_curated_finance_bridge_reconciles_price_volume_and_mix_exactly():
    result = finance_bridge().bridge([50, 50], [30, 90], [10, 20], [12, 18])
    assert result["budget"] == 1500
    assert result["actual"] == 1980
    assert (result["volume"], result["mix"], result["price"]) == (300, 300, -120)
    assert result["variance"] == 480
    assert result["reconciled"] is True
    assert result["unexplained_residual"] == 0


def test_curated_finance_bridge_reports_break_instead_of_plugging_it():
    result = finance_bridge().reconcile(1500, [300, 250, -100], 1980)
    assert result["reconciled"] is False
    assert result["unexplained_residual"] == 30


def test_curated_finance_bridge_keeps_zero_denominators_undefined():
    method = finance_bridge()
    assert method.percentage(5, 0) is None
    result = method.bridge([0, 0], [1, 1], [10, 20], [12, 18])
    assert result["status"] == "needs_method"
    assert result["variance"] == 30
    assert "mix" not in result


def test_curated_finance_bridge_refuses_misaligned_products():
    with pytest.raises(ValueError, match="align"):
        finance_bridge().bridge([1], [1, 2], [2], [2])
