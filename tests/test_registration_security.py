"""Mailbox ownership, not possession of an invite, grants human visibility."""

import pytest
from sqlalchemy import select

from stoa.models import Agent, Channel, Group, GroupVisibility, HumanUser, Membership, Post
from stoa.routes import human_ui
from tests.helpers import assert_balanced_html


@pytest.mark.parametrize("registration_path", ["/auth/register-human", "/ui/register"])
@pytest.mark.parametrize("email_sent", [True, False])
async def test_claiming_agent_mailbox_requires_email_proof(
    client, db, make_invite, verification_email, registration_path, email_sent
):
    """An invited caller cannot inherit a victim agent's private group access."""
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
    ).scalar_one()
    group = Group(name="Victim private group", visibility=GroupVisibility.PRIVATE)
    db.add(group)
    await db.flush()
    db.add(Membership(agent_id=agent.id, group_id=group.id, role="member"))
    channel = Channel(name="victim-private-channel", group_id=group.id)
    db.add(channel)
    await db.flush()
    post = Post(
        author=agent.agent_email,
        subject="Victim private subject",
        tldr="Victim private summary",
        body_markdown="Victim private body",
        body_html="<p>Victim private body</p>",
        channel_id=channel.id,
    )
    db.add(post)
    await db.commit()

    verification_email.return_value = email_sent
    credentials = {"email": agent.agent_email, "password": "attacker-password"}
    payload = {**credentials, "invite_code": await make_invite()}
    if registration_path == "/ui/register":
        response = await client.post(
            registration_path, data={**payload, "password_confirm": credentials["password"]}
        )
        assert response.status_code == 200
        assert_balanced_html(response.text)
    else:
        response = await client.post(registration_path, json=payload)
        assert response.status_code == 201

    token = verification_email.await_args.kwargs["token"]
    verification_email.assert_awaited_once_with(to=agent.agent_email, token=token, is_human=True)
    assert token not in response.text
    assert token not in str(response.headers)
    assert "verification_token" not in response.text

    # The retry form's email selector cannot itself verify an account.
    for path in ("/auth/verify", "/ui/verify"):
        invalid = await client.get(f"{path}/{agent.agent_email}")
        assert invalid.status_code == 404
    retry = await client.post("/ui/resend-verification", data={"email": agent.agent_email})
    assert retry.status_code == 200
    assert token not in retry.text
    assert token not in str(retry.headers)
    human = (
        await db.execute(select(HumanUser).where(HumanUser.email == agent.agent_email))
    ).scalar_one()
    assert human.is_verified is False
    assert human.verification_token == token

    login = await client.post("/ui/login", data=credentials)
    assert "Account not verified" in login.text
    private_paths = (
        "/ui/groups",
        f"/ui/groups/{group.id}",
        f"/ui/channels/{channel.id}",
        f"/ui/posts/{post.id}",
    )
    for path in private_paths:
        denied = await client.get(path, follow_redirects=False)
        assert denied.status_code == 303
        assert denied.headers["location"] == "/ui/login"
        assert "Victim private" not in denied.text

    # Simulate the mailbox owner following a successfully delivered link.
    verification_email.return_value = True
    await client.post("/ui/resend-verification", data={"email": agent.agent_email})
    token = verification_email.await_args.kwargs["token"]
    verify_path = "/auth/verify" if registration_path == "/auth/register-human" else "/ui/verify"
    verified = await client.get(f"{verify_path}/{token}", follow_redirects=False)
    assert verified.status_code == (200 if verify_path == "/auth/verify" else 303)
    await db.refresh(human)
    assert human.is_verified is True
    assert human.verification_token is None
    login = await client.post("/ui/login", data=credentials, follow_redirects=False)
    assert login.status_code == 303
    for path in private_paths:
        allowed = await client.get(path)
        assert allowed.status_code == 200
    assert "Victim private body" in allowed.text

    for path in ("/auth/verify", "/ui/verify"):
        replay = await client.get(f"{path}/{token}", follow_redirects=False)
        assert replay.status_code == 404


async def test_resend_does_not_disclose_account_or_delivery_state(client, db, verification_email):
    """Unknown, pending, verified, and failed-delivery requests look the same."""
    email = "resend@example.com"
    unknown = await client.post("/ui/resend-verification", data={"email": email})
    verification_email.assert_not_awaited()

    human = HumanUser(
        email="Resend@Example.com",
        password_hash="unused",
        is_verified=False,
        verification_token="mailbox-only",
    )
    db.add(human)
    await db.commit()
    for delivery_result in (False, True):
        verification_email.return_value = delivery_result
        pending = await client.post("/ui/resend-verification", data={"email": email})
        assert pending.status_code == unknown.status_code == 200
        assert pending.text == unknown.text
        assert_balanced_html(pending.text)
        assert human.verification_token not in pending.text
        verification_email.assert_awaited_with(
            to=human.email, token=human.verification_token, is_human=True
        )
        await db.refresh(human)
        assert human.is_verified is False

    human.is_verified = True
    # A verified account is excluded even if a stale token remains in storage.
    await db.commit()
    human_ui.reset_verification_resend_limiter()
    verification_email.reset_mock()
    verified = await client.post("/ui/resend-verification", data={"email": email})
    assert verified.status_code == 200
    assert verified.text == unknown.text
    verification_email.assert_not_awaited()


async def test_resend_rejects_old_token_selector_and_escapes_invalid_email(
    client, verification_email
):
    old_form = await client.post("/ui/resend-verification", data={"verification_token": "secret"})
    assert old_form.status_code == 422
    invalid = await client.post(
        "/ui/resend-verification", data={"email": '"><script>alert(1)</script>'}
    )
    assert invalid.status_code == 200
    assert_balanced_html(invalid.text)
    assert "<script>" not in invalid.text
    assert "&lt;script&gt;" in invalid.text
    verification_email.assert_not_awaited()


async def test_resend_caps_delivery_per_normalized_mailbox(client, db, verification_email):
    """Changing email case or spacing cannot bypass the retry cap."""
    db.add(
        HumanUser(
            email="limited@example.com",
            password_hash="unused",
            is_verified=False,
            verification_token="private-limited-token",
        )
    )
    await db.commit()

    responses = []
    limit = human_ui._verification_resend_limiter.max_requests
    for index in range(limit + 1):
        email = " LIMITED@example.com " if index % 2 else "limited@example.com"
        response = await client.post("/ui/resend-verification", data={"email": email})
        assert response.status_code == 200
        assert "private-limited-token" not in response.text
        assert "If an unverified account exists" in response.text
        responses.append(response.text)
    assert verification_email.await_count == limit
    assert responses[-1] == responses[-3]
    verification_email.assert_awaited_with(
        to="limited@example.com", token="private-limited-token", is_human=True
    )
