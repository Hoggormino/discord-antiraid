"""Tests for the Discord adapter's safety logic.

These use small duck-typed fakes instead of a live gateway, and are skipped
automatically if discord.py is not installed (the core engine never needs it).
"""

import datetime
import os
import tempfile
import unittest
from unittest.mock import AsyncMock

try:
    import discord  # noqa: F401
    from antiraid.bot import AntiRaidBot, to_domain_member
    from antiraid.config_store import ConfigStore
    from antiraid.incident_store import Incident, IncidentStore

    HAVE_DISCORD = True
except Exception:  # pragma: no cover
    HAVE_DISCORD = False

from antiraid.actions import Action, ActionType


class FakePerms:
    def __init__(self, **kw):
        self._kw = kw

    def __getattr__(self, name):
        return self._kw.get(name, False)


class FakeRole:
    def __init__(self, id, position, **perms):
        self.id = id
        self.position = position
        self.permissions = FakePerms(**perms)

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class FakeMember:
    def __init__(self, id, position, roles=(), bot=False, name="user", avatar=True):
        self.id = id
        self.name = name
        self.bot = bot
        self.avatar = object() if avatar else None
        self.created_at = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        self._top = FakeRole(id * 10, position)
        self.roles = list(roles) or [self._top]

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)


class FakeGuild:
    def __init__(self, id, owner_id, me):
        self.id = id
        self.owner_id = owner_id
        self.me = me
        self._members = {}

    def get_member(self, mid):
        return self._members.get(mid)

    async def fetch_member(self, mid):
        return self._members.get(mid)


def make_bot(incidents=None):
    d = tempfile.mkdtemp()
    store = ConfigStore(os.path.join(d, "c.json"))
    if incidents is None:
        incidents = IncidentStore(os.path.join(d, "inc.json"))
    return AntiRaidBot(store=store, incidents=incidents)


class FakeLockChannel:
    def __init__(self, cid):
        self.id = cid

    def overwrites_for(self, role):
        return discord.PermissionOverwrite()  # send_messages defaults to None

    async def set_permissions(self, target, *, overwrite=None, reason=None):
        pass


@unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
class TestCanAct(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.me = FakeMember(1, position=50)  # bot's own member
        self.guild = FakeGuild(100, owner_id=999, me=self.me)

    def test_cannot_act_on_owner(self):
        owner = FakeMember(999, position=10)
        self.assertFalse(self.bot._can_act(self.guild, owner))

    def test_cannot_act_above_or_equal_hierarchy(self):
        higher = FakeMember(2, position=50)  # equal to bot
        above = FakeMember(3, position=80)
        self.assertFalse(self.bot._can_act(self.guild, higher))
        self.assertFalse(self.bot._can_act(self.guild, above))

    def test_can_act_on_lower_member(self):
        low = FakeMember(4, position=10)
        self.assertTrue(self.bot._can_act(self.guild, low))

    def test_cannot_act_on_none(self):
        self.assertFalse(self.bot._can_act(self.guild, None))


@unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
class TestToDomainMember(unittest.TestCase):
    def test_mapping_and_owner_and_everyone_filtered(self):
        guild = FakeGuild(100, owner_id=7, me=FakeMember(1, 50))
        roles = [FakeRole(100, 0), FakeRole(55, 5)]  # 100 == guild.id (@everyone)
        m = FakeMember(7, position=5, roles=roles, name="alice", avatar=False)
        dm = to_domain_member(m, guild)
        self.assertEqual(dm.id, 7)
        self.assertEqual(dm.name, "alice")
        self.assertTrue(dm.is_guild_owner)        # id == owner_id
        self.assertFalse(dm.has_avatar)
        self.assertEqual(dm.roles, (55,))          # @everyone (id 100) filtered out


@unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
class TestApplyRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = make_bot()
        self.me = FakeMember(1, position=50)
        self.guild = FakeGuild(100, owner_id=999, me=self.me)
        self.guild.ban = AsyncMock()

    async def test_ban_lower_member(self):
        target = FakeMember(4, position=10)
        target.kick = AsyncMock()
        self.guild._members[4] = target
        await self.bot._apply(
            self.guild, Action(ActionType.BAN_MEMBER, 100, target_id=4, reason="raid")
        )
        self.guild.ban.assert_awaited()

    async def test_ban_owner_blocked(self):
        owner = FakeMember(999, position=10)
        self.guild._members[999] = owner
        await self.bot._apply(
            self.guild, Action(ActionType.BAN_MEMBER, 100, target_id=999, reason="x")
        )
        self.guild.ban.assert_not_awaited()

    async def test_kick_above_hierarchy_blocked(self):
        target = FakeMember(5, position=90)  # above the bot
        target.kick = AsyncMock()
        self.guild._members[5] = target
        await self.bot._apply(
            self.guild, Action(ActionType.KICK_MEMBER, 100, target_id=5, reason="x")
        )
        target.kick.assert_not_awaited()


@unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
class TestBulkBanBatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = make_bot()
        self.me = FakeMember(1, position=50)
        self.guild = FakeGuild(100, owner_id=999, me=self.me)
        self.guild.bulk_ban = AsyncMock()
        self.guild.ban = AsyncMock()
        self.guild.default_role = FakeRole(100, 0)
        self.guild.text_channels = []

    def _ban_batch(self, ids):
        return [
            (self.guild, Action(ActionType.BAN_MEMBER, 100, target_id=i, reason="r"))
            for i in ids
        ]

    async def test_many_bans_become_one_bulk_call(self):
        for i in (2, 3, 4):
            self.guild._members[i] = FakeMember(i, position=10)
        await self.bot._run_batch(self._ban_batch([2, 3, 4]))
        self.guild.bulk_ban.assert_awaited_once()
        ids = sorted(o.id for o in self.guild.bulk_ban.await_args.args[0])
        self.assertEqual(ids, [2, 3, 4])
        self.guild.ban.assert_not_awaited()

    async def test_over_200_splits_into_two_bulk_calls(self):
        await self.bot._run_batch(self._ban_batch(range(250)))
        self.assertEqual(self.guild.bulk_ban.await_count, 2)
        sizes = sorted(len(c.args[0]) for c in self.guild.bulk_ban.await_args_list)
        self.assertEqual(sizes, [50, 200])

    async def test_urgent_first_and_hierarchy_filtered(self):
        self.guild._members[999] = FakeMember(999, position=5)  # owner
        self.guild._members[5] = FakeMember(5, position=90)     # above bot
        self.guild._members[6] = FakeMember(6, position=10)     # bannable
        batch = [(self.guild, Action(ActionType.ENABLE_LOCKDOWN, 100, reason="r"))]
        batch += self._ban_batch([999, 5, 6])
        await self.bot._run_batch(batch)
        # lockdown (urgent) executed
        self.assertIn(100, self.bot._lockdown_snapshots)
        # only id 6 survived the filter -> single-ban path, never bulk
        self.guild.bulk_ban.assert_not_awaited()
        self.guild.ban.assert_awaited_once()
        self.assertEqual(self.guild.ban.await_args.args[0].id, 6)

    async def test_bulk_failure_falls_back_to_single_bans(self):
        import types

        resp = types.SimpleNamespace(status=403, reason="Forbidden")
        self.guild.bulk_ban = AsyncMock(side_effect=discord.Forbidden(resp, "no"))
        for i in (2, 3, 4):
            self.guild._members[i] = FakeMember(i, position=10)
        await self.bot._bulk_ban(self.guild, (2, 3, 4), "r")
        self.assertEqual(self.guild.ban.await_count, 3)


@unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
class TestIncidentRestore(unittest.TestCase):
    def test_restore_rearms_lockdown_and_raid_timers(self):
        d = tempfile.mkdtemp()
        store = IncidentStore(os.path.join(d, "inc.json"))
        store.set(Incident(
            guild_id=100, lockdown_active=True, raid_active=True,
            raid_started_at=5.0, last_raid_signal=9.0,
            channel_snapshot={1: None, 2: True},
        ))
        bot = make_bot(incidents=store)
        bot._restore_incidents()
        self.assertTrue(bot.engine.is_locked_down(100))
        self.assertTrue(bot.engine.is_raid_active(100))
        st = bot.engine.store.get(100)
        self.assertEqual(st.last_raid_signal, 9.0)
        self.assertEqual(bot._lockdown_snapshots[100], {1: None, 2: True})

    def test_nothing_to_restore_is_noop(self):
        d = tempfile.mkdtemp()
        bot = make_bot(incidents=IncidentStore(os.path.join(d, "inc.json")))
        bot._restore_incidents()
        self.assertFalse(bot.engine.is_locked_down(100))


@unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
class TestIncidentPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_enable_persists_then_disable_clears(self):
        d = tempfile.mkdtemp()
        store = IncidentStore(os.path.join(d, "inc.json"))
        bot = make_bot(incidents=store)
        guild = FakeGuild(100, owner_id=999, me=FakeMember(1, position=50))
        guild.default_role = FakeRole(100, 0)
        guild.text_channels = [FakeLockChannel(1), FakeLockChannel(2)]

        # engine marks state before the adapter applies the lockdown
        st = bot.engine.store.get(100)
        st.lockdown_active = True
        st.raid_active = True

        await bot._set_lockdown(guild, True, "raid")
        inc = store.get(100)
        self.assertIsNotNone(inc)
        self.assertTrue(inc.lockdown_active)
        self.assertEqual(set(inc.channel_snapshot), {1, 2})

        # a fresh store reading the same file sees the active incident
        self.assertEqual(IncidentStore(os.path.join(d, "inc.json")).active_guild_ids(),
                         [100])

        await bot._set_lockdown(guild, False, "lifted")
        self.assertIsNone(store.get(100))


if __name__ == "__main__":
    unittest.main()
