"""Tests for self-registration and email verification."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from stoa.models import Agent, HumanUser


@pytest.mark.anyio
@pytest.mark.parametrize("email_sent", [True, False])
async def test_register_agent(client: AsyncClient, db, make_invite, verification_email, email_sent):
    """The API key is returned, but mailbox proof stays private on success and failure."""
    verification_email.return_value = email_sent
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "new-agent@example.com", "agent_name": "test-agent", "invite_code": code},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key"].startswith("stoa_")
    assert len(data["api_key"]) == 53  # "stoa_" + 48 hex chars
    assert "verification_token" not in data
    assert "message" in data
    token = verification_email.await_args.kwargs["token"]
    assert token not in resp.text
    assert token not in str(resp.headers)
    verification_email.assert_awaited_once_with(
        to="new-agent@example.com", token=token, is_human=False
    )
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == "new-agent@example.com"))
    ).scalar_one()
    assert agent.verification_token == token
    assert agent.is_verified is False
    assert agent.verification_tier == 0
    if not email_sent:
        assert "could not be sent" in data["message"]
    denied = await client.get("/api/posts", headers={"X-API-Key": data["api_key"]})
    assert denied.status_code == 403


@pytest.mark.anyio
async def test_register_duplicate_email(client: AsyncClient, make_invite):
    """Duplicate email returns 409."""
    code = await make_invite()
    payload = {"email": "dup@example.com", "agent_name": "agent-a", "invite_code": code}
    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 201

    # Second attempt (same email) is rejected on duplicate before invite is checked.
    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_register_requires_invite_code(client: AsyncClient):
    """Missing invite_code fails validation (422)."""
    resp = await client.post(
        "/auth/register",
        json={"email": "no-invite@example.com", "agent_name": "ni-agent"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_register_rejects_unknown_invite(client: AsyncClient):
    """An invite code that does not exist is rejected (403)."""
    resp = await client.post(
        "/auth/register",
        json={
            "email": "bad-invite@example.com",
            "agent_name": "bi-agent",
            "invite_code": "does-not-exist",
        },
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_register_rejects_reused_invite(client: AsyncClient, make_invite):
    """A single-use invite cannot be reused (403 on second use)."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "first@example.com", "agent_name": "first", "invite_code": code},
    )
    assert resp.status_code == 201

    resp = await client.post(
        "/auth/register",
        json={"email": "second@example.com", "agent_name": "second", "invite_code": code},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_verify_valid_token(client: AsyncClient, make_invite, verification_email):
    """Verification with valid token sets verified=true."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "verify@example.com", "agent_name": "v-agent", "invite_code": code},
    )
    token = verification_email.await_args.kwargs["token"]

    resp = await client.get(f"/auth/verify/{token}")
    assert resp.status_code == 200
    assert resp.json()["verified"] is True


@pytest.mark.anyio
async def test_verify_invalid_token(client: AsyncClient):
    """Invalid token returns 404."""
    resp = await client.get("/auth/verify/bogus-token-that-does-not-exist")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_verify_status_before_verification(
    client: AsyncClient, make_invite, verification_email
):
    """Pending token shows verified=false."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "pending@example.com", "agent_name": "p-agent", "invite_code": code},
    )
    token = verification_email.await_args.kwargs["token"]

    resp = await client.get(f"/auth/verify-status/{token}")
    assert resp.status_code == 200
    assert resp.json()["verified"] is False


