from __future__ import annotations

import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from workers_projects_runtime.api import create_app
from workers_projects_runtime import provider_accounts as provider_accounts_module
from workers_projects_runtime.auth import (
    GlassHiveAuthError,
    InternalAssertionVerifier,
    multi_user_security_enabled,
)
from library_test_support import library_manifest, register_manifest


ISSUER = "https://gateway.example.invalid"
AUDIENCE = "glasshive-runtime"
TENANT = "tenant-public-safe"
KID = "gateway-test-key"


@pytest.fixture()
def assertion_keys() -> tuple[object, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return private_key, {"keys": [public_jwk]}


def configure_signed_assertions(monkeypatch, jwks: dict[str, object]) -> None:
    monkeypatch.setenv("WPR_API_TOKEN", "runtime-service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "workspace-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "signed_internal_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", TENANT)
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_JWKS_JSON", json.dumps(jwks))


def signed_assertion(
    private_key: object,
    *,
    subject: str = "user-public-safe",
    tenant: str = TENANT,
    audience: str = AUDIENCE,
    expires_at: int | None = None,
    role: str = "member",
    scope: str = "runtime:access workspaces:read workspaces:write",
    key_id: str = KID,
    jti: str | None = None,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": audience,
            "sub": subject,
            "tenant_id": tenant,
            "email": "member@example.invalid",
            "role": role,
            "scope": scope,
            "iat": now,
            "nbf": now - 1,
            "exp": expires_at if expires_at is not None else now + 60,
            "jti": jti or f"assertion-{subject}-{uuid.uuid4().hex}",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": key_id, "typ": "JWT"},
    )


def request_headers(token: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer runtime-service-token"}
    if token:
        headers["X-GlassHive-User-Assertion"] = token
    return headers


def fresh_assertion_headers(private_key: object, **token_kwargs) -> dict[str, str]:
    """Mirror the gateway contract: mint one internal assertion per request."""

    return request_headers(signed_assertion(private_key, **token_kwargs))


def test_explicit_security_mode_is_authoritative_over_legacy_enterprise_flags(monkeypatch):
    monkeypatch.setenv("WPR_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    assert multi_user_security_enabled() is False

    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "legacy_compatibility")
    assert multi_user_security_enabled() is False

    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    assert multi_user_security_enabled() is True

    monkeypatch.delenv("GLASSHIVE_SECURITY_MODE")
    assert multi_user_security_enabled() is True


def test_signed_internal_assertion_scopes_runtime_request_to_principal(tmp_path, monkeypatch, assertion_keys):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    client = TestClient(app)

    response = client.get("/v1/preferences", headers=request_headers(signed_assertion(private_key)))

    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == TENANT
    assert response.json()["owner_id"] == "user-public-safe"


def test_signed_internal_assertion_jti_is_single_use_across_runtime_instances(
    tmp_path,
    monkeypatch,
    assertion_keys,
):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    db_path = str(tmp_path / "runtime.db")
    token = signed_assertion(private_key, jti="single-use-assertion-id")
    headers = request_headers(token)
    first_client = TestClient(create_app(db_path=db_path, runtime_backend="stub"))

    first = first_client.get("/v1/preferences", headers=headers)
    replay = first_client.get("/v1/preferences", headers=headers)
    second_client = TestClient(create_app(db_path=db_path, runtime_backend="stub"))
    cross_instance_replay = second_client.get("/v1/preferences", headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Signed internal assertion was already used"
    assert cross_instance_replay.status_code == 401
    assert cross_instance_replay.json()["detail"] == "Signed internal assertion was already used"


def test_missing_resource_error_detail_is_not_quote_wrapped(tmp_path, monkeypatch, assertion_keys):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))

    response = client.get(
        "/v1/projects/missing-project",
        headers=request_headers(signed_assertion(private_key)),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Project not found"


def test_internal_assertion_verifier_accepts_bounded_rotation_overlap_then_drops_old_key():
    old_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def public_jwk(private_key, key_id):
        value = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
        value.update({"kid": key_id, "use": "sig", "alg": "RS256"})
        return value

    overlapping = InternalAssertionVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id=TENANT,
        jwks={
            "keys": [
                public_jwk(new_private, "gateway-new-key"),
                public_jwk(old_private, "gateway-old-key"),
            ]
        },
        jwks_url="",
    )
    old_token = signed_assertion(old_private, key_id="gateway-old-key")
    new_token = signed_assertion(new_private, key_id="gateway-new-key")

    assert overlapping.verify(old_token).user_id == "user-public-safe"
    assert overlapping.verify(new_token).user_id == "user-public-safe"

    new_only = InternalAssertionVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id=TENANT,
        jwks={"keys": [public_jwk(new_private, "gateway-new-key")]},
        jwks_url="",
    )
    assert new_only.verify(new_token).user_id == "user-public-safe"
    with pytest.raises(GlassHiveAuthError, match="key id is not trusted"):
        new_only.verify(old_token)


