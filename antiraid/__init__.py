"""A secure, fully testable Discord anti-raid engine.

The package is split into two layers:

* A **pure** core (``models``, ``actions``, ``config``, ``state``, ``engine``)
  that has *no* third-party dependencies and never touches the network or the
  wall clock directly.  Every decision is a deterministic function of the
  events fed in and the timestamps carried on those events.  This is what the
  test-suite exercises.

* A thin Discord **adapter** (``bot``) that translates gateway events into the
  pure domain events, runs them through the engine, and safely executes the
  actions the engine returns.
"""

from .models import Member, JoinEvent, MessageEvent, AuditEvent, AuditAction
from .actions import Action, ActionType, Severity
from .config import GuildConfig
from .engine import AntiRaidEngine

__all__ = [
    "Member",
    "JoinEvent",
    "MessageEvent",
    "AuditEvent",
    "AuditAction",
    "Action",
    "ActionType",
    "Severity",
    "GuildConfig",
    "AntiRaidEngine",
]

__version__ = "1.0.0"
