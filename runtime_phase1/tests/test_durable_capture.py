from __future__ import annotations

import json
from pathlib import Path

import pytest

from workers_projects_runtime.durable_capture import (
    DurableCaptureError,
    DurableSecretScrubber,
    scrub_durable_text_artifacts,
)
from workers_projects_runtime.profile_runtime import _scrub_provider_owned_artifacts


OPERATION_TOKEN_FIELD = "_viventium_operation_token"
OPERATION_TOKEN = (
    "eyJhdWQiOiJnbGFzc2hpdmUtbmF0aXZlLW9yY2hlc3RyYXRpb24tb3BlcmF0aW9uIiw"
    "ib3duZXJfaWQiOiJvd25lci1wcml2YXRlIiwiY29udmVyc2F0aW9uX2lkIjoiY29udi"
    "1wcml2YXRlIiwibWVzc2FnZV9pZCI6Im1zZy1wcml2YXRlIn0"
)
BROKER_BEARER = "synthetic-invocation-only-broker-bearer"


def _prepared_event() -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "status": "prepared",
                                    OPERATION_TOKEN_FIELD: OPERATION_TOKEN,
                                }
                            ),
                        }
                    ]
                },
            },
        }
    )


def _commit_event() -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "arguments": {
                    "workRef": "work-safe",
                    "action": "stop",
                    OPERATION_TOKEN_FIELD: OPERATION_TOKEN,
                },
                "result": {"status": "ok", "workRef": "work-safe"},
            },
        }
    )


def test_structural_scrubber_keeps_jsonl_valid_while_removing_prepare_and_commit_secrets():
    scrubber = DurableSecretScrubber(exact_values=(BROKER_BEARER,))
    unrelated_base64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="

    prepared = scrubber.scrub_text(_prepared_event() + "\n")
    committed = scrubber.scrub_text(_commit_event() + "\n")
    echoed = scrubber.scrub_text(
        f"provider diagnostic {OPERATION_TOKEN} {BROKER_BEARER} {unrelated_base64}\n"
    )
    serialized = prepared + committed + echoed

    assert OPERATION_TOKEN not in serialized
    assert BROKER_BEARER not in serialized
    assert "owner-private" not in serialized
    assert "conv-private" not in serialized
    assert "msg-private" not in serialized
    assert unrelated_base64 in serialized
    parsed_prepare, parsed_commit = [
        json.loads(line) for line in (prepared + committed).splitlines()
    ]
    prepared_text = parsed_prepare["item"]["result"]["content"][0]["text"]
    assert json.loads(prepared_text)[OPERATION_TOKEN_FIELD] == "[REDACTED_OPERATION_TOKEN]"
    assert (
        parsed_commit["item"]["arguments"][OPERATION_TOKEN_FIELD]
        == "[REDACTED_OPERATION_TOKEN]"
    )
    assert parsed_commit["item"]["result"] == {
        "status": "ok",
        "workRef": "work-safe",
    }


def test_two_pass_artifact_scrub_removes_exact_tokens_from_state_and_workspace(tmp_path: Path):
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    harness_workspace = workspace / "glasshive-run"
    state.mkdir()
    harness_workspace.mkdir(parents=True)
    rollout = state / "rollout.jsonl"
    rollout.write_text(_prepared_event() + "\n" + _commit_event() + "\n")
    diagnostic = state / "stdout.log"
    diagnostic.write_text(f"later echo: {OPERATION_TOKEN}\nAuthorization: Bearer {BROKER_BEARER}\n")
    harness_artifact = harness_workspace / "provider-events.jsonl"
    harness_artifact.write_text(f"accidental token copy: {OPERATION_TOKEN}\n")
    user_artifact = workspace / "notes.txt"
    user_artifact.write_text(f"user-owned content is not a runtime transcript: {OPERATION_TOKEN}\n")
    binary = workspace / "untouched.bin"
    binary.write_bytes(b"\x00" + OPERATION_TOKEN.encode())
    outside = tmp_path / "outside.txt"
    outside.write_text(OPERATION_TOKEN)
    (state / "outside-link.txt").symlink_to(outside)

    scrubber = DurableSecretScrubber(exact_values=(BROKER_BEARER,))
    changed = scrub_durable_text_artifacts((state, harness_workspace), scrubber=scrubber)

    assert {path.name for path in changed} == {
        "provider-events.jsonl",
        "rollout.jsonl",
        "stdout.log",
    }
    for root in (state, harness_workspace):
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix != ".bin":
                text = path.read_text(errors="ignore")
                assert OPERATION_TOKEN not in text, path
                assert BROKER_BEARER not in text, path
                assert "owner-private" not in text, path
                assert "conv-private" not in text, path
                assert "msg-private" not in text, path
    assert OPERATION_TOKEN.encode() in binary.read_bytes()
    assert OPERATION_TOKEN in user_artifact.read_text()
    assert outside.read_text() == OPERATION_TOKEN


