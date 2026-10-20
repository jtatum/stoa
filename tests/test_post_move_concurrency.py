"""Real PostgreSQL transactions must serialize moves with discussion growth."""

import asyncio
import os
from uuid import uuid4

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from stoa.database import Base, get_db
from stoa.main import app
from stoa.models import Comment, Post
from stoa.routes import comments, messages, posts
from stoa.services.post_locking import lock_post

from .helpers import create_test_api_key
from .test_post_moves import ALICE, BOB, CAROL, _comment, _group, _invite, _post

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
async def setup_db():
    """Override the shared SQLite fixture with a disposable PostgreSQL schema.

    Never fall back to the application's DATABASE_URL. Each request gets its
    own connection/transaction; no connection or outer rollback is shared.
    """
    database_url = os.environ.get("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("Set TEST_POSTGRES_URL to run PostgreSQL concurrency regressions")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRES_URL must point to PostgreSQL")
    url = url.set(drivername="postgresql+asyncpg")
    schema = f"stoa_move_test_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "8000",
            }
        },
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            for name in ("alice", "bob", "carol"):
                await create_test_api_key(db, f"{name}@herd.ai", f"{name}-key", verification_tier=2)
        yield sessions
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.fixture
async def postgres_client(setup_db):
    backend_pids = {}

    async def request_db(request: Request):
        async with setup_db.begin() as db:
            operation = request.headers.get("X-Test-Operation")
            if operation:
                backend_pids[operation] = await db.scalar(text("SELECT pg_backend_pid()"))
            yield db

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = request_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, backend_pids
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


async def _wait_for_database_lock(sessions, backend_pids, first, second, waiting_task):
    """Observe the actual blocker from a third connection, not a timing guess."""
    async with asyncio.timeout(5), sessions() as observer:
        while True:
            assert not waiting_task.done(), "Concurrent request escaped the post lock"
            if first in backend_pids and second in backend_pids:
                assert backend_pids[first] != backend_pids[second]
                blocked = await observer.scalar(
                    text("SELECT :blocker = ANY(pg_blocking_pids(:waiter))"),
                    {"blocker": backend_pids[first], "waiter": backend_pids[second]},
                )
                if blocked:
                    return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("write_kind", ["comment", "nested_comment", "post_reply", "channel_reply"])
@pytest.mark.parametrize("first", ["write", "move"])
async def test_move_serializes_with_discussion_growth(
    setup_db, postgres_client, monkeypatch, write_kind, first
):
    client, backend_pids = postgres_client
    source_group, source = await _group(client, "Private A")
    await _invite(client, source_group, "bob@herd.ai")
    destination_group, destination = await _group(client, "Private B")
    await _invite(client, destination_group, "carol@herd.ai")
    post_id = await _post(client, source)
    parent_comment_id = (
        await _comment(client, post_id, ALICE) if write_kind == "nested_comment" else None
    )

    # Pause the first request after validation but before insertion/mutation.
    # Only synchronization is patched; queries, locks, auth, and commits are real.
    validated = asyncio.Event()
    release = asyncio.Event()
    if first == "move":
        route = posts
        gate_name = "_require_safe_post_move"
    else:
        route = {"post_reply": posts, "channel_reply": messages}.get(write_kind, comments)
        gate_name = "enforce_soft_close_acknowledgement"
    original_gate = getattr(route, gate_name)

    async def pause_after_validation(*args, **kwargs):
        result = await original_gate(*args, **kwargs)
        validated.set()
        await release.wait()
        return result

    monkeypatch.setattr(route, gate_name, pause_after_validation)

    async def write():
        headers = {**BOB, "X-Test-Operation": "write"}
        body = {"body_markdown": "Bob's confidential contribution for group A"}
        if write_kind in ("comment", "nested_comment"):
            return await client.post(
                f"/api/posts/{post_id}/comments",
                headers=headers,
                json={**body, "in_reply_to": parent_comment_id},
            )
        if write_kind == "post_reply":
            return await client.post(
                "/api/posts",
                headers=headers,
                json={**body, "subject": "Private reply", "parent_post_id": post_id},
            )
        return await client.post(
            f"/api/channels/{source}/messages",
            headers=headers,
            json={**body, "subject": "Private reply", "parent_id": post_id},
        )

    async def move():
        return await client.patch(
            f"/api/posts/{post_id}/manage",
            headers={**ALICE, "X-Test-Operation": "move"},
            json={"channel_id": destination},
        )

    operations = {"write": write, "move": move}
    second = "move" if first == "write" else "write"
    tasks = {first: asyncio.create_task(operations[first]())}
    try:
        await asyncio.wait_for(validated.wait(), timeout=5)
        tasks[second] = asyncio.create_task(operations[second]())
        await _wait_for_database_lock(setup_db, backend_pids, first, second, tasks[second])
        release.set()
        async with asyncio.timeout(10):
            responses = {operation: await task for operation, task in tasks.items()}
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks.values(), return_exceptions=True), timeout=10)

    if first == "write":
        assert responses["write"].status_code == 201, responses["write"].text
        assert responses["move"].status_code == 409, responses["move"].text
        expected_channel = source
    else:
        assert responses["move"].status_code == 200, responses["move"].text
        # Channel replies retain their explicit source channel; other writers
        # must recheck membership against the parent's newly committed audience.
        expected_status = 400 if write_kind == "channel_reply" else 403
        assert responses["write"].status_code == expected_status, responses["write"].text
        expected_channel = destination

    async with setup_db() as db:
        assert (
            await db.scalar(select(Post.channel_id).where(Post.id == post_id)) == expected_channel
        )
        bob_comments = (
            await db.scalars(select(Comment).where(Comment.author == "bob@herd.ai"))
        ).all()
        bob_replies = (await db.scalars(select(Post).where(Post.parent_post_id == post_id))).all()
        assert len(bob_comments) + len(bob_replies) == (1 if first == "write" else 0)
        assert all(reply.channel_id == expected_channel for reply in bob_replies)

    # Check the disclosure surface through actual HTTP reads after both commits.
    for suffix in ("", "/comments", "/thread"):
        carol_read = await client.get(f"/api/posts/{post_id}{suffix}", headers=CAROL)
        bob_read = await client.get(f"/api/posts/{post_id}{suffix}", headers=BOB)
        assert carol_read.status_code == (403 if first == "write" else 200)
        assert bob_read.status_code == (200 if first == "write" else 403)
        assert "Bob's confidential contribution" not in carol_read.text


async def test_lock_refreshes_an_already_loaded_post(setup_db, postgres_client):
    client, _ = postgres_client
    _, source = await _group(client, "Source")
    _, destination = await _group(client, "Destination")
    post_id = await _post(client, source)

    async with setup_db() as db:
        cached = await db.get(Post, post_id)
        assert cached.channel_id == source
        response = await client.patch(
            f"/api/posts/{post_id}/manage", headers=ALICE, json={"channel_id": destination}
        )
        assert response.status_code == 200, response.text
        locked = await lock_post(db, post_id)
        assert locked is cached
        assert locked.channel_id == destination
