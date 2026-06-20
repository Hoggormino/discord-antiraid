"""Tests for join-based detection: mass-join, coordinated names, age gate."""

import unittest

from antiraid.actions import ActionType
from antiraid.config import GuildConfig, RaidAction
from antiraid.engine import AntiRaidEngine
from tests import _util as u


def engine(**overrides) -> AntiRaidEngine:
    cfg = GuildConfig(guild_id=u.GUILD, **overrides)
    eng = AntiRaidEngine()
    eng.set_config(cfg)
    return eng


class TestMassJoin(unittest.TestCase):
    def test_no_raid_below_threshold(self):
        eng = engine(join_rate_threshold=8, join_window_seconds=10)
        actions = []
        for i in range(7):
            actions += eng.process_join(u.join(u.member(i, age_days=400), u.NOW + i))
        self.assertFalse(eng.is_raid_active(u.GUILD))
        self.assertFalse(u.has(actions, ActionType.ENABLE_LOCKDOWN))

    def test_raid_triggers_at_threshold(self):
        eng = engine(join_rate_threshold=8, join_window_seconds=10)
        actions = []
        for i in range(8):
            actions += eng.process_join(u.join(u.member(i, age_days=400), u.NOW + i))
        self.assertTrue(eng.is_raid_active(u.GUILD))
        self.assertTrue(eng.is_locked_down(u.GUILD))
        self.assertTrue(u.has(actions, ActionType.ENABLE_LOCKDOWN))
        self.assertTrue(u.has(actions, ActionType.RAISE_VERIFICATION))

    def test_joins_spread_out_do_not_trigger(self):
        # 8 joins but each 5s apart -> never 8 inside a 10s window.
        eng = engine(join_rate_threshold=8, join_window_seconds=10)
        for i in range(8):
            eng.process_join(u.join(u.member(i, age_days=400), u.NOW + i * 5))
        self.assertFalse(eng.is_raid_active(u.GUILD))

    def test_retroactive_sweep_bans_all_window_joiners(self):
        # Old accounts, raid_only_suspicious False -> everyone swept.
        eng = engine(
            join_rate_threshold=5,
            join_window_seconds=10,
            raid_action=RaidAction.BAN,
            raid_only_suspicious=False,
        )
        actions = []
        for i in range(5):
            actions += eng.process_join(u.join(u.member(i, age_days=400), u.NOW + i))
        banned = set(u.targets(actions, ActionType.BAN_MEMBER))
        self.assertEqual(banned, {0, 1, 2, 3, 4})

    def test_sweep_only_suspicious_spares_old_accounts(self):
        eng = engine(
            join_rate_threshold=5,
            join_window_seconds=10,
            min_account_age_days=7,
            raid_action=RaidAction.BAN,
            raid_only_suspicious=True,
        )
        actions = []
        # 4 fresh accounts (age 1d) + 1 old account triggers the raid.
        for i in range(4):
            actions += eng.process_join(u.join(u.member(i, age_days=1), u.NOW + i))
        actions += eng.process_join(u.join(u.member(99, age_days=400), u.NOW + 4))
        banned = set(u.targets(actions, ActionType.BAN_MEMBER))
        self.assertEqual(banned, {0, 1, 2, 3})  # old account 99 spared

    def test_no_double_action_for_same_member(self):
        eng = engine(
            join_rate_threshold=3,
            join_window_seconds=30,
            min_account_age_days=7,
            raid_only_suspicious=False,
            raid_action=RaidAction.KICK,
        )
        actions = []
        for i in range(3):
            actions += eng.process_join(u.join(u.member(i, age_days=1), u.NOW + i))
        # A 4th suspicious joiner arrives during the ongoing raid.
        actions += eng.process_join(u.join(u.member(3, age_days=1), u.NOW + 4))
        kicked = u.targets(actions, ActionType.KICK_MEMBER)
        self.assertEqual(sorted(kicked), [0, 1, 2, 3])
        self.assertEqual(len(kicked), len(set(kicked)))  # no duplicates


class TestRaidActionModes(unittest.TestCase):
    def _run(self, action: RaidAction):
        eng = engine(
            join_rate_threshold=3,
            join_window_seconds=10,
            raid_only_suspicious=False,
            raid_action=action,
        )
        actions = []
        for i in range(3):
            actions += eng.process_join(u.join(u.member(i, age_days=400), u.NOW + i))
        return actions

    def test_kick_mode(self):
        self.assertTrue(u.has(self._run(RaidAction.KICK), ActionType.KICK_MEMBER))

    def test_quarantine_mode(self):
        self.assertTrue(
            u.has(self._run(RaidAction.QUARANTINE), ActionType.QUARANTINE_MEMBER)
        )

    def test_alert_only_mode_takes_no_destructive_action(self):
        actions = self._run(RaidAction.ALERT)
        self.assertFalse(u.has(actions, ActionType.BAN_MEMBER))
        self.assertFalse(u.has(actions, ActionType.KICK_MEMBER))
        self.assertTrue(u.has(actions, ActionType.ALERT))


