from __future__ import annotations

import json
import asyncio
import sqlite3
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from cryptography.hazmat.primitives import serialization

import workers_projects_runtime.mcp_oauth as oauth_module
import workers_projects_runtime.mcp_server as mcp_server_module
from workers_projects_runtime.mcp_oauth import (
    McpOAuthConfigurationError,
    OidcJwtTokenVerifier,
    oauth_from_env,
    principal_id,
)
from workers_projects_runtime.mcp_server import create_mcp_server
from workers_projects_runtime.mcp_server import WorkersProjectsApiClient


ISSUER = "https://identity.example.invalid"
RESOURCE = "https://glasshive.example.invalid/mcp"
TOKEN_AUDIENCE = "11111111-2222-3333-4444-555555555555"
TENANT = "tenant-public-safe"
AUTHORIZATION_SCOPE = f"api://{TOKEN_AUDIENCE}/user_impersonation"
TOKEN_SCOPE = "user_impersonation"
CANONICAL_AUTHORIZATION_SCOPE = f"{RESOURCE}/{TOKEN_SCOPE}"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


@pytest.fixture()
def signing_material():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "oauth-test-key", "alg": "RS256", "use": "sig"})
    return private_key, public_jwk


def token(private_key, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": TOKEN_AUDIENCE,
        "sub": "stable-subject",
        "tid": TENANT,
        "email": "member@example.invalid",
        "email_verified": True,
        "scp": f"{TOKEN_SCOPE} profile",
        "azp": "mcp-public-client",
        "iat": now,
        "nbf": now - 1,
        "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "oauth-test-key"})


def create_auth_state(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE auth_principals (
                user_id TEXT PRIMARY KEY,
                issuer TEXT NOT NULL,
                subject TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'member',
                disabled_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(issuer, subject)
            )
            """
        )


def test_oidc_mcp_token_verifier_binds_resource_scope_tenant_and_stable_user(monkeypatch, signing_material):
    private_key, public_jwk = signing_material

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
    )

    access = asyncio.run(verifier.verify_token(token(private_key)))
    wrong_audience = asyncio.run(verifier.verify_token(token(private_key, aud="other-api-client")))
    wrong_tenant = asyncio.run(verifier.verify_token(token(private_key, tid="other-tenant")))
    conflicting_tenant = asyncio.run(
        verifier.verify_token(token(private_key, tenant_id=TENANT, tid="other-tenant"))
    )
    wrong_scope = asyncio.run(verifier.verify_token(token(private_key, scp="profile")))

    assert access is not None
    assert access.subject == principal_id(ISSUER, "stable-subject")
    assert access.resource == RESOURCE
    assert access.claims["tenant_id"] == TENANT
    assert access.claims["email"] == "member@example.invalid"
    assert wrong_audience is None
    assert wrong_tenant is None
    assert conflicting_tenant is None
    assert wrong_scope is None

    viewer_verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
        role_claim="groups",
        role_map={"read-only-users": "viewer"},
    )
    viewer_access = asyncio.run(
        viewer_verifier.verify_token(
            token(private_key, groups=["read-only-users"])
        )
    )
    assert viewer_access is not None
    assert viewer_access.claims["role"] == "viewer"
    missing_role_access = asyncio.run(
        viewer_verifier.verify_token(token(private_key))
    )
    unmapped_role_access = asyncio.run(
        viewer_verifier.verify_token(token(private_key, groups=["unapproved-users"]))
    )
    assert missing_role_access is None
    assert unmapped_role_access is None


def test_mcp_token_tenant_validation_is_independent_from_glasshive_tenant(
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    generic_verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id="glasshive-tenant-public-safe",
    )

    generic_access = asyncio.run(
        generic_verifier.verify_token(token(private_key, tid=None))
    )

    assert generic_access is not None
    assert generic_access.claims["tenant_id"] == "glasshive-tenant-public-safe"
    assert generic_access.claims["upstream_tenant_id"] == ""

    entra_verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id="glasshive-tenant-public-safe",
        token_tenant_id=TENANT,
    )
    accepted_entra = asyncio.run(entra_verifier.verify_token(token(private_key)))
    rejected_entra = asyncio.run(
        entra_verifier.verify_token(token(private_key, tid="other-directory"))
    )

    assert accepted_entra is not None
    assert accepted_entra.claims["tenant_id"] == "glasshive-tenant-public-safe"
    assert accepted_entra.claims["upstream_tenant_id"] == TENANT
    assert rejected_entra is None


def test_mapped_mcp_role_is_required_before_principal_enrollment(
    tmp_path,
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
        role_claim="roles",
        role_map={"GlassHive.Member": "member"},
        allowed_client_ids=("mcp-public-client",),
        auth_state_path=str(state_path),
        require_auth_state=True,
    )

    assert asyncio.run(verifier.verify_token(token(private_key))) is None
    assert asyncio.run(
        verifier.verify_token(token(private_key, roles=["Unapproved.Role"]))
    ) is None
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_principals").fetchone()[0] == 0

    approved = asyncio.run(
        verifier.verify_token(token(private_key, roles=["GlassHive.Member"]))
    )
    assert approved is not None
    assert approved.claims["role"] == "member"
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_principals").fetchone()[0] == 1


def test_oidc_mcp_token_verifier_rejects_unapproved_or_ambiguous_client(monkeypatch, signing_material):
    private_key, public_jwk = signing_material

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
        allowed_client_ids=("codex-public-client", "claude-public-client"),
    )

    assert asyncio.run(
        verifier.verify_token(token(private_key, azp="codex-public-client"))
    ) is not None
    assert asyncio.run(
        verifier.verify_token(token(private_key, azp="other-public-client"))
    ) is None
    assert asyncio.run(
        verifier.verify_token(token(private_key, azp=None))
    ) is None
    assert asyncio.run(
        verifier.verify_token(
            token(private_key, azp="codex-public-client", client_id="claude-public-client")
        )
    ) is None
    assert asyncio.run(
        verifier.verify_token(token(private_key, azp=["codex-public-client"]))
    ) is None


def test_oidc_mcp_token_verifier_honors_local_principal_disable_state(
    tmp_path,
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material
    state_path = tmp_path / "auth.sqlite3"
    canonical_user = principal_id(ISSUER, "stable-subject")
    create_auth_state(state_path)
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            INSERT INTO auth_principals
                (user_id, issuer, subject, email, display_name, role, disabled_at, created_at, updated_at)
            VALUES (?, ?, ?, '', '', 'member', NULL, ?, ?)
            """,
            (canonical_user, ISSUER, "stable-subject", time.time(), time.time()),
        )

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
        allowed_client_ids=("mcp-public-client",),
        auth_state_path=str(state_path),
        require_auth_state=True,
    )

    same_token = token(private_key)
    assert asyncio.run(verifier.verify_token(same_token)) is not None

    with sqlite3.connect(state_path) as connection:
        connection.execute("DELETE FROM auth_principals WHERE user_id = ?", (canonical_user,))
    assert asyncio.run(verifier.verify_token(same_token)) is not None
    with sqlite3.connect(state_path) as connection:
        enrolled = connection.execute(
            "SELECT user_id, issuer, subject, disabled_at FROM auth_principals WHERE user_id = ?",
            (canonical_user,),
        ).fetchone()
    assert enrolled == (canonical_user, ISSUER, "stable-subject", None)

    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE auth_principals SET disabled_at = ? WHERE user_id = ?",
            (time.time(), canonical_user),
        )
    assert asyncio.run(verifier.verify_token(same_token)) is None
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE auth_principals SET disabled_at = NULL WHERE user_id = ?",
            (canonical_user,),
        )
    assert asyncio.run(verifier.verify_token(same_token)) is not None

    with sqlite3.connect(state_path) as connection:
        connection.execute("DROP TABLE auth_principals")
    assert asyncio.run(verifier.verify_token(same_token)) is None


