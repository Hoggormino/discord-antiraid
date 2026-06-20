"""Action scheduling: bulk-ban coalescing + a token-bucket rate limiter.

The *planning* here is pure (no Discord, no clock-by-default) so it is fully
unit-testable.  The adapter (``bot.py``) drains the engine's actions through
:func:`plan_actions` and paces execution with :class:`TokenBucket`, so a raid
that produces hundreds of bans collapses into a handful of bulk-ban API calls
issued safely under Discord's rate limit instead of a burst of hundreds of
requests (which would risk the 10,000-invalid-requests/10-min Cloudflare ban).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Tuple

from .actions import Action, ActionType

#: Discord's Bulk Guild Ban endpoint accepts at most 200 users per request.
MAX_BULK_BAN = 200

#: Guild-wide / urgent actions are executed immediately, ahead of the paced
#: per-member work, so lockdown and rogue-admin neutralisation are not delayed
#: behind a queue of bans.
URGENT_TYPES = frozenset(
    {
        ActionType.ENABLE_LOCKDOWN,
        ActionType.DISABLE_LOCKDOWN,
        ActionType.RAISE_VERIFICATION,
        ActionType.LOWER_VERIFICATION,
        ActionType.STRIP_ACTOR_PERMISSIONS,
        ActionType.ALERT,
    }
)


@dataclass(frozen=True)
class BanChunk:
    """A set of user ids to ban in a single Bulk Guild Ban request."""

    guild_id: int
    target_ids: Tuple[int, ...]
    reason: str


@dataclass
class ExecutionPlan:
    urgent: List[Action] = field(default_factory=list)
    ban_chunks: List[BanChunk] = field(default_factory=list)
    others: List[Action] = field(default_factory=list)


def plan_actions(actions: List[Action], max_per_ban_call: int = MAX_BULK_BAN) -> ExecutionPlan:
    """Split a batch of actions into urgent / coalesced-bans / everything else.

    Bans are de-duplicated and grouped per guild, then chunked to the bulk-ban
    size limit.  Non-ban member actions and urgent guild actions keep their
    original order.
    """
    if max_per_ban_call < 1:
        raise ValueError("max_per_ban_call must be >= 1")

    urgent: List[Action] = []
    others: List[Action] = []
    ban_ids: "OrderedDict[int, List[int]]" = OrderedDict()
    seen: dict = {}

    for action in actions:
        if action.type in URGENT_TYPES:
            urgent.append(action)
        elif action.type is ActionType.BAN_MEMBER and action.target_id is not None:
            guild_seen = seen.setdefault(action.guild_id, set())
            if action.target_id in guild_seen:
                continue  # already going to be banned in this batch
            guild_seen.add(action.target_id)
            ban_ids.setdefault(action.guild_id, []).append(action.target_id)
        else:
            others.append(action)

    chunks: List[BanChunk] = []
    for guild_id, ids in ban_ids.items():
        for start in range(0, len(ids), max_per_ban_call):
            part = tuple(ids[start : start + max_per_ban_call])
            noun = "account" if len(part) == 1 else "accounts"
            chunks.append(
                BanChunk(guild_id, part, f"Raid response: bulk ban ({len(part)} {noun})")
            )

    return ExecutionPlan(urgent=urgent, ban_chunks=chunks, others=others)


class TokenBucket:
    """A simple async token-bucket rate limiter.

    ``time_func`` and ``sleep_func`` are injectable so the pacing math can be
    tested deterministically without real time.  In production they default to
    ``time.monotonic`` and ``asyncio.sleep``.
    """

    def __init__(
        self,
        rate: float,
        capacity: Optional[float] = None,
        *,
        time_func: Optional[Callable[[], float]] = None,
        sleep_func: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self._time = time_func or time.monotonic
        self._sleep = sleep_func
        self.tokens = self.capacity
        self._updated = self._time()

    def _refill(self) -> None:
        now = self._time()
        elapsed = now - self._updated
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self._updated = now

    async def acquire(self, amount: float = 1.0) -> None:
        # Never wait for more than a full bucket — tokens cap at capacity, so
        # asking for more than capacity could otherwise block forever.
        amount = min(amount, self.capacity)
        sleep = self._sleep
        if sleep is None:  # bind lazily so construction needs no running loop
            import asyncio

            sleep = asyncio.sleep
        # The epsilon absorbs floating-point dust: without it, repeated refills
        # can approach `amount` from just below and spin the loop indefinitely
        # with ever-smaller sleeps (a CPU-pinning hang).
        epsilon = 1e-9
        while True:
            self._refill()
            if self.tokens + epsilon >= amount:
                self.tokens = max(0.0, self.tokens - amount)
                return
            await sleep((amount - self.tokens) / self.rate)