def test_personal_provider_setup_route_keeps_accounts_user_scoped(tmp_path, monkeypatch, assertion_keys):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    cli = tmp_path / "synthetic-codex"
    cli.write_text(
        """#!/usr/bin/env python3
import sys
if sys.argv[1:] == ['login', '--device-auth']:
    print('Open https://provider.example.invalid/device and enter SAFE-CODE', flush=True)
    raise SystemExit(0)
if sys.argv[1:] == ['login', 'status']:
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setenv("WPR_CODEX_CLI_PATH", str(cli))
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))
    account_response = client.post(
        "/v1/provider-accounts",
        headers=fresh_assertion_headers(private_key),
        json={
            "provider": "codex",
            "label": "Personal Codex",
            "auth_method": "subscription",
            "platform_support": "supported",
            "secret_locator": "native-home://auto",
            "make_default": True,
        },
    )
    assert account_response.status_code == 201, account_response.text
    account_id = account_response.json()["account_id"]

    started = client.post(
        f"/v1/provider-accounts/{account_id}/setup",
        headers=fresh_assertion_headers(private_key),
    )
    forbidden = client.get(
        f"/v1/provider-accounts/{account_id}/setup",
        headers=fresh_assertion_headers(private_key, subject="other-user"),
    )

    assert started.status_code == 200, started.text
    assert started.json()["status"] == "connecting"
    assert forbidden.status_code == 400
    assert "not found" in forbidden.json()["detail"].lower()
    settled = started
    deadline = time.time() + 3
    while not settled.json()["complete"] and time.time() < deadline:
        time.sleep(0.03)
        settled = client.get(
            f"/v1/provider-accounts/{account_id}/setup",
            headers=fresh_assertion_headers(private_key),
        )
    assert settled.json()["status"] == "ready"


def test_claude_setup_input_route_is_owner_scoped_and_never_returns_the_code(
    tmp_path, monkeypatch, assertion_keys
):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    cli = tmp_path / "synthetic-claude"
    cli.write_text(
        """#!/usr/bin/env python3
import os
import json
import sys
marker = os.path.join(os.environ['CLAUDE_CONFIG_DIR'], 'authenticated')
if sys.argv[1:] == ['auth', 'login', '--claudeai']:
    print('Open https://claude.com/cai/oauth/authorize?code=true&client_id=synthetic', flush=True)
    if sys.stdin.readline().strip() != 'synthetic-browser-code':
        raise SystemExit(3)
    open(marker, 'w', encoding='utf-8').write('ready')
    with open(os.path.join(os.environ['CLAUDE_CONFIG_DIR'], '.claude.json'), 'w', encoding='utf-8') as handle:
        json.dump({'theme': 'dark'}, handle)
    raise SystemExit(0)
if sys.argv[1:] == ['auth', 'status', '--json']:
    raise SystemExit(0 if os.path.exists(marker) else 1)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setattr(provider_accounts_module.sys, "platform", "linux")
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(cli))
    monkeypatch.setenv("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH", "true")
    with TestClient(
        create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    ) as client:
        account_response = client.post(
            "/v1/provider-accounts",
            headers=fresh_assertion_headers(private_key),
            json={
                "provider": "claude",
                "label": "Personal Claude",
                "auth_method": "subscription",
                "platform_support": "supported",
                "secret_locator": "native-home://auto",
            },
        )
        assert account_response.status_code == 201, account_response.text
        account_id = account_response.json()["account_id"]
        setup = client.post(
            f"/v1/provider-accounts/{account_id}/setup",
            headers=fresh_assertion_headers(private_key),
        )
        deadline = time.time() + 5
        while not setup.json().get("input_required") and time.time() < deadline:
            time.sleep(0.03)
            setup = client.get(
                f"/v1/provider-accounts/{account_id}/setup",
                headers=fresh_assertion_headers(private_key),
            )

        forbidden = client.post(
            f"/v1/provider-accounts/{account_id}/setup/input",
            headers=fresh_assertion_headers(private_key, subject="other-user"),
            json={"value": "synthetic-browser-code"},
        )
        rejected_control_input = client.post(
            f"/v1/provider-accounts/{account_id}/setup/input",
            headers=fresh_assertion_headers(private_key),
            json={"value": "synthetic-first-line\nsynthetic-second-line"},
        )
        submitted = client.post(
            f"/v1/provider-accounts/{account_id}/setup/input",
            headers=fresh_assertion_headers(private_key),
            json={"value": "synthetic-browser-code"},
        )

        assert forbidden.status_code == 400
        assert "synthetic-browser-code" not in forbidden.text
        assert rejected_control_input.status_code == 400
        assert "synthetic-first-line" not in rejected_control_input.text
        assert submitted.status_code == 200, submitted.text
        assert "synthetic-browser-code" not in submitted.text
        settled = submitted
        deadline = time.time() + 5
        while not settled.json()["complete"] and time.time() < deadline:
            time.sleep(0.03)
            settled = client.get(
                f"/v1/provider-accounts/{account_id}/setup",
                headers=fresh_assertion_headers(private_key),
            )
        assert settled.json()["status"] == "ready"
        assert "synthetic-browser-code" not in settled.text