def test_existing_durable_role_cannot_be_overwritten_by_mcp_token_role(
    tmp_path,
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)
    now = time.time()
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            INSERT INTO auth_principals
                (user_id, issuer, subject, email, display_name, role, disabled_at, created_at, updated_at)
            VALUES (?, ?, ?, '', '', 'viewer', NULL, ?, ?)
            """,
            (
                principal_id(ISSUER, "stable-subject"),
                ISSUER,
                "stable-subject",
                now,
                now,
            ),
        )

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        token_tenant_id=TENANT,
        role_claim="roles",
        role_map={"glasshive-admin": "tenant_admin"},
        allowed_client_ids=("mcp-public-client",),
        auth_state_path=str(state_path),
        require_auth_state=True,
    )

    access = asyncio.run(
        verifier.verify_token(token(private_key, roles=["glasshive-admin"]))
    )

    assert access is not None
    assert access.claims["role"] == "viewer"
    with sqlite3.connect(state_path) as connection:
        durable_role = connection.execute(
            "SELECT role FROM auth_principals WHERE user_id = ?",
            (principal_id(ISSUER, "stable-subject"),),
        ).fetchone()[0]
    assert durable_role == "viewer"


def test_mcp_effective_role_honors_upstream_demotion_without_mutating_durable_role(
    tmp_path,
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)
    now = time.time()
    canonical_user = principal_id(ISSUER, "stable-subject")
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            INSERT INTO auth_principals
                (user_id, issuer, subject, email, display_name, role, disabled_at, created_at, updated_at)
            VALUES (?, ?, ?, '', '', 'tenant_admin', NULL, ?, ?)
            """,
            (canonical_user, ISSUER, "stable-subject", now, now),
        )

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        token_tenant_id=TENANT,
        role_claim="roles",
        role_map={"glasshive-viewer": "viewer"},
        allowed_client_ids=("mcp-public-client",),
        auth_state_path=str(state_path),
        require_auth_state=True,
    )

    access = asyncio.run(
        verifier.verify_token(token(private_key, roles=["glasshive-viewer"]))
    )

    assert access is not None
    assert access.claims["role"] == "viewer"
    with sqlite3.connect(state_path) as connection:
        durable_role = connection.execute(
            "SELECT role FROM auth_principals WHERE user_id = ?",
            (canonical_user,),
        ).fetchone()[0]
    assert durable_role == "tenant_admin"


