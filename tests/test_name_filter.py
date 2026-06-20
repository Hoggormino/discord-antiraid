"""Tests for the AutoMod-style username filter on join."""

import unittest

from antiraid.actions import ActionType
from antiraid.config import GuildConfig, RaidAction
from antiraid.engine import AntiRaidEngine
from tests import _util as u


def engine(**overrides) -> AntiRaidEngine:
    params = dict(join_rate_threshold=100, similar_name_threshold=100)
    params.update(overrides)  # let individual tests override the defaults
    eng = AntiRaidEngine()
    eng.set_config(GuildConfig(guild_id=u.GUILD, **params))
    return eng


class TestNameFilter(unittest.TestCase):
    def test_matching_name_is_actioned_without_a_raid(self):
        eng = engine(
            banned_name_patterns=frozenset({r"free.?nitro"}),
            name_filter_action=RaidAction.BAN,
        )
        actions = eng.process_join(
            u.join(u.member(1, name="FREE NITRO giveaway", age_days=400), u.NOW)
        )
        self.assertTrue(u.has(actions, ActionType.BAN_MEMBER))
        self.assertFalse(eng.is_raid_active(u.GUILD))  # name filter != raid

    def test_default_action_is_quarantine(self):
        eng = engine(banned_name_patterns=frozenset({r"scam"}))
        actions = eng.process_join(
            u.join(u.member(1, name="scammer", age_days=400), u.NOW)
        )
        self.assertTrue(u.has(actions, ActionType.QUARANTINE_MEMBER))

    def test_matches_normalised_skeleton(self):
        # leet/spaced name should still match a plain pattern via the skeleton
        eng = engine(
            banned_name_patterns=frozenset({r"raider"}),
            name_filter_action=RaidAction.KICK,
        )
        actions = eng.process_join(
            u.join(u.member(1, name="R4 i d3 r", age_days=400), u.NOW)
        )
        self.assertTrue(u.has(actions, ActionType.KICK_MEMBER))

    def test_non_matching_name_untouched(self):
        eng = engine(banned_name_patterns=frozenset({r"scam"}))
        actions = eng.process_join(
            u.join(u.member(1, name="alice", age_days=400), u.NOW)
        )
        self.assertEqual(actions, [])

    def test_exempt_member_skips_filter(self):
        eng = engine(
            banned_name_patterns=frozenset({r"scam"}),
            trusted_roles=frozenset({9}),
        )
        actions = eng.process_join(
            u.join(u.member(1, name="scambot", roles=(9,), age_days=400), u.NOW)
        )
        self.assertEqual(actions, [])

    def test_invalid_pattern_is_skipped_not_crashing(self):
        eng = engine(
            banned_name_patterns=frozenset({r"(unclosed", r"scam"}),
            name_filter_action=RaidAction.BAN,
        )
        actions = eng.process_join(
            u.join(u.member(1, name="scammer", age_days=400), u.NOW)
        )
        self.assertTrue(u.has(actions, ActionType.BAN_MEMBER))  # valid pattern still works

    def test_each_member_actioned_at_most_once_during_raid(self):
        # When both the name filter and the raid sweep apply, no member may be
        # actioned twice (e.g. quarantined AND banned).
        eng = engine(
            join_rate_threshold=3,
            join_window_seconds=30,
            raid_only_suspicious=False,
            raid_action=RaidAction.BAN,
            banned_name_patterns=frozenset({r"raider"}),
            name_filter_action=RaidAction.QUARANTINE,
        )
        actions = []
        for i in range(3):
            actions += eng.process_join(
                u.join(u.member(i, name=f"Raider{i}", age_days=400), u.NOW + i)
            )
        enforced = [
            a.target_id for a in actions
            if a.type in {ActionType.BAN_MEMBER, ActionType.KICK_MEMBER,
                          ActionType.QUARANTINE_MEMBER}
        ]
        self.assertEqual(sorted(enforced), [0, 1, 2])          # all three handled
        self.assertEqual(len(enforced), len(set(enforced)))    # none handled twice


if __name__ == "__main__":
    unittest.main()
