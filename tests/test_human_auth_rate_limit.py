"""Human authentication must consume the shared requester budget before work."""

from unittest.mock import patch

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import AuditLog, HumanUser
from stoa.rate_limit import RateLimiter
from stoa.routes import human_ui
from tests.conftest import TestSession

HUMAN_AUTH_PATHS = (
    "/ui/login",
    "/ui/register",
    "/auth/register-human",
    "/ui/resend-verification",
)


@pytest.mark.parametrize("path", HUMAN_AUTH_PATHS)
async def test_rotating_human_identities_stop_before_db_bcrypt_and_email(
    client: AsyncClient, db: AsyncSession, verification_email, path: str
) -> None:
    password_hash = bcrypt.hashpw(b"correct-password", bcrypt.gensalt(rounds=4)).decode()
    for i in range(4):
        db.add(
            HumanUser(
                email=f"human-{i}@example.com",
                password_hash=password_hash,
                is_verified=False,
                verification_token=f"verification-{i}",
            )
        )
    await db.commit()

    limiter = RateLimiter(max_requests=2, window_seconds=60)
    with (
        patch("stoa.rate_limit._requester_limiter", limiter),
        patch("tests.conftest.TestSession", wraps=TestSession) as sessions,
        patch("bcrypt.checkpw", wraps=bcrypt.checkpw) as checked,
    ):
        statuses = []
        for i in range(4):
            registration = path in ("/ui/register", "/auth/register-human")
            payload = {
                "email": f"{'new' if registration else 'human'}-{i}@example.com",
                "password": f"wrong-password-{i}",
                "password_confirm": f"wrong-password-{i}",
                "invite_code": f"invalid-invite-{i}",
            }
            if path == "/auth/register-human":
                response = await client.post(path, json=payload)
            else:
                response = await client.post(path, data=payload)
            statuses.append(response.status_code)

        expected = 403 if path == "/auth/register-human" else 200
        assert statuses == [expected, expected, 429, 429]
        assert sessions.call_count == 2
        assert checked.call_count == (2 if path == "/ui/login" else 0)
        assert verification_email.await_count == (2 if path == "/ui/resend-verification" else 0)
        assert len(limiter._requests) == 1
        assert response.headers["X-RequesterRateLimit-Remaining"] == "0"
        assert response.headers["X-RateLimit-Limit"] == "2"
        assert 0 < int(response.headers["Retry-After"]) <= 60

    if path == "/ui/login":
        failures = await db.scalars(
            select(AuditLog).where(AuditLog.event_type == "human_login_failed")
        )
        assert len(failures.all()) == 2
    if path == "/ui/resend-verification":
        assert len(human_ui._verification_resend_limiter._requests) == 2


@pytest.mark.parametrize("path", HUMAN_AUTH_PATHS)
async def test_invalid_human_auth_bodies_are_charged_before_parsing(
    client: AsyncClient, path: str
) -> None:
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    with patch("stoa.rate_limit._requester_limiter", limiter):
        # Even missing required fields consume the requester's allowance.
        response = await client.post(path, json={} if path.startswith("/auth/") else None)
        assert response.status_code == 422

        with (
            patch("starlette.requests.Request._get_form", side_effect=ValueError) as form,
            patch("starlette.requests.Request.json", side_effect=ValueError) as json_body,
            patch("tests.conftest.TestSession", wraps=TestSession) as sessions,
        ):
            response = await client.post(
                path,
                content=b"malformed body",
                headers={
                    "Content-Type": "application/json"
                    if path.startswith("/auth/")
                    else "application/x-www-form-urlencoded"
                },
            )
            assert response.status_code == 429
            form.assert_not_called()
            json_body.assert_not_called()
            sessions.assert_not_called()


@pytest.mark.parametrize("human_first", [True, False])
async def test_human_routes_and_api_share_budget_but_other_requesters_can_proceed(
    client: AsyncClient, human_first: bool
) -> None:
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    with patch("stoa.rate_limit._requester_limiter", limiter):
        if human_first:
            assert (await client.post("/ui/login")).status_code == 422
            assert (await client.get("/api/posts")).status_code == 429
        else:
            assert (await client.get("/api/posts")).status_code == 401

        for path in HUMAN_AUTH_PATHS:
            assert (await client.post(path)).status_code == 429

        response = await client.post("/ui/login", headers={"Fly-Client-IP": "203.0.113.9"})
        assert response.status_code == 422
        assert response.headers["X-RequesterRateLimit-Remaining"] == "0"


@pytest.mark.parametrize("path", HUMAN_AUTH_PATHS)
async def test_credential_header_rotation_cannot_reset_human_requester_budget(
    client: AsyncClient, admin_headers: dict, path: str
) -> None:
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    credentials = RateLimiter(max_requests=10, window_seconds=60)
    with (
        patch("stoa.rate_limit._requester_limiter", limiter),
        patch("stoa.rate_limit._limiter", credentials),
    ):
        assert (await client.post(path)).status_code == 422
        for headers in (
            {"X-API-Key": "rotating-key"},
            {"Authorization": "Bearer rotating-token"},
            {"X-Admin-Key": "invalid-admin"},
            admin_headers,
        ):
            assert (await client.post(path, headers=headers)).status_code == 429
        assert not credentials._requests


@pytest.mark.parametrize("path", ["/ui/register", "/auth/register-human"])
async def test_successful_human_flow_charges_once_per_post_and_preserves_browsing(
    client: AsyncClient, make_invite, verification_email, path: str
) -> None:
    payload = {
        "email": "flow@example.com",
        "password": "correct-password",
        "password_confirm": "correct-password",
        "invite_code": await make_invite(),
    }
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    with patch("stoa.rate_limit._requester_limiter", limiter):
        if path == "/auth/register-human":
            response = await client.post(path, json=payload)
            assert response.status_code == 201
        else:
            response = await client.post(path, data=payload)
            assert response.status_code == 200
        assert response.headers["X-RequesterRateLimit-Remaining"] == "2"

        response = await client.post("/ui/resend-verification", data={"email": payload["email"]})
        assert response.status_code == 200
        assert response.headers["X-RequesterRateLimit-Remaining"] == "1"
        token = verification_email.await_args.kwargs["token"]
        assert (await client.get(f"/ui/verify/{token}")).status_code == 303

        response = await client.post("/ui/login", data=payload)
        assert response.status_code == 303
        assert response.headers["X-RequesterRateLimit-Remaining"] == "0"
        assert (await client.post("/ui/login", data=payload)).status_code == 429

        for page in ("/ui/login", "/ui/register", "/ui/groups", "/ui/agents", "/health"):
            response = await client.get(page)
            assert response.status_code == 200
            assert "X-RequesterRateLimit-Limit" not in response.headers


@pytest.mark.parametrize(
    "path", ["/ui/login-extra", "/ui/register/extra", "/auth/register-humanity"]
)
async def test_unrelated_paths_do_not_consume_human_auth_budget(
    client: AsyncClient, path: str
) -> None:
    limiter = RateLimiter(max_requests=0, window_seconds=60)
    with patch("stoa.rate_limit._requester_limiter", limiter):
        response = await client.post(path)
        assert response.status_code == 404
        assert "X-RequesterRateLimit-Limit" not in response.headers
