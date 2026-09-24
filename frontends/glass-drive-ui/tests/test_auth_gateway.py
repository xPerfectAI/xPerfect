from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

import glass_drive_ui.auth_gateway as auth_gateway_module
from glass_drive_ui.auth_gateway import AuthGatewayError, HumanAuthGateway


def configure_oidc(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv("GLASSHIVE_OIDC_REDIRECT_URI", "https://glasshive.example.invalid/auth/oidc/callback")
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_REGISTRATION", "true")
    monkeypatch.setenv("GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY", "synthetic-throttle-key-for-tests-12345")


def test_local_password_login_requires_a_private_throttle_key(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    monkeypatch.delenv("GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY")

    with pytest.raises(RuntimeError, match="LOCAL_AUTH_THROTTLE_KEY"):
        HumanAuthGateway.from_env()


def test_browser_login_presentation_rejects_hiding_every_method(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "false")
    monkeypatch.setenv("GLASSHIVE_OIDC_LOGIN_VISIBLE", "false")

    with pytest.raises(RuntimeError, match="at least one browser login method"):
        HumanAuthGateway.from_env()


def test_local_password_provisioning_requires_admin_generated_entropy(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()
    gateway.preapprove_oidc_principal(subject="password-policy-subject", role="member")

    for weak_password in ("short but varied", "a" * 24):
        with pytest.raises(AuthGatewayError, match="24 characters.*12 distinct"):
            gateway.provision_local_password(
                subject="password-policy-subject",
                login_email="password-policy@example.invalid",
                password=weak_password,
            )

    with sqlite3.connect(gateway.state_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM auth_local_credentials"
        ).fetchone() == (0,)


def test_oidc_principal_rejects_non_utf8_display_metadata(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()

    with pytest.raises(AuthGatewayError, match="valid display name"):
        gateway.upsert_oidc_principal(
            issuer="https://identity.example.invalid",
            subject="display-name-subject",
            email="display@example.invalid",
            display_name="broken\ud800",
            role="member",
        )

    assert gateway.find_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="display-name-subject",
    ) is None


def test_default_auth_state_path_uses_platform_not_a_home_path_heuristic(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_AUTH_STATE_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    fake_home = Path("/", "Users", "synthetic-home-shape")
    monkeypatch.setattr(auth_gateway_module.Path, "home", classmethod(lambda cls: fake_home))

    monkeypatch.setattr(auth_gateway_module.sys, "platform", "linux")
    assert auth_gateway_module._default_state_path() == (
        fake_home / ".local" / "state" / "glasshive" / "auth.sqlite3"
    )

    monkeypatch.setattr(auth_gateway_module.sys, "platform", "darwin")
    assert auth_gateway_module._default_state_path() == (
        fake_home / "Library" / "Application Support" / "GlassHive" / "auth.sqlite3"
    )


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://localhost/oidc",
        "https://service.localhost/oidc",
        "https://127.0.0.1/oidc",
        "https://[::1]/oidc",
        "https://10.0.0.1/oidc",
    ],
)
def test_multi_user_oidc_rejects_https_loopback_and_private_literal_urls(
    tmp_path,
    monkeypatch,
    unsafe_url,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "sub")
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", unsafe_url)

    with pytest.raises(RuntimeError, match="OIDC issuer must use HTTPS"):
        HumanAuthGateway.from_env()


def test_oidc_principal_identity_is_stable_from_issuer_and_subject(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()

    first = gateway.upsert_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="stable-subject",
        email="old@example.invalid",
        display_name="Old Name",
        role="member",
    )
    second = gateway.upsert_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="stable-subject",
        email="new@example.invalid",
        display_name="New Name",
        role="member",
    )

    assert first["user_id"] == second["user_id"]
    assert second["email"] == "new@example.invalid"
    with sqlite3.connect(gateway.state_path) as conn:
        row = conn.execute("SELECT issuer, subject FROM auth_principals").fetchone()
    assert row == ("https://identity.example.invalid", "stable-subject")


@pytest.mark.parametrize("unsafe_return_to", ["//evil.example.invalid", "/\\evil.example.invalid", "https://evil.example.invalid"])
def test_oidc_return_target_rejects_network_path_and_backslash_confusion(
    tmp_path,
    monkeypatch,
    unsafe_return_to,
):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()
    monkeypatch.setattr(
        gateway,
        "_oidc_configuration",
        lambda: {"authorization_endpoint": "https://identity.example.invalid/authorize"},
    )

    flow = gateway.begin_oidc(return_to=unsafe_return_to)
    with sqlite3.connect(gateway.state_path) as connection:
        stored = connection.execute(
            "SELECT return_to FROM auth_oidc_flows WHERE state_hash = ?",
            (auth_gateway_module._hash_secret(flow["state"]),),
        ).fetchone()

    assert stored == ("/",)


def test_oidc_authorization_code_pkce_nonce_and_single_use_callback(tmp_path, monkeypatch):
    issuer = "https://identity.example.invalid"
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_OIDC_PRINCIPAL_CLAIM", "oid")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "oidc-test-key", "alg": "RS256", "use": "sig"})
    captured_token_request = {}

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResponse(
                {
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "jwks_uri": f"{issuer}/jwks",
                }
            )
        if url.endswith("/jwks"):
            return FakeResponse({"keys": [public_jwk]})
        raise AssertionError(url)

    def fake_post(url, data=None, **kwargs):
        captured_token_request.update({"url": url, "data": dict(data or {})})
        nonce = captured_token_request.pop("expected_nonce")
        authorized_party = captured_token_request.pop("azp", "public-safe-client")
        stable_object_id = captured_token_request.pop("oid", "stable-object-id")
        display_email = captured_token_request.pop("email", "member@example.invalid")
        now = int(auth_gateway_module.time.time())
        id_token = jwt.encode(
            {
                "iss": issuer,
                "aud": ["public-safe-client", "secondary-audience"],
                "azp": authorized_party,
                "sub": "pairwise-ui-subject",
                "oid": stable_object_id,
                "email": display_email,
                "email_verified": True,
                "name": "Example Member",
                "nonce": nonce,
                "iat": now,
                "nbf": now - 1,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "oidc-test-key"},
        )
        return FakeResponse({"id_token": id_token, "access_token": "must-not-be-persisted"})

    monkeypatch.setattr(auth_gateway_module.httpx, "get", fake_get)
    monkeypatch.setattr(auth_gateway_module.httpx, "post", fake_post)
    gateway = HumanAuthGateway.from_env()

    flow = gateway.begin_oidc(return_to="/workspaces")
    query = parse_qs(urlparse(flow["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [flow["state"]]
    assert query["nonce"] == [flow["nonce"]]
    assert "client_secret" not in query
    captured_token_request["expected_nonce"] = flow["nonce"]

    completed = gateway.complete_oidc(state=flow["state"], code="synthetic-code")

    assert completed["principal"]["user_id"].startswith("usr_")
    assert completed["principal"]["user_id"] == auth_gateway_module._principal_id(
        issuer, "stable-object-id"
    )
    assert completed["principal"]["email"] == "member@example.invalid"
    assert completed["return_to"] == "/workspaces"
    assert captured_token_request["url"] == f"{issuer}/token"
    assert captured_token_request["data"]["code_verifier"]
    assert b"must-not-be-persisted" not in gateway.state_path.read_bytes()
    with pytest.raises(AuthGatewayError, match="expired or already used"):
        gateway.complete_oidc(state=flow["state"], code="synthetic-code")

    # Once registration is closed, mutable display claims cannot change ownership,
    # while a genuinely different immutable object id is denied as a new account.
    gateway.allow_registration = False
    mutation_flow = gateway.begin_oidc(return_to="/workspaces")
    captured_token_request["expected_nonce"] = mutation_flow["nonce"]
    captured_token_request["email"] = "renamed@example.invalid"
    mutation = gateway.complete_oidc(state=mutation_flow["state"], code="synthetic-code")
    assert mutation["principal"]["user_id"] == completed["principal"]["user_id"]
    assert mutation["principal"]["email"] == "renamed@example.invalid"

    unregistered_flow = gateway.begin_oidc(return_to="/workspaces")
    captured_token_request["expected_nonce"] = unregistered_flow["nonce"]
    captured_token_request["oid"] = "different-stable-object-id"
    with pytest.raises(AuthGatewayError, match="not been approved") as unregistered:
        gateway.complete_oidc(state=unregistered_flow["state"], code="synthetic-code")
    assert unregistered.value.code == "account_not_registered"

    second_flow = gateway.begin_oidc(return_to="/workspaces")
    captured_token_request["expected_nonce"] = second_flow["nonce"]
    captured_token_request["azp"] = "different-client"
    with pytest.raises(AuthGatewayError, match="authorized party"):
        gateway.complete_oidc(state=second_flow["state"], code="synthetic-code")


def test_gateway_refuses_to_duplicate_email_password_identity_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "email")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(tmp_path / "auth.sqlite3"))

    with pytest.raises(RuntimeError, match="external identity provider"):
        HumanAuthGateway.from_env()


def test_oidc_role_map_fails_closed_for_missing_or_unmapped_claims(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()

    assert gateway._oidc_role({}) == "member"

    monkeypatch.setenv(
        "GLASSHIVE_OIDC_ROLE_MAP_JSON",
        json.dumps({"Workspace Readers": "viewer", "Workspace Members": "member"}),
    )
    with pytest.raises(AuthGatewayError, match="approved GlassHive role") as missing:
        gateway._oidc_role({})
    assert missing.value.code == "account_not_authorized"
    with pytest.raises(AuthGatewayError, match="approved GlassHive role") as unknown:
        gateway._oidc_role({"roles": ["Unknown Group"]})
    assert unknown.value.code == "account_not_authorized"
    assert gateway._oidc_role({"roles": ["Workspace Readers"]}) == "viewer"
    assert gateway._oidc_role({"roles": ["Workspace Members"]}) == "member"


def test_allowed_domain_policy_requires_a_valid_approved_email(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_EMAIL_DOMAINS", "example.invalid")
    gateway = HumanAuthGateway.from_env()

    assert gateway.email_allowed("member@example.invalid") is True
    assert gateway.email_allowed("member@team.example.invalid") is True
    assert gateway.email_allowed("member@outside.invalid") is False
    assert gateway.email_allowed("") is False


def test_closed_registration_accepts_existing_oidc_principal_only(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()
    existing = gateway.upsert_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="existing-object-id",
        email="old@example.invalid",
        display_name="Existing User",
        role="member",
    )
    existing_session = gateway.create_session(existing["user_id"])
    gateway.allow_registration = False

    refreshed = gateway.reconcile_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="existing-object-id",
        email="new@outside.invalid",
        display_name="Renamed User",
        role="tenant_admin",
    )
    assert refreshed["user_id"] == existing["user_id"]
    assert refreshed["email"] == "new@outside.invalid"
    assert refreshed["role"] == "tenant_admin"
    assert gateway.resolve_session(existing_session["token"])["role"] == "tenant_admin"

    demoted = gateway.reconcile_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="existing-object-id",
        email="new@outside.invalid",
        display_name="Renamed User",
        role="viewer",
    )
    assert demoted["role"] == "viewer"
    assert gateway.resolve_session(existing_session["token"])["role"] == "viewer"

    with pytest.raises(AuthGatewayError, match="not been approved") as denied:
        gateway.reconcile_oidc_principal(
            issuer="https://identity.example.invalid",
            subject="unseen-object-id",
            email="new@example.invalid",
            display_name="Unseen User",
            role="member",
        )
    assert denied.value.code == "account_not_registered"


def test_admin_preapproval_uses_configured_issuer_and_immutable_subject(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_REGISTRATION", "true")
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "false")
    gateway = HumanAuthGateway.from_env()

    preapproved = gateway.preapprove_oidc_principal(
        subject="stable-object-id",
        email="member@example.invalid",
        display_name="Example Member",
        role="member",
    )
    repeated = gateway.preapprove_oidc_principal(
        subject="stable-object-id",
        email="renamed@example.invalid",
        display_name="Renamed Member",
        role="viewer",
    )
    reconciled = gateway.reconcile_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="stable-object-id",
        email="renamed@example.invalid",
        display_name="Renamed Member",
        role="viewer",
    )

    assert gateway.allow_registration is False
    assert preapproved["user_id"] == repeated["user_id"] == reconciled["user_id"]
    assert repeated["role"] == "viewer"
    with sqlite3.connect(gateway.state_path) as conn:
        row = conn.execute(
            "SELECT issuer, subject FROM auth_principals WHERE user_id = ?",
            (preapproved["user_id"],),
        ).fetchone()
    assert row == ("https://identity.example.invalid", "stable-object-id")


def test_admin_preapproval_never_merges_distinct_subjects_by_email(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "false")
    gateway = HumanAuthGateway.from_env()

    first = gateway.preapprove_oidc_principal(
        subject="first-object-id",
        email="shared@example.invalid",
        display_name="First Member",
        role="member",
    )
    second = gateway.preapprove_oidc_principal(
        subject="second-object-id",
        email="shared@example.invalid",
        display_name="Second Member",
        role="member",
    )

    assert first["user_id"] != second["user_id"]
    with sqlite3.connect(gateway.state_path) as conn:
        assert conn.execute("SELECT count(*) FROM auth_principals").fetchone() == (2,)


@pytest.mark.parametrize(
    ("subject", "role", "message"),
    [
        ("", "member", "stable subject"),
        ("bad\nsubject", "member", "stable subject"),
        ("x" * 513, "member", "stable subject"),
        ("stable-object-id", "owner", "approved GlassHive role"),
    ],
)
def test_admin_preapproval_rejects_invalid_immutable_identity(
    tmp_path,
    monkeypatch,
    subject,
    role,
    message,
):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()

    with pytest.raises(AuthGatewayError, match=message):
        gateway.preapprove_oidc_principal(
            subject=subject,
            email="member@example.invalid",
            display_name="Example Member",
            role=role,
        )


def test_provider_email_and_principal_enrollment_settings_use_canonical_precedence(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_LOGIN", "false")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_EMAIL_LOGIN", "true")
    monkeypatch.setenv("GLASSHIVE_ALLOW_EMAIL_REGISTRATION", "true")
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "false")

    gateway = HumanAuthGateway.from_env()

    assert gateway.provider_email_login is True
    assert gateway.allow_registration is False