def test_mcp_registration_disabled_rejects_unseen_but_allows_existing_principal(
    tmp_path,
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
        allowed_client_ids=("mcp-public-client",),
        auth_state_path=str(state_path),
        require_auth_state=True,
        allow_registration=False,
    )
    access_token = token(private_key)

    assert asyncio.run(verifier.verify_token(access_token)) is None
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_principals").fetchone()[0] == 0
        now = time.time()
        connection.execute(
            """
            INSERT INTO auth_principals
                (user_id, issuer, subject, email, display_name, role, disabled_at, created_at, updated_at)
            VALUES (?, ?, ?, '', '', 'member', NULL, ?, ?)
            """,
            (
                principal_id(ISSUER, "stable-subject"),
                ISSUER,
                "stable-subject",
                now,
                now,
            ),
        )

    existing_access = asyncio.run(verifier.verify_token(access_token))

    assert existing_access is not None
    assert existing_access.subject == principal_id(ISSUER, "stable-subject")


def test_multi_user_mcp_oauth_requires_auth_state_and_allowed_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES", TOKEN_AUDIENCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", TOKEN_SCOPE)
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "oid")
    monkeypatch.setenv("GLASSHIVE_PRINCIPAL_ID_FORMAT", "hashed_issuer_subject")

    with pytest.raises(McpOAuthConfigurationError, match="ALLOWED_CLIENT_IDS"):
        oauth_from_env()

    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS", "codex-client")
    with pytest.raises(McpOAuthConfigurationError, match="AUTH_STATE_PATH"):
        oauth_from_env()

    corrupt_path = tmp_path / "auth.sqlite3"
    corrupt_path.write_text("not a database")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(corrupt_path))
    with pytest.raises(McpOAuthConfigurationError, match="unreadable"):
        oauth_from_env()


