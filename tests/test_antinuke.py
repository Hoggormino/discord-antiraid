"""Tests for the anti-nuke (compromised admin / rogue bot) layer."""

import unittest

from antiraid.actions import ActionType
from antiraid.config import GuildConfig
from antiraid.engine import AntiRaidEngine
from antiraid.models import AuditAction
from tests import _util as u


def engine(**overrides) -> AntiRaidEngine:
    eng = AntiRaidEngine()
    eng.set_config(GuildConfig(guild_id=u.GUILD, **overrides))
    return eng


class TestAntiNuke(unittest.TestCase):
    def test_mass_channel_delete_strips_actor(self):
        eng = engine(nuke_threshold=4, nuke_window=10)
        actor = u.member(500, name="roguemod")
        actions = []
        for i in range(4):
            actions += eng.process_audit(
                u.audit(actor, AuditAction.CHANNEL_DELETE, u.NOW + i, target=i)
            )
        self.assertTrue(u.has(actions, ActionType.STRIP_ACTOR_PERMISSIONS))
        self.assertTrue(u.has(actions, ActionType.ALERT))
        self.assertIn(500, u.targets(actions, ActionType.STRIP_ACTOR_PERMISSIONS))

    def test_below_threshold_safe(self):
        eng = engine(nuke_threshold=4, nuke_window=10)
        actor = u.member(500)
        actions = []
        for i in range(3):
            actions += eng.process_audit(
                u.audit(actor, AuditAction.BAN, u.NOW + i, target=i)
            )
        self.assertFalse(u.has(actions, ActionType.STRIP_ACTOR_PERMISSIONS))

    def test_actions_spread_out_safe(self):
        eng = engine(nuke_threshold=4, nuke_window=10)
        actor = u.member(500)
        actions = []
        for i in range(6):
            actions += eng.process_audit(
                u.audit(actor, AuditAction.CHANNEL_DELETE, u.NOW + i * 5, target=i)
            )
        self.assertFalse(u.has(actions, ActionType.STRIP_ACTOR_PERMISSIONS))

    def test_allowlisted_bot_bypasses(self):
        eng = engine(nuke_threshold=4, nuke_window=10,
                     allowlist_bots=frozenset({500}))
        actor = u.member(500, is_bot=True)
        actions = []
        for i in range(6):
            actions += eng.process_audit(
                u.audit(actor, AuditAction.CHANNEL_DELETE, u.NOW + i, target=i)
            )
        self.assertEqual(actions, [])

    def test_owner_bypasses(self):
        eng = engine(nuke_threshold=4, nuke_window=10)
        actor = u.member(500, is_owner=True)
        actions = []
        for i in range(6):
            actions += eng.process_audit(
                u.audit(actor, AuditAction.ROLE_DELETE, u.NOW + i, target=i)
            )
        self.assertEqual(actions, [])

    def test_strip_only_emitted_once(self):
        eng = engine(nuke_threshold=4, nuke_window=30)
        actor = u.member(500)
        actions = []
        for i in range(8):
            actions += eng.process_audit(
                u.audit(actor, AuditAction.CHANNEL_DELETE, u.NOW + i, target=i)
            )
        strips = u.targets(actions, ActionType.STRIP_ACTOR_PERMISSIONS)
        self.assertEqual(strips, [500])  # exactly one strip, not repeated

    def test_antinuke_disabled(self):
        eng = engine(nuke_threshold=4, nuke_window=10, antinuke_enabled=False)
        actor = u.member(500)
        actions = []
        for i in range(6):
            actions += eng.process_audit(
                u.audit(actor, AuditAction.CHANNEL_DELETE, u.NOW + i, target=i)
            )
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
