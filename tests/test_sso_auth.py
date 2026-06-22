import time
import types

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from src.auth import map_sso_roles_to_role, resolve_role_from_sso_token
from src.settings import Settings

ISSUER = "https://idp.test"
AUDIENCE = "spotlight-client"


def test_map_sso_roles():
    assert map_sso_roles_to_role(["JUNIOR_OP"]) == "JUNIOR_OP"
    assert map_sso_roles_to_role(["ATS_CORE_LEAD"]) == "ATS_CORE_LEAD"
    # ADMIN is treated as ATS_CORE_LEAD.
    assert map_sso_roles_to_role(["ADMIN"]) == "ATS_CORE_LEAD"
    assert map_sso_roles_to_role(["ATS_CORE_LEAD", "ADMIN"]) == "ATS_CORE_LEAD"
    # No recognized role fails closed.
    assert map_sso_roles_to_role(["SOMETHING_ELSE"]) is None
    assert map_sso_roles_to_role([]) is None


def test_sso_disabled_returns_none():
    settings = Settings(sso_issuer="", sso_audience="")
    assert resolve_role_from_sso_token("any.jwt.here", settings) is None


@pytest.fixture()
def signing_setup(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def fake_client(_url):
        return types.SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: types.SimpleNamespace(
                key=key.public_key()
            )
        )

    monkeypatch.setattr("src.auth._get_jwks_client", fake_client)
    return key


def _make_token(key, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
        "roles": ["ATS_CORE_LEAD"],
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def test_valid_sso_token_resolves_role(signing_setup):
    token = _make_token(signing_setup, roles=["ATS_CORE_LEAD"])
    settings = Settings(sso_issuer=ISSUER, sso_audience=AUDIENCE)
    assert resolve_role_from_sso_token(token, settings) == "ATS_CORE_LEAD"


def test_admin_role_maps_to_lead(signing_setup):
    token = _make_token(signing_setup, roles=["ADMIN"])
    settings = Settings(sso_issuer=ISSUER, sso_audience=AUDIENCE)
    assert resolve_role_from_sso_token(token, settings) == "ATS_CORE_LEAD"


def test_token_without_known_role_denied(signing_setup):
    token = _make_token(signing_setup, roles=["GUEST"])
    settings = Settings(sso_issuer=ISSUER, sso_audience=AUDIENCE)
    assert resolve_role_from_sso_token(token, settings) is None


def test_wrong_audience_rejected(signing_setup):
    token = _make_token(signing_setup, aud="some-other-app")
    settings = Settings(sso_issuer=ISSUER, sso_audience=AUDIENCE)
    assert resolve_role_from_sso_token(token, settings) is None


def test_expired_token_rejected(signing_setup):
    now = int(time.time())
    token = _make_token(signing_setup, iat=now - 600, exp=now - 300)
    settings = Settings(sso_issuer=ISSUER, sso_audience=AUDIENCE)
    assert resolve_role_from_sso_token(token, settings) is None