def test_multi_user_mcp_oauth_separates_public_resource_from_token_audience(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "oid")
    monkeypatch.setenv("GLASSHIVE_PRINCIPAL_ID_FORMAT", "hashed_issuer_subject")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS", "codex-client")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))

    with pytest.raises(McpOAuthConfigurationError, match="TOKEN_SCOPES"):
        oauth_from_env()

    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", TOKEN_SCOPE)
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_REGISTRATION", "false")
    with pytest.raises(McpOAuthConfigurationError, match="TOKEN_AUDIENCES"):
        oauth_from_env()

    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES",
        f"{TOKEN_AUDIENCE} api://glasshive-api",
    )
    verifier, settings = oauth_from_env()

    assert verifier.audiences == (TOKEN_AUDIENCE, "api://glasshive-api")
    assert verifier.token_scopes == (TOKEN_SCOPE,)
    assert verifier.allow_registration is False
    assert verifier.resource == RESOURCE
    assert str(settings.resource_server_url).rstrip("/") == RESOURCE


def test_mcp_oauth_rejects_invalid_registration_policy(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_REGISTRATION", "sometimes")

    with pytest.raises(
        McpOAuthConfigurationError,
        match="GLASSHIVE_ALLOW_EMAIL_REGISTRATION must be a boolean",
    ):
        oauth_from_env()


def test_mcp_canonical_closed_enrollment_rejects_unknown_subject(
    tmp_path,
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", TENANT)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES",
        CANONICAL_AUTHORIZATION_SCOPE,
    )
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", TOKEN_SCOPE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES", TOKEN_AUDIENCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_TENANT_ID", TENANT)
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "sub")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS", "mcp-public-client")
    monkeypatch.setenv("GLASSHIVE_OIDC_ROLE_MAP_JSON", '{"GlassHive.Member":"member"}')
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "false")
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_REGISTRATION", "true")

    verifier, _settings = oauth_from_env()
    access = asyncio.run(
        verifier.verify_token(token(private_key, roles=["GlassHive.Member"]))
    )

    assert verifier.allow_registration is False
    assert access is None
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_principals").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("canonical", "legacy", "expected"),
    [
        ("true", "false", True),
        ("false", "true", False),
        (None, "false", False),
    ],
)
def test_mcp_enrollment_policy_uses_canonical_then_legacy(
    monkeypatch,
    canonical,
    legacy,
    expected,
):
    if canonical is not None:
        monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", canonical)
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_REGISTRATION", legacy)

    assert oauth_module._canonical_boolean_env(
        "GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT",
        "GLASSHIVE_ALLOW_EMAIL_REGISTRATION",
        default=False,
    ) is expected


def test_entra_v2_request_scope_is_distinct_from_access_token_scope(
    tmp_path,
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", TENANT)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES", TOKEN_AUDIENCE)
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES",
        CANONICAL_AUTHORIZATION_SCOPE,
    )
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", TOKEN_SCOPE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS", "mcp-public-client")
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "oid")
    monkeypatch.setenv("GLASSHIVE_PRINCIPAL_ID_FORMAT", "hashed_issuer_subject")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "true")

    verifier, settings = oauth_from_env()
    accepted = asyncio.run(
        verifier.verify_token(
            token(private_key, oid="00000000-1111-2222-3333-444444444444")
        )
    )
    rejected_full_uri_claim = asyncio.run(
        verifier.verify_token(
            token(
                private_key,
                oid="00000000-1111-2222-3333-444444444444",
                scp=CANONICAL_AUTHORIZATION_SCOPE,
            )
        )
    )

    assert list(settings.required_scopes) == [CANONICAL_AUTHORIZATION_SCOPE]
    assert verifier.token_scopes == (TOKEN_SCOPE,)
    assert accepted is not None
    assert TOKEN_SCOPE in accepted.scopes
    assert CANONICAL_AUTHORIZATION_SCOPE in accepted.scopes
    assert rejected_full_uri_claim is None


