import asyncio
import base64
import grp
import hmac
import importlib.util
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import glass_drive_ui.server as server_module
import glass_drive_ui.signed_links as signed_links_module
import glass_drive_ui.internal_assertions as internal_assertions_module
from glass_drive_ui.auth_gateway import AuthGatewayError
from glass_drive_ui.server import create_app
from glass_drive_ui.signed_links import (
    SensitiveUrlLogFilter,
    create_signed_link_ref,
    install_sensitive_url_log_filter,
    redact_sensitive_url_text,
    resolve_signed_link_ref,
    revoke_signed_link_refs_for_worker,
)


def worker_cookie_name(worker_id: str) -> str:
    digest = sha256(str(worker_id).encode("utf-8")).hexdigest()[:24]
    return f"glasshive_gh_token_{digest}"


def test_ui_credential_bearing_sqlite_state_is_private(tmp_path, monkeypatch):
    link_path = tmp_path / "private-state" / "link-refs.sqlite3"
    watch_path = tmp_path / "private-state" / "watch-sessions.sqlite3"
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(link_path))
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(watch_path))

    link_connection = signed_links_module._link_ref_conn()
    watch_connection = server_module._watch_session_conn()
    try:
        link_connection.execute(
            "INSERT INTO signed_link_refs "
            "(ref_id, kind, token, target_url, expires_at, created_at) "
            "VALUES ('ghr_private_test', 'worker_view', 'synthetic', '/watch/test', 0, 1)"
        )
        link_connection.commit()
        watch_connection.execute(
            "INSERT INTO watch_sessions "
            "(tenant_id, owner_id, worker_id, started_at, expires_at, updated_at) "
            "VALUES ('tenant', 'owner', 'worker', 1, 2, 1)"
        )
        watch_connection.commit()
        signed_links_module._harden_sqlite_state_path(link_path)
        server_module._harden_watch_session_state(watch_path)
        assert os.stat(link_path.parent).st_mode & 0o077 == 0
        for state_path in (link_path, watch_path):
            for candidate in (
                state_path,
                Path(f"{state_path}-wal"),
                Path(f"{state_path}-shm"),
            ):
                if candidate.exists():
                    assert os.stat(candidate).st_mode & 0o077 == 0
    finally:
        link_connection.close()
        watch_connection.close()


def test_runtime_and_ui_share_only_the_explicit_group_link_ref_store(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared-link-refs"
    shared_dir.mkdir(mode=0o770)
    os.chown(shared_dir, -1, os.getegid())
    shared_dir.chmod(0o2770 if sys.platform.startswith("linux") else 0o770)
    shared_path = shared_dir / "link_refs.sqlite3"
    shared_path.touch(mode=0o660)
    os.chown(shared_path, -1, os.getegid())
    shared_path.chmod(0o660)
    shared_group = grp.getgrgid(os.getegid()).gr_name
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(shared_path))
    monkeypatch.setenv("GLASSHIVE_LINK_REF_SHARED_GROUP", shared_group)
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "shared-ref-secret")

    runtime_signed_links = load_runtime_signed_links_module()
    token = runtime_signed_links.sign_link_token(
        kind="worker_view",
        worker_id="wrk_shared",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = runtime_signed_links.create_signed_link_ref(
        token=token,
        target_url="/watch/wrk_shared",
    )
    record = resolve_signed_link_ref(ref_id)
    ui_token = signed_links_module.sign_link_token(
        kind="artifact_open",
        worker_id="wrk_shared",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="workspace/index.html",
    )
    ui_ref_id = create_signed_link_ref(token=ui_token)
    runtime_record = runtime_signed_links.resolve_signed_link_ref(ui_ref_id)

    assert record is not None
    assert record["token"] == token
    assert runtime_record is not None
    assert runtime_record["token"] == ui_token
    expected_directory_mode = 0o2770 if sys.platform.startswith("linux") else 0o770
    assert os.stat(shared_dir).st_mode & 0o7777 == expected_directory_mode
    for candidate in (shared_path, Path(f"{shared_path}-wal"), Path(f"{shared_path}-shm")):
        if candidate.exists():
            candidate_stat = os.stat(candidate)
            assert candidate_stat.st_gid == os.getegid()
            assert candidate_stat.st_mode & 0o777 == 0o660


def test_explicit_group_link_ref_store_fails_closed_when_world_accessible(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared-link-refs"
    shared_dir.mkdir(mode=0o777)
    os.chown(shared_dir, -1, os.getegid())
    shared_dir.chmod(0o2777 if sys.platform.startswith("linux") else 0o777)
    shared_path = shared_dir / "link_refs.sqlite3"
    shared_group = grp.getgrgid(os.getegid()).gr_name
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(shared_path))
    monkeypatch.setenv("GLASSHIVE_LINK_REF_SHARED_GROUP", shared_group)

    with pytest.raises(PermissionError, match="shared link reference directory"):
        signed_links_module._link_ref_conn()


@pytest.mark.parametrize("unsafe_kind", ["missing", "world_mode", "symlink", "wal_mode"])
def test_explicit_group_link_ref_store_rejects_unsafe_database_state(
    tmp_path, monkeypatch, unsafe_kind
):
    shared_dir = tmp_path / "shared-link-refs"
    shared_dir.mkdir(mode=0o770)
    os.chown(shared_dir, -1, os.getegid())
    shared_dir.chmod(0o2770 if sys.platform.startswith("linux") else 0o770)
    shared_path = shared_dir / "link_refs.sqlite3"
    if unsafe_kind == "symlink":
        target = tmp_path / "outside.sqlite3"
        target.touch(mode=0o660)
        shared_path.symlink_to(target)
    elif unsafe_kind != "missing":
        shared_path.touch(mode=0o660)
        os.chown(shared_path, -1, os.getegid())
        shared_path.chmod(0o666 if unsafe_kind == "world_mode" else 0o660)
        if unsafe_kind == "wal_mode":
            wal_path = Path(f"{shared_path}-wal")
            wal_path.touch(mode=0o644)
            os.chown(wal_path, -1, os.getegid())
            wal_path.chmod(0o644)
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(shared_path))
    monkeypatch.setenv(
        "GLASSHIVE_LINK_REF_SHARED_GROUP",
        grp.getgrgid(os.getegid()).gr_name,
    )

    with pytest.raises(PermissionError, match="shared link reference"):
        signed_links_module._link_ref_conn()


def test_explicit_group_link_ref_store_requires_process_group_membership(monkeypatch):
    class GroupRecord:
        gr_gid = 4567

    monkeypatch.setenv("GLASSHIVE_LINK_REF_SHARED_GROUP", "synthetic-shared")
    monkeypatch.setattr(signed_links_module.grp, "getgrnam", lambda _name: GroupRecord())
    monkeypatch.setattr(signed_links_module.os, "geteuid", lambda: 501)
    monkeypatch.setattr(signed_links_module.os, "getegid", lambda: 1234)
    monkeypatch.setattr(signed_links_module.os, "getgroups", lambda: [1234])

    with pytest.raises(PermissionError, match="not a member"):
        signed_links_module._shared_link_ref_group_gid()


def test_linux_shared_link_ref_directory_requires_setgid(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared-link-refs"
    shared_dir.mkdir(mode=0o770)
    os.chown(shared_dir, -1, os.getegid())
    shared_dir.chmod(0o770)
    shared_path = shared_dir / "link_refs.sqlite3"
    shared_path.touch(mode=0o660)
    os.chown(shared_path, -1, os.getegid())
    shared_path.chmod(0o660)
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(shared_path))
    monkeypatch.setenv(
        "GLASSHIVE_LINK_REF_SHARED_GROUP",
        grp.getgrgid(os.getegid()).gr_name,
    )
    monkeypatch.setattr(signed_links_module.sys, "platform", "linux")

    with pytest.raises(PermissionError, match="shared link reference directory"):
        signed_links_module._link_ref_conn()


@pytest.fixture(autouse=True)
def clear_glasshive_ui_env(monkeypatch, tmp_path):
    for name in (
        "WPR_API_TOKEN",
        "GLASSHIVE_DEFAULT_OWNER_ID",
        "GLASSHIVE_ENTERPRISE_MODE",
        "GLASSHIVE_PUBLIC_LINKS_ONLY",
        "WPR_ENTERPRISE_MODE",
        "GLASSHIVE_AUTH_MODE",
        "GLASSHIVE_ENTERPRISE_TENANT_ID",
        "WPR_ENTERPRISE_TENANT_ID",
        "GLASSHIVE_TRUST_INBOUND_IDENTITY",
        "GLASSHIVE_OWNER_IDENTITY_CLAIMS",
        "GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON",
        "GLASSHIVE_OWNER_IDENTITY_ALIASES_FILE",
        "GLASSHIVE_ALLOW_LOCAL_DEMO_OWNER",
        "GLASSHIVE_COOKIE_SECURE",
        "GLASSHIVE_SIGNED_LINK_SECRET",
        "GLASSHIVE_SIGNED_LINK_TTL_S",
        "GLASSHIVE_LINK_REF_STATE_PATH",
        "GLASSHIVE_LINK_REF_SHARED_GROUP",
        "GLASSHIVE_LINK_REF_TTL_SECONDS",
        "GLASSHIVE_WORKSPACE_LINK_AUTO_RESUME",
        "GLASSHIVE_HOST_WORKERS_ENABLED",
        "GLASSHIVE_RELEASE_ID",
        "GLASSHIVE_PARENT_REVISION",
        "GLASSHIVE_COMPONENT_REVISION",
        "GLASSHIVE_DEFAULT_WORKER_PROFILE",
        "GLASSHIVE_ALLOWED_WORKER_PROFILES",
        "GLASSHIVE_WATCH_SESSION_STATE_PATH",
        "GLASSHIVE_MAX_WATCH_SESSION_DURATION_S",
        "GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE",
        "GLASSHIVE_INTERNAL_ASSERTION_ISSUER",
        "GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE",
        "GLASSHIVE_INTERNAL_ASSERTION_KEY_ID",
        "GLASSHIVE_INTERNAL_ASSERTION_PREVIOUS_JWKS_FILE",
        "GLASSHIVE_INTERNAL_ASSERTION_PREVIOUS_KEYS_EXPIRE_AT",
        "GLASSHIVE_HUMAN_AUTH_MODE",
        "GLASSHIVE_AUTH_STATE_PATH",
        "GLASSHIVE_PROVIDER_EMAIL_LOGIN",
        "GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT",
        "GLASSHIVE_LOCAL_PASSWORD_LOGIN",
        "GLASSHIVE_OIDC_LOGIN_VISIBLE",
        "GLASSHIVE_LOCAL_AUTH_ALLOWED_EMAIL_DOMAINS",
        "GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY",
        "GLASSHIVE_ALLOW_EMAIL_LOGIN",
        "GLASSHIVE_ALLOW_EMAIL_REGISTRATION",
        "GLASSHIVE_ALLOWED_EMAIL_DOMAINS",
        "GLASSHIVE_AUTH_SESSION_TTL_SECONDS",
        "GLASSHIVE_AUTH_MAX_ATTEMPTS",
        "GLASSHIVE_OIDC_ISSUER",
        "GLASSHIVE_OIDC_CLIENT_ID",
        "GLASSHIVE_OIDC_CLIENT_SECRET",
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "GLASSHIVE_OIDC_POST_LOGOUT_REDIRECT_URI",
        "GLASSHIVE_OIDC_SCOPES",
        "GLASSHIVE_OIDC_ROLE_CLAIM",
        "GLASSHIVE_OIDC_ROLE_MAP_JSON",
        "GLASSHIVE_OIDC_PRINCIPAL_CLAIM",
        "GLASSHIVE_OIDC_EMAIL_CLAIM",
        "GLASSHIVE_OIDC_EMAIL_CLAIM_TRUSTED",
        "GLASSHIVE_MCP_OAUTH_SUBJECT_CLAIM",
        "GLASSHIVE_PRINCIPAL_ID_FORMAT",
        "GLASSHIVE_TRUSTED_PROXY_BOUNDARY_PROVEN",
        "GLASSHIVE_SCHEDULING_OWNER",
        "GLASSHIVE_SCHEDULING_OWNER_URL",
        "GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS",
        "GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH",
        "GLASSHIVE_PROVIDER_SECRET_STORE_ENABLED",
        "GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION",
        "WPR_DEFAULT_EXECUTION_MODE",
        "WPR_ALLOWED_WORKER_PROFILES",
        "WPR_DB_PATH",
        "WPR_LINK_REF_TTL_SECONDS",
        "VIVENTIUM_ENV_FILE",
        "VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    # Keep unit tests isolated from the currently installed Viventium runtime.
    # Individual runtime-env loading tests opt into their own synthetic env file.
    monkeypatch.setenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", "1")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(tmp_path / "link_refs.sqlite3"))
    server_module._NOVNC_VIEW_URL_CACHE.clear()
    server_module._NOVNC_ASSET_CACHE.clear()
    server_module._NOVNC_HTTP_CLIENT = None


class StreamingFakeClient:
    """Let existing authorization fakes exercise streamed response lifetimes."""
    async def aclose(self):
        pass

    def stream(self, method, url, headers=None):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def context():
            response=await self.request(method,url,headers=headers,content=None)
            if not hasattr(response,"aiter_bytes"):
                async def body():
                    yield response.content
                response.aiter_bytes=body
            yield response
        return context()


class FakeRuntimeClient:
    def __init__(self):
        self.base_url = "http://runtime.test"
        self.header_contexts = []
        self.desktop_actions = []
        self.launch_failures = []
        self.fail_assign = False
        self.duplicate_requests = []
        self.create_project_requests = []
        self.create_worker_requests = []
        self.create_execution_workspace_requests = []
        self.add_execution_workspace_member_requests = []
        self.shared_workspace_response = {
            "workspace_id": "wsp_shared",
            "mode": "shared",
            "execution_mode": "docker",
            "file_placement": "common",
            "runtime_readiness": {"available": True, "code": "ready"},
        }
        self.assign_requests = []
        self.schedule_requests = []
        self.preference_requests = []
        self.metadata_requests = []
        self.get_worker_requests = []
        self.message_requests = []
        self.steer_requests = []
        self.lifecycle_requests = []
        self.worker_live_requests = []
        self.file_upload_requests = []
        self.worker_live_compact_requests = []
        self.worker_view_open_requests = []
        self.native_control_requests = []
        self.native_control_state_response = {
            "run_id": "run-native",
            "attempt_id": "attempt-native",
            "pending_requests": [],
        }
        self.provider_account_requests = []
        self.provider_setup_requests = []
        self.provider_disconnect_requests = []
        self.provider_verify_requests = []
        self.provider_forget_requests = []
        self.workspace_grant_requests = []
        self.duplication_reapproval_requests = []
        self.pending_change_requests = []
        self.pending_change_reads = []
        self.pending_confirm_requests = []
        self.recurring_schedule_requests = []
        self.workspace_duplicate_requests = []
        self.workspace_template_requests = []
        self.activity_requests = []
        self.schedule_authority_requests = []
        self.schedule_authority_error = None
        self.provider_readiness_response = {
            "readiness": "deployment_managed",
            "status": "Work AI is ready.",
        }
        self.health_response = {
            "status": "ok",
            "provider_setup_support": {"codex": "supported", "claude": "supported"},
        }

    def health(self):
        return dict(self.health_response)

    async def upload_file_content(self, upload_id, chunks):
        content = b''.join([chunk async for chunk in chunks])
        self.file_upload_requests.append((upload_id, content))
        return {'upload_id': upload_id, 'received_bytes': len(content), 'state': 'ready'}

    def file_request(self, method, path, payload=None):
        self.file_upload_requests.append((method, path, payload))
        return {'items': [], 'state': 'available'}

    def provider_readiness(self, profile: str):
        return {**self.provider_readiness_response, "profile": profile}

    def with_headers(self, headers: dict[str, str]):
        self.header_contexts.append(headers)
        return self

    def list_projects(self):
        return [{"project_id": "prj_1", "title": "Alpha"}]

    def list_workers(self, project_id: str):
        return [
            {"worker_id": "wrk_1", "name": "Main Worker", "profile": "codex-cli", "state": "ready"},
            {"worker_id": "wrk_dead", "name": "Old Worker", "profile": "codex-cli", "state": "terminated"},
        ]

    def current_user(self):
        return {"tenant_id": "local", "user_id": "demo-owner", "email": "", "role": "local_operator"}

    def set_schedule_principal_authority(self, principal_id: str, *, enabled: bool):
        self.schedule_authority_requests.append(
            {"principal_id": principal_id, "enabled": enabled}
        )
        if self.schedule_authority_error is not None:
            raise self.schedule_authority_error
        return {
            "principal_id": principal_id,
            "enabled": enabled,
            "native_schedules_deactivated": 1 if not enabled else 0,
            "delegated_schedules_deactivated": 1 if not enabled else 0,
        }

    def list_provider_accounts(self):
        return [{
            "account_id": "acct_1",
            "provider": "codex",
            "label": "Personal Codex",
            "status": "ready",
            "is_default": True,
        }]

    def provider_account_capabilities(self):
        return {
            "codex-cli": ["codex", "openai"],
            "claude-code": ["anthropic", "claude"],
            "grok-build": ["grok", "xai"],
        }

    def create_provider_account(self, payload: dict):
        self.provider_account_requests.append(payload)
        return {"account_id": "acct_new", **{key: value for key, value in payload.items() if key != "secret_locator"}}

    def start_provider_account_setup(self, account_id: str):
        self.provider_setup_requests.append({"action": "start", "account_id": account_id})
        return {"account_id": account_id, "status": "connecting", "instructions": "Visit the provider", "complete": False}

    def provider_account_setup_status(self, account_id: str):
        self.provider_setup_requests.append({"action": "status", "account_id": account_id})
        return {"account_id": account_id, "status": "ready", "instructions": "", "complete": True}

    def cancel_provider_account_setup(self, account_id: str):
        self.provider_setup_requests.append({"action": "cancel", "account_id": account_id})
        return {"account_id": account_id, "status": "action_required", "complete": True}

    def submit_provider_account_setup_input(self, account_id: str, value: str):
        self.provider_setup_requests.append({"action": "input", "account_id": account_id})
        return {
            "account_id": account_id,
            "status": "connecting",
            "instructions": "",
            "complete": False,
            "input_required": False,
        }

    def disconnect_provider_account(self, account_id: str):
        self.provider_disconnect_requests.append(account_id)
        return {"account_id": account_id, "status": "disconnected", "complete": True}

    def verify_provider_account(self, account_id: str):
        self.provider_verify_requests.append(account_id)
        return {"account_id": account_id, "status": "ready", "complete": True}

    def forget_provider_account(self, account_id: str):
        self.provider_forget_requests.append(account_id)
        return {"account_id": account_id, "status": "forgotten"}

    def list_connections(self):
        return [{"connection_id": "conn_1", "label": "Team documents", "status": "ready"}]

    def list_library(self):
        return [{
            "library_id": "lib_1",
            "stable_id": "skill.synthetic.summary",
            "version": "1.0.0",
            "scopes": ["documents:read"],
            "activation_status": "ready",
            "manifest": {"activatable": True},
        }]

    def list_activity(self, *, limit=50):
        self.activity_requests.append(limit)
        return [{"event_id": "evt_1", "workspace_name": "Main Worker", "event_type": "worker.created"}]

    def list_workspace_catalog(self, **kwargs):
        requested = {value for value in str(kwargs.get("kind") or "named").split(",") if value}
        items = []
        for project in self.list_projects():
            for worker in self.list_workers(project["project_id"]):
                workspace_kind = str(worker.get("workspace_kind") or "named")
                if requested and workspace_kind not in requested:
                    continue
                items.append(
                    {
                        **worker,
                        "project_id": project["project_id"],
                        "project_title": project["title"],
                        "workspace_kind": workspace_kind,
                        "favorite": bool(worker.get("favorite")),
                        "tags": list(worker.get("tags") or []),
                    }
                )
        return {"items": items, "next_cursor": None}

    def duplicate_workspace(self, worker_id: str, *, idempotency_key: str, name: str = ""):
        self.workspace_duplicate_requests.append(
            {"worker_id": worker_id, "idempotency_key": idempotency_key, "name": name}
        )
        return {
            "project": {"project_id": "prj_duplicate"},
            "workspace": {
                "worker_id": "wrk_dup",
                "name": name or "Main Worker copy",
                "duplication_report": {"capabilities_requiring_reapproval": 0},
            },
        }

    def list_workspace_templates(self):
        return [{
            "template_id": "wst_synthetic",
            "lineage_id": "wsl_synthetic",
            "version": 1,
            "name": "Synthetic research desk",
            "description": "A reusable, private workspace intent.",
            "profile": "codex-cli",
            "execution_mode": "docker",
            "content_hash": "sha256:synthetic",
            "library_refs": [{
                "stable_id": "skill.synthetic.summary",
                "version": "1.0.0",
                "content_hash": "sha256:library-synthetic",
                "scopes": ["documents:read"],
            }],
        }]

    def save_workspace_template(self, worker_id: str, payload: dict):
        self.workspace_template_requests.append(
            {"action": "save", "worker_id": worker_id, "payload": payload}
        )
        return {**self.list_workspace_templates()[0], **payload}

    def instantiate_workspace_template(self, template_id: str, payload: dict):
        self.workspace_template_requests.append(
            {"action": "instantiate", "template_id": template_id, "payload": payload}
        )
        reapproval_items = [{
            **self.list_workspace_templates()[0]["library_refs"][0],
            "kind": "library",
            "reference": "skill.synthetic.summary@1.0.0",
            "route": "library",
            "resolution": "library_grant",
            "action_id": "rea_synthetic",
        }]
        return {
            "project": {"project_id": "prj_template"},
            "workspace": {
                "worker_id": "wrk_template",
                "name": payload.get("name") or "Synthetic research desk",
                "state": "paused",
                "duplication_report": {
                    "reapproval_items": reapproval_items,
                    "outstanding_reapproval_items": reapproval_items,
                    "capabilities_requiring_reapproval": 1,
                },
            },
            "approvals_required": self.list_workspace_templates()[0]["library_refs"],
            "idempotent_replay": False,
        }

    def create_pending_change(self, payload: dict):
        self.pending_change_requests.append(payload)
        return {"change_id": "chg_1", "confirmation_token": "synthetic-confirmation-token", "status": "pending"}

    def list_workspace_grants(self, worker_id: str):
        self.workspace_grant_requests.append({"action": "list", "worker_id": worker_id})
        return [{"grant_id": "grant_1", "worker_id": worker_id, "library_id": "lib_1"}]

    def revoke_workspace_grant(self, worker_id: str, grant_id: str):
        self.workspace_grant_requests.append(
            {"action": "revoke", "worker_id": worker_id, "grant_id": grant_id}
        )
        return {"grant_id": grant_id, "worker_id": worker_id, "revoked_at": 1}

    def waive_workspace_duplication_reapproval(self, worker_id: str, reference: str):
        self.duplication_reapproval_requests.append(
            {"worker_id": worker_id, "reference": reference}
        )
        return {"worker_id": worker_id, "remaining": 0}

    def get_pending_change(self, change_id: str):
        self.pending_change_reads.append(change_id)
        return {
            "change_id": change_id,
            "change_type": "library_enable",
            "target_id": "wrk_1",
            "payload": {"library_id": "lib_1", "scopes": ["files:read"]},
            "status": "pending",
            "expires_at": 4102444800,
        }

    def confirm_pending_change(self, change_id: str, confirmation_token: str):
        self.pending_confirm_requests.append({"change_id": change_id, "confirmation_token": confirmation_token})
        return {"change_id": change_id, "status": "confirmed"}

    def get_worker(self, worker_id: str):
        self.get_worker_requests.append(worker_id)
        return {"worker_id": worker_id, "project_id": "prj_1", "profile": "codex-cli"}

    def get_project(self, project_id: str):
        return {"project_id": project_id, "title": "Alpha"}

    def get_preferences(self):
        return {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": "",
            "codex_reasoning_effort": "",
            "claude_effort": "",
            "openclaw_effort": "",
            "updated_at": "",
        }

    def update_preferences(self, payload: dict):
        self.preference_requests.append(payload)
        return {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": payload.get("default_worker_profile", ""),
            "codex_reasoning_effort": payload.get("codex_reasoning_effort", ""),
            "claude_effort": payload.get("claude_effort", ""),
            "openclaw_effort": payload.get("openclaw_effort", ""),
            "updated_at": "2026-05-24T00:00:00+00:00",
        }

    def worker_live(self, worker_id: str, *, compact: bool = False):
        self.worker_live_requests.append(worker_id)
        self.worker_live_compact_requests.append({"worker_id": worker_id, "compact": compact})
        return {
            "worker": {
                "worker_id": worker_id,
                "name": "Main Worker",
                "project_id": "prj_1",
                "profile": "codex-cli",
                "state": getattr(self, "worker_state", "ready"),
            },
            "runtime_details": {"view_url": "http://127.0.0.1:60812/?autoconnect=1&password=synthetic-vnc"},
            "latest_output": "OK",
            "deliverable": {
                "kind": "webpage",
                "browser_url": "file:///workspace/project/index.html",
                "label": "index.html",
                "preferred_surface": "desktop",
            },
        }

    def native_control_state(self, worker_id: str):
        self.native_control_requests.append({"action": "state", "worker_id": worker_id})
        return dict(self.native_control_state_response)

    def native_control(self, worker_id: str, payload: dict):
        self.native_control_requests.append(
            {"action": "control", "worker_id": worker_id, "payload": dict(payload)}
        )
        return {"status": "queued"}

    def create_project(self, owner_id: str, title: str, goal: str, default_worker_profile: str):
        self.create_project_requests.append(
            {
                "owner_id": owner_id,
                "title": title,
                "goal": goal,
                "default_worker_profile": default_worker_profile,
            }
        )
        return {"project_id": "prj_new"}

    def create_worker(self, project_id: str, owner_id: str, profile: str, **kwargs):
        self.create_worker_requests.append({"project_id": project_id, "owner_id": owner_id, "profile": profile, **kwargs})
        return {"worker_id": "wrk_new"}

    def create_execution_workspace(self, project_id: str, **kwargs):
        self.create_execution_workspace_requests.append({"project_id": project_id, **kwargs})
        return dict(self.shared_workspace_response)

    def add_execution_workspace_member(self, workspace_id: str, payload: dict):
        self.add_execution_workspace_member_requests.append(
            {"workspace_id": workspace_id, "payload": dict(payload)}
        )
        return {"worker_id": "wrk_shared", "project_id": "prj_new"}

    def duplicate_worker(
        self,
        project_id: str,
        source_worker_id: str,
        owner_id: str,
        *,
        name: str = "Main Workspace",
    ):
        self.duplicate_requests.append(
            {
                "project_id": project_id,
                "source_worker_id": source_worker_id,
                "owner_id": owner_id,
                "name": name,
            }
        )
        return {"worker_id": "wrk_dup", "name": name}

    def assign_run(self, worker_id: str, instruction: str):
        self.assign_requests.append({"worker_id": worker_id, "instruction": instruction})
        if self.fail_assign:
            raise RuntimeError("assign failed")
        return {"run_id": "run_1"}

    def schedule_run(self, worker_id: str, instruction: str, **kwargs):
        self.schedule_requests.append({"worker_id": worker_id, "instruction": instruction, **kwargs})
        return {"schedule_id": "sch_1", "worker_id": worker_id, "run_at": "2026-05-23T19:00:00+00:00", "state": "pending"}

    def create_recurring_schedule(self, worker_id: str, payload: dict):
        self.recurring_schedule_requests.append({"action": "create", "worker_id": worker_id, "payload": payload})
        return {
            "definition_id": "rsd_public_safe",
            "worker_id": worker_id,
            "instruction": payload["instruction"],
            "recurrence_type": payload["recurrence_type"],
            "interval_seconds": payload.get("interval_seconds"),
            "local_time": payload.get("local_time", ""),
            "timezone_name": payload.get("timezone_name", "UTC"),
            "dst_policy": payload.get("dst_policy", "elapsed"),
            "next_run_at": "2027-01-02T14:00:00+00:00",
            "active": True,
        }

    def recurring_schedules(self, *, include_inactive: bool = False):
        self.recurring_schedule_requests.append({"action": "list", "include_inactive": include_inactive})
        return [
            {
                "definition_id": "rsd_public_safe",
                "worker_id": "wrk_1",
                "instruction": "Run the synthetic check.",
                "recurrence_type": "interval",
                "interval_seconds": 3600,
                "local_time": "",
                "timezone_name": "UTC",
                "dst_policy": "elapsed",
                "next_run_at": "2027-01-02T14:00:00+00:00",
                "active": True,
            }
        ]

    def recurring_schedule_occurrences(self, definition_id: str, *, limit: int = 50):
        self.recurring_schedule_requests.append(
            {"action": "occurrences", "definition_id": definition_id, "limit": limit}
        )
        return [{"occurrence_id": "occ_public_safe", "definition_id": definition_id, "state": "completed"}]

    def deactivate_recurring_schedule(self, definition_id: str):
        self.recurring_schedule_requests.append({"action": "deactivate", "definition_id": definition_id})
        return {"definition_id": definition_id, "active": False}

    def update_recurring_schedule(self, definition_id: str, payload: dict):
        self.recurring_schedule_requests.append(
            {"action": "update", "definition_id": definition_id, "payload": payload}
        )
        return {
            "definition_id": definition_id,
            "active": bool(payload.get("enabled", True)),
            "enabled": bool(payload.get("enabled", True)),
        }

    def run_recurring_schedule_now(self, definition_id: str, idempotency_key: str):
        self.recurring_schedule_requests.append(
            {"action": "run_now", "definition_id": definition_id, "idempotency_key": idempotency_key}
        )
        return {"definition_id": definition_id, "status": "scheduled", "schedule_id": "sch_manual"}

    def retire_recurring_schedule(self, definition_id: str):
        self.recurring_schedule_requests.append({"action": "retire", "definition_id": definition_id})
        return {
            "definition_id": definition_id,
            "active": False,
            "retired_at": "2027-01-01T00:00:00+00:00",
        }

    def update_worker_metadata(self, worker_id: str, payload: dict):
        self.metadata_requests.append({"worker_id": worker_id, "payload": payload})
        return {"worker_id": worker_id, "favorite": payload.get("favorite", False)}

    def launch_failed(self, worker_id: str, reason: str):
        self.launch_failures.append({"worker_id": worker_id, "reason": reason})
        return {"worker_id": worker_id, "state": "failed", "last_error": reason}

    def desktop_action(self, worker_id: str, action: str, url: str | None = None, run_id: str | None = None):
        self.desktop_actions.append({"worker_id": worker_id, "action": action, "url": url, "run_id": run_id})
        return {"status": "launched", "action": action}

    def message(self, worker_id: str, message: str):
        self.message_requests.append({"worker_id": worker_id, "message": message})
        return {"status": "queued"}

    def steer(self, worker_id: str, message: str):
        self.steer_requests.append({"worker_id": worker_id, "message": message})
        return {"run_id": "run_steer", "worker_id": worker_id, "project_id": "prj_1", "instruction": message, "state": "queued", "queued_at": "2026-04-17T00:00:00+00:00", "started_at": None, "ended_at": None, "output_text": "", "error_text": ""}

    def lifecycle(self, worker_id: str, action: str):
        self.lifecycle_requests.append({"worker_id": worker_id, "action": action})
        return {"status": action}

    def record_worker_view_open(self, worker_id: str):
        self.worker_view_open_requests.append(worker_id)
        return {}


def signed_worker_token(secret: str, *, worker_id: str = "wrk_1", tenant_id: str = "tenant-alpha", owner_id: str = "user-a") -> str:
    payload = {
        "v": 1,
        "kind": "worker_view",
        "worker_id": worker_id,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "path": "",
        "exp": int(time.time()) + 900,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("utf-8"), sha256).hexdigest()
    return f"{encoded}.{signature}"


def worker_ref_record(url: str) -> dict[str, object]:
    assert url.startswith("/r/")
    assert "gh_token=" not in url
    record = resolve_signed_link_ref(url.rsplit("/", 1)[1])
    assert record is not None
    assert record["kind"] == "worker_view"
    assert "gh_token=" not in str(record.get("target_url") or "")
    return record


def load_runtime_signed_links_module():
    module_path = Path(__file__).resolve().parents[3] / "runtime_phase1" / "src" / "workers_projects_runtime" / "signed_links.py"
    spec = importlib.util.spec_from_file_location("glasshive_runtime_signed_links_for_ui_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def signed_artifact_token(
    secret: str,
    *,
    kind: str = "artifact_download",
    worker_id: str = "wrk_1",
    tenant_id: str = "tenant-alpha",
    owner_id: str = "user-a",
    path: str = "workspace/report.txt",
) -> str:
    payload = {
        "v": 1,
        "kind": kind,
        "worker_id": worker_id,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "path": path,
        "exp": int(time.time()) + 900,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("utf-8"), sha256).hexdigest()
    return f"{encoded}.{signature}"


def set_enterprise_ui_env(
    monkeypatch,
    *,
    service_secret: str = "ui-service-secret",
    signed_secret: str = "ui-signed-link-secret",
) -> None:
    monkeypatch.setenv("WPR_API_TOKEN", service_secret)
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", signed_secret)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")


