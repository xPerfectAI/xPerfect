"""Hosted role contract: the admission record owns a person's role.

Without an identity-provider role map, a verified OIDC sign-in proves identity
only; it must never raise or lower the role an administrator admitted. With a
configured role map, the provider's claims are authoritative.
"""
from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

import glass_drive_ui.auth_gateway as auth
from glass_drive_ui.auth_gateway import HumanAuthGateway

ISSUER = "https://identity.example.invalid"


class _Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def _gateway(tmp_path, monkeypatch, *, role_map=None):
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv("GLASSHIVE_OIDC_REDIRECT_URI", "https://xperfect.example.invalid/auth/oidc/callback")
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "false")
    monkeypatch.setenv("GLASSHIVE_LOCAL_AUTH_THROTTLE_KEY", "synthetic-throttle-key-for-tests-12345")
    if role_map is not None:
        monkeypatch.setenv("GLASSHIVE_OIDC_ROLE_MAP_JSON", json.dumps(role_map))
    return HumanAuthGateway.from_env()


def _sign_in(gateway, monkeypatch, subject, **claims):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="synthetic-idp", alg="RS256", use="sig")

    def get(url, **kwargs):
        if url.endswith("/jwks"):
            return _Response({"keys": [jwk]})
        return _Response({"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                          "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks"})

    monkeypatch.setattr(auth.httpx, "get", get)
    flow = gateway.begin_oidc(return_to="/")
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "aud": gateway.oidc_client_id, "sub": subject, "nonce": flow["nonce"],
                        "iat": now, "exp": now + 120, **claims},
                       key, algorithm="RS256", headers={"kid": "synthetic-idp"})
    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: _Response({"id_token": token}))
    return gateway.complete_oidc(state=flow["state"], code="synthetic-code")["principal"]


@pytest.mark.parametrize("role", ["viewer", "tenant_admin", "member"])
def test_sign_in_keeps_the_admitted_role(tmp_path, monkeypatch, role):
    gateway = _gateway(tmp_path, monkeypatch)
    gateway.preapprove_oidc_principal(subject="stable-subject", role=role)
    assert _sign_in(gateway, monkeypatch, "stable-subject")["role"] == role
    # A provider claim cannot raise it either.
    assert _sign_in(gateway, monkeypatch, "stable-subject", roles=["tenant_admin"])["role"] == role
    stored = gateway.find_oidc_principal(issuer=ISSUER, subject="stable-subject")
    assert stored["role"] == role


def test_an_unadmitted_person_still_cannot_sign_in(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path, monkeypatch)
    with pytest.raises(auth.AuthGatewayError) as refused:
        _sign_in(gateway, monkeypatch, "never-admitted")
    assert refused.value.code == "account_not_registered"


def test_a_configured_role_map_makes_provider_claims_authoritative(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path, monkeypatch, role_map={"Readers": "viewer", "Members": "member"})
    gateway.preapprove_oidc_principal(subject="stable-subject", role="member")
    assert _sign_in(gateway, monkeypatch, "stable-subject", roles=["Readers"])["role"] == "viewer"
