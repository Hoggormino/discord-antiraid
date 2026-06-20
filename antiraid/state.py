"""Per-guild mutable runtime state.

All windows are pruned lazily against the timestamp of the *incoming* event, so
the structures never grow without bound and the engine stays deterministic
regardless of how much wall-clock time has actually passed.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Set, Tuple

from .models import Member


def _prune_left(dq: Deque[Tuple[float, ...]], cutoff: float) -> None:
    """Drop entries whose timestamp (item[0]) is older than ``cutoff``."""
    while dq and dq[0][0] < cutoff:
        dq.popleft()


@dataclass
class GuildState:
    guild_id: int

    # ---- raid / lockdown lifecycle ------------------------------------
    raid_active: bool = False
    lockdown_active: bool = False
    raid_started_at: float = 0.0
    last_raid_signal: float = 0.0
    #: member ids already actioned in the current raid (prevents duplicates).
    actioned_members: Set[int] = field(default_factory=set)

    # ---- join tracking -------------------------------------------------
    joins: Deque[Tuple[float, int]] = field(default_factory=deque)
    recent_joiners: Deque[Tuple[float, Member]] = field(default_factory=deque)

    # ---- message tracking (keyed by user / by content) ----------------
    user_msgs: Dict[int, Deque[Tuple[float, int]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    user_dupes: Dict[int, Deque[Tuple[float, str]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    user_mentions: Dict[int, Deque[Tuple[float, int]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    user_links: Dict[int, Deque[Tuple[float, int]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    #: content-hash -> recent (timestamp, user_id, message_id) for cross-user spam.
    content_authors: Dict[str, Deque[Tuple[float, int, int]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    #: content fingerprints already swept as a coordinated wave (dedup).
    flagged_content: Set[str] = field(default_factory=set)

    # ---- anti-nuke -----------------------------------------------------
    actor_audit: Dict[int, Deque[Tuple[float, str]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    stripped_actors: Set[int] = field(default_factory=set)

    # ------------------------------------------------------------------
    def prune_joins(self, cutoff: float) -> None:
        _prune_left(self.joins, cutoff)
        _prune_left(self.recent_joiners, cutoff)

    def clear_raid(self) -> None:
        self.raid_active = False
        self.raid_started_at = 0.0
        self.actioned_members.clear()

    @staticmethod
    def _prune_dict(d: Dict[int, Deque], cutoff: float) -> None:
        """Prune every deque in ``d`` and drop keys that become empty."""
        for key in [k for k, dq in d.items() if (_prune_left(dq, cutoff) or not dq)]:
            del d[key]

    def housekeep(self, now: float, cfg) -> None:
        """Drop stale tracking data so memory stays bounded over time.

        Safe to call on every tick; it only removes entries that have aged out
        of their detection window, so it can never weaken live detection.
        """
        self.prune_joins(now - cfg.join_window_seconds)
        self._prune_dict(self.user_msgs, now - cfg.msg_rate_window)
        self._prune_dict(self.user_dupes, now - cfg.duplicate_window)
        self._prune_dict(self.user_mentions, now - cfg.mention_window)
        self._prune_dict(self.user_links, now - cfg.link_window)
        self._prune_dict(self.content_authors, now - cfg.cross_user_window)
        self._prune_dict(self.actor_audit, now - cfg.nuke_window)
        # Forget fingerprints whose wave has fully aged out.
        self.flagged_content &= set(self.content_authors)
        # actioned_members is only meaningful during an active raid; outside one
        # it would otherwise accumulate username-filter hits indefinitely.
        if not self.raid_active:
            self.actioned_members.clear()


class StateStore:
    """Holds :class:`GuildState` for every guild the engine has seen."""

    def __init__(self) -> None:
        self._guilds: Dict[int, GuildState] = {}

    def get(self, guild_id: int) -> GuildState:
        state = self._guilds.get(guild_id)
        if state is None:
            state = GuildState(guild_id=guild_id)
            self._guilds[guild_id] = state
        return state

    def all_guild_ids(self) -> List[int]:
        return list(self._guilds)