def test_entra_v2_authorization_scope_projection_requires_validated_token_alias(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", TENANT)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES", TOKEN_AUDIENCE)
    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES",
        f"{CANONICAL_AUTHORIZATION_SCOPE} {RESOURCE}/admin",
    )
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", TOKEN_SCOPE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS", "mcp-public-client")
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "oid")
    monkeypatch.setenv("GLASSHIVE_PRINCIPAL_ID_FORMAT", "hashed_issuer_subject")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))

    with pytest.raises(
        McpOAuthConfigurationError,
        match="authorization scope must map",
    ):
        oauth_from_env()

    monkeypatch.setenv(
        "GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES",
        f"{CANONICAL_AUTHORIZATION_SCOPE} {RESOURCE}/team/{TOKEN_SCOPE}",
    )
    with pytest.raises(
        McpOAuthConfigurationError,
        match="authorization scope must map",
    ):
        oauth_from_env()


def test_generic_oidc_exact_uri_scope_requires_no_alias_projection(
    monkeypatch,
    signing_material,
):
    private_key, public_jwk = signing_material
    exact_uri_scope = f"{RESOURCE}/read"

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(exact_uri_scope,),
        authorization_scopes=(exact_uri_scope,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
    )

    access = asyncio.run(
        verifier.verify_token(token(private_key, scp=exact_uri_scope))
    )

    assert access is not None
    assert access.scopes == [exact_uri_scope]
    assert verifier.authorization_scope_aliases == {}


def test_legacy_enterprise_flag_does_not_opt_into_canonical_mcp_enrollment(tmp_path, monkeypatch):
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)
    monkeypatch.delenv("GLASSHIVE_SECURITY_MODE", raising=False)
    monkeypatch.setenv("WPR_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES", TOKEN_AUDIENCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", TOKEN_SCOPE)
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "oid")
    monkeypatch.setenv("GLASSHIVE_PRINCIPAL_ID_FORMAT", "hashed_issuer_subject")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))

    verifier, _settings = oauth_from_env()

    assert verifier.require_auth_state is False
    assert verifier.allowed_client_ids == ()


def test_effective_multi_user_mcp_rejects_raw_principal_ids(tmp_path, monkeypatch):
    state_path = tmp_path / "auth.sqlite3"
    create_auth_state(state_path)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES", TOKEN_AUDIENCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", TOKEN_SCOPE)
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "oid")
    monkeypatch.setenv("GLASSHIVE_PRINCIPAL_ID_FORMAT", "raw_claim")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ALLOWED_CLIENT_IDS", "codex-client")

    with pytest.raises(McpOAuthConfigurationError, match="hashed issuer/subject"):
        oauth_from_env()


