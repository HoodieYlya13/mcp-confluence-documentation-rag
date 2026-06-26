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
    assert map_sso_roles_to_role(["ADMIN"]) == "ATS_CORE_LEAD"
    assert map_sso_roles_to_role(["ATS_CORE_LEAD", "ADMIN"]) == "ATS_CORE_LEAD"
    assert map_sso_roles_to_role(["ADMIN_DURNAL"]) == "ATS_CORE_LEAD"
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


def test_settings_list_parsing():
    settings = Settings(
        sso_issuer="https://issuer1.test",
        sso_audience="aud1",
        sso_jwks_url="https://issuer1.test/keys"
    )
    assert settings.sso_issuers == ["https://issuer1.test"]
    assert settings.sso_audiences == ["aud1"]
    assert settings.sso_jwks_urls == ["https://issuer1.test/keys"]

    settings = Settings(
        sso_issuer="[https://issuer1.test, https://issuer2.test]",
        sso_audience="[aud1, aud2]",
        sso_jwks_url="[https://issuer1.test/keys, https://issuer2.test/keys]"
    )
    assert settings.sso_issuers == ["https://issuer1.test", "https://issuer2.test"]
    assert settings.sso_audiences == ["aud1", "aud2"]
    assert settings.sso_jwks_urls == ["https://issuer1.test/keys", "https://issuer2.test/keys"]

    settings = Settings(
        sso_issuer="https://issuer1.test,https://issuer2.test",
        sso_audience="aud1, aud2",
    )
    assert settings.sso_issuers == ["https://issuer1.test", "https://issuer2.test"]
    assert settings.sso_audiences == ["aud1", "aud2"]


def test_multiple_issuers_and_audiences_valid(signing_setup):
    settings = Settings(
        sso_issuer="[https://issuer1.test, https://issuer2.test]",
        sso_audience="[aud1, aud2]"
    )

    token1 = _make_token(signing_setup, iss="https://issuer1.test", aud="aud2")
    assert resolve_role_from_sso_token(token1, settings) == "ATS_CORE_LEAD"

    token2 = _make_token(signing_setup, iss="https://issuer2.test", aud="aud1")
    assert resolve_role_from_sso_token(token2, settings) == "ATS_CORE_LEAD"


def test_multiple_audiences_list_in_token(signing_setup):
    settings = Settings(
        sso_issuer="https://idp.test",
        sso_audience="[aud1, aud2]"
    )

    token = _make_token(signing_setup, aud=["some-other-aud", "aud2"])
    assert resolve_role_from_sso_token(token, settings) == "ATS_CORE_LEAD"


def test_azp_matching_instead_of_aud(signing_setup):
    settings = Settings(
        sso_issuer="https://idp.test",
        sso_audience="[confluence-spotlight-gjOtqPBt, admin-durnal-dev]"
    )

    token = _make_token(
        signing_setup,
        aud=["durnal-resources", "lso-dev"],
        azp="admin-durnal-dev"
    )
    assert resolve_role_from_sso_token(token, settings) == "ATS_CORE_LEAD"


def test_unallowed_issuer_rejected(signing_setup):
    settings = Settings(
        sso_issuer="[https://issuer1.test, https://issuer2.test]",
        sso_audience="aud1"
    )

    token = _make_token(signing_setup, iss="https://unallowed.test", aud="aud1")
    assert resolve_role_from_sso_token(token, settings) is None


def test_unallowed_audience_and_azp_rejected(signing_setup):
    settings = Settings(
        sso_issuer="https://idp.test",
        sso_audience="[aud1, aud2]"
    )

    token = _make_token(signing_setup, aud="aud3", azp="azp3")
    assert resolve_role_from_sso_token(token, settings) is None


KEYCLOAK_ISSUER = "https://miam-keycloak.test/realms/pros"


def test_insecure_issuer_bypasses_signature_verification():
    throwaway = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        sso_issuer=f"[https://auth.hy13dev.com, {KEYCLOAK_ISSUER}]",
        sso_audience="[confluence-spotlight-gjOtqPBt, admin-durnal-dev]",
        sso_insecure_issuer=KEYCLOAK_ISSUER,
    )
    token = _make_token(
        throwaway,
        iss=KEYCLOAK_ISSUER,
        aud=["durnal-resources", "lso-dev"],
        azp="admin-durnal-dev",
        roles=["ADMIN_DURNAL", "ADMIN_GIFTCARDS"],
    )
    assert resolve_role_from_sso_token(token, settings) == "ATS_CORE_LEAD"


def test_insecure_issuer_still_rejects_expired():
    throwaway = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        sso_issuer=KEYCLOAK_ISSUER,
        sso_audience="admin-durnal-dev",
        sso_insecure_issuer=KEYCLOAK_ISSUER,
    )
    now = int(time.time())
    token = _make_token(
        throwaway,
        iss=KEYCLOAK_ISSUER,
        azp="admin-durnal-dev",
        iat=now - 600,
        exp=now - 300,
        roles=["ADMIN_DURNAL"],
    )
    assert resolve_role_from_sso_token(token, settings) is None


def test_secure_issuer_still_requires_valid_signature(signing_setup):
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        sso_issuer="https://idp.test",
        sso_audience="spotlight-client",
        sso_insecure_issuer=KEYCLOAK_ISSUER,
    )
    token = _make_token(wrong_key, roles=["ADMIN"])
    assert resolve_role_from_sso_token(token, settings) is None
