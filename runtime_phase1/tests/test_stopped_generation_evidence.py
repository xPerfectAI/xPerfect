"""A confirmed native stop records evidence without auditing workspace deliverables."""
import json

import pytest

from workers_projects_runtime import profile_runtime, run_evidence


SCANS = (
    "_artifact_inventory", "_visual_render_summary", "_text_artifact_payload",
    "check_content_hygiene", "_constraint_compliance", "_coverage_compliance",
    "_completion_compliance",
    "_failure_classification_summary",
)


def request(tmp_path, stop_reason):
    transcript = tmp_path / "stdout.jsonl"
    transcript.write_text("Exact native tool result\n")
    return dict(
        worker={"worker_id": "worker-synthetic", "profile": "codex-cli",
                "execution_mode": "host", "workspace_kind": "legacy",
                "_run_attempt_id": "attempt-synthetic"},
        run_id="run-synthetic", runtime_name="codex-cli", model="model-synthetic",
        command=["native-cli", "exec", "-"], env={}, workspace=tmp_path,
        stdout_text=transcript.read_text(), stderr_text="", output_text="",
        error_text="Native generation stopped", exit_code=-15, timeout_seconds=60,
        stop_reason=stop_reason, constraint_ledger=None,
        transcript_paths={"stdout": str(transcript)},
    )


@pytest.mark.parametrize("stop_reason", ["paused", "interrupted", "terminated"])
def test_confirmed_stop_preserves_evidence_without_workspace_audit(tmp_path, monkeypatch, stop_reason):
    def forbidden(*args, **kwargs):
        raise AssertionError("A stopped generation must not audit workspace files")

    for name in SCANS:
        monkeypatch.setattr(run_evidence, name, forbidden)
    media_calls = []
    media = {"observations": [{"run_id": "run-synthetic", "kind": "image"}], "omitted_count": 0}
    monkeypatch.setattr(profile_runtime, "capture_native_media",
                        lambda *args: media_calls.append(args) or media)
    relative = profile_runtime._write_evidence_for_run(
        **request(tmp_path, stop_reason), stopped_generation=True,
    )
    assert relative
    evidence = json.loads((tmp_path / relative).read_text())
    assert evidence["run_id"] == "run-synthetic"
    assert evidence["attempt_id"] == "attempt-synthetic"
    assert evidence["timeout"]["stop_reason"] == stop_reason
    assert evidence["transcript"]["metadata"]["stdout"]["exists"] is True
    assert evidence["transcript"]["stdout_tail"] == "Exact native tool result\n"
    assert evidence["native_media"] == media
    assert len(media_calls) == 1
    assert evidence["final_output"]["status"] == "failed"
    assert evidence["evidence_result"]["status"] == "fail"
    assert evidence["artifacts"] == {
        "status": "not_applicable", "basis": "stopped_generation",
        "count": 0, "items": [], "shared_workspace": True,
    }
    for name in ("visual_render_evidence", "content_hygiene", "constraint_compliance",
                 "coverage_compliance", "completion_compliance", "failure_classification"):
        assert evidence[name] == {"status": "not_applicable", "basis": "stopped_generation"}


def test_stopped_oversized_transcript_is_retained_without_preview_or_reclassification(tmp_path, monkeypatch):
    arguments = request(tmp_path, "interrupted")
    raw = "sensitive-prefix " * 300 + "\nBearer synthetic-secret-credential\n"
    path = tmp_path / "stdout.jsonl"
    path.write_text(raw)
    arguments["stdout_text"] = raw
    arguments["workspace_dir"] = arguments.pop("workspace")
    original = run_evidence._redact_text

    def bounded_redactor(value, **kwargs):
        assert value != raw, "Do not redact an oversized stopped log for a short preview"
        return original(value, **kwargs)

    monkeypatch.setattr(run_evidence, "_redact_text", bounded_redactor)
    evidence = run_evidence.build_run_evidence(**arguments, stopped_generation=True)
    assert evidence["transcript"]["stdout_tail"] == (
        "[Oversized stopped-generation preview omitted; exact transcript retained]"
    )
    assert evidence["transcript"]["metadata"]["stdout"]["truncated"] is True
    assert evidence["transcript"]["metadata"]["stdout"]["bytes"] == len(raw.encode())
    assert path.read_text() == raw


@pytest.mark.parametrize("stop_reason,exit_code", [("process_exit", 0), ("process_exit", 1), ("interrupted", -15)])
def test_result_evidence_keeps_audits_without_trusted_stopped_generation(tmp_path, monkeypatch, stop_reason, exit_code):
    # Neither a provider failure nor stop-looking text selects the bounded path.
    called = []
    for name in SCANS:
        monkeypatch.setattr(run_evidence, name,
                            lambda *args, name=name, **kwargs: called.append(name) or {})
    arguments = request(tmp_path, stop_reason)
    arguments["workspace_dir"] = arguments.pop("workspace")
    arguments["exit_code"] = exit_code
    evidence = run_evidence.build_run_evidence(**arguments)
    assert set(called) == set(SCANS)
    assert evidence["artifacts"].get("basis") != "stopped_generation"