def configure_internal_assertion_signer(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "gateway-private-key.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "signed_internal_assertion")
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE", str(key_path))
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_ISSUER", "https://gateway.example.invalid")
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE", "glasshive-runtime")
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_KEY_ID", "gateway-test-key")
    return private_key


def test_oidc_start_attempt_limiter_prunes_expired_sources_and_stays_bounded():
    limiter = server_module._BoundedAttemptLimiter(
        window_seconds=10,
        max_attempts=1,
        max_sources=3,
    )

    limiter.admit("source-a", now=0)
    limiter.admit("source-b", now=1)
    limiter.admit("source-c", now=2)
    limiter.admit("source-d", now=3)

    assert len(limiter.attempts_by_source) == 3
    assert "source-a" not in limiter.attempts_by_source

    with pytest.raises(server_module.HTTPException) as limited:
        limiter.admit("source-d", now=4)
    assert limited.value.status_code == 429

    limiter.admit("source-e", now=20)
    assert limiter.attempts_by_source == {"source-e": [20]}


@pytest.mark.parametrize("explicit_exists", [True, False])
def test_ui_explicit_runtime_env_never_imports_another_installation(tmp_path, monkeypatch, explicit_exists):
    default_dir = tmp_path / "Library/Application Support/Viventium/runtime"
    default_dir.mkdir(parents=True)
    (default_dir / "runtime.env").write_text("GLASSHIVE_PUBLIC_LINKS_ONLY=true\nGLASSHIVE_SIGNED_LINK_SECRET=other-installation\n")
    explicit = tmp_path / "selected/runtime.env"
    if explicit_exists:
        explicit.parent.mkdir()
        explicit.write_text("GLASSHIVE_RUNTIME_BASE_URL=http://selected.invalid\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(explicit))
    monkeypatch.delenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", raising=False)
    for key in ["GLASSHIVE_PUBLIC_LINKS_ONLY", "GLASSHIVE_SIGNED_LINK_SECRET", "GLASSHIVE_RUNTIME_BASE_URL"]:
        monkeypatch.delenv(key, raising=False)
    server_module._load_viventium_runtime_env()
    assert "GLASSHIVE_PUBLIC_LINKS_ONLY" not in os.environ
    assert "GLASSHIVE_SIGNED_LINK_SECRET" not in os.environ
    assert os.environ.get("GLASSHIVE_RUNTIME_BASE_URL") == ("http://selected.invalid" if explicit_exists else None)


def test_ui_loads_enterprise_service_auth_from_runtime_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "runtime.env"
    link_ref_state = tmp_path / "shared-link-refs.sqlite3"
    env_file.write_text(
        "\n".join(
            [
                "GLASSHIVE_ENTERPRISE_MODE=true",
                "GLASSHIVE_AUTH_MODE=first_party_assertion",
                "GLASSHIVE_ENTERPRISE_TENANT_ID=tenant_public_safe",
                "GLASSHIVE_TRUST_INBOUND_IDENTITY=true",
                "WPR_API_TOKEN=service-secret",
                "GLASSHIVE_SIGNED_LINK_SECRET=signed-link-secret",
                f"GLASSHIVE_LINK_REF_STATE_PATH={link_ref_state}",
            ]
        )
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(env_file))
    monkeypatch.setenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", "1")
    monkeypatch.delenv("GLASSHIVE_LINK_REF_STATE_PATH", raising=False)
    fake = FakeRuntimeClient()
    loaded_keys = {
        "GLASSHIVE_ENTERPRISE_MODE",
        "GLASSHIVE_AUTH_MODE",
        "GLASSHIVE_ENTERPRISE_TENANT_ID",
        "GLASSHIVE_TRUST_INBOUND_IDENTITY",
        "WPR_API_TOKEN",
        "GLASSHIVE_SIGNED_LINK_SECRET",
        "GLASSHIVE_LINK_REF_STATE_PATH",
    }
    try:
        client = TestClient(create_app(runtime_client=fake))
        response = client.get(
            "/api/bootstrap",
            headers={
                "X-Viventium-Tenant-Id": "tenant_public_safe",
                "X-Viventium-User-Id": "qa-user",
                "X-Viventium-User-Email": "qa@example.invalid",
                "X-Viventium-User-Role": "member",
            },
        )

        assert response.status_code == 200, response.text
        assert os.environ["GLASSHIVE_LINK_REF_STATE_PATH"] == str(link_ref_state)
        assert fake.header_contexts[0]["X-WPR-Token"] == "service-secret"
        assert fake.header_contexts[0]["X-Viventium-User-Id"] == "qa-user"
    finally:
        for key in loaded_keys:
            os.environ.pop(key, None)


def test_health_reports_gateway_and_runtime_release_provenance(monkeypatch):
    expected_release = {
        "release_id": "release_public_safe",
        "parent_revision": "a" * 40,
        "glasshive_revision": "b" * 40,
    }
    monkeypatch.setenv("GLASSHIVE_RELEASE_ID", expected_release["release_id"])
    monkeypatch.setenv("GLASSHIVE_PARENT_REVISION", expected_release["parent_revision"])
    monkeypatch.setenv("GLASSHIVE_COMPONENT_REVISION", expected_release["glasshive_revision"])
    runtime = FakeRuntimeClient()
    runtime.health = lambda: {"status": "ok", "release": expected_release}

    response = TestClient(create_app(runtime_client=runtime)).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "release": expected_release,
        "runtime": {"status": "ok", "release": expected_release},
    }


def test_bootstrap_and_launch_flow():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))
    boot = client.get('/api/bootstrap')
    assert boot.status_code == 200
    assert boot.json()['new_workspace_options'][0]['value'] == 'new:codex-cli'
    assert boot.json()['new_workspace_options'][0]['label'] == 'Codex worker'
    assert boot.json()['default_launch_surface'] == 'desktop'
    assert boot.json()['default_workspace_type'] == 'sandboxed'
    assert boot.json()['workspace_type_options'][0]['label'] == 'Sandboxed Workspace'
    assert boot.json()['provider_accounts'][0]['account_id'] == 'acct_1'
    assert boot.json()['profile_account_providers']['codex-cli'] == ['codex', 'openai']
    assert boot.json()['recurring_schedules_status'] == 'ready'
    assert set(boot.json()['bootstrap_sections'].values()) == {'ready'}
    assert boot.json()['recurring_schedules'][0]['worker_id'] == 'wrk_1'
    assert len(boot.json()['existing_workspaces']) == 1
    assert boot.json()['existing_workspaces'][0]['is_active'] is False
    assert boot.json()['existing_workspaces'][0]['is_resumable'] is True
    assert boot.json()['existing_workspaces'][0]['state_label'] == 'retained'
    assert boot.json()['existing_workspaces'][0]['watch_url'] == '/watch/wrk_1?project_id=prj_1&surface=desktop'
    assert boot.json()['existing_workspaces'][0]['workspace_url'] == '/watch/wrk_1?project_id=prj_1&surface=desktop'
    assert boot.json()['existing_workspaces'][0]['project_url'] == '/ui/projects/prj_1?worker_id=wrk_1'
    assert boot.json()['existing_workspaces'][0]['desktop_url'] == '/desktop/wrk_1'
    assert boot.json()['existing_workspaces'][0]['desktop_preview_url'] == '/desktop/wrk_1?preview=1'
    assert boot.json()['existing_workspaces'][0]['api_url'] == '/api/worker/wrk_1'
    assert boot.json()['existing_workspaces'][0]['control_url'] == '/api/worker/wrk_1'

    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': 'Focus on resumable sandboxes',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_new')
    assert 'surface=desktop' in launch.json()['watch_url']
    assert fake.create_worker_requests[-1]['start_synchronously'] is False


def test_shared_workspace_launch_creates_one_workspace_member_without_isolated_worker():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    response = client.post('/api/launch', json={
        'description': 'Create a shared project workspace',
        'success_criteria': 'Keep the shared files in one project area',
        'workspace_option': 'new:codex-cli',
        'workspace_mode': 'shared',
        'workspace_file_placement': 'common',
        'provider_account_policy': 'personal_required',
        'provider_account_id': 'acct_1',
        'effort': 'high',
    })

    assert response.status_code == 200, response.text
    assert response.json()['worker_id'] == 'wrk_shared'
    assert fake.create_execution_workspace_requests == [{
        'project_id': 'prj_new',
        'execution_mode': 'docker',
        'file_placement': 'common',
    }]
    assert fake.add_execution_workspace_member_requests == [{
        'workspace_id': 'wsp_shared',
        'payload': {
            'name': 'Create a shared project workspace',
            'role': 'Keep the shared files in one project area',
            'profile': 'codex-cli',
            'effort': 'high',
            'provider_account_policy': 'personal_required',
            'provider_account_id': 'acct_1',
        },
    }]
    assert fake.create_worker_requests == []
    assert fake.assign_requests[-1]['worker_id'] == 'wrk_shared'
    assert 'Create a shared project workspace' in fake.assign_requests[-1]['instruction']
    assert 'Keep the shared files in one project area' in fake.assign_requests[-1]['instruction']


def test_shared_workspace_unavailable_keeps_exact_readiness_reason_and_does_not_add_member():
    fake = FakeRuntimeClient()
    fake.shared_workspace_response['runtime_readiness'] = {
        'available': False,
        'code': 'shared_runtime_unavailable',
    }
    client = TestClient(create_app(runtime_client=fake))

    response = client.post('/api/launch', json={
        'description': 'Try a shared workspace',
        'workspace_option': 'new:codex-cli',
        'workspace_mode': 'shared',
    })

    assert response.status_code == 409
    assert response.json()['detail'] == {
        'code': 'shared_runtime_unavailable',
        'message': 'Shared workspace is unavailable (shared_runtime_unavailable).',
    }
    assert fake.add_execution_workspace_member_requests == []
    assert fake.create_worker_requests == []
    assert fake.assign_requests == []


def test_shared_workspace_mode_is_rejected_for_saved_workspace_before_mutation():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    response = client.post('/api/launch', json={
        'description': 'Use the saved workspace',
        'workspace_option': 'open:wrk_1',
        'workspace_mode': 'shared',
    })

    assert response.status_code == 409
    assert response.json()['detail'] == {
        'code': 'shared_new_workspace_required',
        'message': 'Shared workspaces are available when creating a new workspace.',
    }
    assert fake.get_worker_requests == []
    assert fake.create_execution_workspace_requests == []
    assert fake.create_worker_requests == []


def test_shared_workspace_mode_rejects_host_execution_before_project_creation(monkeypatch):
    monkeypatch.setenv('GLASSHIVE_HOST_WORKERS_ENABLED', 'true')
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    response = client.post('/api/launch', json={
        'description': 'Try a shared host workspace',
        'workspace_option': 'new:codex-cli',
        'workspace_type': 'host',
        'workspace_mode': 'shared',
    })

    assert response.status_code == 409
    assert response.json()['detail'] == {
        'code': 'shared_host_execution_unavailable',
        'message': 'Shared workspaces require managed container placement.',
    }
    assert fake.create_project_requests == []
    assert fake.create_execution_workspace_requests == []


def test_new_workspace_launch_blocks_missing_work_ai_before_any_workspace_mutation():
    fake = FakeRuntimeClient()
    fake.provider_readiness_response = {
        "readiness": "action_required",
        "status": "Work AI is not set up.",
    }
    client = TestClient(create_app(runtime_client=fake))

    response = client.post('/api/launch', json={
        'description': 'Create a synthetic provider readiness report',
        'success_criteria': 'Return the report',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })

    assert response.status_code == 409
    assert response.json()['detail'] == (
        'Work AI is not set up. Ask an administrator to finish provider setup '
        'or connect a personal account in Connections.'
    )
    assert fake.create_project_requests == []
    assert fake.create_worker_requests == []
    assert fake.assign_requests == []
    assert fake.launch_failures == []


def test_bootstrap_labels_degraded_personal_sections_instead_of_claiming_empty_state():
    class DegradedRuntime(FakeRuntimeClient):
        def get_preferences(self):
            raise RuntimeError("synthetic unavailable")

        def list_activity(self, *, limit=50):
            raise RuntimeError("synthetic unavailable")

        def list_provider_accounts(self):
            raise RuntimeError("synthetic unavailable")

        def list_workspace_templates(self):
            raise RuntimeError("synthetic unavailable")

        def recurring_schedules(self, *, include_inactive: bool = False):
            raise RuntimeError("synthetic unavailable")

    response = TestClient(create_app(runtime_client=DegradedRuntime())).get('/api/bootstrap')

    assert response.status_code == 200
    payload = response.json()
    assert payload['bootstrap_sections']['workspace_catalog'] == 'ready'
    assert all(
        payload['bootstrap_sections'][section] == 'unavailable'
        for section in (
            'preferences',
            'activity',
            'provider_accounts',
            'workspace_templates',
            'recurring_schedules',
        )
    )
    assert payload['provider_accounts'] == []
    assert payload['workspace_templates'] == []
    assert payload['activity'] == []
    assert payload['recurring_schedules_status'] == 'unavailable'


def test_bootstrap_labels_degraded_workspace_catalog_instead_of_claiming_no_workspaces():
    class DegradedWorkspaceRuntime(FakeRuntimeClient):
        def list_workspace_catalog(self, **kwargs):
            raise RuntimeError("synthetic workspace catalog unavailable")

    response = TestClient(create_app(runtime_client=DegradedWorkspaceRuntime())).get('/api/bootstrap')

    assert response.status_code == 200
    payload = response.json()
    assert payload['bootstrap_sections']['workspace_catalog'] == 'unavailable'
    assert payload['existing_workspaces'] == []


def test_browser_avoids_workspace_fetch_when_bootstrap_marks_catalog_unavailable():
    app_js = (Path(server_module.STATIC_DIR) / 'app.js').read_text(encoding='utf-8')

    assert "incoming.bootstrap_sections?.workspace_catalog !== 'unavailable'" in app_js
    assert 'Your workspaces are temporarily unavailable.' in app_js


def test_launch_personal_preferred_resolves_ready_default_for_selected_profile():
    runtime = FakeRuntimeClient()
    runtime.list_provider_accounts = lambda: [
        {
            "account_id": "acct_claude_default",
            "provider": "claude",
            "label": "Claude account",
            "status": "ready",
            "is_default": True,
        },
        {
            "account_id": "acct_codex_default",
            "provider": "codex",
            "label": "Codex account",
            "status": "ready",
            "is_default": True,
        },
    ]
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Use my default Codex subscription',
        'workspace_option': 'new:codex-cli',
        'provider_account_policy': 'personal_preferred',
    })

    assert response.status_code == 200, response.text
    assert runtime.create_worker_requests[-1]['bootstrap_bundle']['provider_account'] == {
        'policy': 'personal_preferred',
        'account_id': 'acct_codex_default',
    }


def test_launch_personal_required_carries_explicit_account_and_never_falls_back():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Use only my subscription',
        'workspace_option': 'new:codex-cli',
        'provider_account_policy': 'personal_required',
        'provider_account_id': 'acct_1',
    })

    assert response.status_code == 200, response.text
    assert runtime.create_worker_requests[-1]['bootstrap_bundle']['provider_account'] == {
        'policy': 'personal_required',
        'account_id': 'acct_1',
    }


def test_launch_personal_required_without_ready_default_fails_before_project_creation():
    runtime = FakeRuntimeClient()
    runtime.list_provider_accounts = lambda: [{
        "account_id": "acct_not_ready",
        "provider": "codex",
        "label": "Reconnect me",
        "status": "action_required",
        "is_default": True,
    }]
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Do not use a deployment account',
        'workspace_option': 'new:codex-cli',
        'provider_account_policy': 'personal_required',
    })

    assert response.status_code == 409
    assert response.json()['detail'] == {
        'code': 'provider_account_required',
        'message': 'Connect an AI account to start.',
    }
    assert runtime.create_project_requests == []
    assert runtime.create_worker_requests == []


def test_launch_personal_preferred_without_ready_default_keeps_explicit_fallback_policy():
    runtime = FakeRuntimeClient()
    runtime.list_provider_accounts = lambda: []
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Prefer my subscription when one is ready',
        'workspace_option': 'new:codex-cli',
        'provider_account_policy': 'personal_preferred',
    })

    assert response.status_code == 200, response.text
    assert runtime.create_worker_requests[-1]['bootstrap_bundle']['provider_account'] == {
        'policy': 'personal_preferred',
    }
    assert runtime.create_worker_requests[-1]['start_synchronously'] is False


def test_launch_rejects_new_credential_selection_for_existing_workspace_before_mutation():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Continue the saved workspace',
        'workspace_option': 'open:wrk_1',
        'provider_account_policy': 'personal_required',
        'provider_account_id': 'acct_1',
    })

    assert response.status_code == 409
    assert 'saved policy' in response.json()['detail'].lower()
    assert runtime.create_project_requests == []
    assert runtime.create_worker_requests == []


@pytest.mark.parametrize(
    ('accounts', 'account_id', 'expected_detail'),
    [
        (
            [{
                "account_id": "acct_claude",
                "provider": "claude",
                "label": "Claude account",
                "status": "ready",
                "is_default": True,
            }],
            'acct_claude',
            'does not match',
        ),
        (
            [{
                "account_id": "acct_codex",
                "provider": "codex",
                "label": "Codex account",
                "status": "action_required",
                "is_default": True,
            }],
            'acct_codex',
            'not ready',
        ),
    ],
)
def test_launch_rejects_mismatched_or_unready_explicit_personal_account(
    accounts,
    account_id,
    expected_detail,
):
    runtime = FakeRuntimeClient()
    runtime.list_provider_accounts = lambda: accounts
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Use the selected account',
        'workspace_option': 'new:codex-cli',
        'provider_account_policy': 'personal_required',
        'provider_account_id': account_id,
    })

    assert response.status_code == 409
    assert expected_detail in response.json()['detail'].lower()
    assert runtime.create_project_requests == []
    assert runtime.create_worker_requests == []


def test_launch_without_credential_fields_preserves_legacy_bootstrap_contract():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Use the existing compatibility behavior',
        'workspace_option': 'new:codex-cli',
    })

    assert response.status_code == 200, response.text
    assert 'provider_account' not in (runtime.create_worker_requests[-1]['bootstrap_bundle'] or {})


def test_deployment_account_launch_does_not_depend_on_personal_account_catalog():
    runtime = FakeRuntimeClient()

    def unavailable():
        raise RuntimeError('personal account catalog unavailable')

    runtime.list_provider_accounts = unavailable
    runtime.provider_account_capabilities = unavailable
    client = TestClient(create_app(runtime_client=runtime))

    deployment = client.post('/api/launch', json={
        'description': 'Use the deployment-managed account',
        'workspace_option': 'new:codex-cli',
        'provider_account_policy': 'legacy',
    })
    assert deployment.status_code == 200, deployment.text
    assert runtime.create_worker_requests[-1]['bootstrap_bundle']['provider_account'] == {
        'policy': 'legacy',
    }

    personal = client.post('/api/launch', json={
        'description': 'Use only my personal account',
        'workspace_option': 'new:codex-cli',
        'provider_account_policy': 'personal_required',
    })
    assert personal.status_code == 502
    assert len(runtime.create_project_requests) == 1


def test_launch_applies_codex_effort_to_new_workspace_bootstrap():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    launch = client.post('/api/launch', json={
        'description': 'Create a report',
        'success_criteria': 'Report exists',
        'workspace_option': 'new:codex-cli',
        'effort': 'xhigh',
    })

    assert launch.status_code == 200
    bundle = fake.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "xhigh"


def test_launch_accepts_codex_none_effort_to_match_mcp_policy():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    launch = client.post('/api/launch', json={
        'description': 'Create a report',
        'success_criteria': 'Report exists',
        'workspace_option': 'new:codex-cli',
        'effort': 'none',
    })

    assert launch.status_code == 200
    bundle = fake.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "none"


def test_launch_applies_grok_effort_to_native_runtime_env():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    launch = client.post('/api/launch', json={
        'description': 'Create a report',
        'success_criteria': 'Report exists',
        'workspace_option': 'new:grok-build',
        'effort': 'high',
    })

    assert launch.status_code == 200
    bundle = fake.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle["env"]["WPR_GROK_REASONING_EFFORT"] == "high"


def test_launch_applies_claude_max_effort_to_runtime_env():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    launch = client.post('/api/launch', json={
        'description': 'Create a report',
        'success_criteria': 'Report exists',
        'workspace_option': 'new:claude-code',
        'effort': 'max',
    })

    assert launch.status_code == 200
    bundle = fake.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == "max"
    assert "Worker effort preference" not in bundle.get("system_instructions", "")


def test_launch_applies_claude_xhigh_effort_to_runtime_env():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    launch = client.post('/api/launch', json={
        'description': 'Create a report',
        'success_criteria': 'Report exists',
        'workspace_option': 'new:claude-code',
        'effort': 'xhigh',
    })

    assert launch.status_code == 200
    bundle = fake.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == "xhigh"


def test_local_launch_keeps_owner_files_access_when_signed_links_are_enabled(monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Keep local owner authority',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    watch_url=launch.json()["watch_url"]
    assert watch_url.startswith('/watch/wrk_new?')
    assert 'gh_token' not in watch_url
    assert client.get(watch_url).status_code==200
    attached=client.post('/api/workspace/wrk_new/files',json={
        'upload_ids':['fil_one'],'idempotency_key':'local-followup',
    })
    assert attached.status_code==201
    assert runtime.file_upload_requests[-1][2]['upload_ids']==['fil_one']


def test_authenticated_launch_survives_signed_watch_state_failure_without_duplicate_work(
    monkeypatch,
):
    runtime = FakeRuntimeClient()
    monkeypatch.setattr(
        server_module,
        "_append_signed_worker_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic watch state unavailable")),
    )
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Create one durable workspace',
        'workspace_option': 'new:codex-cli',
    })

    assert response.status_code == 200, response.text
    assert response.json()["watch_url"].startswith("/watch/wrk_new?")
    assert len(runtime.create_project_requests) == 1
    assert len(runtime.create_worker_requests) == 1
    assert len(runtime.assign_requests) == 1


@pytest.mark.parametrize("locked_state", ["link_ref", "watch_session"])
def test_authenticated_launch_signed_watch_state_lock_falls_back_within_request_budget(
    tmp_path,
    monkeypatch,
    locked_state,
):
    link_ref_path = tmp_path / "locked-link-refs.sqlite3"
    watch_session_path = tmp_path / "locked-watch-sessions.sqlite3"
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-ui-service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(link_ref_path))
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(watch_session_path))
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "1800")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    with signed_links_module._link_ref_conn():
        pass
    with server_module._watch_session_conn():
        pass
    locked_path = link_ref_path if locked_state == "link_ref" else watch_session_path
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    lock_ready = threading.Event()

    def hold_writer_lock():
        with sqlite3.connect(locked_path, timeout=0.1) as lock:
            lock.execute("BEGIN IMMEDIATE")
            lock_ready.set()
            time.sleep(0.75)
            lock.rollback()

    lock_thread = threading.Thread(target=hold_writer_lock)
    lock_thread.start()
    assert lock_ready.wait(timeout=1.0)

    started_at = time.monotonic()
    try:
        response = client.post('/api/launch', json={
            'description': 'Create one durable workspace without waiting on auxiliary state',
            'workspace_option': 'new:codex-cli',
        })
        elapsed = time.monotonic() - started_at
    finally:
        lock_thread.join(timeout=2.0)

    assert response.status_code == 200, response.text
    assert elapsed < 0.5
    assert response.json()["watch_url"].startswith("/watch/wrk_new?")
    assert len(runtime.create_project_requests) == 1
    assert len(runtime.create_worker_requests) == 1
    assert len(runtime.assign_requests) == 1


@pytest.mark.parametrize("corrupt_state", ["link_ref", "watch_session"])
def test_authenticated_launch_survives_corrupt_auxiliary_watch_state_without_duplicate_work(
    tmp_path,
    monkeypatch,
    corrupt_state,
):
    link_ref_path = tmp_path / "link-refs.sqlite3"
    watch_session_path = tmp_path / "watch-sessions.sqlite3"
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-ui-service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(link_ref_path))
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(watch_session_path))
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "1800")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    corrupt_path = link_ref_path if corrupt_state == "link_ref" else watch_session_path
    corrupt_path.write_bytes(b"synthetic non-SQLite state")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Create one durable workspace despite corrupt auxiliary state',
        'workspace_option': 'new:codex-cli',
    })

    assert response.status_code == 200, response.text
    assert response.json()["watch_url"].startswith("/watch/wrk_new?")
    assert len(runtime.create_project_requests) == 1
    assert len(runtime.create_worker_requests) == 1
    assert len(runtime.assign_requests) == 1


