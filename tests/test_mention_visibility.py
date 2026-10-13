"""Mentions must not grant access to their source post or comment."""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from stoa.models import Agent, Comment, Membership, Mention, Post

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


async def _channel(client: AsyncClient, visibility: str = "private") -> tuple[int, int]:
    response = await client.post(
        "/api/groups",
        json={"name": "Mention privacy", "visibility": visibility},
        headers=ALICE,
    )
    assert response.status_code == 201, response.text
    group_id = response.json()["id"]
    response = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE)
    assert response.status_code == 200
    return group_id, response.json()[0]["id"]


async def _seed_mentions(db, channel_id=None, *, status="open", minute=0):
    """Seed existing post/comment mentions, including legacy unscoped posts."""
    bob = (await db.execute(select(Agent).where(Agent.agent_email == "bob@herd.ai"))).scalar_one()
    post = Post(
        author="alice@herd.ai",
        subject=f"Subject {minute}",
        tldr="Mention source",
        body_markdown=f"Post {minute}: @bob@herd.ai " + "p" * 210,
        body_html="<p>Mention source</p>",
        channel_id=channel_id,
        status=status,
    )
    db.add(post)
    await db.flush()
    comment = Comment(
        post_id=post.id,
        author="alice@herd.ai",
        body_markdown=f"Comment {minute}: @bob@herd.ai " + "c" * 210,
        body_html="<p>Comment mention</p>",
    )
    db.add(comment)
    await db.flush()
    mentions = [
        Mention(
            post_id=post.id if index == 0 else None,
            comment_id=comment.id if index == 1 else None,
            mentioned_agent_id=bob.id,
            mentioned_by="alice@herd.ai",
            created_at=datetime(2026, 1, 1) + timedelta(minutes=minute, seconds=index),
        )
        for index in range(2)
    ]
    db.add_all(mentions)
    await db.commit()
    return post, comment, mentions


async def _assert_surfaces(client: AsyncClient, mentions: list[Mention]) -> None:
    expected_ids = [
        m.id for m in sorted(mentions, key=lambda m: (m.created_at, m.id), reverse=True)
    ]
    response = await client.get("/api/mentions/me", headers=BOB)
    assert response.status_code == 200
    assert [m["id"] for m in response.json()] == expected_ids

    response = await client.get("/api/mentions/me/count", headers=BOB)
    assert response.status_code == 200
    assert response.json() == {"count": len(mentions)}

    response = await client.get("/api/me/dashboard?since=2000-01-01T00:00:00Z", headers=BOB)
    assert response.status_code == 200
    dashboard = response.json()["mentions"]
    assert dashboard["unread_mentions_count"] == len(mentions)
    assert [m["id"] for m in dashboard["recent_mentions"]] == expected_ids[:5]


@pytest.mark.parametrize("visibility", ["private", "discoverable", "public"])
@pytest.mark.parametrize("surface", ["feed", "count", "dashboard"])
async def test_mentioning_outsider_does_not_grant_access(client, db, visibility, surface):
    """Exercise real writes: even public group channels require membership."""
    _, channel_id = await _channel(client, visibility)
    response = await client.post(
        "/api/posts",
        json={
            "subject": "Private subject",
            "body_markdown": "Private post snippet for @bob@herd.ai",
            "channel_id": channel_id,
        },
        headers=ALICE,
    )
    assert response.status_code == 201, response.text
    post_id = response.json()["id"]
    response = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"body_markdown": "Private comment snippet for @bob@herd.ai"},
        headers=ALICE,
    )
    assert response.status_code == 201, response.text
    assert len((await db.execute(select(Mention))).scalars().all()) == 2
    assert (await client.get(f"/api/posts/{post_id}", headers=BOB)).status_code == 403

    path = {
        "feed": "/api/mentions/me",
        "count": "/api/mentions/me/count",
        "dashboard": "/api/me/dashboard",
    }[surface]
    response = await client.get(path, headers=BOB)
    assert response.status_code == 200
    if surface == "dashboard":
        assert response.json()["mentions"] == {"unread_mentions_count": 0, "recent_mentions": []}
    else:
        assert response.json() == ([] if surface == "feed" else {"count": 0})


