"""Real PostgreSQL readers must serialize with moves before loading comments."""

# Imported pytest fixtures are injected by name into the test function.
# ruff: noqa: F811

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.config import settings
from stoa.models import Comment, Post
from stoa.routes import posts, web

from .test_post_move_concurrency import (  # noqa: F401
    _wait_for_database_lock,
    postgres_client,
    setup_db,
)
from .test_post_moves import ALICE, BOB, CAROL, _group, _invite, _post

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("surface", ["comments", "thread", "post", "public", "web", "human"])
async def test_comment_read_does_not_expose_destination_content(
    setup_db, postgres_client, monkeypatch, admin_headers, surface
):
    client, backend_pids = postgres_client
    source_group, source = await _group(
        client, "Source A", visibility="public" if surface == "public" else "private"
    )
    await _invite(client, source_group, "bob@herd.ai")
    destination_group, destination = await _group(client, "Private B")
    await _invite(client, destination_group, "carol@herd.ai")
    post_id = await _post(client, source)
    if surface == "public":
        pinned = await client.patch(
            f"/api/posts/{post_id}/manage",
            headers={**ALICE, **admin_headers},
            json={"pinned": True},
        )
        assert pinned.status_code == 200, pinned.text
    elif surface == "web":
        monkeypatch.setattr(web, "async_session_factory", setup_db)
        client.cookies.set("stoa_session", "bob-key")
    elif surface == "human":
        from .test_human_ui import _create_verified_human

        async with setup_db() as db:
            await _create_verified_human(db, email="bob@herd.ai")
            await db.commit()
        login = await client.post(
            "/ui/login",
            headers={"Origin": settings.public_base_url},
            data={"email": "bob@herd.ai", "password": "testpass123"},
        )
        assert login.status_code == 303, login.text

    url = {
        "comments": f"/api/posts/{post_id}/comments",
        "thread": f"/api/posts/{post_id}/thread",
        "post": f"/api/posts/{post_id}",
        "public": f"/api/public/posts/{post_id}",
        "web": f"/web/posts/{post_id}",
        "human": f"/ui/posts/{post_id}",
    }[surface]
    reader_headers = BOB if surface in ("comments", "thread", "post") else {}
    before = await client.get(url, headers=reader_headers)
    assert before.status_code == 200, before.text
    if surface not in ("web", "human"):
        assert (before.json() if surface == "comments" else before.json()["comments"]) == []
    async with setup_db() as db:
        assert await db.scalar(text("SHOW transaction_isolation")) == "read committed"

    authorized = asyncio.Event()
    release = asyncio.Event()
    original_execute = AsyncSession.execute

    async def pause_before_comments(db, statement, *args, **kwargs):
        # Each affected reader reaches this await only after its real access gate.
        # Move safety uses EXISTS, and comment creation does not select Comment
        # for a top-level comment, so neither writer matches this scheduling hook.
        columns = getattr(statement, "column_descriptions", ())
        if len(columns) == 1 and columns[0].get("expr") is Comment and not authorized.is_set():
            backend_pids["read"] = await db.scalar(text("SELECT pg_backend_pid()"))
            authorized.set()
            await release.wait()
        return await original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", pause_before_comments)
    pending = asyncio.create_task(
        client.get(url, headers={**reader_headers, "X-Test-Operation": "read"})
    )
    secret = "Carol confidential destination-only content created after the move"
    move_task = None
    try:
        await asyncio.wait_for(authorized.wait(), timeout=5)
        move_task = asyncio.create_task(
            client.patch(
                f"/api/posts/{post_id}/manage",
                headers={**ALICE, "X-Test-Operation": "move"},
                json={"channel_id": destination},
            )
        )
        await _wait_for_database_lock(setup_db, backend_pids, "read", "move", move_task)

        # The move cannot commit while an authorized reader holds the shared
        # post lock. The private comment must be created only after the read.
        release.set()
        response = await asyncio.wait_for(pending, timeout=5)
        assert response.status_code == 200, response.text
        assert secret not in response.text
        moved = await asyncio.wait_for(move_task, timeout=5)
        assert moved.status_code == 200, moved.text
        async with asyncio.timeout(5):
            written = await client.post(
                f"/api/posts/{post_id}/comments",
                headers={**CAROL, "X-Test-Operation": "write"},
                json={"body_markdown": secret},
            )
            assert written.status_code == 201, written.text

        # Check the committed destination state from another session.
        async with setup_db() as db:
            assert await db.scalar(select(Post.channel_id).where(Post.id == post_id)) == destination
            assert (
                await db.scalar(select(Comment.body_markdown).where(Comment.post_id == post_id))
                == secret
            )
        assert backend_pids["read"] != backend_pids["move"]
        assert backend_pids["read"] != backend_pids["write"]
        fresh = await client.get(url, headers=reader_headers)
        assert fresh.status_code == (404 if surface in ("public", "web") else 403), fresh.text
        assert secret not in fresh.text
    finally:
        release.set()
        tasks = [pending]
        if move_task is not None:
            tasks.append(move_task)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)