def test_mcp_oauth_uses_the_same_stable_claim_and_verified_domain_policy(monkeypatch, signing_material):
    private_key, public_jwk = signing_material

    def fake_get(url, **kwargs):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return FakeResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})
        if url == f"{ISSUER}/jwks":
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    monkeypatch.setattr(oauth_module.httpx, "get", fake_get)
    verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
        subject_claim="oid",
        allowed_email_domains=("example.invalid",),
    )

    allowed = asyncio.run(
        verifier.verify_token(
            token(
                private_key,
                sub="pairwise-mcp-subject",
                oid="stable-object-id",
                email="member@team.example.invalid",
                email_verified=True,
            )
        )
    )
    outside = asyncio.run(
        verifier.verify_token(
            token(private_key, oid="stable-object-id", email="member@outside.invalid", email_verified=True)
        )
    )
    unverified = asyncio.run(
        verifier.verify_token(
            token(private_key, oid="stable-object-id", email="member@example.invalid", email_verified=False)
        )
    )

    assert allowed is not None
    assert allowed.subject == principal_id(ISSUER, "stable-object-id")
    assert outside is None
    assert unverified is None

    trusted_proxy_verifier = OidcJwtTokenVerifier(
        issuer=ISSUER,
        audience=TOKEN_AUDIENCE,
        resource=RESOURCE,
        token_scopes=(TOKEN_SCOPE,),
        deployment_tenant_id=TENANT,
        token_tenant_id=TENANT,
        subject_claim="oid",
        principal_id_format="raw_claim",
    )
    trusted_proxy_access = asyncio.run(
        trusted_proxy_verifier.verify_token(token(private_key, oid="stable-object-id"))
    )
    assert trusted_proxy_access is not None
    assert trusted_proxy_access.subject == "stable-object-id"