def test_provider_account_api_ignores_client_claims_about_platform_support(
    tmp_path,
    monkeypatch,
    assertion_keys,
):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))
    headers = request_headers(signed_assertion(private_key))

    created = client.post(
        "/v1/provider-accounts",
        headers=headers,
        json={
            "provider": "codex",
            "label": "Personal Codex",
            "auth_method": "subscription",
            "platform_support": "supported",
            "secret_locator": "native-home://auto",
        },
    )

    assert created.status_code == 409
    assert "proof_required" in created.json()["detail"]


def test_signed_internal_assertion_mode_rejects_plain_identity_headers(tmp_path, monkeypatch, assertion_keys):
    _, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))

    response = client.get(
        "/v1/preferences",
        headers={
            **request_headers(),
            "X-Viventium-Tenant-Id": TENANT,
            "X-Viventium-User-Id": "forged-user",
            "X-Viventium-User-Role": "tenant_admin",
        },
    )

    assert response.status_code == 401
    assert "signed internal assertion" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    ("token_kwargs", "expected_detail"),
    [
        ({"audience": "wrong-audience"}, "audience"),
        ({"tenant": "wrong-tenant"}, "tenant"),
        ({"expires_at": 1}, "expired"),
    ],
)
def test_signed_internal_assertion_rejects_invalid_security_claims(
    tmp_path,
    monkeypatch,
    assertion_keys,
    token_kwargs,
    expected_detail,
):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))

    response = client.get(
        "/v1/preferences",
        headers=request_headers(signed_assertion(private_key, **token_kwargs)),
    )

    assert response.status_code == 401
    assert expected_detail in response.json()["detail"].lower()