@pytest.mark.parametrize("surface", ["comments", "public", "web"])
async def test_reader_waiting_on_move_rechecks_audience(
    setup_db, postgres_client, monkeypatch, admin_headers, surface
):
    client, backend_pids = postgres_client
    source_group, source = await _group(
        client, "Source A", visibility="public" if surface == "public" else "private"
    )
    await _invite(client, source_group, "bob@herd.ai")
    destination_group, destination = await _group(client, "Private B")
    await _invite(client, destination_group, "carol@herd.ai")
    post_id = await _post(client, source)
    if surface == "public":
        pinned = await client.patch(
            f"/api/posts/{post_id}/manage",
            headers={**ALICE, **admin_headers},
            json={"pinned": True},
        )
        assert pinned.status_code == 200, pinned.text
    elif surface == "web":

        @asynccontextmanager
        async def web_session():
            async with setup_db() as db:
                backend_pids["read"] = await db.scalar(text("SELECT pg_backend_pid()"))
                yield db

        monkeypatch.setattr(web, "async_session_factory", web_session)
        client.cookies.set("stoa_session", "bob-key")

    url = {
        "comments": f"/api/posts/{post_id}/comments",
        "public": f"/api/public/posts/{post_id}",
        "web": f"/web/posts/{post_id}",
    }[surface]
    headers = BOB if surface == "comments" else {}
    original_gate = posts._require_safe_post_move
    validated = asyncio.Event()
    release = asyncio.Event()

    async def pause_move(db, post, channel):
        await original_gate(db, post, channel)
        validated.set()
        await release.wait()

    monkeypatch.setattr(posts, "_require_safe_post_move", pause_move)
    move_task = asyncio.create_task(
        client.patch(
            f"/api/posts/{post_id}/manage",
            headers={**ALICE, "X-Test-Operation": "move"},
            json={"channel_id": destination},
        )
    )
    reader_task = None
    try:
        await asyncio.wait_for(validated.wait(), timeout=5)
        reader_task = asyncio.create_task(
            client.get(url, headers={**headers, "X-Test-Operation": "read"})
        )
        await _wait_for_database_lock(setup_db, backend_pids, "move", "read", reader_task)
        release.set()
        moved = await asyncio.wait_for(move_task, timeout=5)
        assert moved.status_code == 200, moved.text
        read = await asyncio.wait_for(reader_task, timeout=5)
        assert read.status_code == (403 if surface == "comments" else 404), read.text
        secret = "Destination-only comment after move"
        written = await client.post(
            f"/api/posts/{post_id}/comments",
            headers=CAROL,
            json={"body_markdown": secret},
        )
        assert written.status_code == 201, written.text
        assert secret not in read.text
    finally:
        release.set()
        tasks = [move_task]
        if reader_task is not None:
            tasks.append(reader_task)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
