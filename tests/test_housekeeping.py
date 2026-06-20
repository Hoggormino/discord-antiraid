"""Tests that runtime state stays bounded and housekeeping is non-destructive."""

import unittest

from antiraid.config import GuildConfig
from antiraid.engine import AntiRaidEngine
from tests import _util as u


def engine(**overrides) -> AntiRaidEngine:
    eng = AntiRaidEngine()
    eng.set_config(GuildConfig(guild_id=u.GUILD, **overrides))
    return eng


class TestHousekeeping(unittest.TestCase):
    def test_unique_messages_do_not_leak(self):
        eng = engine(msg_rate_threshold=100, cross_user_window=10)
        state = eng.store.get(u.GUILD)
        t = u.NOW
        # 500 distinct users each sending a distinct message.
        for i in range(500):
            eng.process_message(u.message(u.member(i), f"unique-{i}", t, mid=i))
            t += 1
        self.assertGreater(len(state.content_authors), 0)
        self.assertGreater(len(state.user_msgs), 0)

        # A housekeeping tick well after every window has elapsed.
        eng.tick(u.GUILD, t + 10_000)
        self.assertEqual(len(state.content_authors), 0)
        self.assertEqual(len(state.user_msgs), 0)
        self.assertEqual(len(state.user_dupes), 0)
        self.assertEqual(len(state.flagged_content), 0)

    def test_housekeep_preserves_live_window(self):
        # Housekeeping must not drop events still inside their window.
        eng = engine(msg_rate_threshold=6, msg_rate_window=5)
        m = u.member(1)
        t = u.NOW
        for i in range(3):
            eng.process_message(u.message(m, f"hi {i}", t + i, mid=i))
        # Tick at t+4 (still within the 5s window): data must survive...
        eng.tick(u.GUILD, t + 4)
        # ...so 3 more rapid messages still cross the flood threshold.
        actions = []
        for i in range(3, 6):
            actions += eng.process_message(u.message(m, f"hi {i}", t + 4 + i * 0.1, mid=i))
        from antiraid.actions import ActionType
        self.assertTrue(u.has(actions, ActionType.TIMEOUT_MEMBER))

    def test_audit_buckets_reclaimed(self):
        from antiraid.models import AuditAction
        eng = engine(nuke_threshold=10, nuke_window=10)
        state = eng.store.get(u.GUILD)
        actor = u.member(5)
        for i in range(3):
            eng.process_audit(u.audit(actor, AuditAction.BAN, u.NOW + i, target=i))
        self.assertIn(5, state.actor_audit)
        eng.tick(u.GUILD, u.NOW + 1000)
        self.assertNotIn(5, state.actor_audit)


if __name__ == "__main__":
    unittest.main()