def test_signed_internal_assertion_mode_fails_closed_without_jwks(tmp_path, monkeypatch):
    configure_signed_assertions(monkeypatch, {"keys": []})

    with pytest.raises(RuntimeError, match="JWKS"):
        create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")


def test_signed_internal_assertion_runtime_refuses_readable_private_signing_key(
    tmp_path,
    monkeypatch,
    assertion_keys,
):
    _, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    private_key_path = tmp_path / "gateway-private-key.pem"
    private_key_path.write_text("runtime-must-never-read-this", encoding="utf-8")
    private_key_path.chmod(0o600)
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE", str(private_key_path))

    with pytest.raises(RuntimeError, match="must not be readable by the runtime"):
        create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")


@pytest.mark.parametrize("source", ["inline", "file"])
def test_internal_assertion_jwks_rejects_private_rsa_material(
    tmp_path,
    monkeypatch,
    source,
):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_jwk = json.loads(RSAAlgorithm.to_jwk(private_key))
    private_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    payload = json.dumps({"keys": [private_jwk]})
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE", AUDIENCE)
    if source == "inline":
        monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_JWKS_JSON", payload)
    else:
        jwks_path = tmp_path / "jwks.json"
        jwks_path.write_text(payload, encoding="utf-8")
        monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE", str(jwks_path))

    with pytest.raises(RuntimeError, match="public keys only"):
        InternalAssertionVerifier.from_env(tenant_id=TENANT)


def test_internal_assertion_remote_jwks_rejects_private_key_safely(assertion_keys):
    private_key, _ = assertion_keys
    verifier = InternalAssertionVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_id=TENANT,
        jwks=None,
        jwks_url="https://identity.example.invalid/jwks",
    )
    verifier._remote_client = type(
        "PrivateRemoteClient",
        (),
        {"get_signing_key_from_jwt": lambda self, token: type("SigningKey", (), {"key": private_key})()},
    )()

    with pytest.raises(GlassHiveAuthError, match="non-public RSA key"):
        verifier._signing_key("header.payload.signature")


def test_internal_assertion_jwks_url_rejects_cleartext_and_prefix_bypasses(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.delenv("GLASSHIVE_INTERNAL_ASSERTION_JWKS_JSON", raising=False)
    monkeypatch.delenv("GLASSHIVE_INTERNAL_ASSERTION_JWKS_FILE", raising=False)

    for invalid_url in (
        "http://localhost:8765/jwks",
        "http://localhost.evil.example/jwks",
        "http://127.0.0.1.evil.example/jwks",
        "https://user:password@identity.example.invalid/jwks",
    ):
        monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_JWKS_URL", invalid_url)
        with pytest.raises(RuntimeError, match="trusted HTTPS"):
            InternalAssertionVerifier.from_env(tenant_id=TENANT)


def test_signed_internal_assertion_enforces_viewer_and_scope_write_boundaries(tmp_path, monkeypatch, assertion_keys):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))
    viewer_claims = {
        "role": "viewer",
        "scope": "runtime:access workspaces:read workspaces:write",
    }

    assert client.get(
        "/v1/preferences",
        headers=fresh_assertion_headers(private_key, **viewer_claims),
    ).status_code == 200
    viewer_write = client.patch(
        "/v1/preferences",
        headers=fresh_assertion_headers(private_key, **viewer_claims),
        json={"codex_reasoning_effort": "high"},
    )
    narrow_write = client.patch(
        "/v1/preferences",
        headers=fresh_assertion_headers(
            private_key,
            subject="narrow-member",
            scope="runtime:access workspaces:read",
        ),
        json={"codex_reasoning_effort": "high"},
    )

    assert viewer_write.status_code == 403
    assert "viewer" in viewer_write.json()["detail"].lower()
    assert narrow_write.status_code == 403
    assert "scope" in narrow_write.json()["detail"].lower()


