"""Tests for engine wiring details and action value semantics."""

import unittest

from antiraid.actions import Action, ActionType, Severity
from antiraid.config import GuildConfig, RaidAction
from antiraid.engine import AntiRaidEngine
from tests import _util as u


def engine(**overrides) -> AntiRaidEngine:
    eng = AntiRaidEngine()
    eng.set_config(GuildConfig(guild_id=u.GUILD, **overrides))
    return eng


class TestConfigProvider(unittest.TestCase):
    def test_provider_called_once_then_cached(self):
        calls = []

        def provider(gid):
            calls.append(gid)
            return GuildConfig(guild_id=gid, join_rate_threshold=3)

        eng = AntiRaidEngine(config_provider=provider)
        self.assertEqual(eng.config_for(55).join_rate_threshold, 3)
        eng.config_for(55)  # cached -> provider not called again
        self.assertEqual(calls, [55])

    def test_default_config_without_provider(self):
        eng = AntiRaidEngine()
        self.assertEqual(eng.config_for(7).guild_id, 7)


class TestRejoinDedup(unittest.TestCase):
    def test_kicked_member_not_reactioned_in_same_raid(self):
        eng = engine(
            join_rate_threshold=3,
            join_window_seconds=120,
            raid_only_suspicious=False,
            raid_action=RaidAction.KICK,
        )
        actions = []
        for i in range(3):
            actions += eng.process_join(u.join(u.member(i, age_days=400), u.NOW + i))
        self.assertEqual(sorted(u.targets(actions, ActionType.KICK_MEMBER)), [0, 1, 2])

        # member 0 (kicked) rejoins while the raid is still active.
        again = eng.process_join(u.join(u.member(0, age_days=400), u.NOW + 5))
        # already actioned this raid -> not kicked a second time.
        self.assertNotIn(0, u.targets(again, ActionType.KICK_MEMBER))

        # ...but after the raid clears, the dedup memory resets.
        eng.tick(u.GUILD, u.NOW + 5 + 200)
        self.assertFalse(eng.is_raid_active(u.GUILD))


class TestActionSemantics(unittest.TestCase):
    def test_str_contains_type_and_target(self):
        a = Action(ActionType.BAN_MEMBER, 1, target_id=42, reason="raid")
        self.assertIn("ban_member", str(a))
        self.assertIn("42", str(a))

    def test_severity_ordering(self):
        self.assertTrue(Severity.LOW < Severity.HIGH)
        self.assertEqual(
            max([Severity.LOW, Severity.CRITICAL, Severity.MEDIUM]),
            Severity.CRITICAL,
        )

    def test_severity_lt_rejects_non_severity(self):
        self.assertIs(Severity.LOW.__lt__(5), NotImplemented)


if __name__ == "__main__":
    unittest.main()