def test_production_root_selection_excludes_user_workspace_deliverables(tmp_path: Path):
    state = tmp_path / "worker" / "state"
    home = tmp_path / "worker" / "home"
    run_root = home / ".glasshive-runs" / "run-1"
    workspace = tmp_path / "worker" / "workspace"
    harness_workspace = workspace / "glasshive-run"
    for directory in (state, run_root, workspace, harness_workspace):
        directory.mkdir(parents=True, exist_ok=True)

    state_transcript = state / "provider-session.json"
    state_transcript.write_text(_prepared_event())
    rollout = home / ".codex" / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(_commit_event())
    raw_stdout = run_root / "stdout.log"
    raw_stdout.write_text(f"provider echo: {OPERATION_TOKEN}\n")
    harness_events = harness_workspace / "provider-events.jsonl"
    harness_events.write_text(f"provider echo: {OPERATION_TOKEN}\n")
    user_deliverable = workspace / "customer-session-events.jsonl"
    user_deliverable.write_text(
        f"user-authored deliverable must remain byte-exact: {OPERATION_TOKEN}\n"
    )

    _scrub_provider_owned_artifacts(
        state_dir=state,
        home_dir=home,
        run_root=run_root,
        workspace=workspace,
        scrubber=DurableSecretScrubber(exact_values=(BROKER_BEARER,)),
    )

    for artifact in (state_transcript, rollout, raw_stdout, harness_events):
        assert OPERATION_TOKEN not in artifact.read_text(), artifact
    assert OPERATION_TOKEN in user_deliverable.read_text()


def test_artifact_rewrite_failure_fails_closed_instead_of_leaving_silent_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = tmp_path / "state"
    state.mkdir()
    transcript = state / "stdout.log"
    transcript.write_text(_prepared_event() + "\n")

    def fail_replace(_source, _destination):
        raise OSError("synthetic durable replacement failure")

    monkeypatch.setattr(
        "workers_projects_runtime.durable_capture.os.replace", fail_replace
    )

    with pytest.raises(OSError, match="synthetic durable replacement failure"):
        scrub_durable_text_artifacts(
            (state,),
            scrubber=DurableSecretScrubber(exact_values=(BROKER_BEARER,)),
        )
    assert OPERATION_TOKEN in transcript.read_text()


def test_oversized_harness_transcript_fails_closed_instead_of_skipping_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = tmp_path / "state"
    state.mkdir()
    transcript = state / "rollout.jsonl"
    transcript.write_text(_prepared_event())
    monkeypatch.setattr(
        "workers_projects_runtime.durable_capture._MAX_ARTIFACT_BYTES", 8
    )

    with pytest.raises(DurableCaptureError, match="safe scrub limit"):
        scrub_durable_text_artifacts(
            (state,),
            scrubber=DurableSecretScrubber(exact_values=(BROKER_BEARER,)),
        )
    assert OPERATION_TOKEN in transcript.read_text()


def test_tiny_reserved_field_value_cannot_rewrite_ordinary_transcript_text():
    scrubber = DurableSecretScrubber()
    malicious = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "arguments": {OPERATION_TOKEN_FIELD: "a"},
                "result": {"status": "safe"},
            },
        }
    )

    prepared = scrubber.scrub_text(malicious + "\n")
    ordinary = scrubber.scrub_text("a normal assistant answer remains intact\n")

    parsed = json.loads(prepared)
    assert (
        parsed["item"]["arguments"][OPERATION_TOKEN_FIELD]
        == "[REDACTED_OPERATION_TOKEN]"
    )
    assert ordinary == "a normal assistant answer remains intact\n"