def test_viewer_communication_scope_is_exactly_limited_to_message_and_steer(
    tmp_path,
    monkeypatch,
    assertion_keys,
):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-deployment-provider-key")
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))
    project = client.post(
        "/v1/projects",
        headers=fresh_assertion_headers(private_key),
        json={
            "owner_id": "ignored-in-enterprise",
            "title": "Narrow communication boundary",
            "goal": "Prove restricted workspace communication.",
            "default_worker_profile": "openclaw-general",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=fresh_assertion_headers(private_key),
        json={
            "owner_id": "ignored-in-enterprise",
            "name": "Restricted Worker",
            "role": "operator",
            "profile": "openclaw-general",
            "backend": "openclaw",
        },
    ).json()
    viewer_claims = {
        "role": "viewer",
        "scope": "runtime:access workspaces:read workspaces:communicate",
    }
    worker_path = f"/v1/workers/{worker['worker_id']}"

    assert client.get(
        f"{worker_path}/live",
        headers=fresh_assertion_headers(private_key, **viewer_claims),
    ).status_code == 200
    assert client.post(
        f"{worker_path}/message",
        headers=fresh_assertion_headers(private_key, **viewer_claims),
        json={"message": "Share a concise status update."},
    ).status_code == 202
    assert client.post(
        f"{worker_path}/steer",
        headers=fresh_assertion_headers(private_key, **viewer_claims),
        json={"message": "Focus on the requested output."},
    ).status_code == 202

    forbidden = (
        client.patch(
            "/v1/preferences",
            headers=fresh_assertion_headers(private_key, **viewer_claims),
            json={"codex_reasoning_effort": "high"},
        ),
        client.post(
            f"{worker_path}/pause",
            headers=fresh_assertion_headers(private_key, **viewer_claims),
        ),
        client.post(
            f"{worker_path}/desktop-action",
            headers=fresh_assertion_headers(private_key, **viewer_claims),
            json={"action": "browser"},
        ),
        client.post(
            f"{worker_path}/assign",
            headers=fresh_assertion_headers(private_key, **viewer_claims),
            json={"instruction": "Start a separate broad mutation."},
        ),
    )
    assert [response.status_code for response in forbidden] == [403, 403, 403, 403]


def test_multi_user_security_mode_requires_signed_identity_without_legacy_enterprise_flag(
    tmp_path,
    monkeypatch,
    assertion_keys,
):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("GLASSHIVE_AUTH_MODE", raising=False)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    client = TestClient(create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub"))

    assert client.get("/ui").status_code == 401
    response = client.get("/v1/preferences", headers=request_headers(signed_assertion(private_key)))
    assert response.status_code == 200
    assert response.json()["owner_id"] == "user-public-safe"


def test_multi_user_security_mode_refuses_plain_legacy_assertions(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("WPR_API_TOKEN", "runtime-service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "workspace-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", TENANT)

    with pytest.raises(RuntimeError, match="signed_internal_assertion"):
        create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")


def test_control_plane_routes_are_user_scoped_and_confirmation_is_human_bound(
    tmp_path,
    monkeypatch,
    assertion_keys,
):
    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    monkeypatch.setenv("WPR_CODEX_BIN", "/usr/bin/true")
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    client = TestClient(app)
    created = client.post(
        "/v1/provider-accounts",
        headers=fresh_assertion_headers(private_key, subject="user-a"),
        json={
            "provider": "codex",
            "label": "Personal Codex",
            "auth_method": "subscription",
            "platform_support": "supported",
            "secret_locator": "native-home://account-a",
            "make_default": True,
        },
    )
    assert created.status_code == 201, created.text
    assert "secret_locator" not in created.json()
    assert len(client.get(
        "/v1/provider-accounts",
        headers=fresh_assertion_headers(private_key, subject="user-a"),
    ).json()["items"]) == 1
    assert client.get(
        "/v1/provider-accounts",
        headers=fresh_assertion_headers(private_key, subject="user-b"),
    ).json()["items"] == []

    project = client.post(
        "/v1/projects",
        headers=fresh_assertion_headers(private_key, subject="user-a"),
        json={
            "owner_id": "user-a",
            "title": "Personal workspace",
            "goal": "Use an approved capability",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=fresh_assertion_headers(private_key, subject="user-a"),
        json={
            "owner_id": "user-a",
            "name": "Main workspace",
            "role": "main",
            "profile": "codex-cli",
        },
    ).json()
    item = register_manifest(
        app.state.control_plane,
        library_manifest(
            stable_id="skill.synthetic.approved",
            scopes=["documents:read"],
            files=[
                {
                    "scope": "workspace",
                    "path": ".glasshive/skills/synthetic/SKILL.md",
                    "content": "# Synthetic approved capability",
                }
            ],
            label="Approved capability",
        ),
    )
    pending = client.post(
        "/v1/pending-changes",
        headers=fresh_assertion_headers(private_key, subject="user-a"),
        json={
            "change_type": "library_enable",
            "target_id": worker["worker_id"],
            "payload": {"library_id": item["library_id"]},
        },
    )
    assert pending.status_code == 201
    confirmation_token = pending.json()["confirmation_token"]
    denied = client.post(
        f"/v1/pending-changes/{pending.json()['change_id']}/confirm",
        headers=fresh_assertion_headers(private_key, subject="user-a"),
        json={"confirmation_token": confirmation_token},
    )
    human_token = signed_assertion(
        private_key,
        subject="user-a",
        scope="runtime:access workspaces:read workspaces:write human:confirm",
    )
    confirmed = client.post(
        f"/v1/pending-changes/{pending.json()['change_id']}/confirm",
        headers=request_headers(human_token),
        json={"confirmation_token": confirmation_token},
    )

    assert denied.status_code == 403
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["applied"]["worker_id"] == worker["worker_id"]
    refreshed = app.state.store.get_worker(worker["worker_id"])
    assert ".glasshive/skills/synthetic/SKILL.md" in str(refreshed["bootstrap_bundle_json"])


def test_workspace_zip_post_uses_read_assertion_without_mutation_authority(tmp_path, monkeypatch, assertion_keys):
    import io
    import zipfile

    private_key, jwks = assertion_keys
    configure_signed_assertions(monkeypatch, jwks)
    app = create_app(db_path=str(tmp_path / 'runtime.db'), runtime_backend='stub')
    store = app.state.store
    owner = 'user-public-safe'
    project = store.create_project(owner, 'Read-only ZIP', 'Read selected files', 'codex-cli', tenant_id=TENANT)
    worker = store.create_worker(project['project_id'], owner, 'ZIP worker', 'main', 'codex-cli', 'stub', 'stub', 'stub', tenant_id=TENANT)
    folder = tmp_path / 'workspace'
    folder.mkdir()
    (folder / 'input.txt').write_bytes(b'exact exported bytes')
    store.update_worker(worker['worker_id'], workspace_dir=str(folder), state='paused')
    client = TestClient(app)
    path = '/v1/workers/' + worker['worker_id'] + '/files'
    claims = {'role': 'viewer', 'scope': 'runtime:access workspaces:read'}
    listed = client.get(path, headers=fresh_assertion_headers(private_key, **claims))
    assert listed.status_code == 200, listed.text
    selected = listed.json()['items'][0]['file_id']
    exported = client.post(path+'/export', json={'file_ids':[selected]}, headers=fresh_assertion_headers(private_key, **claims))
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read('input.txt') == b'exact exported bytes'
    assert client.post(path+'/export', json={'file_ids':[selected]}, headers=fresh_assertion_headers(private_key, role='viewer', scope='runtime:access')).status_code == 403
    assert client.post(path+'/export', json={'file_ids':[selected]}, headers=fresh_assertion_headers(private_key, subject='other-owner', **claims)).status_code == 404
    for route in [path, path+'/'+selected+'/restore', path+'/folders']:
        assert client.post(route, json={}, headers=fresh_assertion_headers(private_key, **claims)).status_code == 403