async def test_mentions_follow_current_membership(client, db):
    group_id, channel_id = await _channel(client)
    post, comment, mentions = await _seed_mentions(db, channel_id)
    await _assert_surfaces(client, [])

    membership = Membership(agent_id=mentions[0].mentioned_agent_id, group_id=group_id)
    db.add(membership)
    await db.commit()
    await _assert_surfaces(client, mentions)
    response = await client.get("/api/mentions/me", headers=BOB)
    by_id = {m["id"]: m for m in response.json()}
    assert by_id[mentions[0].id]["post_subject"] == post.subject
    assert by_id[mentions[0].id]["content_snippet"] == post.body_markdown[:200]
    assert by_id[mentions[1].id]["content_snippet"] == comment.body_markdown[:200]
    assert by_id[mentions[1].id]["post_id"] is None
    assert by_id[mentions[1].id]["comment_id"] == comment.id

    await db.delete(membership)
    await db.commit()
    await _assert_surfaces(client, [])


@pytest.mark.parametrize("status", ["open", "closed", "archived", "deleted"])
@pytest.mark.parametrize("scoped", [False, True])
async def test_mentions_respect_source_status_and_legacy_access(client, db, status, scoped):
    channel_id = None
    if scoped:
        group_id, channel_id = await _channel(client)
        bob = (
            await db.execute(select(Agent).where(Agent.agent_email == "bob@herd.ai"))
        ).scalar_one()
        db.add(Membership(agent_id=bob.id, group_id=group_id))
        await db.commit()
    _, _, mentions = await _seed_mentions(db, channel_id, status=status)
    await _assert_surfaces(client, mentions if status in ("open", "closed") else [])


async def test_visibility_applies_before_pagination_and_dashboard_limit(client, db):
    _, private_channel = await _channel(client)
    visible = []
    for minute in range(3):
        _, _, mentions = await _seed_mentions(db, minute=minute)
        visible.extend(mentions)
        # Newer inaccessible mentions must not consume slots in either feed.
        await _seed_mentions(db, private_channel, minute=minute + 10)
    await _assert_surfaces(client, visible)

    expected_ids = [m.id for m in reversed(visible)]
    for offset in range(0, 8, 2):
        response = await client.get(f"/api/mentions/me?limit=2&offset={offset}", headers=BOB)
        assert response.status_code == 200
        assert [m["id"] for m in response.json()] == expected_ids[offset : offset + 2]


async def test_mentions_without_an_unambiguous_source_are_hidden(client, db):
    _, private_channel = await _channel(client)
    public_post, public_comment, visible = await _seed_mentions(db)
    private_post, private_comment, _ = await _seed_mentions(db, private_channel, minute=1)
    for post_id, comment_id in [
        (None, None),
        (public_post.id, private_comment.id),
        (private_post.id, public_comment.id),
    ]:
        db.add(
            Mention(
                post_id=post_id,
                comment_id=comment_id,
                mentioned_agent_id=visible[0].mentioned_agent_id,
                mentioned_by="alice@herd.ai",
            )
        )
    await db.commit()
    await _assert_surfaces(client, visible)


async def test_visible_unread_mentions_keep_dashboard_cursor_semantics(client, db):
    _, private_channel = await _channel(client)
    _, _, old = await _seed_mentions(db)
    _, _, new = await _seed_mentions(db, minute=2)
    await _seed_mentions(db, private_channel, minute=3)
    bob = await db.get(Agent, old[0].mentioned_agent_id)
    bob.last_dashboard_seen_at = datetime(2026, 1, 1, 0, 1)
    await db.commit()
    expected_recent = [m.id for m in reversed(old + new)]

    for suffix, count in [
        ("", 2),
        ("", 2),
        ("?since=2000-01-01T00:00:00Z", 4),
        ("?since=2999-01-01T00:00:00Z", 0),
        ("", 2),
    ]:
        response = await client.get(f"/api/me/dashboard{suffix}", headers=BOB)
        assert response.status_code == 200
        data = response.json()["mentions"]
        assert data["unread_mentions_count"] == count
        assert [m["id"] for m in data["recent_mentions"]] == expected_recent

    response = await client.post("/api/me/dashboard/seen", headers=BOB)
    assert response.status_code == 200
    response = await client.get("/api/me/dashboard", headers=BOB)
    assert response.json()["mentions"]["unread_mentions_count"] == 0
    assert len(response.json()["mentions"]["recent_mentions"]) == 4
