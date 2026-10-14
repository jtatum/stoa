"""Global summaries, legacy pages, and profile counts respect read access."""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Agent, Channel, Comment, Group, Membership, Post, ReadLog

from .conftest import TestSession


@pytest.fixture
async def activity(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Alice belongs to every group; Bob belongs only to the joined group."""
    monkeypatch.setattr("stoa.routes.web.async_session_factory", TestSession)
    agents = {a.agent_email: a for a in (await db.scalars(select(Agent))).all()}
    alice = agents["alice@herd.ai"]
    bob = agents["bob@herd.ai"]
    channels = {"legacy": None}
    groups = {}
    for scope, visibility in (
        ("joined", "private"),
        ("private", "private"),
        ("discoverable", "discoverable"),
        ("public", "public"),
    ):
        group = Group(name=f"{scope} group", visibility=visibility)
        db.add(group)
        await db.flush()
        channel = Channel(group_id=group.id, name="general")
        db.add(channel)
        db.add(Membership(agent_id=alice.id, group_id=group.id))
        if scope == "joined":
            db.add(Membership(agent_id=bob.id, group_id=group.id))
        await db.flush()
        channels[scope] = channel.id
        groups[scope] = group.id

    posts = {}
    for scope, channel_id in channels.items():
        for status in ("open", "closed", "archived", "deleted"):
            name = f"{scope}-{status}"
            post = Post(
                channel_id=channel_id,
                author=alice.agent_email,
                subject=f"Subject {name}",
                tldr=f"Summary {name}",
                body_markdown=f"Body {name}",
                body_html=f"<p>Body {name}</p>",
                token_cost=11,
                status=status,
                # Inaccessible posts sort first, exercising filtering before LIMIT.
                timestamp=datetime(2026, 1, 1) + timedelta(minutes=len(posts)),
                pinned=scope == "private",
                pinned_at=datetime(2026, 1, 2) if scope == "private" else None,
            )
            db.add(post)
            posts[name] = post
    await db.flush()
    db.add(
        Comment(
            post_id=posts["private-open"].id,
            author="confidential-commenter@example.com",
            body_markdown="Confidential comment",
            body_html="<p>Confidential comment</p>",
        )
    )
    await db.commit()
    return {"alice": alice, "bob": bob, "posts": posts, "groups": groups}


def _expected_posts(activity: dict, viewer: str, statuses: tuple[str, ...]) -> list[Post]:
    return [
        post
        for name, post in activity["posts"].items()
        if post.status in statuses
        and (viewer == "alice" or name.startswith(("legacy-", "joined-")))
    ]


@pytest.mark.parametrize("viewer", ["alice", "bob"])
@pytest.mark.parametrize(
    ("url", "statuses"),
    [
        ("/api/posts", ("open", "closed")),
        ("/api/posts?status=archived", ("archived",)),
        ("/api/posts?status=all", ("open", "closed", "archived")),
        ("/api/posts/unread", ("open", "closed")),
    ],
)
async def test_feed_visibility_before_count_and_pagination(
    client: AsyncClient, activity: dict, viewer: str, url: str, statuses: tuple[str, ...]
) -> None:
    expected = _expected_posts(activity, viewer, statuses)
    expected_ids = {post.id for post in expected}
    separator = "&" if "?" in url else "?"
    found = []
    for offset in range(0, len(expected) + 1, 2):
        response = await client.get(
            f"{url}{separator}limit=2&offset={offset}",
            headers={"X-API-Key": f"{viewer}-key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == len(expected)
        assert len(data["posts"]) == min(2, max(0, len(expected) - offset))
        assert all(post["id"] in expected_ids for post in data["posts"])
        found.extend(post["id"] for post in data["posts"])
    assert len(found) == len(expected)
    assert set(found) == expected_ids


@pytest.mark.parametrize("status", ["", "all", "archived"])
async def test_search_cannot_probe_inaccessible_posts(
    client: AsyncClient, activity: dict, status: str
) -> None:
    for keyword in ("Subject private", "Summary discoverable", "Subject public"):
        response = await client.get(
            "/api/posts",
            params={"author": "alice@herd.ai", "keyword": keyword, "status": status},
            headers={"X-API-Key": "bob-key"},
        )
        assert response.status_code == 200
        assert response.json()["posts"] == []
        assert response.json()["total"] == 0


async def test_read_flags_and_unread_totals_only_include_accessible_posts(
    client: AsyncClient, db: AsyncSession, activity: dict
) -> None:
    read_id = activity["posts"]["joined-open"].id
    # A stale read log must not grant access to a private post.
    for name in ("joined-open", "private-open"):
        db.add(
            ReadLog(
                agent_email="bob@herd.ai",
                post_id=activity["posts"][name].id,
                tokens_consumed=11,
            )
        )
    await db.commit()
    headers = {"X-API-Key": "bob-key"}
    response = await client.get("/api/posts", headers=headers)
    posts = response.json()["posts"]
    assert response.json()["total"] == 4
    assert {post["id"] for post in posts if post["read"]} == {read_id}
    response = await client.get("/api/posts/unread", headers=headers)
    assert response.json()["total"] == 3
    assert {post["id"] for post in response.json()["posts"]} == {
        post.id for post in _expected_posts(activity, "bob", ("open", "closed"))
    } - {read_id}
    assert all(not post["read"] for post in response.json()["posts"])


@pytest.mark.parametrize("viewer", ["alice", "bob"])
async def test_api_profile_counts_only_include_visible_activity(
    client: AsyncClient, activity: dict, viewer: str
) -> None:
    headers = {"X-API-Key": f"{viewer}-key"}
    expected_count = len(_expected_posts(activity, viewer, ("open", "closed")))
    response = await client.get("/api/agents", headers=headers)
    assert response.status_code == 200
    alice = next(a for a in response.json()["agents"] if a["agent_email"] == "alice@herd.ai")
    assert alice["post_count"] == expected_count
    response = await client.get(f"/api/agents/{activity['alice'].id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["post_count"] == expected_count


async def test_own_profile_and_dashboard_counts_exclude_hidden_activity(
    client: AsyncClient, db: AsyncSession, activity: dict
) -> None:
    # Authorship alone must not retain access after leaving a group.
    await db.execute(
        delete(Membership).where(
            Membership.agent_id == activity["alice"].id,
            Membership.group_id == activity["groups"]["private"],
        )
    )
    await db.commit()
    headers = {"X-API-Key": "alice-key"}
    response = await client.get("/api/agents/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["post_count"] == 8
    response = await client.patch("/api/agents/me", json={"bio": "Updated"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["post_count"] == 8
    response = await client.get("/api/me/dashboard", headers=headers)
    assert response.status_code == 200
    assert response.json()["identity"]["post_count"] == 8


@pytest.mark.parametrize("viewer", ["alice", "bob"])
async def test_legacy_web_list_hides_inaccessible_and_hidden_posts(
    client: AsyncClient, activity: dict, viewer: str
) -> None:
    client.cookies.set("stoa_session", f"{viewer}-key")
    expected = _expected_posts(activity, viewer, ("open", "closed"))
    response = await client.get("/web/posts", params={"limit": len(expected)})
    assert response.status_code == 200
    assert "Older" not in response.text  # Pagination must use the filtered total.
    for post in activity["posts"].values():
        assert (post.subject in response.text) == (post in expected)
        assert (post.tldr in response.text) == (post in expected)
    # Newer inaccessible posts cannot consume a page of visible results.
    response = await client.get("/web/posts?limit=1")
    latest = max(expected, key=lambda post: post.timestamp)
    assert latest.subject in response.text


@pytest.mark.parametrize("viewer", ["alice", "bob"])
async def test_legacy_web_detail_checks_access_before_rendering_comments(
    client: AsyncClient, activity: dict, viewer: str
) -> None:
    client.cookies.set("stoa_session", f"{viewer}-key")
    for name, post in activity["posts"].items():
        accessible = viewer == "alice" or name.startswith(("legacy-", "joined-"))
        response = await client.get(f"/web/posts/{post.id}")
        if accessible and post.status != "deleted":
            assert response.status_code == 200
            assert post.body_markdown in response.text
            if name == "private-open":
                assert "confidential-commenter@example.com" in response.text
                assert "Confidential comment" in response.text
        else:
            assert response.status_code == 404
            assert post.subject not in response.text
            assert post.body_markdown not in response.text
            assert "confidential-commenter@example.com" not in response.text
            assert "Confidential comment" not in response.text


@pytest.mark.parametrize("viewer", ["alice", "bob"])
async def test_legacy_web_directory_counts_visible_posts_and_hides_private_profiles(
    client: AsyncClient, db: AsyncSession, activity: dict, viewer: str
) -> None:
    activity["bob"].profile_public = False
    activity["bob"].bio = "Private biography"
    await db.commit()
    client.cookies.set("stoa_session", f"{viewer}-key")
    response = await client.get("/web/agents")
    assert response.status_code == 200
    expected_count = len(_expected_posts(activity, viewer, ("open", "closed")))
    assert f"{expected_count} posts" in response.text
    assert "bob@herd.ai" not in response.text
    assert "Private biography" not in response.text


async def test_membership_revocation_takes_effect_in_feeds_and_counts(
    client: AsyncClient, db: AsyncSession, activity: dict
) -> None:
    await db.execute(
        delete(Membership).where(
            Membership.agent_id == activity["bob"].id,
            Membership.group_id == activity["groups"]["joined"],
        )
    )
    await db.commit()
    for url in ("/api/posts", "/api/posts/unread"):
        response = await client.get(url, headers={"X-API-Key": "bob-key"})
        assert response.json()["total"] == 2
        assert {post["id"] for post in response.json()["posts"]} == {
            activity["posts"]["legacy-open"].id,
            activity["posts"]["legacy-closed"].id,
        }
    response = await client.get(
        f"/api/agents/{activity['alice'].id}", headers={"X-API-Key": "bob-key"}
    )
    assert response.json()["post_count"] == 2
    client.cookies.set("stoa_session", "bob-key")
    response = await client.get(f"/web/posts/{activity['posts']['joined-open'].id}")
    assert response.status_code == 404
