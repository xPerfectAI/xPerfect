from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import workers_projects_runtime.api as api_module
import workers_projects_runtime.local_qa_service_ack as ack_module
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService


_QA_ENV_KEYS = (
    "VIVENTIUM_LOCAL_QA_CASE_ID",
    "VIVENTIUM_LOCAL_QA_SESSION_REF",
)


@pytest.fixture(autouse=True)
def _isolate_local_qa_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _QA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _disable_service_background_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        WorkersProjectsService, "_replay_startup_recovery", lambda _self: None
    )
    monkeypatch.setattr(WorkersProjectsService, "_callback_retry_loop", lambda _self: None)
    monkeypatch.setattr(WorkersProjectsService, "_scheduler_loop", lambda _self: None)
    monkeypatch.setattr(
        WorkersProjectsService, "_host_lease_heartbeat_loop", lambda _self: None
    )


def _write_helper(tmp_path: Path, body: str) -> Path:
    helper = tmp_path / "qa-ack-helper"
    helper.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    helper.chmod(0o700)
    return helper


def _activate(monkeypatch: pytest.MonkeyPatch, helper: Path) -> None:
    monkeypatch.setenv("VIVENTIUM_LOCAL_QA_CASE_ID", "PWK-UC-016")
    monkeypatch.setenv("VIVENTIUM_LOCAL_QA_SESSION_REF", "qa_synthetic_session")
    monkeypatch.setattr(ack_module, "_HELPER_PATH", helper)


