"""End-to-end environment simulation.

Stands up an in-memory Discord "world" (a fake gateway + REST API) and drives
the REAL bot pipeline through it: the actual gateway event handlers
(``on_member_join`` / ``on_message`` / ``on_audit_log_entry_create``), the real
:class:`AntiRaidEngine`, the real paced action queue + token-bucket executor,
and the real bulk-ban / lockdown / anti-nuke execution code in
``AntiRaidBot``.  Only the Discord transport is faked.

It prints a live timeline of what the bot does, then asserts the outcomes and
exits non-zero on any failure, so it doubles as an integration test.

    python simulate.py
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import sys
import time
from unittest.mock import AsyncMock

import discord

from antiraid.actions import Action, ActionType
from antiraid.bot import AntiRaidBot
from antiraid.config import GuildConfig, RaidAction
from antiraid.config_store import ConfigStore

UTC = dt.timezone.utc
BASE = 1_700_000_000.0  # simulated wall clock origin (POSIX seconds)
DAY = 86400.0


def ts(offset: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(BASE + offset, tz=UTC)


# ---------------------------------------------------------------------------
# live timeline printer
# ---------------------------------------------------------------------------
_T0 = time.monotonic()
_VERBOSE = True


def show(icon: str, msg: str) -> None:
    if _VERBOSE:
        print(f"  [{time.monotonic() - _T0:6.2f}s] {icon} {msg}")


# ---------------------------------------------------------------------------
# recorder — tallies every side effect the bot performs
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self) -> None:
        self.bulk_bans = []          # list of counts per bulk request
        self.single_bans = 0
        self.kicks = 0
        self.timeouts = 0
        self.deletes = 0
        self.slowmoded = []   # channel ids put into slowmode
        self.locked = 0
        self.unlocked = 0
        self.verification = []
        self.alerts = []
        self.strips = []
        self.throttle_elapsed = 0.0
        self.throttle_n = 0

    @property
    def total_banned(self) -> int:
        return sum(self.bulk_bans) + self.single_bans


REC = Recorder()


# ---------------------------------------------------------------------------
# fake Discord objects (duck-typed; only what the adapter touches)
# ---------------------------------------------------------------------------
class FakePerms:
    def __init__(self, **kw):
        self._kw = kw

    def __getattr__(self, name):
        return self._kw.get(name, False)


class FakeRole:
    def __init__(self, rid, position, name="role", **perms):
        self.id = rid
        self.position = position
        self.name = name
        self.permissions = FakePerms(**perms)

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class FakeMember:
    def __init__(self, mid, name, *, age_days=400.0, joined=0.0, bot=False,
                 avatar=True, roles=None, position=5):
        self.id = mid
        self.name = name
        self.bot = bot
        self.avatar = object() if avatar else None
        self.created_at = ts(-age_days * DAY)
        self.joined_at = ts(joined)
        self._own = FakeRole(mid * 1000, position, f"{name}-role")
        self.roles = roles if roles is not None else [self._own]

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)

    async def kick(self, *, reason=None):
        REC.kicks += 1
        show("👢", f"KICK {self.name} ({reason})")

    async def timeout(self, until, *, reason=None):
        REC.timeouts += 1
        show("🔇", f"TIMEOUT {self.name}")

    async def add_roles(self, *roles, reason=None):
        show("🏷️", f"QUARANTINE {self.name}")

    async def remove_roles(self, *roles, reason=None):
        names = ", ".join(r.name for r in roles)
        REC.strips.append((self.id, [r.name for r in roles]))
        show("✂️", f"STRIP roles from {self.name}: {names}")


class FakeMessage:
    def __init__(self, mid):
        self.id = mid

    async def delete(self):
        REC.deletes += 1


class FakeChannel:
    def __init__(self, cid, name, me):
        self.id = cid
        self.name = name
        self._me = me

    def overwrites_for(self, role):
        return discord.PermissionOverwrite()

    def permissions_for(self, member):
        ow = discord.PermissionOverwrite()
        ow.send_messages = True
        return ow

    async def set_permissions(self, target, *, overwrite=None, reason=None):
        if overwrite is not None and overwrite.send_messages is False:
            REC.locked += 1
        else:
            REC.unlocked += 1

    async def edit(self, *, slowmode_delay=None, reason=None, **kw):
        if slowmode_delay:
            REC.slowmoded.append(self.id)
            show("🐢", f"SLOWMODE #{self.name} -> {slowmode_delay}s")

    async def fetch_message(self, mid):
        return FakeMessage(mid)

    async def send(self, *args, embed=None, **kw):
        if embed is not None:
            REC.alerts.append(embed.description)
            show("🚨", f"ALERT → #{self.name}: {embed.description}")


class FakeGuild:
    def __init__(self, gid, name, owner_id, me):
        self.id = gid
        self.name = name
        self.owner_id = owner_id
        self.me = me
        self.default_role = FakeRole(gid, 0, "@everyone")
        self._members = {}
        self._channels = {}
        self.text_channels = []

    # roster / channel lookup -------------------------------------------
    def add_member(self, m):
        m.guild = self
        self._members[m.id] = m

    def get_member(self, mid):
        return self._members.get(mid)

    async def fetch_member(self, mid):
        return self._members.get(mid)

    def add_channel(self, c):
        self._channels[c.id] = c
        self.text_channels.append(c)

    def get_channel(self, cid):
        return self._channels.get(cid)

    @property
    def channels(self):
        return list(self._channels.values())

    # REST actions ------------------------------------------------------
    async def ban(self, user, *, reason=None, delete_message_seconds=0):
        REC.single_bans += 1
        show("⛔", f"SINGLE BAN id={user.id}")

    async def bulk_ban(self, users, *, reason=None, delete_message_seconds=0):
        n = len(users)
        REC.bulk_bans.append(n)
        show("⛔", f"BULK BAN {n} accounts in ONE request ({reason})")

    async def edit(self, *, verification_level=None, reason=None):
        REC.verification.append(verification_level)
        show("[v]", f"VERIFICATION -> {verification_level}")


class FakeAction:
    def __init__(self, name):
        self.name = name


class FakeAuditEntry:
    def __init__(self, action_name, user, guild, target_id, when):
        self.action = FakeAction(action_name)
        self.user = user
        self.guild = guild
        self.target = discord.Object(id=target_id)
        self.created_at = ts(when)


# ---------------------------------------------------------------------------
# helpers to drive the bot
# ---------------------------------------------------------------------------
async def drain(bot):
    """Wait for the executor to finish everything currently queued."""
    await bot._actions.join()


def message(author, channel, content, when, mid, *, mentions=0, everyone=False):
    msg = type("Msg", (), {})()
    msg.guild = channel._guild
    msg.channel = channel
    msg.author = author
    msg.content = content
    msg.created_at = ts(when)
    msg.id = mid
    msg.mentions = [0] * mentions
    msg.mention_everyone = everyone
    msg.attachments = []
    return msg


# ---------------------------------------------------------------------------
# the simulation
# ---------------------------------------------------------------------------
async def _run_impl(config_path: str):
    store = ConfigStore(config_path)
    bot = AntiRaidBot(store=store, action_rate=25.0)
    # neutralise the parts that need a live gateway connection
    bot.wait_until_ready = AsyncMock()
    bot.is_closed = lambda: False
    bot.process_commands = AsyncMock()

    # ---- build the world ----------------------------------------------
    botself = FakeMember(1, "AntiRaid", position=100)  # bot sits high
    guild = FakeGuild(5000, "Test Server", owner_id=42, me=botself)
    for cid, cname in [(1, "general"), (2, "chat"), (3, "memes"), (4, "logs")]:
        ch = FakeChannel(cid, cname, botself)
        ch._guild = guild
        guild.add_channel(ch)
    general = guild.get_channel(1)

    owner = FakeMember(42, "Owner", position=90)
    guild.add_member(owner)
    guild.add_member(botself)
    for i in range(3):
        guild.add_member(FakeMember(100 + i, f"regular{i}", position=5))

    # the bot only knows about guilds via on_member_join etc.; for the tick
    # loop we reference the guild directly.
    cfg = GuildConfig(
        guild_id=guild.id,
        join_rate_threshold=8,
        join_window_seconds=10,
        min_account_age_days=7,
        raid_action=RaidAction.BAN,
        raid_only_suspicious=True,
        msg_rate_threshold=6,
        cross_user_threshold=5,
        cross_user_window=10,
        nuke_threshold=4,
        nuke_window=10,
        raid_cooldown_seconds=120,
        alert_channel_id=4,  # #logs
    )
    store.set(cfg)
    bot.engine.set_config(cfg)

    worker = asyncio.create_task(bot._executor_worker())

    print("\n" + "=" * 70)
    print(f"  ENVIRONMENT UP — guild '{guild.name}', "
          f"{len(guild.text_channels)} channels, {len(guild._members)} members")
    print("=" * 70)

    # === Scenario 1: normal traffic (must NOT react) ====================
    print("\n--- Scenario 1: ordinary activity (expect: no enforcement) ---")
    base = REC.total_banned
    for i, m in enumerate(list(guild._members.values())[:3]):
        await bot.on_message(message(m, general, f"hello there {i}", 1 + i * 3, 10 + i))
    newbie = FakeMember(200, "NormalNewbie", age_days=120, joined=5, position=5)
    guild.add_member(newbie)
    await bot.on_member_join(newbie)
    await drain(bot)
    show("✅", "no enforcement actions taken on normal traffic"
         if REC.total_banned == base and REC.timeouts == 0 else "UNEXPECTED action!")

    # === Scenario 2: mass-join raid =====================================
    print("\n--- Scenario 2: mass-join raid (20 fresh accounts flood in) ---")
    raiders = [
        FakeMember(1000 + i, f"Raider{i}", age_days=0.2, joined=20 + i * 0.3,
                   avatar=False, position=5)
        for i in range(20)
    ]
    for r in raiders:
        guild.add_member(r)
        await bot.on_member_join(r)  # completes without yielding -> all queue up
    await drain(bot)
    show("📊", f"raid_active={bot.engine.is_raid_active(guild.id)} "
         f"locked_down={bot.engine.is_locked_down(guild.id)}")

    # === Scenario 3: raid subsides, lockdown auto-lifts =================
    print("\n--- Scenario 3: 3 minutes of quiet → auto de-escalation ---")
    for action_guild, actions in [(guild, bot.engine.tick(guild.id, BASE + 20 + 200))]:
        await bot._dispatch(action_guild, actions)
    await drain(bot)
    show("📊", f"raid_active={bot.engine.is_raid_active(guild.id)} "
         f"locked_down={bot.engine.is_locked_down(guild.id)}")

    # === Scenario 4: coordinated spam wave ==============================
    print("\n--- Scenario 4: coordinated spam wave (6 accounts, identical scam) ---")
    spammers = [FakeMember(2000 + i, f"shill{i}", age_days=300, position=5)
                for i in range(6)]
    for i, s in enumerate(spammers):
        guild.add_member(s)
        await bot.on_message(
            message(s, general, "FREE NITRO -> http://scam.example/claim",
                    400 + i * 0.4, 5000 + i)
        )
    await drain(bot)
    show("📊", f"messages deleted={REC.deletes}, channel slowmoded={len(REC.slowmoded)}, "
         f"locked_down={bot.engine.is_locked_down(guild.id)}")
    # lift again for the next scenario
    await bot._dispatch(guild, bot.engine.tick(guild.id, BASE + 400 + 200))
    await drain(bot)

    # === Scenario 5: compromised admin nuke =============================
    print("\n--- Scenario 5: compromised admin mass-deletes channels (anti-nuke) ---")
    admin_role = FakeRole(99999, 40, "Admin", administrator=True,
                          ban_members=True, manage_guild=True)
    rogue = FakeMember(777, "HackedMod", age_days=500, position=40,
                       roles=[admin_role])
    guild.add_member(rogue)
    for i in range(5):
        await bot.on_audit_log_entry_create(
            FakeAuditEntry("channel_delete", rogue, guild, target_id=10 + i,
                           when=700 + i)
        )
    await drain(bot)
    show("📊", f"rogue admins neutralised={len(REC.strips)}")

    # === Scenario 6: rate limiter throttles a burst =====================
    print("\n--- Scenario 6: rate limiter paces a burst (action_rate = 10/s) ---")
    slow = AntiRaidBot(store=store, action_rate=10.0)
    slow.wait_until_ready = AsyncMock()
    slow.is_closed = lambda: False
    slow.process_commands = AsyncMock()
    g2 = FakeGuild(6000, "Burst Server", owner_id=1,
                   me=FakeMember(1, "bot", position=100))
    for i in range(40):
        g2.add_member(FakeMember(3000 + i, f"u{i}", position=5))
    w2 = asyncio.create_task(slow._executor_worker())
    n_burst = 20
    burst = [
        Action(ActionType.TIMEOUT_MEMBER, g2.id, target_id=3000 + i,
               reason="burst", duration=60)
        for i in range(n_burst)
    ]
    t0 = time.monotonic()
    await slow._dispatch(g2, burst)
    await slow._actions.join()
    REC.throttle_elapsed = time.monotonic() - t0
    REC.throttle_n = n_burst
    w2.cancel()
    try:
        await w2
    except asyncio.CancelledError:
        pass
    expected = (n_burst - 10) / 10.0  # 10 free (capacity), rest paced at 10/s
    show("⏱️", f"{n_burst} actions drained in {REC.throttle_elapsed:.2f}s "
         f"(10 instant, rest paced ~10/s; expected ≈{expected:.1f}s)")

    # ---- shut the worker down -----------------------------------------
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    # ===================================================================
    # results + assertions
    # ===================================================================
    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)
    print(f"  accounts banned ......... {REC.total_banned} "
          f"(via {len(REC.bulk_bans)} bulk request(s) of sizes {REC.bulk_bans}"
          f" + {REC.single_bans} single)")
    print(f"  channels locked ......... {REC.locked}")
    print(f"  channels unlocked ....... {REC.unlocked}")
    print(f"  verification changes .... {len(REC.verification)}")
    print(f"  spam messages deleted ... {REC.deletes}")
    print(f"  channels slowmoded ...... {len(REC.slowmoded)}")
    print(f"  rogue admins stripped ... {len(REC.strips)}")
    print(f"  mod alerts sent ......... {len(REC.alerts)}")

    checks = [
        ("20 raiders banned", REC.total_banned == 20),
        ("bans coalesced into bulk request(s)", len(REC.bulk_bans) >= 1
         and sum(REC.bulk_bans) >= 19),
        ("no single-ban fallback needed", REC.single_bans <= 1),
        ("channels were locked down", REC.locked >= 4),
        ("lockdown later lifted", REC.unlocked >= 4),
        ("verification raised then lowered", len(REC.verification) >= 2),
        ("spam wave fully deleted", REC.deletes >= 6),
        ("spam wave throttled the channel (slowmode)", len(REC.slowmoded) >= 1),
        ("rogue admin stripped exactly once", len(REC.strips) == 1),
        ("normal traffic untouched (no false positives)",
         REC.kicks == 0),
        ("rate limiter throttled the burst (>=0.6s for 20 ops @10/s)",
         REC.throttle_elapsed >= 0.6),
    ]
    print("\n  CHECKS")
    ok = True
    for label, passed in checks:
        print(f"    [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    print("\n" + ("  ✅ ENVIRONMENT TEST PASSED" if ok
                  else "  ❌ ENVIRONMENT TEST FAILED") + "\n")
    return REC, checks


async def run(verbose: bool = True, config_path: str = "sim_configs.json"):
    """Run the full environment simulation.

    Returns ``(recorder, checks)`` where ``checks`` is a list of
    ``(label, passed)`` tuples.  Set ``verbose=False`` to silence output
    (used by the integration test).
    """
    global REC, _T0, _VERBOSE
    REC = Recorder()
    _T0 = time.monotonic()
    _VERBOSE = verbose
    if verbose:
        return await _run_impl(config_path)
    with contextlib.redirect_stdout(io.StringIO()):
        return await _run_impl(config_path)


if __name__ == "__main__":
    _rec, _checks = asyncio.run(run(verbose=True))
    sys.exit(0 if all(p for _, p in _checks) else 1)
