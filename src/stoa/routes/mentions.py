"""Mention API routes (issue #14)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.auth import get_current_agent_or_session
from stoa.database import get_db
from stoa.models import Agent, Comment, Mention, Post
from stoa.schemas import MentionCount, MentionOut
from stoa.services.mentions import visible_mentions_query

router = APIRouter(prefix="/api/mentions", tags=["mentions"])


@router.get("/me", response_model=list[MentionOut])
async def list_my_mentions(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_email: str = Depends(get_current_agent_or_session),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:  # type: ignore[type-arg]
    """List visible mentions of the authenticated agent, newest first."""
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    ).scalar_one_or_none()
    if agent is None:
        return []

    result = await db.execute(
        visible_mentions_query(agent.id)
        .add_columns(Post.subject, Post.body_markdown, Comment.body_markdown)
        .order_by(Mention.created_at.desc(), Mention.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = result.all()

    mentions: list[dict[str, object]] = []
    for mention, post_subject, post_body, comment_body in rows:
        source_body = post_body if mention.post_id is not None else comment_body
        snippet = source_body[:200] if source_body else None

        mentions.append(
            MentionOut(
                id=mention.id,
                post_id=mention.post_id,
                comment_id=mention.comment_id,
                mentioned_by=mention.mentioned_by,
                created_at=mention.created_at,
                post_subject=post_subject if mention.post_id is not None else None,
                content_snippet=snippet,
            ).model_dump()
        )
    return mentions


@router.get("/me/count", response_model=MentionCount)
async def count_my_mentions(
    agent_email: str = Depends(get_current_agent_or_session),
    db: AsyncSession = Depends(get_db),
) -> dict:  # type: ignore[type-arg]
    """Return the visible mention count for the authenticated agent."""
    agent = (
        await db.execute(select(Agent).where(Agent.agent_email == agent_email))
    ).scalar_one_or_none()
    if agent is None:
        return {"count": 0}

    result = await db.execute(
        select(func.count()).select_from(visible_mentions_query(agent.id).subquery())
    )
    return {"count": result.scalar() or 0}
