"""Tests for raid/lockdown lifecycle (auto de-escalation via tick)."""

import unittest

from antiraid.actions import ActionType
from antiraid.config import GuildConfig
from antiraid.engine import AntiRaidEngine
from tests import _util as u


def engine(**overrides) -> AntiRaidEngine:
    eng = AntiRaidEngine()
    eng.set_config(GuildConfig(guild_id=u.GUILD, **overrides))
    return eng


class TestLifecycle(unittest.TestCase):
    def _start_raid(self, eng):
        for i in range(5):
            eng.process_join(u.join(u.member(i, age_days=400), u.NOW + i))

    def test_tick_lifts_after_cooldown(self):
        eng = engine(
            join_rate_threshold=5, join_window_seconds=10, raid_cooldown_seconds=120
        )
        self._start_raid(eng)
        self.assertTrue(eng.is_raid_active(u.GUILD))

        # Not yet past cooldown: nothing happens.
        early = eng.tick(u.GUILD, u.NOW + 4 + 60)
        self.assertEqual(early, [])
        self.assertTrue(eng.is_raid_active(u.GUILD))

        # Past cooldown: lockdown lifted, verification lowered, alert sent.
        late = eng.tick(u.GUILD, u.NOW + 4 + 121)
        self.assertFalse(eng.is_raid_active(u.GUILD))
        self.assertFalse(eng.is_locked_down(u.GUILD))
        self.assertTrue(u.has(late, ActionType.DISABLE_LOCKDOWN))
        self.assertTrue(u.has(late, ActionType.LOWER_VERIFICATION))

    def test_continued_activity_resets_cooldown(self):
        eng = engine(
            join_rate_threshold=5, join_window_seconds=60, raid_cooldown_seconds=120
        )
        self._start_raid(eng)
        # A new joiner keeps the raid alive at NOW+100.
        eng.process_join(u.join(u.member(50, age_days=400), u.NOW + 100))
        # NOW+150 is only 50s after the last signal -> still locked.
        eng.tick(u.GUILD, u.NOW + 150)
        self.assertTrue(eng.is_raid_active(u.GUILD))

    def test_tick_noop_when_calm(self):
        eng = engine()
        self.assertEqual(eng.tick(u.GUILD, u.NOW + 9999), [])

    def test_tick_idempotent_after_lift(self):
        eng = engine(
            join_rate_threshold=5, join_window_seconds=10, raid_cooldown_seconds=10
        )
        self._start_raid(eng)
        first = eng.tick(u.GUILD, u.NOW + 4 + 11)
        second = eng.tick(u.GUILD, u.NOW + 4 + 12)
        self.assertTrue(first)  # lifted on first call
        self.assertEqual(second, [])  # nothing left to do


if __name__ == "__main__":
    unittest.main()
