from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Sequence

from .auth_gateway import AuthGatewayError, HumanAuthGateway


MAX_STDIN_BYTES = 64 * 1024
_IDENTITY_FIELDS = (
    "subject",
    "email",
    "display_name",
    "role",
    "login_email",
    "password",
)


def _stdin_payload() -> dict[str, object]:
    raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise AuthGatewayError("Provisioning input is too large")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AuthGatewayError("Provisioning input must be one JSON object") from exc
    if not isinstance(payload, dict):
        raise AuthGatewayError("Provisioning input must be one JSON object")
    for name in _IDENTITY_FIELDS:
        if name in payload and not isinstance(payload[name], str):
            raise AuthGatewayError(f"Provisioning field {name} must be a string")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m glass_drive_ui.auth_admin",
        description="Admin-only xPerfect identity provisioning",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preapprove = commands.add_parser(
        "preapprove-oidc",
        help="Preapprove one exact OIDC subject while public enrollment remains closed",
    )
    preapprove.add_argument(
        "--stdin-json",
        action="store_true",
        required=True,
        help="Read subject, email, display_name, and role from standard input",
    )
    local_owner = commands.add_parser("provision-local-owner", help="Provision or rotate the fixed local package owner")
    local_owner.add_argument("--stdin-json", action="store_true", required=True)
    for name, help_text in (
        (
            "set-local-password",
            "Attach or rotate a local password for one exact preapproved OIDC subject",
        ),
        ("unlock-local-password", "Clear durable local-password lockout state"),
        ("disable-local-password", "Disable local-password login for one subject"),
        ("enable-local-password", "Re-enable local-password login for one subject"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--stdin-json",
            action="store_true",
            required=True,
            help="Read the exact subject and any credential fields from standard input",
        )
    commands.add_parser(
        "revoke-local-sessions",
        help="Revoke every local-password browser session before flag-off or rollback",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "revoke-local-sessions":
            gateway = HumanAuthGateway.from_env()
            gateway.revoke_local_sessions()
            print(json.dumps({"status": "local_sessions_revoked"}))
            return 0
        payload = _stdin_payload()
        gateway = HumanAuthGateway.from_env()
        subject = str(payload.get("subject") or "")
        if args.command == "provision-local-owner":
            principal = gateway.provision_local_owner(password=str(payload.get("password") or ""))
            status = "local_owner_provisioned"
        elif args.command == "preapprove-oidc":
            principal = gateway.preapprove_oidc_principal(
                subject=subject,
                email=str(payload.get("email") or ""),
                display_name=str(payload.get("display_name") or ""),
                role=str(payload.get("role") or "member"),
            )
            status = "preapproved"
        elif args.command == "set-local-password":
            principal = gateway.provision_local_password(
                subject=subject,
                login_email=str(payload.get("login_email") or ""),
                password=str(payload.get("password") or ""),
            )
            status = "local_password_set"
        elif args.command == "unlock-local-password":
            principal = gateway.unlock_local_password(subject=subject)
            status = "local_password_unlocked"
        elif args.command in {"disable-local-password", "enable-local-password"}:
            principal = gateway.set_local_password_disabled(
                subject=subject,
                disabled=args.command == "disable-local-password",
            )
            status = (
                "local_password_disabled"
                if args.command == "disable-local-password"
                else "local_password_enabled"
            )
        else:
            raise AuthGatewayError("Unsupported admin command")
    except (AuthGatewayError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"Provisioning failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": status, "user_id": principal["user_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
