import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

import jwt
from jwt import PyJWKClient

from src.config import KNOWN_ROLES, SecurityRoles
from src.settings import Settings, get_settings

logger = logging.getLogger("auth")

_current_role: ContextVar[str | None] = ContextVar("current_role", default=None)

# Cache one JWKS client per endpoint; it refreshes signing keys internally.
_jwks_clients: dict[str, PyJWKClient] = {}


class AuthenticationError(Exception):
    pass


def resolve_role_from_token(token: str, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    role = settings.auth_tokens.get(token)
    if role is None:
        return None
    if role not in KNOWN_ROLES:
        logger.error(
            "Token registry maps to an unknown role. Failing closed.",
            extra={"mapped_role": role, "security_violation": True},
        )
        return None
    return role


def map_sso_roles_to_role(roles: list[str]) -> str | None:
    """Maps an SSO access token's per-app roles onto an MCP clearance.

    ADMIN and ADMIN_DURNAL are treated as ATS_CORE_LEAD. A token carrying no
    recognized role is denied (returns None) — there is no implicit floor.
    """
    if (
        SecurityRoles.ATS_CORE_LEAD in roles
        or "ADMIN" in roles
        or "ADMIN_DURNAL" in roles
    ):
        return SecurityRoles.ATS_CORE_LEAD
    if SecurityRoles.JUNIOR_OP in roles:
        return SecurityRoles.JUNIOR_OP
    return None


def _get_jwks_client(url: str) -> PyJWKClient:
    client = _jwks_clients.get(url)
    if client is None:
        client = PyJWKClient(url, cache_keys=True)
        _jwks_clients[url] = client
    return client


def resolve_role_from_sso_token(token: str, settings: Settings | None = None) -> str | None:
    """Verifies an OIDC access token from the identity provider and resolves its
    MCP clearance from the ``roles`` claim.

    Disabled (returns None) unless both SSO_ISSUER and SSO_AUDIENCE are set, so
    deployments that rely only on AUTH_TOKENS are unaffected. Any verification
    failure fails closed.
    """
    settings = settings or get_settings()
    if not settings.sso_issuer or not settings.sso_audience:
        return None

    jwks_url = (
        settings.sso_jwks_url
        or settings.sso_issuer.rstrip("/") + "/.well-known/jwks.json"
    )
    try:
        signing_key = _get_jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.sso_audience,
            issuer=settings.sso_issuer,
            options={"require": ["exp", "iat"]},
        )
    except Exception as exc:  # noqa: BLE001 — fail closed on any verification error
        logger.warning("SSO token verification failed.", extra={"error": str(exc)})
        return None

    raw_roles = claims.get("roles", [])
    roles = (
        [r for r in raw_roles if isinstance(r, str)]
        if isinstance(raw_roles, list)
        else []
    )
    role = map_sso_roles_to_role(roles)
    if role is None:
        logger.warning(
            "SSO token carried no authorized role. Failing closed.",
            extra={"roles": roles, "security_violation": True},
        )
    return role


def current_role() -> str:
    role = _current_role.get()
    if role is None:
        stdio_role = get_settings().stdio_role
        role = stdio_role if stdio_role else None
    if role is None:
        raise AuthenticationError(
            "No authenticated identity for this request. Supply a bearer token "
            "(HTTP) or configure STDIO_ROLE (local stdio)."
        )
    if role not in KNOWN_ROLES:
        raise AuthenticationError(f"Authenticated role '{role}' is not a known role.")
    return role


@contextmanager
def role_context(role: str) -> Iterator[None]:
    reset_token = _current_role.set(role)
    try:
        yield
    finally:
        _current_role.reset(reset_token)
