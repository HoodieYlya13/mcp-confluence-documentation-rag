import pytest
from src.auth import (
    AuthenticationError,
    current_role,
    resolve_role_from_token,
    role_context,
)


def test_known_token_resolves_to_role():
    assert resolve_role_from_token("test-junior-token") == "JUNIOR_OP"
    assert resolve_role_from_token("test-lead-token") == "ATS_CORE_LEAD"


def test_unknown_token_resolves_to_none():
    assert resolve_role_from_token("invented-token") is None
    assert resolve_role_from_token("") is None


def test_token_mapped_to_unknown_role_fails_closed():
    assert resolve_role_from_token("bad-role-token") is None


def test_current_role_requires_authentication():
    with pytest.raises(AuthenticationError):
        current_role()


def test_role_context_sets_and_resets():
    with role_context("JUNIOR_OP"):
        assert current_role() == "JUNIOR_OP"
        with role_context("ATS_CORE_LEAD"):
            assert current_role() == "ATS_CORE_LEAD"
        assert current_role() == "JUNIOR_OP"
    with pytest.raises(AuthenticationError):
        current_role()


@pytest.fixture()
def http_client():
    from src.server import BearerTokenAuthMiddleware
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def whoami(request):
        return JSONResponse({"role": current_role()})

    async def health(request):
        return JSONResponse({"status": "ok"})

    from starlette.testclient import TestClient

    inner = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/whoami", whoami, methods=["GET"]),
    ])
    app = BearerTokenAuthMiddleware(inner)
    return TestClient(app)


def test_health_is_public(http_client):
    response = http_client.get("/health")
    assert response.status_code == 200


def test_request_without_token_rejected(http_client):
    response = http_client.get("/whoami")
    assert response.status_code == 401


def test_request_with_invalid_token_rejected(http_client):
    response = http_client.get("/whoami", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_request_with_valid_token_resolves_role(http_client):
    response = http_client.get(
        "/whoami", headers={"Authorization": "Bearer test-lead-token"}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "ATS_CORE_LEAD"


@pytest.fixture()
def server_app_client():
    from src.server import build_http_app
    from starlette.testclient import TestClient

    return TestClient(build_http_app())


def test_admin_sync_requires_token(server_app_client):
    response = server_app_client.post("/admin/sync")
    assert response.status_code == 401


def test_admin_sync_rejects_junior(server_app_client):
    response = server_app_client.post(
        "/admin/sync", headers={"Authorization": "Bearer test-junior-token"}
    )
    assert response.status_code == 403


def test_admin_sync_allows_lead(server_app_client):
    response = server_app_client.post(
        "/admin/sync", headers={"Authorization": "Bearer test-lead-token"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "synced"
    assert payload["indexed_documents"] == 3
