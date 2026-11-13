import json
import os

import pytest
from workers_projects_runtime import process_log as logs


def event(text, kind="agent_message", **fields):
    return json.dumps(
        {"type": "item.completed", "item": {"type": kind, "text": text, **fields}}
    )


def log_home(tmp_path, text, run="old-run", stream="stdout"):
    home = tmp_path / "home"
    target = home / ".glasshive-runs" / run / (stream + ".log")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return home, target


def test_entire_retained_history_paging_download_and_redaction_before_page(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_API_KEY", "known-service-credential")
    records = [
        event("EARLIEST <script>alert(1)</script> " + "x" * 200),
        event(
            'password="page-boundary-credential with spaces"; https://example.test/?tempauth=private-signed-value&x=1'
        ),
        event("known-service-credential /srv/private/home/secret.txt"),
        event(
            "",
            "command_execution",
            command="cat AGENTS.md",
            aggregated_output="private prompt here",
        ),
        event("HIDDEN CHAIN OF THOUGHT", "reasoning"),
        event(
            "",
            "file_change",
            changes=[{"path": "output/result.csv", "patch": "private patch"}],
        ),
    ]
    records += [
        event(f"Synthetic task step {i}: synthetic source output " + "z" * 300)
        for i in range(300)
    ]
    records += [event("LATEST record complete")]
    home, target = log_home(tmp_path, "\n".join(records))
    before = target.read_bytes()
    first = logs.read_process_log(home, "old-run", limit=257)
    result = [first["text"]]
    page = first
    while page["next_cursor"] is not None:
        page = logs.read_process_log(
            home,
            "old-run",
            cursor=page["next_cursor"],
            limit=257,
            version=first["version"],
        )
        result.append(page["text"])
    whole = logs.read_process_log(home, "old-run", download=True)
    text = "".join(result)
    assert text == whole["text"] and len(text) > 16000
    assert (
        "EARLIEST" in text
        and "LATEST" in text
        and "Synthetic task step 299" in text
    )
    for secret in (
        "page-boundary-credential",
        "with spaces",
        "private-signed-value",
        "known-service-credential",
        "/srv/",
        "private prompt here",
        "HIDDEN CHAIN",
        "private patch",
    ):
        assert secret not in text
    assert whole["record_count"] == len(records) and whole["omitted_records"] == 3
    assert target.read_bytes() == before


@pytest.mark.parametrize("secret", ["abc", "synthetic arbitrary named credential"])
def test_contract_credential_is_redacted_regardless_of_name_or_length(tmp_path, monkeypatch, secret):
    monkeypatch.setenv("WPR_CODEX_CLI_ROUTE_CONTRACT", json.dumps({"env_key": "UNUSUAL_AUTHORITY"}))
    monkeypatch.setenv("UNUSUAL_AUTHORITY", secret)
    home, _ = log_home(tmp_path, event("visible prefix " + secret + " visible suffix"))
    result = logs.read_process_log(home, "old-run", download=True)
    assert secret not in result["text"]
    assert "visible prefix" in result["text"] and "visible suffix" in result["text"]


@pytest.mark.parametrize("command", ['/bin/bash -lc "printenv"', "cat /proc/self/environ",
                                     "cat /proc/1234/environ", "bash -c 'export -p'",
                                     "declare -x", "(set)", "/usr/bin/env"])
def test_quoted_environment_reads_are_omitted_with_their_output(tmp_path, command):
    home, _ = log_home(tmp_path, event("", "command_execution", command=command,
                                     aggregated_output="unknown historical credential value"))
    result = logs.read_process_log(home, "old-run", download=True)
    assert result["omitted_records"] == 1
    assert "unknown historical credential value" not in result["text"]


@pytest.mark.parametrize("part", ["home", "runs", "run", "file"])
def test_symlinks_rejected_at_every_lookup(tmp_path, part):
    home, target = log_home(tmp_path, event("secret"))
    path = {
        "home": home,
        "runs": home / ".glasshive-runs",
        "run": target.parent,
        "file": target,
    }[part]
    moved = path.with_name(path.name + "-original")
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises((ValueError, OSError)):
        logs.read_process_log(home, "old-run")


def test_hardlinked_log_cannot_expose_another_file(tmp_path):
    home, target = log_home(tmp_path, event("synthetic private file"))
    os.link(target, tmp_path / "retained-copy")
    with pytest.raises(ValueError, match="without hard links"):
        logs.read_process_log(home, "old-run")


def test_attempt_paging_never_falls_back_or_reuses_another_attempt_version(tmp_path):
    home, legacy = log_home(tmp_path, event("legacy root evidence"))
    refs = ["attempt-sha256-" + letter * 64 for letter in ("a", "b")]
    for ref in refs:
        directory = legacy.parent / "attempts" / ref
        directory.mkdir(parents=True)
        (directory / "stdout.log").write_text(event("same synthetic attempt output"))
    first = logs.read_process_log(home, "old-run", attempt_ref=refs[0], limit=7)
    second = logs.read_process_log(home, "old-run", attempt_ref=refs[1], limit=7)
    assert first["version"] != second["version"]
    with pytest.raises(logs.ProcessLogChanged):
        logs.read_process_log(home, "old-run", attempt_ref=refs[1], version=first["version"])
    with pytest.raises(FileNotFoundError):
        logs.read_process_log(home, "old-run", attempt_ref="attempt-sha256-" + "c" * 64)
    assert "legacy root evidence" in logs.read_process_log(home, "old-run")["text"]


@pytest.mark.parametrize(
    "options",
    [
        {"run_id": "../outside"},
        {"stream": "../../auth.json"},
        {"cursor": -1},
        {"limit": 0},
        {"limit": 131073},
        {"cursor": 999999},
        {"download": True, "cursor": 1},
    ],
)
def test_invalid_selections_rejected(tmp_path, options):
    home, _ = log_home(tmp_path, event("public"))
    options = {"run_id": "old-run", **options}
    with pytest.raises(ValueError):
        logs.read_process_log(home, **options)


def test_changed_version_missing_oversized_nonregular_and_empty(tmp_path, monkeypatch):
    home, target = log_home(tmp_path, event("first"))
    first = logs.read_process_log(home, "old-run")
    target.write_text(event("new"))
    with pytest.raises(logs.ProcessLogChanged):
        logs.read_process_log(home, "old-run", version=first["version"])
    with pytest.raises(FileNotFoundError):
        logs.read_process_log(home, "missing")
    monkeypatch.setattr(logs, "MAX_LOG_BYTES", 4)
    with pytest.raises(OverflowError):
        logs.read_process_log(home, "old-run")
    monkeypatch.setattr(logs, "MAX_LOG_BYTES", 64 * 1024 * 1024)
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ValueError):
        logs.read_process_log(home, "old-run")
    _, stderr = log_home(tmp_path, "", stream="stderr")
    assert logs.read_process_log(home, "old-run", stream="stderr")["text"] == ""
    stderr.write_text("Authorization: Bearer synthetic-bearer\nOther useful error")
    data = logs.read_process_log(home, "old-run", stream="stderr")
    assert (
        "synthetic-bearer" not in data["text"] and "Other useful error" in data["text"]
    )


def test_private_prompt_markers_unsupported_records_omitted(tmp_path):
    text = "\n".join(
        [
            event("<system>private prompt</system>"),
            event("private user", "user_message"),
            '{"type":"unknown","instruction":"private directive"}',
            "unstructured secret",
        ]
    )
    home, _ = log_home(tmp_path, text)
    data = logs.read_process_log(home, "old-run")
    for secret in (
        "private prompt",
        "private user",
        "private directive",
        "unstructured secret",
    ):
        assert secret not in data["text"]


@pytest.mark.parametrize(
    "command",
    [
        '/bin/bash -lc "sed -n 181,360p .agents/skills/synthetic/SKILL.md"',
        '/bin/bash -lc "cat .agents/skills/synthetic/references/field-cell-map.md"',
        "/bin/bash -lc \"rg --files -g 'AGENTS.md' input\"",
    ],
)
def test_general_instruction_reads_are_not_promoted_as_public_tool_output(
    tmp_path, command
):
    home, _ = log_home(
        tmp_path,
        event(
            "",
            "command_execution",
            command=command,
            aggregated_output="private instruction text from the package",
        ),
    )
    data = logs.read_process_log(home, "old-run")
    assert "private instruction text" not in data["text"]
    assert data["omitted_records"] == 1
