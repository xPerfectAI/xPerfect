from __future__ import annotations

import io
import json
import sqlite3

import pytest

import glass_drive_ui.auth_admin as auth_admin
from glass_drive_ui.auth_gateway import HumanAuthGateway


def test_preapprove_oidc_cli_reads_private_identity_from_stdin_and_is_idempotent(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "false")
    payload = {
        "subject": "stable-object-id",
        "email": "member@example.invalid",
        "display_name": "Example Member",
        "role": "member",
    }

    for _ in range(2):
        monkeypatch.setattr(auth_admin.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 0

    output = capsys.readouterr().out
    assert "member@example.invalid" not in output
    assert "stable-object-id" not in output
    gateway = HumanAuthGateway.from_env()
    principals = gateway.list_principals()
    assert len(principals) == 1
    assert principals[0]["email"] == "member@example.invalid"


def test_preapprove_oidc_cli_rejects_malformed_input_without_echoing_it(
    monkeypatch,
    capsys,
):
    private_input = '{"subject":"private-subject"'
    monkeypatch.setattr(auth_admin.sys, "stdin", io.StringIO(private_input))

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-subject" not in captured.err
    assert "one JSON object" in captured.err


def test_local_password_cli_rejects_malformed_unicode_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    gateway = HumanAuthGateway.from_env()
    gateway.preapprove_oidc_principal(subject="stable-object-id", role="member")
    payload = (
        '{"subject":"stable-object-id",'
        '"login_email":"broken\\ud800@example.invalid",'
        '"password":"synthetic password value"}'
    )
    monkeypatch.setattr(auth_admin.sys, "stdin", io.StringIO(payload))

    assert auth_admin.main(["set-local-password", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "valid email" in captured.err


@pytest.mark.parametrize(
    "command,payload",
    [
        ("preapprove-oidc", {"subject": "broken\ud800", "role": "member"}),
        (
            "set-local-password",
            {
                "subject": "broken\ud800",
                "login_email": "valid@example.invalid",
                "password": "synthetic password value",
            },
        ),
        ("unlock-local-password", {"subject": "broken\ud800"}),
        ("disable-local-password", {"subject": "broken\ud800"}),
        ("enable-local-password", {"subject": "broken\ud800"}),
    ],
)
def test_subject_admin_commands_reject_non_utf8_subject_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
    command,
    payload,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setattr(auth_admin.sys, "stdin", io.StringIO(json.dumps(payload)))

    assert auth_admin.main([command, "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "stable subject" in captured.err


def test_preapprove_cli_rejects_non_utf8_display_name_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    payload = {
        "subject": "stable-object-id",
        "display_name": "broken\ud800",
        "role": "member",
    }
    monkeypatch.setattr(auth_admin.sys, "stdin", io.StringIO(json.dumps(payload)))

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "valid display name" in captured.err


def test_preapprove_oidc_cli_does_not_silently_reenable_disabled_account(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    gateway = HumanAuthGateway.from_env()
    principal = gateway.preapprove_oidc_principal(subject="stable-object-id", role="member")
    gateway.set_principal_disabled(principal_id=principal["user_id"], disabled=True)
    monkeypatch.setattr(
        auth_admin.sys,
        "stdin",
        io.StringIO(json.dumps({"subject": "stable-object-id", "role": "member"})),
    )

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Account is disabled" in captured.err
    assert gateway.list_principals()[0]["disabled"] is True


def test_preapprove_oidc_cli_reports_corrupt_state_without_a_traceback(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    state_path.write_text("not sqlite", encoding="utf-8")
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setattr(
        auth_admin.sys,
        "stdin",
        io.StringIO(json.dumps({"subject": "stable-object-id", "role": "member"})),
    )

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Provisioning failed" in captured.err
    assert "Traceback" not in captured.err
    assert "stable-object-id" not in captured.err


def test_local_password_cli_uses_stdin_and_outputs_only_the_opaque_principal(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    monkeypatch.setenv(
        "GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY",
        "synthetic-throttle-key-for-admin-tests",
    )
    gateway = HumanAuthGateway.from_env()
    principal = gateway.preapprove_oidc_principal(subject="local-cli-subject", role="member")
    private_payload = {
        "subject": "local-cli-subject",
        "login_email": "local-cli@example.invalid",
        "password": "private synthetic passphrase",
    }
    monkeypatch.setattr(auth_admin.sys, "stdin", io.StringIO(json.dumps(private_payload)))

    assert auth_admin.main(["set-local-password", "--stdin-json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "private synthetic passphrase" not in captured.out
    assert "local-cli@example.invalid" not in captured.out
    assert "local-cli-subject" not in captured.out
    assert principal["user_id"] in captured.out
    with sqlite3.connect(state_path) as connection:
        password_phc = connection.execute(
            "SELECT password_phc FROM auth_local_credentials"
        ).fetchone()[0]
    assert password_phc.startswith("$argon2id$")
    assert "private synthetic passphrase" not in password_phc


def test_local_password_cli_disables_unlocks_and_revokes_without_identity_output(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    monkeypatch.setenv(
        "GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY",
        "synthetic-throttle-key-for-admin-tests",
    )
    gateway = HumanAuthGateway.from_env()
    gateway.preapprove_oidc_principal(subject="local-control-subject", role="member")
    gateway.provision_local_password(
        subject="local-control-subject",
        login_email="local-control@example.invalid",
        password="control synthetic passphrase",
    )
    session = gateway.authenticate_local_password(
        login_email="local-control@example.invalid",
        password="control synthetic passphrase",
        source="192.0.2.60",
    )

    for command in ("disable-local-password", "enable-local-password", "unlock-local-password"):
        monkeypatch.setattr(
            auth_admin.sys,
            "stdin",
            io.StringIO(json.dumps({"subject": "local-control-subject"})),
        )
        assert auth_admin.main([command, "--stdin-json"]) == 0
    assert auth_admin.main(["revoke-local-sessions"]) == 0

    captured = capsys.readouterr()
    assert "local-control-subject" not in captured.out
    assert "local-control@example.invalid" not in captured.out
    assert gateway.resolve_session(session["token"]) is None


def test_preapprove_oidc_cli_rejects_non_string_identity_fields_before_storage(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setattr(
        auth_admin.sys,
        "stdin",
        io.StringIO(json.dumps({"subject": ["not", "a", "subject"]})),
    )

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "field subject must be a string" in captured.err
    assert not state_path.exists()


def test_preapprove_oidc_cli_bounds_state_permission_errors(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        auth_admin.sys,
        "stdin",
        io.StringIO(json.dumps({"subject": "stable-object-id", "role": "member"})),
    )
    monkeypatch.setattr(
        auth_admin.HumanAuthGateway,
        "from_env",
        classmethod(lambda cls: (_ for _ in ()).throw(PermissionError("state unavailable"))),
    )

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Provisioning failed" in captured.err
    assert "Traceback" not in captured.err
    assert "stable-object-id" not in captured.err
