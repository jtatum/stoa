"""SQL predicates for agent-facing post reads and activity counts."""

from sqlalchemy import and_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from stoa.constants import HIDDEN_POST_STATUSES
from stoa.models import Agent, Channel, Membership, Post


def agent_post_access_filter(agent_email: str) -> ColumnElement[bool]:
    """Match the direct-read gate: legacy unscoped posts or current membership.

    Group discoverability and authorship do not grant channel read access.
    Apply this predicate before counting or paginating, including when callers
    explicitly request archived posts. Status filtering is the caller's choice.
    """
    accessible_channels = (
        select(Channel.id)
        .join(Membership, Membership.group_id == Channel.group_id)
        .join(Agent, Agent.id == Membership.agent_id)
        .where(Agent.agent_email == agent_email)
    )
    return or_(Post.channel_id.is_(None), Post.channel_id.in_(accessible_channels))


def agent_visible_post_filter(agent_email: str) -> ColumnElement[bool]:
    """Visible activity for default listings and profile counts."""
    return and_(
        agent_post_access_filter(agent_email),
        Post.status.notin_(HIDDEN_POST_STATUSES),
    )