class TestCoordinatedNames(unittest.TestCase):
    def test_similar_names_trigger_below_rate(self):
        # Rate threshold is high; only the name cluster should trip it.
        eng = engine(
            join_rate_threshold=100,
            join_window_seconds=60,
            similar_name_threshold=4,
        )
        names = ["Raider01", "raider_02", "RAIDER-3", "Raider004"]
        actions = []
        for i, n in enumerate(names):
            actions += eng.process_join(u.join(u.member(i, name=n, age_days=400), u.NOW + i))
        self.assertTrue(eng.is_raid_active(u.GUILD))
        self.assertTrue(u.has(actions, ActionType.ENABLE_LOCKDOWN))

    def test_distinct_names_do_not_trigger(self):
        eng = engine(
            join_rate_threshold=100, join_window_seconds=60, similar_name_threshold=4
        )
        names = ["alice", "bob", "carol", "dave", "erin"]
        for i, n in enumerate(names):
            eng.process_join(u.join(u.member(i, name=n, age_days=400), u.NOW + i))
        self.assertFalse(eng.is_raid_active(u.GUILD))

    def test_homoglyph_and_leet_names_still_cluster(self):
        # Raiders disguising the same name with Cyrillic / full-width / leet
        # must still be recognised as a coordinated cluster.
        eng = engine(
            join_rate_threshold=100, join_window_seconds=60, similar_name_threshold=4
        )
        names = ["Raider", "R4ider", "Rаider", "Ｒａｉｄｅｒ"]  # latin/leet/cyrillic/fullwidth
        actions = []
        for i, n in enumerate(names):
            actions += eng.process_join(
                u.join(u.member(i, name=n, age_days=400), u.NOW + i)
            )
        self.assertTrue(eng.is_raid_active(u.GUILD))
        self.assertTrue(u.has(actions, ActionType.ENABLE_LOCKDOWN))


class TestAccountAgeGate(unittest.TestCase):
    def test_peacetime_gate_quarantines_new_accounts(self):
        eng = engine(
            join_rate_threshold=100,
            min_account_age_days=7,
            enforce_account_age_outside_raid=True,
        )
        actions = eng.process_join(u.join(u.member(1, age_days=0.5), u.NOW))
        self.assertTrue(u.has(actions, ActionType.QUARANTINE_MEMBER))

    def test_peacetime_gate_off_by_default(self):
        eng = engine(join_rate_threshold=100, min_account_age_days=7)
        actions = eng.process_join(u.join(u.member(1, age_days=0.5), u.NOW))
        self.assertEqual(actions, [])


class TestExemptions(unittest.TestCase):
    def test_owner_never_counts_or_actioned(self):
        eng = engine(join_rate_threshold=3, join_window_seconds=10,
                     raid_only_suspicious=False, raid_action=RaidAction.BAN)
        actions = []
        for i in range(2):
            actions += eng.process_join(u.join(u.member(i, age_days=1), u.NOW + i))
        # Owner joins — must not trip the 3rd slot, must not be banned.
        actions += eng.process_join(
            u.join(u.member(50, age_days=0.1, is_owner=True), u.NOW + 2)
        )
        self.assertFalse(eng.is_raid_active(u.GUILD))
        self.assertNotIn(50, u.targets(actions, ActionType.BAN_MEMBER))

    def test_trusted_role_exempt(self):
        eng = engine(
            join_rate_threshold=3,
            join_window_seconds=10,
            trusted_roles=frozenset({777}),
        )
        for i in range(2):
            eng.process_join(u.join(u.member(i, age_days=1), u.NOW + i))
        eng.process_join(u.join(u.member(9, age_days=1, roles=(777,)), u.NOW + 2))
        self.assertFalse(eng.is_raid_active(u.GUILD))

    def test_disabled_config_is_noop(self):
        eng = engine(enabled=False, join_rate_threshold=2, join_window_seconds=10)
        actions = []
        for i in range(5):
            actions += eng.process_join(u.join(u.member(i, age_days=0.1), u.NOW + i))
        self.assertEqual(actions, [])
        self.assertFalse(eng.is_raid_active(u.GUILD))


if __name__ == "__main__":
    unittest.main()