def test_launch_honors_available_host_workspace_type(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["default_workspace_type"] == "host"
    assert [item["label"] for item in boot.json()["workspace_type_options"]] == [
        "Sandboxed Workspace",
        "Your Computer",
    ]

    launch = client.post(
        "/api/launch",
        json={
            "description": "Create a local marker file",
            "success_criteria": "Marker file exists",
            "workspace_option": "new:codex-cli",
            "workspace_type": "host",
        },
    )

    assert launch.status_code == 200
    assert fake.create_worker_requests[-1]["execution_mode"] == "host"


def test_bootstrap_prefers_glasshive_execution_mode_alias(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["default_workspace_type"] == "host"


def test_host_workspace_type_available_even_when_docker_is_default(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["default_workspace_type"] == "sandboxed"
    assert [item["label"] for item in boot.json()["workspace_type_options"]] == [
        "Sandboxed Workspace",
        "Your Computer",
    ]

    launch = client.post(
        "/api/launch",
        json={
            "description": "Create a local marker file",
            "success_criteria": "Marker file exists",
            "workspace_option": "new:codex-cli",
            "workspace_type": "host",
        },
    )

    assert launch.status_code == 200
    assert fake.create_worker_requests[-1]["execution_mode"] == "host"


def test_launch_rejects_unavailable_host_workspace_type(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["default_workspace_type"] == "sandboxed"
    assert [item["label"] for item in boot.json()["workspace_type_options"]] == ["Sandboxed Workspace"]

    launch = client.post(
        "/api/launch",
        json={
            "description": "Create a local marker file",
            "success_criteria": "Marker file exists",
            "workspace_option": "new:codex-cli",
            "workspace_type": "host",
        },
    )

    assert launch.status_code == 400
    assert "Your Computer workspaces are not available" in launch.text
    assert fake.create_worker_requests == []


def test_bootstrap_filters_worker_profiles_from_guardrail_env(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["new_workspace_options"] == [
        {"value": "new:codex-cli", "label": "Codex worker", "profile": "codex-cli"}
    ]


def test_bootstrap_uses_configured_default_worker_profile(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["default_workspace_option"] == "new:claude-code"
    assert boot.json()["deployment_default_workspace_option"] == "new:claude-code"


def test_bootstrap_uses_saved_user_default_worker_profile():
    class PreferenceRuntime(FakeRuntimeClient):
        def get_preferences(self):
            return {
                "tenant_id": "local",
                "owner_id": "demo-owner",
                "default_worker_profile": "openclaw-general",
                "codex_reasoning_effort": "high",
                "claude_effort": "max",
                "openclaw_effort": "",
                "updated_at": "2026-05-24T00:00:00+00:00",
            }

    client = TestClient(create_app(runtime_client=PreferenceRuntime()))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["default_workspace_option"] == "new:openclaw-general"
    assert boot.json()["user_preferences"]["codex_reasoning_effort"] == "high"


def test_preference_endpoint_proxies_saved_defaults():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.patch(
        "/api/preferences",
        json={"default_worker_profile": "codex-cli", "codex_reasoning_effort": "xhigh"},
    )

    assert response.status_code == 200, response.text
    assert runtime.preference_requests == [
        {"default_worker_profile": "codex-cli", "codex_reasoning_effort": "xhigh"}
    ]
    assert response.json()["default_worker_profile"] == "codex-cli"


def test_bootstrap_fails_loud_for_default_worker_profile_outside_allowlist(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    with pytest.raises(RuntimeError, match="GLASSHIVE_DEFAULT_WORKER_PROFILE"):
        client.get("/api/bootstrap")


def test_bootstrap_fails_loud_for_allowed_profile_list_with_no_supported_profiles(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "not-a-real-worker")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    with pytest.raises(RuntimeError, match="GLASSHIVE_ALLOWED_WORKER_PROFILES"):
        client.get("/api/bootstrap")


def test_bootstrap_dedupes_workspace_rows_by_worker_id():
    class DuplicateRuntime(FakeRuntimeClient):
        def list_projects(self):
            return [
                {"project_id": "prj_1", "title": "Alpha"},
                {"project_id": "prj_2", "title": "Duplicate Reference"},
            ]

        def list_workers(self, project_id: str):
            return [{"worker_id": "wrk_1", "name": "Main Worker", "profile": "codex-cli", "state": "ready"}]

    client = TestClient(create_app(runtime_client=DuplicateRuntime()))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert [item["worker_id"] for item in boot.json()["existing_workspaces"]] == ["wrk_1"]


def test_bootstrap_uses_bounded_primary_catalog_instead_of_scanning_projects():
    class CatalogOnlyRuntime(FakeRuntimeClient):
        def list_projects(self):
            raise AssertionError("bootstrap must not scan every project")

        def list_workspace_catalog(self, **kwargs):
            assert kwargs == {"kind": "named", "limit": 25}
            return {
                "items": [
                    {
                        "worker_id": "wrk_catalog",
                        "project_id": "prj_catalog",
                        "project_title": "Catalog project",
                        "name": "Catalog workspace",
                        "profile": "codex-cli",
                        "state": "paused",
                        "workspace_kind": "named",
                        "tags": ["research"],
                    }
                ],
                "next_cursor": "cursor-more",
            }

    response = TestClient(create_app(runtime_client=CatalogOnlyRuntime())).get("/api/bootstrap")

    assert response.status_code == 200
    assert response.json()["existing_workspaces"][0]["worker_id"] == "wrk_catalog"
    assert response.json()["existing_workspaces"][0]["is_resumable"] is True


def test_bootstrap_exposes_paused_workers_as_resumable_workspaces():
    class PausedRuntime(FakeRuntimeClient):
        def list_workers(self, project_id: str):
            return [
                {"worker_id": "wrk_idle", "name": "Idle Worker", "profile": "codex-cli", "state": "paused", "workspace_kind": "named"},
                {"worker_id": "wrk_dead", "name": "Old Worker", "profile": "codex-cli", "state": "terminated"},
            ]

    client = TestClient(create_app(runtime_client=PausedRuntime()))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["existing_workspaces"] == [
        {
            "project_id": "prj_1",
            "project_title": "Alpha",
            "worker_id": "wrk_idle",
            "name": "Idle Worker",
            "workspace_label": "Alpha",
            "profile": "codex-cli",
            "state": "paused",
            "favorite": False,
            "workspace_kind": "named",
            "tags": [],
            "is_active": False,
            "is_resumable": True,
            "state_label": "paused",
            "watch_url": "/watch/wrk_idle?project_id=prj_1&surface=desktop",
            "workspace_url": "/watch/wrk_idle?project_id=prj_1&surface=desktop",
            "project_url": "/ui/projects/prj_1?worker_id=wrk_idle",
            "desktop_url": "/desktop/wrk_idle",
            "desktop_preview_url": "/desktop/wrk_idle?preview=1",
            "api_url": "/api/worker/wrk_idle",
            "control_url": "/api/worker/wrk_idle",
        }
    ]


def test_watch_assets_render():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    home = client.get('/')
    assert home.status_code == 200
    assert home.headers['cache-control'] == 'no-store'
    assert '<title>xPerfect</title>' in home.text
    assert 'Workspace' in home.text
    assert 'What would you like to do?' in home.text
    assert 'id="success_criteria" name="success_criteria" rows="3" placeholder="Anything the result must include?" required' not in home.text
    assert 'Workspace Type' in home.text
    assert 'Run Project' in home.text
    assert 'role="tablist"' in home.text
    assert 'data-view-tab="workspaces"' in home.text
    assert 'workspace-view' in home.text
    assert 'Inactive Workspaces' in home.text
    assert 'Status Report' in home.text
    assert 'Glass Drive' not in home.text
    assert '<a class="brand-mark" href="/" aria-label="xPerfect home">xPerfect</a>' in home.text
    watch = client.get('/watch/wrk_1')
    assert watch.status_code == 200
    assert watch.headers['cache-control'] == 'no-store'
    assert 'xPerfect' in watch.text
    assert 'Workspace live view' in watch.text
    assert 'Back to workspaces' in watch.text
    assert 'Open worker details' not in watch.text
    assert 'Send redirects now' in watch.text
    assert 'Hold Send or Cmd/Ctrl+Enter to queue instead' in watch.text
    assert 'Glass Drive' not in watch.text
    assert '<a class="brand-mark" href="/#workspaces" aria-label="Back to xPerfect workspaces">xPerfect</a>' in watch.text
    desktop = client.get('/desktop/wrk_1')
    assert desktop.status_code == 200
    assert desktop.headers['cache-control'] == 'no-store'
    assert '<title>xPerfect Desktop</title>' in desktop.text
    live = client.get('/api/worker/wrk_1/live')
    assert live.status_code == 200
    assert live.json()['runtime_details']['view_available'] is True
    assert 'view_url' not in live.json()['runtime_details']
    assert live.json()['project_title'] == 'Alpha'
    compact_live = client.get('/api/workspace/wrk_1/live?compact=1')
    assert compact_live.status_code == 200
    assert runtime.worker_live_compact_requests[-2:] == [
        {'worker_id': 'wrk_1', 'compact': False},
        {'worker_id': 'wrk_1', 'compact': True},
    ]
    credentials = client.get('/api/workspace/wrk_1/desktop-credentials')
    assert credentials.status_code == 200
    assert credentials.json() == {'password': 'synthetic-vnc'}
    assert credentials.headers['cache-control'] == 'no-store, max-age=0'
    assert credentials.headers['pragma'] == 'no-cache'
    assert credentials.headers['referrer-policy'] == 'no-referrer'


def test_legacy_worker_detail_opens_primary_watch_without_losing_owner_scope():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    redirect = client.get('/ui/workers/wrk_1', follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers['location'] == '/watch/wrk_1'
    assert redirect.headers['cache-control'] == 'no-store'
    watch = client.get(redirect.headers['location'])
    assert watch.status_code == 200
    assert 'Workspace live view' in watch.text
    assert 'Open worker details' not in watch.text


def test_legacy_worker_detail_moves_signed_view_into_watch_cookie(monkeypatch):
    secret = 'synthetic-view-secret'
    monkeypatch.setenv('GLASSHIVE_SIGNED_LINK_SECRET', secret)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    token = signed_worker_token(secret)
    redirect = client.get(f'/ui/workers/wrk_1?gh_token={token}', follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers['location'] == '/watch/wrk_1'
    assert 'gh_token' not in redirect.headers['location']
    assert 'set-cookie' in redirect.headers
    assert client.get('/watch/wrk_1').status_code == 200


def test_native_control_bff_keeps_typed_owner_bridge_and_rejects_viewer_writes(monkeypatch):
    runtime = FakeRuntimeClient()
    runtime.native_control_state_response = {
        "run_id": "run-native",
        "attempt_id": "attempt-native",
        "pending_requests": [{"request_id": "request-native", "method": "session/request_permission"}],
    }
    client = TestClient(create_app(runtime_client=runtime))

    state = client.get("/api/worker/wrk_1/native-control")
    assert state.status_code == 200
    assert state.json()["pending_requests"][0]["request_id"] == "request-native"
    result = client.post(
        "/api/worker/wrk_1/native-control",
        json={
            "run_id": "run-native",
            "attempt_id": "attempt-native",
            "action": "permission",
            "request_id": "request-native",
            "option_id": "allow",
        },
    )
    assert result.status_code == 200
    assert runtime.native_control_requests[-1] == {
        "action": "control",
        "worker_id": "wrk_1",
        "payload": {
            "run_id": "run-native",
            "attempt_id": "attempt-native",
            "action": "permission",
            "request_id": "request-native",
            "option_id": "allow",
        },
    }
    assert client.post(
        "/api/worker/wrk_1/native-control",
        json={
            "run_id": "run-native",
            "attempt_id": "attempt-native",
            "action": "cancel",
            "unexpected": "must be rejected",
        },
    ).status_code == 422
    assert client.get("/v1/workers/wrk_1/native-control").status_code == 404
    assert client.post(
        "/v1/workers/wrk_1/native-control",
        json={"run_id": "run-native", "attempt_id": "attempt-native", "action": "cancel"},
    ).status_code == 404

    secret = "ui-signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    token = signed_worker_token(secret)
    assert client.get(f"/api/worker/wrk_1/native-control?gh_token={token}").status_code == 403
    assert client.post(
        f"/api/worker/wrk_1/native-control?gh_token={token}",
        json={"run_id": "run-native", "attempt_id": "attempt-native", "action": "cancel"},
    ).status_code == 403
    assert runtime.native_control_requests[-1]["action"] == "control"


def test_worker_view_signed_token_respects_watch_session_cap(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_TTL_S", "3600")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "120")

    token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    payload = server_module.verify_signed_link_token(token)

    assert payload is not None
    assert 1 <= int(payload["exp"]) - int(time.time()) <= 120


def test_signed_workspace_links_reuse_persisted_watch_session_deadline(tmp_path, monkeypatch):
    signed_secret = "ui-signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", signed_secret)
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "60")
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(tmp_path / "watch-sessions.sqlite3"))
    identity = {"tenant_id": "tenant-alpha", "user_id": "user-a"}
    now = {"value": 1_000}

    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])

    first_url = server_module._append_signed_worker_token("/watch/wrk_1", "wrk_1", identity)
    first_payload = server_module.verify_signed_link_token(str(worker_ref_record(first_url)["token"]))
    assert first_payload is not None
    assert first_payload["exp"] == 1_060

    now["value"] = 1_020
    second_url = server_module._append_signed_worker_token("/watch/wrk_1", "wrk_1", identity)
    second_payload = server_module.verify_signed_link_token(str(worker_ref_record(second_url)["token"]))
    assert second_payload is not None
    assert second_payload["exp"] == 1_060

    now["value"] = 1_061
    third_url = server_module._append_signed_worker_token("/watch/wrk_1", "wrk_1", identity)
    third_payload = server_module.verify_signed_link_token(str(worker_ref_record(third_url)["token"]))
    assert third_payload is not None
    assert third_payload["exp"] == 1_121


def test_runtime_minted_watch_tokens_can_reopen_after_expired_session_deadline(tmp_path, monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "60")
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(tmp_path / "watch-sessions.sqlite3"))
    now = {"value": 1_000}

    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    first_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    assert client.get(f"/watch/wrk_1?gh_token={first_token}").status_code == 200

    now["value"] = 1_020
    fresh_callback_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    assert client.get(f"/watch/wrk_1?gh_token={fresh_callback_token}").status_code == 200

    now["value"] = 1_061
    expired_original_response = client.get(f"/watch/wrk_1?gh_token={first_token}")
    assert expired_original_response.status_code == 401

    expired_session_callback_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    reopened_response = client.get(f"/watch/wrk_1?gh_token={expired_session_callback_token}")
    assert reopened_response.status_code == 200

    main_ui_url = server_module._append_signed_worker_token(
        "/watch/wrk_1",
        "wrk_1",
        {"tenant_id": "tenant-alpha", "user_id": "user-a"},
    )
    assert client.get(main_ui_url).status_code == 200


def test_active_novnc_websocket_closes_at_watch_session_cap(tmp_path, monkeypatch):
    secret = "signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "1")
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(tmp_path / "watch-sessions.sqlite3"))
    upstreams = []

    class FakeUpstream:
        def __init__(self):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.closed = True
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(30)
            raise StopAsyncIteration

        async def send(self, message):
            _ = message

        async def close(self):
            self.closed = True

    def fake_connect(*args, **kwargs):
        _ = args, kwargs
        upstream = FakeUpstream()
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(server_module.websockets, "connect", fake_connect)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    # An authenticated operator websocket remains interactive, but is bounded
    # by the configured session cap. Shareable worker_view links are rejected
    # by the separate read-only capability regression.
    with client.websocket_connect(
        "/novnc/wrk_1/websockify",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()

    assert exc.value.code == 1008
    assert upstreams and upstreams[0].closed is True


def test_launcher_workspace_hive_static_controls():
    static_dir = Path(server_module.__file__).parent / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    desktop_js = (static_dir / "desktop.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    styles_css = (static_dir / "styles.css").read_text(encoding="utf-8")
    watch_html = (static_dir / "watch.html").read_text(encoding="utf-8")
    watch_js = (static_dir / "watch.js").read_text(encoding="utf-8")
    desktop_html = (static_dir / "desktop.html").read_text(encoding="utf-8")
    desktop_css = (static_dir / "desktop.css").read_text(encoding="utf-8")
    assert "workspace-live-preview" in app_js
    assert "workspace-live-frame" not in app_js
    assert "MAX_VIEW_ONLY_PREVIEWS = 3" in app_js
    assert "minmax(min(440px, 100%), 1fr)" in styles_css
    assert "aspect-ratio: 16 / 9" in styles_css
    assert "aspect-ratio: auto" in styles_css
    assert "RETAINED_TILE_REFRESH_MS" in app_js
    assert "dataset.nextLiveRefreshAt" in app_js
    assert "document.hidden" in app_js
    assert "workspaceRefreshInFlight" in app_js
    assert "withAuth('/api/bootstrap')" in app_js
    assert "show-workspace-status" in index_html
    assert "show-workspace-watch" in index_html
    assert "workerApiUrl(workerId, '/steer')" in app_js
    assert "workerApiUrl(workerId, `/action/${encodeURIComponent(action)}`)" in app_js
    assert "workerApiUrl(workerId, '/metadata')" in app_js
    assert "appendUrlPath" in app_js
    assert "deployment_default_workspace_option" in app_js
    assert "dataset.watchVisible !== 'false'" in app_js
    assert "dataset.viewportVisible === 'true'" in app_js
    assert "new IntersectionObserver" in app_js
    assert "workerApiUrl(workerId, '/live?compact=1')" in app_js
    assert "tile.dataset.refreshing" in app_js
    assert "preview=1" in app_js
    assert "frame.tabIndex = -1" in app_js
    assert "rfb.viewOnly = viewOnly" in desktop_js
    assert "rfb.focusOnClick = !viewOnly" in desktop_js
    assert "Open workspace" in app_js
    assert "workspace-tile-more" in app_js
    assert "workspace-tile-more-actions" in app_js
    assert "dataset.desktopPreviewUrl" in app_js
    assert "shouldHydrateWorkspaceDelivery" in app_js
    assert "dataset.deliveryRunId" in app_js
    assert "report.setAttribute('aria-controls', deliveryPanelId)" in app_js
    assert "Hide delivery" not in app_js
    assert "closedLabel.replace(/^View\\s+/, 'Hide ')" in app_js
    assert "workspace-delivery-thumbnail" not in app_js
    assert "moreSummary.textContent = 'More'" in app_js
    assert "moreActions.appendChild(duplicate)" in app_js
    assert "moreActions.appendChild(saveTemplate)" in app_js
    assert "moreActions.appendChild(accountSelect)" in app_js
    assert "moreActions.appendChild(rename)" in app_js
    assert "actions.appendChild(toggle)" in app_js
    assert "moreActions.appendChild(interrupt)" in app_js
    assert "workspace-status-button" in app_js
    assert "Open latest workspace output" in app_js
    assert "workspace-glass-open" in app_js
    assert "Open ${workspaceTileTitle(workspace)} workspace" in app_js
    assert "previewLink.tabIndex = -1" in app_js
    assert "allowPreviewOpen(false)" in app_js
    assert "View delivery" in app_js
    assert "renderWorkspaceDelivery" in app_js
    assert "workspace-delivery-panel" in app_js
    assert "Delivery ready" in app_js
    assert "normalized === 'completed' ? 'Completed'" not in app_js
    assert "workspace?.workspace_url || workspace?.watch_url" in app_js
    assert "workspace.project_url" not in app_js
    assert "runtimeBase}/ui/projects" not in watch_js
    assert "window.location.assign('/#workspaces')" in watch_js
    assert "Status Report" in index_html
    assert "Inactive Workspaces" in index_html
    assert "/api/workspaces/${encodeURIComponent(workerId)}/duplicate" in app_js
    assert "button.dataset.idempotencyKey ||= globalThis.crypto?.randomUUID?.()" in app_js
    assert "idempotency_key: button.dataset.idempotencyKey" in app_js
    assert "delete button.dataset.idempotencyKey" in app_js
    assert "error.code === 'workspace_duplication_failed'" in app_js
    assert "resetText = 'Start fresh copy'" in app_js
    assert "delete button.dataset.launchIdempotencyKey" in app_js
    assert "throw await responseError(response, 'Launch failed')" in app_js
    assert 'id="workspace-template-panel"' in index_html
    assert 'id="recurring-schedule-workspace" required' not in index_html
    assert 'id="recurring-schedule-instruction" rows="4" maxlength="10000" placeholder="Create the weekly project update and save it in the workspace." required' not in index_html
    assert 'id="workspace-template-start-form"' in index_html
    assert 'id="save-template-dialog"' in index_html
    assert "/api/workspaces/${encodeURIComponent(context.workerId)}/templates" in app_js
    assert "/api/workspace-templates/${encodeURIComponent(templateId)}/instantiate" in app_js
    assert "fresh paused workspace" in index_html
    assert "opaque worker-account reference—not files, sign-ins, credential homes, schedules, or history" in index_html
    assert "Choose a saved template first." in app_js
    assert "rename-workspace-dialog" in index_html
    assert "window.prompt" not in app_js
    assert "Duplicate selected workspace" in app_js
    assert "Workspace copied. Review" in app_js
    assert "glasshive.capability-review" in app_js
    assert "copiedWorker.duplication_report?.reapproval_items" in app_js
    assert "data.reapproval_items || []" in app_js
    assert "sessionStorage.removeItem(CAPABILITY_REVIEW_KEY)" in app_js
    assert "window.location.hash = route" in app_js
    assert 'data-view-tab="connections"' in index_html
    assert 'data-view-tab="library"' in index_html
    assert 'data-view-tab="schedules"' in index_html
    assert 'data-view-tab="activity"' in index_html
    assert 'id="workspace-search"' in index_html
    assert 'id="workspace-kind-filter"' in index_html
    assert '<option value="named">Saved workspaces</option>' in index_html
    assert 'id="workspace-tag-filter"' in index_html
    assert 'id="workspace-load-more"' in index_html
    assert 'id="rename-workspace-tags"' in index_html
    assert "fetchCatalogPage" in app_js
    assert "if (workspaceCatalogStatus) workspaceCatalogStatus.textContent = '';" in app_js
    assert "cursor: append ? String(catalogState.nextCursor || '') : ''" in app_js
    assert "workspace.provider_readiness" in app_js
    assert "Work AI is not set up. Ask an administrator." in app_js
    assert "Work AI: organization fallback" in app_js
    assert "function updateWorkspaceMeta" in app_js
    assert "meta.dataset.catalogDetails" in app_js
    assert "updateWorkspaceMeta(meta, data?.worker?.profile, state);" in app_js
    assert "workspace.capability_readiness" in app_js
    assert "workspace.next_schedule_at" in app_js
    assert "nextSchedule?.next_run_at && !workspace.next_schedule_at" in app_js
    assert "glasshive:control-plane-updated" in app_js
    assert "payload.detail && typeof payload.detail === 'object'" in app_js
    assert "openWorkspaceSurface" in app_js
    assert "{ name, tags }" in app_js
    assert "Workspace details" in index_html
    assert "viewRegistry" in app_js
    control_plane_js = (static_dir / "control-plane.js").read_text(encoding="utf-8")
    assert "window.dispatchEvent(new CustomEvent('glasshive:control-plane-updated', { detail: { connectedAccount } }))" in control_plane_js
    assert "We could not confirm whether it started. Check history before trying again." in control_plane_js
    assert "uncertainScheduleRunKeys" in control_plane_js
    assert "if (!workspace)" in control_plane_js
    assert "persistCapabilityReview(null)" in control_plane_js
    assert "prepareBrokeredCapabilityReapproval" not in control_plane_js
    assert "workspace_duplication_reapproval_waiver" in control_plane_js
    assert "outstanding_reapproval_items" in control_plane_js
    assert "copied workspace capabilit" in control_plane_js
    assert "copied workspace connection" not in control_plane_js
    assert "equivalentReapprovalScopes(item.scopes || [], exactReviewItem)" in control_plane_js
    assert "schedule.last_outcome || schedule.last_error" in control_plane_js
    assert "startsAt.required = false" in control_plane_js
    assert "Choose when this schedule should start." in control_plane_js
    assert "Latest result:" in control_plane_js
    assert "String(clients.codex.login_command || '')" in control_plane_js
    assert "Use xPerfect from another AI app" in index_html
    assert "Copy one command" not in index_html
    assert "function referenceRow" in control_plane_js
    assert "Registration reference" in control_plane_js
    assert "Codex callback · Do not open this address" in control_plane_js
    assert "Claude Code callback · Do not open this address" in control_plane_js
    assert "/api/control-plane" in control_plane_js
    assert "/api/connect-ai" in control_plane_js
    assert "provider-account-form" in control_plane_js
    assert "pollSetup" in control_plane_js
    assert 'id="library-request-form"' in index_html
    assert "submitLibraryRequest" in control_plane_js
    assert "Ask this workspace" in index_html
    assert "This sends a workspace-only request" in index_html
    assert "The connection review stays open until access is verified" in control_plane_js
    assert "Continue without" in control_plane_js
    assert "waiveCapabilityReapproval" in control_plane_js
    assert "pendingBrokeredReviewReference" not in control_plane_js
    assert "dispatch owner" not in control_plane_js
    assert "pendingCapabilityReview" in control_plane_js
    assert "/api/workspace/${encodeURIComponent(workspaceId)}/message" in control_plane_js
    assert "message: requestText" in control_plane_js
    assert "api.withAuth(`/watch/${encodeURIComponent(workspaceId)}?surface=desktop`)" in control_plane_js
    assert "workspace.control_url || workspace.api_url" in app_js
    assert "loadWorkspaceChoices" in control_plane_js
    assert "loadAllWorkspacePages" not in control_plane_js
    assert 'value="api_key"' not in index_html
    assert "availableProviderOptions" in control_plane_js
    assert 'value="enterprise_route"' not in index_html
    assert "enterprise_route: 'Enterprise route'" in control_plane_js
    assert "'Add account'" in control_plane_js
    assert "controlPlane?.manage_connections_url" in control_plane_js
    assert "linked · verifies on run" in control_plane_js
    assert "xPerfect will verify it when this workspace runs" in control_plane_js
    assert "/novnc/${workerId}/websockify" in desktop_js
    assert "runtime.view_available" in desktop_js
    assert "/desktop-credentials" in desktop_js
    assert "cache: 'no-store'" in desktop_js
    assert "localStorage" not in desktop_js
    assert "desktopRefreshInFlight" in desktop_js
    assert "scheduleDesktopRefresh" in desktop_js
    assert "isSettledWorkspaceState" in desktop_js
    assert "viewHealthHealthy" in desktop_js
    assert "ACTIVE_WORKER_STATES" in desktop_js
    assert "showSettledWorkspaceStatus" in desktop_js
    assert "settledDesktopSuppressed" in desktop_js
    assert "Workspace complete" in desktop_js
    assert "The latest output and workspace files are available from the status panel" in desktop_js
    assert "Clipboard sync: inactive until workspace resumes" in desktop_js
    assert "desktop.js?v=20260922brand1" in desktop_html
    assert "desktop.css?v=20260811m" in desktop_html
    assert "<style" not in desktop_html
    assert "#desktop-overlay" in desktop_css
    assert 'id="workspace-status-link"' in desktop_html
    assert "showWorkspaceLink: true" in desktop_js
    assert "Open workspace status and files" in desktop_html
    assert 'href="/static/styles.css?v=' in watch_html
    assert "}, 5000);" not in desktop_js
    assert 'id="project-files"' in index_html
    assert 'id="schedule-text"' in index_html
    assert 'id="schedule-button"' in index_html
    assert 'id="workspace-type"' in index_html
    assert 'id="provider-account-selection"' in index_html
    assert 'id="provider-account-policy"' in index_html
    assert "provider_account_id" in app_js
    assert "provider_account_policy" in app_js
    assert "renderLaunchProviderAccounts" in app_js
    assert 'Initial watch surface' not in index_html
    assert "renderWorkspaceTypeOptions" in app_js
    assert "idle_terminated" in app_js
    assert "stopped" in app_js
    assert "resumable" in app_js
    assert "Work complete" in watch_js
    assert "Workspace paused" in watch_js
    assert "Use Resume to continue from the same state" in watch_js
    assert "IDLE_REFRESH_MS" in watch_js
    assert "refreshInFlight" in watch_js
    assert "const GLASSHIVE_UI_REV = '20260811m'" in watch_js
    assert "const workspaceApiBase = `/api/workspace/${workerId}`" in watch_js
    assert "/api/worker/${workerId}/live" not in watch_js
    assert '@app.get("/api/workspace/{worker_id}/live")' in (Path(server_module.__file__).read_text(encoding="utf-8"))
    assert "const connecting = !completed && attachStartedAt" in watch_js
    assert "function filePreviewUrl()" in watch_js
    assert "function fileDeliverableKey(deliverable, runId)" in watch_js
    assert "function isFilePreviewUrl(url)" in watch_js
    assert "lastAttachedFilePreviewKey === filePreviewKey" in watch_js
    assert "if (!isFilePreviewUrl(url))" in watch_js
    assert "currentDeliverable?.kind === 'file'" in watch_js
    assert "syncResultActions(currentDeliverable)" in watch_js
    assert "link.download = '';" in watch_js
    assert "download.download = '';" in watch_js
    assert 'id="artifact-list"' in watch_html
    assert "function syncArtifactList(items)" in watch_js
    assert "function liveProgressText(data)" in watch_js
    assert "consolePayload.stdout || consolePayload.stderr" in watch_js
    assert "watchOutputModel" in watch_js
    assert "Live progress:" not in watch_js
    assert "gh_token|gh_sig|token|signature|sig" in watch_js
    assert "data.artifacts?.items || []" in watch_js
    assert "Workspace files" in watch_js
    assert 'src="/static/watch.js?v=' in watch_html
    assert ".artifact-row" in styles_css
    assert "artifact-list-more" in watch_js
    assert ".artifact-list-more" in styles_css
    assert 'aria-controls="result-panel"' in watch_html
    assert "result-toggle-action" in watch_html
    assert "`${verb} latest workspace ${kind}`" in watch_js
    assert ".result-toggle-action" in styles_css
    assert ".workspace-status-button" in styles_css
    assert "Open current desktop in new tab" in watch_js
    assert "if (previewUrl) return previewUrl" not in watch_js
    assert "setInterval(refresh" not in watch_js
    assert "Workspace resuming" not in watch_js
    assert 'grid-template-areas:' in styles_css
    assert '"brand controls"' in styles_css
    assert '.watch-meta-line p' in styles_css
    assert 'white-space: normal' in styles_css


def test_live_workspace_preview_cap_preserves_fourth_card_and_cleans_frames():
    static_dir = Path(server_module.__file__).parent / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    assert re.search(r"const MAX_VIEW_ONLY_PREVIEWS = 3;", app_js)
    assert "previewWorkerIds(" in app_js
    assert "MAX_VIEW_ONLY_PREVIEWS" in app_js
    assert "const alreadyHasFrame = Boolean(pane.querySelector('.workspace-live-preview'));" in app_js
    assert "workspace-live-preview" in app_js
    assert "pane.replaceChildren(note);" in app_js
    assert "pane.replaceChildren();" in app_js


def test_worker_lifecycle_endpoint_supports_workspace_hive_controls():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post("/api/worker/wrk_1/action/resume")

    assert response.status_code == 200
    assert response.json()["status"] == "resume"
    assert runtime.lifecycle_requests == [{"worker_id": "wrk_1", "action": "resume"}]


def test_launch_uses_desktop_surface_without_blocking_on_desktop_startup():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Create a hello world landing page and verify it renders in the browser',
        'success_criteria': 'The page is visible and renders HELLO WORLD',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert 'surface=desktop' in launch.json()['watch_url']
    assert runtime.desktop_actions == []


def test_launch_defers_explicit_external_navigation_to_the_queued_worker():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Open the browser to https://example.com and inspect the page',
        'success_criteria': 'The page is visible and the title is captured',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert 'surface=desktop' in launch.json()['watch_url']
    assert runtime.desktop_actions == []


def test_launch_never_enters_cold_desktop_startup_before_returning():
    class ColdDesktopRuntime(FakeRuntimeClient):
        def desktop_action(self, worker_id: str, action: str, url: str | None = None, run_id: str | None = None):
            raise AssertionError("desktop startup must remain outside the launch request")

    runtime = ColdDesktopRuntime()
    response = TestClient(create_app(runtime_client=runtime)).post('/api/launch', json={
        'description': 'Create and verify a browser-visible result',
        'workspace_option': 'new:codex-cli',
    })

    assert response.status_code == 200, response.text
    assert len(runtime.create_project_requests) == 1
    assert len(runtime.create_worker_requests) == 1
    assert len(runtime.assign_requests) == 1


def test_workspace_status_scripts_never_label_failed_terminal_runs_completed():
    static_dir = Path(server_module.STATIC_DIR)

    for script_name in ('app.js', 'watch.js'):
        script = (static_dir / script_name).read_text(encoding='utf-8')
        failed_branch = "if (['failed', 'cancelled', 'interrupted'].includes(runState)) return runState;"
        assert failed_branch in script
        ready_branch = "['paused', 'idle', 'idle_terminated', 'stopped', 'ready']"
        assert ready_branch in script
        assert "workerState === 'ready' ? 'completed' : workerState" not in script
        assert script.index(failed_branch) < script.index(ready_branch)

    app_script = (static_dir / 'app.js').read_text(encoding='utf-8')
    watch_script = (static_dir / 'watch.js').read_text(encoding='utf-8')
    desktop_script = (static_dir / 'desktop.js').read_text(encoding='utf-8')
    watch_markup = (static_dir / 'watch.html').read_text(encoding='utf-8')
    terminated_priority = "if (['terminating', 'termination_failed', 'terminated'].includes(workerState)) return workerState;"
    for script in (app_script, watch_script):
        assert terminated_priority in script
        assert script.index(terminated_priority) < script.index(failed_branch)
    assert "const TERMINAL_ATTENTION_STATES = new Set(['failed', 'cancelled', 'interrupted']);" in app_script
    assert "RESUME_STATES.has(normalized) || TERMINAL_ATTENTION_STATES.has(normalized)" not in app_script
    assert "const control = workspaceLifecycleControl(normalized === 'completed' ? normalized : workerState);" in app_script
    assert "toggle.hidden = control.hidden;" in app_script
    assert "function syncTileSteerAvailability(tile, state)" in app_script
    assert app_script.count("syncTileSteerAvailability(tile, state);") >= 2
    assert "tile.dataset.displayState = normalized" in app_script
    assert "syncTileSteerAvailability(tile, tile.dataset.displayState || state);" in app_script
    assert "Send a corrected follow-up below" in app_script
    assert "const TERMINAL_ATTENTION_STATES = new Set(['failed', 'cancelled', 'interrupted']);" in watch_script
    assert "const control = workspaceLifecycleControl(normalized);" in watch_script
    assert "runToggleButton.hidden = control.hidden;" in watch_script
    assert "syncRunToggle(displayState === 'completed' ? displayState : currentWorkerState);" in watch_script
    assert "TERMINAL_ATTENTION_STATES.has(state)" in watch_script
    assert "Workspace needs attention" in watch_script
    assert "then use Resume or send a corrected follow-up" not in watch_script
    assert "then send a corrected follow-up" in watch_script
    assert "steerInput.disabled = closed" in watch_script
    assert "sendButton.disabled = closed" in watch_script
    assert "This workspace is closed" in watch_script
    assert "if (normalized === 'terminating') return 'Closing';" in app_script
    assert "if (normalized === 'terminating') return 'Closing';" in watch_script
    assert "if (normalized === 'termination_failed') return 'Close needs attention';" in app_script
    assert "if (normalized === 'termination_failed') return 'Close needs attention';" in watch_script
    assert "data?.worker?.close_state || data?.worker?.state" in app_script
    assert "if (['terminating', 'termination_failed', 'terminated'].includes(state))" in app_script
    assert "Close needs attention" in app_script
    assert "let currentDisplayState = 'starting';" in watch_script
    assert "const state = currentDisplayState;" in watch_script
    assert "'terminating', 'termination_failed', 'terminated'].includes(currentDisplayState)" in watch_script
    assert "GlassHive will resume when you send more work" not in desktop_script
    assert "This workspace was closed" in desktop_script
    assert ">Close workspace</button>" in watch_markup


def test_browser_action_accepts_explicit_url():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/action/browser', json={'url': 'file:///workspace/project/index.html'})
    assert response.status_code == 200
    assert runtime.desktop_actions[-1] == {
        'worker_id': 'wrk_1',
        'action': 'browser',
        'url': 'file:///workspace/project/index.html',
        'run_id': None,
    }


def test_worker_action_surfaces_runtime_conflict_cleanly():
    class ConflictRuntime(FakeRuntimeClient):
        def desktop_action(self, worker_id: str, action: str, url: str | None = None, run_id: str | None = None):
            response = httpx.Response(409, json={"detail": "Workspace is not ready for browser action"})
            raise httpx.HTTPStatusError("conflict", request=httpx.Request("POST", "http://runtime.test"), response=response)

    client = TestClient(create_app(runtime_client=ConflictRuntime()))

    response = client.post('/api/worker/wrk_1/action/browser', json={'url': 'file:///workspace/project/index.html'})

    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace is not ready for browser action"


def test_launch_respects_explicit_terminal_surface_override():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': '',
        'workspace_option': 'new:codex-cli',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert 'surface=terminal' in launch.json()['watch_url']
    assert runtime.desktop_actions == []


def test_launch_failure_marks_new_worker_failed():
    runtime = FakeRuntimeClient()
    runtime.fail_assign = True
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 502
    assert runtime.launch_failures == [{'worker_id': 'wrk_new', 'reason': 'assign failed'}]


def test_launch_preserves_closed_workspace_conflict_for_existing_workspace():
    class ClosedWorkspaceRuntime(FakeRuntimeClient):
        def assign_run(self, worker_id: str, instruction: str):
            response = httpx.Response(
                409,
                json={"detail": "Workspace is closed; create a new workspace for new work"},
                request=httpx.Request("POST", f"http://runtime.test/v1/workers/{worker_id}/assign"),
            )
            raise httpx.HTTPStatusError("closed", request=response.request, response=response)

    runtime = ClosedWorkspaceRuntime()
    response = TestClient(create_app(runtime_client=runtime)).post('/api/launch', json={
        'description': 'Continue this workspace',
        'workspace_option': 'open:wrk_1',
    })

    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace is closed; create a new workspace for new work"
    assert runtime.launch_failures == []


def test_launch_duplicate_workspace_uses_capability_aware_runtime_duplicate_endpoint():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Branch the existing workspace for a parallel experiment',
        'success_criteria': 'The experiment starts in a duplicated workspace',
        'context': '',
        'workspace_option': 'duplicate:wrk_1',
        'idempotency_key': 'launch-duplicate-workspace-1',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_dup')
    assert runtime.workspace_duplicate_requests == [
        {
            'worker_id': 'wrk_1',
            'idempotency_key': 'launch-duplicate-workspace-1',
            'name': '',
        },
    ]
    assert runtime.duplicate_requests == []


def test_launch_duplicate_with_capabilities_pauses_for_reapproval_without_dispatch():
    class CapabilityDuplicateRuntime(FakeRuntimeClient):
        def duplicate_workspace(self, worker_id: str, *, idempotency_key: str, name: str = ""):
            result = super().duplicate_workspace(
                worker_id,
                idempotency_key=idempotency_key,
                name=name,
            )
            result["workspace"]["duplication_report"]["capabilities_requiring_reapproval"] = 2
            return result

    runtime = CapabilityDuplicateRuntime()
    response = TestClient(create_app(runtime_client=runtime)).post('/api/launch', json={
        'description': 'Branch the saved workspace without silently copying access',
        'workspace_option': 'duplicate:wrk_1',
        'idempotency_key': 'launch-capability-copy-1',
    })

    assert response.status_code == 200
    assert response.json()['status'] == 'action_required'
    assert response.json()['capabilities_requiring_reapproval'] == 2
    assert response.json()['worker_id'] == 'wrk_dup'
    assert runtime.assign_requests == []
    assert runtime.schedule_requests == []
    assert runtime.duplicate_requests == []


def test_launch_duplicate_requires_a_reusable_idempotency_key_before_mutation():
    runtime = FakeRuntimeClient()
    response = TestClient(create_app(runtime_client=runtime)).post('/api/launch', json={
        'description': 'Copy the saved workspace',
        'workspace_option': 'duplicate:wrk_1',
    })

    assert response.status_code == 422
    assert 'idempotency' in response.json()['detail'].lower()
    assert runtime.workspace_duplicate_requests == []
    assert runtime.assign_requests == []


def test_launch_duplicate_with_personal_account_reapproval_never_falls_back_or_dispatches():
    class PersonalAccountDuplicateRuntime(FakeRuntimeClient):
        def duplicate_workspace(self, worker_id: str, *, idempotency_key: str, name: str = ""):
            result = super().duplicate_workspace(worker_id, idempotency_key=idempotency_key, name=name)
            result["workspace"]["duplication_report"] = {
                "capabilities_requiring_reapproval": 1,
                "reapproval_items": [{
                    "kind": "provider_account",
                    "reference": "acct_synthetic",
                    "label": "Personal Codex",
                    "route": "connections",
                    "policy": "personal_required",
                    "scopes": [],
                }],
            }
            return result

    runtime = PersonalAccountDuplicateRuntime()
    response = TestClient(create_app(runtime_client=runtime)).post('/api/launch', json={
        'description': 'Copy the personal workspace safely',
        'workspace_option': 'duplicate:wrk_1',
        'idempotency_key': 'launch-personal-copy-1',
    })

    assert response.status_code == 200
    assert response.json()['status'] == 'action_required'
    assert response.json()['reapproval_items'][0]['kind'] == 'provider_account'
    assert runtime.assign_requests == []
    assert runtime.schedule_requests == []


def test_launch_duplicate_fails_closed_on_an_incomplete_runtime_copy():
    class IncompleteDuplicateRuntime(FakeRuntimeClient):
        def duplicate_workspace(self, worker_id: str, *, idempotency_key: str, name: str = ""):
            return {"project": {}, "workspace": {}}

    runtime = IncompleteDuplicateRuntime()
    response = TestClient(create_app(runtime_client=runtime)).post('/api/launch', json={
        'description': 'Copy the saved workspace',
        'workspace_option': 'duplicate:wrk_1',
        'idempotency_key': 'launch-incomplete-copy-1',
    })

    assert response.status_code == 502
    assert response.json()['detail'] == 'GlassHive returned an incomplete workspace copy'
    assert runtime.assign_requests == []
    assert runtime.schedule_requests == []


def test_saved_workspace_duplicate_is_one_click_and_uses_a_fresh_identity():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    missing_key = client.post('/api/workspaces/wrk_1/duplicate', json={})
    short_key = client.post('/api/workspaces/wrk_1/duplicate', json={'idempotency_key': 'short'})
    response = client.post(
        '/api/workspaces/wrk_1/duplicate',
        json={'idempotency_key': 'duplicate-browser-session-1'},
    )

    assert missing_key.status_code == 422
    assert short_key.status_code == 422
    assert response.status_code == 201
    assert response.json()['worker']['worker_id'] == 'wrk_dup'
    assert response.json()['worker']['duplication_report'] == {
        'capabilities_requiring_reapproval': 0,
    }
    assert runtime.workspace_duplicate_requests == [
        {
            'worker_id': 'wrk_1',
            'idempotency_key': 'duplicate-browser-session-1',
            'name': '',
        }
    ]


def test_copied_workspace_capability_skip_uses_the_human_confirmation_path_through_bff():
    runtime = FakeRuntimeClient()
    response = TestClient(create_app(runtime_client=runtime)).post(
        "/api/pending-changes",
        json={
            "change_type": "workspace_duplication_reapproval_waiver",
            "target_id": "wrk_dup",
            "payload": {"action_id": "rea_synthetic"},
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert runtime.pending_change_requests == [
        {
            "change_type": "workspace_duplication_reapproval_waiver",
            "target_id": "wrk_dup",
            "payload": {"action_id": "rea_synthetic"},
        }
    ]


def test_workspace_templates_list_save_and_instantiate_through_the_bff():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    listed = client.get('/api/workspace-templates')
    saved = client.post(
        '/api/workspaces/wrk_1/templates',
        json={'name': 'My research desk', 'description': 'Reusable intent only'},
    )
    started = client.post(
        '/api/workspace-templates/wst_synthetic/instantiate',
        json={'idempotency_key': 'template-attempt-0001', 'name': 'Fresh research desk'},
    )

    assert listed.status_code == 200
    assert listed.json()['items'][0]['template_id'] == 'wst_synthetic'
    assert saved.status_code == 201
    assert saved.json()['name'] == 'My research desk'
    assert started.status_code == 201
    assert {
        key: started.json()['workspace'][key]
        for key in ('worker_id', 'name', 'state')
    } == {
        'worker_id': 'wrk_template',
        'name': 'Fresh research desk',
        'state': 'paused',
    }
    assert started.json()['approvals_required'][0]['stable_id'] == 'skill.synthetic.summary'
    assert started.json()['workspace']['duplication_report']['outstanding_reapproval_items'][0] == {
        'stable_id': 'skill.synthetic.summary',
        'version': '1.0.0',
        'content_hash': 'sha256:library-synthetic',
        'scopes': ['documents:read'],
        'kind': 'library',
        'reference': 'skill.synthetic.summary@1.0.0',
        'route': 'library',
        'resolution': 'library_grant',
        'action_id': 'rea_synthetic',
    }
    assert runtime.workspace_template_requests == [
        {
            'action': 'save',
            'worker_id': 'wrk_1',
            'payload': {'name': 'My research desk', 'description': 'Reusable intent only'},
        },
        {
            'action': 'instantiate',
            'template_id': 'wst_synthetic',
            'payload': {'idempotency_key': 'template-attempt-0001', 'name': 'Fresh research desk'},
        },
    ]


def test_human_confirmation_page_loads_scoped_metadata_and_never_embeds_token():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    page = client.get('/confirm-change')
    metadata = client.get('/api/pending-changes/chg_1')

    assert page.status_code == 200
    assert 'Human approval required' in page.text
    assert 'confirmation_token' not in page.text
    assert metadata.status_code == 200
    assert metadata.json()['target_label'] == 'Main Worker'
    assert metadata.json()['capability_label'] == 'skill.synthetic.summary'
    assert runtime.pending_change_reads == ['chg_1']


def test_confirmation_script_keeps_token_in_fragment_or_session_storage_only():
    script = (Path(server_module.STATIC_DIR) / 'confirm.js').read_text(encoding='utf-8')
    page = (Path(server_module.STATIC_DIR) / 'confirm.html').read_text(encoding='utf-8')

    assert "window.location.hash" in script
    assert "history.replaceState(null, '', '/confirm-change')" in script
    assert "sessionStorage" in script
    assert "return_to=%2Fconfirm-change" in script
    assert "return_to=%2Fconfirm-change%23" not in script
    assert 'id="confirm-technical"' in page
    assert '>Technical details<' in page
    assert 'id="confirm-back"' in page
    assert "Back to workspace" in page
    assert "back.href = `/watch/${encodeURIComponent(String(pending.target_id || ''))}?surface=desktop`" in script


def test_launch_open_workspace_reuses_existing_worker():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Resume the existing workspace for another task',
        'success_criteria': 'The same workspace starts a new run',
        'context': '',
        'workspace_option': 'open:wrk_1',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_1')
    assert runtime.get_worker_requests == ['wrk_1']
    assert runtime.create_project_requests == []
    assert runtime.create_worker_requests == []
    assert runtime.duplicate_requests == []
    assert runtime.assign_requests[0]['worker_id'] == 'wrk_1'


def test_launch_accepts_legacy_worker_option_fallback():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Resume through the legacy worker option fallback',
        'success_criteria': 'The same workspace starts a new run',
        'context': '',
        'worker_option': 'open:wrk_1',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_1')
    assert runtime.create_project_requests == []
    assert runtime.assign_requests[0]['worker_id'] == 'wrk_1'


def test_novnc_proxy_uses_worker_view_origin(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "ok";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            assert url == 'http://127.0.0.1:60812/core/rfb.js'
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    response = client.get('/novnc/wrk_1/core/rfb.js')
    assert response.status_code == 200
    assert response.text == 'export default "ok";'


def test_novnc_proxy_caches_authorized_view_origin_and_static_assets(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    requested_urls = []

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "cached";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url: str):
            requested_urls.append(url)
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)

    first = client.get('/novnc/wrk_1/core/rfb.js')
    second = client.get('/novnc/wrk_1/core/rfb.js')

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.text == second.text == 'export default "cached";'
    # Runtime authority is rechecked even when the static asset is cached so a close cannot
    # reuse a stale desktop URL.
    assert runtime.worker_live_requests == ['wrk_1', 'wrk_1']
    assert requested_urls == ['http://127.0.0.1:60812/core/rfb.js']


@pytest.mark.parametrize("state", ["terminating", "termination_failed", "terminated"])
def test_closed_workspace_rejects_cached_desktop_credentials_assets_and_websocket(state):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    # Prime the origin cache while the workspace is usable, then close it.
    assert client.get('/api/workspace/wrk_1/desktop-credentials').status_code == 200
    runtime.worker_state = state

    credentials = client.get('/api/workspace/wrk_1/desktop-credentials')
    asset = client.get('/novnc/wrk_1/core/rfb.js')
    assert credentials.status_code == 409
    assert asset.status_code == 409
    assert "closed" in credentials.json()["detail"].lower()
    assert "closed" in asset.json()["detail"].lower()
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect('/novnc/wrk_1/websockify'):
            pass
    assert disconnected.value.code == 1008


def test_signed_watch_token_authenticates_runtime_calls(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    response = client.get(f'/api/worker/wrk_1/live?gh_token={token}')

    assert response.status_code == 200
    assert runtime.header_contexts[-1]["X-WPR-Token"] == secret
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "viewer"


def test_signed_watch_token_allows_only_read_and_narrow_communication(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token(secret)

    assert client.get(f"/api/worker/wrk_1/live?gh_token={token}").status_code == 200
    assert client.post(
        f"/api/worker/wrk_1/message?gh_token={token}",
        json={"message": "Share a concise progress update."},
    ).status_code == 200
    assert client.post(
        f"/api/worker/wrk_1/steer?gh_token={token}",
        json={"message": "Focus on the requested output."},
    ).status_code == 200

    forbidden = (
        client.patch(
            f"/api/worker/wrk_1/metadata?gh_token={token}",
            json={"favorite": True},
        ),
        client.post(f"/api/worker/wrk_1/action/pause?gh_token={token}"),
        client.post(f"/api/worker/wrk_1/action/terminate?gh_token={token}"),
    )
    assert [response.status_code for response in forbidden] == [403, 403, 403]
    assert runtime.metadata_requests == []
    assert runtime.lifecycle_requests == []
    assert runtime.message_requests == [
        {"worker_id": "wrk_1", "message": "Share a concise progress update."}
    ]
    assert runtime.steer_requests == [
        {"worker_id": "wrk_1", "message": "Focus on the requested output."}
    ]
    assert all(
        headers.get("X-Viventium-User-Role") == "viewer"
        for headers in runtime.header_contexts
    )


def test_signed_watch_token_cannot_open_interactive_desktop_or_credentials(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token(secret)

    assert client.get(f"/desktop/wrk_1?gh_token={token}").status_code == 403
    credentials = client.get(f"/api/workspace/wrk_1/desktop-credentials?gh_token={token}")
    assert credentials.status_code == 403
    assert "password" not in credentials.text.lower()
    assert client.get(f"/novnc/wrk_1/core/rfb.js?gh_token={token}").status_code == 403
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(f"/novnc/wrk_1/websockify?gh_token={token}"):
            pass
    assert denied.value.code == 1008
    assert runtime.worker_live_requests == []


def test_bootstrap_signs_workspace_links_in_enterprise_mode(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    headers = {
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }

    response = client.get("/api/bootstrap", headers=headers)

    assert response.status_code == 200
    workspace = response.json()["existing_workspaces"][0]
    watch_ref = worker_ref_record(workspace["watch_url"])
    workspace_ref = worker_ref_record(workspace["workspace_url"])
    project_ref = worker_ref_record(workspace["project_url"])
    desktop_ref = worker_ref_record(workspace["desktop_url"])
    desktop_preview_ref = worker_ref_record(workspace["desktop_preview_url"])
    api_ref = worker_ref_record(workspace["api_url"])
    assert str(watch_ref["target_url"]).startswith("/watch/wrk_1?")
    assert workspace["workspace_url"] == workspace["watch_url"]
    assert str(workspace_ref["target_url"]).startswith("/watch/wrk_1?")
    assert str(project_ref["target_url"]).startswith("/ui/projects/prj_1?")
    assert str(desktop_ref["target_url"]) == "/desktop/wrk_1"
    assert str(desktop_preview_ref["target_url"]) == "/desktop/wrk_1?preview=1"
    assert str(api_ref["target_url"]) == "/api/worker/wrk_1"
    assert workspace["control_url"] == "/api/worker/wrk_1"
    assert client.get(workspace["watch_url"], headers=headers).status_code == 200
    assert client.get(workspace["desktop_url"], headers=headers).status_code == 200
    preview_redirect = client.get(
        workspace["desktop_preview_url"],
        headers=headers,
        follow_redirects=False,
    )
    assert preview_redirect.status_code == 307
    assert preview_redirect.headers["location"] == "/desktop/wrk_1?preview=1"


def test_signed_watch_token_is_worker_scoped(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret, worker_id="wrk_other")
    response = client.get(f'/api/worker/wrk_1/live?gh_token={token}')

    assert response.status_code == 403
    assert runtime.header_contexts == []


def test_signed_watch_token_is_worker_scoped_for_control_and_desktop_routes(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret, worker_id="wrk_other")
    probes = [
        ("post", "/api/worker/wrk_1/steer", {"message": "do not cross workers"}),
        ("post", "/api/worker/wrk_1/message", {"message": "do not cross workers"}),
        ("post", "/api/worker/wrk_1/action/pause", None),
        ("post", "/api/worker/wrk_1/action/resume", None),
        ("post", "/api/worker/wrk_1/action/interrupt", None),
        ("post", "/api/worker/wrk_1/action/terminate", None),
        ("get", "/desktop/wrk_1", None),
        ("get", "/api/workspace/wrk_1/desktop-credentials", None),
        ("get", "/novnc/wrk_1/core/rfb.js", None),
    ]

    for method, path, body in probes:
        request = getattr(client, method)
        response = request(f"{path}?gh_token={token}", json=body) if body is not None else request(f"{path}?gh_token={token}")
        assert response.status_code == 403, path

    assert runtime.header_contexts == []


def test_signed_worker_view_allows_only_narrow_communication_after_cookie_bootstrap(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token(secret)

    # The GET is a valid read-only watch bootstrap and stores the scoped token
    # in an HttpOnly cookie. Neither the explicit bearer nor that cookie may be
    # promoted into a mutable member session.
    assert client.get(f"/watch/wrk_1?gh_token={token}").status_code == 200
    probes = [
        ("post", "/api/worker/wrk_1/metadata", {"favorite": True}),
        ("post", "/api/worker/wrk_1/action/pause", None),
        ("post", "/api/worker/wrk_1/action/resume", None),
        ("post", "/api/worker/wrk_1/action/interrupt", None),
        ("post", "/api/worker/wrk_1/action/terminate", None),
        ("post", "/api/worker/wrk_1/action/browser", {"url": "https://example.test"}),
        ("post", "/v1/workers/wrk_1/pause", None),
        ("patch", "/ui/workers/wrk_1", {"favorite": True}),
    ]
    for method, path, body in probes:
        request = getattr(client, method)
        response = request(path, json=body) if body is not None else request(path)
        assert response.status_code == 403, path

    assert client.post(
        "/api/worker/wrk_1/message",
        json={"message": "Share progress."},
    ).status_code == 200
    assert client.post(
        "/api/worker/wrk_1/steer",
        json={"message": "Focus on the requested result."},
    ).status_code == 200
    assert runtime.steer_requests == [
        {"worker_id": "wrk_1", "message": "Focus on the requested result."}
    ]
    assert runtime.message_requests == [
        {"worker_id": "wrk_1", "message": "Share progress."}
    ]
    assert runtime.metadata_requests == []
    assert runtime.lifecycle_requests == []
    assert runtime.desktop_actions == []


def test_signed_worker_view_cannot_open_interactive_novnc_websocket(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    token = signed_worker_token(secret)

    with pytest.raises(WebSocketDisconnect) as captured:
        with client.websocket_connect(f"/novnc/wrk_1/websockify?gh_token={token}"):
            pass

    assert captured.value.code == 1008


def test_runtime_proxy_strips_signed_query_params_before_upstream(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    token = signed_worker_token(signed_secret)
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"{}"
        headers = {"content-type": "application/json"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(
        f"/v1/workers/wrk_1/live?worker_id=wrk_1&gh_token={token}&gh_kind=worker_view&gh_exp=123&gh_sig=abc&path=outputs%2Freport.txt"
    )

    assert response.status_code == 200
    assert "gh_token" not in captured["url"]
    assert "gh_kind" not in captured["url"]
    assert "gh_exp" not in captured["url"]
    assert "gh_sig" not in captured["url"]
    assert captured["url"] == "http://runtime.test/v1/workers/wrk_1/live?worker_id=wrk_1&path=outputs%2Freport.txt"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"


def test_operator_work_view_proxies_exact_opaque_ref_to_configured_runtime(monkeypatch):
    runtime = FakeRuntimeClient()
    captured = {}
    signed_secret = "ui-signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", signed_secret)
    ref_id = create_signed_link_ref(token=signed_worker_token(signed_secret))

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html><body>Read-only mission view</body></html>"
        headers = {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        }

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update(
                {
                    "method": method,
                    "url": url,
                    "headers": headers or {},
                    "content": content,
                }
            )
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(f"/w/{ref_id}?gh_token=must-not-be-forwarded")

    assert response.status_code == 200
    assert response.text == "<html><body>Read-only mission view</body></html>"
    assert captured["method"] == "GET"
    assert captured["url"] == f"http://runtime.test/w/{ref_id}"
    assert captured["client_kwargs"]["follow_redirects"] is False
    assert "X-WPR-Token" not in captured["headers"]
    assert "X-Viventium-User-Id" not in captured["headers"]
    assert runtime.lifecycle_requests == []


def test_enterprise_work_view_preserves_security_and_authorizes_artifact_click(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(
        monkeypatch,
        service_secret=service_secret,
        signed_secret=signed_secret,
    )
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    worker_token = signed_worker_token(signed_secret)
    worker_ref = create_signed_link_ref(token=worker_token)
    artifact_token = signed_artifact_token(signed_secret, kind="artifact_open")
    artifact_ref = create_signed_link_ref(token=artifact_token)
    captured = []

    class FakeUpstreamResponse:
        def __init__(self, *, content, content_type, headers=None):
            self.status_code = 200
            self.content = content
            self.headers = {"content-type": content_type, **(headers or {})}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.append({"method": method, "url": url, "headers": headers or {}})
            if f"/w/{worker_ref}" in url:
                return FakeUpstreamResponse(
                    content=f'<a href="/v1/link-refs/{artifact_ref}">Open</a>'.encode(),
                    content_type="text/html; charset=utf-8",
                    headers={
                        "cache-control": "no-store",
                        "pragma": "no-cache",
                        "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
                        "referrer-policy": "no-referrer",
                        "x-content-type-options": "nosniff",
                    },
                )
            return FakeUpstreamResponse(
                content=b"artifact bytes",
                content_type="text/plain",
            )

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    identity_headers = {
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "user-a",
        "X-GlassHive-User-Role": "member",
    }

    view = client.get(f"/w/{worker_ref}", headers=identity_headers)

    assert view.status_code == 200
    assert view.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert view.headers["pragma"] == "no-cache"
    assert f"{worker_cookie_name('wrk_1')}=" in view.headers["set-cookie"]
    assert captured[0]["headers"]["X-WPR-Token"] == service_secret
    assert captured[0]["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured[0]["headers"]["X-Viventium-User-Id"] == "user-a"

    artifact = client.get(f"/v1/link-refs/{artifact_ref}")

    assert artifact.status_code == 200
    assert artifact.content == b"artifact bytes"
    assert captured[1]["headers"]["X-WPR-Token"] == service_secret
    assert captured[1]["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured[1]["headers"]["X-Viventium-User-Id"] == "user-a"


@pytest.mark.parametrize(
    "ref_id",
    [
        "not-a-view-ref",
        "ghr_short",
        "ghr_1234567890abcdef12345678%2Factions",
    ],
)
def test_operator_work_view_rejects_invalid_opaque_ref_before_upstream(monkeypatch, ref_id):
    class UnexpectedAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("invalid work-view refs must not reach the runtime")

    monkeypatch.setattr(server_module.httpx, "AsyncClient", UnexpectedAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/w/{ref_id}")

    assert response.status_code in {400, 404}


def test_operator_work_view_preserves_runtime_invalid_ref_response(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", signed_secret)
    ref_id = create_signed_link_ref(token=signed_worker_token(signed_secret))

    class FakeUpstreamResponse:
        status_code = 401
        content = b'{"detail":"Invalid or expired GlassHive workspace link"}'
        headers = {"content-type": "application/json"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/w/{ref_id}")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired GlassHive workspace link"


def test_operator_work_view_never_proxies_mutations(monkeypatch):
    ref_id = "ghr_1234567890abcdef12345678"

    class UnexpectedAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("work-view mutations must not reach the runtime")

    monkeypatch.setattr(server_module.httpx, "AsyncClient", UnexpectedAsyncClient)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    action = client.post(f"/w/{ref_id}/actions/terminate")
    desktop = client.post(f"/w/{ref_id}/desktop-action", json={"action": "terminal"})
    direct = client.post(f"/w/{ref_id}")

    assert action.status_code in {404, 405}
    assert desktop.status_code in {404, 405}
    assert direct.status_code == 405
    assert runtime.lifecycle_requests == []
    assert runtime.desktop_actions == []


def test_operator_work_view_does_not_expose_ref_in_upstream_redirect(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", signed_secret)
    ref_id = create_signed_link_ref(token=signed_worker_token(signed_secret))

    class FakeUpstreamResponse:
        status_code = 307
        content = b""
        headers = {"location": f"http://runtime.test/w/{ref_id}"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/w/{ref_id}", follow_redirects=False)

    assert response.status_code == 502
    assert "location" not in response.headers
    assert ref_id not in response.text


def test_novnc_submodule_imports_can_inherit_signed_token_from_referer(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "ok";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            assert url == 'http://127.0.0.1:60812/core/util/int.js'
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    token = signed_worker_token(secret)
    response = client.get(
        '/novnc/wrk_1/core/util/int.js',
        headers={'referer': f'http://glasshive.example.test/novnc/wrk_1/core/rfb.js?gh_token={token}'},
    )

    assert response.status_code == 403
    assert runtime.header_contexts == []


def test_signed_watch_sets_worker_scoped_cookie(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    response = client.get(f'/watch/wrk_1?gh_token={token}')

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert f"{worker_cookie_name('wrk_1')}=" in set_cookie
    assert "glasshive_gh_token_wrk_1=" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert runtime.lifecycle_requests == []


def test_short_worker_view_ref_stays_read_only_when_legacy_auto_resume_is_configured(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_WORKSPACE_LINK_AUTO_RESUME", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    target_url = f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}"
    ref_id = create_signed_link_ref(token=token, target_url=target_url)

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in response.headers["location"]
    assert runtime.worker_view_open_requests == ["wrk_1"]
    assert runtime.lifecycle_requests == []


def test_short_worker_view_ref_redirects_and_sets_worker_cookie(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    target_url = f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}"
    ref_id = create_signed_link_ref(token=token, target_url=target_url)

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in response.headers["location"]
    assert f"/r/{ref_id}" not in target_url
    set_cookie = response.headers["set-cookie"]
    assert f"{worker_cookie_name('wrk_1')}=" in set_cookie
    assert "glasshive_gh_token_wrk_1=" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie


@pytest.mark.parametrize("failure_kind, expected_status", [("terminated", 404), ("unavailable", 503)])
def test_short_worker_view_ref_requires_authoritative_runtime_acceptance(
    tmp_path,
    monkeypatch,
    failure_kind,
    expected_status,
):
    secret = "ui-signed-link-secret"
    mismatched_runtime_db = tmp_path / "ambient" / "wrong-runtime.db"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("WPR_DB_PATH", str(mismatched_runtime_db))

    class RejectingRuntime(FakeRuntimeClient):
        def record_worker_view_open(self, worker_id: str):
            self.worker_view_open_requests.append(worker_id)
            request = httpx.Request("POST", f"http://runtime.test/v1/workers/{worker_id}/view-opened")
            if failure_kind == "unavailable":
                raise httpx.ConnectError("synthetic runtime unavailable", request=request)
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("synthetic terminated worker", request=request, response=response)

    runtime = RejectingRuntime()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token(secret)
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}",
    )

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == expected_status
    assert "location" not in response.headers
    assert "set-cookie" not in response.headers
    assert runtime.worker_view_open_requests == ["wrk_1"]
    assert not mismatched_runtime_db.exists()


def test_direct_worker_view_token_requires_authoritative_runtime_acceptance(
    tmp_path,
    monkeypatch,
):
    secret = "ui-signed-link-secret"
    mismatched_runtime_db = tmp_path / "ambient" / "wrong-runtime.db"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("WPR_DB_PATH", str(mismatched_runtime_db))

    class UnavailableRuntime(FakeRuntimeClient):
        def record_worker_view_open(self, worker_id: str):
            self.worker_view_open_requests.append(worker_id)
            request = httpx.Request("POST", f"http://runtime.test/v1/workers/{worker_id}/view-opened")
            raise httpx.ConnectError("synthetic runtime unavailable", request=request)

    runtime = UnavailableRuntime()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(f"/watch/wrk_1?gh_token={signed_worker_token(secret)}")

    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert runtime.worker_view_open_requests == ["wrk_1"]
    assert not mismatched_runtime_db.exists()


def test_short_worker_view_ref_rejects_unconfigured_absolute_redirect_target(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"https://unexpected.example.test/watch/wrk_1?gh_token={token}",
    )

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 403
    assert "target is not allowed" in response.text


@pytest.mark.parametrize(
    "target_url",
    [
        "//unexpected.example.test/watch/wrk_1",
        "////unexpected.example.test/watch/wrk_1",
        r"/\unexpected.example.test/watch/wrk_1",
    ],
)
def test_short_worker_view_ref_rejects_relative_redirect_bypass_targets(monkeypatch, target_url):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    ref_id = create_signed_link_ref(token=token, target_url=target_url)

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 400
    assert "target path is not allowed" in response.text


def test_short_worker_view_ref_allows_explicit_redirect_host_allowlist(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_REDIRECT_HOSTS", "allowed.example.test, https://other.example.test")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"https://allowed.example.test/watch/wrk_1?surface=desktop&gh_token={token}",
    )

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://allowed.example.test/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in response.headers["location"]


def test_enterprise_short_worker_view_ref_bootstraps_direct_link_and_rechecks_asserted_owner(monkeypatch):
    secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "300")
    now = {"value": 1_000}
    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}",
    )
    assert ref_id

    now["value"] = 2_000
    direct_response = client.get(f"/r/{ref_id}", follow_redirects=False)
    assert direct_response.status_code == 401
    assert "authenticated user assertion" in direct_response.text
    assert runtime.worker_view_open_requests == []
    wrong_user = {
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "user-b",
        "X-GlassHive-User-Email": "user-a",
        "X-GlassHive-User-Role": "member",
    }
    assert client.get(f"/r/{ref_id}", headers=wrong_user, follow_redirects=False).status_code == 404
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id,email")
    email_claim_response = client.get(f"/r/{ref_id}", headers=wrong_user, follow_redirects=False)
    assert email_claim_response.status_code == 307
    assert email_claim_response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id")
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", json.dumps({"user-a": ["user-b"]}))
    alias_response = client.get(f"/r/{ref_id}", headers=wrong_user, follow_redirects=False)
    assert alias_response.status_code == 307
    assert alias_response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    wrong_tenant = {**wrong_user, "X-GlassHive-Tenant-Id": "tenant-beta"}
    assert client.get(f"/r/{ref_id}", headers=wrong_tenant, follow_redirects=False).status_code in {401, 404}
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", json.dumps({"*": ["user-b"]}))
    assert client.get(f"/r/{ref_id}", headers=wrong_user, follow_redirects=False).status_code == 404
    monkeypatch.delenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", raising=False)
    response = client.get(
        f"/r/{ref_id}",
        headers={
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-User-Role": "member",
        },
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    set_cookie = response.headers["set-cookie"]
    cookie_value = set_cookie.split(f"{worker_cookie_name('wrk_1')}=", 1)[1].split(";", 1)[0]
    assert cookie_value != token
    refreshed_payload = server_module.verify_signed_link_token(cookie_value)
    assert refreshed_payload is not None
    assert refreshed_payload["worker_id"] == "wrk_1"
    assert refreshed_payload["owner_id"] == "user-a"
    assert runtime.worker_view_open_requests == ["wrk_1", "wrk_1", "wrk_1"]
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"


def test_ui_resolves_runtime_created_short_ref_when_state_path_is_shared(monkeypatch):
    secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime_signed_links = load_runtime_signed_links_module()
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = runtime_signed_links.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = runtime_signed_links.create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}",
    )
    assert ref_id

    response = client.get(
        f"/r/{ref_id}",
        headers={
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-User-Role": "member",
        },
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in response.headers["location"]


def test_short_worker_view_ref_ttl_config_can_expire_refs(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    monkeypatch.setenv("GLASSHIVE_LINK_REF_TTL_SECONDS", "30")
    now = {"value": 1_000}
    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = create_signed_link_ref(token=token, target_url=f"http://testserver/watch/wrk_1?gh_token={token}")
    record = resolve_signed_link_ref(ref_id)
    assert record is not None
    assert record["expires_at"] == 1_030

    now["value"] = 1_031
    response = client.get(f"/r/{ref_id}", follow_redirects=False)
    assert response.status_code == 401


def test_ui_sensitive_url_log_filter_redacts_signed_tokens():
    view_ref = "ghr_1234567890abcdef12345678"
    raw = (
        'GET /novnc/wrk_1/websockify?gh_token=secret-token&gh_sig=signature&gh_exp=123 '
        f'GET /v1/signed-links/opaque-token?download=1 GET /w/{view_ref}'
    )

    cookie = f"Set-Cookie: {worker_cookie_name('wrk_1')}=worker-secret; HttpOnly; SameSite=lax"

    assert redact_sensitive_url_text(f"{raw} {cookie}") == (
        'GET /novnc/wrk_1/websockify?gh_token=[redacted]&gh_sig=[redacted]&gh_exp=[redacted] '
        'GET /v1/signed-links/[redacted]?download=1 GET /w/[redacted] '
        f"Set-Cookie: {worker_cookie_name('wrk_1')}=[redacted]; HttpOnly; SameSite=lax"
    )
    assert redact_sensitive_url_text("gh_sig=signature&gh_token=secret-token") == (
        "gh_sig=[redacted]&gh_token=[redacted]"
    )
    oidc_callback = (
        "/auth/oidc/callback?code=authorization-secret&state=state-secret"
        "&error_description=private-provider-detail&error=access_denied"
    )
    oidc_redacted = redact_sensitive_url_text(oidc_callback)
    assert "authorization-secret" not in oidc_redacted
    assert "state-secret" not in oidc_redacted
    assert "private-provider-detail" not in oidc_redacted
    assert "error=access_denied" in oidc_redacted
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="%s",
        args=(f"{raw} {cookie}",),
        exc_info=None,
    )
    assert SensitiveUrlLogFilter().filter(record) is True
    assert "secret-token" not in record.args[0]
    assert "opaque-token" not in record.args[0]
    assert view_ref not in record.args[0]
    assert "worker-secret" not in record.args[0]
    assert "gh_token=[redacted]" in record.args[0]
    assert f"{worker_cookie_name('wrk_1')}=[redacted]" in record.args[0]

    class UrlLike:
        def __str__(self) -> str:
            return "http://runtime.test/v1/link-refs/ghr_1234567890123456"

    structured_record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="HTTP Request: GET %s",
        args=(UrlLike(),),
        exc_info=None,
    )
    assert SensitiveUrlLogFilter().filter(structured_record) is True
    assert structured_record.getMessage() == "HTTP Request: GET http://runtime.test/v1/link-refs/[redacted]"


def test_ui_static_pages_do_not_load_third_party_fonts():
    static_root = Path(__file__).parents[1] / "src" / "glass_drive_ui" / "static"

    for filename in ("index.html", "watch.html", "styles.css", "desktop.html"):
        content = (static_root / filename).read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in content
        assert "fonts.gstatic.com" not in content


def test_ui_sensitive_url_log_filter_installs_for_child_loggers(caplog):
    install_sensitive_url_log_filter()
    raw = "https://glasshive.example.test/watch/wrk_1?gh_token=secret-token&gh_sig=signature"
    logger = logging.getLogger("glass_drive_ui.server")

    with caplog.at_level(logging.INFO, logger="glass_drive_ui.server"):
        logger.info("opening %s", raw, extra={"target_url": raw})

    assert "secret-token" not in caplog.text
    assert "gh_token=[redacted]" in caplog.text
    assert caplog.records
    assert getattr(caplog.records[-1], "target_url") == (
        "https://glasshive.example.test/watch/wrk_1?gh_token=[redacted]&gh_sig=[redacted]"
    )


def test_signed_watch_does_not_set_cookie_for_different_worker(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret, worker_id="wrk_other")
    response = client.get(f'/watch/wrk_1?gh_token={token}')

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_novnc_submodule_imports_reject_signed_token_from_cookie(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "ok";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            assert url == 'http://127.0.0.1:60812/core/input/util.js'
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    token = signed_worker_token(secret)
    client.cookies.set(worker_cookie_name("wrk_1"), token)
    response = client.get('/novnc/wrk_1/core/input/util.js')

    assert response.status_code == 403
    assert runtime.header_contexts == []


def test_novnc_rejects_invalid_asset_path():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get('/novnc/wrk_1/core/%5Cbad.js')

    assert response.status_code == 400


def test_novnc_proxy_handles_upstream_transport_error(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            raise httpx.ConnectError("upstream unavailable")

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    response = client.get('/novnc/wrk_1/core/rfb.js')

    assert response.status_code == 502


def test_unsigned_inbound_identity_headers_are_ignored_by_default(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_DEFAULT_OWNER_ID", "default-owner")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        '/api/bootstrap',
        headers={
            "X-Viventium-User-Id": "forged-user",
            "X-Viventium-User-Role": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["owner_id"] == "default-owner"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "default-owner"
    assert "X-Viventium-User-Role" not in runtime.header_contexts[-1]


def test_enterprise_ui_requires_service_token_at_startup(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")

    with pytest.raises(RuntimeError, match="requires WPR_API_TOKEN"):
        create_app(runtime_client=FakeRuntimeClient())


def test_public_links_only_ui_requires_signed_link_secret_at_startup(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PUBLIC_LINKS_ONLY", "true")

    with pytest.raises(RuntimeError, match="public link mode requires GLASSHIVE_SIGNED_LINK_SECRET"):
        create_app(runtime_client=FakeRuntimeClient())


def test_public_links_only_ui_rejects_operator_surfaces_without_signed_session(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PUBLIC_LINKS_ONLY", "true")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-link-secret")
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    assert client.get("/docs").status_code == 404
    assert client.get("/").status_code == 401
    assert client.get("/api/bootstrap").status_code == 401
    assert client.get("/watch/wrk_1").status_code == 401
    assert client.get("/v1/workers/wrk_1").status_code == 404


def test_public_links_only_worker_ref_opens_scoped_watch_session(monkeypatch):
    secret = "public-link-secret"
    monkeypatch.setenv("GLASSHIVE_PUBLIC_LINKS_ONLY", "true")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    token = signed_worker_token(secret)
    ref_id = create_signed_link_ref(
        token=token,
        target_url="/watch/wrk_1?project_id=prj_1&surface=desktop",
    )
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("/watch/wrk_1?")
    assert client.get(response.headers["location"]).status_code == 200


def test_public_links_only_artifact_ref_is_bearer_but_raw_token_route_is_closed(monkeypatch):
    secret = "public-link-secret"
    monkeypatch.setenv("GLASSHIVE_PUBLIC_LINKS_ONLY", "true")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    token = signed_artifact_token(secret, kind="artifact_open")
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/v1/signed-links/{token}",
    )
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html><body>artifact preview</body></html>"
        headers = {"content-type": "text/html; charset=utf-8"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/v1/link-refs/{ref_id}")

    assert response.status_code == 200
    assert captured["url"] == f"http://runtime.test/v1/link-refs/{ref_id}"
    assert client.get(f"/v1/signed-links/{token}").status_code == 404


def test_enterprise_ui_requires_signed_link_secret_at_startup(monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    with pytest.raises(RuntimeError, match="requires GLASSHIVE_SIGNED_LINK_SECRET"):
        create_app(runtime_client=FakeRuntimeClient())


def test_enterprise_ui_requires_signed_link_secret_distinct_from_service_token(monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "same-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "same-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    with pytest.raises(RuntimeError, match="differ from WPR_API_TOKEN"):
        create_app(runtime_client=FakeRuntimeClient())


def test_enterprise_ui_rejects_invalid_owner_identity_config_at_startup(monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id,role")

    with pytest.raises(RuntimeError, match="only supports"):
        create_app(runtime_client=FakeRuntimeClient())

    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id")
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", "[]")

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        create_app(runtime_client=FakeRuntimeClient())

    monkeypatch.delenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", raising=False)
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_FILE", "/tmp/glasshive-missing-aliases.json")

    with pytest.raises(RuntimeError, match="could not be read"):
        create_app(runtime_client=FakeRuntimeClient())


def test_enterprise_bootstrap_requires_authenticated_user_assertion(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get('/api/bootstrap')

    assert response.status_code == 401
    assert "authenticated user assertion" in response.json()["detail"]
    assert runtime.header_contexts == []


def test_enterprise_ui_disables_builtin_openapi_docs(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/").status_code == 401
    assert client.get("/watch/wrk_1").status_code == 401
    assert client.get("/desktop/wrk_1").status_code == 401


def test_enterprise_ui_static_shells_require_trusted_identity(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    headers = {
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "operator",
    }

    assert client.get("/", headers=headers).status_code == 200
    assert client.get("/watch/wrk_1", headers=headers).status_code == 200
    assert client.get("/desktop/wrk_1", headers=headers).status_code == 200


def test_enterprise_watch_shell_accepts_signed_worker_link(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(signed_secret)

    assert client.get(f"/watch/wrk_1?gh_token={token}").status_code == 200
    assert client.get(f"/desktop/wrk_1?gh_token={token}").status_code == 403


def test_viewer_identity_is_read_only_and_cannot_use_signed_link_as_privilege_escalation(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token(signed_secret)
    headers = {
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "viewer",
    }

    assert client.get(f"/api/worker/wrk_1/live?gh_token={token}", headers=headers).status_code == 200
    assert client.post(
        f"/api/worker/wrk_1/message?gh_token={token}",
        headers=headers,
        json={"message": "This viewer must remain read-only."},
    ).status_code == 403
    assert client.post(
        f"/api/worker/wrk_1/action/pause?gh_token={token}",
        headers=headers,
    ).status_code == 403
    assert client.get(f"/desktop/wrk_1?gh_token={token}", headers=headers).status_code == 403
    assert runtime.message_requests == []
    assert runtime.lifecycle_requests == []
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "viewer"


def test_enterprise_signed_worker_link_is_tenant_scoped(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(signed_secret, tenant_id="tenant-beta")

    assert client.get(f"/watch/wrk_1?gh_token={token}").status_code == 401
    assert client.get(f"/desktop/wrk_1?gh_token={token}").status_code == 401
    assert client.get(f"/api/worker/wrk_1/live?gh_token={token}").status_code == 401
    assert runtime.header_contexts == []


def test_enterprise_signed_artifact_link_proxies_without_user_assertion(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    token = signed_artifact_token(signed_secret)
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"artifact bytes"
        headers = {
            "content-type": "text/plain",
            "content-disposition": 'attachment; filename="report.txt"',
        }

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/v1/signed-links/{token}")

    assert response.status_code == 200, response.text
    assert response.content == b"artifact bytes"
    assert response.headers["content-disposition"] == 'attachment; filename="report.txt"'
    assert captured["url"] == f"http://runtime.test/v1/signed-links/{token}"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"
    assert captured["headers"]["X-Viventium-User-Role"] == "viewer"


def test_enterprise_signed_artifact_open_link_proxies_without_user_assertion(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    token = signed_artifact_token(signed_secret, kind="artifact_open")
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html><body>artifact preview</body></html>"
        headers = {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store, no-cache, private, max-age=0",
        }

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/v1/signed-links/{token}")

    assert response.status_code == 200
    assert b"artifact preview" in response.content
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store, no-cache, private, max-age=0"
    assert captured["url"] == f"http://runtime.test/v1/signed-links/{token}"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"
    assert captured["headers"]["X-Viventium-User-Role"] == "viewer"


def test_enterprise_artifact_link_ref_uses_worker_cookie_identity(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"artifact bytes"
        headers = {"content-type": "text/plain"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    app = create_app(runtime_client=FakeRuntimeClient())
    artifact_token = signed_artifact_token(signed_secret, kind="artifact_open")
    artifact_ref_id = create_signed_link_ref(
        token=artifact_token,
        target_url=f"http://testserver/v1/signed-links/{artifact_token}",
    )

    unauthenticated_client = TestClient(app)
    assert unauthenticated_client.get(f"/v1/link-refs/{artifact_ref_id}").status_code == 401
    assert captured == {}

    authenticated_client = TestClient(app)
    worker_token = signed_worker_token(signed_secret)
    worker_ref_id = create_signed_link_ref(
        token=worker_token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={worker_token}",
    )
    short_response = authenticated_client.get(
        f"/r/{worker_ref_id}",
        headers={
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-User-Role": "member",
        },
        follow_redirects=False,
    )
    assert short_response.status_code == 307
    set_cookie = short_response.headers["set-cookie"]
    assert f"{worker_cookie_name('wrk_1')}=" in set_cookie
    assert "glasshive_gh_token_wrk_1=" not in set_cookie
    cookie_value = set_cookie.split(f"{worker_cookie_name('wrk_1')}=", 1)[1].split(";", 1)[0]
    authenticated_client.cookies.set(worker_cookie_name("wrk_1"), cookie_value)

    response = authenticated_client.get(f"/v1/link-refs/{artifact_ref_id}")

    assert response.status_code == 200, response.text
    assert response.content == b"artifact bytes"
    assert captured["url"] == f"http://runtime.test/v1/link-refs/{artifact_ref_id}"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"
    assert captured["headers"]["X-Viventium-User-Role"] == "viewer"


def test_signed_runtime_proxy_sets_worker_scoped_cookie(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    token = signed_worker_token(signed_secret)

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html>project</html>"
        headers = {"content-type": "text/html"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/ui/projects/prj_1?worker_id=wrk_1&gh_token={token}")

    assert response.status_code == 200
    assert f"{worker_cookie_name('wrk_1')}=" in response.headers["set-cookie"]
    assert "glasshive_gh_token_wrk_1=" not in response.headers["set-cookie"]


def test_signed_runtime_proxy_refreshes_worker_cookie_expiry(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    now = {"value": 1_000}
    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    old_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html>project</html>"
        headers = {"content-type": "text/html"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    now["value"] = 1_040
    client.cookies.set(worker_cookie_name("wrk_1"), old_token)

    response = client.get("/ui/projects/prj_1?worker_id=wrk_1")

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    refreshed = set_cookie.split(f"{worker_cookie_name('wrk_1')}=", 1)[1].split(";", 1)[0]
    assert refreshed != old_token
    refreshed_payload = server_module.verify_signed_link_token(refreshed)
    assert refreshed_payload is not None
    assert refreshed_payload["exp"] > 1_060


def test_worker_live_poll_refreshes_worker_cookie_expiry(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    now = {"value": 1_000}
    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    old_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    now["value"] = 1_040
    client.cookies.set(worker_cookie_name("wrk_1"), old_token)

    response = client.get("/api/worker/wrk_1/live")

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    refreshed = set_cookie.split(f"{worker_cookie_name('wrk_1')}=", 1)[1].split(";", 1)[0]
    assert refreshed != old_token
    refreshed_payload = server_module.verify_signed_link_token(refreshed)
    assert refreshed_payload is not None
    assert refreshed_payload["exp"] > 1_060
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"


def test_enterprise_signed_artifact_link_cannot_open_workspace_shell(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    for token in (
        signed_artifact_token(signed_secret, kind="artifact_download"),
        signed_artifact_token(signed_secret, kind="artifact_open"),
    ):
        assert client.get(f"/watch/wrk_1?gh_token={token}").status_code == 403
        assert client.get(f"/desktop/wrk_1?gh_token={token}").status_code == 403
    assert runtime.header_contexts == []


def test_enterprise_signed_worker_link_cannot_proxy_signed_artifact_endpoint(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(signed_secret)

    assert client.get(f"/v1/signed-links/{token}").status_code == 403
    assert runtime.header_contexts == []


def test_enterprise_signed_artifact_link_cannot_proxy_raw_runtime_routes(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_artifact_token(signed_secret, kind="artifact_open")

    assert client.get(f"/v1/workers/wrk_1/artifacts/open?gh_token={token}&path=workspace/report.txt").status_code == 403
    assert runtime.header_contexts == []


def test_signed_watch_rejects_unsafe_worker_cookie_name(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get("/watch/wrk_1%3Bbad")

    assert response.status_code == 400


def test_enterprise_trusted_identity_requires_user_assertion(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get('/api/bootstrap', headers={"X-Viventium-Tenant-Id": "tenant-alpha"})

    assert response.status_code == 401
    assert "authenticated user assertion" in response.json()["detail"]
    assert runtime.header_contexts == []


def test_trusted_inbound_identity_headers_can_be_enabled(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        '/api/bootstrap',
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "asserted-user",
            "X-Viventium-User-Role": "operator",
        },
    )

    assert response.status_code == 200
    assert response.json()["owner_id"] == "asserted-user"
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "asserted-user"
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "operator"


def test_enterprise_trusted_identity_uses_proxy_assertion(monkeypatch):
    service_secret = "ui-service-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        '/api/bootstrap',
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Email": "user-a@example.test",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200
    assert response.json()["owner_id"] == "user-a"
    assert runtime.header_contexts[-1]["X-WPR-Token"] == service_secret
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"
    assert runtime.header_contexts[-1]["X-Viventium-User-Email"] == "user-a@example.test"
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "member"


def test_enterprise_live_api_hides_raw_desktop_url_but_backend_requests_internal_details(monkeypatch):
    service_secret = "ui-service-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        "/api/worker/wrk_1/live",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200
    runtime_details = response.json()["runtime_details"]
    assert runtime_details["view_available"] is True
    assert "view_url" not in runtime_details
    assert runtime.header_contexts[-1]["X-WPR-Token"] == service_secret
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "operator"


def test_multi_user_desktop_credentials_fail_closed_without_vnc_password(monkeypatch):
    service_secret = "ui-service-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")

    class PasswordlessRuntime(FakeRuntimeClient):
        def worker_live(self, worker_id: str, *, compact: bool = False):
            payload = super().worker_live(worker_id, compact=compact)
            payload["runtime_details"]["view_url"] = "http://127.0.0.1:60812/?autoconnect=1"
            return payload

    runtime = PasswordlessRuntime()
    client = TestClient(create_app(runtime_client=runtime))
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    response = client.get(
        "/api/workspace/wrk_1/desktop-credentials",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 503
    assert "password" not in response.text.lower()


def test_enterprise_trusted_identity_rejects_tenant_mismatch(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        '/api/bootstrap',
        headers={
            "X-Viventium-Tenant-Id": "tenant-beta",
            "X-Viventium-User-Id": "user-a",
        },
    )

    assert response.status_code == 401
    assert "tenant assertion" in response.json()["detail"]
    assert runtime.header_contexts == []


def test_enterprise_local_demo_owner_requires_explicit_escape_hatch(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOW_LOCAL_DEMO_OWNER", "true")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get('/api/bootstrap')

    assert response.status_code == 200
    assert response.json()["owner_id"] == "demo-owner"
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "demo-owner"


def test_runtime_ui_proxy_injects_enterprise_identity(monkeypatch):
    service_secret = "ui-service-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html>runtime ui</html>"
        headers = {"content-type": "text/html"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(
        "/ui/projects/prj_1?worker_id=wrk_1",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200
    assert response.text == "<html>runtime ui</html>"
    assert captured["url"] == "http://runtime.test/ui/projects/prj_1?worker_id=wrk_1"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"
    assert captured["headers"]["X-Viventium-User-Role"] == "member"


@pytest.mark.parametrize("upstream_status", [200, 401, 403, 404])
def test_mission_view_proxies_only_read_only_runtime_authority(tmp_path, monkeypatch, upstream_status):
    captured = []
    service_secret = "runtime-service-secret"
    signed_secret = "mission-view-secret"
    monkeypatch.setenv("WPR_API_TOKEN", service_secret)
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", signed_secret)
    monkeypatch.setenv(
        "GLASSHIVE_LINK_REF_STATE_PATH",
        str(tmp_path / "mission-view-link-refs.sqlite3"),
    )
    ref_id = create_signed_link_ref(
        token=signed_worker_token(signed_secret),
        target_url="/watch/wrk_1",
    )

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.append({"method": method, "url": url, "headers": headers or {}})
            return httpx.Response(
                upstream_status,
                content=b'<html><meta http-equiv="refresh" content="5">Mission result</html>',
                headers={"content-type": "text/html", "content-security-policy": "default-src 'none'"},
            )

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    response = client.get(
        f"/w/{ref_id}?worker_id=unrelated",
        headers={"Authorization": "Bearer unrelated", "X-WPR-Token": "unrelated"},
    )
    assert response.status_code == upstream_status
    assert response.headers["content-security-policy"] == "default-src 'none'"
    if upstream_status < 400:
        assert "httponly" in response.headers["set-cookie"].lower()
        assert "samesite=lax" in response.headers["set-cookie"].lower()
    else:
        assert "set-cookie" not in response.headers
    assert len(captured) == 1
    assert captured[0]["url"].startswith(f"http://runtime.test/w/{ref_id}")
    assert "Authorization" not in captured[0]["headers"]
    assert captured[0]["headers"]["X-WPR-Token"] == service_secret
    assert all(value != "unrelated" for value in captured[0]["headers"].values())
    client.cookies.clear()
    assert client.post(f"/w/{ref_id}").status_code == 405
    assert client.post(f"/w/{ref_id}/actions/terminate").status_code == 404
    assert client.get(f"/w/{ref_id}/desktop").status_code == 404
    assert client.get("/w/invalid").status_code == 400
    assert len(captured) == 1


def test_runtime_proxy_is_default_deny_for_unlisted_runtime_routes(monkeypatch):
    captured = []

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, *args, **kwargs):
            captured.append((args, kwargs))
            raise AssertionError("unlisted route reached the private runtime")

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    assert client.get("/v1/projects").status_code == 404
    assert client.post("/v1/workers/wrk_1/terminate").status_code == 404
    assert client.get("/ui").status_code == 404
    assert captured == []


def test_control_plane_bff_exposes_user_scoped_existing_substrates(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get("/api/control-plane")
    workspaces = client.get("/api/workspaces?kind=named&search=main")
    metadata = client.post(
        "/api/worker/wrk_1/metadata",
        json={"name": "Planning workspace", "tags": ["finance", "quarterly"]},
    )

    assert response.status_code == 200
    assert response.json()["me"]["user_id"] == "demo-owner"
    assert response.json()["provider_accounts"][0]["provider"] == "codex"
    assert response.json()["connections"][0]["connection_id"] == "conn_1"
    assert response.json()["library"][0]["library_id"] == "lib_1"
    assert response.json()["provider_options"][0]["subscription_support"] == "supported"
    assert workspaces.status_code == 200
    assert workspaces.json()["items"][0]["workspace_kind"] == "named"
    assert metadata.status_code == 200
    assert runtime.metadata_requests[-1] == {
        "worker_id": "wrk_1",
        "payload": {"name": "Planning workspace", "tags": ["finance", "quarterly"]},
    }


def test_recurring_schedule_bff_routes_use_the_scoped_runtime_client():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    listed = client.get("/api/recurring-schedules?include_inactive=true")
    created = client.post(
        "/api/workspace/wrk_1/recurring-schedules",
        json={
            "instruction": "Run the synthetic check.",
            "recurrence_type": "interval",
            "interval_seconds": 3600,
            "timezone_name": "UTC",
        },
    )
    occurrences = client.get("/api/recurring-schedules/rsd_public_safe/occurrences?limit=10")
    deactivated = client.post("/api/recurring-schedules/rsd_public_safe/deactivate")

    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["definition_id"] == "rsd_public_safe"
    assert created.status_code == 201, created.text
    assert created.json()["worker_id"] == "wrk_1"
    assert occurrences.status_code == 200, occurrences.text
    assert occurrences.json()["items"][0]["occurrence_id"] == "occ_public_safe"
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["active"] is False
    assert runtime.recurring_schedule_requests == [
        {"action": "list", "include_inactive": True},
        {
            "action": "create",
            "worker_id": "wrk_1",
            "payload": {
                "instruction": "Run the synthetic check.",
                "recurrence_type": "interval",
                "interval_seconds": 3600,
                "local_time": "",
                "timezone_name": "UTC",
                    "dst_policy": "next_valid_earliest",
                    "first_run_at": None,
                    "cron_expression": "",
                    "rrule": "",
                    "starts_at": None,
                    "ends_at": None,
                    "enabled": True,
                    "overlap_policy": "skip",
                    "misfire_grace_seconds": 300,
                    "catch_up_policy": "skip",
                    "max_catch_up_occurrences": 1,
                    "jitter_seconds": 0,
                    "schedule_text": "",
            },
        },
        {"action": "occurrences", "definition_id": "rsd_public_safe", "limit": 10},
        {"action": "deactivate", "definition_id": "rsd_public_safe"},
    ]


def test_recurring_schedule_bff_forwards_full_structured_contract():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    created = client.post(
        "/api/workspace/wrk_1/recurring-schedules",
        json={
            "instruction": "Run the synthetic weekday check.",
            "recurrence_type": "cron",
            "cron_expression": "0 9 * * 1-5",
            "timezone_name": "America/Toronto",
            "starts_at": "2027-01-01T00:00:00-05:00",
            "ends_at": "2027-06-30T23:59:59-04:00",
            "enabled": True,
            "overlap_policy": "skip",
            "misfire_grace_seconds": 600,
            "catch_up_policy": "bounded",
            "max_catch_up_occurrences": 2,
            "jitter_seconds": 120,
        },
    )

    assert created.status_code == 201, created.text
    payload = runtime.recurring_schedule_requests[-1]["payload"]
    assert payload["recurrence_type"] == "cron"
    assert payload["cron_expression"] == "0 9 * * 1-5"
    assert payload["overlap_policy"] == "skip"
    assert payload["catch_up_policy"] == "bounded"
    assert payload["max_catch_up_occurrences"] == 2
    assert payload["jitter_seconds"] == 120


def test_recurring_schedule_bff_updates_enabled_state_without_replacing_history():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.patch(
        "/api/recurring-schedules/rsd_public_safe",
        json={"enabled": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    assert runtime.recurring_schedule_requests == [
        {
            "action": "update",
            "definition_id": "rsd_public_safe",
            "payload": {"enabled": True},
        }
    ]


def test_recurring_schedule_bff_run_now_and_retire_preserve_owner_scoping():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    run_now = client.post(
        "/api/recurring-schedules/rsd_public_safe/run-now",
        json={"idempotency_key": "manual-public-safe-1"},
    )
    retired = client.delete("/api/recurring-schedules/rsd_public_safe")

    assert run_now.status_code == 200, run_now.text
    assert run_now.json()["status"] == "scheduled"
    assert retired.status_code == 200, retired.text
    assert retired.json()["retired_at"]
    assert runtime.recurring_schedule_requests == [
        {
            "action": "run_now",
            "definition_id": "rsd_public_safe",
            "idempotency_key": "manual-public-safe-1",
        },
        {"action": "retire", "definition_id": "rsd_public_safe"},
    ]


def test_recurring_schedule_bff_uses_signed_user_assertion(tmp_path, monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        "/api/recurring-schedules",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "member-public-safe",
            "X-Viventium-User-Email": "member@example.invalid",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200, response.text
    scoped_headers = runtime.header_contexts[-1]
    assert scoped_headers["X-WPR-Token"] == "ui-service-secret"
    assert scoped_headers["X-GlassHive-User-Assertion"]
    assert "X-Viventium-User-Id" not in scoped_headers


def test_recurring_schedule_bff_rejects_invalid_payload_before_runtime_call():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post(
        "/api/workspace/wrk_1/recurring-schedules",
        json={
            "instruction": "",
            "recurrence_type": "weekly",
        },
    )

    assert response.status_code == 422
    assert runtime.recurring_schedule_requests == []

    invalid_id = client.post(
        "/api/recurring-schedules/../deactivate",
    )
    assert invalid_id.status_code in {400, 404}
    assert runtime.recurring_schedule_requests == []


def test_recurring_schedule_ui_has_structured_accessible_controls():
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    page = client.get("/")

    assert page.status_code == 200
    assert 'id="recurring-schedule-form"' in page.text
    assert 'id="recurring-schedule-workspace"' in page.text
    assert 'id="recurring-schedule-instruction"' in page.text
    assert 'id="recurring-schedule-type"' in page.text
    assert 'id="recurring-schedule-timezone"' in page.text
    assert 'value="once"' in page.text
    assert 'value="weekly"' in page.text
    assert 'value="weeks"' in page.text
    assert 'value="cron"' in page.text
    assert 'value="rfc5545"' in page.text
    assert 'Custom · cron' in page.text
    assert 'Custom · calendar rule' in page.text
    assert 'id="recurring-schedule-starts-at"' in page.text
    assert 'id="recurring-schedule-once-field"' in page.text
    assert 'id="recurring-schedule-ends-at"' in page.text
    assert 'id="recurring-schedule-enabled"' in page.text
    assert 'id="recurring-schedule-overlap-policy"' in page.text
    assert 'id="recurring-schedule-misfire-grace"' in page.text
    assert 'id="recurring-schedule-catch-up-policy"' in page.text
    assert 'id="recurring-schedule-jitter"' in page.text
    assert 'id="recurring-schedule-status"' in page.text
    assert 'id="recurring-schedule-cancel-edit"' in page.text
    assert 'id="recurring-schedule-submit"' in page.text
    control_plane_script = (server_module.STATIC_DIR / "control-plane.js").read_text(encoding="utf-8")
    assert "renderSchedules" in control_plane_script
    assert "runScheduleNow" in control_plane_script
    assert "retireSchedule" in control_plane_script
    assert "editSchedule" in control_plane_script
    assert "data-schedule-status" in control_plane_script
    assert "actionStatus.setAttribute('aria-live', 'polite')" in control_plane_script
    assert "intervalUnit === 'weeks' ? 604800" in control_plane_script
    assert "recurrenceSubmissionPolicy(selectedRecurrenceType" in control_plane_script
    assert "scheduleEditorType(schedule)" in control_plane_script
    assert "selectedRecurrenceType === 'weekly' ? 'interval'" not in control_plane_script
    assert "Dispatch owner:" not in control_plane_script
    assert "onceField.hidden = kind !== 'once'" in control_plane_script
    assert "weeklyField.hidden = kind !== 'weekly'" in control_plane_script
    assert "customTypeField.hidden = selectedKind !== 'custom'" in control_plane_script
    assert "{ enabled: false }" in control_plane_script
    assert "if (controlPlane?.recurrence_owner === 'viventium_cortex') return" not in control_plane_script


def test_weekly_schedule_uses_calendar_recurrence_in_the_browser_timezone() -> None:
    module = (Path(server_module.STATIC_DIR) / "schedule-policy.js").as_uri()
    script = f"""
      import {{
        recurrenceSubmissionPolicy,
        scheduleEditorType,
        zonedDateTimeLocalValue,
      }} from {json.dumps(module)};
      const policy = recurrenceSubmissionPolicy('weekly', {{
        intervalSeconds: 604800,
        timezoneName: 'America/Toronto',
        rrule: '',
      }});
      if (JSON.stringify(policy) !== JSON.stringify({{
        recurrenceType: 'rfc5545',
        intervalSeconds: null,
        timezoneName: 'America/Toronto',
        rrule: 'FREQ=WEEKLY',
      }})) throw new Error(JSON.stringify(policy));
      if (scheduleEditorType({{ recurrence_type: 'rfc5545', rrule: 'FREQ=WEEKLY' }}) !== 'weekly') {{
        throw new Error('weekly editor mapping missing');
      }}
      if (scheduleEditorType({{ recurrence_type: 'interval', interval_seconds: 604800 }}) !== 'interval') {{
        throw new Error('elapsed intervals must not be relabeled weekly');
      }}
      if (zonedDateTimeLocalValue('2026-01-15T14:00:00Z', 'America/Toronto') !== '2026-01-15T09:00') {{
        throw new Error('winter wall time was not preserved');
      }}
      if (zonedDateTimeLocalValue('2026-07-15T14:00:00Z', 'America/Toronto') !== '2026-07-15T10:00') {{
        throw new Error('summer wall time was not preserved');
      }}
      if (zonedDateTimeLocalValue('not-a-date', 'America/Toronto') !== '') {{
        throw new Error('invalid instants must fail closed');
      }}
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_duplicate_reapproval_never_widens_the_prior_workspace_scopes() -> None:
    module = (Path(server_module.STATIC_DIR) / "capability-review.js").as_uri()
    script = f"""
      import {{ equivalentReapprovalScopes }} from {json.dumps(module)};
      const exact = equivalentReapprovalScopes(
        ['documents:read', 'documents:write'],
        {{ scopes: ['documents:read', 'unknown:scope'] }},
      );
      if (JSON.stringify(exact) !== JSON.stringify(['documents:read'])) throw new Error(JSON.stringify(exact));
      if (equivalentReapprovalScopes(['documents:read'], null) !== null) throw new Error('ordinary grants changed');
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _configure_multi_user_connect_test(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "legacy_compatibility")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "signed_internal_assertion")
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    monkeypatch.setenv("GLASSHIVE_TRUSTED_PROXY_BOUNDARY_PROVEN", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", "https://glasshive.example.test/mcp")


def _multi_user_headers() -> dict[str, str]:
    return {
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }


def _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch) -> None:
    _configure_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "false")
    monkeypatch.setenv("GLASSHIVE_TRUSTED_PROXY_BOUNDARY_PROVEN", "false")

    class FakeHumanAuth:
        mode = "oidc"
        session_enabled = True

        def resolve_session(self, token):
            if token != "connect-session":
                return None
            return {
                "tenant_id": "tenant-alpha",
                "user_id": "user-a",
                "email": "member@example.invalid",
                "role": "member",
            }

    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: FakeHumanAuth())


def test_connect_ai_fails_loud_when_multi_user_oauth_is_not_configured(tmp_path, monkeypatch):
    _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.delenv("GLASSHIVE_MCP_OAUTH_ISSUER", raising=False)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    client.cookies.set("glasshive_session", "connect-session")

    response = client.get("/api/connect-ai")

    assert response.status_code == 503
    assert response.json()["detail"] == "GlassHive MCP client connection requires a configured OAuth issuer"


def test_connect_ai_returns_official_client_commands_when_oauth_is_configured(tmp_path, monkeypatch):
    _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", "https://identity.example.test")
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES",
        "00000000-0000-4000-8000-000000000123",
    )
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", "user_impersonation")
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES",
        "https://glasshive.example.test/mcp/access_as_user",
    )
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS",
        "registered-codex-client registered-claude-client",
    )
    monkeypatch.setenv("GLASSHIVE_MCP_CLAUDE_CLIENT_ID", "registered-claude-client")
    monkeypatch.setenv("GLASSHIVE_MCP_CLAUDE_CALLBACK_PORT", "49152")
    monkeypatch.setenv("GLASSHIVE_MCP_CODEX_CLIENT_ID", "registered-codex-client")
    monkeypatch.setenv("GLASSHIVE_MCP_CODEX_CALLBACK_PORT", "49153")
    monkeypatch.setenv(
        "GLASSHIVE_MCP_CODEX_RESOURCE",
        "https://glasshive.example.test/mcp",
    )
    monkeypatch.setenv(
        "GLASSHIVE_MCP_DOCUMENTATION_URL",
        "https://docs.example.test/glasshive-client-registration",
    )
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    client.cookies.set("glasshive_session", "connect-session")

    response = client.get("/api/connect-ai")

    assert response.status_code == 200
    payload = response.json()
    assert payload["server_name"] == "glasshive-d0c2dae3d5cd"
    assert payload["supported_clients"] == ["claude", "codex"]
    assert payload["clients"]["codex"]["add_command"] == (
        "codex mcp add -c mcp_oauth_callback_port=49153 "
        "-c 'mcp_oauth_callback_url=\"http://127.0.0.1:49153/callback\"' glasshive-d0c2dae3d5cd "
        "--url https://glasshive.example.test/mcp "
        "--oauth-client-id registered-codex-client"
    )
    assert "--oauth-resource" not in payload["clients"]["codex"]["add_command"]
    assert payload["clients"]["codex"]["login_command"] == (
        "codex mcp login -c mcp_oauth_callback_port=49153 "
        "-c 'mcp_oauth_callback_url=\"http://127.0.0.1:49153/callback\"' "
        "glasshive-d0c2dae3d5cd"
    )
    assert payload["clients"]["codex"]["config_toml"] == (
        "[mcp_servers.glasshive-d0c2dae3d5cd]\n"
        'url = "https://glasshive.example.test/mcp"\n'
        'scopes = ["https://glasshive.example.test/mcp/access_as_user", "offline_access"]\n\n'
        "[mcp_servers.glasshive-d0c2dae3d5cd.oauth]\n"
        'client_id = "registered-codex-client"'
    )
    assert payload["clients"]["codex"]["callback_uri"] == (
        "http://127.0.0.1:49153/callback/0MLa49XNV_Yw"
    )
    assert payload["clients"]["claude"]["add_command"] == (
        "claude mcp add --transport http --scope user "
        "--client-id registered-claude-client --callback-port 49152 "
        "glasshive-d0c2dae3d5cd https://glasshive.example.test/mcp"
    )
    assert payload["clients"]["claude"]["callback_port"] == 49152
    assert payload["clients"]["claude"]["callback_uri"] == (
        "http://localhost:49152/callback"
    )
    assert payload["configuration_status"] == "ready"
    assert payload["documentation_url"].endswith("/glasshive-client-registration")
    assert payload["source"]["license"] == "Apache-2.0"
    codex_prompt = payload["clients"]["codex"]["setup_prompt"]
    claude_prompt = payload["clients"]["claude"]["setup_prompt"]
    assert payload["clients"]["codex"]["config_toml"] in codex_prompt
    assert payload["clients"]["codex"]["login_command"] in codex_prompt
    assert "Persist these scopes" in codex_prompt
    assert "can renew the login" in codex_prompt
    assert "If xPerfect tools already work" in codex_prompt
    assert "codex plugin marketplace add xPerfectAI/xPerfect" in codex_prompt
    assert "codex plugin add glasshive@project-glasshive" in codex_prompt
    assert "claude plugin" not in codex_prompt
    assert "unscoped" not in codex_prompt
    assert "interrupt" not in codex_prompt
    assert "start a new Codex task" not in codex_prompt
    assert "Restart the Codex/ChatGPT desktop app once" in codex_prompt
    assert "Complete the native browser sign-in" in codex_prompt
    assert "Claude" not in codex_prompt
    assert "workspace_list once" in codex_prompt
    assert "Never enumerate or summarize the tool catalog" in codex_prompt
    assert payload["clients"]["claude"]["add_command"] in claude_prompt
    assert payload["clients"]["claude"]["login_note"] in claude_prompt
    assert "claude plugin marketplace add xPerfectAI/xPerfect" in claude_prompt
    assert "claude plugin install glasshive@glasshive --scope user --yes" in claude_prompt
    assert "codex plugin" not in claude_prompt
    assert "Codex" not in claude_prompt
    assert "workspace_list once" in claude_prompt
    assert "Never enumerate or summarize the tool catalog" in claude_prompt
    assert "If you are Codex, follow only the Codex section." in payload["guided_prompt"]
    assert "If you are Claude Code, follow only the Claude Code section." in payload["guided_prompt"]
    assert codex_prompt in payload["guided_prompt"]
    assert claude_prompt in payload["guided_prompt"]
    assert "list the GlassHive tools" not in payload["guided_prompt"]
    assert "Do not build OAuth URLs" in payload["guided_prompt"]
    assert "custom callback" not in payload["guided_prompt"]
    assert "administrator" not in payload["guided_prompt"].lower()
    control_plane_script = (
        server_module.STATIC_DIR / "control-plane.js"
    ).read_text(encoding="utf-8")
    assert "String(clients.codex.callback_uri || '')" in control_plane_script
    assert "String(clients.claude.callback_uri || '')" in control_plane_script


def test_mcp_client_server_name_is_canonical_stable_and_deployment_specific():
    upper = server_module._mcp_client_server_name(
        "https://GLASSHIVE.EXAMPLE.TEST:443/mcp"
    )
    canonical = server_module._mcp_client_server_name(
        "https://glasshive.example.test/mcp"
    )
    path_scoped = server_module._mcp_client_server_name(
        "https://glasshive.example.test/team-a/mcp"
    )

    assert upper == canonical == "glasshive-d0c2dae3d5cd"
    assert path_scoped == "glasshive-76d186f127b6"
    assert server_module.re.fullmatch(r"glasshive-[0-9a-f]{12}", canonical)


def test_connect_ai_advertises_only_clients_with_a_complete_deployment_contract(tmp_path, monkeypatch):
    _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES", "synthetic-audience")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", "user_impersonation")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES", "user_impersonation")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS", "registered-codex-client")
    monkeypatch.setenv("GLASSHIVE_MCP_CODEX_CLIENT_ID", "registered-codex-client")
    monkeypatch.setenv("GLASSHIVE_MCP_CODEX_CALLBACK_PORT", "49153")
    monkeypatch.setenv("GLASSHIVE_MCP_CODEX_RESOURCE", "https://glasshive.example.test/mcp")
    monkeypatch.delenv("GLASSHIVE_MCP_CLAUDE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GLASSHIVE_MCP_CLAUDE_CALLBACK_PORT", raising=False)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    client.cookies.set("glasshive_session", "connect-session")

    payload = client.get("/api/connect-ai").json()

    assert payload["supported_clients"] == ["codex"]
    assert set(payload["clients"]) == {"codex"}
    assert payload["clients"]["codex"]["setup_prompt"] in payload["guided_prompt"]
    assert payload["clients"]["codex"]["config_toml"] in payload["guided_prompt"]
    assert payload["clients"]["codex"]["login_command"] in payload["guided_prompt"]
    assert "If you are Claude Code" not in payload["guided_prompt"]


def test_external_ai_primary_ui_is_url_first_and_callbacks_are_admin_details():
    page = (Path(server_module.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    script = (Path(server_module.STATIC_DIR) / "control-plane.js").read_text(encoding="utf-8")

    assert "Use xPerfect from another AI app" in page
    assert "Control your workspaces" in page
    assert 'id="connect-ai-server-url"' in page
    assert 'id="copy-connect-ai-url"' in page
    assert 'id="connect-ai-terminal-setup"' in page
    assert 'id="connect-ai-registration-details"' in page
    assert "Copy server address" in page
    assert "Recommended" in page
    assert "Connect this AI" in page
    assert "Paste once. Your AI follows only its own setup." in page
    assert "Copy connect instruction" in page
    assert "Do not open this address" in script
    assert "connectAi.mcp_url" in script
    assert "connectAi.server_name" in script
    assert "referenceRow('Codex · Registered callback'" not in script
    assert "['ArrowLeft', 'ArrowRight', 'Home', 'End']" in script
    assert "manualTab.tabIndex = manual ? 0 : -1" in script
    assert "if (canSetup && clients.codex)" in script
    assert "if (canSetup && clients.claude)" in script
    assert "String(clients.codex.config_toml || '')" in script
    assert "ChatGPT or Codex" not in script
    assert "connect-ai-supported-summary" in page
    assert "connect-ai-auto-copy" in page
    assert "supportedSummary.textContent" in script
    assert "AI accounts" in page
    assert "Worker accounts" not in page
    assert "response.status === 404" in script


def test_connect_ai_hides_false_commands_without_pre_registered_client(tmp_path, monkeypatch):
    _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", "https://identity.example.test")
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES",
        "00000000-0000-4000-8000-000000000123",
    )
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", "user_impersonation")
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES",
        "api://00000000-0000-4000-8000-000000000123/user_impersonation",
    )
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS",
        "registered-codex-client registered-claude-client",
    )
    for name in (
        "GLASSHIVE_MCP_CLAUDE_CLIENT_ID",
        "GLASSHIVE_MCP_CLAUDE_CALLBACK_PORT",
        "GLASSHIVE_MCP_CODEX_CLIENT_ID",
        "GLASSHIVE_MCP_CODEX_CALLBACK_PORT",
        "GLASSHIVE_MCP_CODEX_RESOURCE",
    ):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    client.cookies.set("glasshive_session", "connect-session")

    response = client.get("/api/connect-ai")

    assert response.status_code == 200
    assert response.json()["clients"] == {}
    assert response.json()["configuration_status"] == "action_required"
    assert "pre-registered" in response.json()["configuration_note"]
    assert "mcp add" not in response.text


def test_connect_ai_fails_closed_on_oauth_audience_scope_or_allowlist_drift(
    tmp_path,
    monkeypatch,
):
    _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", "https://identity.example.test")
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES",
        "00000000-0000-4000-8000-000000000123",
    )
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", "user_impersonation")
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES",
        "api://00000000-0000-4000-8000-000000000123/user_impersonation",
    )
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS", "registered-claude-client")
    monkeypatch.setenv("GLASSHIVE_MCP_CLAUDE_CLIENT_ID", "registered-claude-client")
    monkeypatch.setenv("GLASSHIVE_MCP_CLAUDE_CALLBACK_PORT", "49152")
    monkeypatch.setenv("GLASSHIVE_MCP_CODEX_CLIENT_ID", "unapproved-codex-client")
    monkeypatch.setenv("GLASSHIVE_MCP_CODEX_CALLBACK_PORT", "49153")
    monkeypatch.setenv(
        "GLASSHIVE_MCP_CODEX_RESOURCE",
        "https://glasshive.example.test/mcp",
    )
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    client.cookies.set("glasshive_session", "connect-session")

    allowlist_drift = client.get("/api/connect-ai")

    assert allowlist_drift.status_code == 200
    assert set(allowlist_drift.json()["clients"]) == {"claude"}
    assert "unapproved-codex-client" not in allowlist_drift.text

    monkeypatch.delenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES")
    audience_drift = client.get("/api/connect-ai")

    assert audience_drift.status_code == 200
    assert audience_drift.json()["clients"] == {}
    assert audience_drift.json()["configuration_status"] == "action_required"
    assert "mcp add" not in audience_drift.text

    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES",
        "00000000-0000-4000-8000-000000000123",
    )
    monkeypatch.delenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES")
    token_scope_drift = client.get("/api/connect-ai")

    assert token_scope_drift.status_code == 200
    assert token_scope_drift.json()["clients"] == {}
    assert token_scope_drift.json()["configuration_status"] == "action_required"


def test_trusted_proxy_domain_policy_rejects_an_outside_account(tmp_path, monkeypatch):
    _configure_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_EMAIL_DOMAINS", "example.test")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", "https://identity.example.test")
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    headers = _multi_user_headers()
    headers["X-Viventium-User-Email"] = "member@outside.test"

    response = client.get("/api/connect-ai", headers=headers)

    assert response.status_code == 403
    assert response.json()["detail"] == "This account is outside the approved email domains"


def test_provider_account_bff_generates_opaque_locator_and_honest_platform_status(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post(
        "/api/provider-accounts",
        json={
            "provider": "codex",
            "label": "My Codex",
            "auth_method": "subscription",
            "make_default": True,
        },
    )

    assert response.status_code == 200
    request_payload = runtime.provider_account_requests[-1]
    assert request_payload["secret_locator"] == "native-home://auto"
    assert request_payload["platform_support"] == "proof_required"
    assert "token" not in json.dumps(request_payload).lower()


def test_control_plane_advertises_only_the_enabled_native_claude_subscription(
    monkeypatch,
):
    monkeypatch.setattr(server_module.sys, "platform", "linux")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get("/api/control-plane")
    created = client.post(
        "/api/provider-accounts",
        json={
            "provider": "claude",
            "label": "Personal Claude",
            "auth_method": "subscription",
            "make_default": False,
        },
    )

    assert response.status_code == 200
    claude = next(
        option
        for option in response.json()["provider_options"]
        if option["provider"] == "claude"
    )
    assert "api_key" not in claude["methods"]
    assert claude["methods"] == ["subscription"]
    assert claude["subscription_support"] == "supported"
    assert claude["api_key_support"] == "unavailable"
    assert "private account home" in claude["api_key_support_note"]
    assert created.status_code == 200
    assert runtime.provider_account_requests[-1]["platform_support"] == "supported"


def test_control_plane_hides_native_claude_when_the_runtime_setup_cli_is_missing(
    monkeypatch,
):
    monkeypatch.setattr(server_module.sys, "platform", "linux")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    runtime = FakeRuntimeClient()
    runtime.health_response["provider_setup_support"]["claude"] = "setup_cli_required"
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get("/api/control-plane")

    assert response.status_code == 200
    claude = next(
        option
        for option in response.json()["provider_options"]
        if option["provider"] == "claude"
    )
    assert claude["methods"] == []
    assert claude["subscription_support"] == "setup_cli_required"


def test_control_plane_keeps_disabled_claude_subscription_out_of_the_picker(monkeypatch):
    monkeypatch.setattr(server_module.sys, "platform", "linux")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.delenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", raising=False)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get("/api/control-plane")

    assert response.status_code == 200
    claude = next(
        option
        for option in response.json()["provider_options"]
        if option["provider"] == "claude"
    )
    assert claude["methods"] == []
    assert claude["subscription_support"] == "provider_permission_required"


def test_control_plane_keeps_claude_subscription_out_of_the_macos_picker(monkeypatch):
    monkeypatch.setattr(server_module.sys, "platform", "darwin")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get("/api/control-plane")

    assert response.status_code == 200
    claude = next(
        option
        for option in response.json()["provider_options"]
        if option["provider"] == "claude"
    )
    assert claude["methods"] == []
    assert claude["subscription_support"] == "unsupported_macos_host"


def test_provider_account_bff_registers_only_opaque_broker_metadata(monkeypatch):
    monkeypatch.setenv(
        "GLASSHIVE_INFERENCE_BROKER_URL",
        "https://librechat.example.test/api/viventium/glasshive/inference",
    )
    monkeypatch.setenv(
        "GLASSHIVE_INFERENCE_BROKER_SECRET",
        "synthetic-broker-secret-with-at-least-32-characters",
    )
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_TENANT_ID", "broker-tenant")
    monkeypatch.setenv(
        "GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON",
        '[{"glasshive_tenant_id":"local","glasshive_owner_id":"demo-owner","librechat_user_id":"user-a","proof":"operator_verified"}]',
    )
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    control_plane = client.get("/api/control-plane")
    response = client.post(
        "/api/provider-accounts",
        json={
            "provider": "codex",
            "label": "Personal OpenAI",
            "auth_method": "api_key",
            "make_default": True,
        },
    )

    assert response.status_code == 200
    codex = next(
        item
        for item in control_plane.json()["provider_options"]
        if item["provider"] == "codex"
    )
    assert codex["inference_broker_support"] == "supported"
    assert {"api_key", "enterprise_route"}.issubset(codex["methods"])
    request_payload = runtime.provider_account_requests[-1]
    assert request_payload["secret_locator"] == "broker://librechat-openai"
    assert request_payload["platform_support"] == "supported"
    assert "api_key" not in request_payload.keys()
    assert "token" not in json.dumps(request_payload).lower()


def test_provider_account_bff_never_registers_claude_against_openai_broker(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post(
        "/api/provider-accounts",
        json={
            "provider": "claude",
            "label": "Unsupported Claude route",
            "auth_method": "api_key",
        },
    )

    assert response.status_code == 409
    assert runtime.provider_account_requests == []


def test_multi_user_codex_account_route_opens_only_with_reviewed_container_isolation(
    tmp_path,
    monkeypatch,
):
    _configure_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    control_plane = client.get("/api/control-plane", headers=_multi_user_headers())
    created = client.post(
        "/api/provider-accounts",
        headers=_multi_user_headers(),
        json={
            "provider": "codex",
            "label": "Private Codex",
            "auth_method": "subscription",
            "make_default": True,
        },
    )

    assert control_plane.status_code == 200
    codex = next(
        item for item in control_plane.json()["provider_options"] if item["provider"] == "codex"
    )
    assert codex["subscription_support"] == "supported"
    assert created.status_code == 200
    assert runtime.provider_account_requests[-1]["platform_support"] == "supported"


def test_provider_account_setup_bff_is_user_scoped_through_signed_runtime_client():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    started = client.post("/api/provider-accounts/acct_public_safe/setup")
    submitted = client.post(
        "/api/provider-accounts/acct_public_safe/setup/input",
        json={"value": "synthetic-browser-code"},
    )
    status = client.get("/api/provider-accounts/acct_public_safe/setup")
    cancelled = client.post("/api/provider-accounts/acct_public_safe/setup/cancel")

    assert started.status_code == 200
    assert started.json()["status"] == "connecting"
    assert submitted.status_code == 200
    assert "synthetic-browser-code" not in submitted.text
    assert status.json()["status"] == "ready"
    assert cancelled.json()["status"] == "action_required"
    assert runtime.provider_setup_requests == [
        {"action": "start", "account_id": "acct_public_safe"},
        {"action": "input", "account_id": "acct_public_safe"},
        {"action": "status", "account_id": "acct_public_safe"},
        {"action": "cancel", "account_id": "acct_public_safe"},
    ]


def test_provider_account_setup_bff_preserves_actionable_runtime_error():
    class SetupUnavailableRuntime(FakeRuntimeClient):
        def start_provider_account_setup(self, account_id: str):
            response = httpx.Response(
                409,
                request=httpx.Request(
                    "POST", f"http://runtime.test/v1/provider-accounts/{account_id}/setup"
                ),
                json={"detail": "Claude setup is not installed on this GlassHive deployment"},
            )
            raise httpx.HTTPStatusError(
                "setup unavailable", request=response.request, response=response
            )

    client = TestClient(create_app(runtime_client=SetupUnavailableRuntime()))

    response = client.post("/api/provider-accounts/acct_public_safe/setup")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Claude setup is not installed on this GlassHive deployment"
    }


def test_provider_disconnect_and_workspace_capability_revoke_are_user_scoped():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    disconnected = client.post("/api/provider-accounts/acct_public_safe/disconnect")
    grants = client.get("/api/workspaces/wrk_1/capability-grants")
    revoked = client.delete("/api/workspaces/wrk_1/capability-grants/grant_1")

    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "disconnected"
    assert grants.status_code == 200
    assert grants.json()["items"][0]["grant_id"] == "grant_1"
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] == 1
    assert runtime.provider_disconnect_requests == ["acct_public_safe"]
    assert runtime.workspace_grant_requests == [
        {"action": "list", "worker_id": "wrk_1"},
        {"action": "revoke", "worker_id": "wrk_1", "grant_id": "grant_1"},
    ]


def test_workspace_detail_bff_maps_owner_miss_to_safe_404(monkeypatch):
    runtime = FakeRuntimeClient()
    response = httpx.Response(
        404,
        request=httpx.Request("GET", "http://runtime.test/v1/workers/wrk_missing"),
    )
    monkeypatch.setattr(
        runtime,
        "get_worker",
        lambda _worker_id: (_ for _ in ()).throw(
            httpx.HTTPStatusError("missing", request=response.request, response=response)
        ),
    )
    client = TestClient(create_app(runtime_client=runtime))

    missing = client.get("/api/workspaces/wrk_missing")

    assert missing.status_code == 404
    assert missing.json()["detail"] == "Workspace not found"


def test_workspace_detail_bff_returns_only_review_state(monkeypatch):
    runtime = FakeRuntimeClient()
    monkeypatch.setattr(
        runtime,
        "get_worker",
        lambda worker_id: {
            "worker_id": worker_id,
            "duplication_report": {"outstanding_reapproval_items": []},
            "gateway_url": "http://private.invalid",
            "takeover_url": "http://private.invalid/takeover",
            "control_url": "http://private.invalid/control",
            "gateway_port": 65535,
            "session_key": "private-session",
            "state_dir": "/private/state",
            "workspace_dir": "/private/workspace",
            "workspace_root": "/private/root",
        },
    )
    client = TestClient(create_app(runtime_client=runtime))

    detail = client.get("/api/workspaces/wrk_1")

    assert detail.status_code == 200
    assert detail.json() == {
        "worker_id": "wrk_1",
        "duplication_report": {"outstanding_reapproval_items": []},
    }


def test_provider_verify_and_forget_are_user_scoped_through_the_runtime_client():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    verified = client.post("/api/provider-accounts/acct_public_safe/verify")
    forgotten = client.delete("/api/provider-accounts/acct_public_safe")

    assert verified.status_code == 200
    assert verified.json()["status"] == "ready"
    assert forgotten.status_code == 200
    assert forgotten.json()["status"] == "forgotten"
    assert runtime.provider_verify_requests == ["acct_public_safe"]
    assert runtime.provider_forget_requests == ["acct_public_safe"]


def test_control_plane_ui_exposes_safe_disconnect_and_capability_remove_paths():
    script = (Path(server_module.STATIC_DIR) / "control-plane.js").read_text(encoding="utf-8")

    assert "removeProviderAccount" in script
    assert "Removing…" in script
    assert "result?.message || `${label} removed.`" in script
    assert "Reconnect" in script
    assert "Test connection" in script
    assert "Check connection" in script
    assert "Sign in again" in script
    assert "credential_cleanup_failed" in script
    assert "subscriptionRouteAvailable(account)" in script
    assert "'Remove'" in script
    assert "'Forget'" not in script
    assert "last_verified_at" in script
    assert "last_used_at" in script
    assert "observed_runs" in script
    assert "observed_failures" in script
    assert "observed_duration_seconds" in script
    assert "Observed by xPerfect" in script
    assert "Tokens reported by worker" in script
    assert "/verify" in script
    assert "Activity is temporarily unavailable" in script
    assert "Reconnect the affected account, then try again." in script
    app_script = (Path(server_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    assert "empty lists do not mean your data was removed" in app_script
    assert "Remove from workspace" in script
    assert "Upgrade workspace" in script
    assert "change_type: replacementGrant ? 'library_upgrade' : 'library_enable'" in script
    assert "replaces_grant_id" in script
    assert "api.deleteJson" in script
    assert "Keep as workspace" in (Path(server_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    confirm_page = (Path(server_module.STATIC_DIR) / "confirm.html").read_text(encoding="utf-8")
    confirm_script = (Path(server_module.STATIC_DIR) / "confirm.js").read_text(encoding="utf-8")
    assert 'id="confirm-provenance"' in confirm_page
    assert 'id="confirm-dependencies"' in confirm_page
    assert "library_plan_snapshot" in confirm_script
    assert "librarySnapshot.content_hash" in confirm_script


def test_provider_account_remove_disconnects_before_delete_and_stops_on_cleanup_failure():
    module = (Path(server_module.STATIC_DIR) / "control-plane.js").as_uri()
    script = f"""
      import {{ removeProviderAccountRequest }} from {json.dumps(module)};
      const events = [];
      const ok = {{
        postJson: async (url) => {{ events.push(['post', url]); return {{provider_logout_confirmed: false}}; }},
        deleteJson: async (url) => {{ events.push(['delete', url]); }},
      }};
      const result = await removeProviderAccountRequest(ok, 'acct_test');
      if (result.provider_logout_confirmed !== false) process.exit(1);
      if (JSON.stringify(events) !== JSON.stringify([
        ['post', '/api/provider-accounts/acct_test/disconnect'],
        ['delete', '/api/provider-accounts/acct_test'],
      ])) process.exit(2);
      const failed = [];
      try {{
        await removeProviderAccountRequest({{
          postJson: async (url) => {{ failed.push(['post', url]); throw new Error('cleanup failed'); }},
          deleteJson: async (url) => {{ failed.push(['delete', url]); }},
        }}, 'acct_retry');
        process.exit(3);
      }} catch (error) {{
        if (error.message !== 'cleanup failed') process.exit(4);
      }}
      if (JSON.stringify(failed) !== JSON.stringify([
        ['post', '/api/provider-accounts/acct_retry/disconnect'],
      ])) process.exit(5);
      const retryEvents = [];
      let deleteAttempts = 0;
      const retryApi = {{
        postJson: async (url) => {{ retryEvents.push(['post', url]); return {{}}; }},
        deleteJson: async (url) => {{
          retryEvents.push(['delete', url]);
          deleteAttempts += 1;
          if (deleteAttempts === 1) throw new Error('delete failed');
        }},
      }};
      try {{
        await removeProviderAccountRequest(retryApi, 'acct_delete_retry');
        process.exit(6);
      }} catch (error) {{
        if (error.message !== 'delete failed') process.exit(7);
      }}
      await removeProviderAccountRequest(retryApi, 'acct_delete_retry');
      if (JSON.stringify(retryEvents) !== JSON.stringify([
        ['post', '/api/provider-accounts/acct_delete_retry/disconnect'],
        ['delete', '/api/provider-accounts/acct_delete_retry'],
        ['post', '/api/provider-accounts/acct_delete_retry/disconnect'],
        ['delete', '/api/provider-accounts/acct_delete_retry'],
      ])) process.exit(8);
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_connections_recovery_is_verify_first_and_external_client_failure_is_optional():
    script = (Path(server_module.STATIC_DIR) / "control-plane.js").read_text(encoding="utf-8")

    recovery = script.index("String(account.recovery_code || '') === 'credential_cleanup_failed'")
    check = script.index("'Check connection'", recovery)
    verify = script.index("verifyProviderAccount(account, check)", check)
    sign_in = script.index("'Sign in again'", verify)
    reconnect = script.index("reconnectProviderAccount(account, signInAgain)", sign_in)
    assert recovery < check < verify < sign_in < reconnect
    assert "const idleLabel = button.textContent || 'Test connection';" in script
    assert "button.textContent = idleLabel;" in script
    assert "fetch(api.withAuth('/api/connect-ai')).catch(() => null)" in script
    assert "if (connectResponse?.ok)" in script
    assert "External AI client setup is temporarily unavailable." in script
    assert "if (!connectResponse.ok) throw" not in script
    removal = script[
        script.index("async function removeProviderAccount") : script.index(
            "function renderProviderAccounts"
        )
    ]
    removal_error = removal.index("setProviderStatus(error.message);")
    assert removal.index("button.disabled = false;", removal_error) > removal_error
    assert removal.index("button.textContent = 'Remove';", removal_error) > removal_error


def test_provider_account_errors_match_the_single_remove_action():
    script = Path(server_module.__file__).read_text(encoding="utf-8")

    assert 'exc, "GlassHive could not remove this account"' in script
    assert 'exc, "GlassHive could not disconnect this account"' not in script
    assert 'exc, "GlassHive could not forget this account"' not in script


def test_connections_ui_keeps_primary_account_setup_short_and_actionable():
    page = (Path(server_module.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    script = (Path(server_module.STATIC_DIR) / "control-plane.js").read_text(encoding="utf-8")

    assert 'id="add-provider-account"' in page
    assert 'id="connect-ai-advanced"' in page
    assert '<summary>Use xPerfect from another AI app</summary>' in page
    assert "Your AI accounts and tools." not in page
    assert "Connect the subscription your workers should use." not in page
    assert 'id="provider-setup-link"' in page
    assert 'id="provider-setup-code"' in page
    assert 'id="copy-provider-setup-code"' in page
    assert 'id="provider-setup-input-form"' in page
    assert 'id="provider-setup-input"' in page
    assert 'type="password"' in page
    assert 'autocomplete="one-time-code"' in page
    assert 'maxlength="1024"' in page
    assert 'id="submit-provider-setup-input"' in page
    assert 'id="restart-provider-setup"' in page
    assert 'id="provider-setup-state"' not in page
    assert '<pre id="provider-setup-instructions"' not in page
    assert 'id="provider-account-status" class="inline-status" aria-live="polite" hidden' not in page
    assert "credential bytes" not in page
    assert "short-lived broker grant" not in page
    assert "short-lived broker grant" not in script
    assert 'id="microsoft-connection-note"' not in page
    assert 'id="connected-services-card" class="control-card compact-connections-card" hidden' in page
    assert "copyText" in script
    assert "Open ${providerName} sign-in" in script
    assert "Copy code" in page
    assert "Finish connecting" in page
    assert "/setup/input" in script
    assert "setupInput.value = '';" in script
    assert "&& !inputSubmitted" in script
    assert "inputSubmitted ? 'Finishing sign-in…'" in script
    assert "Open ChatGPT security settings" in script
    assert "Having trouble?" in page
    assert "technical.dataset.autoOpened = 'true'" in script
    assert "technical.open = false" in script
    assert "accountMore.hidden = !payload.complete" in script
    assert "accountRecovery.hidden = !payload.complete" in script
    assert "addAccount.hidden = !payload.complete" in script
    assert "externalClients.hidden = !payload.complete" in script
    assert "copyResetTimers" in script
    assert '<option value="claude">Claude Code</option>' not in page
    assert '<option value="api_key">Connected API key</option>' not in page
    assert "renderProviderOptionControls" in script
    assert "availableProviderOptions" in script
    assert "if (!addAccount?.open)" in script
    assert "defaultToggle.checked = accounts.length === 0" in script


def test_provider_account_name_is_prefilled_but_preserves_a_custom_name():
    module = (Path(server_module.STATIC_DIR) / "control-plane.js").as_uri()
    script = (
        f"import {{ providerAccountLabelTransition }} from {json.dumps(module)};"
        "const initial = providerAccountLabelTransition({provider: 'codex', value: '', autoLabel: ''});"
        "const changed = providerAccountLabelTransition({provider: 'claude', value: initial.value, autoLabel: initial.autoLabel});"
        "const custom = providerAccountLabelTransition({provider: 'codex', value: 'Research account', autoLabel: changed.autoLabel});"
        "if (initial.value !== 'Personal Codex') process.exit(1);"
        "if (changed.value !== 'Personal Claude') process.exit(2);"
        "if (custom.value !== 'Research account') process.exit(3);"
    )

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_launch_ui_uses_the_sole_ready_personal_account_without_silent_fallback():
    page = (Path(server_module.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    script = (Path(server_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    policy_module = (Path(server_module.STATIC_DIR) / "launch-policy.js").as_uri()

    assert '<option value="personal_required" selected>' in page
    assert '<option value="personal_preferred"' in page
    assert "preferredProviderAccountId(readyAccounts, currentAccount)" in script
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            (
                f"import {{ credentialPolicyTransition, preferredProviderAccountId }} from {json.dumps(policy_module)};"
                "const ready = [{account_id: 'acct-ready', is_default: false}];"
                "const forced = credentialPolicyTransition({currentPolicy: 'personal_required', "
                "savedPersonalPolicy: '', forcedLegacy: false, supportsPersonalAccounts: false});"
                "const restored = credentialPolicyTransition({currentPolicy: forced.value, "
                "savedPersonalPolicy: forced.savedPersonalPolicy, forcedLegacy: forced.forcedLegacy, "
                "supportsPersonalAccounts: true});"
                "process.stdout.write(JSON.stringify({account: preferredProviderAccountId(ready, ''), forced, restored}));"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "account": "acct-ready",
        "forced": {
            "value": "legacy",
            "savedPersonalPolicy": "personal_required",
            "forcedLegacy": True,
        },
        "restored": {
            "value": "personal_required",
            "savedPersonalPolicy": "",
            "forcedLegacy": False,
        },
    }


def test_workspace_delivery_model_exposes_completed_output_without_stale_failure_actions():
    module = (Path(server_module.STATIC_DIR) / "delivery-presenter.js").as_uri()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            (
                f"import {{ workspaceDeliveryModel }} from {json.dumps(module)};"
                "const completed = workspaceDeliveryModel({latest_run:{state:'completed'},latest_output:'Finished the page',"
                "deliverable:{kind:'file',label:'index.html',open_url:'/v1/link-refs/open',download_url:'/v1/link-refs/down'},"
                "artifacts:{items:[{path:'workspace/index.html',content_type:'text/html',open_url:'/v1/link-refs/open',download_url:'/v1/link-refs/down'},"
                "{path:'workspace/preview.png',content_type:'image/png',open_url:'/v1/link-refs/image'}]}});"
                "const failed = workspaceDeliveryModel({latest_run:{state:'failed'},latest_output:'Build failed',"
                "deliverable:{kind:'file',open_url:'/stale'},artifacts:{items:[{path:'stale.txt',open_url:'/stale'}]}});"
                "const hosted = workspaceDeliveryModel({latest_run:{state:'completed'},latest_output:'Hosted page ready',"
                "deliverable:{kind:'webpage',label:'index.html',browser_url:'file:///workspace/project/index.html'},"
                "artifacts:{items:[{path:'workspace/preview.png',open_url:'/v1/link-refs/image'},"
                "{path:'workspace/index.html',content_type:'text/html',open_url:'/v1/link-refs/hosted-open',download_url:'/v1/link-refs/hosted-down'}]}});"
                "const duplicateBasename = workspaceDeliveryModel({latest_run:{state:'completed'},latest_output:'Exact page ready',"
                "deliverable:{kind:'webpage',label:'index.html',workspace_path:'dist/index.html',browser_url:'file:///workspace/project/dist/index.html'},"
                "artifacts:{items:[{path:'docs/index.html',open_url:'/v1/link-refs/wrong'},"
                "{path:'dist/index.html',open_url:'/v1/link-refs/exact',download_url:'/v1/link-refs/exact-down'}]}});"
                "const rootDuplicate = workspaceDeliveryModel({latest_run:{state:'completed'},latest_output:'Root page ready',"
                "deliverable:{kind:'webpage',label:'index.html',workspace_path:'index.html',browser_url:'file:///workspace/project/index.html'},"
                "artifacts:{items:[{path:'docs/index.html',open_url:'/v1/link-refs/root-wrong'},"
                "{path:'index.html',open_url:'/v1/link-refs/root-exact'}]}});"
                "process.stdout.write(JSON.stringify({completed,failed,hosted,duplicateBasename,rootDuplicate}));"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["completed"]["available"] is True
    assert payload["completed"]["summary"] == "Finished the page"
    assert payload["completed"]["primary"]["openUrl"] == "/v1/link-refs/open"
    assert payload["completed"]["primary"]["downloadUrl"] == "/v1/link-refs/down"
    assert [item["label"] for item in payload["completed"]["artifacts"]] == [
        "workspace/index.html",
        "workspace/preview.png",
    ]
    assert payload["failed"] == {
        "available": False,
        "state": "failed",
        "summary": "Build failed",
        "primary": None,
        "artifacts": [],
    }
    assert payload["hosted"]["primary"] == {
        "label": "workspace/index.html",
        "contentType": "text/html",
        "openUrl": "/v1/link-refs/hosted-open",
        "downloadUrl": "/v1/link-refs/hosted-down",
    }
    assert not payload["hosted"]["primary"]["openUrl"].startswith("file:")
    assert payload["duplicateBasename"]["primary"]["label"] == "dist/index.html"
    assert payload["duplicateBasename"]["primary"]["openUrl"] == "/v1/link-refs/exact"
    assert payload["rootDuplicate"]["primary"]["label"] == "index.html"
    assert payload["rootDuplicate"]["primary"]["openUrl"] == "/v1/link-refs/root-exact"


def test_workspace_overview_orders_attention_bounds_previews_and_rehydrates_each_completed_run():
    module = (Path(server_module.STATIC_DIR) / "workspace-overview.js").as_uri()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            (
                f"import {{ compareWorkspacePriority, previewWorkerIds, shouldHydrateWorkspaceDelivery }} from {json.dumps(module)};"
                "const items=["
                "{worker_id:'normal',state:'running',favorite:false},"
                "{worker_id:'favorite',state:'ready',favorite:true},"
                "{worker_id:'failed',state:'failed',favorite:false},"
                "{worker_id:'blocked',state:'action_required',favorite:false}];"
                "items.sort(compareWorkspacePriority);"
                "const previews=previewWorkerIds(Array.from({length:7},(_,i)=>({worker_id:`w${i}`,visible:i!==1,active:true})),3);"
                "const hydration=["
                "shouldHydrateWorkspaceDelivery({runState:'completed',runId:'run-a'}),"
                "shouldHydrateWorkspaceDelivery({runState:'completed',runId:'run-a',hydratedRunId:'run-a'}),"
                "shouldHydrateWorkspaceDelivery({runState:'running',runId:'run-b',hydratedRunId:'run-a'}),"
                "shouldHydrateWorkspaceDelivery({runState:'completed',runId:'run-b',hydratedRunId:'run-a'}),"
                "shouldHydrateWorkspaceDelivery({runState:'completed',legacyLoaded:true})];"
                "process.stdout.write(JSON.stringify({order:items.map(x=>x.worker_id),previews,hydration}));"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "order": ["blocked", "failed", "favorite", "normal"],
        "previews": ["w0", "w2", "w3"],
        "hydration": [True, False, False, True, False],
    }


def test_primary_navigation_remains_visible_at_tablet_and_narrow_desktop_widths():
    styles = (Path(server_module.STATIC_DIR) / "styles.css").read_text(encoding="utf-8")

    assert "@media (max-width: 1100px)" in styles
    assert ".view-tabs {\n    order: 3;\n    width: 100%;\n    flex-wrap: wrap;" in styles
    assert ".current-user-control { width: 100%; flex-wrap: wrap; }" in styles
    assert ".current-user-label { flex: 1 1 100%; max-width: 100%; }" in styles


def test_main_ui_exposes_current_user_and_explicit_account_logout_actions():
    page = (Path(server_module.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    script = (Path(server_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")

    assert 'id="current-user-label"' in page
    assert 'id="switch-account"' in page
    assert 'id="local-sign-out"' in page
    assert "renderCurrentUser(bootstrap.identity || {})" in script
    assert "await signOut('provider')" in script
    assert "await signOut('local')" in script
    assert "'X-GlassHive-CSRF': csrfToken" in script


def test_oidc_identity_owner_advertises_provider_email_login_with_closed_enrollment(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "glasshive-ui")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.test/auth/oidc/callback",
    )
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_LOGIN", "false")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_EMAIL_LOGIN", "true")
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_REGISTRATION", "true")
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "false")
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    config = client.get("/auth/config")
    auth_script = (Path(server_module.STATIC_DIR) / "auth.js").read_text(encoding="utf-8")

    assert config.status_code == 200
    assert config.json() == {
        "mode": "oidc",
        "email_login": True,
        "email_registration": False,
        "provider_email_login": True,
        "local_password_login": False,
        "local_password_signup": False,
        "principal_enrollment": False,
        "identity_owner": "external_provider",
        "oidc": True,
        "oidc_login_visible": True,
        "login_methods": ["oidc"],
    }
    assert "Continue with email or organization" in auth_script
    assert "create an account if needed" not in auth_script
    assert "Need access? Contact your administrator." in auth_script
    assert client.get("/auth/email/login").status_code == 404
    assert client.get("/auth/email/register").status_code == 404


def test_local_password_login_is_explicit_same_origin_and_has_no_signup_surface(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "glasshive-ui")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.test/auth/oidc/callback",
    )
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    monkeypatch.setenv("GLASSHIVE_OIDC_LOGIN_VISIBLE", "false")
    monkeypatch.setenv(
        "GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY",
        "synthetic-throttle-key-for-server-tests",
    )
    gateway = server_module.HumanAuthGateway.from_env()
    principal = gateway.preapprove_oidc_principal(subject="local-browser-subject", role="member")
    gateway.provision_local_password(
        subject="local-browser-subject",
        login_email="browser-login@example.invalid",
        password="browser synthetic passphrase",
    )
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    config = client.get("/auth/config")
    page = client.get("/login")
    login_csrf = client.cookies.get("glasshive_login_csrf")

    assert config.json()["local_password_login"] is True
    assert config.json()["local_password_signup"] is False
    assert config.json()["oidc"] is True
    assert config.json()["oidc_login_visible"] is False
    assert config.json()["login_methods"] == ["local_password"]
    assert 'id="local-login"' in page.text
    assert '/static/auth.js?v=20260922a' in page.text
    assert page.headers["cache-control"] == "no-store, no-cache, private, max-age=0"
    assert login_csrf
    assert client.get("/auth/email/register").status_code == 404
    assert client.post("/auth/email/register", json={}).status_code == 404
    assert client.get("/auth/email/reset").status_code == 404
    assert client.post("/auth/email/reset", json={}).status_code == 404

    no_origin = client.post(
        "/auth/email/login",
        headers={"X-GlassHive-CSRF": login_csrf},
        json={
            "email": "browser-login@example.invalid",
            "password": "browser synthetic passphrase",
            "return_to": "/workspaces",
        },
    )
    no_csrf = client.post(
        "/auth/email/login",
        headers={"Origin": "http://testserver"},
        json={
            "email": "browser-login@example.invalid",
            "password": "browser synthetic passphrase",
            "return_to": "/workspaces",
        },
    )
    assert no_origin.status_code == 403
    assert no_csrf.status_code == 403

    unknown = client.post(
        "/auth/email/login",
        headers={
            "Origin": "http://testserver",
            "X-GlassHive-CSRF": login_csrf,
        },
        json={"email": "unknown@example.invalid", "password": "wrong synthetic passphrase"},
    )
    wrong = client.post(
        "/auth/email/login",
        headers={
            "Origin": "http://testserver",
            "X-GlassHive-CSRF": login_csrf,
        },
        json={"email": "browser-login@example.invalid", "password": "wrong synthetic passphrase"},
    )
    assert (unknown.status_code, unknown.json()) == (wrong.status_code, wrong.json()) == (
        401,
        {"detail": "Email or password is incorrect"},
    )

    original_authenticate = server_module.HumanAuthGateway.authenticate_local_password
    monkeypatch.setattr(
        server_module.HumanAuthGateway,
        "authenticate_local_password",
        lambda _self, **_kwargs: (_ for _ in ()).throw(
            AuthGatewayError("busy", code="sign_in_busy")
        ),
    )
    busy = client.post(
        "/auth/email/login",
        headers={"Origin": "http://testserver", "X-GlassHive-CSRF": login_csrf},
        json={
            "email": "browser-login@example.invalid",
            "password": "browser synthetic passphrase",
        },
    )
    assert busy.status_code == 503
    assert busy.json() == {"detail": "Sign-in is temporarily busy; retry shortly"}
    monkeypatch.setattr(
        server_module.HumanAuthGateway,
        "authenticate_local_password",
        original_authenticate,
    )

    for malformed_body in (
        b'{"email":"broken\\ud800@example.invalid","password":"synthetic password value"}',
        b'{"email":"browser-login@example.invalid","password":"broken\\ud800"}',
        b'{"email":"browser-login@example.invalid","password":"browser synthetic passphrase","return_to":"/broken\\ud800"}',
    ):
        malformed = client.post(
            "/auth/email/login",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://testserver",
                "X-GlassHive-CSRF": login_csrf,
            },
            content=malformed_body,
        )
        assert malformed.status_code == 400
        assert malformed.json() == {"detail": "Invalid sign-in request"}
    with sqlite3.connect(gateway.state_path) as connection:
        assert connection.execute("SELECT count(*) FROM auth_local_sessions").fetchone() == (0,)

    accepted = client.post(
        "/auth/email/login",
        headers={
            "Origin": "http://testserver",
            "X-GlassHive-CSRF": login_csrf,
        },
        json={
            "email": "browser-login@example.invalid",
            "password": "browser synthetic passphrase",
            "return_to": "/workspaces",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"authenticated": True, "redirect_url": "/workspaces"}
    session = client.get("/auth/session").json()
    assert session["authenticated"] is True
    assert session["user_id"] == principal["user_id"]
    assert session["auth_method"] == "local_password"
    bootstrap = client.get("/api/bootstrap").json()
    assert bootstrap["identity"]["auth_method"] == "local_password"
    assert bootstrap["identity"]["provider_switch_visible"] is False


def test_login_dom_uses_visible_methods_without_stale_provider_copy():
    page = (Path(server_module.STATIC_DIR) / "login.html").read_text(encoding="utf-8")
    script = (Path(server_module.STATIC_DIR) / "auth.js").read_text(encoding="utf-8")
    app_script = (Path(server_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")

    assert 'id="auth-footnote"' in page
    assert "config.login_methods" in script
    assert "loginMethods.has('oidc')" in script
    assert "loginMethods.has('local_password')" in script
    assert "authDivider.hidden = !(oidcVisible && localVisible)" in script
    assert "identity.provider_switch_visible" in app_script
    assert "switchAccount.hidden" in app_script


class _FakeOidcHumanAuth:
    mode = "oidc"
    session_enabled = True
    allowed_email_domains: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.completed: object = None
        self.revoked: list[str] = []
        self.provider_logout = ""

    def resolve_session(self, token):
        if token != "opaque-session":
            return None
        return {
            "tenant_id": "local",
            "user_id": "stable-user-id",
            "email": "member@example.invalid",
            "display_name": "Example Member",
            "role": "member",
            "_csrf_hash": "unused-by-fake",
        }

    def session_csrf_valid(self, session, supplied):
        return bool(session and supplied == "synthetic-csrf")

    def begin_oidc(self, *, return_to="/"):
        if isinstance(self.completed, AuthGatewayError) and self.completed.code == "provider_unavailable":
            raise self.completed
        return {
            "authorization_url": "https://identity.example.invalid/authorize",
            "state": "opaque-state",
            "nonce": "opaque-nonce",
        }

    def complete_oidc(self, *, state, code):
        if isinstance(self.completed, AuthGatewayError):
            raise self.completed
        return {
            "principal": {
                "user_id": "stable-user-id",
                "email": "member@example.invalid",
                "display_name": "Example Member",
                "role": "member",
            },
            "return_to": "/workspaces",
        }

    def create_session(self, principal_id):
        assert principal_id == "stable-user-id"
        return {
            "token": "new-session",
            "csrf_token": "new-csrf",
            "expires_at": server_module.time.time() + 3600,
        }

    def revoke_session(self, token):
        self.revoked.append(token)

    def provider_logout_url(self):
        return self.provider_logout

    def email_allowed(self, email):
        return True


def test_oidc_unhappy_paths_redirect_to_bounded_retry_ux_without_callback_secrets(
    monkeypatch,
    caplog,
):
    auth = _FakeOidcHumanAuth()
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: auth)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    with caplog.at_level(logging.INFO):
        denied = client.get(
            "/auth/oidc/callback?error=access_denied&error_description=outside-domain-secret",
            follow_redirects=False,
        )
    assert denied.status_code == 303
    assert denied.headers["location"] == "/login?auth_error=access_denied"
    assert "outside-domain-secret" not in denied.headers["location"]
    assert "outside-domain-secret" not in caplog.text

    cancelled = client.get(
        "/auth/oidc/callback?error=user_cancelled&error_description=private-provider-copy",
        follow_redirects=False,
    )
    assert cancelled.headers["location"] == "/login?auth_error=cancelled"

    invalid_state = client.get(
        "/auth/oidc/callback?state=attacker-state&code=private-code",
        follow_redirects=False,
    )
    assert invalid_state.headers["location"] == "/login?auth_error=state_invalid"

    auth.completed = AuthGatewayError("expired or replayed", code="state_expired")
    client.cookies.set("glasshive_oidc_state", "opaque-state")
    replayed = client.get(
        "/auth/oidc/callback?state=opaque-state&code=private-code",
        follow_redirects=False,
    )
    assert replayed.headers["location"] == "/login?auth_error=state_expired"

    auth.completed = AuthGatewayError("issuer or audience mismatch", code="token_invalid")
    client.cookies.set("glasshive_oidc_state", "opaque-state")
    invalid_token = client.get(
        "/auth/oidc/callback?state=opaque-state&code=another-private-code",
        follow_redirects=False,
    )
    assert invalid_token.headers["location"] == "/login?auth_error=token_invalid"

    auth.completed = AuthGatewayError("provider outage", code="provider_unavailable")
    unavailable = client.get("/auth/oidc/start", follow_redirects=False)
    assert unavailable.headers["location"] == "/login?auth_error=provider_unavailable"

    auth.completed = None
    client.cookies.set("glasshive_oidc_state", "opaque-state")
    retried = client.get(
        "/auth/oidc/callback?state=opaque-state&code=retry-private-code",
        follow_redirects=False,
    )
    assert retried.status_code == 303
    assert retried.headers["location"] == "/workspaces"
    assert "glasshive_session=new-session" in retried.headers["set-cookie"]


def test_login_dom_has_bounded_actionable_error_copy_and_preserves_safe_return_target():
    auth_script = (Path(server_module.STATIC_DIR) / "auth.js").read_text(encoding="utf-8")
    login_page = (Path(server_module.STATIC_DIR) / "login.html").read_text(encoding="utf-8")

    for code in (
        "access_denied",
        "account_not_authorized",
        "account_not_registered",
        "cancelled",
        "provider_unavailable",
        "state_expired",
        "state_invalid",
        "token_invalid",
    ):
        assert f"{code}:" in auth_script
    assert "error_description" not in auth_script
    assert "encodeURIComponent(returnTo)" in auth_script
    assert 'id="auth-status"' in login_page


@pytest.mark.parametrize(
    ("mcp_url", "canonical", "callback_uri"),
    [
        (
            "https://glasshive.example.com/mcp",
            "https://glasshive.example.com/mcp",
            "http://127.0.0.1:49153/callback/t-bKRAz0k2fk",
        ),
        (
            "https://GLASSHIVE.EXAMPLE.TEST:443/mcp",
            "https://glasshive.example.test/mcp",
            "http://127.0.0.1:49153/callback/0MLa49XNV_Yw",
        ),
        (
            "https://glasshive.example.test/a%2fb/%7Euser",
            "https://glasshive.example.test/a%2fb/%7Euser",
            "http://127.0.0.1:49153/callback/4UHUnvnRKbLF",
        ),
        (
            "https://glasshive.example.test/mcp?x=1#ignored",
            "https://glasshive.example.test/mcp?x=1",
            "http://127.0.0.1:49153/callback/UEcxre_7-GPO",
        ),
    ],
)
def test_codex_callback_uri_matches_rust_url_canonicalization(
    mcp_url,
    canonical,
    callback_uri,
):
    assert server_module._canonical_codex_server_url(mcp_url) == canonical
    assert server_module._codex_oauth_callback_uri(mcp_url, 49153) == callback_uri


def test_logout_is_csrf_protected_and_distinguishes_local_from_provider_scope(monkeypatch):
    auth = _FakeOidcHumanAuth()
    auth.provider_logout = (
        "https://identity.example.invalid/logout?post_logout_redirect_uri="
        "https%3A%2F%2Fglasshive.example.invalid%2Flogin"
    )
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: auth)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    client.cookies.set("glasshive_session", "opaque-session")
    client.cookies.set("glasshive_csrf", "synthetic-csrf")

    rejected = client.post("/auth/logout", json={"scope": "provider"})
    assert rejected.status_code == 403

    switched = client.post(
        "/auth/logout",
        headers={"X-GlassHive-CSRF": "synthetic-csrf"},
        json={"scope": "provider"},
    )
    assert switched.status_code == 200
    assert switched.json() == {
        "authenticated": False,
        "logout_scope": "provider",
        "redirect_url": auth.provider_logout,
    }
    assert auth.revoked == ["opaque-session"]

    client.cookies.set("glasshive_session", "opaque-session")
    client.cookies.set("glasshive_csrf", "synthetic-csrf")
    local = client.post(
        "/auth/logout",
        headers={"X-GlassHive-CSRF": "synthetic-csrf"},
        json={"scope": "local"},
    )
    assert local.json()["logout_scope"] == "local"
    assert local.json()["redirect_url"] == "/login?logged_out=local"


def test_unauthenticated_ui_routes_preserve_safe_deep_links_but_not_signed_secrets(monkeypatch):
    auth = _FakeOidcHumanAuth()
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: auth)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    home = client.get("/", follow_redirects=False)
    watch = client.get(
        "/watch/wrk_1?project_id=prj_1&surface=desktop",
        follow_redirects=False,
    )
    project = client.get(
        "/ui/projects/prj_1?worker_id=wrk_1",
        follow_redirects=False,
    )

    assert home.headers["location"] == "/login?return_to=%2F"
    assert "return_to=%2Fwatch%2Fwrk_1%3Fproject_id%3Dprj_1%26surface%3Ddesktop" in watch.headers["location"]
    assert "return_to=%2Fui%2Fprojects%2Fprj_1%3Fworker_id%3Dwrk_1" in project.headers["location"]

    # A signed-link attempt remains on the signed-link path and is never copied into
    # a login return URL, even when the token is invalid or expired.
    signed = client.get(
        "/watch/wrk_1?gh_token=private-signed-token",
        follow_redirects=False,
    )
    assert signed.status_code == 401
    assert "location" not in signed.headers


def test_session_authenticated_writes_reject_cross_origin_even_with_valid_csrf(monkeypatch):
    class FakeHumanAuth:
        mode = "oidc"
        session_enabled = True

        def resolve_session(self, token):
            if token != "opaque-session":
                return None
            return {
                "tenant_id": "local",
                "user_id": "user-public-safe",
                "email": "member@example.invalid",
                "role": "member",
                "csrf_token": "synthetic-csrf",
            }

        def session_csrf_valid(self, session, supplied):
            return bool(session and supplied == "synthetic-csrf")

    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: FakeHumanAuth())
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    client.cookies.set("glasshive_session", "opaque-session")
    client.cookies.set("glasshive_csrf", "synthetic-csrf")

    response = client.post(
        "/api/provider-accounts",
        headers={"Origin": "https://attacker.example.invalid", "X-GlassHive-CSRF": "synthetic-csrf"},
        json={"provider": "codex", "label": "Unsafe", "auth_method": "subscription"},
    )

    assert response.status_code == 403
    assert "origin" in response.json()["detail"].lower()
    assert runtime.provider_account_requests == []


def test_oidc_csrf_preserves_only_valid_signed_link_communication(tmp_path, monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: _FakeOidcHumanAuth())
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token(signed_secret)

    message = client.post(
        f"/api/worker/wrk_1/message?gh_token={token}",
        headers={"Origin": "http://testserver"},
        json={"message": "Share a concise progress update."},
    )
    steer = client.post(
        f"/api/workspace/wrk_1/steer?gh_token={token}",
        headers={"Origin": "http://testserver"},
        json={"message": "Focus on the requested output."},
    )
    forbidden_metadata = client.patch(
        f"/api/worker/wrk_1/metadata?gh_token={token}",
        headers={"Origin": "http://testserver"},
        json={"favorite": True},
    )
    forged_message = client.post(
        "/api/worker/wrk_1/message?gh_token=not-a-valid-signed-link",
        headers={"Origin": "http://testserver"},
        json={"message": "Do not deliver this."},
    )
    cross_origin = client.post(
        f"/api/worker/wrk_1/message?gh_token={token}",
        headers={"Origin": "https://attacker.example.invalid"},
        json={"message": "Do not deliver this either."},
    )

    assert message.status_code == 200
    assert steer.status_code == 200
    assert forbidden_metadata.status_code == 403
    assert "read-only" in forbidden_metadata.json()["detail"].lower()
    assert forged_message.status_code == 403
    assert "csrf" in forged_message.json()["detail"].lower()
    assert cross_origin.status_code == 403
    assert "origin" in cross_origin.json()["detail"].lower()
    assert runtime.message_requests == [
        {"worker_id": "wrk_1", "message": "Share a concise progress update."}
    ]
    assert runtime.steer_requests == [
        {"worker_id": "wrk_1", "message": "Focus on the requested output."}
    ]
    assert runtime.metadata_requests == []

    forged_link_response = client.post(
        "/api/provider-accounts?gh_token=not-a-valid-signed-link",
        json={"provider": "codex", "label": "Unsafe", "auth_method": "subscription"},
    )

    assert forged_link_response.status_code == 403
    assert "csrf" in forged_link_response.json()["detail"].lower()
    assert runtime.provider_account_requests == []


def test_trusted_proxy_writes_reject_cross_origin(tmp_path, monkeypatch):
    _configure_multi_user_connect_test(tmp_path, monkeypatch)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post(
        "/api/worker/wrk_1/metadata",
        headers={**_multi_user_headers(), "Origin": "https://attacker.example.invalid"},
        json={"favorite": True},
    )

    assert response.status_code == 403
    assert "origin" in response.json()["detail"].lower()
    assert runtime.metadata_requests == []


def test_oidc_mode_cannot_trust_raw_inbound_identity_headers(tmp_path, monkeypatch):
    _configure_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")

    with pytest.raises(RuntimeError, match="cannot trust inbound identity"):
        create_app(runtime_client=FakeRuntimeClient())


def test_worker_steer_endpoint_uses_runtime_steer():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/steer', json={'message': 'Redirect to the new plan now.'})
    assert response.status_code == 200
    assert runtime.steer_requests == [{'worker_id': 'wrk_1', 'message': 'Redirect to the new plan now.'}]


def test_worker_message_endpoint_uses_runtime_queue_message():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/message', json={'message': 'Queue this after the current run finishes.'})
    assert response.status_code == 200
    assert runtime.message_requests == [{'worker_id': 'wrk_1', 'message': 'Queue this after the current run finishes.'}]


@pytest.mark.parametrize("route", [
    "/api/worker/wrk_1/message",
    "/api/workspace/wrk_1/message",
    "/api/worker/wrk_1/steer",
    "/api/workspace/wrk_1/steer",
])
def test_worker_message_and_steer_preserve_closed_workspace_conflict(route):
    class ClosedWorkspaceRuntime(FakeRuntimeClient):
        def message(self, worker_id: str, message: str):
            return self._closed(worker_id, "message")

        def steer(self, worker_id: str, message: str):
            return self._closed(worker_id, "steer")

        @staticmethod
        def _closed(worker_id: str, action: str):
            response = httpx.Response(
                409,
                json={"detail": "Workspace is closed; create a new workspace for new work"},
                request=httpx.Request("POST", f"http://runtime.test/v1/workers/{worker_id}/{action}"),
            )
            raise httpx.HTTPStatusError("closed", request=response.request, response=response)

    runtime = ClosedWorkspaceRuntime()
    response = TestClient(create_app(runtime_client=runtime)).post(
        route,
        json={"message": "Do not strand this work."},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace is closed; create a new workspace for new work"
    assert runtime.message_requests == []
    assert runtime.steer_requests == []


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
def test_pending_workspace_change_preserves_closed_workspace_conflict(operation):
    class ClosedWorkspaceRuntime(FakeRuntimeClient):
        @staticmethod
        def _closed(path: str):
            response = httpx.Response(
                409,
                json={"detail": "Workspace is closed; create a new workspace for new work"},
                request=httpx.Request("POST", f"http://runtime.test{path}"),
            )
            raise httpx.HTTPStatusError(
                "closed",
                request=response.request,
                response=response,
            )

        def create_pending_change(self, payload: dict):
            _ = payload
            return self._closed("/v1/pending-changes")

        def confirm_pending_change(self, change_id: str, confirmation_token: str):
            _ = confirmation_token
            return self._closed(f"/v1/pending-changes/{change_id}/confirm")

    runtime = ClosedWorkspaceRuntime()
    client = TestClient(create_app(runtime_client=runtime), raise_server_exceptions=False)
    if operation == "prepare":
        response = client.post(
            "/api/pending-changes",
            json={
                "change_type": "workspace_provider_account",
                "target_id": "wrk_1",
                "payload": {"policy": "legacy"},
            },
        )
    else:
        response = client.post(
            "/api/pending-changes/chg_1/confirm",
            json={"confirmation_token": "synthetic-confirmation-token"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace is closed; create a new workspace for new work"


@pytest.mark.parametrize("destination",["new:codex-cli","open:wrk_1","duplicate:wrk_1"])
def test_legacy_inline_upload_transport_is_rejected_before_any_workspace_change(destination):
    runtime=FakeRuntimeClient()
    response=TestClient(create_app(runtime_client=runtime)).post('/api/launch',json={
        'description':'Use attached file','workspace_option':destination,
        'files':[{'name':'brief.txt','size':3,'content_base64':'YWJj'}],
    })
    assert response.status_code==422
    assert response.json()['detail']['code']=='legacy_upload_transport'
    assert runtime.create_worker_requests==[]
    assert runtime.workspace_duplicate_requests==[]
    assert runtime.assign_requests==[]
    assert runtime.file_upload_requests==[]


def test_launch_keeps_every_selected_draft_reference_without_default_count_cap(monkeypatch):
    monkeypatch.delenv('GLASSHIVE_UI_UPLOAD_MAX_FILES',raising=False)
    runtime=FakeRuntimeClient()
    ids=[f'fil_{index}' for index in range(16)]
    response=TestClient(create_app(runtime_client=runtime)).post('/api/launch',json={
        'description':'Use every selected input','workspace_option':'new:codex-cli',
        'file_upload_ids':ids,'idempotency_key':'all-selected-files',
    })
    assert response.status_code==200,response.text
    assert runtime.file_upload_requests[0][2]['upload_ids']==ids


def test_watch_file_upload_proxies_exact_bytes_without_resuming_worker():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.put('/api/file-uploads/fil_1/content', content=b'\x00exact\xff')
    assert response.status_code == 200
    assert runtime.file_upload_requests == [('fil_1', b'\x00exact\xff')]
    assert runtime.lifecycle_requests == []
    assert runtime.message_requests == []


def test_draft_file_management_proxies_revision_bound_streams_and_patch():
    from contextlib import asynccontextmanager
    from urllib.parse import parse_qs, urlsplit

    runtime = FakeRuntimeClient()

    @asynccontextmanager
    async def download(path, headers, **_kwargs):
        runtime.file_upload_requests.append(('download', path, headers))
        yield httpx.Response(200, content=b'file bytes', request=httpx.Request('GET', 'http://runtime.test'))

    runtime.stream_file_download = download
    client = TestClient(create_app(runtime_client=runtime))
    changed = client.patch('/api/file-uploads/fil_1', json={'relative_path': 'notes/renamed.txt', 'revision': 'rev-1'})
    assert changed.status_code == 200, changed.text
    assert runtime.file_upload_requests[-1] == (
        'PATCH', '/v1/file-uploads/fil_1', {'relative_path': 'notes/renamed.txt', 'revision': 'rev-1'}
    )
    assert client.get('/api/file-uploads/fil_1').status_code == 200
    assert runtime.file_upload_requests[-1] == ('GET', '/v1/file-uploads/fil_1', None)
    content = client.get('/api/file-uploads/fil_1/content?revision=rev-2')
    assert content.status_code == 200 and content.content == b'file bytes'
    assert runtime.file_upload_requests[-1][1] == '/v1/file-uploads/fil_1/content?revision=rev-2'
    exported = client.get('/api/file-uploads/export?upload_ids=fil_1&upload_ids=fil_2&revisions=rev-2&revisions=rev-3')
    assert exported.status_code == 200 and exported.content == b'file bytes'
    path = runtime.file_upload_requests[-1][1]
    assert urlsplit(path).path == '/v1/file-uploads/export'
    assert parse_qs(urlsplit(path).query) == {'upload_ids': ['fil_1', 'fil_2'], 'revisions': ['rev-2', 'rev-3']}
    assert client.get('/api/file-uploads/export?upload_ids=fil_1&upload_ids=fil_2&revisions=rev-2').status_code == 422
    assert client.get('/api/file-uploads/fil_1/content').status_code == 422


def test_draft_management_rejects_legacy_signed_link_without_cookie(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token('ui-signed-link-secret')
    paths = (
        '/api/file-uploads/fil_1',
        '/api/file-uploads/fil_1/content?revision=rev-1',
        '/api/file-uploads/export?upload_ids=fil_1&revisions=rev-1',
    )
    for path in paths:
        separator = '&' if '?' in path else '?'
        assert client.get(f'{path}{separator}gh_token={token}').status_code == 401
        assert client.get(path, headers={'Referer': f'http://testserver/watch/wrk_1?gh_token={token}'}).status_code == 401
    assert runtime.file_upload_requests == []


def test_draft_native_zip_form_requires_same_origin_session_csrf(monkeypatch):
    from contextlib import asynccontextmanager

    class FakeHumanAuth:
        mode = 'oidc'
        session_enabled = True

        def resolve_session(self, token):
            if token != 'opaque-session':
                return None
            return {'tenant_id': 'local', 'user_id': 'owner-a', 'role': 'member'}

        def session_csrf_valid(self, session, supplied):
            return bool(session and supplied == 'synthetic-csrf')

    monkeypatch.setattr(server_module.HumanAuthGateway, 'from_env', lambda: FakeHumanAuth())
    runtime = FakeRuntimeClient()

    @asynccontextmanager
    async def download(path, headers, **kwargs):
        runtime.file_upload_requests.append(('download', path, headers, kwargs))
        yield httpx.Response(200, content=b'zip bytes', request=httpx.Request('POST', 'http://runtime.test'))

    runtime.stream_file_download = download
    client = TestClient(create_app(runtime_client=runtime))
    client.cookies.set('glasshive_session', 'opaque-session')
    client.cookies.set('glasshive_csrf', 'synthetic-csrf')
    headers = {'Origin': 'http://testserver', 'Content-Type': 'application/x-www-form-urlencoded'}
    fields = 'upload_ids=fil_1&revisions=rev-1'
    assert client.post('/api/file-uploads/export', content=fields, headers=headers).status_code == 403
    assert client.post('/api/file-uploads/export', content=fields + '&draft_export_csrf=wrong', headers=headers).status_code == 403
    assert client.post('/api/file-uploads/export', content=fields + '&draft_export_csrf=synthetic-csrf',
                       headers={**headers, 'Origin': 'https://attacker.example.invalid'}).status_code == 403
    assert runtime.file_upload_requests == []
    response = client.post('/api/file-uploads/export', content=fields + '&draft_export_csrf=synthetic-csrf', headers=headers)
    assert response.status_code == 200 and response.content == b'zip bytes'
    assert runtime.file_upload_requests[-1] == ('download', '/v1/file-uploads/export', {}, {
        'method': 'POST', 'payload': {'upload_ids': ['fil_1'], 'revisions': ['rev-1']}
    })


def test_read_only_watch_cannot_upload_files(monkeypatch):
    set_enterprise_ui_env(monkeypatch, signed_secret='ui-signed-link-secret')
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token('ui-signed-link-secret')
    response = client.post(f'/api/workspace/wrk_1/files?gh_token={token}', json={'upload_ids':['fil_1'],'idempotency_key':'binding'})
    assert response.status_code == 403
    assert runtime.file_upload_requests == []


def test_file_routes_require_session_or_scoped_worker_cookie(monkeypatch):
    from contextlib import asynccontextmanager

    set_enterprise_ui_env(monkeypatch)
    runtime = FakeRuntimeClient()

    @asynccontextmanager
    async def download(path, headers, **_kwargs):
        runtime.file_upload_requests.append(('download', path, headers))
        yield httpx.Response(200, content=b'owned bytes', request=httpx.Request('GET', 'http://runtime.test'))

    runtime.stream_file_download = download
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token('ui-signed-link-secret')
    listing = '/api/workspace/wrk_1/files'
    content = '/api/workspace/wrk_1/files/fen_1/content?revision=rev-1'
    for path in (listing, content):
        separator = '&' if '?' in path else '?'
        assert client.get(f'{path}{separator}gh_token={token}').status_code == 401
        assert client.get(path, headers={'Referer': f'http://testserver/watch/wrk_1?gh_token={token}'}).status_code == 401
    assert runtime.file_upload_requests == []

    assert client.get(f'/watch/wrk_1?gh_token={token}').status_code == 200
    assert worker_cookie_name('wrk_1') in client.cookies
    assert client.get(listing).status_code == 200
    downloaded = client.get(content)
    assert downloaded.status_code == 200 and downloaded.content == b'owned bytes'
    assert runtime.file_upload_requests[-1][1].endswith('/fen_1/content?revision=rev-1')
    assert client.get('/api/workspace/wrk_1/files/trash').status_code == 403
    assert client.patch('/api/workspace/wrk_1/files/fen_1', json={'path': 'other.txt', 'revision': 'rev-1'}).status_code == 403
    assert client.get('/api/workspace/wrk_2/files').status_code == 401
    assert client.get('/api/file-uploads?draft_id=draft').status_code == 401
    assert client.get('/api/storage').status_code == 401


def test_file_trash_read_requires_member_write_assertion(tmp_path, monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    private_key = configure_internal_assertion_signer(tmp_path, monkeypatch)
    monkeypatch.setenv('GLASSHIVE_TRUST_INBOUND_IDENTITY', 'true')
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.get('/api/workspace/wrk_1/files/trash', headers={
        'X-Viventium-Tenant-Id': 'tenant-alpha',
        'X-Viventium-User-Id': 'user-a',
        'X-Viventium-User-Role': 'member',
    })
    assert response.status_code == 200, response.text
    claims = jwt.decode(
        runtime.header_contexts[-1]['X-GlassHive-User-Assertion'],
        private_key.public_key(), algorithms=['RS256'],
        audience='glasshive-runtime', issuer='https://gateway.example.invalid',
    )
    assert 'workspaces:write' in claims['scope'].split()
    viewer = TestClient(client.app)
    token = signed_worker_token('ui-signed-link-secret')
    assert viewer.get(f'/watch/wrk_1?gh_token={token}').status_code == 200
    result = viewer.get('/api/workspace/wrk_1/files')
    assert result.status_code == 200
    view_claims = jwt.decode(
        runtime.header_contexts[-1]['X-GlassHive-User-Assertion'],
        private_key.public_key(), algorithms=['RS256'],
        audience='glasshive-runtime', issuer='https://gateway.example.invalid',
    )
    assert 'workspaces:write' not in view_claims['scope'].split()


def test_schedule_project_creates_worker_without_starting_and_persists_schedule():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Check the workspace later',
        'success_criteria': 'The later check is queued',
        'context': '',
        'workspace_option': 'new:codex-cli',
        'schedule_text': 'in 20 minutes',
    })

    assert response.status_code == 200
    assert response.json()['status'] == 'scheduled'
    assert response.json()['schedule_id'] == 'sch_1'
    assert runtime.create_worker_requests[-1]['start_synchronously'] is False
    assert runtime.assign_requests == []
    assert runtime.schedule_requests[-1]['schedule_text'] == 'in 20 minutes'


def test_worker_metadata_endpoint_updates_favorite():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/worker/wrk_1/metadata', json={'favorite': True})

    assert response.status_code == 200
    assert runtime.metadata_requests == [{'worker_id': 'wrk_1', 'payload': {'favorite': True}}]


def test_enterprise_bff_signs_short_lived_internal_user_assertion(tmp_path, monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    private_key = configure_internal_assertion_signer(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        "/api/bootstrap",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Email": "user-a@example.invalid",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200, response.text
    runtime_headers = runtime.header_contexts[-1]
    assert runtime_headers["X-WPR-Token"] == "ui-service-secret"
    assert "X-Viventium-User-Id" not in runtime_headers
    assertion = runtime_headers["X-GlassHive-User-Assertion"]
    claims = jwt.decode(
        assertion,
        private_key.public_key(),
        algorithms=["RS256"],
        audience="glasshive-runtime",
        issuer="https://gateway.example.invalid",
    )
    assert claims["sub"] == "user-a"
    assert claims["tenant_id"] == "tenant-alpha"
    assert claims["role"] == "member"
    assert set(claims["scope"].split()) >= {"runtime:access", "workspaces:read"}
    assert claims["exp"] - claims["iat"] <= 90
    assert claims["jti"]

    confirm = client.post(
        "/api/pending-changes/chg_1/confirm",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Email": "user-a@example.invalid",
            "X-Viventium-User-Role": "member",
        },
        json={"confirmation_token": "synthetic-confirmation-token"},
    )
    assert confirm.status_code == 200
    confirm_claims = jwt.decode(
        runtime.header_contexts[-1]["X-GlassHive-User-Assertion"],
        private_key.public_key(),
        algorithms=["RS256"],
        audience="glasshive-runtime",
        issuer="https://gateway.example.invalid",
    )
    assert "human:confirm" in confirm_claims["scope"].split()

    metadata = client.post(
        "/api/worker/wrk_1/metadata",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
        json={"favorite": True},
    )
    assert metadata.status_code == 200
    metadata_claims = jwt.decode(
        runtime.header_contexts[-1]["X-GlassHive-User-Assertion"],
        private_key.public_key(),
        algorithms=["RS256"],
        audience="glasshive-runtime",
        issuer="https://gateway.example.invalid",
    )
    assert "human:confirm" not in metadata_claims["scope"].split()


def test_signed_workspace_link_assertion_has_viewer_communication_scope_only(tmp_path, monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    private_key = configure_internal_assertion_signer(tmp_path, monkeypatch)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token(signed_secret)

    response = client.post(
        f"/api/worker/wrk_1/message?gh_token={token}",
        json={"message": "Share a concise progress update."},
    )

    assert response.status_code == 200
    claims = jwt.decode(
        runtime.header_contexts[-1]["X-GlassHive-User-Assertion"],
        private_key.public_key(),
        algorithms=["RS256"],
        audience="glasshive-runtime",
        issuer="https://gateway.example.invalid",
    )
    scopes = set(claims["scope"].split())
    assert claims["role"] == "viewer"
    assert "workspaces:communicate" in scopes
    assert "workspaces:write" not in scopes
    assert "runtime:internal_details" not in scopes


def test_internal_assertion_jwks_publishes_public_key_only(tmp_path, monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["keys"][0]["kid"] == "gateway-test-key"
    assert payload["keys"][0]["kty"] == "RSA"
    assert "d" not in payload["keys"][0]


def test_internal_assertion_jwks_keeps_previous_public_key_for_bounded_rotation(
    tmp_path,
    monkeypatch,
):
    set_enterprise_ui_env(monkeypatch)
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    previous_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    previous_public_jwk = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(previous_private_key.public_key())
    )
    previous_public_jwk.update(
        {"kid": "gateway-previous-key", "use": "sig", "alg": "RS256"}
    )
    previous_jwks_path = tmp_path / "gateway-previous-public-jwks.json"
    previous_jwks_path.write_text(
        json.dumps({"keys": [previous_public_jwk]}),
        encoding="utf-8",
    )
    now = 2_000_000_000
    monkeypatch.setattr(internal_assertions_module.time, "time", lambda: now)
    monkeypatch.setenv(
        "GLASSHIVE_INTERNAL_ASSERTION_PREVIOUS_JWKS_FILE",
        str(previous_jwks_path),
    )
    monkeypatch.setenv(
        "GLASSHIVE_INTERNAL_ASSERTION_PREVIOUS_KEYS_EXPIRE_AT",
        str(now + 600),
    )
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    overlapping = client.get("/.well-known/jwks.json")
    assert overlapping.status_code == 200
    assert [key["kid"] for key in overlapping.json()["keys"]] == [
        "gateway-test-key",
        "gateway-previous-key",
    ]
    assert all("d" not in key for key in overlapping.json()["keys"])

    monkeypatch.setattr(internal_assertions_module.time, "time", lambda: now + 601)
    expired = client.get("/.well-known/jwks.json")
    assert expired.status_code == 200
    assert [key["kid"] for key in expired.json()["keys"]] == ["gateway-test-key"]


def test_internal_assertion_mode_fails_closed_without_dedicated_private_key(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "signed_internal_assertion")
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_ISSUER", "https://gateway.example.invalid")
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE", "glasshive-runtime")

    with pytest.raises(RuntimeError, match="private signing key"):
        create_app(runtime_client=FakeRuntimeClient())


def test_multi_user_security_mode_rejects_plaintext_trusted_proxy_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    monkeypatch.setenv("GLASSHIVE_TRUSTED_PROXY_BOUNDARY_PROVEN", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    monkeypatch.delenv("GLASSHIVE_AUTH_MODE", raising=False)

    with pytest.raises(RuntimeError, match="requires built-in OIDC"):
        create_app(runtime_client=FakeRuntimeClient())


def test_multi_user_security_mode_refuses_disabled_human_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    monkeypatch.delenv("GLASSHIVE_AUTH_MODE", raising=False)

    with pytest.raises(RuntimeError, match="human auth"):
        create_app(runtime_client=FakeRuntimeClient())


def test_tenant_admin_can_disable_another_user_but_not_self(monkeypatch):
    mutation_order = []

    class FakeHumanAuth:
        mode = "oidc"
        session_enabled = True
        allowed_email_domains = ()

        def __init__(self):
            self.updated = []

        def resolve_session(self, token):
            if token != "admin-session":
                return None
            return {
                "tenant_id": "tenant-alpha",
                "user_id": "admin-user",
                "email": "admin@example.invalid",
                "role": "tenant_admin",
                "_csrf_hash": "synthetic",
            }

        def session_csrf_valid(self, session, supplied):
            return bool(session and supplied == "admin-csrf")

        def list_principals(self, *, limit=100):
            return [{"user_id": "member-user", "disabled": False}][:limit]

        def set_principal_disabled(self, *, principal_id, disabled):
            mutation_order.append("human_auth")
            self.updated.append((principal_id, disabled))
            return {"user_id": principal_id, "disabled": disabled}

    auth = FakeHumanAuth()
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: auth)
    runtime = FakeRuntimeClient()
    original_authority_update = runtime.set_schedule_principal_authority

    def record_authority_update(principal_id, *, enabled):
        mutation_order.append("schedule_authority")
        return original_authority_update(principal_id, enabled=enabled)

    runtime.set_schedule_principal_authority = record_authority_update
    client = TestClient(create_app(runtime_client=runtime))
    client.cookies.set("glasshive_session", "admin-session")
    client.cookies.set("glasshive_csrf", "admin-csrf")

    listed = client.get("/api/admin/users")
    disabled = client.patch(
        "/api/admin/users/member-user",
        headers={"Origin": "http://testserver", "X-GlassHive-CSRF": "admin-csrf"},
        json={"disabled": True},
    )
    self_disable = client.patch(
        "/api/admin/users/admin-user",
        headers={"Origin": "http://testserver", "X-GlassHive-CSRF": "admin-csrf"},
        json={"disabled": True},
    )

    assert listed.status_code == 200
    assert listed.json()["items"] == [{"user_id": "member-user", "disabled": False}]
    assert disabled.status_code == 200
    assert auth.updated == [("member-user", True)]
    assert runtime.schedule_authority_requests == [
        {"principal_id": "member-user", "enabled": False}
    ]
    assert mutation_order == ["schedule_authority", "human_auth"]
    assert self_disable.status_code == 409


def test_tenant_admin_disable_fails_closed_when_schedule_authority_is_unavailable(monkeypatch):
    class FakeHumanAuth:
        mode = "oidc"
        session_enabled = True
        allowed_email_domains = ()

        def __init__(self):
            self.updated = []

        def resolve_session(self, token):
            if token != "admin-session":
                return None
            return {
                "tenant_id": "tenant-alpha",
                "user_id": "admin-user",
                "email": "admin@example.invalid",
                "role": "tenant_admin",
                "_csrf_hash": "synthetic",
            }

        def session_csrf_valid(self, session, supplied):
            return bool(session and supplied == "admin-csrf")

        def set_principal_disabled(self, *, principal_id, disabled):
            self.updated.append((principal_id, disabled))
            return {"user_id": principal_id, "disabled": disabled}

    auth = FakeHumanAuth()
    runtime = FakeRuntimeClient()
    runtime.schedule_authority_error = RuntimeError("synthetic authority outage")
    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: auth)
    client = TestClient(create_app(runtime_client=runtime))
    client.cookies.set("glasshive_session", "admin-session")
    client.cookies.set("glasshive_csrf", "admin-csrf")

    response = client.patch(
        "/api/admin/users/member-user",
        headers={"Origin": "http://testserver", "X-GlassHive-CSRF": "admin-csrf"},
        json={"disabled": True},
    )

    assert response.status_code == 503
    assert "account was not changed" in response.json()["detail"]
    assert runtime.schedule_authority_requests == [
        {"principal_id": "member-user", "enabled": False}
    ]
    assert auth.updated == []


def test_active_novnc_websocket_is_revoked_when_workspace_closes(monkeypatch):
    upstreams = []

    class FakeUpstream:
        def __init__(self):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.closed = True
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(30)
            raise StopAsyncIteration

        async def send(self, message):
            _ = message

        async def close(self):
            self.closed = True

    def fake_connect(*args, **kwargs):
        _ = args, kwargs
        upstream = FakeUpstream()
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(server_module.websockets, "connect", fake_connect)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    with client.websocket_connect("/novnc/wrk_1/websockify") as websocket:
        runtime.worker_state = "termination_failed"
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()

    assert exc.value.code == 1008
    assert upstreams and upstreams[0].closed is True


def test_open_completed_workspace_never_resumes_compute_implicitly():
    app_js = (Path(server_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    start = app_js.index("async function openWorkspaceSurface")
    end = app_js.index("async function runWorkerAction", start)
    open_workspace = app_js[start:end]

    assert "workspace?.workspace_url || workspace?.watch_url" in open_workspace
    assert "shouldResumeOnWorkspaceOpen" in open_workspace
    assert "renderedState: button?.closest('.workspace-tile')?.dataset.displayState" in open_workspace
    assert "fallbackState: workspaceStateLabel(workspace)" in open_workspace
    assert "rawWorkspaceState(workspace)" not in open_workspace
    assert "Boolean(workspace?.compute_released_at)" not in open_workspace
    assert "'/action/resume'" in open_workspace


def test_watch_footer_composer_keeps_instruction_wide_and_send_compact():
    styles = (Path(server_module.STATIC_DIR) / "styles.css").read_text(encoding="utf-8")

    def rule_body(selector: str) -> str:
        start = styles.index(f"{selector} {{")
        return styles[start : styles.index("}", start)]

    textarea_rule = rule_body(".steer-form textarea")
    send_rule = rule_body(".steer-form #send-button")

    assert "grid-column: 2;" in textarea_rule
    assert "min-width: 0;" in textarea_rule
    assert "grid-column: 3;" in send_rule
    assert "width: auto;" in send_rule
    assert "min-width: 96px;" in send_rule
    assert ".steer-form #send-button { grid-column: 1 / -1; width: 100%; }" not in styles


def test_main_frame_is_visible_without_waiting_for_a_compositor_animation():
    styles = (Path(server_module.STATIC_DIR) / "styles.css").read_text(encoding="utf-8")
    start = styles.index(".composer-frame {")
    composer_rule = styles[start : styles.index("}", start)]

    assert "animation:" not in composer_rule
    assert "backdrop-filter:" not in composer_rule
    assert "@keyframes float-up" not in styles


def test_workspace_open_resume_policy_uses_the_user_visible_state():
    policy_module = (Path(server_module.STATIC_DIR) / "launch-policy.js").as_uri()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            (
                f"import {{ shouldResumeOnWorkspaceOpen }} from {json.dumps(policy_module)};"
                "const states = ['completed','failed','cancelled','running','paused','idle','idle_terminated','stopped'];"
                "const outcomes = Object.fromEntries(states.map(renderedState => [renderedState, "
                "shouldResumeOnWorkspaceOpen({workspaceKind:'named', renderedState, fallbackState:'paused'})]));"
                "outcomes.fallbackPaused = shouldResumeOnWorkspaceOpen({workspaceKind:'named', renderedState:'', fallbackState:'paused'});"
                "outcomes.oneOffPaused = shouldResumeOnWorkspaceOpen({workspaceKind:'run', renderedState:'paused', fallbackState:'paused'});"
                "process.stdout.write(JSON.stringify(outcomes));"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "completed": False,
        "failed": False,
        "cancelled": False,
        "running": False,
        "paused": True,
        "idle": True,
        "idle_terminated": True,
        "stopped": True,
        "fallbackPaused": True,
        "oneOffPaused": False,
    }


def test_workspace_native_tool_setup_uses_the_selected_profile_without_connector_wiring():
    policy_module = (Path(server_module.STATIC_DIR) / "launch-policy.js").as_uri()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            (
                f"import {{ workspaceSetupAction }} from {json.dumps(policy_module)};"
                "process.stdout.write(JSON.stringify({"
                "codex:workspaceSetupAction('codex-cli'),"
                "claude:workspaceSetupAction('claude-code'),"
                "openclaw:workspaceSetupAction('openclaw-general'),"
                "unknown:workspaceSetupAction('custom-worker')"
                "}));"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "codex": "codex",
        "claude": "claude",
        "openclaw": "openclaw",
        "unknown": "terminal",
    }

    app_js = (Path(server_module.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    assert "Set up tools" in app_js
    assert "workspaceSetupAction(workspace?.profile)" in app_js
    assert "workerApiUrl(workerId, `/action/${encodeURIComponent(action)}`)" in app_js
    assert "workspace?.workspace_url || workspace?.watch_url" in app_js


def test_failed_workspace_keeps_the_recommended_pause_recovery_visible():
    policy_module = (Path(server_module.STATIC_DIR) / "launch-policy.js").as_uri()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            (
                f"import {{ workspaceLifecycleControl }} from {json.dumps(policy_module)};"
                "const states = ['failed','cancelled','interrupted','completed','paused','terminated'];"
                "process.stdout.write(JSON.stringify(Object.fromEntries(states.map(state => "
                "[state, workspaceLifecycleControl(state)]))));"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    controls = json.loads(result.stdout)
    for state in ("failed", "cancelled", "interrupted"):
        assert controls[state] == {
            "action": "pause",
            "label": "Pause",
            "hidden": False,
            "disabled": False,
        }
    assert controls["completed"] == {
        "action": "resume",
        "label": "Continue",
        "hidden": False,
        "disabled": False,
    }
    assert controls["paused"] == {
        "action": "resume",
        "label": "Resume",
        "hidden": False,
        "disabled": False,
    }
    assert controls["terminated"] == {
        "action": "pause",
        "label": "Pause",
        "hidden": True,
        "disabled": True,
    }


def test_watch_authenticated_actions_forward_current_csrf_cookie():
    watch_js = (Path(server_module.STATIC_DIR) / "watch.js").read_text(encoding="utf-8")

    assert "glasshive_csrf" in watch_js
    assert "function csrfHeaders(headers = {})" in watch_js
    assert "'X-GlassHive-CSRF': token" in watch_js
    assert watch_js.count("headers: csrfHeaders(") == 3  # native approval, workspace action, and steer


def test_enterprise_short_worker_view_ref_accepts_the_authenticated_browser_session(
    tmp_path,
    monkeypatch,
):
    _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "300")

    class SessionAuth:
        mode = "oidc"
        session_enabled = True

        def resolve_session(self, token):
            sessions = {
                "owner-session": {
                    "tenant_id": "tenant-alpha",
                    "user_id": "user-a",
                    "email": "owner@example.invalid",
                    "role": "member",
                },
                "other-session": {
                    "tenant_id": "tenant-alpha",
                    "user_id": "user-b",
                    "email": "other@example.invalid",
                    "role": "member",
                },
            }
            return sessions.get(token)

    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: SessionAuth())
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}",
    )

    client.cookies.set("glasshive_session", "owner-session")
    accepted = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert accepted.status_code == 307
    assert accepted.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in accepted.headers["location"]
    assert runtime.worker_view_open_requests == ["wrk_1"]

    client.cookies.set("glasshive_session", "other-session")
    denied = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert denied.status_code == 404
    assert runtime.worker_view_open_requests == ["wrk_1"]


def test_enterprise_short_worker_view_ref_recovers_an_expired_browser_session_via_login(
    tmp_path,
    monkeypatch,
):
    _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}",
    )

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?return_to=%2Fr%2F{ref_id}"


def test_runtime_artifact_ref_recovers_login_then_preserves_owner_scope(
    tmp_path,
    monkeypatch,
):
    _configure_oidc_session_for_multi_user_connect_test(tmp_path, monkeypatch)
    shared_dir = tmp_path / "shared-link-refs"
    shared_dir.mkdir(mode=0o770)
    os.chown(shared_dir, -1, os.getegid())
    shared_dir.chmod(0o2770 if sys.platform.startswith("linux") else 0o770)
    shared_path = shared_dir / "link_refs.sqlite3"
    shared_path.touch(mode=0o660)
    os.chown(shared_path, -1, os.getegid())
    shared_path.chmod(0o660)
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(shared_path))
    monkeypatch.setenv(
        "GLASSHIVE_LINK_REF_SHARED_GROUP",
        grp.getgrgid(os.getegid()).gr_name,
    )

    class SessionAuth:
        mode = "oidc"
        session_enabled = True

        def resolve_session(self, token):
            identities = {
                "owner-session": {
                    "tenant_id": "tenant-alpha",
                    "user_id": "user-a",
                    "email": "owner@example.invalid",
                    "role": "member",
                },
                "other-session": {
                    "tenant_id": "tenant-alpha",
                    "user_id": "user-b",
                    "email": "other@example.invalid",
                    "role": "member",
                },
            }
            return identities.get(token)

    monkeypatch.setattr(server_module.HumanAuthGateway, "from_env", lambda: SessionAuth())
    runtime_signed_links = load_runtime_signed_links_module()
    artifact_token = runtime_signed_links.sign_link_token(
        kind="artifact_open",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="workspace/index.html",
    )
    ref_id = runtime_signed_links.create_signed_link_ref(token=artifact_token)

    class FakeUpstreamResponse:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content
            self.headers = {"content-type": "text/html; charset=utf-8"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            assertion = str((headers or {}).get("X-GlassHive-User-Assertion") or "")
            claims = jwt.decode(
                assertion,
                options={"verify_signature": False},
            )
            if claims.get("sub") == "user-a":
                return FakeUpstreamResponse(200, b"<h1>Hello World</h1>")
            return FakeUpstreamResponse(404, b'{"detail":"GlassHive link not found"}')

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    signed_out = client.get(f"/v1/link-refs/{ref_id}", follow_redirects=False)
    assert signed_out.status_code == 303
    assert signed_out.headers["location"] == f"/login?return_to=%2Fv1%2Flink-refs%2F{ref_id}"

    worker_cookie_name = f"glasshive_gh_token_{sha256(b'wrk_1').hexdigest()[:24]}"
    worker_cookie_token = signed_links_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    client.cookies.set(worker_cookie_name, worker_cookie_token)
    cookie_opened = client.get(f"/v1/link-refs/{ref_id}")
    assert cookie_opened.status_code == 200
    assert cookie_opened.content == b"<h1>Hello World</h1>"
    client.cookies.delete(worker_cookie_name)

    client.cookies.set(worker_cookie_name, "stale-or-corrupt-worker-cookie")
    stale_cookie = client.get(f"/v1/link-refs/{ref_id}", follow_redirects=False)
    assert stale_cookie.status_code == 303
    assert stale_cookie.headers["location"] == (
        f"/login?return_to=%2Fv1%2Flink-refs%2F{ref_id}"
    )
    client.cookies.delete(worker_cookie_name)

    client.cookies.set("glasshive_session", "owner-session")
    opened = client.get(f"/v1/link-refs/{ref_id}")
    assert opened.status_code == 200
    assert opened.content == b"<h1>Hello World</h1>"

    client.cookies.set("glasshive_session", "other-session")
    denied = client.get(f"/v1/link-refs/{ref_id}")
    assert denied.status_code == 404
    assert b"Hello World" not in denied.content


def test_public_links_only_nonexpiring_artifact_ref_outlives_embedded_token(monkeypatch):
    secret = "public-link-secret"
    now = {"value": 1_000}
    monkeypatch.setattr(signed_links_module.time, "time", lambda: now["value"])
    monkeypatch.setenv("GLASSHIVE_PUBLIC_LINKS_ONLY", "true")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_TTL_S", "60")
    token = server_module.sign_link_token(
        kind="artifact_open",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="workspace/report.txt",
    )
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/v1/signed-links/{token}",
    )
    assert signed_links_module.verify_signed_link_token(token) is not None

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html><body>artifact preview</body></html>"
        headers = {"content-type": "text/html; charset=utf-8"}

    class FakeAsyncClient(StreamingFakeClient):
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    now["value"] = 1_061
    assert signed_links_module.verify_signed_link_token(token) is None

    response = client.get(f"/v1/link-refs/{ref_id}")

    assert response.status_code == 200
    bypass = client.get(f"/v1/link-refs/ghr_unknown_123456?gh_token={token}")
    assert bypass.status_code == 401


def test_public_links_only_artifact_ref_still_enforces_ref_ttl_and_revocation(monkeypatch):
    secret = "public-link-secret"
    now = {"value": 1_000}
    monkeypatch.setattr(signed_links_module.time, "time", lambda: now["value"])
    monkeypatch.setenv("GLASSHIVE_PUBLIC_LINKS_ONLY", "true")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    monkeypatch.setenv("GLASSHIVE_LINK_REF_TTL_SECONDS", "60")
    token = signed_artifact_token(secret, kind="artifact_open")
    expiring_ref = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/v1/signed-links/{token}",
    )
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    now["value"] = 1_061

    assert client.get(f"/v1/link-refs/{expiring_ref}").status_code == 401

    monkeypatch.setenv("GLASSHIVE_LINK_REF_TTL_SECONDS", "0")
    now["value"] = 2_000
    token = signed_artifact_token(secret, kind="artifact_open")
    revoked_ref = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/v1/signed-links/{token}",
    )
    assert revoke_signed_link_refs_for_worker("wrk_1") == 1
    assert client.get(f"/v1/link-refs/{revoked_ref}").status_code == 401


def test_public_links_only_artifact_ref_ignores_unsigned_cache_and_rejects_hmac_tampering(monkeypatch):
    secret = "public-link-secret"
    monkeypatch.setenv("GLASSHIVE_PUBLIC_LINKS_ONLY", "true")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    token = signed_artifact_token(secret, kind="artifact_open")
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/v1/signed-links/{token}",
    )
    state_path = os.environ["GLASSHIVE_LINK_REF_STATE_PATH"]
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    with sqlite3.connect(state_path) as conn:
        conn.execute(
            "UPDATE signed_link_refs SET payload_json = ? WHERE ref_id = ?",
            (json.dumps({"kind": "artifact_open", "worker_id": "wrk_other"}), ref_id),
        )

    resolved = resolve_signed_link_ref(ref_id)
    assert resolved is not None
    assert resolved["payload"]["worker_id"] == "wrk_1"

    with sqlite3.connect(state_path) as conn:
        conn.execute(
            "UPDATE signed_link_refs SET token = ? WHERE ref_id = ?",
            (f"{token}tampered", ref_id),
        )

    assert client.get(f"/v1/link-refs/{ref_id}").status_code == 401


def test_watch_zip_native_form_keeps_session_csrf_and_large_selection(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from urllib.parse import urlencode

    class HumanAuth:
        mode = 'oidc'
        session_enabled = True

        def resolve_session(self, token):
            return {'tenant_id': 'tenant-alpha', 'user_id': 'user-a', 'role': 'viewer'} if token == 'session-a' else None

        def session_csrf_valid(self, session, token):
            return bool(session and token == 'csrf-a')

    monkeypatch.setattr(server_module.HumanAuthGateway, 'from_env', lambda: HumanAuth())
    set_enterprise_ui_env(monkeypatch)
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    runtime = FakeRuntimeClient()

    @asynccontextmanager
    async def download(path, headers, **kwargs):
        runtime.file_upload_requests.append((path, kwargs))
        yield httpx.Response(200, content=b'zip bytes', request=httpx.Request('POST', 'http://runtime.test'))

    runtime.stream_file_download = download
    client = TestClient(create_app(runtime_client=runtime))
    client.cookies.set('glasshive_session', 'session-a')
    client.cookies.set('glasshive_csrf', 'csrf-a')
    # A prior worker view cookie must not turn this authenticated read into a mutation.
    client.cookies.set('glasshive_gh_token_'+sha256(b'wrk_old').hexdigest()[:24], signed_worker_token('ui-signed-link-secret', worker_id='wrk_old'))
    ids = ['fen_' + f'{i:032x}' for i in range(2000)]
    fields = urlencode({'file_ids': ids}, doseq=True)
    headers = {'Origin': 'http://testserver', 'Content-Type': 'application/x-www-form-urlencoded'}
    path = '/api/workspace/wrk_1/files/export'
    for body, origin in [(fields, headers['Origin']), (fields+'&workspace_export_csrf=wrong', headers['Origin']),
                         (fields+'&workspace_export_csrf=csrf-a', 'https://attacker.example.invalid')]:
        assert client.post(path, content=body, headers={**headers, 'Origin': origin}).status_code == 403
    assert runtime.file_upload_requests == []
    result = client.post(path, content=fields+'&workspace_export_csrf=csrf-a', headers=headers)
    assert result.status_code == 200 and result.content == b'zip bytes'
    assert runtime.file_upload_requests == [('/v1/workers/wrk_1/files/export', {'method': 'POST', 'payload': {'file_ids': ids}})]
    claims = jwt.decode(runtime.header_contexts[-1]['X-GlassHive-User-Assertion'], options={'verify_signature': False})
    assert 'workspaces:read' in claims['scope'].split()
    assert 'workspaces:write' not in claims['scope'].split()


@pytest.mark.parametrize('enabled, enterprise', [(True, True), (False, True), (False, False)])
def test_watch_zip_view_cookie_post_requires_origin_worker_and_read_route(tmp_path, monkeypatch, enabled, enterprise):
    from contextlib import asynccontextmanager

    class HumanAuth:
        mode = 'oidc'
        session_enabled = enabled
        def resolve_session(self, token):
            return None

    monkeypatch.setattr(server_module.HumanAuthGateway, 'from_env', lambda: HumanAuth())
    set_enterprise_ui_env(monkeypatch)
    configure_internal_assertion_signer(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true" if enterprise else "false")
    if not enterprise:
        monkeypatch.setenv("GLASSHIVE_PUBLIC_LINKS_ONLY", "true")
    runtime = FakeRuntimeClient()

    @asynccontextmanager
    async def download(path, headers, **kwargs):
        runtime.file_upload_requests.append((path, kwargs))
        yield httpx.Response(200, content=b'zip bytes', request=httpx.Request('POST', 'http://runtime.test'))

    runtime.stream_file_download = download
    client = TestClient(create_app(runtime_client=runtime))
    token = signed_worker_token('ui-signed-link-secret')
    path = '/api/workspace/wrk_1/files/export'
    headers = {'Origin': 'http://testserver', 'Content-Type': 'application/x-www-form-urlencoded'}
    for suffix, extra in [('?gh_token='+token, {}), ('', {'Referer':'http://testserver/watch/wrk_1?gh_token='+token})]:
        assert client.post(path+suffix, content='file_ids=fen_1', headers={**headers, **extra}).status_code in (401,403)
    client.cookies.set('glasshive_gh_token_'+sha256(b'wrk_1').hexdigest()[:24], token)
    for origin in ['', 'https://attacker.example.invalid']:
        assert client.post(path, content='file_ids=fen_1', headers={**headers,'Origin':origin}).status_code == 403
    assert client.post('/api/workspace/wrk_other/files/export', content='file_ids=fen_1', headers=headers).status_code in (401,403)
    for route in ['/api/workspace/wrk_1/files', '/api/workspace/wrk_1/files/fen_1/restore', '/api/file-uploads/export']:
        assert client.post(route, json={'file_ids':['fen_1']}, headers={'Origin':'http://testserver'}).status_code == 403
    assert runtime.file_upload_requests == []
    for kwargs in [{'content':'file_ids=fen_1','headers':headers},
                   {'json':{'file_ids':['fen_1']},'headers':{'Origin':'http://testserver'}}]:
        result = client.post(path, **kwargs)
        assert result.status_code == 200, result.text
    if enterprise:
        claims = jwt.decode(runtime.header_contexts[-1]['X-GlassHive-User-Assertion'], options={'verify_signature': False})
        assert claims['role'] == 'viewer'
        assert set(claims['scope'].split()) == {'runtime:access','workspaces:read'}
    else:
        assert runtime.header_contexts[-1]['X-Viventium-User-Role'] == 'viewer'
