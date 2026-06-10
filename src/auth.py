import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from src.config import KNOWN_ROLES
from src.settings import Settings, get_settings

logger = logging.getLogger("auth")

_current_role: ContextVar[str | None] = ContextVar("current_role", default=None)


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
