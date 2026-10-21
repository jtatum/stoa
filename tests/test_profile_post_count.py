"""Every caller-profile response uses the same visible authored-post count."""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Agent, Channel, Group, Membership, Post
from stoa.services.auth_sessions import request_challenge


@pytest.mark.parametrize(
    ("method", "url", "profile_key"),
    [
        ("PATCH", "/api/agents/me", None),
        ("GET", "/api/me/dashboard", "identity"),
        ("PATCH", "/api/me/notification-preferences", None),
        ("POST", "/api/auth/verify", "agent"),
    ],
)
async def test_caller_profile_counts_agree_after_losing_group_access(
    client: AsyncClient,
    db: AsyncSession,
    method: str,
    url: str,
    profile_key: str | None,
) -> None:
    alice = (await db.scalars(select(Agent).where(Agent.agent_email == "alice@herd.ai"))).one()
    channels = []
    groups = []
    for name in ("Current group", "Former group"):
        group = Group(name=name, visibility="private")
        db.add(group)
        await db.flush()
        channel = Channel(name="general", group_id=group.id)
        db.add_all([channel, Membership(agent_id=alice.id, group_id=group.id)])
        await db.flush()
        channels.append(channel)
        groups.append(group)

    for channel, status in (
        (channels[0], "open"),
        (channels[0], "deleted"),
        (channels[1], "open"),
    ):
        db.add(
            Post(
                author=alice.agent_email,
                channel_id=channel.id,
                status=status,
                subject="Profile count regression",
                tldr="Summary",
                body_markdown="Body",
                body_html="<p>Body</p>",
            )
        )
    await db.flush()
    await db.execute(
        delete(Membership).where(
            Membership.agent_id == alice.id, Membership.group_id == groups[1].id
        )
    )
    body = None
    if url == "/api/auth/verify":
        _, code = await request_challenge(db, alice, purpose="mint", ttl_seconds=600)
        body = {"agent_id": alice.id, "code": code}
    elif url == "/api/agents/me":
        body = {"bio": "Updated"}
    elif url == "/api/me/notification-preferences":
        body = {"notification_scope": "off"}
    await db.commit()

    headers = {"X-API-Key": "alice-key"}
    own_profile = await client.get("/api/agents/me", headers=headers)
    assert own_profile.status_code == 200
    assert own_profile.json()["post_count"] == 1

    response = await client.request(
        method, url, headers={} if url == "/api/auth/verify" else headers, json=body
    )
    assert response.status_code == 200, response.text
    profile = response.json()[profile_key] if profile_key else response.json()
    assert profile["post_count"] == own_profile.json()["post_count"]
