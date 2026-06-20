"""Immutable domain events fed into the engine.

These types are intentionally decoupled from discord.py so the engine can be
driven by simulated events in tests.  All timestamps are POSIX seconds
(``float``); the engine *never* reads the clock itself, it only ever compares
timestamps carried on the events.  That is what makes every decision
reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Tuple


@dataclass(frozen=True)
class Member:
    """A guild member as the engine sees it."""

    id: int
    name: str
    #: account-creation time, POSIX seconds (Discord snowflake -> timestamp).
    created_at: float
    is_bot: bool = False
    #: role ids the member currently holds.
    roles: Tuple[int, ...] = ()
    has_avatar: bool = True
    #: True only for the guild owner — always exempt from enforcement.
    is_guild_owner: bool = False

    def age_seconds(self, now: float) -> float:
        """Account age at ``now`` (never negative)."""
        return max(0.0, now - self.created_at)


@dataclass(frozen=True)
class JoinEvent:
    guild_id: int
    member: Member
    timestamp: float


@dataclass(frozen=True)
class MessageEvent:
    guild_id: int
    channel_id: int
    author: Member
    content: str
    timestamp: float
    message_id: int
    mention_count: int = 0
    #: True if the message pings @everyone / @here.
    mentions_everyone: bool = False
    attachment_count: int = 0


class AuditAction(str, Enum):
    """Destructive moderation actions the anti-nuke layer watches for."""

    CHANNEL_DELETE = "channel_delete"
    CHANNEL_CREATE = "channel_create"
    ROLE_DELETE = "role_delete"
    ROLE_CREATE = "role_create"
    BAN = "ban"
    KICK = "kick"
    WEBHOOK_CREATE = "webhook_create"
    MEMBER_ROLE_UPDATE = "member_role_update"


@dataclass(frozen=True)
class AuditEvent:
    """A single entry from the guild audit log (used for anti-nuke detection)."""

    guild_id: int
    actor: Member
    action: AuditAction
    target_id: int
    timestamp: float


@dataclass(frozen=True)
class JoinerRecord:
    """A recent joiner remembered for retroactive raid clean-up."""

    timestamp: float
    member: Member = field(compare=False)
