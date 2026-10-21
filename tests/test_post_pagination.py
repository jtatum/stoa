"""Post listings must make partial results and continuation explicit (issue #187)."""

from datetime import datetime

import pytest
from httpx import AsyncClient

from stoa.main import app
from stoa.models import Post, ReadLog

from .conftest import TestSession, provision_channel

ALICE_HEADERS = {"X-API-Key": "alice-key"}
LIST_PATHS = ["/api/posts", "/api/posts/unread"]


@pytest.fixture
async def post_ids(client: AsyncClient) -> list[int]:
    """Exceed both page limits, including ties in pin time and post time."""
    channel_id = await provision_channel(client, ALICE_HEADERS)
    timestamp = datetime(2026, 10, 1)
    async with TestSession() as db:
        posts = [
            Post(
                author="alice@herd.ai",
                subject=f"Post {i}",
                tldr=f"Summary {i}",
                body_markdown=f"Body {i}",
                body_html=f"<p>Body {i}</p>",
                channel_id=channel_id,
                timestamp=timestamp,
                pinned=i < 2,
                pinned_at=timestamp if i < 2 else None,
            )
            for i in range(113)
        ]
        db.add_all(posts)
        await db.commit()
        # Pins first, with descending IDs breaking otherwise identical sort keys.
        return [posts[1].id, posts[0].id, *(post.id for post in reversed(posts[2:]))]


@pytest.mark.parametrize("path", LIST_PATHS)
@pytest.mark.parametrize("limit", [None, 100])
async def test_follow_continuation_past_default_and_max_page_sizes(
    client: AsyncClient, post_ids: list[int], path: str, limit: int | None
) -> None:
    params = {} if limit is None else {"limit": limit}
    seen = []
    for offset in range(0, len(post_ids), limit or 50):
        response = await client.get(path, params=params, headers=ALICE_HEADERS)
        assert response.status_code == 200, response.text
        page = response.json()
        ids = [post["id"] for post in page["posts"]]
        assert ids == post_ids[offset : offset + (limit or 50)]
        assert page["total"] == len(post_ids)
        assert page["offset"] == offset
        assert page["limit"] == (limit or 50)
        seen.extend(ids)
        has_more = len(seen) < len(post_ids)
        assert page["has_more"] is has_more
        assert page["next_offset"] == (len(seen) if has_more else None)
        if page["next_offset"] is None:
            break
        params["offset"] = page["next_offset"]

    assert seen == post_ids
    assert len(set(seen)) == len(post_ids)


@pytest.mark.parametrize("path", LIST_PATHS)
async def test_empty_listing_has_no_continuation(client: AsyncClient, path: str) -> None:
    response = await client.get(path, headers=ALICE_HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "posts": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
        "has_more": False,
        "next_offset": None,
    }


@pytest.mark.parametrize("path", LIST_PATHS)
@pytest.mark.parametrize("offset", [13, 113, 120])
async def test_full_final_page_and_offsets_at_or_beyond_end_have_no_continuation(
    client: AsyncClient, post_ids: list[int], path: str, offset: int
) -> None:
    response = await client.get(
        path, params={"limit": 100, "offset": offset}, headers=ALICE_HEADERS
    )
    assert response.status_code == 200
    page = response.json()
    assert [post["id"] for post in page["posts"]] == post_ids[offset:]
    assert page["total"] == len(post_ids)
    assert page["has_more"] is False
    assert page["next_offset"] is None


async def test_continuation_respects_filters(client: AsyncClient, post_ids: list[int]) -> None:
    async with TestSession() as db:
        for post_id in post_ids[:3]:
            post = await db.get(Post, post_id)
            post.author = "bob@herd.ai"
            post.subject = "Matching post"
            post.status = "archived"
        await db.commit()

    params = {"author": "bob@herd.ai", "keyword": "Matching", "status": "archived", "limit": 2}
    first = (await client.get("/api/posts", params=params, headers=ALICE_HEADERS)).json()
    assert first["total"] == 3
    assert [post["id"] for post in first["posts"]] == post_ids[:2]
    assert first["has_more"] is True
    assert first["next_offset"] == 2

    last = (
        await client.get(
            "/api/posts", params={**params, "offset": first["next_offset"]}, headers=ALICE_HEADERS
        )
    ).json()
    assert last["total"] == 3
    assert [post["id"] for post in last["posts"]] == post_ids[2:3]
    assert last["has_more"] is False
    assert last["next_offset"] is None


async def test_unread_continuation_excludes_read_posts(
    client: AsyncClient, post_ids: list[int]
) -> None:
    async with TestSession() as db:
        db.add_all(
            ReadLog(post_id=post_id, agent_email="alice@herd.ai", tokens_consumed=0)
            for post_id in post_ids[3:]
        )
        await db.commit()

    first = (
        await client.get("/api/posts/unread", params={"limit": 2}, headers=ALICE_HEADERS)
    ).json()
    assert first["total"] == 3
    assert first["next_offset"] == 2
    last = (
        await client.get(
            "/api/posts/unread",
            params={"limit": 2, "offset": first["next_offset"]},
            headers=ALICE_HEADERS,
        )
    ).json()
    assert [post["id"] for post in last["posts"]] == post_ids[2:3]
    assert last["has_more"] is False
    assert last["next_offset"] is None


@pytest.mark.parametrize("path", LIST_PATHS)
@pytest.mark.parametrize(
    "query", ["per_page=100", "per_page=", "per_page=abc", "limit=5&per_page=5"]
)
async def test_per_page_is_rejected_with_actionable_error(
    client: AsyncClient, path: str, query: str
) -> None:
    response = await client.get(f"{path}?{query}", headers=ALICE_HEADERS)
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["query", "per_page"]
    assert "limit" in error["msg"]
    assert "offset" in error["msg"]


@pytest.mark.parametrize("path", LIST_PATHS)
@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
async def test_pagination_bounds(client: AsyncClient, path: str, query: str) -> None:
    response = await client.get(f"{path}?{query}", headers=ALICE_HEADERS)
    assert response.status_code == 422


def test_openapi_documents_pagination_contract() -> None:
    schema = app.openapi()
    for path in LIST_PATHS:
        endpoint = schema["paths"][path]["get"]
        params = {param["name"]: param for param in endpoint["parameters"]}
        assert params["limit"]["schema"]["default"] == 50
        assert params["limit"]["schema"]["maximum"] == 100
        assert params["offset"]["schema"]["default"] == 0
        assert "next_offset" in endpoint["description"]
    fields = schema["components"]["schemas"]["PaginatedPosts"]["properties"]
    assert fields["has_more"]["type"] == "boolean"
    assert {field["type"] for field in fields["next_offset"]["anyOf"]} == {"integer", "null"}
