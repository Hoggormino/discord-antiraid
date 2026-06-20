"""Shared test helpers: deterministic builders for members and events."""

from __future__ import annotations

import string
from typing import List, Optional

from antiraid.actions import Action, ActionType
from antiraid.models import (
    AuditAction,
    AuditEvent,
    JoinEvent,
    Member,
    MessageEvent,
)

GUILD = 1000

# A fixed "now" reference so account ages are predictable.
NOW = 1_700_000_000.0
DAY = 86400.0


def alpha_name(uid: int) -> str:
    """Map an id to a distinct letters-only name (a, b, ... z, aa, ab, ...).

    Letters-only and unique per id, so ``normalize_name`` keeps them distinct.
    Used as the default member name so unrelated joiners don't accidentally
    look like a coordinated-username cluster in tests.
    """
    s = ""
    n = uid + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = string.ascii_lowercase[r] + s
    return s


def member(
    uid: int,
    name: Optional[str] = None,
    age_days: float = 365.0,
    now: float = NOW,
    is_bot: bool = False,
    roles=(),
    has_avatar: bool = True,
    is_owner: bool = False,
) -> Member:
    return Member(
        id=uid,
        name=alpha_name(uid) if name is None else name,
        created_at=now - age_days * DAY,
        is_bot=is_bot,
        roles=tuple(roles),
        has_avatar=has_avatar,
        is_guild_owner=is_owner,
    )


def join(m: Member, ts: float, guild: int = GUILD) -> JoinEvent:
    return JoinEvent(guild_id=guild, member=m, timestamp=ts)


def message(
    author: Member,
    content: str,
    ts: float,
    mid: int,
    guild: int = GUILD,
    channel: int = 42,
    mentions: int = 0,
    everyone: bool = False,
) -> MessageEvent:
    return MessageEvent(
        guild_id=guild,
        channel_id=channel,
        author=author,
        content=content,
        timestamp=ts,
        message_id=mid,
        mention_count=mentions,
        mentions_everyone=everyone,
    )


def audit(
    actor: Member, action: AuditAction, ts: float, target: int = 9, guild: int = GUILD
) -> AuditEvent:
    return AuditEvent(
        guild_id=guild, actor=actor, action=action, target_id=target, timestamp=ts
    )


def types(actions: List[Action]) -> List[ActionType]:
    return [a.type for a in actions]


def has(actions: List[Action], t: ActionType) -> bool:
    return any(a.type is t for a in actions)


def targets(actions: List[Action], t: ActionType) -> List[int]:
    return [a.target_id for a in actions if a.type is t]