def test_admin_preapproval_requires_the_configured_oidc_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "disabled")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    gateway = HumanAuthGateway.from_env()

    with pytest.raises(AuthGatewayError, match="OIDC issuer is unavailable"):
        gateway.preapprove_oidc_principal(subject="stable-object-id")


def test_provider_logout_requires_registered_post_logout_uri(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_POST_LOGOUT_REDIRECT_URI",
        "https://glasshive.example.invalid/login",
    )
    gateway = HumanAuthGateway.from_env()
    monkeypatch.setattr(
        gateway,
        "_oidc_configuration",
        lambda: {"end_session_endpoint": "https://identity.example.invalid/logout"},
    )

    parsed = urlparse(gateway.provider_logout_url())
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://identity.example.invalid/logout"
    assert query == {
        "post_logout_redirect_uri": ["https://glasshive.example.invalid/login"],
        "client_id": ["public-safe-client"],
    }

    gateway.oidc_post_logout_redirect_uri = ""
    assert gateway.provider_logout_url() == ""


def test_admin_disable_revokes_sessions_and_oidc_relogin_does_not_reenable(tmp_path, monkeypatch):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()
    principal = gateway.upsert_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="disable-subject",
        email="member@example.invalid",
        display_name="Example Member",
        role="member",
    )
    session = gateway.create_session(principal["user_id"])
    assert gateway.resolve_session(session["token"]) is not None

    disabled = gateway.set_principal_disabled(
        principal_id=principal["user_id"], disabled=True
    )

    assert disabled["disabled"] is True
    assert gateway.resolve_session(session["token"]) is None
    with pytest.raises(AuthGatewayError, match="unavailable"):
        gateway.create_session(principal["user_id"])
    gateway.upsert_oidc_principal(
        issuer="https://identity.example.invalid",
        subject="disable-subject",
        email="updated@example.invalid",
        display_name="Updated Member",
        role="member",
    )
    assert gateway.list_principals()[0]["disabled"] is True

    enabled = gateway.set_principal_disabled(
        principal_id=principal["user_id"], disabled=False
    )
    assert enabled["disabled"] is False
    assert gateway.create_session(principal["user_id"])["token"]


