"""Authority proof uses the selected native session owner, never a policy-copy home."""
import json
from pathlib import Path
import sqlite3

import pytest

from workers_projects_runtime import codex_authority as authority
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase
from test_codex_authority import runtime_fixture, developer, write_event


def bound_fixture(tmp_path):
    runtime, worker = runtime_fixture(tmp_path)
    account = tmp_path / "selected-account" / "codex"
    account.mkdir(parents=True, mode=0o700)
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle["provider_account"] = {"policy": "personal_required", "account_id": "acct_selected"}
    worker.update(bootstrap_bundle_json=json.dumps(bundle),
        _glasshive_provider_account_bound=True,
        _glasshive_provider_account_env={"CODEX_HOME": str(account)})
    runtime._write_session_key(worker["worker_id"], "selected-session")
    ledger = account / "rollout.jsonl"
    write_event(ledger, {"type": "session_meta", "payload": {"id": "selected-session"}})
    write_event(ledger, developer("Stable\n\nMemory"))
    with sqlite3.connect(account / "state_1.sqlite") as conn:
        conn.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
        conn.execute("INSERT INTO threads VALUES (?,?)", ("selected-session", str(ledger)))
    root = tmp_path / "run"
    root.mkdir()
    return runtime, worker, account, ledger, root


def test_selected_account_ledger_proves_unchanged_authority_without_control(tmp_path):
    runtime, worker, account, ledger, root = bound_fixture(tmp_path)
    command = ["codex", "exec", "resume", "selected-session", "-"]
    result = runtime._prepare_conversation_authority_command(worker, command, run_root=root, timeout_sec=20)
    assert result is command
    assert not (root / "native-authority-context.json").exists()
    saved = authority.read_json(runtime._session_meta_path(worker["worker_id"]))
    assert Path(saved["codex_authority"]["path"]) == ledger
    assert not runtime._host_codex_home(worker).exists()


def test_changed_authority_injects_in_exact_selected_account_home(tmp_path):
    runtime, worker, account, ledger, root = bound_fixture(tmp_path)
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle.update(developer_instructions="Stable\n\nCurrent memory", declared_developer_instruction_tail="Current memory")
    worker["bootstrap_bundle_json"] = json.dumps(bundle)
    command = ["codex", "exec", "resume", "selected-session", "-"]
    prepared = runtime._prepare_conversation_authority_command(worker, command, run_root=root, timeout_sec=20)
    context = authority.read_json(Path(prepared[-1]))
    assert context["codex_home"] == str(account)
    assert context["session"] == "selected-session"
    assert context["command"] == command
    assert context["declared_tail"] == "Current memory"
    assert not runtime._host_codex_home(worker).exists()


def test_remembered_authority_and_control_home_match_validated_command_owner(tmp_path):
    runtime, worker, account, ledger, root = bound_fixture(tmp_path)
    runtime._remember_native_session_key(worker, "selected-session")
    saved = authority.read_json(runtime._session_meta_path(worker["worker_id"]))
    assert Path(saved["codex_authority"]["path"]) == ledger
    assert runtime._native_codex_session_home(worker) == account


def test_explicit_unbound_account_cannot_claim_authority(tmp_path):
    runtime, worker, account, ledger, root = bound_fixture(tmp_path)
    worker.pop("_glasshive_provider_account_bound")
    command = ["codex", "exec", "resume", "selected-session", "-"]
    with pytest.raises(RuntimeErrorBase, match="cannot be reconciled"):
        runtime._prepare_conversation_authority_command(worker, command, run_root=root, timeout_sec=20)
    runtime._remember_native_session_key(worker, "selected-session")
    saved = authority.read_json(runtime._session_meta_path(worker["worker_id"]))
    assert "codex_authority" not in saved
    assert not (root / "native-authority-context.json").exists()


def test_selected_account_does_not_admit_outside_rollout_or_ambient_home(tmp_path, monkeypatch):
    runtime, worker, account, ledger, root = bound_fixture(tmp_path)
    foreign = tmp_path / "foreign-ledger.jsonl"
    write_event(foreign, developer("Stable\n\nMemory"))
    with sqlite3.connect(account / "state_1.sqlite") as conn:
        conn.execute("UPDATE threads SET rollout_path=?", (str(foreign),))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ambient-account"))
    command = ["codex", "exec", "resume", "selected-session", "-"]
    prepared = runtime._prepare_conversation_authority_command(worker, command, run_root=root, timeout_sec=20)
    context = authority.read_json(Path(prepared[-1]))
    assert context["codex_home"] == str(account)
    assert authority.rollout_path(account, "selected-session") is None
    assert "codex_authority" not in authority.read_json(runtime._session_meta_path(worker["worker_id"]))


def test_ordinary_native_worker_keeps_its_isolated_home(tmp_path, monkeypatch):
    runtime, worker = runtime_fixture(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ambient-account"))
    assert runtime._native_codex_session_home(worker) == runtime._host_codex_home(worker)


@pytest.mark.parametrize("bound", [False, True])
def test_provider_control_does_not_read_another_native_home(tmp_path, monkeypatch, bound):
    runtime, worker, account, ledger, root = bound_fixture(tmp_path)
    if not bound:
        worker.pop("_glasshive_provider_account_bound")
    monkeypatch.setattr(runtime, "_host_env", lambda _worker: {"HOME": str(tmp_path), "PATH": "/synthetic/bin"})
    calls = []
    def unavailable(_command, **options):
        calls.append(options["env"])
        raise OSError("Synthetic unavailable control process")
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", unavailable)
    assert runtime._query_codex_provider_control(worker, "selected-session") is None
    assert len(calls) == int(bound)
    if bound:
        assert calls[0]["CODEX_HOME"] == str(account)