@pytest.mark.parametrize(
    "present_keys",
    [(), ("VIVENTIUM_LOCAL_QA_CASE_ID",), ("VIVENTIUM_LOCAL_QA_SESSION_REF",)],
)
def test_ack_is_inactive_unless_case_and_session_are_both_set(
    present_keys: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in present_keys:
        monkeypatch.setenv(key, "synthetic")

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("inactive local QA must not launch a helper")

    monkeypatch.setattr(ack_module.subprocess, "run", forbidden_run)

    assert ack_module.acknowledge_local_qa_service("glasshive-runtime") is False


@pytest.mark.parametrize(
    "unsafe_kind",
    ["relative", "symlink", "world_writable", "not_executable", "directory"],
)
def test_ack_rejects_unsafe_helper_without_launching_it(
    unsafe_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    helper = _write_helper(tmp_path, "raise SystemExit(0)")
    configured: Path | str = helper
    if unsafe_kind == "relative":
        configured = Path("qa-ack-helper")
    elif unsafe_kind == "symlink":
        configured = tmp_path / "qa-ack-link"
        configured.symlink_to(helper)
    elif unsafe_kind == "world_writable":
        helper.chmod(0o722)
    elif unsafe_kind == "not_executable":
        helper.chmod(0o600)
    elif unsafe_kind == "directory":
        configured = tmp_path / "qa-ack-directory"
        configured.mkdir(mode=0o700)

    monkeypatch.setenv("VIVENTIUM_LOCAL_QA_CASE_ID", "PWK-UC-016")
    monkeypatch.setenv("VIVENTIUM_LOCAL_QA_SESSION_REF", "qa_synthetic_session")
    monkeypatch.setattr(ack_module, "_HELPER_PATH", Path(configured))
    monkeypatch.setattr(
        ack_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe helper must not launch")
        ),
    )

    with caplog.at_level(logging.WARNING, logger=ack_module.__name__):
        assert ack_module.acknowledge_local_qa_service("glasshive-runtime") is False

    assert [record.getMessage() for record in caplog.records] == [
        "local_qa_service_ack_failed"
    ]


def test_ack_invokes_exact_command_with_inherited_environment_and_suppressed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    capture_path = tmp_path / "capture.json"
    helper = _write_helper(
        tmp_path,
        "\n".join(
            (
                "import json, os, pathlib, sys",
                "pathlib.Path(os.environ['SYNTHETIC_ACK_CAPTURE']).write_text(",
                "    json.dumps({'argv': sys.argv[1:], "
                "'inherited': os.environ.get('SYNTHETIC_PARENT_VALUE')}),",
                "    encoding='utf-8',",
                ")",
                "print('child-private-stdout')",
                "print('child-private-stderr', file=sys.stderr)",
            )
        ),
    )
    _activate(monkeypatch, helper)
    monkeypatch.setenv("SYNTHETIC_ACK_CAPTURE", str(capture_path))
    monkeypatch.setenv("SYNTHETIC_PARENT_VALUE", "inherited")

    assert ack_module.acknowledge_local_qa_service("glasshive-runtime") is True

    captured = json.loads(capture_path.read_text(encoding="utf-8"))
    assert captured == {
        "argv": [
            "acknowledge",
            "--service-id",
            "glasshive-runtime",
            "--pid",
            str(os.getpid()),
            "--executable",
            sys.executable,
        ],
        "inherited": "inherited",
    }
    assert capfd.readouterr() == ("", "")


def test_nonzero_helper_logs_only_safe_marker_and_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    helper = _write_helper(
        tmp_path,
        "import sys\nprint('private-child-output')\nraise SystemExit(7)",
    )
    _activate(monkeypatch, helper)

    with caplog.at_level(logging.WARNING, logger=ack_module.__name__):
        assert ack_module.acknowledge_local_qa_service("glasshive-runtime") is False

    assert [record.getMessage() for record in caplog.records] == [
        "local_qa_service_ack_failed"
    ]
    assert str(helper) not in caplog.text
    assert "private-child-output" not in caplog.text


@pytest.mark.parametrize(
    "failure",
    (
        subprocess.TimeoutExpired(cmd="synthetic", timeout=5),
        OSError("synthetic private child failure"),
    ),
)
def test_timeout_or_launch_error_logs_only_safe_marker_and_does_not_raise(
    failure: Exception,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    helper = _write_helper(tmp_path, "raise SystemExit(0)")
    _activate(monkeypatch, helper)

    def fail_run(*_args, **kwargs):
        assert kwargs["timeout"] == 5
        assert "env" not in kwargs
        raise failure

    monkeypatch.setattr(ack_module.subprocess, "run", fail_run)
    with caplog.at_level(logging.WARNING, logger=ack_module.__name__):
        assert ack_module.acknowledge_local_qa_service("glasshive-runtime") is False

    assert [record.getMessage() for record in caplog.records] == [
        "local_qa_service_ack_failed"
    ]
    assert "synthetic private child failure" not in caplog.text


def test_glasshive_ack_runs_during_lifespan_after_service_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acknowledgements: list[str] = []
    app = None

    def acknowledge(service_id: str) -> bool:
        assert app is not None
        assert app.state.store is not None
        assert app.state.service is not None
        acknowledgements.append(service_id)
        return True

    monkeypatch.setattr(
        api_module, "acknowledge_local_qa_service", acknowledge, raising=False
    )
    app = api_module.create_app(
        db_path=str(tmp_path / "runtime.sqlite3"),
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )

    assert acknowledgements == []
    with TestClient(app) as client:
        assert acknowledgements == ["glasshive-runtime"]
        assert client.get("/health").status_code == 200


def test_glasshive_remains_available_when_acknowledgement_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    helper = _write_helper(tmp_path, "raise SystemExit(9)")
    _activate(monkeypatch, helper)
    app = api_module.create_app(
        db_path=str(tmp_path / "runtime.sqlite3"),
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )

    with caplog.at_level(logging.WARNING, logger=ack_module.__name__):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    assert [record.getMessage() for record in caplog.records] == [
        "local_qa_service_ack_failed"
    ]


def test_shallow_standalone_install_has_no_optional_parent_import_requirement():
    from pathlib import Path
    from workers_projects_runtime import local_qa_service_ack
    source = Path(local_qa_service_ack.__file__).read_text()
    namespace = {"__file__": "/runtime/package/local_qa_service_ack.py", "__name__": "standalone_ack_probe"}
    exec(compile(source, namespace["__file__"], "exec"), namespace)
    assert namespace["_HELPER_PATH"] is None