def test_local_password_is_default_off_and_attaches_only_to_an_exact_preapproved_subject(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    gateway = HumanAuthGateway.from_env()
    approved = gateway.preapprove_oidc_principal(
        subject="stable-local-login-subject",
        email="display-only@example.invalid",
        role="member",
    )

    assert gateway.local_password_login is False
    provisioned = gateway.provision_local_password(
        subject="stable-local-login-subject",
        login_email="login@example.invalid",
        password="correct horse battery staple",
    )
    with pytest.raises(AuthGatewayError, match="disabled"):
        gateway.authenticate_local_password(
            login_email="login@example.invalid",
            password="correct horse battery staple",
            source="192.0.2.5",
        )

    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    gateway = HumanAuthGateway.from_env()

    assert provisioned["user_id"] == approved["user_id"]
    with sqlite3.connect(gateway.state_path) as connection:
        row = connection.execute(
            "SELECT principal_id, login_email, password_phc FROM auth_local_credentials"
        ).fetchone()
        columns = {
            item[1]
            for item in connection.execute("PRAGMA table_info(auth_local_credentials)")
        }
    assert row[0:2] == (approved["user_id"], "login@example.invalid")
    assert row[2].startswith("$argon2id$")
    assert "$m=19456,t=2,p=1$" in row[2]
    assert "password" not in columns
    assert "password_phc" in columns
    assert "correct horse battery staple" not in row[2]

    with pytest.raises(AuthGatewayError, match="preapproved"):
        gateway.provision_local_password(
            subject="different-subject-with-same-display-email",
            login_email="another-login@example.invalid",
            password="another correct horse battery staple",
        )
    assert len(gateway.list_principals()) == 1


def test_local_password_login_is_generic_persistent_and_rotation_revokes_sessions(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    gateway = HumanAuthGateway.from_env()
    principal = gateway.preapprove_oidc_principal(subject="local-auth-subject", role="member")
    oidc_session = gateway.create_session(principal["user_id"])
    gateway.provision_local_password(
        subject="local-auth-subject",
        login_email="local-member@example.invalid",
        password="first synthetic passphrase",
    )

    session = gateway.authenticate_local_password(
        login_email="LOCAL-MEMBER@example.invalid",
        password="first synthetic passphrase",
        source="192.0.2.10",
    )
    resolved = gateway.resolve_session(session["token"])
    assert resolved is not None
    assert resolved["user_id"] == principal["user_id"]
    assert resolved["auth_method"] == "local_password"

    failures = []
    for email, password in (
        ("local-member@example.invalid", "not the password at all"),
        ("unknown@example.invalid", "not the password at all"),
    ):
        with pytest.raises(AuthGatewayError) as failure:
            gateway.authenticate_local_password(
                login_email=email,
                password=password,
                source="192.0.2.11",
            )
        failures.append((failure.value.code, str(failure.value)))
    assert failures == [("sign_in_failed", "Email or password is incorrect")] * 2

    with sqlite3.connect(gateway.state_path) as connection:
        first_phc = connection.execute(
            "SELECT password_phc FROM auth_local_credentials WHERE principal_id = ?",
            (principal["user_id"],),
        ).fetchone()[0]
    gateway.provision_local_password(
        subject="local-auth-subject",
        login_email="local-member@example.invalid",
        password="second synthetic passphrase",
    )
    with sqlite3.connect(gateway.state_path) as connection:
        second_phc = connection.execute(
            "SELECT password_phc FROM auth_local_credentials WHERE principal_id = ?",
            (principal["user_id"],),
        ).fetchone()[0]
    assert first_phc != second_phc
    assert gateway.resolve_session(session["token"]) is None
    assert gateway.resolve_session(oidc_session["token"]) is not None

    with pytest.raises(AuthGatewayError, match="Email or password is incorrect"):
        gateway.authenticate_local_password(
            login_email="local-member@example.invalid",
            password="first synthetic passphrase",
            source="192.0.2.10",
        )
    assert gateway.authenticate_local_password(
        login_email="local-member@example.invalid",
        password="second synthetic passphrase",
        source="192.0.2.10",
    )["token"]


def test_local_password_lockout_is_durable_and_disabled_accounts_are_indistinguishable(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    monkeypatch.setattr(auth_gateway_module, "LOCAL_ACCOUNT_MAX_FAILURES", 2)
    monkeypatch.setattr(auth_gateway_module, "LOCAL_LOCK_BASE_SECONDS", 10)
    now = [1_900_000_000.0]
    monkeypatch.setattr(auth_gateway_module.time, "time", lambda: now[0])
    gateway = HumanAuthGateway.from_env()
    principal = gateway.preapprove_oidc_principal(subject="locked-local-subject", role="member")
    gateway.provision_local_password(
        subject="locked-local-subject",
        login_email="locked@example.invalid",
        password="valid synthetic passphrase",
    )

    for _ in range(2):
        with pytest.raises(AuthGatewayError, match="Email or password is incorrect"):
            gateway.authenticate_local_password(
                login_email="locked@example.invalid",
                password="wrong synthetic passphrase",
                source="192.0.2.20",
            )

    restarted = HumanAuthGateway.from_env()
    with pytest.raises(AuthGatewayError, match="Email or password is incorrect"):
        restarted.authenticate_local_password(
            login_email="locked@example.invalid",
            password="valid synthetic passphrase",
            source="192.0.2.20",
        )

    now[0] += 11
    restarted.unlock_local_password(subject="locked-local-subject")
    assert restarted.authenticate_local_password(
        login_email="locked@example.invalid",
        password="valid synthetic passphrase",
        source="192.0.2.20",
    )["token"]

    restarted.set_principal_disabled(principal_id=principal["user_id"], disabled=True)
    with pytest.raises(AuthGatewayError) as disabled:
        restarted.authenticate_local_password(
            login_email="locked@example.invalid",
            password="valid synthetic passphrase",
            source="192.0.2.21",
        )
    with pytest.raises(AuthGatewayError) as unknown:
        restarted.authenticate_local_password(
            login_email="missing@example.invalid",
            password="valid synthetic passphrase",
            source="192.0.2.22",
        )
    assert (disabled.value.code, str(disabled.value)) == (
        unknown.value.code,
        str(unknown.value),
    ) == ("sign_in_failed", "Email or password is incorrect")


def test_disabling_local_password_login_revokes_existing_local_sessions_only(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    gateway = HumanAuthGateway.from_env()
    principal = gateway.preapprove_oidc_principal(subject="rollback-local-subject", role="member")
    gateway.provision_local_password(
        subject="rollback-local-subject",
        login_email="rollback@example.invalid",
        password="rollback synthetic passphrase",
    )
    oidc_session = gateway.create_session(principal["user_id"])
    local_session = gateway.authenticate_local_password(
        login_email="rollback@example.invalid",
        password="rollback synthetic passphrase",
        source="192.0.2.30",
    )
    with sqlite3.connect(gateway.state_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM auth_sessions WHERE session_hash = ?",
            (auth_gateway_module._hash_secret(local_session["token"]),),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM auth_local_sessions WHERE session_hash = ?",
            (auth_gateway_module._hash_secret(local_session["token"]),),
        ).fetchone() == (1,)

    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "false")
    disabled_gateway = HumanAuthGateway.from_env()

    assert disabled_gateway.resolve_session(local_session["token"]) is None
    assert disabled_gateway.resolve_session(oidc_session["token"]) is not None


def test_success_on_one_account_cannot_clear_source_throttle_for_password_spray(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    monkeypatch.setattr(auth_gateway_module, "LOCAL_SOURCE_MAX_FAILURES", 3)
    gateway = HumanAuthGateway.from_env()
    for subject, login_email in (
        ("source-safe-a", "source-a@example.invalid"),
        ("source-safe-b", "source-b@example.invalid"),
    ):
        gateway.preapprove_oidc_principal(subject=subject, role="member")
        gateway.provision_local_password(
            subject=subject,
            login_email=login_email,
            password="shared synthetic passphrase",
        )

    for _ in range(2):
        with pytest.raises(AuthGatewayError):
            gateway.authenticate_local_password(
                login_email="source-b@example.invalid",
                password="wrong synthetic passphrase",
                source="192.0.2.70",
            )
    assert gateway.authenticate_local_password(
        login_email="source-a@example.invalid",
        password="shared synthetic passphrase",
        source="192.0.2.70",
    )["token"]
    with pytest.raises(AuthGatewayError):
        gateway.authenticate_local_password(
            login_email="source-b@example.invalid",
            password="wrong synthetic passphrase",
            source="192.0.2.70",
        )
    with pytest.raises(AuthGatewayError, match="Email or password is incorrect"):
        gateway.authenticate_local_password(
            login_email="source-a@example.invalid",
            password="shared synthetic passphrase",
            source="192.0.2.70",
        )
    with sqlite3.connect(gateway.state_path) as connection:
        source_hash = connection.execute(
            "SELECT source_hash FROM auth_local_source_attempts"
        ).fetchone()[0]
    assert len(source_hash) == 64
    assert source_hash != "192.0.2.70"
    assert "192.0.2.70" not in gateway.state_path.read_bytes().decode(
        "utf-8", errors="ignore"
    )


def test_local_credentials_and_sessions_fail_closed_after_oidc_issuer_change(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    gateway = HumanAuthGateway.from_env()
    gateway.preapprove_oidc_principal(subject="issuer-bound-subject", role="member")
    gateway.provision_local_password(
        subject="issuer-bound-subject",
        login_email="issuer-bound@example.invalid",
        password="issuer bound synthetic passphrase",
    )
    session = gateway.authenticate_local_password(
        login_email="issuer-bound@example.invalid",
        password="issuer bound synthetic passphrase",
        source="192.0.2.80",
    )

    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://replacement-identity.example.invalid")
    replacement = HumanAuthGateway.from_env()

    with pytest.raises(AuthGatewayError, match="Email or password is incorrect"):
        replacement.authenticate_local_password(
            login_email="issuer-bound@example.invalid",
            password="issuer bound synthetic passphrase",
            source="192.0.2.81",
        )
    assert replacement.resolve_session(session["token"]) is None


def test_password_rotation_does_not_charge_stale_verified_attempt_to_new_credential(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    gateway = HumanAuthGateway.from_env()
    gateway.preapprove_oidc_principal(subject="rotation-race-subject", role="member")
    gateway.provision_local_password(
        subject="rotation-race-subject",
        login_email="rotation-race@example.invalid",
        password="before rotation synthetic passphrase",
    )
    entered = threading.Event()
    release = threading.Event()
    real_match = auth_gateway_module._password_matches

    def blocking_match(password, phc):
        result = real_match(password, phc)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(auth_gateway_module, "_password_matches", blocking_match)
    result: list[str] = []

    def stale_login() -> None:
        try:
            gateway.authenticate_local_password(
                login_email="rotation-race@example.invalid",
                password="before rotation synthetic passphrase",
                source="192.0.2.82",
            )
        except AuthGatewayError as exc:
            result.append(exc.code)

    thread = threading.Thread(target=stale_login)
    thread.start()
    assert entered.wait(5)
    gateway.provision_local_password(
        subject="rotation-race-subject",
        login_email="rotation-race@example.invalid",
        password="after rotation synthetic passphrase",
    )
    release.set()
    thread.join(5)

    assert result == ["sign_in_failed"]
    with sqlite3.connect(gateway.state_path) as connection:
        assert connection.execute(
            "SELECT failed_attempts, locked_until FROM auth_local_credentials"
        ).fetchone() == (0, None)


def test_kdf_capacity_and_source_lock_fail_without_charging_the_account(
    tmp_path,
    monkeypatch,
):
    configure_oidc(tmp_path, monkeypatch)
    monkeypatch.setenv("GLASSHIVE_LOCAL_PASSWORD_LOGIN", "true")
    gateway = HumanAuthGateway.from_env()
    gateway.preapprove_oidc_principal(subject="kdf-capacity-subject", role="member")
    gateway.provision_local_password(
        subject="kdf-capacity-subject",
        login_email="kdf-capacity@example.invalid",
        password="capacity synthetic passphrase",
    )

    class BusySlots:
        def acquire(self, **_kwargs):
            return False

        def release(self):
            raise AssertionError("unacquired KDF slot must not be released")

    monkeypatch.setattr(auth_gateway_module, "_PASSWORD_KDF_SLOTS", BusySlots())
    with pytest.raises(AuthGatewayError) as busy:
        gateway.authenticate_local_password(
            login_email="kdf-capacity@example.invalid",
            password="capacity synthetic passphrase",
            source="192.0.2.83",
        )
    assert busy.value.code == "sign_in_busy"
    with sqlite3.connect(gateway.state_path) as connection:
        assert connection.execute(
            "SELECT failed_attempts, locked_until FROM auth_local_credentials"
        ).fetchone() == (0, None)

    monkeypatch.setattr(
        auth_gateway_module,
        "_password_matches",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("source-locked request must not run Argon2")
        ),
    )
    with sqlite3.connect(gateway.state_path) as connection:
        connection.execute(
            """
            INSERT INTO auth_local_source_attempts
                (source_hash, failed_attempts, window_started, locked_until, last_failed_at)
            VALUES (?, 99, ?, ?, ?)
            """,
            (
                gateway._source_key("192.0.2.84"),
                auth_gateway_module.time.time(),
                auth_gateway_module.time.time() + 300,
                auth_gateway_module.time.time(),
            ),
        )
    with pytest.raises(AuthGatewayError, match="Email or password is incorrect"):
        gateway.authenticate_local_password(
            login_email="kdf-capacity@example.invalid",
            password="capacity synthetic passphrase",
            source="192.0.2.84",
        )