def test_mcp_oauth_configuration_is_all_or_nothing_and_https(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.delenv("GLASSHIVE_MCP_PUBLIC_URL", raising=False)
    with pytest.raises(McpOAuthConfigurationError, match="both"):
        oauth_from_env()

    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", "http://glasshive.example.invalid/mcp")
    with pytest.raises(McpOAuthConfigurationError, match="HTTPS"):
        oauth_from_env()

    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    for local_https_url in (
        "https://localhost/mcp",
        "https://service.localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://10.0.0.1/mcp",
    ):
        monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
        monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", local_https_url)
        with pytest.raises(McpOAuthConfigurationError, match="HTTPS"):
            oauth_from_env()

    for prefix_bypass in (
        "http://localhost.evil.example/mcp",
        "http://127.0.0.1.evil.example/mcp",
    ):
        assert oauth_module._oauth_url_allowed(prefix_bypass) is False
        monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", prefix_bypass)
        with pytest.raises(McpOAuthConfigurationError, match="HTTPS"):
            oauth_from_env()

    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", "http://localhost:9000")
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", "http://127.0.0.1:8767/mcp")
    with pytest.raises(McpOAuthConfigurationError, match="HTTPS"):
        oauth_from_env()


def test_multi_user_mcp_startup_requires_oauth(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.delenv("GLASSHIVE_MCP_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("GLASSHIVE_MCP_PUBLIC_URL", raising=False)

    with pytest.raises(McpOAuthConfigurationError, match="requires configured OAuth"):
        create_mcp_server(api_client=object())


def test_oauth_principal_replaces_all_raw_identity_aliases(monkeypatch):
    monkeypatch.setattr(
        mcp_server_module,
        "get_http_headers",
        lambda: {
            "x-glasshive-user-id": "attacker",
            "x-librechat-storage-user-id": "victim-storage",
            "x-glasshive-tenant-id": "attacker-tenant",
            "x-librechat-user-email": "victim@example.invalid",
            "x-glasshive-user-role": "tenant_admin",
        },
    )
    monkeypatch.setattr(
        mcp_server_module,
        "get_access_token",
        lambda: SimpleNamespace(
            subject="verified-user",
            claims={"tenant_id": "verified-tenant", "role": "member"},
        ),
    )

    headers = mcp_server_module._request_headers()

    assert mcp_server_module._header_value(headers, mcp_server_module.HEADER_USER_ID) == "verified-user"
    assert mcp_server_module._header_value(headers, mcp_server_module.HEADER_STORAGE_USER_ID) == "verified-user"
    assert mcp_server_module._header_value(headers, mcp_server_module.HEADER_TENANT_ID) == "verified-tenant"
    assert mcp_server_module._header_value(headers, mcp_server_module.HEADER_USER_EMAIL) == ""
    assert mcp_server_module._header_value(headers, mcp_server_module.HEADER_USER_ROLE) == "member"

def test_mcp_oauth_hashed_principal_requires_same_browser_issuer(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://different-identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_AUDIENCES", TOKEN_AUDIENCE)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_TOKEN_SCOPES", TOKEN_SCOPE)
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "oid")
    monkeypatch.setenv("GLASSHIVE_PRINCIPAL_ID_FORMAT", "hashed_issuer_subject")

    with pytest.raises(McpOAuthConfigurationError, match="issuers must match"):
        oauth_from_env()


def test_fastmcp_oauth_publishes_rfc9728_metadata_and_challenge(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_RELEASE_ID", "release-public-safe")
    monkeypatch.setenv("GLASSHIVE_PARENT_REVISION", "a" * 40)
    monkeypatch.setenv("GLASSHIVE_COMPONENT_REVISION", "b" * 40)
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_MCP_PUBLIC_URL", RESOURCE)
    required_scope = f"{RESOURCE}/access_as_user"
    monkeypatch.setenv("GLASSHIVE_MCP_OAUTH_REQUIRED_SCOPES", required_scope)
    server = create_mcp_server(api_client=object())
    app = server.streamable_http_app()
    client = TestClient(app)

    metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    health = client.get("/health")
    unauthorized = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    assert metadata.status_code == 200
    assert health.status_code == 200
    assert health.json()["release"] == {
        "release_id": "release-public-safe",
        "parent_revision": "a" * 40,
        "glasshive_revision": "b" * 40,
    }
    assert metadata.json()["resource"] == RESOURCE
    assert [value.rstrip("/") for value in metadata.json()["authorization_servers"]] == [ISSUER]
    assert metadata.json()["scopes_supported"] == [required_scope]
    assert unauthorized.status_code == 401
    assert "resource_metadata=" in unauthorized.headers["www-authenticate"]
    assert f'scope="{required_scope}"' in unauthorized.headers["www-authenticate"]


def test_oauth_mcp_front_door_mints_narrow_signed_runtime_assertion(tmp_path, monkeypatch, signing_material):
    private_key, _ = signing_material
    key_path = tmp_path / "mcp-gateway.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_PRIVATE_KEY_FILE", str(key_path))
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_ISSUER", "https://mcp-gateway.example.invalid")
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_AUDIENCE", "glasshive-runtime")
    monkeypatch.setenv("GLASSHIVE_INTERNAL_ASSERTION_KEY_ID", "mcp-gateway-test")
    monkeypatch.setattr(
        mcp_server_module,
        "_request_headers",
        lambda: {
            "x-viventium-tenant-id": TENANT,
            "x-viventium-user-id": "usr_public_safe",
            "x-viventium-user-email": "member@example.invalid",
            "x-viventium-user-role": "member",
        },
    )
    monkeypatch.setattr(mcp_server_module, "_require_enterprise_mcp_service_auth", lambda headers: None)
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"owner_id": "usr_public_safe"}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, method, url, json=None, headers=None):
            captured.update({"method": method, "url": url, "headers": dict(headers or {})})
            return FakeResponse()

    monkeypatch.setattr(mcp_server_module.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        mcp_server_module,
        "get_access_token",
        lambda: SimpleNamespace(subject="usr_public_safe", claims={"tenant_id": TENANT}),
    )
    client = WorkersProjectsApiClient(base_url="http://runtime.example.invalid", api_token="service-token")

    assert client.get_preferences()["owner_id"] == "usr_public_safe"
    assertion = captured["headers"]["X-GlassHive-User-Assertion"]
    claims = jwt.decode(
        assertion,
        private_key.public_key(),
        algorithms=["RS256"],
        issuer="https://mcp-gateway.example.invalid",
        audience="glasshive-runtime",
    )
    assert claims["sub"] == "usr_public_safe"
    assert claims["tenant_id"] == TENANT
    assert claims["scope"] == "runtime:access workspaces:read"
    assert "x-viventium-user-id" not in captured["headers"]
