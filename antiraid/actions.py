"""Actions emitted by the engine.

The engine is *pure*: it never performs side effects, it only returns a list of
:class:`Action` objects describing what *should* happen.  The Discord adapter
(``bot.py``) is responsible for executing them safely (permission/hierarchy
checks, rate-limit handling, dedup).  This separation is what makes the engine
testable and keeps all the dangerous side effects behind one auditable layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Severity(Enum):
    INFO = 10
    LOW = 20
    MEDIUM = 30
    HIGH = 40
    CRITICAL = 50

    def __lt__(self, other: "Severity") -> bool:  # enables max()/sorting
        if isinstance(other, Severity):
            return self.value < other.value
        return NotImplemented


class ActionType(Enum):
    # Per-member enforcement
    BAN_MEMBER = "ban_member"
    KICK_MEMBER = "kick_member"
    TIMEOUT_MEMBER = "timeout_member"
    QUARANTINE_MEMBER = "quarantine_member"  # strip to a locked "unverified" role
    DELETE_MESSAGE = "delete_message"
    SET_SLOWMODE = "set_slowmode"  # channel-level rate limit (target = channel id)
    WARN_MEMBER = "warn_member"     # post a public warning in the channel

    # Guild-wide raid response
    ENABLE_LOCKDOWN = "enable_lockdown"
    DISABLE_LOCKDOWN = "disable_lockdown"
    RAISE_VERIFICATION = "raise_verification"
    LOWER_VERIFICATION = "lower_verification"

    # Anti-nuke
    STRIP_ACTOR_PERMISSIONS = "strip_actor_permissions"

    # Always safe
    ALERT = "alert"


@dataclass
class Action:
    """A single instruction returned by the engine."""

    type: ActionType
    guild_id: int
    reason: str
    severity: Severity = Severity.MEDIUM
    #: member/message/actor id the action targets (None for guild-wide actions).
    target_id: Optional[int] = None
    #: optional duration in seconds for TIMEOUT_MEMBER.
    duration: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        tgt = f" target={self.target_id}" if self.target_id is not None else ""
        return f"<{self.type.value} guild={self.guild_id}{tgt} sev={self.severity.name}: {self.reason}>"