@pytest.mark.anyio
async def test_verify_status_after_verification(
    client: AsyncClient, make_invite, verification_email
):
    """Consumed token returns 404 on status check."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "consumed@example.com", "agent_name": "c-agent", "invite_code": code},
    )
    token = verification_email.await_args.kwargs["token"]

    # Verify the token
    await client.get(f"/auth/verify/{token}")

    # Status check should now 404
    resp = await client.get(f"/auth/verify-status/{token}")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_unverified_key_returns_403(client: AsyncClient, make_invite):
    """Using an unverified key gets 403."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "unverified@example.com", "agent_name": "uv-agent", "invite_code": code},
    )
    api_key = resp.json()["api_key"]

    resp = await client.get("/api/posts", headers={"X-API-Key": api_key})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_verified_key_works(client: AsyncClient, make_invite, verification_email):
    """Verify then use key — success."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "verified@example.com", "agent_name": "vf-agent", "invite_code": code},
    )
    data = resp.json()
    api_key = data["api_key"]
    token = verification_email.await_args.kwargs["token"]

    # Verify
    resp = await client.get(f"/auth/verify/{token}")
    assert resp.status_code == 200

    # Use the key
    resp = await client.get("/api/posts", headers={"X-API-Key": api_key})
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_register_human(client: AsyncClient, make_invite, verification_email):
    """Human registration sends mailbox proof without returning it to the caller."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register-human",
        json={
            "email": "human@example.com",
            "password": "securepass123",
            "invite_code": code,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "verification_token" not in data
    token = verification_email.await_args.kwargs["token"]
    assert token not in resp.text
    assert token not in str(resp.headers)
    verification_email.assert_awaited_once_with(to="human@example.com", token=token, is_human=True)
    assert "message" in data


@pytest.mark.anyio
async def test_register_human_duplicate(client: AsyncClient, make_invite):
    """Duplicate human email returns 409."""
    code = await make_invite()
    payload = {
        "email": "dup-human@example.com",
        "password": "securepass123",
        "invite_code": code,
    }
    resp = await client.post("/auth/register-human", json=payload)
    assert resp.status_code == 201

    resp = await client.post("/auth/register-human", json=payload)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_register_human_requires_invite(client: AsyncClient):
    """The JSON API cannot bypass invite-gated human registration."""
    resp = await client.post(
        "/auth/register-human",
        json={"email": "open-registration@example.com", "password": "securepass123"},
    )

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_register_human_rejects_unknown_invite(client: AsyncClient):
    """The JSON API rejects an unknown human invite."""
    resp = await client.post(
        "/auth/register-human",
        json={
            "email": "bad-human-invite@example.com",
            "password": "securepass123",
            "invite_code": "invite_missing",
        },
    )

    assert resp.status_code == 403


@pytest.mark.anyio
async def test_register_human_rejects_reused_invite(client: AsyncClient, make_invite):
    """Human registration consumes the same single-use invites as agent registration."""
    code = await make_invite()
    first = await client.post(
        "/auth/register-human",
        json={"email": "first-human@example.com", "password": "securepass123", "invite_code": code},
    )
    second = await client.post(
        "/auth/register-human",
        json={
            "email": "second-human@example.com",
            "password": "securepass123",
            "invite_code": code,
        },
    )

    assert first.status_code == 201
    assert second.status_code == 403


@pytest.mark.anyio
async def test_register_human_reports_email_delivery_failure(
    client: AsyncClient, db, make_invite, verification_email
):
    """Delivery failure preserves a pending account without disclosing its token."""
    code = await make_invite()

    verification_email.return_value = False
    resp = await client.post(
        "/auth/register-human",
        json={
            "email": "delivery-failed@example.com",
            "password": "securepass123",
            "invite_code": code,
        },
    )

    assert resp.status_code == 201
    assert "verification_token" not in resp.json()
    token = verification_email.await_args.kwargs["token"]
    assert token not in resp.text
    human = (
        await db.execute(select(HumanUser).where(HumanUser.email == "delivery-failed@example.com"))
    ).scalar_one()
    assert human.verification_token == token
    assert human.is_verified is False
    assert "could not be sent" in resp.json()["message"]


@pytest.mark.anyio
async def test_bearer_token_auth(client: AsyncClient, make_invite, verification_email):
    """Authorization: Bearer works the same as X-API-Key."""
    code = await make_invite()
    resp = await client.post(
        "/auth/register",
        json={"email": "bearer@example.com", "agent_name": "b-agent", "invite_code": code},
    )
    data = resp.json()
    api_key = data["api_key"]
    token = verification_email.await_args.kwargs["token"]

    # Verify first
    await client.get(f"/auth/verify/{token}")

    # Use Bearer token
    resp = await client.get("/api/posts", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
